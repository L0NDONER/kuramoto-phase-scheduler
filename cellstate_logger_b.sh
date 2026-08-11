#!/bin/bash
# Companion logger for quartz_cellstate_node_b.c (opposite-corner start).
while true; do
    if [ -f /tmp/cellstate_state_b.json ]; then
        echo "$(date +%s.%N) $(cat /tmp/cellstate_state_b.json)" >> ~/claude/cellstate_trajectory_b.log
    fi
    sleep 1
done
