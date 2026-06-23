/*
 * tc_shaper.c — Kuramoto phase → tc HTB rate modulator for Mint WAN egress
 *
 * Subscribes to AxisPulse multicast (239.0.0.2:7404).
 * Maps θ → outbound tc rate on enp0s31f6:
 *   trough (θ≈0)  → RATE_MIN
 *   peak   (θ≈π)  → RATE_MAX
 *   rate = RATE_MIN + (RATE_MAX - RATE_MIN) * (1 - cosθ) / 2
 *
 * Sets up HTB qdisc on startup, tears it down on exit.
 * Only applies tc change when rate step changes (hysteresis via N_STEPS).
 *
 * Usage: sudo ./tc_shaper [iface] [min_mbit] [max_mbit]
 *        defaults: enp0s31f6  50  800
 */

#include <endian.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

#define AXIS_PORT   7404
#define AXIS_GRP    "239.0.0.2"
#define AXIS_MAGIC  0x4158

#define TC_BIN      "/usr/sbin/tc"
#define N_STEPS     16      /* quantise sine into 16 steps to reduce tc calls */

typedef struct __attribute__((packed)) {
    uint16_t magic; uint8_t sid; uint8_t locked; uint32_t tick;
    float theta1; float theta2; float pd; float pd_dev;
    float load_avg; uint16_t drains; uint64_t t0_ns;
} AxisPulse;  /* 38 bytes */

static inline float ntohf(float f) {
    uint32_t u; memcpy(&u, &f, 4); u = ntohl(u); memcpy(&f, &u, 4); return f;
}

static char g_iface[32] = "enp0s31f6";
static int  g_teardown  = 0;

static void tc_run(const char *cmd) {
    int r = system(cmd); (void)r;
}

static void tc_setup(long max_mbit) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
        "%s qdisc replace dev %s root handle 1: htb default 10", TC_BIN, g_iface);
    tc_run(cmd);
    snprintf(cmd, sizeof(cmd),
        "%s class replace dev %s parent 1: classid 1:10 htb rate %ldmbit ceil %ldmbit burst 1500k quantum 1514",
        TC_BIN, g_iface, max_mbit, max_mbit);
    tc_run(cmd);
}

static void tc_set_rate(long mbit) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
        "%s class change dev %s classid 1:10 htb rate %ldmbit ceil %ldmbit burst 1500k quantum 1514",
        TC_BIN, g_iface, mbit, mbit);
    tc_run(cmd);
}

static void tc_teardown(void) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd), "%s qdisc del dev %s root 2>/dev/null", TC_BIN, g_iface);
    tc_run(cmd);
}

static void sig_handler(int s) {
    (void)s;
    if (!g_teardown) {
        g_teardown = 1;
        fprintf(stderr, "\n[tc_shaper] teardown %s\n", g_iface);
        tc_teardown();
    }
    exit(0);
}

int main(int argc, char **argv) {
    long rate_min = 50;
    long rate_max = 800;

    if (argc > 1) snprintf(g_iface, sizeof(g_iface), "%s", argv[1]);
    if (argc > 2) rate_min = atol(argv[2]);
    if (argc > 3) rate_max = atol(argv[3]);

    signal(SIGINT,  sig_handler);
    signal(SIGTERM, sig_handler);

    /* axis multicast socket */
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
    struct sockaddr_in addr = {
        .sin_family = AF_INET, .sin_port = htons(AXIS_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY)
    };
    bind(fd, (struct sockaddr *)&addr, sizeof(addr));
    struct ip_mreq mreq;
    inet_pton(AF_INET, AXIS_GRP, &mreq.imr_multiaddr);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

    tc_setup(rate_max);
    fprintf(stderr, "[tc_shaper] %s  %ldmbit–%ldmbit  steps=%d\n",
            g_iface, rate_min, rate_max, N_STEPS);

    AxisPulse ap;
    int      cur_step  = -1;
    long     step_size = (rate_max - rate_min) / N_STEPS;
    uint64_t t0_ns     = 0;

    while (1) {
        ssize_t n = recv(fd, &ap, sizeof(ap), 0);
        if (n != sizeof(ap)) continue;
        if (ntohs(ap.magic) != AXIS_MAGIC) continue;

        if (ap.t0_ns) t0_ns = be64toh(ap.t0_ns);
        uint64_t utc_ns = t0_ns
            ? t0_ns + (uint64_t)ntohl(ap.tick) * 50000000ULL : 0;
        (void)utc_ns;

        int locked = ap.locked;
        double theta = ntohf(ap.theta1);

        double frac = (1.0 - cos(theta)) / 2.0;   /* 0=trough, 1=peak */
        int step = (int)(frac * N_STEPS);
        if (step > N_STEPS) step = N_STEPS;

        if (step != cur_step) {
            long rate = locked
                ? rate_min + step * step_size
                : rate_max;
            tc_set_rate(rate);
            cur_step = step;

            fprintf(stderr, "\r[tc_shaper] θ=%.3f  frac=%.3f  rate=%4ldmbit  %s    ",
                    theta, frac, rate, locked ? "LOCKED" : "hunting");
        }
    }
}
