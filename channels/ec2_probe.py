#!/usr/bin/env python3
"""
glyph/ec2_probe.py — Mint-only EC2 reachability sensor for truthd.

quartz_node's Kuramoto substrate never included EC2 (LAN-only,
Mint/Pi1/Pi2 -- see quartz-substrate/README.md), so there's no live
phase signal for it. This is the discrete, polled substitute: every
PROBE_INTERVAL_S, send the existing signed "uptime" intent
(ec2_intent_common.py, BENIGN_READ per ec2_intent_listener.py's
INTENT_VERB_CLASS -- always allowed, so the probe is never gated by the
tier it's helping compute) and wait for a DONE_OK. Purely observational:
the result never feeds back into anything, it only gets written to
EC2_REACHABLE_PATH for truthd to read (same "sensor reading in, gate
decision out" split as the rest of this project -- see
project_signal_vs_actuation in memory).

Same cycle also sends a signed "witness:<TIER>" report carrying Mint's
own currently-observed local-triad tier (from Mint's own truthd, see
truthd_client.current_tier()), for EC2's truthd (run as `./truthd
--witness`, see truthd.c) to use in place of the Kuramoto health file
it can never have. This is a trust shift, not a stronger guarantee --
EC2's gate now depends on whoever holds ec2_intent.key telling the
truth, same as every other command in this system. It can only narrow
what EC2 grants itself, never grant more than TRUTH_ALLOWED already
permits.

Only meaningful run on Mint: it's the only host holding ec2_intent.key.
Running this elsewhere would just fail to load the key.

Talks to EC2's wg0 address (10.8.0.1), not the public IP -- the intent
channel is firewalled to 10.8.0.0/24 only, no raw internet exposure.

Writes (atomic tmp+rename, same pattern as quartz_node.c's write_health):
  /tmp/ec2_reachable.json: {"reachable": true|false, "checked_at": <epoch>, "detail": "..."}

Relays that same file to Pi1/Pi2 2026-08-14 -- found live that TIER_FULL
was structurally unreachable from either Pi's OWN truthd, not because
this probe was broken (it wasn't -- 5 days uptime, fresh reachable=true
the whole time) but because truthd.c reads EC2_REACHABLE_PATH from the
LOCAL filesystem of whichever host it's running on, and this probe only
ever wrote it on Mint. Pi1/Pi2 had no such file at all, so their own
gates fell back to TIER_LOCAL_TRIAD forever regardless of Mint's view.
Relay uses the already-signed, already-validated shell_launch channel
(no new key, no new trust surface) to push the identical JSON body to
both Pis' local /tmp/ec2_reachable.json every cycle, same atomic
tmp+rename pattern as the local write. This widens what each Pi's own
gate can grant itself no further than TRUTH_ALLOWED already permits --
same one-way trust-narrowing shape as the witness report to EC2.

Run: python3 glyph/ec2_probe.py
"""
import json
import os
import secrets
import shlex
import socket
import sys
import time

# 2026-08-15 reorg: ec2_probe.py and shell_launch/ both now live
# directly under channels/ (siblings, not "shell_launch one dir up" the
# way it was under the old glyph/ layout) -- truthd_client.py moved to
# gate/, a sibling of channels/, so that needs its own path entry too.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gate"))
from ec2_intent_common import (load_key, pack, unpack_response,
                                ACCEPTED, DONE_OK, DONE_FAIL)
from truthd_client import current_tier

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "shell_launch"))
from shell_launch_common import (load_key as load_shell_key, send_run,
                                  PROGRESS as SHELL_PROGRESS,
                                  DONE_OK as SHELL_DONE_OK,
                                  DONE_FAIL as SHELL_DONE_FAIL)

EC2_HOST = "10.8.0.1"
EC2_PORT = 58551
PROBE_INTERVAL_S = 5.0     # cadence; EC2_STALE_S/WITNESS_STALE_S in truthd.c
                            # are 3x this
RESPONSE_TIMEOUT_S = 3.0   # generous for a LAN-adjacent-to-WAN round trip,
                            # short enough that one probe cycle can't stall
                            # the next
REACHABLE_PATH = "/tmp/ec2_reachable.json"

_SHELL_DIR = os.path.join(os.path.dirname(__file__), "shell_launch")
RELAY_TARGETS = {
    "pi1": dict(host="10.8.0.7", port=58701,
                key=os.path.join(_SHELL_DIR, "shell_launch_pi1.key")),
    "pi2": dict(host="10.8.0.5", port=58701,
                key=os.path.join(_SHELL_DIR, "shell_launch_pi2.key")),
}
RELAY_TIMEOUT_S = 5.0


def _write_result(reachable, detail):
    body = {"reachable": reachable, "checked_at": time.time(), "detail": detail}
    tmp_path = REACHABLE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(body, f)
    os.rename(tmp_path, REACHABLE_PATH)
    return body


def relay_result(node, body_json):
    cfg = RELAY_TARGETS[node]
    key = load_shell_key(cfg["key"])
    inner = (f"printf '%s' {shlex.quote(body_json)} > /tmp/ec2_reachable.json.tmp "
             f"&& mv /tmp/ec2_reachable.json.tmp /tmp/ec2_reachable.json")
    cmd = "bash -c " + shlex.quote(inner)   # listener uses shlex.split, no shell
    outcome = []

    def on_status(status, sid, detail, elapsed_s):
        if status in (SHELL_PROGRESS, SHELL_DONE_OK, SHELL_DONE_FAIL):
            outcome.append((status, detail))

    session_id = send_run(cfg["host"], cfg["port"], cmd, "", key, on_status,
                           initial_timeout_s=RELAY_TIMEOUT_S)
    if session_id is None:
        return False, "timeout"
    return (bool(outcome) and outcome[-1][0] == SHELL_DONE_OK), (outcome[-1][1] if outcome else "no response")


def send_intent(key, intent):
    """Sends one signed intent, waits for a terminal response. Returns
    (ok: bool, detail: str). Shared by the uptime probe and the witness
    report -- same envelope, different payload string."""
    nonce = secrets.randbits(64)
    trigger_at = time.time()
    pkt = pack(intent, trigger_at, nonce, key)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(RESPONSE_TIMEOUT_S)
    try:
        sock.sendto(pkt, (EC2_HOST, EC2_PORT))
        deadline = time.time() + RESPONSE_TIMEOUT_S
        while time.time() < deadline:
            sock.settimeout(max(deadline - time.time(), 0.05))
            try:
                data, _ = sock.recvfrom(512)
            except socket.timeout:
                return False, "timeout waiting for response"
            try:
                status, detail, resp_nonce = unpack_response(data, key)
            except ValueError:
                continue  # not a well-formed response for our key, ignore
            if resp_nonce != nonce:
                continue  # not ours
            if status == ACCEPTED:
                continue  # scheduled, not terminal -- keep waiting for
                          # the real DONE_OK/DONE_FAIL (witness reports
                          # skip this and respond DONE_OK directly)
            if status == DONE_OK:
                return True, detail or "ok"
            return False, f"status={status} detail={detail}"
        return False, "timeout waiting for response"
    except OSError as e:
        return False, f"send/recv error: {e}"
    finally:
        sock.close()


def send_witness(key):
    """Reports Mint's own current local-triad tier to EC2. Returns
    (ok, detail) same shape as send_intent -- purely informational for
    logging, the tier value itself is what matters on EC2's side."""
    tier = current_tier()
    if tier is None:
        return False, "local truthd unreachable, not reporting"
    ok, detail = send_intent(key, f"witness:{tier}")
    return ok, f"{tier}: {detail}"


def main():
    key = load_key()
    print(f"[ec2_probe] probing {EC2_HOST}:{EC2_PORT} every {PROBE_INTERVAL_S}s, "
          f"writing {REACHABLE_PATH}, also sending witness reports and "
          f"relaying to {list(RELAY_TARGETS)}", flush=True)
    while True:
        reachable, detail = send_intent(key, "uptime")
        body = _write_result(reachable, detail)
        print(f"[ec2_probe] reachable={reachable} ({detail})", flush=True)

        body_json = json.dumps(body)
        for node in RELAY_TARGETS:
            r_ok, r_detail = relay_result(node, body_json)
            if not r_ok:
                print(f"[ec2_probe] relay to {node} FAILED: {r_detail}", flush=True)

        w_ok, w_detail = send_witness(key)
        print(f"[ec2_probe] witness sent={w_ok} ({w_detail})", flush=True)

        time.sleep(PROBE_INTERVAL_S)


if __name__ == "__main__":
    main()
