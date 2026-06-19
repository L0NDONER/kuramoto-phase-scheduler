import turtle
import math
import time
import random
import socket
import struct
import traceback as _tb

# ------------------------------------
# KURAMOTO TWO-OSCILLATOR MODEL
# Live beacon receiver + simulation fallback
# ------------------------------------

OMEGA_MINT   = 0.048
OMEGA_PI     = 0.060
K            = 0.040   # tuned: π − arcsin(Δω/2K) ≈ 2.99
NOISE        = 0.003
PHASE_TARGET = 3.0
ANTI_THRESH  = 0.15

ORBIT_R     = 160
TRACE_X     = 0
TRACE_Y     = -210
TRACE_W     = 280
TRACE_H     = 60
HISTORY     = 120

FS_DRAIN_THRESH = 0.15

SCREEN_W    = 700
SCREEN_H    = 680

# Beacon format (mirrors beacon.py)
MCAST_GRP   = "239.0.0.1"
MCAST_PORT  = 7400
MAGIC       = 0x1B4A
FMT         = "!HBIff x"
BEACON_SIZE = struct.calcsize(FMT)
BEACON_TIMEOUT = 1.0   # seconds before falling back to simulation

# ------------------------------------
# BEACON SOCKET
# ------------------------------------
_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
_rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_rx.bind(("", MCAST_PORT))
_mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
_rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, _mreq)
_rx.setblocking(False)

def drain_beacons():
    """Read all pending beacons, return {sender_id: theta} for freshest of each."""
    latest = {}
    ticks  = {}
    try:
        while True:
            data, _ = _rx.recvfrom(64)
            if len(data) < BEACON_SIZE:
                continue
            magic, sender_id, tick, theta, omega = struct.unpack(FMT, data)
            if magic != MAGIC:
                continue
            if sender_id not in ticks or tick > ticks[sender_id]:
                ticks[sender_id]  = tick
                latest[sender_id] = theta
    except BlockingIOError:
        pass
    return latest

# ------------------------------------
# SETUP
# ------------------------------------
screen = turtle.Screen()
screen.setup(SCREEN_W, SCREEN_H)
screen.bgcolor("black")
screen.tracer(0)

def make_pen(color="white", width=1, hide=True):
    t = turtle.Turtle()
    t.penup()
    t.color(color)
    t.width(width)
    if hide:
        t.hideturtle()
    return t

mint_dot = turtle.Turtle(shape="circle")
mint_dot.penup(); mint_dot.shapesize(2.5); mint_dot.color("cyan")

pi_dot = turtle.Turtle(shape="circle")
pi_dot.penup(); pi_dot.shapesize(2.5); pi_dot.color("orange")

link = make_pen("white", 2)

FS_X, FS_Y = 0, 60

fs_dot = turtle.Turtle(shape="circle")
fs_dot.penup(); fs_dot.shapesize(1.8); fs_dot.color("tomato")
fs_dot.goto(FS_X, FS_Y)

fs_lbl = make_pen("tomato")
fs_lbl.goto(FS_X, FS_Y - 24)
fs_lbl.write("Firestick", align="center", font=("Arial", 10, "bold"))
fs_pulse = make_pen("white", 1)

for name, angle, col in [("Mint", 90, "cyan"), ("Pi", 270, "orange")]:
    lbl = make_pen(col)
    r = ORBIT_R + 35
    lbl.goto(r * math.cos(math.radians(angle)),
             r * math.sin(math.radians(angle)) + 10)
    lbl.write(name, align="center", font=("Arial", 12, "bold"))

border = make_pen("gray")
border.goto(TRACE_X - TRACE_W, TRACE_Y - TRACE_H); border.pendown()
border.goto(TRACE_X + TRACE_W, TRACE_Y - TRACE_H)
border.goto(TRACE_X + TRACE_W, TRACE_Y + TRACE_H)
border.goto(TRACE_X - TRACE_W, TRACE_Y + TRACE_H)
border.goto(TRACE_X - TRACE_W, TRACE_Y - TRACE_H); border.penup()


antiy = TRACE_Y + TRACE_H - 4
antiline = make_pen((0.3, 0.4, 1.0))
antiline.goto(TRACE_X - TRACE_W, antiy); antiline.pendown()
antiline.goto(TRACE_X + TRACE_W, antiy); antiline.penup()
antiline.goto(TRACE_X - TRACE_W + 5, antiy - 16)
antiline.write("anti-phase  φ=π", font=("Arial", 8, "normal"))

status_lbl  = make_pen("white")
status_lbl.goto(TRACE_X + TRACE_W - 5, TRACE_Y + TRACE_H - 18)

mode_lbl  = make_pen("gray")
mode_lbl.goto(TRACE_X - TRACE_W + 5, TRACE_Y + TRACE_H - 18)

trace_pen = make_pen("lime green", 2)

# ------------------------------------
# STATE
# ------------------------------------
theta_mint = 0.0
theta_pi   = PHASE_TARGET   # start at lock point

phase_history  = []
prev_phase     = None
tick           = 0
fs_drain_count = 0
fs_pulse_ttl   = 0
prev_theta_mod = None    # for half-revolution crossing detection

last_live = {0: 0.0, 1: 0.0}   # monotonic time of last beacon per sender

# ------------------------------------
# MAIN LOOP
# ------------------------------------
while True:
    # --- receive live beacons ---
    beacons = drain_beacons()
    now = time.monotonic()
    live_mint = live_pi = False

    if 0 in beacons:
        theta_mint = beacons[0]
        last_live[0] = now
    if 1 in beacons:
        theta_pi = beacons[1]
        last_live[1] = now

    live_mint = (now - last_live[0]) < BEACON_TIMEOUT
    live_pi   = (now - last_live[1]) < BEACON_TIMEOUT
    any_live  = live_mint or live_pi

    # --- simulation fallback for sides with no live beacon ---
    if not live_mint or not live_pi:
        diff       = theta_pi - theta_mint
        prev_pd    = abs((diff + math.pi) % (2 * math.pi) - math.pi)
        noise = NOISE
        if not live_mint:
            theta_mint = (theta_mint + OMEGA_MINT - K * math.sin(diff) + random.gauss(0, noise)) % (2 * math.pi)
        if not live_pi:
            theta_pi   = (theta_pi   + OMEGA_PI   - K * math.sin(-diff) + random.gauss(0, noise)) % (2 * math.pi)

    phase_diff  = abs((theta_pi - theta_mint + math.pi) % (2 * math.pi) - math.pi)
    antiphasing = abs(phase_diff - PHASE_TARGET) < ANTI_THRESH

    # --- draw oscillators ---
    mx = ORBIT_R * math.cos(theta_mint)
    my = ORBIT_R * math.sin(theta_mint)
    px = ORBIT_R * math.cos(theta_pi)
    py = ORBIT_R * math.sin(theta_pi)

    mint_dot.goto(mx, my + 60)
    pi_dot.goto(px, py + 60)

    if antiphasing:
        mint_dot.color("cornflower blue"); pi_dot.color("cornflower blue")
    else:
        mint_dot.color("cyan");           pi_dot.color("orange")

    # live indicator — dot brightens when fed from real beacon
    if live_mint: mint_dot.shapesize(3.0)
    else:         mint_dot.shapesize(2.5)
    if live_pi:   pi_dot.shapesize(3.0)
    else:         pi_dot.shapesize(2.5)

    # --- connecting line ---
    link.clear()
    link.goto(mx, my + 60); link.pendown()
    link.goto(px, py + 60); link.penup()

    # --- Firestick drain ---
    # Fires each time the rotating diameter completes a half-revolution —
    # detected as a θ % π wraparound, matching beacon.py drain logic.
    curr_mod = theta_mint % math.pi
    draining = False
    if prev_theta_mod is not None and curr_mod < prev_theta_mod:
        draining = True
    prev_theta_mod = curr_mod

    if draining and fs_pulse_ttl == 0:
        fs_drain_count += 1
        fs_pulse_ttl = 10

    fs_pulse.clear()
    if fs_pulse_ttl > 0:
        fs_dot.color("white")
        r = 14 + (10 - fs_pulse_ttl) * 8
        fs_pulse.penup(); fs_pulse.goto(FS_X, FS_Y - r); fs_pulse.pendown()
        fs_pulse.width(2)
        fs_pulse.color(1.0, 1.0 * fs_pulse_ttl / 10, 0.0)
        fs_pulse.circle(r); fs_pulse.penup()
        fs_pulse_ttl -= 1
    else:
        fs_dot.color("tomato")

    # --- phase-diff trace ---
    rate = abs(phase_diff - prev_phase) if prev_phase is not None else 0.0
    prev_phase = phase_diff
    phase_history.append((phase_diff, rate))
    if len(phase_history) > HISTORY:
        phase_history.pop(0)

    max_rate = max((r for _, r in phase_history), default=1e-6)

    trace_pen.clear(); trace_pen.penup()
    prev_tx = prev_ty = None
    for j, (pd, r) in enumerate(phase_history):
        tx = TRACE_X - TRACE_W + (j / HISTORY) * TRACE_W * 2
        ty = TRACE_Y + (pd / math.pi) * TRACE_H * 1.8
        ty = max(TRACE_Y - TRACE_H + 2, min(TRACE_Y + TRACE_H - 2, ty))
        w  = max(1, int(1 + 5 * (r / max_rate))) if max_rate > 0 else 1
        col = ((0.3, 0.4, 1.0) if abs(pd - PHASE_TARGET) < ANTI_THRESH else
               (0.2, 0.8, 0.2))
        if prev_tx is None:
            trace_pen.goto(tx, ty)
        else:
            trace_pen.penup(); trace_pen.goto(prev_tx, prev_ty)
            trace_pen.color(col); trace_pen.width(w)
            trace_pen.pendown(); trace_pen.goto(tx, ty)
        prev_tx, prev_ty = tx, ty
    trace_pen.penup()

    # --- status labels ---
    status_lbl.clear()
    status = f"LOCKED  drains: {fs_drain_count}" if antiphasing else f"drains: {fs_drain_count}"
    status_lbl.write(status, align="right", font=("Arial", 9, "normal"))

    mode_lbl.clear()
    if live_mint and live_pi:
        mode_str = "● LIVE"
        mode_col = (0.2, 1.0, 0.2)
    elif any_live:
        mode_str = "◑ LIVE/SIM"
        mode_col = (1.0, 0.8, 0.0)
    else:
        mode_str = "○ SIM"
        mode_col = (0.5, 0.5, 0.5)
    mode_lbl.color(mode_col)
    mode_lbl.write(mode_str, align="left", font=("Arial", 9, "normal"))

    tick += 1
    try:
        screen.update()
    except Exception as _e:
        with open("/tmp/turtle_crash.log", "w") as _f:
            _tb.print_exc(file=_f)
            _f.write(f"\ntick={tick}\n")
        raise
    time.sleep(0.05)
