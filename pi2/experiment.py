#!/usr/bin/env python3
"""
pi2/experiment.py — two-neuron characterisation harness.

Four regimes, each 180s under fixed 80% load on 4 cores:
  baseline   — no neurons; 4 cores, 1500 MHz throughout
  dvfs       — DVFS neuron only; 4 cores, freq free
  topology   — topology neuron only; cores free, freq=1500 MHz
  both       — both neurons free

Measures per regime:
  thermal mean, variance, peak
  cooling time from peak back to 72°C
  PARK/UNPARK commit count per neuron
  final settled freq and core count

Prereqs (on Mint): SSH tunnel up, nazare running, cerebellum running on EC2.
Usage: python3 pi2/experiment.py
"""
import subprocess, time, statistics, csv, sys

PI2          = "pi2"
LOAD_PCT     = 80
LOAD_CORES   = 4
OBS_SECS     = 180
POLL_SECS    = 5
COOL_C       = 72.0
COOL_LIMIT   = 600
MAX_FREQ_KHZ = 1500000
MIN_FREQ_KHZ = 600000
NCPUS        = 4

# ── SSH helpers ──────────────────────────────────────────────────────────────

def ssh(cmd):
    r = subprocess.run(["ssh", PI2, cmd], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def temp():
    try:
        return int(ssh("cat /sys/class/thermal/thermal_zone0/temp")) / 1000.0
    except Exception:
        return None

def cur_freq():
    try:
        return int(ssh("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")) // 1000
    except Exception:
        return None

def cur_cores():
    s = ssh("cat /sys/fs/cgroup/shaper/cpuset.cpus 2>/dev/null || echo 0-3")
    return int(s.split("-")[1]) + 1 if "-" in s else 1

# ── plant control ────────────────────────────────────────────────────────────

def stop_all():
    for p in ["pi2_reader", "pi2_actuator", "pi2_dvfs_reader", "pi2_dvfs_actuator", "stress-ng"]:
        ssh(f"sudo pkill -f {p} 2>/dev/null || true")
    time.sleep(2)

def reset_plant():
    ssh("sudo mkdir -p /sys/fs/cgroup/shaper")
    ssh("sudo sh -c '"
        "echo 0 > /sys/fs/cgroup/shaper/cpuset.mems 2>/dev/null; "
        "echo 0-3 > /sys/fs/cgroup/shaper/cpuset.cpus 2>/dev/null; "
        "true'")
    for cpu in range(NCPUS):
        ssh(f"sudo sh -c '"
            f"echo {MAX_FREQ_KHZ} > /sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_max_freq 2>/dev/null; "
            f"echo {MIN_FREQ_KHZ} > /sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_min_freq 2>/dev/null; "
            f"true'")

def pin_freq(khz):
    for cpu in range(NCPUS):
        ssh(f"sudo sh -c '"
            f"echo {khz} > /sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_min_freq; "
            f"echo {khz} > /sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_max_freq'")

def start_stress():
    ssh(f"sudo stress-ng --cpu {LOAD_CORES} --cpu-load {LOAD_PCT} --timeout 0 > /dev/null 2>&1 &")
    time.sleep(1)

def clear_logs():
    for log in ["pi2_reader.log", "pi2_actuator.log",
                "pi2_dvfs_reader.log", "pi2_dvfs_actuator.log"]:
        ssh(f"sudo rm -f /home/pi/{log}")

# ── wait for cooldown ────────────────────────────────────────────────────────

def wait_cool():
    t0 = time.time()
    while time.time() - t0 < COOL_LIMIT:
        t = temp()
        if t is not None:
            print(f"\r  cooling: {t:.1f}°C ", end="", flush=True)
            if t <= COOL_C:
                elapsed = time.time() - t0
                print(f"\r  cooled to {t:.1f}°C in {elapsed:.0f}s   ", flush=True)
                return elapsed
        time.sleep(POLL_SECS)
    print(flush=True)
    return COOL_LIMIT

# ── commit counting ──────────────────────────────────────────────────────────

def count_commits(log):
    # commit lines contain " → " (space-arrow-space)
    out = ssh(f"grep -c ' → ' /home/pi/{log} 2>/dev/null || echo 0")
    try:
        return int(out.strip())
    except Exception:
        return 0

# ── regimes ──────────────────────────────────────────────────────────────────

REGIMES = ["baseline", "dvfs", "topology", "both"]
LABELS  = {
    "baseline": "Baseline (no neurons)",
    "dvfs":     "DVFS only",
    "topology": "Topology only",
    "both":     "Both neurons",
}

results = []

for regime in REGIMES:
    print(f"\n{'='*60}", flush=True)
    print(f"Regime: {LABELS[regime]}", flush=True)

    stop_all()
    reset_plant()
    clear_logs()

    t = temp()
    if t is not None and t > COOL_C:
        print(f"  hot at {t:.1f}°C, waiting for cooldown...", flush=True)
        wait_cool()
    else:
        print(f"  cool: {t:.1f}°C", flush=True)

    # ── start neurons ────────────────────────────────────────────────────────
    if regime in ("dvfs", "both"):
        ssh("sudo sh -c '/home/pi/pi2_dvfs_reader > /home/pi/pi2_dvfs_reader.log 2>&1 &'")
        ssh("sudo sh -c 'python3 /home/pi/pi2_dvfs_actuator.py > /home/pi/pi2_dvfs_actuator.log 2>&1 &'")
        time.sleep(1)

    if regime in ("topology", "both"):
        ssh("sudo sh -c '/home/pi/pi2_reader > /home/pi/pi2_reader.log 2>&1 &'")
        ssh(f"sudo sh -c 'python3 /home/pi/pi2_actuator.py --load {LOAD_PCT} --cores-start 4 "
            f"> /home/pi/pi2_actuator.log 2>&1 &'")
        time.sleep(3)   # let topology actuator launch stress-ng

    if regime == "topology":
        pin_freq(MAX_FREQ_KHZ)  # hold freq at max for topology-only

    # baseline and dvfs need external stress (topology actuator provides its own)
    if regime in ("baseline", "dvfs"):
        start_stress()

    # ── observe ──────────────────────────────────────────────────────────────
    temps = []
    print(f"  observing {OBS_SECS}s...", flush=True)
    t0 = time.time()
    while time.time() - t0 < OBS_SECS:
        tv = temp()
        if tv is not None:
            temps.append(tv)
            f = cur_freq()
            c = cur_cores()
            elapsed = int(time.time() - t0)
            print(f"  t={elapsed:3d}s  {tv:.1f}°C  {f} MHz  {c} cores", flush=True)
        time.sleep(POLL_SECS)

    final_freq  = cur_freq()
    final_cores = cur_cores()

    topo_commits = count_commits("pi2_actuator.log")
    dvfs_commits = count_commits("pi2_dvfs_actuator.log")

    # ── stop and measure cooling ──────────────────────────────────────────────
    stop_all()
    reset_plant()
    time.sleep(2)

    hot = temp()
    print(f"  measuring cooling from {hot:.1f}°C...", flush=True)
    cool_secs = wait_cool()

    # ── summarise ────────────────────────────────────────────────────────────
    mean = statistics.mean(temps) if temps else 0
    var  = statistics.variance(temps) if len(temps) > 1 else 0
    peak = max(temps) if temps else 0

    rec = dict(
        regime      = LABELS[regime],
        n           = len(temps),
        mean        = round(mean, 2),
        variance    = round(var,  4),
        peak        = round(peak, 1),
        cool_secs   = round(cool_secs, 0),
        topo_commits= topo_commits,
        dvfs_commits= dvfs_commits,
        final_freq  = final_freq or 0,
        final_cores = final_cores or 0,
    )
    results.append(rec)
    print(f"  done — mean={mean:.1f}°C  σ²={var:.4f}  peak={peak:.1f}°C  "
          f"cool={cool_secs:.0f}s  commits topo={topo_commits} dvfs={dvfs_commits}  "
          f"{final_freq}MHz {final_cores}cores", flush=True)

# ── final table ───────────────────────────────────────────────────────────────

print("\n" + "="*80)
print(f"{'Regime':<22} {'mean':>6} {'σ²':>8} {'peak':>6} {'cool_s':>7} "
      f"{'topo_c':>7} {'dvfs_c':>7} {'MHz':>6} {'cores':>6}")
print("-"*80)
for r in results:
    print(f"{r['regime']:<22} {r['mean']:>6.1f} {r['variance']:>8.4f} {r['peak']:>6.1f} "
          f"{r['cool_secs']:>7.0f} {r['topo_commits']:>7} {r['dvfs_commits']:>7} "
          f"{r['final_freq']:>6} {r['final_cores']:>6}")

# ── delta summary ─────────────────────────────────────────────────────────────
if len(results) == 4:
    base = results[0]
    print()
    for r in results[1:]:
        dvar  = (base['variance'] - r['variance']) / max(base['variance'], 1e-9) * 100
        dpeak = base['peak'] - r['peak']
        dcool = base['cool_secs'] - r['cool_secs']
        print(f"{r['regime']:<22}  σ² {dvar:+.1f}%  peak {dpeak:+.1f}°C  cool {dcool:+.0f}s")

# ── CSV ──────────────────────────────────────────────────────────────────────
out_csv = "/home/martin/claude/pi2/experiment_results.csv"
with open(out_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(results)
print(f"\nSaved → {out_csv}")
