# Hogwarts — Control plane contract

Hogwarts is the **operator desk** inside Reach. Live fleet data comes from a C2
**control-plane API** you host. Until that API is configured, Agents stays empty
and local tools (channel, egress, listener notes, playbooks) still work.

Lab mock plane: `plane/server.py` (SQLite). Reference agent: `agent/agent.py`.

Config is stored under Reach plugin data:
`~/.local/share/reach/plugin-data/com__digitizable__hogwarts/plane.json`

---

## Base

| Item | Requirement |
|------|-------------|
| Transport | HTTPS (HTTP only on localhost/dev) |
| Format | JSON, UTF-8 |
| Auth | `Authorization: Bearer <token>` |
| Time | ISO-8601 UTC |
| Errors | `{ "error": { "code": "…", "message": "…" } }` + HTTP status |

### Secrets

| Secret | Who | Routes |
|--------|-----|--------|
| `operator_token` | Hogwarts desk | inventory, task create, events, enroll-secrets |
| `enroll_secret` | One-shot install | `POST /api/v1/agent/enroll` |
| `agent_token` | Agent only | checkin / results |

---

## Health

```
GET /api/v1/health
→ 200 { "status": "ok", "version": "…", "time": "…" }
```

---

## Operator — agents

```
GET /api/v1/agents?status=online|idle|offline&q=<search>&limit=200
→ 200 {
  "agents": [
    {
      "id": "agt_…",
      "hostname": "wkstn-04",
      "username": "jdoe",
      "os": "Linux 6.x",
      "arch": "x86_64",
      "status": "online",
      "last_seen": "…Z",
      "external_ip": "203.0.113.1",
      "internal_ip": "10.0.0.12",
      "group": "red-team",
      "tags": ["vip"],
      "sleep": 5,
      "jitter": 0.2
    }
  ],
  "next_cursor": null
}
```

```
GET /api/v1/agents/{id}
→ 200 { "agent": { … } }
```

---

## Operator — tasks (T4)

```
POST /api/v1/agents/{id}/tasks
Body: {
  "type": "shell|ping|note|fs_list|download|…",
  "payload": { … },
  "client_request_id": "uuid-optional"
}
→ 202 { "task_id": "tsk_…", "status": "queued", "created": "…Z" }
```

| type | payload | result |
|------|---------|--------|
| `ping` | `{}` | `{ok, pong}` |
| `note` | `{text}` | `{acked, text}` |
| `shell` | `{cmd, shell?, timeout_sec?}` | `{stdout, stderr, exit_code, shell, truncated?}` |
| `fs_list` | `{path?, show_hidden?}` | `{path, entries[{name,type,size,mtime,mode}], parent, sep}` |
| `fs_index_start` | `{roots?: string[], max_entries?}` | start local volume index on agent (walk MVP; MFT/Everything later) |
| `fs_index_status` | `{}` | `{state, progress, count, roots, engine, last_built, error?}` state=idle\|building\|ready\|error\|stopping |
| `fs_index_stop` | `{}` | stop in-progress build; keep partial index if any |
| `fs_search` | `{query, path_prefix?, limit?, offset?}` | `{query, hits[{path,name,type,size,mtime}], count, total, offset, truncated?, index_state}` — requires local index |
| `screenshot` | `{max_side?, inline?, live?, quality?, include_cursor?}` | frame `{width,height,format,method,data?}` base64 JPEG; path if large. `live=true` → faster encode; still capture uses higher JPEG quality. `max_side` up to ~4096. `include_cursor` (default true) composites the **host OS cursor** into the frame (Windows GDI DrawIconEx) so Remote Viewer matches the remote pointer |
| `desktop_start` | `{mode?: capture\|auto\|vnc, port?}` | capture session + optional loopback VNC (`x11vnc`) |
| `desktop_stop` | `{}` | stop VNC/capture session |
| `desktop_input` | `{events:[{type, fx?, fy?, x?, y?, button?, key?, text?}]}` | inject mouse/keyboard (remote control ladder). `fx`/`fy` are 0–1 of primary screen; `type`=move\|click\|dblclick\|down\|up\|key\|type |

**Remote desktop ladder (desk Remote Viewer):**
1. **View** — `screenshot` / Live poll (no input).  
2. **Control** — Live frame + `desktop_input` click/key (agent: xdotool / Win32).  
3. **Session (Keepstream — research → build)** — continuous media face, not task-poll. Draft tasks:
   - `session_start` `{mode: keepstream, face: reverse|forward, port?, max_side?, codec?, input_provider?}` → `{session_id, face, host, port, psk, codec, width?, height?, elevated?, input_provider?}`
   - `session_stop` `{session_id}` → `{stopped}`
   - Wire: see Anguish notes `research/keepstream-v0` (TCP HELLO + length-prefixed VIDEO/INPUT/CTRL). Plane stores **metadata only**, never frames.
   - **`input_provider` (optional plug-in):** operator-supplied elevated-input / UAC helper. Hogwarts does **not** ship a bypass. Shape:
     ```json
     {
       "enabled": true,
       "kind": "exec",
       "command": "C:\\tools\\my-helper.exe",
       "args": [],
       "spawn": true,
       "pipe": "\\\\.\\pipe\\optional"
     }
     ```
     Also set on the host in `agent.json` as `"input_provider": { ... }` (desk Session field can override for one start).  
     **Helper protocol (stdin or pipe, line-based):**
     1. Agent → helper: `HELLO hogwarts-input/1 <session_id> <psk>\n`
     2. Helper → agent (optional): `HELLO_OK\n` or `HELLO_OK elevated=1\n`
     3. Agent → helper: `{"events":[{...},...]}\n` (same event objects as `desktop_input`)
     4. Agent → helper: `BYE\n` on session stop  
     Env when `kind=exec`: `HOGWARTS_INPUT_PROTOCOL`, `HOGWARTS_SESSION_ID`, `HOGWARTS_INPUT_PSK`, optional `HOGWARTS_INPUT_PIPE`.  
     If provider fails or is unset → built-in SendInput/xdotool (Medium IL; Task Manager blocked unless agent elevated).
4. **Session (legacy)** — `desktop_start` (+ optional RFB/`x11vnc`) tunneled via `socks_start`.

**Latency notes:** Live/Control is still **task+check-in** polling. Parsec BUD is closed (no public wire RE); do not aim for BUD compatibility. Mitigations on task path: screenshot supersede, input-first pull, turbo sleep ~0.10–0.15s. True low-latency = **Keepstream Session** (or Sunshine baseline for comparison).

| `download` | `{path, offset?, length?}` | chunk `{offset,length,total_size,has_more,data}` (chunk ≤256KiB, file ≤8MiB) |
| `upload` | `{path, data, offset?, mode?}` | `{path, size, chunk, written}` mode=write\|append |

**Embedded desktop (desk):** Screenshot / Live poll `screenshot` into a Gtk.Picture viewer.  
**RFB:** if `x11vnc` is on the host, `desktop_start` mode `auto` binds `127.0.0.1:5901`; tunnel with `socks_start`.

**shell** values: `auto` (default), `sh`, `bash`, `zsh`, `fish`, `cmd`, `powershell`/`ps`, `pwsh`.  
**fs_list** types: `dir` \| `file` \| `link` \| `other`. Cap 2000 entries.

**fs_search / fs_index_*** (WizFile-class path index — agent-local):
- Index lives **on the agent** (memory/disk). Desk never bulk-syncs the MFT.
- Phase 1 engine: `walk` (background `os.walk` of roots). Phase 2 (Windows): `mft` / `everything`.
- `fs_list` remains the folder browser; `fs_search` is full-index substring/glob search.
- Search hit cap default 200 (max 2000). Index entry cap default 200_000 (agent-enforced).

### Listeners (plane-managed)

```
GET    /api/v1/listeners
POST   /api/v1/listeners          body: listener fields
PUT    /api/v1/listeners/{id}
DELETE /api/v1/listeners/{id}
GET    /api/v1/listeners/{id}
```

Fields: `id, name, accept_host, accept_port, proto, face, agent_id, state, evidence, notes`.
State: planned|deployed|disabled|burned. Evidence: none|tcp_ok|process_ok|plane_managed|unknown.
| `socks_start` | `{port?}` 0=ephemeral | `{started, port, bind, proto}` lab SOCKS5 |
| `socks_stop` | `{}` | `{stopped, port}` |
| `rekey` | plane injects `new_token` on deliver | `{rekeyed}` agent persists new token |

Task `status`: `queued` | `assigned` | `succeeded` | `failed` | `cancelled`.

```
GET /api/v1/agents/{id}/tasks?limit=50
→ 200 { "tasks": [ { "id", "type", "status", "created", "updated", "result"? } ] }

GET /api/v1/tasks/{task_id}
→ 200 { "task": { … } }

POST /api/v1/tasks/{task_id}/cancel
→ 200 { "task": { … status: cancelled } }
  (only queued|assigned; terminal states returned unchanged)
```

---

## Operator — events & enroll

```
GET /api/v1/events?since=<iso>&limit=100
→ 200 {
  "events": [
    {
      "ts": "…Z",
      "level": "info|ok|warn|error",
      "channel": "agent|listener|task|system",
      "message": "…",
      "agent_id": "…?"
    }
  ]
}
```

```
POST /api/v1/operator/enroll-secrets
Body: { "max_uses": 1, "ttl_sec": 3600 }
→ 201 { "id", "secret", "max_uses", "expires_at" }
```

---

## Agent routes

```
POST /api/v1/agent/enroll
Body: { "enroll_secret", "hostname", "username", "os", "arch", … }
→ 201 { "agent_id", "agent_token", "sleep", "jitter" }

POST /api/v1/agent/checkin
Authorization: Bearer <agent_token>
Body: host facts
→ 200 { "server_time", "sleep", "jitter", "tasks": [ {id, type, payload} ] }

POST /api/v1/agent/results
Authorization: Bearer <agent_token>
Body: { "task_id", "status": "succeeded|failed", "result": { … } }
→ 200 { "ok": true }
```

### Agent stability (reference agent ≥0.5.5)

| Mechanism | Behavior |
|-----------|----------|
| Loop never exits on plane down | Exponential backoff 2s→120s + jitter |
| Result spool | `agent.spool.json` beside config; flush on next good check-in |
| Multi-URL | `base_urls: ["https://edge","https://backup"]` or `PLANE_URLS=a,b` |
| Outer watchdog | Optional `watchdog-linux.sh` / `.bat` for process death |

Host facts may include `agent_version`, `plane_url`, `agent_error`, `agent_fail_streak` (informational).

---

## Lab

```bash
# terminal 1
PLANE_OPERATOR_TOKEN=dev PLANE_HTTP_ADDR=127.0.0.1:8080 python3 plane/server.py

# mint enroll secret
curl -s -X POST http://127.0.0.1:8080/api/v1/operator/enroll-secrets \
  -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  -d '{"max_uses":1,"ttl_sec":3600}'

# terminal 2 — agent
cat > /tmp/agent.json <<EOF
{"base_url":"http://127.0.0.1:8080","enroll_secret":"<secret>"}
EOF
python3 agent/agent.py once -c /tmp/agent.json

# Hogwarts Plane panel: http://127.0.0.1:8080  token: dev
```

Docker: see `lab/docker-compose.yml`.
