#!/usr/bin/env python3
"""
glyph/ec2_intent_listener.py — receives signed intent packets, verifies
them, waits for the requested trigger time, then executes the intent.

Runs on EC2. Rejects: bad signature, stale trigger_at (too far in the
past or too far in the future), and reused nonces (replay defence).

Bind IP defaults to EC2's wg0 address (10.8.0.1) so the channel is
WireGuard-only, same "no raw internet exposure" pattern already used by
pi_intent_listener.py -- defense in depth alongside the ufw rule scoping
58551/udp to 10.8.0.0/24.

Usage:
    python3 glyph/ec2_intent_listener.py [bind_ip] [bind_port]
"""
import subprocess
import sys
import threading
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ec2_intent_common import load_key, unpack, pack_response, ACCEPTED, REJECTED, DONE_OK, DONE_FAIL
from truthd_client import verb_allowed
import socket

BIND_IP     = sys.argv[1] if len(sys.argv) > 1 else "10.8.0.1"
BIND_PORT   = int(sys.argv[2]) if len(sys.argv) > 2 else 58551
MAX_FUTURE_S  = 3600     # refuse triggers scheduled more than 1h out
MAX_PAST_S    = 30       # refuse packets whose trigger_at is already this stale
SEEN_NONCE_TTL = 3600

INTENTS = {
    "uptime":          ["uptime"],
    "status":          ["sudo", "docker", "ps", "--filter", "name=wg-easy",
                         "--format", "{{.Names}}: {{.Status}}"],
    "start_wg_easy":   ["sudo", "docker", "start", "wg-easy"],
    "restart_wg_easy": ["sudo", "docker", "restart", "wg-easy"],
    "stop_wg_easy":    ["sudo", "docker", "stop", "wg-easy"],
}

# Gate hook (see ../truthd.c, truthd_client.py, ../truth_manifest.h).
# "uptime" stays BENIGN_READ (always allowed, every tier) -- it's also
# what ec2_probe.py uses as the reachability signal truthd needs before
# it can ever report TIER_FULL, so it can't be gated by the tier it
# helps compute.
INTENT_VERB_CLASS = {
    "uptime": "BENIGN_READ",
    "status": "EC2_SELF",
    "start_wg_easy": "EC2_SELF",
    "restart_wg_easy": "EC2_SELF",
    "stop_wg_easy": "EC2_SELF",
}

# glyph/truth_manifest.h's TIER_NAMES, duplicated here only as a
# validation whitelist for witness reports -- see WITNESS_PATH below.
KNOWN_TIER_NAMES = {"TIER_FULL", "TIER_LOCAL_TRIAD", "TIER_PARTITIONED"}
WITNESS_PREFIX = "witness:"
WITNESS_PATH = "/tmp/mint_witness.json"

_seen_nonces = {}   # nonce -> expiry time
_seen_lock = threading.Lock()


def _prune_nonces(now):
    dead = [n for n, exp in _seen_nonces.items() if exp < now]
    for n in dead:
        del _seen_nonces[n]


def _write_witness(tier_name, received_at):
    import json
    tmp_path = WITNESS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"triad_tier": tier_name, "reported_at": received_at}, f)
    __import__("os").rename(tmp_path, WITNESS_PATH)


def _run_at(intent, trigger_at, cmd, nonce, addr, sock, key, verb_class):
    delay = trigger_at - time.time()
    if delay > 0:
        print(f"[listener] waiting {delay:.2f}s to run intent={intent!r}", flush=True)
        time.sleep(delay)

    allowed, detail = verb_allowed(verb_class)
    if not allowed:
        print(f"[listener] GATE DENIED intent={intent!r} verb_class={verb_class} "
              f"({detail}) -- treated as noise, not executed", flush=True)
        try:
            sock.sendto(pack_response(nonce, REJECTED, f"gate: {detail}", key), addr)
        except OSError:
            pass
        return

    print(f"[listener] EXECUTING intent={intent!r}: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[listener] exit={result.returncode} stdout={result.stdout.strip()!r} "
          f"stderr={result.stderr.strip()!r}", flush=True)
    status = DONE_OK if result.returncode == 0 else DONE_FAIL
    detail = result.stdout.strip() or result.stderr.strip() or f"exit={result.returncode}"
    sock.sendto(pack_response(nonce, status, detail, key), addr)


def main():
    key = load_key()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND_IP, BIND_PORT))
    print(f"[listener] bound {BIND_IP}:{BIND_PORT}, intents={list(INTENTS)}, "
          f"witness_prefix={WITNESS_PREFIX!r}", flush=True)

    while True:
        data, addr = sock.recvfrom(256)
        now = time.time()
        try:
            intent, trigger_at, nonce = unpack(data, key)
        except ValueError as e:
            print(f"[listener] REJECTED from {addr}: {e}", flush=True)
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

        # Witness reports are a distinct control path, not a subprocess
        # verb -- same signature/freshness/replay checks as everything
        # else above, but they never touch INTENTS or the gate. They can
        # only ever narrow what this host later grants itself (see
        # truthd.c's --witness mode): a forged or replayed-but-old
        # report just makes EC2 look more degraded, never less.
        if intent.startswith(WITNESS_PREFIX):
            claimed_tier = intent[len(WITNESS_PREFIX):]
            if claimed_tier not in KNOWN_TIER_NAMES:
                print(f"[listener] REJECTED malformed witness tier={claimed_tier!r} "
                      f"from {addr}", flush=True)
                sock.sendto(pack_response(nonce, REJECTED, "bad witness tier", key), addr)
                continue
            _write_witness(claimed_tier, now)
            print(f"[listener] WITNESS recorded triad_tier={claimed_tier} from {addr}",
                  flush=True)
            sock.sendto(pack_response(nonce, DONE_OK, "witness recorded", key), addr)
            continue

        if intent not in INTENTS:
            print(f"[listener] REJECTED unknown intent={intent!r} from {addr}", flush=True)
            sock.sendto(pack_response(nonce, REJECTED, f"unknown intent {intent!r}", key), addr)
            continue

        print(f"[listener] ACCEPTED intent={intent!r} trigger_at={trigger_at:.3f} "
              f"from {addr}", flush=True)
        sock.sendto(pack_response(nonce, ACCEPTED, "scheduled", key), addr)
        threading.Thread(target=_run_at,
                          args=(intent, trigger_at, INTENTS[intent], nonce, addr, sock, key,
                                INTENT_VERB_CLASS[intent]),
                          daemon=True).start()


if __name__ == "__main__":
    main()
