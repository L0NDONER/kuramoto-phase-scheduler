"""shell_launch_common.py — wire format for an unprivileged, real-time
shell-launch channel over WireGuard. Same HMAC-signed/nonce-replay-
protected shape as pi_intent_common.py/ec2_intent_common.py, but a
DELIBERATELY SEPARATE trust domain and port -- this runs arbitrary
scripts as the normal user (no sudo, no root, no sudoers involvement),
which is a broader surface than the fixed-intent whitelist those
channels use, so it gets its own per-host key
(shell_launch_<host>.key) rather than reusing pi1_intent.key etc.

Built to replace SSH for test/dev orchestration on this substrate --
SSH's variable connection/handshake latency was directly responsible
for a real test failure (fractal_layer1 3-node test, 2026-08-10):
launches drifted out of sync with the aggregator's window because each
ssh call's setup time was unpredictable. This channel is fire-and-track
over the same low-jitter WireGuard mesh the rest of the substrate
already trusts, with periodic real-time progress instead of one
blocking response.

Two request types:
  RUN(cmd, cwd)   -> ACCEPTED(session_id, pid), then PROGRESS(...)
                     every ~0.5s while running, then DONE_OK/DONE_FAIL
                     with the final exit code and last output chunk.
  KILL(session_id) -> DONE_OK/REJECTED (no such session)

Packet layout (all network byte order):
  Request:  magic(H) action(B) [RUN: cmd_len(H) cmd cwd_len(B) cwd]
                                [KILL: session_id(Q)]
            nonce(Q) resp_port(H) sig(32 bytes, HMAC-SHA256)
  Response: magic(H) status(B) session_id(Q) detail_len(H) detail
            nonce(Q) sig(32 bytes)

sig covers everything before it. Freshness/replay defence (reject
stale/reused nonces) is the listener's job, same as the other channels.
"""
import hashlib
import hmac
import os
import pathlib
import struct

MAGIC = 0x5348       # "SH"
RESP_MAGIC = 0x5349  # "SI"

ACTION_RUN = 1
ACTION_KILL = 2

# response status codes
ACCEPTED = 1
REJECTED = 2
PROGRESS = 3
DONE_OK = 4
DONE_FAIL = 5

REQ_HEADER_FMT = "!HB"      # magic, action
REQ_TAIL_FMT = "!QH"        # nonce, resp_port
RESP_HEADER_FMT = "!HBQH"   # magic, status, session_id, detail_len
RESP_TAIL_FMT = "!Q"        # nonce
SIG_LEN = 32

KEY_PATH = pathlib.Path(__file__).resolve().parent / os.environ.get(
    "SHELL_LAUNCH_KEY_FILE", "shell_launch.key")

MAX_DETAIL = 900   # keeps total packet comfortably under typical 1472 MTU


def load_key(path=None) -> bytes:
    """path overrides KEY_PATH for this call specifically -- needed by
    any process that talks to more than one keyed host at once (e.g.
    fractal_layer1/run_all.py, one thread per host). KEY_PATH itself is
    a module-level constant fixed at whichever import happens first
    (Python caches the module), so mutating SHELL_LAUNCH_KEY_FILE in
    os.environ per-thread and re-importing does NOT re-evaluate it --
    confirmed live 2026-08-10: both pi1 and pi2 threads silently ended
    up signing with the same (first-imported) key, so the mismatched
    one got rejected with no response, not a timing fluke."""
    p = pathlib.Path(path) if path is not None else KEY_PATH
    return bytes.fromhex(p.read_text().strip())


def pack_run(cmd: str, cwd: str, nonce: int, resp_port: int, key: bytes) -> bytes:
    cmd_b = cmd.encode("utf-8")
    cwd_b = cwd.encode("utf-8")
    body = (struct.pack(REQ_HEADER_FMT, MAGIC, ACTION_RUN)
             + struct.pack("!H", len(cmd_b)) + cmd_b
             + struct.pack("!B", len(cwd_b)) + cwd_b
             + struct.pack(REQ_TAIL_FMT, nonce, resp_port))
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return body + sig


def pack_kill(session_id: int, nonce: int, resp_port: int, key: bytes) -> bytes:
    body = (struct.pack(REQ_HEADER_FMT, MAGIC, ACTION_KILL)
             + struct.pack("!Q", session_id)
             + struct.pack(REQ_TAIL_FMT, nonce, resp_port))
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return body + sig


def unpack_request(data: bytes, key: bytes):
    """Returns a dict: {action, nonce, resp_port, ...action-specific...}."""
    off0 = struct.calcsize(REQ_HEADER_FMT)
    if len(data) < off0:
        raise ValueError("short packet")
    magic, action = struct.unpack_from(REQ_HEADER_FMT, data, 0)
    if magic != MAGIC:
        raise ValueError("bad magic")

    off = off0
    result = {"action": action}
    if action == ACTION_RUN:
        (cmd_len,) = struct.unpack_from("!H", data, off)
        off += 2
        result["cmd"] = data[off:off + cmd_len].decode("utf-8")
        off += cmd_len
        (cwd_len,) = struct.unpack_from("!B", data, off)
        off += 1
        result["cwd"] = data[off:off + cwd_len].decode("utf-8")
        off += cwd_len
    elif action == ACTION_KILL:
        (result["session_id"],) = struct.unpack_from("!Q", data, off)
        off += 8
    else:
        raise ValueError(f"unknown action {action}")

    nonce, resp_port = struct.unpack_from(REQ_TAIL_FMT, data, off)
    off += struct.calcsize(REQ_TAIL_FMT)
    result["nonce"] = nonce
    result["resp_port"] = resp_port

    sig = data[off:off + SIG_LEN]
    body = data[:off]
    expect = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("bad signature")
    return result


def pack_response(status: int, session_id: int, detail: str, nonce: int, key: bytes) -> bytes:
    detail_b = detail.encode("utf-8", "replace")[:MAX_DETAIL]
    body = (struct.pack(RESP_HEADER_FMT, RESP_MAGIC, status, session_id, len(detail_b))
             + detail_b + struct.pack(RESP_TAIL_FMT, nonce))
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return body + sig


def unpack_response(data: bytes, key: bytes):
    """Returns (status, session_id, detail, nonce) or raises ValueError."""
    off0 = struct.calcsize(RESP_HEADER_FMT)
    if len(data) < off0:
        raise ValueError("short response")
    magic, status, session_id, dlen = struct.unpack_from(RESP_HEADER_FMT, data, 0)
    if magic != RESP_MAGIC:
        raise ValueError("bad response magic")
    off = off0
    detail = data[off:off + dlen].decode("utf-8", "replace")
    off += dlen
    (nonce,) = struct.unpack_from(RESP_TAIL_FMT, data, off)
    off += struct.calcsize(RESP_TAIL_FMT)
    sig = data[off:off + SIG_LEN]
    body = data[:off]
    expect = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("bad response signature")
    return status, session_id, detail, nonce
