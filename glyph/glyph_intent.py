#!/usr/bin/env python3
"""
glyph/glyph_intent.py — inject intent signals into the Kuramoto substrate.

reader_glyph translates each intent into a phase perturbation on Pi1:
  ADVISORY  (0) — 1 unit  (~100ms)  soft bias toward PARK
  DIRECTIVE (1) — 3 units (~300ms)  strong PARK push
  ALARM     (2) — 9 units (~900ms)  sustained phase disruption

Neurons respond to the real pd_dev excursion — no decoding needed.

Usage:
  python3 glyph/glyph_intent.py advisory
  python3 glyph/glyph_intent.py directive
  python3 glyph/glyph_intent.py alarm
  python3 glyph/glyph_intent.py boost
  python3 glyph/glyph_intent.py boost2
"""
import socket, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from quartz_core.phase_auth import gate_check

READER_IP  = "127.0.0.1"
GLYPH_PORT = 7408
MAGIC      = 0x474C

PRINTER_IP   = "10.0.0.82"
PRINTER_PORT = 9100

ADVISORY  = 0
DIRECTIVE = 1
ALARM     = 2
BOOST     = 3
BOOST2    = 4

NAMES = {"advisory": ADVISORY, "directive": DIRECTIVE, "alarm": ALARM, "boost": BOOST, "boost2": BOOST2}

def send(intent: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(struct.pack(">HB", MAGIC, intent), (READER_IP, GLYPH_PORT))
    sock.close()

def print_alarm():
    body = f"ALARM\r\n{time.ctime()}\r\n\f"
    sock = socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=5)
    sock.sendall(body.encode("ascii"))
    sock.close()

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].lower() not in NAMES:
        print(f"usage: {sys.argv[0]} advisory|directive|alarm|boost|boost2")
        sys.exit(1)
    name = sys.argv[1].lower()
    intent = NAMES[name]

    if intent == ALARM:
        print("[glyph] phase-auth gate...", flush=True)
        if not gate_check():
            print("[glyph] GATE BLOCKED — no LAN prover responded", flush=True)
            sys.exit(1)

    send(intent)
    print(f"intent: {name}", flush=True)

    if intent == ALARM:
        print(f"[print] → {PRINTER_IP}:{PRINTER_PORT}", flush=True)
        print_alarm()
        print("[print] sent", flush=True)
