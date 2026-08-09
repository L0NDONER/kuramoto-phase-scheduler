#!/usr/bin/env python3
"""
wave-pacer.py — NFQUEUE pacer with DRR fairness, sine envelope from Pi1 quartz,
plus a bounded adaptive boost driven by real queue-depth/drop feedback.

iptables setup (already in shaper-setup.sh):
  iptables -t mangle -A POSTROUTING -o eth0 -j NFQUEUE --queue-num 10 --queue-maxlen 1024
  Mark packets by service: 40=iplayer 35=itvx 20=sky 10=youtube
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
MAX_QUEUE_PKTS = 200            # per-flow software queue cap — bounds latency/memory
                                 # under sustained overload instead of growing unbounded
TOKEN_BURST_S  = 0.05           # per-flow rate_bps enforced via token bucket, 50ms burst allowance

# Sine period derived from Pi1 adjtimex freq_ppm = -14.143646
# abs(ppm) → seconds: this crystal's own deviation signature
PERIOD_S       = 14.143646    # quartz-derived; do not hand-tune

# Adaptive boost: real feedback from queue depth / drop rate, not a fixed
# schedule and not ML - EWMA-smoothed congestion signal computed entirely
# from Flow state this process already owns (no file/network/subprocess
# I/O, so it costs nothing extra inside the locked hot loop). Boost only
# ever adds on top of the sine envelope's 0.8-1.0 floor/ceiling; it never
# reduces below 0.8, only relieves genuine congestion above 1.0.
ADAPT_GAIN     = 0.2   # max extra envelope headroom when fully congested
EWMA_ALPHA     = 0.1   # smoothing: ~10-tick (100ms) rise, few-second decay
ENVELOPE_FLOOR = 0.8
ENVELOPE_CEIL  = 0.8 + 0.2 + ADAPT_GAIN   # sine ceiling (1.0) + full adaptive boost

# iptables mark → flow name
MARK_FLOW = {
    40: "iplayer",
    35: "itvx",
    20: "sky",     # shaper-setup.sh marks this "Sky Go — mark 20"; there is no NOW TV rule
    10: "youtube",
}
DEFAULT_FLOW = "default"

# Per-flow rate target and DRR quantum (quantum ∝ rate for weighted fairness)
FLOW_CFG = {
    "iplayer": {"rate_bps":  4_500_000, "quantum":  4_500},
    "itvx":    {"rate_bps":  7_000_000, "quantum":  7_000},
    "sky":     {"rate_bps":  4_200_000, "quantum":  4_200},
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
    dropped: int = 0     # packets tail-dropped because queue was at MAX_QUEUE_PKTS
    tokens: float = 0.0  # rate_bps token bucket — enforces the per-flow ceiling
                          # independently of DRR quantum, which only governs
                          # fairness when multiple flows are contending


class WavePacer:
    def __init__(self):
        self.flows = {
            name: Flow(name, cfg["rate_bps"], cfg["quantum"])
            for name, cfg in FLOW_CFG.items()
        }
        self.nfq = NetfilterQueue()
        self._lock = threading.Lock()
        self.running = False

        # Adaptive-boost state
        self._congestion_ewma = 0.0
        self._last_total_drops = 0
        self._max_queue_total = MAX_QUEUE_PKTS * len(self.flows)

    # ------------------------------------------------------------------
    # NFQUEUE callback — runs in nfq.run() thread
    # ------------------------------------------------------------------

    def _callback(self, pkt):
        flow_name = MARK_FLOW.get(pkt.get_mark(), DEFAULT_FLOW)
        with self._lock:
            flow = self.flows[flow_name]
            if len(flow.queue) >= MAX_QUEUE_PKTS:
                # Tail-drop: bounds latency/memory under sustained overload
                # instead of the software queue growing without limit.
                flow.dropped += 1
                pkt.drop()
                return
            pkt.retain()                    # keep handle alive across threads
            payload = pkt.get_payload()
            flow.queue.append((pkt, payload))

    # ------------------------------------------------------------------
    # Adaptive boost — real feedback, computed from data already in hand
    # ------------------------------------------------------------------

    def _update_adaptive_boost(self):
        """Must be called with self._lock held. Returns the current boost
        (0..ADAPT_GAIN) to add on top of the sine envelope."""
        total_depth = sum(len(f.queue) for f in self.flows.values())
        depth_pressure = total_depth / self._max_queue_total  # 0..1

        total_drops = sum(f.dropped for f in self.flows.values())
        drop_delta = total_drops - self._last_total_drops
        self._last_total_drops = total_drops
        drop_pressure = 1.0 if drop_delta > 0 else 0.0  # any drop this tick is a hard signal

        congestion_signal = min(1.0, depth_pressure + drop_pressure)
        self._congestion_ewma += EWMA_ALPHA * (congestion_signal - self._congestion_ewma)

        return ADAPT_GAIN * self._congestion_ewma

    # ------------------------------------------------------------------
    # Drain loop — runs in dedicated thread at TICK_HZ
    # ------------------------------------------------------------------

    def loop(self):
        self.running = True
        tick_interval = 1.0 / TICK_HZ
        last_tick = time.monotonic()
        next_status_at = last_tick + 1.0

        while self.running:
            now = time.monotonic()
            dt = now - last_tick
            if dt < tick_interval:
                time.sleep(tick_interval - dt)
                continue
            last_tick = now

            with self._lock:
                # Quartz sine envelope (0.8-1.0) plus a bounded adaptive
                # boost from real queue-depth/drop feedback - the boost can
                # only push the ceiling up under genuine congestion, never
                # push the floor down.
                sine_envelope = 0.8 + 0.2 * math.sin(2 * math.pi * now / PERIOD_S)
                boost = self._update_adaptive_boost()
                envelope = min(ENVELOPE_CEIL, sine_envelope + boost)
                budget   = int((BASE_RATE_BPS * envelope / 8) * dt)

                # Refill each flow's rate_bps token bucket. This enforces
                # rate_bps as a real per-flow ceiling — DRR quantum below
                # only governs fairness *among contending flows*, it doesn't
                # cap a single flow that has the queue to itself.
                for flow in self.flows.values():
                    cap = flow.rate_bps / 8 * TOKEN_BURST_S
                    flow.tokens = min(cap, flow.tokens + flow.rate_bps / 8 * dt)

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
                            if flow.tokens < sz:
                                break           # rate_bps ceiling: flow is out of tokens
                            if budget <= 0:
                                break
                            flow.queue.popleft()
                            pkt_obj.accept()
                            flow.deficit -= sz
                            flow.tokens -= sz
                            flow.bytes_sent += sz
                            budget -= sz
                            sent_this_round = True
                        if budget <= 0:
                            break
                    if not sent_this_round:
                        break                   # all queues empty, deficit too low, or out of tokens

                if now >= next_status_at:
                    next_status_at = now + 1.0
                    depth = {n: len(f.queue) for n, f in self.flows.items() if f.queue}
                    drops = {n: f.dropped for n, f in self.flows.items() if f.dropped}
                    print(f"[wave-pacer] envelope={envelope:.3f} (sine={sine_envelope:.3f} "
                          f"boost={boost:.3f}) q_bytes={self._q_bytes()} depth={depth or '{}'} "
                          f"drops={drops or '{}'} "
                          f"sent={ {n: f.bytes_sent for n, f in self.flows.items()} }",
                          flush=True)

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
              f"period={PERIOD_S}s  envelope={ENVELOPE_FLOOR}-{ENVELOPE_CEIL:.2f} "
              f"(sine 0.8-1.0 + adaptive boost up to {ADAPT_GAIN})",
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
