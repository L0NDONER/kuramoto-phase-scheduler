#!/bin/bash
# fault_test.sh — automated version of the kill/restart matrix run by hand
# against the live portfolio job (coord on Mint, worker0 on pi, worker1 on
# pi2, substrate quartz_node on all three) on 2026-07-29. Scripts these five
# scenarios and asserts recovery so future daemon changes get checked the
# same way instead of by hand again:
#
#   1. kill worker0 (pi)          -> coord stalls, resumes on restart
#   2. kill worker1 (pi2)         -> coord stalls, resumes on restart
#   3. kill both workers          -> coord stalls, resumes on restart
#   4. kill coordinator           -> workers keep running unaffected
#   5. kill substrate on pi2      -> peer_health flips unhealthy <1s,
#                                    coord skips (not stalls), recovers
#
# Assumes the portfolio job is already live in its normal locations
# (~/claude on all three hosts) before this script runs, and leaves it
# running (recovered) when done, pass or fail. Ctrl-C-safe: traps and
# attempts recovery of whatever it just killed before exiting.
set -u

REMOTE_DIR="claude"
COORD_LOG="$HOME/$REMOTE_DIR/portfolio_coord.log"
WORKER0_LOG="$REMOTE_DIR/portfolio_worker0.log"
WORKER1_LOG="$REMOTE_DIR/portfolio_worker1.log"
SUBSTRATE_LOG="$REMOTE_DIR/quartz_node.log"

SETTLE_WAIT=2      # after a kill, let any in-flight round finish before baselining
STALL_WAIT=8      # seconds to confirm no further progress past the settle point
RESUME_WAIT=8      # seconds to allow resync after restart (SSH round-trip
                     # + process start is occasionally slower than 5s)
PASS=0
FAIL=0

local_lines() { wc -l < "$1" 2>/dev/null | tr -d '[:space:]'; }
remote_lines() { ssh "$1" "wc -l < '$2'" 2>/dev/null | grep -oE '^[0-9]+$'; }

kill_local_pattern() { pkill -f "$1" 2>/dev/null; }
kill_remote_pattern() { ssh "$1" "pkill -f '$2'" 2>/dev/null; }

kill_worker0() { kill_remote_pattern pi "quartz_metro_node worker portfolio 0"; }
kill_worker1() { kill_remote_pattern pi2 "quartz_metro_node worker portfolio 1"; }

start_worker0() { timeout 5 ssh pi "cd ~/$REMOTE_DIR && nohup stdbuf -oL -eL ./quartz_metro_node worker portfolio 0 10.0.0.71 > portfolio_worker0.log 2>&1 < /dev/null & disown" >/dev/null 2>&1; }
start_worker1() { timeout 5 ssh pi2 "cd ~/$REMOTE_DIR && nohup stdbuf -oL -eL ./quartz_metro_node worker portfolio 1 10.0.0.71 > portfolio_worker1.log 2>&1 < /dev/null & disown" >/dev/null 2>&1; }
start_coord()   { (cd "$HOME/$REMOTE_DIR" && nohup stdbuf -oL -eL ./quartz_metro_node coord portfolio 10.0.0.122 10.0.0.174 > portfolio_coord.log 2>&1 < /dev/null & disown) }
start_substrate_pi2() { timeout 5 ssh pi2 "cd ~/$REMOTE_DIR && nohup stdbuf -oL -eL ./quartz_node 3 10.0.0.71 10.0.0.122 > quartz_node.log 2>&1 < /dev/null & disown" >/dev/null 2>&1; }

pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# --- scenario 1 & 2: kill one worker, confirm coord stalls then resumes ---
# kill_fn/start_fn are real function names (not stringly-typed commands —
# `$var` on a string containing quotes does not re-parse them, it splits on
# the literal quote characters, so a multi-word pkill pattern silently
# never matched anything. Cost a false "still progressing" result once.)
test_kill_worker() {
    local host="$1" slot="$2" kill_fn="$3" start_fn="$4"
    echo "== kill worker$slot ($host) =="
    "$kill_fn"
    sleep "$SETTLE_WAIT"
    local before after
    before=$(local_lines "$COORD_LOG")
    sleep "$STALL_WAIT"
    after=$(local_lines "$COORD_LOG")
    if [ "$before" = "$after" ]; then
        pass "coordinator stalled cleanly (no growth in ${STALL_WAIT}s)"
    else
        fail "coordinator kept progressing without worker$slot ($before -> $after lines) — sender validation may be broken"
    fi
    "$start_fn"
    sleep "$RESUME_WAIT"
    local resumed
    resumed=$(local_lines "$COORD_LOG")
    if [ "$resumed" -gt "$after" ]; then
        pass "coordinator resumed after worker$slot restarted ($after -> $resumed lines)"
    else
        fail "coordinator did not resume after worker$slot restarted"
    fi
}

# --- scenario 3: kill both workers at once ---
test_kill_both_workers() {
    echo "== kill both workers =="
    kill_worker0
    kill_worker1
    sleep "$SETTLE_WAIT"
    local before after
    before=$(local_lines "$COORD_LOG")
    sleep "$STALL_WAIT"
    after=$(local_lines "$COORD_LOG")
    if [ "$before" = "$after" ]; then
        pass "coordinator stalled cleanly with both workers dead"
    else
        fail "coordinator kept progressing with no workers alive ($before -> $after lines)"
    fi
    start_worker0
    start_worker1
    sleep "$RESUME_WAIT"
    local resumed
    resumed=$(local_lines "$COORD_LOG")
    if [ "$resumed" -gt "$after" ]; then
        pass "coordinator resumed after both workers restarted ($after -> $resumed lines)"
    else
        fail "coordinator did not resume after both workers restarted"
    fi
}

# --- scenario 4: kill coordinator, workers must survive (not crash) ---
# Workers do NOT keep meaningfully progressing without a coordinator: their
# per-step recv_until for MSG_STEP_REQ has no probe, so once any in-flight
# backlog drains, each step silently times out for up to
# MAX_RETRIES*RECV_TIMEOUT_MS (1.6s), and steps_each (300) never completes
# in any short window. An earlier version of this test asserted log growth
# here and either passed on a lucky in-flight burst or failed honestly —
# the only real invariant is that the worker process itself survives.
test_kill_coordinator() {
    echo "== kill coordinator =="
    kill_local_pattern "quartz_metro_node coord portfolio"
    sleep "$SETTLE_WAIT"
    sleep "$STALL_WAIT"
    if ssh pi "pgrep -f 'quartz_metro_node worker portfolio 0'" >/dev/null 2>&1 \
        && ssh pi2 "pgrep -f 'quartz_metro_node worker portfolio 1'" >/dev/null 2>&1; then
        pass "both worker processes still alive with no coordinator"
    else
        fail "a worker process died when the coordinator died"
    fi
    start_coord
    sleep "$RESUME_WAIT"
    local resumed
    resumed=$(local_lines "$COORD_LOG")
    if [ -n "$resumed" ] && [ "$resumed" -gt 0 ]; then
        pass "coordinator resynced with already-running workers"
    else
        fail "coordinator did not come back up cleanly"
    fi
}

# --- scenario 5: kill substrate on pi2, expect skip (not stall), not storm ---
test_kill_substrate() {
    echo "== kill substrate (quartz_node on pi2) =="
    local before after
    before=$(local_lines "$COORD_LOG")
    kill_remote_pattern pi2 "quartz_node 3 10.0.0.71 10.0.0.122"
    sleep 2   # HEALTH_TIMEOUT_S=0.5s in quartz_node.c, give it margin
    local healthy
    healthy=$(grep '"peer_ip": "10.0.0.174"' /tmp/quartz_peer_health.json | grep -o '"healthy": [a-z]*')
    if echo "$healthy" | grep -q false; then
        pass "Mint's substrate marked 10.0.0.174 unhealthy"
    else
        fail "Mint's substrate did not mark 10.0.0.174 unhealthy ($healthy)"
    fi
    sleep 3
    if tail -n 20 "$COORD_LOG" | grep -q "10.0.0.174 unhealthy, skipping round"; then
        pass "coordinator is skipping rounds, not stalling or storming"
    else
        fail "coordinator did not log the expected skip message"
    fi
    start_substrate_pi2
    sleep 3
    healthy=$(grep '"peer_ip": "10.0.0.174"' /tmp/quartz_peer_health.json | grep -o '"healthy": [a-z]*')
    after=$(local_lines "$COORD_LOG")
    if echo "$healthy" | grep -q true && [ "$after" -gt "$before" ]; then
        pass "substrate recovered and coordinator resumed normal rounds"
    else
        fail "substrate/coordinator did not recover cleanly ($healthy)"
    fi
}

trap 'echo; echo "interrupted — attempting best-effort recovery"; start_substrate_pi2; start_worker0; start_worker1; start_coord' INT TERM

echo "=== quartz-substrate fault test ==="
test_kill_worker pi 0 kill_worker0 start_worker0
test_kill_worker pi2 1 kill_worker1 start_worker1
test_kill_both_workers
test_kill_coordinator
test_kill_substrate

echo
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
