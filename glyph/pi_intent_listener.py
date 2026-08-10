#!/usr/bin/env python3
"""
glyph/pi_intent_listener.py — receives signed intent packets over the
Pi's WireGuard interface only, verifies them, waits for the requested
trigger time, then executes the intent. No interactive shell involved.

Runs on the Pi. Rejects: bad signature, stale trigger_at (too far in the
past or too far in the future), and reused nonces (replay defence).

Usage:
    python3 glyph/pi_intent_listener.py <bind_ip> [bind_port]

Example:
    python3 glyph/pi_intent_listener.py 10.8.0.5 58601
"""
import subprocess
import sys
import threading
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from pi_intent_common import load_key, unpack, pack_response, ACCEPTED, REJECTED, DONE_OK, DONE_FAIL
from truthd_client import verb_allowed
import socket

BIND_IP     = sys.argv[1] if len(sys.argv) > 1 else "10.8.0.5"
BIND_PORT   = int(sys.argv[2]) if len(sys.argv) > 2 else 58601
MAX_FUTURE_S  = 3600     # refuse triggers scheduled more than 1h out
MAX_PAST_S    = 30       # refuse packets whose trigger_at is already this stale
SEEN_NONCE_TTL = 3600

INTENTS = {
    "uptime": ["uptime"],
    "whoami": ["sudo", "-n", "whoami"],
    "reboot": ["sudo", "-n", "reboot"],
    "restart_wg": ["sudo", "-n", "bash", "-c", "wg-quick down wg0 && wg-quick up wg0"],
    "restart_container": ["docker", "restart", "portainer"],
}

# Gate hook (see ../truthd.c, truthd_client.py) -- verb class per intent,
# taken from ../truth_manifest.h's VERB_NAMES. "exec:" is classified
# LOCAL_DESTRUCTIVE (the most restrictive local class) since its actual
# command isn't known ahead of time -- see the EXEC_PREFIX comment below
# on why the wire format is generic but sudoers still caps what it can do.
INTENT_VERB_CLASS = {
    "uptime": "BENIGN_READ",
    "whoami": "BENIGN_READ",
    "reboot": "LOCAL_DESTRUCTIVE",
    "restart_wg": "LOCAL_HEAL",
    "restart_container": "LOCAL_HEAL",
}
EXEC_VERB_CLASS = "LOCAL_DESTRUCTIVE"

# "exec:<shell command>" is accepted at the protocol layer for any signed,
# correctly-timed, non-replayed packet -- but that's not the real security
# boundary. sudo -n only succeeds without a password for the exact command
# string(s) listed in /etc/sudoers.d/pi-intent on the Pi; any other exec:
# payload still passes signature/nonce/trigger checks (so it prints
# ACCEPTED) but sudo then demands a password non-interactively and the run
# fails closed with DONE_FAIL. So this is generic at the wire format but
# capped to specific pre-approved commands in practice, same shape as
# restart_wg's fixed bash -c string above.
EXEC_PREFIX = "exec:"
PAUSE_FLAG_PATH = "/tmp/watchdog.pause"

_seen_nonces = {}   # nonce -> expiry time
_seen_lock = threading.Lock()


def _prune_nonces(now):
    dead = [n for n, exp in _seen_nonces.items() if exp < now]
    for n in dead:
        del _seen_nonces[n]


def _pause_watchdog():
    subprocess.run(["touch", PAUSE_FLAG_PATH])
    print(f"[listener] watchdog paused ({PAUSE_FLAG_PATH})", flush=True)


def _resume_watchdog_and_refresh():
    # Detached via setsid, but that only protects against signal
    # propagation, not systemd's default KillMode=control-group -- when
    # "systemctl restart pi-intent-listener" stops the service, systemd
    # tears down the *whole cgroup*, including this spawned script,
    # wherever it's up to. So "systemctl restart" must be the LAST
    # command here: everything before it is guaranteed to run, nothing
    # after it is (confirmed live: an earlier version put it first and
    # the trailing `rm -f {PAUSE_FLAG_PATH}` silently never ran, leaving
    # the watchdog stuck paused).
    script = (
        "sleep 2; "
        "sudo -n bash -c 'wg-quick down wg0 && wg-quick up wg0'; "
        f"rm -f {PAUSE_FLAG_PATH}; "
        "sudo -n systemctl restart pi-intent-listener"
    )
    subprocess.Popen(["setsid", "bash", "-c", script],
                      start_new_session=True,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[listener] scheduled listener+wg0 refresh and watchdog resume", flush=True)


def _run_at(intent, trigger_at, cmd, nonce, addr, sock, key, verb_class):
    delay = trigger_at - time.time()
    if delay > 0:
        print(f"[listener] waiting {delay:.2f}s to run intent={intent!r}", flush=True)
        time.sleep(delay)

    # Gate check happens here, not at accept time -- trigger_at can be up
    # to MAX_FUTURE_S out, and the tier is allowed to change during that
    # wait. The gate has to reflect quorum at execution time, not at the
    # moment the packet arrived.
    allowed, detail = verb_allowed(verb_class)
    if not allowed:
        print(f"[listener] GATE DENIED intent={intent!r} verb_class={verb_class} "
              f"({detail}) -- treated as noise, not executed", flush=True)
        try:
            sock.sendto(pack_response(nonce, REJECTED, f"gate: {detail}", key), addr)
        except OSError:
            pass
        return

    is_exec = intent.startswith(EXEC_PREFIX)
    if is_exec:
        _pause_watchdog()
    print(f"[listener] EXECUTING intent={intent!r}: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[listener] exit={result.returncode} stdout={result.stdout.strip()!r} "
          f"stderr={result.stderr.strip()!r}", flush=True)
    status = DONE_OK if result.returncode == 0 else DONE_FAIL
    detail = result.stdout.strip() or result.stderr.strip() or f"exit={result.returncode}"
    try:
        sock.sendto(pack_response(nonce, status, detail, key), addr)
    except OSError:
        pass  # e.g. reboot tore the interface down before the response went out
    if is_exec:
        _resume_watchdog_and_refresh()


def main():
    key = load_key()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND_IP, BIND_PORT))
    print(f"[listener] bound {BIND_IP}:{BIND_PORT}, intents={list(INTENTS)}", flush=True)

    while True:
        data, addr = sock.recvfrom(256)
        now = time.time()
        try:
            intent, trigger_at, nonce = unpack(data, key)
        except ValueError as e:
            print(f"[listener] REJECTED from {addr}: {e}", flush=True)
            continue

        if intent.startswith(EXEC_PREFIX):
            shell_cmd = intent[len(EXEC_PREFIX):]
            if not shell_cmd:
                print(f"[listener] REJECTED empty exec command from {addr}", flush=True)
                sock.sendto(pack_response(nonce, REJECTED, "empty exec command", key), addr)
                continue
            cmd = ["sudo", "-n", "bash", "-c", shell_cmd]
            verb_class = EXEC_VERB_CLASS
        elif intent in INTENTS:
            cmd = INTENTS[intent]
            verb_class = INTENT_VERB_CLASS[intent]
        else:
            print(f"[listener] REJECTED unknown intent={intent!r} from {addr}", flush=True)
            sock.sendto(pack_response(nonce, REJECTED, f"unknown intent {intent!r}", key), addr)
            continue

        if trigger_at < now - MAX_PAST_S or trigger_at > now + MAX_FUTURE_S:
            print(f"[listener] REJECTED stale/out-of-range trigger_at={trigger_at} "
                  f"(now={now}) from {addr}", flush=True)
            sock.sendto(pack_response(nonce, REJECTED, "trigger_at out of range", key), addr)
            continue

        with _seen_lock:
            _prune_nonces(now)
            if nonce in _seen_nonces:
                print(f"[listener] REJECTED replay nonce={nonce} from {addr}", flush=True)
                sock.sendto(pack_response(nonce, REJECTED, "replayed nonce", key), addr)
                continue
            _seen_nonces[nonce] = now + SEEN_NONCE_TTL

        print(f"[listener] ACCEPTED intent={intent!r} trigger_at={trigger_at:.3f} "
              f"from {addr}", flush=True)
        sock.sendto(pack_response(nonce, ACCEPTED, "scheduled", key), addr)
        threading.Thread(target=_run_at,
                          args=(intent, trigger_at, cmd, nonce, addr, sock, key, verb_class),
                          daemon=True).start()


if __name__ == "__main__":
    main()
