#!/usr/bin/env python3
"""Lab smoke: Keepstream Session dialed through a local SOCKS5 (Spike 2 path).

Proves desk KeepstreamClient + socks5_connect when Reach path is down by
standing up a tiny no-auth SOCKS5 on 127.0.0.1 and dialing the agent face
through it.

Usage (plane + Windows agent online):
  PLANE_URL=http://127.0.0.1:8080 PLANE_OPERATOR_TOKEN=dev \\
    python3 scripts/smoke_keepstream_socks.py [agent_id]

Exit 0 if HELLO_OK + enough frames via socks5://…
"""

from __future__ import annotations

import json
import os
import select
import socket
import struct
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hogwarts.keepstream import KeepstreamClient  # noqa: E402
from hogwarts.net import socks5_connect  # noqa: E402


PLANE = os.environ.get("PLANE_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("PLANE_OPERATOR_TOKEN", "dev")


def api(method: str, path: str, body: dict | None = None, timeout: float = 12.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        PLANE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class MiniSocks5:
    """No-auth SOCKS5 CONNECT only — lab stand-in for Reach path SOCKS."""

    def __init__(self) -> None:
        self._stop = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self._srv.settimeout(0.5)
        self.host, self.port = self._srv.getsockname()
        self.n_connects = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _rx(self, c: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("eof")
            buf += chunk
        return buf

    def _client(self, c: socket.socket) -> None:
        remote: socket.socket | None = None
        try:
            c.settimeout(20.0)
            ver_n = self._rx(c, 2)
            if ver_n[0] != 5:
                return
            self._rx(c, ver_n[1])
            c.sendall(b"\x05\x00")
            req = self._rx(c, 4)
            if req[0] != 5 or req[1] != 1:
                c.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            atyp = req[3]
            if atyp == 1:
                addr = socket.inet_ntoa(self._rx(c, 4))
            elif atyp == 3:
                ln = self._rx(c, 1)[0]
                addr = self._rx(c, ln).decode("utf-8", "surrogateescape")
            elif atyp == 4:
                addr = socket.inet_ntop(socket.AF_INET6, self._rx(c, 16))
            else:
                c.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = struct.unpack("!H", self._rx(c, 2))[0]
            remote = socket.create_connection((addr, port), timeout=12.0)
            c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            self.n_connects += 1
            for s in (c, remote):
                s.setblocking(False)
            while not self._stop:
                rlist, _, _ = select.select([c, remote], [], [], 1.0)
                if not rlist:
                    continue
                for s in rlist:
                    other = remote if s is c else c
                    try:
                        data = s.recv(65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        return
                    view = memoryview(data)
                    while len(view):
                        try:
                            n = other.send(view)
                            view = view[n:]
                        except BlockingIOError:
                            select.select([], [other], [], 1.0)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
            if remote is not None:
                try:
                    remote.close()
                except OSError:
                    pass


def pick_agent(prefer: str | None) -> str:
    if prefer:
        return prefer
    agents = api("GET", "/api/v1/agents?status=live&limit=50").get("agents") or []
    # Prefer Windows hosts for gdigrab truth
    for a in agents:
        os_s = str(a.get("os") or "").lower()
        if "windows" in os_s and a.get("id"):
            return str(a["id"])
    for a in agents:
        if a.get("id"):
            return str(a["id"])
    raise SystemExit("no live agents")


def main() -> int:
    agent_id = pick_agent(sys.argv[1] if len(sys.argv) > 1 else None)
    proxy = MiniSocks5()
    print(f"[smoke] SOCKS {proxy.host}:{proxy.port} agent={agent_id}")
    try:
        # sanity
        s = socks5_connect(proxy.host, proxy.port, "127.0.0.1", 8080, timeout=5)
        s.close()
    except Exception as exc:
        print(f"[smoke] SOCKS self-check failed: {exc}")
        proxy.stop()
        return 2

    try:
        api("POST", f"/api/v1/agents/{agent_id}/tasks", {"type": "session_stop", "payload": {}})
        time.sleep(0.8)
    except Exception:
        pass

    created = api(
        "POST",
        f"/api/v1/agents/{agent_id}/tasks",
        {
            "type": "session_start",
            "payload": {
                "mode": "keepstream",
                "bind": "0.0.0.0",
                "port": 0,
                "max_side": 1280,
                "fps": 60,
                "quality": 72,
            },
        },
    )
    tid = str(created.get("task_id") or "")
    result: dict = {}
    status = ""
    for _ in range(60):
        task = api("GET", f"/api/v1/tasks/{tid}").get("task") or {}
        status = str(task.get("status") or "")
        if status in ("succeeded", "failed", "cancelled"):
            result = task.get("result") or {}
            break
        time.sleep(0.25)
    print(
        f"[smoke] session_start {status} capture={result.get('capture')} "
        f"ver={result.get('agent_version')} port={result.get('port')}"
    )
    if status != "succeeded" or result.get("error"):
        proxy.stop()
        return 1

    host = str(result.get("connect_host") or result.get("host") or "")
    port = int(result.get("port") or 0)
    frames: list[tuple[float, int]] = []

    def on_frame(data: bytes, _meta: dict) -> None:
        frames.append((time.time(), len(data)))

    def on_status(msg: str, ok: bool | None) -> None:
        print(f"[smoke] ks ok={ok}: {msg}")

    ks = KeepstreamClient(
        host=host,
        port=port,
        session_id=str(result.get("session_id") or ""),
        psk=str(result.get("psk") or ""),
        on_frame=on_frame,
        on_status=on_status,
        socks_host=proxy.host,
        socks_port=proxy.port,
    )
    ks.start()
    t0 = time.time()
    while time.time() - t0 < 5.0:
        time.sleep(0.05)
    elapsed = time.time() - t0
    ks.stop()
    try:
        api("POST", f"/api/v1/agents/{agent_id}/tasks", {"type": "session_stop", "payload": {}})
    except Exception:
        pass
    proxy.stop()

    n = len(frames)
    fps = n / max(elapsed, 0.01)
    sizes = [s for _, s in frames]
    print(
        f"[smoke] via={ks.via} frames={n} fps≈{fps:.1f} "
        f"socks_connects={proxy.n_connects} remote={ks.remote_w}x{ks.remote_h} "
        f"jpeg_avg={sum(sizes)//n if sizes else 0}"
    )
    ok = (
        n >= 30
        and proxy.n_connects >= 1
        and str(ks.via).startswith("socks5://")
        and ks.remote_w > 0
    )
    print("[smoke]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
