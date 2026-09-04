#!/usr/bin/env python3
"""shell_launch_send.py — fire a command at a shell_launch_listener.py
and stream its output back in real time, over WireGuard, no SSH.

Usage:
    python3 shell_launch_send.py <host> <port> <cmd> [cwd]
    python3 shell_launch_send.py <host> <port> --kill <session_id_hex>

Example:
    python3 shell_launch_send.py 10.8.0.7 58701 "python3 run_pi1.py" ~/pi1_cpu_layer0
"""
import secrets
import socket
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from shell_launch_common import (
    load_key, pack_kill, send_run, unpack_response,
    ACCEPTED, REJECTED, PROGRESS, DONE_OK, DONE_FAIL,
)

STATUS_NAME = {ACCEPTED: "ACCEPTED", REJECTED: "REJECTED", PROGRESS: "PROGRESS",
               DONE_OK: "DONE_OK", DONE_FAIL: "DONE_FAIL"}


def main():
    if len(sys.argv) < 4:
        sys.exit(f"usage: {sys.argv[0]} <host> <port> <cmd> [cwd]\n"
                  f"       {sys.argv[0]} <host> <port> --kill <session_id_hex>")

    host, port = sys.argv[1], int(sys.argv[2])
    key = load_key()

    if sys.argv[3] == "--kill":
        nonce = secrets.randbits(64)
        resp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        resp_sock.bind(("0.0.0.0", 0))
        resp_port = resp_sock.getsockname()[1]
        req_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        session_id = int(sys.argv[4], 16)
        pkt = pack_kill(session_id, nonce, resp_port, key)
        req_sock.sendto(pkt, (host, port))
        print(f"sent KILL session={session_id:#x} -> {host}:{port}")

        resp_sock.settimeout(5.0)
        try:
            data, _ = resp_sock.recvfrom(2048)
            status, sid, detail, _resp_nonce = unpack_response(data, key)
            name = STATUS_NAME.get(status, f"UNKNOWN({status})")
            print(f"[{name} session={sid:#x}] {detail}")
        except socket.timeout:
            print("[recv] timeout waiting for response")
        except ValueError as e:
            print(f"[recv] bad response: {e}")
        return

    cmd = sys.argv[3]
    cwd = sys.argv[4] if len(sys.argv) > 4 else ""
    print(f"sent RUN {cmd!r} cwd={cwd!r} -> {host}:{port}")

    # PROGRESS/DONE_OK/DONE_FAIL all carry _tail()'s sliding window of
    # recent output (see shell_launch_listener.py), not a new increment
    # each time -- printing detail verbatim on every packet reprints
    # whatever's still inside the window. Track what's already been
    # printed and only emit the new suffix.
    last_tail = [""]

    def new_suffix(detail):
        last = last_tail[0]
        last_tail[0] = detail
        if detail.startswith(last):
            return detail[len(last):]
        # window slid (old buffer's front got truncated) -- find the
        # longest suffix of `last` that's still a prefix of `detail`
        for i in range(min(len(last), len(detail)), 0, -1):
            if last.endswith(detail[:i]):
                return detail[i:]
        return detail

    def on_status(status, sid, detail, _elapsed_s):
        name = STATUS_NAME.get(status, f"UNKNOWN({status})")
        if status in (PROGRESS, DONE_OK, DONE_FAIL):
            text = new_suffix(detail)
            if text:
                print(text, end="", flush=True)
            if status != PROGRESS:
                print(f"\n[{name} session={sid:#x}]", flush=True)
        else:
            print(f"[{name} session={sid:#x}] {detail}", flush=True)

    session_id = send_run(host, port, cmd, cwd, key, on_status)
    if session_id is None:
        print("[recv] timeout waiting for response")
    else:
        print(f"session id (for --kill): {session_id:#x}")


if __name__ == "__main__":
    main()
