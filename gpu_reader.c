#define _GNU_SOURCE
/*
 * gpu_reader.c — Kuramoto phase follower with NVML power cap enforcement
 *
 * Listens to Pi + Pi2 beacons. Evolves a local Kuramoto oscillator.
 * When locked, sets GPU power limit to T(theta) via NVML each tick.
 * When lock is lost, restores full TDP.
 *
 * Usage: ./gpu_reader [target_sid] [gpu_index]   (defaults: 1, 0)
 *
 * Compile without NVML (logging only):
 *   gcc -O2 -Wall -Wextra -o gpu_reader gpu_reader.c -lm
 *
 * Compile with NVML:
 *   gcc -O2 -Wall -Wextra -DUSE_NVML -o gpu_reader gpu_reader.c -lm -lnvidia-ml
 */

#include <arpa/inet.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#ifdef USE_NVML
#include <nvml.h>
#endif

#define MCAST_GRP    "239.0.0.1"
#define PORT         7400
#define MAGIC        0x1B4A

#define OMEGA        0.052
#define K_COUPLE     0.30
#define ANTI_THRESH  0.20
#define LOCK_WINDOW  20
#define LOCK_STD     0.10

#define GPU_IDLE_W   500.0   /* H100 SXM floor under ML workload */
#define GPU_DELTA_W  500.0   /* swing to 1000W TDP peak */

/* only update power limit if change exceeds this — avoid hammering NVML */
#define NVML_CHANGE_THRESH_W  5.0

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t  sid;
    uint32_t tick;
    float    theta;
    float    omega;
    uint8_t  _pad;
} Beacon;

#ifdef USE_NVML
static nvmlDevice_t g_dev;
static unsigned int g_max_limit_mw = 0;
static unsigned int g_min_limit_mw = 0;
static double       g_last_set_w   = -1.0;

static void nvml_restore(void) {
    if (g_max_limit_mw > 0) {
        nvmlDeviceSetPowerManagementLimit(g_dev, g_max_limit_mw);
        printf("\n[nvml] restored TDP limit %.0fW\n", g_max_limit_mw / 1000.0);
    }
}

static void sig_handler(int s) { (void)s; nvml_restore(); nvmlShutdown(); _exit(0); }

static void nvml_set_limit(double watts) {
    if (fabs(watts - g_last_set_w) < NVML_CHANGE_THRESH_W) return;
    unsigned int mw = (unsigned int)(watts * 1000.0);
    if (mw < g_min_limit_mw) mw = g_min_limit_mw;
    if (mw > g_max_limit_mw) mw = g_max_limit_mw;
    nvmlReturn_t r = nvmlDeviceSetPowerManagementLimit(g_dev, mw);
    if (r == NVML_SUCCESS) g_last_set_w = watts;
}
#else
static void sig_handler(int s) { (void)s; _exit(0); }
#endif

static float ntohf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = ntohl(n);
    float r; memcpy(&r, &n, 4); return r;
}

static double gpu_watts(double theta) {
    return GPU_IDLE_W + GPU_DELTA_W * (1.0 - cos(theta)) / 2.0;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);

    int target_sid = (argc > 1) ? atoi(argv[1]) : 1;
    int gpu_index  = (argc > 2) ? atoi(argv[2]) : 0;

    if (target_sid != 1 && target_sid != 2) {
        fprintf(stderr, "usage: %s [1|2] [gpu_index]\n", argv[0]);
        return 1;
    }

    signal(SIGINT,  sig_handler);
    signal(SIGTERM, sig_handler);

#ifdef USE_NVML
    nvmlReturn_t r = nvmlInit();
    if (r != NVML_SUCCESS) {
        fprintf(stderr, "[nvml] init failed: %s\n", nvmlErrorString(r));
        return 1;
    }
    r = nvmlDeviceGetHandleByIndex((unsigned int)gpu_index, &g_dev);
    if (r != NVML_SUCCESS) {
        fprintf(stderr, "[nvml] get device %d failed: %s\n", gpu_index, nvmlErrorString(r));
        nvmlShutdown(); return 1;
    }
    nvmlDeviceGetPowerManagementLimitConstraints(g_dev, &g_min_limit_mw, &g_max_limit_mw);
    char name[64]; nvmlDeviceGetName(g_dev, name, sizeof(name));
    printf("[nvml] %s  limits %.0f–%.0fW\n",
           name, g_min_limit_mw/1000.0, g_max_limit_mw/1000.0);
#endif

    int rx = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(rx, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr = {.sin_family=AF_INET, .sin_port=htons(PORT),
                               .sin_addr.s_addr=INADDR_ANY};
    bind(rx, (struct sockaddr *)&addr, sizeof(addr));
    struct ip_mreq mreq = {.imr_multiaddr.s_addr=inet_addr(MCAST_GRP),
                            .imr_interface.s_addr=INADDR_ANY};
    setsockopt(rx, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

    struct timeval tv = {.tv_sec=0, .tv_usec=1000};
    setsockopt(rx, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    double remote[3] = {-1.0, -1.0, -1.0};
    double theta     = 0.0;
    double history[LOCK_WINDOW];
    int    hist_n = 0, hist_full = 0;
    int    prev_locked = 0;
    long   tick = 0;

    memset(history, 0, sizeof(history));
    printf("[gpu_reader] follower → Pi%d  gpu=%d  idle=%.1fW  peak=%.1fW\n",
           target_sid, gpu_index, GPU_IDLE_W, GPU_IDLE_W + GPU_DELTA_W);

    while (1) {
        Beacon pkt;
        struct sockaddr_in src;
        socklen_t slen = sizeof(src);
        ssize_t n;
        while ((n = recvfrom(rx, &pkt, sizeof(pkt), 0,
                             (struct sockaddr *)&src, &slen)) == sizeof(pkt)) {
            if (ntohs(pkt.magic) != MAGIC) continue;
            int sid = pkt.sid;
            if (sid == 1 || sid == 2)
                remote[sid] = ntohf(pkt.theta);
        }

        /* Kuramoto step */
        if (remote[target_sid] >= 0.0) {
            double coupling = sin(remote[target_sid] - theta);
            theta = fmod(theta + OMEGA + K_COUPLE * coupling, 2.0 * M_PI);
            if (theta < 0) theta += 2.0 * M_PI;
        } else {
            theta = fmod(theta + OMEGA, 2.0 * M_PI);
        }

        double watts = gpu_watts(theta);

        /* lock detection */
        double pd = -1.0;
        if (remote[target_sid] >= 0.0) {
            double diff = remote[target_sid] - theta;
            pd = fabs(fmod(diff + M_PI, 2.0 * M_PI) - M_PI);
            history[hist_n % LOCK_WINDOW] = pd;
            hist_n++;
            if (hist_n >= LOCK_WINDOW) hist_full = 1;
        }

        int locked = 0;
        if (hist_full && pd >= 0.0) {
            int cnt = LOCK_WINDOW;
            double mean = 0;
            for (int i = 0; i < cnt; i++) mean += history[i];
            mean /= cnt;
            double var = 0;
            for (int i = 0; i < cnt; i++) var += (history[i]-mean)*(history[i]-mean);
            double std = sqrt(var / cnt);
            locked = (std < LOCK_STD && pd < ANTI_THRESH);
        }

#ifdef USE_NVML
        if (locked) {
            nvml_set_limit(watts);
        } else if (prev_locked && !locked) {
            /* lock lost — restore full TDP */
            nvml_set_limit(g_max_limit_mw / 1000.0);
            g_last_set_w = -1.0;
            printf("\n[nvml] lock lost — TDP restored\n");
        }
#endif

        (void)prev_locked;
        prev_locked = locked;

        int wbar = (int)(watts / (GPU_IDLE_W + GPU_DELTA_W) * 20);
        printf("\r[gpu%d] tick=%6ld  θ=%.3f  T=%6.1fW  ",
               target_sid, tick, theta, watts);
        for (int i = 0; i < wbar; i++)  printf("█");
        for (int i = wbar; i < 20; i++) printf("░");
        if (pd >= 0.0)
            printf("  pd=%.3f  %s   ", pd, locked ? "LOCKED" : "      ");
        else
            printf("  pd=---  waiting   ");
        fflush(stdout);

        tick++;
        usleep(50000);
    }
}
