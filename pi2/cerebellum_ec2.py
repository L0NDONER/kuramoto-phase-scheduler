#!/usr/bin/env python3
"""
Cerebellar EC2 receiver — pure observer.
CortexPulse in:  magic(H) X(f) Y(f) cycle(I) margin(f)  — 18 bytes  ← forward tunnel :7420
DCN out:         magic(H) pred_err_ema(f)                —  6 bytes  → reverse tunnel :7421
Shaper (pi2) owns all control logic via temlum + composite error.
"""
import socket, struct, time

LISTEN_PORT    = 7420
DCN_IP         = "127.0.0.1"
DCN_PORT       = 7421
EMA_ALPHA      = 0.005
UNCERTAIN_BAND = 1.0
DCN_INTERVAL   = 20

_CP_FMT   = ">HffIf"
_CP_SIZE  = struct.calcsize(_CP_FMT)
_CP_MAGIC = 0x4358

_DCN_FMT   = ">Hf"
_DCN_MAGIC = 0x4443

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", LISTEN_PORT))
srv.listen(1)

print(f"[cerebellum] TCP :{LISTEN_PORT}  α={EMA_ALPHA}  DCN→:{DCN_PORT}", flush=True)

ema_x = ema_y = 0.0
event = 0
state = "UNCERTAIN"
prev_diff = None
pred_err_ema = 0.0

_dcn_conn = None
_dcn_last_attempt = 0.0

def _get_dcn_conn():
    global _dcn_conn, _dcn_last_attempt
    if _dcn_conn is not None:
        return _dcn_conn
    now = time.time()
    if now - _dcn_last_attempt < 2.0:
        return None
    _dcn_last_attempt = now
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((DCN_IP, DCN_PORT))
        s.settimeout(None)
        _dcn_conn = s
        print(f"[cerebellum] DCN connected", flush=True)
    except OSError as e:
        print(f"[cerebellum] DCN connect failed: {e}", flush=True)
        _dcn_conn = None
    return _dcn_conn

def send_obs(val):
    global _dcn_conn
    s = _get_dcn_conn()
    if not s:
        return
    try:
        s.sendall(struct.pack(_DCN_FMT, _DCN_MAGIC, val))
    except OSError:
        _dcn_conn = None

while True:
    conn, addr = srv.accept()
    print(f"[cerebellum] CortexPulse from {addr}", flush=True)
    buf = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= _CP_SIZE:
                magic, x, y, cycle, margin = struct.unpack_from(_CP_FMT, buf)
                buf = buf[_CP_SIZE:]
                if magic != _CP_MAGIC:
                    continue

                event += 1
                ema_x = EMA_ALPHA * x + (1 - EMA_ALPHA) * ema_x
                ema_y = EMA_ALPHA * y + (1 - EMA_ALPHA) * ema_y
                diff  = ema_x - ema_y

                if prev_diff is not None:
                    pred_err_ema = 0.05 * abs(diff - prev_diff) + 0.95 * pred_err_ema
                prev_diff = diff

                prev = state
                if diff > UNCERTAIN_BAND:    state = "YES"
                elif diff < -UNCERTAIN_BAND: state = "NO"
                else:                        state = "UNCERTAIN"
                if state != prev:
                    print(f"[cerebellum] STATE → {state}  (after {event} events)", flush=True)

                if event % DCN_INTERVAL == 0:
                    send_obs(pred_err_ema)
                    print(f"[cerebellum] event={event:6d}  diff={diff:+.3f}"
                          f"  pred_err={pred_err_ema:.5f}  state={state}", flush=True)

    except Exception as e:
        print(f"[cerebellum] connection lost: {e}", flush=True)
    finally:
        conn.close()
