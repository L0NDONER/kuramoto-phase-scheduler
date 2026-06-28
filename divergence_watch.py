#!/usr/bin/env python3
"""
Watch both Pi2 triangle logs for the shared-hysteresis divergence signature:
  - temlum_a and temlum_b differ
  - pd_pop shifts in opposite directions between ticks
  - e_C crosses PARK↔HOLD at different ticks
  - both triangles still agree on intent

Usage: python3 divergence_watch.py
"""
import subprocess, re, sys, time
from collections import deque

PI2 = "pi2"
LOG_A = "/home/pi/pi2_reader.log"
LOG_B = "/home/pi/pi2_dvfs_reader.log"

pat_a = re.compile(
    r'\[pi2_reader/A\].*?temlum=([+-]?\d+\.\d+).*?temlum_b=([+-]?\d+\.\d+)'
    r'.*?pop=([+-]?\d+\.\d+).*?e_C=([+-]?\d+\.\d+).*?→ (\w+)'
)
pat_b = re.compile(
    r'\[pi2_dvfs_reader/B\].*?temlum=([+-]?\d+\.\d+).*?temlum_a=([+-]?\d+\.\d+)'
    r'.*?pop=([+-]?\d+\.\d+).*?e_C=([+-]?\d+\.\d+).*?→ (\w+)'
)

E_PARK   = -0.003
E_UNPARK =  0.004

def band(ec):
    if   ec >  E_UNPARK: return "UNPARK"
    elif ec <  E_PARK:   return "PARK"
    else:                return "HOLD"

last_a = last_b = None
prev_pop_a = prev_pop_b = None
event_log = deque(maxlen=200)

def check(a, b):
    ta, tb_seen, pop_a, ec_a, intent_a = a
    tb, ta_seen, pop_b, ec_b, intent_b = b

    temlum_delta = abs(ta - tb)
    pop_delta    = pop_a - pop_b          # positive = A higher
    band_a = band(ec_a)
    band_b = band(ec_b)
    agree  = intent_a == intent_b

    diverging  = temlum_delta > 0.02
    opp_pop    = (prev_pop_a is not None and prev_pop_b is not None and
                  (pop_a - prev_pop_a) * (pop_b - prev_pop_b) < 0)
    diff_cross = band_a != band_b
    signature  = diverging and agree and (opp_pop or diff_cross)

    now = time.strftime("%H:%M:%S")
    line = (f"{now}  A: tl={ta:+.3f} tl_b={tb_seen:+.3f} pop={pop_a:+.5f} "
            f"e_C={ec_a:+.5f} [{band_a}]→{intent_a}  |  "
            f"B: tl={tb:+.3f} tl_a={ta_seen:+.3f} pop={pop_b:+.5f} "
            f"e_C={ec_b:+.5f} [{band_b}]→{intent_b}  "
            f"Δtl={temlum_delta:.3f}")
    event_log.append(line)

    if signature:
        print("\n" + "═"*90)
        print("DIVERGENCE SIGNATURE DETECTED")
        print(f"  Δtemlum={temlum_delta:.3f}  opp_pop={opp_pop}  diff_band={diff_cross}  agree={agree}")
        print("═"*90)
        for l in list(event_log)[-10:]:
            marker = "  ◀ NOW" if l == line else ""
            print(l + marker)
        print("═"*90 + "\n")
        sys.stdout.flush()
    else:
        print(line, flush=True)

    return pop_a, pop_b

proc_a = subprocess.Popen(
    ["ssh", PI2, f"tail -f {LOG_A}"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
)
proc_b = subprocess.Popen(
    ["ssh", PI2, f"tail -f {LOG_B}"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
)

import select as sel_mod

print("watching Pi2 triangles for divergence signature…", flush=True)

fds = {proc_a.stdout.fileno(): ("A", proc_a.stdout),
       proc_b.stdout.fileno(): ("B", proc_b.stdout)}

while True:
    ready, _, _ = sel_mod.select(list(fds.keys()), [], [], 5.0)
    for fd in ready:
        tag, stream = fds[fd]
        line = stream.readline()
        if not line:
            continue
        if tag == "A":
            m = pat_a.search(line)
            if m:
                last_a = (float(m.group(1)), float(m.group(2)),
                          float(m.group(3)), float(m.group(4)), m.group(5))
        else:
            m = pat_b.search(line)
            if m:
                last_b = (float(m.group(1)), float(m.group(2)),
                          float(m.group(3)), float(m.group(4)), m.group(5))

        if last_a and last_b:
            prev_pop_a_new, prev_pop_b_new = check(last_a, last_b)
            prev_pop_a, prev_pop_b = prev_pop_a_new, prev_pop_b_new
            last_a = last_b = None
