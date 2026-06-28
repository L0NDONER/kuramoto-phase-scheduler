# Kuramoto Substrate

A self-synchronising oscillator pair used as a timing substrate for a three-tier cerebellar learning stack, a closed-loop thermal regulator, and a phase-space intent signalling layer.

Two Raspberry Pis run repulsive Kuramoto coupling over UDP multicast, converge to anti-phase (φ≈π), and distribute stable timing to downstream consumers. No shared clock. No central controller.

**Role boundaries:**
- **Pi2** — substrate host: both Kuramoto oscillators, axis reader, and both trineuron triangles run here. Self-contained; no cross-host multicast dependencies in the oscillator layer.
- **Pi1** — pure actuator: receives intent via NucleusState and executes `tc HTB` ceiling changes on eth0. Does not originate timing, does not participate in oscillator semantics, does not host substrate processes.
- **Mint** — transport + WAN actuation: nazare.py stages CortexPulse to cerebellum and relays DCN/HOLD to Pi2; ns_wan_gain.py modulates WAN egress rate.
- **EC2** — cerebellum: slow integrator, emits pred_err or authoritative HOLD (0x484F).

---

## Findings

### 1. DCN eliminates thermal variance

A deep cerebellar nucleus (DCN) correction signal was applied to an LMDE host running a controlled 30% CPU load. Ten episodes per regime, matched conditions.

| Regime | temp μ | temp σ² |
|---|---|---|
| DCN=OFF | 48.05°C | 0.051 |
| DCN=ON  | 48.00°C | 0.000 |

Mean is unchanged. Variance is eliminated. The DCN loop does not regulate the mean — it damps pathway surges, producing homeostatic temperature stability. Phase deviation (pd_dev) was indistinguishable between regimes, confirming the correction acts on the thermal pathway, not the oscillator.

### 2. Signal vs actuation separation

The cerebellum sends timing only. It does not inject energy into the actuator. Proved by a null experiment on Pi2 at 30% load: DCN corrections arrived, were received, and produced no thermal effect — because at 30% load there was no actuatable lever. The signal is present; without a plant to act on, nothing moves.

This mirrors the biological distinction between a nerve spike (timing carrier) and the current that stiffens a muscle (energy from the plant). The cerebellum modulates gain. The actuator energy comes from the load itself.

### 3. Stable lock under perturbation

| Condition | Ticks | Locked | mean φ | σ (rad) |
|---|---|---|---|---|
| Pi↔Pi2 baseline | 201 | 100% | 3.1017 | 0.0173 |
| Pi↔Pi2 + Mint (3-node coupled) | 483 | 100% | 3.1114 | 0.0165 |
| Pi2 under 4-core CPU load | 337 | 100% | 2.9236 | 0.0285 |
| Post-stress recovery | 231 | 100% | 3.1023 | 0.0204 |

Lock never broke. 4-core saturation shifted the equilibrium but did not break it. Post-stress mean φ returned to within 0.001 rad of baseline — full self-recovery.

### 4. Temlum: slow integrator on a fast substrate

A thermal error integrator (temlum) running on the Pi2 reader produces stable topology decisions from a noisy control signal:

```
temlum = 0.95·temlum_prev + 0.05·(T − T_target)
e_C    = 1.0·(pred_err − 0.0045) − 0.15·temlum
```

The AxisPulse substrate (~40Hz) paces the temlum update. The cerebellar pred_err arrives at ~1Hz. The actuator only commits a topology change after a 15-second sustained-majority vote. Three timescales — substrate, signal, actuation — are cleanly separated, preventing chatter without losing responsiveness.

### 5. Complementary homeostasis in a two-neuron system

Two substrate neurons sharing a timing and error substrate reduce thermal variance by 46% relative to uncontrolled baseline. Topology control reduces heat generation; DVFS control accelerates thermal recovery. The two mechanisms are orthogonal and non-redundant.

| Regime | temp μ | temp σ² | peak | cool_s | commits |
|---|---|---|---|---|---|
| Baseline (no neurons) | 82.8°C | 6.31 | 86.2°C | 403s | — |
| DVFS only | 83.4°C | 5.28 | 85.7°C | 310s | 3 freq steps |
| Topology only | 83.0°C | 3.80 | 86.2°C | 332s | 3 core steps |
| Both neurons | 83.0°C | **3.42** | **85.2°C** | 326s | 2+1 steps |

Topology alone accounts for 40% variance reduction; DVFS alone 16%; together 46%. The residual gain from combining them is modest in variance terms but the mechanisms target different phases: topology suppresses the thermal rise, DVFS shortens the recovery tail. Neither neuron is redundant.

### 6. Glyph intent signals — pre-semantic state transitions via phase injection

An intent signal is not a packet. It is a phase-space event: a deformation of the oscillator field that every neuron feels simultaneously, with no routing and no decoding.

The axis node (`reader_glyph.c`) injects a spoofed Pi1 beacon with `θ1 + Δ` during an intent window. Pi2's Kuramoto coupling responds to the perturbed phase. Every downstream consumer sees the real `pd_dev` excursion in AxisPulse — the signal propagates through actual coupling dynamics.

Four intent types, distinguished by amplitude, duration, and sign:

| Intent | Δ (rad) | Duration | pd_dev | Direction | Effect |
|---|---|---|---|---|---|
| ADVISORY | +0.30 | 1 unit (~100ms) | ~0.03 | PARK | Soft bias |
| DIRECTIVE | +0.50 | 3 units (~300ms) | ~0.05 | PARK | Sustained push |
| ALARM | +1.00 | 2 ticks (~25ms) | **1.035** | PARK | Attractor collapse, self-re-lock |
| BOOST | −0.40 | 3 units (~300ms) | ~0.08 | UNPARK | Negative delta, pd below π |

Sign matters. Positive Δ: Pi2 coupling drives θ2 up → pd above π → pd_signed positive → e_C decreases (PARK). Negative Δ: Pi2 coupling drives θ2 down → pd below π → pd_signed negative → e_C increases (UNPARK).

BOOST amplitude is bounded by the nazare DRIFT_THRESH (0.25). At −0.40 rad, pd_dev ≈ 0.08 — well within threshold and the DCN pathway stays live.

### 7. Glyph v1 calibration — boundary behaviour under DCN + BOOST

With `W_PD = 0.080`, a live temperature sweep hovered Pi2 near T_target (84°C) with DCN active.

At the boundary (|temlum| < 0.1, T within ~0.5°C of target):

```
temlum=+0.019  pd_s=−0.014  e_C=−0.011  → PARK     (no glyph)
temlum=+0.019  pd_s=−0.080  e_C=+0.002  → HOLD     (BOOST active)
```

BOOST shifted e_C by +0.013, crossing E_PARK (−0.003) into HOLD. BOOST is a modulator, not a command. Once temlum > 0.1, thermal dominates and BOOST is inert.

**v1 stable parameters:**

| Parameter | Value | Notes |
|---|---|---|
| W_PD | 0.080 | 10× original; glyph signal lands at intent thresholds |
| BOOST Δ | −0.40 rad | pd_dev ≈ 0.08; DRIFT_THRESH safe margin |
| BOOST duration | 3 units (~300ms) | Sufficient dwell for DCN tick to sample |
| Active band | ±0.5°C of T_target | Outside this, thermal term dominates |

### 8. Thermal field entrainment

The Pi2 CPU thermal field is entrained by the Kuramoto carrier. Observed live via `ns_lan_gain.log` (3299 ticks, ~55 minutes): temlum traces a clean sinusoid whose frequency is set by the oscillator cycle through the actuator, not by random load variation. PARK/UNPARK decisions track the zero crossings.

The ns_lan_gain asymmetric EMA (attack α=0.05, release α=0.30) shapes the waveform independently of its frequency. Fast release means the system takes thermal headroom quickly on cooling; slow attack means brief warming events don't trigger PARK. The result is a structural bias toward UNPARK — the system leans into headroom aggressively and backs off cautiously. This is a thermal ratchet, not symmetric hysteresis.

The two effects are separable: frequency from the Kuramoto carrier, waveform asymmetry from the EMA. Both are visible simultaneously in the live chart.

### 9. HOLD as a first-class mode

HOLD is not a failure state — it is active suppression of commitment under uncertainty. The architecture distinguishes three responses to uncertainty:

- **PARK / UNPARK** — directional commit
- **HOLD** — pause, assess, do not commit

When the SSH tunnel goes dark, the danger is not absent data — it is stale data. The cerebellum continuing to emit corrections based on its last-known EMA state is the hazard.

**Implementation:**

- Cerebellum (`cerebellum_ec2.py`) owns the authoritative HOLD signal (`0x484F`, 2 bytes). Emitted when:
  - CortexPulse has been absent > 5 seconds on an open connection (`settimeout`)
  - First `DCN_INTERVAL` events after reconnect (EMA not yet valid)
  - EMA state is reset on every new connection — stale values never cross connection boundaries
- Nazare (`nazare.py`) recognises `0x484F` and relays it to both Pi2 neurons (ports 7430, 7432)
- Pi2 reader (`pi2_reader.c`) recognises `0x484F` on port 7430 and emits HOLD intent + NucleusState with `intent=HOLD`
- Downstream gain daemons (`ns_lan_gain.py`, `ns_wan_gain.py`) use local staleness on NucleusState as their own fallback

The cerebellum's HOLD is the shared semantic truth. Local staleness detection at each layer is the safety rail for when the HOLD signal itself cannot get through.

---

## Architecture

```
Pi2 (beacon.c sid=1) ─── loopback multicast 239.0.0.1:7400 ─── Pi2 (beacon.c sid=2)
                    ↓
           Pi2 reader.c  (axis node — local to Pi2)
           AxisPulse → 239.0.0.2:7404  (~40Hz, locked=1 when Δφ≈π)
                    ↓
      ┌────────────────────────────────────┐
      │  Pi2 trineuron substrate           │
      │  pi2_reader.c (Triangle A)         │
      │    NucleusState-A → 239.0.0.3:7440 │◄──┐ cross-field W_CROSS=0.05
      │  pi2_dvfs_reader.c (Triangle B)    │   │
      │    NucleusState-B → 239.0.0.4:7441 │───┘
      └────────────────────────────────────┘
                    ↓ (via Mint nazare.py)
           Mint nazare.py  (transport + staging)
           CortexPulse → EC2 cerebellum  :7420 TCP  (α=0.005, ~289 events)
                    ↑
           DCN pred_err / HOLD ← EC2 cerebellum (reverse SSH tunnel :7421)
                    ↓ UDP relay :7430/:7432
           Pi2 pi2_reader.c / pi2_dvfs_reader.c
           intent → 127.0.0.1:7431/7433
                    ↓                              ↓
           Pi2 pi2_actuator.py          Pi1 ns_lan_gain.py  ← NucleusState :7440
           (cgroup cpuset, 15s dwell)   (Firestick tc HTB ceil — pure actuator)
                                                   ↓
                                        Mint ns_wan_gain.py
                                        (WAN egress HTB rate)
```

**Timescale separation:**
- Substrate tick: 25ms (40Hz AxisPulse)
- Cerebellar observation: ~1Hz (every 20 events)
- Topology commit: 15s minimum dwell

**Signal flow — read-only boundary:**
Cerebellum is a pure observer. It sends `pred_err_ema` (raw prediction error) or `HOLD` — no setpoint, no direct actuation. The pi2_reader owns all control logic.

---

## Port map

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 7400 | UDP multicast 239.0.0.1 | Pi1↔Pi2 | Beacon (oscillators) |
| 7403 | UDP loopback | reader→phase_sched | WanPulse (per-tick) |
| 7404 | UDP multicast 239.0.0.2 | reader→consumers | AxisPulse (locked timing) |
| 7405 | UDP | consumers→reader | LoadFeedback |
| 7408 | UDP loopback | glyph_intent→reader_glyph | Intent pulse (ADVISORY/DIRECTIVE/ALARM) |
| 7420 | TCP (SSH tunnel) | nazare→EC2 | CortexPulse → cerebellum |
| 7421 | TCP (SSH tunnel) | EC2→Mint | DCN pred_err / HOLD |
| 7430 | UDP | nazare→Pi2 | DCN relay (cpuset neuron) |
| 7431 | UDP loopback | pi2_reader→pi2_actuator | Intent (PARK/UNPARK/HOLD) — cpuset |
| 7432 | UDP | nazare→Pi2 | DCN relay (dvfs neuron) |
| 7433 | UDP loopback | pi2_dvfs_reader→pi2_dvfs_actuator | Intent (PARK/UNPARK/HOLD) — cpufreq |
| 7440 | UDP multicast 239.0.0.3 | Pi2→LAN | NucleusState-A Triangle A (e_C, temlum, pd_pop, intent) |
| 7441 | UDP multicast 239.0.0.4 | Pi2→LAN | NucleusState-B Triangle B (e_C, temlum, pd_pop, intent) |

---

## Files

### Glyph intent layer (`glyph/`)

| File | Role |
|---|---|
| `glyph/reader_glyph.c` | Fork of reader.c; injects `θ1+Δ` to beacon multicast during intent window |
| `glyph/glyph_intent.py` | Fires typed intent pulses: `advisory \| directive \| alarm` |
| `glyph/glyph_tx.py` | Glyph radio transmitter (base-4 rate-coded text over UDP) |
| `glyph/glyph_rx.py` | Glyph radio receiver |

Run `reader_glyph` **instead of** `reader.c` during glyph sessions.

```bash
gcc -O2 -o glyph/reader_glyph glyph/reader_glyph.c -lm
sudo ./glyph/reader_glyph

python3 glyph/glyph_intent.py advisory
python3 glyph/glyph_intent.py directive
python3 glyph/glyph_intent.py alarm
```

### Substrate (Mint + Pi)

| File | Runs on | Role |
|---|---|---|
| `beacon.c` | Pi2 (×2) | Kuramoto oscillators sid=1 and sid=2, both on Pi2 |
| `reader.c` | Pi2 | Axis node, distributes AxisPulse locally |
| `cpu_reader.c` | Mint | DVFS consumer (cpufreq + MSR voltage) |
| `tc_shaper.c` | Mint | WAN egress rate modulator |
| `entropy_reader.c` | Mint | Phase → /dev/urandom entropy injection |
| `phase_sched.c` | Mint | Thundering herd suppressor |
| `wan_receiver.c` | Mint | WanPulse decoder |

### Cerebellar stack

| File | Runs on | Role |
|---|---|---|
| `nazare.py` | Mint | Transport + staging layer; relays DCN and HOLD to Pi2 |
| `pi2/cerebellum_ec2.py` | EC2 | Deep slow integrator (α=0.005); emits HOLD on stale input |

### Pi2 thermal regulator (`pi2/`)

| File | Runs on | Role |
|---|---|---|
| `pi2_reader.c` | Pi2 | Tricast pd nucleus + temlum controller; emits NucleusState :7440 |
| `pi2_actuator.py` | Pi2 | cgroup cpuset actuator, 15s dwell gate |
| `pi2_dvfs_reader.c` | Pi2 | Identical neuron, intent → cpufreq |
| `pi2_dvfs_actuator.py` | Pi2 | cpufreq actuator, 15s dwell gate |

### NucleusState consumers

| File | Runs on | Role |
|---|---|---|
| `pi2/ns_lan_gain.py` | Pi1 | Modulates Firestick tc HTB ceil from NucleusState :7440 |
| `ns_wan_gain.py` | Mint | Modulates WAN egress HTB rate from AxisPulse + NucleusState |

### Tooling

| File | Role |
|---|---|
| `live_plot.py` | Live matplotlib chart of temlum + e_C. Run: `ssh pi "tail -n 120 -f /tmp/ns_lan_gain.log" \| python3 live_plot.py` |

---

## Reproducing the Pi2 thermal regulator

**Prerequisites:** SSH tunnel up, cerebellum running on EC2, nazare running on Mint.

```bash
# 1. SSH tunnel (Mint)
ssh -fNL 7420:localhost:7420 -R 7421:localhost:7421 -o ExitOnForwardFailure=no aws

# 2. Cerebellum (EC2)
nohup python3 ~/cerebellum_ec2.py > ~/cerebellum.log 2>&1 &

# 3. Nazare (Mint)
nohup python3 -u ~/claude/nazare.py > /tmp/nazare.log 2>&1 &

# 4. Build and deploy reader to Pi2
scp pi2/pi2_reader.c pi2:~
ssh pi2 "gcc -O2 -o ~/pi2_reader ~/pi2_reader.c -lm"

# 5. Start on Pi2
ssh pi2 "sudo sh -c '/home/pi/pi2_reader > /home/pi/pi2_reader.log 2>&1 &'"
ssh pi2 "sudo sh -c 'python3 /home/pi/pi2_actuator.py > /home/pi/pi2_actuator.log 2>&1 &'"

# 6. NucleusState consumers
ssh pi "sudo python3 ~/ns_lan_gain.py > /tmp/ns_lan_gain.log 2>&1 &"
sudo python3 ~/claude/ns_wan_gain.py > /tmp/ns_wan_gain.log 2>&1 &
```

**Watch it:**
```bash
ssh pi2 "tail -f /home/pi/pi2_actuator.log"   # topology commits
ssh pi2 "tail -f /home/pi/pi2_reader.log"      # e_C + intent per DCN tick
ssh pi "tail -f /tmp/ns_lan_gain.log"          # LAN gain + temlum live
ssh pi "tail -n 120 -f /tmp/ns_lan_gain.log" | python3 ~/claude/live_plot.py  # live chart
```

**Tunnel staleness:** when the tunnel drops, the cerebellum emits `HOLD` (0x484F) on reconnect and during the EMA warmup window. Consumers hold their last committed state until corrections resume. If the tunnel dies completely, each layer detects its own staleness and enters HOLD independently. Restart the tunnel; cerebellum reconnects automatically within ~10s.

---

## Build

```bash
make          # builds all C binaries
make beacon   # individual target
```

Cross-compiled ARM binaries (`reader_arm`, `phase_sched_arm`, `wan_receiver_arm`) are built separately for deployment to Pi.
