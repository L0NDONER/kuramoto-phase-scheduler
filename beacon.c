/*
 * beacon.c — Kuramoto oscillator beacon with PI lock controller
 *
 * Usage: sudo ./beacon <pi|pi2>
 *
 * Packet format (network byte order, 24 bytes):
 *   uint16_t magic  = 0x1B4A
 *   uint8_t  sid    = 1 (pi) | 2 (pi2)
 *   uint32_t tick
 *   float    theta
 *   float    omega
 *   uint8_t  _pad
 *   uint64_t t0_ns  = CLOCK_REALTIME at tick 0 (ns since epoch); 0 on other ticks
 */

#define _GNU_SOURCE
#include <endian.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MCAST_GRP      "239.0.0.1"
#define PORT           7400
#define MAGIC          0x1B4A
#define TICK_NS        50000000L   /* 50 ms */

/* Tuned omega values */
#define OMEGA_PI       0.054
#define OMEGA_PI2      0.052

#define ANCHOR_INTERVAL 200   /* re-broadcast t0_ns every N ticks (~10 s) */

#define LP_ALPHA       0.20    /* peer theta low-pass: ~1.0 = no filter, lower = more damping */

#define K_MIN          0.22
#define K_MAX          0.28
#define NOISE_AMP      0.008
#define PHASE_TARGET   M_PI

/* Lock detection */
#define LOCK_WINDOW    20
#define LOCK_STD       0.10
#define ANTI_THRESH    0.22

/* PI gains */
#define KP             0.1
#define KI             0.00005
#define INTEG_CLAMP    0.5

/* TC drain command — adjust device/class as needed */
#define TC_DRAIN  "tc class change dev eth0 classid 1:20 htb rate 20mbit burst 500k"
#define TC_IDLE   "tc class change dev eth0 classid 1:20 htb rate 20mbit burst 64k"

/* Beacon packet (packed, big-endian on wire) */
typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t  sid;
    uint32_t tick;
    float    theta;
    float    omega;
    uint8_t  _pad;
    uint64_t t0_ns;
} Beacon;

static double noise(void) {
    /* Box-Muller */
    double u1 = (rand() + 1.0) / (RAND_MAX + 1.0);
    double u2 = (rand() + 1.0) / (RAND_MAX + 1.0);
    return sqrt(-2.0 * log(u1)) * cos(2.0 * M_PI * u2);
}

static void drain_async(void) {
    if (fork() == 0) {
        (void)system(TC_DRAIN);
        usleep(200000);
        (void)system(TC_IDLE);
        _exit(0);
    }
}

static float htonf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = htonl(n);
    float r; memcpy(&r, &n, 4); return r;
}
static float ntohf(float f) { return htonf(f); } /* ntoh == hton for floats — same bit-swap */

int main(int argc, char *argv[]) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGCHLD, SIG_IGN);

    if (argc < 2 || (strcmp(argv[1], "pi") && strcmp(argv[1], "pi2"))) {
        fprintf(stderr, "usage: beacon <pi|pi2>\n");
        return 1;
    }

    int is_pi2  = !strcmp(argv[1], "pi2");
    uint8_t sid = is_pi2 ? 2 : 1;
    double omega = is_pi2 ? OMEGA_PI2 : OMEGA_PI;

    srand((unsigned)time(NULL) ^ (unsigned)getpid());

    /* TX socket */
    int tx = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in dst = {.sin_family=AF_INET, .sin_port=htons(PORT)};
    inet_pton(AF_INET, MCAST_GRP, &dst.sin_addr);
    uint8_t ttl = 32;
    setsockopt(tx, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));

    /* RX socket */
    int rx = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(rx, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in bind_addr = {.sin_family=AF_INET, .sin_port=htons(PORT),
                                    .sin_addr.s_addr=INADDR_ANY};
    bind(rx, (struct sockaddr *)&bind_addr, sizeof(bind_addr));
    struct ip_mreq mreq = {.imr_multiaddr.s_addr=inet_addr(MCAST_GRP),
                            .imr_interface.s_addr=INADDR_ANY};
    setsockopt(rx, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));
    fcntl(rx, F_SETFL, O_NONBLOCK);

    double theta       = 0.0;
    double remote      = -1.0;   /* last remote theta */
    double peer_lp     = 0.0;   /* low-pass filtered peer theta */
    int    peer_lp_init = 0;
    double integral    = 0.0;
    double history[LOCK_WINDOW];
    int    hist_n      = 0;
    int    hist_full   = 0;
    uint32_t tick      = 0;
    int    drains      = 0;
    double prev_diff   = -1.0;
    int    locked      = 0;

    memset(history, 0, sizeof(history));

    struct timespec t0;
    clock_gettime(CLOCK_REALTIME, &t0);
    printf("[beacon:%s] sid=%d omega=%.4f anchor=%ld.%09ld\n",
           argv[1], sid, omega, (long)t0.tv_sec, (long)t0.tv_nsec);

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);

    while (1) {
        /* drain RX */
        Beacon pkt;
        struct sockaddr_in src;
        socklen_t slen = sizeof(src);
        while (recvfrom(rx, &pkt, sizeof(pkt), 0,
                        (struct sockaddr *)&src, &slen) == sizeof(pkt)) {
            if (ntohs(pkt.magic) != MAGIC) continue;
            uint8_t rsid = pkt.sid;
            if (rsid == sid) continue;   /* own packet */
            if (rsid != (is_pi2 ? 1 : 2)) continue;
            remote = ntohf(pkt.theta);
            if (!peer_lp_init) {
                peer_lp      = remote;
                peer_lp_init = 1;
            } else {
                peer_lp += LP_ALPHA * sin(remote - peer_lp);
            }
        }

        /* Kuramoto coupling */
        double k = K_MIN;
        if (peer_lp_init) {
            /* phase_diff in [0,pi] */
            double phase_diff = fabs(fmod(peer_lp - theta + M_PI, 2*M_PI) - M_PI);
            double serr = phase_diff - PHASE_TARGET;
            k = K_MIN + fabs(serr) * KP + integral * 0.000005;
            if (k < K_MIN) k = K_MIN;
            if (k > K_MAX) k = K_MAX;

            double coupling = sin(peer_lp - theta);
            theta += omega - k * coupling + NOISE_AMP * noise();

            /* lock detection — rolling window */
            history[hist_n % LOCK_WINDOW] = phase_diff;
            hist_n++;
            if (hist_n >= LOCK_WINDOW) hist_full = 1;

            int n = hist_full ? LOCK_WINDOW : hist_n;
            double mean = 0;
            for (int i = 0; i < n; i++) mean += history[i];
            mean /= n;
            double var = 0;
            for (int i = 0; i < n; i++) var += (history[i]-mean)*(history[i]-mean);
            double std = sqrt(var / n);

            locked = (hist_full &&
                      std < LOCK_STD &&
                      fabs(phase_diff - PHASE_TARGET) < ANTI_THRESH);

            if (locked)
                integral += serr * (TICK_NS / 1e9) * KI;
            else
                integral *= 0.999;   /* slow leak while unlocked — stale integral
                                         shouldn't shove omega the wrong way on re-lock */
            if (integral >  INTEG_CLAMP) integral =  INTEG_CLAMP;
            if (integral < -INTEG_CLAMP) integral = -INTEG_CLAMP;

            /* tc drain at phase crossing */
            if (prev_diff >= 0 && prev_diff > PHASE_TARGET && phase_diff <= PHASE_TARGET) {
                drain_async();
                drains++;
            }
            prev_diff = phase_diff;

            /* status line */
            if (tick % 200 == 0) {
                double phase_diff_print = fabs(fmod(peer_lp - theta + M_PI, 2*M_PI) - M_PI);
                int bar = (int)(phase_diff_print / M_PI * 39);
                printf("\r[%s] tick=%6u  φ=%.3f  ", argv[1], tick, phase_diff_print);
                for (int i=0;i<bar;i++) printf("█");
                for (int i=bar;i<40;i++) printf("░");
                printf("  %s  k=%.4f  ∫=%.4f  drains=%d   ",
                       locked ? "LOCKED" : "      ", k, integral, drains);
                fflush(stdout);
            }
        } else {
            /* free-run */
            theta += omega + NOISE_AMP * noise();
            if (tick % 200 == 0) {
                printf("\r[%s] tick=%6u  free-run (no remote)   ", argv[1], tick);
                fflush(stdout);
            }
        }

        theta = fmod(theta, 2 * M_PI);
        if (theta < 0) theta += 2 * M_PI;

        /* send beacon */
        Beacon out;
        out.magic = htons(MAGIC);
        out.sid   = sid;
        out.tick  = htonl(tick);
        out.theta = htonf((float)theta);
        out.omega = htonf((float)omega);
        out._pad  = 0;
        out.t0_ns = (tick % ANCHOR_INTERVAL == 0)
            ? htobe64((uint64_t)t0.tv_sec * 1000000000ULL + (uint64_t)t0.tv_nsec)
            : 0;
        sendto(tx, &out, sizeof(out), 0,
               (struct sockaddr *)&dst, sizeof(dst));

        tick++;

        /* precise sleep to next tick boundary */
        next.tv_nsec += TICK_NS;
        if (next.tv_nsec >= 1000000000L) {
            next.tv_nsec -= 1000000000L;
            next.tv_sec++;
        }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
    }
}
