import turtle
import math
import time
import random

# ------------------------------------
# KURAMOTO TWO-OSCILLATOR MODEL
# Mint and Pi with weak coupling + noise
# Kiss = phase difference collapses to near zero
# ------------------------------------

OMEGA_MINT  = 0.048        # natural frequency (rad/tick)
OMEGA_PI    = 0.060        # Δω = 0.012
K           = 0.030        # repulsive coupling; anti-phase (φ=π) is the attractor
NOISE       = 0.008        # reduced: anti-phase basin needs less kick to stay put
LOCK_NOISE  = 0.000        # noise clamped to this once inside anti-phase threshold
KISS_THRESH  = 0.18        # phase diff (rad) that counts as a kiss
ANTI_THRESH  = 0.25        # distance from π that counts as anti-phase (wider to show lock)

ORBIT_R     = 160          # oscillator orbit radius
TRACE_X     = 0            # phase-diff trace centre x
TRACE_Y     = -210         # phase-diff trace centre y
TRACE_W     = 280          # half-width of trace window
TRACE_H     = 60           # half-height of trace window
HISTORY     = 120          # ticks of phase-diff history to show

FS_DRAIN_THRESH = 0.15       # how close gap must pass to axis to trigger drain
FS_ALPHA        = 0.12       # PLL follow rate (not used for position, used for pulse decay)

SCREEN_W    = 700
SCREEN_H    = 680

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

# Oscillator dots
mint_dot = turtle.Turtle(shape="circle")
mint_dot.penup()
mint_dot.shapesize(2.5)
mint_dot.color("cyan")

pi_dot = turtle.Turtle(shape="circle")
pi_dot.penup()
pi_dot.shapesize(2.5)
pi_dot.color("orange")

# Connecting line between oscillators
link = make_pen("white", 2)

# Firestick — fixed at the axis
fs_dot = turtle.Turtle(shape="circle")
fs_dot.penup()
fs_dot.shapesize(1.8)
fs_dot.color("tomato")
fs_dot.goto(0, 60)

fs_lbl = make_pen("tomato")
fs_lbl.goto(0, 30)
fs_lbl.write("Firestick", align="center", font=("Arial", 10, "bold"))

fs_pulse = make_pen("white", 1)   # drain flash ring

# Labels
for name, angle, col in [("Mint", 90, "cyan"), ("Pi", 270, "orange")]:
    lbl = make_pen(col)
    r = ORBIT_R + 35
    lbl.goto(r * math.cos(math.radians(angle)),
             r * math.sin(math.radians(angle)) + 10)
    lbl.write(name, align="center", font=("Arial", 12, "bold"))

# Phase-diff trace border
border = make_pen("gray")
border.goto(TRACE_X - TRACE_W, TRACE_Y - TRACE_H)
border.pendown()
for dx, dy in [(TRACE_W*2, 0), (0, TRACE_H*2), (-TRACE_W*2, 0), (0, -TRACE_H*2)]:
    border.forward(math.hypot(dx, dy)) if dx == 0 or dy == 0 else None
    # just draw the box manually
border.penup()
border.goto(TRACE_X - TRACE_W, TRACE_Y - TRACE_H)
border.pendown()
border.goto(TRACE_X + TRACE_W, TRACE_Y - TRACE_H)
border.goto(TRACE_X + TRACE_W, TRACE_Y + TRACE_H)
border.goto(TRACE_X - TRACE_W, TRACE_Y + TRACE_H)
border.goto(TRACE_X - TRACE_W, TRACE_Y - TRACE_H)
border.penup()

# Kiss zone line + label (φ=0, bottom)
zeroline = make_pen((0.8, 0.2, 0.2))
zeroline.goto(TRACE_X - TRACE_W, TRACE_Y)
zeroline.pendown()
zeroline.goto(TRACE_X + TRACE_W, TRACE_Y)
zeroline.penup()
zeroline.goto(TRACE_X - TRACE_W + 5, TRACE_Y + 3)
zeroline.write("kiss  φ=0", font=("Arial", 8, "normal"))

# Anti-phase zone line + label (φ=π, top)
antiy = TRACE_Y + TRACE_H - 4
antiline = make_pen((0.3, 0.4, 1.0))
antiline.goto(TRACE_X - TRACE_W, antiy)
antiline.pendown()
antiline.goto(TRACE_X + TRACE_W, antiy)
antiline.penup()
antiline.goto(TRACE_X - TRACE_W + 5, antiy - 16)
antiline.write("anti-phase  φ=π", font=("Arial", 8, "normal"))

# Kiss counter label
kiss_lbl = make_pen("white")
kiss_lbl.goto(TRACE_X + TRACE_W - 5, TRACE_Y + TRACE_H - 18)

# Trace pen
trace_pen = make_pen("lime green", 2)

# Kiss flash overlay on oscillators
flash = make_pen("white", 1)

# ------------------------------------
# STATE
# ------------------------------------
theta_mint = 0.0
theta_pi   = math.pi / 3    # start offset so they're not in sync

phase_history = []   # list of (phase_diff, rate)
prev_phase    = None
kiss_count    = 0
tick          = 0

fs_drain_count = 0
fs_pulse_ttl   = 0   # ticks remaining on drain flash

# ------------------------------------
# MAIN LOOP
# ------------------------------------
import traceback as _tb

while True:
    # --- Kuramoto update ---
    diff = theta_pi - theta_mint
    prev_phase_diff = abs((theta_pi - theta_mint + math.pi) % (2 * math.pi) - math.pi)
    antiphasing = prev_phase_diff > (math.pi - ANTI_THRESH)

    noise_level = LOCK_NOISE if antiphasing else NOISE
    d_mint = OMEGA_MINT - K * math.sin(diff)  + random.gauss(0, noise_level)
    d_pi   = OMEGA_PI   - K * math.sin(-diff) + random.gauss(0, noise_level)

    theta_mint = (theta_mint + d_mint) % (2 * math.pi)
    theta_pi   = (theta_pi   + d_pi)   % (2 * math.pi)

    phase_diff  = abs((theta_pi - theta_mint + math.pi) % (2 * math.pi) - math.pi)
    kissing     = phase_diff < KISS_THRESH
    antiphasing = phase_diff > (math.pi - ANTI_THRESH)

    if kissing:
        kiss_count += 1

    # --- Draw oscillators ---
    mx = ORBIT_R * math.cos(theta_mint)
    my = ORBIT_R * math.sin(theta_mint)
    px = ORBIT_R * math.cos(theta_pi)
    py = ORBIT_R * math.sin(theta_pi)

    mint_dot.goto(mx, my + 60)
    pi_dot.goto(px, py + 60)

    if kissing:
        mint_dot.color("white")
        pi_dot.color("white")
    elif antiphasing:
        mint_dot.color("cornflower blue")
        pi_dot.color("cornflower blue")
    else:
        mint_dot.color("cyan")
        pi_dot.color("orange")

    # --- Connecting line ---
    link.clear()
    link.goto(mx, my + 60)
    link.pendown()
    link.goto(px, py + 60)
    link.penup()

    # --- Firestick drain ---
    # Gap is the midpoint angle between the two oscillators on the orbit.
    # Project it to a point on the orbit and measure distance to axis (0,60).
    # When either gap point sweeps close to the axis, that's a drain event.
    gap_theta_a = (theta_mint + theta_pi) / 2
    gap_theta_b = gap_theta_a + math.pi
    gap_ax = ORBIT_R * math.cos(gap_theta_a)
    gap_ay = ORBIT_R * math.sin(gap_theta_a) + 60
    gap_bx = ORBIT_R * math.cos(gap_theta_b)
    gap_by = ORBIT_R * math.sin(gap_theta_b) + 60
    dist_a = math.hypot(gap_ax, gap_ay - 60)
    dist_b = math.hypot(gap_bx, gap_by - 60)
    gap_near = min(dist_a, dist_b)
    draining = gap_near < FS_DRAIN_THRESH * ORBIT_R

    if draining and fs_pulse_ttl == 0:
        fs_drain_count += 1
        fs_pulse_ttl = 6

    # pulse ring
    fs_pulse.clear()
    if fs_pulse_ttl > 0:
        fs_dot.color("white")
        r = 18 + (6 - fs_pulse_ttl) * 6
        fs_pulse.penup()
        fs_pulse.goto(0, 60 - r)
        fs_pulse.pendown()
        fs_pulse.color(1.0, 1.0 * fs_pulse_ttl / 6, 0.0)
        fs_pulse.circle(r)
        fs_pulse.penup()
        fs_pulse_ttl -= 1
    else:
        fs_dot.color("tomato")

    # --- Phase-diff trace ---
    rate = abs(phase_diff - prev_phase) if prev_phase is not None else 0.0
    prev_phase = phase_diff
    phase_history.append((phase_diff, rate))
    if len(phase_history) > HISTORY:
        phase_history.pop(0)

    max_rate = max((r for _, r in phase_history), default=1e-6)

    trace_pen.clear()
    trace_pen.penup()
    prev_tx, prev_ty, prev_w = None, None, None
    for j, (pd, r) in enumerate(phase_history):
        tx = TRACE_X - TRACE_W + (j / HISTORY) * TRACE_W * 2
        ty = TRACE_Y + (pd / math.pi) * TRACE_H * 1.8
        ty = max(TRACE_Y - TRACE_H + 2, min(TRACE_Y + TRACE_H - 2, ty))
        w  = max(1, int(1 + 5 * (r / max_rate))) if max_rate > 0 else 1

        if pd < KISS_THRESH:
            col = (1.0, 0.2, 0.2)
        elif pd > (math.pi - ANTI_THRESH):
            col = (0.3, 0.4, 1.0)
        else:
            col = (0.2, 0.8, 0.2)

        if prev_tx is None:
            trace_pen.goto(tx, ty)
        else:
            trace_pen.penup()
            trace_pen.goto(prev_tx, prev_ty)
            trace_pen.color(col)
            trace_pen.width(w)
            trace_pen.pendown()
            trace_pen.goto(tx, ty)

        prev_tx, prev_ty, prev_w = tx, ty, w

    trace_pen.penup()

    # --- Kiss counter ---
    kiss_lbl.clear()
    status = f"LOCKED  drains: {fs_drain_count}" if antiphasing else f"kisses: {kiss_count}  drains: {fs_drain_count}"
    kiss_lbl.write(status, align="right", font=("Arial", 9, "normal"))

    tick += 1
    try:
        screen.update()
    except Exception as _e:
        with open("/tmp/turtle_crash.log", "w") as _f:
            _tb.print_exc(file=_f)
            _f.write(f"\ntick={tick} kisses={kiss_count} kissing={kissing}\n")
        raise
    time.sleep(0.05)
