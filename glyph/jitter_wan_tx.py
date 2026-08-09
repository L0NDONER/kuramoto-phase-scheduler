#!/usr/bin/env python3
"""
glyph/jitter_wan_tx.py — jitter_encoder.py's scheme, sent as real unicast
UDP over the WireGuard tunnel to EC2, instead of multicast on the LAN
(jitter_live_tx.py). Wider jitter/threshold margins than the LAN version:
measured WAN RTT to EC2 is ~161ms with ~1-3ms round-trip jitter, close to
jitter_encoder.py's LAN-tuned THRESH=1.5ms -- too close for comfort, so
this uses its own wider constants instead of importing jitter_encoder's.

Gated the same way as ec2_intent_send.py: local phase-auth gate_check()
must pass before anything is sent -- this is a data transport, not an
authentication mechanism, so it doesn't replace that gate.

Retries: jitter_wan_rx.py signs and acks back its decoded text (reuses
ec2_intent_common's HMAC format, not new crypto). Real-WAN trials landed
9/10 clean, 1/10 with a single bit error -- close enough that verifying
the ack and resending on mismatch/timeout closes the gap, rather than
chasing threshold tuning further. Re-runs gate_check() on every retry,
not just once -- each attempt is its own presence proof.

Usage:
    python3 glyph/jitter_wan_tx.py "text to send"
"""
import random, socket, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from quartz_core.phase_auth import gate_check
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ec2_intent_common import load_key, unpack_response, DONE_OK

EC2_WG_IP = "10.8.0.1"
PORT      = 7410
MAGIC     = 0x4A57  # "JW"

ACK_PORT       = 7412
RESULT_PORT    = 7413
ACK_TIMEOUT_S  = 5.0     # generous: WAN RTT (~161ms) + decode processing
RESULT_TIMEOUT_S = 15.0  # generous: EC2 has to run the command before reporting
MAX_ATTEMPTS   = 3

TICK_S        = 0.01           # 100Hz cadence, same as the LAN version
TICKS_PER_BIT = 24
JITTER_SIGMA  = 0.035           # 20ms -- well clear of the ~1-3ms WAN jitter floor
THRESH        = 0.010           # 8ms decision boundary (rx side, kept here for reference)


def text_to_bits(text):
    bits = []
    for byte in text.encode("utf-8"):
        bits.extend((byte >> i) & 1 for i in range(7, -1, -1))
    return bits


def sleep_until(t):
    while True:
        remaining = t - time.time()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.001)


def send_burst(text):
    bits = text_to_bits(text)
    print(f"[jitter-wan-tx] {text!r} -> {len(bits)} bits, "
          f"{len(bits) * TICKS_PER_BIT} ticks -> {EC2_WG_IP}:{PORT}", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rng = random.Random()
    seq = 0
    next_at = time.time() + 1.0
    for bit in bits:
        for _ in range(TICKS_PER_BIT):
            jitter = rng.gauss(0, JITTER_SIGMA) if bit else 0.0
            sleep_until(next_at + jitter)
            sock.sendto(struct.pack(">HI", MAGIC, seq), (EC2_WG_IP, PORT))
            seq += 1
            next_at += TICK_S
    print(f"[jitter-wan-tx] done — {seq} ticks sent, waiting for ack...", flush=True)


def wait_for_ack(key):
    """Returns decoded text on a verified ack, or None on timeout/bad sig."""
    ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ack_sock.bind(("", ACK_PORT))
    ack_sock.settimeout(ACK_TIMEOUT_S)
    try:
        data, addr = ack_sock.recvfrom(512)
    except socket.timeout:
        print("[jitter-wan-tx] no ack within timeout", flush=True)
        return None
    finally:
        ack_sock.close()
    try:
        status, detail, _nonce = unpack_response(data, key)
    except ValueError as e:
        print(f"[jitter-wan-tx] bad ack: {e}", flush=True)
        return None
    if status != DONE_OK:
        print(f"[jitter-wan-tx] ack status={status}, not DONE_OK", flush=True)
        return None
    return detail


def wait_for_result(key):
    """Waits (best-effort) for EC2's post-execution result report on
    RESULT_PORT. Distinct from the transmission ack -- this only arrives
    if the receiver is running with --execute, so its absence is not a
    failure, just "nothing to report" (e.g. decode-only mode)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", RESULT_PORT))
    sock.settimeout(RESULT_TIMEOUT_S)
    try:
        data, addr = sock.recvfrom(512)
    except socket.timeout:
        print("[jitter-wan-tx] no execution result reported "
              "(receiver may not be in --execute mode)", flush=True)
        return
    finally:
        sock.close()
    try:
        status, detail, _nonce = unpack_response(data, key)
    except ValueError as e:
        print(f"[jitter-wan-tx] bad result report: {e}", flush=True)
        return
    print(f"[jitter-wan-tx] EC2 result: {detail}", flush=True)


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "hello ec2"
    key = load_key()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[jitter-wan-tx] attempt {attempt}/{MAX_ATTEMPTS}", flush=True)
        print("[jitter-wan-tx] phase-auth gate...", flush=True)
        if not gate_check():
            print("[jitter-wan-tx] GATE BLOCKED — no LAN prover responded, not sending", flush=True)
            continue

        send_burst(text)
        acked = wait_for_ack(key)

        if acked == text:
            print(f"[jitter-wan-tx] CONFIRMED — EC2's ack matches exactly (attempt {attempt})", flush=True)
            wait_for_result(key)
            return True
        elif acked is not None:
            print(f"[jitter-wan-tx] MISMATCH — sent {text!r}, EC2 decoded {acked!r}, retrying", flush=True)
        # else: wait_for_ack already printed why (timeout / bad sig)

    sys.exit(f"[jitter-wan-tx] FAILED after {MAX_ATTEMPTS} attempts, giving up")


if __name__ == "__main__":
    main()
