#!/usr/bin/env python3
"""run_all.py — launch the fractal test (mint + pi2 + Layer 1 aggregator)
via shell_launch instead of SSH, so launches land close together in real
time instead of drifting on SSH handshake jitter (the exact failure mode
that hit the first 3-node test attempt, 2026-08-10).

Pi2's launch goes through shell_launch_send (near-instant ACCEPTED, no
SSH). Mint's own node and the Layer 1 aggregator run as local
subprocesses directly (no channel needed -- this script already runs on
Mint).

Pi1 dropped from this table 2026-08-31: pi1_cpu_layer0/run_pi1.py was
never actually deployed on the real Pi1 (confirmed live -- no such
directory exists there). The "pi1" identity on the real 239.0.0.6 wire
belongs to pi1_thermal_layer0/run_pi1_thermal.py instead, which already
runs persistently and isn't something this bounded-test-run script
should be launching/killing.

Requires shell_launch_listener.py already running on Pi2 (10.8.0.5:58701).

Usage:
    python3 run_all.py [duration_s]
"""
import os
import subprocess
import sys
import threading
import time

SHELL_LAUNCH_DIR = os.path.join(os.path.dirname(__file__), "..", "cascade_pll", "channels", "shell_launch")
sys.path.insert(0, SHELL_LAUNCH_DIR)

DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

REMOTE_NODES = [
    # name, host, port, key_path, cmd, cwd
    ("pi2", "10.8.0.5", 58701, os.path.join(SHELL_LAUNCH_DIR, "shell_launch_pi2.key"),
     f"python3 run_pi2.py {DURATION_S:.0f}", "/home/martin/pi2_cpu_layer0"),
]


def launch_remote(name, host, port, key_path, cmd, cwd):
    """Fires a RUN and prints each streamed line prefixed with the node
    name, in its own thread so all remote launches fire in parallel.

    Takes an explicit key PATH (not an env var) -- load_key() reads
    whatever path it's given directly, no shared module-level state
    involved, so two threads with different keys can't race each other
    (see shell_launch_common.load_key()'s docstring for the bug this
    replaced)."""
    from shell_launch_common import load_key, send_run, ACCEPTED, DONE_OK, DONE_FAIL, REJECTED

    key = load_key(key_path)

    def on_status(status, sid, detail, elapsed_s):
        if status == ACCEPTED:
            print(f"[{name}] ACCEPTED after {elapsed_s:.3f}s  {detail}", flush=True)
        elif status in (DONE_OK, DONE_FAIL, REJECTED):
            print(f"[{name}] DONE after {elapsed_s:.3f}s", flush=True)

    session_id = send_run(host, port, cmd, cwd, key, on_status,
                           initial_timeout_s=5.0, running_timeout_s=DURATION_S + 5.0)
    if session_id is None:
        print(f"[{name}] timeout waiting for response", flush=True)


def main():
    fractal_dir = os.path.dirname(__file__)
    scratch = os.path.join(fractal_dir, "logs")
    os.makedirs(scratch, exist_ok=True)
    mint_dir = os.path.join(fractal_dir, "..", "mint_cpu_layer0")

    print(f"[run_all] launching aggregator (local)", flush=True)
    agg_log = open(f"{scratch}/run_all_layer1.log", "w")
    agg_proc = subprocess.Popen(["python3", "layer1_aggregator.py", str(int(DURATION_S) + 5)],
                                  cwd=fractal_dir, stdout=agg_log, stderr=subprocess.STDOUT)

    threads = [threading.Thread(target=launch_remote, args=args, daemon=True)
               for args in REMOTE_NODES]

    print(f"[run_all] launching mint (local) + pi2 (shell_launch)", flush=True)
    mint_log = open(f"{scratch}/run_all_mint.log", "w")
    mint_proc = subprocess.Popen(["python3", "run_local.py", str(DURATION_S)],
                                   cwd=mint_dir, stdout=mint_log, stderr=subprocess.STDOUT)
    for t in threads:
        t.start()

    print(f"[run_all] all launched, aggregator log: {scratch}/run_all_layer1.log", flush=True)
    print(f"[run_all] waiting {DURATION_S:.0f}s for the run to complete...", flush=True)
    time.sleep(DURATION_S + 6.0)
    for t in threads:
        t.join(timeout=2.0)
    mint_proc.wait(timeout=10.0)
    print(f"[run_all] done", flush=True)


if __name__ == "__main__":
    main()
