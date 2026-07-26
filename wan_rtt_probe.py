#!/usr/bin/env python3
"""
wan_rtt_probe.py — RTT/jitter characterisation of a WAN hop (default:
TalkTalk's real first-hop gateway, found via traceroute past the Eero).

This measures path *geometry* — latency distribution, jitter, tail
spikes — not an oscillator. The ISP's own quartz isn't reachable (no
ICMP Timestamp reply past the Eero), so this is deliberately just RTT
statistics, reusing adev.py's Allan-deviation machinery to describe how
noisy/stable the path is over different timescales, the same way it's
already used for the presence-chain gap threshold.

Standalone unicast tool: no multicast, no beacon/quartz_beacon.py
dependency, no interaction with the production AxisPulse substrate.

Usage:
    sudo python3 wan_rtt_probe.py [target_ip] [--count N] [--interval S] [--csv FILE]
"""
import argparse
import socket
import struct
import sys
import time

from adev import adev, print_adev_table, gap_threshold

CLOCK = time.CLOCK_MONOTONIC_RAW
now_raw = lambda: time.clock_gettime(CLOCK)

ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
DEFAULT_TARGET = "78.149.64.1"   # TalkTalk first real hop past the Eero


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def build_echo(ident, seq, payload=b"wan-rtt-probe"):
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, ident, seq)
    csum = checksum(header + payload)
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, csum, ident, seq)
    return header + payload


def raw_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    s.settimeout(2.0)
    return s


def probe(sock, target, ident, seq):
    pkt = build_echo(ident, seq)
    t0 = now_raw()
    sock.sendto(pkt, (target, 0))
    try:
        while True:
            data, addr = sock.recvfrom(1024)
            t1 = now_raw()
            ip_header_len = (data[0] & 0x0F) * 4
            icmp = data[ip_header_len:]
            if len(icmp) < 8:
                continue
            itype, icode, _csum, rident, rseq = struct.unpack("!BBHHH", icmp[:8])
            if itype != ICMP_ECHO_REPLY or rident != ident or rseq != seq:
                continue
            return t1 - t0
    except socket.timeout:
        return None


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default=DEFAULT_TARGET)
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    print(f"[wan_rtt_probe] target={args.target} count={args.count} interval={args.interval}s", flush=True)

    try:
        sock = raw_socket()
    except PermissionError:
        sys.exit("raw ICMP sockets need root — rerun with sudo")

    ident = 0xFEED & 0xFFFF
    rtts, sent_at = [], []
    lost = 0

    for seq in range(args.count):
        t_send = time.time()
        rtt = probe(sock, args.target, ident, seq)
        if rtt is None:
            lost += 1
        else:
            rtts.append(rtt)
            sent_at.append(t_send)
        time.sleep(args.interval)

    if len(rtts) < 10:
        sys.exit(f"only {len(rtts)} replies received out of {args.count} — target may be rate-limiting/filtering ICMP")

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("t_unix,rtt_s\n")
            for t, r in zip(sent_at, rtts):
                f.write(f"{t:.6f},{r:.9f}\n")
        print(f"[wan_rtt_probe] wrote {len(rtts)} rows to {args.csv}")

    srt = sorted(rtts)
    mean = sum(rtts) / len(rtts)
    var = sum((r - mean) ** 2 for r in rtts) / len(rtts)
    loss_pct = 100.0 * lost / args.count

    print(f"\n=== RTT distribution — {args.target} ===")
    print(f"  n={len(rtts)}  loss={lost}/{args.count} ({loss_pct:.1f}%)")
    print(f"  mean={mean*1000:.3f}ms  std={var**0.5*1000:.3f}ms")
    print(f"  min={srt[0]*1000:.3f}ms  p50={percentile(srt,0.50)*1000:.3f}ms  "
          f"p95={percentile(srt,0.95)*1000:.3f}ms  p99={percentile(srt,0.99)*1000:.3f}ms  "
          f"max={srt[-1]*1000:.3f}ms")

    tau0 = args.interval
    print(f"\n=== Allan deviation of RTT — tau0={tau0*1000:.1f}ms ===")
    ad = print_adev_table(rtts, tau0)
    gt = gap_threshold(ad)
    if gt:
        g_tau, g_ad = gt
        print(f"\n  Quietest timescale: ADEV={g_ad*1000:.4f}ms at tau={g_tau:.2f}s")
        print(f"  Below this tau, RTT noise is still falling (averaging helps); "
              f"above it, the path has a noise floor averaging won't beat.")


if __name__ == "__main__":
    main()
