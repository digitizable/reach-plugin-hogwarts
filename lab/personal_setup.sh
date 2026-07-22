#!/usr/bin/env bash
# Personal lab: plane + mock Docker agents + host agent + Windows enroll pack.
# Safe to re-run. Does not start libvirt VMs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NET=hogwarts-lab
PLANE_IMG=hogwarts-plane:lab
AGENT_IMG=hogwarts-agent:lab
PLUGIN_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/reach/plugin-data/com__digitizable__hogwarts"
PLUGIN_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/reach/plugins/com__digitizable__hogwarts"
PERSONAL="$PLUGIN_DATA/personal"
HOST_PLANE="http://127.0.0.1:8080"
# libvirt default NAT gateway = host from guest's view
VIRT_PLANE="http://192.168.122.1:8080"
TOKEN=dev

mkdir -p "$PLUGIN_DATA" "$PERSONAL"

echo "==> build images"
docker build -t "$PLANE_IMG" -f lab/Dockerfile.plane . -q
docker build -t "$AGENT_IMG" -f lab/Dockerfile.agent . -q

echo "==> network + plane"
docker network create "$NET" >/dev/null 2>&1 || true
docker rm -f hogwarts-plane hogwarts-agent hogwarts-agent-2 hogwarts-agent-3 2>/dev/null || true
docker run -d --name hogwarts-plane --network "$NET" \
  -e PLANE_OPERATOR_TOKEN="$TOKEN" \
  -e PLANE_HTTP_ADDR=0.0.0.0:8080 \
  -e PLANE_DB=/data/plane.db \
  -p 8080:8080 \
  "$PLANE_IMG" >/dev/null

echo "==> wait for plane"
for i in $(seq 1 40); do
  if curl -sf "$HOST_PLANE/api/v1/health" >/dev/null; then break; fi
  sleep 0.5
done
curl -sf "$HOST_PLANE/api/v1/health" | python3 -m json.tool

# Mock fleet: three Linux lab agents with distinct hostnames
echo "==> mock agents (Docker)"
for spec in "hogwarts-agent:lab-docker-1:1.5" "hogwarts-agent-2:lab-docker-2:1.5" "hogwarts-agent-3:lab-docker-3:1.5"; do
  name="${spec%%:*}"
  rest="${spec#*:}"
  host="${rest%%:*}"
  sleep_s="${rest##*:}"
  docker run -d --name "$name" --hostname "$host" --network "$NET" \
    -e PLANE_URL=http://hogwarts-plane:8080 \
    -e PLANE_OPERATOR_TOKEN="$TOKEN" \
    -e AGENT_SLEEP="$sleep_s" \
    -e AGENT_JITTER=0.15 \
    "$AGENT_IMG" >/dev/null
  echo "    started $name hostname=$host sleep=${sleep_s}s"
done

echo "==> wait for enrollments"
for i in $(seq 1 40); do
  N=$(curl -sf -H "Authorization: Bearer $TOKEN" "$HOST_PLANE/api/v1/agents" \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('agents') or []))" 2>/dev/null || echo 0)
  if [ "${N:-0}" -ge 3 ]; then
    echo "    agents online: $N"
    break
  fi
  sleep 1
done

# Hogwarts desk plane config (personal)
echo "==> write plane.json for Hogwarts desk"
python3 - <<PY
import json
from pathlib import Path
path = Path("$PLUGIN_DATA") / "plane.json"
path.write_text(json.dumps({
    "api_token": "$TOKEN",
    "base_url": "$HOST_PLANE",
    "poll_interval_sec": 3.0,
    "extra": {
        "profile": "personal-lab",
        "notes": "Docker mock fleet + host agent; Win11 use $VIRT_PLANE",
    },
}, indent=2) + "\n")
print("    ", path)
PY

# Host agent — real Linux desktop screenshots (ImageGrab / tools / synthetic)
echo "==> host agent (this machine's desktop)"
HOST_DIR="$PERSONAL/host-agent"
mkdir -p "$HOST_DIR"
SECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$HOST_PLANE/api/v1/operator/enroll-secrets" \
  -d '{"max_uses":1,"ttl_sec":86400}' | python3 -c "import sys,json; print(json.load(sys.stdin)['secret'])")
cat > "$HOST_DIR/agent.json" <<EOF
{
  "base_url": "$HOST_PLANE",
  "base_urls": ["$HOST_PLANE"],
  "enroll_secret": "$SECRET",
  "sleep": 3,
  "jitter": 0.15,
  "clear_enroll_secret": true
}
EOF
cp -f "$ROOT/agent/agent.py" "$HOST_DIR/agent.py"
# stop prior host agent if we wrote a pid
if [ -f "$PERSONAL/host-agent.pid" ]; then
  old=$(cat "$PERSONAL/host-agent.pid" || true)
  if [ -n "${old:-}" ] && kill -0 "$old" 2>/dev/null; then
    kill "$old" 2>/dev/null || true
    sleep 0.5
  fi
fi
nohup python3 "$HOST_DIR/agent.py" loop -c "$HOST_DIR/agent.json" \
  >"$PERSONAL/host-agent.log" 2>&1 &
echo $! >"$PERSONAL/host-agent.pid"
echo "    pid $(cat "$PERSONAL/host-agent.pid")  log $PERSONAL/host-agent.log"

# Windows / Parsec enroll pack (for guest or other machine)
echo "==> Windows enroll pack (virt-manager NAT → host)"
WIN_DIR="$PERSONAL/win11-agent"
mkdir -p "$WIN_DIR"
WSECRET=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$HOST_PLANE/api/v1/operator/enroll-secrets" \
  -d '{"max_uses":2,"ttl_sec":86400}' | python3 -c "import sys,json; print(json.load(sys.stdin)['secret'])")
cp -f "$ROOT/agent/agent.py" "$WIN_DIR/agent.py"
cat > "$WIN_DIR/agent.json" <<EOF
{
  "base_url": "$VIRT_PLANE",
  "base_urls": ["$VIRT_PLANE", "$HOST_PLANE"],
  "enroll_secret": "$WSECRET",
  "sleep": 4,
  "jitter": 0.2,
  "clear_enroll_secret": true
}
EOF
cat > "$WIN_DIR/run-windows.bat" <<'EOF'
@echo off
cd /d "%~dp0"
py -3 agent.py loop -c agent.json
if errorlevel 1 python agent.py loop -c agent.json
pause
EOF
cat > "$WIN_DIR/README.txt" <<EOF
Hogwarts personal Windows agent pack
====================================
Plane (from libvirt guest): $VIRT_PLANE
Plane (same host only):     $HOST_PLANE
Token (desk only, never on agent): $TOKEN

1. Copy this folder into the Win11 VM (spice/shared folder, scp, etc.)
2. Install Python 3.10+ from python.org (add to PATH)
   Optional:  py -3 -m pip install pillow mss
3. Run:  run-windows.bat
   Or:   py -3 agent.py loop -c agent.json
4. In Reach → Hogwarts → Plane: $HOST_PLANE  token $TOKEN
5. Agents → select the Win11 host → Screenshot / Live

Parsec note: Parsec is a separate remote-desktop path to your *other*
physical PC. To C2 that box, copy this pack there and set base_url to a
plane URL that machine can reach (public edge or VPN), not 192.168.122.1.
EOF

# Sync plugin tree so desk has latest agent for export
if [ -d "$PLUGIN_DIR" ]; then
  rsync -a --delete --exclude .git --exclude __pycache__ --exclude '*.pyc' \
    "$ROOT/" "$PLUGIN_DIR/"
  echo "==> plugin reinstalled → $PLUGIN_DIR"
fi

echo
echo "==> fleet"
curl -sf -H "Authorization: Bearer $TOKEN" "$HOST_PLANE/api/v1/agents" | python3 -m json.tool
echo
echo "OK — personal lab ready"
echo "  Hogwarts Plane:  $HOST_PLANE   token=$TOKEN   poll=3s"
echo "  Docker mocks:    lab-docker-1/2/3 (synthetic desktop frames)"
echo "  Host agent:      this machine (real screenshot if display available)"
echo "  Win11 pack:      $WIN_DIR"
echo "  Start Win11:     virsh start win11   then copy pack into guest"
echo "  Restart Reach to load plane.json if it was already open."
echo
echo "Viewer test order:"
echo "  1) Docker agent → Screenshot (synthetic purple frame) proves the pipe"
echo "  2) Host agent   → Screenshot (your Linux desktop if grab works)"
echo "  3) Win11 pack   → real Win11 desktop after agent enrolls"
echo "  4) Parsec host  → same as (3) but on the remote physical PC"
