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
"""
import socket, struct, sys

READER_IP  = "127.0.0.1"
GLYPH_PORT = 7408
MAGIC      = 0x474C

ADVISORY  = 0
DIRECTIVE = 1
ALARM     = 2

NAMES = {"advisory": ADVISORY, "directive": DIRECTIVE, "alarm": ALARM}

def send(intent: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(struct.pack(">HB", MAGIC, intent), (READER_IP, GLYPH_PORT))
    sock.close()

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].lower() not in NAMES:
        print(f"usage: {sys.argv[0]} advisory|directive|alarm")
        sys.exit(1)
    intent = NAMES[sys.argv[1].lower()]
    send(intent)
    print(f"intent: {sys.argv[1].lower()}", flush=True)
