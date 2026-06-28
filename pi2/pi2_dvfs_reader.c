#define _GNU_SOURCE
/*
 * pi2_dvfs_reader.c — Triangle B: temlum controller for Pi2 DVFS plant.
 *
 * Inputs:
 *   239.0.0.2:7404  AxisPulse multicast  — tick pacing; updates temlum each locked tick
 *   0.0.0.0:7432    DCN from cerebellum  — pred_err_ema or HOLD (0x484F)
 *   239.0.0.3:7440  NucleusState-A       — Triangle A cross-field (temlum_a)
 *
 * Output:
 *   127.0.0.1:7433  ASCII intent         — "PARK\n" / "UNPARK\n" / "HOLD\n"
 *   239.0.0.4:7441  NucleusState-B       — e_C, temlum, pd_pop, intent
 *
 * Control law (same neuron as Triangle A):
 *   temlum = α·temlum_prev + (1−α)·(T − T_target)
 *   pd_pop = W_FAST·pd_fast + W_MID·pd_mid + W_SLOW·pd_slow
 *   e_C    = W_P·(pred_err − P_target) − pd_pop − W_T·temlum − W_CROSS·temlum_a
 *   e_C >  E_UNPARK → UNPARK  (clock up)
 *   e_C < −E_PARK   → PARK    (clock down)
 *   else             → HOLD
 *
 * Build: gcc -O2 -o pi2_dvfs_reader pi2_dvfs_reader.c -lm
 * Run:   sudo ./pi2_dvfs_reader [intent_ip]   (default 127.0.0.1)
 */

#include <arpa/inet.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

/* ── control params (identical to Triangle A) ────────────────────────────── */
#define T_TARGET    84.0f
#define ALPHA_T     0.95f
#define P_TARGET    0.0045f
#define W_P         1.0f
#define W_T         0.15f
#define W_CROSS     0.05f   /* Triangle A cross-field weight */
#define E_UNPARK    0.0040f
#define E_PARK      0.0030f

/* ── tricast pd nucleus (identical to Triangle A) ────────────────────────── */
#define ALPHA_FAST  0.35f
#define ALPHA_MID   0.12f
#define ALPHA_SLOW  0.03f
#define W_FAST      0.30f
#define W_MID       0.25f
#define W_SLOW      0.18f

/* ── ports ───────────────────────────────────────────────────────────────── */
#define AXIS_PORT    7404
#define AXIS_GRP     "239.0.0.2"
#define DCN_PORT     7432   /* own DCN port — :7430 is owned by Triangle A */
#define INTENT_PORT  7433
#define NS_A_PORT    7440
#define NS_A_GRP     "239.0.0.3"   /* Triangle A cross-field */
#define NS_B_PORT    7441
#define NS_B_GRP     "239.0.0.4"   /* Triangle B emits here */

/* ── wire magic ──────────────────────────────────────────────────────────── */
#define AP_MAGIC   0x4158   /* "AX" */
#define DCN_MAGIC  0x4443   /* "DC" */
#define HOLD_MAGIC 0x484F   /* "HO" — cerebellum authoritative HOLD */
#define NS_MAGIC   0x4E53   /* "NS" */

/* ── staleness ───────────────────────────────────────────────────────────── */
#define DCN_STALE_SECS 5

/* ── NucleusState (16 bytes, big-endian) ─────────────────────────────────── */
typedef struct __attribute__((packed)) {
    uint16_t magic;
    float    e_C;
    float    temlum;
    float    pd_pop;
    uint8_t  intent;   /* 0=PARK 1=HOLD 2=UNPARK */
    uint8_t  _pad;
} NucleusState;

/* ── AxisPulse (38 bytes, big-endian) ────────────────────────────────────── */
typedef struct __attribute__((packed)) {
    uint16_t magic;
    uint8_t  sid;
    uint8_t  locked;
    uint32_t tick;
    float    theta1;
    float    theta2;
    float    pd;
    float    pd_dev;
    float    load_avg;
    uint16_t drains;
    uint64_t t0_ns;
} AxisPulse;

static float be_float(const uint8_t *p) {
    uint32_t u = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
               | ((uint32_t)p[2] <<  8) |  (uint32_t)p[3];
    float f;
    memcpy(&f, &u, 4);
    return f;
}

static float read_temp(void) {
    FILE *f = fopen("/sys/class/thermal/thermal_zone0/temp", "r");
    if (!f) return 70.0f;
    int raw = 0;
    (void)fscanf(f, "%d", &raw);
    fclose(f);
    return (float)raw / 1000.0f;
}

static void emit_intent(int fd, struct sockaddr_in *dst,
                        int ns_fd, struct sockaddr_in *ns_dst,
                        float e_C, float temlum, float pd_pop,
                        const char *intent, uint8_t intent_byte) {
    sendto(fd, intent, strlen(intent), 0, (struct sockaddr *)dst, sizeof(*dst));

    NucleusState ns;
    uint32_t tmp;
    ns.magic = htons(NS_MAGIC);
#define F2N(f) (memcpy(&tmp, &(f), 4), htonl(tmp))
    uint32_t ec_n = F2N(e_C);
    uint32_t tl_n = F2N(temlum);
    uint32_t pp_n = F2N(pd_pop);
#undef F2N
    memcpy(&ns.e_C,    &ec_n, 4);
    memcpy(&ns.temlum, &tl_n, 4);
    memcpy(&ns.pd_pop, &pp_n, 4);
    ns.intent = intent_byte;
    ns._pad   = 0;
    sendto(ns_fd, &ns, sizeof(ns), 0, (struct sockaddr *)ns_dst, sizeof(*ns_dst));
}

int main(int argc, char **argv) {
    const char *intent_ip = (argc > 1) ? argv[1] : "127.0.0.1";

    int yes = 1;

    /* AxisPulse multicast socket */
    int ap_fd = socket(AF_INET, SOCK_DGRAM, 0);
    setsockopt(ap_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in ap_addr = {
        .sin_family = AF_INET, .sin_port = htons(AXIS_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(ap_fd, (struct sockaddr *)&ap_addr, sizeof(ap_addr));
    struct ip_mreq mreq = {
        .imr_multiaddr.s_addr = inet_addr(AXIS_GRP),
        .imr_interface.s_addr = htonl(INADDR_ANY),
    };
    setsockopt(ap_fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

    /* DCN receive socket */
    int dcn_fd = socket(AF_INET, SOCK_DGRAM, 0);
    setsockopt(dcn_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in dcn_addr = {
        .sin_family = AF_INET, .sin_port = htons(DCN_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(dcn_fd, (struct sockaddr *)&dcn_addr, sizeof(dcn_addr));

    /* NucleusState-A receive (Triangle A cross-field) */
    int ns_a_fd = socket(AF_INET, SOCK_DGRAM, 0);
    setsockopt(ns_a_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in ns_a_addr = {
        .sin_family = AF_INET, .sin_port = htons(NS_A_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(ns_a_fd, (struct sockaddr *)&ns_a_addr, sizeof(ns_a_addr));
    struct ip_mreq mreq_a = {
        .imr_multiaddr.s_addr = inet_addr(NS_A_GRP),
        .imr_interface.s_addr = htonl(INADDR_ANY),
    };
    setsockopt(ns_a_fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq_a, sizeof(mreq_a));

    /* Intent send socket */
    int intent_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in intent_dst = {
        .sin_family = AF_INET, .sin_port = htons(INTENT_PORT),
        .sin_addr.s_addr = inet_addr(intent_ip),
    };

    /* NucleusState-B emit */
    int ns_b_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in ns_b_dst = {
        .sin_family = AF_INET, .sin_port = htons(NS_B_PORT),
        .sin_addr.s_addr = inet_addr(NS_B_GRP),
    };
    { int ttl = 4; setsockopt(ns_b_fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl)); }

    float temlum   = 0.0f;
    float temlum_a = 0.0f;   /* Triangle A cross-field */
    float pred_err = P_TARGET;
    float pd_fast  = 0.0f;
    float pd_mid   = 0.0f;
    float pd_slow  = 0.0f;
    uint32_t last_tick = UINT32_MAX;

    struct timespec last_dcn_time;
    clock_gettime(CLOCK_MONOTONIC, &last_dcn_time);

    int nfds = ap_fd;
    if (dcn_fd  > nfds) nfds = dcn_fd;
    if (ns_a_fd > nfds) nfds = ns_a_fd;
    nfds++;

    printf("[pi2_dvfs_reader/B] T_target=%.1f P_target=%.4f W_CROSS=%.2f intent=%s:%d\n",
           T_TARGET, P_TARGET, W_CROSS, intent_ip, INTENT_PORT);
    fflush(stdout);

    uint8_t buf[256];

    while (1) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(ap_fd,   &fds);
        FD_SET(dcn_fd,  &fds);
        FD_SET(ns_a_fd, &fds);
        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };

        if (select(nfds, &fds, NULL, NULL, &tv) < 0) break;

        /* ── AxisPulse → update temlum; check DCN staleness ── */
        if (FD_ISSET(ap_fd, &fds)) {
            ssize_t n = recv(ap_fd, buf, sizeof(buf), 0);
            if (n >= (ssize_t)sizeof(AxisPulse)) {
                uint16_t magic  = (uint16_t)((buf[0] << 8) | buf[1]);
                uint8_t  locked = buf[3];
                uint32_t tick   = (uint32_t)((buf[4]<<24)|(buf[5]<<16)|(buf[6]<<8)|buf[7]);
                if (magic == AP_MAGIC && locked && tick != last_tick) {
                    last_tick = tick;
                    float pd  = be_float(buf + 16);
                    float pd_s = pd - (float)M_PI;
                    pd_fast = ALPHA_FAST * pd_s + (1.0f - ALPHA_FAST) * pd_fast;
                    pd_mid  = ALPHA_MID  * pd_s + (1.0f - ALPHA_MID)  * pd_mid;
                    pd_slow = ALPHA_SLOW * pd_s + (1.0f - ALPHA_SLOW) * pd_slow;
                    float T = read_temp();
                    temlum = ALPHA_T * temlum + (1.0f - ALPHA_T) * (T - T_TARGET);

                    struct timespec now;
                    clock_gettime(CLOCK_MONOTONIC, &now);
                    double age = (double)(now.tv_sec  - last_dcn_time.tv_sec)
                               + (double)(now.tv_nsec - last_dcn_time.tv_nsec) * 1e-9;
                    if (age > DCN_STALE_SECS) {
                        float pd_pop = W_FAST*pd_fast + W_MID*pd_mid + W_SLOW*pd_slow;
                        emit_intent(intent_fd, &intent_dst, ns_b_fd, &ns_b_dst,
                                    0.0f, temlum, pd_pop, "HOLD", 1);
                        printf("[pi2_dvfs_reader/B] HOLD (local stale %.0fs)\n", age);
                        fflush(stdout);
                    }
                }
            }
        }

        /* ── NucleusState-A → update temlum_a cross-field ── */
        if (FD_ISSET(ns_a_fd, &fds)) {
            ssize_t n = recv(ns_a_fd, buf, sizeof(buf), 0);
            if (n >= 16) {
                uint16_t magic = (uint16_t)((buf[0] << 8) | buf[1]);
                if (magic == NS_MAGIC)
                    temlum_a = be_float(buf + 6);   /* temlum at offset 6 */
            }
        }

        /* ── DCN → compute e_C, emit intent ── */
        if (FD_ISSET(dcn_fd, &fds)) {
            ssize_t n = recv(dcn_fd, buf, sizeof(buf), 0);
            if (n < 2) continue;
            uint16_t magic = (uint16_t)((buf[0] << 8) | buf[1]);

            if (magic == HOLD_MAGIC) {
                float pd_pop = W_FAST*pd_fast + W_MID*pd_mid + W_SLOW*pd_slow;
                emit_intent(intent_fd, &intent_dst, ns_b_fd, &ns_b_dst,
                            0.0f, temlum, pd_pop, "HOLD", 1);
                printf("[pi2_dvfs_reader/B] HOLD (cerebellum)\n");
                fflush(stdout);
                clock_gettime(CLOCK_MONOTONIC, &last_dcn_time);
                continue;
            }

            if (magic != DCN_MAGIC || n < 6) continue;
            pred_err = be_float(buf + 2);
            clock_gettime(CLOCK_MONOTONIC, &last_dcn_time);

            float e_P    = pred_err - P_TARGET;
            float pd_pop = W_FAST*pd_fast + W_MID*pd_mid + W_SLOW*pd_slow;
            float e_C    = W_P * e_P - pd_pop - W_T * temlum - W_CROSS * temlum_a;

            const char *intent;
            uint8_t intent_byte;
            if      (e_C >  E_UNPARK) { intent = "UNPARK"; intent_byte = 2; }
            else if (e_C < -E_PARK)   { intent = "PARK";   intent_byte = 0; }
            else                       { intent = "HOLD";   intent_byte = 1; }

            printf("[pi2_dvfs_reader/B] pred=%.5f temlum=%+.3f temlum_a=%+.3f "
                   "pf=%+.4f pm=%+.4f ps=%+.4f pop=%+.5f e_C=%+.5f → %s\n",
                   pred_err, temlum, temlum_a,
                   pd_fast, pd_mid, pd_slow, pd_pop, e_C, intent);
            fflush(stdout);

            emit_intent(intent_fd, &intent_dst, ns_b_fd, &ns_b_dst,
                        e_C, temlum, pd_pop, intent, intent_byte);
        }
    }

    return 0;
}
