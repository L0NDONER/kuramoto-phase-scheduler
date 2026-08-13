"""run_all.py — runs all 6 conditions in counterbalanced order (not
grouped by depth or by gate) so no single factor's conditions cluster at
one point in real time, reducing the chance that ordinary machine-load
drift over the ~8 minute total runtime confounds one factor consistently.
"""
from run_ablation import run

ORDER = [
    (1, "trust"), (3, "fixed"), (2, "trust"),
    (1, "fixed"), (3, "trust"), (2, "fixed"),
]

for depth, gate in ORDER:
    run(depth, gate, f"result_d{depth}_{gate}.csv")

print("\n[run_all] all 6 conditions complete")
