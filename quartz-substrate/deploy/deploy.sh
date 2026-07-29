#!/bin/bash
# deploy.sh — sync quartz-substrate daemon sources to their real hosts and
# compile the C binaries locally on each (arch differs: Mint x86_64, Pis
# aarch64). Targets ~/claude on all three hosts -- the same location
# start.sh/stop.sh use and where the daemons actually run in production.
# Unified 2026-07-29 (previously targeted ~/quartz-os, a separate staging
# copy that needed a manual `cp` into ~/claude before start.sh would ever
# see the new binary).
#
# Builds land via compile-to-temp-name + atomic `mv`, not a direct
# `gcc -o <live path>`, because the live path is very likely the currently
# running binary: overwriting a running executable's inode in place hits
# ETXTBSY (confirmed live this session with a plain `cp`). `mv` on the same
# filesystem is a rename, not a write into the running inode -- the
# in-flight process keeps running fine off the now-unlinked old inode, and
# the next start.sh/restart picks up the new file.
#
# Does NOT touch reference/ — those files are pre-existing production
# infrastructure on the Pi (shaper-setup.sh / quic-sni-gate.py /
# wave-pacer.py), captured here for documentation only.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMONS="$ROOT/daemons"
REMOTE_DIR="claude"

echo "== Mint (local) =="
mkdir -p ~/$REMOTE_DIR/jobs
cp "$DAEMONS"/quartz_node.c ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_metro_node.c ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_job.h ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_wan_gain.py ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_ps.py ~/$REMOTE_DIR/
cp "$DAEMONS"/quartz_admit.py ~/$REMOTE_DIR/
cp "$DAEMONS"/jobs/sphere.c ~/$REMOTE_DIR/jobs/
gcc -O2 -o ~/$REMOTE_DIR/quartz_node.new ~/$REMOTE_DIR/quartz_node.c -lpthread -lm
mv ~/$REMOTE_DIR/quartz_node.new ~/$REMOTE_DIR/quartz_node
gcc -O2 -o ~/$REMOTE_DIR/quartz_metro_node.new ~/$REMOTE_DIR/quartz_metro_node.c -lpthread -lm -ldl
mv ~/$REMOTE_DIR/quartz_metro_node.new ~/$REMOTE_DIR/quartz_metro_node
gcc -O2 -shared -fPIC -o ~/$REMOTE_DIR/jobs/sphere.so.new ~/$REMOTE_DIR/jobs/sphere.c -lm
mv ~/$REMOTE_DIR/jobs/sphere.so.new ~/$REMOTE_DIR/jobs/sphere.so

for host in pi pi2; do
    echo "== $host =="
    timeout 10 ssh "$host" "mkdir -p ~/$REMOTE_DIR/jobs"
    timeout 10 scp "$DAEMONS"/quartz_node.c "$host:~/$REMOTE_DIR/"
    timeout 10 scp "$DAEMONS"/quartz_metro_node.c "$host:~/$REMOTE_DIR/"
    timeout 10 scp "$DAEMONS"/quartz_job.h "$host:~/$REMOTE_DIR/"
    timeout 10 scp "$DAEMONS"/quartz_ps.py "$host:~/$REMOTE_DIR/"
    timeout 10 scp "$DAEMONS"/quartz_admit.py "$host:~/$REMOTE_DIR/"
    timeout 10 scp "$DAEMONS"/jobs/sphere.c "$host:~/$REMOTE_DIR/jobs/"
    timeout 30 ssh "$host" "gcc -O2 -o ~/$REMOTE_DIR/quartz_node.new ~/$REMOTE_DIR/quartz_node.c -lpthread -lm && mv ~/$REMOTE_DIR/quartz_node.new ~/$REMOTE_DIR/quartz_node"
    timeout 30 ssh "$host" "gcc -O2 -o ~/$REMOTE_DIR/quartz_metro_node.new ~/$REMOTE_DIR/quartz_metro_node.c -lpthread -lm -ldl && mv ~/$REMOTE_DIR/quartz_metro_node.new ~/$REMOTE_DIR/quartz_metro_node"
    timeout 30 ssh "$host" "gcc -O2 -shared -fPIC -o ~/$REMOTE_DIR/jobs/sphere.so.new ~/$REMOTE_DIR/jobs/sphere.c -lm && mv ~/$REMOTE_DIR/jobs/sphere.so.new ~/$REMOTE_DIR/jobs/sphere.so"
done

echo "== pi (Firestick gain daemon) =="
timeout 10 scp "$DAEMONS"/quartz_firestick_gain.py "pi:~/$REMOTE_DIR/"

echo "done"
