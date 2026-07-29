/**
 * quartz_node.c — distributed Kuramoto oscillator daemon, now also the
 * peer-health substrate for quartz_metro_node.
 *
 * Same role as the original: each machine reads its own quartz crystal
 * via CLOCK_MONOTONIC_RAW and couples with 2 peers over UDP, deliberately
 * NOT NTP-disciplined (quartz_now() below). This isn't clock sync — the
 * OS (systemd-timesyncd) already does that; this is a continuous
 * phase-agreement signal between nodes.
 *
 * NEW over the original: tracks per-peer last-arrival time and phase
 * deviation, and exports both as a peer-health file — the "no extra
 * traffic" answer to dead-peer detection. quartz_metro_node reads this
 * file before dispatching a job round instead of pinging anyone itself;
 * it doesn't touch the wire, this daemon does.
 *
 * No RTT is measured or exported — that would need a round-trip
 * (ping/pong), which this deliberately doesn't add. Liveness is judged
 * from the existing one-way broadcast stream only: silence duration and
 * phase deviation.
 *
 * HEALTH_TIMEOUT_S=0.5 (50 missed sends at the 10ms/100Hz broadcast
 * cadence — generous margin against a single dropped packet) and
 * DEV_THRESHOLD=1.0 rad (~57°, well outside normal locked behavior,
 * which typically runs dev ~1e-4-1e-2 per prior quartz_beacon.py logs)
 * are starting points, not tuned against real drift data yet.
 *
 * Compile: gcc -O2 -o quartz_node quartz_node.c -lm -lpthread
 * Run:     ./quartz_node <node_id> <peer1_ip> <peer2_ip>
 *
 * Writes: /tmp/quartz_peer_health.json — first line is this node's own
 * phase (consumed by quartz_wan_gain.py for its carrier signal), then
 * one line per peer:
 *   {"self_phase": <rad>}
 *   {"peer_ip": "...", "last_seen": <epoch>, "phase_offset": <rad>, "dev": <rad>, "healthy": true|false}
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#define PORT 5000
#define K 0.25
#define OMEGA 1.0
#define LOOP_SLEEP_US 1000

#define HEALTH_PATH "/tmp/quartz_peer_health.json"
#define HEALTH_TIMEOUT_S 0.5
#define DEV_THRESHOLD 1.0
#define DEV_EMA_ALPHA 0.1   // smoothing for the deviation estimate

typedef struct {
    double phase;
    double omega;
    double coupling_K;
    int node_id;
    int num_peers;
    double peer_phases[2];
    char peer_ips[2][16];
    double last_seen[2];    // quartz_now() at last received packet, 0 = never
    double dev[2];          // EMA of |wrapped phase difference|
    pthread_mutex_t lock;
} Oscillator;

Oscillator osc;

double quartz_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

// wrap to [-pi, pi]
static double wrap_pi(double a) {
    a = fmod(a + M_PI, 2 * M_PI);
    if (a < 0) a += 2 * M_PI;
    return a - M_PI;
}

void kuramoto_step(double dt) {
    pthread_mutex_lock(&osc.lock);

    double coupling = 0.0;
    for (int i = 0; i < osc.num_peers; i++) {
        coupling += osc.coupling_K * sin(osc.peer_phases[i] - osc.phase);
    }

    osc.phase += (osc.omega + coupling) * dt;
    osc.phase = fmod(osc.phase, 2 * M_PI);
    if (osc.phase < 0) osc.phase += 2 * M_PI;

    pthread_mutex_unlock(&osc.lock);
}

void write_health(void) {
    pthread_mutex_lock(&osc.lock);
    double now = quartz_now();
    double self_phase = osc.phase;
    double offsets[2], devs[2], last_seen[2];
    char ips[2][16];
    int n = osc.num_peers;
    for (int i = 0; i < n; i++) {
        offsets[i] = wrap_pi(osc.peer_phases[i] - osc.phase);
        devs[i] = osc.dev[i];
        last_seen[i] = osc.last_seen[i];
        strcpy(ips[i], osc.peer_ips[i]);
    }
    pthread_mutex_unlock(&osc.lock);

    char tmp_path[] = HEALTH_PATH ".tmp";
    FILE *f = fopen(tmp_path, "w");
    if (!f) return;
    fprintf(f, "{\"self_phase\": %.6f}\n", self_phase);
    for (int i = 0; i < n; i++) {
        int healthy = last_seen[i] > 0 &&
                      (now - last_seen[i]) < HEALTH_TIMEOUT_S &&
                      fabs(devs[i]) < DEV_THRESHOLD;
        fprintf(f, "{\"peer_ip\": \"%s\", \"last_seen\": %.6f, \"phase_offset\": %.6f, \"dev\": %.6f, \"healthy\": %s}\n",
                ips[i], last_seen[i], offsets[i], devs[i], healthy ? "true" : "false");
    }
    fclose(f);
    rename(tmp_path, HEALTH_PATH);
}

void *sender_thread(void *arg) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) { perror("sender socket"); return NULL; }

    struct sockaddr_in peer_addr;
    socklen_t addr_len = sizeof(peer_addr);

    while (1) {
        pthread_mutex_lock(&osc.lock);
        double phase = osc.phase;
        pthread_mutex_unlock(&osc.lock);

        for (int i = 0; i < osc.num_peers; i++) {
            memset(&peer_addr, 0, sizeof(peer_addr));
            peer_addr.sin_family = AF_INET;
            peer_addr.sin_port = htons(PORT);
            inet_pton(AF_INET, osc.peer_ips[i], &peer_addr.sin_addr);

            sendto(sock, &phase, sizeof(phase), 0,
                   (struct sockaddr*)&peer_addr, addr_len);
        }

        usleep(10000);  // 10ms / 100Hz
    }
    return NULL;
}

void *receiver_thread(void *arg) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) { perror("receiver socket"); return NULL; }

    struct sockaddr_in my_addr;
    memset(&my_addr, 0, sizeof(my_addr));
    my_addr.sin_family = AF_INET;
    my_addr.sin_port = htons(PORT);
    my_addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(sock, (struct sockaddr*)&my_addr, sizeof(my_addr)) < 0) {
        perror("bind");
        return NULL;
    }

    struct sockaddr_in peer_addr;
    socklen_t addr_len = sizeof(peer_addr);
    double received_phase;
    char peer_ip[16];

    while (1) {
        int n = recvfrom(sock, &received_phase, sizeof(received_phase), 0,
                         (struct sockaddr*)&peer_addr, &addr_len);
        if (n > 0) {
            inet_ntop(AF_INET, &peer_addr.sin_addr, peer_ip, sizeof(peer_ip));

            pthread_mutex_lock(&osc.lock);
            for (int i = 0; i < osc.num_peers; i++) {
                if (strcmp(peer_ip, osc.peer_ips[i]) == 0) {
                    osc.peer_phases[i] = received_phase;
                    osc.last_seen[i] = quartz_now();
                    double pd = wrap_pi(received_phase - osc.phase);
                    osc.dev[i] = (1 - DEV_EMA_ALPHA) * osc.dev[i] + DEV_EMA_ALPHA * fabs(pd);
                    break;
                }
            }
            pthread_mutex_unlock(&osc.lock);
        }
    }
    return NULL;
}

int main(int argc, char *argv[]) {
    if (argc < 4) {
        fprintf(stderr, "Usage: %s <node_id> <peer1_ip> <peer2_ip>\n", argv[0]);
        return 1;
    }

    osc.node_id = atoi(argv[1]);
    osc.phase = fmod(quartz_now(), 2 * M_PI);
    osc.omega = OMEGA;
    osc.coupling_K = K;
    osc.num_peers = 2;
    strncpy(osc.peer_ips[0], argv[2], sizeof(osc.peer_ips[0]) - 1);
    strncpy(osc.peer_ips[1], argv[3], sizeof(osc.peer_ips[1]) - 1);
    osc.peer_ips[0][sizeof(osc.peer_ips[0]) - 1] = '\0';
    osc.peer_ips[1][sizeof(osc.peer_ips[1]) - 1] = '\0';
    osc.peer_phases[0] = 0.0;
    osc.peer_phases[1] = 0.0;
    osc.last_seen[0] = 0.0;
    osc.last_seen[1] = 0.0;
    osc.dev[0] = 0.0;
    osc.dev[1] = 0.0;
    pthread_mutex_init(&osc.lock, NULL);

    printf("=== QUARTZ NODE %d (daemon + peer-health) ===\n", osc.node_id);
    printf("Peers: %s, %s\n", osc.peer_ips[0], osc.peer_ips[1]);
    printf("Initial phase: %.4f rad\n", osc.phase);
    printf("Health file: %s (timeout=%.2fs dev_threshold=%.2frad)\n\n",
           HEALTH_PATH, HEALTH_TIMEOUT_S, DEV_THRESHOLD);

    pthread_t recv_thread;
    pthread_create(&recv_thread, NULL, receiver_thread, NULL);

    pthread_t send_thread;
    pthread_create(&send_thread, NULL, sender_thread, NULL);

    double last_t = quartz_now();
    double last_print_t = last_t;
    double last_health_t = last_t;

    while (1) {
        double now = quartz_now();
        double dt = now - last_t;
        last_t = now;

        kuramoto_step(dt);

        if (now - last_print_t >= 1.0) {
            last_print_t = now;
            pthread_mutex_lock(&osc.lock);
            printf("[%d] phase=%.4f  peers: %.4f, %.4f  dev: %.4f, %.4f\n",
                   osc.node_id, osc.phase, osc.peer_phases[0], osc.peer_phases[1],
                   osc.dev[0], osc.dev[1]);
            pthread_mutex_unlock(&osc.lock);
        }

        if (now - last_health_t >= 1.0) {
            last_health_t = now;
            write_health();
        }

        usleep(LOOP_SLEEP_US);
    }

    return 0;
}
