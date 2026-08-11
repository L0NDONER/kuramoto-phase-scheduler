"""relay_to_ec2.py — bridges LAN multicast Layer 0 reports out to a
remote box's public IP as unicast, since multicast doesn't cross from
a home LAN to the public internet.

Runs on Mint. Listens on the same local multicast group/port
layer0_report.py already uses (239.0.0.6:7460), and for every report
received from Mint/Pi1/Pi2, forwards the exact same signed... no --
NOT signed (layer0_report.py has no HMAC, unlike the intent channels;
it's LAN-trust-only telemetry, never used to trigger actuation
directly) -- packet as unicast to the remote host.

This is a dumb byte-for-byte relay, not a re-encode -- whatever
layer0_report.py's wire format is, this doesn't need to know or care.

Usage: python3 relay_to_ec2.py <remote_ip> [remote_port]
"""
import socket
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from layer0_report import mcast_in, PORT as LOCAL_PORT

REMOTE_IP = sys.argv[1]
REMOTE_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else LOCAL_PORT


def main():
    in_sock = mcast_in()
    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[relay] forwarding local multicast reports -> {REMOTE_IP}:{REMOTE_PORT}",
          flush=True)

    in_sock.setblocking(True)
    while True:
        data, addr = in_sock.recvfrom(64)
        out_sock.sendto(data, (REMOTE_IP, REMOTE_PORT))
        print(f"[relay] forwarded {len(data)}B from {addr}", flush=True)


if __name__ == "__main__":
    main()
