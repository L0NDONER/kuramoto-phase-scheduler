"""cpu_telemetry.py — real per-core CPU load from /proc/stat, plus
frequency, thermal-zone temp, and a category breakdown of busy time
(user/system/irq/softirq). No external dependencies (unlike
gpu_layer0's pynvml, this only needs stdlib -- /proc is always there).

Extended from the shared mint_cpu_layer0 version specifically for
deluge_watch's diagnostic use case: knowing a core is busy doesn't say
WHY, and this session's real example (a core flagged by
sustained_deviation_watch.py during a real Deluge download) turned out
to sometimes be network-interrupt-driven (heavy softirq, confirmed live
via mpstat) and sometimes Deluge's own process work (piece-hash
verification etc, confirmed via `ps` showing real deluge-gtk %CPU) --
two different real causes that looked identical in load_frac alone.
soft_frac/irq_frac/usr_frac/sys_frac let a consumer report which one it
actually was without a human running mpstat by hand each time. Other
copies of this file elsewhere in the repo don't need this breakdown for
their own use (plain deviation-from-target gain doesn't care why a core
is loaded), so it isn't propagated there -- this stays specific to
deluge_watch.

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

# /proc/stat per-core field order (after the "cpuN" label):
# user nice system idle iowait irq softirq steal guest guest_nice
_USER, _NICE, _SYSTEM, _IDLE, _IOWAIT, _IRQ, _SOFTIRQ = range(7)


@dataclass
class CpuSample:
    core: int
    load_frac: float      # 0..1, fraction of the interval spent non-idle
    usr_frac: float        # fraction spent in user+nice (this core's own processes)
    sys_frac: float         # fraction spent in kernel syscall/system work
    irq_frac: float          # fraction spent in hardware interrupt handlers
    soft_frac: float          # fraction spent in softirq -- network packet
                               # processing lands here, the signature that
                               # caught the Deluge case
    freq_mhz: float
    temp_c: float


def _read_stat_lines():
    """Returns {core_index: raw field tuple} for each per-core line
    (cpu0, cpu1, ...), skipping the aggregate 'cpu' line -- the full
    /proc/stat fields, not pre-reduced to idle/total, so sample_all() can
    break down which category any given interval's busy time fell into,
    not just whether the core was busy."""
    out = {}
    with open(STAT_PATH) as f:
        for line in f:
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            core = int(parts[0][3:])
            fields = tuple(int(x) for x in parts[1:])
            out[core] = fields
    return out


class CpuTelemetry:
    def __init__(self):
        self.n_cores = len(_read_stat_lines())
        self._prev = None   # {core: raw field tuple} from the last sample() call

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
            fields = cur[core]
            if self._prev is None or core not in self._prev:
                samples.append(None)
                continue
            prev = self._prev[core]
            d = [fields[i] - prev[i] for i in range(len(fields))]
            d_total = sum(d)

            if d_total > 0:
                d_idle = d[_IDLE] + d[_IOWAIT]
                load_frac = 1.0 - (d_idle / d_total)
                usr_frac = (d[_USER] + d[_NICE]) / d_total
                sys_frac = d[_SYSTEM] / d_total
                irq_frac = d[_IRQ] / d_total
                soft_frac = d[_SOFTIRQ] / d_total
            else:
                load_frac = usr_frac = sys_frac = irq_frac = soft_frac = 0.0

            samples.append(CpuSample(core=core, load_frac=load_frac,
                                      usr_frac=usr_frac, sys_frac=sys_frac,
                                      irq_frac=irq_frac, soft_frac=soft_frac,
                                      freq_mhz=self._read_freq_mhz(core),
                                      temp_c=temp))
        self._prev = cur
        return samples
