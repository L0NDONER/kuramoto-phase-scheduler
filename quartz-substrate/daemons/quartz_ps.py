#!/usr/bin/env python3
"""
quartz_ps.py — reads /tmp/quartz_jobs/*.json (written by quartz_metro_node's
write_registry(), once at startup and once per round/restart thereafter) and
prints a live table of what's actually running on this host.

No cleanup-on-exit in the writer -- a registry file whose pid isn't alive
anymore is stale, not meaningful, so this script prunes it on sight rather
than trusting it. Same fail-toward-simple approach as quartz_metro_node's
own peer_healthy().

Usage:
  quartz_ps.py                    local host only
  quartz_ps.py --cluster          also ssh to pi/pi2 and run this remotely,
                                   assumes it's deployed at ~/claude/quartz_ps.py there
"""
import json, os, socket, subprocess, sys, time

REGISTRY_DIR = "/tmp/quartz_jobs"
HOSTS = ["pi", "pi2"]   # ssh aliases; this host (mint) is always included


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just owned by someone else -- still alive
    return True


def read_local():
    rows = []
    if not os.path.isdir(REGISTRY_DIR):
        return rows
    now = time.time()
    for fname in os.listdir(REGISTRY_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(REGISTRY_DIR, fname)
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        pid = d.get("pid")
        if pid is None or not pid_alive(pid):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        d["_age_s"] = now - d.get("last_update", now)
        rows.append(d)
    return rows


def print_table(host, rows):
    if not rows:
        print(f"{host}: no jobs registered")
        return
    print(f"{host}:")
    print(f"  {'PID':<8}{'JOB':<14}{'ROLE':<8}{'SLOT':<6}{'PLUGIN':<8}{'RESTART':<9}{'LAST_UPDATE':<12}")
    for d in sorted(rows, key=lambda r: (r.get("role", ""), r.get("slot", -1))):
        slot = d.get("slot", -1)
        print(f"  {d.get('pid', '?'):<8}{d.get('job', '?'):<14}{d.get('role', '?'):<8}"
              f"{slot if slot >= 0 else '-':<6}{str(d.get('plugin', False)):<8}"
              f"{d.get('restart', '?'):<9}{d['_age_s']:.1f}s ago")


def main():
    cluster = "--cluster" in sys.argv
    print_table(socket.gethostname(), read_local())
    if cluster:
        for host in HOSTS:
            try:
                out = subprocess.run(
                    ["ssh", host, "python3", "~/claude/quartz_ps.py"],
                    capture_output=True, text=True, timeout=10,
                )
                print(out.stdout.rstrip() or f"{host}: no output")
                if out.returncode != 0 and out.stderr.strip():
                    print(f"  ({host} stderr: {out.stderr.strip().splitlines()[-1]})")
            except Exception as e:
                print(f"{host}: unreachable ({e})")


if __name__ == "__main__":
    main()
