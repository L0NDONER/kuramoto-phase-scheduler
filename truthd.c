/**
 * truthd.c — reads /tmp/quartz_peer_health.json (written every ~1s by
 * quartz_node, see quartz_node.c) and exposes the current QuorumTier
 * (truth_manifest.h) over a local Unix socket. One line in, one word
 * out, nothing else. This process never touches the network and never
 * writes anything — the manifest it enforces is compiled in, and the
 * only thing it produces at runtime is a read of local state.
 *
 * EC2 signal: quartz_node's Kuramoto substrate is Mint/Pi1/Pi2 only (see
 * quartz-substrate/README.md) -- EC2 was never coupled into it, so it
 * can't appear in HEALTH_PATH. Instead, glyph/ec2_probe.py (Mint-only --
 * it's the only host holding ec2_intent.key) polls EC2 with a signed
 * BENIGN_READ intent and writes EC2_REACHABLE_PATH. This is a sensor
 * reading, never actuation: it only ever narrows/widens what this
 * daemon reports, it can't reach back into the probe or the substrate.
 * On Pi1/Pi2, that file simply never exists (nothing runs the probe
 * there), so file_is_stale() naturally caps them at TIER_LOCAL_TRIAD --
 * same code, no host-specific branch needed.
 *
 *   TIER_PARTITIONED  -- HEALTH_PATH missing/stale/malformed, or either
 *                        local peer unhealthy
 *   TIER_LOCAL_TRIAD  -- local triad intact, EC2_REACHABLE_PATH missing,
 *                        stale, or reachable:false
 *   TIER_FULL         -- local triad intact AND EC2_REACHABLE_PATH fresh
 *                        with reachable:true
 *
 * --witness mode (EC2 only): run as `./truthd --witness`. EC2 was never
 * coupled into the Kuramoto substrate (WAN, no physical phase-lock), so
 * it has no HEALTH_PATH of its own and can never independently confirm
 * the local triad is intact. Instead it reads WITNESS_PATH, written by
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
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <errno.h>

#include "truth_manifest.h"

#define HEALTH_PATH "/tmp/quartz_peer_health.json"
#define EC2_REACHABLE_PATH "/tmp/ec2_reachable.json"
#define WITNESS_PATH "/tmp/mint_witness.json"
#define SOCK_PATH   "/tmp/truthd.sock"
#define NUM_PEERS   2          /* quartz_node's local triad is always self+2 */
#define HEALTH_STALE_S 2.0     /* same margin quartz_wan_gain.py uses against
                                 * one missed ~1s write */
#define EC2_STALE_S 15.0       /* 3x ec2_probe.py's PROBE_INTERVAL_S -- margin
                                 * against one missed cycle plus WAN jitter */
#define WITNESS_STALE_S 15.0   /* same cadence as EC2_STALE_S -- ec2_probe.py
                                 * sends the witness report on the same loop */
#define POLL_INTERVAL_US 500000 /* 0.5s, matches quartz_presence_chain.py */

static int witness_mode = 0;  /* set from argv[1] == "--witness" */
static QuorumTier current_tier = TIER_PARTITIONED;
static pthread_mutex_t tier_lock = PTHREAD_MUTEX_INITIALIZER;

/* Both HEALTH_PATH and EC2_REACHABLE_PATH are written with their own
 * internal timestamps, but we only need wall-clock mtime freshness here,
 * so stat() is enough -- no need to parse either writer's own clock a
 * second time. */
static int file_is_stale(const char *path, double stale_s) {
    struct stat st;
    if (stat(path, &st) != 0) return 1;
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    double age = (now.tv_sec - st.st_mtim.tv_sec) +
                 (now.tv_nsec - st.st_mtim.tv_nsec) / 1e9;
    return age > stale_s;
}

/* Deliberately not a general JSON parser -- quartz_node.c writes exactly
 * one fixed line format per peer (see its write_health()), so scanning
 * for the literal "healthy": token is enough and matches the rest of
 * this codebase's style of not pulling in a JSON library for a format
 * only one writer ever produces. */
static int count_healthy_peers(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;

    char line[512];
    int healthy_count = 0;
    int peer_lines = 0;
    while (fgets(line, sizeof(line), f)) {
        char *tag = strstr(line, "\"healthy\":");
        if (!tag) continue;   /* the self_phase line has no healthy tag */
        peer_lines++;
        if (strstr(tag, "true")) healthy_count++;
    }
    fclose(f);

    if (peer_lines != NUM_PEERS) return -1;  /* malformed/partial write */
    return healthy_count;
}

/* Same "scan for the literal token" approach as count_healthy_peers --
 * ec2_probe.py writes exactly one fixed object per file, one writer,
 * no need for a real JSON parser. */
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

    if (file_is_stale(HEALTH_PATH, HEALTH_STALE_S)) return TIER_PARTITIONED;

    int healthy = count_healthy_peers(HEALTH_PATH);
    if (healthy < 0) return TIER_PARTITIONED;   /* missing/malformed */
    if (healthy < NUM_PEERS) return TIER_PARTITIONED;

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

    pthread_t poll_thread, serve_thread;
    pthread_create(&poll_thread, NULL, poll_loop, NULL);
    pthread_create(&serve_thread, NULL, serve_loop, &listen_fd);

    if (witness_mode) {
        fprintf(stderr, "[truthd] up (--witness), watching %s, serving %s\n",
                WITNESS_PATH, SOCK_PATH);
    } else {
        fprintf(stderr, "[truthd] up, watching %s + %s, serving %s\n",
                HEALTH_PATH, EC2_REACHABLE_PATH, SOCK_PATH);
    }

    pthread_join(poll_thread, NULL);
    pthread_join(serve_thread, NULL);
    return 0;
}
