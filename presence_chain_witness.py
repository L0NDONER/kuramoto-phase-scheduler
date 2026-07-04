#!/usr/bin/env python3
"""
presence_chain_witness.py — independent witness for presence_chain.py.

Listens to the ChainLink multicast broadcast (239.0.0.5:7460) and logs,
for every tick seen, the local wall-clock time it actually arrived on the
wire — independent of anything presence_chain.py later writes to its own
checkpoint file.

presence_chain.py's --verify mode used to trust the checkpoint file's own
self-reported iso timestamps as the yardstick for judging whether the
file's timing was plausible. A forger holding the seed can compute the
whole HMAC chain offline in an instant, then invent iso timestamps that
are perfectly self-consistent — that check has no ground truth outside the
file it's judging. This witness log is the ground truth: a second,
independently-running process the checkpoint producer never talks to and
can't retroactively edit.

Run this continuously (ideally on a different machine than whatever writes
checkpoints), then:

    presence_chain.py --verify --witness ~/.presence_witness_log
"""
import socket, struct, json, os, time

CL_GRP, CL_PORT = "239.0.0.5", 7460
CL_FMT = ">HI16sffff"
CL_MAGIC = 0x434C

WITNESS_PATH = os.path.expanduser("~/.presence_witness_log")


def mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    return s


def main():
    sock = mcast_in(CL_GRP, CL_PORT)
    print(f"[witness] watching ChainLink {CL_GRP}:{CL_PORT} -> {WITNESS_PATH}", flush=True)

    n = 0
    with open(WITNESS_PATH, "a") as f:
        while True:
            data, _ = sock.recvfrom(64)
            if len(data) < struct.calcsize(CL_FMT):
                continue
            fields = struct.unpack_from(CL_FMT, data)
            magic, tick, commit16 = fields[0], fields[1], fields[2]
            if magic != CL_MAGIC:
                continue
            entry = {
                "tick": tick,
                "commit_prefix": commit16.hex(),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            f.write(json.dumps(entry) + "\n")
            f.flush()
            n += 1
            if n % 100 == 0:
                print(f"[witness] logged {n} ticks", flush=True)


if __name__ == "__main__":
    main()
