#!/usr/bin/env python3
"""
glyph/jitter_wan_rx.py — EC2-side receiver for jitter_wan_tx.py.

Binds ONLY to the WireGuard-internal address (10.8.0.1) -- same
"no raw internet exposure" pattern as ec2_intent_listener.py. Timing
patterns alone are not cryptographic proof of anything (anyone who can
reach this port over the tunnel could send the same pattern), so this
is not a replacement for the existing HMAC-signed intent channel --
it's a different transport, gated the same way execution already is:
via truthd's EC2_SELF check before anything runs.

Modes:
    python3 glyph/jitter_wan_rx.py                       decode only, print, no execution
    python3 glyph/jitter_wan_rx.py --execute              decode, then if the text matches
                                                            a known intent, gate through
                                                            truthd and run it
    python3 glyph/jitter_wan_rx.py --threshold 18.0        override THRESH (ms) for this run
    python3 glyph/jitter_wan_rx.py --expect "hello ec2 test"
                                                            score exact bit errors against a
                                                            known sent string, prints
                                                            "bits_wrong=N/M" for trial scripts

After every decode, signs and sends the decoded text back to Mint as an
ack (reuses ec2_intent_common.pack_response -- same HMAC key and format
already used for the ec2_intent request/response channel, not new
crypto) so jitter_wan_tx.py can verify EC2 actually received what was
sent and retry on mismatch/timeout instead of trusting a one-way
transport blind. 9/10 real-WAN trials landed 0 bit errors and 1/10 had
a single bit error (see quartz-substrate notes) -- close enough that a
retry layer, not more threshold tuning, is what actually closes the gap.

Usage: run on EC2.
"""
import socket, struct, subprocess, sys, time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ec2_intent_common import load_key, pack_response, DONE_OK

BIND_IP = "10.8.0.1"   # WG-internal only
PORT    = 7410
MAGIC   = 0x4A57

MINT_WG_IP = "10.8.0.4"   # Mint's wg-irssi address
ACK_PORT   = 7412

TICKS_PER_BIT = 24
THRESH        = 0.010   # 8ms -- must match jitter_wan_tx.py's JITTER_SIGMA margin

TRUTHD_SOCK = "/tmp/truthd.sock"

INTENTS = {
    "restart_wg_easy": ["sudo", "docker", "restart", "wg-easy"],
    "start_wg_easy":   ["sudo", "docker", "start", "wg-easy"],
    "stop_wg_easy":    ["sudo", "docker", "stop", "wg-easy"],
}


def text_to_bits(text):
    bits = []
    for byte in text.encode("utf-8"):
        bits.extend((byte >> i) & 1 for i in range(7, -1, -1))
    return bits


def bits_to_text(bits):
    byte_vals = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        byte_vals.append(byte)
    return byte_vals.decode("utf-8", errors="replace")


def decode(ticks, n_bits, thresh):
    intervals = [b - a for a, b in zip(ticks, ticks[1:])]
    bits = []
    for i in range(n_bits):
        window = intervals[i * TICKS_PER_BIT:i * TICKS_PER_BIT + TICKS_PER_BIT - 1]
        if not window:
            break
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        std = var ** 0.5
        bits.append(1 if std > thresh else 0)
    return bits


def truthd_check(verb):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(TRUTHD_SOCK)
        s.sendall(f"CHECK {verb}\n".encode())
        resp = s.recv(64).decode().strip()
        s.close()
        return resp
    except OSError as e:
        return f"DENY UNREACHABLE({e})"


def mcast_in():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, PORT))
    return s


def _cli_arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    execute = "--execute" in sys.argv
    thresh_arg = _cli_arg("--threshold")
    thresh = float(thresh_arg) / 1000.0 if thresh_arg else THRESH
    expect = _cli_arg("--expect")

    s = mcast_in()
    print(f"[jitter-wan-rx] listening on {BIND_IP}:{PORT}  execute={execute}  "
          f"threshold={thresh*1000:.1f}ms", flush=True)

    # (seq, arrival_time) -- real WAN paths reorder packets (unlike the
    # LAN version, single host, effectively FIFO). decode() assumes send
    # order; sort by the sender's seq before decoding, don't trust
    # arrival order (found live 2026-08-09: raw arrival order produced a
    # completely garbled decode, unrelated to jitter margin at all).
    received = []
    s.settimeout(30.0)
    while True:
        try:
            data, addr = s.recvfrom(64)
        except socket.timeout:
            break
        if len(data) < 6:
            continue
        magic, seq = struct.unpack(">HI", data[:6])
        if magic != MAGIC:
            continue
        received.append((seq, time.time()))
        s.settimeout(1.0)   # 1s idle (WAN margin) = end of burst

    received.sort(key=lambda r: r[0])
    ticks = [t for _, t in received]
    n_bits = len(ticks) // TICKS_PER_BIT
    print(f"[jitter-wan-rx] received {len(ticks)} ticks -> {n_bits} bits", flush=True)

    bits = decode(ticks, n_bits, thresh)
    text = bits_to_text(bits)
    print(f"[jitter-wan-rx] decoded: {text!r}", flush=True)

    try:
        key = load_key()
        ack = pack_response(0, DONE_OK, text, key)
        ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ack_sock.sendto(ack, (MINT_WG_IP, ACK_PORT))
        ack_sock.close()
        print(f"[jitter-wan-rx] ack sent -> {MINT_WG_IP}:{ACK_PORT}", flush=True)
    except OSError as e:
        print(f"[jitter-wan-rx] ack send failed: {e}", flush=True)

    if expect is not None:
        expected_bits = text_to_bits(expect)
        m = len(expected_bits)
        wrong = sum(1 for a, b in zip(bits, expected_bits) if a != b)
        wrong += abs(len(bits) - m)   # length mismatch (dropped/extra ticks) counts too
        print(f"[jitter-wan-rx] bits_wrong={wrong}/{m}", flush=True)

    if not execute:
        return

    intent = text.strip()
    if intent not in INTENTS:
        print(f"[jitter-wan-rx] unknown intent {intent!r}, not executing", flush=True)
        return

    resp = truthd_check("EC2_SELF")
    print(f"[jitter-wan-rx] truthd: {resp}", flush=True)
    if not resp.startswith("ALLOW"):
        print(f"[jitter-wan-rx] GATE DENIED, not executing", flush=True)
        return

    result = subprocess.run(INTENTS[intent], capture_output=True, text=True)
    print(f"[jitter-wan-rx] EXECUTED {intent}: exit={result.returncode} "
          f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}", flush=True)


if __name__ == "__main__":
    main()
