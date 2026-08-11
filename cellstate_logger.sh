#!/bin/bash
# Appends /tmp/cellstate_state.json with a wall-clock timestamp once a second.
# Companion to quartz_cellstate_node.c, which writes that file each round.
while true; do
    if [ -f /tmp/cellstate_state.json ]; then
        echo "$(date +%s.%N) $(cat /tmp/cellstate_state.json)" >> ~/claude/cellstate_trajectory.log
    fi
    sleep 1
done
