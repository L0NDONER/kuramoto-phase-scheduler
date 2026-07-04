#!/usr/bin/env python3
"""
carrier_verify.py — verify a nazare_carrier_sender.py train by
cross-referencing against AxisPulse ticks this process observed itself,
not against a claimed tick_us the sender put in the packet.

pulse_hash_recv.py's verify_physical() infers "real oscillator" from
statistics of arrival timing alone — a self-consistent story a sender
never actually watching a crystal can still fake (adversary_sender.py
proved this). This verifier makes a different, stronger claim: it does
not trust *any* number the sender sends. It keeps its own independent
log of every AxisPulse tick's arrival time, and for each PULSE checks
that the tick it claims to be riding actually happened, at roughly the
time this verifier itself saw it happen.

A sender with a private sleep loop and no multicast subscription cannot
pass this even in principle — it has no way to know which tick number
is "now" without receiving the same broadcast the verifier is watching.
That's the access-control property a jitter/shape statistic can never
give you: this isn't "does the timing look physical", it's "did this
event actually happen when I saw it happen."

Timing-plausibility judgment itself is delegated to Nazaré (nazare.py),
via whichever domain-specific fork matches the packet's origin —
nazare_wg.py (post-WireGuard, calibrated to tunnel/RTT jitter) or
nazare_wan.py (raw WAN sine, calibrated to ISP/fibre jitter). The two
verdicts are tracked completely independently per origin and are never
merged or compared: a WG verdict says nothing about WAN timing and vice
versa. See [[project_verification_lattice]] and
[[project_nazare_timebase_placement]].

Usage:
    python3 carrier_verify.py --sid 3
"""
import argparse
import selectors
import socket
import struct
import time

import nazare_wg
import nazare_wan

AP_GRP, AP_PORT = "239.0.0.2", 7404
AP_FMT = ">HBBIfffffHQ"
AP_MAGIC = 0x4158

NC_GRP, NC_PORT = "239.0.0.9", 7480
NC_MAGIC = 0x4E43
NC_START, NC_PULSE, NC_END, NC_HEARTBEAT = 1, 2, 3, 4
NC_START_FMT = ">HBHHQ"
NC_PULSE_FMT = ">HBH"
NC_HEARTBEAT_FMT = ">HBQ"

COVERAGE_MIN = 0.90     # fraction of pulses that must resolve to a Nazaré-plausible tick

HB_STREAK_TRIPS = 3      # a forger free-running on a guessed cadence drifts
                          # monotonically once it goes dark (see fake_carrier_
                          # sender.py) — a *streak* of consecutive bad
                          # heartbeats is that drift; one isolated bad one is
                          # just the same local-ordering noise the pulse check
                          # already tolerates, not evidence of anything

# WireGuard subnet (see project memory reference_wireguard_mint) — the origin
# discriminator that decides which Nazaré domain owns a given packet.
WG_SUBNET_PREFIX = "10.8."


def _origin_of(addr):
    return "wg" if addr[0].startswith(WG_SUBNET_PREFIX) else "wan"


def _make_verifiers():
    return {"wg": nazare_wg.make_verifier(), "wan": nazare_wan.make_verifier()}


def _new_train_state():
    return {
        "n_bits": None, "beat_ticks": None, "start_ap_tick": None,
        "pulses": [],
        "hb_first_bad_at": None, "hb_train_start": None,
        "n_hb": 0, "n_hb_bad": 0, "hb_streak": 0,
    }


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", type=int, default=3)
    args = ap.parse_args()

    ap_in = mcast_in(AP_GRP, AP_PORT)
    nc_in = mcast_in(NC_GRP, NC_PORT)
    sel = selectors.DefaultSelector()
    sel.register(ap_in, selectors.EVENT_READ, data="ap")
    sel.register(nc_in, selectors.EVENT_READ, data="nc")

    # two independent Nazaré instances — WG and WAN each get their own local
    # observation log, config, and verdict labels. Both are fed the same
    # AxisPulse ground truth (that's just "what did we see, when" — recording
    # it isn't a domain claim), but a train's claims only ever get checked
    # against the one verifier matching its origin.
    verifiers = _make_verifiers()
    trains = {"wg": _new_train_state(), "wan": _new_train_state()}

    print(f"[carrier-verify] watching AxisPulse sid={args.sid} + NC channel "
          f"{NC_GRP}:{NC_PORT}  (wg={nazare_wg.DELTA_WG_S*1000:.0f}ms±{nazare_wg.EPSILON_WG_S*1000:.0f}ms, "
          f"wan={nazare_wan.DELTA_WAN_S*1000:.0f}ms±{nazare_wan.EPSILON_WAN_S*1000:.0f}ms)...", flush=True)

    while True:
        for key, _ in sel.select(timeout=1.0):
            tag = key.data
            if tag == "ap":
                data, _ = ap_in.recvfrom(64)
                if len(data) < struct.calcsize(AP_FMT):
                    continue
                f = struct.unpack_from(AP_FMT, data)
                if f[0] != AP_MAGIC or f[1] != args.sid:
                    continue
                arrival = time.monotonic()
                for v in verifiers.values():
                    v.observe(f[3], arrival)

            elif tag == "nc":
                data, addr = nc_in.recvfrom(64)
                if len(data) < 3:
                    continue
                magic, ptype = struct.unpack_from(">HB", data)
                if magic != NC_MAGIC:
                    continue

                origin = _origin_of(addr)
                nazare = verifiers[origin]
                st = trains[origin]

                if ptype == NC_START:
                    _, _, st["n_bits"], st["beat_ticks"], st["start_ap_tick"] = \
                        struct.unpack_from(NC_START_FMT, data)
                    st["pulses"] = []
                    st["hb_first_bad_at"] = None
                    st["hb_train_start"] = time.monotonic()
                    st["n_hb"] = st["n_hb_bad"] = st["hb_streak"] = 0
                    print(f"[carrier-verify:{origin}] NC START from {addr[0]}  n_bits={st['n_bits']} "
                          f"beat_ticks={st['beat_ticks']}  start_ap_tick={st['start_ap_tick']}", flush=True)

                elif ptype == NC_PULSE and st["n_bits"] is not None:
                    t_arrival = time.monotonic()
                    _, _, idx = struct.unpack_from(NC_PULSE_FMT, data)
                    st["pulses"].append((idx, t_arrival))

                elif ptype == NC_HEARTBEAT and st["n_bits"] is not None:
                    t_arrival = time.monotonic()
                    _, _, claimed_tick = struct.unpack_from(NC_HEARTBEAT_FMT, data)
                    st["n_hb"] += 1
                    ok, _, _ = nazare.check(claimed_tick, t_arrival)
                    if not ok:
                        st["n_hb_bad"] += 1
                        st["hb_streak"] += 1
                        if st["hb_streak"] >= HB_STREAK_TRIPS and st["hb_first_bad_at"] is None:
                            st["hb_first_bad_at"] = t_arrival - st["hb_train_start"]
                            print(f"[carrier-verify:{origin}] !! {HB_STREAK_TRIPS} consecutive bad "
                                  f"heartbeats by t={st['hb_first_bad_at']:.3f}s into train "
                                  f"(tick={claimed_tick}) — carrier lock lost or never real", flush=True)
                    else:
                        st["hb_streak"] = 0

                elif ptype == NC_END and st["n_bits"] is not None:
                    hits = 0
                    misses = []
                    for idx, t_arrival in st["pulses"]:
                        expected_tick = st["start_ap_tick"] + idx * st["beat_ticks"]
                        ok, _, detail = nazare.check(expected_tick, t_arrival)
                        if ok:
                            hits += 1
                        else:
                            misses.append((idx, detail))

                    total = len(st["pulses"])
                    coverage = hits / total if total else 0.0
                    pulses_ok = total and coverage >= COVERAGE_MIN
                    hb_ok = st["hb_first_bad_at"] is None

                    verdict = nazare.config.verdict_pass if (pulses_ok and hb_ok) else nazare.config.verdict_fail
                    first_bad_at = st["hb_first_bad_at"]
                    first_bad_str = f"{first_bad_at:.3f}s" if first_bad_at is not None else "none"

                    print(f"[carrier-verify:{origin}] NC END  pulses={total}  hits={hits}  "
                          f"coverage={coverage*100:.1f}%  heartbeats={st['n_hb']} bad={st['n_hb_bad']} "
                          f"first_bad_at={first_bad_str}  >>> {verdict} <<<", flush=True)
                    if misses[:5]:
                        print(f"[carrier-verify:{origin}] sample pulse misses: {misses[:5]}", flush=True)

                    n_bits = None


if __name__ == "__main__":
    main()
