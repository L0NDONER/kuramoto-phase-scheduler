#!/bin/bash
# set_cpu_cap.sh — cap Pi2's CPU frequency ceiling by a suppression
# percentage, applied to every core. Writes scaling_max_freq, NOT the
# governor -- ondemand keeps scaling freely underneath the new ceiling,
# this only lowers the top of its range.
#
# Snaps to the nearest real hardware P-state at or below the nominal
# target and reports the TRUE achieved suppression percentage, rather
# than letting the kernel silently round and leaving you to assume the
# nominal number landed exactly. Confirmed live on Pi2 2026-08-10:
# requesting 25% (nominal 1125MHz) actually applied 1100MHz (26.7%
# true suppression) -- the CPU only supports 100MHz-step P-states
# (600/700/.../1500MHz on Pi2), nothing in between.
#
# Usage:
#   sudo ./set_cpu_cap.sh 25       # suppress max freq by ~25% (nearest real step)
#   sudo ./set_cpu_cap.sh reset    # restore to full ceiling
#
# Needs root (scaling_max_freq is root-owned, 0644).

set -euo pipefail

N_CORES=4
FREQ_LIST_PATH="/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies"

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <suppress_pct 0-100 | reset>" >&2
    exit 1
fi

# Read real hardware P-states (kHz), not a hardcoded range -- portable
# across whatever CPU this actually runs on.
read -r -a AVAIL_KHZ <<< "$(tr ' ' '\n' < "$FREQ_LIST_PATH" | sort -n | tr '\n' ' ')"
base_khz=${AVAIL_KHZ[-1]}
min_khz=${AVAIL_KHZ[0]}

if [ "$1" = "reset" ]; then
    new_khz=$base_khz
else
    pct=$1
    nominal_khz=$(awk -v base="$base_khz" -v pct="$pct" 'BEGIN { printf "%d", base * (1 - pct/100.0) }')
    if [ "$nominal_khz" -lt "$min_khz" ]; then
        echo "requested cap is below the hardware min (${min_khz}kHz) -- refusing" >&2
        exit 1
    fi
    # Nearest available step AT OR BELOW the nominal target -- matches
    # the kernel's own rounding direction, so what we report is what
    # actually gets applied, not a guess.
    new_khz=$min_khz
    for f in "${AVAIL_KHZ[@]}"; do
        if [ "$f" -le "$nominal_khz" ]; then
            new_khz=$f
        fi
    done
fi

for i in $(seq 0 $((N_CORES - 1))); do
    path="/sys/devices/system/cpu/cpu${i}/cpufreq/scaling_max_freq"
    echo "$new_khz" > "$path"
done

true_pct=$(awk -v base="$base_khz" -v cur="$new_khz" 'BEGIN { printf "%.1f", 100.0 * (base - cur) / base }')

echo "set scaling_max_freq to $((new_khz / 1000))MHz on all ${N_CORES} cores (true suppression: ${true_pct}%)"
echo "current values:"
for i in $(seq 0 $((N_CORES - 1))); do
    echo "  core${i}: $(cat /sys/devices/system/cpu/cpu${i}/cpufreq/scaling_max_freq) kHz"
done
