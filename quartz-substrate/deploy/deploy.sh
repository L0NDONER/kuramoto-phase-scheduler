#!/bin/bash
# deploy.sh — sync quartz-os daemon sources to their real hosts and compile
# the C binaries locally on each (arch differs: Mint x86_64, Pis aarch64).
#
# Does NOT touch reference/ — those files are pre-existing production
# infrastructure on the Pi (shaper-setup.sh / quic-sni-gate.py /
# wave-pacer.py), captured here for documentation only.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMONS="$ROOT/daemons"
REMOTE_DIR="quartz-os"

echo "== Mint (local) =="
mkdir -p ~/$REMOTE_DIR/jobs
cp "$DAEMONS"/quartz_node.c ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_metro_node.c ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_job.h ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_wan_gain.py ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_ps.py ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_admit.py ~/$REMOTE_DIR/
cp "$DAEMONS"/jobs/sphere.c ~/$REMOTE_DIR/jobs/
gcc -O2 -o ~/$REMOTE_DIR/quartz_node ~/$REMOTE_DIR/quartz_node.c -lpthread -lm
gcc -O2 -o ~/$REMOTE_DIR/quartz_metro_node ~/$REMOTE_DIR/quartz_metro_node.c -lpthread -lm -ldl
gcc -O2 -shared -fPIC -o ~/$REMOTE_DIR/jobs/sphere.so ~/$REMOTE_DIR/jobs/sphere.c -lm

for host in pi pi2; do
    echo "== $host =="
    ssh "$host" "mkdir -p ~/$REMOTE_DIR/jobs"
    scp "$DAEMONS"/quartz_node.c "$host:~/$REMOTE_DIR/"
    scp "$DAEMONS"/quartz_metro_node.c "$host:~/$REMOTE_DIR/"
    scp "$DAEMONS"/quartz_job.h "$host:~/$REMOTE_DIR/"
    scp "$DAEMONS"/quartz_ps.py "$host:~/$REMOTE_DIR/"
    scp "$DAEMONS"/quartz_admit.py "$host:~/$REMOTE_DIR/"
    scp "$DAEMONS"/jobs/sphere.c "$host:~/$REMOTE_DIR/jobs/"
    ssh "$host" "gcc -O2 -o ~/$REMOTE_DIR/quartz_node ~/$REMOTE_DIR/quartz_node.c -lpthread -lm"
    ssh "$host" "gcc -O2 -o ~/$REMOTE_DIR/quartz_metro_node ~/$REMOTE_DIR/quartz_metro_node.c -lpthread -lm -ldl"
    ssh "$host" "gcc -O2 -shared -fPIC -o ~/$REMOTE_DIR/jobs/sphere.so ~/$REMOTE_DIR/jobs/sphere.c -lm"
done

echo "== pi (Firestick gain daemon) =="
scp "$DAEMONS"/quartz_firestick_gain.py "pi:~/$REMOTE_DIR/"

echo "done"
