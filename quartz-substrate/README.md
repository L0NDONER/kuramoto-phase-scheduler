# quartz-substrate

Not an OS. A small distributed substrate: one continuous phase-coupling
daemon per host, a job-dispatch layer on top of it, and two traffic-shaping
daemons that read the substrate's health as an input. Packaged here as the
actual files currently running, on their actual hosts — nothing renamed,
nothing invented.

## Hosts

| Host | IP | Role |
|---|---|---|
| Mint | 10.0.0.71 | `quartz_node` (node 1), `quartz_metro_node` coordinator, `quartz_wan_gain.py` |
| pi (`ssh pi`) | 10.0.0.122 | `quartz_node` (node 2), `quartz_metro_node` worker 0, `quartz_firestick_gain.py`, bump-in-wire for the Firestick |
| pi2 (`ssh pi2`) | 10.0.0.174 | `quartz_node` (node 3), `quartz_metro_node` worker 1 |

## daemons/

- **`quartz_node.c`** — the substrate. 3-node Kuramoto phase coupling over UDP
  multicast-free unicast, `CLOCK_MONOTONIC_RAW` (deliberately not
  NTP-disciplined). Writes `/tmp/quartz_peer_health.json` every ~1s: own
  phase plus per-peer last-seen/deviation/healthy. Nothing else on any host
  talks to the wire directly — everything downstream reads this file.
- **`quartz_metro_node.c`** — job-table coordinator/worker binary (xor,
  portfolio, cellstate job types selected at runtime via argv, not compiled
  in). Reads `quartz_peer_health.json` before each round; skips (doesn't
  retry-storm) a round if a worker is marked unhealthy. Also loads job types
  it wasn't built with, via `dlopen` — see `daemons/jobs/` below.
  `recv_until()` validates the sender's IP against the expected worker for
  the slot being read, so a dead worker's slot can't be silently filled by
  another worker's reply (fixed 2026-07-29 after a fault-injection test
  showed exactly that happening, freezing the dead worker's weight with no
  error logged). For continuous jobs (portfolio), `MSG_INIT` used to be
  sent by the coordinator exactly once, at its own startup, so a worker
  restarting mid-job would wait forever for an `MSG_INIT` that never came
  — recovering required restarting the coordinator too. Fixed 2026-07-29:
  a restarted worker now probes with `MSG_INIT_REQ`, which the coordinator
  drains and answers (via `MSG_PEEK`, so it doesn't eat an in-flight
  `MSG_CANDIDATE`) with that worker's current weight, so it can rejoin
  without a coordinator restart. The reverse direction needed no fix:
  killing the coordinator is a non-event for workers — they have no
  dependency on its liveness and just keep looping on their last known
  weight until it reappears, at which point the normal step-req/candidate
  exchange resyncs everything (verified live, 2026-07-29).
- **`quartz_job.h`** — the `JobSpec` ABI shared between `quartz_metro_node.c`
  and every job plugin `.so`. Struct layout is the contract; a plugin built
  against a different copy of this header is a mismatch, not just a warning.
- **`jobs/sphere.c`** — demo plugin (minimizes sum-of-squares, no workers).
  Proves the actual claim: `./quartz_metro_node coord sphere` runs a job
  type that didn't exist when the binary was compiled — no rebuild, no
  redeploy of `quartz_metro_node` itself. `deploy.sh` compiles it to
  `jobs/sphere.so` alongside the two main binaries. Drop a new
  `<job_type>.so` in `$QUARTZ_JOBS_DIR` (default `./jobs/`) exporting
  `quartz_job_init()` to add a job type the same way, on an
  already-deployed node.
- **`quartz_ps.py`** — the job registry reader. Every `quartz_metro_node`
  process writes `/tmp/quartz_jobs/<pid>.json` (atomic tmp+rename, wall-clock
  timestamps) at startup and after each round/restart. `quartz_ps.py` reads
  it, self-prunes any file whose pid isn't alive (no cleanup-on-exit handler
  in the writer — same fail-toward-simple pattern as `peer_healthy()`), and
  with `--cluster` ssh's to pi/pi2 to print a live cluster-wide table.
- **`quartz_admit.py`** — admission control. `quartz_admit.py --max N -- <cmd>`
  blocks until fewer than N `quartz_metro_node` processes are registered and
  alive on the host, then `execvp`s `<cmd>` in place (same pid, so the
  registry slot it reserves before exec becomes that job's own first
  registry entry — no race between "admitted" and "actually registered").
  Counts every registered job, including the permanent production ones, not
  just other `quartz_admit`-launched jobs — the budget is host-wide.
- **`quartz_wan_gain.py`** (Mint only) — modulates the Mint WAN egress `tc`
  class between a floor and ceiling rate as a function of the substrate's
  phase. Forces the floor immediately on substrate-down, never freezes at
  the last rate.
- **`quartz_firestick_gain.py`** (pi only) — same shape, but applies one
  global multiplier across all of `shaper-setup.sh`'s per-service Firestick
  classes (Netflix/Disney+/BBC/Sky/etc). Does not retune any class
  individually and does not touch the NFQUEUE packet path.

## reference/ (not deployed by this package)

Pre-existing production code on `pi`, captured here for documentation only —
`deploy.sh` never touches these, and neither gain daemon works without them
already having run:

- **`shaper-setup.sh`** — creates the tc HTB class hierarchy and iptables
  SNI/CDN-IP marking both gain daemons modulate.
- **`quic-sni-gate.py`** — NFQUEUE QUIC-Initial decryption to classify
  traffic by SNI (systemd service `quic-sni-gate.service`).
- **`wave-pacer.py`** — a separate NFQUEUE fairness/pacing layer, own rate
  targets, does not call `tc` (systemd service `wave-pacer.service`).

Sky NOW isn't classified yet — deferred pending live SNI capture while
someone actually plays something in the NOW app (BBC/Prime CDN overlap makes
guessing its IP ranges unsafe).

## deploy/

- **`deploy.sh`** — syncs `daemons/*` to all three hosts and compiles the
  two C binaries locally on each (Mint is x86_64, both Pis are aarch64 —
  binaries are never copied cross-arch, only source).
- **`start.sh`** — starts the substrate + coordinator/workers + both gain
  daemons in their fixed roles. Gain daemons need root (`tc`).
- **`stop.sh`** — kills everything `start.sh` started, on all three hosts.

Known quirk: SSH commands that background a detached child on the Pis
(`... & disown`) reliably hang the *local* ssh client even though the
remote process detaches fine. Check remote state with a fresh `ssh ... ps`
call rather than waiting on the hung one.

## What this replaced

Until 2026-07-29 there was a separate always-on timing swarm plus a
multicast beacon (`quartz_beacon.py`, AxisPulse) doing the phase-coupling
role, and per-service WAN/LAN gain scripts (`ns_wan_gain.py`,
`ns_lan_gain.py`) fed by a dead NucleusState producer. All torn out with
explicit authorization; `quartz_node.c` + the two gain daemons here are the
full replacement, not an addition on top.
