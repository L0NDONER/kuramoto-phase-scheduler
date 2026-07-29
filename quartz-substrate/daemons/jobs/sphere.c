/**
 * jobs/sphere.c — demo dlopen job plugin for quartz_metro_node.
 *
 * Proves the actual claim behind the dlopen work: a NEW job type can be
 * added to an already-built, already-deployed quartz_metro_node WITHOUT
 * recompiling that binary. Build this .so, drop it in ./jobs/, then
 *   ./quartz_metro_node coord sphere
 * picks it up. quartz_metro_node itself is untouched and unrecompiled.
 *
 * Deliberately trivial energy so correctness is a glance, not an audit:
 * minimize sum of squares of 3 local weights, no workers -- converges to
 * ~0 from any random init.
 *
 * Build: gcc -O2 -shared -fPIC -o jobs/sphere.so jobs/sphere.c -lm
 */
#include "../quartz_job.h"
#include <stdio.h>

static double sphere_energy(const double *w, void *ctx) {
    (void)ctx;
    return w[0]*w[0] + w[1]*w[1] + w[2]*w[2];
}

static void sphere_round_end(double t, const double *w, double e) {
    printf("[sphere] t=%.3f w=(%.4f,%.4f,%.4f) e=%.6f\n", t, w[0], w[1], w[2], e);
}

int quartz_job_init(int argc, char **argv, JobSpec *out) {
    (void)argc; (void)argv;
    JobSpec j = {0};
    j.name = "sphere";
    j.n_weights = 3; j.n_local = 3; j.n_workers = 0;
    j.energy_fn = sphere_energy;
    j.continuous = 0;
    j.n_restarts = 3; j.steps_each = 2000;
    j.t0 = 1.0; j.cool = 0.998; j.step_sigma = 0.2; j.init_range = 5.0;
    j.on_round_end = sphere_round_end;
    j.coord_port = 5010; j.worker_port = 5011;   // unused (n_workers=0); outside builtin ranges
    *out = j;
    return 0;
}
