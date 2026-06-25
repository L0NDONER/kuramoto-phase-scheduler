#!/usr/bin/env python3
import socket
import struct
import os
import selectors
import time
import math

# ------------------------------------------------------------
# Nazaré: staged‑intent, deferred‑commit semantic wave engine
# ------------------------------------------------------------

MCAST_GRP = "239.0.0.2"
MCAST_PORT = 7404

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
            # binary decision readout when pathway weights updated
            if target == "pathway":
                pw = consumer_state["pathway"]
                x, y = float(pw.get("X", 0)), float(pw.get("Y", 0))
                decision = "YES (A leads)" if x > y else "NO  (B leads)"
                margin = abs(x - y)
                print(f"  ↳ decision: {decision}  X={x:.2f} Y={y:.2f} margin={margin:.2f}")
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

print("Nazaré running. Listening for AxisPulse + FIFO stage/commit.")

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
                if commit_pending and staged and pkt["locked"]:
                    staged = _try_fire(staged, pkt)
                    if not staged:
                        commit_pending = False

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
