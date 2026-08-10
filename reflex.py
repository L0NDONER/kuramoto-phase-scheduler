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
import math, socket, struct, selectors, time

CALM=0; ALERT=1; WITHDRAW=2; PARK=3; RECOVER=4
_NAME = ["CALM","ALERT","WITHDRAW","PARK","RECOVER"]

# -- Thresholds --
ALERT_PD       = 0.15   # pd_dev rising → ALERT
ALERT_TEMP     = 0.80   # temlum °C above setpoint → ALERT
WITHDRAW_PD    = 0.30   # pd_dev badly incoherent → WITHDRAW
PARK_TEMP      = 1.00   # thermal stress → PARK
RECOVER_TEMP   = 0.40   # must cool below this to begin RECOVER
AP_STALE_S     = 5.0    # no AxisPulse → WITHDRAW

# pd_dev alone can't see a peer going stale: a frozen peer's theta makes pd
# drift smoothly as our own theta advances, so the frame-to-frame EMA in
# pd_dev stays low for the full PEER_STALE_S (3.0s in quartz_beacon.py)
# window before that sid's own packets finally stop. peer_age_s (AP_FMT
# slot 8, carried in every packet since 2026-08-09) is seconds since the
# broadcasting node's peer last sent it a RAW update — this rises well
# before pd_dev does, so it's checked independently, not as a replacement.
ALERT_AGE_S    = 1.5    # peer_age_s rising → treat as noisy even if pd_dev looks calm
WITHDRAW_AGE_S = 2.5    # peer_age_s nearing quartz_beacon's 3.0s cutoff → treat as ragged

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

# per-sid pd_dev EMA — 3 independent quartz witnesses, not one flapping scalar
sid_pd_ema  = {}   # sid -> ema
sid_last_t  = {}   # sid -> last packet time
sid_peer_age = {}  # sid -> latest peer_age_s reported by that sid's own beacon
SID_STALE_S = 3.0


def surprise_bits(p):
    """Self-information of a fraction: bits = -log2(p)."""
    if p <= 0.0:
        return float("inf")
    return -math.log2(p)


def emit():
    try:
        rs_out.sendto(struct.pack(RS_FMT, RS_MAGIC, state, pd_dev, temlum), rs_addr)
    except OSError:
        pass


def go(new):
    global state, alert_clear_since, recover_since
    if new == state:
        return
    now  = time.time()
    live = [sid for sid, t in sid_last_t.items() if (now - t) < SID_STALE_S]
    votes     = {sid: round(sid_pd_ema[sid], 4) for sid in live}
    worst_pd  = max((sid_pd_ema[sid] for sid in live), default=0.0)
    worst_age = max((sid_peer_age.get(sid, 0.0) for sid in live), default=0.0)
    n_noisy  = sum(1 for sid in live
                    if sid_pd_ema[sid] > ALERT_PD or sid_peer_age.get(sid, 0.0) > ALERT_AGE_S)
    p_vote   = n_noisy / len(live) if live else 1.0
    print(f"[reflex] {_NAME[state]} → {_NAME[new]}  votes={votes} "
          f"bits_vote={surprise_bits(p_vote):.3f} pd_ratio={worst_pd/ALERT_PD:.3f} "
          f"age_ratio={worst_age/WITHDRAW_AGE_S:.3f}", flush=True)
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
                    sid, pkt_pd_dev, pkt_peer_age = f[1], f[7], f[8]
                    prev = sid_pd_ema.get(sid, pkt_pd_dev)
                    sid_pd_ema[sid]   = 0.20 * pkt_pd_dev + 0.80 * prev
                    sid_last_t[sid]   = time.time()
                    sid_peer_age[sid] = pkt_peer_age
                    pd_dev    = pkt_pd_dev   # latest raw reading, for telemetry only
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

    live_sids = [sid for sid, t in sid_last_t.items() if (now - t) < SID_STALE_S]
    n_noisy   = sum(1 for sid in live_sids
                     if sid_pd_ema[sid] > ALERT_PD or sid_peer_age.get(sid, 0.0) > ALERT_AGE_S)
    n_ragged  = sum(1 for sid in live_sids
                     if sid_pd_ema[sid] > WITHDRAW_PD or sid_peer_age.get(sid, 0.0) > WITHDRAW_AGE_S)
    quorum    = max(2, (len(live_sids) // 2) + 1) if live_sids else 1

    # majority of witnesses, not whichever pairwise packet arrived last
    noisy  = n_noisy  >= quorum
    ragged = n_ragged >= quorum

    if state == CALM:
        if hot:                               go(PARK)
        elif withdrawal or ragged or stale:   go(WITHDRAW)
        elif warm or noisy:                   go(ALERT)

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
        if not hot and temlum_ema < RECOVER_TEMP: go(RECOVER)

    elif state == RECOVER:
        if hot:                               go(PARK)
        elif withdrawal or ragged or stale:   go(WITHDRAW)
        elif warm or noisy:                   go(ALERT)
        else:
            if recover_since is None:         recover_since = now
            elif now - recover_since >= RECOVER_HOLD_S: go(CALM)

    emit()
