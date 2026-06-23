/*
 * entropy_reader.c — Kuramoto phase → /dev/urandom entropy injection
 *
 * Subscribes to AxisPulse multicast (239.0.0.2:7404).
 * Each cycle derives an entropy word and injects it via RNDADDENTROPY.
 *
 * Modes:
 *   no secret:  word = tick ^ θ_bits          (presence-on-wire proof)
 *   --secret S: word = SipHash-2-4(tick||θ, key(S))  (presence + knowledge)
 *
 * The resulting /dev/urandom output is only reproducible by someone who
 * observed the beacon AND held the secret at that cycle. No secret stored.
 *
 * Usage: sudo ./entropy_reader [reader_ip] [--secret <passphrase>]
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <endian.h>
#include <fcntl.h>
#include <linux/random.h>
#include <math.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <inttypes.h>
#include <unistd.h>

#define AXIS_PORT   7404
#define AXIS_GRP    "239.0.0.2"
#define LOAD_PORT   7405
#define AXIS_MAGIC  0x4158
#define LOAD_MAGIC  0x4C44

/* Conservative entropy credit per injection (bits).
 * Locked: tick is 32-bit counter + theta adds ~8 bits of real oscillator noise.
 * Unlocked: theta less structured; credit halved. */
#define ENTROPY_BITS_LOCKED   40
#define ENTROPY_BITS_UNLOCKED 16

typedef struct __attribute__((packed)) {
    uint16_t magic; uint8_t sid; uint8_t locked; uint32_t tick;
    float theta1; float theta2; float pd; float pd_dev;
    float load_avg; uint16_t drains; uint64_t t0_ns;
} AxisPulse;  /* 38 bytes */

typedef struct __attribute__((packed)) {
    uint16_t magic; float load; float temp;
} LoadFeedback;

static inline float ntohf(float f) {
    uint32_t u; memcpy(&u, &f, 4); u = ntohl(u); memcpy(&f, &u, 4); return f;
}
static inline float htonf(float f) { return ntohf(f); }

/* ── SipHash-2-4 inline ──────────────────────────────────────────────────── */

#define ROTL64(x, b) (((x) << (b)) | ((x) >> (64 - (b))))
#define SIP_ROUND(v0, v1, v2, v3) do {              \
    v0 += v1; v1 = ROTL64(v1, 13); v1 ^= v0;       \
    v0 = ROTL64(v0, 32);                            \
    v2 += v3; v3 = ROTL64(v3, 16); v3 ^= v2;       \
    v0 += v3; v3 = ROTL64(v3, 21); v3 ^= v0;       \
    v2 += v1; v1 = ROTL64(v1, 17); v1 ^= v2;       \
    v2 = ROTL64(v2, 32);                            \
} while (0)

static uint64_t siphash24(const void *in, size_t len, uint64_t k0, uint64_t k1) {
    uint64_t v0 = k0 ^ 0x736f6d6570736575ULL;
    uint64_t v1 = k1 ^ 0x646f72616e646f6dULL;
    uint64_t v2 = k0 ^ 0x6c7967656e657261ULL;
    uint64_t v3 = k1 ^ 0x7465646279746573ULL;
    const uint8_t *p = in;
    size_t left = len;
    while (left >= 8) {
        uint64_t m; memcpy(&m, p, 8);
        v3 ^= m; SIP_ROUND(v0,v1,v2,v3); SIP_ROUND(v0,v1,v2,v3); v0 ^= m;
        p += 8; left -= 8;
    }
    uint64_t last = (uint64_t)len << 56;
    for (size_t i = 0; i < left; i++) last |= (uint64_t)p[i] << (i * 8);
    v3 ^= last; SIP_ROUND(v0,v1,v2,v3); SIP_ROUND(v0,v1,v2,v3); v0 ^= last;
    v2 ^= 0xff;
    SIP_ROUND(v0,v1,v2,v3); SIP_ROUND(v0,v1,v2,v3);
    SIP_ROUND(v0,v1,v2,v3); SIP_ROUND(v0,v1,v2,v3);
    return v0 ^ v1 ^ v2 ^ v3;
}

/* derive SipHash key pair from passphrase via FNV-1a double-pass */
static void key_from_secret(const char *s, uint64_t *k0, uint64_t *k1) {
    uint64_t h = 14695981039346656037ULL;
    for (const char *c = s; *c; c++) { h ^= (uint8_t)*c; h *= 1099511628211ULL; }
    *k0 = h;
    h ^= 0xdeadbeefcafe0000ULL;
    for (const char *c = s; *c; c++) { h ^= (uint8_t)*c; h *= 1099511628211ULL; }
    *k1 = h;
}

/* ── entropy injection ───────────────────────────────────────────────────── */

static int urandom_fd = -1;

static void inject(uint64_t word, int entropy_bits) {
    struct {
        int entropy_count;
        int buf_size;
        uint32_t buf[2];
    } rpi;
    rpi.entropy_count = entropy_bits;
    rpi.buf_size      = 8;
    rpi.buf[0]        = (uint32_t)(word & 0xffffffffULL);
    rpi.buf[1]        = (uint32_t)(word >> 32);
    ioctl(urandom_fd, RNDADDENTROPY, &rpi);
}

/* ── main ────────────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    const char *reader_ip = "10.0.0.122";
    const char *secret    = NULL;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--secret") && i + 1 < argc) secret = argv[++i];
        else if (argv[i][0] != '-') reader_ip = argv[i];
    }

    uint64_t k0 = 0, k1 = 0;
    if (secret) key_from_secret(secret, &k0, &k1);

    urandom_fd = open("/dev/urandom", O_WRONLY);
    if (urandom_fd < 0) { perror("open /dev/urandom"); return 1; }

    /* axis multicast receiver */
    int axis_fd = socket(AF_INET, SOCK_DGRAM, 0);
    { int one = 1; setsockopt(axis_fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one)); }
    struct sockaddr_in aa = {
        .sin_family = AF_INET, .sin_port = htons(AXIS_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY)
    };
    bind(axis_fd, (struct sockaddr *)&aa, sizeof(aa));
    struct ip_mreq mreq;
    inet_pton(AF_INET, AXIS_GRP, &mreq.imr_multiaddr);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    setsockopt(axis_fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));

    /* load feedback socket */
    int load_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in la = {
        .sin_family = AF_INET, .sin_port = htons(LOAD_PORT)
    };
    inet_pton(AF_INET, reader_ip, &la.sin_addr);

    fprintf(stderr, "[entropy] axis %s:%d  feedback→%s:%d  mode=%s\n",
            AXIS_GRP, AXIS_PORT, reader_ip, LOAD_PORT,
            secret ? "presence+secret" : "presence-only");

    AxisPulse ap;
    uint64_t  injected   = 0;
    uint32_t  tick_count = 0;
    uint64_t  t0_ns      = 0;

    while (1) {
        ssize_t n = recv(axis_fd, &ap, sizeof(ap), 0);
        if (n != (ssize_t)sizeof(ap)) continue;
        if (ntohs(ap.magic) != AXIS_MAGIC) continue;

        if (ap.t0_ns) t0_ns = be64toh(ap.t0_ns);
        uint32_t tick  = ntohl(ap.tick);
        uint64_t utc_ns = t0_ns
            ? t0_ns + (uint64_t)tick * 50000000ULL : 0;
        (void)utc_ns;

        float    theta = ntohf(ap.theta1);
        int      locked = ap.locked;

        /* build input block: tick (4 bytes) || theta bits (4 bytes) */
        uint32_t theta_bits; memcpy(&theta_bits, &theta, 4);
        uint8_t  blk[8];
        memcpy(blk,     &tick,       4);
        memcpy(blk + 4, &theta_bits, 4);

        uint64_t word;
        if (secret) {
            word = siphash24(blk, 8, k0, k1);
        } else {
            word = (uint64_t)tick ^ ((uint64_t)theta_bits << 32) ^ theta_bits;
        }

        int bits = locked ? ENTROPY_BITS_LOCKED : ENTROPY_BITS_UNLOCKED;
        inject(word, bits);
        injected++;

        /* load feedback */
        LoadFeedback lf;
        lf.magic = htons(LOAD_MAGIC);
        lf.load  = htonf(0.0f);
        lf.temp  = htonf(-1.0f);
        sendto(load_fd, &lf, sizeof(lf), 0, (struct sockaddr *)&la, sizeof(la));

        if (++tick_count % 40 == 0) {
            fprintf(stderr, "\r[entropy] θ=%.4f  tick=%u  injected=%-6" PRIu64
                    "  bits/cycle=%-2d  %s    ",
                    theta, tick, injected, bits, locked ? "LOCKED" : "hunting");
        }
    }

    close(urandom_fd);
    return 0;
}
