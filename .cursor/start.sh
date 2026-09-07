#!/usr/bin/env bash
# Per-boot startup for the Timberdoodle Cloud Agent environment.
# Starts the Docker daemon (no systemd in the VM) with the nested-container
# networking fix in place, then brings the whole compose stack up healthy.
set -euxo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Container-to-container traffic on a compose bridge is dropped in this nested
# VM when bridged packets traverse the iptables FORWARD chain. Turning off
# bridge-nf makes same-network traffic bypass it (published ports still work).
sudo modprobe br_netfilter 2>/dev/null || true
if [ -e /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
  sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 >/dev/null || true
  sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 >/dev/null || true
fi

# Start dockerd if it isn't already serving (idempotent across restarts).
if ! sudo docker info >/dev/null 2>&1; then
  sudo nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 1 60); do
    sudo docker info >/dev/null 2>&1 && break
    sleep 1
  done
fi
sudo docker info >/dev/null 2>&1 || { echo "dockerd failed to start"; sudo tail -n 40 /tmp/dockerd.log; exit 1; }

# Bring up all infra + APIs + gateway; --wait blocks until healthchecks pass.
sudo docker compose up -d --wait

echo "Timberdoodle stack is up (gateway on http://localhost:8080)."
