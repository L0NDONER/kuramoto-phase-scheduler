#!/usr/bin/env python3
# Mint — WAN shaper + final commit
import hashlib, math, selectors, socket, struct, sys, time

from quartz_core import phase_auth
import ns_wan_gain as nwg

RAW_GRP, RAW_PORT = "239.0.0.1", 7400

AP_CARRIER_GRP, AP_CARRIER_PORT = "239.0.0.2", 7404
AP_MAGIC = 0x4158
AP_FMT   = ">HBBIfffffHQ"
AP_SIZE  = struct.calcsize(AP_FMT)

AP_GRP, AP_PORT = "239.0.0.2", 7405
EVT_MAGIC = 0xE701
EVT_FMT   = "!HBd"
EVT_SIZE  = struct.calcsize(EVT_FMT)

NS_GRP, NS_PORT = "239.0.0.3", 7440
NS_MAGIC = 0x4E53
NS_FMT   = "!HfffBB"
NS_SIZE  = struct.calcsize(NS_FMT)

RS_GRP, RS_PORT = "239.0.0.4", 7450
RS_MAGIC = 0x5253
RS_FMT   = "!HBff"
RS_SIZE  = struct.calcsize(RS_FMT)
RS_PARK  = 3

THERMAL_MAX  = nwg.TEMLUM_SANITY_MAX  # raw-packet sanity bound, not the EMA-tuned PARK_TEMP
WAIT_EVENTS_S = 1500.0  # Pi1's own WAIT state runs ~1200s (20min); leave ~5min margin

PRINTER_HOST, PRINTER_PORT = "10.0.0.82", 9100


def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def drain_state_events(sel, rs_sock, ns_sock, ap_carrier_sock, timeout=2.0, verbose=False):
    """Accumulate ReflexState/NucleusState/AP-carrier within `timeout`s.
    Returns (reflex_state, temlum, e_C, theta1). verbose=True enables
    periodic "still waiting" logging (off by default — timeout/FAIL-CLOSED
    always logs regardless of verbose).

    FAIL-CLOSED: the original single sel.select(timeout=0.2) silently
    defaulted to reflex_state=0 (CALM) if RS/NS/carrier hadn't arrived
    yet — on this reflex gate, a late PARK signal getting replaced by
    CALM is fail-open. If all three signals aren't seen before timeout,
    reflex_state is forced to RS_PARK so REFLEX_CHECK drops WAN instead
    of proceeding on stale/default state.
    """
    start = time.monotonic()
    last_log = start
    log_interval = 0.5
    seen = {"rs": False, "ns": False, "carrier": False}
    reflex_state, temlum, e_C, theta1 = 0, 0.0, 0.0, 0.0

    while (time.monotonic() - start) < timeout and not all(seen.values()):
        remaining = timeout - (time.monotonic() - start)
        poll_timeout = max(0.01, min(0.2, remaining))
        for key, _ in sel.select(timeout=poll_timeout):
            if key.data == "rs":
                data, _ = rs_sock.recvfrom(64)
                if len(data) >= RS_SIZE:
                    f = struct.unpack_from(RS_FMT, data)
                    if f[0] == RS_MAGIC:
                        reflex_state = f[1]
                        seen["rs"] = True
            elif key.data == "ns":
                data, _ = ns_sock.recvfrom(64)
                if len(data) >= NS_SIZE:
                    f = struct.unpack_from(NS_FMT, data)
                    if f[0] == NS_MAGIC:
                        e_C, temlum = f[1], f[2]
                        seen["ns"] = True
            elif key.data == "carrier":
                data, _ = ap_carrier_sock.recvfrom(64)
                if len(data) >= AP_SIZE:
                    f = struct.unpack_from(AP_FMT, data)
                    if f[0] == AP_MAGIC and f[2]:
                        theta1 = f[4]
                        seen["carrier"] = True

        now = time.monotonic()
        if verbose and not all(seen.values()) and (now - last_log) >= log_interval:
            missing = [k for k, v in seen.items() if not v]
            print(f"[mint] STATE_DRAIN waiting for {missing} at {now - start:.2f}s",
                  flush=True)
            last_log = now

    if all(seen.values()):
        if verbose:
            print(f"[mint] STATE_DRAIN all signals acquired in "
                  f"{time.monotonic() - start:.3f}s", flush=True)
        return reflex_state, temlum, e_C, theta1

    missing = [k for k, v in seen.items() if not v]
    print(f"[mint] STATE_DRAIN TIMEOUT after {timeout:.2f}s — missing: {missing}",
          flush=True)
    print("[mint] STATE_DRAIN FAILING CLOSED: reflex_state=RS_PARK", flush=True)
    return RS_PARK, temlum, e_C, theta1


def print_job(lines):
    payload = "TIME\n" + "\n".join(lines) + "\n\f"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((PRINTER_HOST, PRINTER_PORT))
    s.sendall(payload.encode())
    s.close()


def shape_wan(theta1, temlum, e_C):
    nwg.setup_qdisc()
    carrier   = (1.0 - math.cos(theta1)) / 2.0
    mod_depth = min(1.0, max(0.0, e_C) / nwg.EC_MOD_SCALE)
    t         = max(0.0, min(1.0, temlum / nwg.HEADROOM))
    thermal   = 1.0 - t * (1.0 - nwg.G_MIN)
    ec_bias   = max(-0.15, min(0.15, e_C / nwg.EC_SCALE * 0.15))
    G = max(0.0, min(1.0, thermal * (nwg.G_BASE + (1.0 - nwg.G_BASE) * mod_depth * carrier) + ec_bias))
    rate_mbit = nwg.RATE_MIN + round((nwg.RATE_MAX - nwg.RATE_MIN) * G)
    nwg.set_rate(rate_mbit)
    return rate_mbit


def main():
    raw_sock = _mcast_in(RAW_GRP, RAW_PORT)   # noqa: F841 — subscribed per spec
    ap_carrier_sock = _mcast_in(AP_CARRIER_GRP, AP_CARRIER_PORT)
    ap_sock = _mcast_in(AP_GRP, AP_PORT)
    ns_sock = _mcast_in(NS_GRP, NS_PORT)
    rs_sock = _mcast_in(RS_GRP, RS_PORT)

    sel = selectors.DefaultSelector()
    sel.register(ap_sock, selectors.EVENT_READ, data="evt")
    sel.register(ap_carrier_sock, selectors.EVENT_READ, data="carrier")
    sel.register(ns_sock, selectors.EVENT_READ, data="ns")
    sel.register(rs_sock, selectors.EVENT_READ, data="rs")

    # STATE WAIT_EVENTS
    deadline = time.time() + WAIT_EVENTS_S
    pi1_time = None
    pi2_time = None
    while pi1_time is None or pi2_time is None:
        remaining = deadline - time.time()
        if remaining <= 0:
            print("[mint] WAIT_EVENTS TIMEOUT — "
                  f"pi1={'ok' if pi1_time else 'missing'} "
                  f"pi2={'ok' if pi2_time else 'missing'}", flush=True)
            sys.exit(1)
        for key, _ in sel.select(timeout=min(remaining, 1.0)):
            if key.data != "evt":
                continue
            data, _ = ap_sock.recvfrom(64)
            if len(data) < EVT_SIZE:
                continue
            f = struct.unpack_from(EVT_FMT, data)
            if f[0] != EVT_MAGIC:
                continue
            if f[1] == 1 and pi1_time is None:
                pi1_time = f[2]
            elif f[1] == 2 and pi2_time is None:
                pi2_time = f[2]

    print(f"[mint] WAIT_EVENTS observed pi1={pi1_time} pi2={pi2_time}", flush=True)

    # drain latest ReflexState / NucleusState / carrier theta seen so far
    reflex_state, temlum, e_C, theta1 = drain_state_events(
        sel, rs_sock, ns_sock, ap_carrier_sock)

    # STATE REFLEX_CHECK
    if reflex_state == RS_PARK:
        nwg.setup_qdisc()
        nwg.set_rate(nwg.RATE_MIN)
        print("[mint] REFLEX_CHECK FAIL — ReflexState == RS_PARK, WAN dropped", flush=True)
        sys.exit(1)

    # STATE THERMAL_CHECK
    if abs(temlum) > THERMAL_MAX:
        print(f"[mint] THERMAL_CHECK FAIL — temlum={temlum} outside safe bounds "
              f"(±{THERMAL_MAX})", flush=True)
        sys.exit(1)

    # STATE GATE
    if not phase_auth.gate_check():
        print("[mint] GATE FAIL — phase_auth.gate_check() returned False", flush=True)
        sys.exit(1)

    # STATE SHAPE_WAN
    rate_mbit = shape_wan(theta1, temlum, e_C)
    print(f"[mint] SHAPE_WAN theta1={theta1:.3f} temlum={temlum:+.3f} "
          f"e_C={e_C:+.5f} → {rate_mbit}Mbit", flush=True)

    # STATE COMMIT
    mint_time = time.time()
    digest = hashlib.sha256(f"{pi1_time}|{pi2_time}|{mint_time}".encode()).hexdigest()
    print_job(["job finished", f"hash={digest}"])
    print(f"[mint] COMMIT hash={digest}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
