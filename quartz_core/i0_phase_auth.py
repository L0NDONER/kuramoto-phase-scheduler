#!/usr/bin/env python3
"""
i0_phase_auth.py -- I(0)-based replacement for phase_auth.py.

Same protocol shape, same security reasoning, no stored secret --
presence IS the proof. Swapped from quartz_beacon's AxisPulse wire
(239.0.0.2:7404, now dead -- quartz-beacon.service was retired
2026-09-03) onto layer0_daemon.py's I(0) substrate (239.0.0.6:7500,
tick/theta/pd, free-running, 20Hz).

Protocol (unchanged in shape from phase_auth.py):
  1. Challenger observes I(0), waits for it to be FRESH (new here --
     the old system had no equivalent freshness check; refusing to
     issue a challenge against a stale/dead substrate is strictly
     safer than the old behavior of trusting whatever tick it last saw).
  2. Picks target_tick = current_tick + CHALLENGE_AHEAD.
  3. Broadcasts: nonce(16) | target_tick(I) | resp_port(H) on
     239.0.0.5:7451 (same phase-auth channel, unchanged -- no reason
     to move it, it was never part of AxisPulse).
  4. Both sides independently observe I(0) at target_tick -> (theta, pd).
  5. Prover sends: nonce(16) | SHA256(nonce + tick + theta + pd)
     to challenger:resp_port.
  6. Challenger computes the same hash, checks match.

One real difference from AxisPulse worth being explicit about, not
silently carrying over: I(0)'s theta is a deterministic free-running
counter (tick * theta_step mod 2*pi, see layer0_daemon.py) -- anyone
who knows the daemon's public parameters can COMPUTE theta at a given
tick without observing anything live. This does NOT weaken the actual
security model, because theta/pd was never the secret ingredient here
-- re-reading phase_auth.py's own documented properties, the
load-bearing ones are:
  - the nonce: fresh random bytes, unknown to anyone until the
    challenge is broadcast -- this is the actual unpredictable secret
  - multicast LAN-locality: routers don't forward multicast off-LAN,
    so an off-LAN attacker can't receive the challenge (or the nonce)
    at all, regardless of whether theta is computable
  - the tight response window: proves REAL-TIME presence, not just
    knowledge of a formula
theta/pd's real job is proving "you decoded a real Layer0 packet of
the right shape at the right tick" -- a liveness/format sanity check,
not a cryptographic secret. Swapping the phase source doesn't change
which properties are actually doing the security work.

Security properties (restated for I(0)):
  identity:    only a LAN-adjacent listener could have received the
               nonce at all (multicast never leaves the LAN)
  liveness:    target_tick window closes in ~300ms (6 ticks @ 20Hz)
  adjacency:   I(0) multicast is LAN-only, same as AxisPulse was
  freshness:   challenger refuses to issue a challenge if I(0) itself
               is stale -- new, stronger than the old system had

Usage:
  python3 i0_phase_auth.py [--loop]   loop issues a challenge every 10s
  python3 i0_phase_auth.py            single challenge then exit
"""
import hashlib, ipaddress, os, selectors, socket, struct, sys, time

# LAN (real physical adjacency) + WireGuard mesh (EC2, via i0_relay.py).
# Widened 2026-09-03 for the Mint<->EC2 validation test -- whether a WAN
# prover reached over WireGuard should be trusted the SAME as a LAN one
# is a real, separate design question (WireGuard adjacency isn't the
# same security property as LAN multicast adjacency), not resolved
# here, just unblocked for tonight's "does I(0) even work" test.
PROVER_SUBNET = [ipaddress.ip_network("10.0.0.0/24"), ipaddress.ip_network("10.8.0.0/24")]

# I(0) -- layer0_daemon.py's real wire, unchanged from that file
I0_GRP, I0_PORT = "239.0.0.6", 7500
I0_MAGIC = 0x4C30
I0_FMT = "!HIffd"   # magic, tick, theta, pd, t0
I0_SIZE = struct.calcsize(I0_FMT)
I0_TTL_S = 5.0       # same freshness TTL every other N-domain in this project uses

# Phase-auth channels -- unchanged from phase_auth.py, never part of AxisPulse
PA_GRP, PA_CHAL_PORT = "239.0.0.5", 7451

CHAL_FMT = "!H16sIH"; CHAL_MAGIC = 0x5043; CHAL_SIZE = struct.calcsize(CHAL_FMT)
RESP_FMT = "!H16s32s"; RESP_MAGIC = 0x5052; RESP_SIZE = struct.calcsize(RESP_FMT)

CHALLENGE_AHEAD = 6     # ticks ahead ~= 300ms @ 20Hz, matches old system's timing
WINDOW_S        = 3.0
FRESH_TIMEOUT_S = 10.0  # seconds to wait for I(0) to be observed fresh before failing


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


def run_challenge(verbose=True):
    def _p(msg):
        if verbose:
            print(msg, flush=True)

    i0_sock   = _mcast_in(I0_GRP, I0_PORT)
    resp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Ephemeral port, not fixed -- same reasoning as phase_auth.py: a
    # shared fixed port with only SO_REUSEADDR silently starves one of
    # two concurrent challengers (found live 2026-08-09 against the old
    # system, applies identically here).
    resp_sock.bind(("", 0))
    resp_port = resp_sock.getsockname()[1]
    resp_sock.setblocking(False)

    chal_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    chal_out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    sel = selectors.DefaultSelector()
    sel.register(i0_sock,   selectors.EVENT_READ, data="i0")
    sel.register(resp_sock, selectors.EVENT_READ, data="resp")

    # Wait for a FRESH I(0) packet -- new relative to phase_auth.py,
    # refuses to challenge against a dead/stale substrate rather than
    # trusting whatever tick it last happened to see.
    _p("[i0_phase_auth] waiting for fresh I(0)...")
    current_tick = None
    last_seen = 0.0
    lock_deadline = time.time() + FRESH_TIMEOUT_S
    while current_tick is None and time.time() < lock_deadline:
        for key, _ in sel.select(timeout=2.0):
            if key.data == "i0":
                data, _ = i0_sock.recvfrom(64)
                if len(data) < I0_SIZE:
                    continue
                magic, tick, theta, pd, t0 = struct.unpack_from(I0_FMT, data)
                if magic != I0_MAGIC:
                    continue
                current_tick = tick
                last_seen = time.time()

    if current_tick is None or (time.time() - last_seen) > I0_TTL_S:
        _p(f"[i0_phase_auth] FAIL -- I(0) not fresh within {FRESH_TIMEOUT_S:.0f}s")
        return False

    target_tick = current_tick + CHALLENGE_AHEAD
    nonce       = os.urandom(16)

    chal_pkt = struct.pack(CHAL_FMT, CHAL_MAGIC, nonce, target_tick, resp_port)
    chal_out.sendto(chal_pkt, (PA_GRP, PA_CHAL_PORT))
    _p(f"[i0_phase_auth] challenge  nonce={nonce.hex()[:12]}...  target_tick={target_tick}")

    expected_hash = None
    deadline = time.time() + 5.0

    while expected_hash is None and time.time() < deadline:
        for key, _ in sel.select(timeout=0.05):
            if key.data == "i0":
                data, _ = i0_sock.recvfrom(64)
                if len(data) < I0_SIZE:
                    continue
                magic, tick, theta, pd, t0 = struct.unpack_from(I0_FMT, data)
                if magic == I0_MAGIC and tick == target_tick:
                    expected_hash = make_hash(nonce, target_tick, theta, pd)
                    _p(f"[i0_phase_auth] observed  tick={target_tick}  theta={theta:.4f}  pd={pd:+.4f}")

    if expected_hash is None:
        _p("[i0_phase_auth] FAIL -- target tick never observed")
        return False

    deadline = time.time() + WINDOW_S
    while time.time() < deadline:
        for key, _ in sel.select(timeout=0.1):
            if key.data == "resp":
                data, addr = resp_sock.recvfrom(256)
                if len(data) >= RESP_SIZE:
                    prover_ip = ipaddress.ip_address(addr[0])
                    if not any(prover_ip in net for net in PROVER_SUBNET):
                        _p(f"[i0_phase_auth] IGNORED  prover={addr[0]} outside {PROVER_SUBNET}")
                        continue
                    magic, r_nonce, digest = struct.unpack_from(RESP_FMT, data)
                    if magic == RESP_MAGIC and r_nonce == nonce:
                        if digest == expected_hash:
                            _p(f"[i0_phase_auth] PASS  prover={addr[0]}")
                            return True
                        else:
                            _p(f"[i0_phase_auth] REJECTED  hash mismatch  prover={addr[0]}  (still waiting)")

    _p("[i0_phase_auth] FAIL -- no correct response in window")
    return False


def gate_check(verbose=True) -> bool:
    """Importable gate: returns True if a LAN prover responds correctly."""
    return run_challenge(verbose=verbose)


if __name__ == "__main__":
    loop = "--loop" in sys.argv
    if loop:
        while True:
            run_challenge()
            time.sleep(10)
    else:
        run_challenge()
