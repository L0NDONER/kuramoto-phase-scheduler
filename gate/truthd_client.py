#!/usr/bin/env python3
"""
glyph/truthd_client.py — gate hook client for truthd (../truthd.c).

Asks the local truthd (a unix socket, same host only) whether a verb
class is allowed at the current quorum tier. Fail-closed: any error
(truthd not running, timeout, malformed reply) is treated as DENY. An
indeterminate answer about who's allowed to act is not a green light --
that's the entire point of this gate, so it can't default open just
because the daemon it depends on is unreachable.

Usage (from an intent listener, right before executing a verb):
    from truthd_client import verb_allowed
    allowed, tier = verb_allowed("LOCAL_HEAL")
    if not allowed:
        ...reject, don't execute...
"""
import socket

SOCK_PATH = "/tmp/truthd.sock"
TIMEOUT_S = 1.0


def verb_allowed(verb_class: str):
    """Returns (allowed: bool, tier_or_reason: str)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_S)
            s.connect(SOCK_PATH)
            s.sendall(f"CHECK {verb_class}\n".encode("ascii"))
            reply = s.recv(64).decode("ascii", "replace").strip()
    except OSError as e:
        return False, f"truthd unreachable: {e}"

    if reply.startswith("ALLOW "):
        return True, reply[len("ALLOW "):]
    if reply.startswith("DENY "):
        return False, reply[len("DENY "):]
    return False, f"malformed truthd reply {reply!r}"


def current_tier():
    """Returns the tier name string, or None if truthd is unreachable/
    malformed -- used by ec2_probe.py to read Mint's own local-triad
    status for the witness report, not for gating decisions."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_S)
            s.connect(SOCK_PATH)
            s.sendall(b"\n")
            reply = s.recv(64).decode("ascii", "replace").strip()
    except OSError:
        return None
    return reply if reply.startswith("TIER_") else None


if __name__ == "__main__":
    import sys
    verb = sys.argv[1] if len(sys.argv) > 1 else "BENIGN_READ"
    allowed, detail = verb_allowed(verb)
    print(f"{verb}: {'ALLOW' if allowed else 'DENY'} ({detail})")
