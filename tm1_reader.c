/*
 * tm1_reader.c — Kuramoto phase → TM1 clock modulation (LMDE/Celeron 900)
 *
 * Subscribes to AxisPulse multicast (239.0.0.2:7404).
 * Maps θ → MSR 0x19A duty cycle: trough=25%, peak=full.
 * Sends LoadFeedback to reader on :7405.
 * Logs CSV to /tmp/tm1_reader_<pid>.csv.
 *
 * Usage: sudo ./tm1_reader [reader_ip]
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <glob.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <sys/socket.h>
#include <netinet/in.h>

#define AXIS_PORT   7404
#define AXIS_GRP    "239.0.0.2"
#define LOAD_PORT   7405
#define AXIS_MAGIC  0x4158
#define LOAD_MAGIC  0x4C44

#define MSR_CLOCK_MOD  0x19A
#define TM1_FULL       0x00        /* disabled = full speed */
#define TM1_87         0x1e        /* 87.5% */
#define TM1_75         0x1c        /* 75.0% */
#define TM1_62         0x1a        /* 62.5% */
#define TM1_50         0x18        /* 50.0% */
#define TM1_37         0x16        /* 37.5% */
#define TM1_25         0x14        /* 25.0% */

typedef struct __attribute__((packed)) {
    uint16_t magic; uint8_t sid; uint8_t locked; uint32_t tick;
    float theta1; float theta2; float pd; float pd_dev;
    float load_avg; uint16_t drains;
} AxisPulse;

typedef struct __attribute__((packed)) {
    uint16_t magic; float load; float temp;
} LoadFeedback;

static inline float ntohf(float f) {
    uint32_t u; memcpy(&u, &f, 4); u = ntohl(u); memcpy(&f, &u, 4); return f;
}
static inline float htonf(float f) { return ntohf(f); }

static int  msr_fds[8];
static int  n_cpus = 0;
static FILE *csv_fp = NULL;

static void tm1_set(uint64_t val) {
    for (int i = 0; i < n_cpus; i++)
        if (msr_fds[i] >= 0)
            pwrite(msr_fds[i], &val, sizeof(val), MSR_CLOCK_MOD);
}

static void restore(void) {
    tm1_set(TM1_FULL);
    fprintf(stderr, "\n[tm1] restored full speed\n");
    if (csv_fp) fclose(csv_fp);
}

static void sig_handler(int s) { (void)s; restore(); exit(0); }

static float read_temp(void) {
    FILE *f = fopen("/sys/class/thermal/thermal_zone0/temp", "r");
    if (!f) return 0.0f;
    int v = 0; fscanf(f, "%d", &v); fclose(f);
    return v / 1000.0f;
}

/* map θ → TM1 register value (7 steps) */
static uint64_t theta_to_tm1(double theta, int locked) {
    if (!locked) return TM1_FULL;
    double frac = (1.0 - cos(theta)) / 2.0;  /* 0=trough, 1=peak */
    if (frac >= 0.857) return TM1_FULL;
    if (frac >= 0.714) return TM1_87;
    if (frac >= 0.571) return TM1_75;
    if (frac >= 0.429) return TM1_62;
    if (frac >= 0.286) return TM1_50;
    if (frac >= 0.143) return TM1_37;
    return TM1_25;
}

static const char *tm1_label(uint64_t v) {
    switch (v) {
        case TM1_FULL: return "full";
        case TM1_87:   return "87.5%";
        case TM1_75:   return "75.0%";
        case TM1_62:   return "62.5%";
        case TM1_50:   return "50.0%";
        case TM1_37:   return "37.5%";
        case TM1_25:   return "25.0%";
        default:       return "?";
    }
}

int main(int argc, char **argv) {
    const char *reader_ip = "10.0.0.122";
    if (argc > 1) reader_ip = argv[1];

    /* open MSR fds */
    glob_t g;
    if (glob("/dev/cpu/*/msr", 0, NULL, &g) == 0) {
        for (size_t i = 0; i < g.gl_pathc && n_cpus < 8; i++) {
            msr_fds[n_cpus] = open(g.gl_pathv[i], O_WRONLY);
            if (msr_fds[n_cpus] >= 0) n_cpus++;
        }
        globfree(&g);
    }
    if (n_cpus == 0) { fprintf(stderr, "no /dev/cpu/*/msr — need root\n"); return 1; }
    fprintf(stderr, "[tm1] %d CPU(s) MSR open\n", n_cpus);

    /* axis multicast socket */
    int axis_fd = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(axis_fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
    struct sockaddr_in axis_addr = {
        .sin_family = AF_INET, .sin_port = htons(AXIS_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY)
    };
    bind(axis_fd, (struct sockaddr *)&axis_addr, sizeof(axis_addr));
    struct ip_mreq mreq;
    inet_pton(AF_INET, AXIS_GRP, &mreq.imr_multiaddr);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    setsockopt(axis_fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

    /* load feedback socket */
    int load_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in load_addr = {
        .sin_family = AF_INET, .sin_port = htons(LOAD_PORT)
    };
    inet_pton(AF_INET, reader_ip, &load_addr.sin_addr);

    /* CSV log */
    char csvpath[64];
    snprintf(csvpath, sizeof(csvpath), "/tmp/tm1_reader_%d.csv", getpid());
    csv_fp = fopen(csvpath, "w");
    if (csv_fp) fprintf(csv_fp, "t_ms,theta,frac,duty,temp_c,locked\n");

    signal(SIGINT,  sig_handler);
    signal(SIGTERM, sig_handler);

    fprintf(stderr, "[tm1] axis %s:%d  feedback→%s:%d  log=%s\n",
            AXIS_GRP, AXIS_PORT, reader_ip, LOAD_PORT, csvpath);

    AxisPulse ap;
    uint64_t cur_tm1 = TM1_FULL;
    uint32_t tick_count = 0;

    while (1) {
        ssize_t n = recv(axis_fd, &ap, sizeof(ap), 0);
        if (n != sizeof(ap)) continue;
        if (ntohs(ap.magic) != AXIS_MAGIC) continue;

        int locked = ap.locked;
        double theta = ntohf(ap.theta1);

        double frac = (1.0 - cos(theta)) / 2.0;
        uint64_t new_tm1 = theta_to_tm1(theta, locked);

        if (new_tm1 != cur_tm1) {
            tm1_set(new_tm1);
            cur_tm1 = new_tm1;
        }

        float temp = read_temp();

        /* send load feedback */
        LoadFeedback lf;
        lf.magic = htons(LOAD_MAGIC);
        lf.load  = htonf(0.0f);
        lf.temp  = htonf(temp);
        sendto(load_fd, &lf, sizeof(lf), 0,
               (struct sockaddr *)&load_addr, sizeof(load_addr));

        /* log */
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        long t_ms = ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;

        if (csv_fp)
            fprintf(csv_fp, "%ld,%.4f,%.4f,%s,%.1f,%d\n",
                    t_ms, theta, frac, tm1_label(new_tm1), temp, locked);

        /* progress every 40 ticks (~1s) */
        if (++tick_count % 40 == 0) {
            fprintf(stderr, "\r[tm1] θ=%.3f  frac=%.3f  duty=%-5s  temp=%.0f°C  %s    ",
                    theta, frac, tm1_label(cur_tm1), temp,
                    locked ? "LOCKED" : "hunting");
        }
    }

    restore();
    return 0;
}
