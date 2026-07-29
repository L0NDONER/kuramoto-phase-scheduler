#!/bin/bash
# start.sh — bring up quartz_node (substrate) + quartz_metro_node (portfolio
# job, coordinator on Mint) + the two gain daemons, on their real hosts.
# Run after deploy.sh. Requires root on Mint (tc) and pi (tc).
#
# Host roles (fixed, not discovered):
#   Mint (10.0.0.71)  node_id=1  metro: coordinator   + quartz_wan_gain.py
#   pi   (10.0.0.122) node_id=2  metro: worker slot 0 + quartz_firestick_gain.py
#   pi2  (10.0.0.174) node_id=3  metro: worker slot 1
#
# Known quirk: an ssh command that backgrounds a detached child on the Pis
# (`... & disown`) can hang the *local* ssh client even though the remote
# process detaches fine. Each remote call below is wrapped in `timeout 5`
# so one hung call can't block the rest of the script indefinitely --
# don't wrap the whole script in an external `timeout` instead, that sends
# its signal to the whole process group and kills the local daemons this
# script just backgrounded (quartz_node/quartz_metro_node on Mint aren't
# under sudo, so they share Mint's process group and die with it).
set -e
D="~/claude"

cd "$HOME/claude"

echo "== Mint =="
nohup ./quartz_node 1 10.0.0.122 10.0.0.174 >quartz_node.log 2>&1 &
disown
sleep 1
nohup ./quartz_metro_node coord portfolio 10.0.0.122 10.0.0.174 >portfolio_coord.log 2>&1 &
disown
sudo -b nohup python3 quartz_wan_gain.py >quartz_wan_gain.log 2>&1 &

timeout 5 ssh pi "cd $D && nohup ./quartz_node 2 10.0.0.71 10.0.0.174 >quartz_node.log 2>&1 & disown" || true
timeout 5 ssh pi "cd $D && nohup ./quartz_metro_node worker portfolio 0 10.0.0.71 >portfolio_worker0.log 2>&1 & disown" || true
timeout 5 ssh pi "cd $D && sudo -b nohup python3 quartz_firestick_gain.py >quartz_firestick_gain.log 2>&1 &" || true

timeout 5 ssh pi2 "cd $D && nohup ./quartz_node 3 10.0.0.71 10.0.0.122 >quartz_node.log 2>&1 & disown" || true
timeout 5 ssh pi2 "cd $D && nohup ./quartz_metro_node worker portfolio 1 10.0.0.71 >portfolio_worker1.log 2>&1 & disown" || true

echo "started (each remote command is wrapped in 'timeout 5' since the ssh"
echo "client can hang on a successfully-backgrounded remote process -- see"
echo "the known-quirk note above. Verify with 'ps' on the remote host.)"
