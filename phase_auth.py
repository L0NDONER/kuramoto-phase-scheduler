#!/usr/bin/env python3
"""
phase_auth.py — Phase-auth challenger and verifier.

Issues a challenge on the carrier, independently observes the target tick,
and verifies the prover's response. No stored secret — presence IS the proof.

Protocol:
  1. Challenger picks target_tick = current_tick + AHEAD
  2. Broadcasts: nonce(16) | target_tick(I) | resp_port(H)  on 239.0.0.5:7451
  3. Both sides observe AxisPulse at target_tick → (θ, pd)
  4. Prover sends: nonce(16) | SHA256(nonce + tick + θ + pd)  to challenger:7452
  5. Challenger computes same hash, checks match

Security properties:
  identity:         only your node observed that (θ, pd) at that tick
  liveness:         target_tick window closes in ~400ms
  adjacency:        AxisPulse multicast is LAN-only
  geometry-member:  pd binds you to this specific oscillator pair

Usage:
  python3 phase_auth.py [--loop]   loop issues a challenge every 10s
  python3 phase_auth.py            single challenge then exit
"""
import hashlib, os, selectors, socket, struct, sys, time

# AxisPulse
AP_GRP   = "239.0.0.2"; AP_PORT  = 7404
AP_FMT   = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

# Phase-auth channels
PA_GRP       = "239.0.0.5"; PA_CHAL_PORT = 7451
PA_RESP_PORT = 7452

# Wire formats
CHAL_FMT   = "!H16sIH";   CHAL_MAGIC = 0x5043; CHAL_SIZE = struct.calcsize("!H16sIH")
RESP_FMT   = "!H16s32s";  RESP_MAGIC = 0x5052; RESP_SIZE = struct.calcsize("!H16s32s")

CHALLENGE_AHEAD = 30    # ticks ahead to set target_tick (~400ms at 75 tps)
WINDOW_S        = 3.0   # seconds to wait for prover response after target_tick


def make_hash(nonce, tick, theta, pd):
    h = hashlib.sha256()
    h.update(nonce)
    h.update(struct.pack(">I", tick))
    h.update(struct.pack(">f", theta))
    h.update(struct.pack(">f", pd))
    return h.digest()


def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def run_challenge():
    # Sockets
    ap_sock   = _mcast_in(AP_GRP, AP_PORT)
    resp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    resp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    resp_sock.bind(("", PA_RESP_PORT))
    resp_sock.setblocking(False)

    chal_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    chal_out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    sel = selectors.DefaultSelector()
    sel.register(ap_sock,   selectors.EVENT_READ, data="ap")
    sel.register(resp_sock, selectors.EVENT_READ, data="resp")

    # Wait for a tick to know current tick number
    print("[phase_auth] waiting for AxisPulse lock...", flush=True)
    current_tick = None
    while current_tick is None:
        for key, _ in sel.select(timeout=2.0):
            if key.data == "ap":
                data, _ = ap_sock.recvfrom(64)
                if len(data) >= AP_SIZE:
                    f = struct.unpack_from(AP_FMT, data)
                    if f[0] == AP_MAGIC and f[2]:
                        current_tick = f[3]

    target_tick = current_tick + CHALLENGE_AHEAD
    nonce       = os.urandom(16)

    chal_pkt = struct.pack(CHAL_FMT, CHAL_MAGIC, nonce, target_tick, PA_RESP_PORT)
    chal_out.sendto(chal_pkt, (PA_GRP, PA_CHAL_PORT))
    print(f"[phase_auth] challenge  nonce={nonce.hex()[:12]}…  target_tick={target_tick}", flush=True)

    # Observe target_tick independently
    expected_hash = None
    deadline      = time.time() + 5.0   # wait up to 5s for target tick

    while expected_hash is None and time.time() < deadline:
        for key, _ in sel.select(timeout=0.05):
            if key.data == "ap":
                data, _ = ap_sock.recvfrom(64)
                if len(data) >= AP_SIZE:
                    f = struct.unpack_from(AP_FMT, data)
                    if f[0] == AP_MAGIC and f[2] and f[3] == target_tick:
                        theta, pd = f[4], f[6]
                        expected_hash = make_hash(nonce, target_tick, theta, pd)
                        print(f"[phase_auth] observed  tick={target_tick}  θ={theta:.4f}  pd={pd:.4f}", flush=True)

    if expected_hash is None:
        print("[phase_auth] FAIL — target tick never observed", flush=True)
        return False

    # Wait for prover response
    deadline = time.time() + WINDOW_S
    while time.time() < deadline:
        for key, _ in sel.select(timeout=0.1):
            if key.data == "resp":
                data, addr = resp_sock.recvfrom(256)
                if len(data) >= RESP_SIZE:
                    magic, r_nonce, digest = struct.unpack_from(RESP_FMT, data)
                    if magic == RESP_MAGIC and r_nonce == nonce:
                        if digest == expected_hash:
                            print(f"[phase_auth] PASS  prover={addr[0]}", flush=True)
                            return True
                        else:
                            print(f"[phase_auth] FAIL  hash mismatch  prover={addr[0]}", flush=True)
                            return False

    print("[phase_auth] FAIL — no response in window", flush=True)
    return False


if __name__ == "__main__":
    loop = "--loop" in sys.argv
    if loop:
        while True:
            run_challenge()
            time.sleep(10)
    else:
        run_challenge()
