/**
 * quartz_cellstate_multistart.c — same Metropolis annealer as
 * quartz_cellstate_node.c, but batch-mode: takes a start position and a
 * round count on the command line, runs to completion, prints one final
 * line, and exits. No state file, no infinite loop — for running many
 * independent walkers in parallel to survey basin structure.
 *
 * Compile: gcc -O2 -o quartz_cellstate_multistart quartz_cellstate_multistart.c -lm
 * Run:     ./quartz_cellstate_multistart <landscape.bin> <x> <y> <z> <rounds> [T0] [--force]
 *
 * Landscape file format: header+axes+grid as before, plus an appended
 * optional trailing section (n_cells: int32, then n_cells x [x,y,z] float32)
 * written by export_landscape_bin.py. Older landscape files without that
 * section still load fine (n_cells stays 0, start-point check is skipped).
 *
 * DATA-SUPPORT CHECK: refuses to start (unless --force) a walker further
 * than MIN_CELL_DIST from every real cell — see project note on the
 * (-130,-77,155) corner trap, which sat 137 units from the nearest real
 * cell, at ~global-max energy, in a pure KDE-extrapolation region.
 *
 * E_CAP: the interpolated grid energy (before boundary penalty) is capped
 * at E_CAP to bound numerical blowup in deep-extrapolation regions; this
 * does not change the boundary wall penalty used to keep walkers in-domain.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <unistd.h>

#define STEP_SIGMA 0.15
#define COOL 0.995
#define STEPS_PER_ROUND 300
#define MIN_CELL_DIST 50.0
#define E_CAP 100.0

typedef struct {
    int nx, ny, nz;
    float *ax, *ay, *az;
    float *E;
    int n_cells;
    float *cells; // flattened [x0,y0,z0, x1,y1,z1, ...]
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

    // optional trailing cell-coordinate section; absent in older files
    L->n_cells = 0;
    L->cells = NULL;
    int32_t n_cells;
    if (ok && read_exact(&n_cells, sizeof(int32_t), 1, f) && n_cells > 0) {
        float *cells = malloc(sizeof(float) * 3 * (size_t)n_cells);
        if (read_exact(cells, sizeof(float), 3 * (size_t)n_cells, f)) {
            L->n_cells = n_cells;
            L->cells = cells;
        } else {
            free(cells);
        }
    }

    fclose(f);
    return ok;
}

// distance from pos to the nearest real cell; INFINITY if no cell data loaded
double nearest_cell_dist(const double *pos, const Landscape *L) {
    if (L->n_cells == 0) return 0.0;
    double best = 1e18;
    for (int i = 0; i < L->n_cells; i++) {
        double dx = pos[0] - L->cells[i * 3 + 0];
        double dy = pos[1] - L->cells[i * 3 + 1];
        double dz = pos[2] - L->cells[i * 3 + 2];
        double d2 = dx * dx + dy * dy + dz * dz;
        if (d2 < best) best = d2;
    }
    return sqrt(best);
}

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
    if (e > E_CAP) e = E_CAP;
    e += boundary_penalty(L->ax, L->nx, pos[0]);
    e += boundary_penalty(L->ay, L->ny, pos[1]);
    e += boundary_penalty(L->az, L->nz, pos[2]);
    return e;
}

int main(int argc, char *argv[]) {
    if (argc < 6) {
        fprintf(stderr, "usage: %s <landscape.bin> <x> <y> <z> <rounds> [T0] [--force]\n", argv[0]);
        return 1;
    }
    const char *path = argv[1];
    double start[3] = { atof(argv[2]), atof(argv[3]), atof(argv[4]) };
    long rounds = atol(argv[5]);
    double T0 = argc > 6 && strcmp(argv[6], "--force") != 0 ? atof(argv[6]) : 10.0;
    int force = 0;
    for (int i = 6; i < argc; i++) if (strcmp(argv[i], "--force") == 0) force = 1;

    Landscape L;
    if (!load_landscape(path, &L)) {
        fprintf(stderr, "failed to load landscape from %s\n", path);
        return 1;
    }

    double d = nearest_cell_dist(start, &L);
    if (L.n_cells > 0 && d > MIN_CELL_DIST) {
        fprintf(stderr, "REFUSED: start (%.2f, %.2f, %.2f) is %.1f units from the "
                "nearest real cell (> %.0f) — KDE-extrapolated region, not "
                "data-supported. Pass --force to override.\n",
                start[0], start[1], start[2], d, MIN_CELL_DIST);
        if (!force) return 1;
        fprintf(stderr, "--force set, proceeding anyway.\n");
    }

    srand((unsigned)(quartz_now() * 1e6) ^ (unsigned)getpid());

    double pos[3] = { start[0], start[1], start[2] };

    for (long round_num = 0; round_num < rounds; round_num++) {
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
    }

    double e_final = energy(pos, &L);
    double d_final = nearest_cell_dist(pos, &L);
    const char *tag = (L.n_cells == 0) ? "unknown"
                     : (d_final <= MIN_CELL_DIST) ? "data-supported" : "extrapolated";
    printf("START %.6f %.6f %.6f  FINAL %.6f %.6f %.6f  E %.6f  nearest_cell %.1f  %s\n",
           start[0], start[1], start[2], pos[0], pos[1], pos[2], e_final, d_final, tag);
    return 0;
}
