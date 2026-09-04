#!/usr/bin/env python3
"""
glyph/pi_intent_send.py — send a signed, scheduled intent packet to a
Pi's intent listener (pi_intent_listener.py), over WireGuard.

Two Pis run this listener, each its own trust domain (separate key,
separate WireGuard IP) -- see pi_intent_common.py's PI_INTENT_KEY_FILE.
The defaults below (no env override, host=10.8.0.5) are a MATCHED PAIR
that target Pi2. Overriding just the host without also overriding the
key sends a correctly-shaped packet signed with the wrong key, which
the listener will reject with a bad-signature error, not a helpful one.

Usage:
    python3 glyph/pi_intent_send.py <intent> <seconds_from_now> [host] [port]

Example (Pi2 -- default key, default host):
    python3 glyph/pi_intent_send.py uptime 3 10.8.0.5

Example (Pi -- must override key AND host together):
    PI_INTENT_KEY_FILE=pi1_intent.key python3 glyph/pi_intent_send.py uptime 3 10.8.0.7
"""
import secrets
import socket
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from pi_intent_common import (load_key, pack, unpack_response,
                               ACCEPTED, REJECTED, DONE_OK, DONE_FAIL)

PI_PORT = 58601

STATUS_NAME = {ACCEPTED: "ACCEPTED", REJECTED: "REJECTED",
               DONE_OK: "DONE_OK", DONE_FAIL: "DONE_FAIL"}

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(f"usage: {sys.argv[0]} <intent> <seconds_from_now> [host] [port]")

    intent = sys.argv[1]
    delay = float(sys.argv[2])
    host = sys.argv[3] if len(sys.argv) > 3 else "10.8.0.5"
    port = int(sys.argv[4]) if len(sys.argv) > 4 else PI_PORT

    trigger_at = time.time() + delay
    nonce = secrets.randbits(64)
    key = load_key()
    pkt = pack(intent, trigger_at, nonce, key)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(max(delay, 0) + 15)
    sock.sendto(pkt, (host, port))
    print(f"sent intent={intent!r} trigger_at={trigger_at:.3f} "
          f"({delay:.1f}s from now) nonce={nonce} -> {host}:{port}")

    deadline = time.time() + max(delay, 0) + 15
    while time.time() < deadline:
        remaining = deadline - time.time()
        sock.settimeout(max(remaining, 0.1))
        try:
            data, _ = sock.recvfrom(512)
        except socket.timeout:
            break
        try:
            status, detail, resp_nonce = unpack_response(data, key)
        except ValueError as e:
            print(f"[recv] bad response: {e}")
            continue
        if resp_nonce != nonce:
            continue   # not ours (or a replayed/foreign packet), ignore
        name = STATUS_NAME.get(status, f"UNKNOWN({status})")
        print(f"[recv] {name}: {detail}")
        if status in (DONE_OK, DONE_FAIL, REJECTED):
            break
    sock.close()
