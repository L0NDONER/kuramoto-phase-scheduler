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
consumer_state = {"A": {}, "B": {}}
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

def _eval_condition(cond, pkt):
    """
    Evaluate a condition string against current state.
    Supported forms:
      B.key=val      consumer_state["B"]["key"] == "val"
      A.key=val      consumer_state["A"]["key"] == "val"
      A.descending   theta1 is falling this tick
      B.descending   theta2 is falling this tick
      cycle=even     cycle counter is even
      cycle=odd      cycle counter is odd
    """
    if not cond:
        return True
    if cond == "A.descending":
        return theta_prev["A"] is not None and pkt["theta1"] < theta_prev["A"]
    if cond == "B.descending":
        return theta_prev["B"] is not None and pkt["theta2"] < theta_prev["B"]
    if cond == "cycle=even":
        return cycle % 2 == 0
    if cond == "cycle=odd":
        return cycle % 2 == 1
    if "." in cond and "=" in cond:
        who, rest = cond.split(".", 1)
        key, _, val = rest.partition("=")
        return consumer_state.get(who, {}).get(key) == val
    return True

def _apply_intent(consumer, body, theta):
    """Parse and apply intent body (key=val or plain action). Returns display string."""
    if "=" in body:
        key, _, val = body.partition("=")
        consumer_state[consumer][key] = val
        return f"set {key}={val}  state={consumer_state[consumer]}"
    else:
        return f"{body} fired"

def _try_fire(staged, pkt):
    """Fire intents that are in their drain window and pass their condition."""
    global cycle
    theta1 = pkt["theta1"]
    theta2 = pkt["theta2"]
    a_win = theta1 < _DRAIN_WIN or theta1 > (2*math.pi - _DRAIN_WIN)
    b_win = abs(theta2 - math.pi) < _DRAIN_WIN
    if a_win:
        cycle += 1
    remaining = []
    for intent in staged:
        if intent.startswith("A:"):
            body_cond = intent[2:].strip()
            body, _, cond = body_cond.partition("?")
            if a_win and _eval_condition(cond, pkt):
                msg = _apply_intent("A", body, theta1)
                print(f"[A] θ1={theta1:.3f}  cycle={cycle}  {msg}")
            else:
                remaining.append(intent)
        elif intent.startswith("B:"):
            body_cond = intent[2:].strip()
            body, _, cond = body_cond.partition("?")
            if b_win and _eval_condition(cond, pkt):
                msg = _apply_intent("B", body, theta2)
                print(f"[B] θ2={theta2:.3f}  cycle={cycle}  {msg}")
            else:
                remaining.append(intent)
        else:
            if pkt["locked"]:
                print(f"[*] {intent}")
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
