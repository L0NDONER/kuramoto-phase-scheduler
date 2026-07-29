#!/usr/bin/env python3
"""
quartz_admit.py — admission control for quartz_metro_node.

The gap this closes: nothing currently stops an unbounded number of
quartz_metro_node processes from running on one host. Linux's own scheduler
will timeslice them, but a resource-constrained Pi genuinely can't usefully
run an arbitrary number of annealing jobs at once -- something has to
decide who runs now vs who waits. That decision is what "resource
arbitration" actually means here; it's not about jobs sharing one worker
process, since each job type already gets its own port pair specifically so
that never has to happen.

Uses the same registry quartz_ps.py reads (/tmp/quartz_jobs/*.json) as the
one source of truth for "how many jobs are running right now" -- no
separate bookkeeping to go stale or disagree with it.

Usage:
  quartz_admit.py --max N -- <command...>

Blocks (polling, no busy spin) until fewer than N quartz_metro_node
processes are registered and alive on this host, then execs <command...>
in place -- same pid, same stdout/exit code/signal behavior as if
quartz_admit.py wasn't there.

Race note: the naive version of this (check count, then exec) has a window
between the check and the child's own write_registry() call where a second
concurrent quartz_admit could also see a free slot and over-admit. Closed
by writing a placeholder registry entry under *this* process's own pid
before exec'ing -- os.execvp() preserves the pid, so the placeholder and the
job's own first write_registry() call are the same file; the slot is
reserved from the moment the admission decision is made, not from whenever
the child gets around to registering itself.
"""
import json, os, sys, time

REGISTRY_DIR = "/tmp/quartz_jobs"
POLL_S = 0.5


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_count():
    if not os.path.isdir(REGISTRY_DIR):
        return 0
    n = 0
    for fname in os.listdir(REGISTRY_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(REGISTRY_DIR, fname)
        try:
            pid = int(fname[:-5])
        except ValueError:
            continue
        if pid_alive(pid):
            n += 1
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return n


def reserve_placeholder(cmd):
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    pid = os.getpid()
    path = os.path.join(REGISTRY_DIR, f"{pid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({
            "pid": pid, "job": "(admitting)", "role": "pending",
            "slot": -1, "plugin": False,
            "start_time": time.time(), "last_update": time.time(),
            "restart": 0, "cmd": " ".join(cmd),
        }, f)
    os.rename(tmp, path)


def main():
    if "--max" not in sys.argv or "--" not in sys.argv:
        print("usage: quartz_admit.py --max N -- <command...>", file=sys.stderr)
        sys.exit(1)
    max_n = int(sys.argv[sys.argv.index("--max") + 1])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    if not cmd:
        print("no command given after --", file=sys.stderr)
        sys.exit(1)

    waited = False
    while running_count() >= max_n:
        if not waited:
            print(f"[quartz_admit] {running_count()}/{max_n} slots full, queueing: {' '.join(cmd)}", flush=True)
            waited = True
        time.sleep(POLL_S)

    # Reserve the slot under our own pid -- execvp() below keeps this pid,
    # so the job's own write_registry() overwrites this same file with its
    # real job/role/slot the moment it starts.
    reserve_placeholder(cmd)
    if waited:
        print(f"[quartz_admit] slot free, admitting: {' '.join(cmd)}", flush=True)

    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
