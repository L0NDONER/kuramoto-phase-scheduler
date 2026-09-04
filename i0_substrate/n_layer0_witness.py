#!/usr/bin/env python3
"""
layer0_shared_substrate/n_layer0_witness.py -- the domain that watches
the floor from outside the floor.

Every other N-domain in this project *gates* on Layer0 freshness (real
DENY when stale, real ALLOW when fresh) -- that's the whole point of
touching the substrate. This one is the deliberate exception: it never
gates on anything. Its only job is to independently record the
substrate's own health -- tick continuity, restarts, outages, and the
pd distribution -- so that later, when someone asks "was that a
network hiccup or was Mint just busy," there's a real record instead
of a guess. See the README's "Where the weight actually went" section
for the problem this exists to answer: pd is computed once by the
daemon and broadcast, so Mint's own local scheduling noise becomes
every domain's shared truth. A domain that only reads pd at the moment
it needs it can't tell noise from a genuine event after the fact --
this one keeps the history.

It must keep working when Layer0 dies. If it gated on freshness like
every other domain here, it would go blind at exactly the moment its
job matters most (recording that Layer0 died, and for how long).

Records, all independent of gating:
  - restarts: daemon tick counter going backwards (new process started)
  - outages: no packet for OUTAGE_THRESHOLD_S or longer, with duration
  - pd distribution: rolling window mean/stdev/min/max, logged periodically
  - tick continuity: gaps between observed ticks (missed packets, not
    necessarily outages -- UDP has no delivery guarantee)

Telegram notification on OUTAGE START/END, added 2026-09-02: reuses
the same bot already wired up for Watchtower (see
[[project_watchtower_containers]]) rather than a new one -- currently
the ONLY way to know Layer0 itself went down was to grep this
process's own log, the same silent-gap shape Watchtower's `notify=no`
had before tonight. Non-fatal by design: a Telegram failure prints a
warning and moves on, never crashes the loop or blocks recording --
this domain's whole reason to exist is staying up when everything else
including the network to Telegram might not be.

Usage:
    python3 n_layer0_witness.py [--name mint-witness]
"""
import argparse
import json
import math
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from collections import deque

LAYER0_GRP, LAYER0_PORT = "239.0.0.6", 7500
LAYER0_MAGIC = 0x4C30
LAYER0_FMT = "!HIffd"
LAYER0_SIZE = struct.calcsize(LAYER0_FMT)

OUTAGE_THRESHOLD_S = 3.0   # no packet for this long -> a real outage, not just one lost UDP frame
REPORT_INTERVAL_S = 30.0  # how often the pd-distribution summary prints
PD_WINDOW = 600           # ~30s of samples at 20Hz

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def notify_telegram(name, text):
    """Best-effort, non-fatal. Missing credentials or a network failure
    just prints a warning -- never allowed to affect the witness's own
    recording, which is the one thing that must never stop."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"Layer0 witness ({name})\n{text}",
        }).encode()
        req = urllib.request.Request(url, data=payload,
                                      headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError) as e:
        print(f"[{name}] telegram notify failed (non-fatal): {e!r}", flush=True)


def get_layer0_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", LAYER0_PORT))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(LAYER0_GRP) + socket.inet_aton("0.0.0.0"))
    sock.settimeout(1.0)
    return sock


def pd_stats(window):
    n = len(window)
    if n == 0:
        return None
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / n
    return mean, math.sqrt(var), min(window), max(window), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="witness")
    args = ap.parse_args()

    sock = get_layer0_socket()
    print(f"[{args.name}] bound to {LAYER0_GRP}:{LAYER0_PORT} -- ungated, "
          f"recording tick continuity/restarts/outages/pd distribution, "
          f"never blocked on freshness", flush=True)

    last_tick = None
    last_seen_wall = None
    outage_start = None      # wall time an outage began, None if currently up
    pd_window = deque(maxlen=PD_WINDOW)
    next_report_at = time.time() + REPORT_INTERVAL_S
    total_restarts = 0
    total_outages = 0
    total_outage_s = 0.0
    started_at = time.time()

    while True:
        try:
            data, _ = sock.recvfrom(64)
        except socket.timeout:
            now = time.time()
            if last_seen_wall is not None:
                age = now - last_seen_wall
                if age >= OUTAGE_THRESHOLD_S and outage_start is None:
                    outage_start = last_seen_wall
                    msg = (f"OUTAGE START -- no packet since "
                           f"{time.strftime('%H:%M:%S', time.localtime(last_seen_wall))} "
                           f"(age={age:.1f}s)")
                    print(f"[{args.name}] {msg}", flush=True)
                    notify_telegram(args.name, msg)
            if now >= next_report_at:
                _maybe_report(args.name, pd_window, started_at, total_restarts,
                               total_outages, total_outage_s, outage_start, now)
                next_report_at = now + REPORT_INTERVAL_S
            continue

        if len(data) < LAYER0_SIZE:
            continue
        magic, tick, theta, pd, t0 = struct.unpack(LAYER0_FMT, data[:LAYER0_SIZE])
        if magic != LAYER0_MAGIC:
            continue

        now = time.time()

        if outage_start is not None:
            duration = now - outage_start
            total_outages += 1
            total_outage_s += duration
            msg = f"OUTAGE END -- back after {duration:.1f}s (tick resumed at {tick})"
            print(f"[{args.name}] {msg}", flush=True)
            notify_telegram(args.name, msg)
            outage_start = None

        if last_tick is not None and tick < last_tick:
            total_restarts += 1
            print(f"[{args.name}] RESTART -- tick went backwards "
                  f"({last_tick} -> {tick}), daemon process restarted", flush=True)

        last_tick = tick
        last_seen_wall = now
        pd_window.append(pd)

        if now >= next_report_at:
            _maybe_report(args.name, pd_window, started_at, total_restarts,
                           total_outages, total_outage_s, outage_start, now)
            next_report_at = now + REPORT_INTERVAL_S


def _maybe_report(name, pd_window, started_at, total_restarts, total_outages,
                   total_outage_s, outage_start, now):
    stats = pd_stats(pd_window)
    uptime_s = now - started_at
    live_outage_s = total_outage_s + (now - outage_start if outage_start else 0.0)
    availability = 100.0 * (1 - live_outage_s / uptime_s) if uptime_s > 0 else 100.0
    if stats:
        mean, stdev, mn, mx, n = stats
        print(f"[{name}] SUMMARY pd(n={n}): mean={mean:+.4f} stdev={stdev:.4f} "
              f"min={mn:+.4f} max={mx:+.4f} | restarts={total_restarts} "
              f"outages={total_outages} availability={availability:.3f}%", flush=True)
    else:
        print(f"[{name}] SUMMARY no pd samples yet | restarts={total_restarts} "
              f"outages={total_outages} availability={availability:.3f}%", flush=True)


if __name__ == "__main__":
    main()
