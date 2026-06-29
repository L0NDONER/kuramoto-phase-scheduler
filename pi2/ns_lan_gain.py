#!/usr/bin/env python3
"""
ns_lan_gain.py — LAN ceiling modulator driven by Pi2 NucleusState.

Subscribes to 239.0.0.3:7440 (NucleusState from pi2_reader).
Modulates ceil on tc class 1:20 (Firestick, 10.0.0.131) based on
Pi2's thermal headroom — backing off burst allowance when Pi2 is
under thermal stress, restoring when Pi2 cools.

G_LAN = f(temlum, e_C):
  temlum ≤ 0              → G_LAN = 1.0  (full 20Mbit ceil)
  temlum = +HEADROOM      → G_LAN = G_MIN (5Mbit floor)
  e_C biases ±10% on top

Asymmetric EMA: slow attack (compress slowly on warming),
fast release (restore quickly on cooling).

Run: sudo python3 ns_lan_gain.py
"""
import socket, struct, subprocess, time

NS_GRP   = "239.0.0.3"
NS_PORT  = 7440
NS_MAGIC = 0x4E53
NS_FMT   = "!HfffBB"

WITHDRAW_DIP_S  = 0.30
WITHDRAW_HYST_S = 1.50

TC       = "/usr/sbin/tc"
TC_DEV   = "eth0"
TC_CLASS = "1:20"
RATE_MAX = 20        # Mbit — nominal ceil
RATE_MIN = 5         # Mbit — floor under thermal stress
G_MIN    = RATE_MIN / RATE_MAX

HEADROOM     = 1.5   # °C above T_TARGET for full compression
EC_SCALE     = 0.05  # e_C range for ±10% bias

ALPHA_ATTACK  = 0.05  # slow: ~20 ticks to fully reflect a heat spike
ALPHA_RELEASE = 0.30  # fast: ~3 ticks to restore on cooling

INTENT = {0: "PARK", 1: "HOLD", 2: "UNPARK"}

def set_ceil(mbit):
    cmd = [TC, "class", "change", "dev", TC_DEV, "classid", TC_CLASS,
           "htb", "rate", f"{mbit}mbit", "ceil", f"{mbit}mbit",
           "burst", "64k"]
    subprocess.run(cmd, check=True)

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", NS_PORT))
    mreq = socket.inet_aton(NS_GRP) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(10.0)

    temlum_ema       = 0.0
    e_C_ema          = 0.0
    last_ceil        = RATE_MAX
    withdraw_until   = 0.0
    hysteresis_until = 0.0

    print(f"[ns_lan_gain] {TC_DEV} class {TC_CLASS}  "
          f"ceil {RATE_MIN}–{RATE_MAX}Mbit  listening {NS_GRP}:{NS_PORT}", flush=True)

    while True:
        try:
            data, _ = sock.recvfrom(32)
        except TimeoutError:
            print("[ns_lan_gain] timeout — no NucleusState", flush=True)
            continue

        if len(data) < 16:
            continue
        magic, e_C, temlum, pd_pop, intent, withdrawal = struct.unpack(NS_FMT, data[:16])
        if magic != NS_MAGIC:
            continue

        now = time.time()
        if withdrawal:
            withdraw_until   = now + WITHDRAW_DIP_S
            hysteresis_until = now + WITHDRAW_HYST_S
            print("[ns_lan_gain] WITHDRAWAL → burst suppression", flush=True)

        # asymmetric EMA — slow attack, fast release
        alpha_t = ALPHA_ATTACK if temlum > temlum_ema else ALPHA_RELEASE
        temlum_ema = alpha_t * temlum + (1 - alpha_t) * temlum_ema
        e_C_ema    = 0.10 * e_C + 0.90 * e_C_ema

        # thermal factor: 1.0 when cool, G_MIN when HEADROOM above setpoint
        t = max(0.0, min(1.0, temlum_ema / HEADROOM))
        thermal = 1.0 - t * (1.0 - G_MIN)

        # e_C bias: ±10%, positive e_C (UNPARK headroom) relaxes ceiling slightly
        ec_bias = max(-0.10, min(0.10, e_C_ema / EC_SCALE * 0.10))

        G_LAN = max(G_MIN, min(1.0, thermal + ec_bias))
        if now < withdraw_until:
            G_LAN = G_MIN
        elif now < hysteresis_until:
            G_LAN = G_MIN + (G_LAN - G_MIN) * 0.5
        ceil_mbit = round(RATE_MIN + (RATE_MAX - RATE_MIN) * G_LAN)

        print(f"[ns_lan_gain] temlum={temlum_ema:+.3f} e_C={e_C_ema:+.5f} "
              f"G={G_LAN:.3f} ceil={ceil_mbit}Mbit  {INTENT.get(intent,'?')}", flush=True)

        if ceil_mbit != last_ceil:
            set_ceil(ceil_mbit)
            last_ceil = ceil_mbit

if __name__ == "__main__":
    main()
