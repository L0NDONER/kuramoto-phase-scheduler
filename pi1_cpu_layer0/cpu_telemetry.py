"""cpu_telemetry.py — real per-core CPU load from /proc/stat, plus
frequency and thermal-zone temp. No external dependencies (unlike
gpu_layer0's pynvml, this only needs stdlib -- /proc is always there).

/proc/stat gives cumulative jiffie counters, not an instantaneous load
fraction -- unlike NVML's power reading, this needs a delta between two
samples. First sample() call has no prior state to diff against, so it
returns None for every core; callers should discard the first sample,
same shape as the Firestick telemetry app's DECODE delta handling.
"""
import os
from dataclasses import dataclass

STAT_PATH = "/proc/stat"
THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
FREQ_PATH_FMT = "/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq"


@dataclass
class CpuSample:
    core: int
    load_frac: float      # 0..1, fraction of the interval spent non-idle
    freq_mhz: float
    temp_c: float


def _read_stat_lines():
    """Returns {core_index: (idle_jiffies, total_jiffies)} for each
    per-core line (cpu0, cpu1, ...), skipping the aggregate 'cpu' line."""
    out = {}
    with open(STAT_PATH) as f:
        for line in f:
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            core = int(parts[0][3:])
            fields = [int(x) for x in parts[1:]]
            idle = fields[3] + fields[4]   # idle + iowait
            total = sum(fields)
            out[core] = (idle, total)
    return out


class CpuTelemetry:
    def __init__(self):
        self.n_cores = len(_read_stat_lines())
        self._prev = None   # {core: (idle, total)} from the last sample() call

    def _read_freq_mhz(self, core):
        try:
            with open(FREQ_PATH_FMT.format(core)) as f:
                return int(f.read().strip()) / 1000.0
        except FileNotFoundError:
            return 0.0

    def _read_temp_c(self):
        try:
            with open(THERMAL_PATH) as f:
                return int(f.read().strip()) / 1000.0
        except FileNotFoundError:
            return 0.0

    def sample_all(self):
        """Returns a list of CpuSample, or None entries for cores where
        no prior sample exists yet to diff against (first call only)."""
        cur = _read_stat_lines()
        temp = self._read_temp_c()
        samples = []
        for core in sorted(cur):
            idle, total = cur[core]
            if self._prev is None or core not in self._prev:
                samples.append(None)
                continue
            prev_idle, prev_total = self._prev[core]
            d_idle = idle - prev_idle
            d_total = total - prev_total
            load_frac = 1.0 - (d_idle / d_total) if d_total > 0 else 0.0
            samples.append(CpuSample(core=core, load_frac=load_frac,
                                      freq_mhz=self._read_freq_mhz(core),
                                      temp_c=temp))
        self._prev = cur
        return samples
