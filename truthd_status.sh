#!/bin/bash
# truthd_status.sh -- read-only quorum-tier check across every host running
# truthd, over the WireGuard mesh (10.8.0.x) rather than LAN hostnames, so
# it works the same whether you're on the LAN or roaming. Doesn't write
# anything anywhere -- just opens each host's local Unix socket (over SSH,
# since truthd.c deliberately never exposes that socket to the network
# itself) and prints the one-line tier reply.
set -u

query() {
    local label="$1" sshcmd="$2"
    local tier
    tier=$(eval "$sshcmd" 'printf "" | timeout 2 nc -U /tmp/truthd.sock' 2>/dev/null)
    printf "%-6s %s\n" "$label" "${tier:-UNREACHABLE}"
}

query "mint"  ""
query "pi1"   "ssh -i ~/.ssh/pi-hole -o ConnectTimeout=3 martin@10.8.0.7"
query "pi2"   "ssh -o ConnectTimeout=3 martin@10.8.0.5"
query "ec2"   "ssh -i ~/.ssh/AWS-Secure.pem -o ConnectTimeout=3 ubuntu@10.8.0.1"
