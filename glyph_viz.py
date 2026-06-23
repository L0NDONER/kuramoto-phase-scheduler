#!/usr/bin/env python3
"""
glyph_viz.py — live 3D trajectory through (θ, ω, pd_dev) glyph space

R → θ      phase position on the circle
G → ω      rate of change (derived from consecutive θ)
B → pd_dev phase coherence quality

Each point is a glyph. The trajectory is the substrate speaking.
REST attractor sits near (π, ω₀, 0).

Usage: python3 glyph_viz.py [reader_ip]
"""

import math
import socket
import struct
import sys
import threading
import time
from collections import deque

import matplotlib.pyplot as plt
import matplotlib.animation as animation
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

AXIS_GRP   = "239.0.0.2"
AXIS_PORT  = 7404
AXIS_MAGIC = 0x4158

TRAIL      = 300   # points to show
TICK_S     = 0.025 # ~40Hz


# ── glyph classifier ─────────────────────────────────────────────────────────

def classify(locked, pd_dev, hold_ticks):
    if not locked:
        return "UNLOCKED", "red"
    if pd_dev < 0.05:
        return "REST", "#00aaff"
    if hold_ticks >= 8:            # sustained off-attractor = DAH
        return "DAH", "#ff6600"
    return "DRIFT", "#00ff88"      # brief perturbation


# ── beacon receiver thread ────────────────────────────────────────────────────

class BeaconReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.points  = deque(maxlen=TRAIL)  # (theta, omega, pd_dev, locked, glyph, color)
        self.lock    = threading.Lock()
        self._prev_theta = None
        self._prev_time  = None
        self._hold       = 0

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", AXIS_PORT))
        mreq = socket.inet_aton(AXIS_GRP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(2.0)

        while True:
            try:
                data, _ = sock.recvfrom(64)
            except TimeoutError:
                continue
            if len(data) < 38:
                continue
            magic, sid, locked, tick, t1, t2, pd, pd_dev, load, drains, t0_ns = struct.unpack(
                "!HBBIfffffHQ", data[:38]
            )
            if magic != AXIS_MAGIC:
                continue
            utc_ns = (t0_ns + tick * 50_000_000) if t0_ns else 0

            now = time.monotonic()

            # derive ω from consecutive θ (handle 2π wraparound)
            if self._prev_theta is not None and self._prev_time is not None:
                dt = now - self._prev_time
                if dt > 0:
                    diff = t1 - self._prev_theta
                    diff = (diff + math.pi) % (2 * math.pi) - math.pi
                    omega = diff / dt
                else:
                    omega = 0.0
            else:
                omega = 0.0

            self._prev_theta = t1
            self._prev_time  = now

            # hold counter for DAH detection
            if locked and pd_dev >= 0.05:
                self._hold += 1
            else:
                self._hold = 0

            glyph, color = classify(locked, pd_dev, self._hold)

            with self.lock:
                self.points.append((t1, omega, pd_dev, locked, glyph, color))

    def snapshot(self):
        with self.lock:
            return list(self.points)


# ── plot ──────────────────────────────────────────────────────────────────────

def main():
    reader = BeaconReader()
    reader.start()

    fig = plt.figure(figsize=(10, 7), facecolor="#0a0a0a")
    ax  = fig.add_subplot(111, projection="3d")
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)

    def style_ax():
        ax.set_facecolor("#0a0a0a")
        ax.xaxis.pane.fill = False
        ax.yaxis.pane.fill = False
        ax.zaxis.pane.fill = False
        ax.xaxis.pane.set_edgecolor("#222")
        ax.yaxis.pane.set_edgecolor("#222")
        ax.zaxis.pane.set_edgecolor("#222")
        ax.tick_params(colors="#444")
        ax.set_xlabel("θ  (phase)", color="#666", labelpad=8)
        ax.set_ylabel("ω  (rate)",  color="#666", labelpad=8)
        ax.set_zlabel("pd_dev  (coherence)", color="#666", labelpad=8)
        ax.set_xlim(0, 2 * math.pi)
        ax.set_ylim(-1.0, 1.0)
        ax.set_zlim(0, 0.5)
        # REST attractor marker
        ax.scatter([math.pi], [0], [0], color="#ffffff", s=60, marker="*",
                   zorder=10, alpha=0.6)

    # title text
    title = fig.text(0.5, 0.96, "glyph space", ha="center", color="#555",
                     fontsize=11, fontfamily="monospace")
    glyph_text = fig.text(0.5, 0.92, "—", ha="center", color="#ffffff",
                          fontsize=18, fontfamily="monospace", fontweight="bold")

    trail_line  = [None]
    head_point  = [None]

    def update(_frame):
        pts = reader.snapshot()
        if len(pts) < 2:
            return

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]
        colors = [p[5] for p in pts]

        ax.cla()
        style_ax()

        # trail — fade alpha along length
        n = len(pts)
        for i in range(1, n):
            alpha = 0.15 + 0.85 * (i / n)
            ax.plot(xs[i-1:i+1], ys[i-1:i+1], zs[i-1:i+1],
                    color=colors[i], alpha=alpha, linewidth=1.2)

        # head
        ax.scatter([xs[-1]], [ys[-1]], [zs[-1]],
                   color=colors[-1], s=80, zorder=10)

        latest = pts[-1]
        glyph_text.set_text(latest[4])
        glyph_text.set_color(latest[5])
        title.set_text(
            f"θ={latest[0]:.3f}  ω={latest[1]:.3f}  pd_dev={latest[2]:.4f}  "
            f"{'LOCKED' if latest[3] else 'hunting'}"
        )

    ani = animation.FuncAnimation(fig, update, interval=50, cache_frame_data=False)
    plt.show()


if __name__ == "__main__":
    main()
