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
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from shell_launch_common import (
    load_key, pack_run, pack_kill, unpack_response,
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
    nonce = secrets.randbits(64)

    resp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    resp_sock.bind(("0.0.0.0", 0))
    resp_port = resp_sock.getsockname()[1]

    req_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if sys.argv[3] == "--kill":
        session_id = int(sys.argv[4], 16)
        pkt = pack_kill(session_id, nonce, resp_port, key)
        req_sock.sendto(pkt, (host, port))
        print(f"sent KILL session={session_id:#x} -> {host}:{port}")
        deadline = time.time() + 5.0
    else:
        cmd = sys.argv[3]
        cwd = sys.argv[4] if len(sys.argv) > 4 else ""
        pkt = pack_run(cmd, cwd, nonce, resp_port, key)
        req_sock.sendto(pkt, (host, port))
        print(f"sent RUN {cmd!r} cwd={cwd!r} -> {host}:{port}")
        deadline = None   # open-ended: stream until DONE

    resp_sock.settimeout(10.0)
    session_id = None
    while True:
        try:
            data, _ = resp_sock.recvfrom(2048)
        except socket.timeout:
            print("[recv] timeout waiting for response")
            break
        try:
            status, sid, detail, resp_nonce = unpack_response(data, key)
        except ValueError as e:
            print(f"[recv] bad response: {e}")
            continue
        if resp_nonce != nonce:
            continue
        session_id = sid
        name = STATUS_NAME.get(status, f"UNKNOWN({status})")
        if status == PROGRESS:
            print(f"[{name} session={sid:#x}]\n{detail}", flush=True)
        else:
            print(f"[{name} session={sid:#x}] {detail}", flush=True)
        if status in (DONE_OK, DONE_FAIL, REJECTED):
            break
        resp_sock.settimeout(30.0)   # once running, allow long gaps between progress ticks

    if session_id is not None:
        print(f"session id (for --kill): {session_id:#x}")


if __name__ == "__main__":
    main()
