# I(0) Substrate

Born: 2026-09-04

The single shared ground-truth timing plane for this project. A free-running,
ungated multicast broadcast (tick/theta/pd, no coupling, no feedback) that
everything else gates on rather than trusts blindly.

Replaces the old Quartz/AxisPulse Kuramoto oscillator-coupling stack
(mesh-health, presence-proof, beacon carrier), retired the same day after
finding it never added predictive or synchronization value beyond what the
plain, free-running I(0) broadcast already provides -- see
`project_substrate_smooths_not_predicts` in memory for the empirical case.

## Files

- **`layer0_daemon.py`** -- the substrate itself. Broadcasts tick/theta/pd
  over multicast (239.0.0.6:7500), 20Hz, no coupling to anything.
- **`i0_phase_auth.py`** + **`i0_phase_auth_prover.py`** -- presence-proof
  challenge/response gate. Nonce-bound, no stored secret; presence on the
  live substrate at a specific future tick IS the proof.
- **`truthd.c`** + **`truth_manifest.h`** -- local quorum-tier gate daemon.
  Judges TIER_PARTITIONED / TIER_LOCAL_TRIAD / TIER_FULL from I(0)
  freshness (+ EC2 reachability), served over a local Unix socket.
- **`i0_relay.py`** -- unicast relay of I(0) + phase-auth challenge traffic
  over WireGuard to EC2, since multicast doesn't cross the WAN.
- **`n_layer0_witness.py`** -- ungated outage/restart/pd-distribution
  recorder. Never gates on freshness itself -- its job is watching the
  substrate's own health from outside, including recording when it dies.

Copied here for reference; originals stay where they're actually deployed
and running (`quartz_core/`, `layer0_shared_substrate/`, top-level).
