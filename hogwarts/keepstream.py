"""Keepstream client — continuous JPEG Session over TCP.

Wire (see Anguish notes/hogwarts/research/keepstream-v0):
  text: KS0 / HELLO / HELLO_OK
  binary: length-prefixed VIDEO (0x01), INPUT (0x02), CTRL (0x03), PING/PONG

The client keeps only the latest VIDEO if the consumer is slow (``pop_latest_frame``).
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any, Callable


TYPE_VIDEO = 0x01
TYPE_INPUT = 0x02
TYPE_CTRL = 0x03
TYPE_PING = 0x05
TYPE_PONG = 0x06


class KeepstreamClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        session_id: str,
        psk: str,
        on_frame: Callable[[bytes, dict[str, Any]], None] | None = None,
        on_status: Callable[[str, bool | None], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.session_id = session_id
        self.psk = psk
        self._on_frame = on_frame
        self._on_status = on_status
        self._on_closed = on_closed
        self._sock: socket.socket | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._ping_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._latest_lock = threading.Lock()
        self._latest: tuple[bytes, dict[str, Any]] | None = None
        self.connected = False
        self.remote_w = 0
        self.remote_h = 0
        self.codec = "jpeg"
        self.frames = 0
        self.dropped = 0
        self.last_rtt_ms: float | None = None

    def _status(self, msg: str, ok: bool | None = None) -> None:
        if self._on_status:
            try:
                self._on_status(msg, ok)
            except Exception:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(
            target=self._run, name="keepstream-client", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        try:
            self._send_ctrl("BYE")
        except Exception:
            pass
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        thr = self._thread
        if thr is not None and thr.is_alive():
            thr.join(timeout=2.0)
        self.connected = False
        if self._on_closed:
            try:
                self._on_closed()
            except Exception:
                pass

    def send_input(self, events: list[dict[str, Any]]) -> None:
        if not events or not self.connected:
            return
        body = json.dumps({"events": events[:48]}, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_record(TYPE_INPUT, body)

    def pop_latest_frame(self) -> tuple[bytes, dict[str, Any]] | None:
        """Take the newest unconsumed frame (drops intermediates)."""
        with self._latest_lock:
            item = self._latest
            self._latest = None
            return item

    def _send_ctrl(self, text: str) -> None:
        self._send_record(TYPE_CTRL, text.encode("utf-8"))

    def _send_record(self, typ: int, payload: bytes, flags: int = 0) -> None:
        sock = self._sock
        if sock is None:
            return
        hdr = struct.pack(">IBBH", len(payload), typ & 0xFF, flags & 0xFF, 0)
        with self._send_lock:
            sock.sendall(hdr + payload)

    def _recv_exact(self, n: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise ConnectionError("no_sock")
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer_closed")
            buf += chunk
        return buf

    def _store_frame(self, jpeg: bytes, meta: dict[str, Any]) -> None:
        with self._latest_lock:
            if self._latest is not None:
                self.dropped += 1
            self._latest = (jpeg, meta)
        if self._on_frame:
            try:
                self._on_frame(jpeg, meta)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            self._status(f"Keepstream connecting {self.host}:{self.port}…", None)
            sock = socket.create_connection((self.host, self.port), timeout=8.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except Exception:
                pass
            self._sock = sock
            f = sock.makefile("rwb", buffering=0)
            f.write(b"KS0\n")
            f.write(f"HELLO {self.session_id} {self.psk}\n".encode("utf-8"))
            f.flush()
            line = f.readline().decode("utf-8", errors="replace").strip()
            if line.startswith("ERR"):
                self._status(f"Keepstream auth failed: {line}", False)
                return
            parts = line.split()
            if len(parts) < 5 or parts[0] != "HELLO_OK":
                self._status(f"Keepstream bad hello: {line}", False)
                return
            try:
                self.remote_w = int(parts[2])
                self.remote_h = int(parts[3])
            except (TypeError, ValueError):
                pass
            self.codec = parts[4] if len(parts) > 4 else "jpeg"
            self.connected = True
            self._status(
                f"Keepstream up · {self.remote_w}×{self.remote_h} · {self.codec}",
                True,
            )

            def pinger() -> None:
                while not self._stop and self.connected:
                    try:
                        now = int(time.time() * 1_000_000)
                        self._send_record(TYPE_PING, struct.pack(">Q", now))
                    except Exception:
                        break
                    time.sleep(1.0)

            self._ping_thread = threading.Thread(
                target=pinger, name="ks-ping", daemon=True
            )
            self._ping_thread.start()

            while not self._stop:
                try:
                    sock.settimeout(5.0)
                    hdr = self._recv_exact(8)
                    length, typ, _flags, _r = struct.unpack(">IBBH", hdr)
                    if length > 12_000_000:
                        raise ValueError("frame_too_large")
                    payload = self._recv_exact(length) if length else b""
                except Exception as exc:
                    if not self._stop:
                        self._status(f"Keepstream read: {exc}", False)
                    break
                if typ == TYPE_VIDEO:
                    if len(payload) < 16:
                        continue
                    (
                        frame_id,
                        pts_ms,
                        w,
                        h,
                        codec,
                        is_key,
                        rect_count,
                    ) = struct.unpack_from(">IIHHBBH", payload, 0)
                    off = 16 + int(rect_count) * 8
                    jpeg = payload[off:]
                    if not jpeg:
                        continue
                    self.frames += 1
                    meta = {
                        "frame_id": frame_id,
                        "pts_ms": pts_ms,
                        "width": w,
                        "height": h,
                        "codec": codec,
                        "keyframe": bool(is_key),
                        "bytes": len(jpeg),
                        "rtt_ms": self.last_rtt_ms,
                        "dropped": self.dropped,
                    }
                    self._store_frame(jpeg, meta)
                elif typ == TYPE_PONG:
                    if len(payload) >= 8:
                        sent = struct.unpack(">Q", payload[:8])[0]
                        now = int(time.time() * 1_000_000)
                        self.last_rtt_ms = max(0.0, (now - sent) / 1000.0)

        except Exception as exc:
            self._status(f"Keepstream failed: {exc}", False)
        finally:
            self.connected = False
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            if self._on_closed:
                try:
                    self._on_closed()
                except Exception:
                    pass
