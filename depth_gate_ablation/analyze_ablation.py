"""analyze_ablation.py — reads all 9 condition CSVs (3 depths x 3 gates:
trust, fixed, random) and computes the same mean-shift z-score used
throughout this session's load_probe analysis, for each condition's own
output_val column. Prints the 3x3 table and the contrasts needed to
answer the actual question: does information transfer come from mere
gain variability (random should then score near trust), or specifically
from informed/coherence-based gating (random should then score near
fixed's ~0, not near trust).
"""
import csv
import glob

GATES = ("trust", "fixed", "random")


def load_rows(path):
    with open(path) as f:
        return [dict(elapsed_s=float(r["elapsed_s"]),
                     output_val=float(r["output_val"]),
                     spike_active=float(r["spike_active"]))
                for r in csv.DictReader(f)]


def mean_shift_z(rows):
    base = [abs(r["output_val"]) for r in rows if 5.0 <= r["elapsed_s"] < 30.0]
    spike = [abs(r["output_val"]) for r in rows if r["spike_active"] > 0.5]
    bmean = sum(base) / len(base)
    bstd = (sum((x - bmean) ** 2 for x in base) / len(base)) ** 0.5
    smean = sum(spike) / len(spike)
    return abs(smean - bmean) / bstd if bstd > 1e-9 else 0.0


def main():
    results = {}
    for depth in (1, 2, 3):
        for gate in GATES:
            path = f"result_d{depth}_{gate}.csv"
            if not glob.glob(path):
                print(f"missing: {path}")
                continue
            results[(depth, gate)] = mean_shift_z(load_rows(path))

    header = f"{'depth':>6}" + "".join(f"{g:>10}" for g in GATES)
    print(header)
    for depth in (1, 2, 3):
        row = f"{depth:>6}"
        for gate in GATES:
            z = results.get((depth, gate))
            row += f"{z:>10.3f}" if z is not None else f"{'n/a':>10}"
        print(row)

    print()
    print("interpretation, per depth (2 and 3 are the ones that matter --")
    print("depth 1 has no gate mechanism at all, included as a sanity check):")
    for depth in (2, 3):
        zt = results.get((depth, "trust"))
        zf = results.get((depth, "fixed"))
        zr = results.get((depth, "random"))
        if None in (zt, zf, zr):
            continue
        if abs(zr - zf) < abs(zr - zt):
            verdict = ("RANDOM tracks FIXED (~0) -- confirms the informed trust "
                       "gate specifically matters, not mere gain variability")
        elif abs(zr - zt) < abs(zr - zf):
            verdict = ("RANDOM tracks TRUST -- mere gain variability explains the "
                       "result, 'trust-gated' oversells the mechanism")
        else:
            verdict = "RANDOM sits ambiguously between the two -- inconclusive"
        print(f"  depth={depth}: trust={zt:.3f} fixed={zf:.3f} random={zr:.3f}  ->  {verdict}")


if __name__ == "__main__":
    main()
