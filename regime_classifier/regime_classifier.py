"""regime_classifier.py — separate consumer of the substrate's real
broadcast, same shape as gpu_layer0/reflex_power_cap.py: listens to
Layer 1's (r1, psi1, dt_lag1) reports on the real wire
(layer0_report.py, 239.0.0.6:7460), makes its own decision, never feeds
anything back into the substrate itself.

Classifies CALM vs TURBULENT from a 30s EMA of r1 -- Layer 1 is the
level this session actually validated for slow-regime characterization
(rock-solid on multi-minute drift, proven blind to sub-minute events via
the load_probe CPU-spike test). A single instantaneous r1 reading isn't
trustworthy on its own (see the t=62.5s false spike in tracking_err1
during that same test) -- the 30s EMA filters that out while staying far
more responsive than goal_field.py's 120s-time-constant S, which lags
too much to flag a live regime shift promptly.

Hysteresis, not a single threshold: enters CALM only above
CALM_ENTER_R, exits CALM (back to TURBULENT) only below CALM_EXIT_R.
Without the gap between those two thresholds, a reading sitting near one
fixed cutoff flaps on ordinary noise -- the same multi-timescale-
hysteresis pattern already load-bearing elsewhere in this codebase.

Pure classification + logging. No actuation of any kind -- if a real
action needs to be taken on a regime change, that's a separate,
explicit, later decision by a different consumer, not this one.
"""
import math
import sys
import time

from layer0_report import mcast_in, parse_report

EMA_TIME_CONSTANT_S = 30.0
POLL_INTERVAL_S = 0.5
EMA_ALPHA = 2.0 / (EMA_TIME_CONSTANT_S / POLL_INTERVAL_S + 1)

CALM_ENTER_R = 0.92   # r_ema must rise above this to (re-)enter CALM
CALM_EXIT_R = 0.80    # r_ema must fall below this to leave CALM for TURBULENT

STALE_AFTER_S = 5.0    # no fresh layer1 report in this long -> classification UNKNOWN

CALM, TURBULENT, UNKNOWN = "CALM", "TURBULENT", "UNKNOWN"


def classify(state, r_ema):
    """Hysteresis state machine: only two real thresholds, the gap
    between them is what prevents flapping. UNKNOWN in, UNKNOWN out --
    a stale/no-data state should never silently promote to CALM or
    TURBULENT just because the EMA still holds an old value."""
    if state == UNKNOWN:
        return CALM if r_ema >= CALM_ENTER_R else TURBULENT if r_ema < CALM_EXIT_R else UNKNOWN
    if state == CALM:
        return TURBULENT if r_ema < CALM_EXIT_R else CALM
    if state == TURBULENT:
        return CALM if r_ema >= CALM_ENTER_R else TURBULENT
    return UNKNOWN


def main():
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else None
    sock = mcast_in()

    r_ema = None
    last_seen = 0.0
    state = UNKNOWN
    start = time.monotonic()

    print(f"[regime] listening for layer1 broadcasts  "
          f"EMA_tau={EMA_TIME_CONSTANT_S:.0f}s  enter>={CALM_ENTER_R} exit<{CALM_EXIT_R}",
          flush=True)

    while duration_s is None or time.monotonic() - start < duration_s:
        deadline = time.monotonic() + POLL_INTERVAL_S
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(64)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            parsed = parse_report(data)
            if parsed is None:
                continue
            name, r, _psi, _dt_lag_s = parsed
            if name != "layer1":
                continue
            r_ema = r if r_ema is None else r_ema + EMA_ALPHA * (r - r_ema)
            last_seen = time.monotonic()

        now = time.monotonic()
        fresh = (now - last_seen) < STALE_AFTER_S if last_seen else False
        effective_r_ema = r_ema if fresh else None

        new_state = classify(state, effective_r_ema) if effective_r_ema is not None else UNKNOWN
        if new_state != state:
            elapsed = now - start
            r_str = f"{effective_r_ema:.4f}" if effective_r_ema is not None else "n/a"
            print(f"[regime] t={elapsed:7.1f}s  {state} -> {new_state}  r_ema={r_str}",
                  flush=True)
            state = new_state

    print(f"[regime] done, final state={state}", flush=True)


if __name__ == "__main__":
    main()
