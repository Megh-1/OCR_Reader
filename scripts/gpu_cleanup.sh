#!/usr/bin/env bash
# Run from the LAPTOP after the demo. Stops the processes recorded in ~/idp_sprint/pids on the GPU host (only those, and only
# if they still run something under ~/idp_sprint), deletes ~/idp_sprint, then stops the local tunnel. Touches nothing else.
set -u
ssh -o BatchMode=yes gpu 'S="$HOME/idp_sprint"
if [ -f "$S/pids" ]; then
  for p in $(cat "$S/pids"); do
    if ps -p "$p" -o args= 2>/dev/null | grep -q "idp_sprint"; then kill "$p" && echo "stopped $p"; else echo "skip $p (gone or not ours)"; fi
  done
fi
sleep 3
rm -rf "$S" && echo "removed $S"'
PIDFILE="${TMPDIR:-/tmp}/idp_gpu_tunnel.pid"
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE")
  pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null && echo "stopped local tunnel $pid"; rm -f "$PIDFILE"
fi
