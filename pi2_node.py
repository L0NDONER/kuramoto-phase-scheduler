#!/usr/bin/env python3
# Pi2 — thermal reply (sid=2)
import selectors, socket, struct, sys, time

from quartz_core import phase_auth

RAW_GRP, RAW_PORT = "239.0.0.1", 7400
RAW_MAGIC = 0x1B4A
RAW_FMT   = "!HBIffBQ"
RAW_SIZE  = struct.calcsize(RAW_FMT)

AP_GRP, AP_PORT = "239.0.0.2", 7405
EVT_MAGIC = 0xE701
EVT_FMT   = "!HBd"
EVT_SIZE  = struct.calcsize(EVT_FMT)

NS_GRP, NS_PORT = "239.0.0.3", 7440
NS_MAGIC = 0x4E53
NS_FMT   = "!HfffBB"
NS_SIZE  = struct.calcsize(NS_FMT)

SID          = 2
WAIT_PI1_S   = 1500.0  # Pi1's own WAIT state runs ~1200s (20min); leave ~5min margin
THERMAL_MAX  = 3.00   # raw-packet sanity bound, matches ns_wan_gain.TEMLUM_SANITY_MAX —
                       # not reflex.py's PARK_TEMP=1.00, which is only valid against
                       # EMA-smoothed temlum, not a single raw NucleusState packet

PRINTER_HOST, PRINTER_PORT = "10.0.0.82", 9100


def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def print_job(message):
    payload = f"TIME\n{time.time()}\n{message}\n\f"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((PRINTER_HOST, PRINTER_PORT))
    s.sendall(payload.encode())
    s.close()


def publish_event(t):
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    out.sendto(struct.pack(EVT_FMT, EVT_MAGIC, SID, t), (AP_GRP, AP_PORT))
    out.close()


def main():
    raw_sock = _mcast_in(RAW_GRP, RAW_PORT)   # noqa: F841 — subscribed per spec, unused otherwise
    ap_sock = _mcast_in(AP_GRP, AP_PORT)
    ns_sock = _mcast_in(NS_GRP, NS_PORT)

    sel = selectors.DefaultSelector()
    sel.register(ap_sock, selectors.EVENT_READ, data="ap")
    sel.register(ns_sock, selectors.EVENT_READ, data="ns")

    # STATE WAIT_PI1
    deadline = time.time() + WAIT_PI1_S
    pi1_time = None
    while pi1_time is None:
        remaining = deadline - time.time()
        if remaining <= 0:
            print("[pi2] WAIT_PI1 TIMEOUT — no Pi1 completion event", flush=True)
            sys.exit(1)
        for key, _ in sel.select(timeout=min(remaining, 1.0)):
            if key.data != "ap":
                continue
            data, _ = ap_sock.recvfrom(64)
            if len(data) < EVT_SIZE:
                continue
            f = struct.unpack_from(EVT_FMT, data)
            if f[0] == EVT_MAGIC and f[1] == 1:
                pi1_time = f[2]
                break

    print(f"[pi2] WAIT_PI1 observed sid=1 t={pi1_time}", flush=True)

    # STATE THERMAL_OK
    ns_raw = None
    ns_deadline = time.time() + 5.0
    while ns_raw is None and time.time() < ns_deadline:
        for key, _ in sel.select(timeout=0.5):
            if key.data != "ns":
                continue
            data, _ = ns_sock.recvfrom(64)
            if len(data) >= NS_SIZE:
                ns_raw = data
                break

    if ns_raw is None:
        print("[pi2] THERMAL_OK FAIL — no NucleusState observed", flush=True)
        sys.exit(1)

    magic, e_C, temlum, pd_pop, intent, wd = struct.unpack_from(NS_FMT, ns_raw)
    if magic != NS_MAGIC or abs(temlum) > THERMAL_MAX:
        print(f"[pi2] THERMAL_OK FAIL — temlum={temlum} outside safe bounds "
              f"(±{THERMAL_MAX})", flush=True)
        sys.exit(1)

    # STATE GATE
    if not phase_auth.gate_check():
        print("[pi2] GATE FAIL — phase_auth.gate_check() returned False", flush=True)
        sys.exit(1)

    # STATE PRINT
    pi2_time = time.time()
    print_job("hi")
    publish_event(pi2_time)
    print(f"[pi2] PRINT sid=2 t={pi2_time}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
