#!/usr/bin/env python3
"""
glyph/glyph_rx.py — Morse decoder from real AxisPulse substrate.

Listens on 239.0.0.2:7404 (the real AxisPulse stream from reader.c).
When glyph_tx injects via port 7408, reader.c forces pd_dev=0.30 in
AxisPulse — this receiver classifies those excursions as DIT or DAH.

REST:   pd_dev < 0.05   (substrate at attractor)
ACTIVE: pd_dev >= 0.05  (injected perturbation)
DIT/DAH threshold: 12 ticks (midpoint between 8 and 24)

Usage: python3 glyph/glyph_rx.py
"""
import socket, struct, sys

AXIS_GRP   = "239.0.0.2"
AXIS_PORT  = 7404
AXIS_MAGIC = 0x4158
AXIS_FMT   = "!HBBIfffffHQ"   # 38 bytes
AXIS_SIZE  = struct.calcsize(AXIS_FMT)

REST_THRESH   = 0.15    # inject=0.30, lock noise<0.05 — 0.15 cleanly separates
DIT_MAX_TICKS = 16      # UNIT=8 ticks DIT, 24 ticks DAH; midpoint=16
REST_LETTER   = 16      # sym_gap=8 ticks, letter_gap=24 ticks; threshold=16
REST_WORD     = 40      # letter_gap=24, word_gap=56; threshold=40

MORSE_REV = {v: k for k, v in {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',
    'E': '.',     'F': '..-.',  'G': '--.',   'H': '....',
    'I': '..',    'J': '.---',  'K': '-.-',   'L': '.-..',
    'M': '--',    'N': '-.',    'O': '---',   'P': '.--.',
    'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',
    'Y': '-.--',  'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--',
    '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..',  '9': '----.',
    '.': '.-.-.-', ',': '--..--', '?': '..--..', '/': '-..-.',
}.items()}

def listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", AXIS_PORT))
    mreq = socket.inet_aton(AXIS_GRP) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)

    print(f"RX listening on AxisPulse {AXIS_GRP}:{AXIS_PORT}", flush=True)

    active_run  = 0
    rest_run    = 0
    letter_buf  = ""
    word_buf    = []
    output      = []
    prev_active = False

    def commit_symbol():
        nonlocal active_run, letter_buf
        if active_run == 0:
            return
        sym = '.' if active_run < DIT_MAX_TICKS else '-'
        letter_buf += sym
        print(f"  {'DIT' if sym == '.' else 'DAH'} ({active_run} ticks)", flush=True)
        active_run = 0

    def commit_letter():
        nonlocal letter_buf, word_buf
        if letter_buf:
            ch = MORSE_REV.get(letter_buf, f'?({letter_buf})')
            word_buf.append(ch)
            print(f"  → {ch}  ({letter_buf})", flush=True)
            letter_buf = ""

    def commit_word():
        nonlocal word_buf, output
        if word_buf:
            w = "".join(word_buf)
            output.append(w)
            print(f"  WORD: {w}", flush=True)
            word_buf = []

    def flush_output():
        nonlocal output
        if output:
            print(f"\nRX: {' '.join(output)}", flush=True)
            output = []

    while True:
        try:
            data, _ = sock.recvfrom(64)
        except TimeoutError:
            # end of transmission — flush whatever's buffered
            if active_run:
                commit_symbol()
            if letter_buf:
                commit_letter()
            if word_buf:
                commit_word()
            flush_output()
            continue

        if len(data) < AXIS_SIZE:
            continue
        fields = struct.unpack(AXIS_FMT, data[:AXIS_SIZE])
        magic, sid, locked, tick, theta1, theta2, pd, pd_dev, load, drains, t0 = fields
        if magic != AXIS_MAGIC:
            continue

        active = pd_dev >= REST_THRESH

        if active:
            if rest_run >= REST_WORD:
                commit_symbol(); commit_letter(); commit_word()
            elif rest_run >= REST_LETTER:
                commit_symbol(); commit_letter()
            elif rest_run > 0 and active_run > 0:
                commit_symbol()   # inter-symbol gap just ended
            rest_run = 0
            active_run += 1
        else:
            rest_run += 1
            # eagerly flush on letter/word gap so we don't wait for next ACTIVE
            if rest_run == REST_LETTER:
                commit_symbol()
                commit_letter()
            elif rest_run == REST_WORD:
                commit_word()

        prev_active = active

if __name__ == "__main__":
    listen()
