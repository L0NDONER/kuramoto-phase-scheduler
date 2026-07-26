#################################################################################
#                                                                               #
#             IF YOU ARE NOT MARTIN OR UBUNTU, KINDLY DISAPPEAR L0NDONER.       #
#                                                                               #
#################################################################################
#!/usr/bin/env python3
import pathlib
import re
import random
import socket
import statistics as stats_mod
import subprocess
import threading
import time
from statistics import median

try:
    import psutil
except ImportError:
    psutil = None

CPUFREQ_PATH = pathlib.Path("/sys/devices/system/cpu/cpu0/cpufreq")
TC_BIN       = "/usr/sbin/tc"
ETH_DEV      = "eth0"
MINT_IP      = "10.0.0.71"
MINT_PORT    = 5055

# ---------- Living swarm configuration ----------
ALPHA            = 0.1    # EMA pull toward fusion vote
NOISE_FRAC       = 0.005  # tiny state drift per tick
RESPAWN_ENABLED  = True


# ---------- Arm (hysteresis) ----------

class Arm:
    def __init__(self, enter_panic, exit_panic, enter_recovery, exit_recovery):
        self.state         = 0
        self.enter_panic   = enter_panic
        self.exit_panic    = exit_panic
        self.enter_recovery   = enter_recovery
        self.exit_recovery    = exit_recovery

    def update(self, signal):
        if signal >= self.enter_panic:
            self.state = -1
            return self.state
        if signal <= self.enter_recovery:
            self.state = +1
            return self.state
        if self.state == -1 and signal > self.exit_panic:
            return -1
        if self.state == +1 and signal < self.exit_recovery:
            return +1
        self.state = 0
        return 0


# ---------- DVFS ----------

def get_available_frequencies():
    path = CPUFREQ_PATH / "scaling_available_frequencies"
    if not path.exists():
        raise RuntimeError("CPU frequency sysfs not found.")
    return sorted(int(f) for f in path.read_text().strip().split())


def get_current_frequency():
    return int((CPUFREQ_PATH / "scaling_cur_freq").read_text().strip())


def set_userspace_governor():
    (CPUFREQ_PATH / "scaling_governor").write_text("userspace")


def set_frequency_safe(freq: int, avail: list[int]) -> int:
    closest = min(avail, key=lambda f: abs(f - freq))
    (CPUFREQ_PATH / "scaling_setspeed").write_text(f"{closest}\n")
    return closest


# ---------- Median-of-medians ----------

def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def median_of_medians(values, chunk_size=3):
    values = list(values)
    if len(values) <= chunk_size:
        return median(values)
    return median_of_medians([median(c) for c in chunk(values, chunk_size)], chunk_size)


# ---------- Sensors ----------

def read_cpu_temp():
    if psutil is None:
        return None
    temps = psutil.sensors_temperatures()
    for key in ("cpu-thermal", "cpu_thermal"):
        if key in temps and temps[key]:
            return temps[key][0].current
    return None


def read_queue_backlog() -> int:
    try:
        out = subprocess.check_output(
            ["sudo", TC_BIN, "-s", "qdisc", "show", "dev", ETH_DEV],
            stderr=subprocess.DEVNULL, text=True, timeout=1,
        )
        in_root = False
        for line in out.splitlines():
            if "htb" in line and "root" in line:
                in_root = True
            elif in_root and "backlog" in line:
                m = re.search(r'backlog\s+\S+\s+(\d+)p', line)
                return int(m.group(1)) if m else 0
    except Exception:
        pass
    try:
        nfq = pathlib.Path("/proc/net/nfnetlink_queue")
        if nfq.exists():
            parts = nfq.read_text().split()
            if len(parts) >= 3:
                return int(parts[2])
    except Exception:
        pass
    return 0


def read_decode_drops() -> float:
    with _fs_lock:
        if _decode_ts == 0.0 or (time.monotonic() - _decode_ts) > DECODE_STALE_S:
            return 0.0   # no recent telemetry -- don't trust a frozen old reading
        return _decode_drops


TC_CLASSID  = "1:10"                    # YouTube / CDN / torrent (uncapped)
LINE_RATE   = 1_000 * 1_000_000        # 1 Gbps in bits

_link_last_bytes: int | None = None

def _read_tc_class_bytes() -> int | None:
    try:
        out = subprocess.check_output(
            ["sudo", TC_BIN, "-s", "class", "show", "dev", ETH_DEV],
            stderr=subprocess.DEVNULL, text=True, timeout=1,
        )
        for block in out.split("\n\n"):
            if f"class htb {TC_CLASSID}" in block:
                for line in block.splitlines():
                    # Pi tc format: " Sent 12345 bytes 678 pkt ..."
                    if "Sent" in line and "bytes" in line:
                        parts = line.strip().split()
                        return int(parts[1])
    except Exception:
        pass
    return None

def read_link_utilisation(elapsed: float) -> float:
    """Bytes-per-interval on HTB class 1:10 → utilisation %. 50.0 on error."""
    global _link_last_bytes
    bytes_now = _read_tc_class_bytes()
    if bytes_now is None:
        return 50.0
    prev, _link_last_bytes = _link_last_bytes, bytes_now
    if prev is None or elapsed <= 0:
        return 50.0
    delta = bytes_now - prev
    if delta < 0:
        return 50.0
    return min(100.0, (delta * 8) / elapsed / LINE_RATE * 100.0)


# ---------- Arm instances ----------
#                               enter_panic  exit_panic  enter_recovery  exit_recovery
ARMS = {
    "thermal":   Arm(            70.0,        68.0,       60.0,           63.0),
    "nfqueue":   Arm(            30,          15,         0,              5),
    "sdio":      Arm(            5,           3,          0,              1),
    # jitter/touch_rtt recovery thresholds below are grounded in live
    # observed values (tick cadence ~1.00-1.02, rtt ~0.8-1.0ms) — the
    # previous -1.0/0.0 floors were below the signal's physical minimum
    # (a ratio and a round-trip time can't go negative), so those two
    # arms could never vote +1/recovery, only 0 or -1.
    "jitter":    Arm(            1.5,         1.2,        1.02,           1.05),
    # DECODE is now a real dropped-frame count (0 when healthy), not ms
    # latency — enter_recovery/exit_recovery are set unreachable (delta is
    # clamped >=0 in fs_telemetry.sh) since "0 drops" is neutral-healthy,
    # not a positive signal to speed up; any drop (>=1) is a real problem.
    "decode":    Arm(             1.0,         0.0,       -1.0,           -0.5),
    "touch_rtt": Arm(            8.0,         5.0,        1.5,            2.5),
    "mint_load": Arm(            180,         150,        30,             60),
    "link_util": Arm(            90.0,        87.0,       60.0,           65.0),  # %
    "fs_load":   Arm(            75.0,        70.0,       40.0,           45.0),  # % Firestick CPU
    "fs_jitter": Arm(             5.0,         3.0,        0.0,            2.0),  # ms tick jitter
}


# ---------- pi_touch background thread ----------

_touch = {"rtt_ms": 5.0, "mint_load": 100, "ok": False}
_touch_lock = threading.Lock()

def _touch_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    rtts: list[float] = []
    while True:
        t0 = time.monotonic()
        try:
            sock.sendto(b'\x01', (MINT_IP, MINT_PORT))
            data, _ = sock.recvfrom(16)
            rtt = (time.monotonic() - t0) * 1000  # ms
            mint_load = data[0]
            rtts.append(rtt)
            if len(rtts) > 10:
                rtts.pop(0)
            with _touch_lock:
                _touch["rtt_ms"]    = rtt
                _touch["mint_load"] = mint_load
                _touch["ok"]        = True
        except socket.timeout:
            rtts.clear()
            with _touch_lock:
                _touch["rtt_ms"]    = 5.0   # neutral default
                _touch["mint_load"] = 100
                _touch["ok"]        = False
        time.sleep(0.2)

threading.Thread(target=_touch_loop, daemon=True, name="pi_touch").start()


# ---------- Firestick telemetry listener ----------

FS_TELEM_PORT = 5057
DECODE_STALE_S = 3.0   # telemetry restarts every 5-10s; older than this is not "live"
_fs_load:     float | None = None
_fs_rssi:     float | None = None
_fs_jitter_ms: float = 0.0   # neutral until first packet
_decode_drops: float = 0.0   # frames dropped since last tick; neutral until first packet
_decode_ts:   float = 0.0    # monotonic time of last DECODE reading; 0 = never received
_fs_lock = threading.Lock()

def _fs_listener_loop():
    global _fs_load, _fs_rssi, _fs_jitter_ms, _decode_drops, _decode_ts
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", FS_TELEM_PORT))
    while True:
        try:
            data, _ = sock.recvfrom(512)
            parts = data.decode(errors="replace").strip().split(",")
            it = iter(parts)
            kv = dict(zip(it, it))
            with _fs_lock:
                if "LOAD"   in kv: _fs_load      = float(kv["LOAD"])
                if "RSSI"   in kv: _fs_rssi      = float(kv["RSSI"])
                if "DECODE" in kv:
                    _decode_drops = float(kv["DECODE"])
                    _decode_ts    = time.monotonic()
                if "JITTER" in kv: _fs_jitter_ms = float(kv["JITTER"])
        except Exception:
            pass

threading.Thread(target=_fs_listener_loop, daemon=True, name="fs_telem").start()


# ---------- Signal readers ----------

def read_sdio_signal() -> float:
    try:
        stat = pathlib.Path("/sys/block/mmcblk0/stat").read_text().split()
        return float(stat[8])
    except Exception:
        return 0.0


# ---------- Named arm functions (zero-arg, read own signals) ----------
# _tick_elapsed is set at the top of each swarm tick before arms are called.

_tick_elapsed: float = 1.0

def thermal_arm() -> int:
    t = read_cpu_temp()
    return ARMS["thermal"].update(t if t is not None else 65.0)

def nfqueue_arm() -> int:
    return ARMS["nfqueue"].update(read_queue_backlog())

def sdio_arm() -> int:
    return ARMS["sdio"].update(read_sdio_signal())

def jitter_arm() -> int:
    return ARMS["jitter"].update(_tick_elapsed)   # normalised below in loop

def decode_arm() -> int:
    return ARMS["decode"].update(read_decode_drops())

def touch_rtt_arm() -> int:
    with _touch_lock:
        return ARMS["touch_rtt"].update(_touch["rtt_ms"])

def mint_load_arm() -> int:
    with _touch_lock:
        return ARMS["mint_load"].update(_touch["mint_load"])

def link_util_arm() -> int:
    return ARMS["link_util"].update(read_link_utilisation(_tick_elapsed))

def firestick_load_arm() -> int:
    with _fs_lock:
        load = _fs_load
    if load is None:
        return 0
    return ARMS["fs_load"].update(load)

def firestick_jitter_arm() -> int:
    with _fs_lock:
        j = _fs_jitter_ms
    return ARMS["fs_jitter"].update(j)


# ---------- Main loop ----------

def run_loop(interval=1.0):
    set_userspace_governor()
    avail = get_available_frequencies()
    print("Available frequencies (kHz):", avail)

    floor   = avail[0]
    ceiling = avail[-1]

    # normalised state: -1.0 = floor, 0.0 = mid, +1.0 = ceiling
    current = get_current_frequency()
    state   = (current - floor) / (ceiling - floor) * 2.0 - 1.0

    t_last = time.monotonic()

    while True:
        t_now   = time.monotonic()
        elapsed = t_now - t_last
        t_last  = t_now

        global _tick_elapsed
        _tick_elapsed = elapsed / interval   # normalised for jitter arm

        # snapshot key signals for display (arms re-read internally)
        _snap_temp    = read_cpu_temp()
        _snap_backlog = read_queue_backlog()
        with _touch_lock:
            _snap_rtt  = _touch["rtt_ms"]
            _snap_mint = _touch["mint_load"]

        arms = [
            thermal_arm(),
            nfqueue_arm(),
            sdio_arm(),
            jitter_arm(),
            decode_arm(),
            touch_rtt_arm(),
            mint_load_arm(),
            link_util_arm(),
            firestick_load_arm(),
            firestick_jitter_arm(),
        ]

        fusion_vote = float(median_of_medians(arms))

        # EMA nudge in normalised space
        state += ALPHA * (fusion_vote - state)
        state  = max(-1.0, min(1.0, state))

        # tiny drift
        state += random.uniform(-NOISE_FRAC, NOISE_FRAC)
        state  = max(-1.0, min(1.0, state))

        freq    = int(floor + (state + 1.0) / 2.0 * (ceiling - floor))
        applied = set_frequency_safe(freq, avail)

        label = "PANIC" if fusion_vote < -0.3 else ("RECOVERY" if fusion_vote > 0.3 else "PLATEAU")

        print(
            f"[{label}] "
            f"arms={arms} fusion={fusion_vote:+.1f} "
            f"state={state:+.3f} freq={freq} applied={applied} "
            f"temp={round(_snap_temp, 2) if _snap_temp else 'n/a'} "
            f"backlog={_snap_backlog}p rtt={_snap_rtt:.2f}ms mint={_snap_mint}"
        )

        time.sleep(interval)


if __name__ == "__main__":
    while RESPAWN_ENABLED:
        try:
            run_loop(interval=1.0)
        except Exception as e:
            print(f"[respawn] crashed: {e} — restarting in 2s")
            time.sleep(2)
    run_loop(interval=1.0)
