#!/usr/bin/env python3
"""
glyph/glyph_rx.py — Morse decoder from real Kuramoto phase excursions.

Reads beacon multicast 239.0.0.1:7400 directly.
During ACTIVE: reader_glyph injects a spoofed Pi1 beacon with θ1+DELTA,
perturbing Pi2's coupling. The injected sid=1 packets shift pd_dev from
~0 (locked) to ~DELTA (0.5 rad).

Two-track processing:
  pd_ema   — updated on EVERY packet (real + injected); stable ACTIVE signal
  tick counting — only on REAL beacons (Pi1=10.0.0.122, Pi2=10.0.0.174);
                  matches glyph_tick() cadence in reader_glyph

Usage: python3 glyph/glyph_rx.py
"""
import socket, struct, math, sys

BEACON_GRP   = "239.0.0.1"
BEACON_PORT  = 7400
BEACON_MAGIC = 0x1B4A
BEACON_FMT   = "!HBIffBQ"
BEACON_SIZE  = struct.calcsize(BEACON_FMT)

PI1_IP = "10.0.0.122"
PI2_IP = "10.0.0.174"

EMA_ALPHA     = 0.20    # slow enough to bridge real Pi1 resets (~50ms gap)
ACTIVE_THRESH = 0.08    # above lock noise, below steady-state EMA (~0.22)

DIT_MAX_TICKS = 16
REST_LETTER   = 16
REST_WORD     = 40

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
}.items()}

def ip_str(addr_bytes):
    return socket.inet_ntoa(addr_bytes)

def listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", BEACON_PORT))
    mreq = socket.inet_aton(BEACON_GRP) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)

    pi1_packed = socket.inet_aton(PI1_IP)
    pi2_packed = socket.inet_aton(PI2_IP)

    print(f"RX listening on beacon multicast {BEACON_GRP}:{BEACON_PORT}", flush=True)

    phases = [0.0, 0.0, 0.0]
    seen   = [False, False, False]
    pd_ema = 0.0

    active_run = 0
    rest_run   = 0
    letter_buf = ""
    word_buf   = []
    output     = []

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
            data, (src_ip, _) = sock.recvfrom(64)
        except TimeoutError:
            if active_run:
                commit_symbol()
            if letter_buf:
                commit_letter()
            if word_buf:
                commit_word()
            flush_output()
            continue

        if len(data) < BEACON_SIZE:
            continue
        fields = struct.unpack(BEACON_FMT, data[:BEACON_SIZE])
        magic, sid, tick, theta, omega, _pad, t0 = fields
        if magic != BEACON_MAGIC or sid not in (1, 2):
            continue

        # always update phases for pd_dev computation
        phases[sid] = theta
        seen[sid] = True

        if not (seen[1] and seen[2]):
            continue

        diff = phases[1] - phases[2]
        phase_diff = abs(math.fmod(diff + math.pi, 2 * math.pi) - math.pi)
        pd_dev = abs(phase_diff - math.pi)
        pd_ema = EMA_ALPHA * pd_dev + (1 - EMA_ALPHA) * pd_ema

        # tick counting only for real Pi beacons — matches glyph_tick cadence
        src_packed = socket.inet_aton(src_ip)
        if src_packed != pi1_packed and src_packed != pi2_packed:
            continue   # injected packet: updated EMA, skip tick

        active = pd_ema >= ACTIVE_THRESH

        if active:
            if rest_run >= REST_WORD:
                commit_symbol(); commit_letter(); commit_word()
            elif rest_run >= REST_LETTER:
                commit_symbol(); commit_letter()
            elif rest_run > 0 and active_run > 0:
                commit_symbol()
            rest_run = 0
            active_run += 1
        else:
            rest_run += 1
            if rest_run == REST_LETTER:
                commit_symbol()
                commit_letter()
            elif rest_run == REST_WORD:
                commit_word()

if __name__ == "__main__":
    listen()
