/*
 * cpu_reader.c — Kuramoto phase → CPU DVFS modulator
 *
 * Listens on 7403 for per-tick WanPulse from reader.c.
 * When LOCKED, maps θ to both frequency and voltage simultaneously:
 *   f(θ) = f_min + (f_max - f_min) * (1 - cos θ) / 2
 *   V(θ) = V_OFFSET_MV * (1 + cos θ) / 2   (most negative at trough θ=0)
 *
 * Voltage via Intel OC Mailbox MSR 0x150 (Skylake+, plane 0 = CPU core).
 * Requires: modprobe msr, msr-tools (wrmsr).
 *
 * Sets governor to 'userspace' on start, restores V=0 + original governor
 * on SIGINT/SIGTERM.
 *
 * Usage: sudo ./cpu_reader [port]   (default port 7403)
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define WAN_MAGIC     0x5257
#define DEF_PORT      7403

#define PHASE_TARGET    M_PI
#define ANTI_THRESH     0.20
#define LOCK_WINDOW     20
#define LOCK_STD        0.10

/* Voltage offset at trough (mV, negative = undervolt).
 * 0 disables voltage scaling. Start conservative; push lower if stable. */
#define V_OFFSET_MV     (-50)

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint32_t tick;
    float    theta;
    float    omega;
    float    pd;
    uint16_t drains;
} WanPulse;

static float ntohf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = ntohl(n);
    float r; memcpy(&r, &n, 4); return r;
}

/* ── cpufreq helpers ─────────────────────────────────────────────────────── */

static int  n_cpus = 0;
static char cpu_paths[64][320];   /* scaling_setspeed paths */
static char gov_paths[64][320];   /* scaling_governor paths */
static char saved_gov[64][64];    /* governor before we started */
static long f_min = 0, f_max = 0;

static void cpufreq_discover(void) {
    DIR *d = opendir("/sys/devices/system/cpu");
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d)) && n_cpus < 64) {
        if (strncmp(e->d_name, "cpu", 3) != 0) continue;
        if (e->d_name[3] < '0' || e->d_name[3] > '9') continue;
        snprintf(cpu_paths[n_cpus], sizeof(cpu_paths[0]),
                 "/sys/devices/system/cpu/%.16s/cpufreq/scaling_setspeed", e->d_name);
        snprintf(gov_paths[n_cpus], sizeof(gov_paths[0]),
                 "/sys/devices/system/cpu/%.16s/cpufreq/scaling_governor", e->d_name);
        /* save current governor */
        FILE *f = fopen(gov_paths[n_cpus], "r");
        if (f) {
            if (fgets(saved_gov[n_cpus], sizeof(saved_gov[0]), f)) {
                saved_gov[n_cpus][strcspn(saved_gov[n_cpus], "\n")] = '\0';
            }
            fclose(f);
        }
        n_cpus++;
    }
    closedir(d);

    /* read min/max from cpu0 */
    FILE *f;
    f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq", "r");
    if (f) { char buf[32]; if (fgets(buf, sizeof(buf), f)) f_min = atol(buf); fclose(f); }
    f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r");
    if (f) { char buf[32]; if (fgets(buf, sizeof(buf), f)) f_max = atol(buf); fclose(f); }
}

static void set_governor(const char *gov) {
    for (int i = 0; i < n_cpus; i++) {
        FILE *f = fopen(gov_paths[i], "w");
        if (f) { fprintf(f, "%s\n", gov); fclose(f); }
    }
}

static void set_freq(long khz) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%ld\n", khz);
    for (int i = 0; i < n_cpus; i++) {
        FILE *f = fopen(cpu_paths[i], "w");
        if (f) { fputs(buf, f); fclose(f); }
    }
}

static void restore_governors(void) {
    for (int i = 0; i < n_cpus; i++) {
        FILE *f = fopen(gov_paths[i], "w");
        if (f) { fprintf(f, "%s\n", saved_gov[i]); fclose(f); }
    }
}

/* ── voltage helpers (Intel OC Mailbox, MSR 0x150, plane 0 = CPU core) ───── */

static void set_voltage_offset(int mv) {
    if (mv == 0 && V_OFFSET_MV == 0) return;
    int v = (int)round(mv * 1.024);
    if (v < 0) v += 2048;
    v &= 0x7FF;
    unsigned long long msr = 0x80000011ULL | ((unsigned long long)v << 21);
    char cmd[128];
    for (int i = 0; i < n_cpus; i++) {
        snprintf(cmd, sizeof(cmd), "wrmsr -p %d 0x150 0x%llx", i, msr);
        int r = system(cmd); (void)r;
    }
}

/* ── signal handling ─────────────────────────────────────────────────────── */

static volatile int running = 1;
static void on_signal(int s) { (void)s; running = 0; }

/* ── main ────────────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);

    int port = (argc >= 2) ? atoi(argv[1]) : DEF_PORT;

    cpufreq_discover();
    if (n_cpus == 0 || f_min == 0 || f_max == 0) {
        fprintf(stderr, "[cpu_reader] cpufreq not available\n");
        return 1;
    }
    printf("[cpu_reader] %d CPUs  f_min=%ldkHz  f_max=%ldkHz\n",
           n_cpus, f_min, f_max);

    set_governor("userspace");
    set_freq(f_max);
    set_voltage_offset(0);
    printf("[cpu_reader] governor → userspace, V=0mV, starting at %ldkHz\n", f_max);

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons((uint16_t)port),
        .sin_addr.s_addr = INADDR_ANY
    };
    bind(fd, (struct sockaddr *)&addr, sizeof(addr));

    struct timeval tv = {.tv_sec=0, .tv_usec=100000};  /* 100ms timeout */
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    printf("[cpu_reader] listening on :%d\n", port);

    double history[LOCK_WINDOW];
    int    hist_n = 0, hist_full = 0;
    memset(history, 0, sizeof(history));

    long   cur_khz = f_max;
    int    cur_mv  = 0;
    int    locked_prev = 0;
    struct timespec last_pkt = {0};

    while (running) {
        WanPulse wp;
        ssize_t n = recv(fd, &wp, sizeof(wp), 0);
        if (n != sizeof(wp)) continue;
        if (ntohs(wp.magic) != WAN_MAGIC) continue;

        clock_gettime(CLOCK_MONOTONIC, &last_pkt);

        float theta = ntohf(wp.theta);
        float pd    = ntohf(wp.pd);

        history[hist_n % LOCK_WINDOW] = (double)pd;
        hist_n++;
        if (hist_n >= LOCK_WINDOW) hist_full = 1;

        int cnt = hist_full ? LOCK_WINDOW : hist_n;
        double mean = 0;
        for (int i = 0; i < cnt; i++) mean += history[i];
        mean /= cnt;
        double var = 0;
        for (int i = 0; i < cnt; i++) var += (history[i]-mean)*(history[i]-mean);
        double std = sqrt(var / cnt);

        int locked = (hist_full &&
                      std < LOCK_STD &&
                      fabs((double)pd - PHASE_TARGET) < ANTI_THRESH);

        if (locked) {
            /* f = f_min + (f_max - f_min) * (1 - cos θ) / 2  [trough=min, peak=max] */
            double frac = (1.0 - cos((double)theta)) / 2.0;
            cur_khz = f_min + (long)((double)(f_max - f_min) * frac);
            set_freq(cur_khz);
            /* V = V_OFFSET_MV * (1 + cos θ) / 2  [trough=V_OFFSET_MV, peak=0] */
            cur_mv = (int)(V_OFFSET_MV * (1.0 + cos((double)theta)) / 2.0);
            set_voltage_offset(cur_mv);
        } else {
            if (locked_prev) {
                /* just lost lock — restore stock */
                cur_khz = f_max;
                cur_mv  = 0;
                set_freq(cur_khz);
                set_voltage_offset(0);
            }
        }
        locked_prev = locked;

        printf("\r[cpu_reader] θ=%.3f  pd=%.3f  %s  %ldkHz  %+dmV   ",
               theta, pd,
               locked ? "LOCKED" : "      ",
               cur_khz, cur_mv);
        fflush(stdout);
    }

    printf("\n[cpu_reader] restoring stock voltage + governors...\n");
    set_voltage_offset(0);
    set_freq(f_max);
    restore_governors();
    close(fd);
    return 0;
}
