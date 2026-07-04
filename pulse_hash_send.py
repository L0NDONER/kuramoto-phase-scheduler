#!/usr/bin/env python3
"""
pulse_hash_send.py — pulse-coded hash over a live quartz tick.

Sample one live AxisPulse packet: (tick, theta1, theta2, pd, pd_dev).
That tuple only exists once, at the instant it's observed — it's the salt.
Hash it to 256 bits, then *spell* the hash across physical ticks:
pulse (UDP datagram) on tick i iff bit_i == 1, silence iff bit_i == 0.

Nothing that goes on the wire is the salt itself — only its pulse-coded
shadow. The receiver has to reconstruct the hash from arrival timing
alone (pulse_hash_recv.py).

Usage:
    python3 pulse_hash_send.py [--demo TEXT] [--dest HOST]
"""
import ctypes, hashlib, math, socket, struct, sys, time

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

PS_GRP  = "239.0.0.7"; PS_PORT = 7470
PS_MAGIC = 0x5053   # "PS"
PS_START, PS_PULSE, PS_END = 1, 2, 3
PS_START_FMT = ">HBHI"   # magic, type, n_bits, tick_us
PS_PULSE_FMT = ">HBH"    # magic, type, bit_index
PS_END_FMT   = ">HB"     # magic, type


class _timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_int), ("offset", ctypes.c_long),
                ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                ("tolerance", ctypes.c_long), ("time", ctypes.c_long * 2),
                ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
                ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long),
                ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
                ("pad", ctypes.c_int * 11)]


def read_freq_ppm():
    tx = _timex()
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.adjtimex(ctypes.byref(tx)) < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return tx.freq / 65536.0


def capture_phase_salt(timeout_s=5.0):
    """Block for one locked AxisPulse packet, return (tick,theta1,theta2,pd,pd_dev)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", AP_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(AP_GRP) + socket.inet_aton("0.0.0.0"))
    s.settimeout(timeout_s)
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        try:
            data, _ = s.recvfrom(64)
        except socket.timeout:
            break
        if len(data) < AP_SIZE:
            continue
        f = struct.unpack_from(AP_FMT, data)
        if f[0] != AP_MAGIC or not f[2]:
            continue
        return f[3], f[4], f[5], f[6], f[7]   # tick, theta1, theta2, pd, pd_dev
    return None


def main():
    dest = "239.0.0.7"
    demo = None
    if "--dest" in sys.argv:
        dest = sys.argv[sys.argv.index("--dest") + 1]
    if "--demo" in sys.argv:
        demo = sys.argv[sys.argv.index("--demo") + 1]

    if demo is not None:
        salt_src = f"demo:{demo}"
        print(f"[send] --demo mode, no live crystal lock required, salt_src={salt_src!r}")
    else:
        print("[send] waiting for locked AxisPulse to sample phase-salt...", flush=True)
        sample = capture_phase_salt()
        if sample is None:
            sys.exit("[send] no locked AxisPulse packet received — is quartz_beacon.py running? "
                     "(or use --demo TEXT to test without a live crystal)")
        tick, theta1, theta2, pd, pd_dev = sample
        salt_src = f"{tick}:{theta1:.6f}:{theta2:.6f}:{pd:.6f}:{pd_dev:.6f}"
        print(f"[send] sampled phase-salt  tick={tick} theta1={theta1:.4f} "
              f"theta2={theta2:.4f} pd={pd:.4f} pd_dev={pd_dev:.5f}")

    H = hashlib.sha256(salt_src.encode()).digest()
    bits = []
    for byte in H:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    n_bits = len(bits)
    print(f"[send] hash={H.hex()}  ({n_bits} bits to pulse-code)")

    ppm = read_freq_ppm()
    tick_s = 1.0 / abs(ppm)
    tick_us = int(tick_s * 1e6)
    print(f"[send] freq_ppm={ppm:.6f}  tick={tick_s*1000:.3f}ms (live adjtimex, this crystal's own pace)")

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    start_pkt = struct.pack(PS_START_FMT, PS_MAGIC, PS_START, n_bits, tick_us)
    out.sendto(start_pkt, (dest, PS_PORT))
    print(f"[send] START  n_bits={n_bits}  tick_us={tick_us}  → {dest}:{PS_PORT}", flush=True)

    t_next = time.monotonic()
    n_pulses = 0
    for i, b in enumerate(bits):
        t_next += tick_s
        gap = t_next - time.monotonic()
        if gap > 0:
            time.sleep(gap)
        if b:
            out.sendto(struct.pack(PS_PULSE_FMT, PS_MAGIC, PS_PULSE, i), (dest, PS_PORT))
            n_pulses += 1
        print("█" if b else "░", end="", flush=True)

    t_next += tick_s
    gap = t_next - time.monotonic()
    if gap > 0:
        time.sleep(gap)
    out.sendto(struct.pack(PS_END_FMT, PS_MAGIC, PS_END), (dest, PS_PORT))
    print(f"\n[send] END  {n_pulses}/{n_bits} ticks pulsed  hash={H.hex()}", flush=True)


if __name__ == "__main__":
    main()
