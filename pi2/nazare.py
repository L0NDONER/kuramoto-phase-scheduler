#!/usr/bin/env python3
import socket
import struct
import os
import selectors
import time
import math
import csv
import sys

DCN_ENABLED = "--no-dcn" not in sys.argv

# ------------------------------------------------------------
# Nazaré: staged‑intent, deferred‑commit semantic wave engine
# ------------------------------------------------------------

MCAST_GRP = "239.0.0.2"
MCAST_PORT = 7404

# Cortical output — LMDE slow integrator
CORTEX_IP   = "10.0.0.175"
CORTEX_PORT = 7410

# Cerebellar output — EC2 deep integrator (SSH tunnel → localhost:7420)
CEREBELLUM_IP   = "127.0.0.1"
CEREBELLUM_PORT = 7420

# Pi2 shaper — DCN nudge relay (LAN UDP)
PI2_IP   = "10.0.0.174"
PI2_PORT = 7430

# CortexPulse wire format: magic(H) X(f) Y(f) cycle(I) margin(f) — 18 bytes
_CP_FMT   = ">HffIf"
_CP_MAGIC = 0x4358   # "CX"

# DCN_CONTROL wire format: magic(H) correction(f) — 6 bytes
_DCN_FMT   = ">Hf"
_DCN_SIZE  = struct.calcsize(_DCN_FMT)
_DCN_MAGIC = 0x4443   # "DC"
DCN_PORT   = 7421
DRIFT_THRESH_MIN = 0.05
DRIFT_THRESH_MAX = 0.80

_cortex_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_cerebellum_sock = None
_cerebellum_last_attempt = 0.0
_cerebellum_retry_interval = 1.0

def _get_cerebellum_sock():
    global _cerebellum_sock, _cerebellum_last_attempt, _cerebellum_retry_interval
    if _cerebellum_sock is not None:
        return _cerebellum_sock
    now = time.time()
    if now - _cerebellum_last_attempt < _cerebellum_retry_interval:
        return None
    _cerebellum_last_attempt = now
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((CEREBELLUM_IP, CEREBELLUM_PORT))
        s.settimeout(None)
        _cerebellum_sock = s
        _cerebellum_retry_interval = 1.0
    except OSError:
        _cerebellum_sock = None
        _cerebellum_retry_interval = min(_cerebellum_retry_interval * 2, 60.0)
    return _cerebellum_sock

def send_cortex_pulse(x, y, cycle, margin):
    pkt = struct.pack(_CP_FMT, _CP_MAGIC, x, y, cycle, margin)
    try:
        _cortex_sock.sendto(pkt, (CORTEX_IP, CORTEX_PORT))
    except OSError:
        pass
    global _cerebellum_sock
    s = _get_cerebellum_sock()
    if s:
        try:
            s.sendall(pkt)
        except OSError:
            _cerebellum_sock = None

# FIFOs for stage/commit
FIFO_STAGE = "/tmp/nazare_stage"
FIFO_COMMIT = "/tmp/nazare_commit"

# Create FIFOs if missing
for f in (FIFO_STAGE, FIFO_COMMIT):
    if not os.path.exists(f):
        os.mkfifo(f)

sel = selectors.DefaultSelector()

# ------------------------------------------------------------
# 1. Multicast listener for AxisPulse alignment
# ------------------------------------------------------------

def setup_axispulse_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", MCAST_PORT))

    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    s.setblocking(False)
    return s

axispulse_sock = setup_axispulse_socket()
sel.register(axispulse_sock, selectors.EVENT_READ, data="axispulse")

# DCN_CONTROL listener — EC2 cerebellar correction via reverse SSH tunnel (:7421 TCP)
dcn_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
dcn_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
dcn_srv.bind(("127.0.0.1", DCN_PORT))
dcn_srv.listen(1)
dcn_srv.setblocking(False)
sel.register(dcn_srv, selectors.EVENT_READ, data="dcn_srv")
_dcn_conn = None   # accepted DCN socket

# ------------------------------------------------------------
# 2. FIFOs for staged intents
# ------------------------------------------------------------

def open_fifo(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    sel.register(fd, selectors.EVENT_READ, data=path)
    return fd

stage_fd  = open_fifo(FIFO_STAGE)
commit_fd = open_fifo(FIFO_COMMIT)

# ------------------------------------------------------------
# Internal state
# ------------------------------------------------------------

staged = []          # list of staged intents
last_alignment = {}  # last AxisPulse packet
commit_pending = False
consumer_state = {"A": {}, "B": {}, "pathway": {"X": 0.0, "Y": 0.0}}
theta_prev = {"A": None, "B": None}   # for descent detection
cycle = 0                              # increments each full A drain window

# Pathway stability / synaptic scaling
DRIFT_THRESH = 0.25   # pd_dev above this = incoherent geometry
DECAY_RATE   = 0.95   # multiplicative decay per unstable tick

# Episode logging
EPISODE_CYCLES = 100
LMDE_TEMP_FILE = "/tmp/lmde_temp"
_csv_path = f"/tmp/nazare_episode{'_nodcn' if not DCN_ENABLED else ''}.csv"
_csv_file = open(_csv_path, "w", newline="")
_csv_writer = csv.writer(_csv_file)
_csv_writer.writerow(["ts", "cycle", "pd_dev", "drift_thresh", "dcn_correction", "lmde_temp"])
_csv_file.flush()

_ep_start_cycle = 0
_ep_pd_devs     = []
_ep_temps       = []
_last_dcn_corr  = 0.0

def _read_lmde_temp():
    try:
        return float(open(LMDE_TEMP_FILE).read().strip())
    except Exception:
        return float("nan")

def _log_tick(cycle, pd_dev, dcn_corr, lmde_temp):
    thresh = consumer_state.get("_drift_thresh", DRIFT_THRESH)
    _csv_writer.writerow([f"{time.time():.3f}", cycle, f"{pd_dev:.4f}",
                          f"{thresh:.3f}", f"{dcn_corr:.5f}", f"{lmde_temp:.1f}"])
    _csv_file.flush()
    _ep_pd_devs.append(pd_dev)
    if not math.isnan(lmde_temp):
        _ep_temps.append(lmde_temp)

def _check_episode(cycle):
    global _ep_start_cycle, _ep_pd_devs, _ep_temps
    if cycle - _ep_start_cycle < EPISODE_CYCLES:
        return
    n = len(_ep_pd_devs)
    if n == 0:
        return
    mean_pd = sum(_ep_pd_devs) / n
    var_pd  = sum((v - mean_pd)**2 for v in _ep_pd_devs) / n
    mean_t  = sum(_ep_temps) / len(_ep_temps) if _ep_temps else float("nan")
    var_t   = (sum((v - mean_t)**2 for v in _ep_temps) / len(_ep_temps)
               if len(_ep_temps) > 1 else float("nan"))
    dcn_tag = "DCN=ON " if DCN_ENABLED else "DCN=OFF"
    print(f"[episode] {dcn_tag}  cycles {_ep_start_cycle}–{cycle}"
          f"  pd_dev μ={mean_pd:.4f} σ²={var_pd:.6f}"
          f"  temp μ={mean_t:.1f}°C σ²={var_t:.2f}")
    _ep_start_cycle = cycle
    _ep_pd_devs = []
    _ep_temps   = []

print(f"Nazaré running. DCN={'ON' if DCN_ENABLED else 'OFF'}  CSV→{_csv_path}")

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

# AxisPulse wire format (38 bytes, big-endian):
# H  magic, B sid, B locked, I tick,
# f theta1, f theta2, f pd, f pd_dev, f load_avg,
# H drains, Q t0_ns
_AP_FMT  = ">HBBIfffffHQ"
_AP_SIZE = struct.calcsize(_AP_FMT)   # 38
_AP_MAGIC = 0x4158

def parse_axispulse_packet(data):
    if len(data) < _AP_SIZE:
        return None
    fields = struct.unpack_from(_AP_FMT, data)
    magic, sid, locked, tick, theta1, theta2, pd, pd_dev, load_avg, drains, t0_ns = fields
    if magic != _AP_MAGIC:
        return None
    return dict(sid=sid, locked=locked, tick=tick,
                theta1=theta1, theta2=theta2,
                pd=pd, pd_dev=pd_dev, load_avg=load_avg,
                drains=drains, t0_ns=t0_ns)

_DRAIN_WIN = 0.25   # rad either side of drain point

def _a_leads(pkt):
    """True when A is ahead of anti-phase: signed pd > π."""
    signed = (pkt["theta1"] - pkt["theta2"]) % (2 * math.pi)
    return signed > math.pi

def _eval_one(cond, pkt):
    """Evaluate a single condition token."""
    if not cond:
        return True
    if cond == "A.descending":
        return theta_prev["A"] is not None and pkt["theta1"] < theta_prev["A"]
    if cond == "A.ascending":
        return theta_prev["A"] is not None and pkt["theta1"] > theta_prev["A"]
    if cond == "B.descending":
        return theta_prev["B"] is not None and pkt["theta2"] < theta_prev["B"]
    if cond == "B.ascending":
        return theta_prev["B"] is not None and pkt["theta2"] > theta_prev["B"]
    if cond == "A.leading":
        return _a_leads(pkt)
    if cond == "A.following":
        return not _a_leads(pkt)
    if cond == "cycle=even":
        return cycle % 2 == 0
    if cond == "cycle=odd":
        return cycle % 2 == 1
    if "." in cond and "=" in cond:
        who, rest = cond.split(".", 1)
        key, _, val = rest.partition("=")
        return consumer_state.get(who, {}).get(key) == val
    return True

def _eval_condition(cond, pkt):
    """Evaluate condition string; '+' = AND, '|' = OR."""
    if not cond:
        return True
    if "+" in cond:
        return all(_eval_one(c.strip(), pkt) for c in cond.split("+"))
    if "|" in cond:
        return any(_eval_one(c.strip(), pkt) for c in cond.split("|"))
    return _eval_one(cond, pkt)

def _apply_intent(target, body, theta):
    """
    Apply intent body to target consumer. Body forms:
      key=val          set state
      key+=num         increment float state
      key-=num         decrement float state
      action           plain fire
    Target may differ from the firing consumer (cross-consumer plasticity).
    """
    st = consumer_state[target]
    if "+=" in body:
        key, _, val = body.partition("+=")
        st[key] = float(st.get(key, 0)) + float(val)
        return f"{target}.{key} += {val} → {st[key]:.4f}"
    elif "-=" in body:
        key, _, val = body.partition("-=")
        st[key] = float(st.get(key, 0)) - float(val)
        return f"{target}.{key} -= {val} → {st[key]:.4f}"
    elif "=" in body:
        key, _, val = body.partition("=")
        st[key] = val
        return f"{target}.{key} = {val}"
    else:
        return f"{body} fired"

def _parse_intent(raw):
    """Parse 'PREFIX:body?condition:flags' → (prefix, body, cond, flags)."""
    prefix, _, rest = raw.partition(":")
    flags_part = ""
    if rest.count(":") >= 1 and "?" in rest:
        body_cond, _, flags_part = rest.rpartition(":")
    elif rest.count(":") >= 1 and "?" not in rest:
        body_cond, _, flags_part = rest.rpartition(":")
    else:
        body_cond = rest
    body, _, cond = body_cond.partition("?")
    return prefix, body.strip(), cond.strip(), flags_part.strip()

def _apply_autonomous_decay(pkt):
    """
    Apply multiplicative decay to pathway weights if drift is high.
    Maintains synaptic homeostasis at the cerebellar level.
    Called before intent evaluation so intents always see current valid state.
    """
    if pkt["pd_dev"] > consumer_state.get("_drift_thresh", DRIFT_THRESH):
        pw = consumer_state["pathway"]
        pw["X"] = float(pw.get("X", 0)) * DECAY_RATE
        pw["Y"] = float(pw.get("Y", 0)) * DECAY_RATE
        x, y = pw["X"], pw["Y"]
        print(f"[stability] pd_dev={pkt['pd_dev']:.3f}  decay → X={x:.4f} Y={y:.4f}")
        send_cortex_pulse(x, y, cycle, abs(x - y))

def _try_fire(staged, pkt):
    """Fire intents in their drain window that pass their condition."""
    global cycle
    theta1 = pkt["theta1"]
    theta2 = pkt["theta2"]
    a_win = theta1 < _DRAIN_WIN or theta1 > (2*math.pi - _DRAIN_WIN)
    b_win = abs(theta2 - math.pi) < _DRAIN_WIN
    if a_win:
        cycle += 1
    remaining = []
    for intent in staged:
        prefix, body, cond, flags = _parse_intent(intent)
        in_win = (prefix == "A" and a_win) or (prefix == "B" and b_win)
        theta = theta1 if prefix == "A" else theta2
        if in_win and _eval_condition(cond, pkt):
            # body may target another consumer: "B.key+=val" fired by A
            if "." in body and body.split(".")[0] in consumer_state:
                target, _, tbody = body.partition(".")
            else:
                target, tbody = prefix, body
            msg = _apply_intent(target, tbody, theta)
            print(f"[{prefix}→{target}] θ={theta:.3f}  cycle={cycle}  {msg}")
            # binary decision readout + cortex relay when pathway weights updated
            if target == "pathway":
                pw = consumer_state["pathway"]
                x, y = float(pw.get("X", 0)), float(pw.get("Y", 0))
                decision = "YES (A leads)" if x > y else "NO  (B leads)"
                margin = abs(x - y)
                print(f"  ↳ decision: {decision}  X={x:.2f} Y={y:.2f} margin={margin:.2f}")
                send_cortex_pulse(x, y, cycle, margin)
            if "repeat" in flags.split(","):
                remaining.append(intent)   # re-stage for next cycle
        else:
            remaining.append(intent)
    theta_prev["A"] = theta1
    theta_prev["B"] = theta2
    return remaining

# ------------------------------------------------------------
# Main loop
# ------------------------------------------------------------

print(f"Nazaré: AxisPulse + FIFO stage/commit.")

while True:
    events = sel.select(timeout=0.1)

    for key, _ in events:
        tag = key.data

        # -------------------------
        # AxisPulse alignment
        # -------------------------
        if tag == "axispulse":
            data, _ = axispulse_sock.recvfrom(2048)
            pkt = parse_axispulse_packet(data)
            if pkt:
                last_alignment = pkt
                if pkt["locked"]:
                    _apply_autonomous_decay(pkt)
                    pw = consumer_state["pathway"]
                    send_cortex_pulse(float(pw.get("X", 0)), float(pw.get("Y", 0)), cycle, 0.0)
                    lmde_temp = _read_lmde_temp()
                    _log_tick(cycle, pkt["pd_dev"], _last_dcn_corr, lmde_temp)
                    _check_episode(cycle)
                if commit_pending and staged and pkt["locked"]:
                    staged = _try_fire(staged, pkt)
                    if not staged:
                        commit_pending = False

        # -------------------------
        # DCN_CONTROL from EC2
        # -------------------------
        elif tag == "dcn_srv":
            # new connection from EC2 via reverse tunnel
            conn, _ = dcn_srv.accept()
            conn.setblocking(False)
            if _dcn_conn:
                try: sel.unregister(_dcn_conn); _dcn_conn.close()
                except OSError: pass
            _dcn_conn = conn
            sel.register(_dcn_conn, selectors.EVENT_READ, data="dcn_data")
            print("[dcn] EC2 cerebellar connection established")

        elif tag == "dcn_data":
            try:
                data = key.fileobj.recv(64)
            except OSError:
                data = b""
            if not data:
                sel.unregister(key.fileobj); key.fileobj.close()
                _dcn_conn = None
            else:
                while len(data) >= _DCN_SIZE:
                    magic, correction = struct.unpack_from(_DCN_FMT, data)
                    data = data[_DCN_SIZE:]
                    if magic == _DCN_MAGIC:
                        _last_dcn_corr = correction
                        # relay to pi2 shaper (LAN UDP, always — shaper gates internally)
                        try:
                            _cortex_sock.sendto(
                                struct.pack(_DCN_FMT, _DCN_MAGIC, correction),
                                (PI2_IP, PI2_PORT))
                        except OSError:
                            pass
                        if DCN_ENABLED:
                            thresh = consumer_state.get("_drift_thresh", DRIFT_THRESH)
                            thresh = max(DRIFT_THRESH_MIN, min(DRIFT_THRESH_MAX, thresh + correction))
                            consumer_state["_drift_thresh"] = thresh
                            print(f"[dcn] correction={correction:+.4f}  DRIFT_THRESH→{thresh:.3f}")

        # -------------------------
        # Stage FIFO
        # -------------------------
        elif tag in (FIFO_STAGE, FIFO_COMMIT):
            fd = key.fd
            try:
                raw = os.read(fd, 4096)
            except BlockingIOError:
                raw = b""
            if not raw:
                # EOF — writer disconnected; reopen
                sel.unregister(fd)
                os.close(fd)
                path = tag
                if path == FIFO_STAGE:
                    stage_fd = open_fifo(path)
                else:
                    commit_fd = open_fifo(path)
                continue
            if tag == FIFO_STAGE:
                for msg in raw.decode("utf-8").splitlines():
                    msg = msg.strip()
                    if msg:
                        staged.append(msg)
                        print("Staged:", msg)
            else:
                msg = raw.decode("utf-8").strip()
                if msg:
                    print("Commit requested:", msg)
                    commit_pending = True

    # If commit requested but no alignment yet, keep waiting
    time.sleep(0.01)
