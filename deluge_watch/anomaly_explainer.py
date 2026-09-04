"""anomaly_explainer.py — LLM consumer, triggered only when
sustained_deviation_watch.py's real output changes state (flags a new
sustained deviation, or clears one). Reads the substrate's log, never
writes back into it -- same boundary as every other consumer built this
session (regime_classifier.py, reflex_power_cap.py).

Only fires on state TRANSITIONS (the ">>> SUSTAINED DEVIATION" and
"cleared" lines), not on every "still flagged" tick -- otherwise this
would query the model every ~0.7s while a real anomaly is ongoing, which
is neither useful (nothing new to explain) nor cheap.

Switched from local Ollama (gpt-oss:20b) to Groq's cloud API 2026-08-14.
Local was CPU-bound (~150-200s/call on 8 cores, no GPU). Trade-off, made
explicitly with the user: trigger context (CPU load %, process/network
attribution) now leaves the machine over the network on every trigger,
instead of staying fully local. Same consumer boundary as before --
reads the log, decides nothing, reports. Model wiring (name, quota
backoff) now lives in groq_client.py, shared with ask.py/ask_node.py/
consensus_explainer.py.
"""
import os
import re
import sys
import time

# groq_client.py moved into cascade_pll/readers/ in the 2026-08-15
# reorg (was directly at the old repo root, one level up from here).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "readers"))
from groq_client import query_llm as _query_llm, QuotaExceeded, MODEL

WATCH_LOG = "sustained.log"
POLL_INTERVAL_S = 1.0
CONTEXT_LINES = 6   # recent log lines included as context before the trigger


def query_llm(prompt, state_hash=None):
    return _query_llm(prompt, user_agent="deluge-watch-anomaly-explainer/1.0",
                       state_hash=state_hash)


def _severity_bucket(pct):
    if pct >= 90:
        return "severe"
    if pct >= 75:
        return "high"
    return "moderate"


def trigger_state_hash(line):
    """Persistent cache key, added 2026-08-16 -- same pattern as
    consensus_explainer.py/mesh_healer.py: bucketed by which cores, their
    tag (process/network), and a rough severity tier, NOT raw percentages
    (which change every tick and would never repeat, defeating the
    cache). Real crash-restart risk now that this runs under systemd
    with Restart=on-failure -- a restart right after a real trigger
    could otherwise immediately re-explain the same ongoing deviation."""
    if line.strip().startswith("cleared"):
        return "sustained:cleared"
    cores = re.findall(r"c(\d+):([\d.]+)%.*?/(process|network)", line)
    if not cores:
        return None
    core_ids = sorted(c[0] for c in cores)
    max_pct = max(float(c[1]) for c in cores)
    tags = sorted(set(c[2] for c in cores))
    return f"sustained:{','.join(core_ids)}:{'-'.join(tags)}:{_severity_bucket(max_pct)}"


def build_prompt(trigger_line, context_lines):
    context = "\n".join(context_lines) if context_lines else "(no prior context yet)"
    return f"""You are reading real, live telemetry from an automated CPU-load
anomaly detector on a Linux workstation. It flags when a core's coupling
gain (a deviation-proportional measure of real load) stays high and
tightly locked for several seconds straight. "process" means the busy
time is real application CPU work; "network" means it's kernel softirq
(network interrupt) processing.

Recent readings leading up to this line:
{context}

Line that triggered this explanation:
{trigger_line}

In 2-3 plain-English sentences, explain what's most likely happening on
this machine right now, based only on this data. Be concrete about which
core(s) and whether it looks process-driven or network-driven. Do not
speculate about anything not supported by the numbers."""


def tail_new_lines(path, last_pos):
    with open(path) as f:
        f.seek(last_pos)
        lines = f.readlines()
        new_pos = f.tell()
    return lines, new_pos


def is_transition(line):
    return ">>> SUSTAINED DEVIATION" in line or line.strip().startswith("cleared")


def main():
    print(f"[anomaly_explainer] watching {WATCH_LOG} for state transitions, "
          f"model={MODEL}", flush=True)

    last_pos = os.path.getsize(WATCH_LOG) if os.path.exists(WATCH_LOG) else 0
    recent_buffer = []

    while True:
        try:
            lines, last_pos = tail_new_lines(WATCH_LOG, last_pos)
        except FileNotFoundError:
            time.sleep(POLL_INTERVAL_S)
            continue

        for line in lines:
            line = line.rstrip("\n")
            if not line:
                continue

            if is_transition(line):
                context = recent_buffer[-CONTEXT_LINES:]
                prompt = build_prompt(line, context)
                print(f"[anomaly_explainer] triggered: {line}", flush=True)
                try:
                    explanation = query_llm(prompt, state_hash=trigger_state_hash(line))
                    print(f"[anomaly_explainer] explanation: {explanation}", flush=True)
                except QuotaExceeded as e:
                    print(f"[anomaly_explainer] skipping -- {e}", flush=True)
                except Exception as e:
                    print(f"[anomaly_explainer] ERROR querying LLM: {e}", flush=True)

            recent_buffer.append(line)
            recent_buffer = recent_buffer[-50:]

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
