"""netlat_telemetry.py — real network round-trip latency to an external
host, via the system `ping` binary (no raw-socket privileges needed).
Genuinely independent of both the market feed (external price) and CPU
load (this machine's own scheduling) -- network path congestion, routing,
and the remote host's own load are a third, uncorrelated real-world
process, which is the actual point of this signal existing.
"""
import re
import subprocess

PING_TARGET = "1.1.1.1"   # Cloudflare -- stable, low-latency, widely reachable
PING_TIMEOUT_S = 1.5

_RTT_RE = re.compile(r"time=([\d.]+)\s*ms")


def sample_rtt_ms():
    """One real ping, returns RTT in ms, or None if it timed out/failed
    (a real network hiccup -- report it as missing data, not a fake 0)."""
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT_S), PING_TARGET],
            capture_output=True, text=True, timeout=PING_TIMEOUT_S + 1,
        ).stdout
    except subprocess.TimeoutExpired:
        return None
    m = _RTT_RE.search(out)
    return float(m.group(1)) if m else None
