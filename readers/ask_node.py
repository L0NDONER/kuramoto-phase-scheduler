"""ask_node.py — "how's pi1 doing?" Same idea as ask.py, pointed at a
remote node instead of local logs: fire a real status command over the
existing shell_launch channel (signed, WireGuard-only, no SSH -- see
shell_launch/shell_launch_common.py), hand the real output to Groq,
print a plain-English answer. Decides nothing, no actuation, same
boundary as every other consumer in this repo.

Node registry confirmed live 2026-08-14 against the actual wg-easy hub
peer list (not guessed): pi1=10.8.0.7 (live, recent handshake),
pi2=10.8.0.5 (was 2 days stale at confirmation time). pi2's port is
NOT independently confirmed -- assumed 58701 by convention (matches
pi1's and shell_launch_send.py's own docstring example) since pi2 was
unreachable at the WireGuard layer when this was built, so there was
nothing to test against. If pi2 comes back up and 58701 is wrong,
sending to it will just time out like any other unreachable case.
"""
import os
import shlex
import sys

# ask_node.py lives in cascade_pll/readers/ -- REPO (where channels/
# actually lives) is one dir up; OWN_DIR (same dir as ask.py,
# groq_client.py) stays this file's own directory. Two different
# things after the 2026-08-15 reorg, was one and the same before it.
OWN_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(OWN_DIR)
sys.path.insert(0, OWN_DIR)
sys.path.insert(0, os.path.join(REPO, "channels", "shell_launch"))

from shell_launch_common import load_key, send_run, PROGRESS, DONE_OK, DONE_FAIL
from ask import query_llm

NODES = {
    "pi1": dict(host="10.8.0.7", port=58701, key="shell_launch_pi1.key"),
    "pi2": dict(host="10.8.0.5", port=58701, key="shell_launch_pi2.key"),
}

# The listener runs argv via shlex.split(cmd) -- NOT a shell -- so ";"
# and "|" only work wrapped in an explicit bash -c, confirmed live
# 2026-08-14 (a bare semicolon-chained command failed with "no such
# file or directory: uptime;", the whole first token including the
# semicolon getting treated as one literal argv[0]).
_STATUS_INNER = "uptime; echo ---; cat /proc/loadavg; echo ---; free -h | head -2; echo ---; wg show 2>/dev/null"
STATUS_CMD = "bash -c " + shlex.quote(_STATUS_INNER)
SEND_TIMEOUT_S = 8.0


def fetch_status(node):
    cfg = NODES[node]
    key = load_key(os.path.join(REPO, "channels", "shell_launch", cfg["key"]))
    output = []

    def on_status(status, sid, detail, elapsed_s):
        if status in (PROGRESS, DONE_OK, DONE_FAIL):
            output.append(detail)

    session_id = send_run(cfg["host"], cfg["port"], STATUS_CMD, "", key,
                           on_status, initial_timeout_s=SEND_TIMEOUT_S)
    if session_id is None:
        return None
    return "\n".join(output).strip()


def build_prompt(node, raw_output, question):
    return f"""You are reading the real, just-fetched output of a status command
(uptime, load average, memory, WireGuard peer info) run moments ago on
a remote Linux node called "{node}", reached over a signed WireGuard
channel (not SSH).

Raw command output:
{raw_output}

Question: {question}

Answer in plain English, 2-4 sentences, based only on this output. If
something isn't in the data, say so rather than guessing. Do not
speculate about causes not supported by the numbers."""


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in NODES:
        sys.exit(f"usage: {sys.argv[0]} <{'|'.join(NODES)}> [question]")

    node = sys.argv[1]
    question = " ".join(sys.argv[2:]) or f"How's {node} doing?"

    raw = fetch_status(node)
    if raw is None:
        print(f"{node} is unreachable over shell_launch right now "
              f"(no response within {SEND_TIMEOUT_S:.0f}s) -- can't answer that.")
        return

    print(query_llm(build_prompt(node, raw, question)))


if __name__ == "__main__":
    main()
