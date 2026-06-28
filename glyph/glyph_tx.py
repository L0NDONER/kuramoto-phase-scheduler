#!/usr/bin/env python3
"""
glyph/glyph_tx.py — sends characters to reader_glyph for Morse encoding.

reader_glyph.c owns all timing; this just streams chars at 127.0.0.1:7408.
Packet: magic(H=0x474C) char(B)

Usage:
  python3 glyph/glyph_tx.py "hello world"
"""
import socket, struct, time, sys

READER_IP  = "127.0.0.1"
GLYPH_PORT = 7408
MAGIC      = 0x474C

def transmit(text):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (READER_IP, GLYPH_PORT)
    for ch in text.upper():
        sock.sendto(struct.pack(">HB", MAGIC, ord(ch)), dst)
        time.sleep(0.05)
    sock.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        text = "HELLO WORLD"
    print(f"TX → {text!r}", flush=True)
    transmit(text)
    print("done", flush=True)
