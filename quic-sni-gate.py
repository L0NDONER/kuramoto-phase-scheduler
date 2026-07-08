#!/usr/bin/env python3
"""
QUIC v1 Initial SNI gate — NFQUEUE.

Receives NEW UDP packets via NFQUEUE, decrypts the QUIC Initial packet
using RFC 9001 Initial keys (derived from DCID), extracts SNI from the
TLS ClientHello in the CRYPTO frame, then:
  - SNI_MARKS match  → accept, set the associated fw-mark
  - no match         → drop  (client falls back to TCP/443, handled by xt_string)
  - non-Initial QUIC or parse failure → accept unchanged

The mark is saved to conntrack by a CONNMARK rule in shaper-setup.sh,
so Short Header packets later in the flow inherit it without re-inspection.
"""
import struct
import re
import logging
import sys

from netfilterqueue import NetfilterQueue
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('quic-sni')

# ── CONFIG — the only things you change per-device ────────────────────────────
NFQUEUE_NUM = 1            # must match NFQUEUE_NUM in shaper-setup.sh

# Ordered SNI routing table: first match wins.  No match → drop.
SNI_MARKS = [
    (re.compile(r'(?:^|\.)googlevideo\.com$',     re.I), 10),  # YouTube video CDN → uncapped
    (re.compile(r'doubleclick',                      re.I), 11),  # doubleclick_ads_rule
    (re.compile(r'googlesyndication',                re.I), 11),  # googlesyndication_ads_rule
    (re.compile(r'youtubei',                         re.I), 11),  # youtubei_ads_rule
    (re.compile(r'yt3\.ggpht',                      re.I), 11),  # yt3_metadata_rule
    (re.compile(r'googleadservices',                 re.I), 11),  # adservice_rule
]
# ─────────────────────────────────────────────────────────────────────────────

# RFC 9001 §5.2 — QUIC v1 Initial salt (constant, do not change)
INITIAL_SALT = bytes.fromhex('38762cf7f55934b34d179ae6a4c80cadccbb7f0a')

_BACKEND = default_backend()


# ── Key derivation ────────────────────────────────────────────────────────────

def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    h = HMAC(salt, hashes.SHA256(), backend=_BACKEND)
    h.update(ikm)
    return h.finalize()


def _hkdf_expand_label(secret: bytes, label: str, length: int) -> bytes:
    full = b'tls13 ' + label.encode()
    info = struct.pack('>H', length) + bytes([len(full)]) + full + b'\x00'
    return HKDFExpand(hashes.SHA256(), length, info, _BACKEND).derive(secret)


def _derive_keys(dcid: bytes):
    init = _hkdf_extract(INITIAL_SALT, dcid)
    cli  = _hkdf_expand_label(init, 'client in', 32)
    key  = _hkdf_expand_label(cli,  'quic key',  16)
    iv   = _hkdf_expand_label(cli,  'quic iv',   12)
    hp   = _hkdf_expand_label(cli,  'quic hp',   16)
    return key, iv, hp


# ── QUIC helpers ──────────────────────────────────────────────────────────────

def _varint(buf: bytes, off: int):
    """Decode QUIC variable-length integer; return (value, bytes_consumed)."""
    prefix = (buf[off] >> 6) & 0x3
    if prefix == 0: return buf[off] & 0x3f,                                    1
    if prefix == 1: return struct.unpack_from('>H', buf, off)[0] & 0x3fff,     2
    if prefix == 2: return struct.unpack_from('>I', buf, off)[0] & 0x3fffffff, 4
    return          struct.unpack_from('>Q', buf, off)[0] & 0x3fffffffffffffff, 8


def _remove_hp(raw: bytearray, pn_off: int, hp_key: bytes):
    """Remove header protection in-place; return (packet_number, pn_length)."""
    sample = bytes(raw[pn_off + 4: pn_off + 20])
    enc = Cipher(algorithms.AES(hp_key), modes.ECB(), backend=_BACKEND).encryptor()
    mask = enc.update(sample)
    raw[0] ^= mask[0] & 0x0f               # long header: low 4 bits of first byte
    pn_len = (raw[0] & 0x03) + 1
    pn = 0
    for i in range(pn_len):
        raw[pn_off + i] ^= mask[1 + i]
        pn = (pn << 8) | raw[pn_off + i]
    return pn, pn_len


# ── TLS SNI extraction ────────────────────────────────────────────────────────

def _sni_from_tls(data: bytes):
    """Parse TLS ClientHello bytes and return the SNI hostname, or None."""
    off = 0
    if len(data) < 4:
        return None
    if data[0] == 0x16 and len(data) > 5:  # TLS record wrapper
        off = 5
    if off >= len(data) or data[off] != 0x01:   # must be ClientHello
        return None
    off += 4                                     # type(1) + length(3)
    off += 34                                    # ProtocolVersion(2) + Random(32)
    if off >= len(data): return None
    off += 1 + data[off]                         # session_id
    if off + 2 > len(data): return None
    off += 2 + struct.unpack_from('>H', data, off)[0]   # cipher_suites
    if off >= len(data): return None
    off += 1 + data[off]                         # compression_methods
    if off + 2 > len(data): return None
    ext_end = off + 2 + struct.unpack_from('>H', data, off)[0]
    off += 2
    while off + 4 <= ext_end and off + 4 <= len(data):
        etype = struct.unpack_from('>H', data, off)[0]; off += 2
        elen  = struct.unpack_from('>H', data, off)[0]; off += 2
        if etype == 0x0000:                      # server_name extension
            inner = data[off: off + elen]
            idx = 2                              # skip list_len
            if idx + 3 <= len(inner) and inner[idx] == 0:   # host_name
                nlen = struct.unpack_from('>H', inner, idx + 1)[0]
                return inner[idx + 3: idx + 3 + nlen].decode('ascii', errors='ignore')
        off += elen
    return None


def _sni_from_frames(payload: bytes):
    """Walk QUIC frames to find the CRYPTO frame, then extract SNI."""
    off = 0
    while off < len(payload):
        ft = payload[off]; off += 1
        if ft == 0x00:   # PADDING
            continue
        if ft == 0x01:   # PING
            continue
        if ft == 0x06:   # CRYPTO — contains TLS ClientHello
            _data_offset, n = _varint(payload, off); off += n
            data_len,     n = _varint(payload, off); off += n
            return _sni_from_tls(payload[off: off + data_len])
        return None      # ACK, or anything unexpected — bail
    return None


# ── Main QUIC Initial parser ──────────────────────────────────────────────────

def _extract_sni(udp_payload: bytes):
    """
    Attempt to decrypt a QUIC v1 Initial packet and return its SNI.
    Returns None if the packet isn't a QUIC Initial or decryption fails.
    """
    # RFC 9000 §14.1: Initial packets must be padded to ≥1200 bytes
    if len(udp_payload) < 1200:
        return None

    first = udp_payload[0]
    if not (first & 0x80): return None          # Short Header — not Initial
    if not (first & 0x40): return None          # fixed bit must be 1
    if (first >> 4) & 0x03 != 0x00: return None # Long Header type != Initial

    try:
        off = 1
        version = struct.unpack_from('>I', udp_payload, off)[0]; off += 4
        if version != 1: return None                # only QUIC v1

        dcil = udp_payload[off]; off += 1
        dcid = udp_payload[off: off + dcil]; off += dcil
        scil = udp_payload[off]; off += 1; off += scil

        token_len, n = _varint(udp_payload, off); off += n + token_len
        pkt_len,   n = _varint(udp_payload, off); off += n
        pn_off = off
    except (IndexError, struct.error):
        return None   # truncated or malformed header — not a valid Initial

    try:
        key, iv, hp = _derive_keys(dcid)
    except Exception:
        return None

    raw = bytearray(udp_payload)
    try:
        pn, pn_len = _remove_hp(raw, pn_off, hp)
    except Exception:
        return None

    payload_off = pn_off + pn_len
    ciphertext  = bytes(raw[payload_off: pn_off + pkt_len])

    nonce = bytearray(iv)
    for i, b in enumerate(pn.to_bytes(12, 'big')):
        nonce[i] ^= b

    aad = bytes(raw[:payload_off])

    try:
        plaintext = AESGCM(key).decrypt(bytes(nonce), ciphertext, aad)
    except Exception:
        return None

    return _sni_from_frames(plaintext)


# ── NFQUEUE callback ──────────────────────────────────────────────────────────

def _udp_payload(raw: bytes):
    if (raw[0] >> 4) != 4: return None          # IPv4 only
    ihl = (raw[0] & 0x0f) * 4
    if raw[9] != 17: return None                # must be UDP
    return raw[ihl + 8:]


def callback(packet):
    try:
        raw         = packet.get_payload()
        udp_payload = _udp_payload(raw)
        if udp_payload is None:
            packet.accept(); return

        sni = _extract_sni(bytes(udp_payload))
        if sni is None:
            # Not a parseable QUIC Initial — pass through, IP-based rules apply
            packet.accept(); return

        mark = next((m for pat, m in SNI_MARKS if pat.search(sni)), None)
        if mark is not None:
            log.info('QUIC ALLOW  mark=%d  sni=%s', mark, sni)
            packet.set_mark(mark)
            packet.accept()
        else:
            log.info('QUIC DROP          sni=%s', sni)
            packet.drop()

    except Exception as exc:
        log.error('callback error: %s', exc)
        packet.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    nfq = NetfilterQueue()
    nfq.bind(NFQUEUE_NUM, callback)
    log.info('QUIC SNI gate listening on NFQUEUE %d', NFQUEUE_NUM)
    try:
        nfq.run()
    except KeyboardInterrupt:
        pass
    finally:
        nfq.unbind()
