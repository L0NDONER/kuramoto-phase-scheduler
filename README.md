# Kuramoto Phase Scheduler

A self-synchronising phase-lock mesh for home network traffic shaping. Two nodes run a repulsive Kuramoto coupling loop over UDP multicast, converge to anti-phase (φ≈π), and coordinate tc burst windows without a shared clock or central controller. Additional nodes join as phase-readers — they time their own WAN traffic from the oscillator pair without perturbing the lock.

## Benchmark results

All runs on Raspberry Pi ↔ Pi2 over a home LAN, 20 Hz tick rate. No external time reference.

| Condition | Ticks | Locked | mean φ | σ (rad) | Δ from π |
|---|---|---|---|---|---|
| Pi↔Pi2 baseline | 201 | **100%** | 3.1017 | 0.0173 | 1.27% |
| Pi↔Pi2 + Mint (3-node coupled) | 483 | **100%** | 3.1114 | 0.0165 | 0.96% |
| Pi2 under 4-core CPU load | 337 | **100%** | 2.9236 | 0.0285 | 6.94% |
| Post-stress recovery | 231 | **100%** | 3.1023 | 0.0204 | 1.25% |
| Pi↔Pi2 + Mint as reader | 20 | **100%** | 3.130 | 0.009 | 0.36% |

Key observations:
- Lock never broke across all conditions including Mint joining/leaving as a reader
- Mint dropout and rejoin has zero effect on the Pi↔Pi2 lock
- Reader topology (Mint passive) is tighter than 3-node coupled — no third oscillator perturbing equilibrium
- 4-core CPU saturation shifted the equilibrium point but did not break lock
- Post-stress mean φ returned to within 0.001 rad of baseline — full self-recovery

## Concept

```
Pi ──── repulsive Kuramoto ──── Pi2
         (oscillators)

Mint: reads Pi+Pi2 phase → times own tc drain → never transmits
Firestick: reads Pi+Pi2 phase → phase-reader at axis
```

Pi and Pi2 own the lock. Mint and Firestick are consumers of that ground truth. Readers can be added or removed without touching the oscillator pair.

Each oscillator broadcasts its phase at 20 Hz over UDP multicast and applies the Kuramoto coupling step:

```
dθ_i = ω_i - K · Σ sin(θ_i - θ_j) + noise
```

When locked at φ≈π, drain windows on each node are coordinated — bursts never collide on the uplink.

## Files

### `beacon.py`

Runs on Pi and Pi2 (oscillators). Each node:

1. Broadcasts a phase beacon (`239.0.0.1:7400`) at 20 Hz
2. Reads remote beacons and applies repulsive Kuramoto coupling
3. Converges to anti-phase via adaptive K and jitter-gated frequency tracker (ω gain 0.001)
4. Detects statistical lock (std gate over 20-tick rolling window)
5. Pi only: opens a 500k burst window on `tc class 1:20` at each drain crossing

Pi2 coupling rule: couples to Mint only when Mint is alive. Coupling to the anti-phase Mint+Pi pair cancels to zero — avoided by design.

**Run:**
```bash
python3 beacon.py pi     # Raspberry Pi shaper (requires root for tc)
python3 beacon.py pi2    # Second Raspberry Pi
```

**As a service:**
```bash
sudo systemctl enable --now kuramoto-beacon
```

### `reader.py`

Runs on Mint (phase-reader). Never transmits a beacon. Never applies coupling. Purely observes Pi↔Pi2 and times Mint's own WAN traffic from their phase.

1. Joins multicast group, receives Pi(1) + Pi2(2) beacons only
2. Tracks Pi↔Pi2 phase gap
3. Detects drain crossing (phase_diff descends through PHASE_TARGET)
4. Opens 500k burst window on local HTB class 1:20 for 200ms
5. No feedback into the mesh — Pi↔Pi2 lock is undisturbed

**HTB setup required on Mint's interface before first run:**
```bash
sudo tc qdisc replace dev enp0s31f6 root handle 1: htb default 20
sudo tc class add dev enp0s31f6 parent 1: classid 1:20 htb rate 5mbit ceil 5mbit burst 64k
sudo tc qdisc add dev enp0s31f6 parent 1:20 handle 20: sfq perturb 10
```

**As a service:**
```bash
sudo systemctl enable --now kuramoto-beacon   # ExecStart points to reader.py
```

### `bench.py`

Passive multicast listener for statistical benchmarks. Joins the beacon group without transmitting, records phase samples, and prints mean/std/min/max/lock% at the end.

```bash
python3 bench.py pi2 120   # watch pi2 for 120 seconds
```

### `turtle_sim.py`

Visual simulation. Shows Mint (cyan) and Pi (orange) orbiting anti-phase, Firestick (red) at the axis, phase-diff trace, and drain pulse ring. Switches to live beacon data automatically when beacons are detected on the LAN. Shows `● LIVE`, `◑ LIVE/SIM`, or `○ SIM` mode indicator.

```bash
python3 turtle_sim.py
```

## Architecture

```
beacon.py (Pi, id=1)            beacon.py (Pi2, id=2)
  θ₁ ──── UDP mcast 20Hz ─────► coupling Σ sin(θ₂ - θⱼ)
  θ₁ ◄──── UDP mcast 20Hz ────  θ₂

  Kuramoto step:
    dθ = ω - K·Σsin(θ - θⱼ) + noise
    K  = 0.12–0.16 adaptive (stronger when far from target)
    ω  = adaptive via jitter-gated frequency tracker

  Pi/Pi2:
    drain crossing → ThreadPoolExecutor → tc class change
                                          burst 64k → 500k → 64k (~200ms)

reader.py (Mint)
  Receives Pi + Pi2 beacons — no transmit
  phase_diff = |θ₁ - θ₂| wrapped to [0, π]
  drain crossing → tc class change on enp0s31f6
                   burst 64k → 500k → 64k (~200ms)
```

## Beacon packet (16 bytes)

```
magic   u16   0x1B4A
sender  u8    1=Pi  2=Pi2
tick    u32   monotonic counter
theta   f32   current phase (0–2π)
omega   f32   natural frequency (rad/tick)
pad     u8
```

## Node IDs and frequencies

| Role | ID | ω (rad/tick) | Mode |
|---|---|---|---|
| pi | 1 | 0.056 | oscillator |
| pi2 | 2 | 0.052 | oscillator |
| mint | — | — | reader |

Small asymmetry in ω is required for the Kuramoto attractor to exist. Lock point: `φ_lock = π − arcsin(Δω / 2K)`.

## Graceful degradation

- If a peer drops, its slot times out after 500ms and coupling continues over remaining live peers
- If all peers drop, the node free-runs at its natural ω and rebroadcasts — relock is automatic on reconnect
- Mint (reader) joining or leaving has zero effect on Pi↔Pi2 lock
- PID file at `/tmp/beacon-{role}.pid` prevents accidental duplicate instances

## Jitter gate

The ω tracker rejects frequency updates when the peer's wall-clock beacon interval deviates >30% from the expected 50ms. This prevents load-induced tick jitter from corrupting the natural frequency estimate and drifting the equilibrium away from π.

## Third-device integration (Firestick / additional readers)

Subscribe to `239.0.0.1:7400`, receive Pi+Pi2 beacons, track the gap:

```python
gap_theta = (theta_pi + theta_pi2) / 2
error     = wrap(gap_theta - theta_local)
theta_local += alpha * error   # follow, don't oscillate
# drain when abs(error) < threshold
```

No feedback into the mesh. Phase-reader only.

## Requirements

- Python 3.8+
- stdlib only (`socket`, `struct`, `statistics`, `math`)
- Root on Pi/Pi2/Mint for `tc` calls
- Multicast-capable LAN (`239.0.0.1/8`)
