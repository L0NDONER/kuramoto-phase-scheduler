"""groq_client.py — shared Groq wiring for every LLM consumer in this
repo (ask.py, ask_node.py, consensus_explainer.py,
deluge_watch/anomaly_explainer.py). One place for the model name and
the quota-backoff logic instead of four copies drifting apart.

Switched llama-3.3-70b-versatile -> openai/gpt-oss-120b 2026-08-14
(Groq decommissioning llama-3.3-70b-versatile 2026-08-16, per their own
notice). Tested against llama-3.1-8b-instant first since it's not a
reasoning model (faster, cheaper, no hidden-thinking-token overhead) --
rejected because it mislabeled a "process"-tagged sustained-deviation
entry as "network" and used that to justify a network-latency fix that
wasn't supported, exactly the trap the prompt was tightened to catch.
gpt-oss-120b passed the same trap correctly. Real cost of that
correctness: reasoning_tokens showed up in its usage (~135 on a typical
query) and its free-tier per-minute bucket is smaller than
llama-3.3-70b-versatile's was (8000 tokens/min vs 12000).

Quota backoff: Groq's TPD (tokens-per-day) 429 error body includes a
"Please try again in <N>m<N>s" string -- confirmed live 2026-08-14
against llama-3.3-70b-versatile hitting its 100k/day cap. Parsing that
and refusing to even attempt another call until it elapses turns
"retry every ~2 minutes and 429 every time" into one real backoff.
gpt-oss-120b's own daily cap isn't known yet (Groq doesn't expose TPD
until you actually hit it) -- this same parsing handles whatever number
it turns out to be, no hardcoded threshold needed.

Persistent cache, added 2026-08-16: a real, previously-unnamed cause of
repeat token spend -- these consumer processes get restarted often
during active development (path fixes, model migrations), and their
own in-memory novelty/cooldown state (e.g. consensus_explainer.py's
last_z_query_val) resets to empty on every restart, losing "we just
explained this" memory and letting a near-identical situation re-fire
immediately. A sqlite file (stdlib, no new service -- checked live,
Redis isn't even installed here, and one machine with low write volume
doesn't need it) survives restarts, so the memory survives too.
Callers opt in with a `state_hash` -- a deliberately BUCKETED summary
of the situation (e.g. "z-spike:market:high"), not raw floats, which
change every tick and would never repeat. No state_hash (the default)
means no caching, exactly today's behaviour -- ask.py's free-text
questions have no natural bucket, so they're unaffected. Cache checks
run before the quota-backoff check, on purpose: a cache hit costs
nothing and should still work during a quota-exhausted window.
"""
import json
import os
import re
import sqlite3
import time
import urllib.request

MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

_RETRY_RE = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s")

CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groq_cache.db")
DEFAULT_CACHE_TTL_S = 600   # 10 min -- long enough to absorb restart-triggered
                             # re-fires and near-simultaneous repeats, short
                             # enough that a genuinely new situation still
                             # gets a fresh explanation promptly

# Process-wide: once a TPD-exhausted 429 is seen, don't even attempt
# another call until this wall-clock time. Not persisted across
# restarts -- each consumer is long-running, a restart is rare enough
# that re-discovering the wait via one more 429 is an acceptable cost.
_quota_resume_at = 0.0


class QuotaExceeded(Exception):
    def __init__(self, retry_after_s):
        self.retry_after_s = retry_after_s
        super().__init__(f"Groq quota exceeded, retry in {retry_after_s:.0f}s")


def quota_resume_at():
    return _quota_resume_at


def _cache_conn():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS cache "
                 "(state_hash TEXT PRIMARY KEY, response TEXT, cached_at REAL)")
    return conn


def _cache_get(state_hash, ttl_s):
    if state_hash is None:
        return None
    conn = _cache_conn()
    row = conn.execute("SELECT response, cached_at FROM cache WHERE state_hash=?",
                        (state_hash,)).fetchone()
    conn.close()
    if row and (time.time() - row[1]) < ttl_s:
        return row[0]
    return None


def _cache_put(state_hash, response):
    if state_hash is None:
        return
    conn = _cache_conn()
    conn.execute("INSERT OR REPLACE INTO cache (state_hash, response, cached_at) VALUES (?, ?, ?)",
                 (state_hash, response, time.time()))
    conn.commit()
    conn.close()


def query_llm(prompt, model=MODEL, timeout=30, user_agent="claude-substrate/1.0",
              state_hash=None, cache_ttl_s=DEFAULT_CACHE_TTL_S):
    cached = _cache_get(state_hash, cache_ttl_s)
    if cached is not None:
        return cached

    global _quota_resume_at

    now = time.time()
    if now < _quota_resume_at:
        raise QuotaExceeded(_quota_resume_at - now)

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(GROQ_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
        # Groq's Cloudflare front blocks urllib's default User-Agent as
        # bot traffic (403) -- confirmed live 2026-08-14.
        "User-Agent": user_agent,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 429:
            m = _RETRY_RE.search(body)
            retry_after_s = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 60.0
            _quota_resume_at = time.time() + retry_after_s
            raise QuotaExceeded(retry_after_s) from None
        raise
    answer = result["choices"][0]["message"]["content"].strip()
    _cache_put(state_hash, answer)
    return answer
