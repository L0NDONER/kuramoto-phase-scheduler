#!/usr/bin/env python3
"""
nazare_carrier_sender.py — Nazare envelope paced by the live quartz
carrier instead of a private sleep loop.

pulse_hash_send.py / adversary_sender.py both pace PS_PULSE packets
against t0 + i*tick_s, computed locally with time.sleep(). The WAN
regression test proved that's forgeable: adversary_sender.py hits the
same tick grid with zero access to any real oscillator, just adjtimex
and a sleep loop, because "the grid" is something either side can derive
alone from nothing but a claimed tick_s.

This sender instead subscribes to quartz_beacon.py's live AxisPulse
stream (239.0.0.2:7404) and fires PULSE idx only when it has actually
*observed* AxisPulse tick (start_ap_tick + idx*BEAT_TICKS) arrive on the
wire. The semantic envelope (which idx get a pulse = the hash bits) now
rides on top of a carrier neither side can compute in isolation — you
either have a live multicast feed from a real crystal, or you don't.

Wire format (NC_*) is deliberately separate from PS_* (pulse_hash_send)
so this doesn't get confused with the sleep-loop-paced trains already
in the regression suite — this is a different claim (carrier-lock, not
just plausible-shaped jitter) and needs its own verifier.

Usage:
    python3 nazare_carrier_sender.py --dest HOST --sid 3 --beat-ticks 20 --salt TEXT
"""
import argparse
import hashlib
import selectors
import socket
import struct
import sys
import time

from axis_pulse import AP_GRP, AP_PORT, AP_TICK_S, mcast_in, next_ap_tick, hash_to_bits

NC_GRP, NC_PORT = "239.0.0.9", 7480
NC_MAGIC = 0x4E43  # "NC"
NC_START, NC_PULSE, NC_END, NC_HEARTBEAT = 1, 2, 3, 4
NC_START_FMT = ">HBHHQ"   # magic,type,n_bits,beat_ticks,start_ap_tick
NC_PULSE_FMT = ">HBH"     # magic,type,idx
NC_END_FMT = ">HB"
NC_HEARTBEAT_FMT = ">HBQ"  # magic,type,ap_tick — no bit index, just "I saw this tick"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="127.0.0.1")
    ap.add_argument("--sid", type=int, default=3, help="which local beacon sid to lock onto")
    ap.add_argument("--beat-ticks", type=int, default=20, help="AxisPulse ticks per semantic beat (20*10ms=200ms)")
    ap.add_argument("--hb-ticks", type=int, default=0,
                     help="AxisPulse ticks per heartbeat (0=disabled). Fast inner phase that "
                          "carries no payload, just proves continued live carrier access "
                          "between semantic beats — e.g. 5 = 50ms heartbeat under a 200ms beat.")
    ap.add_argument("--salt", default="carrier")
    ap.add_argument("--ap-port", type=int, default=AP_PORT,
                     help="local port to receive AxisPulse on — override when it's relayed "
                          "to a non-standard port to avoid colliding with another listener")
    args = ap.parse_args()

    H = hashlib.sha256(args.salt.encode()).digest()
    bits = hash_to_bits(H)
    n_bits = len(bits)

    ap_in = mcast_in(AP_GRP, args.ap_port)
    sel = selectors.DefaultSelector()
    sel.register(ap_in, selectors.EVENT_READ)

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    print(f"[nazare-carrier] waiting to observe live AxisPulse sid={args.sid} "
          f"before starting — no crystal lock, no train.", flush=True)

    latest_tick = None
    t_wait = time.monotonic() + 10.0
    while latest_tick is None and time.monotonic() < t_wait:
        latest_tick = next_ap_tick(sel, ap_in, args.sid)

    if latest_tick is None:
        sys.exit("[nazare-carrier] no live AxisPulse observed — is quartz_beacon.py running for this sid?")

    start_ap_tick = latest_tick + args.beat_ticks  # give ourselves one full beat of runway
    print(f"[nazare-carrier] locked. current_tick={latest_tick}  "
          f"start_ap_tick={start_ap_tick}  beat_ticks={args.beat_ticks} "
          f"({args.beat_ticks*AP_TICK_S*1000:.0f}ms/beat)  hash={H.hex()[:16]}...", flush=True)

    start_pkt = struct.pack(NC_START_FMT, NC_MAGIC, NC_START, n_bits, args.beat_ticks, start_ap_tick)
    out.sendto(start_pkt, (args.dest, NC_PORT))

    n_pulses = n_hb = 0
    last_hb_tick = latest_tick
    for i, b in enumerate(bits):
        target_tick = start_ap_tick + i * args.beat_ticks
        # block until we've actually seen this AxisPulse tick arrive — this is
        # the whole point: we cannot know "now" is the right beat without a
        # live feed telling us so, unlike a sleep-loop's self-computed grid.
        while latest_tick < target_tick:
            t = next_ap_tick(sel, ap_in, args.sid)
            if t is None:
                continue
            latest_tick = t

            # inner phase: fire a heartbeat every hb_ticks, independent of
            # (much faster than) the outer semantic beat — proves live
            # carrier access continuously, not just once per bit
            if args.hb_ticks and latest_tick - last_hb_tick >= args.hb_ticks:
                out.sendto(struct.pack(NC_HEARTBEAT_FMT, NC_MAGIC, NC_HEARTBEAT, latest_tick),
                           (args.dest, NC_PORT))
                last_hb_tick = latest_tick
                n_hb += 1

        if b:
            out.sendto(struct.pack(NC_PULSE_FMT, NC_MAGIC, NC_PULSE, i), (args.dest, NC_PORT))
            n_pulses += 1
        print("█" if b else "░", end="", flush=True)

    out.sendto(struct.pack(NC_END_FMT, NC_MAGIC, NC_END), (args.dest, NC_PORT))
    print(f"\n[nazare-carrier] END  {n_pulses}/{n_bits} pulsed  {n_hb} heartbeats  hash={H.hex()}", flush=True)


if __name__ == "__main__":
    main()
