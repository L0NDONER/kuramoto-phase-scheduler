# Kuramoto Phase Scheduler

A self-synchronising phase-lock mesh for home network traffic shaping. Two or more nodes run a repulsive Kuramoto coupling loop over UDP multicast, converge to anti-phase (φ≈π), and coordinate tc burst windows without a shared clock or central controller.

## Benchmark results

All runs on Raspberry Pi ↔ Pi2 over a home LAN, 20 Hz tick rate. No external time reference.

| Condition | Ticks | Locked | mean φ | σ (rad) | Δ from π |
|---|---|---|---|---|---|
| Pi↔Pi2 baseline | 201 | **100%** | 3.1017 | 0.0173 | 1.27% |
| Pi↔Pi2 + Mint (3-node) | 483 | **100%** | 3.1114 | 0.0165 | 0.96% |
| Pi2 under 4-core CPU load | 337 | **100%** | 2.9236 | 0.0285 | 6.94% |
| Post-stress recovery | 231 | **100%** | 3.1023 | 0.0204 | 1.25% |

Key observations:
- Lock never broke across 1252 ticks covering all four conditions
- Adding a third node (Mint) pulled the mean field closer to π and tightened σ
- 4-core CPU saturation shifted the equilibrium point but did not break lock
- Post-stress mean φ returned to within 0.001 rad of baseline — full self-recovery

## Concept

```
Mint ──── repulsive Kuramoto ──── Pi ──── Pi2
              ↑
          Firestick
        (phase-reader at axis)
```

Each node broadcasts its phase at 20 Hz over UDP multicast and applies the Kuramoto coupling step:

```
dθ_i = ω_i - K · Σ sin(θ_i - θ_j) + noise
```

When locked at φ≈π, drain windows on each Pi are coordinated — bursts never collide on the uplink.

## Files

### `beacon.py`

Runs on Mint, Pi, and Pi2. Each node:

1. Broadcasts a phase beacon (`239.0.0.1:7400`) at 20 Hz
2. Reads all remote beacons and applies N-node Kuramoto coupling
3. Converges to anti-phase via adaptive K and frequency tracking (ω tracker gain 0.001)
4. Detects statistical lock (std gate over 20-tick rolling window)
5. Pi only: opens a 500k burst window on `tc class 1:20` at each drain crossing

**Run:**
```bash
python3 beacon.py mint   # Linux Mint desktop
python3 beacon.py pi     # Raspberry Pi shaper (requires root for tc)
python3 beacon.py pi2    # Second Raspberry Pi
```

A PID lock prevents duplicate instances of the same role.

**As a service:**
```bash
sudo systemctl enable --now kuramoto-beacon
```

### `bench.py`

Passive multicast listener for statistical benchmarks. Joins the beacon group without transmitting, records phase samples, and prints mean/std/min/max/lock% at the end.

```bash
python3 bench.py pi2 120   # watch pi2 for 120 seconds
```

### `turtle_sim.py`

Visual simulation. Shows Mint (cyan) and Pi (orange) orbiting anti-phase, Firestick (red) at the axis, phase-diff trace, and drain pulse ring. Switches to live beacon data automatically when beacons are detected on the LAN.

```bash
python3 turtle_sim.py
```

## Architecture

```
beacon.py (Mint, id=0)         beacon.py (Pi, id=1)
  θ₀ ──── UDP mcast 20Hz ────► coupling Σ sin(θ₁ - θⱼ)
  θ₀ ◄─── UDP mcast 20Hz ────  θ₁

  Kuramoto step:
    dθ = ω - K·Σsin(θ - θⱼ) + noise
    K  = 0.12–0.16 adaptive (stronger when far from target)
    ω  = adaptive via frequency tracker (gain 0.001)

  Pi/Pi2:
    drain crossing → ThreadPoolExecutor → tc class change
                                          burst 64k → 500k → 64k (~200ms)

  Pi2 coupling rule:
    Couples to Mint only when Mint is alive.
    Coupling to the anti-phase Mint+Pi pair cancels to zero — avoided by design.
```

## Beacon packet (16 bytes)

```
magic   u16   0x1B4A
sender  u8    0=Mint  1=Pi  2=Pi2
tick    u32   monotonic counter
theta   f32   current phase (0–2π)
omega   f32   natural frequency (rad/tick)
pad     u8
```

## Node IDs and frequencies

| Role | ID | ω (rad/tick) |
|---|---|---|
| mint | 0 | 0.052 |
| pi | 1 | 0.056 |
| pi2 | 2 | 0.054 |

Small asymmetry in ω is required for the Kuramoto attractor to exist. Lock point: `φ_lock = π − arcsin(Δω / 2K)`.

## Graceful degradation

- If a peer drops, its slot times out after 500ms and coupling continues over remaining live peers
- If all peers drop, the node free-runs at its natural ω and rebroadcasts — relock is automatic on reconnect
- PID file at `/tmp/beacon-{role}.pid` prevents accidental duplicate instances

## Third-device integration (Firestick / read-only nodes)

Subscribe to `239.0.0.1:7400`, receive beacons, track the gap:

```python
gap_theta = (theta_mint + theta_pi) / 2
error     = wrap(gap_theta - theta_local)
theta_local += alpha * error   # follow, don't oscillate
# drain when abs(error) < threshold
```

No feedback into the mesh. Phase-reader only.

## Requirements

- Python 3.8+
- stdlib only (`socket`, `struct`, `statistics`, `math`)
- Root on Pi/Pi2 for `tc` calls
- Multicast-capable LAN (`239.0.0.1/8`)
