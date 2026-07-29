#!/bin/bash
# start.sh — bring up quartz_node (substrate) + quartz_metro_node (portfolio
# job, coordinator on Mint) + the two gain daemons, on their real hosts.
# Run after deploy.sh. Requires root on Mint (tc) and pi (tc).
#
# Host roles (fixed, not discovered):
#   Mint (10.0.0.71)  node_id=1  metro: coordinator   + quartz_wan_gain.py
#   pi   (10.0.0.122) node_id=2  metro: worker slot 0 + quartz_firestick_gain.py
#   pi2  (10.0.0.174) node_id=3  metro: worker slot 1
set -e
D="~/quartz-os"

echo "== Mint =="
nohup ./quartz_node 1 10.0.0.122 10.0.0.174 >quartz_node.log 2>&1 &
disown
sleep 1
nohup ./quartz_metro_node coord portfolio 10.0.0.122 10.0.0.174 >quartz_metro_node.log 2>&1 &
disown
sudo -b nohup python3 quartz_wan_gain.py >quartz_wan_gain.log 2>&1 &

ssh pi "cd $D && nohup ./quartz_node 2 10.0.0.71 10.0.0.174 >quartz_node.log 2>&1 & disown" || true
ssh pi "cd $D && nohup ./quartz_metro_node worker portfolio 0 10.0.0.71 >quartz_metro_node.log 2>&1 & disown" || true
ssh pi "cd $D && sudo -b nohup python3 quartz_firestick_gain.py >quartz_firestick_gain.log 2>&1 &" || true

ssh pi2 "cd $D && nohup ./quartz_node 3 10.0.0.71 10.0.0.122 >quartz_node.log 2>&1 & disown" || true
ssh pi2 "cd $D && nohup ./quartz_metro_node worker portfolio 1 10.0.0.71 >quartz_metro_node.log 2>&1 & disown" || true

echo "started (note: ssh backgrounding commands may hang the local ssh client even on success -- check with 'ps' on the remote host, don't wait)"
