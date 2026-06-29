#!/usr/bin/env python3
"""
reflex.py — Global reflex layer: thin supervisory state machine.

Inputs:
  239.0.0.2:7404  AxisPulse       — pd_dev, locked, oscillator age
  239.0.0.3:7440  NucleusState    — temlum, e_C, withdrawal

States:
  CALM(0) → ALERT(1) → WITHDRAW(2) → PARK(3)
            ↑                              |
          RECOVER(4) ←────────────────────┘

  Fast to degrade, slow to recover. Hysteresis is structural.

Output:
  239.0.0.4:7450  RefleState  magic(H) state(B) pd_dev(f) temlum(f) — 11 bytes
  Emitted on every locked AxisPulse tick (and every 0.5s idle).

Consumers obey RefleState; they do no health assessment of their own.
"""
import socket, struct, selectors, time

CALM=0; ALERT=1; WITHDRAW=2; PARK=3; RECOVER=4
_NAME = ["CALM","ALERT","WITHDRAW","PARK","RECOVER"]

# -- Thresholds --
ALERT_PD       = 0.15   # pd_dev rising → ALERT
ALERT_TEMP     = 0.80   # temlum °C above setpoint → ALERT
WITHDRAW_PD    = 0.30   # pd_dev badly incoherent → WITHDRAW
PARK_TEMP      = 1.00   # thermal stress → PARK
RECOVER_TEMP   = 0.40   # must cool below this to begin RECOVER
AP_STALE_S     = 5.0    # no AxisPulse → WITHDRAW

# -- Hysteresis --
CALM_HOLD_S    = 5.0    # clear this long to exit ALERT → CALM
RECOVER_HOLD_S = 10.0   # stable this long to exit RECOVER → CALM

# -- Wire formats --
_AP_FMT  = ">HBBIfffffHQ";  _AP_SIZE = struct.calcsize(_AP_FMT);  _AP_MAGIC = 0x4158
_NS_FMT  = "!HfffBB";       _NS_SIZE = struct.calcsize(_NS_FMT);  _NS_MAGIC = 0x4E53
RS_FMT   = "!HBff";         RS_MAGIC  = 0x5253   # "RS"

AP_GRP = "239.0.0.2"; AP_PORT = 7404
NS_GRP = "239.0.0.3"; NS_PORT = 7440
RS_GRP = "239.0.0.4"; RS_PORT = 7450


def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


ap_sock = _mcast_in(AP_GRP, AP_PORT)
ns_sock = _mcast_in(NS_GRP, NS_PORT)

rs_out  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rs_out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
rs_addr = (RS_GRP, RS_PORT)

sel = selectors.DefaultSelector()
sel.register(ap_sock, selectors.EVENT_READ, data="ap")
sel.register(ns_sock, selectors.EVENT_READ, data="ns")

state             = CALM
alert_clear_since = None
recover_since     = None
last_ap_t         = time.time()
pd_dev = 0.0; temlum = 0.0; withdrawal = False
pd_ema = 0.0; temlum_ema = 0.0   # smoothed inputs for threshold decisions


def emit():
    try:
        rs_out.sendto(struct.pack(RS_FMT, RS_MAGIC, state, pd_dev, temlum), rs_addr)
    except OSError:
        pass


def go(new):
    global state, alert_clear_since, recover_since
    if new == state:
        return
    print(f"[reflex] {_NAME[state]} → {_NAME[new]}", flush=True)
    state = new
    alert_clear_since = recover_since = None


print(f"[reflex] AP:{AP_PORT} NS:{NS_PORT} → {RS_GRP}:{RS_PORT}", flush=True)

while True:
    for key, _ in sel.select(timeout=0.5):
        tag = key.data
        if tag == "ap":
            data, _ = ap_sock.recvfrom(64)
            if len(data) >= _AP_SIZE:
                f = struct.unpack_from(_AP_FMT, data)
                if f[0] == _AP_MAGIC and f[2]:   # locked only
                    pd_dev    = f[7]
                    pd_ema    = 0.20 * pd_dev    + 0.80 * pd_ema
                    last_ap_t = time.time()
        elif tag == "ns":
            data, _ = ns_sock.recvfrom(32)
            if len(data) >= _NS_SIZE:
                f = struct.unpack_from(_NS_FMT, data)
                if f[0] == _NS_MAGIC:
                    temlum     = f[2]
                    temlum_ema = 0.20 * temlum   + 0.80 * temlum_ema
                    withdrawal = bool(f[5])

    now    = time.time()
    stale  = (now - last_ap_t) > AP_STALE_S
    hot    = temlum_ema > PARK_TEMP
    warm   = temlum_ema > ALERT_TEMP
    noisy  = pd_ema > ALERT_PD
    ragged = pd_ema > WITHDRAW_PD

    if state == CALM:
        if hot:                               go(PARK)
        elif withdrawal or ragged or stale:   go(WITHDRAW)
        elif warm or noisy:
            print(f"[reflex] trigger warm={warm}(ema={temlum_ema:.3f}) noisy={noisy}(ema={pd_ema:.4f})", flush=True)
            go(ALERT)

    elif state == ALERT:
        if hot:                               go(PARK)
        elif withdrawal or ragged or stale:   go(WITHDRAW)
        elif not warm and not noisy:
            if alert_clear_since is None:     alert_clear_since = now
            elif now - alert_clear_since >= CALM_HOLD_S: go(CALM)
        else:
            alert_clear_since = None

    elif state == WITHDRAW:
        if hot:                               go(PARK)
        elif not withdrawal and not ragged and not stale and not warm:
                                              go(RECOVER)

    elif state == PARK:
        if not hot and temlum < RECOVER_TEMP: go(RECOVER)

    elif state == RECOVER:
        if hot:                               go(PARK)
        elif withdrawal or ragged or stale:   go(WITHDRAW)
        elif warm or noisy:                   go(ALERT)
        else:
            if recover_since is None:         recover_since = now
            elif now - recover_since >= RECOVER_HOLD_S: go(CALM)

    emit()
