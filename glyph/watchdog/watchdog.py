#!/usr/bin/env python3
"""
glyph/watchdog/watchdog.py — substrate self-maintenance daemon. Runs
inside a container on the target Pi with --network host --pid host
--privileged (needs to observe and repair the host's wg0 interface and
the pi-intent-listener service, both of which live outside the
container's own namespaces).

Host-specific config comes via env vars (see deploy commands): each
target Pi has its own key (PI_INTENT_KEY_FILE, separate trust domain
per host), its own wg0 address (WATCHDOG_WG_SELF_IP), and its own set
of WireGuard interfaces to watch (WATCHDOG_WG_IFACES -- Pi2 also runs a
wg1 break-glass sandbox tunnel that Pi1 doesn't have).

Checks each cycle:
  - handshake age on every interface in WATCHDOG_WG_IFACES
    (drift/heartbeat proxy)
  - end-to-end intent round-trip on wg0 (send 'uptime' to
    WATCHDOG_WG_SELF_IP:58601, require DONE_OK within timeout) -- the
    real proof the substrate is usable, not just that the process
    exists. Non-wg0 interfaces (e.g. Pi2's wg1) have no bound service,
    so they only get the handshake-age check.

Remediation, in order:
  1. wg0/wg1 stale or down -> local wg-quick down/up <iface> (via
     nsenter into the host namespace -- can't route through the intent
     plane to fix the interface the intent plane runs on)
  2. listener not responding but wg0 healthy -> local systemctl
     restart pi-intent-listener (same reasoning)
  3. still failing after MAX_CONSECUTIVE_FAILS_BEFORE_REBOOT cycles ->
     last resort, send a real 'reboot' intent through the substrate
     itself. Safe specifically because by this point the packet was
     accepted, so wg0+listener are demonstrably healthy enough to
     carry it -- this is a genuine corrective action, not a self-goal.
"""
import os
import socket
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pi_intent_common import load_key, pack, unpack_response, DONE_OK, DONE_FAIL

WG_IFACE = "wg0"
WG_IFACES = os.environ.get("WATCHDOG_WG_IFACES", "wg0,wg1").split(",")
WG_SELF_IP = os.environ.get("WATCHDOG_WG_SELF_IP", "10.8.0.5")
INTENT_PORT = 58601
CHECK_INTERVAL_S = 30
# WireGuard's PersistentKeepalive (10s) only maintains the NAT mapping --
# it does NOT force a fresh handshake that often. The "latest handshake"
# timestamp only updates on the actual Noise rekey, which happens on its
# own roughly every 120s by protocol design. A healthy idle tunnel can
# legitimately sit at up to ~120-150s handshake age; this threshold must
# stay above that or the watchdog flags a healthy tunnel as dead every
# cycle and thrashes it (confirmed live: 40s caused a self-inflicted
# down/up loop against a tunnel that was never actually unhealthy).
HANDSHAKE_STALE_S = 150
HEARTBEAT_TIMEOUT_S = 8
MAX_CONSECUTIVE_FAILS_BEFORE_REBOOT = 3
# Set by pi_intent_listener.py while an "exec:" intent (e.g. apt upgrade)
# is running, so the watchdog doesn't fight a deliberate wg0/listener
# restart mid-upgrade by "fixing" it itself.
PAUSE_FLAG_PATH = "/tmp/watchdog.pause"


NSENTER_TIMEOUT_S = 15


def nsenter(cmd, timeout=NSENTER_TIMEOUT_S):
    full = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc",
            "--net", "--pid", "--"] + cmd
    try:
        return subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[watchdog] nsenter TIMED OUT after {timeout}s: {cmd}", flush=True)
        return subprocess.CompletedProcess(full, returncode=-1, stdout="", stderr="timeout")


def is_paused():
    r = nsenter(["test", "-f", PAUSE_FLAG_PATH])
    return r.returncode == 0


def wg_handshake_age(iface=WG_IFACE):
    r = nsenter(["wg", "show", iface, "latest-handshakes"])
    if r.returncode != 0:
        return None
    parts = r.stdout.split()
    if len(parts) < 2:
        return None
    ts = int(parts[1])
    if ts == 0:
        return None
    return time.time() - ts


def _send_intent(intent, timeout=HEARTBEAT_TIMEOUT_S):
    key = load_key()
    nonce = int.from_bytes(os.urandom(8), "big")
    pkt = pack(intent, time.time() + 1, nonce, key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(pkt, (WG_SELF_IP, INTENT_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(deadline - time.time(), 0.1))
            try:
                data, _ = sock.recvfrom(512)
            except socket.timeout:
                return False
            try:
                status, detail, resp_nonce = unpack_response(data, key)
            except ValueError:
                continue
            if resp_nonce != nonce:
                continue
            if status == DONE_OK:
                return True
            if status == DONE_FAIL:
                return False
        return False
    finally:
        sock.close()


def send_heartbeat_intent():
    return _send_intent("uptime")


def fix_wg(iface):
    print(f"[watchdog] repairing {iface} locally (down/up)", flush=True)
    nsenter(["wg-quick", "down", iface])
    r = nsenter(["wg-quick", "up", iface])
    print(f"[watchdog] {iface} fix exit={r.returncode} "
          f"{r.stdout.strip()!r} {r.stderr.strip()!r}", flush=True)


def fix_listener():
    print("[watchdog] restarting pi-intent-listener locally", flush=True)
    r = nsenter(["systemctl", "restart", "pi-intent-listener"])
    print(f"[watchdog] listener restart exit={r.returncode} "
          f"{r.stderr.strip()!r}", flush=True)


def escalate_reboot():
    print("[watchdog] local fixes exhausted -- sending reboot via substrate",
          flush=True)
    _send_intent("reboot", timeout=3)


def main():
    print(f"[watchdog] starting, interval={CHECK_INTERVAL_S}s", flush=True)
    consecutive_fails = 0
    while True:
        if is_paused():
            print("[watchdog] paused (exec intent in progress), skipping checks",
                  flush=True)
            time.sleep(CHECK_INTERVAL_S)
            continue

        for iface in WG_IFACES:
            age = wg_handshake_age(iface)
            if age is None or age > HANDSHAKE_STALE_S:
                print(f"[watchdog] {iface} unhealthy (age={age})", flush=True)
                fix_wg(iface)
                time.sleep(5)

        if send_heartbeat_intent():
            if consecutive_fails:
                print("[watchdog] heartbeat recovered", flush=True)
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            print(f"[watchdog] heartbeat FAILED ({consecutive_fails} consecutive)",
                  flush=True)
            fix_listener()
            time.sleep(5)
            if send_heartbeat_intent():
                print("[watchdog] recovered after listener restart", flush=True)
                consecutive_fails = 0
            elif consecutive_fails >= MAX_CONSECUTIVE_FAILS_BEFORE_REBOOT:
                escalate_reboot()
                consecutive_fails = 0
                time.sleep(90)

        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
