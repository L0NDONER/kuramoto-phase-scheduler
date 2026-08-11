/**
 * quartz_cellstate_swarm.c — fixed-size walker pool over the melanoma
 * cellstate landscape with collapse+respawn, built on top of the same
 * Metropolis core as quartz_cellstate_multistart.c.
 *
 * Minimal version (one collapse rule, one respawn rule):
 *   COLLAPSE  ("novelty stall"): a walker that hasn't landed in a new
 *             (previously-unvisited) grid voxel for STALL_LIMIT rounds
 *             is marked dead — it's just jittering in an already-mapped
 *             basin, not adding new information.
 *   RESPAWN   ("low-occupancy, data-supported"): a dead walker restarts
 *             at a grid voxel chosen from the least-visited-so-far cells,
 *             restricted to voxels within MIN_CELL_DIST of a real cell
 *             (see quartz_cellstate_multistart.c) — otherwise respawn
 *             would repeatedly aim walkers at the empty-space kinetic
 *             trap discovered this session (E~298, 137 units from any
 *             real cell): it's the emptiest region by construction, so
 *             an occupancy-only respawn rule would love it forever.
 *
 * All N walkers share one voxel occupancy grid (same 40^3 resolution as
 * the landscape), which is what respawn reads from and collapse writes to.
 *
 * Compile: gcc -O2 -o quartz_cellstate_swarm quartz_cellstate_swarm.c -lm
 * Run:     ./quartz_cellstate_swarm <landscape.bin> <n_walkers> <rounds>
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
#define MIN_CELL_DIST 50.0
#define E_CAP 100.0
#define STALL_LIMIT_DEFAULT 30   // rounds with no new voxel before collapse
#define LIVE_STATE_PATH "/tmp/swarm_live_state.json"
#define LIVE_STATE_INTERVAL 0.3  // seconds between live-state writes
#define RECENT_COLLAPSE_CAP 256   // max collapse events reported per snapshot

typedef struct {
    int nx, ny, nz;
    float *ax, *ay, *az;
    float *E;
    int n_cells;
    float *cells;
} Landscape;

typedef struct {
    double pos[3];
    int stall;
    int alive;
    long collapses;
} Walker;

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

static inline int voxel_index(const Landscape *L, const double *pos, int *ix, int *iy, int *iz) {
    double fx, fy, fz;
    *ix = locate(L->ax, L->nx, pos[0], &fx);
    *iy = locate(L->ay, L->ny, pos[1], &fy);
    *iz = locate(L->az, L->nz, pos[2], &fz);
    return ((*ix) * L->ny + (*iy)) * L->nz + (*iz);
}

// Precomputed once at startup: data_supported[voxel_idx] = 1 if that
// voxel's center is within MIN_CELL_DIST of a real cell. Respawn is called
// often (every STALL_LIMIT rounds per walker), so this must not repeat the
// O(voxels x cells) nearest_cell_dist scan on every call.
char *build_support_mask(const Landscape *L) {
    long n_voxels = (long)L->nx * L->ny * L->nz;
    char *mask = malloc(n_voxels);
    for (int ix = 0; ix < L->nx; ix++) {
        for (int iy = 0; iy < L->ny; iy++) {
            for (int iz = 0; iz < L->nz; iz++) {
                long idx = ((long)ix * L->ny + iy) * L->nz + iz;
                double p[3] = { L->ax[ix], L->ay[iy], L->az[iz] };
                mask[idx] = (nearest_cell_dist(p, L) <= MIN_CELL_DIST);
            }
        }
    }
    return mask;
}

// Respawn target: among voxels within MIN_CELL_DIST of a real cell, pick
// uniformly at random from those with the current minimum occupancy count.
void pick_respawn(const Landscape *L, const long *occupancy, const char *support_mask, double *out_pos) {
    long min_occ = -1;
    int candidates[4096];
    int n_candidates = 0;
    long n_voxels = (long)L->nx * L->ny * L->nz;

    for (long idx = 0; idx < n_voxels; idx++) {
        if (!support_mask[idx]) continue;
        if (min_occ < 0 || occupancy[idx] < min_occ) {
            min_occ = occupancy[idx];
            n_candidates = 0;
        }
        if (occupancy[idx] == min_occ && n_candidates < 4096) {
            candidates[n_candidates++] = idx;
        }
    }

    if (n_candidates == 0) {
        // fallback: grid center (always data-supported in this landscape)
        out_pos[0] = L->ax[L->nx / 2];
        out_pos[1] = L->ay[L->ny / 2];
        out_pos[2] = L->az[L->nz / 2];
        return;
    }

    long pick = candidates[rand() % n_candidates];
    long iz = pick % L->nz;
    long iy = (pick / L->nz) % L->ny;
    long ix = pick / ((long)L->nz * L->ny);
    out_pos[0] = L->ax[ix];
    out_pos[1] = L->ay[iy];
    out_pos[2] = L->az[iz];
}

void write_live_state(const Walker *walkers, int n_walkers, long round_num,
                       const int *recent_collapse_ids, int n_recent) {
    char tmp_path[] = LIVE_STATE_PATH ".tmp";
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f, "{\"round\": %ld, \"walkers\": [", round_num);
    for (int i = 0; i < n_walkers; i++) {
        fprintf(f, "%s[%.3f,%.3f,%.3f]", i ? "," : "",
                walkers[i].pos[0], walkers[i].pos[1], walkers[i].pos[2]);
    }
    fprintf(f, "], \"recent_collapses\": [");
    for (int i = 0; i < n_recent; i++)
        fprintf(f, "%s%d", i ? "," : "", recent_collapse_ids[i]);
    fprintf(f, "]}");
    fclose(f);
    rename(tmp_path, LIVE_STATE_PATH);
}

int main(int argc, char *argv[]) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <landscape.bin> <n_walkers> <rounds> [stall_limit]\n", argv[0]);
        return 1;
    }
    const char *path = argv[1];
    int n_walkers = atoi(argv[2]);
    long rounds = atol(argv[3]);
    long stall_limit = argc > 4 ? atol(argv[4]) : STALL_LIMIT_DEFAULT;

    Landscape L;
    if (!load_landscape(path, &L)) {
        fprintf(stderr, "failed to load landscape from %s\n", path);
        return 1;
    }

    srand((unsigned)(quartz_now() * 1e6) ^ (unsigned)getpid());

    long n_voxels = (long)L.nx * L.ny * L.nz;
    long *occupancy = calloc(n_voxels, sizeof(long));
    char *support_mask = build_support_mask(&L);

    Walker *walkers = malloc(sizeof(Walker) * n_walkers);
    for (int i = 0; i < n_walkers; i++) {
        walkers[i].pos[0] = L.ax[L.nx / 2];
        walkers[i].pos[1] = L.ay[L.ny / 2];
        walkers[i].pos[2] = L.az[L.nz / 2];
        walkers[i].stall = 0;
        walkers[i].alive = 1;
        walkers[i].collapses = 0;
    }

    long total_collapses = 0;
    double t_start = quartz_now();
    double t_last_heartbeat = t_start;
    double t_last_live = t_start;
    int recent_collapse_ids[RECENT_COLLAPSE_CAP];
    int n_recent_collapse = 0;

    for (long round_num = 0; round_num < rounds; round_num++) {
        for (int w = 0; w < n_walkers; w++) {
            Walker *W = &walkers[w];
            double T = T0;
            for (int step = 0; step < STEPS_PER_ROUND; step++) {
                double cur_e = energy(W->pos, &L);
                double cand[3] = {
                    W->pos[0] + gauss(STEP_SIGMA),
                    W->pos[1] + gauss(STEP_SIGMA),
                    W->pos[2] + gauss(STEP_SIGMA)
                };
                double e = energy(cand, &L);
                int accept = (e < cur_e) ||
                    (((double)rand() / RAND_MAX) < exp((cur_e - e) / (T > 1e-9 ? T : 1e-9)));
                if (accept) { W->pos[0] = cand[0]; W->pos[1] = cand[1]; W->pos[2] = cand[2]; }
                T *= COOL;
            }

            int ix, iy, iz;
            int idx = voxel_index(&L, W->pos, &ix, &iy, &iz);
            int is_new = (occupancy[idx] == 0);
            occupancy[idx]++;
            W->stall = is_new ? 0 : W->stall + 1;

            if (W->stall >= stall_limit) {
                pick_respawn(&L, occupancy, support_mask, W->pos);
                W->stall = 0;
                W->collapses++;
                total_collapses++;
                if (n_recent_collapse < RECENT_COLLAPSE_CAP)
                    recent_collapse_ids[n_recent_collapse++] = w;
            }
        }

        double now = quartz_now();
        if (now - t_last_live >= LIVE_STATE_INTERVAL) {
            write_live_state(walkers, n_walkers, round_num, recent_collapse_ids, n_recent_collapse);
            n_recent_collapse = 0;
            t_last_live = now;
        }
        if (now - t_last_heartbeat >= 5.0) {
            long visited_so_far = 0;
            for (long i = 0; i < n_voxels; i++) if (occupancy[i] > 0) visited_so_far++;
            printf("[hb] t=%.0fs round=%ld/%ld collapses=%ld voxels=%ld/%ld (%.1f%%)\n",
                   now - t_start, round_num, rounds, total_collapses,
                   visited_so_far, n_voxels, 100.0 * visited_so_far / n_voxels);
            fflush(stdout);
            t_last_heartbeat = now;
        }
    }

    long visited = 0;
    for (long i = 0; i < n_voxels; i++) if (occupancy[i] > 0) visited++;

    printf("=== %d walkers, %ld rounds (%ld steps each) ===\n",
           n_walkers, rounds, rounds * STEPS_PER_ROUND);
    printf("total collapse+respawn events: %ld\n", total_collapses);
    for (int w = 0; w < n_walkers; w++)
        printf("  walker %d: %ld collapses, final E=%.2f\n", w, walkers[w].collapses, energy(walkers[w].pos, &L));
    printf("voxels visited: %ld / %ld (%.1f%%)\n", visited, n_voxels, 100.0 * visited / n_voxels);
    return 0;
}
