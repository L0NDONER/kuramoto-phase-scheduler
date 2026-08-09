#!/usr/bin/env python3
"""
glyph/jitter_wan_rx.py — EC2-side receiver for jitter_wan_tx.py.

Binds ONLY to the WireGuard-internal address (10.8.0.1) -- same
"no raw internet exposure" pattern as ec2_intent_listener.py. Timing
patterns alone are not cryptographic proof of anything (anyone who can
reach this port over the tunnel could send the same pattern), so this
is not a replacement for the existing HMAC-signed intent channel --
it's a different transport, gated the same way execution already is:
via truthd's EC2_SELF check before anything runs.

Modes:
    python3 glyph/jitter_wan_rx.py                       decode only, print, no execution
    python3 glyph/jitter_wan_rx.py --execute              decode, then if the text matches
                                                            a known intent, gate through
                                                            truthd and run it
    python3 glyph/jitter_wan_rx.py --threshold 18.0        override THRESH (ms) for this run
    python3 glyph/jitter_wan_rx.py --expect "hello ec2 test"
                                                            score exact bit errors against a
                                                            known sent string, prints
                                                            "bits_wrong=N/M" for trial scripts

After every decode, signs and sends the decoded text back to Mint as an
ack (reuses ec2_intent_common.pack_response -- same HMAC key and format
already used for the ec2_intent request/response channel, not new
crypto) so jitter_wan_tx.py can verify EC2 actually received what was
sent and retry on mismatch/timeout instead of trusting a one-way
transport blind. 9/10 real-WAN trials landed 0 bit errors and 1/10 had
a single bit error (see quartz-substrate notes) -- close enough that a
retry layer, not more threshold tuning, is what actually closes the gap.

Persistent daemon (2026-08-09): loops forever handling one burst at a
time on the same bound socket, rather than exiting after the first
message. Previously single-shot -- found live when a retry's second
attempt had nothing listening because the first attempt's mismatch had
already consumed the one running instance.

Session multiplexing (2026-08-09): every tick packet carries a random
32-bit session_id, not just a sender-local seq. Found live that two
overlapping sends corrupt each other -- seq alone can't disambiguate
two independent senders (each starts its own stream at seq=0), so a
single shared bucket with one global idle timer scrambled both streams
into one garbled decode (confirmed: 2496 + 2304 ticks from two real
overlapping sends merged into one 4798-tick burst that decoded to pure
mush). Now buckets incoming ticks by session_id and finalizes each
session independently on its own 1s idle timer, so concurrent sends no
longer interfere with each other.

Usage: run on EC2, e.g. under systemd or nohup. Ctrl-C / SIGTERM to stop.
"""
import socket, struct, subprocess, sys, time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ec2_intent_common import load_key, pack_response, DONE_OK

BIND_IP = "10.8.0.1"   # WG-internal only
PORT    = 7410
MAGIC   = 0x4A57

MINT_WG_IP  = "10.8.0.4"   # Mint's wg-irssi address
ACK_PORT    = 7412         # transmission-confirmation ack (decoded text echo)
RESULT_PORT = 7413         # post-execution result report (only in --execute mode)

TICKS_PER_BIT = 24
THRESH        = 0.010   # 8ms -- must match jitter_wan_tx.py's JITTER_SIGMA margin

TRUTHD_SOCK = "/tmp/truthd.sock"

INTENTS = {
    "restart_wg_easy": ["sudo", "docker", "restart", "wg-easy"],
    "start_wg_easy":   ["sudo", "docker", "start", "wg-easy"],
    "stop_wg_easy":    ["sudo", "docker", "stop", "wg-easy"],
}


def text_to_bits(text):
    bits = []
    for byte in text.encode("utf-8"):
        bits.extend((byte >> i) & 1 for i in range(7, -1, -1))
    return bits


def bits_to_text(bits):
    byte_vals = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        byte_vals.append(byte)
    return byte_vals.decode("utf-8", errors="replace")


def decode(ticks, n_bits, thresh):
    intervals = [b - a for a, b in zip(ticks, ticks[1:])]
    bits = []
    for i in range(n_bits):
        window = intervals[i * TICKS_PER_BIT:i * TICKS_PER_BIT + TICKS_PER_BIT - 1]
        if not window:
            break
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        std = var ** 0.5
        bits.append(1 if std > thresh else 0)
    return bits


def truthd_check(verb):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(TRUTHD_SOCK)
        s.sendall(f"CHECK {verb}\n".encode())
        resp = s.recv(64).decode().strip()
        s.close()
        return resp
    except OSError as e:
        return f"DENY UNREACHABLE({e})"


def send_signed(nonce, detail, port):
    """Sends a signed pack_response to Mint on the given port. Used for
    both the transmission-confirmation ack (ACK_PORT) and the separate
    post-execution result report (RESULT_PORT) -- same signing, same
    key, deliberately not the same packet: the tx side's retry logic
    depends on the ack always being exactly the decoded text, so exec
    results go out as a second, distinct report instead of overloading
    that field."""
    try:
        key = load_key()
        pkt = pack_response(nonce, DONE_OK, detail, key)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(pkt, (MINT_WG_IP, port))
        sock.close()
        print(f"[jitter-wan-rx] sent -> {MINT_WG_IP}:{port}: {detail!r}", flush=True)
    except OSError as e:
        print(f"[jitter-wan-rx] send to port {port} failed: {e}", flush=True)


def mcast_in():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, PORT))
    return s


def _cli_arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


IDLE_S = 1.0        # per-session idle (WAN margin) = that session's burst is done
POLL_S = 0.2        # how often to check all sessions for idle, while also recv'ing

# Hard caps, independent of idle detection. Without these a session that
# gets fed faster than IDLE_S apart never finalizes at all -- confirmed
# live 2026-08-09: 16 packets sent one every 0.5s (< IDLE_S=1.0) sat in
# memory for the full 8s with zero finalization, only closing out ~1s
# after the sender actually stopped. Longest real message so far
# (restart_wg_easy, 15 chars) is 2880 ticks; both caps sit well above
# any real use and well below "grows forever."
MAX_TICKS_PER_SESSION = 20000
MAX_SESSION_AGE_S     = 60.0


def collect_sessions(s, on_session_done):
    """Runs forever. Buckets incoming ticks by session_id (not a single
    shared list), and calls on_session_done(session_id, ticks) for any
    session that's gone IDLE_S without a new packet -- independently of
    every other session currently in flight. This is what actually fixes
    the overlap bug: two concurrent senders each get their own bucket
    and their own idle clock, so neither's ticks leak into the other's
    decode. Also force-finalizes (and discards from tracking) any
    session that hits MAX_TICKS_PER_SESSION or MAX_SESSION_AGE_S
    regardless of whether it's still actively receiving -- otherwise a
    session fed faster than IDLE_S apart never closes out at all."""
    sessions = {}     # session_id -> [(seq, arrival_time), ...]
    last_seen = {}     # session_id -> arrival_time of most recent packet
    first_seen = {}    # session_id -> arrival_time of first packet

    s.settimeout(POLL_S)
    while True:
        try:
            data, addr = s.recvfrom(64)
            if len(data) >= 10:
                magic, seq, session_id = struct.unpack(">HII", data[:10])
                if magic == MAGIC:
                    now = time.time()
                    sessions.setdefault(session_id, []).append((seq, now))
                    last_seen[session_id] = now
                    first_seen.setdefault(session_id, now)
        except socket.timeout:
            pass   # just a poll tick, not a real timeout -- check idle sessions below

        now = time.time()
        done = [sid for sid, t in last_seen.items()
                if now - t > IDLE_S
                or len(sessions[sid]) > MAX_TICKS_PER_SESSION
                or now - first_seen[sid] > MAX_SESSION_AGE_S]
        for sid in done:
            # (seq, arrival_time) -- real WAN paths reorder packets. decode()
            # assumes send order; sort by the sender's seq before decoding,
            # don't trust arrival order (found live 2026-08-09: raw arrival
            # order produced a completely garbled decode).
            received = sessions.pop(sid)
            del last_seen[sid]
            del first_seen[sid]
            received.sort(key=lambda r: r[0])
            on_session_done(sid, [t for _, t in received])


def handle_burst(session_id, ticks, thresh, expect, execute):
    n_bits = len(ticks) // TICKS_PER_BIT
    print(f"[jitter-wan-rx] session={session_id:#010x}  received {len(ticks)} ticks "
          f"-> {n_bits} bits", flush=True)

    bits = decode(ticks, n_bits, thresh)
    text = bits_to_text(bits)
    print(f"[jitter-wan-rx] decoded: {text!r}", flush=True)

    send_signed(0, text, ACK_PORT)

    if expect is not None:
        expected_bits = text_to_bits(expect)
        m = len(expected_bits)
        wrong = sum(1 for a, b in zip(bits, expected_bits) if a != b)
        wrong += abs(len(bits) - m)   # length mismatch (dropped/extra ticks) counts too
        print(f"[jitter-wan-rx] bits_wrong={wrong}/{m}", flush=True)

    if not execute:
        return

    intent = text.strip()
    if intent not in INTENTS:
        print(f"[jitter-wan-rx] unknown intent {intent!r}, not executing", flush=True)
        send_signed(1, f"unknown intent {intent!r}", RESULT_PORT)
        return

    resp = truthd_check("EC2_SELF")
    print(f"[jitter-wan-rx] truthd: {resp}", flush=True)
    if not resp.startswith("ALLOW"):
        print(f"[jitter-wan-rx] GATE DENIED, not executing", flush=True)
        send_signed(1, f"GATE DENIED: {resp}", RESULT_PORT)
        return

    result = subprocess.run(INTENTS[intent], capture_output=True, text=True)
    summary = (f"exit={result.returncode} stdout={result.stdout.strip()!r} "
               f"stderr={result.stderr.strip()!r}")
    print(f"[jitter-wan-rx] EXECUTED {intent}: {summary}", flush=True)
    send_signed(1, summary, RESULT_PORT)


def main():
    execute = "--execute" in sys.argv
    thresh_arg = _cli_arg("--threshold")
    thresh = float(thresh_arg) / 1000.0 if thresh_arg else THRESH
    expect = _cli_arg("--expect")

    s = mcast_in()
    print(f"[jitter-wan-rx] listening on {BIND_IP}:{PORT}  execute={execute}  "
          f"threshold={thresh*1000:.1f}ms  (persistent, session-multiplexed)", flush=True)

    def on_session_done(session_id, ticks):
        handle_burst(session_id, ticks, thresh, expect, execute)
        print("[jitter-wan-rx] --- session done, still listening for others ---", flush=True)

    collect_sessions(s, on_session_done)


if __name__ == "__main__":
    main()
