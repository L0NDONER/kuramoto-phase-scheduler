# Kuramoto Phase Scheduler

A two-oscillator phase-lock scheduler for home network traffic shaping.

## Concept

Two nodes — **Mint** (Linux Mint desktop) and **Pi** (Raspberry Pi bump-in-wire shaper) — run a repulsive Kuramoto coupling loop over UDP multicast. They converge to anti-phase (φ≈π), placing their queue drain windows on opposite sides of the clock. Any third device (Firestick, TV box, additional Pi) sits at the geometric axis between them and drains into the gap — passively, with no negotiation required.

```
Mint ──── repulsive Kuramoto ──── Pi
              ↑
          Firestick
        (phase-reader at axis)
```

When locked at φ=π:
- Mint and Pi queue drains never collide
- The axis is permanently coincident with the gap
- Third devices drain into a gap that comes to them — they never wait

## Files

### `beacon.py`

Runs on Mint and Pi. Each side:

1. Broadcasts a phase beacon (UDP multicast `239.0.0.1:7400`) at 20 Hz
2. Reads the remote beacon and applies repulsive Kuramoto coupling
3. Converges to anti-phase (φ≈π) via adaptive K and frequency tracking
4. Detects statistical lock (variance gate over 20-tick window)
5. On drain crossing, opens a 500k burst window on `tc class 1:20` via threaded subprocess

**Run:**
```bash
python3 beacon.py mint   # on Linux Mint
python3 beacon.py pi     # on Raspberry Pi (requires root for tc)
```

**As a service:**
```bash
sudo systemctl enable --now kuramoto-beacon
```

### `turtle_sim.py`

Visual simulation of the two-oscillator model. Shows:

- Mint (cyan) and Pi (orange) orbiting anti-phase
- Firestick (red) fixed at the axis — the geometric gap
- Phase-diff trace with statistical lock indicator
- Drain pulse ring when gap sweeps the axis

```bash
python3 turtle_sim.py
```

## Architecture

```
beacon.py (Mint)              beacon.py (Pi)
  θ_mint ──── UDP mcast ────► θ_pi
  θ_mint ◄─── UDP mcast ────  θ_pi

  Kuramoto step (repulsive):
    dθ = ω - K·sin(Δθ) + noise
    K  = adaptive (stronger when far from π)
    noise = 0 when statistically locked

  Pi only:
    drain crossing → ThreadPoolExecutor → tc class change eth0 1:20
                                          burst 64k → 500k → 64k
```

## Beacon packet (16 bytes)

```
magic   u16   0x1B4A
sender  u8    0=Mint  1=Pi
tick    u32   monotonic counter
theta   f32   current phase (0–2π)
omega   f32   natural frequency (rad/tick)
pad     u8
```

## Third-device integration (Firestick / additional nodes)

Subscribe to `239.0.0.1:7400`, receive both beacons, compute:

```python
gap_theta = (theta_mint + theta_pi) / 2
error     = wrap(gap_theta - theta_local)
theta_local += alpha * error   # follow, don't oscillate
# drain when abs(error) < threshold
```

No feedback into the Mint–Pi loop. Phase-reader only.

## Requirements

- Python 3.8+
- `python3-statistics` (stdlib)
- Root on Pi for `tc` calls
- Multicast-capable LAN (`239.0.0.1/8`)
