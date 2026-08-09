#!/usr/bin/env python3
"""
phase_auth_prover.py — Phase-auth prover daemon.

Listens for challenges on 239.0.0.5:7451, observes the target AxisPulse tick,
and responds with SHA256(nonce + tick + θ + pd) to the challenger.

Only a node subscribed to the real AxisPulse multicast can answer correctly.
No secret. Geometry is the credential.

Run: python3 phase_auth_prover.py
"""
import collections, hashlib, selectors, socket, struct, time

AP_GRP   = "239.0.0.2"; AP_PORT  = 7404
AP_FMT   = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

PA_GRP       = "239.0.0.5"; PA_CHAL_PORT = 7451

CHAL_FMT   = "!H16sIHB";  CHAL_MAGIC = 0x5043; CHAL_SIZE = struct.calcsize("!H16sIHB")
RESP_FMT   = "!H16s32s";  RESP_MAGIC = 0x5052

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
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s


def main():
    ap_sock   = _mcast_in(AP_GRP, AP_PORT)
    chal_sock = _mcast_in(PA_GRP, PA_CHAL_PORT)
    resp_out  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sel = selectors.DefaultSelector()
    sel.register(ap_sock,   selectors.EVENT_READ, data="ap")
    sel.register(chal_sock, selectors.EVENT_READ, data="chal")

    tick_buf = collections.deque(maxlen=TICK_BUF)
    # (sid, target_tick) -> list of (nonce, chal_ip, resp_port, expires).
    # A list, not a single tuple: two concurrent challengers can legitimately
    # target the same (sid, tick), and a single-slot dict would let the
    # second registration silently overwrite the first, starving one
    # challenger of any response at all (found live 2026-08-09).
    pending  = {}

    print(f"[prover] AP:{AP_PORT}  chal:{PA_CHAL_PORT}  buf={TICK_BUF}", flush=True)

    while True:
        for key, _ in sel.select(timeout=0.05):
            tag = key.data

            if tag == "ap":
                data, _ = ap_sock.recvfrom(64)
                if len(data) < AP_SIZE:
                    continue
                f = struct.unpack_from(AP_FMT, data)
                if f[0] != AP_MAGIC or not f[2]:
                    continue
                sid, tick, theta, pd = f[1], f[3], f[4], f[6]
                tick_buf.append((sid, tick, theta, pd))

                pkey = (sid, tick)
                if pkey in pending:
                    now = time.time()
                    for nonce, chal_ip, resp_port, expires in pending.pop(pkey):
                        if now < expires:
                            digest   = make_hash(nonce, tick, theta, pd)
                            resp_pkt = struct.pack(RESP_FMT, RESP_MAGIC, nonce, digest)
                            resp_out.sendto(resp_pkt, (chal_ip, resp_port))
                            print(f"[prover] responded  sid={sid}  tick={tick}  θ={theta:.4f}  pd={pd:.4f}"
                                  f"  → {chal_ip}:{resp_port}", flush=True)
                        else:
                            print(f"[prover] expired  sid={sid}  tick={tick}  → {chal_ip}:{resp_port}", flush=True)

            elif tag == "chal":
                data, addr = chal_sock.recvfrom(64)
                if len(data) < CHAL_SIZE:
                    continue
                magic, nonce, target_tick, resp_port, req_sid = struct.unpack_from(CHAL_FMT, data)
                if magic != CHAL_MAGIC:
                    continue
                chal_ip = addr[0]
                print(f"[prover] challenge  nonce={nonce.hex()[:12]}…"
                      f"  sid={req_sid}  target={target_tick}  from={chal_ip}", flush=True)

                for s, t, theta, pd in tick_buf:
                    if s == req_sid and t == target_tick:
                        digest   = make_hash(nonce, target_tick, theta, pd)
                        resp_pkt = struct.pack(RESP_FMT, RESP_MAGIC, nonce, digest)
                        resp_out.sendto(resp_pkt, (chal_ip, resp_port))
                        print(f"[prover] responded (buffered)  sid={req_sid}  tick={target_tick}"
                              f"  θ={theta:.4f}  pd={pd:.4f}  → {chal_ip}:{resp_port}", flush=True)
                        break
                else:
                    pending.setdefault((req_sid, target_tick), []).append(
                        (nonce, chal_ip, resp_port, time.time() + 5.0))

        now = time.time()
        pending = {k: [w for w in v if w[3] > now] for k, v in pending.items()}
        pending = {k: v for k, v in pending.items() if v}


if __name__ == "__main__":
    main()
