#define _GNU_SOURCE
/*
 * reader.c — passive Kuramoto phase reader for Mint
 *
 * Listens to Pi + Pi2 beacons. Never transmits into the oscillator.
 * Detects the Pi↔Pi2 drain crossing, fires a local tc burst window,
 * and optionally sends a one-way WAN telemetry pulse on each crossing.
 *
 * Usage: sudo ./reader [wan_ip] [wan_port]
 *   wan_ip    destination for phase-crossing telemetry (optional)
 *   wan_port  UDP port (default 7402)
 *
 * Telemetry packet (18 bytes, network byte order):
 *   uint16_t magic   = 0x5257  ("RW")
 *   uint32_t tick    — Pi1 tick at crossing
 *   float    theta   — Pi1 phase at crossing
 *   float    omega   — Pi1 omega at crossing
 *   float    pd      — phase diff at crossing
 *   uint16_t drains  — drain count
 */

#include <arpa/inet.h>
#include <inttypes.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MCAST_GRP    "239.0.0.1"
#define PORT         7400
#define MAGIC        0x1B4A

#define WAN_MAGIC     0x5257
#define WAN_PORT_DEF  7402

#define PHASE_TARGET  M_PI
#define ANTI_THRESH   0.20
#define LOCK_WINDOW   20
#define LOCK_STD      0.10

#define TC_BIN    "/usr/sbin/tc"
#define TC_DEV    "enp0s31f6"
#define TC_CLASS  "1:20"
#define TC_RATE   "20mbit"

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t  sid;
    uint32_t tick;
    float    theta;
    float    omega;
    uint8_t  _pad;
} Beacon;

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

static float htonf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = htonl(n);
    float r; memcpy(&r, &n, 4); return r;
}

static void tc_run(const char *cmd) { int r = system(cmd); (void)r; }

static uint64_t read_tx_bytes(void) {
    FILE *f = fopen("/proc/net/dev", "r");
    if (!f) return 0;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (strstr(line, TC_DEV)) {
            /* fields: iface rx_bytes ... (8 rx fields) tx_bytes ... */
            char iface[32]; uint64_t v[16];
            if (sscanf(line, " %31[^:]: %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                              " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                              " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                              " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64,
                       iface, &v[0],&v[1],&v[2],&v[3],&v[4],&v[5],&v[6],&v[7],
                       &v[8],&v[9],&v[10],&v[11],&v[12],&v[13],&v[14],&v[15]) == 17) {
                fclose(f);
                return v[8];  /* tx_bytes is 9th value (index 8) */
            }
        }
    }
    fclose(f);
    return 0;
}

static void tc_setup(void) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
             "%s qdisc replace dev %s root handle 1: htb default 20",
             TC_BIN, TC_DEV);
    tc_run(cmd);
    snprintf(cmd, sizeof(cmd),
             "%s class replace dev %s parent 1: classid %s htb rate %s burst 64k quantum 1514",
             TC_BIN, TC_DEV, TC_CLASS, TC_RATE);
    tc_run(cmd);
}

static void tc_burst(const char *burst) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
             "%s class change dev %s classid %s htb rate %s burst %s quantum 1514",
             TC_BIN, TC_DEV, TC_CLASS, TC_RATE, burst);
    tc_run(cmd);
}

static void drain_async(void) {
    if (fork() == 0) {
        tc_burst("500k");
        usleep(200000);
        tc_burst("64k");
        _exit(0);
    }
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGCHLD, SIG_IGN);  /* auto-reap drain_async children */

    /* optional WAN telemetry destination */
    int wan_fd = -1;
    struct sockaddr_in wan_addr = {0};
    if (argc >= 2) {
        int port = (argc >= 3) ? atoi(argv[2]) : WAN_PORT_DEF;
        wan_fd = socket(AF_INET, SOCK_DGRAM, 0);
        wan_addr.sin_family      = AF_INET;
        wan_addr.sin_port        = htons((uint16_t)port);
        wan_addr.sin_addr.s_addr = inet_addr(argv[1]);
        printf("[reader] WAN telemetry → %s:%d\n", argv[1], port);
    }

    /* local phase_sched feed — always sends to 127.0.0.1:7403 */
    int sched_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in sched_addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(7403),
        .sin_addr.s_addr = inet_addr("127.0.0.1")
    };

    int rx = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(rx, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr = {.sin_family=AF_INET, .sin_port=htons(PORT),
                               .sin_addr.s_addr=INADDR_ANY};
    bind(rx, (struct sockaddr *)&addr, sizeof(addr));
    struct ip_mreq mreq = {.imr_multiaddr.s_addr=inet_addr(MCAST_GRP),
                            .imr_interface.s_addr=INADDR_ANY};
    setsockopt(rx, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

    /* 1ms poll timeout */
    struct timeval tv = {.tv_sec=0, .tv_usec=1000};
    setsockopt(rx, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    double phases[3] = {-1.0, -1.0, -1.0};  /* index 1=Pi, 2=Pi2 */
    struct timespec last_seen[3] = {{0},{0},{0}};
    uint64_t prev_tx_bytes = 0;
    struct timespec prev_tx_ts = {0};
    double history[LOCK_WINDOW];
    int    hist_n = 0, hist_full = 0;
    double prev_diff = -1.0;
    int    drains = 0;
    struct timespec last_drain_ts = {0};
    /* last seen Pi1 telemetry for WAN pulse */
    uint32_t pi1_tick = 0;
    float    pi1_omega = 0.0f;

    tc_setup();
    memset(history, 0, sizeof(history));
    printf("[reader] passive — listening to Pi(1) + Pi2(2)\n");

    while (1) {
        Beacon pkt;
        struct sockaddr_in src;
        socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(rx, &pkt, sizeof(pkt), 0,
                             (struct sockaddr *)&src, &slen);
        if (n != sizeof(pkt)) continue;
        if (ntohs(pkt.magic) != MAGIC) continue;
        int sid = pkt.sid;
        if (sid != 1 && sid != 2) continue;

        phases[sid] = ntohf(pkt.theta);
        clock_gettime(CLOCK_MONOTONIC, &last_seen[sid]);
        if (sid == 1) { pi1_tick = ntohl(pkt.tick); pi1_omega = ntohf(pkt.omega); }

        if (phases[1] < 0 || phases[2] < 0) continue;

        double diff = phases[1] - phases[2];
        double phase_diff = fabs(fmod(diff + M_PI, 2*M_PI) - M_PI);

        history[hist_n % LOCK_WINDOW] = phase_diff;
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
                      fabs(phase_diff - PHASE_TARGET) < ANTI_THRESH);

        /* send every tick to local phase_sched on 7403 — only when both beacons fresh */
        {
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            long age1 = (now.tv_sec - last_seen[1].tv_sec) * 1000
                      + (now.tv_nsec - last_seen[1].tv_nsec) / 1000000;
            long age2 = (now.tv_sec - last_seen[2].tv_sec) * 1000
                      + (now.tv_nsec - last_seen[2].tv_nsec) / 1000000;
            if (age1 <= 500 && age2 <= 500) {            WanPulse wp;
            wp.magic  = htons(WAN_MAGIC);
            wp.tick   = htonl(pi1_tick);
            wp.theta  = htonf((float)phases[1]);
            wp.omega  = htonf(pi1_omega);
            wp.pd     = htonf((float)phase_diff);
            wp.drains = htons((uint16_t)drains);
            sendto(sched_fd, &wp, sizeof(wp), 0,
                   (struct sockaddr *)&sched_addr, sizeof(sched_addr));
            }
        }

        struct timespec _now; clock_gettime(CLOCK_MONOTONIC, &_now);
        long _since_drain = (_now.tv_sec - last_drain_ts.tv_sec) * 1000
                          + (_now.tv_nsec - last_drain_ts.tv_nsec) / 1000000;
        if (prev_diff >= 0 && prev_diff > PHASE_TARGET && phase_diff <= PHASE_TARGET
            && _since_drain > 3000) {
            last_drain_ts = _now;
            drain_async();
            drains++;
            if (wan_fd >= 0) {
                WanPulse wp;
                wp.magic  = htons(WAN_MAGIC);
                wp.tick   = htonl(pi1_tick);
                wp.theta  = htonf((float)phases[1]);
                wp.omega  = htonf(pi1_omega);
                wp.pd     = htonf((float)phase_diff);
                wp.drains = htons((uint16_t)drains);
                sendto(wan_fd, &wp, sizeof(wp), 0,
                       (struct sockaddr *)&wan_addr, sizeof(wan_addr));
            }
        }
        prev_diff = phase_diff;

        /* TX rate */
        double tx_mbps = 0.0;
        {
            struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
            uint64_t tx = read_tx_bytes();
            if (prev_tx_bytes && prev_tx_ts.tv_sec) {
                double dt = (now.tv_sec - prev_tx_ts.tv_sec)
                          + (now.tv_nsec - prev_tx_ts.tv_nsec) * 1e-9;
                if (dt > 0.0) tx_mbps = (double)(tx - prev_tx_bytes) * 8.0 / 1e6 / dt;
            }
            prev_tx_bytes = tx;
            prev_tx_ts    = now;
        }

        int bar = (int)(phase_diff / M_PI * 39);
        printf("\r[reader] φ=%.3f  ", phase_diff);
        for (int i=0;i<bar;i++) printf("█");
        for (int i=bar;i<40;i++) printf("░");
        printf("  %s  drains=%d  up=%.1fMbps   ",
               locked ? "LOCKED" : "      ", drains, tx_mbps);
        fflush(stdout);
    }
}
