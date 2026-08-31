# quartz_core

Real-crystal carrier (`quartz_beacon.py`) plus a presence-based auth
protocol (`phase_auth.py` / `phase_auth_prover.py`) that rides on it.
No stored secrets anywhere in this package — every credential here is
"I can currently observe the same physical oscillator you can."

Status: **load-bearing, not deployed.** The auth protocol here is not
throwaway research code — `phase_auth.gate_check` is imported as the
real auth gate by `pi1_node.py`, `pi2_node.py`, `mint_node.py`,
`ec2_relay.py`, `cascade_pll/channels/ec2_intent_send.py`, and four
files under `glyph/` (`glyph_tx.py`, `glyph_intent.py`,
`jitter_wan_tx.py`, `tick_print.py`). Don't delete this package without
checking those callers first (nearly happened 2026-08-30). Separately,
and still true: no `quartz_beacon.py` carrier is currently running
anywhere (verified live on Mint/pi/pi2, 2026-08-30), so `gate_check()`
has nothing to observe and cannot currently PASS — see [Production
gaps](#production-gaps) and [Known caveats](#known-caveats).

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

4. **Unbounded initial lock wait — found and fixed (2026-08-09).**
   `run_challenge()`'s "wait for AxisPulse lock" loop had no overall
   deadline, only a repeating 2s `select()` poll — with the carrier
   fully down it blocked forever instead of failing. Found while
   testing 100% packet loss on `239.0.0.5`... on the wrong port (see
   below). Fixed: `LOCK_TIMEOUT_S = 10.0` bounds it now. Verified:
   fails cleanly at ~10.03s with no carrier running at all.
5. **Packet loss on the real peer link (`239.0.0.1:7400`, RAW) —
   tested, one real blind spot found.** An earlier pass injected loss
   on `239.0.0.2:7404` (AxisPulse, the *output* stream to consumers)
   and wrongly concluded the beacon was robust to loss — that port
   never touches beacon-to-beacon coupling, so nothing was actually
   being tested on the peer-lock path. Re-run against the actual RAW
   port with `iptables -m statistic --mode random`, scoped to
   `udp dport 7400 -d 239.0.0.1` only:
   - 30% loss: stayed `locked=True`, `dev` rose ~250x (0.003–0.013 vs
     baseline ~0.00003).
   - 70% loss: still `locked=True`, `dev` 0.009–0.025.
   - 100% loss: `pd` drifted wildly for the full `PEER_STALE_S` window
     (`+1.77` → `+2.86` → `-2.33` across three ticks, since the peer's
     `theta` is frozen while ours keeps advancing) before correctly
     dropping to `locked=False no peers` at the 3s mark. Removing the
     rule re-locked immediately at `dev=0.00006`, back to baseline.
   - **Blind spot — found and fixed (2026-08-09).** `dev` is the EMA
     of *frame-to-frame* `pd` change, and a frozen peer produces
     smooth, predictable drift rather than noise — so `dev` stayed
     flat (~0.011) for the entire 3-second window where `pd` itself
     was meaningless, right up until the peer was dropped. Anything
     treating `dev` as a live trust/health signal has a blind window
     exactly where a dying peer matters most. Fixed by repurposing the
     AxisPulse packet's dead `load_avg` slot (always `0.0`, unread by
     any consumer in the codebase) to carry `peer_age_s` — seconds
     since the broadcasting node's peer last sent a RAW update, which
     `quartz_beacon.py` already tracked internally but never exposed.
     `reflex.py` (the one real consumer doing threshold classification
     on `pd_dev`, via `ALERT_PD`/`WITHDRAW_PD`) now also checks
     `ALERT_AGE_S = 1.5` / `WITHDRAW_AGE_S = 2.5` against `peer_age_s`
     independently of `pd_dev`. Verified live: under the same RAW-loss
     injection, `age_ratio` drove `CALM → ALERT → WITHDRAW` while
     `pd_ratio` stayed at ~0.07 the whole time — `reflex.py` reached
     `WITHDRAW` before the beacon's own `PEER_STALE_S` cliff, then
     fully recovered to `CALM` once the link was restored.

6. **Concurrent challenges — two real bugs found and fixed
   (2026-08-09).** Ran two `phase_auth.py` challengers simultaneously
   against one prover.
   - **Fixed response port collision.** `resp_sock` bound the
     hardcoded `PA_RESP_PORT=7452` with only `SO_REUSEADDR`. Two
     challengers on the same host collide silently — Linux delivers
     unicast traffic on a shared bound port to whichever socket bound
     *last*, not both, so the other challenger's response never
     arrives at all. Reproduced live: one challenger `PASS`ed, the
     other timed out to `FAIL` even though the prover answered both
     correctly. Fixed: each challenger now binds an ephemeral,
     OS-assigned port and puts the real port number in the challenge
     packet — the wire format already had a `resp_port` field for
     this, it just wasn't being used per-challenge.
   - **`pending` dict overwrite on tick collision.** With the port fix
     alone, a second bug surfaced: when both challengers happened to
     lock onto the exact same `(sid, target_tick)` (common — both
     processes read the same live stream), `phase_auth_prover.py`'s
     `pending` dict, keyed by `(sid, target_tick)` holding a single
     tuple, let the second registration silently overwrite the first.
     Only one challenger ever got a response; the other timed out with
     no explanation. Fixed: `pending` now holds a list per
     `(sid, tick)`, and every waiter gets answered when the tick
     arrives.
   - Retest: 5 rounds of simultaneous challenger pairs, 4 of which
     landed on the exact same `(sid, tick)`, all 10/10 `PASS`.

7. **Packet reordering — tested, held clean, no fix needed
   (2026-08-09).** Run in an isolated `ip netns` sandbox (not
   `iptables` on the real interface — see methodology note below):
   `tc qdisc ... netem delay <N>ms reorder <P>% <correlation>%` on the
   namespace's own `lo`, two profiles — mild (10ms delay, 25%/50%
   reorder) and aggressive (100ms delay, 50%/90% reorder). 18/18
   `phase_auth` challenges `PASS`ed across both. Makes sense
   architecturally: both the observe loop and the prover match
   strictly on exact `(sid, tick)` value, never on arrival sequence,
   so reordering has nothing to exploit. Useful side confirmation:
   beacon `dev` climbed to 0.007–0.053 under reordering (real noise,
   correctly detected) vs. the 0.011-flat-while-wrong reading from the
   staleness blind spot above (item 5) — `dev` isn't broken in
   general, it specifically can't see smooth/frozen drift.

**Methodology note:** the loss tests (items 5–6 above) used scoped
`iptables -m statistic --mode random` rules on the real interface,
cleaned up after each run. This one used an isolated `ip netns`
instead — `tc netem` covers loss, reorder, delay, duplication, and
corruption all from one tool, fully isolated on the namespace's own
`lo`, single-command teardown (`ip netns del`), zero risk to real
traffic on the host. Preferred over `iptables` for any future fault
injection here.

8. **Prover restart mid-window — tested, held clean, no fix needed
   (2026-08-09).** Killed `phase_auth_prover.py` 0.15s into an
   in-flight challenge (before it could respond): challenger correctly
   `FAIL`ed after the full `WINDOW_S` (3s), no hang, clean exit.
   Restarted the prover: the very next challenge `PASS`ed immediately,
   full recovery. Architectural note, not a bug: a challenge is a
   one-shot multicast broadcast, not persisted anywhere — if the
   prover is down at the exact moment a challenge is sent, restarting
   it afterward can't retroactively catch that specific challenge, the
   broadcast is already gone. The clean `FAIL` is the correct outcome
   for that case.

Not yet tested: clock step during a challenge — left untested on
purpose, not worth stepping the system clock on a shared machine to
find out; better reasoned about by reading the deadline code (plain
`time.time()` throughout `phase_auth.py`/`phase_auth_prover.py`, no
monotonic clock) than by actually doing it. Point 2 of [Production
gaps](#production-gaps) below still stands — eight fault scenarios
covered so far, not a systematic matrix.

## Wire formats

| Packet | Group:port | Format | Notes |
|---|---|---|---|
| RAW | `239.0.0.1:7400` | `!HBIffBQ` (24B) | magic, sid, tick, theta, omega, pad, t0_ns |
| AxisPulse | `239.0.0.2:7404` | `>HBBIfffffHQ` (38B) | magic, sid, locked, tick, theta1, theta2, pd, pd_dev, peer_age_s, drains, t0_ns |
| Challenge | `239.0.0.5:7451` | `!H16sIHB` | magic, nonce, target_tick, resp_port, sid |
| Response | unicast, `resp_port` | `!H16s32s` | magic, nonce, sha256 digest |

## Production gaps

Real callers already import `gate_check` (see [Status](#quartz_core)
above), but no carrier is currently deployed for it to check against,
so today it can only ever fail closed. Before deploying a carrier and
making this gate actually enforce something live, it needs, in order:

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
