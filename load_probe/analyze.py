"""analyze.py — reads probe_results.csv and answers the actual question:
did tracking_err1 or raw avg_load_frac notice the injected spike first,
and by how much? Both signals get their own baseline mean/std from the
pre-spike window (skipping the first few seconds while both levels are
still locking in), then we find the first post-spike-start tick where
each one crosses baseline_mean + 3*baseline_std -- a real z-score
threshold, not eyeballed.
"""
import csv

WARMUP_SKIP_S = 5.0   # ignore the first few seconds -- Layer0/Layer1 still locking in
Z_THRESHOLD = 3.0


def load_rows(path="probe_results.csv"):
    with open(path) as f:
        return [
            {k: float(v) for k, v in row.items()}
            for row in csv.DictReader(f)
        ]


def detection_lag(rows, field, spike_start_s):
    baseline = [r[field] for r in rows
                if WARMUP_SKIP_S <= r["elapsed_s"] < spike_start_s]
    mean = sum(baseline) / len(baseline)
    var = sum((x - mean) ** 2 for x in baseline) / len(baseline)
    std = var ** 0.5

    for r in rows:
        if r["elapsed_s"] < spike_start_s:
            continue
        z = abs(r[field] - mean) / std if std > 1e-12 else 0.0
        if z > Z_THRESHOLD:
            return r["elapsed_s"] - spike_start_s, mean, std, z
    return None, mean, std, None


def main():
    rows = load_rows()
    spike_rows = [r for r in rows if r["spike_active"] > 0.5]
    if not spike_rows:
        print("no spike found in this CSV")
        return
    spike_start_s = min(r["elapsed_s"] for r in spike_rows)
    spike_end_s = max(r["elapsed_s"] for r in spike_rows)
    print(f"spike active from t={spike_start_s:.1f}s to t={spike_end_s:.1f}s "
          f"({spike_end_s - spike_start_s:.1f}s duration)")
    print()

    for field, label in [("avg_load_frac", "raw avg_load_frac"),
                          ("tracking_err1", "tracking_err1 (residual)")]:
        lag, mean, std, z = detection_lag(rows, field, spike_start_s)
        if lag is None:
            print(f"{label:28s}: never crossed {Z_THRESHOLD}-sigma "
                  f"(baseline mean={mean:.5f} std={std:.5f})")
        else:
            print(f"{label:28s}: detected {lag:5.1f}s after spike start "
                  f"(z={z:.1f}, baseline mean={mean:.5f} std={std:.5f})")


if __name__ == "__main__":
    main()
