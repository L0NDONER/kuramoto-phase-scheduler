#!/usr/bin/env python3
"""
i0_nazare_sender.py -- I(0) port of nazare_carrier_sender.py.

Ported from AxisPulse (239.0.0.2:7404, quartz_beacon.py, now retired)
to I(0) (239.0.0.6:7500, layer0_daemon.py). Same core claim as the
original: a receiver reconstructing bits purely from pulse-arrival
timing knows the sender was actually observing a live tick at the
moment it decided to pulse or stay silent -- a sleep-loop replay can't
fake this without the same live feed.

Difference from the original: pulses the TICKET'S ACTUAL BYTES (host,
UTC timestamp, "I am present"), not a hash of an arbitrary salt. The
point here isn't just proving carrier-lock in the abstract -- the
printed ticket's own content is what got carrier-locked. Unmasks
identity via the payload itself, but the *unforgeability* still comes
from the fact that whoever pulsed those bits had to be watching a real
I(0) tick at the moment each bit went out.

Usage:
    python3 i0_nazare_sender.py --dest HOST --port 7480 --text "..."
    python3 i0_nazare_sender.py --dest HOST --port 7480 --ticket   # auto-builds hostname+UTC+"I am present"
"""
import argparse
import selectors
import socket
import struct
import sys
import time
from datetime import datetime, timezone

from i0_carrier import I0_GRP, I0_PORT, I0_TICK_S, mcast_in, next_i0_tick, bytes_to_bits

NC_MAGIC = 0x4E43  # "NC"
NC_START, NC_PULSE, NC_END = 1, 2, 3
NC_START_FMT = ">HBHHQ"   # magic,type,n_bits,beat_ticks,start_tick
NC_PULSE_FMT = ">HBH"     # magic,type,idx
NC_END_FMT = ">HB"


def build_ticket():
    host = socket.gethostname()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{host} {ts} I am present"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="verifier host")
    ap.add_argument("--port", type=int, required=True, help="verifier's receive port for this sender")
    ap.add_argument("--text", default=None, help="literal text to pulse-code")
    ap.add_argument("--ticket", action="store_true", help="auto-build hostname+UTC+'I am present'")
    ap.add_argument("--beat-ticks", type=int, default=1,
                     help="I(0) ticks per bit (1 tick = 50ms/bit at 20Hz)")
    ap.add_argument("--i0-port", type=int, default=I0_PORT,
                     help="local port to receive I(0) on -- override when relayed to a non-standard port")
    args = ap.parse_args()

    if args.text is None and not args.ticket:
        sys.exit("need --text or --ticket")
    text = args.text if args.text is not None else build_ticket()
    payload = text.encode("utf-8")
    bits = bytes_to_bits(payload)
    n_bits = len(bits)

    i0_in = mcast_in(I0_GRP, args.i0_port)
    sel = selectors.DefaultSelector()
    sel.register(i0_in, selectors.EVENT_READ)

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"[i0-nazare-tx] waiting to observe live I(0) before starting -- "
          f"no carrier, no train.", flush=True)

    latest_tick = None
    t_wait = time.monotonic() + 10.0
    while latest_tick is None and time.monotonic() < t_wait:
        latest_tick = next_i0_tick(sel, i0_in)

    if latest_tick is None:
        sys.exit("[i0-nazare-tx] no live I(0) observed -- is layer0_daemon.py running?")

    start_tick = latest_tick + args.beat_ticks
    print(f"[i0-nazare-tx] locked. current_tick={latest_tick} start_tick={start_tick} "
          f"beat_ticks={args.beat_ticks} ({args.beat_ticks*I0_TICK_S*1000:.0f}ms/bit) "
          f"payload={text!r} ({n_bits} bits)", flush=True)

    start_pkt = struct.pack(NC_START_FMT, NC_MAGIC, NC_START, n_bits, args.beat_ticks, start_tick)
    out.sendto(start_pkt, (args.dest, args.port))

    n_pulses = 0
    for i, b in enumerate(bits):
        target_tick = start_tick + i * args.beat_ticks
        while latest_tick < target_tick:
            t = next_i0_tick(sel, i0_in)
            if t is None:
                continue
            latest_tick = t

        if b:
            out.sendto(struct.pack(NC_PULSE_FMT, NC_MAGIC, NC_PULSE, i), (args.dest, args.port))
            n_pulses += 1
        print("#" if b else ".", end="", flush=True)

    out.sendto(struct.pack(NC_END_FMT, NC_MAGIC, NC_END), (args.dest, args.port))
    print(f"\n[i0-nazare-tx] END  {n_pulses}/{n_bits} pulsed  payload={text!r}", flush=True)


if __name__ == "__main__":
    main()
