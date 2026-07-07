#!/bin/bash
set -e

# ── CONFIG — add a row per device; everything else stays identical ────────────
# PROXY_IPS[i]  = public identity IP the Pi claims via proxy ARP
# TARGET_IPS[i] = real device IP (DNAT destination)
PROXY_IPS=(10.0.0.130 10.0.0.200)   # Firestick  Mint
TARGET_IPS=(10.0.0.131 10.0.0.71)   # Firestick  Mint
NFQUEUE_NUM=1              # queue number — must match quic-sni-gate.py
NFQUEUE_MATCH="-s 10.0.0.131 -p udp --dport 443 -m conntrack --ctstate NEW"  # flows -> SNI gate (Firestick only)
# ─────────────────────────────────────────────────────────────────────────────

# Dummy interface so proxy_arp has a non-eth0 route to each claimed IP
ip link add dummy0 type dummy 2>/dev/null || true
ip link set dummy0 up

# enable_proxy_arp_for_mint (and Firestick): Pi answers ARP for all proxy IPs
echo 1 > /proc/sys/net/ipv4/conf/eth0/proxy_arp
# arp_accept_for_mint: accept unsolicited ARP replies so the table stays clean
echo 1 > /proc/sys/net/ipv4/conf/eth0/arp_accept

# Per-device: claim proxy IP, DNAT → real IP, MASQUERADE return, allow FORWARD
for i in "${!PROXY_IPS[@]}"; do
  PROXY_IP=${PROXY_IPS[$i]}
  TARGET_IP=${TARGET_IPS[$i]}

  ip addr add ${PROXY_IP}/32 dev dummy0 2>/dev/null || true

  iptables -t nat -C PREROUTING -d $PROXY_IP -j DNAT --to-destination $TARGET_IP 2>/dev/null || \
      iptables -t nat -A PREROUTING -d $PROXY_IP -j DNAT --to-destination $TARGET_IP

  iptables -t nat -C POSTROUTING -d $TARGET_IP -j MASQUERADE 2>/dev/null || \
      iptables -t nat -A POSTROUTING -d $TARGET_IP -j MASQUERADE

  # MASQUERADE outbound internet traffic from device (src→internet via Pi)
  iptables -t nat -C POSTROUTING -s $TARGET_IP ! -d 10.0.0.0/8 -o eth0 -j MASQUERADE 2>/dev/null || \
      iptables -t nat -A POSTROUTING -s $TARGET_IP ! -d 10.0.0.0/8 -o eth0 -j MASQUERADE

  iptables -C FORWARD -d $TARGET_IP -j ACCEPT 2>/dev/null || \
      iptables -I FORWARD 1 -d $TARGET_IP -j ACCEPT

  iptables -C FORWARD -s $TARGET_IP -j ACCEPT 2>/dev/null || \
      iptables -I FORWARD 1 -s $TARGET_IP -j ACCEPT
done

# iptables mangle: mark packets by CDN source IP / payload
# (runs after DNAT, before MASQUERADE — CDN src IP still visible)

# BBC iPlayer — mark 40
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "bbc" --algo bm -j MARK --set-mark 40 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "bbc" --algo bm -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 151.101.0.0/16  -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 151.101.0.0/16  -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 23.66.0.0/16    -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 23.66.0.0/16    -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 23.67.0.0/16    -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 23.67.0.0/16    -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 23.72.0.0/16    -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 23.72.0.0/16    -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 23.102.0.0/16   -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 23.102.0.0/16   -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 212.58.224.0/19 -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 212.58.224.0/19 -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 212.58.240.0/20 -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 212.58.240.0/20 -j MARK --set-mark 40
iptables -t mangle -C FORWARD -s 212.58.246.0/23 -j MARK --set-mark 40 2>/dev/null || iptables -t mangle -A FORWARD -s 212.58.246.0/23 -j MARK --set-mark 40

# Amazon Prime Video — mark 10 (uncapped)
iptables -t mangle -C FORWARD -s 52.84.0.0/15    -j MARK --set-mark 10 2>/dev/null || iptables -t mangle -A FORWARD -s 52.84.0.0/15    -j MARK --set-mark 10

# YouTube/Google — mark 10 (928 ranges via cdn-ip-database, loaded into ipset)
/usr/sbin/ipset create google_cdn_new hash:net family inet hashsize 4096 maxelem 65536 2>/dev/null || true
/usr/sbin/ipset flush google_cdn_new
tail -n +2 /etc/shaper/google_cdn.ipset | sed 's/google_cdn /google_cdn_new /' | /usr/sbin/ipset restore -exist
/usr/sbin/ipset create google_cdn hash:net family inet hashsize 4096 maxelem 65536 2>/dev/null || true
/usr/sbin/ipset swap google_cdn google_cdn_new 2>/dev/null || true
/usr/sbin/ipset destroy google_cdn_new 2>/dev/null || true
iptables -t mangle -C FORWARD -m set --match-set google_cdn src -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -I FORWARD 1 -m set --match-set google_cdn src -j MARK --set-mark 10

# YouTube video CDN — mark 10 via SNI (TCP/443 TLS ClientHello, SNI is plaintext)
# Matches r*---sn-*.googlevideo.com and any *.googlevideo.com hostname.
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "googlevideo.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "googlevideo.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10

# Sky Go — mark 20
iptables -t mangle -C FORWARD -s 90.216.0.0/13   -j MARK --set-mark 20 2>/dev/null || iptables -t mangle -A FORWARD -s 90.216.0.0/13   -j MARK --set-mark 20
iptables -t mangle -C FORWARD -s 2.120.0.0/14    -j MARK --set-mark 20 2>/dev/null || iptables -t mangle -A FORWARD -s 2.120.0.0/14    -j MARK --set-mark 20
iptables -t mangle -C FORWARD -s 2.122.0.0/15    -j MARK --set-mark 20 2>/dev/null || iptables -t mangle -A FORWARD -s 2.122.0.0/15    -j MARK --set-mark 20
iptables -t mangle -C FORWARD -s 2.124.0.0/16    -j MARK --set-mark 20 2>/dev/null || iptables -t mangle -A FORWARD -s 2.124.0.0/16    -j MARK --set-mark 20

# Netflix Open Connect (AS2906 — Netflix-owned, no overlap) — mark 15
iptables -t mangle -C FORWARD -s 45.57.0.0/17      -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 45.57.0.0/17      -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 64.120.128.0/17   -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 64.120.128.0/17   -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 66.197.128.0/17   -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 66.197.128.0/17   -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 108.175.32.0/20   -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 108.175.32.0/20   -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 192.173.64.0/18   -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 192.173.64.0/18   -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 198.38.96.0/19    -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 198.38.96.0/19    -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 198.45.48.0/20    -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 198.45.48.0/20    -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 208.75.76.0/22    -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 208.75.76.0/22    -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 23.246.0.0/18     -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 23.246.0.0/18     -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 37.77.184.0/21    -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 37.77.184.0/21    -j MARK --set-mark 15
iptables -t mangle -C FORWARD -s 69.53.224.0/19    -j MARK --set-mark 15 2>/dev/null || iptables -t mangle -A FORWARD -s 69.53.224.0/19    -j MARK --set-mark 15

# Disney+ (CloudFront non-overlapping + Qwilt) — mark 25
iptables -t mangle -C FORWARD -s 13.32.0.0/15      -j MARK --set-mark 25 2>/dev/null || iptables -t mangle -A FORWARD -s 13.32.0.0/15      -j MARK --set-mark 25
iptables -t mangle -C FORWARD -s 54.230.0.0/16     -j MARK --set-mark 25 2>/dev/null || iptables -t mangle -A FORWARD -s 54.230.0.0/16     -j MARK --set-mark 25
iptables -t mangle -C FORWARD -s 54.239.128.0/18   -j MARK --set-mark 25 2>/dev/null || iptables -t mangle -A FORWARD -s 54.239.128.0/18   -j MARK --set-mark 25
iptables -t mangle -C FORWARD -s 99.84.0.0/16      -j MARK --set-mark 25 2>/dev/null || iptables -t mangle -A FORWARD -s 99.84.0.0/16      -j MARK --set-mark 25
iptables -t mangle -C FORWARD -s 99.86.0.0/16      -j MARK --set-mark 25 2>/dev/null || iptables -t mangle -A FORWARD -s 99.86.0.0/16      -j MARK --set-mark 25
iptables -t mangle -C FORWARD -s 212.162.0.0/16    -j MARK --set-mark 25 2>/dev/null || iptables -t mangle -A FORWARD -s 212.162.0.0/16    -j MARK --set-mark 25

# ITVX (Akamai — Fastly 151.101.0.0/16 excluded to avoid BBC conflict) — mark 35
iptables -t mangle -C FORWARD -s 2.16.0.0/13       -j MARK --set-mark 35 2>/dev/null || iptables -t mangle -A FORWARD -s 2.16.0.0/13       -j MARK --set-mark 35
iptables -t mangle -C FORWARD -s 23.32.0.0/11      -j MARK --set-mark 35 2>/dev/null || iptables -t mangle -A FORWARD -s 23.32.0.0/11      -j MARK --set-mark 35
iptables -t mangle -C FORWARD -s 104.64.0.0/11     -j MARK --set-mark 35 2>/dev/null || iptables -t mangle -A FORWARD -s 104.64.0.0/11     -j MARK --set-mark 35
iptables -t mangle -C FORWARD -s 184.24.0.0/13     -j MARK --set-mark 35 2>/dev/null || iptables -t mangle -A FORWARD -s 184.24.0.0/13     -j MARK --set-mark 35

# Jellyfin — mark 5 (local server: 10.0.0.71:8096)
iptables -t mangle -C FORWARD -s 10.0.0.71 -p tcp --sport 8096 -j MARK --set-mark 5 2>/dev/null || \
    iptables -t mangle -A FORWARD -s 10.0.0.71 -p tcp --sport 8096 -j MARK --set-mark 5

# UDP/443 QUIC — SNI gate via NFQUEUE (quic-sni-gate.py)
# Only NEW flows go to userspace; daemon decrypts QUIC Initial (RFC 9001 keys),
# accepts *.googlevideo.com with mark 10, drops everything else (→ TCP fallback).
iptables -t mangle -C FORWARD $NFQUEUE_MATCH -j NFQUEUE --queue-num $NFQUEUE_NUM 2>/dev/null || \
    iptables -t mangle -A FORWARD $NFQUEUE_MATCH -j NFQUEUE --queue-num $NFQUEUE_NUM

# CONNMARK: persist service mark (lower byte) across the flow so Short Header
# QUIC packets and TCP data segments inherit the mark without re-inspection.
# Generic — no per-device changes needed here.
iptables -t mangle -C FORWARD -m conntrack --ctstate NEW -m mark ! --mark 0/0xff -j CONNMARK --save-mark --nfmask 0xff --ctmask 0xff 2>/dev/null || \
    iptables -t mangle -A FORWARD -m conntrack --ctstate NEW -m mark ! --mark 0/0xff -j CONNMARK --save-mark --nfmask 0xff --ctmask 0xff
iptables -t mangle -C PREROUTING -m conntrack --ctstate RELATED,ESTABLISHED -j CONNMARK --restore-mark --nfmask 0xff --ctmask 0xff 2>/dev/null || \
    iptables -t mangle -A PREROUTING -m conntrack --ctstate RELATED,ESTABLISHED -j CONNMARK --restore-mark --nfmask 0xff --ctmask 0xff

# tc HTB classes
tc qdisc del dev eth0 root 2>/dev/null || true
tc qdisc add dev eth0 root handle 1: htb default 10
tc class add dev eth0 parent 1: classid 1:1  htb rate 1000mbit
tc class add dev eth0 parent 1: classid 1:5  htb rate 1000mbit ceil 1000mbit  burst 32kb  # Jellyfin    → uncapped
tc class add dev eth0 parent 1: classid 1:10 htb rate 1000mbit ceil 1000mbit              # default / YouTube → uncapped
tc class add dev eth0 parent 1: classid 1:15 htb rate 15mbit   ceil 15mbit   burst 32kb  # Netflix     → 15 Mbps
tc class add dev eth0 parent 1: classid 1:20 htb rate 5mbit    ceil 5mbit    burst 32kb  # Sky / catch-all → 5 Mbps
tc class add dev eth0 parent 1: classid 1:25 htb rate 8mbit    ceil 8mbit    burst 32kb  # Disney+     → 8 Mbps
tc class add dev eth0 parent 1: classid 1:35 htb rate 4mbit    ceil 4mbit    burst 32kb  # ITVX        → 4 Mbps
tc class add dev eth0 parent 1: classid 1:40 htb rate 4500kbit ceil 4500kbit burst 32kb  # BBC iPlayer → 4.5 Mbps
tc class add dev eth0 parent 1: classid 1:11 htb rate 1mbit    ceil 1mbit    burst 16kb  # Google Ads  → 1 Mbps
tc class add dev eth0 parent 1: classid 1:50 htb rate 1000mbit ceil 1000mbit              # Mint general → uncapped
tc qdisc add dev eth0 parent 1:5  handle 5:  fq_codel
tc qdisc add dev eth0 parent 1:15 handle 15: fq_codel
tc qdisc add dev eth0 parent 1:20 handle 20: fq_codel
tc qdisc add dev eth0 parent 1:25 handle 25: fq_codel
tc qdisc add dev eth0 parent 1:35 handle 35: fq_codel
tc qdisc add dev eth0 parent 1:40 handle 40: fq_codel
tc qdisc add dev eth0 parent 1:11 handle 11: fq_codel

# Google Ads — mark 11 via SNI (TCP/443 ClientHello dport direction — plaintext SNI)
# Replaces broken sport-443 encrypted-payload matching with proper ClientHello inspection.
# doubleclick_ads_rule
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "doubleclick" --algo bm --from 40 --to 400 -j MARK --set-mark 11 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "doubleclick" --algo bm --from 40 --to 400 -j MARK --set-mark 11
# googlesyndication_ads_rule
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "googlesyndication" --algo bm --from 40 --to 400 -j MARK --set-mark 11 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "googlesyndication" --algo bm --from 40 --to 400 -j MARK --set-mark 11
# youtubei_ads_rule
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "youtubei" --algo bm --from 40 --to 400 -j MARK --set-mark 11 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "youtubei" --algo bm --from 40 --to 400 -j MARK --set-mark 11
# yt3_metadata_rule
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "yt3.ggpht" --algo bm --from 40 --to 400 -j MARK --set-mark 11 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "yt3.ggpht" --algo bm --from 40 --to 400 -j MARK --set-mark 11
# adservice_rule
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "googleadservices" --algo bm --from 40 --to 400 -j MARK --set-mark 11 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "googleadservices" --algo bm --from 40 --to 400 -j MARK --set-mark 11

# tc filters: service-specific (fw mark, prio 1) then device catch-all (u32, prio 2)
tc filter add dev eth0 parent 1: prio 1 handle 5  fw flowid 1:5   # Jellyfin    → uncapped
tc filter add dev eth0 parent 1: prio 1 handle 10 fw flowid 1:10  # YouTube     → uncapped
tc filter add dev eth0 parent 1: prio 1 handle 11 fw flowid 1:11  # Google Ads  → 1 Mbps
tc filter add dev eth0 parent 1: prio 1 handle 15 fw flowid 1:15  # Netflix     → 15 Mbps
tc filter add dev eth0 parent 1: prio 1 handle 20 fw flowid 1:20  # Sky         → 5 Mbps
tc filter add dev eth0 parent 1: prio 1 handle 25 fw flowid 1:25  # Disney+     → 8 Mbps
tc filter add dev eth0 parent 1: prio 1 handle 35 fw flowid 1:35  # ITVX        → 4 Mbps
tc filter add dev eth0 parent 1: prio 1 handle 40 fw flowid 1:40  # BBC iPlayer → 4.5 Mbps
# Catch-all: Firestick unclassified → 5 Mbps; Mint general → uncapped
tc filter add dev eth0 parent 1: prio 2 protocol ip u32 match ip dst 10.0.0.131/32 flowid 1:20
tc filter add dev eth0 parent 1: prio 2 protocol ip u32 match ip dst 10.0.0.71/32  flowid 1:50

# Prime Video SNI whitelist — mark 10 (must come AFTER ads rules; last MARK wins)
# These domains serve video frames / DRM; overwrite any prior mark-11 hit.
# prime_atv_ps
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "atv-ps.amazon.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "atv-ps.amazon.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10
# prime_atv_ext
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "atv-ext.amazon.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "atv-ext.amazon.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10
# prime_dp_video
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "dp-video-na.amazon.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "dp-video-na.amazon.com" --algo bm --from 40 --to 400 -j MARK --set-mark 10
# prime_cloudfront
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "cloudfront.net" --algo bm --from 40 --to 400 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "cloudfront.net" --algo bm --from 40 --to 400 -j MARK --set-mark 10
# prime_akamai
iptables -t mangle -C FORWARD -p tcp --dport 443 -m string --string "akamaihd.net" --algo bm --from 40 --to 400 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -p tcp --dport 443 -m string --string "akamaihd.net" --algo bm --from 40 --to 400 -j MARK --set-mark 10

# qBittorrent — mark 10 (uncapped), Mint (10.0.0.71), port 47997 TCP+UDP
iptables -t mangle -C FORWARD -s 10.0.0.71 -p tcp --sport 47997 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -s 10.0.0.71 -p tcp --sport 47997 -j MARK --set-mark 10
iptables -t mangle -C FORWARD -d 10.0.0.71 -p tcp --dport 47997 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -d 10.0.0.71 -p tcp --dport 47997 -j MARK --set-mark 10
iptables -t mangle -C FORWARD -s 10.0.0.71 -p udp --sport 47997 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -s 10.0.0.71 -p udp --sport 47997 -j MARK --set-mark 10
iptables -t mangle -C FORWARD -d 10.0.0.71 -p udp --dport 47997 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A FORWARD -d 10.0.0.71 -p udp --dport 47997 -j MARK --set-mark 10

# Wave-pacer — Firestick downlink smoothing via NFQUEUE (wave-pacer.py, queue 10)
# Mint is shaped by HTB directly — wave-pacer adds latency to general browsing
WAVE_PACER_QUEUE_NUM=10
iptables -t mangle -C FORWARD -d 10.0.0.131 -j NFQUEUE --queue-num $WAVE_PACER_QUEUE_NUM 2>/dev/null || \
    iptables -t mangle -A FORWARD -d 10.0.0.131 -j NFQUEUE --queue-num $WAVE_PACER_QUEUE_NUM

# Default mark: Firestick outbound starts as mark 10 (uncapped).
# CDN rules in FORWARD override this per-service before NFQUEUE fires.
iptables -t mangle -C PREROUTING -s 10.0.0.131 -j MARK --set-mark 10 2>/dev/null || \
    iptables -t mangle -A PREROUTING -s 10.0.0.131 -j MARK --set-mark 10
