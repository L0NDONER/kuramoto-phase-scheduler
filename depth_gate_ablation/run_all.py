"""run_all.py — runs all 9 conditions (3 depths x 3 gates: trust, fixed,
random) in counterbalanced order so no single factor's conditions
cluster at one point in real time, reducing the chance that ordinary
machine-load drift over the ~13 minute total runtime confounds one
factor consistently.
"""
from run_ablation import run

ORDER = [
    (1, "trust"), (3, "fixed"), (2, "random"),
    (1, "fixed"), (3, "trust"), (2, "fixed"),
    (1, "random"), (3, "random"), (2, "trust"),
]

for depth, gate in ORDER:
    run(depth, gate, f"result_d{depth}_{gate}.csv")

print("\n[run_all] all 9 conditions complete")
