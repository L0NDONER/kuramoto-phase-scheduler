#define _GNU_SOURCE
/*
 * reader.c — passive Kuramoto phase reader for Mint
 *
 * Listens to Pi + Pi2 beacons. Never transmits. Never couples.
 * Detects the Pi↔Pi2 drain crossing and fires a local tc burst window.
 *
 * Usage: sudo ./reader
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
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MCAST_GRP    "239.0.0.1"
#define PORT         7400
#define MAGIC        0x1B4A

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

static float ntohf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = ntohl(n);
    float r; memcpy(&r, &n, 4); return r;
}

static void tc_burst(const char *burst) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
             "%s class change dev %s classid %s htb rate %s burst %s",
             TC_BIN, TC_DEV, TC_CLASS, TC_RATE, burst);
    (void)system(cmd);
}

static void drain_async(void) {
    if (fork() == 0) {
        tc_burst("500k");
        usleep(200000);
        tc_burst("64k");
        _exit(0);
    }
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGCHLD, SIG_IGN);  /* auto-reap drain_async children */

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
    double history[LOCK_WINDOW];
    int    hist_n = 0, hist_full = 0;
    double prev_diff = -1.0;
    int    drains = 0;

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

        if (prev_diff >= 0 && prev_diff > PHASE_TARGET && phase_diff <= PHASE_TARGET) {
            drain_async();
            drains++;
        }
        prev_diff = phase_diff;

        int bar = (int)(phase_diff / M_PI * 39);
        printf("\r[reader] φ=%.3f  ", phase_diff);
        for (int i=0;i<bar;i++) printf("█");
        for (int i=bar;i<40;i++) printf("░");
        printf("  %s  drains=%d   ", locked ? "LOCKED" : "      ", drains);
        fflush(stdout);
    }
}
