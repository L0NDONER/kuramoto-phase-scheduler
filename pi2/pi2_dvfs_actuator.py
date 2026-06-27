#!/usr/bin/env python3
"""
pi2_dvfs_actuator.py — cpufreq actuator for Pi2 DVFS neuron.

Listens UDP :7433 for ASCII intents from pi2_dvfs_reader:
  "PARK"   → step down one frequency level (clock down)
  "UNPARK" → step up one frequency level   (clock up)
  "HOLD"   → no-op

Pins frequency by writing the same value to scaling_min_freq and
scaling_max_freq for all CPUs. Works with any governor.

Dwell gate is identical to pi2_actuator.py: 15s sustained majority.

Usage: sudo python3 pi2_dvfs_actuator.py
"""
import socket, os, sys, time

INTENT_PORT = 7433
DWELL_SECS  = 15
NCPUS       = 4

def _freq_path(cpu, attr):
    return f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/{attr}"

def read_available_freqs():
    with open(_freq_path(0, "scaling_available_frequencies")) as f:
        freqs = sorted(int(x) for x in f.read().split())
    return freqs

def current_freq():
    with open(_freq_path(0, "scaling_cur_freq")) as f:
        return int(f.read().strip())

def set_freq(freq_khz):
    for cpu in range(NCPUS):
        try:
            with open(_freq_path(cpu, "scaling_min_freq"), "w") as f:
                f.write(str(freq_khz))
            with open(_freq_path(cpu, "scaling_max_freq"), "w") as f:
                f.write(str(freq_khz))
        except OSError as e:
            print(f"[dvfs] cpu{cpu} freq={freq_khz} failed: {e}", flush=True)

def step_down(freqs, cur):
    idx = freqs.index(cur) if cur in freqs else len(freqs) - 1
    nxt = freqs[max(0, idx - 1)]
    set_freq(nxt)
    return nxt

def step_up(freqs, cur):
    idx = freqs.index(cur) if cur in freqs else 0
    nxt = freqs[min(len(freqs) - 1, idx + 1)]
    set_freq(nxt)
    return nxt

def restore_scaling(freqs):
    lo, hi = freqs[0], freqs[-1]
    for cpu in range(NCPUS):
        try:
            with open(_freq_path(cpu, "scaling_min_freq"), "w") as f:
                f.write(str(lo))
            with open(_freq_path(cpu, "scaling_max_freq"), "w") as f:
                f.write(str(hi))
        except OSError:
            pass

freqs = read_available_freqs()
cur_freq = freqs[-1]   # start at max
set_freq(cur_freq)

print(f"[dvfs] freqs={[f//1000 for f in freqs]} MHz  start={cur_freq//1000} MHz", flush=True)

import atexit
atexit.register(restore_scaling, freqs)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", INTENT_PORT))
sock.settimeout(1.0)

print(f"[dvfs] listening on 127.0.0.1:{INTENT_PORT}  dwell={DWELL_SECS}s", flush=True)

last_change   = 0.0
pending       = None
pending_since = 0.0
votes_for     = 0
votes_against = 0

while True:
    try:
        data, _ = sock.recvfrom(64)
    except socket.timeout:
        continue

    intent = data.decode(errors="ignore").strip().upper()
    if intent not in ("PARK", "UNPARK", "HOLD"):
        print(f"[dvfs] unknown intent: {intent!r}", flush=True)
        continue

    now = time.time()

    if now - last_change < DWELL_SECS:
        continue

    if intent == "HOLD":
        pending = None
        votes_for = votes_against = 0
        continue

    if pending is None:
        pending       = intent
        pending_since = now
        votes_for     = 1
        votes_against = 0
        continue

    if intent == pending:
        votes_for += 1
    else:
        votes_against += 1
        if votes_against > votes_for:
            pending       = intent
            pending_since = now
            votes_for     = 1
            votes_against = 0
            continue

    if now - pending_since >= DWELL_SECS and votes_for > votes_against:
        prev = cur_freq
        if pending == "PARK":
            cur_freq = step_down(freqs, cur_freq)
        else:
            cur_freq = step_up(freqs, cur_freq)
        if cur_freq != prev:
            last_change = now
            ratio = votes_for / max(1, votes_for + votes_against)
            print(f"[dvfs] {pending}  {prev//1000} → {cur_freq//1000} MHz"
                  f"  (held {now-pending_since:.0f}s  {ratio:.0%} agreement)", flush=True)
        pending = None
        votes_for = votes_against = 0
