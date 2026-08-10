# gpu_layer0 — Layer 0 (GPU) prototype for the fractal control hierarchy

Layer 0 of the source→transit→storage→datacenter→GPU control fractal
sketched alongside `grid_twin/` (see memory `project_grid_twin` and
`project_kuramoto_engineering_pitfalls`). Layer 2/3 (datacenter/grid)
already exist and are validated against real Elexon data; this is the
opposite end — real GPU telemetry, no synthetic data.

**Honest scope, on a Kaggle session (1-2 T4/P100 GPUs):** this proves the
*mechanics* work against real hardware telemetry — sampling, phase
computation, coupling, order-parameter reporting. It does **not**
demonstrate real swarm consensus: the order parameter `r` is only a
meaningful coherence statistic at real cluster scale (tens+ of
oscillators). At N=1-2 it will read close to 1 or be noisy/undefined
almost by construction — don't read anything into that number here.
The point of running this is validating the wiring, not the swarm
behaviour itself.

## Files
- `pynvml_telemetry.py` — fast in-process GPU sampling (power draw,
  temperature, SM clock, utilization) via `pynvml`. Deliberately not
  shelling out to the `nvidia-smi` CLI: that subprocess is slow
  (~50-100ms per call) relative to this layer's carrier period, which
  would alias the phase integration exactly like the RK4-dt-vs-carrier-
  period pitfall already documented for grid_twin's engine.
- `layer0_oscillator.py` — per-GPU phase, RK4-integrated at dt well
  below the carrier period, coupled toward a target via a **gain**
  modulated by telemetry deviation (not a constant omega bias — see
  pitfall #1: a fixed omega offset is a permanent one-directional
  drift, not a proportional response).
- `run_kaggle.py` — ties the two together in a runnable loop. Copy this
  file's contents into a Kaggle notebook cell (or upload as a dataset
  file and `import`), after `pip install pynvml` as the first cell.

## Note on telemetry cadence
`pynvml` calls themselves are fast, but the underlying power/thermal
sensor on the GPU doesn't necessarily update that fast internally — a
few reads in a tight loop may return the same held value. That's
expected (same zero-order-hold situation as `grid_twin`'s ambient
temperature changing slowly while the sim steps faster) — the phase
integration is allowed to run faster than the signal it's tracking.
