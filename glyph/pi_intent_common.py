#!/usr/bin/env python3
"""
glyph/pi_intent_common.py — shared packet format for signed intents sent
to a Pi over its WireGuard-only listener (pi_intent_listener.py).

Same wire format as ec2_intent_common.py, separate key: this is a
different trust domain (Pi root access) from the EC2 intent channel, so
a leaked EC2 key must not grant Pi control and vice versa.

Packet layout (all network byte order):
    magic(H) intent_len(B) intent(intent_len bytes, ascii)
    trigger_at(d, unix epoch seconds) nonce(Q) sig(32 bytes, HMAC-SHA256)

sig covers everything before it (magic..nonce). Freshness/replay defence
is the caller's job (see pi_intent_listener.py): reject stale trigger_at
and reject reused nonces.
"""
import hashlib
import hmac
import os
import pathlib
import struct

MAGIC = 0x91A2
RESP_MAGIC = 0x91A3
# PI_INTENT_KEY_FILE lets each target host use its own key (separate trust
# domain per host -- see module docstring), while keeping the old bare
# "pi_intent.key" default for existing single-host callers.
KEY_PATH = pathlib.Path(__file__).resolve().parent.parent / os.environ.get(
    "PI_INTENT_KEY_FILE", "pi_intent.key")

HEADER_FMT = "!HB"          # magic, intent_len
TAIL_FMT   = "!dQ"          # trigger_at, nonce
SIG_LEN    = 32

# response status codes
ACCEPTED = 1   # signature ok, scheduled, waiting for trigger_at
REJECTED = 2   # signature ok, but bad intent/stale trigger/replay
DONE_OK  = 3   # executed, exit code 0
DONE_FAIL = 4  # executed, nonzero exit code

RESP_HEADER_FMT = "!HBB"    # magic, status, detail_len
RESP_TAIL_FMT   = "!Q"      # nonce (echoes the request's nonce)


def pack_response(nonce: int, status: int, detail: str, key: bytes) -> bytes:
    detail_b = detail.encode("ascii", "replace")[:200]
    body = struct.pack(RESP_HEADER_FMT, RESP_MAGIC, status, len(detail_b)) + detail_b \
         + struct.pack(RESP_TAIL_FMT, nonce)
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return body + sig


def unpack_response(data: bytes, key: bytes):
    """Returns (status, detail, nonce) or raises ValueError."""
    off0 = struct.calcsize(RESP_HEADER_FMT)
    if len(data) < off0:
        raise ValueError("short response")
    magic, status, dlen = struct.unpack_from(RESP_HEADER_FMT, data, 0)
    if magic != RESP_MAGIC:
        raise ValueError("bad response magic")
    off = off0
    detail = data[off:off + dlen].decode("ascii", "replace")
    off += dlen
    (nonce,) = struct.unpack_from(RESP_TAIL_FMT, data, off)
    off += struct.calcsize(RESP_TAIL_FMT)
    sig = data[off:off + SIG_LEN]
    body = data[:off]
    expect = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("bad response signature")
    return status, detail, nonce


def load_key() -> bytes:
    return bytes.fromhex(KEY_PATH.read_text().strip())


def pack(intent: str, trigger_at: float, nonce: int, key: bytes) -> bytes:
    intent_b = intent.encode("ascii")
    body = struct.pack(HEADER_FMT, MAGIC, len(intent_b)) + intent_b \
         + struct.pack(TAIL_FMT, trigger_at, nonce)
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return body + sig


def unpack(data: bytes, key: bytes):
    """Returns (intent, trigger_at, nonce) or raises ValueError."""
    if len(data) < 3:
        raise ValueError("short packet")
    magic, ilen = struct.unpack_from(HEADER_FMT, data, 0)
    if magic != MAGIC:
        raise ValueError("bad magic")
    off = struct.calcsize(HEADER_FMT)
    intent = data[off:off + ilen].decode("ascii")
    off += ilen
    trigger_at, nonce = struct.unpack_from(TAIL_FMT, data, off)
    off += struct.calcsize(TAIL_FMT)
    sig = data[off:off + SIG_LEN]
    body = data[:off]
    expect = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise ValueError("bad signature")
    return intent, trigger_at, nonce
