"""Decode Keepstream H.264 Annex-B access units for GTK paint.

Prefers GStreamer (appsrc → h264parse → avdec_h264 → jpegenc → appsink).
Falls back to None if GStreamer/plugins are unavailable (caller keeps waiting
for JPEG frames or the agent may fall back to MJPEG).

Important: call :func:`ensure_gst_init` once from the **GTK main thread** before
Session starts. Initializing GStreamer from a worker while GTK owns the default
GLib main context freezes the Control/Session UI.
"""

from __future__ import annotations

import threading
from typing import Any

_gst_init_lock = threading.Lock()
_gst_ready = False
_gst_ok = False


def ensure_gst_init() -> bool:
    """Initialize GStreamer once. Prefer calling from the GTK main thread."""
    global _gst_ready, _gst_ok
    if _gst_ready:
        return _gst_ok
    with _gst_init_lock:
        if _gst_ready:
            return _gst_ok
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            if not getattr(Gst, "is_initialized", lambda: False)():
                Gst.init(None)
            _gst_ok = True
        except Exception:
            _gst_ok = False
        _gst_ready = True
        return _gst_ok


class H264ToJpeg:
    def __init__(self) -> None:
        self._pipe: Any = None
        self._src: Any = None
        self._sink: Any = None
        self._lock = threading.Lock()
        self._ok = False
        self._Gst: Any = None
        self._pushed = 0

    @property
    def available(self) -> bool:
        return ensure_gst_init()

    def start(self) -> bool:
        if self._ok and self._pipe is not None:
            return True
        if not ensure_gst_init():
            return False
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            self._Gst = Gst
            # byte-stream Annex-B; config-interval so parse can recover SPS/PPS
            # Short appsink queue + drop keeps decode off the UI path under load
            desc = (
                "appsrc name=src is-live=true do-timestamp=true format=time "
                "block=false max-bytes=0 "
                "caps=video/x-h264,stream-format=byte-stream,alignment=au ! "
                "h264parse config-interval=-1 ! "
                "avdec_h264 ! videoconvert ! "
                "jpegenc quality=75 ! "
                "appsink name=sink emit-signals=false sync=false "
                "max-buffers=1 drop=true enable-last-sample=false"
            )
            pipe = Gst.parse_launch(desc)
            src = pipe.get_by_name("src")
            sink = pipe.get_by_name("sink")
            if src is None or sink is None:
                return False
            src.set_property("stream-type", 0)  # GST_APP_STREAM_TYPE_STREAM
            src.set_property("format", Gst.Format.TIME)
            ret = pipe.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                pipe.set_state(Gst.State.NULL)
                return False
            self._pipe = pipe
            self._src = src
            self._sink = sink
            self._ok = True
            self._pushed = 0
            return True
        except Exception:
            self.stop()
            return False

    def stop(self) -> None:
        pipe = self._pipe
        self._pipe = None
        self._src = None
        self._sink = None
        self._ok = False
        self._pushed = 0
        if pipe is not None:
            try:
                pipe.set_state(self._Gst.State.NULL if self._Gst else 1)
            except Exception:
                pass

    def decode(self, annex_b_au: bytes, *, timeout_s: float = 0.12) -> bytes | None:
        """Decode one AU → JPEG. Short timeout so the stream thread never stalls long."""
        if not annex_b_au:
            return None
        with self._lock:
            if not self.start() or self._src is None or self._sink is None:
                return None
            Gst = self._Gst
            try:
                buf = Gst.Buffer.new_allocate(None, len(annex_b_au), None)
                buf.fill(0, annex_b_au)
                ret = self._src.emit("push-buffer", buf)
                if ret != Gst.FlowReturn.OK:
                    self.stop()
                    if not self.start():
                        return None
                    buf = Gst.Buffer.new_allocate(None, len(annex_b_au), None)
                    buf.fill(0, annex_b_au)
                    ret = self._src.emit("push-buffer", buf)
                    if ret != Gst.FlowReturn.OK:
                        return None
                self._pushed += 1
                # First few AUs (SPS/PPS or first IDR) may need a bit longer
                to = timeout_s
                if self._pushed <= 4:
                    to = max(to, 0.35)
                timeout_ns = int(max(0.02, to) * Gst.SECOND)
                sample = self._sink.emit("try-pull-sample", timeout_ns)
                if sample is None:
                    return None
                out_buf = sample.get_buffer()
                if out_buf is None:
                    return None
                ok, mapinfo = out_buf.map(Gst.MapFlags.READ)
                if not ok:
                    return None
                try:
                    data = bytes(mapinfo.data)
                finally:
                    out_buf.unmap(mapinfo)
                if data[:2] == b"\xff\xd8":
                    return data
                return data if data else None
            except Exception:
                return None


_decoder: H264ToJpeg | None = None
_decoder_lock = threading.Lock()


def decode_h264_au_to_jpeg(au: bytes) -> bytes | None:
    global _decoder
    with _decoder_lock:
        if _decoder is None:
            _decoder = H264ToJpeg()
        return _decoder.decode(au)


def stop_h264_decoder() -> None:
    global _decoder
    with _decoder_lock:
        if _decoder is not None:
            _decoder.stop()
            _decoder = None
