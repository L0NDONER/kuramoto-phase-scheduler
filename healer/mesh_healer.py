"""mesh_healer.py — job-table self-heal loop for mesh nodes (pi1, pi2).

Started 2026-08-14 as a single hardcoded check (shell_launch responsive)
+ single hardcoded heal (systemctl --user restart). Refactored
2026-08-15 into a small table of independent JOBS so new checks can be
added without touching the working one -- each job carries its own name
(identity), verb class (permission tier, from truth_manifest.h's fixed
4-value set: BENIGN_READ/LOCAL_HEAL/LOCAL_DESTRUCTIVE/EC2_SELF -- shared
across many jobs, doesn't disambiguate them), check function, and an
OPTIONAL heal function. A job with no heal function never attempts a
fix -- it only ever escalates, because for some failure modes (disk
nearly full) there is no safe automated action.

Deliberately NOT the "ask an LLM what to do" orchestrator that was
proposed and rejected in conversation: no job's heal action is chosen by
a model. Every heal is a fixed function picked by code. The LLM's only
job is narrating a job's outcome afterward, same read-only-reporter
boundary as every other consumer in this repo
([[feedback_substrate_not_a_cron_job]]).

Per (node, job), every CHECK_INTERVAL_S:
  1. Run the job's check function.
  2. Healthy -> clear that job's failure/escalation state, done.
  3. Unhealthy, job has a heal function, past cooldown -> run it, wait
     HEAL_VERIFY_DELAY_S, re-check, narrate the outcome.
  4. Unhealthy, job has NO heal function -> narrate once on first
     detection, then stay quiet (no re-narration every cycle) until
     either healthy again or escalated.
  5. After HEAL_ATTEMPTS_BEFORE_ESCALATE consecutive unhealthy cycles
     (heal attempted-and-failed, or no-heal-available), mark that
     (node, job) ESCALATED and stop attempting/re-notifying until a
     human clears it -- no infinite hammering, no infinite token spend
     narrating the same unresolved problem every cycle.
"""
import os
import re
import secrets
import shlex
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone

# mesh_healer.py lives one level deeper now (cascade_pll/healer/) after
# the 2026-08-15 reorg -- REPO is the cascade_pll/ root, two dirs up,
# not this file's own directory.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS = os.path.join(REPO, "channels")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(CHANNELS, "shell_launch"))
sys.path.insert(0, CHANNELS)
sys.path.insert(0, os.path.join(REPO, "readers"))

sys.path.insert(0, os.path.join(REPO, "layer1"))


def log(msg):
    """UTC timestamp + [mesh_healer] tag, replaces bare print() at every
    log call site (2026-09-01 -- the log had no timestamps at all before
    this, making it impossible to correlate an entry with when it
    actually happened)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [mesh_healer] {msg}", flush=True)

from shell_launch_common import load_key as load_shell_key, send_run, PROGRESS, DONE_OK, DONE_FAIL
import pi_intent_common as pic
import ec2_intent_common as eic
import groq_client
from groq_client import query_llm, QuotaExceeded
from layer0_report import mcast_in, parse_report

NODES = {
    "pi1": dict(
        shell_host="10.8.0.7", shell_port=58701,
        shell_key=os.path.join(CHANNELS, "shell_launch", "shell_launch_pi1.key"),
        intent_host="10.8.0.7", intent_port=58601,
        intent_key=os.path.join(CHANNELS, "pi1_intent.key"),
        repo_path="/home/martin/shell_launch",
    ),
    "pi2": dict(
        shell_host="10.8.0.5", shell_port=58701,
        shell_key=os.path.join(CHANNELS, "shell_launch", "shell_launch_pi2.key"),
        intent_host="10.8.0.5", intent_port=58601,
        intent_key=os.path.join(CHANNELS, "pi_intent.key"),
        repo_path="/home/martin/shell_launch",
    ),
}

CHECK_INTERVAL_S = 60
HEALTH_TIMEOUT_S = 8
HEAL_VERIFY_DELAY_S = 5
HEAL_ATTEMPTS_BEFORE_ESCALATE = 3
HEAL_COOLDOWN_S = 120   # minimum real time between two heal attempts on the same job

# layer0_shared_substrate gate (added 2026-09-01) -- a SECOND, independent
# presence gate on top of phase_auth's existing one. phase_auth already
# gates LOCAL_HEAL/LOCAL_DESTRUCTIVE on the RECEIVING end (inside
# pi_intent_listener.py, using AxisPulse). This gates the SAME verb class
# on the SENDING end (here, in mesh_healer itself), using a completely
# different substrate (layer0_daemon.py's tick/pd on 239.0.0.6:7500, not
# AxisPulse on :7460) -- covers the case where Layer0 itself is down but
# AxisPulse/phase_auth is fine, which the existing gate can't see.
# Deliberately scoped to LOCAL_DESTRUCTIVE only for now, not LOCAL_HEAL --
# see the comment on the gate check in run_job() for why.
LAYER0_GRP, LAYER0_PORT = "239.0.0.6", 7500
LAYER0_MAGIC = 0x4C30
LAYER0_FMT = "!HIffd"
LAYER0_SIZE = struct.calcsize(LAYER0_FMT)
LAYER0_TTL_S = 5.0   # own threshold, deliberately not reusing OSCILLATOR_STALE_S -- different substrate

DISK_USAGE_PCT_UNHEALTHY = 90   # unhealthy at or above this -- pi1/pi2 both sat
                                  # at 59% when this was picked, generous margin


def _shell_run(node, cmd, timeout_s=HEALTH_TIMEOUT_S):
    """Shared real request/response over shell_launch -- used by every
    check job, not just listener_responsive. Returns the joined output
    string, or None on timeout/no response."""
    cfg = NODES[node]
    key = load_shell_key(cfg["shell_key"])
    output = []

    def on_status(status, sid, detail, elapsed_s):
        if status in (PROGRESS, DONE_OK, DONE_FAIL):
            output.append(detail)

    session_id = send_run(cfg["shell_host"], cfg["shell_port"], cmd, "", key,
                           on_status, initial_timeout_s=timeout_s)
    if session_id is None:
        return None
    return "\n".join(output)


def check_listener(node):
    """Real request/response over shell_launch, not just a ping --
    matches how the outage was actually diagnosed tonight (network-up
    but listener-dead was indistinguishable from ping alone)."""
    out = _shell_run(node, "echo ok")
    return out is not None and "ok" in out


def check_disk_space(node):
    """BENIGN_READ, no heal function -- there is no safe automated fix
    for disk nearly full, so this job only ever escalates.

    The listener runs commands via shlex.split, not a shell (confirmed
    live 2026-08-14 for a different job) -- "|" needs an explicit
    bash -c wrapper or it gets passed as a literal argv token."""
    out = _shell_run(node, "bash -c " + shlex.quote("df -h / | tail -1"))
    if out is None:
        return False   # can't reach the node at all -- let listener_responsive's
                        # own job report the real reason, this just also fails safe
    m = re.search(r"(\d+)%", out)
    if not m:
        return False
    return int(m.group(1)) < DISK_USAGE_PCT_UNHEALTHY


def send_intent(node, intent, delay_s=2.0, wait_s=15.0):
    """Direct port of pi_intent_send.py's send/receive loop, reading the
    key file directly per-call instead of via pi_intent_common's
    load_key() -- that function caches KEY_PATH at first import from a
    fixed env var, which silently signs with the wrong host's key if
    you try to target more than one host from the same process
    (documented footgun in pi_intent_common.py, hit for real 2026-08-10).
    Returns (status_name, detail) or ("TIMEOUT", None)."""
    cfg = NODES[node]
    key = bytes.fromhex(open(cfg["intent_key"]).read().strip())

    trigger_at = time.time() + delay_s
    nonce = secrets.randbits(64)
    pkt = pic.pack(intent, trigger_at, nonce, key)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.sendto(pkt, (cfg["intent_host"], cfg["intent_port"]))

    deadline = time.time() + delay_s + wait_s
    while time.time() < deadline:
        sock.settimeout(max(deadline - time.time(), 0.1))
        try:
            data, _ = sock.recvfrom(512)
        except socket.timeout:
            break
        try:
            status, detail, resp_nonce = pic.unpack_response(data, key)
        except ValueError:
            continue
        if resp_nonce != nonce:
            continue
        name = {pic.ACCEPTED: "ACCEPTED", pic.REJECTED: "REJECTED",
                pic.DONE_OK: "DONE_OK", pic.DONE_FAIL: "DONE_FAIL"}.get(status, str(status))
        if name in ("DONE_OK", "DONE_FAIL", "REJECTED"):
            return name, detail
    return "TIMEOUT", None


# Validated against real samples (2026-08-15), not picked blind: 15 live
# `wg show` reads across pi1 (1 handshake) and pi2 (2, wg0+wg1) ranged
# 6s-1m57s, consistently topping out just under WireGuard's own protocol
# rekey interval (REKEY_AFTER_TIME=120s, a real protocol constant, not a
# guess). 5 min gives ~2.5x margin above that observed natural cycle --
# same spirit as the regime classifier's percentile-derived thresholds,
# though on a thinner sample (15 reads over ~2min, not tens of
# thousands) -- call this "validated with real margin," not "proven
# optimal."
WG_HANDSHAKE_STALE_MIN = 5


def _handshake_is_fresh(desc):
    if "day" in desc or "hour" in desc:
        return False
    if "minute" in desc:
        m = re.search(r"(\d+)\s*minute", desc)
        return bool(m) and int(m.group(1)) < WG_HANDSHAKE_STALE_MIN
    return True   # "N seconds ago" or "now"


def check_wg_handshake(node):
    """`wg show` needs root (CAP_NET_ADMIN) -- confirmed live 2026-08-15
    that a bare, unprivileged call fails with "Operation not permitted".
    Routed through shell_launch (not pi_intent's exec:) since this is a
    read-only check, not a gated heal action -- same sudoers NOPASSWD
    entry either channel would need, since the permission is tied to
    the user account running sudo, not which channel invoked it.
    Checks every "latest handshake" line found (pi2 runs two WG
    interfaces, wg0 and wg1) -- healthy if ANY is fresh."""
    out = _shell_run(node, "sudo -n wg show")
    if out is None or "Operation not permitted" in out or "password is required" in out:
        return False
    handshakes = re.findall(r"latest handshake:\s*(.+)", out)
    if not handshakes:
        return False   # interface up but never completed a handshake at all
    return any(_handshake_is_fresh(h) for h in handshakes)


# Oscillator-liveness check -- watches the same 239.0.0.6:7460 wire
# run_layer1.py/layer1_aggregator.py already listen on, so a node
# missing here is exactly a node Layer 1's mean-field has already
# silently dropped (see fractal_layer1/layer0_report.py's STALE_AFTER_S
# fix, 2026-08-30). Matching that threshold means mesh_healer flags
# unhealthy at the same moment Layer 1 stops counting the node, not
# before (false alarm) or after (blind spot).
#
# One shared non-blocking socket + last-seen cache, not one bind per
# job -- SO_REUSEPORT would allow multiple binds, but every job in a
# given check cycle should see the same drained snapshot rather than
# racing each other for packets off the wire, same reasoning
# run_layer1.py's own _drain_reports() already documents for itself.
OSCILLATOR_STALE_S = 5.0   # == run_layer1.py's STALE_AFTER_S

# Confirmed live 2026-08-31: on a freshly (re)started mesh_healer, the
# very first check_oscillator_alive() call binds a brand-new socket that
# hasn't had time to receive anything yet -- "nothing seen" read as
# "unhealthy" and fired a real, unnecessary heal action against a node
# that was fine the whole time. OSCILLATOR_GRACE_S gives the socket real
# time to actually receive a few ticks (nodes broadcast every ~0.5s)
# before this check's verdict counts for anything -- generous margin
# above that cadence, not tuned to the minimum that would technically
# work, same "validated with real margin" spirit as this file's other
# thresholds.
OSCILLATOR_GRACE_S = 10.0

_osc_sock = None
_osc_sock_created_at = None
_osc_last_seen = {}   # node_name -> monotonic timestamp of last report seen


def _drain_oscillator_reports():
    global _osc_sock, _osc_sock_created_at
    if _osc_sock is None:
        _osc_sock = mcast_in()
        _osc_sock_created_at = time.monotonic()
    while True:
        try:
            data, _addr = _osc_sock.recvfrom(64)
        except BlockingIOError:
            return
        parsed = parse_report(data)
        if parsed is None:
            continue
        _osc_last_seen[parsed[0]] = time.monotonic()


def check_oscillator_alive(node):
    """Healthy if a layer0 report bearing this node's real name has
    arrived on the wire within OSCILLATOR_STALE_S. Reads whatever's
    already buffered -- no new network round trip, same "cheap enough to
    run every cycle, even while escalated" property every check here
    relies on. During OSCILLATOR_GRACE_S after the socket is first
    created, always reports healthy -- there hasn't been enough time to
    honestly observe anything yet, so "no data" isn't evidence of a
    problem here the way it is once the socket has been listening a
    while (see OSCILLATOR_GRACE_S's own comment for the real incident
    this fixes)."""
    _drain_oscillator_reports()
    if time.monotonic() - _osc_sock_created_at < OSCILLATOR_GRACE_S:
        return True
    last = _osc_last_seen.get(node)
    return last is not None and (time.monotonic() - last) < OSCILLATOR_STALE_S


_l0_sock = None
_l0_last_seen = None   # monotonic timestamp of last real layer0 packet, None if never seen


def _get_layer0_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", LAYER0_PORT))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(LAYER0_GRP) + socket.inet_aton("0.0.0.0"))
    sock.setblocking(False)
    return sock


def _drain_layer0():
    """Same non-blocking drain-latest shape as _drain_oscillator_reports()
    -- reads whatever's already buffered, no new round trip, cheap enough
    to call every cycle."""
    global _l0_sock, _l0_last_seen
    if _l0_sock is None:
        _l0_sock = _get_layer0_socket()
    while True:
        try:
            data, _addr = _l0_sock.recvfrom(64)
        except BlockingIOError:
            return
        if len(data) < LAYER0_SIZE:
            continue
        magic = struct.unpack(LAYER0_FMT, data[:LAYER0_SIZE])[0]
        if magic == LAYER0_MAGIC:
            _l0_last_seen = time.monotonic()


def layer0_fresh():
    """No grace-period exemption like check_oscillator_alive() -- unlike
    that check, this isn't reporting a health verdict on a monitored node,
    it's mesh_healer asking permission to act on itself. 'never seen a
    packet yet' (e.g. layer0-daemon down at mesh_healer startup) should
    read as not-fresh, not as an assumed-healthy grace window."""
    _drain_layer0()
    return _l0_last_seen is not None and (time.monotonic() - _l0_last_seen) < LAYER0_TTL_S


# Heal targets, one real deployed process per node -- keyed by node
# NAME (what the wire calls it), not by a guessed script name. pi1's
# case is exactly why that distinction matters: confirmed live
# 2026-08-31 that pi1_cpu_layer0/run_pi1.py was never deployed on the
# real Pi1 at all (no such directory there) -- pi1_thermal_layer0/
# run_pi1_thermal.py is what actually broadcasts the "pi1" identity on
# 239.0.0.6, so that's the process this heals. Every field below (cwd,
# launch command, pkill pattern) was read directly off each node's real
# running process via shell_launch, not assumed from the repo layout --
# pi1_thermal_layer0/run_pi1_thermal.py was launched with no duration
# arg (loops forever, see its own TOTAL_DURATION_S default), pi2's
# run_pi2.py with an explicit 999999999s. pkill_pattern matches the
# full real command line, not just the script basename, so a heal
# attempt can't accidentally kill an unrelated process sharing a
# substring.
# Both real oscillator broadcasters already run under systemd --user
# (pi1-thermal-layer0.service, pi2-cpu-layer0.service -- Restart=on-
# failure, enabled, survive a reboot). Heal via `systemctl --user
# restart`, not a raw pkill+relaunch -- confirmed live 2026-08-31 that
# pkill+nohup was actively wrong here: pkill'ing the systemd-supervised
# PID directly makes systemd see a clean SIGTERM, which
# Restart=on-failure correctly does NOT treat as a failure, so it never
# auto-restarts. The process only came back because the old
# heal_oscillator's own nohup relaunched it manually -- orphaned from
# systemd from that point on, silently losing both Restart=on-failure
# and reboot survival for good. `systemctl --user restart` keeps the
# unit (and its supervision) intact through every heal instead of
# quietly stripping it away.
OSCILLATOR_TARGETS = {
    "pi1": dict(unit="pi1-thermal-layer0.service"),
    "pi2": dict(unit="pi2-cpu-layer0.service"),
}


def heal_oscillator(node):
    """Restarts the node's real oscillator broadcaster via its systemd
    --user unit (OSCILLATOR_TARGETS). Routed through shell_launch --
    LOCAL_HEAL tier, same as heal_wg -- rather than pi_intent's root
    exec:, since `systemctl --user` needs no root (same reasoning
    shell_launch itself was built for). Returns (status_name, detail),
    same shape as send_intent()'s heals, for run_job()'s
    narration/logging to stay uniform across every job."""
    unit = OSCILLATOR_TARGETS[node]["unit"]
    out = _shell_run(node, f"systemctl --user restart {shlex.quote(unit)}")
    if out is None:
        return "TIMEOUT", f"no response restarting {unit}"
    return "DONE_OK", f"restarted {unit}"


def heal_wg(node):
    # restart_wg is one of pi_intent_listener.py's original fixed
    # intents (predates last night's work) -- confirmed live 2026-08-15
    # it already has a working sudoers entry, no new one needed for
    # this specific heal action (only the check function above needed
    # a new "wg show" sudoers line).
    return send_intent(node, "restart_wg")


def heal_listener(node):
    # Both Pis' shell-launch-listener.service are systemd-managed now
    # (2026-08-14, Restart=on-failure + linger enabled). Routed through
    # pi_intent's exec:, which always runs as ROOT (sudo -n bash -c,
    # no -u martin) -- confirmed live that root's environment has no
    # DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR, so a bare `systemctl
    # --user` as root can't find martin's lingering user-systemd
    # session at all (different failure than the earlier sudoers
    # block, which is now fixed via a NOPASSWD entry on each Pi).
    # `runuser -u martin --` properly switches into martin's session
    # (root can always do this natively, no further sudoers entry
    # needed) with the right runtime dir for the --user bus.
    cmd = ("runuser -u martin -- env XDG_RUNTIME_DIR=/run/user/1000 "
           "systemctl --user restart shell-launch-listener.service")
    return send_intent(node, f"exec:{cmd}")


# Local Mint-side process liveness -- structurally different from the
# node jobs above (plain pgrep, not a remote shell_launch/pi_intent
# request), so it doesn't fit NODES' remote-host fields. Reuses run_job/
# narrate/state as-is by tagging these under a synthetic "mint" node
# label rather than adding a second, parallel loop -- these are exactly
# the writer processes that "check S"/"check regime" have been silently
# assuming are alive all session; this makes that assumption checked
# instead of assumed. BENIGN_READ, no heal function, same as disk_space
# -- blindly restarting a process that died for an unknown real reason
# (bug, port conflict) is not a safe default action, always escalate.
LOCAL_PROCESSES = {
    "market_layer0": "run_test.py",
    "mint_cpu_layer0": "run_local.py",
    "netlat_layer0": "run_netlat.py",
    "layer1": "run_layer1.py",
    "layer2": "run_layer2.py",
    "consensus_explainer": "consensus_explainer.py",
    "anomaly_explainer": "anomaly_explainer.py",
    "log_trimmer": "log_trimmer.py",
    "ec2_probe": "ec2_probe.py",
}


def _pgrep_alive(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _make_local_process_check(pattern):
    return lambda node: _pgrep_alive(pattern)


LOCAL_JOBS = [
    dict(name=f"{name}_alive", verb="BENIGN_READ",
         check=_make_local_process_check(pattern), heal=None)
    for name, pattern in LOCAL_PROCESSES.items()
]


# Tier 1 observer-drift check ("the stick hasn't moved, but has the
# ruler warped" -- 2026-08-15 conversation): _alive above only proves
# the PROCESS exists, not that it's still doing anything -- a hung
# process reads as healthy forever. This checks output freshness (real
# wall-clock mtime, not the log's own internal elapsed-seconds label,
# which is relative to process start and useless for "is this recent
# right now") against each writer's own known real tick rate, same
# freshness-not-existence principle as truthd.c's HEALTH_STALE_S/
# EC2_STALE_S.
#
# Only the genuinely periodic writers get this -- consensus_explainer/
# anomaly_explainer/log_trimmer are event-triggered
# or irregular (a long quiet period is correct behaviour, not staleness)
# and would false-positive under a freshness model; they stay covered
# by _alive only. market_layer0 is journal-only (systemd, no log file),
# a different check shape -- left out of this first pass, not folded in
# just to hit a round number.
#
# stale_after_s margins: ec2_probe reuses truthd.c's own EC2_STALE_S
# (15s = 3x its real PROBE_INTERVAL_S=5.0, already validated there).
# The four 0.5s-tick oscillator/telemetry logs use 30s -- a deliberately
# chosen ~60x margin, not independently sampled the way the WG
# handshake threshold was -- picked generous because checks only run
# every CHECK_INTERVAL_S=60s anyway, so anything tighter risks a false
# stale reading from ordinary scheduling jitter between checks.
FRESHNESS_TARGETS = {
    "layer1": dict(path="layer1/layer1.log", stale_after_s=30),
    "layer2": dict(path="layer2/layer2.log", stale_after_s=30),
    "netlat_layer0": dict(path="netlat_layer0/run_netlat.log", stale_after_s=30),
    "mint_cpu_layer0": dict(path="mint_cpu_layer0/run_local.log", stale_after_s=30),
    "ec2_probe": dict(path="channels/ec2_probe.log", stale_after_s=15),
    "grid_freq_layer0": dict(path="grid_freq_layer0/run_grid_freq.log", stale_after_s=30),
}


def _fresh(path, stale_after_s):
    full_path = os.path.join(REPO, path)
    try:
        mtime = os.path.getmtime(full_path)
    except FileNotFoundError:
        return False
    return (time.time() - mtime) < stale_after_s


def _make_freshness_check(path, stale_after_s):
    return lambda node: _fresh(path, stale_after_s)


FRESHNESS_JOBS = [
    dict(name=f"{name}_fresh", verb="BENIGN_READ",
         check=_make_freshness_check(cfg["path"], cfg["stale_after_s"]), heal=None)
    for name, cfg in FRESHNESS_TARGETS.items()
]


# Groq quota visibility -- 2026-08-15, tied directly to tonight's real
# recurring friction: consensus_explainer/anomaly_explainer/mesh_healer
# itself have all silently hit QuotaExceeded more than once, discovered
# only after the fact from a skipped narration line. This surfaces it
# proactively instead. BENIGN_READ, no heal -- there's no automated fix
# for "quota's nearly gone," same reasoning as disk_space.
#
# Honest cost: Groq only exposes quota via response headers on a real
# request, there's no free headers-only endpoint -- so checking quota
# costs a real request against the same daily request budget it's
# watching. Confirmed live 2026-08-31 this was NOT a tiny sliver in
# practice: CHECK_INTERVAL_S=60s means this ran ~1440 times/day against
# a ~1000 requests/day plan limit -- the quota check alone exceeded the
# entire daily budget, and every job's narrate() call rides the same
# shared quota, so this single job starved the whole daemon's narration
# (and, per the plan's real numbers, would burn through in well under a
# day). GROQ_QUOTA_CHECK_INTERVAL_S decouples the real API call from
# CHECK_INTERVAL_S: the cheap common case (cache still fresh) just
# returns the last real result, no request sent, so run_job()'s
# every-cycle-even-while-escalated re-check stays actually cheap for
# this job too, not just in the comment.
GROQ_QUOTA_LOW_FRAC = 0.10   # unhealthy if remaining tokens OR requests
                              # drop below this fraction of the limit
GROQ_QUOTA_CHECK_INTERVAL_S = 1800   # 48 real requests/day -- <5% of a
                                       # 1000/day budget, leaves real
                                       # headroom for actual narration calls

_groq_quota_last_check_t = 0.0
_groq_quota_last_result = True   # assume healthy until the first real check


def check_groq_quota(node):
    global _groq_quota_last_check_t, _groq_quota_last_result

    if time.monotonic() - _groq_quota_last_check_t < GROQ_QUOTA_CHECK_INTERVAL_S:
        return _groq_quota_last_result

    import json
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "model": groq_client.MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 1,
    }).encode()
    req = urllib.request.Request(groq_client.GROQ_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {groq_client.GROQ_API_KEY}",
        "User-Agent": "mesh-healer-quota-check/1.0",
    })
    _groq_quota_last_check_t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers   # even a 429 response carries the rate-limit headers
    except OSError:
        _groq_quota_last_result = False
        return False

    try:
        remaining_tokens = int(headers.get("x-ratelimit-remaining-tokens", -1))
        limit_tokens = int(headers.get("x-ratelimit-limit-tokens", -1))
        remaining_requests = int(headers.get("x-ratelimit-remaining-requests", -1))
        limit_requests = int(headers.get("x-ratelimit-limit-requests", -1))
    except (TypeError, ValueError):
        _groq_quota_last_result = False
        return False
    if limit_tokens <= 0 or limit_requests <= 0:
        _groq_quota_last_result = False
        return False

    _groq_quota_last_result = (remaining_tokens / limit_tokens >= GROQ_QUOTA_LOW_FRAC
                                and remaining_requests / limit_requests >= GROQ_QUOTA_LOW_FRAC)
    return _groq_quota_last_result


GROQ_QUOTA_JOB = dict(name="groq_quota_healthy", verb="BENIGN_READ",
                       check=check_groq_quota, heal=None)


# Tier 2 observer-drift ("is the ruler bent, not just is the trace
# recent" -- 2026-08-15 conversation): every freshness job above trusts
# Mint's own mtime implicitly. If Mint's system clock silently drifted,
# every one of those checks would agree with each other and all be
# wrong together, since they'd all be measuring against the same bent
# ruler. This checks Mint's clock against an independent reference --
# EC2, reusing its already-established "outside gauge" role (same as
# ec2_probe.py) rather than introducing a new dependency.
#
# Deliberately NOT over SSH -- measured live 2026-08-15: a fresh SSH
# handshake per check has 2.2-2.9s RTT with ~300ms jitter (connection
# setup dominates, not real network latency), too noisy to trust for a
# clock check, and the exact problem shell_launch was built to replace
# SSH for in the first place -- mixing it into the substrate's own
# measurement path was correctly rejected. ec2_intent's existing signed
# UDP "uptime" intent gives ~162ms, low-jitter RTT instead, and its
# response already carries a real wall-clock HH:MM:SS -- no new
# listener code needed.
#
# Validated against 5 real live samples: offset was 0-1s every time
# (the 1s spread is uptime's own HH:MM:SS rounding, the measurement's
# resolution floor, not real drift) against a consistent ~162ms RTT.
# CLOCK_DRIFT_UNHEALTHY_S=10 gives real margin above that observed
# floor while still catching genuine, meaningful drift (minutes), same
# validated-with-real-margin standard as the WG handshake threshold.
CLOCK_DRIFT_UNHEALTHY_S = 10


def check_clock_drift(node):
    key = eic.load_key()
    nonce = secrets.randbits(64)
    pkt = eic.pack("uptime", time.time(), nonce, key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    try:
        t0 = time.time()
        sock.sendto(pkt, ("10.8.0.1", 58551))
        while True:
            data, _ = sock.recvfrom(512)
            t2 = time.time()
            status, detail, resp_nonce = eic.unpack_response(data, key)
            if resp_nonce != nonce or status == pic.ACCEPTED:
                continue
            break
    except OSError:
        return False
    finally:
        sock.close()

    m = re.match(r"(\d+):(\d+):(\d+)", detail)
    if not m:
        return False
    hh, mm, ss = map(int, m.groups())
    now_utc = time.gmtime((t0 + t2) / 2)
    remote_s = hh * 3600 + mm * 60 + ss
    local_s = now_utc.tm_hour * 3600 + now_utc.tm_min * 60 + now_utc.tm_sec
    return abs(remote_s - local_s) <= CLOCK_DRIFT_UNHEALTHY_S


CLOCK_DRIFT_JOB = dict(name="mint_clock_drift", verb="BENIGN_READ",
                        check=check_clock_drift, heal=None)


# pi1_thermal_layer0's log lives on Pi1 itself (deployed 2026-08-17,
# real Kuramoto stone off pi1's real thermal state -- see
# pi1_thermal_layer0/run_pi1_thermal.py), not under REPO on Mint like
# every FRESHNESS_TARGETS entry -- _fresh()'s local os.path.getmtime
# can't reach it. Same remote-check shape as check_disk_space/
# check_wg_handshake instead. Listed under MINT_JOBS rather than JOBS
# (like CLOCK_DRIFT_JOB, which also reaches a remote host) since it's
# conceptually "does mesh_healer still see a fresh pi1 thermal reading,"
# not a pi1-node-generic check meant to run identically against pi2 too.
PI1_THERMAL_LOG = "/home/martin/pi1_thermal_layer0/run_pi1_thermal.log"
PI1_THERMAL_STALE_S = 30   # same margin as FRESHNESS_TARGETS' other
                            # 0.5s-tick oscillator/telemetry logs


def check_pi1_thermal_fresh(node):
    out = _shell_run("pi1", f"stat -c %Y {PI1_THERMAL_LOG}")
    if out is None:
        return False
    try:
        mtime = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    return (time.time() - mtime) < PI1_THERMAL_STALE_S


PI1_THERMAL_FRESH_JOB = dict(name="pi1_thermal_fresh", verb="BENIGN_READ",
                              check=check_pi1_thermal_fresh, heal=None)

JOBS = [
    dict(name="listener_responsive", verb="LOCAL_DESTRUCTIVE",
         check=check_listener, heal=heal_listener),
    dict(name="disk_space", verb="BENIGN_READ",
         check=check_disk_space, heal=None),
    dict(name="wg_handshake_fresh", verb="LOCAL_HEAL",
         check=check_wg_handshake, heal=heal_wg),
    dict(name="oscillator_alive", verb="LOCAL_HEAL",
         check=check_oscillator_alive, heal=heal_oscillator),
]


def narrate(node, job_name, event, state_hash=None):
    prompt = f"""A deterministic self-heal script for a WireGuard mesh node called
"{node}" just observed this sequence of real, already-completed events
for its "{job_name}" check (the script decided and acted on its own
using fixed rules -- you are only being asked to summarize what
happened, not to decide anything):

{event}

Summarize in 1-2 plain-English sentences what happened and whether the
node is healthy now for this check. Do not suggest further actions."""
    try:
        return query_llm(prompt, state_hash=state_hash)
    except QuotaExceeded as e:
        return f"(narration skipped -- Groq quota, retry in {e.retry_after_s:.0f}s)"
    except Exception as e:
        return f"(narration failed: {e})"


def new_state():
    return dict(last_heal_attempt=0.0, consecutive_failures=0, escalated=False, notified=False)


def _safe_check(job, node):
    """Wraps job['check'](node) so a bug or unexpected exception in one
    job can't crash run_job() -- and with it, main()'s whole loop,
    taking down monitoring for every other node/job over one bad check.
    Every check function already catches its own known failure modes
    (socket.timeout, OSError, etc.); this is only a backstop for
    whatever isn't anticipated. Treated the same as check() returning
    False -- still unhealthy, normal failure/escalation path applies,
    it just doesn't take the daemon down."""
    try:
        return job["check"](node)
    except Exception as e:
        log(f"{node}/{job['name']}: check() raised {e!r} -- "
            f"treating as unhealthy")
        return False


def _safe_heal(job, node):
    """Same reasoning as _safe_check() for job['heal'](node) -- an
    unexpected exception here is reported as a failed heal attempt
    (run_job() already retries/escalates on that), not a crash."""
    try:
        return job["heal"](node)
    except Exception as e:
        return "ERROR", repr(e)


def run_job(node, job, state):
    tag = f"{node}/{job['name']}"

    healthy = _safe_check(job, node)
    if healthy:
        if state["escalated"]:
            # Real gap found 2026-08-18: a genuine transient outage
            # (Mint's own DNS dropped) escalated 9 jobs at once, and the
            # old main() loop skipped escalated (node, job) pairs
            # forever -- state["escalated"] was never cleared anywhere,
            # so all 9 stayed permanently unmonitored even once they were
            # actually healthy again, until mesh_healer itself was
            # manually restarted. Fixed here (recovery is detected and
            # logged) and in main() (escalated jobs are still cheaply
            # re-checked every cycle instead of skipped -- see that
            # function's own comment for why this doesn't reintroduce the
            # "infinite hammering" problem escalation was built to stop).
            log(f"{tag}: RECOVERED -- healthy again after "
                f"being escalated, resuming normal monitoring")
            state["escalated"] = False
        elif state["consecutive_failures"] > 0:
            log(f"{tag}: healthy again "
                f"(after {state['consecutive_failures']} failed cycle(s))")
        state["consecutive_failures"] = 0
        state["notified"] = False
        return

    if state["escalated"]:
        # Already escalated and still unhealthy -- the check itself is
        # cheap and worth re-running every cycle (that's the fix), but
        # re-attempting heal/re-narrating every cycle for an already-known,
        # already-reported problem is exactly the "infinite hammering, no
        # infinite token spend" that escalation exists to prevent. Stay
        # silent until the healthy branch above detects real recovery.
        return

    if job["heal"] is None:
        state["consecutive_failures"] += 1
        if not state["notified"]:
            state["notified"] = True
            event = f"health check: FAILED\nno automated heal available for this job"
            # Bucketed by (node, job) only -- this event's text is
            # identical every time for a given (node, job), so a repeat
            # notification within the cache TTL is a genuine duplicate,
            # not a distinct situation needing a fresh explanation.
            state_hash = f"nofix:{node}:{job['name']}"
            log(f"{tag}: {event.replace(chr(10), ' | ')}")
            log(f"{tag}: {narrate(node, job['name'], event, state_hash)}")
        if state["consecutive_failures"] >= HEAL_ATTEMPTS_BEFORE_ESCALATE:
            state["escalated"] = True
            log(f"{tag}: ESCALATED to human after "
                f"{state['consecutive_failures']} failed checks -- no automated "
                f"fix exists for this job.")
        return

    # layer0_shared_substrate gate: LOCAL_HEAL and LOCAL_DESTRUCTIVE heals
    # both require Layer0 to be fresh before mesh_healer will send them at
    # all. Widened to LOCAL_HEAL 2026-09-01 (heal_wg, heal_oscillator) --
    # originally scoped to LOCAL_DESTRUCTIVE only (heal_listener's root
    # exec: restart), same reasoning n_wave_pacer.py only ever gates its
    # multiplier and never `boost`: gate the one thing first, widen later
    # if it turns out to matter rather than gating everything up front.
    # Checked every cycle, cheap (drains an already-buffered socket, no
    # new I/O) -- same cost profile as check_oscillator_alive(). Does NOT
    # touch last_heal_attempt/cooldown or consecutive_failures/escalation
    # -- a substrate outage is not this job's fault, and must not count
    # against it or reset its cooldown.
    if job["verb"] in ("LOCAL_HEAL", "LOCAL_DESTRUCTIVE") and not layer0_fresh():
        log(f"{tag}: LAYER0 DENY -- substrate stale, withholding "
            f"{job['verb']} heal action this cycle")
        return

    now = time.time()
    if now - state["last_heal_attempt"] < HEAL_COOLDOWN_S:
        log(f"{tag}: unhealthy, within cooldown, skipping heal attempt")
        return

    state["last_heal_attempt"] = now
    status, detail = _safe_heal(job, node)
    time.sleep(HEAL_VERIFY_DELAY_S)
    verified = _safe_check(job, node)

    event = (f"health check: FAILED\nheal action sent\n"
             f"gate response: {status} ({detail})\n"
             f"post-heal health check: {'PASSED' if verified else 'STILL FAILED'}")
    # Bucketed by outcome CATEGORY (status + pass/fail), not the raw
    # detail text (real command stdout/gate messages vary slightly even
    # for the same underlying situation, which would defeat the cache
    # if included in the hash).
    state_hash = f"heal:{node}:{job['name']}:{status}:{'passed' if verified else 'failed'}"
    log(f"{tag}: {event.replace(chr(10), ' | ')}")
    log(f"{tag}: {narrate(node, job['name'], event, state_hash)}")

    if verified:
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= HEAL_ATTEMPTS_BEFORE_ESCALATE:
            state["escalated"] = True
            log(f"{tag}: ESCALATED to human after "
                f"{state['consecutive_failures']} failed heal attempts -- "
                f"not retrying automatically.")


MINT_JOBS = LOCAL_JOBS + FRESHNESS_JOBS + [GROQ_QUOTA_JOB, CLOCK_DRIFT_JOB, PI1_THERMAL_FRESH_JOB]


def main():
    log(f"watching {list(NODES)}, jobs={[j['name'] for j in JOBS]}, "
        f"local(mint) jobs={[j['name'] for j in MINT_JOBS]}, "
        f"check every {CHECK_INTERVAL_S}s")
    state = {(node, job["name"]): new_state() for node in NODES for job in JOBS}
    state.update({("mint", job["name"]): new_state() for job in MINT_JOBS})

    while True:
        # No more "if escalated: continue" skip here -- that was the bug
        # (see run_job()'s comment): an escalated (node, job) pair was
        # skipped forever, so a real outage that later recovered stayed
        # permanently unmonitored until mesh_healer itself was restarted.
        # Every job's check() is cheap (a read, not a heal attempt or LLM
        # call), so re-running it every cycle even while escalated costs
        # nothing real -- run_job() itself now handles staying silent
        # while still-unhealthy-and-escalated, and detecting+logging real
        # recovery.
        for node in NODES:
            for job in JOBS:
                run_job(node, job, state[(node, job["name"])])
        for job in MINT_JOBS:
            run_job("mint", job, state[("mint", job["name"])])
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
