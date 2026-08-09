# quartz_core

Real-crystal carrier (`quartz_beacon.py`) plus a presence-based auth
protocol (`phase_auth.py` / `phase_auth_prover.py`) that rides on it.
No stored secrets anywhere in this package — every credential here is
"I can currently observe the same physical oscillator you can."

Status: **research/demo lane**, not production. See [Production gaps](#production-gaps).

## Files

| File | Role |
|---|---|
| `quartz_beacon.py` | Carrier. Reads each host's own crystal drift and broadcasts phase (AxisPulse) over multicast. |
| `hpet_ppm.py` | Third independent clock witness (HPET vs `CLOCK_MONOTONIC_RAW`) used by the `mint` beacon instance instead of `adjtimex`. |
| `phase_auth.py` | Challenger/verifier. Issues a challenge, independently observes the target tick, checks the prover's response. |
| `phase_auth_prover.py` | Prover daemon. Answers challenges by hashing its own observation of the same tick. |

## The carrier: `quartz_beacon.py`

Each of three hosts (`pi`=sid 1, `pi2`=sid 2, `mint`=sid 3) runs its own
instance. Each instance:

1. Reads its own crystal's drift in ppm — `adjtimex` on pi/pi2, HPET on
   mint (`adjtimex` reports the NTP-disciplined correction, not the raw
   crystal rate; HPET diffed against `CLOCK_MONOTONIC_RAW` is a true
   third witness that NTP cannot slew).
2. Derives a phase `theta` advancing at `OMEGA0 * (1 + ppm * 1e-6)` —
   `OMEGA0 = 1.06 rad/s` is a shared nominal carrier; ppm is a
   fractional correction on top of it, not a carrier of its own.
3. Broadcasts raw phase on `239.0.0.1:7400` (RAW format, 24 bytes) and
   listens for the other two hosts' raw phase on the same group.
4. For each live peer, computes the phase difference `pd` between
   itself and that peer, and broadcasts the combined AxisPulse packet
   (38 bytes, `239.0.0.2:7404`) that all downstream consumers
   (`phase_auth.py`, `nazare.py`, `reflex.py`, `bio.py`, `glyph/*`)
   subscribe to.

Tick cadence is fixed at `TICK_S = 0.01` — **100 ticks/second**, not
75 as an earlier draft of `phase_auth.py` assumed (fixed 2026-08-08).

A peer is only "live" if seen within `PEER_STALE_S = 3.0` seconds, and
once a `sid` is bound to a source IP, a second source claiming the
same `sid` is rejected until the bound source goes stale — this is the
origin-pinning fix for the 2026-07-09 corruption incident, where two
sources racing for the same slot made `pd` alternate between real
values and garbage (dev spiking 0.02–0.87 against a ~0.0001 baseline
with no pinning).

Run (needs root — HPET mmap and/or adjtimex):

```
sudo python3 quartz_core/quartz_beacon.py pi     # sid=1
sudo python3 quartz_core/quartz_beacon.py pi2    # sid=2
sudo python3 quartz_core/quartz_beacon.py mint   # sid=3, uses hpet_ppm.py
```

At least two instances need to be running and mutually reachable over
multicast before there's a `pd` for anything downstream (including
phase-auth) to use.

## The auth protocol: `phase_auth.py` + `phase_auth_prover.py`

```
Challenger                                   Prover
-----------                                  ------
target_tick = current_tick + 30 (~300ms out)
nonce = random 16 bytes
--- CHAL: nonce | target_tick | resp_port | sid ----------> (239.0.0.5:7451)
                                              observes AxisPulse at
                                              target_tick -> (theta, pd)
                                              digest = SHA256(nonce
                                                | tick | theta | pd)
<-------------------------------- RESP: nonce | digest --- (challenger:7452)
observes AxisPulse at target_tick independently
-> (theta, pd) -> expected_digest
PASS iff digest == expected_digest
```

Both sides decode the *same* multicast AxisPulse packet for that tick,
so `theta`/`pd` match bit-for-bit by construction — this is not
relying on clock-synced computation, it's relying on both parties
having received the identical bytes. What's actually being proven is
narrower than "identity": **presence on the multicast group that
carries the real carrier, at that specific tick.**

Claimed security properties (from the module docstring):

- **identity** — only a node that observed that `(theta, pd)` at that
  tick can answer.
- **liveness** — the target-tick window closes in ~300ms
  (`CHALLENGE_AHEAD = 30` ticks at 100 tps). Measured against a live
  local carrier (2026-08-08, 6 runs): actual challenge-to-observed-tick
  delay ran 314–329ms, consistently 5–10% over the 300ms nominal
  figure — not carrier jitter (the carrier's own `pd_dev` held at
  ~0.00006 across the same runs). The observed-tick-to-PASS verify leg
  was tight, 6–9ms.

  **Diagnosed (2026-08-09):** the observe loop reads every AxisPulse
  packet from both live sids (~200pps combined) while waiting for the
  one matching `(sid, tick)`, and each `select()`+`recvfrom()`+
  `struct.unpack`+compare costs a fairly constant ~0.25ms in CPython.
  10 instrumented runs logged 60–61 packets processed per run;
  overshoot ÷ packet count clustered tightly at 0.23–0.28ms/packet —
  that alone accounts for the full 14–17ms gap. GC was checked and
  ruled out (zero collections fell inside the send→observe window on
  any run). At 0.5% of the 3s `WINDOW_S` margin, this is noise, not a
  defect — not worth optimizing unless the window is tightened a lot
  further.
- **adjacency** — AxisPulse multicast doesn't route off-LAN, so a
  correct response implies LAN presence.
- **geometry-member** — `pd` ties the response to a specific oscillator
  *pair*, not just "a" carrier.

The prover also keeps a 200-tick ring buffer (`TICK_BUF`) so it can
answer a challenge for a tick it already saw before the challenge
arrived, and holds unanswered challenges in a `pending` dict with a
5-second expiry for ticks it hasn't reached yet.

Run:

```
# on the prover host
python3 quartz_core/phase_auth_prover.py

# on the challenger host, one-shot or looping every 10s
python3 quartz_core/phase_auth.py
python3 quartz_core/phase_auth.py --loop
```

Both require a live `quartz_beacon.py` carrier reachable over the same
multicast groups — there is nothing to verify without one running.

Importable gate used by `pi1_node.py`, `pi2_node.py`, `mint_node.py`,
and `glyph/glyph_intent.py`, `glyph/tick_print.py`, `glyph/glyph_tx.py`:

```python
from quartz_core.phase_auth import gate_check

if not gate_check():
    ...  # no LAN prover answered correctly in time
```

## Fault injection (2026-08-08)

Two live tests run against a real carrier + real prover, both LAN-only,
no raw sockets:

1. **Off-path race attack — found and fixed.** The challenge nonce is
   broadcast in cleartext on `239.0.0.5:7451`, so any listener on the
   LAN can read it without observing anything real. `run_challenge()`
   used to return `FAIL` on the *first* nonce-matching response,
   correct or not — an attacker who echoes the nonce back with 32
   random bytes doesn't have to wait for a future tick like the real
   prover does, so it reliably won the race and produced a false
   `FAIL` before the legitimate `PASS` arrived. Reproduced live, then
   fixed: mismatched digests are now logged as `REJECTED ... (still
   waiting)` and the challenger keeps listening until either a correct
   digest arrives or `WINDOW_S` expires. Re-run of the same attack
   post-fix: `REJECTED` followed by the real prover's `PASS`.
2. **Off-subnet forgery — held.** A response spoofed from an address
   outside `PROVER_SUBNET` (10.0.0.0/24) was correctly logged
   `IGNORED`, and the real prover's response still passed through.
3. **Tick-namespace collision across sids — found and fixed
   (post-fix retest, same day).** `tick` in the AxisPulse packet is a
   per-node counter — each `quartz_beacon.py` instance starts counting
   from 0 at its own process start, it is not a shared clock. Neither
   `phase_auth.py` nor `phase_auth_prover.py` filtered on `sid`, only
   on `tick`, so with two live nodes broadcasting (required for `pd`
   to exist at all) a `target_tick` number could match either node's
   stream. Reproduced live with no attacker present: the prover
   answered from one sid's buffered packet while the challenger later
   observed the other sid's packet for the same tick number, producing
   a legitimate `REJECTED` → `FAIL` with both sides honest. Fixed by
   adding `sid` to the challenge packet (challenger locks onto a sid
   during its initial AxisPulse wait and pins the whole exchange to
   it) and filtering both the live-tick match and the prover's ring
   buffer on `(sid, tick)` instead of `tick` alone. Retest: 8/8 PASS,
   alternating cleanly across both live sids.

Not yet tested: packet loss/reordering on the AxisPulse multicast
itself, prover restart mid-window, clock step during a challenge,
concurrent challenges. Point 2 of [Production
gaps](#production-gaps) below still stands — three fault scenarios
found and fixed so far, not a systematic matrix.

## Wire formats

| Packet | Group:port | Format | Notes |
|---|---|---|---|
| RAW | `239.0.0.1:7400` | `!HBIffBQ` (24B) | magic, sid, tick, theta, omega, pad, t0_ns |
| AxisPulse | `239.0.0.2:7404` | `>HBBIfffffHQ` (38B) | magic, sid, locked, tick, theta1, theta2, pd, pd_dev, load_avg, drains, t0_ns |
| Challenge | `239.0.0.5:7451` | `!H16sIHB` | magic, nonce, target_tick, resp_port, sid |
| Response | unicast, `resp_port` | `!H16s32s` | magic, nonce, sha256 digest |

## Production gaps

This package is deliberately staying in the research/demo lane. If it
ever needs to gate something real, it needs, in order:

1. **mTLS on top** — phase-auth demoted from "the credential" to an
   attestation signal layered onto an already-authenticated channel,
   not a standalone auth mechanism. `hash(tick, theta, pd)` is
   unaudited and has no proven cryptographic properties as a bare
   credential.
2. **Fault injection** — one scenario tested and fixed (see [Fault
   injection](#fault-injection-2026-08-08) above); no systematic
   Jepsen-style partition/reorder matrix exists yet. The
   origin-pinning fix in the carrier was validated against one real
   corruption incident, not a fault matrix either.
3. **Service discovery** — hosts and sids are hardcoded
   (`SID_BY_HOST = {"pi": 1, "pi2": 2, "mint": 3}`); nothing resembling
   real peer discovery exists.
4. **Persistence / restart behavior** — untested across process
   restarts, clock steps, or `t0` re-anchoring edge cases beyond the
   one resample-continuity fix already in `quartz_beacon.py`.
5. **Monitoring** — stdout `print` only; no metrics, no alerting on a
   silently-stale carrier.

## Known caveats

- `quartz_beacon.py` needs root (HPET mmap on `mint`, `adjtimex` on
  `pi`/`pi2`).
- No AxisPulse carrier is deployed anywhere as of 2026-08-08 (the old
  beacon deployment was decommissioned 2026-07-29); it has only been
  run ad hoc, locally, for the timing/fault-injection tests above —
  not a live service.
