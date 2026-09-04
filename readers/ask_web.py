"""ask_web.py — the first non-terminal way to reach the substrate: a
minimal web wrapper around ask.py's existing logic, reachable from
localhost and from Mint's own WireGuard interface (phone-over-WG).
Deliberately not a new tool, not a new trust surface -- reuses
build_prompt/build_trend_prompt/query_llm exactly as ask.py already
does, same ghost-reading, decide-nothing boundary. Only new part is the
transport (HTTP instead of a terminal invocation).

Scope, confirmed explicitly 2026-08-16, and re-confirmed after a
public-internet proposal was deliberately rejected: localhost + WG-phone
are the FULL scope of this file. Public internet is explicitly deferred
until three real prerequisites exist -- auth, rate limiting, and a
deliberate decision about which telemetry is admissible to expose --
none of which exist yet. This binds ONLY to 127.0.0.1 and Mint's own
WireGuard interface IP (BIND_HOSTS below) -- deliberately NOT 0.0.0.0,
which would also expose it on Mint's real LAN interface, broader than
this scope. Only devices already inside the WG mesh (the same trust
boundary as every other channel in this repo) can reach it.

stdlib only (http.server), same discipline as groq_client.py's plain
urllib -- no new pip dependency for something this small.

/status.json is a real-time ghost-freshness ticker: server-anchored,
zero trust in the viewing device's clock -- no Date() call anywhere in
the page's JS. The server computes every age string itself (using its
own clock, verified against EC2 via mesh_healer.py's mint_clock_drift
job) on every poll; the browser only ever displays what it's told. This
endpoint deliberately never calls Groq -- it's polled every few seconds,
and burning a Groq request per poll would be wasteful and eat directly
into the same daily quota the LLM consumers share.

Run: python3 ask_web.py
Then open http://127.0.0.1:8420/ in a browser on this machine.
"""
import html
import http.server
import json
import os
import sys
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ask

# Localhost + Mint's own WireGuard interface IP ONLY -- deliberately
# NOT 0.0.0.0, which would also bind Mint's real LAN interface
# (10.0.0.x), broader than the confirmed scope (localhost + WG-phone).
# 10.8.0.4 is Mint's wg-irssi identity, confirmed live 2026-08-16.
BIND_HOSTS = ["127.0.0.1", "10.8.0.4"]
PORT = 8420
STATUS_POLL_MS = 3000

PAGE = """<!doctype html>
<html><head><title>ask the substrate</title>
<style>body{{font-family:monospace;max-width:700px;margin:2em auto;padding:0 1em}}
textarea{{width:100%;box-sizing:border-box}}
.answer{{white-space:pre-wrap;border:1px solid #ccc;padding:1em;margin-top:1em}}
.status{{white-space:pre-wrap;border:1px solid #ccc;padding:0.75em;font-size:0.9em;color:#555;margin-bottom:1em}}
.stale{{color:#a60}}
</style></head>
<body>
<h3>ask the substrate</h3>
<div class="status" id="status">(loading status...)</div>
<form method="get" action="/ask">
<input type="text" name="q" size="60" placeholder="what's currently happening?" value="{q}">
<label><input type="checkbox" name="trend" {trend_checked}> trend (windowed history, not a snapshot)</label>
<button type="submit">ask</button>
</form>
{answer_block}
<script>
// Server-anchored ticking, zero trust in the viewing device's clock --
// no Date() call anywhere in this script. Every string shown here is
// computed by the server, using its own (EC2-verified) clock, on every
// poll. The browser only ever displays what it was told, never
// computes an age itself from a local timestamp.
async function pollStatus() {{
  try {{
    const resp = await fetch('/status.json');
    const data = await resp.json();
    document.getElementById('status').textContent = data.text;
  }} catch (e) {{
    document.getElementById('status').textContent = '(status poll failed: ' + e + ')';
  }}
}}
pollStatus();
setInterval(pollStatus, {poll_ms});
</script>
</body></html>"""


def status_text():
    """Real ghost-freshness snapshot, no Groq call -- pure file reads,
    safe to poll every few seconds. Reuses ask.py's own functions so
    there's exactly one definition of "how fresh is X," not a second
    one drifting out of sync with the LLM-facing prompts."""
    layer1_path = os.path.join(ask.REPO, "layer1/layer1.log")
    s_line = next((l for l in reversed(ask.tail_line(layer1_path, n=200).splitlines())
        if "goal_field" in l), "(no S reading yet)")
    mesh_summary, mesh_age = ask.mesh_health_summary()
    return (f"S: {s_line}  (updated {ask.age_str(layer1_path)})\n"
            f"mesh: {mesh_summary}  (updated {mesh_age})")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[ask_web] {self.address_string()} {fmt % args}", flush=True)

    def _send_html(self, body, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, obj):
        encoded = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/status.json":
            self._send_json({"text": status_text()})
            return

        params = urllib.parse.parse_qs(parsed.query)
        q = params.get("q", [""])[0]
        trend = "trend" in params

        if parsed.path == "/" and not q:
            self._send_html(PAGE.format(q="", trend_checked="", answer_block="",
                                         poll_ms=STATUS_POLL_MS))
            return

        if parsed.path not in ("/", "/ask"):
            self._send_html("<h3>not found</h3>", status=404)
            return

        if not q:
            self._send_html(PAGE.format(q="", trend_checked="", answer_block="",
                                         poll_ms=STATUS_POLL_MS))
            return

        try:
            prompt = ask.build_trend_prompt(q) if trend else ask.build_prompt(q)
            answer = ask.query_llm(prompt)
        except SystemExit as e:
            answer = f"(refused: {e})"
        except Exception as e:
            answer = f"(error: {e})"

        answer_block = f'<div class="answer">{html.escape(answer)}</div>'
        self._send_html(PAGE.format(
            q=html.escape(q), trend_checked="checked" if trend else "",
            answer_block=answer_block, poll_ms=STATUS_POLL_MS))


def main():
    servers = []
    for host in BIND_HOSTS:
        server = http.server.HTTPServer((host, PORT), Handler)
        print(f"[ask_web] listening on http://{host}:{PORT}/", flush=True)
        servers.append(server)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers[1:]]
    for t in threads:
        t.start()
    servers[0].serve_forever()   # main thread runs the first server, blocks here


if __name__ == "__main__":
    main()
