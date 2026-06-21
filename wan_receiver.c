#define _GNU_SOURCE
/*
 * wan_receiver.c — decodes one-way phase-crossing pulses from reader.c
 *
 * Usage: ./wan_receiver [port]   (default 7402)
 *
 * Packet format (18 bytes, network byte order):
 *   uint16_t magic   = 0x5257
 *   uint32_t tick    — Pi1 tick at crossing
 *   float    theta   — Pi1 phase at crossing
 *   float    omega   — Pi1 omega at crossing
 *   float    pd      — phase diff at crossing
 *   uint16_t drains  — drain count
 */

#include <arpa/inet.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define WAN_MAGIC    0x5257
#define WAN_PORT_DEF 7402

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

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);

    int port = (argc > 1) ? atoi(argv[1]) : WAN_PORT_DEF;

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
    struct sockaddr_in addr = {.sin_family=AF_INET, .sin_port=htons((uint16_t)port),
                               .sin_addr.s_addr=INADDR_ANY};
    bind(fd, (struct sockaddr *)&addr, sizeof(addr));

    printf("[wan_receiver] listening on :%d\n", port);
    printf("%-26s  %8s  %7s  %7s  %6s  %6s  %8s\n",
           "time", "tick", "theta", "omega", "pd", "drains", "interval");

    uint32_t last_tick = 0;
    struct timespec last_ts = {0};
    long pulse = 0;

    while (1) {
        WanPulse pkt;
        struct sockaddr_in src;
        socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(fd, &pkt, sizeof(pkt), 0,
                             (struct sockaddr *)&src, &slen);
        if (n != sizeof(pkt)) continue;
        if (ntohs(pkt.magic) != WAN_MAGIC) continue;

        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);

        uint32_t tick   = ntohl(pkt.tick);
        float    theta  = ntohf(pkt.theta);
        float    omega  = ntohf(pkt.omega);
        float    pd     = ntohf(pkt.pd);
        uint16_t drains = ntohs(pkt.drains);

        /* wall time */
        char tbuf[32];
        struct tm *tm = localtime(&ts.tv_sec);
        strftime(tbuf, sizeof(tbuf), "%Y-%m-%d %H:%M:%S", tm);
        snprintf(tbuf + 19, sizeof(tbuf) - 19, ".%03d", (int)(ts.tv_nsec / 1000000));

        /* interval between pulses */
        double interval = -1.0;
        if (last_ts.tv_sec > 0)
            interval = (ts.tv_sec - last_ts.tv_sec) +
                       (ts.tv_nsec - last_ts.tv_nsec) * 1e-9;

        if (interval >= 0)
            printf("%-26s  %8u  %7.4f  %7.5f  %6.4f  %6u  %7.3fs\n",
                   tbuf, tick, theta, omega, pd, drains, interval);
        else
            printf("%-26s  %8u  %7.4f  %7.5f  %6.4f  %6u  %8s\n",
                   tbuf, tick, theta, omega, pd, drains, "---");

        last_tick = tick;
        last_ts   = ts;
        pulse++;
        (void)last_tick;
    }
}
