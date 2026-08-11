#!/usr/bin/env python3
"""
quartz_wan_gain.py — Mint WAN egress shaper, driven by quartz_node's phase.

Replaces ns_wan_gain.py. That script read AxisPulse (theta, locked) from
quartz_beacon.py and NucleusState (temlum, e_C thermal signal) from a
Pi2 process that was never found running even before quartz_beacon.py
was decommissioned — so the thermal half of its formula was already
dead weight. This keeps only the part that still has a real input: the
phase carrier, now read from quartz_node's peer-health file instead of
a beacon multicast group.

  WAN_rate(t) = BaseWAN x G(theta)
  G = G_BASE + (1 - G_BASE) x carrier(theta)
  carrier(theta) = (1 - cos(theta)) / 2      -- trough=0 at theta=0, peak=1 at theta=pi

Input: /tmp/quartz_peer_health.json (written every ~1s by quartz_node) --
  first line {"self_phase": <rad>} for theta, plus per-peer "healthy"
  flags used only to confirm the substrate is actually locked, not just
  that the local daemon process happens to be alive.

Substrate considered down (hold at G_BASE floor, matching the "absence
of input should degrade toward boring, not park at the ceiling"
philosophy of the original) if the health file is missing, stale
(mtime older than HEALTH_TIMEOUT_S), has no parseable self_phase, or
no peer is marked healthy.

Output: tc HTB rate on IFACE 1:10, same RATE_MIN-RATE_MAX range and
N_STEPS quantisation as the original. Manages its own qdisc; tears it
down on SIGINT/SIGTERM.

Run: sudo python3 quartz_wan_gain.py [iface]
"""

import json, math, signal, subprocess, sys, time

IFACE    = sys.argv[1] if len(sys.argv) > 1 else "enp0s31f6"
TC       = "/usr/sbin/tc"
CLASSID  = "1:10"
RATE_MAX = 900     # Mbit nominal WAN ceil
RATE_MIN = 450     # Mbit floor (G=0)
N_STEPS  = 16       # quantisation -- limits tc syscall rate

G_BASE = 0.55        # floor gain at carrier trough (55% of RATE range) -- same as original

HEALTH_PATH    = "/tmp/quartz_peer_health.json"
HEALTH_TIMEOUT_S = 2.0   # daemon writes every ~1s; 2x margin against one missed write
POLL_S           = 0.2
SUBSTRATE_WARN_S = 60.0  # loud log cadence when down, same as original


def tc_run(args):
    subprocess.run([TC] + args, check=True)

def setup_qdisc():
    # Start at G_BASE, not RATE_MAX -- until the first healthy read there's
    # no signal to justify anything above the neutral floor.
    safe_mbit = RATE_MIN + round((RATE_MAX - RATE_MIN) * G_BASE)
    subprocess.run([TC, "qdisc", "del", "dev", IFACE, "root"], capture_output=True)
    tc_run(["qdisc", "add", "dev", IFACE, "root", "handle", "1:", "htb", "default", "10"])
    tc_run(["class", "add", "dev", IFACE, "parent", "1:", "classid", CLASSID,
            "htb", "rate", f"{safe_mbit}mbit", "ceil", f"{safe_mbit}mbit",
            "burst", "1500k", "quantum", "1514"])

def teardown_qdisc():
    subprocess.run([TC, "qdisc", "del", "dev", IFACE, "root"], capture_output=True)

def set_rate(mbit):
    tc_run(["class", "change", "dev", IFACE, "classid", CLASSID,
            "htb", "rate", f"{mbit}mbit", "ceil", f"{mbit}mbit",
            "burst", "1500k", "quantum", "1514"])


def read_substrate():
    """Returns (theta, healthy) or (None, False) if substrate is down."""
    try:
        mtime = __import__("os").path.getmtime(HEALTH_PATH)
        if time.time() - mtime > HEALTH_TIMEOUT_S:
            return None, False
        theta = None
        any_healthy = False
        with open(HEALTH_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "self_phase" in d:
                    theta = d["self_phase"]
                elif d.get("healthy"):
                    any_healthy = True
        if theta is None or not any_healthy:
            return None, False
        return theta, True
    except (FileNotFoundError, OSError):
        return None, False


def main():
    setup_qdisc()

    def _exit(sig, frame):
        teardown_qdisc()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)

    floor_mbit = RATE_MIN + round((RATE_MAX - RATE_MIN) * G_BASE)
    floor_step = round(G_BASE * N_STEPS)

    last_step = -1
    last_locked_t = None
    last_warn_t = 0.0

    print(f"[quartz_wan_gain] {IFACE} {CLASSID}  {RATE_MIN}-{RATE_MAX}Mbit  "
          f"steps={N_STEPS}  G_BASE={G_BASE}", flush=True)

    while True:
        theta, healthy = read_substrate()

        if not healthy:
            now = time.time()
            # Force the floor immediately, not just log about it — freezing
            # at whatever the last real rate happened to be would "park at
            # the ceiling" exactly when the design intent says not to.
            if last_step != floor_step:
                set_rate(floor_mbit)
                last_step = floor_step
                print(f"[quartz_wan_gain] substrate down -> forcing floor {floor_mbit}Mbit", flush=True)

            since = None if last_locked_t is None else now - last_locked_t
            if since is None or since > SUBSTRATE_WARN_S:
                if now - last_warn_t > SUBSTRATE_WARN_S:
                    since_str = "never" if last_locked_t is None else f"{since:.0f}s ago"
                    print(f"[quartz_wan_gain] SUBSTRATE DOWN -- no healthy peer "
                          f"(last locked {since_str}), holding at floor", flush=True)
                    last_warn_t = now
            time.sleep(POLL_S)
            continue

        last_locked_t = time.time()

        carrier = (1.0 - math.cos(theta)) / 2.0
        G_WAN = G_BASE + (1.0 - G_BASE) * carrier
        G_WAN = max(0.0, min(1.0, G_WAN))

        step = round(G_WAN * N_STEPS)
        rate_mbit = RATE_MIN + round((RATE_MAX - RATE_MIN) * step / N_STEPS)

        if step != last_step:
            set_rate(rate_mbit)
            last_step = step
            print(f"[quartz_wan_gain] theta={theta:.3f} carrier={carrier:.3f} "
                  f"G={G_WAN:.3f} -> {rate_mbit}Mbit", flush=True)

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
