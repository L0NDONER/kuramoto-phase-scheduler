#!/usr/bin/env python3
"""
glyph/ec2_intent_send.py — send a signed, scheduled intent packet to the
EC2 listener (ec2_intent_listener.py).

Usage:
    python3 glyph/ec2_intent_send.py <intent> <seconds_from_now> [host] [port]

Example:
    python3 glyph/ec2_intent_send.py start_wg_easy 10
"""
import secrets
import socket
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ec2_intent_common import (load_key, pack, unpack_response,
                                ACCEPTED, REJECTED, DONE_OK, DONE_FAIL)

sys.path.insert(0, "/home/martin/claude/quartz_core")
from i0_phase_auth import gate_check

EC2_HOST = "10.8.0.1"   # WireGuard-only, matches ec2_intent_listener.py's BIND_IP
EC2_PORT = 58551

STATUS_NAME = {ACCEPTED: "ACCEPTED", REJECTED: "REJECTED",
               DONE_OK: "DONE_OK", DONE_FAIL: "DONE_FAIL"}

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(f"usage: {sys.argv[0]} <intent> <seconds_from_now> [host] [port]")

    intent = sys.argv[1]
    delay = float(sys.argv[2])
    host = sys.argv[3] if len(sys.argv) > 3 else EC2_HOST
    port = int(sys.argv[4]) if len(sys.argv) > 4 else EC2_PORT

    # Attestation, not authentication: the HMAC signature below is still
    # what EC2 actually trusts. This only adds "and you were physically
    # present on the LAN with a live carrier when you sent it" as a local
    # precondition -- same pattern as glyph_tx.py's transmit() gate.
    print("[ec2_intent_send] phase-auth gate...", flush=True)
    if not gate_check():
        sys.exit("[ec2_intent_send] GATE BLOCKED — no LAN prover responded, not sending")

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
