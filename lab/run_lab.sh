#!/usr/bin/env bash
# Start plane + agent containers (no compose plugin required) and smoke-test.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
NET=hogwarts-lab
PLANE_IMG=hogwarts-plane:lab
AGENT_IMG=hogwarts-agent:lab

cleanup() {
  docker rm -f hogwarts-plane hogwarts-agent 2>/dev/null || true
  docker network rm "$NET" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> build images"
docker build -t "$PLANE_IMG" -f lab/Dockerfile.plane .
docker build -t "$AGENT_IMG" -f lab/Dockerfile.agent .

echo "==> network + containers"
docker network create "$NET" >/dev/null 2>&1 || true
docker rm -f hogwarts-plane hogwarts-agent 2>/dev/null || true
docker run -d --name hogwarts-plane --network "$NET" \
  -e PLANE_OPERATOR_TOKEN=dev \
  -e PLANE_HTTP_ADDR=0.0.0.0:8080 \
  -e PLANE_DB=/data/plane.db \
  -p 8080:8080 \
  "$PLANE_IMG" >/dev/null
docker run -d --name hogwarts-agent --network "$NET" \
  -e PLANE_URL=http://hogwarts-plane:8080 \
  -e PLANE_OPERATOR_TOKEN=dev \
  -e AGENT_SLEEP=3 \
  -e AGENT_JITTER=0.2 \
  "$AGENT_IMG" >/dev/null

echo "==> wait for plane health"
for i in $(seq 1 40); do
  if curl -sf http://127.0.0.1:8080/api/v1/health >/dev/null; then
    break
  fi
  sleep 1
done
curl -s http://127.0.0.1:8080/api/v1/health | python3 -m json.tool

echo "==> wait for agent enroll"
for i in $(seq 1 40); do
  N=$(curl -s -H "Authorization: Bearer dev" http://127.0.0.1:8080/api/v1/agents \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('agents') or []))" 2>/dev/null || echo 0)
  if [ "${N:-0}" -ge 1 ]; then
    echo "agents: $N"
    break
  fi
  sleep 2
done

AGENTS=$(curl -s -H "Authorization: Bearer dev" http://127.0.0.1:8080/api/v1/agents)
echo "$AGENTS" | python3 -m json.tool
AID=$(echo "$AGENTS" | python3 -c "import sys,json; a=json.load(sys.stdin).get('agents') or []; print(a[0]['id'] if a else '')")
if [ -z "$AID" ]; then
  echo "FAIL: no agents enrolled" >&2
  docker logs hogwarts-plane --tail=40
  docker logs hogwarts-agent --tail=40
  exit 1
fi

echo "==> create shell task on $AID"
TASK=$(curl -s -X POST -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  "http://127.0.0.1:8080/api/v1/agents/${AID}/tasks" \
  -d '{"type":"shell","payload":{"cmd":"uname -a && id && hostname"}}')
echo "$TASK" | python3 -m json.tool
TID=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task_id',''))")

echo "==> wait for task result"
for i in $(seq 1 40); do
  TR=$(curl -s -H "Authorization: Bearer dev" "http://127.0.0.1:8080/api/v1/tasks/${TID}")
  ST=$(echo "$TR" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task',{}).get('status',''))")
  echo "  status=$ST"
  if [ "$ST" = "succeeded" ] || [ "$ST" = "failed" ]; then
    echo "$TR" | python3 -m json.tool
    if [ "$ST" != "succeeded" ]; then
      exit 1
    fi
    break
  fi
  sleep 2
  if [ "$i" -eq 40 ]; then
    echo "FAIL: task did not complete" >&2
    docker logs hogwarts-plane --tail=50
    docker logs hogwarts-agent --tail=50
    exit 1
  fi
done

echo "==> cancel queued task"
CTASK=$(curl -s -X POST -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  "http://127.0.0.1:8080/api/v1/agents/${AID}/tasks" \
  -d '{"type":"shell","payload":{"cmd":"sleep 180"}}')
CTID=$(echo "$CTASK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task_id',''))")
CST=$(curl -s -X POST -H "Authorization: Bearer dev" \
  "http://127.0.0.1:8080/api/v1/tasks/${CTID}/cancel" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('task',{}).get('status',''))")
echo "  cancel status=$CST"
if [ "$CST" != "cancelled" ]; then
  echo "FAIL: expected cancelled, got $CST" >&2
  exit 1
fi

echo "==> multi-chunk download (700KB → 3 chunks)"
docker exec hogwarts-agent python3 -c \
  'open("/tmp/big.bin","wb").write(bytes([i%256 for i in range(700000)]))'
python3 - "$AID" <<'PY'
import base64, json, sys, time, urllib.request

aid = sys.argv[1]
base = "http://127.0.0.1:8080"
auth = {"Authorization": "Bearer dev", "Content-Type": "application/json"}
chunk, offset, parts, n = 256_000, 0, [], 0
while True:
    body = json.dumps(
        {"type": "download", "payload": {"path": "/tmp/big.bin", "offset": offset, "length": chunk}}
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/v1/agents/{aid}/tasks", data=body, headers=auth, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        tid = json.load(r)["task_id"]
    task = None
    for _ in range(60):
        time.sleep(1)
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/api/v1/tasks/{tid}",
                headers={"Authorization": "Bearer dev"},
            )
        ) as r:
            task = json.load(r)["task"]
        if task["status"] in ("succeeded", "failed", "cancelled"):
            break
    if not task or task["status"] != "succeeded":
        raise SystemExit(f"chunk failed: {task}")
    res = task["result"] or {}
    raw = base64.b64decode(res.get("data") or "")
    parts.append(raw)
    got = int(res.get("length") or len(raw))
    has_more = bool(res.get("has_more"))
    print(f"  chunk{n} offset={offset} got={got} has_more={has_more}")
    offset += got
    n += 1
    if not has_more or got == 0:
        break
blob = b"".join(parts)
assert len(blob) == 700000, len(blob)
assert n >= 3, n
print(f"  assembled {len(blob)} bytes in {n} chunks")
PY

echo "OK — lab happy path + cancel + multi-chunk complete"
echo "Point Hogwarts Plane at http://127.0.0.1:8080 token=dev"
# keep containers up for manual Hogwarts testing
trap - EXIT
echo "(containers still running: docker logs -f hogwarts-agent)"
exit 0
