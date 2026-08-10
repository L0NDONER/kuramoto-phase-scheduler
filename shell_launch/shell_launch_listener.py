#!/usr/bin/env python3
"""shell_launch_listener.py — unprivileged real-time shell-launch daemon.

Runs as the normal user (no sudo, no root, nothing in sudoers) on each
node's WireGuard interface. Accepts signed RUN/KILL requests, executes
RUN as a plain subprocess under this process's own privilege level --
there is no privilege escalation path here at all, by construction, not
by policy: this process never calls sudo, never touches root, so there
is nothing for a compromised or buggy command to escalate through.

RUN streams PROGRESS packets (recent stdout/stderr tail) to the
sender's resp_port every PROGRESS_INTERVAL_S while the process runs,
then a final DONE_OK/DONE_FAIL with the exit code. KILL terminates a
tracked session by session_id (SIGTERM, then SIGKILL after a grace
period if still alive).

Usage:
    python3 shell_launch_listener.py <bind_ip> [bind_port]
"""
import shlex
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from shell_launch_common import (
    load_key, unpack_request, pack_response,
    ACTION_RUN, ACTION_KILL, ACCEPTED, REJECTED, PROGRESS, DONE_OK, DONE_FAIL,
)

BIND_IP = sys.argv[1] if len(sys.argv) > 1 else "10.8.0.7"
BIND_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 58701

PROGRESS_INTERVAL_S = 0.5
KILL_GRACE_S = 3.0
SEEN_NONCE_TTL = 3600
TAIL_MAX_CHARS = 900   # matches shell_launch_common.MAX_DETAIL headroom

_sessions = {}    # session_id -> dict(proc, output, lock, addr, resp_port, nonce)
_seen_nonces = {}
_seen_lock = threading.Lock()


def _prune_nonces(now):
    dead = [n for n, exp in _seen_nonces.items() if exp < now]
    for n in dead:
        del _seen_nonces[n]


def _reader_thread(session_id):
    s = _sessions[session_id]
    proc = s["proc"]
    for line in proc.stdout:
        with s["lock"]:
            s["output"].append(line)
            # keep only enough to satisfy TAIL_MAX_CHARS on send, not unbounded
            total = sum(len(l) for l in s["output"])
            while total > TAIL_MAX_CHARS * 2 and len(s["output"]) > 1:
                total -= len(s["output"].pop(0))


def _tail(session_id):
    s = _sessions[session_id]
    with s["lock"]:
        text = "".join(s["output"])
    return text[-TAIL_MAX_CHARS:]


def _send(sock, addr, status, session_id, detail, nonce, key):
    try:
        sock.sendto(pack_response(status, session_id, detail, nonce, key), addr)
    except OSError:
        pass


def _run_session(session_id, cmd, cwd, addr, key, nonce, sock):
    s = _sessions[session_id]
    try:
        argv = shlex.split(cmd)
        proc = subprocess.Popen(argv, cwd=cwd or None,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
    except (OSError, ValueError) as e:
        _send(sock, addr, DONE_FAIL, session_id, f"launch failed: {e}", nonce, key)
        del _sessions[session_id]
        return

    s["proc"] = proc
    reader = threading.Thread(target=_reader_thread, args=(session_id,), daemon=True)
    reader.start()

    _send(sock, addr, ACCEPTED, session_id, f"pid={proc.pid}", nonce, key)
    print(f"[shell-launch] session={session_id:#x} ACCEPTED pid={proc.pid} cmd={cmd!r} cwd={cwd!r}",
          flush=True)

    while proc.poll() is None:
        time.sleep(PROGRESS_INTERVAL_S)
        if session_id not in _sessions:   # killed and cleaned up already
            return
        _send(sock, addr, PROGRESS, session_id, _tail(session_id), nonce, key)

    reader.join(timeout=2.0)
    status = DONE_OK if proc.returncode == 0 else DONE_FAIL
    _send(sock, addr, status, session_id, _tail(session_id), nonce, key)
    print(f"[shell-launch] session={session_id:#x} DONE exit={proc.returncode}", flush=True)
    del _sessions[session_id]


def _handle_kill(session_id, addr, nonce, key, sock):
    s = _sessions.get(session_id)
    if s is None or "proc" not in s:
        _send(sock, addr, REJECTED, session_id, "no such session", nonce, key)
        return
    proc = s["proc"]
    print(f"[shell-launch] session={session_id:#x} KILL requested (pid={proc.pid})", flush=True)
    proc.terminate()
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)
    _send(sock, addr, DONE_OK, session_id, f"killed, exit={proc.returncode}", nonce, key)


def main():
    key = load_key()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND_IP, BIND_PORT))
    print(f"[shell-launch] bound {BIND_IP}:{BIND_PORT} (unprivileged, uid={__import__('os').getuid()})",
          flush=True)

    while True:
        data, addr = sock.recvfrom(2048)
        now = time.time()
        try:
            req = unpack_request(data, key)
        except ValueError as e:
            print(f"[shell-launch] REJECTED from {addr}: {e}", flush=True)
            continue

        nonce = req["nonce"]
        with _seen_lock:
            _prune_nonces(now)
            if nonce in _seen_nonces:
                print(f"[shell-launch] REJECTED replay nonce={nonce} from {addr}", flush=True)
                continue
            _seen_nonces[nonce] = now + SEEN_NONCE_TTL

        resp_addr = (addr[0], req["resp_port"])

        if req["action"] == ACTION_RUN:
            session_id = int.from_bytes(__import__("os").urandom(8), "big")
            _sessions[session_id] = {"output": [], "lock": threading.Lock()}
            threading.Thread(target=_run_session,
                              args=(session_id, req["cmd"], req["cwd"], resp_addr,
                                    key, nonce, sock),
                              daemon=True).start()
        elif req["action"] == ACTION_KILL:
            _handle_kill(req["session_id"], resp_addr, nonce, key, sock)


if __name__ == "__main__":
    main()
