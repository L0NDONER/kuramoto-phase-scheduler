#!/usr/bin/env python3
"""
bio.py — Organism biography log.

Appends one JSONL record per REPORT ticks to organism.bio.
START sentinel on launch, END sentinel on clean exit.
A gap between END and the next START is a death — the chain broke.

Fields: iso, tick, theta, pd, reflex, age_s
"""
import json, os, selectors, signal, socket, struct, sys, time

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

RS_GRP  = "239.0.0.4"; RS_PORT = 7450
RS_FMT  = "!HBff";     RS_SIZE = struct.calcsize(RS_FMT);   RS_MAGIC = 0x5253
RS_NAME = ["CALM", "ALERT", "WITHDRAW", "PARK", "RECOVER"]

BIO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "organism.bio")
REPORT   = 1000   # ticks between entries (~25s at 40Hz)


def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


bio = open(BIO_PATH, "a")


def _write(rec):
    bio.write(json.dumps(rec) + "\n")
    bio.flush()


t0 = time.time()
_write({"event": "START", "iso": _iso(t0), "t": round(t0, 3)})
print(f"[bio] → {BIO_PATH}", flush=True)


def _on_exit(*_):
    t = time.time()
    _write({"event": "END", "iso": _iso(t), "t": round(t, 3),
            "age_s": round(t - t0, 1)})
    bio.close()
    print("[bio] END written — chain closed", flush=True)
    sys.exit(0)


signal.signal(signal.SIGTERM, _on_exit)
signal.signal(signal.SIGINT,  _on_exit)

ap_sock = _mcast_in(AP_GRP, AP_PORT)
rs_sock = _mcast_in(RS_GRP, RS_PORT)

sel = selectors.DefaultSelector()
sel.register(ap_sock, selectors.EVENT_READ, data="ap")
sel.register(rs_sock, selectors.EVENT_READ, data="rs")

reflex    = 0
last_tick = -1

while True:
    for key, _ in sel.select(timeout=1.0):
        if key.data == "ap":
            data, _ = ap_sock.recvfrom(64)
            if len(data) < AP_SIZE:
                continue
            f = struct.unpack_from(AP_FMT, data)
            if f[0] != AP_MAGIC or not f[2]:   # unlocked ticks don't count
                continue
            tick, theta, pd = f[3], f[4], f[6]
            if tick % REPORT == 0 and tick != last_tick:
                last_tick = tick
                t = time.time()
                rec = {
                    "iso":    _iso(t),
                    "t":      round(t, 3),
                    "tick":   tick,
                    "theta":  round(theta, 4),
                    "pd":     round(pd, 5),
                    "reflex": RS_NAME[reflex],
                    "age_s":  round(t - t0, 1),
                }
                _write(rec)
                print(f"[bio] tick={tick:8d}  θ={theta:.4f}  pd={pd:.5f}"
                      f"  {RS_NAME[reflex]}  age={rec['age_s']}s", flush=True)

        elif key.data == "rs":
            data, _ = rs_sock.recvfrom(16)
            if len(data) >= RS_SIZE:
                f = struct.unpack_from(RS_FMT, data)
                if f[0] == RS_MAGIC and 0 <= f[1] < len(RS_NAME):
                    reflex = f[1]
