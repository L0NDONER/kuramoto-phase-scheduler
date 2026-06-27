#define _GNU_SOURCE
/*
 * pi2_reader.c — temlum controller for Pi2 topology.
 *
 * Inputs:
 *   239.0.0.2:7404  AxisPulse multicast  — tick pacing; updates temlum each locked tick
 *   0.0.0.0:7430    DCN from cerebellum  — pred_err_ema; triggers intent emit
 *
 * Output:
 *   127.0.0.1:7431  ASCII intent         — "PARK\n" / "UNPARK\n" / "HOLD\n"
 *
 * Control law:
 *   temlum = α·temlum_prev + (1−α)·(T − T_target)
 *   e_C    = W_P·(pred_err − P_target) − W_T·temlum
 *   e_C >  E_UNPARK → UNPARK
 *   e_C < −E_PARK   → PARK
 *   else             → HOLD
 *
 * Build: gcc -O2 -o pi2_reader pi2_reader.c -lm
 * Run:   sudo ./pi2_reader [intent_ip]   (default 127.0.0.1)
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

/* ── control params ──────────────────────────────────────────────────────── */
#define T_TARGET    84.0f
#define ALPHA_T     0.95f
#define P_TARGET    0.0045f
#define W_P         1.0f
#define W_T         0.15f
#define W_PD        0.008f   /* signed phase deviation weight */
#define E_UNPARK    0.0040f
#define E_PARK      0.0030f

/* ── ports ───────────────────────────────────────────────────────────────── */
#define AXIS_PORT    7404
#define AXIS_GRP     "239.0.0.2"
#define DCN_PORT     7430
#define INTENT_PORT  7431

/* ── wire magic ──────────────────────────────────────────────────────────── */
#define AP_MAGIC   0x4158   /* "AX" */
#define DCN_MAGIC  0x4443   /* "DC" */

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

/* ── DCN packet (6 bytes, big-endian) ────────────────────────────────────── */
typedef struct __attribute__((packed)) {
    uint16_t magic;
    float    pred_err;
} DcnPkt;

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

int main(int argc, char **argv) {
    const char *intent_ip = (argc > 1) ? argv[1] : "127.0.0.1";

    /* AxisPulse multicast socket */
    int ap_fd = socket(AF_INET, SOCK_DGRAM, 0);
    int yes = 1;
    setsockopt(ap_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in ap_addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(AXIS_PORT),
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
        .sin_family      = AF_INET,
        .sin_port        = htons(DCN_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(dcn_fd, (struct sockaddr *)&dcn_addr, sizeof(dcn_addr));

    /* Intent send socket */
    int intent_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in intent_dst = {
        .sin_family      = AF_INET,
        .sin_port        = htons(INTENT_PORT),
        .sin_addr.s_addr = inet_addr(intent_ip),
    };

    float temlum    = 0.0f;
    float pred_err  = P_TARGET;
    float pd_signed = 0.0f;   /* pd − π: negative = below target (BOOST), positive = above (PARK) */
    uint32_t last_tick = UINT32_MAX;

    printf("[pi2_reader] T_target=%.1f P_target=%.4f intent=%s:%d\n",
           T_TARGET, P_TARGET, intent_ip, INTENT_PORT);
    fflush(stdout);

    uint8_t buf[256];
    int nfds = (ap_fd > dcn_fd ? ap_fd : dcn_fd) + 1;

    while (1) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(ap_fd, &fds);
        FD_SET(dcn_fd, &fds);
        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };

        if (select(nfds, &fds, NULL, NULL, &tv) < 0) break;

        /* ── AxisPulse → update temlum ── */
        if (FD_ISSET(ap_fd, &fds)) {
            ssize_t n = recv(ap_fd, buf, sizeof(buf), 0);
            if (n < (ssize_t)sizeof(AxisPulse)) goto dcn;
            uint16_t magic  = (uint16_t)((buf[0] << 8) | buf[1]);
            if (magic != AP_MAGIC) goto dcn;
            uint8_t  locked = buf[3];
            uint32_t tick   = (uint32_t)((buf[4]<<24)|(buf[5]<<16)|(buf[6]<<8)|buf[7]);
            if (!locked || tick == last_tick) goto dcn;
            last_tick = tick;

            float pd = be_float(buf + 16);   /* AxisPulse pd field (offset 16) */
            pd_signed = pd - (float)M_PI;

            float T = read_temp();
            float e_T = T - T_TARGET;
            temlum = ALPHA_T * temlum + (1.0f - ALPHA_T) * e_T;
        }

dcn:
        /* ── DCN → compute e_C, emit intent ── */
        if (FD_ISSET(dcn_fd, &fds)) {
            ssize_t n = recv(dcn_fd, buf, sizeof(buf), 0);
            if (n < 6) continue;
            uint16_t magic = (uint16_t)((buf[0] << 8) | buf[1]);
            if (magic != DCN_MAGIC) continue;
            pred_err = be_float(buf + 2);

            float e_P = pred_err - P_TARGET;
            float e_C = W_P * e_P - W_PD * pd_signed - W_T * temlum;

            const char *intent;
            if      (e_C >  E_UNPARK) intent = "UNPARK";
            else if (e_C < -E_PARK)   intent = "PARK";
            else                       intent = "HOLD";

            printf("[pi2_reader] pred=%.5f temlum=%+.3f e_C=%+.5f → %s\n",
                   pred_err, temlum, e_C, intent);
            fflush(stdout);

            sendto(intent_fd, intent, strlen(intent), 0,
                   (struct sockaddr *)&intent_dst, sizeof(intent_dst));
        }
    }

    return 0;
}
