#!/usr/bin/env python3
# Pi1 — sender (sid=1)
import selectors, socket, struct, sys, time

from quartz_core import i0_phase_auth as phase_auth

RAW_GRP, RAW_PORT = "239.0.0.1", 7400
RAW_MAGIC = 0x1B4A
RAW_FMT   = "!HBIffBQ"
RAW_SIZE  = struct.calcsize(RAW_FMT)

AP_GRP, AP_PORT = "239.0.0.2", 7405
EVT_MAGIC = 0xE701
EVT_FMT   = "!HBd"

SID          = 1
RATE_WINDOW_S = 4.0
LEAD_TICKS    = 1200
GUARD_WINDOW  = 500   # ticks; ~5-7s slack at 75-100 tps, matches PEER_STALE_S/AP_STALE_S scale

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


def _next_tick(sel, sock):
    for key, _ in sel.select(timeout=1.0):
        data, _ = sock.recvfrom(64)
        if len(data) < RAW_SIZE:
            continue
        f = struct.unpack_from(RAW_FMT, data)
        if f[0] != RAW_MAGIC or f[1] != SID:
            continue
        return f[2]
    return None


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
    raw_sock = _mcast_in(RAW_GRP, RAW_PORT)
    sel = selectors.DefaultSelector()
    sel.register(raw_sock, selectors.EVENT_READ, data="raw")

    # STATE INIT
    t0 = time.time()
    first_tick = None
    last_tick = None
    n_ticks = 0
    while time.time() - t0 < RATE_WINDOW_S:
        tick = _next_tick(sel, raw_sock)
        if tick is None:
            continue
        if first_tick is None:
            first_tick = tick
        last_tick = tick
        n_ticks += 1

    if first_tick is None or last_tick is None or last_tick <= first_tick:
        print("[pi1] INIT FAIL — no ticks observed in rate window", flush=True)
        sys.exit(1)

    rate = (last_tick - first_tick) / RATE_WINDOW_S
    target_tick = last_tick + round(rate * LEAD_TICKS)
    print(f"[pi1] INIT rate={rate:.2f} tick/s target_tick={target_tick}", flush=True)

    # STATE WAIT
    current_tick = last_tick
    while current_tick < target_tick:
        tick = _next_tick(sel, raw_sock)
        if tick is None:
            continue
        current_tick = tick
        if current_tick > target_tick + GUARD_WINDOW:
            print(f"[pi1] WAIT ABORT — tick {current_tick} exceeded "
                  f"target_tick+GUARD_WINDOW ({target_tick + GUARD_WINDOW})", flush=True)
            sys.exit(1)

    # STATE GATE
    if not phase_auth.gate_check():
        print("[pi1] GATE FAIL — phase_auth.gate_check() returned False", flush=True)
        sys.exit(1)

    # STATE PRINT
    pi1_time = time.time()
    print_job("helloworld")
    publish_event(pi1_time)
    print(f"[pi1] PRINT sid=1 t={pi1_time}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
