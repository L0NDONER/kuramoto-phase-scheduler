#!/usr/bin/env python3
"""
ns_wan_gain.py — Mint WAN egress, Nazaré-coupled continuous shaper.

The WAN rate is a modulated derivative of the Nazaré carrier — not a
standalone oscillator.  On every locked AxisPulse tick:

  WAN_rate(t) = BaseWAN × G(theta, margin, temlum, e_C)

  G = thermal(temlum) × [G_BASE + (1−G_BASE) × mod_depth(margin) × carrier(theta)]
    + ec_bias(e_C)

  carrier(theta) = (1 − cos θ₁) / 2          — trough=0 at θ=0, peak=1 at θ=π
  mod_depth      = clamp(margin / MARGIN_SCALE, 0, 1)  — nazaré population gain
  thermal        = 1 − clamp(max(0, temlum) / HEADROOM, 0, 1−G_MIN)  — Pi2 headroom
  ec_bias        = clamp(e_C / EC_SCALE × 0.15, −0.15, +0.15)

Inputs (non-blocking; latest value used on each AP tick):
  239.0.0.2:7404  AxisPulse multicast    — theta1, locked   (pacing clock)
  239.0.0.3:7440  NucleusState multicast — temlum, e_C       (Pi2 thermal state)
  127.0.0.1:7411  CortexPulse loopback   — X, Y, margin      (nazaré gain field)

Output: tc HTB rate on IFACE 1:10, range RATE_MIN–RATE_MAX Mbit, N_STEPS quantised.
Manages its own qdisc; tears it down on SIGINT/SIGTERM.

Run: sudo python3 ns_wan_gain.py [iface]
"""

import math, signal, socket, struct, subprocess, sys, time

IFACE    = sys.argv[1] if len(sys.argv) > 1 else "enp0s31f6"
TC       = "/usr/sbin/tc"
CLASSID  = "1:10"
RATE_MAX = 900     # Mbit nominal WAN ceil
RATE_MIN = 450     # Mbit floor (G=0)
N_STEPS  = 16      # quantisation — limits tc syscall rate

# Gain shaping constants
G_MIN        = 0.0          # G at full thermal stress (floor from RATE_MIN)
G_BASE       = 0.30         # minimum gain at carrier trough (30% × RATE range)
HEADROOM     = 1.5          # °C above T_TARGET for full thermal compression
EC_SCALE     = 0.05         # e_C range for ±15% bias
EC_MOD_SCALE = 0.02         # e_C for full mod depth (E_UNPARK=0.004, headroom ~5×)

# EMA alphas for thermal (asymmetric: slow attack, fast release)
ALPHA_ATTACK  = 0.05
ALPHA_RELEASE = 0.30

# Wire formats
AP_MAGIC  = 0x4158;  AP_FMT  = "!HBBIfffffHQ"   # AxisPulse 38 bytes
NS_MAGIC  = 0x4E53;  NS_FMT  = "!HfffBB"         # NucleusState 16 bytes

WITHDRAW_DIP_S   = 0.30   # rate floor duration on withdrawal signal
WITHDRAW_HYST_S  = 1.50   # half-restored hysteresis after dip
CP_MAGIC  = 0x4358;  CP_FMT  = "!HffIf"          # CortexPulse 18 bytes

AP_PORT = 7404;  AP_GRP = "239.0.0.2"
NS_PORT = 7440;  NS_GRP = "239.0.0.3"


def tc_run(args):
    subprocess.run([TC] + args, check=True)

def setup_qdisc():
    subprocess.run([TC, "qdisc", "del", "dev", IFACE, "root"], capture_output=True)
    tc_run(["qdisc", "add", "dev", IFACE, "root", "handle", "1:", "htb", "default", "10"])
    tc_run(["class", "add", "dev", IFACE, "parent", "1:", "classid", CLASSID,
            "htb", "rate", f"{RATE_MAX}mbit", "ceil", f"{RATE_MAX}mbit",
            "burst", "1500k", "quantum", "1514"])

def teardown_qdisc():
    subprocess.run([TC, "qdisc", "del", "dev", IFACE, "root"], capture_output=True)

def set_rate(mbit):
    tc_run(["class", "change", "dev", IFACE, "classid", CLASSID,
            "htb", "rate", f"{mbit}mbit", "ceil", f"{mbit}mbit",
            "burst", "1500k", "quantum", "1514"])

def _mcast_sock(port, grp):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def drain(sock, size):
    """Read all pending packets, return last valid payload or None."""
    last = None
    while True:
        try:
            data, _ = sock.recvfrom(size)
            last = data
        except BlockingIOError:
            break
    return last


def main():
    setup_qdisc()

    def _exit(sig, frame):
        teardown_qdisc()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT,  _exit)

    ap_sock = _mcast_sock(AP_PORT, AP_GRP)
    ap_sock.setblocking(True)
    ap_sock.settimeout(1.0)    # pace on AxisPulse ticks; 1s timeout = watchdog
    ns_sock = _mcast_sock(NS_PORT, NS_GRP)

    AP_SIZE = struct.calcsize(AP_FMT)   # 38
    NS_SIZE = struct.calcsize(NS_FMT)   # 16

    temlum_ema       = 0.0
    e_C_ema          = 0.0
    last_step        = -1
    last_intent      = 1
    withdraw_until   = 0.0
    hysteresis_until = 0.0

    print(f"[ns_wan_gain] {IFACE} {CLASSID}  {RATE_MIN}–{RATE_MAX}Mbit  "
          f"steps={N_STEPS}  G_BASE={G_BASE}", flush=True)

    while True:
        # -- drain NucleusState (latest wins) --
        raw = drain(ns_sock, 32)
        if raw and len(raw) >= NS_SIZE:
            magic, e_C, temlum, pd_pop, intent, withdrawal = struct.unpack(NS_FMT, raw[:NS_SIZE])
            if magic == NS_MAGIC:
                a = ALPHA_ATTACK if temlum > temlum_ema else ALPHA_RELEASE
                temlum_ema = a * temlum + (1 - a) * temlum_ema
                e_C_ema    = 0.10 * e_C + 0.90 * e_C_ema
                if withdrawal:
                    now = time.time()
                    withdraw_until   = now + WITHDRAW_DIP_S
                    hysteresis_until = now + WITHDRAW_HYST_S
                    print(f"[ns_wan_gain] WITHDRAWAL → rate dip {WITHDRAW_DIP_S*1000:.0f}ms", flush=True)

        # -- wait for a locked AxisPulse tick --
        try:
            raw, _ = ap_sock.recvfrom(64)
        except TimeoutError:
            continue

        if not raw or len(raw) < AP_SIZE:
            continue
        fields = struct.unpack(AP_FMT, raw[:AP_SIZE])
        magic, sid, locked, tick = fields[0], fields[1], fields[2], fields[3]
        theta1 = fields[4]
        if magic != AP_MAGIC or not locked:
            continue

        # -- compute G_WAN --
        carrier    = (1.0 - math.cos(theta1)) / 2.0
        mod_depth  = min(1.0, max(0.0, e_C_ema) / EC_MOD_SCALE)
        t          = max(0.0, min(1.0, temlum_ema / HEADROOM))
        thermal    = 1.0 - t * (1.0 - G_MIN)
        ec_bias    = max(-0.15, min(0.15, e_C_ema / EC_SCALE * 0.15))

        G_WAN      = thermal * (G_BASE + (1.0 - G_BASE) * mod_depth * carrier) + ec_bias
        G_WAN      = max(0.0, min(1.0, G_WAN))

        now = time.time()
        if now < withdraw_until:
            G_WAN = 0.0
        elif now < hysteresis_until:
            G_WAN *= 0.5

        # -- quantise to N_STEPS, only tc when step changes --
        step      = round(G_WAN * N_STEPS)
        rate_mbit = RATE_MIN + round((RATE_MAX - RATE_MIN) * step / N_STEPS)

        if step != last_step:
            set_rate(rate_mbit)
            last_step = step
            print(f"[ns_wan_gain] θ={theta1:.3f} carrier={carrier:.3f} "
                  f"e_C={e_C_ema:+.5f} temlum={temlum_ema:+.3f} "
                  f"G={G_WAN:.3f} → {rate_mbit}Mbit", flush=True)


if __name__ == "__main__":
    main()
