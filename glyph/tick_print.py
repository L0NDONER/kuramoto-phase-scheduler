#!/usr/bin/env python3
"""
glyph/tick_print.py — waits for sid1's beacon tick to advance ~10 minutes
worth of ticks from now, then prints the wall-clock time to the printer.

Ticks (not wall clock) are the wait condition: watches the real RAW
beacon counter for sid=1 (Pi1) rather than sleeping, so a stalled carrier
stalls the wait too instead of firing on a dead substrate.
"""
import selectors, socket, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from phase_auth import gate_check

RAW_GRP, RAW_PORT = "239.0.0.1", 7400
RAW_FMT = "!HBIffBQ"; RAW_MAGIC = 0x1B4A
SID = 1

PRINTER_IP   = "10.0.0.82"
PRINTER_PORT = 9100

WAIT_S = 600  # ~10 minutes, converted to ticks via the live rate


def mcast_in():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", RAW_PORT))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(RAW_GRP) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def read_tick(sock, sel):
    for key, _ in sel.select(timeout=1.0):
        data, _ = sock.recvfrom(64)
        if len(data) < struct.calcsize(RAW_FMT):
            continue
        f = struct.unpack_from(RAW_FMT, data)
        if f[0] != RAW_MAGIC or f[1] != SID:
            continue
        return f[2]
    return None


def print_time():
    body = f"TIME\r\n{time.ctime()}\r\n\f"
    sock = socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=5)
    sock.sendall(body.encode("ascii"))
    sock.close()


def main():
    sock = mcast_in()
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)

    print("[tick_print] measuring sid1 tick rate...", flush=True)
    t0 = time.time()
    first = last = None
    while time.time() - t0 < 3.0:
        tick = read_tick(sock, sel)
        if tick is None:
            continue
        if first is None:
            first = tick
        last = tick
    if first is None or last is None or last == first:
        print("[tick_print] no sid1 beacon traffic seen — aborting", flush=True)
        sys.exit(1)

    rate = (last - first) / (time.time() - t0)
    target = last + int(rate * WAIT_S)
    print(f"[tick_print] rate={rate:.1f} ticks/s  now={last}  target={target}"
          f"  (~{WAIT_S/60:.0f} min)", flush=True)

    while True:
        tick = read_tick(sock, sel)
        if tick is None:
            continue
        if tick >= target:
            break

    print(f"[tick_print] target tick reached ({tick}) — phase-auth gate...", flush=True)
    if not gate_check():
        print("[tick_print] GATE BLOCKED — no LAN prover responded", flush=True)
        sys.exit(1)

    print(f"[tick_print] print → {PRINTER_IP}:{PRINTER_PORT}", flush=True)
    print_time()
    print("[tick_print] sent", flush=True)


if __name__ == "__main__":
    main()
