"""consensus_test_simple.py — the plain, no-oscillator comparison for
tonight's multi-signal consensus test. Tails the real Layer 0 sources'
own stdout logs directly (market_layer0/run_test.py's value_frac,
mint_cpu_layer0/run_local.py's avg_load, netlat_layer0/run_netlat.py's
avg_rtt vs target, grid_freq_layer0/run_grid_freq.py's freq_hz), keeps a
rolling mean/std per source (deque-based, window=60 samples), and
reports a simple average |z-score| across whichever sources have
reported recently -- no RK4, no coupling, no gain, just "how many
standard deviations from each source's own recent normal is it right
now, averaged."

Compared directly against layer1/layer1.log's real r1 over the same
wall-clock window: does the oscillator consensus (r1) diverge from this
in any way that matters, or does it track the same information a plain
rolling z-score already captures -- same question load_probe and
depth_gate_ablation already answered for single-signal detection, now
asked for genuine multi-signal consensus. Original validation (3
sources: market/mint/netlat) found correlation(r1, avg|z|) ~= -0.05 over
9 real minutes -- essentially uncorrelated, not redundant information.

pi1_thermal_layer0 deliberately excluded here, not silently forgotten --
its raw temp_c only exists in a log on Pi1 itself, not locally on Mint
like the other four. Reading it here would mean either a remote
shell_launch call inside this 0.5s poll loop (real added latency, not
comparable to the other sources' free local file tails) or deriving a
z-score-equivalent from its multicast psi instead of its raw telemetry
(a lossy, indirect proxy, not the same measurement this comparison is
supposed to make for every other source). Left out rather than faked.
grid_freq_layer0 IS included -- its freq_hz is a plain local log line,
same tail pattern as mint/netlat, no such problem.
"""
import re
import time
from collections import deque

MINT_LOG = "mint_cpu_layer0/run_local.log"
NETLAT_LOG = "netlat_layer0/run_netlat.log"
GRIDFREQ_LOG = "grid_freq_layer0/run_grid_freq.log"

WINDOW = 60
POLL_INTERVAL_S = 0.5

MARKET_RE = re.compile(r"value_frac=([\d.]+)")
MINT_RE = re.compile(r"avg_load=([\d.]+)%")
NETLAT_RE = re.compile(r"avg_rtt=([\d.]+)ms\s+target=([\d.]+)ms")
GRIDFREQ_RE = re.compile(r"freq_hz=([\d.]+)")


class RollingZ:
    def __init__(self, window=WINDOW):
        self.buf = deque(maxlen=window)

    def push_and_z(self, value):
        if len(self.buf) < 5:
            self.buf.append(value)
            return None
        mean = sum(self.buf) / len(self.buf)
        var = sum((x - mean) ** 2 for x in self.buf) / len(self.buf)
        std = var ** 0.5
        z = abs(value - mean) / std if std > 1e-9 else 0.0
        self.buf.append(value)
        return z


def tail_new(path, pos):
    try:
        with open(path) as f:
            f.seek(pos)
            lines = f.readlines()
            return lines, f.tell()
    except FileNotFoundError:
        return [], pos


def main():
    import subprocess
    # market_layer0 runs under systemd (journal-only, no plain log file
    # here) -- pull its most recent value_frac via journalctl instead of
    # tailing a file, same information, different source.
    def latest_market_value_frac():
        out = subprocess.run(
            ["journalctl", "--user", "-u", "market-layer0.service",
             "--no-pager", "-n", "5"],
            capture_output=True, text=True,
        ).stdout
        m = None
        for line in out.splitlines():
            mm = MARKET_RE.search(line)
            if mm:
                m = mm
        return float(m.group(1)) if m else None

    zs = {"market": RollingZ(), "mint": RollingZ(), "netlat": RollingZ(),
          "gridfreq": RollingZ()}
    pos_mint = pos_netlat = pos_gridfreq = 0

    print("[simple_consensus] watching market (journalctl)/mint/netlat/gridfreq "
          "raw deviations, plain rolling z-score, no oscillator "
          "(pi1_thermal excluded -- see module docstring)", flush=True)

    start = time.time()
    while True:
        latest_z = {}

        mv = latest_market_value_frac()
        if mv is not None:
            z = zs["market"].push_and_z(mv)
            if z is not None:
                latest_z["market"] = z

        lines, pos_mint = tail_new(MINT_LOG, pos_mint)
        for line in lines:
            m = MINT_RE.search(line)
            if m:
                z = zs["mint"].push_and_z(float(m.group(1)) / 100.0)
                if z is not None:
                    latest_z["mint"] = z

        lines, pos_netlat = tail_new(NETLAT_LOG, pos_netlat)
        for line in lines:
            m = NETLAT_RE.search(line)
            if m:
                rtt, target = float(m.group(1)), float(m.group(2))
                z = zs["netlat"].push_and_z(abs(rtt - target) / target)
                if z is not None:
                    latest_z["netlat"] = z

        lines, pos_gridfreq = tail_new(GRIDFREQ_LOG, pos_gridfreq)
        for line in lines:
            m = GRIDFREQ_RE.search(line)
            if m:
                z = zs["gridfreq"].push_and_z(float(m.group(1)))
                if z is not None:
                    latest_z["gridfreq"] = z

        if latest_z:
            avg_z = sum(latest_z.values()) / len(latest_z)
            elapsed = time.time() - start
            detail = " ".join(f"{k}={v:.2f}" for k, v in latest_z.items())
            print(f"[{elapsed:7.1f}s] avg|z|={avg_z:.3f}  ({detail})", flush=True)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
