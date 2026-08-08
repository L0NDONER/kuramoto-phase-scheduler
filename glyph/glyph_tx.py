#!/usr/bin/env python3
"""
glyph/glyph_tx.py — sends characters to reader_glyph for Morse encoding.

reader_glyph.c owns all timing; this just streams chars at 127.0.0.1:7408.
Packet: magic(H=0x474C) char(B)

Usage:
  python3 glyph/glyph_tx.py "hello world"
"""
import socket, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from quartz_core.phase_auth import gate_check

READER_IP  = "127.0.0.1"
GLYPH_PORT = 7408
MAGIC      = 0x474C

PRINTER_IP   = "10.0.0.82"
PRINTER_PORT = 9100

def transmit(text):
    print("[glyph] phase-auth gate...", flush=True)
    if not gate_check():
        print("[glyph] GATE BLOCKED — no LAN prover responded", flush=True)
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (READER_IP, GLYPH_PORT)
    for ch in text.upper():
        sock.sendto(struct.pack(">HB", MAGIC, ord(ch)), dst)
        time.sleep(0.05)
    sock.close()
    return True

def print_job(text):
    body = text.replace("\n", "\r\n") + "\r\n\f"
    sock = socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=5)
    sock.sendall(body.encode("ascii", errors="replace"))
    sock.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        text = "HELLO WORLD"
    print(f"TX → {text!r}", flush=True)
    if transmit(text):
        print("done", flush=True)
        print(f"[print] → {PRINTER_IP}:{PRINTER_PORT}", flush=True)
        print_job(text)
        print("[print] sent", flush=True)
