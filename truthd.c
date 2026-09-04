/**
 * truthd.c — exposes the current QuorumTier (truth_manifest.h) over a
 * local Unix socket. One line in, one word out, nothing else. This
 * process never writes anything -- the manifest it enforces is
 * compiled in, and the only things it produces at runtime are reads of
 * local state and a passive multicast join.
 *
 * 2026-09-04: local-triad health used to come from
 * /tmp/quartz_peer_health.json, written by quartz_node's AxisPulse
 * mesh-coupling telemetry. quartz_node was retired 2026-09-03 (see
 * [[project_quartz_metro_node]] / the night's "no more quartz" call);
 * that file went stale forever and silently wedged this daemon at
 * TIER_PARTITIONED for ~11h before anyone noticed -- caught only by
 * checking LOCAL_HEAL/LOCAL_DESTRUCTIVE end to end, not by anything
 * that alarmed on its own. Local-triad health is now: is I(0)
 * (layer0_daemon.py, 239.0.0.6:7500) fresh at this host. Same
 * freshness-gate pattern i0_ingestion_buffer/i0_server.c already
 * validated live -- reused verbatim here, not reinvented. This is a
 * real narrowing of what "healthy" meant: the old check independently
 * confirmed 2 named peers were each reporting good phase-lock; I(0)
 * freshness only proves this host can hear Mint's broadcast. It does
 * NOT independently prove the other peer is alive. Accepted per
 * project directive: I(0) is the one shared ground truth now, and
 * every other peer-mesh consensus mechanism this codebase tried
 * added cost without adding real predictive or safety value (see
 * [[project_substrate_smooths_not_predicts]]).
 *
 * EC2 signal: I(0) is Mint-LAN-only (multicast doesn't cross the WAN),
 * so it can't appear on EC2 at all. Instead, glyph/ec2_probe.py
 * (Mint-only -- it's the only host holding ec2_intent.key) polls EC2
 * with a signed BENIGN_READ intent and writes EC2_REACHABLE_PATH. This
 * is a sensor reading, never actuation: it only ever narrows/widens
 * what this daemon reports, it can't reach back into the probe or the
 * substrate.
 *
 *   TIER_PARTITIONED  -- I(0) not fresh at this host (substrate
 *                        unreachable/dead)
 *   TIER_LOCAL_TRIAD  -- I(0) fresh, EC2_REACHABLE_PATH missing,
 *                        stale, or reachable:false
 *   TIER_FULL         -- I(0) fresh AND EC2_REACHABLE_PATH fresh
 *                        with reachable:true
 *
 * --witness mode (EC2 only): run as `./truthd --witness`. EC2 has no
 * LAN to join I(0) on and can never independently confirm the local
 * triad is intact. Instead it reads WITNESS_PATH, written by
 * ec2_intent_listener.py from a signed "witness:<TIER>" report sent by
 * Mint's own truthd (see ec2_probe.py) over the existing HMAC channel.
 * This is a trust shift, not a stronger guarantee: EC2's gate now
 * depends on whoever holds ec2_intent.key telling the truth, same trust
 * level as every other command in this system, not a cryptographic
 * proof of physical quorum. It can only narrow what EC2 grants itself
 * (missing/stale/PARTITIONED witness -> TIER_PARTITIONED, fail-closed)
 * -- never more than TRUTH_ALLOWED already permits regardless of what's
 * claimed.
 *
 * Protocol (one line request, one line response, connection closes after):
 *   ""                    -> "<TIER_NAME>"               -- what tier now
 *   "CHECK <VERB_NAME>\n" -> "ALLOW <TIER_NAME>"          -- gate hook query
 *                          | "DENY <TIER_NAME>"
 *                          | "DENY UNKNOWN_VERB"
 * VERB_NAME is one of truth_manifest.h's VERB_NAMES (BENIGN_READ,
 * LOCAL_HEAL, LOCAL_DESTRUCTIVE, EC2_SELF). The ALLOW/DENY decision is
 * TRUTH_ALLOWED[current_tier][verb] -- callers never see or reimplement
 * that table, they just ask.
 *
 * Compile: gcc -O2 -o truthd truthd.c -lpthread
 * Run:     ./truthd            (Mint/Pi1/Pi2 -- local-triad mode)
 *          ./truthd --witness  (EC2 -- witness mode)
 * Query:   printf '' | nc -U /tmp/truthd.sock
 *          printf 'CHECK LOCAL_HEAL\n' | nc -U /tmp/truthd.sock
 */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <time.h>
#include <errno.h>

#include "truth_manifest.h"

#define EC2_REACHABLE_PATH "/tmp/ec2_reachable.json"
#define WITNESS_PATH "/tmp/mint_witness.json"
#define SOCK_PATH   "/tmp/truthd.sock"
#define EC2_STALE_S 15.0       /* 3x ec2_probe.py's PROBE_INTERVAL_S -- margin
                                 * against one missed cycle plus WAN jitter */
#define WITNESS_STALE_S 15.0   /* same cadence as EC2_STALE_S -- ec2_probe.py
                                 * sends the witness report on the same loop */
#define POLL_INTERVAL_US 500000 /* 0.5s, matches the old quartz_presence_chain.py cadence */

#define LAYER0_GRP "239.0.0.6"
#define LAYER0_PORT 7500
#define LAYER0_MAGIC 0x4C30
#define LAYER0_TTL_US 5000000ULL   /* 5s, same TTL every I(0) consumer in this project uses */

static int witness_mode = 0;  /* set from argv[1] == "--witness" */
static QuorumTier current_tier = TIER_PARTITIONED;
static pthread_mutex_t tier_lock = PTHREAD_MUTEX_INITIALIZER;

/* Python's "!HIffd" is network-byte-order, no padding: magic(u16) +
   tick(u32) + theta(f32) + pd(f32) + t0(f64) = 22 bytes. Only magic +
   tick are needed for the freshness gate, so no float/double
   byte-swap helper needed -- ntohs/ntohl cover it. Same parse as
   i0_ingestion_buffer/i0_server.c, reused verbatim. */
#define LAYER0_PKT_SIZE 22

static uint64_t layer0_last_seen_us = 0;   /* guarded by layer0_lock */
static pthread_mutex_t layer0_lock = PTHREAD_MUTEX_INITIALIZER;

static uint64_t monotonic_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

static int layer0_parse(const uint8_t *buf, size_t n, uint32_t *tick_out) {
    if (n < LAYER0_PKT_SIZE) return 0;
    uint16_t magic;
    memcpy(&magic, buf, 2);
    if (ntohs(magic) != LAYER0_MAGIC) return 0;
    uint32_t tick;
    memcpy(&tick, buf + 2, 4);
    *tick_out = ntohl(tick);
    return 1;
}

static int layer0_join_multicast(void) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    int reuse = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
#ifdef SO_REUSEPORT
    setsockopt(sock, SOL_SOCKET, SO_REUSEPORT, &reuse, sizeof(reuse));
#endif
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(LAYER0_PORT);
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("[truthd] layer0 bind"); return -1;
    }
    struct ip_mreq mreq = {0};
    mreq.imr_multiaddr.s_addr = inet_addr(LAYER0_GRP);
    mreq.imr_interface.s_addr = htonl(INADDR_ANY);
    if (setsockopt(sock, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq)) < 0) {
        perror("[truthd] layer0 IP_ADD_MEMBERSHIP"); return -1;
    }
    struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    return sock;
}

/* Not run on EC2 (--witness mode never calls this) -- I(0) is
   Mint-LAN-only, there's nothing to join over the WAN.
 *
 * 2026-09-04: a real reboot of pi1 caught this loop giving up for good
 * on a single failed join (network interface not up yet at the moment
 * systemd started truthd -- "No such device"), permanently wedging the
 * tier at PARTITIONED for the rest of the process's life even after
 * the network came up seconds later. The old HEALTH_PATH-file version
 * had no such boot-ordering dependency at all. Retrying here, not just
 * tightening the systemd unit's ordering, because a network interface
 * can flap at any time after boot too -- this loop should recover from
 * that the same way it recovers from I(0) itself going stale. */
static void *layer0_poll_loop(void *arg) {
    (void)arg;
    for (;;) {
        int sock = layer0_join_multicast();
        if (sock < 0) {
            fprintf(stderr, "[truthd] I(0) multicast join failed, retrying in 2s\n");
            sleep(2);
            continue;
        }
        uint8_t buf[64];
        for (;;) {
            ssize_t n = recv(sock, buf, sizeof(buf), 0);
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) continue;  /* SO_RCVTIMEO tick, not an error */
                break;   /* real socket error -- rejoin from scratch */
            }
            if (n == 0) continue;
            uint32_t tick;
            if (layer0_parse(buf, (size_t)n, &tick)) {
                pthread_mutex_lock(&layer0_lock);
                layer0_last_seen_us = monotonic_us();
                pthread_mutex_unlock(&layer0_lock);
            }
        }
        close(sock);
    }
    return NULL;
}

static int layer0_fresh(void) {
    pthread_mutex_lock(&layer0_lock);
    uint64_t last = layer0_last_seen_us;
    pthread_mutex_unlock(&layer0_lock);
    if (last == 0) return 0;   /* never seen a packet yet */
    return (monotonic_us() - last) <= LAYER0_TTL_US;
}

/* EC2_REACHABLE_PATH is written with its own internal timestamp, but we
 * only need wall-clock mtime freshness here, so stat() is enough -- no
 * need to parse the writer's own clock a second time. */
static int file_is_stale(const char *path, double stale_s) {
    struct stat st;
    if (stat(path, &st) != 0) return 1;
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    double age = (now.tv_sec - st.st_mtim.tv_sec) +
                 (now.tv_nsec - st.st_mtim.tv_nsec) / 1e9;
    return age > stale_s;
}

/* Deliberately not a general JSON parser -- ec2_probe.py writes exactly
 * one fixed object per file, one writer, no need for a real JSON
 * library for a format only one writer ever produces. */
static int ec2_is_reachable(void) {
    if (file_is_stale(EC2_REACHABLE_PATH, EC2_STALE_S)) return 0;

    FILE *f = fopen(EC2_REACHABLE_PATH, "r");
    if (!f) return 0;
    char buf[256];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';

    return strstr(buf, "\"reachable\": true") != NULL;
}

/* EC2 has no local triad of its own to check -- it trusts Mint's signed
 * witness report instead (see the --witness mode note at top of file).
 * A witness claiming PARTITIONED is itself an honest signal: Mint's own
 * triad is degraded, so EC2 shouldn't consider itself part of a solid
 * quorum either. EC2 doesn't need an intermediate LOCAL_TRIAD state for
 * itself -- from a 4th node's perspective it's either part of a
 * complete quorum or it isn't. */
static QuorumTier compute_tier_witness(void) {
    if (file_is_stale(WITNESS_PATH, WITNESS_STALE_S)) return TIER_PARTITIONED;

    FILE *f = fopen(WITNESS_PATH, "r");
    if (!f) return TIER_PARTITIONED;
    char buf[256];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';

    if (strstr(buf, "\"triad_tier\": \"TIER_PARTITIONED\"")) return TIER_PARTITIONED;
    if (strstr(buf, "\"triad_tier\": \"TIER_LOCAL_TRIAD\"")) return TIER_FULL;
    if (strstr(buf, "\"triad_tier\": \"TIER_FULL\"")) return TIER_FULL;
    return TIER_PARTITIONED;  /* malformed/unrecognized -- fail closed */
}

static QuorumTier compute_tier(void) {
    if (witness_mode) return compute_tier_witness();

    if (!layer0_fresh()) return TIER_PARTITIONED;

    return ec2_is_reachable() ? TIER_FULL : TIER_LOCAL_TRIAD;
}

static void *poll_loop(void *arg) {
    (void)arg;
    for (;;) {
        QuorumTier t = compute_tier();
        pthread_mutex_lock(&tier_lock);
        if (t != current_tier) {
            fprintf(stderr, "[truthd] tier change: %s -> %s\n",
                    TIER_NAMES[current_tier], TIER_NAMES[t]);
        }
        current_tier = t;
        pthread_mutex_unlock(&tier_lock);
        usleep(POLL_INTERVAL_US);
    }
    return NULL;
}

static void *serve_loop(void *arg) {
    int listen_fd = *(int *)arg;
    for (;;) {
        int client_fd = accept(listen_fd, NULL, NULL);
        if (client_fd < 0) {
            if (errno == EINTR) continue;
            perror("[truthd] accept");
            continue;
        }

        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
        setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        char req[64] = {0};
        ssize_t n = recv(client_fd, req, sizeof(req) - 1, 0);
        if (n < 0) n = 0;  /* timeout or error -- treat like an empty request */
        req[n] = '\0';
        char *nl = strchr(req, '\n');
        if (nl) *nl = '\0';

        pthread_mutex_lock(&tier_lock);
        QuorumTier t = current_tier;
        pthread_mutex_unlock(&tier_lock);

        if (strncmp(req, "CHECK ", 6) == 0) {
            int verb = truth_verb_from_name(req + 6);
            if (verb < 0) {
                dprintf(client_fd, "DENY UNKNOWN_VERB\n");
            } else {
                dprintf(client_fd, "%s %s\n",
                        TRUTH_ALLOWED[t][verb] ? "ALLOW" : "DENY",
                        TIER_NAMES[t]);
            }
        } else {
            dprintf(client_fd, "%s\n", TIER_NAMES[t]);
        }
        close(client_fd);
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--witness") == 0) witness_mode = 1;

    const char *manifest_err = truth_manifest_selfcheck();
    if (manifest_err) {
        fprintf(stderr, "[truthd] refusing to start: %s\n", manifest_err);
        return 1;
    }

    int listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) { perror("socket"); return 1; }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof(addr.sun_path) - 1);
    unlink(SOCK_PATH);  /* stale socket from a previous run */

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("bind");
        return 1;
    }
    chmod(SOCK_PATH, 0666);  /* local listeners on the same host read this;
                              * no remote exposure -- it's a unix socket */
    if (listen(listen_fd, 8) != 0) { perror("listen"); return 1; }

    pthread_t poll_thread, serve_thread, layer0_thread;
    pthread_create(&poll_thread, NULL, poll_loop, NULL);
    pthread_create(&serve_thread, NULL, serve_loop, &listen_fd);
    if (!witness_mode) {
        pthread_create(&layer0_thread, NULL, layer0_poll_loop, NULL);
    }

    if (witness_mode) {
        fprintf(stderr, "[truthd] up (--witness), watching %s, serving %s\n",
                WITNESS_PATH, SOCK_PATH);
    } else {
        fprintf(stderr, "[truthd] up, watching I(0) %s:%d + %s, serving %s\n",
                LAYER0_GRP, LAYER0_PORT, EC2_REACHABLE_PATH, SOCK_PATH);
    }

    pthread_join(poll_thread, NULL);
    pthread_join(serve_thread, NULL);
    return 0;
}
