#!/bin/bash
# bench.sh — Kuramoto DVFS energy surface mapping
#
# Workload: duty-cycle — 5 reps of (job + 3s idle), RAPL integrated over
# the full window. Captures active power AND idle-trough savings.
#
# Three modes: stock (performance gov), dvfs-all-hot, dvfs-park (1/8).
# Output: bench_TIMESTAMP.csv + summary table.
#
# Usage: sudo ./bench.sh [reps]   (default 5)

set -uo pipefail

REPS=${1:-5}
IDLE_S=3                          # idle gap between jobs (lets DVFS trough)
RAPL=/sys/class/powercap/intel-rapl:0/energy_uj
READER=/home/martin/claude/cpu_reader
TS=$(date +%Y%m%d_%H%M%S)
CSV=/tmp/bench_${TS}.csv

echo "mode,rep,job_ms,job_J,job_W,window_ms,window_J,window_W,jobs_done" > "$CSV"

# ── helpers ──────────────────────────────────────────────────────────────────

rapl_uj() { cat "$RAPL"; }

single_job_ms() {
    local t0 t1
    t0=$(date +%s%3N)
    python3 -c "print(sum(i*i for i in range(10000000)))" > /dev/null
    t1=$(date +%s%3N)
    echo $(( t1 - t0 ))
}

# Run REPS × (job + idle), measure per-job and whole-window energy
run_mode() {
    local mode=$1
    echo "  reps=$REPS  idle=${IDLE_S}s between jobs"

    local win_e0 win_t0 win_e1 win_t1
    win_e0=$(rapl_uj); win_t0=$(date +%s%3N)

    local total_jobs=0
    for rep in $(seq 1 "$REPS"); do
        local e0 e1 t0 t1
        e0=$(rapl_uj); t0=$(date +%s%3N)
        python3 -c "print(sum(i*i for i in range(10000000)))" > /dev/null
        e1=$(rapl_uj); t1=$(date +%s%3N)

        local job_uj=$(( e1 - e0 ))
        local job_ms=$(( t1 - t0 ))

        python3 -c "
uj,ms = $job_uj, $job_ms
J  = uj/1e6; W = J/(ms/1000)
print(f'  rep=$rep  job: {J:.3f}J  {ms}ms  {W:.1f}W')
"
        total_jobs=$(( total_jobs + 1 ))
        sleep "$IDLE_S"
    done

    win_e1=$(rapl_uj); win_t1=$(date +%s%3N)
    local win_uj=$(( win_e1 - win_e0 ))
    local win_ms=$(( win_t1 - win_t0 ))

    python3 - "$mode" "$win_uj" "$win_ms" "$total_jobs" "$CSV" <<'PYEOF'
import sys, csv as csvmod

mode     = sys.argv[1]
win_uj   = int(sys.argv[2])
win_ms   = int(sys.argv[3])
jobs     = int(sys.argv[4])
csvpath  = sys.argv[5]

win_J    = win_uj / 1e6
win_ms_f = win_ms
win_W    = win_J / (win_ms_f / 1000)
j_per_job= win_J / jobs

print(f"  window: {win_J:.2f}J  {win_ms_f:.0f}ms  {win_W:.2f}W avg  →  {j_per_job:.3f}J/job")

with open(csvpath, 'a') as f:
    f.write(f"{mode},total,{win_ms_f:.0f},{win_J:.3f},{win_W:.2f},{win_ms_f:.0f},{win_J:.3f},{win_W:.2f},{jobs}\n")
PYEOF
}

is_locked() {
    # check axis log file or reader journal for recent lock signal
    local latest
    latest=$(ls -t /tmp/cpu_reader_*.csv 2>/dev/null | head -1)
    if [[ -n "$latest" ]]; then
        tail -5 "$latest" 2>/dev/null | awk -F, '$5 != "" {print}' | wc -l | grep -qv "^0$" && return 0
    fi
    return 1
}

wait_lock() {
    echo -n "  waiting for axis feed"
    for _ in $(seq 1 20); do
        if pgrep -f cpu_reader > /dev/null; then
            # give it a moment to start receiving ticks
            sleep 1
            echo " ready"
            return 0
        fi
        echo -n "."; sleep 1
    done
    echo " (proceeding)"
}

stop_cpu_reader() {
    pkill -f cpu_reader 2>/dev/null || true
    sleep 2
}

# ── MODE 1: stock ─────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " MODE 1: stock  (performance governor)"
echo "══════════════════════════════════════════"
stop_cpu_reader
cpupower frequency-set -g performance > /dev/null 2>&1 || true
sleep 1
run_mode "stock"

# ── MODE 2: dvfs all-hot ──────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " MODE 2: dvfs-all-hot  (8/8 cores)"
echo "══════════════════════════════════════════"
stop_cpu_reader
"$READER" > /tmp/cpu_reader_bench_allhot.log 2>&1 &
CPUPID=$!
wait_lock
sleep 3   # let it reach trough before first job
run_mode "dvfs-all-hot"
kill "$CPUPID" 2>/dev/null; sleep 2

# ── MODE 3: dvfs park ─────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " MODE 3: dvfs-park  (1 hot / 7 parked)"
echo "══════════════════════════════════════════"
stop_cpu_reader
"$READER" --park-cores > /tmp/cpu_reader_bench_park.log 2>&1 &
CPUPID=$!
wait_lock
sleep 3
run_mode "dvfs-park"
kill "$CPUPID" 2>/dev/null; sleep 2

cpupower frequency-set -g powersave > /dev/null 2>&1 || true

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " RESULTS  (window = ${REPS} jobs + ${IDLE_S}s idle each)"
echo "══════════════════════════════════════════"

python3 - "$CSV" <<'PYEOF'
import sys, csv

rows = [r for r in csv.DictReader(open(sys.argv[1])) if r['rep'] == 'total']
modes = ['stock', 'dvfs-all-hot', 'dvfs-park']

ref_J = ref_W = None
print(f"\n{'mode':<20} {'J (window)':>11} {'W avg':>8} {'J/job':>8}  {'ΔJ':>8}  {'ΔW':>8}")
print("─" * 72)
for mode in modes:
    rs = [r for r in rows if r['mode'] == mode]
    if not rs:
        continue
    r      = rs[0]
    win_J  = float(r['window_J'])
    win_W  = float(r['window_W'])
    jobs   = int(r['jobs_done'])
    j_job  = win_J / jobs
    if ref_J is None:
        ref_J, ref_W = win_J, win_W
    dJ = f"{(win_J - ref_J) / ref_J * 100:+.1f}%" if ref_J else "—"
    dW = f"{(win_W - ref_W) / ref_W * 100:+.1f}%" if ref_W else "—"
    print(f"{mode:<20} {win_J:>11.2f} {win_W:>8.2f} {j_job:>8.3f}  {dJ:>8}  {dW:>8}")
PYEOF

echo ""
echo "data → $CSV"
