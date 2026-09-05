#!/usr/bin/env python3
"""
i0_nazare_verify.py -- receives pulse-coded tickets from multiple
senders (i0_nazare_sender.py) on separate ports, reconstructs each
one's literal text purely from pulse-arrival timing, and once all
expected senders complete, prints a combined ticket to the kitchen
printer (10.0.0.82:9100, raw/JetDirect socket -- same endpoint as the
2026-07-10 beacon-physical-invariant demo).

No cryptographic verification here -- the "proof" is structural: the
receiver never sees the sender's plaintext directly, only which tick
indices carried a pulse. Reconstructing readable text (not noise) IS
the demonstration that the sender was actually pacing against a live
tick it observed, one bit at a time, rather than replaying something
precomputed. This is a prototype (explicit user framing) -- same
"parked, not hardened" honesty as the original demo: no nonce/replay
defence, no auth on who's allowed to claim which sender slot. Treat
accordingly before this gates anything beyond a demo print.

Usage:
    python3 i0_nazare_verify.py --sender mint:7480 --sender pi1:7481 --sender pi2:7482 [--dry-run]
"""
import argparse
import selectors
import socket
import struct
import sys
import time

from i0_carrier import bits_to_bytes

NC_MAGIC = 0x4E43
NC_START, NC_PULSE, NC_END = 1, 2, 3
NC_START_FMT = ">HBHHQ"; NC_START_SIZE = struct.calcsize(NC_START_FMT)
NC_PULSE_FMT = ">HBH";   NC_PULSE_SIZE = struct.calcsize(NC_PULSE_FMT)
NC_END_FMT = ">HB";      NC_END_SIZE = struct.calcsize(NC_END_FMT)

PRINTER_HOST, PRINTER_PORT = "10.0.0.82", 9100
OVERALL_TIMEOUT_S = 120.0


class Session:
    def __init__(self, name):
        self.name = name
        self.bits = None
        self.n_bits = 0
        self.beat_ticks = None
        self.start_tick = None
        self.done = False
        self.text = None


def _bind(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.setblocking(False)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sender", action="append", required=True,
                     help="name:port, repeatable, one per expected sender (e.g. mint:7480)")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout instead of the real printer")
    args = ap.parse_args()

    sessions = {}
    port_to_name = {}
    sel = selectors.DefaultSelector()
    for spec in args.sender:
        name, port_s = spec.split(":")
        port = int(port_s)
        sessions[name] = Session(name)
        port_to_name[port] = name
        sock = _bind(port)
        sel.register(sock, selectors.EVENT_READ, data=name)
        print(f"[i0-nazare-verify] listening for '{name}' on :{port}", flush=True)

    deadline = time.monotonic() + OVERALL_TIMEOUT_S
    while time.monotonic() < deadline:
        if all(s.done for s in sessions.values()):
            break
        for key, _ in sel.select(timeout=1.0):
            name = key.data
            sess = sessions[name]
            data, _ = key.fileobj.recvfrom(64)

            if len(data) >= NC_START_SIZE:
                magic, typ, n_bits, beat_ticks, start_tick = struct.unpack_from(NC_START_FMT, data)
                if magic == NC_MAGIC and typ == NC_START:
                    sess.n_bits = n_bits
                    sess.beat_ticks = beat_ticks
                    sess.start_tick = start_tick
                    sess.bits = [0] * n_bits
                    print(f"[i0-nazare-verify] '{name}' START n_bits={n_bits} beat_ticks={beat_ticks}", flush=True)
                    continue

            if len(data) >= NC_PULSE_SIZE and sess.bits is not None:
                magic, typ, idx = struct.unpack_from(NC_PULSE_FMT, data)
                if magic == NC_MAGIC and typ == NC_PULSE and 0 <= idx < sess.n_bits:
                    sess.bits[idx] = 1
                    continue

            if len(data) >= NC_END_SIZE and sess.bits is not None:
                magic, typ = struct.unpack_from(NC_END_FMT, data)
                if magic == NC_MAGIC and typ == NC_END:
                    raw = bits_to_bytes(sess.bits)
                    try:
                        sess.text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        sess.text = f"<undecodable: {raw!r}>"
                    sess.done = True
                    print(f"[i0-nazare-verify] '{name}' END -> {sess.text!r}", flush=True)
                    continue
    else:
        missing = [n for n, s in sessions.items() if not s.done]
        sys.exit(f"[i0-nazare-verify] timed out waiting for: {missing}")

    lines = [f"{s.name}: {s.text}" for s in sessions.values()]
    ticket = "=== I(0) Triad Presence Ticket ===\n" + "\n".join(lines) + "\n"
    print("\n" + ticket)

    if args.dry_run:
        print("[i0-nazare-verify] --dry-run, not sending to printer")
        return

    print(f"[i0-nazare-verify] sending to printer {PRINTER_HOST}:{PRINTER_PORT}", flush=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5.0)
        s.connect((PRINTER_HOST, PRINTER_PORT))
        s.sendall(ticket.encode("utf-8") + b"\f")   # form-feed to eject the page
    print("[i0-nazare-verify] sent.", flush=True)


if __name__ == "__main__":
    main()
