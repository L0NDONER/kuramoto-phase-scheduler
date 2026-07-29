/**
 * quartz_metro_node.c — staged replacement for quartz_xor_node.c +
 * quartz_portfolio_node.c, now with cellstate as a third job type.
 *
 * All three originals ran variants of the same Metropolis pattern with
 * only the energy function, weight count, worker topology, and
 * restart-vs-continuous schedule differing. This collapses them into
 * one binary where job_type ("xor" | "portfolio" | "cellstate") is a
 * runtime argv, not a compile-time choice — adding a new job type means
 * adding an energy_fn + a JobSpec entry, not a new .c file.
 *
 * PORTS: job_type picks its port pair from JobSpec, matching each
 * original exactly — xor 5002/5003, portfolio 5004/5005 — so an xor job
 * and an already-running portfolio job never collide. cellstate needs no
 * real network traffic (n_workers=0, single local process, same as the
 * original) but still binds a port for structural uniformity; 5006/5007
 * are unused elsewhere so this is inert.
 *
 * CORRECTNESS FIX during the cellstate merge: the original version of
 * this file re-randomized all weights via uniform_range() at the top of
 * *every* round, including continuous jobs. That matched xor (which
 * really does want a fresh random restart each time) but silently broke
 * portfolio's actual behavior — the original quartz_portfolio_node.c
 * only initializes weights once and carries them forward across rounds,
 * only resetting temperature T each round. Cellstate needs the same
 * carry-forward (it's a persistent random walk across a landscape,
 * that's the entire point). Fixed: init/INIT-message now only fires on
 * restart==0 for continuous jobs; xor still re-randomizes every restart.
 * This means the earlier "validated" portfolio test (best_e swinging
 * between rounds instead of monotonically settling) was actually
 * exercising the bug, not the original's real behavior — worth knowing
 * if you compare old logs to new ones.
 *
 * Compile: gcc -O2 -o quartz_metro_node quartz_metro_node.c -lm
 * Run:
 *   coord (xor/portfolio):
 *     ./quartz_metro_node coord <xor|portfolio> <worker1_ip> <worker2_ip> [max_rounds]
 *   worker (xor/portfolio):
 *     ./quartz_metro_node worker <xor|portfolio> <slot: 0|1> <coord_ip>
 *   coord (cellstate — no workers, single local process):
 *     ./quartz_metro_node coord cellstate [landscape_path] [max_rounds]
 *
 * job=xor:       coordinator owns w0,w1 locally; worker slot0 owns w2,w3;
 *                worker slot1 owns w4,w5. Fixed N_RESTARTS then FINAL,
 *                same as quartz_xor_node.c.
 * job=portfolio: coordinator owns no weights; worker slot0 owns w0 (BTC),
 *                slot1 owns w1 (ETH). Runs forever, polling
 *                /tmp/market_state.json for a fresh snapshot each round,
 *                same as quartz_portfolio_node.c. Paper decisions only —
 *                no exchange order execution, same as the original.
 * job=cellstate: coordinator owns all 3 position weights locally, no
 *                workers. Runs forever over a static landscape loaded
 *                once at startup, writes /tmp/cellstate_state.json every
 *                round in the same atomic tmp+rename schema as the
 *                original — cellstate_logger.sh needs no changes.
 *                Standalone from the BTC/ETH swarm, same as the original.
 *
 * STAGED — xor validated on loopback and over real LAN to one Pi
 * (10.0.0.122) at production ports. portfolio and cellstate validated
 * on loopback only post-fix. Not yet promoted over the retired
 * quartz_xor_node.c / quartz_portfolio_node.c.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <unistd.h>
#include <dlfcn.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/stat.h>
#include "quartz_job.h"

#define RECV_TIMEOUT_MS 200
#define MAX_RETRIES 8

typedef enum { MSG_INIT = 1, MSG_STEP_REQ = 2, MSG_CANDIDATE = 3, MSG_DECISION = 4, MSG_FINAL = 5 } MsgType;

typedef struct {
    uint8_t type;
    uint8_t id_a, id_b;   // id_b = 0xFF for single-weight workers (portfolio)
    double val_a, val_b;
    uint8_t decision;
} Msg;

double quartz_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

// Wall-clock time, only for the job registry -- quartz_now() is
// deliberately MONOTONIC_RAW (arbitrary epoch, not comparable across
// processes or by an external reader like quartz_ps.py). The registry is
// the one place this codebase actually needs a wall-clock timestamp.
double wall_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

double gauss(double sigma) {
    double u1 = (double)rand() / RAND_MAX, u2 = (double)rand() / RAND_MAX;
    return sqrt(-2.0 * log(u1 + 1e-12)) * cos(2 * M_PI * u2) * sigma;
}

double uniform_range(double r) {
    return ((double)rand() / RAND_MAX) * 2 * r - r;
}

// ---- job-specific energy functions ----

static const double XOR_IN[4][2] = {{0,0},{0,1},{1,0},{1,1}};
static const double XOR_TARGET[4] = {0, 1, 1, 0};

double net6(const double *w, double a, double b) {
    double h1 = tanh(w[0]*a + w[1]*b);
    double h2 = tanh(w[2]*a + w[3]*b);
    return tanh(w[4]*h1 + w[5]*h2);
}

double xor_energy(const double *w, void *ctx) {
    (void)ctx;
    double e = 0.0;
    for (int i = 0; i < 4; i++) {
        double out = net6(w, XOR_IN[i][0], XOR_IN[i][1]);
        double d = out - XOR_TARGET[i];
        e += d * d;
    }
    return e;
}

typedef struct { double r[2]; double sigma[2][2]; } MarketCtx;

double portfolio_energy(const double *w, void *ctx) {
    MarketCtx *m = (MarketCtx *)ctx;
    double risk = w[0]*w[0]*m->sigma[0][0] + 2*w[0]*w[1]*m->sigma[0][1] + w[1]*w[1]*m->sigma[1][1];
    double ret = 1e3 * (w[0]*m->r[0] + w[1]*m->r[1]);
    double reg = 1.0 * (w[0]*w[0] + w[1]*w[1]);
    return risk - ret + reg;
}

int read_market_state(MarketCtx *m) {
    FILE *f = fopen("/tmp/market_state.json", "r");
    if (!f) return 0;
    char buf[512];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    double t;
    int ok = sscanf(buf, "{\"t\": %lf, \"r\": [%lf, %lf], \"sigma\": [[%lf, %lf], [%lf, %lf]]}",
                     &t, &m->r[0], &m->r[1], &m->sigma[0][0], &m->sigma[0][1], &m->sigma[1][0], &m->sigma[1][1]);
    return ok == 7;
}

// ---- peer health, read from quartz_node's daemon-side export ----
// Fails open: no health file, or peer not listed in it, counts as healthy
// (the daemon may not be running/deployed everywhere; this is an
// optimization to skip known-dead peers, not a hard gate). Only an
// explicit "healthy": false marks a peer down.
int peer_healthy(const char *ip) {
    FILE *f = fopen("/tmp/quartz_peer_health.json", "r");
    if (!f) return 1;
    char line[256];
    int result = 1;
    while (fgets(line, sizeof(line), f)) {
        char found_ip[32], healthy_str[8];
        if (sscanf(line,
                   "{\"peer_ip\": \"%31[^\"]\", \"last_seen\": %*f, \"phase_offset\": %*f, \"dev\": %*f, \"healthy\": %7[a-z]}",
                   found_ip, healthy_str) == 2) {
            if (strcmp(found_ip, ip) == 0) {
                result = (strcmp(healthy_str, "true") == 0);
                break;
            }
        }
    }
    fclose(f);
    return result;
}

// ---- job registry: one file per process, atomic tmp+rename like every
// other cross-process state file in this codebase (market_state.json,
// cellstate_state.json, quartz_peer_health.json). No cleanup-on-exit
// handler -- a reader treats a file whose pid isn't alive as stale and
// prunes it itself, same fail-toward-simple approach as peer_healthy().
#define REGISTRY_DIR "/tmp/quartz_jobs"
void write_registry(const char *job_name, const char *role, int slot, int restart, int is_plugin) {
    static double start_time = 0;
    if (start_time == 0) start_time = wall_now();
    mkdir(REGISTRY_DIR, 0777);   // ignore EEXIST
    char path[128], tmp_path[136];
    snprintf(path, sizeof(path), REGISTRY_DIR "/%d.json", getpid());
    snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path);
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f, "{\"pid\": %d, \"job\": \"%s\", \"role\": \"%s\", \"slot\": %d, "
               "\"plugin\": %s, \"start_time\": %.6f, \"last_update\": %.6f, \"restart\": %d}\n",
            getpid(), job_name, role, slot, is_plugin ? "true" : "false",
            start_time, wall_now(), restart);
    fclose(f);
    rename(tmp_path, path);
}

// ---- cellstate: static landscape energy, ported from quartz_cellstate_node.c ----

typedef struct {
    int nx, ny, nz;
    float *ax, *ay, *az;
    float *E; // flattened, C order: E[((ix*ny)+iy)*nz+iz]
} Landscape;

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

double cellstate_energy(const double *w, void *ctx) {
    const Landscape *L = (const Landscape *)ctx;
    double fx, fy, fz;
    int ix = locate(L->ax, L->nx, w[0], &fx);
    int iy = locate(L->ay, L->ny, w[1], &fy);
    int iz = locate(L->az, L->nz, w[2], &fz);

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
    e += boundary_penalty(L->ax, L->nx, w[0]);
    e += boundary_penalty(L->ay, L->ny, w[1]);
    e += boundary_penalty(L->az, L->nz, w[2]);
    return e;
}

#define CELLSTATE_STATE_PATH "/tmp/cellstate_state.json"
void write_cellstate(double t, const double *w, double e) {
    char tmp_path[] = CELLSTATE_STATE_PATH ".tmp";
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f, "{\"t\": %.6f, \"pos\": [%.6f, %.6f, %.6f], \"E\": %.6f}",
            t, w[0], w[1], w[2], e);
    fclose(f);
    rename(tmp_path, CELLSTATE_STATE_PATH);
}

// ---- generalized job spec ----
// Two ways to add a new job type:
//   1. Built-in: write an EnergyFn, add a branch below. Requires
//      recompiling quartz_metro_node itself.
//   2. Plugin: write a .so exporting quartz_job_init() (see quartz_job.h),
//      drop it in $QUARTZ_JOBS_DIR (default ./jobs/) as <job_type>.so.
//      No recompile of this binary -- an already-running/deployed node
//      picks it up on next invocation. This is what actually answers "can
//      you submit a new job type without recompiling the node."
// JobSpec/EnergyFn/RoundEndFn now live in quartz_job.h since plugins need
// the identical struct layout.

JobSpec make_job(const char *job_type, int argc, char **argv) {
    JobSpec j = {0};
    if (strcmp(job_type, "xor") == 0) {
        j.name = "xor"; j.n_weights = 6; j.n_local = 2; j.n_workers = 2;
        j.worker_ids[0][0] = 2; j.worker_ids[0][1] = 3;
        j.worker_ids[1][0] = 4; j.worker_ids[1][1] = 5;
        j.energy_fn = xor_energy; j.continuous = 0;
        j.n_restarts = 4; j.steps_each = 5000;
        j.t0 = 1.0; j.cool = 0.998; j.step_sigma = 0.15; j.init_range = 2.0;
        j.coord_port = 5002; j.worker_port = 5003;   // matches quartz_xor_node.c
    } else if (strcmp(job_type, "portfolio") == 0) {
        j.name = "portfolio"; j.n_weights = 2; j.n_local = 0; j.n_workers = 2;
        j.worker_ids[0][0] = 0; j.worker_ids[0][1] = -1;
        j.worker_ids[1][0] = 1; j.worker_ids[1][1] = -1;
        j.energy_fn = portfolio_energy; j.poll_market = 1; j.continuous = 1;
        j.steps_each = 300;
        j.t0 = 1e-4; j.cool = 0.985; j.step_sigma = 0.1; j.init_range = 2.0;
        j.coord_port = 5004; j.worker_port = 5005;   // matches quartz_portfolio_node.c
    } else if (strcmp(job_type, "cellstate") == 0) {
        j.name = "cellstate"; j.n_weights = 3; j.n_local = 3; j.n_workers = 0;
        j.energy_fn = cellstate_energy; j.continuous = 1;
        j.steps_each = 300;
        j.t0 = 50.0; j.cool = 0.995; j.step_sigma = 0.15; j.init_range = 0.0;
        j.on_round_end = write_cellstate;
        j.coord_port = 5006; j.worker_port = 5007;   // unused (n_workers=0), kept for uniformity
    } else {
        // Not a built-in -- try a plugin .so instead of failing outright.
        const char *jobs_dir = getenv("QUARTZ_JOBS_DIR");
        if (!jobs_dir) jobs_dir = "./jobs";
        char path[512];
        snprintf(path, sizeof(path), "%s/%s.so", jobs_dir, job_type);

        void *handle = dlopen(path, RTLD_NOW);
        if (!handle) {
            fprintf(stderr, "unknown job_type '%s' -- not built in, and no plugin at %s (%s)\n",
                    job_type, path, dlerror());
            exit(1);
        }
        dlerror();
        quartz_job_init_fn init_fn = (quartz_job_init_fn)dlsym(handle, "quartz_job_init");
        const char *err = dlerror();
        if (err) {
            fprintf(stderr, "plugin %s missing quartz_job_init symbol: %s\n", path, err);
            exit(1);
        }
        if (init_fn(argc, argv, &j) != 0) {
            fprintf(stderr, "plugin %s: quartz_job_init failed\n", path);
            exit(1);
        }
        j.is_plugin = 1;   // after init_fn -- it does *out = <its own local>, wiping any earlier write
        printf("[job=%s] loaded plugin %s\n", j.name, path);
        // Never dlclose(handle) -- j.energy_fn/on_round_end point into it
        // and must stay valid for the life of this process.
    }
    return j;
}

int udp_socket(int port) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) { perror("socket"); exit(1); }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) { perror("bind"); exit(1); }
    struct timeval tv = {0, RECV_TIMEOUT_MS * 1000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    return sock;
}

void send_msg(int sock, const char *ip, int port, const Msg *m) {
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, ip, &addr.sin_addr);
    sendto(sock, m, sizeof(*m), 0, (struct sockaddr*)&addr, sizeof(addr));
}

int recv_until(int sock, MsgType want_type, Msg *out, const char *expect_ip,
                const char *retry_ip, int retry_port, const Msg *probe) {
    struct in_addr expect_addr;
    int has_expect = expect_ip && inet_pton(AF_INET, expect_ip, &expect_addr) == 1;
    for (int attempt = 0; attempt < MAX_RETRIES; attempt++) {
        if (probe && attempt > 0) send_msg(sock, retry_ip, retry_port, probe);
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        int n = recvfrom(sock, out, sizeof(*out), 0, (struct sockaddr*)&from, &fl);
        if (n == (int)sizeof(*out) && out->type == want_type) {
            // A packet of the right type from the wrong peer must not satisfy
            // a slot meant for a different worker (see quartz-substrate fault
            // test, 2026-07-29: a dead worker's slot was silently filled by
            // the other worker's reply, freezing that worker's weight with
            // no error or skip logged).
            if (has_expect && from.sin_addr.s_addr != expect_addr.s_addr) continue;
            return 1;
        }
    }
    return 0;
}

// ============== WORKER ==============
// Only used by job types with n_workers > 0 (xor, portfolio) — cellstate
// is a single local process and never spawns a worker role.
int run_worker(JobSpec *j, int slot, const char *coord_ip) {
    srand((unsigned)(quartz_now() * 1e6) ^ (unsigned)getpid());
    int sock = udp_socket(j->worker_port);
    int id_a = j->worker_ids[slot][0];
    int id_b = j->worker_ids[slot][1];
    int two = (id_b >= 0);
    double w[2] = {0, 0}, pending[2] = {0, 0};

    printf("=== QUARTZ METRO WORKER (job=%s) slot=%d ids=%d,%d ===\n", j->name, slot, id_a, id_b);
    write_registry(j->name, "worker", slot, 0, j->is_plugin);

    int restart = 0;
    int max_restarts = j->continuous ? 1000000 : j->n_restarts;
    while (restart < max_restarts) {
        // Continuous jobs (portfolio) initialize once and carry weights
        // forward across rounds — only restart==0 waits for MSG_INIT.
        if (restart == 0 || !j->continuous) {
            Msg m;
            if (!recv_until(sock, MSG_INIT, &m, coord_ip, NULL, 0, NULL)) continue;
            if (m.id_a != id_a) continue;
            w[0] = m.val_a; if (two) w[1] = m.val_b;
        }

        for (int step = 0; step < j->steps_each; step++) {
            Msg m;
            if (!recv_until(sock, MSG_STEP_REQ, &m, coord_ip, NULL, 0, NULL)) continue;
            pending[0] = w[0] + gauss(j->step_sigma);
            if (two) pending[1] = w[1] + gauss(j->step_sigma);

            Msg cand = {MSG_CANDIDATE, (uint8_t)id_a, two ? (uint8_t)id_b : 0xFF, pending[0], two ? pending[1] : 0.0, 0};
            Msg decision;
            send_msg(sock, coord_ip, j->coord_port, &cand);
            if (recv_until(sock, MSG_DECISION, &decision, coord_ip, coord_ip, j->coord_port, &cand)) {
                if (decision.decision) { w[0] = pending[0]; if (two) w[1] = pending[1]; }
            }
        }
        printf("[job=%s slot=%d restart=%d] w=%.4f%s\n", j->name, slot, restart,
               w[0], two ? " (+w1 above)" : "");
        write_registry(j->name, "worker", slot, restart, j->is_plugin);
        restart++;
    }

    Msg fin;
    if (!j->continuous && recv_until(sock, MSG_FINAL, &fin, coord_ip, NULL, 0, NULL)) {
        printf("FINAL slot=%d w%d=%.4f\n", slot, id_a, fin.val_a);
    }
    return 0;
}

// ============== COORDINATOR ==============
int run_coordinator(JobSpec *j, const char *worker_ips[2], int max_rounds_override) {
    srand((unsigned)(quartz_now() * 1e6) ^ (unsigned)getpid());
    int sock = udp_socket(j->coord_port);

    printf("=== QUARTZ METRO COORDINATOR (job=%s) ===\n", j->name);
    write_registry(j->name, "coord", -1, 0, j->is_plugin);

    double best_e = 1e18, best_w[6] = {0};
    double w[6] = {0};
    MarketCtx mkt = {0};
    int rounds_run = 0;
    int did_init = 0;   // tracks real init, not restart==0 — a skipped-round
                         // (unhealthy worker) must not count as "the first round"

    for (int restart = 0; restart < (j->continuous ? 1000000 : j->n_restarts); restart++) {
        int all_healthy = 1;
        for (int s = 0; s < j->n_workers; s++) {
            if (!peer_healthy(worker_ips[s])) {
                printf("[job=%s restart=%d] worker %s unhealthy, skipping round\n", j->name, restart, worker_ips[s]);
                all_healthy = 0;
            }
        }
        if (!all_healthy) { usleep(200000); continue; }

        if (j->continuous && j->poll_market) {
            MarketCtx fresh;
            int have = read_market_state(&fresh);
            if (!have) { usleep(100000); continue; }
            mkt = fresh;
        }

        // Continuous jobs init once (first non-skipped round) and carry
        // weights forward; xor (non-continuous) gets a fresh random init
        // every restart.
        if (!did_init || !j->continuous) {
            if (j->has_init_w) {
                memcpy(w, j->init_w, sizeof(double) * j->n_weights);
            } else {
                for (int i = 0; i < j->n_weights; i++) w[i] = uniform_range(j->init_range);
            }

            for (int s = 0; s < j->n_workers; s++) {
                int id_a = j->worker_ids[s][0], id_b = j->worker_ids[s][1];
                int two = (id_b >= 0);
                Msg init = {MSG_INIT, (uint8_t)id_a, two ? (uint8_t)id_b : 0xFF, w[id_a], two ? w[id_b] : 0.0, 0};
                for (int r = 0; r < 3; r++) { send_msg(sock, worker_ips[s], j->worker_port, &init); usleep(20000); }
            }
            did_init = 1;
        }

        void *ctx = j->poll_market ? (void*)&mkt : j->static_ctx;
        double cur_e = j->energy_fn(w, ctx);
        double T = j->t0;
        printf("[job=%s restart=%d] init energy=%.4f\n", j->name, restart, cur_e);

        for (int step = 0; step < j->steps_each; step++) {
            double cand[6];
            memcpy(cand, w, sizeof(double) * j->n_weights);
            for (int i = 0; i < j->n_local; i++) cand[i] = w[i] + gauss(j->step_sigma);

            Msg req = {MSG_STEP_REQ, 0, 0, 0, 0, 0};
            int ok = 1;
            for (int s = 0; s < j->n_workers; s++) {
                send_msg(sock, worker_ips[s], j->worker_port, &req);
            }
            for (int s = 0; s < j->n_workers; s++) {
                Msg c;
                if (!recv_until(sock, MSG_CANDIDATE, &c, worker_ips[s], worker_ips[s], j->worker_port, &req)) { ok = 0; break; }
                cand[c.id_a] = c.val_a;
                if (c.id_b != 0xFF) cand[c.id_b] = c.val_b;
            }
            if (!ok) { step--; continue; }

            double e = j->energy_fn(cand, ctx);
            int accept = (e < cur_e) || (((double)rand() / RAND_MAX) < exp((cur_e - e) / (T > 1e-9 ? T : 1e-9)));
            if (accept) {
                memcpy(w, cand, sizeof(double) * j->n_weights);
                cur_e = e;
                if (cur_e < best_e) { best_e = cur_e; memcpy(best_w, w, sizeof(double) * j->n_weights); }
            }

            Msg dec = {MSG_DECISION, 0, 0, 0, 0, (uint8_t)accept};
            for (int s = 0; s < j->n_workers; s++) send_msg(sock, worker_ips[s], j->worker_port, &dec);

            T *= j->cool;
        }

        printf("[job=%s restart=%d] final energy=%.4f best=%.4f\n", j->name, restart, cur_e, best_e);
        if (j->on_round_end) j->on_round_end(quartz_now(), w, cur_e);
        write_registry(j->name, "coord", -1, restart, j->is_plugin);
        rounds_run++;
        if (max_rounds_override > 0 && rounds_run >= max_rounds_override) break;
    }

    printf("\n=== DONE job=%s best_e=%.5f ===\nweights: ", j->name, best_e);
    for (int i = 0; i < j->n_weights; i++) printf("w%d=%.4f  ", i, best_w[i]);
    printf("\n");

    if (!j->continuous) {
        for (int s = 0; s < j->n_workers; s++) {
            int id_a = j->worker_ids[s][0], id_b = j->worker_ids[s][1];
            int two = (id_b >= 0);
            Msg fin = {MSG_FINAL, (uint8_t)id_a, two ? (uint8_t)id_b : 0xFF, best_w[id_a], two ? best_w[id_b] : 0.0, 0};
            for (int r = 0; r < 3; r++) { send_msg(sock, worker_ips[s], j->worker_port, &fin); usleep(20000); }
        }
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr,
            "Usage:\n"
            "  %s coord <xor|portfolio> <worker1_ip> <worker2_ip> [max_rounds]\n"
            "  %s coord cellstate [landscape_path] [max_rounds]\n"
            "  %s coord <plugin_job_type> [job-specific args...]\n"
            "  %s worker <xor|portfolio|plugin_job_type> <slot: 0|1> <coord_ip>\n"
            "\n"
            "Plugin job types load %s/<job_type>.so at runtime (dlopen) --\n"
            "no rebuild of this binary needed. Override the search dir with\n"
            "$QUARTZ_JOBS_DIR. See quartz_job.h.\n",
            argv[0], argv[0], argv[0], argv[0],
            getenv("QUARTZ_JOBS_DIR") ? getenv("QUARTZ_JOBS_DIR") : "./jobs");
        return 1;
    }
    if (strcmp(argv[1], "coord") == 0) {
        JobSpec j = make_job(argv[2], argc - 3, argv + 3);

        if (strcmp(argv[2], "cellstate") == 0) {
            const char *path = argc > 3 ? argv[3] : "cellstate_landscape.bin";
            Landscape *L = malloc(sizeof(Landscape));
            if (!load_landscape(path, L)) {
                fprintf(stderr, "failed to load landscape from %s\n", path);
                return 1;
            }
            printf("landscape: %dx%dx%d grid loaded from %s\n", L->nx, L->ny, L->nz, path);
            j.static_ctx = L;
            j.has_init_w = 1;
            j.init_w[0] = L->ax[L->nx / 2];
            j.init_w[1] = L->ay[L->ny / 2];
            j.init_w[2] = L->az[L->nz / 2];
            int max_rounds = argc > 4 ? atoi(argv[4]) : 0;
            const char *no_workers[2] = {"", ""};
            return run_coordinator(&j, no_workers, max_rounds);
        }

        if (j.n_workers == 0) {
            // Plugin jobs shaped like cellstate (no workers). quartz_job_init
            // already saw the full leftover argv and consumed whatever it
            // needed; main() then also tries argv[3] as an optional trailing
            // max_rounds. If a plugin's own arg lands in that slot and isn't
            // numeric, atoi() just yields 0 (run forever) -- harmless, but
            // plugins wanting >1 arg plus max_rounds need a convention of
            // their own (env var, fixed arg count) since there's no length
            // handshake here.
            int max_rounds = argc > 3 ? atoi(argv[3]) : 0;
            const char *no_workers[2] = {"", ""};
            return run_coordinator(&j, no_workers, max_rounds);
        }

        if (argc < 5) { fprintf(stderr, "coord needs <job_type> <w1_ip> <w2_ip>\n"); return 1; }
        const char *ips[2] = { argv[3], argv[4] };
        int max_rounds = argc > 5 ? atoi(argv[5]) : 0;
        return run_coordinator(&j, ips, max_rounds);
    } else if (strcmp(argv[1], "worker") == 0) {
        if (argc < 5) { fprintf(stderr, "worker needs <job_type> <slot> <coord_ip>\n"); return 1; }
        JobSpec j = make_job(argv[2], 0, NULL);
        int slot = atoi(argv[3]);
        return run_worker(&j, slot, argv[4]);
    }
    fprintf(stderr, "unknown role '%s'\n", argv[1]);
    return 1;
}
