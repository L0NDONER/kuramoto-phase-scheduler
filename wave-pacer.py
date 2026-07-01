#!/usr/bin/env python3
"""
wave-pacer.py — NFQUEUE pacer with DRR fairness, sine envelope from Pi1 quartz.

iptables setup (already in shaper-setup.sh):
  iptables -t mangle -A POSTROUTING -o eth0 -j NFQUEUE --queue-num 10 --queue-maxlen 1024
  Mark packets by service: 40=iplayer 35=itvx 20=nowtv 10=youtube
"""

import math
import time
import threading
import signal
from collections import deque
from dataclasses import dataclass, field  # field used by Flow
from netfilterqueue import NetfilterQueue

QUEUE_NUM      = 10
BASE_RATE_BPS  = 20_000_000   # 20 Mbps envelope
TICK_HZ        = 100           # 10 ms ticks; cooperates with scheduler

# Sine period derived from Pi1 adjtimex freq_ppm = -14.143646
# abs(ppm) → seconds: this crystal's own deviation signature
PERIOD_S       = 14.143646    # quartz-derived; do not hand-tune

# iptables mark → flow name
MARK_FLOW = {
    40: "iplayer",
    35: "itvx",
    20: "nowtv",
    10: "youtube",
}
DEFAULT_FLOW = "default"

# Per-flow rate target and DRR quantum (quantum ∝ rate for weighted fairness)
FLOW_CFG = {
    "iplayer": {"rate_bps":  4_500_000, "quantum":  4_500},
    "itvx":    {"rate_bps":  7_000_000, "quantum":  7_000},
    "nowtv":   {"rate_bps":  4_200_000, "quantum":  4_200},
    "youtube": {"rate_bps": 14_000_000, "quantum": 14_000},
    "default": {"rate_bps": BASE_RATE_BPS, "quantum": 1_500},
}


@dataclass
class Flow:
    name: str
    rate_bps: int
    quantum: int
    queue: deque = field(default_factory=deque)   # items: (pkt_handle, payload_bytes)
    deficit: int = 0
    bytes_sent: int = 0


class WavePacer:
    def __init__(self):
        self.flows = {
            name: Flow(name, cfg["rate_bps"], cfg["quantum"])
            for name, cfg in FLOW_CFG.items()
        }
        self.nfq = NetfilterQueue()
        self._lock = threading.Lock()
        self.running = False

    # ------------------------------------------------------------------
    # NFQUEUE callback — runs in nfq.run() thread
    # ------------------------------------------------------------------

    def _callback(self, pkt):
        flow_name = MARK_FLOW.get(pkt.get_mark(), DEFAULT_FLOW)
        pkt.retain()                        # keep handle alive across threads
        payload = pkt.get_payload()
        with self._lock:
            self.flows[flow_name].queue.append((pkt, payload))

    # ------------------------------------------------------------------
    # Drain loop — runs in dedicated thread at TICK_HZ
    # ------------------------------------------------------------------

    def loop(self):
        self.running = True
        tick_interval = 1.0 / TICK_HZ
        last_tick = time.monotonic()

        while self.running:
            now = time.monotonic()
            dt = now - last_tick
            if dt < tick_interval:
                time.sleep(tick_interval - dt)
                continue
            last_tick = now

            with self._lock:
                # Quartz sine envelope: 0.8–1.0 × BASE_RATE over PERIOD_S
                envelope = 0.8 + 0.2 * math.sin(2 * math.pi * now / PERIOD_S)
                budget   = int((BASE_RATE_BPS * envelope / 8) * dt)

                # Deficit Round Robin across flows
                while budget > 0:
                    sent_this_round = False
                    for flow in self.flows.values():
                        if not flow.queue:
                            flow.deficit = 0    # reset so idle flows don't bank credit
                            continue
                        flow.deficit += flow.quantum
                        while flow.queue:
                            pkt_obj, payload = flow.queue[0]
                            sz = len(payload)
                            if flow.deficit < sz:
                                break           # DRR: wait until deficit covers packet
                            if budget <= 0:
                                break
                            flow.queue.popleft()
                            pkt_obj.accept()
                            flow.deficit -= sz
                            flow.bytes_sent += sz
                            budget -= sz
                            sent_this_round = True
                        if budget <= 0:
                            break
                    if not sent_this_round:
                        break                   # all queues empty or deficit too low

    def _q_bytes(self) -> int:
        return sum(sum(len(p) for _, p in f.queue) for f in self.flows.values())

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def start(self):
        self._stop = threading.Event()
        self.nfq.bind(QUEUE_NUM, self._callback)
        threading.Thread(target=self.loop, daemon=True, name="pacer-drain").start()
        threading.Thread(target=self.nfq.run, daemon=True, name="nfq-recv").start()
        print(f"[wave-pacer] queue={QUEUE_NUM}  "
              f"rate={BASE_RATE_BPS // 1_000_000} Mbps  tick={TICK_HZ} Hz  "
              f"period={PERIOD_S}s  envelope=0.8–1.0",
              flush=True)
        self._stop.wait()           # main thread blocks here until signal
        self.running = False
        self.nfq.unbind()           # closes netlink socket → unblocks nfq.run()

    def stop(self):
        self._stop.set()


def main():
    pacer = WavePacer()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: pacer.stop())
    pacer.start()


if __name__ == "__main__":
    main()
