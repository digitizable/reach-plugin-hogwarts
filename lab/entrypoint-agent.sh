#!/bin/sh
set -eu
PLANE_URL="${PLANE_URL:-http://plane:8080}"
TOKEN="${PLANE_OPERATOR_TOKEN:-dev}"
SLEEP="${AGENT_SLEEP:-1.5}"
JITTER="${AGENT_JITTER:-0.12}"

echo "[lab-agent] waiting for plane ${PLANE_URL}…"
i=0
while [ "$i" -lt 60 ]; do
  if python3 -c "import urllib.request; urllib.request.urlopen('${PLANE_URL}/api/v1/health')" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 1
done

echo "[lab-agent] minting enroll secret…"
SECRET=$(python3 - <<PY
import json, os, urllib.request
url = os.environ.get("PLANE_URL", "http://plane:8080").rstrip("/") + "/api/v1/operator/enroll-secrets"
req = urllib.request.Request(
    url,
    data=json.dumps({"max_uses": 1, "ttl_sec": 7200}).encode(),
    headers={
        "Authorization": "Bearer " + os.environ.get("PLANE_OPERATOR_TOKEN", "dev"),
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as r:
    print(json.load(r)["secret"])
PY
)

cat > /tmp/agent.json <<EOF
{
  "base_url": "${PLANE_URL}",
  "enroll_secret": "${SECRET}",
  "sleep": ${SLEEP},
  "jitter": ${JITTER},
  "clear_enroll_secret": true
}
EOF

echo "[lab-agent] starting loop against ${PLANE_URL}"
exec python3 /app/agent.py loop -c /tmp/agent.json
