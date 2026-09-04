"""log_trimmer.py — keeps the three high-churn raw per-tick logs bounded
to roughly the last 10 minutes. Deliberately does NOT touch layer1.log/
layer2.log (the multi-hour S/r1 validation record) or any of the small
event-only logs (regime.log, consensus_explainer.log, explainer.log,
mesh_healer.log, sustained.log) -- those only write on real triggers and
are already small; trimming them would delete actual incident history,
not noise.

Truncates in place (open(path, "w") on the SAME inode) rather than
write-temp-then-rename -- the writers (run_netlat.py,
consensus_test_simple.py, run_local.py) hold their files open in append
mode (O_APPEND), which always writes at current end-of-file. A rename
would leave their file descriptor pointing at the old, now-detached
inode, silently growing forever while the new file sat static;
in-place truncation is safe because O_APPEND re-seeks to the (now
shorter) end-of-file on every write, same principle as `: > file.log`
against a running process.
"""
import os
import time

# log_trimmer.py lives in cascade_pll/maintenance/ after the 2026-08-15
# reorg -- absolute paths from REPO now, rather than relying on launch
# CWD, since the file itself moved one level deeper than its targets.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEEP_LINES = 1200   # ~10 min at these files' observed ~2 lines/sec cadence
INTERVAL_S = 60

TARGETS = [
    os.path.join(REPO, "netlat_layer0/run_netlat.log"),
    os.path.join(REPO, "simple_consensus.log"),
    os.path.join(REPO, "mint_cpu_layer0/run_local.log"),
]


def trim_file(path, keep_lines=KEEP_LINES):
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    if len(lines) <= keep_lines:
        return None
    with open(path, "w") as f:
        f.writelines(lines[-keep_lines:])
    return len(lines) - keep_lines


def main():
    print(f"[log_trimmer] watching {TARGETS}, keep_lines={KEEP_LINES}, "
          f"every {INTERVAL_S}s", flush=True)
    while True:
        for path in TARGETS:
            removed = trim_file(path)
            if removed:
                print(f"[log_trimmer] {path}: trimmed {removed} lines", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
