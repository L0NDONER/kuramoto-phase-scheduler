#define _GNU_SOURCE
/*
 * phase_sched.c — thundering herd suppressor via Kuramoto phase signal
 *
 * Reads WAN phase-crossing pulses (UDP 7402), tracks θ each tick,
 * emits SUBMIT at trough (θ < TROUGH_THRESH) and COLLECT at peak
 * (θ > PEAK_THRESH) to a named pipe. One signal per window per cycle.
 *
 * Consumers: read /tmp/phase_sched (blocks until signal arrives)
 *
 * Usage: ./phase_sched [port] [pipe_path]
 *   port       default 7402
 *   pipe_path  default /tmp/phase_sched
 *
 * Signal format (one line per event):
 *   SUBMIT <tick> <theta> <pd> <cycle>
 *   COLLECT <tick> <theta> <pd> <cycle>
 */

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define WAN_MAGIC      0x5257
#define WAN_PORT_DEF   7402
#define PIPE_PATH_DEF  "/tmp/phase_sched"

/* trough: θ < TROUGH_THRESH, peak: θ > PEAK_THRESH */
#define TROUGH_THRESH  0.30   /* rad from 0 */
#define PEAK_THRESH    2.84   /* rad — π - 0.30 */

/* minimum pd quality to emit signals */
#define MIN_PD         2.50   /* only signal when phase diff near π */

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

static volatile int running = 1;
static void sig_handler(int s) { (void)s; running = 0; }

static int pipe_fd = -1;

static void emit(const char *event, uint32_t tick, float theta,
                 float pd, long cycle) {
    if (pipe_fd < 0) return;
    char buf[128];
    int n = snprintf(buf, sizeof(buf), "%s %u %.4f %.4f %ld\n",
                     event, tick, theta, pd, cycle);
    /* non-blocking write — drop if no reader */
    int w = write(pipe_fd, buf, (size_t)n); (void)w;
    printf("[sched] %s  tick=%-8u  θ=%.4f  pd=%.4f  cycle=%ld\n",
           event, tick, theta, pd, cycle);
    fflush(stdout);
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGINT,  sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGPIPE, SIG_IGN);

    int port          = (argc > 1) ? atoi(argv[1]) : WAN_PORT_DEF;
    const char *ppath = (argc > 2) ? argv[2] : PIPE_PATH_DEF;

    /* create named pipe if missing */
    if (mkfifo(ppath, 0666) < 0 && errno != EEXIST) {
        perror("mkfifo"); return 1;
    }
    /* open non-blocking so we don't block waiting for a reader */
    pipe_fd = open(ppath, O_WRONLY | O_NONBLOCK);
    if (pipe_fd < 0 && errno != ENXIO) {
        perror("open pipe"); return 1;
    }

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr = {.sin_family=AF_INET,
                               .sin_port=htons((uint16_t)port),
                               .sin_addr.s_addr=INADDR_ANY};
    bind(fd, (struct sockaddr *)&addr, sizeof(addr));

    /* 100ms recv timeout so we can recheck running flag */
    struct timeval tv = {.tv_sec=0, .tv_usec=100000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    printf("[sched] pipe=%s  port=%d  trough<%.2f  peak>%.2f\n",
           ppath, port, TROUGH_THRESH, PEAK_THRESH);

    int    in_trough = 0, in_peak = 0;
    long   cycle = 0;
    float  prev_theta = -1.0f;

    while (running) {
        WanPulse pkt;
        struct sockaddr_in src;
        socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(fd, &pkt, sizeof(pkt), 0,
                             (struct sockaddr *)&src, &slen);
        if (n != sizeof(pkt)) continue;
        if (ntohs(pkt.magic) != WAN_MAGIC) continue;

        uint32_t tick  = ntohl(pkt.tick);
        float    theta = ntohf(pkt.theta);
        float    pd    = ntohf(pkt.pd);

        /* reopen pipe if reader connected since last attempt */
        if (pipe_fd < 0) {
            pipe_fd = open(ppath, O_WRONLY | O_NONBLOCK);
        }

        /* detect cycle boundary: theta wrapped 2π→0 */
        if (prev_theta > M_PI && theta < 1.0f) cycle++;
        prev_theta = theta;

        if (pd < MIN_PD) { in_trough = 0; in_peak = 0; continue; }

        /* trough window */
        if (theta < TROUGH_THRESH && !in_trough) {
            in_trough = 1;
            in_peak   = 0;
            emit("SUBMIT ", tick, theta, pd, cycle);
        } else if (theta >= TROUGH_THRESH) {
            in_trough = 0;
        }

        /* peak window */
        if (theta > PEAK_THRESH && theta < M_PI + 0.30f && !in_peak) {
            in_peak   = 1;
            in_trough = 0;
            emit("COLLECT", tick, theta, pd, cycle);
        } else if (theta < PEAK_THRESH) {
            in_peak = 0;
        }
    }

    close(fd);
    if (pipe_fd >= 0) close(pipe_fd);
    printf("\n[sched] stopped\n");
    return 0;
}
