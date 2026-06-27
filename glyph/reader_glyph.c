#define _GNU_SOURCE
/*
 * reader.c — Kuramoto axis node
 *
 * Pis are the pendulum (locked anti-phase, never touched).
 * reader.c is the axis — sits between Pi1 and Pi2, distributes
 * stable timing to all downstream consumers.
 *
 * Output:
 *   239.0.0.2:7404  AxisPulse multicast — fires on every beacon packet
 *                   (Pi1 + Pi2 interleaved → ~40Hz effective tick rate)
 *   127.0.0.1:7403  WanPulse loopback   — phase_sched compatibility
 *   <wan_ip>:7402   WanPulse unicast    — legacy telemetry
 *
 * Input:
 *   239.0.0.1:7400  beacon multicast from Pi1 + Pi2
 *   0.0.0.0:7405    LoadFeedback from consumers (non-blocking)
 *                   Reader accumulates load avg and echoes it in AxisPulse.
 *
 * Consumers subscribe to 239.0.0.2:7404 for AxisPulse.
 * phase_sched continues on 7403 unchanged.
 *
 * Usage: sudo ./reader [wan_ip] [wan_port]
 */

#include <arpa/inet.h>
#include <endian.h>
#include <fcntl.h>
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

/* ── ports ───────────────────────────────────────────────────────────────── */
#define BEACON_PORT   7400
#define BEACON_GRP    "239.0.0.1"
#define SCHED_PORT    7403
#define AXIS_PORT     7404
#define AXIS_GRP      "239.0.0.2"
#define LOAD_PORT     7405
#define GLYPH_PORT    7408   /* glyph inject: 0x474C GL packets override pd_dev */
#define WAN_PORT_DEF  7402

/* ── packet magic ────────────────────────────────────────────────────────── */
#define BEACON_MAGIC  0x1B4A
#define WAN_MAGIC     0x5257   /* "RW" */
#define AXIS_MAGIC    0x4158   /* "AX" */
#define LOAD_MAGIC    0x4C44   /* "LD" */
#define GLYPH_MAGIC   0x474C   /* "GL" */

/* ── peer addresses (filter own injections from phase tracking) ──────────── */
#define PI1_ADDR  "10.0.0.122"
#define PI2_ADDR  "10.0.0.174"
#define GLYPH_DELTA 0.50f   /* rad phase advance injected on Pi1 during ACTIVE */

/* ── lock params ─────────────────────────────────────────────────────────── */
#define PHASE_TARGET  M_PI
#define ANTI_THRESH   0.20
#define LOCK_WINDOW   20
#define LOCK_STD      0.10

/* ── intent pulse scheduler ──────────────────────────────────────────────── */
/*
 * Intent types (gbuf[2]):
 *   0 = ADVISORY  — 1 unit  (~100ms)  soft bias
 *   1 = DIRECTIVE — 3 units (~300ms)  strong push
 *   2 = ALARM     — 9 units (~900ms)  sustained disruption
 *
 * Each intent queues: ACTIVE(N ticks) → REST(1 unit gap).
 * Neurons respond to the real pd_dev excursion naturally.
 */
#define INTENT_UNIT   8    /* beacon ticks per base unit (~100ms at 80Hz) */
#define INTENT_QSIZE 64

struct glyph_entry { int active; int ticks; };
static struct glyph_entry glyph_queue[INTENT_QSIZE];
static int gq_head = 0, gq_tail = 0, gq_remaining = 0, glyph_active = 0;

static void gq_push(int active, int ticks) {
    int next = (gq_tail + 1) % INTENT_QSIZE;
    if (next == gq_head) return;
    glyph_queue[gq_tail] = (struct glyph_entry){active, ticks};
    gq_tail = next;
}
static void glyph_tick(void) {
    if (gq_remaining > 0) { gq_remaining--; return; }
    if (gq_head == gq_tail) { glyph_active = 0; return; }
    struct glyph_entry e = glyph_queue[gq_head];
    gq_head = (gq_head + 1) % INTENT_QSIZE;
    glyph_active = e.active;
    gq_remaining = e.ticks - 1;
}

/* ── tc ──────────────────────────────────────────────────────────────────── */
#define TC_BIN    "/usr/sbin/tc"
#define TC_DEV    "enp0s31f6"
#define TC_CLASS  "1:20"
#define TC_RATE   "20mbit"

/* ── packets ─────────────────────────────────────────────────────────────── */

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t  sid;
    uint32_t tick;
    float    theta;
    float    omega;
    uint8_t  _pad;
    uint64_t t0_ns;   /* CLOCK_REALTIME at tick 0 (ns since epoch); 0 otherwise */
} Beacon;   /* 24 bytes */

typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint32_t tick;
    float    theta;
    float    omega;
    float    pd;
    uint16_t drains;
} WanPulse;  /* 18 bytes */

/*
 * AxisPulse — sent on every beacon packet to 239.0.0.2:7404.
 *
 * sid=1 packets carry Pi1's phase in theta1; theta2 is last known Pi2.
 * sid=2 packets carry Pi2's phase in theta2; theta1 is last known Pi1.
 * quality = |pd - π| as uint8 (0=perfect, 255=unlocked).
 * load_avg = rolling average of LoadFeedback.load from consumers.
 */
typedef struct __attribute__((packed)) {
    uint16_t magic;      /* 0x4158 */
    uint8_t  sid;        /* triggering beacon: 1 or 2 */
    uint8_t  locked;     /* 1 = anti-phase lock confirmed */
    uint32_t tick;       /* triggering beacon tick */
    float    theta1;     /* Pi1 phase */
    float    theta2;     /* Pi2 phase */
    float    pd;         /* phase difference |θ1−θ2| normalised */
    float    pd_dev;     /* |pd − π|  (quality: lower = tighter) */
    float    load_avg;   /* rolling avg load from consumers, 0–1 */
    uint16_t drains;
    uint64_t t0_ns;      /* CLOCK_REALTIME anchor for triggering sid (ns since epoch); 0 if not yet seen */
} AxisPulse;  /* 38 bytes */

/*
 * LoadFeedback — sent by consumers to reader on port 7405.
 */
typedef struct __attribute__((packed)) {
    uint16_t magic;    /* 0x4C44 */
    float    load;     /* 0.0–1.0 */
    float    temp;     /* °C, or -1 if unknown */
} LoadFeedback;  /* 10 bytes */

/* ── helpers ─────────────────────────────────────────────────────────────── */

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
            char iface[32]; uint64_t v[16];
            if (sscanf(line, " %31[^:]: %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                              " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                              " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                              " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64,
                       iface,&v[0],&v[1],&v[2],&v[3],&v[4],&v[5],&v[6],&v[7],
                       &v[8],&v[9],&v[10],&v[11],&v[12],&v[13],&v[14],&v[15]) == 17) {
                fclose(f); return v[8];
            }
        }
    }
    fclose(f); return 0;
}

static void tc_setup(void) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
             "%s qdisc replace dev %s root handle 1: htb default 20", TC_BIN, TC_DEV);
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
    if (fork() == 0) { tc_burst("500k"); usleep(200000); tc_burst("64k"); _exit(0); }
}

/* ── main ────────────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGCHLD, SIG_IGN);

    /* --sid N: follow only sid 1 or 2; 0 = both (default) */
    int target_sid = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--sid") == 0 && i+1 < argc) {
            target_sid = atoi(argv[i+1]);
            for (int j = i; j < argc-2; j++) argv[j] = argv[j+2];
            argc -= 2;
            break;
        }
    }

    /* optional legacy WAN unicast */
    int wan_fd = -1;
    struct sockaddr_in wan_addr = {0};
    if (argc >= 2) {
        int port = (argc >= 3) ? atoi(argv[2]) : WAN_PORT_DEF;
        wan_fd = socket(AF_INET, SOCK_DGRAM, 0);
        wan_addr.sin_family      = AF_INET;
        wan_addr.sin_port        = htons((uint16_t)port);
        wan_addr.sin_addr.s_addr = inet_addr(argv[1]);
        printf("[reader] WAN → %s:%d\n", argv[1], port);
    }

    /* phase_sched loopback 7403 */
    int sched_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in sched_addr = {
        .sin_family = AF_INET, .sin_port = htons(SCHED_PORT),
        .sin_addr.s_addr = inet_addr("127.0.0.1")
    };

    /* axis multicast output 239.0.0.2:7404 */
    int axis_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in axis_addr = {
        .sin_family = AF_INET, .sin_port = htons(AXIS_PORT),
        .sin_addr.s_addr = inet_addr(AXIS_GRP)
    };
    {   /* TTL=4: crosses routers but not the internet */
        int ttl = 4;
        setsockopt(axis_fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));
    }

    /* load feedback receiver 7405 — non-blocking */
    int load_fd = socket(AF_INET, SOCK_DGRAM, 0);
    {
        int one = 1;
        setsockopt(load_fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
        struct sockaddr_in la = {
            .sin_family = AF_INET, .sin_port = htons(LOAD_PORT),
            .sin_addr.s_addr = INADDR_ANY
        };
        bind(load_fd, (struct sockaddr *)&la, sizeof(la));
        fcntl(load_fd, F_SETFL, O_NONBLOCK);
    }

    /* glyph inject receiver 7408 — non-blocking; overrides pd_dev when active */
    int glyph_fd = socket(AF_INET, SOCK_DGRAM, 0);
    {
        int one = 1;
        setsockopt(glyph_fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
        struct sockaddr_in ga = {
            .sin_family = AF_INET, .sin_port = htons(GLYPH_PORT),
            .sin_addr.s_addr = INADDR_ANY
        };
        bind(glyph_fd, (struct sockaddr *)&ga, sizeof(ga));
        fcntl(glyph_fd, F_SETFL, O_NONBLOCK);
    }

    /* beacon injection socket — sends θ1+DELTA to perturb Pi2's coupling */
    int inj_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in inj_addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(BEACON_PORT),
        .sin_addr.s_addr = inet_addr(BEACON_GRP)
    };
    {
        int ttl = 4;
        setsockopt(inj_fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));
        /* loop=1: glyph_rx on Mint also receives injected beacons for detection */
        int loop = 1;
        setsockopt(inj_fd, IPPROTO_IP, IP_MULTICAST_LOOP, &loop, sizeof(loop));
    }

    /* beacon multicast receiver 7400 */
    int rx = socket(AF_INET, SOCK_DGRAM, 0);
    {
        int one = 1;
        setsockopt(rx, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        struct sockaddr_in ra = {
            .sin_family = AF_INET, .sin_port = htons(BEACON_PORT),
            .sin_addr.s_addr = INADDR_ANY
        };
        bind(rx, (struct sockaddr *)&ra, sizeof(ra));
        struct ip_mreq mreq = { .imr_multiaddr.s_addr = inet_addr(BEACON_GRP),
                                .imr_interface.s_addr = INADDR_ANY };
        setsockopt(rx, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));
        struct timeval tv = {.tv_sec=0, .tv_usec=1000};
        setsockopt(rx, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    }

    uint32_t pi1_net = inet_addr(PI1_ADDR);
    uint32_t pi2_net = inet_addr(PI2_ADDR);

    double phases[3]            = {-1.0, -1.0, -1.0};
    struct timespec last_seen[3]= {{0},{0},{0}};
    uint32_t ticks[3]           = {0, 0, 0};
    float    omegas[3]          = {0.0f, 0.0f, 0.0f};
    uint64_t t0_ns[3]           = {0, 0, 0};

    double history[LOCK_WINDOW];
    int    hist_n = 0, hist_full = 0;
    memset(history, 0, sizeof(history));

    double prev_diff     = -1.0;
    int    drains        = 0;
    struct timespec last_drain_ts = {0};

    /* load feedback state */
    double load_avg      = 0.0;
    double load_alpha    = 0.1;   /* EMA weight */

    uint64_t prev_tx_bytes = 0;
    struct timespec prev_tx_ts = {0};

    tc_setup();
    printf("[reader] axis — Pi1+Pi2 → 239.0.0.2:%d  feedback ← :%d\n",
           AXIS_PORT, LOAD_PORT);

    while (1) {
        /* ── receive beacon ── */
        Beacon pkt;
        struct sockaddr_in src; socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(rx, &pkt, sizeof(pkt), 0,
                             (struct sockaddr *)&src, &slen);
        if (n != sizeof(pkt) || ntohs(pkt.magic) != BEACON_MAGIC) goto drain_load;
        /* skip own injections — only track real beacons from the Pis */
        if (src.sin_addr.s_addr != pi1_net && src.sin_addr.s_addr != pi2_net)
            goto drain_load;
        int sid = pkt.sid;
        if (sid != 1 && sid != 2) goto drain_load;
        if (target_sid != 0 && sid != target_sid) goto drain_load;

        phases[sid]  = ntohf(pkt.theta);
        omegas[sid]  = ntohf(pkt.omega);
        ticks[sid]   = ntohl(pkt.tick);
        clock_gettime(CLOCK_MONOTONIC, &last_seen[sid]);
        if (pkt.t0_ns != 0) {
            t0_ns[sid] = be64toh(pkt.t0_ns);
            time_t sec = (time_t)(t0_ns[sid] / 1000000000ULL);
            uint32_t ms = (uint32_t)((t0_ns[sid] % 1000000000ULL) / 1000000ULL);
            struct tm *tm = gmtime(&sec);
            printf("\n[reader] sid=%d anchor %04d-%02d-%02dT%02d:%02d:%02d.%03dZ\n",
                   sid, tm->tm_year+1900, tm->tm_mon+1, tm->tm_mday,
                   tm->tm_hour, tm->tm_min, tm->tm_sec, ms);
        }

        if (target_sid != 0) {
            /* single-oscillator mode: emit AxisPulse for this sid only */
            AxisPulse ap = {0};
            ap.magic    = htons(AXIS_MAGIC);
            ap.sid      = (uint8_t)sid;
            ap.locked   = 0;
            ap.tick     = htonl(ticks[sid]);
            ap.theta1   = htonf((float)(sid == 1 ? phases[1] : 0.0));
            ap.theta2   = htonf((float)(sid == 2 ? phases[2] : 0.0));
            ap.pd       = 0;
            ap.pd_dev   = 0;
            ap.load_avg = htonf((float)load_avg);
            ap.drains   = 0;
            ap.t0_ns    = htobe64(t0_ns[sid]);
            sendto(axis_fd, &ap, sizeof(ap), 0,
                   (struct sockaddr *)&axis_addr, sizeof(axis_addr));
            int bar = (int)(phases[sid] / (2*M_PI) * 39);
            printf("\r[axis:sid%d] θ=%.3f  ", sid, phases[sid]);
            for (int i=0;i<bar;i++)  printf("█");
            for (int i=bar;i<40;i++) printf("░");
            fflush(stdout);
            goto drain_load;
        }

        if (phases[1] < 0 || phases[2] < 0) goto drain_load;

        /* ── phase diff + lock ── */
        double diff      = phases[1] - phases[2];
        double phase_diff= fabs(fmod(diff + M_PI, 2*M_PI) - M_PI);
        double pd_dev    = fabs(phase_diff - PHASE_TARGET);

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
        int locked = (hist_full && std < LOCK_STD && pd_dev < ANTI_THRESH);

        /* ── glyph scheduler tick ── */
        glyph_tick();

        /* ── glyph beacon injection — perturb Pi2's coupling with θ1+DELTA ── */
        if (glyph_active && sid == 1) {
            Beacon inj;
            memcpy(&inj, &pkt, sizeof(inj));
            inj.theta = htonf((float)phases[1] + GLYPH_DELTA);
            sendto(inj_fd, &inj, sizeof(inj), 0,
                   (struct sockaddr *)&inj_addr, sizeof(inj_addr));
        }

        /* ── AxisPulse → 239.0.0.2:7404 (every packet) ── */
        {
            float out_pd_dev = (float)pd_dev;   /* real phase — no synthetic override */

            AxisPulse ap;
            ap.magic    = htons(AXIS_MAGIC);
            ap.sid      = (uint8_t)sid;
            ap.locked   = (uint8_t)locked;
            ap.tick     = htonl(ticks[sid]);
            ap.theta1   = htonf((float)phases[1]);
            ap.theta2   = htonf((float)phases[2]);
            ap.pd       = htonf((float)phase_diff);
            ap.pd_dev   = htonf(out_pd_dev);
            ap.load_avg = htonf((float)load_avg);
            ap.drains   = htons((uint16_t)drains);
            ap.t0_ns    = htobe64(t0_ns[sid]);
            sendto(axis_fd, &ap, sizeof(ap), 0,
                   (struct sockaddr *)&axis_addr, sizeof(axis_addr));
        }

        /* ── WanPulse → 7403 phase_sched (only when both beacons fresh) ── */
        {
            struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
            long age1 = (now.tv_sec - last_seen[1].tv_sec) * 1000
                      + (now.tv_nsec - last_seen[1].tv_nsec) / 1000000;
            long age2 = (now.tv_sec - last_seen[2].tv_sec) * 1000
                      + (now.tv_nsec - last_seen[2].tv_nsec) / 1000000;
            if (age1 <= 500 && age2 <= 500) {
                WanPulse wp;
                wp.magic  = htons(WAN_MAGIC);
                wp.tick   = htonl(ticks[1]);
                wp.theta  = htonf((float)phases[1]);
                wp.omega  = htonf(omegas[1]);
                wp.pd     = htonf((float)phase_diff);
                wp.drains = htons((uint16_t)drains);
                sendto(sched_fd, &wp, sizeof(wp), 0,
                       (struct sockaddr *)&sched_addr, sizeof(sched_addr));
            }
        }

        /* ── drain crossing → tc burst + optional WAN ── */
        {
            struct timespec _now; clock_gettime(CLOCK_MONOTONIC, &_now);
            long since = (_now.tv_sec  - last_drain_ts.tv_sec)  * 1000
                       + (_now.tv_nsec - last_drain_ts.tv_nsec) / 1000000;
            if (prev_diff >= 0 && prev_diff > PHASE_TARGET
                && phase_diff <= PHASE_TARGET && since > 3000) {
                last_drain_ts = _now;
                drain_async();
                drains++;
                if (wan_fd >= 0) {
                    WanPulse wp;
                    wp.magic  = htons(WAN_MAGIC);
                    wp.tick   = htonl(ticks[1]);
                    wp.theta  = htonf((float)phases[1]);
                    wp.omega  = htonf(omegas[1]);
                    wp.pd     = htonf((float)phase_diff);
                    wp.drains = htons((uint16_t)drains);
                    sendto(wan_fd, &wp, sizeof(wp), 0,
                           (struct sockaddr *)&wan_addr, sizeof(wan_addr));
                }
            }
        }
        prev_diff = phase_diff;

        /* ── TX rate ── */
        double tx_mbps = 0.0;
        {
            struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
            uint64_t tx = read_tx_bytes();
            if (prev_tx_bytes && prev_tx_ts.tv_sec) {
                double dt = (now.tv_sec  - prev_tx_ts.tv_sec)
                          + (now.tv_nsec - prev_tx_ts.tv_nsec) * 1e-9;
                if (dt > 0.0)
                    tx_mbps = (double)(tx - prev_tx_bytes) * 8.0 / 1e6 / dt;
            }
            prev_tx_bytes = tx; prev_tx_ts = now;
        }

        /* ── progress line ── */
        int bar = (int)(phase_diff / M_PI * 39);
        printf("\r[axis] φ=%.3f(±%.3f)  ", phase_diff, pd_dev);
        for (int i=0;i<bar;i++)  printf("█");
        for (int i=bar;i<40;i++) printf("░");
        printf("  %s  drains=%d  up=%.1fMbps  load=%.0f%%   ",
               locked ? "LOCKED" : "      ", drains, tx_mbps, load_avg*100.0);
        fflush(stdout);

    drain_load:
        /* ── drain load feedback (non-blocking) ── */
        {
            LoadFeedback lf;
            while (recv(load_fd, &lf, sizeof(lf), 0) == sizeof(lf)) {
                if (ntohs(lf.magic) != LOAD_MAGIC) continue;
                float l = ntohf(lf.load);
                if (l >= 0.0f && l <= 1.0f)
                    load_avg = load_avg * (1.0 - load_alpha) + l * load_alpha;
            }
        }

        /* ── drain intent packets (non-blocking) ── */
        {
            uint8_t gbuf[8];
            ssize_t gn;
            while ((gn = recv(glyph_fd, gbuf, sizeof(gbuf), 0)) >= 3) {
                uint16_t gmagic = ((uint16_t)gbuf[0] << 8) | gbuf[1];
                if (gmagic != GLYPH_MAGIC) continue;
                switch (gbuf[2]) {
                    case 0: gq_push(1, INTENT_UNIT);   gq_push(0, INTENT_UNIT); break;
                    case 1: gq_push(1, INTENT_UNIT*3); gq_push(0, INTENT_UNIT); break;
                    case 2: gq_push(1, INTENT_UNIT*9); gq_push(0, INTENT_UNIT); break;
                }
            }
        }
    }
}
