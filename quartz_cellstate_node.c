/**
 * quartz_cellstate_node.c — Metropolis annealer over a static energy
 * landscape derived from melanoma scRNA-seq cell states (Tirosh et al.
 * 2016), instead of the live BTC/ETH feed used by quartz_portfolio_node.c.
 *
 * Same Metropolis pattern (T0/COOL/reheat) as the portfolio node, but:
 *  - the energy function E(x,y,z) comes from a static 40x40x40 grid
 *    (trilinearly interpolated) exported from a PCA+KDE landscape, not a
 *    live market snapshot — so there's no polling loop for fresh data.
 *  - runs as a single local process (no coordinator/worker UDP split);
 *    this is a standalone exploration node, deliberately separate from
 *    the live financial swarm so BTC/ETH keeps running untouched.
 *
 * State (grid, axes) is loaded once at startup from cellstate_landscape.bin
 * (see export_landscape_bin.py). Current position is written to
 * /tmp/cellstate_state.json every round, mirroring market_state.json's
 * atomic tmp+rename pattern, for any downstream viewer/logger.
 *
 * This computes and PRINTS a position. No actuation, no execution path.
 *
 * Compile: gcc -O2 -o quartz_cellstate_node quartz_cellstate_node.c -lm
 * Run:     ./quartz_cellstate_node cellstate_landscape.bin
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <unistd.h>

#define STEP_SIGMA 0.15
#define T0 10.0
#define COOL 0.995
#define STEPS_PER_ROUND 300
#define STATE_PATH "/tmp/cellstate_state.json"

typedef struct {
    int nx, ny, nz;
    float *ax, *ay, *az;
    float *E; // flattened, C order: E[((ix*ny)+iy)*nz+iz]
} Landscape;

double quartz_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

double gauss(double sigma) {
    double u1 = (double)rand() / RAND_MAX, u2 = (double)rand() / RAND_MAX;
    return sqrt(-2.0 * log(u1 + 1e-12)) * cos(2 * M_PI * u2) * sigma;
}

static int read_exact(void *buf, size_t sz, size_t n, FILE *f) {
    return fread(buf, sz, n, f) == n;
}

int load_landscape(const char *path, Landscape *L) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror("fopen"); return 0; }
    int32_t nx, ny, nz;
    if (!read_exact(&nx, sizeof(int32_t), 1, f) ||
        !read_exact(&ny, sizeof(int32_t), 1, f) ||
        !read_exact(&nz, sizeof(int32_t), 1, f)) { fclose(f); return 0; }
    L->nx = nx; L->ny = ny; L->nz = nz;
    L->ax = malloc(sizeof(float) * nx);
    L->ay = malloc(sizeof(float) * ny);
    L->az = malloc(sizeof(float) * nz);
    L->E = malloc(sizeof(float) * (size_t)nx * ny * nz);
    int ok = read_exact(L->ax, sizeof(float), nx, f) &&
             read_exact(L->ay, sizeof(float), ny, f) &&
             read_exact(L->az, sizeof(float), nz, f) &&
             read_exact(L->E, sizeof(float), (size_t)nx * ny * nz, f);
    fclose(f);
    return ok;
}

// find left index i such that axis[i] <= v <= axis[i+1], clamped to grid bounds
static int locate(const float *axis, int n, double v, double *frac) {
    if (v <= axis[0]) { *frac = 0.0; return 0; }
    if (v >= axis[n - 1]) { *frac = 1.0; return n - 2; }
    int i = 0;
    while (i < n - 2 && axis[i + 1] < v) i++;
    *frac = (v - axis[i]) / (axis[i + 1] - axis[i]);
    return i;
}

static inline float grid_at(const Landscape *L, int ix, int iy, int iz) {
    return L->E[((size_t)ix * L->ny + iy) * L->nz + iz];
}

// locate() clamps the interpolation fraction at the grid edge, so without
// this the trilinear lookup goes flat outside the bounding box (zero
// gradient => Metropolis accepts every step => unconstrained random walk
// drifting off the data manifold, as actually observed on real runs).
// K_WALL chosen so a step of ~10 units past the edge costs roughly as much
// energy as descending an entire real basin (basins span ~10-100 in E).
#define K_WALL 0.05
static double boundary_penalty(const float *axis, int n, double v) {
    double lo = axis[0], hi = axis[n - 1];
    if (v < lo) { double d = lo - v; return K_WALL * d * d; }
    if (v > hi) { double d = v - hi; return K_WALL * d * d; }
    return 0.0;
}

double energy(const double *pos, const Landscape *L) {
    double fx, fy, fz;
    int ix = locate(L->ax, L->nx, pos[0], &fx);
    int iy = locate(L->ay, L->ny, pos[1], &fy);
    int iz = locate(L->az, L->nz, pos[2], &fz);

    double c000 = grid_at(L, ix, iy, iz),     c001 = grid_at(L, ix, iy, iz + 1);
    double c010 = grid_at(L, ix, iy + 1, iz), c011 = grid_at(L, ix, iy + 1, iz + 1);
    double c100 = grid_at(L, ix + 1, iy, iz), c101 = grid_at(L, ix + 1, iy, iz + 1);
    double c110 = grid_at(L, ix + 1, iy + 1, iz), c111 = grid_at(L, ix + 1, iy + 1, iz + 1);

    double c00 = c000 * (1 - fx) + c100 * fx;
    double c01 = c001 * (1 - fx) + c101 * fx;
    double c10 = c010 * (1 - fx) + c110 * fx;
    double c11 = c011 * (1 - fx) + c111 * fx;

    double c0 = c00 * (1 - fy) + c10 * fy;
    double c1 = c01 * (1 - fy) + c11 * fy;

    double e = c0 * (1 - fz) + c1 * fz;
    e += boundary_penalty(L->ax, L->nx, pos[0]);
    e += boundary_penalty(L->ay, L->ny, pos[1]);
    e += boundary_penalty(L->az, L->nz, pos[2]);
    return e;
}

void write_cellstate(double t, const double *pos, double e) {
    char tmp_path[] = STATE_PATH ".tmp";
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f, "{\"t\": %.6f, \"pos\": [%.6f, %.6f, %.6f], \"E\": %.6f}",
            t, pos[0], pos[1], pos[2], e);
    fclose(f);
    rename(tmp_path, STATE_PATH);
}

int main(int argc, char *argv[]) {
    const char *path = argc > 1 ? argv[1] : "cellstate_landscape.bin";
    Landscape L;
    if (!load_landscape(path, &L)) {
        fprintf(stderr, "failed to load landscape from %s\n", path);
        return 1;
    }
    printf("=== QUARTZ CELLSTATE NODE ===\n");
    printf("landscape: %dx%dx%d grid, energy range explored below\n", L.nx, L.ny, L.nz);
    printf("PRINTS POSITIONS ONLY — no actuation, standalone from BTC/ETH swarm.\n\n");

    srand((unsigned)(quartz_now() * 1e6) ^ (unsigned)getpid());

    // start at the grid's center
    double pos[3] = {
        L.ax[L.nx / 2], L.ay[L.ny / 2], L.az[L.nz / 2]
    };

    long round_num = 0;
    while (1) {
        double T = T0;
        for (int step = 0; step < STEPS_PER_ROUND; step++) {
            double cur_e = energy(pos, &L);
            double cand[3] = {
                pos[0] + gauss(STEP_SIGMA),
                pos[1] + gauss(STEP_SIGMA),
                pos[2] + gauss(STEP_SIGMA)
            };
            double e = energy(cand, &L);
            int accept = (e < cur_e) ||
                (((double)rand() / RAND_MAX) < exp((cur_e - e) / (T > 1e-9 ? T : 1e-9)));
            if (accept) { pos[0] = cand[0]; pos[1] = cand[1]; pos[2] = cand[2]; }
            T *= COOL;
        }
        round_num++;

        double e_now = energy(pos, &L);
        printf("[round %ld] pos=(%.3f, %.3f, %.3f)  E=%.4f\n",
               round_num, pos[0], pos[1], pos[2], e_now);
        write_cellstate(quartz_now(), pos, e_now);

        usleep(200 * 1000);
    }
    return 0;
}
