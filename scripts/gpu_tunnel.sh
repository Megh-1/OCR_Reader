#!/usr/bin/env bash
# Keep an ssh tunnel to the GPU host's model server(s) alive, restarting it when it drops.
#   scripts/gpu_tunnel.sh [port ...]        (default: 11434 = the Ollama server that serves every page engine)
# BIND defaults to 0.0.0.0 so containers can reach it through host.docker.internal; set BIND=127.0.0.1 to keep it local.
# Stop it with scripts/gpu_cleanup.sh (or kill this script). Needs the `Host gpu` entry in ~/.ssh/config (key auth only).
set -u
BIND="${BIND:-0.0.0.0}"
PORTS=("$@"); [ ${#PORTS[@]} -eq 0 ] && PORTS=(11434)
FWD=()
for p in "${PORTS[@]}"; do FWD+=(-L "${BIND}:${p}:127.0.0.1:${p}"); done
echo $$ > "${TMPDIR:-/tmp}/idp_gpu_tunnel.pid"
while true; do
  ssh -N -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes "${FWD[@]}" gpu
  echo "$(date +%T) tunnel dropped, restarting in 5 s" >&2
  sleep 5
done
