"""quartz_gain_common.py — shared substrate-read + carrier/gain polling
loop for quartz_wan_gain.py and quartz_firestick_gain.py. Both drive a tc
rate off the same quartz_node peer-health carrier signal and were
independently implementing the identical read/compute/warn-logging loop;
this is that loop pulled out so a fix to the substrate-down semantics
only has to be made once.
"""
import json
import math
import os
import time


def read_substrate(health_path, timeout_s):
    """Returns (theta, healthy) or (None, False) if substrate is down."""
    try:
        mtime = os.path.getmtime(health_path)
        if time.time() - mtime > timeout_s:
            return None, False
        theta = None
        any_healthy = False
        with open(health_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "self_phase" in d:
                    theta = d["self_phase"]
                elif d.get("healthy"):
                    any_healthy = True
        if theta is None or not any_healthy:
            return None, False
        return theta, True
    except (FileNotFoundError, OSError):
        return None, False


def compute_gain(theta, g_base):
    """G = g_base + (1 - g_base) * carrier(theta), carrier = (1-cos)/2."""
    carrier = (1.0 - math.cos(theta)) / 2.0
    g = g_base + (1.0 - g_base) * carrier
    return max(0.0, min(1.0, g)), carrier


def run_gain_loop(g_base, n_steps, on_locked, on_floor, label,
                   health_path, timeout_s, poll_s, warn_s):
    """Polls read_substrate() forever. Calls on_floor() once when the
    substrate goes unhealthy (forcing the floor immediately rather than
    freezing at the last rate -- absence of signal should degrade toward
    boring, not park wherever G last left it), and on_locked(theta,
    carrier, g) whenever locked and the quantized gain step changes.
    Callers own the rate formula and any per-change logging; this only
    owns the polling/step-dedup/warn-cadence bookkeeping shared by both
    gain daemons.
    """
    floor_step = round(g_base * n_steps)
    last_step = -1
    last_locked_t = None
    last_warn_t = 0.0

    while True:
        theta, healthy = read_substrate(health_path, timeout_s)

        if not healthy:
            now = time.time()
            if last_step != floor_step:
                on_floor()
                last_step = floor_step
                print(f"[{label}] substrate down -> forcing floor G={g_base}", flush=True)

            since = None if last_locked_t is None else now - last_locked_t
            if since is None or since > warn_s:
                if now - last_warn_t > warn_s:
                    since_str = "never" if last_locked_t is None else f"{since:.0f}s ago"
                    print(f"[{label}] SUBSTRATE DOWN -- no healthy peer "
                          f"(last locked {since_str}), holding at floor", flush=True)
                    last_warn_t = now
            time.sleep(poll_s)
            continue

        last_locked_t = time.time()
        g, carrier = compute_gain(theta, g_base)
        step = round(g * n_steps)
        if step != last_step:
            on_locked(theta, carrier, g)
            last_step = step

        time.sleep(poll_s)
