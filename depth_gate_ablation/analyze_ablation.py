"""analyze_ablation.py — reads all 6 condition CSVs and computes the
same mean-shift z-score used throughout this session's load_probe
analysis, for each condition's own output_val column. Prints the 2x3
table and the depth/gate contrasts needed to answer the actual question:
does z drop mainly with depth, mainly with gate, or neither cleanly.
"""
import csv
import glob


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
        for gate in ("trust", "fixed"):
            path = f"result_d{depth}_{gate}.csv"
            files = glob.glob(path)
            if not files:
                print(f"missing: {path}")
                continue
            z = mean_shift_z(load_rows(path))
            results[(depth, gate)] = z

    print(f"{'depth':>6} {'trust':>10} {'fixed':>10} {'trust/fixed ratio':>18}")
    for depth in (1, 2, 3):
        zt = results.get((depth, "trust"))
        zf = results.get((depth, "fixed"))
        ratio = f"{zt/zf:.3f}" if zt is not None and zf and zf > 1e-9 else "n/a"
        print(f"{depth:>6} {zt if zt is not None else float('nan'):>10.3f} "
              f"{zf if zf is not None else float('nan'):>10.3f} {ratio:>18}")

    print()
    print("depth effect (holding gate fixed, depth1 -> depth3):")
    for gate in ("trust", "fixed"):
        z1, z3 = results.get((1, gate)), results.get((3, gate))
        if z1 is not None and z3 is not None:
            print(f"  gate={gate}: z drops {z1:.3f} -> {z3:.3f}  "
                  f"(ratio {z3/z1:.3f})" if z1 > 1e-9 else f"  gate={gate}: n/a")

    print()
    print("gate effect (holding depth fixed, trust vs fixed):")
    for depth in (2, 3):
        zt, zf = results.get((depth, "trust")), results.get((depth, "fixed"))
        if zt is not None and zf is not None:
            print(f"  depth={depth}: trust={zt:.3f} vs fixed={zf:.3f}  "
                  f"(trust is {'weaker' if zt < zf else 'stronger'} detector)")


if __name__ == "__main__":
    main()
