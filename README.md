# Kuramoto Substrate

A self-synchronising oscillator pair used as a timing substrate for a three-tier cerebellar learning stack and a closed-loop thermal regulator.

Two Raspberry Pis run repulsive Kuramoto coupling over UDP multicast, converge to anti-phase (φ≈π), and distribute stable timing to downstream consumers via a Mint axis node. No shared clock. No central controller.

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

### 5. Complementary homeostasis in a two-neuron system

Two substrate neurons sharing a timing and error substrate reduce thermal variance by 46% relative to uncontrolled baseline. Topology control reduces heat generation; DVFS control accelerates thermal recovery. The two mechanisms are orthogonal and non-redundant, demonstrating complementary homeostatic behaviour in a minimal artificial nervous system.

| Regime | temp μ | temp σ² | peak | cool_s | commits |
|---|---|---|---|---|---|
| Baseline (no neurons) | 82.8°C | 6.31 | 86.2°C | 403s | — |
| DVFS only | 83.4°C | 5.28 | 85.7°C | 310s | 3 freq steps |
| Topology only | 83.0°C | 3.80 | 86.2°C | 332s | 3 core steps |
| Both neurons | 83.0°C | **3.42** | **85.2°C** | 326s | 2+1 steps |

Topology alone accounts for 40% variance reduction; DVFS alone 16%; together 46%. The residual gain from combining them is modest in variance terms but the mechanisms target different phases: topology suppresses the thermal rise, DVFS shortens the recovery tail. Neither neuron is redundant.

### 4. Temlum: slow integrator on a fast substrate

A thermal error integrator (temlum) running on the Pi2 reader produces stable topology decisions from a noisy control signal:

```
temlum = 0.95·temlum_prev + 0.05·(T − T_target)
e_C    = 1.0·(pred_err − 0.0045) − 0.15·temlum
```

The AxisPulse substrate (~40Hz) paces the temlum update. The cerebellar pred_err arrives at ~1Hz. The actuator only commits a topology change after a 15-second sustained-majority vote. Three timescales — substrate, signal, actuation — are cleanly separated, preventing chatter without losing responsiveness.

---

## Architecture

```
Pi1 (beacon.c, sid=1)          Pi2 (beacon.c, sid=2)
  └──── UDP multicast 239.0.0.1:7400 ────┘
                    ↓
           Mint reader.c  (axis node)
           AxisPulse → 239.0.0.2:7404  (~40Hz, locked=1 when Δφ≈π)
                    ↓
           Mint nazare.py  (transport + staging)
           CortexPulse → LMDE cortex.py   :7410 UDP  (α=0.02, ~50 events)
                       → EC2  cerebellum  :7420 TCP  (α=0.005, ~289 events)
                    ↑
           DCN pred_err_ema ← EC2 cerebellum (reverse SSH tunnel :7421)
                    ↓ UDP relay
           Pi2 pi2_reader.c  (temlum controller)
           intent → 127.0.0.1:7431
                    ↓
           Pi2 pi2_actuator.py  (cgroup cpuset + stress-ng)
```

**Timescale separation:**
- Substrate tick: 25ms (40Hz AxisPulse)
- Cerebellar observation: ~1Hz (every 20 events)
- Topology commit: 15s minimum dwell

**Signal flow — read-only boundary:**
Cerebellum is a pure observer. It sends `pred_err_ema` (raw prediction error) — no correction, no setpoint. The pi2_reader owns all control logic. The cerebellum cannot actuate anything directly.

---

## Port map

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 7400 | UDP multicast 239.0.0.1 | Pi1↔Pi2 | Beacon (oscillators) |
| 7403 | UDP loopback | reader→phase_sched | WanPulse (per-tick) |
| 7404 | UDP multicast 239.0.0.2 | reader→consumers | AxisPulse (locked timing) |
| 7405 | UDP | consumers→reader | LoadFeedback |
| 7410 | UDP | nazare→LMDE | CortexPulse → cortex.py |
| 7420 | TCP (SSH tunnel) | nazare→EC2 | CortexPulse → cerebellum |
| 7421 | TCP (SSH tunnel) | EC2→Mint | DCN pred_err_ema |
| 7430 | UDP | nazare→Pi2 | DCN relay |
| 7431 | UDP loopback | pi2_reader→pi2_actuator | Intent (PARK/UNPARK/HOLD) — cpuset |
| 7433 | UDP loopback | pi2_dvfs_reader→pi2_dvfs_actuator | Intent (PARK/UNPARK/HOLD) — cpufreq |

---

## Files

### Substrate (Mint + Pi)

| File | Runs on | Role |
|---|---|---|
| `beacon.c` | Pi1, Pi2 | Kuramoto oscillator, tc drain |
| `reader.c` | Mint | Axis node, distributes AxisPulse |
| `cpu_reader.c` | Mint | DVFS consumer (cpufreq + MSR voltage) |
| `tc_shaper.c` | Mint | WAN egress rate modulator |
| `tm1_reader.c` | LMDE | TM1 clock duty-cycle consumer |
| `entropy_reader.c` | Mint | Phase → /dev/urandom entropy injection |
| `phase_sched.c` | Mint | Thundering herd suppressor |
| `wan_receiver.c` | Mint | WanPulse decoder |

### Cerebellar stack

| File | Runs on | Role |
|---|---|---|
| `nazare.py` | Mint | Transport + staging layer |
| `cortex.py` | LMDE | Fast EMA integrator (α=0.02) |
| `pi2/cerebellum_ec2.py` | EC2 | Deep slow integrator (α=0.005), pure observer |

### Pi2 thermal regulator (`pi2/`)

| File | Runs on | Role |
|---|---|---|
| `pi2_reader.c` | Pi2 | Sensor + temlum controller + intent emitter → cpuset |
| `pi2_actuator.py` | Pi2 | cgroup cpuset actuator, 15s dwell gate |
| `pi2_dvfs_reader.c` | Pi2 | Identical neuron, intent → cpufreq |
| `pi2_dvfs_actuator.py` | Pi2 | cpufreq actuator, 15s dwell gate |
| `nazare.py` | (copy) | Transport reference |
| `cerebellum_ec2.py` | (copy) | Observer reference |

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
```

**Watch it:**
```bash
ssh pi2 "tail -f /home/pi/pi2_actuator.log"   # topology commits
ssh pi2 "tail -f /home/pi/pi2_reader.log"      # e_C + intent per DCN tick
```

**Tunnel keep-alive note:** the SSH tunnel drops silently. If the reader log goes quiet, restart the tunnel and kill/restart cerebellum so it reconnects.

---

## Build

```bash
make          # builds all C binaries
make beacon   # individual target
```

Cross-compiled ARM binaries (`reader_arm`, `phase_sched_arm`, `wan_receiver_arm`) are built separately for deployment to Pi.
