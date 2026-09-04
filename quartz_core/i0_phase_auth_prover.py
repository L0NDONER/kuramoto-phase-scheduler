#!/usr/bin/env python3
"""
i0_phase_auth_prover.py -- prover daemon for i0_phase_auth.py. Same
role as phase_auth_prover.py, ported from AxisPulse to I(0)
(239.0.0.6:7500, tick/theta/pd, no sid -- single shared stream, unlike
AxisPulse's multiple sid'd oscillators, so the (sid, tick) keying
phase_auth_prover.py needed collapses to just tick here).

Listens for challenges on 239.0.0.5:7451 (same channel, unchanged),
observes the target I(0) tick, and responds with
SHA256(nonce + tick + theta + pd) to the challenger. No secret --
receiving I(0) at all (multicast, or a WireGuard-relayed unicast copy
of it, e.g. via i0_relay.py) is the credential.

Run: python3 i0_phase_auth_prover.py
"""
import collections, hashlib, selectors, socket, struct, time

I0_GRP, I0_PORT = "239.0.0.6", 7500
I0_MAGIC = 0x4C30
I0_FMT = "!HIffd"   # magic, tick, theta, pd, t0
I0_SIZE = struct.calcsize(I0_FMT)

PA_GRP, PA_CHAL_PORT = "239.0.0.5", 7451

CHAL_FMT = "!H16sIH"; CHAL_MAGIC = 0x5043; CHAL_SIZE = struct.calcsize(CHAL_FMT)
RESP_FMT = "!H16s32s"; RESP_MAGIC = 0x5052

TICK_BUF = 200


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
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    except OSError:
        pass   # EC2 has no LAN multicast segment -- fine, we only need
               # this socket to receive the relayed UNICAST copies
    s.setblocking(False)
    return s


def main():
    i0_sock   = _mcast_in(I0_GRP, I0_PORT)
    chal_sock = _mcast_in(PA_GRP, PA_CHAL_PORT)
    resp_out  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sel = selectors.DefaultSelector()
    sel.register(i0_sock,   selectors.EVENT_READ, data="i0")
    sel.register(chal_sock, selectors.EVENT_READ, data="chal")

    tick_buf = collections.deque(maxlen=TICK_BUF)
    # target_tick -> list of (nonce, chal_ip, resp_port, expires). A list,
    # not a single tuple -- same reasoning as phase_auth_prover.py: two
    # concurrent challengers can legitimately target the same tick.
    pending = {}

    print(f"[i0-prover] I0:{I0_PORT}  chal:{PA_CHAL_PORT}  buf={TICK_BUF}", flush=True)

    while True:
        for key, _ in sel.select(timeout=0.05):
            tag = key.data

            if tag == "i0":
                data, _ = i0_sock.recvfrom(64)
                if len(data) < I0_SIZE:
                    continue
                magic, tick, theta, pd, t0 = struct.unpack_from(I0_FMT, data)
                if magic != I0_MAGIC:
                    continue
                tick_buf.append((tick, theta, pd))

                if tick in pending:
                    now = time.time()
                    for nonce, chal_ip, resp_port, expires in pending.pop(tick):
                        if now < expires:
                            digest = make_hash(nonce, tick, theta, pd)
                            resp_pkt = struct.pack(RESP_FMT, RESP_MAGIC, nonce, digest)
                            resp_out.sendto(resp_pkt, (chal_ip, resp_port))
                            print(f"[i0-prover] responded  tick={tick}  theta={theta:.4f}"
                                  f"  pd={pd:+.4f}  -> {chal_ip}:{resp_port}", flush=True)
                        else:
                            print(f"[i0-prover] expired  tick={tick}  -> {chal_ip}:{resp_port}", flush=True)

            elif tag == "chal":
                data, addr = chal_sock.recvfrom(64)
                if len(data) < CHAL_SIZE:
                    continue
                magic, nonce, target_tick, resp_port = struct.unpack_from(CHAL_FMT, data)
                if magic != CHAL_MAGIC:
                    continue
                chal_ip = addr[0]
                print(f"[i0-prover] challenge  nonce={nonce.hex()[:12]}..."
                      f"  target={target_tick}  from={chal_ip}", flush=True)

                for t, theta, pd in tick_buf:
                    if t == target_tick:
                        digest = make_hash(nonce, target_tick, theta, pd)
                        resp_pkt = struct.pack(RESP_FMT, RESP_MAGIC, nonce, digest)
                        resp_out.sendto(resp_pkt, (chal_ip, resp_port))
                        print(f"[i0-prover] responded (buffered)  tick={target_tick}"
                              f"  theta={theta:.4f}  pd={pd:+.4f}  -> {chal_ip}:{resp_port}", flush=True)
                        break
                else:
                    pending.setdefault(target_tick, []).append(
                        (nonce, chal_ip, resp_port, time.time() + 5.0))

        now = time.time()
        pending = {k: [w for w in v if w[3] > now] for k, v in pending.items()}
        pending = {k: v for k, v in pending.items() if v}


if __name__ == "__main__":
    main()
