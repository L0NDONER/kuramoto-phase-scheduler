"""layer1_aggregator.py — collects (r, psi) reports from real Layer 0
nodes over multicast, computes the recursive mean-field order parameter
across them: r1*e^(i*psi1) = mean(r_i * e^(i*psi_i)) across currently-
live nodes.

Weighting each node's contribution by its own r_i (not just psi_i) is
deliberate: a node reporting psi with low r itself is a less trustworthy
phase reading (its own oscillators aren't coherent yet), so it should
count for less here too -- same "don't treat all inputs as equally
confident" logic as reflex.py's peer_age_s handling.

A node is considered live if a report arrived within STALE_S; drops out
of the aggregate (not zeroed-in) if it goes silent, same as
project_pi_swarm_arms's stale-peer handling elsewhere in this codebase.

Also broadcasts its own result (r1, psi1) back out under node name
"layer1" -- Layer 1 reporting upward exactly like a Layer 0 node does,
same wire format, same port. Explicitly excluded from its own aggregate
input: multicast loopback is on by default, so this process's listening
socket would otherwise receive its own broadcast back and recursively
feed r1 into the computation that produced it.

Usage: python3 layer1_aggregator.py [duration_s]
"""
import cmath
import sys
import time

from layer0_report import mcast_in, mcast_out, send_report, parse_report

STALE_S = 3.0
PRINT_INTERVAL_S = 0.5
SELF_NAME = "layer1"


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    sock = mcast_in()
    out_sock = mcast_out()
    print(f"[layer1] listening for Layer 0 reports, {duration_s:.0f}s run, "
          f"broadcasting r1/psi1 as {SELF_NAME!r}", flush=True)

    last_seen = {}   # node_name -> (r, psi, arrival_time)
    deadline = time.time() + duration_s
    next_print = time.time() + PRINT_INTERVAL_S

    while time.time() < deadline:
        now = time.time()
        try:
            data, _addr = sock.recvfrom(64)
            parsed = parse_report(data)
            if parsed and parsed[0] != SELF_NAME:
                name, r, psi, _dt_lag_s, _amplitude = parsed
                last_seen[name] = (r, psi, now)
        except BlockingIOError:
            pass

        if now >= next_print:
            next_print = now + PRINT_INTERVAL_S
            live = {n: (r, psi) for n, (r, psi, t) in last_seen.items()
                     if now - t < STALE_S}

            if not live:
                print(f"[layer1] no live Layer 0 nodes reporting", flush=True)
            else:
                z = sum(r * cmath.exp(1j * psi) for r, psi in live.values()) / len(live)
                r1, psi1 = abs(z), cmath.phase(z)
                send_report(out_sock, SELF_NAME, r1, psi1)
                detail = " ".join(f"{n}: r={r:.3f} psi={psi:+.3f}"
                                   for n, (r, psi) in sorted(live.items()))
                print(f"[layer1] r1={r1:.3f} psi1={psi1:+.3f}  n_live={len(live)}  {detail}",
                      flush=True)

        time.sleep(0.05)


if __name__ == "__main__":
    main()
