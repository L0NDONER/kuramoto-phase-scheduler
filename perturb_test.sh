#!/bin/bash
# perturb_test.sh — balanced perturbation test for Kuramoto substrate
#
# Applies identical CPU stress to both Pis simultaneously.
# Records phi from reader journal before, during, and after.
# Reports: max drift, recovery time, signal dropout count.

set -uo pipefail

LOG=/tmp/perturb_$(date +%Y%m%d_%H%M%S).csv
SCHED_LOG=/tmp/perturb_sched_$$.txt
BASELINE=20    # seconds to record before stress
STRESS_DUR=30  # seconds of stress
RECOVER=90     # seconds to observe after stress

echo "level,t,phi,pd,event" > "$LOG"

log_phi() {
    local level="$1"
    local event="${2:-}"
    local t phi pd line
    t=$(date +%s%3N)
    line=$(sudo journalctl -u reader --no-pager -o cat -n 2 2>&1 | strings | grep -oE '[0-9]\.[0-9]{3}' | head -1 || true)
    phi=${line:-0}
    pd=$(sudo journalctl -u reader --no-pager -o cat -n 2 2>&1 | strings | grep -oE '3\.[0-9]{3,4}' | tail -1 || true)
    pd=${pd:-0}
    echo "$level,$t,$phi,$pd,$event"
}

count_signals() {
    grep -c "SUBMIT\|COLLECT" "$SCHED_LOG" 2>/dev/null || echo 0
}

echo "[perturb] logging to $LOG"
echo "[perturb] monitoring phase_sched signals..."

# capture phase_sched signals throughout test
timeout $((BASELINE + STRESS_DUR + RECOVER + 10)) cat /tmp/phase_sched >> "$SCHED_LOG" &
SCHED_PID=$!

# ── BASELINE ────────────────────────────────────────────────────────────────
echo "[perturb] BASELINE ${BASELINE}s..."
for level in light medium heavy; do
    STRESS_CPU=2
    [[ "$level" == "medium" ]] && STRESS_CPU=4
    [[ "$level" == "heavy"  ]] && STRESS_CPU=4

    echo ""
    echo "══════════════════════════════════════════"
    echo " LEVEL: $level  (stress-ng --cpu $STRESS_CPU --timeout ${STRESS_DUR}s)"
    echo "══════════════════════════════════════════"

    # baseline recording
    echo "[perturb] baseline..."
    T_END=$(( $(date +%s) + BASELINE ))
    while [ "$(date +%s)" -lt "$T_END" ]; do
        log_phi "$level" "baseline" >> "$LOG"
        sleep 1
    done

    # fire stress on both Pis simultaneously
    echo "[perturb] STRESS START"
    T_STRESS=$(date +%s%3N)
    ssh pi  "stress-ng --cpu $STRESS_CPU --timeout ${STRESS_DUR}s > /dev/null 2>&1" &
    SSH_PI=$!
    ssh pi2 "stress-ng --cpu $STRESS_CPU --timeout ${STRESS_DUR}s > /dev/null 2>&1" &
    SSH_PI2=$!

    T_END=$(( $(date +%s) + STRESS_DUR ))
    while [ "$(date +%s)" -lt "$T_END" ]; do
        log_phi "$level" "stress" >> "$LOG"
        sleep 1
    done
    wait $SSH_PI $SSH_PI2 2>/dev/null || true

    echo "[perturb] STRESS END — recovery..."
    T_END=$(( $(date +%s) + RECOVER ))
    while [ "$(date +%s)" -lt "$T_END" ]; do
        log_phi "$level" "recovery" >> "$LOG"
        sleep 1
    done

    echo "[perturb] $level done"
done

kill $SCHED_PID 2>/dev/null || true
wait $SCHED_PID 2>/dev/null || true

# ── REPORT ───────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " RESULTS"
echo "══════════════════════════════════════════"

python3 - "$LOG" "$SCHED_LOG" <<'PYEOF'
import sys, csv, math

log_file  = sys.argv[1]
sched_file = sys.argv[2]

rows = []
with open(log_file) as f:
    for r in csv.DictReader(f):
        try:
            rows.append({
                'level': r['level'],
                'phi':   float(r['phi']),
                'pd':    float(r['pd']) if r['pd'] else 0.0,
                'event': r['event']
            })
        except ValueError:
            pass

PI = math.pi

for level in ['light', 'medium', 'heavy']:
    lrows = [r for r in rows if r['level'] == level]
    if not lrows:
        continue

    baseline  = [r for r in lrows if r['event'] == 'baseline']
    stress    = [r for r in lrows if r['event'] == 'stress']
    recovery  = [r for r in lrows if r['event'] == 'recovery']

    base_mean  = sum(r['phi'] for r in baseline)  / max(len(baseline), 1)
    stress_max = max((abs(r['phi'] - PI) for r in stress),  default=0)
    base_dev   = max((abs(r['phi'] - PI) for r in baseline), default=0)

    # recovery time: first recovery sample within 0.05 of base_mean
    rec_time = None
    for i, r in enumerate(recovery):
        if abs(r['phi'] - base_mean) < 0.05:
            rec_time = i  # seconds
            break

    # signal dropout: gaps > 12s in sched file
    try:
        with open(sched_file) as f:
            lines = [l.strip() for l in f if 'SUBMIT' in l or 'COLLECT' in l]
        dropouts = len(lines)
    except FileNotFoundError:
        dropouts = 0

    print(f"\n  {level.upper()}")
    print(f"    baseline φ mean : {base_mean:.4f}  (π={PI:.4f})")
    print(f"    max drift under stress: {stress_max:.4f} rad  (base dev: {base_dev:.4f})")
    print(f"    recovery time   : {rec_time}s" if rec_time is not None else "    recovery time   : >90s or not measured")
    print(f"    sched signals   : {dropouts} total")

PYEOF

echo ""
echo "[perturb] full data: $LOG"
echo "[perturb] sched log: $SCHED_LOG"
