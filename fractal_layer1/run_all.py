#!/usr/bin/env python3
"""run_all.py — launch the full 3-node fractal test (mint + pi1 + pi2 +
Layer 1 aggregator) via shell_launch instead of SSH, so launches land
close together in real time instead of drifting on SSH handshake
jitter (the exact failure mode that hit the first 3-node test attempt,
2026-08-10).

Pi1/Pi2 launches go through shell_launch_send (near-instant ACCEPTED,
no SSH). Mint's own node and the Layer 1 aggregator run as local
subprocesses directly (no channel needed -- this script already runs on
Mint).

Requires shell_launch_listener.py already running on Pi1 (10.8.0.7:58701)
and Pi2 (10.8.0.5:58701).

Usage:
    python3 run_all.py [duration_s]
"""
import os
import subprocess
import sys
import threading
import time

SHELL_LAUNCH_DIR = os.path.join(os.path.dirname(__file__), "..", "shell_launch")
sys.path.insert(0, SHELL_LAUNCH_DIR)

DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

REMOTE_NODES = [
    # name, host, port, key_path, cmd, cwd
    ("pi1", "10.8.0.7", 58701, os.path.join(SHELL_LAUNCH_DIR, "shell_launch_pi1.key"),
     f"python3 run_pi1.py {DURATION_S:.0f}", "/home/martin/pi1_cpu_layer0"),
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
    from shell_launch_common import load_key, pack_run, unpack_response
    import socket
    import secrets

    key = load_key(key_path)
    nonce = secrets.randbits(64)
    resp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    resp_sock.bind(("0.0.0.0", 0))
    resp_port = resp_sock.getsockname()[1]

    req_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = pack_run(cmd, cwd, nonce, resp_port, key)
    t0 = time.time()
    req_sock.sendto(pkt, (host, port))

    resp_sock.settimeout(5.0)
    while True:
        try:
            data, _ = resp_sock.recvfrom(2048)
        except socket.timeout:
            print(f"[{name}] timeout waiting for response", flush=True)
            return
        from shell_launch_common import ACCEPTED, DONE_OK, DONE_FAIL, REJECTED
        try:
            status, sid, detail, resp_nonce = unpack_response(data, key)
        except ValueError:
            continue
        if resp_nonce != nonce:
            continue
        if status == ACCEPTED:
            print(f"[{name}] ACCEPTED after {time.time()-t0:.3f}s  {detail}", flush=True)
        elif status in (DONE_OK, DONE_FAIL, REJECTED):
            print(f"[{name}] DONE after {time.time()-t0:.3f}s", flush=True)
            return
        resp_sock.settimeout(DURATION_S + 5.0)


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

    print(f"[run_all] launching mint (local) + pi1/pi2 (shell_launch, parallel)", flush=True)
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
