#!/usr/bin/env python3
"""
presence_chain.py — single-quartz continuity-of-presence proof.

At each locked tick, extend a HMAC chain over the physical phase state:
  commit[N] = HMAC(commit[N-1], "{tick}:{theta1:.6f}:{theta2:.6f}:{pd:.6f}")

The chain embeds the specific noise trajectory of this crystal. A clone,
replay, or offline gap breaks it — you cannot pre-compute tick N+1 without
physically running the crystal to it.

No second Pi required. The crystal proves itself.

Checkpoints are saved to disk and optionally shipped to EC2.
Run presence_chain.py --verify [--witness <path>] to check continuity.

The tick only ever asserts "this is when" — it does not, by itself, prove
the checkpoint file is legitimate. Without an independent witness, --verify
can only check the file's *internal* self-consistency (tick spacing, and a
fast-hop rate derived from the file's own iso timestamps). A forger holding
the seed can compute the whole HMAC chain offline in an instant and then
write down whatever self-consistent iso timestamps they like — the old
internal check has no way to catch that, because it has no ground truth
outside the file it's judging.

presence_chain_witness.py closes that gap: run it anywhere on the LAN and
it independently timestamps every ChainLink broadcast (239.0.0.5:7460) as
it actually arrives on the wire, into its own append-only log that the
checkpoint producer never touches. Pass that log to --verify --witness and
each checkpoint's self-reported iso gets checked against a time nobody
writing the checkpoint file controls. No witness log = the run only proves
"this data is internally consistent," not "this took the time it claims."

Live tamper detection: every checkpoint also gets its commit prefix
pulse-coded across the following AxisPulse ticks on 239.0.0.7:7470 (see
pulse_hash_send.py/pulse_hash_recv.py). A companion listener
(presence_chain_live_verify.py) reconstructs that prefix from pulse
timing alone and cross-checks it against the same prefix already visible
in the plaintext ChainLink broadcast (239.0.0.5:7460) — the two channels
should always agree. If they don't, or the pulse timing's jitter looks
too clean/too noisy for a real oscillator, that's tampering caught at
the moment it happens, not something you find hours later re-reading a
checkpoint file.
"""
import socket, struct, time, hashlib, hmac as _hmac, os, json, sys, statistics
from collections import deque
from datetime import datetime, timezone

AP_GRP   = "239.0.0.2"; AP_PORT = 7404
AP_FMT   = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

# ChainLink multicast — presence proofs broadcast on the LAN
CL_GRP   = "239.0.0.5"; CL_PORT  = 7460
CL_FMT   = ">HI16sffff"; CL_SIZE = struct.calcsize(CL_FMT); CL_MAGIC = 0x434C

SEED_PATH = os.path.expanduser("~/.presence_seed")
CKPT_PATH = os.path.expanduser("~/.presence_checkpoint")
CKPT_INTERVAL = 100   # ticks between checkpoints

# EC2 — optional checkpoint shipping (reuse cerebellum port)
EC2_HOST = "ec2"
EC2_PORT = 7420

# live pulse-coded channel — see pulse_hash_send.py/pulse_hash_recv.py
PS_GRP  = "239.0.0.7"; PS_PORT = 7470
PS_MAGIC = 0x5053
PS_START, PS_PULSE, PS_END = 1, 2, 3
PS_START_FMT = ">HBHI"   # magic, type, n_bits, tick_us
PS_PULSE_FMT = ">HBH"    # magic, type, bit_index
PS_END_FMT   = ">HB"     # magic, type
PULSE_BYTES  = 4         # pulse-code the first 4 bytes (32 bits) of each checkpoint commit

def _load_or_generate_seed():
    if os.path.exists(SEED_PATH):
        return open(SEED_PATH, "rb").read()
    seed = os.urandom(32)
    open(SEED_PATH, "wb").write(seed)
    os.chmod(SEED_PATH, 0o600)
    print(f"[chain] genesis seed created  fp={hashlib.sha256(seed).hexdigest()[:8]}")
    return seed

def _extend(state: bytes, tick: int, theta1: float, theta2: float, pd: float) -> bytes:
    msg = f"{tick}:{theta1:.6f}:{theta2:.6f}:{pd:.6f}".encode()
    return _hmac.new(state, msg, hashlib.sha256).digest()

def _checkpoint(tick, commit, theta1, theta2, pd, iso):
    ckpt = {"tick": tick, "commit": commit.hex(), "theta1": theta1,
            "theta2": theta2, "pd": pd, "iso": iso}
    # keep last N checkpoints as a JSONL log
    with open(CKPT_PATH, "a") as f:
        f.write(json.dumps(ckpt) + "\n")
    return ckpt

def _iso_to_epoch(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()

# a hop is flagged as too-fast if its wall time is under this fraction of
# the chain's own median s/tick rate for that many ticks — catches a
# forger who has the seed and can recompute the HMAC chain offline, but
# didn't (or couldn't) burn the real wall-clock time to live it.
FAST_HOP_RATIO = 0.5

# how far a checkpoint's self-reported iso is allowed to drift from when an
# independent witness actually saw that tick's ChainLink packet on the wire —
# generous, since this only needs to catch "fabricated offline in one shot",
# not ordinary clock/scheduling jitter between two live processes
WITNESS_MAX_SKEW_S = 2.0

def _load_witness(path):
    entries = {}
    if not path or not os.path.exists(path):
        return entries
    for line in open(path):
        if not line.strip():
            continue
        e = json.loads(line)
        entries[e["tick"]] = e
    return entries

def _verify_chain(path_or_list, witness_path=None):
    """
    Verify a sequence of checkpoints form an unbroken chain.
    Checks two internal things per hop: tick-count integrity (no missing
    checkpoints) and wall-clock self-consistency (elapsed time vs this same
    chain's own observed tick rate). Those checks alone are not proof of
    anything — the tick only asserts "this is when", and a forger holding
    the seed can compute the whole HMAC chain offline in an instant, then
    invent iso timestamps that are perfectly self-consistent. Internal
    consistency is necessary, not sufficient.

    Real proof requires an independent witness (see presence_chain_witness.py)
    that timestamped these same commits as they crossed the wire, on a log
    the checkpoint's producer never touches. If witness_path is given, each
    checkpoint's claimed iso is checked against that independent observation
    instead of being trusted on its own say-so.
    """
    if isinstance(path_or_list, str):
        ckpts = [json.loads(l) for l in open(path_or_list) if l.strip()]
    else:
        ckpts = path_or_list

    if len(ckpts) < 2:
        print("Need at least 2 checkpoints to verify.")
        return False

    print(f"Verifying {len(ckpts)} checkpoints from tick {ckpts[0]['tick']} to {ckpts[-1]['tick']}")

    hops = []
    for i in range(1, len(ckpts)):
        a, b = ckpts[i-1], ckpts[i]
        tick_gap = b['tick'] - a['tick']
        wall_gap = _iso_to_epoch(b['iso']) - _iso_to_epoch(a['iso'])
        hops.append((a, b, tick_gap, wall_gap))

    # calibrate expected s/tick from this chain's own median hop — no
    # trust placed in any nominal constant, since observed pacing (e.g.
    # ~50ms/tick under load) can differ a lot from the nominal 10ms tick
    rates = [wall_gap / tick_gap for _, _, tick_gap, wall_gap in hops if tick_gap > 0 and wall_gap > 0]
    median_rate = statistics.median(rates) if rates else None

    tick_gaps = 0
    fast_hops = 0
    for a, b, tick_gap, wall_gap in hops:
        if tick_gap != CKPT_INTERVAL:
            print(f"  GAP at tick {a['tick']}→{b['tick']}: expected {CKPT_INTERVAL} ticks, got {tick_gap}")
            tick_gaps += 1
        if median_rate is not None and tick_gap > 0:
            expected_wall = median_rate * tick_gap
            if wall_gap < expected_wall * FAST_HOP_RATIO:
                print(f"  FAST HOP at tick {a['tick']}→{b['tick']}: {tick_gap} ticks claimed "
                      f"{wall_gap:.3f}s wall time, chain rate implies ≥{expected_wall * FAST_HOP_RATIO:.3f}s "
                      f"— likely precomputed, not lived")
                fast_hops += 1

    internally_consistent = tick_gaps == 0 and fast_hops == 0
    if internally_consistent:
        print(f"  Internally consistent — {len(ckpts)-1} intervals, no tick gaps, no fast hops")
    else:
        print(f"  {tick_gaps} tick gap(s), {fast_hops} fast hop(s) — internal consistency broken")

    witness = _load_witness(witness_path)
    if not witness_path:
        print("  No witness log passed — the above only checked self-consistency, which "
              "a seed-holding forger can fabricate offline. Not proof by itself.")
        return internally_consistent

    unwitnessed = mismatched = 0
    for ckpt in ckpts:
        w = witness.get(ckpt["tick"])
        if w is None:
            unwitnessed += 1
            continue
        if w["commit_prefix"] != ckpt["commit"][:32]:
            print(f"  WITNESS MISMATCH at tick {ckpt['tick']}: independently observed commit disagrees with checkpoint")
            mismatched += 1
            continue
        skew = abs(_iso_to_epoch(ckpt["iso"]) - _iso_to_epoch(w["iso"]))
        if skew > WITNESS_MAX_SKEW_S:
            print(f"  WITNESS SKEW at tick {ckpt['tick']}: checkpoint claims {ckpt['iso']}, "
                  f"witness saw it at {w['iso']} ({skew:.1f}s apart) — self-reported time uncorroborated")
            mismatched += 1

    if unwitnessed == len(ckpts):
        print("  No independent witness coverage for any checkpoint — still unproven, just internally consistent")
        return internally_consistent
    if mismatched:
        print(f"  {mismatched} checkpoint(s) disagree with independent witness — not trustworthy, "
              f"regardless of internal consistency")
        return False

    print(f"  Witness-corroborated: {len(ckpts) - unwitnessed}/{len(ckpts)} checkpoints "
          f"match independently observed ChainLink timing")
    return internally_consistent

# ── verify mode ───────────────────────────────────────────────────────────────
if "--verify" in sys.argv:
    _witness_path = None
    if "--witness" in sys.argv:
        _idx = sys.argv.index("--witness")
        _witness_path = sys.argv[_idx + 1]
    _verify_chain(CKPT_PATH, witness_path=_witness_path)
    sys.exit(0)

# ── run mode ──────────────────────────────────────────────────────────────────
seed = _load_or_generate_seed()
fp   = hashlib.sha256(seed).hexdigest()[:8]
print(f"[chain] seed fp={fp}  ckpt_interval={CKPT_INTERVAL} ticks")

# genesis commit: seed + "genesis" — no prior state
state = _hmac.new(seed, b"genesis", hashlib.sha256).digest()
print(f"[chain] genesis commit={state.hex()[:16]}")

ap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ap_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ap_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
ap_sock.bind(("", AP_PORT))
ap_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                   socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))
ap_sock.settimeout(1.0)

cl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
cl_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)

ps_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ps_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)

# rolling AxisPulse arrival deltas — gives the live pulse channel its own
# real observed tick_s instead of trusting a nominal constant
_tick_times = deque(maxlen=20)
_pulse_bits = None   # bits pending transmission for the current checkpoint, or None
_pulse_idx  = 0

ec2_sock = None
try:
    ec2_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _ec2_addr = socket.getaddrinfo(EC2_HOST, EC2_PORT, socket.AF_INET)[0][4]
except Exception:
    _ec2_addr = None
    print("[chain] EC2 not reachable — checkpoints local only")

# AxisPulse carries two independent per-node tick counters (sid=1 pi,
# sid=2 pi2), each incrementing on its own beacon's own 10ms cadence.
# Consuming both interleaved corrupts the tick sequence (numbers jump
# non-monotonically between two counters) and the pulse channel's timing
# (looks like ~5ms noise from two 10ms streams interleaved, not real
# oscillator jitter) — same trap adev.py's collect_live() already guards
# against. Lock the chain to whichever sid this process observes first.
_LOCK_SID = None

tick_count = 0
last_tick  = None

print("[chain] listening for AxisPulse...", flush=True)

while True:
    try:
        data, _ = ap_sock.recvfrom(64)
    except socket.timeout:
        continue
    if len(data) < AP_SIZE:
        continue
    f = struct.unpack_from(AP_FMT, data)
    if f[0] != AP_MAGIC or not f[2]:
        continue

    if _LOCK_SID is None:
        _LOCK_SID = f[1]
        print(f"[chain] locking to sid={_LOCK_SID}", flush=True)
    if f[1] != _LOCK_SID:
        continue

    tick   = f[3]
    theta1 = f[4]
    theta2 = f[5]
    pd     = abs(f[6])

    # skip duplicate ticks (AxisPulse fires twice per beacon period)
    if last_tick is not None and tick == last_tick:
        continue
    last_tick = tick

    now = time.monotonic()
    _tick_times.append(now)

    # extend chain
    state = _extend(state, tick, theta1, theta2, pd)
    tick_count += 1
    commit16 = state[:16]

    # broadcast ChainLink
    cl_pkt = struct.pack(CL_FMT, CL_MAGIC, tick, commit16, theta1, theta2, pd, 0.0)
    cl_sock.sendto(cl_pkt, (CL_GRP, CL_PORT))

    # checkpoint
    if tick_count % CKPT_INTERVAL == 0:
        iso  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ckpt = _checkpoint(tick, state, theta1, theta2, pd, iso)
        print(f"[chain] tick={tick:>10}  commit={state.hex()[:16]}  pd={pd:.4f}  {iso}")

        # ship to EC2
        if ec2_sock and _ec2_addr:
            try:
                ec2_sock.sendto(json.dumps(ckpt).encode(), _ec2_addr)
            except Exception:
                pass

        # start pulse-coding this checkpoint's commit prefix across the
        # next AxisPulse ticks — physically timed, catchable-if-forged
        if _pulse_bits is not None:
            print("[chain] WARNING: previous pulse train still in flight — "
                  "CKPT_INTERVAL too short for PULSE_BYTES*8 ticks, dropping it")
        prefix = state[:PULSE_BYTES]
        bits = []
        for byte in prefix:
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)
        _pulse_bits = bits
        _pulse_idx  = 0
        deltas = [b - a for a, b in zip(_tick_times, list(_tick_times)[1:])]
        tick_s = statistics.median(deltas) if len(deltas) >= 2 else 0.05
        tick_us = max(1, int(tick_s * 1e6))
        ps_sock.sendto(struct.pack(PS_START_FMT, PS_MAGIC, PS_START, len(bits), tick_us),
                        (PS_GRP, PS_PORT))
        print(f"[chain] pulse-coding commit prefix {prefix.hex()} "
              f"({len(bits)} bits, tick~{tick_s*1000:.1f}ms)")

    # advance the in-flight pulse train by one AxisPulse tick
    elif _pulse_bits is not None:
        if _pulse_idx < len(_pulse_bits):
            if _pulse_bits[_pulse_idx]:
                ps_sock.sendto(struct.pack(PS_PULSE_FMT, PS_MAGIC, PS_PULSE, _pulse_idx),
                               (PS_GRP, PS_PORT))
            _pulse_idx += 1
        if _pulse_idx >= len(_pulse_bits):
            ps_sock.sendto(struct.pack(PS_END_FMT, PS_MAGIC, PS_END), (PS_GRP, PS_PORT))
            _pulse_bits = None
