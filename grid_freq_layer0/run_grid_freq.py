"""run_grid_freq.py — Layer 0 driven by real UK national grid frequency.
Same shape as market_layer0/run_test.py: one scalar telemetry stream (a
single national frequency reading, not per-core) drives N_NODES
pseudo-oscillators uniformly.

Reference/target is exact, not calibrated: 50.00Hz is the UK grid's real
statutory nominal frequency, not a guessed or warm-up-averaged baseline
the way market_layer0's vol_ref or pi1_thermal's TARGET_TEMP_FRAC needed
to be -- deviation from it is what National Grid ESO's own frequency
response services are already built to correct.

MAX_DEV_HZ=0.5 (the ceiling used to normalize deviation into value_frac)
picked from real data, not guessed -- checked live 2026-08-17 against
~1hr of real freq.csv history: mean |deviation|=0.075Hz, max
observed=0.143Hz, both comfortably inside the UK's normal statutory
operating band (49.8-50.2Hz) with room before the wider 49.5-50.5Hz
emergency limits. At that real deviation scale, gain = K_BASE(6000) *
(0.075/0.5) = 900, ~4.3x margin over OMEGA0(209.4) -- confirmed BEFORE
deploying that K_BASE=6000 (unchanged from market_layer0, no divergence
needed) would actually lock, unlike pi1_thermal_layer0's first attempt
which deployed on a guess and had to be retuned live.
"""
import sys
import time

from grid_freq_telemetry import read_last_frequency
from layer0_oscillator import (Layer0Node, gain_from_deviation,
                                integrate_interval, order_parameter)
from layer0_report import mcast_out, send_report

NODE_NAME = "gridfreq"
N_NODES = 8
TARGET_HZ = 50.00
MAX_DEV_HZ = 0.5   # see module docstring -- calibrated from real data, not guessed
REPORT_INTERVAL_S = 0.5
# No arg -> run forever (production default). Pass an explicit duration
# for a bounded test run instead.
TOTAL_DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else None


def main():
    freq_hz, ts = read_last_frequency()
    print(f"[layer0-gridfreq] first read: {freq_hz} Hz @ {ts}", flush=True)

    nodes = [Layer0Node(i) for i in range(N_NODES)]
    report_sock = mcast_out()

    tick = 0
    while TOTAL_DURATION_S is None or tick * REPORT_INTERVAL_S < TOTAL_DURATION_S:
        freq_hz, ts = read_last_frequency()
        if freq_hz is None:
            # No data yet (live_ingest hasn't written a first row) --
            # zero deviation, zero gain, nodes free-run rather than
            # fabricating a fake 50.0Hz reading.
            value_frac, target_frac = 0.0, 0.0
        else:
            value_frac = min(1.0, abs(freq_hz - TARGET_HZ) / MAX_DEV_HZ)
            target_frac = 0.0
        gain = gain_from_deviation(value_frac, target_frac)

        integrate_interval(nodes, [gain] * N_NODES, REPORT_INTERVAL_S)
        r, psi = order_parameter(nodes)
        send_report(report_sock, NODE_NAME, r, psi)

        print(f"[{tick * REPORT_INTERVAL_S:6.1f}s] r={r:.3f} psi={psi:+.3f}  "
              f"freq_hz={freq_hz}  dev_hz={None if freq_hz is None else freq_hz - TARGET_HZ:+.3f}  "
              f"gain={gain:6.1f}  reading_ts={ts}", flush=True)

        tick += 1
        time.sleep(REPORT_INTERVAL_S)


if __name__ == "__main__":
    main()
