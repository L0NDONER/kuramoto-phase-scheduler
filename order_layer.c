/* order_layer.c — Order Layer: quartz-paced tick counter, drift tracking,
 * forward-cone admissibility, reflex classification, and the shared-memory
 * ring buffer Commander reads from (Conveyance).
 *
 * Two-host mode: each instance sends a timestamped PROBE to its peer every
 * tick; on receiving a peer's PROBE it immediately fires back an ECHO with
 * the same payload untouched; when its own ECHO returns, it captures the
 * kernel's SO_TIMESTAMPING RX timestamp — the same API used for real NIC
 * hardware timestamping — and measures round-trip drift across the actual
 * LAN link. This replaces the earlier loopback self-probe, whose jitter
 * was OS-scheduler noise carrying zero transport information (see
 * project_reflex_transport_geometry in memory) — reflex must be calibrated
 * against real transport geometry, not against this box's own scheduler.
 *
 * Pipeline: NIC/kernel RX timestamp (CLOCK_REALTIME domain) -> converted
 * into CLOCK_MONOTONIC_RAW (the quartz spine's domain) -> compared against
 * the send timestamp carried in the probe payload -> fed as `drift` into
 * both the forward-cone admissibility gate and the reflex classifier.
 *
 * Build:  gcc -O2 -o order_layer order_layer.c -lrt -lm
 * Run:    ./order_layer {mint|lmde} [port]
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/errqueue.h>
#include <linux/net_tstamp.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/timex.h>
#include <time.h>
#include <unistd.h>

#include "order_layer.h"

#define TICK_S          0.01
#define OMEGA0          1.06
#define RESAMPLE_EVERY  3000
#define DEFAULT_PORT    27400

#define KIND_PROBE 0
#define KIND_ECHO  1
#define NET_MAGIC  0x4F4C4E50u  /* "OLNP" */

static const struct { const char *name; const char *ip; } HOSTS[] = {
    { "mint", "10.0.0.71"  },
    { "lmde", "10.0.0.175" },
};

/* Forward-cone admissibility: a tick is admitted only if its measured
 * drift falls inside a bounded window — the same binary admit/reject
 * primitive as the pulse seqlock below (see project_conveyance_bimodal_gate
 * in memory): no third state, no confidence score, just in-bound or dropped. */
#define MAX_DRIFT_MS       50.0f

/* Reflex thresholds — same CALM/ALERT/WITHDRAW/RECOVER semantics as
 * reflex.py, but NOT yet calibrated: this build's drift comes from a
 * loopback self-probe, which is OS-scheduler/local-clock noise, not
 * network-transport geometry. reflex.py's production classifier answers
 * "is the network run degrading" (pd_dev between real quartz nodes);
 * Order Layer's reflex is meant to succeed that classifier, not to become
 * a hardware-health check on this one box. Loopback jitter carries zero
 * transport information, so these numbers only prove the state machine is
 * reachable at all (confirmed unreachable under real CPU stress at the old
 * 0.5/2.0ms floors — peak observed was 0.37ms). Do not tune these from any
 * further loopback measurement. Real calibration requires a real transport
 * in the loop — a two-host LAN exchange (sandboxed hosts only, never
 * production Pi beacons) analogous to quartz_beacon.py's peer exchange,
 * feeding real inter-node drift through this same pipeline. See
 * project_reflex_transport_geometry in memory. */
#define ALERT_DRIFT_MS     0.5f
#define WITHDRAW_DRIFT_MS  2.0f
#define CALM_HOLD_S        5.0
#define RECOVER_HOLD_S     10.0

/* Staleness: drift_ms only updates when an echo actually arrives, so a dead
 * peer leaves the last real number sitting there looking current forever.
 * This is substrate-level staleness (no fresh admitted measurement at all),
 * not Nazaré-level staleness (reflex.py's AP_STALE_S, which watches for a
 * missing AxisPulse packet) — same shape, different layer. If nothing has
 * landed in STALE_WINDOW_S, reflex is forced to WITHDRAW regardless of what
 * drift_ms still says, because drift_ms is no longer a live observation. */
#define STALE_WINDOW_S     3.0

/* Beyond DEAD_WINDOW_S, no measurement for that long stops looking like a
 * transient stall and starts looking like the peer process is actually
 * gone. Reflex treats STALE and DEAD identically today (both force
 * WITHDRAW) — signal_state exists so Commander can tell them apart even
 * though reflex currently can't/doesn't need to. */
#define DEAD_WINDOW_S      15.0

typedef struct {
    uint32_t magic;
    uint8_t  kind;      /* KIND_PROBE or KIND_ECHO */
    uint64_t tick;      /* sender's own tick counter, for logging/attribution only */
    uint64_t t_send_ns; /* sender's CLOCK_MONOTONIC_RAW at send, unchanged through the echo */
} __attribute__((packed)) NetProbe;

static volatile sig_atomic_t running = 1;
static void on_sigint(int sig) { (void)sig; running = 0; }

/* Single centralized transition point for the reflex state machine — same
 * role as reflex.py's go(): any state change unconditionally clears both
 * hold-timers, so no case branch has to remember to reset them itself. */
static uint8_t reflex_state    = REFLEX_CALM;
static double  alert_clear_at  = 0.0;
static double  recover_since_t = 0.0;
static int     have_clear      = 0, have_recover = 0;

static void set_reflex(uint8_t new_state) {
    if (new_state == reflex_state) return;
    reflex_state = new_state;
    have_clear = have_recover = 0;
}

static double read_freq_ppm(void) {
    struct timex tx;
    memset(&tx, 0, sizeof(tx));
    if (adjtimex(&tx) < 0) { perror("adjtimex"); return 0.0; }
    return tx.freq / 65536.0;
}

static double now_raw(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* Bind a UDP socket on all interfaces (so it receives from the real LAN
 * NIC, not just loopback) and ask the kernel for RX timestamps. Hardware
 * flags are requested first — if this NIC has a PHC, real hardware
 * timestamps land in ts[2]; otherwise we fall back to the software domain
 * (ts[0]), same code path either way. */
static int setup_ts_socket(int port) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); exit(1); }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) { perror("bind"); exit(1); }

    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    int ts_flags = SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE |
                   SOF_TIMESTAMPING_RX_SOFTWARE | SOF_TIMESTAMPING_SOFTWARE;
    if (setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING, &ts_flags, sizeof(ts_flags)) < 0) {
        ts_flags = SOF_TIMESTAMPING_RX_SOFTWARE | SOF_TIMESTAMPING_SOFTWARE;
        if (setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING, &ts_flags, sizeof(ts_flags)) < 0) {
            perror("setsockopt SO_TIMESTAMPING");
            exit(1);
        }
        fprintf(stderr, "[order_layer] hardware timestamping unavailable, using software domain\n");
    }

    return fd;
}

/* Drain arrived packets. A peer PROBE is echoed straight back (untouched
 * payload, no timestamp needed for the echo send itself). A returning ECHO
 * of our own PROBE is where the real measurement happens: convert the
 * kernel's RX timestamp into the quartz spine's MONOTONIC_RAW domain and
 * diff it against the send time we stamped the PROBE with — that diff is
 * real round-trip latency/jitter across the actual LAN link, not a
 * self-referential loopback placeholder. Updates *drift_ms; returns 1 if
 * at least one ECHO measurement landed this call. */
static int drain_ts_socket(int fd, const struct sockaddr_in *peer_addr, float *drift_ms) {
    char pbuf[64];
    char cbuf[512];
    int got_any = 0;

    for (;;) {
        struct iovec iov = { .iov_base = pbuf, .iov_len = sizeof(pbuf) };
        struct sockaddr_in src;
        struct msghdr msg;
        memset(&msg, 0, sizeof(msg));
        msg.msg_name = &src;
        msg.msg_namelen = sizeof(src);
        msg.msg_iov = &iov;
        msg.msg_iovlen = 1;
        msg.msg_control = cbuf;
        msg.msg_controllen = sizeof(cbuf);

        ssize_t n = recvmsg(fd, &msg, MSG_DONTWAIT);
        if (n < 0) break;
        if ((size_t)n < sizeof(NetProbe)) continue;

        NetProbe probe;
        memcpy(&probe, pbuf, sizeof(probe));
        if (probe.magic != NET_MAGIC) continue;

        if (probe.kind == KIND_PROBE) {
            probe.kind = KIND_ECHO;
            sendto(fd, &probe, sizeof(probe), 0,
                   (const struct sockaddr *)peer_addr, sizeof(*peer_addr));
            continue;
        }
        /* KIND_ECHO: our own probe, reflected back — extract the RX timestamp. */

        struct timespec rx_ts = {0, 0};
        int have_ts = 0;
        for (struct cmsghdr *cm = CMSG_FIRSTHDR(&msg); cm; cm = CMSG_NXTHDR(&msg, cm)) {
            if (cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_TIMESTAMPING) continue;
            struct scm_timestamping *tss = (struct scm_timestamping *)CMSG_DATA(cm);
            if (tss->ts[2].tv_sec || tss->ts[2].tv_nsec) { rx_ts = tss->ts[2]; have_ts = 1; }      /* hardware */
            else if (tss->ts[0].tv_sec || tss->ts[0].tv_nsec) { rx_ts = tss->ts[0]; have_ts = 1; } /* software */
        }
        if (!have_ts) continue;

        /* rx_ts is in CLOCK_REALTIME (or a PHC synced to it). Convert to
         * MONOTONIC_RAW by sampling the two clocks' offset right now — over
         * the microsecond span this measurement cares about, that offset
         * only moves via NTP steps, not free-running drift. */
        struct timespec real_now, mono_now;
        clock_gettime(CLOCK_REALTIME, &real_now);
        clock_gettime(CLOCK_MONOTONIC_RAW, &mono_now);
        int64_t offset_ns = (int64_t)(mono_now.tv_sec - real_now.tv_sec) * 1000000000LL
                           + (mono_now.tv_nsec - real_now.tv_nsec);
        int64_t rx_ns      = (int64_t)rx_ts.tv_sec * 1000000000LL + rx_ts.tv_nsec;
        int64_t rx_mono_ns = rx_ns + offset_ns;

        int64_t delta_ns = rx_mono_ns - (int64_t)probe.t_send_ns;
        *drift_ms = (float)(fabs((double)delta_ns) / 1e6);
        got_any = 1;
    }
    return got_any;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s {mint|lmde} [port]\n", argv[0]);
        return 1;
    }
    const char *self_ip = NULL, *peer_ip = NULL, *peer_name = NULL;
    for (size_t i = 0; i < sizeof(HOSTS) / sizeof(HOSTS[0]); i++) {
        if (strcmp(argv[1], HOSTS[i].name) == 0) self_ip = HOSTS[i].ip;
    }
    if (!self_ip) { fprintf(stderr, "unknown host '%s'\n", argv[1]); return 1; }
    for (size_t i = 0; i < sizeof(HOSTS) / sizeof(HOSTS[0]); i++) {
        if (strcmp(argv[1], HOSTS[i].name) != 0) { peer_ip = HOSTS[i].ip; peer_name = HOSTS[i].name; }
    }
    int port = argc >= 3 ? atoi(argv[2]) : DEFAULT_PORT;

    signal(SIGINT, on_sigint);
    signal(SIGTERM, on_sigint);

    int fd = shm_open(ORDER_LAYER_SHM_NAME, O_CREAT | O_RDWR, 0666);
    if (fd < 0) { perror("shm_open"); return 1; }
    if (ftruncate(fd, sizeof(OrderLayerShm)) < 0) { perror("ftruncate"); return 1; }

    OrderLayerShm *shm = mmap(NULL, sizeof(OrderLayerShm), PROT_READ | PROT_WRITE,
                              MAP_SHARED, fd, 0);
    if (shm == MAP_FAILED) { perror("mmap"); return 1; }
    memset(shm, 0, sizeof(OrderLayerShm));
    shm->magic = ORDER_LAYER_MAGIC;

    int ts_fd = setup_ts_socket(port);

    struct sockaddr_in peer_addr;
    memset(&peer_addr, 0, sizeof(peer_addr));
    peer_addr.sin_family = AF_INET;
    peer_addr.sin_port = htons(port);
    inet_pton(AF_INET, peer_ip, &peer_addr.sin_addr);

    double ppm = read_freq_ppm();
    double omega = OMEGA0 * (1.0 + ppm * 1e-6);
    fprintf(stderr, "[order_layer] self=%s(%s) peer=%s(%s) port=%d freq_ppm=%.6f omega=%.9f shm=%s size=%zu\n",
            argv[1], self_ip, peer_name, peer_ip, port, ppm, omega,
            ORDER_LAYER_SHM_NAME, sizeof(OrderLayerShm));

    float    drift_ms        = 0.0f;   /* last real measurement; holds steady between arrivals */
    double   last_measurement_t = now_raw();  /* substrate staleness clock, see STALE_WINDOW_S */

    double t0 = now_raw();
    uint64_t tick = 0;
    double next_tick_at = t0;

    while (running) {
        double now = now_raw();
        if (now < next_tick_at) {
            struct timespec req = { .tv_sec = 0,
                                     .tv_nsec = (long)((next_tick_at - now) * 1e9) };
            if (req.tv_nsec > 0) nanosleep(&req, NULL);
            continue;
        }
        next_tick_at = now + TICK_S;

        if (tick && tick % RESAMPLE_EVERY == 0) {
            double theta_now = fmod(omega * (now - t0), 2 * M_PI);
            ppm = read_freq_ppm();
            omega = OMEGA0 * (1.0 + ppm * 1e-6);
            t0 = now - theta_now / omega;
        }
        if (drain_ts_socket(ts_fd, &peer_addr, &drift_ms)) last_measurement_t = now;

        /* Send this tick's probe to the peer; the echo (and hence the real
         * measurement) is drained on whatever future iteration it actually
         * arrives on — never assumed to be this tick or the next. */
        NetProbe probe = { .magic = NET_MAGIC, .kind = KIND_PROBE,
                            .tick = tick, .t_send_ns = (uint64_t)(now * 1e9) };
        sendto(ts_fd, &probe, sizeof(probe), 0,
               (struct sockaddr *)&peer_addr, sizeof(peer_addr));

        int admissible = drift_ms <= MAX_DRIFT_MS;
        double since_measurement = now - last_measurement_t;
        int stale = since_measurement > STALE_WINDOW_S;
        uint8_t signal_state = since_measurement > DEAD_WINDOW_S  ? SIGNAL_DEAD
                              : since_measurement > STALE_WINDOW_S ? SIGNAL_STALE
                              : SIGNAL_LIVE;

        if (stale) {
            /* No fresh admitted measurement at all — drift_ms is a frozen
             * number, not a live observation. Force WITHDRAW outright,
             * bypassing the hysteresis machine below entirely, since
             * "regardless of drift_ms" means the switch-case must not
             * get a vote here (it could otherwise walk WITHDRAW -> RECOVER
             * on a stale-but-low frozen value). */
            set_reflex(REFLEX_WITHDRAW);
        } else {
            /* Reflex classification with hysteresis, same shape as reflex.py.
             * set_reflex() resets both hold-timers on any actual transition;
             * branches below only need to manage same-state timer bookkeeping. */
            switch (reflex_state) {
            case REFLEX_CALM:
                if (drift_ms > WITHDRAW_DRIFT_MS)       set_reflex(REFLEX_WITHDRAW);
                else if (drift_ms > ALERT_DRIFT_MS)      set_reflex(REFLEX_ALERT);
                break;
            case REFLEX_ALERT:
                if (drift_ms > WITHDRAW_DRIFT_MS)       set_reflex(REFLEX_WITHDRAW);
                else if (drift_ms <= ALERT_DRIFT_MS) {
                    if (!have_clear) { alert_clear_at = now; have_clear = 1; }
                    else if (now - alert_clear_at >= CALM_HOLD_S) set_reflex(REFLEX_CALM);
                } else have_clear = 0;
                break;
            case REFLEX_WITHDRAW:
                if (drift_ms <= ALERT_DRIFT_MS) set_reflex(REFLEX_RECOVER);
                break;
            case REFLEX_RECOVER:
                if (drift_ms > WITHDRAW_DRIFT_MS)       set_reflex(REFLEX_WITHDRAW);
                else if (drift_ms > ALERT_DRIFT_MS)      set_reflex(REFLEX_ALERT);
                else {
                    if (!have_recover) { recover_since_t = now; have_recover = 1; }
                    else if (now - recover_since_t >= RECOVER_HOLD_S) set_reflex(REFLEX_CALM);
                }
                break;
            }
        }

        shm->pulse++;                       /* now odd: update in progress */
        if (admissible) {
            uint64_t slot = shm->write_idx % RING_SLOTS;
            RingEntry *e = &shm->ring[slot];
            e->tick_id = tick;
            memset(e->payload, 0, PAYLOAD_SIZE);
            snprintf((char *)e->payload, PAYLOAD_SIZE, "tick=%llu",
                     (unsigned long long)tick);
            e->drift = drift_ms;
            e->reflex_state = reflex_state;
            e->signal_state = signal_state;
            e->admissible = 1;
            e->t_ns = (uint64_t)(now * 1e9);
            shm->write_idx++;
        }
        /* else: strict drop — no ring write. reflex/signal/drift and the
         * pulse counter below still advance every tick regardless, so
         * Commander's pacing never stalls on a dropped tick. */
        shm->reflex_state = reflex_state;
        shm->signal_state = signal_state;
        shm->drift = drift_ms;
        shm->pulse++;                       /* now even: update stable */

        if (tick % 500 == 0) {
            fprintf(stderr, "[order_layer] tick=%llu drift_ms=%.4f reflex=%d signal=%d admissible=%d\n",
                    (unsigned long long)tick, drift_ms, reflex_state, signal_state, admissible);
        }
        tick++;
    }

    close(ts_fd);
    munmap(shm, sizeof(OrderLayerShm));
    close(fd);
    shm_unlink(ORDER_LAYER_SHM_NAME);
    return 0;
}
