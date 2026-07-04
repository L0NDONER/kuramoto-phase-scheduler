#!/usr/bin/env python3
"""
glyph_send.py — Morse transmission paced by a real quartz tick.

No coupling, no injection into the pd measurement channel, no reader_glyph,
no tc. The crystal's own adjtimex-derived tick *is* the Morse clock —
same idea as sudoku_quartz.py, applied to sending text instead of solving
a puzzle.

Usage:
    python3 glyph_send.py "HELLO WORLD"
"""
import ctypes, sys, time

MORSE = {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',
    'E': '.',     'F': '..-.',  'G': '--.',   'H': '....',
    'I': '..',    'J': '.---',  'K': '-.-',   'L': '.-..',
    'M': '--',    'N': '-.',    'O': '---',   'P': '.--.',
    'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',
    'Y': '-.--',  'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--',
    '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.',
}

DIT_TICKS = 3   # base unit; DAH = 3x, intra-symbol gap = 1x,
                # inter-letter gap = 3x, inter-word gap = 7x (standard ratios)


class _timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_int), ("offset", ctypes.c_long),
                ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                ("tolerance", ctypes.c_long), ("time", ctypes.c_long * 2),
                ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
                ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long),
                ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
                ("pad", ctypes.c_int * 11)]


def read_freq_ppm():
    tx = _timex()
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.adjtimex(ctypes.byref(tx)) < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return tx.freq / 65536.0


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "HELLO WORLD"

    ppm = read_freq_ppm()
    tick_s = 1.0 / abs(ppm)
    print(f"[glyph] freq_ppm={ppm:.6f}  tick={tick_s*1000:.3f}ms (live adjtimex)")
    print(f"[glyph] TX → {text!r}\n")

    t_next = time.monotonic()

    def wait_ticks(n):
        nonlocal t_next
        for _ in range(n):
            t_next += tick_s
            gap = t_next - time.monotonic()
            if gap > 0:
                time.sleep(gap)

    def blink(on, ticks):
        print("█" if on else "░", end="", flush=True)
        wait_ticks(ticks)

    words = text.upper().split(" ")
    for wi, word in enumerate(words):
        for ci, ch in enumerate(word):
            code = MORSE.get(ch)
            if not code:
                continue
            for si, sym in enumerate(code):
                blink(True, DIT_TICKS * (3 if sym == '-' else 1))
                if si < len(code) - 1:
                    blink(False, DIT_TICKS)
            print(f"  {ch}", flush=True)
            if ci < len(word) - 1:
                blink(False, DIT_TICKS * 3)
        if wi < len(words) - 1:
            blink(False, DIT_TICKS * 7)
            print("  /", flush=True)

    print(f"\n[glyph] done, {tick_s*1000:.3f}ms/tick", flush=True)


if __name__ == "__main__":
    main()
