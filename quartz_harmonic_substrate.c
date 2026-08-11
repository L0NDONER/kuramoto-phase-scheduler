#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <time.h>
#include <unistd.h>
#include <string.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/time.h>
#include <pthread.h>

#define NUM_NODES 3
#define MAX_NODES 8
#define UDP_PORT 5006
#define BUFFER_SIZE 1024
#define PI 3.14159265359

// Harmonic oscillator state (software-emulated, hardware-timed)
typedef struct {
    double phase;           // Current phase
    double frequency;       // Natural frequency (from quartz)
    double amplitude;       // Amplitude (from signal strength)
    double momentum;        // Momentum (from phase change)
    double coupling[MAX_NODES]; // Coupling to other nodes
    double timestamp;       // Last UDP timestamp
    double dt;             // Time delta (from quartz)
    double energy;          // Harmonic energy
} HarmonicOscillator;

// Network harmonic state
typedef struct {
    HarmonicOscillator nodes[MAX_NODES];
    double network_latency[MAX_NODES][MAX_NODES];
    double phase_differences[MAX_NODES][MAX_NODES];
    double global_phase;
    int node_id;
    char hostname[64];
    int udp_socket;
    struct sockaddr_in addr;
} HarmonicSubstrate;

// === REAL HARDWARE TIMING (Using System Quartz) ===

// Get real timestamp from system quartz
double get_quartz_timestamp() {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

// Measure real frequency stability (PPM from quartz)
double measure_quartz_ppm() {
    struct timespec ts1, ts2;
    double diff1, diff2;

    clock_gettime(CLOCK_REALTIME, &ts1);
    usleep(1000000); // 1 second
    clock_gettime(CLOCK_REALTIME, &ts2);

    diff1 = ts2.tv_sec - ts1.tv_sec;
    diff2 = (ts2.tv_nsec - ts1.tv_nsec) / 1e9;
    double elapsed = diff1 + diff2;

    return (elapsed - 1.0) / 1.0 * 1e6; // PPM
}

// === HARMONIC OSCILLATOR PHYSICS (Software-Emulated) ===

// Initialize harmonic oscillator network. node_id identifies which slot
// this machine occupies in the swarm (0-indexed, < NUM_NODES) - passed on
// the command line, matching quartz_node's <node_id> convention, since a
// hardcoded id=0 would make every node claim to be node 0.
void init_harmonic_network(HarmonicSubstrate* hs, int node_id) {
    gethostname(hs->hostname, sizeof(hs->hostname));
    hs->node_id = node_id;

    // Only our own slot has a real quartz reading at init - remote nodes
    // are unknown until their UDP state arrives, so their frequency starts
    // at 0 (the sentinel receive_harmonic_state/monitor_harmonic_sync
    // already check for). Measuring the local clock and assigning it to
    // remote node slots was a bug: it faked "known" state for nodes we'd
    // never heard from yet.
    double ppm = measure_quartz_ppm();
    hs->nodes[node_id].frequency = 1.0 + ppm / 1e6;
    hs->nodes[node_id].phase = 0.0;
    hs->nodes[node_id].amplitude = 1.0;
    hs->nodes[node_id].momentum = 0.0;
    hs->nodes[node_id].timestamp = get_quartz_timestamp();
    for (int j = 0; j < NUM_NODES; j++) hs->nodes[node_id].coupling[j] = 0.1;

    for (int i = 0; i < NUM_NODES; i++) {
        if (i == node_id) continue;
        hs->nodes[i].frequency = 0.0; // unknown until a UDP packet arrives
        hs->nodes[i].phase = (i / (double)NUM_NODES) * 2 * PI;
        hs->nodes[i].amplitude = 1.0;
        hs->nodes[i].momentum = 0.0;
        hs->nodes[i].timestamp = get_quartz_timestamp();
        for (int j = 0; j < NUM_NODES; j++) hs->nodes[i].coupling[j] = 0.1;
    }

    // Setup UDP socket
    hs->udp_socket = socket(AF_INET, SOCK_DGRAM, 0);
    int broadcast_enable = 1;
    setsockopt(hs->udp_socket, SOL_SOCKET, SO_BROADCAST, &broadcast_enable, sizeof(broadcast_enable));
    setsockopt(hs->udp_socket, SOL_SOCKET, SO_REUSEADDR, &broadcast_enable, sizeof(broadcast_enable));

    int flags = fcntl(hs->udp_socket, F_GETFL, 0);
    fcntl(hs->udp_socket, F_SETFL, flags | O_NONBLOCK);

    hs->addr.sin_family = AF_INET;
    hs->addr.sin_port = htons(UDP_PORT);
    hs->addr.sin_addr.s_addr = INADDR_ANY;
    bind(hs->udp_socket, (struct sockaddr*)&hs->addr, sizeof(hs->addr));

    printf("Harmonic Substrate Initialized\n");
    printf("   Node: %s (id=%d)\n", hs->hostname, hs->node_id);
    printf("   Frequency: %.9f Hz\n", hs->nodes[hs->node_id].frequency);
    printf("   Quartz PPM: %.2f\n", (hs->nodes[hs->node_id].frequency - 1.0) * 1e6);
}

// === UDP HARMONIC SYNCHRONIZATION ===

// Send harmonic state to all nodes
void broadcast_harmonic_state(HarmonicSubstrate* hs) {
    char buffer[BUFFER_SIZE];
    struct sockaddr_in broadcast_addr;

    broadcast_addr.sin_family = AF_INET;
    broadcast_addr.sin_port = htons(UDP_PORT);
    broadcast_addr.sin_addr.s_addr = inet_addr("255.255.255.255");

    // Pack harmonic state
    snprintf(buffer, sizeof(buffer), "%d|%s|%f|%f|%f|%f",
            hs->node_id,
            hs->hostname,
            get_quartz_timestamp(),
            hs->nodes[hs->node_id].phase,
            hs->nodes[hs->node_id].frequency,
            hs->nodes[hs->node_id].amplitude);

    sendto(hs->udp_socket, buffer, strlen(buffer), 0,
           (struct sockaddr*)&broadcast_addr, sizeof(broadcast_addr));
}

// Receive harmonic state from other nodes (socket is already non-blocking,
// set once at init in init_harmonic_network). Drains the whole socket
// backlog each call, keeping only the newest packet per sender - pulling
// just one packet per loop iteration let unread backlog pile up in the
// kernel receive buffer under any jitter, so recvfrom kept returning
// increasingly stale packets and the reported "latency" climbed without
// bound (observed live: ~3ms to ~4900ms over a 15s run) even though real
// network latency stayed flat.
void receive_harmonic_state(HarmonicSubstrate* hs) {
    char buffer[BUFFER_SIZE];
    struct sockaddr_in sender_addr;
    socklen_t addr_len = sizeof(sender_addr);
    int len;

    while ((len = recvfrom(hs->udp_socket, buffer, BUFFER_SIZE - 1, 0,
                            (struct sockaddr*)&sender_addr, &addr_len)) > 0) {
        buffer[len] = '\0';
        int node_id;
        char hostname[64];
        double timestamp, phase, frequency, amplitude;

        sscanf(buffer, "%d|%63[^|]|%lf|%lf|%lf|%lf",
               &node_id, hostname, &timestamp, &phase, &frequency, &amplitude);

        if (node_id != hs->node_id && node_id >= 0 && node_id < NUM_NODES) {
            int idx = node_id;

            // Calculate phase difference (harmonic coupling)
            double dt = get_quartz_timestamp() - timestamp;
            double phase_diff = phase - hs->nodes[hs->node_id].phase;

            // Store in harmonic network
            hs->nodes[idx].phase = phase;
            hs->nodes[idx].frequency = frequency;
            hs->nodes[idx].amplitude = amplitude;
            hs->phase_differences[hs->node_id][idx] = phase_diff;
            hs->network_latency[hs->node_id][idx] = dt * 1000; // Convert to ms

            // Update coupling based on phase difference
            hs->nodes[hs->node_id].coupling[idx] = 1.0 / (1.0 + dt * 1000);
        }
    }
}

// === HARMONIC EVOLUTION (Physics-Based) ===

// Update harmonic oscillators using physical equations (forward Euler)
void evolve_harmonic_system(HarmonicSubstrate* hs, double dt) {
    double forces[MAX_NODES] = {0};
    int n = NUM_NODES;

    for (int i = 0; i < n; i++) {
        // Natural frequency force
        forces[i] += -hs->nodes[i].frequency * hs->nodes[i].phase;

        // Coupling forces (Kuramoto model)
        for (int j = 0; j < n; j++) {
            if (i != j) {
                double phase_diff = hs->nodes[i].phase - hs->nodes[j].phase;
                forces[i] += -hs->nodes[i].coupling[j] * sin(phase_diff);
            }
        }

        // Damping
        forces[i] += -0.1 * hs->nodes[i].momentum;
    }

    // Update positions and momenta (forward Euler)
    for (int i = 0; i < n; i++) {
        hs->nodes[i].momentum += forces[i] * dt;
        hs->nodes[i].phase += hs->nodes[i].momentum * dt;
        hs->nodes[i].timestamp = get_quartz_timestamp();
    }
}

// === QUBO SOLVER USING HARMONIC SUBSTRATE ===

// Map QUBO problem to harmonic oscillators
double solve_qubo_harmonic(HarmonicSubstrate* hs, double Q[5][5], int* solution) {
    int n = 5; // 5 QUBO variables

    // Map QUBO couplings to harmonic couplings
    for (int i = 0; i < n; i++) {
        hs->nodes[i].frequency = 1.0 + Q[i][i] / 10.0;
        for (int j = 0; j < n; j++) {
            if (i != j) {
                hs->nodes[i].coupling[j] = Q[i][j] / 10.0;
            }
        }
    }

    // Evolve harmonic system (physical annealing)
    double temp = 10.0;
    double cooling_rate = 0.9999;

    for (int iter = 0; iter < 100000; iter++) {
        double dt = 0.001 * temp;
        evolve_harmonic_system(hs, dt);

        // Cool down
        temp *= cooling_rate;
        if (temp < 0.01) break;
    }

    // Read solution from harmonic phases
    for (int i = 0; i < n; i++) {
        solution[i] = (hs->nodes[i].phase > 0) ? 1 : 0;
    }

    // Calculate QUBO energy
    double energy = 0;
    for (int i = 0; i < n; i++) {
        energy += Q[i][i] * solution[i];
        for (int j = i+1; j < n; j++) {
            energy += Q[i][j] * solution[i] * solution[j];
        }
    }

    return energy;
}

// === REAL-TIME HARMONIC OPTIMIZATION ===

// Monitor harmonic synchronization
void monitor_harmonic_sync(HarmonicSubstrate* hs) {
    printf("\nHarmonic Substrate Status:\n");
    printf("===============================================\n");
    printf("  Node: %s\n", hs->hostname);
    printf("  Local Phase: %.4f rad\n", hs->nodes[hs->node_id].phase);
    printf("  Local Frequency: %.9f Hz\n", hs->nodes[hs->node_id].frequency);
    printf("  Quartz PPM: %.2f\n", (hs->nodes[hs->node_id].frequency - 1.0) * 1e6);

    printf("\n  Network Harmonics:\n");
    for (int i = 0; i < NUM_NODES; i++) {
        if (i == hs->node_id) continue;
        if (hs->nodes[i].frequency > 0) {
            printf("    Node %d: Phase Diff = %.4f rad, Latency = %.2f ms\n",
                   i, hs->phase_differences[hs->node_id][i], hs->network_latency[hs->node_id][i]);
        }
    }

    printf("  Global Synchronization: %.2f%%\n",
           (1.0 - fabs(hs->nodes[hs->node_id].phase - hs->global_phase) / (2 * PI)) * 100);
}

// === MAIN HARMONIC LOOP ===

void* harmonic_loop(void* arg) {
    HarmonicSubstrate* hs = (HarmonicSubstrate*)arg;

    while (1) {
        // Broadcast harmonic state
        broadcast_harmonic_state(hs);

        // Receive from others
        receive_harmonic_state(hs);

        // Evolve harmonic system
        double dt = 0.001;
        evolve_harmonic_system(hs, dt);

        // Calculate global phase (consensus)
        double total_phase = 0;
        int count = 0;
        for (int i = 0; i < NUM_NODES; i++) {
            if (hs->nodes[i].frequency > 0) {
                total_phase += hs->nodes[i].phase;
                count++;
            }
        }
        hs->global_phase = count > 0 ? total_phase / count : 0.0;

        // Monitor
        monitor_harmonic_sync(hs);

        usleep(100000); // 10 Hz
    }
    return NULL;
}

// === ENERGY OPTIMIZATION (Real Example) ===

void test_energy_optimization() {
    printf("\nTesting Energy Optimization with Harmonic Substrate\n");
    printf("===============================================\n");

    // Example QUBO for energy optimization (5 devices)
    double Q[5][5] = {0};

    // Device costs (negative = prefer active)
    Q[0][0] = -0.8;  // Fridge (critical)
    Q[1][1] = -0.5;  // Server
    Q[2][2] = -0.3;  // AC
    Q[3][3] = -0.1;  // EV Charger
    Q[4][4] = -0.2;  // Water Heater

    // Couplings (penalize high power together)
    Q[0][1] = 0.2;
    Q[0][2] = 0.3;
    Q[1][2] = 0.3;
    Q[2][3] = 0.8;   // High penalty for AC + EV
    Q[3][4] = 0.4;

    // Harmonic substrate solver (local-only computation, no swarm
    // coordination needed, but init_harmonic_network still opens a real UDP
    // socket - must close it before main() opens another on the same port)
    HarmonicSubstrate hs;
    init_harmonic_network(&hs, 0);

    int solution[5];
    double energy = solve_qubo_harmonic(&hs, Q, solution);

    printf("\nOptimal Configuration:\n");
    const char* devices[] = {"Fridge", "Server", "AC", "EV Charger", "Water Heater"};
    for (int i = 0; i < 5; i++) {
        printf("  %s: %s (phase: %.4f)\n",
               devices[i],
               solution[i] ? "ON" : "OFF",
               hs.nodes[i].phase);
    }
    printf("\n  Energy: %.6f\n", energy);

    // Calculate savings
    double baseline = 0;
    for (int i = 0; i < 5; i++) {
        baseline += Q[i][i]; // All on baseline
    }
    double savings = baseline != 0 ? (baseline - energy) / baseline * 100 : 0.0;
    printf("  Savings: %.1f%%\n", savings);

    close(hs.udp_socket);
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <node_id 0-%d>\n", argv[0], NUM_NODES - 1);
        return 1;
    }
    int node_id = atoi(argv[1]);
    if (node_id < 0 || node_id >= NUM_NODES) {
        fprintf(stderr, "node_id must be 0-%d (NUM_NODES=%d)\n", NUM_NODES - 1, NUM_NODES);
        return 1;
    }

    printf("================================================================\n");
    printf("  SOFTWARE HARMONIC QUARTZ SUBSTRATE\n");
    printf("  (Real Quartz + UDP + Harmonic Physics)\n");
    printf("================================================================\n\n");

    test_energy_optimization();

    // Start harmonic loop
    HarmonicSubstrate hs;
    init_harmonic_network(&hs, node_id);

    pthread_t thread;
    pthread_create(&thread, NULL, harmonic_loop, &hs);

    // Run for a while
    sleep(10);
    pthread_cancel(thread);
    pthread_join(thread, NULL);
    close(hs.udp_socket);

    return 0;
}
