#!/usr/bin/env python3
"""
plot_pd.py — raw pd(t) and smoothed pd_dev(t) from pd_log.csv.
"""
import csv
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

IN  = sys.argv[1] if len(sys.argv) > 1 else "pd_log.csv"
OUT = sys.argv[2] if len(sys.argv) > 2 else "pd_plot.png"

t, pd, pd_dev = [], [], []
with open(IN) as f:
    r = csv.DictReader(f)
    t0 = None
    for row in r:
        ts = float(row["t"])
        if t0 is None:
            t0 = ts
        t.append(ts - t0)
        pd.append(float(row["pd"]))
        pd_dev.append(float(row["pd_dev"]))

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
ax1.plot(t, pd, ".", ms=1, alpha=0.4)
ax1.set_ylabel("pd (rad)")
ax1.set_title(f"raw pd(t) — {len(t)} samples over {t[-1]/3600:.2f}h")

ax2.plot(t, pd_dev, lw=0.8)
ax2.set_ylabel("pd_dev (smoothed)")
ax2.set_xlabel("t (s)")

fig.tight_layout()
fig.savefig(OUT, dpi=130)
print(f"[plot_pd] {len(t)} samples → {OUT}")
