#!/usr/bin/env python3
"""
pulse_hash_recv.py — reconstruct a pulse-coded hash from arrival timing alone,
then verify the pulse train was actually paced by a real oscillator.

Listens for START (n_bits, tick_us), then PULSE packets (bit_index only —
the packet carries no bit *value*, just "a 1 happened here"). Every index
that never arrives before END is a 0. Rebuilds the 256-bit hash purely
from which ticks carried a pulse.

Reconstructing the hash only proves the envelope round-tripped. It doesn't
prove a real crystal paced it — a script could fire UDP packets at the
claimed tick offsets with a plain sleep loop and the hash would still
reconstruct fine. The jitter check below is what catches that: real
network+scheduler-paced ticks have irreducible noise (Allan-deviation-style
short-term jitter); a synthetic/simulated sender tends to be either too
metronomic (near-zero jitter) or, if faked carelessly, wildly inconsistent
with the tick grid it claims to be on.

verify_jitter() alone is a single scalar (stdev of adjacent-tick residual
diffs) thresholded against a band — measured empirically against
adversary_sender.py and nazare_sender.py (see /home/martin/claude, both
kept as permanent regression fixtures) to catch a *flat* forger (constant
or absent jitter) but to be blind to *shape*: a train that's silent for
95% of ticks and then has one real 100ms delay spike lands on the same
stdev as a train with uniform noise spread across every tick, and reads
identically PLAUSIBLE. verify_shape() adds two shape statistics computed
from the same residual-diff series that a flat forger's stdev-matching
doesn't fix for free: burst_ratio (stdev inflated by a few outliers vs a
robust median-based scale — ~1 for real/flat noise, seen 180-785 for
distinct burst spikes) and excess kurtosis (near-0 for Gaussian, large for
heavy-tailed). Thresholds below were calibrated against real WAN quartz
runs (burst_ratio <= 6.1, kurtosis <= 23.6) vs three deliberately bursty
nazare_sender.py runs (burst_ratio 184-785, kurtosis 25-67) — see
nazare_vs_flat.png. NOTE: a burst landing on a tick that never carries a
pulse (bit=0) is invisible to this check entirely — arrival timing only
exists for ticks that actually sent a packet, so roughly half of any
train's ticks are unobservable. This is a hard limit of a jitter/shape
check built from pulse-arrival timestamps alone, not a tuning problem.
"""
import socket, statistics, struct, sys, time

PS_PORT = 7470
PS_MAGIC = 0x5053
PS_START, PS_PULSE, PS_END = 1, 2, 3
PS_PULSE_FMT = ">HBH"

# plausibility band for per-tick jitter, as a fraction of tick_s.
# below MIN: timing is suspiciously metronomic for real UDP + scheduler noise.
# above MAX: timing is too noisy to trust which tick a pulse belongs to.
MIN_JITTER_FRAC = 0.0005   # e.g. 55ms tick → ~28us floor
MAX_JITTER_FRAC = 0.30     # e.g. 55ms tick → ~16ms ceiling

# shape thresholds — see docstring above for calibration data.
BURST_RATIO_MAX = 15.0
KURTOSIS_MAX = 30.0


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    return s


def bits_to_hex(bits):
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i+8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out).hex()


def verify_jitter(arrivals, tick_s):
    """
    arrivals: list of (idx, t_arrival) for received pulses.
    Residual_i = t_arrival_i - idx_i*tick_s should be constant (= start
    offset + one-way network latency) up to real oscillator/network noise.
    Allan-style: use *adjacent* (idx-gap==1) pairs so a small tick-rate
    miscalibration (linear drift in the residual) doesn't get counted as
    noise — same overlapping-difference idea as adev.py, just applied to
    this one pulse train instead of a long AxisPulse series.
    """
    if len(arrivals) < 3:
        return None

    arrivals = sorted(arrivals)
    residuals = [(idx, t - idx * tick_s) for idx, t in arrivals]

    adjacent_diffs = []
    slot_mismatches = 0
    for (idx_a, r_a), (idx_b, r_b) in zip(residuals, residuals[1:]):
        if idx_b - idx_a == 1:
            adjacent_diffs.append(r_b - r_a)

    if len(adjacent_diffs) >= 2:
        jitter_s = statistics.stdev(adjacent_diffs)
        basis = f"{len(adjacent_diffs)} adjacent-tick pairs"
    else:
        # fallback: not enough back-to-back pulses this run, use raw residual spread
        rs = [r for _, r in residuals]
        jitter_s = statistics.stdev(rs)
        basis = f"{len(rs)} residuals (no adjacent pairs available)"

    min_ok = tick_s * MIN_JITTER_FRAC
    max_ok = tick_s * MAX_JITTER_FRAC

    if jitter_s < min_ok:
        verdict = f"TOO CLEAN — jitter {jitter_s*1e6:.1f}us < floor {min_ok*1e6:.1f}us; " \
                  f"suspiciously metronomic for real network+scheduler noise"
        physical = False
    elif jitter_s > max_ok:
        verdict = f"TOO NOISY — jitter {jitter_s*1e3:.3f}ms > ceiling {max_ok*1e3:.3f}ms; " \
                  f"not reliably paced by a single stable tick source"
        physical = False
    else:
        verdict = f"PLAUSIBLE — jitter {jitter_s*1e6:.1f}us within [{min_ok*1e6:.1f}us, {max_ok*1e3:.3f}ms]"
        physical = True

    return dict(jitter_s=jitter_s, basis=basis, verdict=verdict, physical=physical)


def _adjacent_diffs(arrivals, tick_s):
    arrivals = sorted(arrivals)
    residuals = [(idx, t - idx * tick_s) for idx, t in arrivals]
    return [r_b - r_a for (idx_a, r_a), (idx_b, r_b) in zip(residuals, residuals[1:])
            if idx_b - idx_a == 1]


def verify_shape(arrivals, tick_s):
    """
    Second-opinion check on the same adjacent-tick residual-diff series
    verify_jitter() reduces to a single stdev — this looks at *shape*
    instead of magnitude, so a train that matches the stdev band by
    mixing many quiet ticks with a couple of huge spikes (indistinguishable
    from uniform noise to verify_jitter) still gets caught. See module
    docstring for the calibration data behind BURST_RATIO_MAX/KURTOSIS_MAX.
    """
    diffs = _adjacent_diffs(arrivals, tick_s)
    if len(diffs) < 5:
        return None

    sd = statistics.stdev(diffs)
    med = statistics.median(diffs)
    mad = statistics.median(abs(d - med) for d in diffs)
    mad_scale = 1.4826 * mad if mad > 0 else 0.0
    burst_ratio = (sd / mad_scale) if mad_scale > 0 else float("inf")

    n = len(diffs)
    mean = statistics.mean(diffs)
    m2 = sum((d - mean) ** 2 for d in diffs) / n
    m4 = sum((d - mean) ** 4 for d in diffs) / n
    kurtosis = (m4 / (m2 ** 2) - 3.0) if m2 > 0 else 0.0

    reasons = []
    if burst_ratio > BURST_RATIO_MAX:
        reasons.append(f"burst_ratio {burst_ratio:.1f} > {BURST_RATIO_MAX}")
    if kurtosis > KURTOSIS_MAX:
        reasons.append(f"kurtosis {kurtosis:.1f} > {KURTOSIS_MAX}")

    physical = not reasons
    verdict = ("SHAPE OK — flat/uncorrelated noise profile" if physical else
               "STRUCTURED — " + "; ".join(reasons) + " (looks like a burst/spike train, not uniform noise)")

    return dict(burst_ratio=burst_ratio, kurtosis=kurtosis, verdict=verdict, physical=physical)


def verify_physical(arrivals, tick_s):
    """Combined gate: both the magnitude band and the shape check must pass."""
    jv = verify_jitter(arrivals, tick_s)
    sv = verify_shape(arrivals, tick_s)
    physical = bool(jv and jv["physical"]) and bool(sv is None or sv["physical"])
    return dict(jitter=jv, shape=sv, physical=physical)


def main():
    s = mcast_in("239.0.0.7", PS_PORT)
    print(f"[recv] listening on 239.0.0.7:{PS_PORT}...", flush=True)

    while True:
        n_bits = None
        tick_s = None
        bits = None
        pulses_seen = set()
        arrivals = []
        t_start = None

        while n_bits is None:
            data, addr = s.recvfrom(64)
            if len(data) < 3:
                continue
            magic, ptype = struct.unpack_from(">HB", data)
            if magic != PS_MAGIC or ptype != PS_START:
                continue
            _, _, n_bits, tick_us = struct.unpack_from(">HBHI", data)
            tick_s = tick_us / 1e6
            t_start = time.monotonic()
            bits = [0] * n_bits
            print(f"[recv] START from {addr[0]}  n_bits={n_bits}  tick={tick_s*1000:.3f}ms", flush=True)

        while True:
            data, addr = s.recvfrom(64)
            if len(data) < 3:
                continue
            magic, ptype = struct.unpack_from(">HB", data)
            if magic != PS_MAGIC:
                continue
            if ptype == PS_PULSE:
                t_arrival = time.monotonic()
                _, _, idx = struct.unpack_from(PS_PULSE_FMT, data)
                # sanity: does the claimed bit_index line up with arrival timing?
                # (kept as a printed observation for now — no reject logic yet,
                # that's the jitter/Allan-deviation check for a later pass)
                expected_idx = round((t_arrival - t_start) / tick_s) - 1
                if 0 <= idx < n_bits:
                    bits[idx] = 1
                    pulses_seen.add(idx)
                    arrivals.append((idx, t_arrival))
                drift = idx - expected_idx
                print(f"█(idx={idx},Δ={drift})", end="", flush=True)
            elif ptype == PS_END:
                print(flush=True)
                h = bits_to_hex(bits)
                print(f"[recv] END  {len(pulses_seen)}/{n_bits} ticks pulsed  reconstructed hash={h}", flush=True)

                pv = verify_physical(arrivals, tick_s)
                jv, sv = pv["jitter"], pv["shape"]
                if jv is None:
                    print("[recv] jitter check: not enough pulses to evaluate", flush=True)
                else:
                    tag = "PHYSICAL" if jv["physical"] else "SUSPECT "
                    print(f"[recv] jitter check [{tag}]  {jv['verdict']}  ({jv['basis']})", flush=True)
                if sv is not None:
                    tag = "PHYSICAL" if sv["physical"] else "SUSPECT "
                    print(f"[recv] shape  check [{tag}]  {sv['verdict']}  "
                          f"(burst_ratio={sv['burst_ratio']:.2f} kurtosis={sv['kurtosis']:.2f})", flush=True)
                tag = "PHYSICAL" if pv["physical"] else "SUSPECT "
                print(f"[recv] >>> combined [{tag}] <<<", flush=True)
                break


if __name__ == "__main__":
    main()
