#!/bin/bash
# stop.sh — kill everything start.sh started, on all three hosts.
#
# Processes are launched as `cd ~/quartz-os && ./quartz_node ...`, so their
# cmdline is just `./quartz_node ...` -- no path substring for `pkill -f` to
# match. The production copies of these same binaries run from ~/claude/ on
# all three hosts with the identical bare argv, so name/cmdline matching
# can't tell them apart. Match on cwd instead.
# sudo prefix on readlink/kill because the gain daemons run as root
# (started via `sudo -b`) -- a plain non-root readlink on their /proc/PID/cwd
# is permission-denied, and a plain kill on their pid is a no-op.
kill_by_cwd() {
    local pattern="$1"
    for pid in $(pgrep -f "$pattern" 2>/dev/null); do
        cwd=$(sudo readlink -f "/proc/$pid/cwd" 2>/dev/null)
        if [[ "$cwd" == */quartz-os ]]; then
            sudo kill "$pid" 2>/dev/null
        fi
    done
}

REMOTE_KILL='
kill_by_cwd() {
    local pattern="$1"
    for pid in $(pgrep -f "$pattern" 2>/dev/null); do
        cwd=$(sudo readlink -f "/proc/$pid/cwd" 2>/dev/null)
        if [[ "$cwd" == */quartz-os ]]; then
            sudo kill "$pid" 2>/dev/null
        fi
    done
}
kill_by_cwd "quartz_node"
kill_by_cwd "quartz_metro_node"
kill_by_cwd "quartz_wan_gain.py"
kill_by_cwd "quartz_firestick_gain.py"
'

kill_by_cwd "quartz_node"
kill_by_cwd "quartz_metro_node"
kill_by_cwd "quartz_wan_gain.py"

for host in pi pi2; do
    ssh "$host" "bash -c '$REMOTE_KILL'"
done
echo "stopped"
