/**
 * quartz_job.h — shared ABI between quartz_metro_node.c and job plugins.
 *
 * A plugin .so must export exactly one symbol:
 *
 *   int quartz_job_init(int argc, char **argv, JobSpec *out);
 *
 * argc/argv are whatever's left on the command line after `coord
 * <job_type>` (workers get argc=0/argv=NULL -- a job's non-network
 * parameters belong on the coordinator's invocation, same convention as
 * built-in cellstate's landscape-path arg). Return 0 on success and fill
 * *out completely; nonzero aborts startup with the plugin's own stderr
 * message already printed.
 *
 * energy_fn/on_round_end/static_ctx pointers must stay valid for the life
 * of the process -- quartz_metro_node never dlclose()s a loaded plugin.
 *
 * This struct's layout IS the ABI. Don't reorder or resize fields without
 * rebuilding every plugin against the new header.
 */
#ifndef QUARTZ_JOB_H
#define QUARTZ_JOB_H

typedef double (*EnergyFn)(const double *w, void *ctx);
typedef void (*RoundEndFn)(double t, const double *w, double e);

typedef struct {
    const char *name;
    int n_weights;             // total weights across coordinator+workers
    int n_local;                // weights the coordinator itself owns
    int n_workers;
    int worker_ids[2][2];       // worker_ids[slot][0..1], id_b = -1 if single
    EnergyFn energy_fn;
    void *static_ctx;           // fixed ctx for energy_fn, or NULL
    int poll_market;            // 1 = re-read /tmp/market_state.json each round
    int continuous;             // 1 = runs forever, init once, carry weights across rounds
    int n_restarts;             // used only when !continuous
    int steps_each;
    double t0, cool, step_sigma, init_range;
    int has_init_w;              // 1 = use init_w instead of uniform_range for first init
    double init_w[6];
    RoundEndFn on_round_end;     // optional, called after each round
    int coord_port, worker_port;
    int is_plugin;               // set by the host after a successful dlopen
                                  // load, not by the plugin itself -- for
                                  // the job registry, purely informational
} JobSpec;

typedef int (*quartz_job_init_fn)(int argc, char **argv, JobSpec *out);

#endif
