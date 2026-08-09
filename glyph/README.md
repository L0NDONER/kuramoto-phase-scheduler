# glyph

Two mostly-independent things live here: the EC2 remote-intent channel
(HMAC-signed UDP commands over WireGuard), and a family of tick-jitter
data transports built on `jitter_encoder.py`'s scheme. This doc covers
both, focused on the 2026-08-09 session that built and tested the
jitter-over-WAN path end to end.

## EC2 intent channel

- **`ec2_intent_common.py`** — shared HMAC-signed packet format
  (`ec2_intent.key`, present on both Mint and EC2). `pack()`/`unpack()`
  for requests, `pack_response()`/`unpack_response()` for acks — reused
  as-is by the jitter-WAN ack mechanism below, not reimplemented.
- **`ec2_intent_send.py`** — sends a signed, scheduled intent (e.g.
  `restart_wg_easy`) to `ec2_intent_listener.py` on EC2. Gated behind a
  local `phase_auth.gate_check()` before anything is sent (2026-08-09):
  the HMAC signature is still what EC2 actually trusts, this only adds
  "and you were physically present on the LAN with a live carrier when
  you sent it" as an attestation layered on top. Verified live both
  ways: blocks in ~10s with no carrier running, passes through cleanly
  with one.
- **`ec2_intent_listener.py`** (runs on EC2) — verifies signature +
  freshness + replay, then gates actual execution through `truthd`'s
  `EC2_SELF` tier check before running anything.
- **`ec2_probe.py`** (Mint only, systemd `ec2-probe.service`) — polls
  EC2 reachability every 5s and sends a signed `witness:<TIER>` report
  so EC2's own `truthd --witness` can reflect Mint's local-triad
  health, which it has no independent way to observe (EC2 was never in
  the Kuramoto substrate). Confirmed live 2026-08-09: once Mint's own
  `quartz-node`/`quartz-presence-chain` were added (see
  `quartz-substrate/README.md`), both Mint's and EC2's `truthd` agreed
  on `TIER_FULL`.

## Jitter transports

`jitter_encoder.py`'s scheme: split a tick stream into fixed-width
symbol windows, jitter every tick in the window if the bit is 1, don't
if it's 0; decode by thresholding the measured inter-tick interval
standard deviation per window. Three implementations, increasing in
realism:

1. **`jitter_encoder.py`** — pure sandbox, synthetic timestamps, no
   network at all. Fixed 2026-08-09 to use real `text.encode("utf-8")`/
   `bytearray.decode("utf-8")` instead of a hand-rolled `ord(ch)&0xFF`
   scheme that silently mangled anything outside Latin-1.
2. **`jitter_live_tx.py`/`jitter_live_rx.py`** — real UDP, LAN
   multicast (`239.0.0.6:7409`), real Python scheduling jitter instead
   of synthetic. 7/7 clean decodes (ASCII + multi-byte UTF-8 + 5
   repeated trials), 0 bit errors.
3. **`jitter_wan_tx.py`/`jitter_wan_rx.py`** — real UDP, unicast over
   WireGuard to EC2 (`10.8.0.1:7410`), the real internet path instead
   of a single host's loopback. This is where the interesting bugs
   were.

### WAN jitter transport: what actually broke, in order

Getting this reliable took several real, distinct bugs, not one
threshold tweak:

1. **Firewall.** `10.8.0.1:7410`/`7411`/`7412` weren't open — packets
   went nowhere, 0 ticks received. Added scoped `ufw` rules
   (`from 10.8.0.0/24 to any port <N> proto udp`), same WG-only pattern
   already used for `58551/udp`.
2. **Packet reordering.** The LAN version trusted raw arrival order
   (fine on one host, effectively FIFO). Real WAN paths reorder
   packets; `decode()` assumes send order. Fixed by embedding a `seq`
   in every tick packet and sorting by it before decoding, not trusting
   arrival order.
3. **Wrong threshold direction.** First guess (18ms) was *worse* than
   the original (10ms) — not because higher is riskier in general, but
   because the actual measured `1`-bit cluster for this jitter/window
   config ranged 15.1-22.4ms, and 18ms sat inside that cluster's own
   low tail, misclassifying real `1`s as `0`. Root-caused with a
   dedicated diagnostic (`jitter_wan_diag_tx/rx.py`, not kept in the
   final version) that sent a known alternating 0/1 pattern and printed
   every window's measured std, rather than guessing from ping RTT.
4. **Small-sample estimator noise.** `TICKS_PER_BIT=8` (LAN-tuned)
   means each bit's classification rests on only 7 real interval
   samples — a real Gaussian jitter injection's *measured* std over 7
   draws is itself noisy, occasionally landing low by chance regardless
   of true threshold placement. Raised to `TICKS_PER_BIT=24` (3x more
   samples per window), which produced a clean, well-separated split in
   the diagnostic (`0`-cluster 1.0-4.4ms vs `1`-cluster 15.1-22.4ms).
5. **Residual ~1/10 trial error rate.** Even with (3) and (4) fixed, a
   rigorous 10-trial exact-bit-scoring run (`--expect`/`bits_wrong=N/M`
   flags added to `jitter_wan_rx.py` for this) landed 9/10 at 0 bit
   errors, 1/10 with exactly 1. Per the standard rule of thumb for this
   shape of result (few trials with 1-2 errors → add retry logic, not
   more threshold tuning; only chase the encoding itself if most trials
   fail) — added an ack: `jitter_wan_rx.py` signs and sends its decoded
   text back to Mint (reusing `ec2_intent_common.pack_response`, not
   new crypto) on `10.8.0.4:7412`; `jitter_wan_tx.py` waits up to 5s,
   compares the ack to what it actually sent, and retries (up to 3
   attempts, re-running `gate_check()` each time) on mismatch or
   timeout. Verified live: one round caught a real bit error
   (`'hello ec2 test'` → EC2 decoded `'hello ec2 tew'`) and correctly
   refused to confirm it rather than silently accepting the wrong text.

### Security note

Timing patterns are a data transport, not a credential. Anyone who can
reach `10.8.0.1:7410` over the tunnel could send the same tick pattern
— there's no cryptographic binding on the jitter packets themselves.
`jitter_wan_rx.py`'s optional `--execute` mode (decode, then run a
known intent) is deliberately still gated through `truthd`'s
`EC2_SELF` check before anything runs, same as the HMAC path, and the
receiver binds only to the WireGuard-internal address — the "no raw
internet exposure" pattern used everywhere else in this project, not a
new trust model.

### Real end-to-end run (2026-08-09)

`--execute` exercised against the live `restart_wg_easy` path, not just
a harmless test string: phase-auth gate `PASS` → `restart_wg_easy`
decoded correctly on the first attempt (120 bits, 2880 ticks) → ack
`CONFIRMED` → `truthd` `ALLOW TIER_FULL` → `docker restart wg-easy`
executed (`exit=0`, `stdout='wg-easy'`) → fresh WireGuard handshake
within 5s. Full chain — physical-presence attestation → timing-encoded
command → signed verification → distributed trust-tier gate → real
execution — worked end to end across three real machines and a WAN hop.

### Result reporting

Considered sending command output back over the *same jitter/timing
channel* (full duplex). Decided against it: the timing channel's only
purpose is proving physical presence, a property only the sender needs
— EC2 has no oscillator to attest to on the reply leg, so encoding the
response as jitter would just be slow (many seconds for real output)
for no security benefit over the already-signed, already-fast ack
channel. Instead, `jitter_wan_rx.py` now sends a second, distinct
signed report (`RESULT_PORT=7413`, separate from the transmission ack
on `ACK_PORT=7412` so it can't be confused with it or with a retry) after
executing, containing the actual `exit`/`stdout`/`stderr`. Reuses
`ec2_intent_common.pack_response` again, no new crypto.
`jitter_wan_tx.py` waits for it (`RESULT_TIMEOUT_S=15.0`) after a
confirmed send and prints it — no SSH needed to see the outcome.
Verified live: `'EC2 result: unknown intent 'hello ec2 test''` arrived
automatically for a non-intent test string.

### Persistent daemon (2026-08-09)

`jitter_wan_rx.py` was single-shot — handled exactly one burst then
exited, needing a manual restart before each send. Found by testing a
retry scenario: attempt 1's mismatch consumed the one running instance,
so attempts 2/3 had nothing listening and correctly (if confusingly,
until traced) timed out. Fixed: `receive_one_burst()`/`handle_burst()`
factored out of `main()`, which now loops forever on the same bound
socket. Verified live: two consecutive sends confirmed cleanly against
the same running instance (pid unchanged), and it survived a couple of
garbled partial bursts from interrupted test sends without crashing,
just moved on to the next one.

### Known limitation: no framing across concurrent senders

Confirmed live (2026-08-09), not just a theoretical concern: two
overlapping sends corrupt each other. `receive_one_burst()` collects
every tick arriving within any 1s gap into one lump, and `seq` is
per-sender (every `jitter_wan_tx.py` process starts its own stream at
`seq=0`), so two independent senders' ticks sort into one scrambled
mess with no way to tell them apart. Reproduced with two staggered raw
tick streams ("message alpha" at t+0, "message beta" at t+4, ~21s of
real overlap): the receiver logged `received 4798 ticks -> 199 bits`
(2496 + 2304 - 2, i.e. both streams merged into one burst) and decoded
pure garbage (`'������������������������'`).

Not fixed. The real fix would be a per-transmission session id embedded
in every tick packet (not just `seq`) so the receiver can separate
interleaved streams before decoding — worth doing if this is ever used
by more than one operator/script at a time; for now, single-sender use
only.
