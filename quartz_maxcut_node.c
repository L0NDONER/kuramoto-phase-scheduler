/**
 * quartz_maxcut_node.c — physical annealed-Kuramoto Max-Cut solver, K6.
 *
 * Each of the 3 machines (Mint, Pi1, Pi2) hosts 2 of the 6 spins/oscillators.
 * K6 is complete, so every oscillator couples to all 5 others — 2 locally
 * integrated, 4 received over UDP from the other two machines. Coupling is
 * uniformly repulsive (-K0) since Max-Cut wants every edge's endpoints in
 * different partitions. Annealing = phase noise that decays over each
 * process's own real elapsed time (measured off CLOCK_MONOTONIC_RAW, same
 * fix as quartz_node.c — dt is measured every step, not assumed).
 *
 * Uses PORT 5001, distinct from quartz_node.c's 5000, so this can run
 * alongside the existing 3-node timekeeping swarm without disturbing it.
 *
 * Compile: gcc -O2 -o quartz_maxcut_node quartz_maxcut_node.c -lm -lpthread
 * Run:     ./quartz_maxcut_node <local_id_a> <local_id_b> <peer1_ip> <peer2_ip>
 *
 * Mint (hosts spins 0,1): ./quartz_maxcut_node 0 1 10.0.0.122 10.0.0.174
 * Pi1  (hosts spins 2,3): ./quartz_maxcut_node 2 3 10.0.0.71  10.0.0.174
 * Pi2  (hosts spins 4,5): ./quartz_maxcut_node 4 5 10.0.0.71  10.0.0.122
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

#define PORT 5001
#define NSPINS 6
#define K0 0.3
#define SIGMA0 2.0
#define ANNEAL_TAU 8.0     // seconds
#define TOTAL_RUN_S 90.0   // seconds — plenty of margin over ~5*ANNEAL_TAU
#define LOOP_SLEEP_US 1000

typedef struct {
    double phase[NSPINS];
    int local_id[2];
    int num_peers;
    char peer_ips[2][16];
    pthread_mutex_t lock;
} MCState;

MCState st;
volatile int running = 1;

double quartz_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int is_local(int id) {
    return id == st.local_id[0] || id == st.local_id[1];
}

void *sender_thread(void *arg) {
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) { perror("sender socket"); return NULL; }

    struct sockaddr_in peer_addr;
    socklen_t addr_len = sizeof(peer_addr);

    while (running) {
        for (int li = 0; li < 2; li++) {
            int id = st.local_id[li];
            pthread_mutex_lock(&st.lock);
            double phase = st.phase[id];
            pthread_mutex_unlock(&st.lock);

            unsigned char pkt[11];
            unsigned short magic = htons(0x4D43);
            memcpy(pkt, &magic, 2);
            pkt[2] = (unsigned char)id;
            memcpy(pkt + 3, &phase, 8);

            for (int p = 0; p < st.num_peers; p++) {
                memset(&peer_addr, 0, sizeof(peer_addr));
                peer_addr.sin_family = AF_INET;
                peer_addr.sin_port = htons(PORT);
                inet_pton(AF_INET, st.peer_ips[p], &peer_addr.sin_addr);
                sendto(sock, pkt, sizeof(pkt), 0, (struct sockaddr*)&peer_addr, addr_len);
            }
        }
        usleep(10000);  // 100 Hz
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
        perror("bind"); return NULL;
    }
    struct timeval tv = {0, 200000};  // 200ms recv timeout so it notices `running`
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    unsigned char pkt[11];
    struct sockaddr_in peer_addr;
    socklen_t addr_len = sizeof(peer_addr);

    while (running) {
        int n = recvfrom(sock, pkt, sizeof(pkt), 0, (struct sockaddr*)&peer_addr, &addr_len);
        if (n != sizeof(pkt)) continue;
        unsigned short magic;
        memcpy(&magic, pkt, 2);
        if (ntohs(magic) != 0x4D43) continue;
        int id = pkt[2];
        if (id < 0 || id >= NSPINS || is_local(id)) continue;
        double phase;
        memcpy(&phase, pkt + 3, 8);

        pthread_mutex_lock(&st.lock);
        st.phase[id] = phase;
        pthread_mutex_unlock(&st.lock);
    }
    return NULL;
}

int main(int argc, char *argv[]) {
    if (argc < 5) {
        fprintf(stderr, "Usage: %s <local_id_a> <local_id_b> <peer1_ip> <peer2_ip>\n", argv[0]);
        return 1;
    }
    st.local_id[0] = atoi(argv[1]);
    st.local_id[1] = atoi(argv[2]);
    st.num_peers = 2;
    strncpy(st.peer_ips[0], argv[3], sizeof(st.peer_ips[0]) - 1);
    strncpy(st.peer_ips[1], argv[4], sizeof(st.peer_ips[1]) - 1);
    st.peer_ips[0][sizeof(st.peer_ips[0]) - 1] = '\0';
    st.peer_ips[1][sizeof(st.peer_ips[1]) - 1] = '\0';
    pthread_mutex_init(&st.lock, NULL);

    srand((unsigned)(quartz_now() * 1e6) ^ (unsigned)getpid());
    for (int i = 0; i < NSPINS; i++)
        st.phase[i] = ((double)rand() / RAND_MAX) * 2 * M_PI;

    printf("=== QUARTZ MAXCUT NODE — local spins %d,%d ===\n", st.local_id[0], st.local_id[1]);
    printf("Peers: %s, %s\n", st.peer_ips[0], st.peer_ips[1]);
    printf("K0=%.2f  SIGMA0=%.2f  ANNEAL_TAU=%.1fs  TOTAL_RUN=%.1fs\n\n",
           K0, SIGMA0, ANNEAL_TAU, TOTAL_RUN_S);

    pthread_t recv_t, send_t;
    pthread_create(&recv_t, NULL, receiver_thread, NULL);
    pthread_create(&send_t, NULL, sender_thread, NULL);

    double t_start = quartz_now();
    double last_t = t_start;
    double last_print = t_start;

    while (1) {
        double now = quartz_now();
        double t = now - t_start;
        if (t >= TOTAL_RUN_S) break;

        double dt = now - last_t;
        last_t = now;
        double sigma = SIGMA0 * exp(-t / ANNEAL_TAU);

        pthread_mutex_lock(&st.lock);
        double snapshot[NSPINS];
        memcpy(snapshot, st.phase, sizeof(snapshot));
        for (int li = 0; li < 2; li++) {
            int i = st.local_id[li];
            double coupling = 0.0;
            for (int j = 0; j < NSPINS; j++) {
                if (j == i) continue;
                coupling += -K0 * sin(snapshot[j] - snapshot[i]);
            }
            double u1 = (double)rand() / RAND_MAX, u2 = (double)rand() / RAND_MAX;
            double gauss = sqrt(-2.0 * log(u1 + 1e-12)) * cos(2 * M_PI * u2);
            double noise = gauss * sigma;
            st.phase[i] = fmod(snapshot[i] + (coupling + noise) * dt, 2 * M_PI);
            if (st.phase[i] < 0) st.phase[i] += 2 * M_PI;
        }
        pthread_mutex_unlock(&st.lock);

        if (now - last_print >= 5.0) {
            last_print = now;
            pthread_mutex_lock(&st.lock);
            printf("[t=%.0fs sigma=%.3f] spin%d=%.3f spin%d=%.3f  ref(0)=%.3f\n",
                   t, sigma, st.local_id[0], st.phase[st.local_id[0]],
                   st.local_id[1], st.phase[st.local_id[1]], st.phase[0]);
            pthread_mutex_unlock(&st.lock);
        }

        usleep(LOOP_SLEEP_US);
    }

    pthread_mutex_lock(&st.lock);
    double ref = st.phase[0];
    for (int li = 0; li < 2; li++) {
        int i = st.local_id[li];
        int spin = (cos(st.phase[i] - ref) >= 0) ? 1 : -1;
        printf("MAXCUT_RESULT node=%d phase=%.4f spin=%+d\n", i, st.phase[i], spin);
    }
    pthread_mutex_unlock(&st.lock);
    fflush(stdout);

    running = 0;
    sleep(1);  // let threads notice `running` and exit before process does
    return 0;
}
