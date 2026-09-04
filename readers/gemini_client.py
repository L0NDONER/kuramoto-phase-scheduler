"""gemini_client.py — Gemini Flash Lite wiring, same interface as
groq_client.py (query_llm, QuotaExceeded, sqlite cache), so ask.py can
swap backends with a one-line import change. Added 2026-09-01: Groq's
free-tier daily request cap was limiting personal ask.py/ask_web.py
usage; Gemini Flash Lite has a much larger free daily quota and is
already one of this repo's three default model choices.

Model pinned to the "gemini-flash-lite-latest" alias (confirmed live
2026-09-01 -- resolves to gemini-3.5-flash-lite today, modelVersion in
the response confirms it) rather than a dated model string, so this
tracks Google's own "latest" designation instead of needing a manual
bump every time they ship a new flash-lite generation.

Separate cache db (gemini_cache.db) from groq_client.py's -- same
state_hash-bucketed opt-in scheme, kept as two files rather than one
shared table so a Groq-vs-Gemini answer for the same state_hash can
never silently collide.

Quota backoff: Gemini's 429 (RESOURCE_EXHAUSTED) body includes a
RetryInfo detail with a "retryDelay" field like "34s" when the API
provides one; when it doesn't, falls back to a flat 60s wait rather
than hammering immediately -- same "one real backoff, not retry-every-
few-seconds" reasoning as groq_client.py's TPD handling.
"""
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

MODEL = "gemini-flash-lite-latest"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

_RETRY_DELAY_RE = re.compile(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"')

CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_cache.db")
DEFAULT_CACHE_TTL_S = 600

_quota_resume_at = 0.0


class QuotaExceeded(Exception):
    def __init__(self, retry_after_s):
        self.retry_after_s = retry_after_s
        super().__init__(f"Gemini quota exceeded, retry in {retry_after_s:.0f}s")


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

    url = GEMINI_URL.format(model=model, key=GEMINI_API_KEY)
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 429:
            m = _RETRY_DELAY_RE.search(body)
            retry_after_s = float(m.group(1)) if m else 60.0
            _quota_resume_at = time.time() + retry_after_s
            raise QuotaExceeded(retry_after_s) from None
        raise
    answer = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    _cache_put(state_hash, answer)
    return answer
