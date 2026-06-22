/*
 * cpu_reader.c — Kuramoto axis consumer: DVFS + core parking modulator
 *
 * Subscribes to reader.c axis multicast on 239.0.0.2:7404 (AxisPulse).
 *
 * Step 1: Direct MSR writes via pre-opened /dev/cpu/N/msr fds.
 * Step 2: Logs (θ, f, V, power_W, temp_C, load, active_cores) to CSV.
 * Step 3: Load-aware θ nudge. Sends LoadFeedback to reader on :7405.
 * Step 4: Core parking policy — N "hot" cores ride full DVFS curve;
 *         remainder are held at trough (f_min, V_OFFSET_MV) regardless
 *         of θ. Voltage is package-wide so all cores benefit at trough.
 *
 * Usage: sudo ./cpu_reader [reader_ip] [--cores-active=N] [--park-cores]
 *   --park-cores        1 hot core, rest parked at floor
 *   --cores-active=N    N hot cores (1–n_cpus), rest parked
 *   (default: all cores hot, current DVFS behaviour)
 *
 * Pin reader to cpu0 before parking: taskset -c 0 in reader.service.
 * Ctrl-C restores stock voltage + governor on all cores.
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define WAN_MAGIC     0x5257
#define AXIS_MAGIC    0x4158
#define AXIS_GRP      "239.0.0.2"
#define AXIS_PORT     7404
#define LOAD_PORT     7405
#define LOAD_MAGIC    0x4C44

#define PHASE_TARGET  M_PI
#define ANTI_THRESH   0.20
#define LOCK_WINDOW   20
#define LOCK_STD      0.10

/* Voltage offset at trough (mV, negative = undervolt). 0 = disabled. */
#define V_OFFSET_MV   (-50)

/* Max load nudge in radians (π/2 = full shift to peak at 100% load). */
#define LOAD_NUDGE_MAX (M_PI / 2.0)

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
} AxisPulse;  /* 30 bytes */

typedef struct __attribute__((packed)) {
    uint16_t magic;
    float    load;
    float    temp;
} LoadFeedback;  /* 10 bytes */

static float ntohf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = ntohl(n);
    float r; memcpy(&r, &n, 4); return r;
}

static float htonf(float f) {
    uint32_t n; memcpy(&n, &f, 4); n = htonl(n);
    float r; memcpy(&r, &n, 4); return r;
}

/* ── cpufreq ─────────────────────────────────────────────────────────────── */

static int  n_cpus = 0;
static int  cores_active = 0;   /* set after arg parse + discover */
static char cpu_paths[64][320];
static char gov_paths[64][320];
static char saved_gov[64][64];
static long f_min = 0, f_max = 0;

static void cpufreq_discover(void) {
    DIR *d = opendir("/sys/devices/system/cpu");
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d)) && n_cpus < 64) {
        if (strncmp(e->d_name, "cpu", 3) != 0) continue;
        if (e->d_name[3] < '0' || e->d_name[3] > '9') continue;
        snprintf(cpu_paths[n_cpus], sizeof(cpu_paths[0]),
                 "/sys/devices/system/cpu/%.16s/cpufreq/scaling_setspeed", e->d_name);
        snprintf(gov_paths[n_cpus], sizeof(gov_paths[0]),
                 "/sys/devices/system/cpu/%.16s/cpufreq/scaling_governor", e->d_name);
        FILE *f = fopen(gov_paths[n_cpus], "r");
        if (f) {
            if (fgets(saved_gov[n_cpus], sizeof(saved_gov[0]), f))
                saved_gov[n_cpus][strcspn(saved_gov[n_cpus], "\n")] = '\0';
            fclose(f);
        }
        n_cpus++;
    }
    closedir(d);
    FILE *f;
    f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq", "r");
    if (f) { char b[32]; if (fgets(b, sizeof(b), f)) f_min = atol(b); fclose(f); }
    f = fopen("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "r");
    if (f) { char b[32]; if (fgets(b, sizeof(b), f)) f_max = atol(b); fclose(f); }
}

static void set_governor(const char *gov) {
    for (int i = 0; i < n_cpus; i++) {
        FILE *f = fopen(gov_paths[i], "w");
        if (f) { fprintf(f, "%s\n", gov); fclose(f); }
    }
}


/* set freq on a single CPU by logical index (bypasses cpu_paths[] order) */
static void set_freq_core(int cpu_num, long khz) {
    char path[320], buf[32];
    snprintf(path, sizeof(path),
             "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_setspeed", cpu_num);
    snprintf(buf, sizeof(buf), "%ld\n", khz);
    FILE *f = fopen(path, "w");
    if (f) { fputs(buf, f); fclose(f); }
}

static void restore_governors(void) {
    for (int i = 0; i < n_cpus; i++) {
        FILE *f = fopen(gov_paths[i], "w");
        if (f) { fprintf(f, "%s\n", saved_gov[i]); fclose(f); }
    }
}

/* ── MSR direct writes (Step 1) ──────────────────────────────────────────── */

static int msr_fds[64];

static void msr_open_all(void) {
    char path[64];
    for (int i = 0; i < n_cpus; i++) {
        snprintf(path, sizeof(path), "/dev/cpu/%d/msr", i);
        msr_fds[i] = open(path, O_WRONLY);
        if (msr_fds[i] < 0)
            fprintf(stderr, "[cpu_reader] warn: cannot open %s: %s\n",
                    path, strerror(errno));
    }
}

static void msr_close_all(void) {
    for (int i = 0; i < n_cpus; i++)
        if (msr_fds[i] >= 0) { close(msr_fds[i]); msr_fds[i] = -1; }
}

static void msr_write_voltage(int mv) {
    int v = (int)round(mv * 1.024);
    if (v < 0) v += 2048;
    v &= 0x7FF;
    uint64_t val = 0x80000011ULL | ((uint64_t)v << 21);
    for (int i = 0; i < n_cpus; i++)
        if (msr_fds[i] >= 0)
            if (pwrite(msr_fds[i], &val, sizeof(val), 0x150) < 0) {}
}

/* ── RAPL package power (Step 2) ─────────────────────────────────────────── */

static int      rapl_fd = -1;
static uint64_t rapl_prev_uj = 0;
static struct timespec rapl_prev_ts = {0};

static void rapl_open(void) {
    rapl_fd = open("/sys/class/powercap/intel-rapl:0/energy_uj", O_RDONLY);
}

static double rapl_read_watts(void) {
    if (rapl_fd < 0) return -1.0;
    char buf[32]; buf[0] = '\0';
    lseek(rapl_fd, 0, SEEK_SET);
    int n = (int)read(rapl_fd, buf, sizeof(buf)-1);
    if (n <= 0) return -1.0;
    buf[n] = '\0';
    uint64_t uj = strtoull(buf, NULL, 10);
    struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
    double watts = -1.0;
    if (rapl_prev_uj > 0) {
        double dt = (now.tv_sec  - rapl_prev_ts.tv_sec)
                  + (now.tv_nsec - rapl_prev_ts.tv_nsec) * 1e-9;
        if (dt > 0.0) {
            uint64_t delta = (uj >= rapl_prev_uj) ? uj - rapl_prev_uj
                                                   : uj + (UINT64_MAX - rapl_prev_uj);
            watts = (double)delta / 1e6 / dt;
        }
    }
    rapl_prev_uj = uj;
    rapl_prev_ts = now;
    return watts;
}

/* ── CPU temperature (Step 2) ────────────────────────────────────────────── */

static int temp_fd = -1;

static void temp_open(void) {
    /* find hwmon named "coretemp", use temp1_input (package) */
    DIR *d = opendir("/sys/class/hwmon");
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char namepath[256], temppath[256];
        snprintf(namepath, sizeof(namepath), "/sys/class/hwmon/%.16s/name", e->d_name);
        FILE *f = fopen(namepath, "r");
        if (!f) continue;
        char name[32]; int ok = (fgets(name, sizeof(name), f) != NULL); fclose(f);
        if (!ok || strncmp(name, "coretemp", 8) != 0) continue;
        snprintf(temppath, sizeof(temppath),
                 "/sys/class/hwmon/%.16s/temp1_input", e->d_name);
        temp_fd = open(temppath, O_RDONLY);
        break;
    }
    closedir(d);
}

static double temp_read_c(void) {
    if (temp_fd < 0) return -1.0;
    char buf[16]; buf[0] = '\0';
    lseek(temp_fd, 0, SEEK_SET);
    int n = (int)read(temp_fd, buf, sizeof(buf)-1);
    if (n <= 0) return -1.0;
    buf[n] = '\0';
    return atol(buf) / 1000.0;
}

/* ── CPU load from /proc/stat (Step 3) ───────────────────────────────────── */

static uint64_t load_prev_idle = 0, load_prev_total = 0;

static double load_read_pct(void) {
    FILE *f = fopen("/proc/stat", "r");
    if (!f) return 0.0;
    uint64_t user, nice, sys, idle, iowait, irq, softirq, steal;
    int ok = (fscanf(f, "cpu %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64
                        " %"SCNu64" %"SCNu64" %"SCNu64" %"SCNu64,
                     &user, &nice, &sys, &idle,
                     &iowait, &irq, &softirq, &steal) == 8);
    fclose(f);
    if (!ok) return 0.0;
    uint64_t total      = user + nice + sys + idle + iowait + irq + softirq + steal;
    uint64_t idle_total = idle + iowait;
    double load = 0.0;
    if (load_prev_total > 0 && total > load_prev_total) {
        uint64_t dtotal = total - load_prev_total;
        uint64_t didle  = idle_total - load_prev_idle;
        load = 1.0 - (double)didle / (double)dtotal;
        if (load < 0.0) load = 0.0;
        if (load > 1.0) load = 1.0;
    }
    load_prev_idle  = idle_total;
    load_prev_total = total;
    return load;
}

/* ── signal handling ─────────────────────────────────────────────────────── */

static volatile int running = 1;
static void on_signal(int s) { (void)s; running = 0; }

/* ── main ────────────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);

    /* parse args: [reader_ip] [--cores-active=N] [--park-cores] */
    const char *reader_ip   = "127.0.0.1";
    int         cores_arg   = -1;   /* -1 = not set, use all */
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "--cores-active=", 15) == 0)
            cores_arg = atoi(argv[i] + 15);
        else if (strcmp(argv[i], "--park-cores") == 0)
            cores_arg = 1;
        else
            reader_ip = argv[i];
    }

    cpufreq_discover();
    if (n_cpus == 0 || f_min == 0 || f_max == 0) {
        fprintf(stderr, "[cpu_reader] cpufreq not available\n");
        return 1;
    }

    /* resolve cores_active */
    cores_active = (cores_arg > 0) ? cores_arg : n_cpus;
    if (cores_active > n_cpus) cores_active = n_cpus;
    if (cores_active < 1)      cores_active = 1;

    msr_open_all();
    rapl_open();
    temp_open();

    /* CSV log */
    char logpath[128];
    {
        time_t t = time(NULL);
        struct tm *tm = localtime(&t);
        strftime(logpath, sizeof(logpath), "/tmp/cpu_reader_%Y%m%d_%H%M%S.csv", tm);
    }
    FILE *log_fp = fopen(logpath, "w");
    if (log_fp)
        fprintf(log_fp,
                "t_ms,theta1,theta_eff,freq_khz,v_offset_mv,"
                "power_w,temp_c,load_pct,active_cores\n");

    printf("[cpu_reader] %d CPUs  %ldkHz–%ldkHz  V_max=%dmV  hot=%d/parked=%d\n",
           n_cpus, f_min, f_max, V_OFFSET_MV, cores_active, n_cpus - cores_active);
    printf("[cpu_reader] log → %s\n", logpath);

    set_governor("userspace");
    /* hot cores start at peak; parked cores go straight to floor */
    for (int i = 0; i < cores_active; i++)  set_freq_core(i, f_max);
    for (int i = cores_active; i < n_cpus; i++) set_freq_core(i, f_min);
    msr_write_voltage(0);

    /* subscribe to axis multicast 239.0.0.2:7404 */
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    {
        int one = 1;
        setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
        struct sockaddr_in a = {
            .sin_family = AF_INET, .sin_port = htons(AXIS_PORT),
            .sin_addr.s_addr = INADDR_ANY
        };
        bind(fd, (struct sockaddr *)&a, sizeof(a));
        struct ip_mreq mreq = { .imr_multiaddr.s_addr = inet_addr(AXIS_GRP),
                                .imr_interface.s_addr = INADDR_ANY };
        setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq, sizeof(mreq));
        struct timeval tv = {.tv_sec=0, .tv_usec=100000};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    }

    /* load feedback sender → reader :7405 */
    int load_fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in load_addr = {
        .sin_family = AF_INET, .sin_port = htons(LOAD_PORT),
        .sin_addr.s_addr = inet_addr(reader_ip)
    };

    printf("[cpu_reader] axis ← %s:%d  feedback → %s:%d\n",
           AXIS_GRP, AXIS_PORT, reader_ip, LOAD_PORT);

    double history[LOCK_WINDOW];
    int    hist_n = 0, hist_full = 0;
    memset(history, 0, sizeof(history));

    long   cur_khz     = f_max;
    int    cur_mv      = 0;
    int    locked_prev = 0;

    while (running) {
        AxisPulse ap;
        ssize_t n = recv(fd, &ap, sizeof(ap), 0);
        if (n != sizeof(ap)) continue;
        if (ntohs(ap.magic) != AXIS_MAGIC) continue;

        float  theta  = ntohf(ap.theta1);
        float  pd     = ntohf(ap.pd);
        int    locked = ap.locked;

        /* lock detection (local std check supplements axis locked flag) */
        history[hist_n % LOCK_WINDOW] = (double)pd;
        hist_n++;
        if (hist_n >= LOCK_WINDOW) hist_full = 1;
        int cnt = hist_full ? LOCK_WINDOW : hist_n;
        double mean = 0;
        for (int i = 0; i < cnt; i++) mean += history[i];
        mean /= cnt;
        double var = 0;
        for (int i = 0; i < cnt; i++) var += (history[i]-mean)*(history[i]-mean);
        double std = sqrt(var / cnt);
        locked = locked && hist_full && std < LOCK_STD;

        /* Step 3: local load + nudge */
        double load      = load_read_pct();
        double nudge     = load * LOAD_NUDGE_MAX;
        double theta_eff = fmod((double)theta + nudge, 2.0 * M_PI);

        /* Step 1+3+4: apply DVFS — hot cores follow θ_eff, parked stay at floor */
        if (locked) {
            double frac = (1.0 - cos(theta_eff)) / 2.0;
            cur_khz = f_min + (long)((double)(f_max - f_min) * frac);
            cur_mv  = (int)(V_OFFSET_MV * (1.0 + cos(theta_eff)) / 2.0);
            for (int i = 0; i < cores_active; i++)
                set_freq_core(i, cur_khz);
            for (int i = cores_active; i < n_cpus; i++)
                set_freq_core(i, f_min);
            msr_write_voltage(cur_mv);
        } else if (locked_prev) {
            cur_khz = f_max;
            cur_mv  = 0;
            for (int i = 0; i < cores_active; i++)  set_freq_core(i, f_max);
            for (int i = cores_active; i < n_cpus; i++) set_freq_core(i, f_min);
            msr_write_voltage(0);
        }
        locked_prev = locked;

        /* Step 2: instrumentation */
        double power = rapl_read_watts();
        double temp  = temp_read_c();

        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        long t_ms = now.tv_sec * 1000L + now.tv_nsec / 1000000L;

        if (log_fp) {
            fprintf(log_fp, "%ld,%.4f,%.4f,%ld,%d,%.2f,%.1f,%.3f,%d\n",
                    t_ms, theta, theta_eff, cur_khz, cur_mv,
                    power, temp, load, cores_active);
            fflush(log_fp);
        }

        /* send load feedback to reader axis */
        {
            LoadFeedback lf;
            lf.magic = htons(LOAD_MAGIC);
            lf.load  = htonf((float)load);
            lf.temp  = htonf((float)temp);
            sendto(load_fd, &lf, sizeof(lf), 0,
                   (struct sockaddr *)&load_addr, sizeof(load_addr));
        }

        /* progress line */
        char pwr_buf[16], tmp_buf[16];
        if (power >= 0) snprintf(pwr_buf, sizeof(pwr_buf), "%.1fW", power);
        else            snprintf(pwr_buf, sizeof(pwr_buf), "---");
        if (temp  >= 0) snprintf(tmp_buf, sizeof(tmp_buf), "%.0f°C", temp);
        else            snprintf(tmp_buf, sizeof(tmp_buf), "---");

        printf("\r[cpu_reader] θ=%.3f(→%.3f)  %s  %ldkHz  %+dmV  %s  %s  load=%.0f%%  [%d/%dhot]   ",
               theta, theta_eff,
               locked ? "LOCKED" : "      ",
               cur_khz, cur_mv,
               pwr_buf, tmp_buf,
               load * 100.0,
               cores_active, n_cpus);
        fflush(stdout);
    }

    printf("\n[cpu_reader] restoring stock...\n");
    msr_write_voltage(0);
    for (int i = 0; i < n_cpus; i++) set_freq_core(i, f_max);
    restore_governors();
    msr_close_all();
    if (rapl_fd >= 0) close(rapl_fd);
    if (temp_fd >= 0) close(temp_fd);
    if (log_fp)  fclose(log_fp);
    close(load_fd);
    close(fd);
    return 0;
}
