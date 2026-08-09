#!/usr/bin/env python3
"""
glyph/jitter_live_rx.py — receiver for jitter_live_tx.py.

Records real UDP arrival timestamps and decodes them with
jitter_encoder.py's interval-stddev threshold logic — the same
decode() used against synthetic timestamps in the pure sandbox
simulation, now fed real network + scheduling jitter instead.

Usage:
    python3 glyph/jitter_live_rx.py
"""
import socket, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from jitter_encoder import bits_to_text, decode, TICKS_PER_BIT

GRP, PORT = "239.0.0.6", 7409
MAGIC = 0x4A54


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    return s


def main():
    s = mcast_in(GRP, PORT)
    print(f"[jitter-rx] listening on {GRP}:{PORT}", flush=True)

    ticks = []
    s.settimeout(10.0)   # wait for tx to start
    while True:
        try:
            data, _ = s.recvfrom(64)
        except socket.timeout:
            break
        if len(data) < 6:
            continue
        magic, seq = struct.unpack(">HI", data[:6])
        if magic != MAGIC:
            continue
        ticks.append(time.time())
        s.settimeout(0.5)   # after the first tick, 0.5s idle = end of burst

    n_bits = len(ticks) // TICKS_PER_BIT
    print(f"[jitter-rx] received {len(ticks)} ticks -> {n_bits} bits", flush=True)

    bits = decode(ticks, n_bits)
    text = bits_to_text(bits)
    print(f"[jitter-rx] decoded: {text!r}", flush=True)


if __name__ == "__main__":
    main()
