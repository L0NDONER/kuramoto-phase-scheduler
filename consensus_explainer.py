"""consensus_explainer.py — LLM consumer fed by BOTH real signals tonight
proved complementary, not redundant:

  - per-source rolling z-score (consensus_test_simple.py's output) --
    catches an individual signal (market/mint/netlat) acting unusual
    relative to its OWN recent history. Proven tonight to match or beat
    the oscillator for this specific job (single-signal detection).

  - Layer 1's real r1 (layer1/layer1.log) -- catches the three sources'
    phases currently disagreeing with each other, a genuinely different,
    cross-signal question a per-source z-score structurally cannot ask.
    Proven tonight: correlation between r1 and avg|z| was ~0 (-0.05) over
    9 real minutes -- not redundant information.

Fires on EITHER trigger (sustained individual spike, OR sustained phase
disagreement), feeding the LLM both pieces of context together so it can
distinguish "one source is acting up but the group still agrees" from
"the group itself is disagreeing" -- two different real situations that
look the same if you only watch one of the two signals.

Same architecture as anomaly_explainer.py: reads two logs, decides
nothing on its own, reports. No actuation.

Switched from local Ollama (gpt-oss:20b) to Groq's cloud API 2026-08-14
-- local was CPU-bound (~150-200s/call, no GPU). Trade-off, made
explicitly with the user: per-source z-scores and r1 readings now leave
the machine over the network on every trigger, instead of staying fully
local. Model wiring (name, quota backoff) now lives in groq_client.py,
shared with ask.py/ask_node.py/anomaly_explainer.py.
"""
import os
import re
import sys
import time

# groq_client.py moved into readers/ (sibling dir) in the 2026-08-15
# reorg -- this file stayed at cascade_pll/ root.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "readers"))
from groq_client import query_llm as _query_llm, QuotaExceeded, MODEL

Z_LOG = "simple_consensus.log"
R1_LOG = "layer1/layer1.log"
POLL_INTERVAL_S = 1.0

# Hysteresis, not a single threshold -- confirmed live 2026-08-14 that a
# single Z_THRESHOLD flaps: market's z-score is genuinely noisy and
# hovers near 2.0 often, so a hit-counter that resets to 0 on any single
# dip below threshold re-crosses and re-fires within seconds, producing
# 386 triggers instead of rare sustained events (and burning the Groq
# free-tier token bucket doing it). Same hysteresis-gap pattern as
# R1_LOW/R1_HIGH below.
Z_ENTER = 2.0            # per-source |z| must rise above this to flag a spike
Z_EXIT = 1.3             # must fall below this to clear -- hysteresis gap
Z_SUSTAIN_TICKS = 6      # ~3-6s at the z-log's own ~0.5-0.6s cadence
Z_COOLDOWN_S = 120       # minimum real time between two Z-SPIKE queries
# Novelty gate on top of the cooldown -- the cooldown alone still lets
# two spikes 2+ min apart fire on essentially the same z-value if a
# source is just sitting noisily near threshold. Require the new
# trigger's trigger_z to have moved meaningfully from the last query's,
# not just "far enough apart in time."
Z_DELTA_MIN = 0.5
R1_LOW = 0.75            # enter "disagreement" below this
R1_HIGH = 0.90            # exit "disagreement" (return to agreement) above this -- hysteresis gap
R1_SUSTAIN_TICKS = 10     # 5s at layer1's 0.5s tick

Z_LINE_RE = re.compile(r"avg\|z\|=([\d.]+)\s+\(([^)]*)\)")
R1_LINE_RE = re.compile(r"nodes=\[([^\]]*)\].*?r1=([\d.]+)\s+psi1=([+-][\d.]+)")


def query_llm(prompt, state_hash=None):
    return _query_llm(prompt, user_agent="deluge-watch-consensus-explainer/1.0",
                       state_hash=state_hash)


def _z_bucket(z):
    # Bucketed, not raw -- a raw float state_hash would almost never
    # repeat given normal tick-to-tick jitter, defeating the cache.
    if z >= 3.5:
        return "severe"
    if z >= 2.5:
        return "high"
    return "moderate"


def _r1_bucket(r1):
    return "very-low" if r1 < 0.4 else "low"


def tail_new(path, pos):
    try:
        with open(path) as f:
            f.seek(pos)
            lines = f.readlines()
            return lines, f.tell()
    except FileNotFoundError:
        return [], pos


def build_prompt(reason, z_context, r1_context):
    return f"""You are reading real, live telemetry from a multi-signal consensus
monitor watching three independent real sources on a Linux workstation:
"market" (live crypto price feed), "mint" (this machine's real CPU
load), and "netlat" (real network latency to an external host).

Two separate signals are provided:
1. Per-source rolling z-score: how far EACH source individually sits
   from its own recent history (does not compare sources to each other).
2. r1: a coherence value (0-1) measuring whether the three sources'
   underlying oscillators currently agree with each other in phase --
   high r1 means genuine agreement, low r1 means real disagreement
   between the sources, independent of whether any one of them looks
   individually unusual.

Trigger reason: {reason}

Recent per-source z-scores:
{z_context}

Recent r1 (cross-source agreement) readings:
{r1_context}

In 2-4 plain-English sentences, explain what's most likely happening
right now. Be explicit about whether this looks like ONE source acting
up while the others still agree, or the sources genuinely disagreeing
with each other as a group. Do not speculate beyond what these numbers
support."""


def main():
    print(f"[consensus_explainer] watching {Z_LOG} and {R1_LOG}, model={MODEL}", flush=True)

    z_pos = os.path.getsize(Z_LOG) if os.path.exists(Z_LOG) else 0
    r1_pos = os.path.getsize(R1_LOG) if os.path.exists(R1_LOG) else 0

    z_context_buf = []
    r1_context_buf = []

    z_hit = 0
    z_clear = 0
    z_flagged = False
    last_z_query_t = 0.0
    last_z_query_val = None
    r1_low_count = 0
    r1_high_count = 0
    r1_disagreeing = False

    while True:
        z_lines, z_pos = tail_new(Z_LOG, z_pos)
        for line in z_lines:
            line = line.rstrip("\n")
            m = Z_LINE_RE.search(line)
            if not m:
                continue
            per_source = m.group(2)
            # "market" excluded from the trigger gate 2026-08-15 -- real
            # numbers confirmed it's the dominant driver of Z-SPIKE
            # triggers (mean|z|=1.30 vs mint's 0.75/netlat's 0.86, spikes
            # 17.6% of ticks vs 7.6%/4.4%), and it's not being used for
            # anything real. Still logged in z_context_buf/the prompt for
            # context if a real mint/netlat spike fires -- only excluded
            # from deciding WHETHER to fire, not hidden from the LLM.
            trigger_z = max((float(x.split("=")[1]) for x in per_source.split()
                              if not x.startswith("market=")), default=0.0)
            z_context_buf.append(line)
            z_context_buf = z_context_buf[-10:]

            if trigger_z >= Z_ENTER:
                z_hit += 1
                z_clear = 0
            elif trigger_z < Z_EXIT:
                z_clear += 1
                z_hit = 0
            else:
                z_hit = 0  # in the gap: neither building toward a flag nor clearing

            novel = (last_z_query_val is None
                     or abs(trigger_z - last_z_query_val) >= Z_DELTA_MIN)

            if (not z_flagged and z_hit >= Z_SUSTAIN_TICKS
                    and time.time() - last_z_query_t >= Z_COOLDOWN_S):
                z_flagged = True
                if not novel:
                    print(f"[consensus_explainer] Z-SPIKE flagged but not novel "
                          f"(|{trigger_z:.2f} - {last_z_query_val:.2f}| < {Z_DELTA_MIN}), "
                          f"skipping Groq: {line}", flush=True)
                else:
                    last_z_query_t = time.time()
                    last_z_query_val = trigger_z
                    print(f"[consensus_explainer] Z-SPIKE trigger: {line}", flush=True)
                    try:
                        sources_over = [x.split("=")[0] for x in per_source.split()
                                        if not x.startswith("market=")
                                        and float(x.split("=")[1]) >= Z_ENTER]
                        dominant = sources_over[0] if sources_over else "unknown"
                        state_hash = f"zspike:{dominant}:{_z_bucket(trigger_z)}"
                        prompt = build_prompt(
                            "sustained individual source spike (rolling z-score)",
                            "\n".join(z_context_buf), "\n".join(r1_context_buf) or "(none yet)")
                        print(f"[consensus_explainer] explanation: "
                              f"{query_llm(prompt, state_hash=state_hash)}", flush=True)
                    except QuotaExceeded as e:
                        print(f"[consensus_explainer] skipping -- {e}", flush=True)
                    except Exception as e:
                        print(f"[consensus_explainer] ERROR querying LLM: {e}", flush=True)
            elif z_flagged and z_clear >= Z_SUSTAIN_TICKS:
                z_flagged = False
                z_hit = 0

        r1_lines, r1_pos = tail_new(R1_LOG, r1_pos)
        for line in r1_lines:
            line = line.rstrip("\n")
            m = R1_LINE_RE.search(line)
            if not m or not m.group(1):
                continue
            r1 = float(m.group(2))
            r1_context_buf.append(line)
            r1_context_buf = r1_context_buf[-10:]

            if r1 < R1_LOW:
                r1_low_count += 1
                r1_high_count = 0
            elif r1 >= R1_HIGH:
                r1_high_count += 1
                r1_low_count = 0
            else:
                r1_low_count = 0
                r1_high_count = 0

            if not r1_disagreeing and r1_low_count >= R1_SUSTAIN_TICKS:
                r1_disagreeing = True
                print(f"[consensus_explainer] PHASE-DISAGREEMENT trigger: {line}", flush=True)
                try:
                    state_hash = f"phasedis:{_r1_bucket(r1)}"
                    prompt = build_prompt(
                        "sustained cross-source phase disagreement (low r1)",
                        "\n".join(z_context_buf) or "(none yet)", "\n".join(r1_context_buf))
                    print(f"[consensus_explainer] explanation: "
                          f"{query_llm(prompt, state_hash=state_hash)}", flush=True)
                except QuotaExceeded as e:
                    print(f"[consensus_explainer] skipping -- {e}", flush=True)
                except Exception as e:
                    print(f"[consensus_explainer] ERROR querying LLM: {e}", flush=True)
            elif r1_disagreeing and r1_high_count >= R1_SUSTAIN_TICKS:
                r1_disagreeing = False
                print(f"[consensus_explainer] phase agreement restored: {line}", flush=True)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
