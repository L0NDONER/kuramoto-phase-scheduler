#!/usr/bin/env python3
"""
quartz_presence_chain.py — per-host continuity-of-presence proof, sibling
to presence_chain.py but reading quartz_node's live self_phase instead of
the (decommissioned) AxisPulse multicast feed.

At each new self_phase observation, extend a local HMAC chain:
  commit[N] = HMAC(commit[N-1], "{tick}:{self_phase:.6f}")

quartz_node.c rewrites /tmp/quartz_peer_health.json (atomic tmp+rename)
about once per second with the live Kuramoto phase for this host. Tick
here is this process's own observation count, not a wire-carried counter
-- the chain still can't be precomputed offline, because self_phase is
governed by the live oscillator's actual physical trajectory (coupling
to two real peers over the network), not by anything this process alone
controls.

Deliberately NOT shared across hosts -- see project note "per-host, no
shared field": Pi1/Pi2/Mint each run their own instance against their
own local health file. There is no cross-host verification step here;
that's the whole point (compromising one chain doesn't touch the others).

Usage:
    python3 quartz_presence_chain.py            # run
    python3 quartz_presence_chain.py --verify   # check local chain continuity
"""
import hashlib
import hmac as _hmac
import json
import os
import statistics
import sys
import time

HEALTH_PATH = "/tmp/quartz_peer_health.json"
SEED_PATH = os.path.expanduser("~/.quartz_presence_seed")
CKPT_PATH = os.path.expanduser("~/.quartz_presence_checkpoint")
CKPT_INTERVAL = 20        # ticks between checkpoints (health file updates ~1/s)
POLL_INTERVAL_S = 0.5      # poll faster than the ~1s write cadence so no update is missed
FAST_HOP_RATIO = 0.5       # same "precomputed, not lived" check as presence_chain.py


def _load_or_generate_seed():
    if os.path.exists(SEED_PATH):
        return open(SEED_PATH, "rb").read()
    seed = os.urandom(32)
    open(SEED_PATH, "wb").write(seed)
    os.chmod(SEED_PATH, 0o600)
    print(f"[qchain] genesis seed created  fp={hashlib.sha256(seed).hexdigest()[:8]}")
    return seed


def _extend(state, tick, self_phase):
    msg = f"{tick}:{self_phase:.6f}".encode()
    return _hmac.new(state, msg, hashlib.sha256).digest()


def _read_self_phase():
    try:
        with open(HEALTH_PATH) as f:
            first_line = f.readline()
        return json.loads(first_line)["self_phase"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def _checkpoint(tick, commit, self_phase, iso):
    ckpt = {"tick": tick, "commit": commit.hex(), "self_phase": self_phase, "iso": iso}
    with open(CKPT_PATH, "a") as f:
        f.write(json.dumps(ckpt) + "\n")
    return ckpt


def _iso_to_epoch(iso):
    import datetime
    return datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def _verify_chain(path):
    ckpts = [json.loads(l) for l in open(path) if l.strip()]
    if len(ckpts) < 2:
        print("Need at least 2 checkpoints to verify.")
        return False

    print(f"Verifying {len(ckpts)} checkpoints from tick {ckpts[0]['tick']} "
          f"to {ckpts[-1]['tick']}")

    hops = []
    for i in range(1, len(ckpts)):
        a, b = ckpts[i - 1], ckpts[i]
        tick_gap = b["tick"] - a["tick"]
        wall_gap = _iso_to_epoch(b["iso"]) - _iso_to_epoch(a["iso"])
        hops.append((a, b, tick_gap, wall_gap))

    rates = [w / t for _, _, t, w in hops if t > 0 and w > 0]
    median_rate = statistics.median(rates) if rates else None

    tick_gaps = fast_hops = 0
    for a, b, tick_gap, wall_gap in hops:
        if tick_gap != CKPT_INTERVAL:
            print(f"  GAP at tick {a['tick']}->{b['tick']}: expected {CKPT_INTERVAL}, got {tick_gap}")
            tick_gaps += 1
        if median_rate is not None and tick_gap > 0:
            expected_wall = median_rate * tick_gap
            if wall_gap < expected_wall * FAST_HOP_RATIO:
                print(f"  FAST HOP at tick {a['tick']}->{b['tick']}: claimed {wall_gap:.3f}s, "
                      f"chain rate implies >={expected_wall * FAST_HOP_RATIO:.3f}s -- likely precomputed")
                fast_hops += 1

    ok = tick_gaps == 0 and fast_hops == 0
    if ok:
        print(f"  Internally consistent -- {len(ckpts)-1} intervals, no gaps, no fast hops")
    else:
        print(f"  {tick_gaps} gap(s), {fast_hops} fast hop(s) -- continuity broken")
    return ok


if "--verify" in sys.argv:
    _verify_chain(CKPT_PATH)
    sys.exit(0)

def _resume_from_checkpoint():
    """Resume tick count + chain state from the last checkpoint, if any.
    Without this, a process restart silently forks a second chain into
    the same log at tick=0 -- confirmed live: two independent runs both
    starting from tick 0 appended overlapping ticks to one file and
    --verify correctly flagged it as a broken chain, because it was."""
    if not os.path.exists(CKPT_PATH):
        return None
    lines = [l for l in open(CKPT_PATH) if l.strip()]
    if not lines:
        return None
    last = json.loads(lines[-1])
    return last["tick"], bytes.fromhex(last["commit"])


seed = _load_or_generate_seed()
resumed = _resume_from_checkpoint()
if resumed:
    tick, state = resumed
    print(f"[qchain] resuming from checkpoint  tick={tick}  commit={state.hex()[:16]}"
          f"  reading {HEALTH_PATH}", flush=True)
else:
    state = _hmac.new(seed, b"genesis", hashlib.sha256).digest()
    tick = 0
    print(f"[qchain] genesis commit={state.hex()[:16]}  reading {HEALTH_PATH}", flush=True)

last_phase = None

while True:
    phase = _read_self_phase()
    if phase is None:
        print("[qchain] health file missing/unreadable -- quartz_node not running?", flush=True)
        time.sleep(POLL_INTERVAL_S)
        continue

    if phase == last_phase:
        time.sleep(POLL_INTERVAL_S)
        continue
    last_phase = phase

    tick += 1
    state = _extend(state, tick, phase)

    if tick % CKPT_INTERVAL == 0:
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _checkpoint(tick, state, phase, iso)
        print(f"[qchain] tick={tick:>6}  commit={state.hex()[:16]}  "
              f"self_phase={phase:.4f}  {iso}", flush=True)

    time.sleep(POLL_INTERVAL_S)
