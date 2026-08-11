# Melanoma cellstate walker — session log (2026-07-30)

Metropolis annealer walking a static energy landscape built from the Tirosh
et al. 2016 melanoma scRNA-seq dataset (GEO GSE72056, 4645 cells x 23686
genes, PCA-reduced + KDE'd into a 40x40x40 grid). Standalone from the
BTC/ETH swarm — prints positions only, no actuation.

## Architecture

- `export_landscape_bin.py` — builds `cellstate_landscape.bin`: header
  (nx,ny,nz int32) + 3 axis arrays (float32) + energy grid (float32) +
  appended `n_cells` (int32) + cell coords (float32 x3/cell). The cell
  block is a backward-compatible append — older readers that only read
  header+axes+grid never see it.
- `quartz_cellstate_node.c` / `_b.c` — the two long-running production
  walkers. A starts at grid center, B starts at the opposite corner.
  Metropolis, T0/COOL/reheat pattern, trilinear grid interpolation,
  quadratic boundary-wall penalty (`K_WALL`) outside the axis extents so
  the walker can't wander into flat, zero-gradient space off the grid edge.
- `quartz_cellstate_multistart.c` — batch tool: takes a start position +
  round count (+ optional T0, `--force`), runs to completion, prints one
  final line. Used for basin surveys and kinetic-trap testing. Has:
  - **Data-support guard**: refuses to start (unless `--force`) more than
    `MIN_CELL_DIST` (50 units) from every real cell — see kinetic trap
    below.
  - **E_CAP** (100.0): caps the raw interpolated grid value before the
    boundary penalty, to bound numerical blowup in KDE-extrapolated
    (empty) regions.
  - Reports `nearest_cell` distance and a `data-supported` /
    `extrapolated` tag on every result line.
- `quartz_cellstate_swarm.c` — fixed pool of N walkers sharing one voxel
  occupancy grid, with collapse+respawn:
  - **Collapse** ("novelty stall"): a walker that hasn't landed in a new
    voxel for `STALL_LIMIT` rounds (default 30) is marked dead.
  - **Respawn**: dead walker restarts at a voxel chosen uniformly from the
    currently-lowest-occupancy cells, restricted to data-supported voxels
    (reuses the same `MIN_CELL_DIST` mask, precomputed once at startup —
    naive per-respawn recompute would be O(voxels x cells) ~300M ops/call).

## Experiment 1 — T0=50 vs T0=10 (basin tightening)

Lowering the Metropolis start temperature from 50 to 10 (cooling to ~11
either way by round end, since T resets to T0 every 300-step round).

Matched-sample voxel-overlap check (same n for both, using `histogramdd`
over a 25-bin grid spanning both trajectories + the real cell embedding):

| n       | T0=50 voxels A/B | Jaccard | T0=10 voxels A/B | Jaccard |
|---------|------------------|---------|-------------------|---------|
| 5,623   | 1963 / 1853       | 0.363   | 1082 / 1068        | 0.539   |
| 12,642  | 2785 / 2752       | 0.519   | 1311 / 1292        | 0.593   |

Consistent across sample sizes: T0=10 visits ~45-53% fewer voxels per
walker than T0=50, and A/B overlap is *higher* at T0=10 despite starting
at opposite ends of the space — basins tighten and both walkers converge
onto the same attractor more reliably at lower temperature.

## Experiment 2 — 10-point multistart basin survey

8 cube corners + center + 1 random point, 50,000 rounds each (~15M steps,
~6s/walker, run in parallel). Result: **7-8 distinct low-energy basins
from 9 valid walkers** (E range 11.4-26.0) — no single dominant attractor,
lopsided in energy but not in walker count. Only two pairs of walkers
landed close enough (<20-25 units) to call the same basin; every other
start found its own distinct region.

## Experiment 3 — kinetic trap at the (-130.2, -77.0, 155.2) corner

One start point sat at grid index (0,0,39) — the literal corner of the
40^3 grid — 137 units from the nearest real cell (zero cells within 30
units), at E=298.4, essentially the single highest-energy point in the
whole landscape (grid max = 298.42). It's pure KDE-extrapolation with no
real data behind it.

Escape behavior was stochastic, not just slow: at 50k rounds it sometimes
escaped (E~17) and sometimes stayed stuck (E~104); across 4×200k-round
replicates, 3 escaped and 1 stayed stuck the whole time.

**High-T validation test** (6 replicates each, same 50k-round budget):

| T0  | escaped corner (x>0) | final E range | avg final E |
|-----|-----------------------|----------------|-------------|
| 10  | 1/6                    | 15-123          | 87.5        |
| 100 | 6/6                    | 13-125          | 55.5        |

Confirms it's a **kinetic trap, not a thermodynamic basin**: raising T
reliably crosses the barrier that low T rarely does. Root cause: the
straight-line energy profile from the corner to the real basin isn't
monotonic — it dips, rises again to a secondary ridge (E~275 at t=0.14),
then descends smoothly past t~0.36. That non-monotonic hump, combined
with starting at ~the landscape's global max, is what traps low-T walkers.

This is why `quartz_cellstate_multistart.c` and `quartz_cellstate_swarm.c`
both carry the data-support guard/mask — an occupancy- or energy-blind
respawn/start rule would otherwise be drawn to exactly this kind of empty,
KDE-extrapolated corner.

## Experiment 4 — "blocked" landscape: unusable, deleted

A second landscape file (`cellstate_landscape_blocked.bin`, someone/
something else's modification, arrived mid-session missing its axis
arrays — patched by splicing the original axes back in) turned out to
contain a **spurious deep minimum** off the data manifold: two multistart
walkers landed at E=0.19 and E=1.02 (lower than any real basin, which run
E~11-26), 80-85 units from the nearest real cell. On the original
landscape that same corner region was the *worst* point (E~298); on the
blocked one it's the best-looking point, but fake. Confirmed unusable and
deleted (file, binaries, logs, comparison plot) — see the `extrapolated`
tag doing exactly its job here: energy alone would have called this run a
success.

## Experiment 5 — collapse+respawn swarm coverage

`quartz_cellstate_swarm.c`, 8 walkers, 2000 rounds (600k steps/walker),
3 replicates each:

| mode                     | voxel coverage |
|--------------------------|----------------|
| passive (stall disabled) | 7.5% - 8.5%    |
| active (stall_limit=30)  | 9.8% - 10.2%   |

~25-30% relative coverage improvement from collapse+respawn alone, with
only ~12 collapse events per run. Only one collapse rule (novelty stall)
and one respawn rule (lowest-occupancy, data-supported) implemented so
far — path-redundancy collapse, KDE-variance-weighted respawn, and
rare-state cloning are documented as follow-ons, not yet built.

## Production run status (T0=10, real landscape)

`quartz_cellstate_node` / `_b` + their logger scripts have been running
continuously since 15:06 on 2026-07-30 against the real (unblocked)
landscape. T0=50 baseline logs archived as `cellstate_trajectory_T50.log`
/ `_b_T50.log` (153,331 points each) for ongoing comparison.

## Related toy model (separate track)

`gossip_swarm_sim.py` — standalone simulation testing the quartz-machine
-> swarm-consensus mapping (crystal oscillator -> meter reading, walker
alignment -> median-of-medians corroboration, watchdog -> kill+respawn,
regulation -> lifecycle management). 8 nodes, 400 ticks, real measured
run: mean consensus tracking error 0.033 (max 0.133) despite 24 injected-
fault kill/respawn cycles. `quartz_swarm_diagrams.py` produces illustrative
(non-measured) architecture diagrams of the same mapping — kept clearly
labeled as synthetic vs. the real sim's measured output.
