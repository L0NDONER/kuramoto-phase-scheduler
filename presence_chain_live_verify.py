#!/usr/bin/env python3
"""
presence_chain_live_verify.py — catch presence_chain.py tampering live.

presence_chain.py speaks on two independent channels per checkpoint:
  1. ChainLink (239.0.0.5:7460)  — plaintext commit16 broadcast every tick
  2. Pulse channel (239.0.0.7:7470) — same checkpoint's commit prefix,
     pulse-coded across the following ticks, content carried by timing only

A forger who controls the process can fake channel 1 trivially (it's just
data). Faking channel 2 requires either also owning a real oscillator to
pace the pulses, or accepting a timing signature (the jitter check) that
gives away the fake. This listener reconstructs the pulse-coded prefix and
checks it against two things:
  a) does it match the plaintext prefix already seen on ChainLink?
  b) does its arrival jitter look like a real tick source paced it?

Both have to pass for a checkpoint to be called live-verified.

Usage:
    python3 presence_chain_live_verify.py
"""
import selectors, socket, statistics, struct, sys, time
from collections import deque

from pulse_hash_recv import verify_physical, bits_to_hex, PS_MAGIC, PS_START, PS_PULSE, PS_END, PS_PULSE_FMT

CL_GRP = "239.0.0.5"; CL_PORT = 7460
CL_FMT = ">HI16sffff"; CL_SIZE = struct.calcsize(CL_FMT); CL_MAGIC = 0x434C

PS_GRP = "239.0.0.7"; PS_PORT = 7470
PULSE_BYTES = 4


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def main():
    cl_sock = mcast_in(CL_GRP, CL_PORT)
    ps_sock = mcast_in(PS_GRP, PS_PORT)

    # recent ChainLink commit16 prefixes, newest last — matched against
    # whichever pulse train finishes next (pulse train starts right after
    # the checkpoint tick, so the most recent ChainLink entry is the one)
    recent_commits = deque(maxlen=50)

    n_bits = tick_s = t_start = bits = None
    arrivals = []
    pulses_seen = set()
    commit_at_start = None

    print("[live-verify] listening: ChainLink 239.0.0.5:7460 + Pulse 239.0.0.7:7470", flush=True)

    sel = selectors.DefaultSelector()
    sel.register(cl_sock, selectors.EVENT_READ, data="cl")
    sel.register(ps_sock, selectors.EVENT_READ, data="ps")

    while True:
        events = sel.select(timeout=1.0)

        for key, _ in events:
            if key.data == "cl":
                data, _ = cl_sock.recvfrom(64)
                if len(data) < CL_SIZE:
                    continue
                f = struct.unpack_from(CL_FMT, data)
                if f[0] != CL_MAGIC:
                    continue
                tick, commit16 = f[1], f[2]
                recent_commits.append((tick, commit16))

            elif key.data == "ps":
                data, _ = ps_sock.recvfrom(64)
                if len(data) < 3:
                    continue
                magic, ptype = struct.unpack_from(">HB", data)
                if magic != PS_MAGIC:
                    continue

                if ptype == PS_START:
                    _, _, n_bits, tick_us = struct.unpack_from(">HBHI", data)
                    tick_s = tick_us / 1e6
                    t_start = time.monotonic()
                    bits = [0] * n_bits
                    arrivals = []
                    pulses_seen = set()
                    # snapshot the ChainLink commit as of right now — the
                    # checkpoint this pulse train encodes, before the chain
                    # advances any further across the 32 ticks it takes to send
                    commit_at_start = recent_commits[-1][1] if recent_commits else None
                    print(f"[live-verify] pulse START  n_bits={n_bits}  tick~{tick_s*1000:.1f}ms", flush=True)

                elif ptype == PS_PULSE and bits is not None:
                    t_arrival = time.monotonic()
                    _, _, idx = struct.unpack_from(PS_PULSE_FMT, data)
                    if 0 <= idx < n_bits:
                        bits[idx] = 1
                        pulses_seen.add(idx)
                        arrivals.append((idx, t_arrival))

                elif ptype == PS_END and bits is not None:
                    prefix_hex = bits_to_hex(bits)
                    pv = verify_physical(arrivals, tick_s)
                    jv, sv = pv["jitter"], pv["shape"]

                    # match against the ChainLink commit snapshotted at START —
                    # NOT whatever's most recent now, since the chain has kept
                    # advancing across the 32 ticks the pulse train took to send
                    plaintext_match = None
                    if commit_at_start is not None:
                        plaintext_match = commit_at_start[:PULSE_BYTES].hex() == prefix_hex

                    physical_ok = pv["physical"]
                    verdict = "LIVE-VERIFIED" if (plaintext_match and physical_ok) else "TAMPER SUSPECTED"

                    print(f"[live-verify] pulse END  reconstructed={prefix_hex}  "
                          f"chainlink_match={plaintext_match}  "
                          f"jitter={'OK' if (jv and jv['physical']) else 'BAD'}"
                          f"{'  (' + jv['verdict'] + ')' if jv else '  (not enough pulses)'}  "
                          f"shape={'OK' if (sv is None or sv['physical']) else 'BAD'}"
                          f"{'  (' + sv['verdict'] + ')' if sv else ''}", flush=True)
                    print(f"[live-verify] >>> {verdict} <<<", flush=True)

                    bits = None


if __name__ == "__main__":
    main()
