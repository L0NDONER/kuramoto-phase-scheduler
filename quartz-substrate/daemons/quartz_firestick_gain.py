#!/usr/bin/env python3
"""
quartz_firestick_gain.py — global headroom multiplier over ALL Firestick
per-service tc classes, driven by quartz_node's phase/health.

shaper-setup.sh gives each streaming service its own tuned static class
(Netflix 15Mbps, Disney+ 8Mbps, BBC 4.5Mbps, Sky 5Mbps, YouTube/Prime/
Jellyfin uncapped, etc.) via quic-sni-gate.py's SNI marking. This daemon
does NOT retune those per-service — it applies one global multiplier G
on top of every one of them, same shape as quartz_wan_gain.py's WAN
egress modulation but scoped to the Firestick's classes specifically:

  rate(classid) = BASE_RATE[classid] x G
  G = G_BASE + (1 - G_BASE) x carrier(theta)
  carrier(theta) = (1 - cos(theta)) / 2

theta = self_phase from /tmp/quartz_peer_health.json (written by the
quartz_node daemon already running on this host). Substrate-down
(missing/stale health file, or no peer healthy) forces G to the floor
immediately — same fix as quartz_wan_gain.py's fault-injection finding:
freezing at the last computed rate during an outage would "park" each
service wherever it happened to be, not degrade toward boring.

This is a coarse "something's wrong with the substrate, back off
everything together" signal, not a per-service retune. Deliberately
does not touch quic-sni-gate.py's packet-classification path at all —
runs as its own independent loop against tc, decoupled from the NFQUEUE
hot path.

Run: sudo python3 quartz_firestick_gain.py
"""

import signal, subprocess, sys, time

from quartz_gain_common import run_gain_loop

TC   = "/usr/sbin/tc"
IFACE = "eth0"

# classid -> (base_rate_mbit, label). Excludes 1:1 (root placeholder) and
# 1:50 (Mint general — not Firestick traffic).
BASE_RATES = {
    "1:5":  (1000,   "Jellyfin"),
    "1:10": (1000,   "YouTube/Prime/default"),
    "1:11": (1,      "Google Ads"),
    "1:15": (15,     "Netflix"),
    "1:20": (5,      "Sky/catch-all"),
    "1:25": (8,      "Disney+"),
    "1:35": (4,      "ITVX"),
    "1:40": (4.5,    "BBC iPlayer"),
}

G_BASE = 0.55   # floor multiplier — same constant as quartz_wan_gain.py

HEALTH_PATH      = "/tmp/quartz_peer_health.json"
HEALTH_TIMEOUT_S = 2.0
POLL_S           = 0.2
SUBSTRATE_WARN_S = 60.0


def set_rate(classid, mbit):
    subprocess.run([TC, "class", "change", "dev", IFACE, "classid", classid,
                     "htb", "rate", f"{mbit:.3f}mbit", "ceil", f"{mbit:.3f}mbit"],
                    check=True)


def apply_gain(G):
    for classid, (base, _label) in BASE_RATES.items():
        set_rate(classid, base * G)


def on_floor():
    apply_gain(G_BASE)


def on_locked(theta, carrier, g):
    apply_gain(g)
    print(f"[quartz_firestick_gain] theta={theta:.3f} carrier={carrier:.3f} G={g:.3f}", flush=True)


def main():
    # Confirm every class actually exists before starting — this daemon
    # modulates shaper-setup.sh's classes, it doesn't create them.
    check = subprocess.run([TC, "class", "show", "dev", IFACE], capture_output=True, text=True)
    for classid in BASE_RATES:
        if classid not in check.stdout:
            print(f"[quartz_firestick_gain] FATAL: {classid} not found on {IFACE} — "
                  f"has shaper-setup.sh run?", file=sys.stderr)
            sys.exit(1)

    def _exit(sig, frame):
        # Restore full static rates on exit rather than leaving classes
        # wherever G last left them.
        apply_gain(1.0)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)

    G_STEPS = 16

    print(f"[quartz_firestick_gain] {IFACE}  {len(BASE_RATES)} classes  G_BASE={G_BASE}", flush=True)

    run_gain_loop(G_BASE, G_STEPS, on_locked, on_floor, "quartz_firestick_gain",
                  HEALTH_PATH, HEALTH_TIMEOUT_S, POLL_S, SUBSTRATE_WARN_S)


if __name__ == "__main__":
    main()
