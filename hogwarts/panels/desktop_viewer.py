"""Remote desktop / control viewer.

Ladder:
  1. View   — screenshot + Live poll (no input)
  2. Control — Live frame + click/key via desktop_input
  3. Session — desktop_start (+ optional VNC) / SOCKS tunnel hints

Also: ShareX-style archive under ~/Pictures/Hogwarts/<agent>/.
Keepstream Session (Spike 1): continuous JPEG over TCP when Session is started.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

_ZOOM_PRESETS: list[tuple[str, float | None]] = [
    ("Fit", None),
    ("25%", 0.25),
    ("50%", 0.50),
    ("75%", 0.75),
    ("100%", 1.0),
    ("150%", 1.5),
    ("200%", 2.0),
]

_QUALITY: list[tuple[str, int]] = [
    ("Fast 960", 960),
    ("HD 1280", 1280),
    ("FHD 1920", 1920),
    ("QHD 2560", 2560),
    ("Native 4096", 4096),
]

_ARCHIVE_CAP = 250  # max shots kept per agent folder
_THUMB = 72


def _img(name: str, size: int = 16) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(name)
    image.set_pixel_size(size)
    return image


def _icon_btn(icon: str, tip: str, *, css: str = "rdv-tool-btn") -> Gtk.Button:
    b = Gtk.Button()
    b.set_child(_img(icon, 16))
    b.add_css_class("flat")
    b.add_css_class(css)
    b.set_tooltip_text(tip)
    return b


def _detect_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "img"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class RemoteDesktopViewer(Gtk.Window):
    """ShareX-inspired remote screenshot viewer with on-disk archive."""

    def __init__(
        self,
        *,
        agent_label: str,
        agent_id: str,
        archive_dir: str | Path | None = None,
        initial_bytes: bytes | None = None,
        initial_note: str = "",
        live_on: bool = False,
        max_side: int = 1920,
        on_screenshot: Callable[[int], None] | None = None,
        on_live: Callable[[bool], None] | None = None,
        on_session: Callable[..., None] | None = None,
        on_desktop_input: Callable[[list[dict[str, Any]]], None] | None = None,
        on_socks_start: Callable[[], None] | None = None,
        on_live_interval: Callable[[float], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(title=f"Remote Viewer - {agent_label}")
        self.set_default_size(1180, 740)
        self.set_modal(False)
        self.add_css_class("rdv-win")

        self._agent_id = agent_id
        self._agent_label = agent_label
        self._on_screenshot = on_screenshot
        self._on_live = on_live
        self._on_session = on_session
        self._on_desktop_input = on_desktop_input
        self._on_socks_start = on_socks_start
        self._on_live_interval = on_live_interval
        self._on_closed = on_closed
        self._mode = "view"  # view | control | session
        self._control_on = False
        self._session_info: dict[str, Any] = {}
        self._capturing = False
        self._capture_gen: int = 0  # invalidate stale 12s re-enable timers
        # path-str → button (path-keyed so soft-prepend does not stale indices)
        self._archive_btns: dict[str, Gtk.Button] = {}
        # Input batching (Control mode latency)
        self._input_queue: list[dict[str, Any]] = []
        self._input_flush_src: int | None = None
        self._drag_active = False
        self._drag_sent_down = False
        self._drag_start: tuple[float, float] | None = None
        self._last_move_flush = 0.0
        self._live_interval_sec = 1.0  # float seconds; Control drops to 0.5
        self._keepstream: Any = None  # KeepstreamClient when Session connected

        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent_id)[:48]
        if archive_dir:
            self._archive_dir = Path(archive_dir)
        else:
            # Default: ~/Pictures/Hogwarts/<agent>
            host_part = "".join(
                c if c.isalnum() or c in "-_." else "_"
                for c in (agent_label or safe_id)
            )[:48] or safe_id
            self._archive_dir = Path.home() / "Pictures" / "Hogwarts" / host_part
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self._raw: bytes | None = initial_bytes
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        self._note = initial_note
        self._zoom_mode: float | None = None
        self._max_side = max_side
        self._fullscreen = False
        self._current_path: Path | None = None
        # Archive entries: newest first
        self._items: list[dict[str, Any]] = []
        self._selected_i = -1
        self._loading_archive = False  # suppress re-save when loading from disk
        self._archive_live = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("rdv")
        root.set_hexpand(True)
        root.set_vexpand(True)
        self.set_child(root)

        # ── Title ────────────────────────────────────────────────────
        title_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_bar.add_css_class("rdv-titlebar")
        title_bar.append(_img("camera-photo", 20))
        tcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        tcol.set_hexpand(True)
        t1 = Gtk.Label(label="Remote Viewer", xalign=0)
        t1.add_css_class("rdv-title")
        tcol.append(t1)
        self._subtitle = Gtk.Label(
            label=f"{agent_label}  ·  {self._archive_dir}",
            xalign=0,
        )
        self._subtitle.add_css_class("rdv-subtitle")
        self._subtitle.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        tcol.append(self._subtitle)
        title_bar.append(tcol)
        root.append(title_bar)

        # ── Mode: View | Control | Session (remote desktop ladder) ───
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mode_row.add_css_class("rdv-mode-row")
        self._mode_view = Gtk.ToggleButton(label="1 · View")
        self._mode_view.add_css_class("rdv-mode-btn")
        self._mode_view.set_active(True)
        self._mode_view.set_tooltip_text("Screenshots + Live (no remote input)")
        self._mode_control = Gtk.ToggleButton(label="2 · Control")
        self._mode_control.add_css_class("rdv-mode-btn")
        self._mode_control.set_group(self._mode_view)
        self._mode_control.set_tooltip_text(
            "Interactive: click the frame to inject mouse/keys (desktop_input)"
        )
        self._mode_session = Gtk.ToggleButton(label="3 · Session")
        self._mode_session.add_css_class("rdv-mode-btn")
        self._mode_session.set_group(self._mode_view)
        self._mode_session.set_tooltip_text(
            "Keepstream Session: continuous JPEG + input over TCP (not task-poll)"
        )
        self._mode_view.connect(
            "toggled", lambda b: b.get_active() and self._set_mode("view")
        )
        self._mode_control.connect(
            "toggled", lambda b: b.get_active() and self._set_mode("control")
        )
        self._mode_session.connect(
            "toggled", lambda b: b.get_active() and self._set_mode("session")
        )
        mode_row.append(self._mode_view)
        mode_row.append(self._mode_control)
        mode_row.append(self._mode_session)
        self.mode_hint = Gtk.Label(
            label="Ladder: View → Control (poll) → Session (Keepstream stream)",
            xalign=0,
        )
        self.mode_hint.add_css_class("rdv-mode-hint")
        self.mode_hint.set_hexpand(True)
        self.mode_hint.set_ellipsize(Pango.EllipsizeMode.END)
        mode_row.append(self.mode_hint)
        self.live_badge = Gtk.Label(label="")
        self.live_badge.add_css_class("rdv-badge")
        mode_row.append(self.live_badge)
        root.append(mode_row)

        # ── Capture ribbon ───────────────────────────────────────────
        ribbon = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ribbon.add_css_class("rdv-ribbon")

        self.btn_shot = Gtk.Button()
        shot_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        shot_box.append(_img("camera-photo", 16))
        shot_box.append(Gtk.Label(label="Capture"))
        self.btn_shot.set_child(shot_box)
        self.btn_shot.add_css_class("rdv-primary")
        self.btn_shot.set_tooltip_text("Capture remote screenshot (R)")
        self.btn_shot.connect("clicked", lambda *_: self._do_shot())
        ribbon.append(self.btn_shot)

        self.btn_live = Gtk.ToggleButton()
        live_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        live_box.append(_img("media-playback-start", 16))
        live_box.append(Gtk.Label(label="Live"))
        self.btn_live.set_child(live_box)
        self.btn_live.add_css_class("rdv-tool-btn")
        self.btn_live.set_active(live_on)
        self.btn_live.set_tooltip_text("Poll screenshots continuously")
        self.btn_live.connect("toggled", lambda *_: self._do_live())
        ribbon.append(self.btn_live)

        self.btn_sess_on = Gtk.Button()
        sb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        sb.append(_img("video-display", 16))
        sb.append(Gtk.Label(label="Session"))
        self.btn_sess_on.set_child(sb)
        self.btn_sess_on.add_css_class("flat")
        self.btn_sess_on.add_css_class("rdv-tool-btn")
        self.btn_sess_on.set_tooltip_text("Start Keepstream Session (continuous stream)")
        self.btn_sess_on.connect("clicked", lambda *_: self._do_session("start"))
        ribbon.append(self.btn_sess_on)

        self.btn_sess_off = Gtk.Button()
        se = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        se.append(_img("media-playback-stop", 16))
        se.append(Gtk.Label(label="Stop"))
        self.btn_sess_off.set_child(se)
        self.btn_sess_off.add_css_class("flat")
        self.btn_sess_off.add_css_class("rdv-tool-btn")
        self.btn_sess_off.set_tooltip_text("desktop_stop")
        self.btn_sess_off.connect("clicked", lambda *_: self._do_session("stop"))
        ribbon.append(self.btn_sess_off)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        ribbon.append(sep)

        qlab = Gtk.Label(label="Quality", xalign=0)
        qlab.add_css_class("rdv-field")
        ribbon.append(qlab)
        self.quality_dd = Gtk.DropDown.new_from_strings([lab for lab, _ in _QUALITY])
        qi = 2  # FHD 1920 default
        for i, (_, side) in enumerate(_QUALITY):
            if side == max_side:
                qi = i
                break
        self.quality_dd.set_selected(qi)
        self.quality_dd.set_tooltip_text(
            "Capture resolution (long side). Live uses this up to a soft cap "
            "(HD/FHD) so Control stays responsive."
        )
        self.quality_dd.connect(
            "notify::selected", lambda *_: self._on_quality_changed()
        )
        ribbon.append(self.quality_dd)

        self.chk_archive_live = Gtk.CheckButton(label="Archive stream")
        self.chk_archive_live.add_css_class("rdv-check")
        self.chk_archive_live.set_active(False)
        self.chk_archive_live.set_tooltip_text(
            "OFF by default. When on, also save Live/Keepstream frames to "
            "~/Pictures/Hogwarts (can fill disk very quickly)."
        )
        self.chk_archive_live.connect(
            "toggled",
            lambda *_: setattr(
                self, "_archive_live", bool(self.chk_archive_live.get_active())
            ),
        )
        ribbon.append(self.chk_archive_live)

        rsp = Gtk.Box()
        rsp.set_hexpand(True)
        ribbon.append(rsp)

        self.btn_fs = _icon_btn("view-fullscreen", "Fullscreen (F11)")
        self.btn_fs.connect("clicked", lambda *_: self._toggle_fullscreen())
        ribbon.append(self.btn_fs)
        root.append(ribbon)

        # ── Zoom + after-capture actions (ShareX-like) ───────────────
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        tools.add_css_class("rdv-tools")

        self.btn_prev = _icon_btn("go-previous", "Previous in archive (Left)")
        self.btn_prev.connect("clicked", lambda *_: self._hist_step(-1))
        tools.append(self.btn_prev)
        self.btn_next = _icon_btn("go-next", "Next in archive (Right)")
        self.btn_next.connect("clicked", lambda *_: self._hist_step(1))
        tools.append(self.btn_next)

        self.hist_lab = Gtk.Label(label="0 / 0", xalign=0)
        self.hist_lab.add_css_class("rdv-hist-lab")
        self.hist_lab.set_margin_start(4)
        self.hist_lab.set_margin_end(8)
        tools.append(self.hist_lab)

        self.btn_zoom_out = _icon_btn("zoom-out", "Zoom out")
        self.btn_zoom_out.connect("clicked", lambda *_: self._nudge_zoom(-1))
        tools.append(self.btn_zoom_out)
        self.zoom_dd = Gtk.DropDown.new_from_strings([lab for lab, _ in _ZOOM_PRESETS])
        self.zoom_dd.set_selected(0)
        self.zoom_dd.connect("notify::selected", lambda *_: self._on_zoom_dd())
        tools.append(self.zoom_dd)
        self.btn_zoom_in = _icon_btn("zoom-in", "Zoom in")
        self.btn_zoom_in.connect("clicked", lambda *_: self._nudge_zoom(1))
        tools.append(self.btn_zoom_in)
        self.btn_fit = _icon_btn("zoom-fit-best", "Fit (F)")
        self.btn_fit.connect("clicked", lambda *_: self._set_zoom(None))
        tools.append(self.btn_fit)
        self.btn_100 = _icon_btn("zoom-original", "1:1 (1)")
        self.btn_100.connect("clicked", lambda *_: self._set_zoom(1.0))
        tools.append(self.btn_100)

        tsep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        tsep.set_margin_start(6)
        tsep.set_margin_end(6)
        tools.append(tsep)

        self.btn_copy = Gtk.Button()
        cb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cb.append(_img("edit-copy", 16))
        cb.append(Gtk.Label(label="Copy"))
        self.btn_copy.set_child(cb)
        self.btn_copy.add_css_class("flat")
        self.btn_copy.add_css_class("rdv-tool-btn")
        self.btn_copy.set_tooltip_text("Copy image to clipboard (Ctrl+C)")
        self.btn_copy.connect("clicked", lambda *_: self._copy_frame())
        tools.append(self.btn_copy)

        self.btn_save = Gtk.Button()
        sv = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        sv.append(_img("document-save", 16))
        sv.append(Gtk.Label(label="Save as…"))
        self.btn_save.set_child(sv)
        self.btn_save.add_css_class("flat")
        self.btn_save.add_css_class("rdv-tool-btn")
        self.btn_save.set_tooltip_text("Export a copy elsewhere")
        self.btn_save.connect("clicked", lambda *_: self._save_as())
        tools.append(self.btn_save)

        self.btn_open_folder = Gtk.Button()
        of = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        of.append(_img("folder-open", 16))
        of.append(Gtk.Label(label="Open folder"))
        self.btn_open_folder.set_child(of)
        self.btn_open_folder.add_css_class("flat")
        self.btn_open_folder.add_css_class("rdv-tool-btn")
        self.btn_open_folder.set_tooltip_text("Open archive folder in file manager")
        self.btn_open_folder.connect("clicked", lambda *_: self._open_folder())
        tools.append(self.btn_open_folder)

        self.btn_delete = Gtk.Button()
        db = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        db.append(_img("user-trash", 16))
        db.append(Gtk.Label(label="Delete"))
        self.btn_delete.set_child(db)
        self.btn_delete.add_css_class("flat")
        self.btn_delete.add_css_class("rdv-tool-btn")
        self.btn_delete.add_css_class("rdv-danger")
        self.btn_delete.set_tooltip_text("Delete selected screenshot (Delete)")
        self.btn_delete.connect("clicked", lambda *_: self._delete_selected())
        tools.append(self.btn_delete)

        self.btn_delete_all = Gtk.Button()
        da = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        da.append(_img("edit-delete", 16))
        da.append(Gtk.Label(label="Clear archive"))
        self.btn_delete_all.set_child(da)
        self.btn_delete_all.add_css_class("flat")
        self.btn_delete_all.add_css_class("rdv-tool-btn")
        self.btn_delete_all.add_css_class("rdv-danger")
        self.btn_delete_all.set_tooltip_text("Delete all screenshots for this agent")
        self.btn_delete_all.connect("clicked", lambda *_: self._delete_all())
        tools.append(self.btn_delete_all)

        tsp = Gtk.Box()
        tsp.set_hexpand(True)
        tools.append(tsp)

        self.btn_refresh = _icon_btn("view-refresh", "Recapture (R)")
        self.btn_refresh.connect("clicked", lambda *_: self._do_shot())
        tools.append(self.btn_refresh)
        root.append(tools)

        # ── Body: archive sidebar + image ────────────────────────────
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)
        body.add_css_class("rdv-body")

        # Archive panel (ShareX history)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        side.add_css_class("rdv-archive")
        side.set_size_request(200, -1)
        side.set_hexpand(False)

        side_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        side_head.add_css_class("rdv-archive-head")
        side_head.append(_img("image-x-generic", 14))
        sh = Gtk.Label(label="Archive", xalign=0)
        sh.add_css_class("rdv-archive-title")
        sh.set_hexpand(True)
        side_head.append(sh)
        self.archive_count = Gtk.Label(label="0", xalign=1)
        self.archive_count.add_css_class("rdv-archive-count")
        side_head.append(self.archive_count)
        side.append(side_head)

        self.archive_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.archive_list.add_css_class("rdv-archive-list")
        a_scroll = Gtk.ScrolledWindow()
        a_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        a_scroll.set_vexpand(True)
        a_scroll.set_hexpand(True)
        a_scroll.set_child(self.archive_list)
        a_scroll.add_css_class("rdv-archive-scroll")
        side.append(a_scroll)

        side_foot = Gtk.Label(label="", xalign=0, wrap=True)
        side_foot.add_css_class("rdv-archive-foot")
        side_foot.set_margin_start(6)
        side_foot.set_margin_end(6)
        side_foot.set_margin_top(4)
        side_foot.set_margin_bottom(4)
        side_foot.set_text(str(self._archive_dir.name))
        side_foot.set_tooltip_text(str(self._archive_dir))
        side.append(side_foot)
        body.append(side)

        vdiv = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        body.append(vdiv)

        # Image pane
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_hexpand(True)
        main.set_vexpand(True)
        main.add_css_class("rdv-main")

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_can_shrink(True)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.picture.add_css_class("rdv-picture")

        # View mode: click toggles zoom / double-click fullscreen
        click = Gtk.GestureClick()
        click.set_button(1)

        def on_img_click(
            _g: Gtk.GestureClick, n_press: int, x: float, y: float
        ) -> None:
            if self._accepts_remote_input():
                return  # drag gesture owns left button when controlling/session
            if n_press == 1 and self._pixbuf is not None:
                self._set_zoom(1.0 if self._zoom_mode is None else None)
            elif n_press >= 2:
                self._toggle_fullscreen()

        click.connect("pressed", on_img_click)
        self.picture.add_controller(click)

        # Control: left drag = click or drag (down/move/up)
        drag = Gtk.GestureDrag()
        drag.set_button(1)

        def on_drag_begin(_g: Gtk.GestureDrag, x: float, y: float) -> None:
            if not self._accepts_remote_input() or self._pixbuf is None:
                return
            frac = self._widget_xy_to_frac(self.picture, x, y)
            if frac is None:
                self._drag_active = False
                return
            self._drag_active = True
            self._drag_sent_down = False
            self._drag_start = frac

        def on_drag_update(_g: Gtk.GestureDrag, x: float, y: float) -> None:
            if not self._drag_active or not self._accepts_remote_input():
                return
            ok, ox, oy = _g.get_start_point()
            if not ok:
                return
            frac = self._widget_xy_to_frac(self.picture, ox + x, oy + y)
            if frac is None:
                return
            fx, fy = frac
            # Distance in widget px
            dist = (x * x + y * y) ** 0.5
            if not self._drag_sent_down:
                if dist < 6:
                    return  # still a potential click
                # Begin drag
                sx, sy = self._drag_start or (fx, fy)
                self._queue_input({"type": "down", "fx": sx, "fy": sy, "button": "left"})
                self._drag_sent_down = True
            self._queue_input({"type": "move", "fx": fx, "fy": fy})

        def on_drag_end(_g: Gtk.GestureDrag, x: float, y: float) -> None:
            if not self._drag_active or not self._accepts_remote_input():
                self._drag_active = False
                return
            ok, ox, oy = _g.get_start_point()
            self._drag_active = False
            if not ok:
                return
            frac = self._widget_xy_to_frac(self.picture, ox + x, oy + y)
            if frac is None:
                frac = self._drag_start
            if frac is None:
                return
            fx, fy = frac
            if self._drag_sent_down:
                self._queue_input({"type": "up", "fx": fx, "fy": fy, "button": "left"})
                self._set_status(f"Drag end @ ({fx:.2f}, {fy:.2f})", ok=True)
            else:
                # Click (no meaningful drag)
                self._queue_input(
                    {"type": "click", "fx": fx, "fy": fy, "button": "left"}
                )
            self._drag_sent_down = False
            self._flush_input(force=True)

        drag.connect("drag-begin", on_drag_begin)
        drag.connect("drag-update", on_drag_update)
        drag.connect("drag-end", on_drag_end)
        self.picture.add_controller(drag)

        # Right click in Control mode
        rclick = Gtk.GestureClick()
        rclick.set_button(3)

        def on_right(
            _g: Gtk.GestureClick, n_press: int, x: float, y: float
        ) -> None:
            if not self._accepts_remote_input() or self._pixbuf is None:
                return
            frac = self._widget_xy_to_frac(self.picture, x, y)
            if frac is None:
                return
            fx, fy = frac
            self._queue_input(
                {"type": "click", "fx": fx, "fy": fy, "button": "right"}
            )
            self._flush_input(force=True)

        rclick.connect("pressed", on_right)
        self.picture.add_controller(rclick)

        # Hover move while Control (throttled) — keeps cursor in sync without click
        motion = Gtk.EventControllerMotion()

        def on_motion(_c: Gtk.EventControllerMotion, x: float, y: float) -> None:
            if (
                not self._accepts_remote_input()
                or self._pixbuf is None
                or self._drag_active
            ):
                return
            import time as _time

            now = _time.monotonic()
            if now - self._last_move_flush < 0.04:
                return
            frac = self._widget_xy_to_frac(self.picture, x, y)
            if frac is None:
                return
            self._last_move_flush = now
            fx, fy = frac
            self._queue_input({"type": "move", "fx": fx, "fy": fy})

        motion.connect("motion", on_motion)
        self.picture.add_controller(motion)

        scroll_c = Gtk.EventControllerScroll()
        scroll_c.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)

        def on_scroll(
            _c: Gtk.EventControllerScroll, _dx: float, dy: float
        ) -> bool:
            if self._mode == "control":
                # Don't zoom while controlling — ignore or map later
                return True
            if dy < 0:
                self._nudge_zoom(1)
            elif dy > 0:
                self._nudge_zoom(-1)
            return True

        scroll_c.connect("scroll", on_scroll)
        self.picture.add_controller(scroll_c)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame.add_css_class("rdv-frame")
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        frame.append(self.picture)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_hexpand(True)
        self._scroll.set_vexpand(True)
        self._scroll.set_child(frame)
        self._scroll.add_css_class("rdv-scroll")
        main.append(self._scroll)

        # Empty / control overlay hint
        self.empty_lab = Gtk.Label(
            label="Capture or enable Live to see the remote desktop",
            xalign=0.5,
        )
        self.empty_lab.add_css_class("rdv-empty-overlay")
        self.empty_lab.set_visible(initial_bytes is None)
        main.append(self.empty_lab)

        # Session mode panel (VNC / SOCKS ladder)
        session = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        session.add_css_class("rdv-session")
        session.set_margin_top(12)
        session.set_margin_start(16)
        session.set_margin_end(16)
        stitle = Gtk.Label(label="Keepstream Session (level 3)", xalign=0)
        stitle.add_css_class("rdv-title")
        session.append(stitle)
        sdesc = Gtk.Label(
            label=(
                "Spike 1: continuous JPEG stream + input over a dedicated TCP face "
                "(not plane task-poll). Agent reverse-accepts; desk connects to "
                "host:port with a one-time PSK. Use Control mode gestures while "
                "Session is connected — input rides Keepstream. "
                "Legacy VNC (x11vnc) remains available via SOCKS if needed."
            ),
            xalign=0,
            wrap=True,
        )
        sdesc.add_css_class("rdv-mode-hint")
        session.append(sdesc)
        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_sess_start2 = Gtk.Button(label="Start Keepstream")
        self.btn_sess_start2.add_css_class("rdv-primary")
        self.btn_sess_start2.connect(
            "clicked", lambda *_: self._do_session("start")
        )
        srow.append(self.btn_sess_start2)
        self.btn_sess_stop2 = Gtk.Button(label="Stop Session")
        self.btn_sess_stop2.add_css_class("flat")
        self.btn_sess_stop2.add_css_class("rdv-tool-btn")
        self.btn_sess_stop2.connect(
            "clicked", lambda *_: self._do_session("stop")
        )
        srow.append(self.btn_sess_stop2)
        self.btn_socks = Gtk.Button(label="SOCKS start (tunnel)")
        self.btn_socks.add_css_class("flat")
        self.btn_socks.add_css_class("rdv-tool-btn")
        self.btn_socks.set_tooltip_text(
            "Queue socks_start if you need to tunnel to agent loopback"
        )
        self.btn_socks.connect("clicked", lambda *_: self._do_socks())
        srow.append(self.btn_socks)
        self.btn_copy_vnc = Gtk.Button(label="Copy connect hint")
        self.btn_copy_vnc.add_css_class("flat")
        self.btn_copy_vnc.add_css_class("rdv-tool-btn")
        self.btn_copy_vnc.connect("clicked", lambda *_: self._copy_vnc_hint())
        srow.append(self.btn_copy_vnc)
        session.append(srow)

        # Optional plug-in: High-IL inject helper (lab: agent/windows/input-provider)
        ip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        ip_box.set_margin_top(10)
        ip_title = Gtk.Label(label="Custom input provider (optional)", xalign=0)
        ip_title.add_css_class("rdv-title")
        ip_box.append(ip_title)
        ip_hint = Gtk.Label(
            label=(
                "For Task Manager / admin UI when the agent is not elevated: "
                "install agent/windows/input-provider once (Highest task), start "
                "it silently, then enable Use provider. Empty path defaults to "
                "\\\\.\\pipe\\hogwarts-input (pipe). Or set an exec path. "
                "Do not self-elevate on each Session start (UAC every time)."
            ),
            xalign=0,
            wrap=True,
        )
        ip_hint.add_css_class("rdv-mode-hint")
        ip_box.append(ip_hint)
        ip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.input_provider_enable = Gtk.CheckButton(label="Use provider")
        self.input_provider_enable.set_tooltip_text(
            "When checked, session_start sends input_provider. Empty path → "
            "default Windows pipe hogwarts-input. Unchecked = local inject only "
            "(agent.json can still enable a provider)."
        )
        ip_row.append(self.input_provider_enable)
        self.input_provider_entry = Gtk.Entry()
        self.input_provider_entry.set_hexpand(True)
        self.input_provider_entry.set_placeholder_text(
            r"empty = \\.\pipe\hogwarts-input   or  C:\tools\helper.exe"
        )
        self.input_provider_entry.set_tooltip_text(
            "Empty + Use provider → kind=pipe hogwarts-input. "
            "\\\\.\\pipe\\… → pipe. Else → kind=exec spawn. "
            "Protocol: HELLO hogwarts-input/1, JSON events, BYE."
        )
        ip_row.append(self.input_provider_entry)
        btn_pipe = Gtk.Button(label="Default pipe")
        btn_pipe.add_css_class("flat")
        btn_pipe.set_tooltip_text(
            "Fill Windows default named pipe and enable Use provider"
        )
        btn_pipe.connect("clicked", lambda *_: self._fill_default_input_pipe())
        ip_row.append(btn_pipe)
        ip_box.append(ip_row)
        session.append(ip_box)
        self._load_input_provider_prefs()

        self.session_lab = Gtk.Label(
            label="No Keepstream session — click Start Keepstream.",
            xalign=0,
            wrap=True,
            selectable=True,
        )
        self.session_lab.add_css_class("rdv-session-info")
        session.append(self.session_lab)

        self._main_stack = Gtk.Stack()
        self._main_stack.set_hexpand(True)
        self._main_stack.set_vexpand(True)
        self._main_stack.add_named(main, "view")
        self._main_stack.add_named(session, "session")
        body.append(self._main_stack)
        root.append(body)

        # Status
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_bar.add_css_class("rdv-statusbar")
        self.status = Gtk.Label(label="Ready — Capture or pick from archive", xalign=0)
        self.status.add_css_class("rdv-status")
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        status_bar.append(self.status)
        self.meta_lab = Gtk.Label(label="", xalign=1)
        self.meta_lab.add_css_class("rdv-meta")
        status_bar.append(self.meta_lab)
        root.append(status_bar)

        # Keys
        key = Gtk.EventControllerKey()

        def on_key(
            _c: Gtk.EventControllerKey,
            keyval: int,
            _keycode: int,
            state: Gdk.ModifierType,
        ) -> bool:
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if keyval == Gdk.KEY_Escape and self._fullscreen:
                self._toggle_fullscreen()
                return True
            if keyval == Gdk.KEY_F11:
                self._toggle_fullscreen()
                return True
            # When remote input is active (Control or Keepstream Session), do not
            # steal Left/Right/Space/R/F/1 for archive/zoom — send them remote.
            remote_input = False
            try:
                remote_input = bool(self._accepts_remote_input())
            except Exception:
                remote_input = self._mode == "control"
            if remote_input and not ctrl:
                name = Gdk.keyval_name(keyval) or ""
                if len(name) == 1 or name.lower() in (
                    "return",
                    "escape",
                    "backspace",
                    "tab",
                    "space",
                    "up",
                    "down",
                    "left",
                    "right",
                    "delete",
                ):
                    self._send_input([{"type": "key", "key": name}])
                    return True
                # Unmapped keys still consumed so they do not hit local shortcuts
                return True
            if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self._nudge_zoom(1)
                return True
            if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self._nudge_zoom(-1)
                return True
            if keyval == Gdk.KEY_f and not ctrl:
                self._set_zoom(None)
                return True
            if keyval == Gdk.KEY_1 and not ctrl:
                self._set_zoom(1.0)
                return True
            if keyval == Gdk.KEY_Left:
                self._hist_step(-1)
                return True
            if keyval == Gdk.KEY_Right:
                self._hist_step(1)
                return True
            if keyval in (Gdk.KEY_r, Gdk.KEY_R) and not ctrl:
                self._do_shot()
                return True
            if keyval == Gdk.KEY_space and not ctrl:
                self._do_shot()
                return True
            if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and self._mode != "control":
                self._delete_selected()
                return True
            if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
                self._copy_frame()
                return True
            if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
                self._save_as()
                return True
            if ctrl and keyval in (Gdk.KEY_o, Gdk.KEY_O):
                self._open_folder()
                return True
            return False

        key.connect("key-pressed", on_key)
        self.add_controller(key)
        self.connect("close-request", self._on_close)

        # Load existing archive from disk
        self._load_archive_from_disk()
        if initial_bytes:
            self.apply_frame(initial_bytes, note=initial_note, ok=True)
        else:
            self._update_hist_ui()
            if self._items:
                self._select_index(0)

    # ── Public API ───────────────────────────────────────────────────

    def apply_frame(
        self,
        data: bytes,
        *,
        note: str = "",
        ok: bool | None = True,
        record_history: bool = True,
        path: Path | None = None,
    ) -> None:
        """Decode and display a frame; archive new captures to disk.

        Keepstream / Live stream frames take a **fast path**: JPEG→pixbuf→paint
        only. Skipping history/title/status thrash every frame is what keeps
        Control + Session responsive under 20–30 fps H.264.
        """
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pb = loader.get_pixbuf()
            if pb is None:
                raise RuntimeError("no pixbuf")
        except Exception as exc:
            self._set_status(f"Decode failed: {exc}", ok=False)
            return

        self._raw = data
        self._pixbuf = pb
        w, h = pb.get_width(), pb.get_height()
        self._note = note or f"{w}×{h} · {_fmt_bytes(len(data))}"
        # Stream frames must NEVER flood ~/Pictures unless "Archive live" is on
        note_u = self._note.upper()
        is_stream = (
            (not record_history)
            or note_u.startswith(("LIVE", "SESSION", "STREAM", "KEEPSTREAM"))
            or " · #" in self._note  # Keepstream frame_id notes
        )

        if is_stream and not self._archive_live:
            # ── Fast path (Session / Live) ──────────────────────────
            try:
                self.empty_lab.set_visible(False)
            except Exception:
                pass
            self._capturing = False
            self._current_path = None
            self._render()
            # Throttle chrome updates so paint doesn't starve input
            import time as _time

            now = _time.monotonic()
            last = float(getattr(self, "_stream_chrome_ts", 0.0) or 0.0)
            if now - last >= 0.35:
                self._stream_chrome_ts = now
                self._set_status(self._note, ok=ok)
                self._update_meta()
                # Title less often — WM updates are relatively expensive
                if now - float(getattr(self, "_stream_title_ts", 0.0) or 0.0) >= 1.5:
                    self._stream_title_ts = now
                    self.set_title(
                        f"Remote Viewer - {self._agent_label} · {w}×{h}"
                    )
            return

        if record_history and not self._loading_archive:
            should_archive = (not is_stream) or self._archive_live
            if should_archive:
                saved = path or self._archive_to_disk(
                    data, pb, note=self._note, live=is_stream
                )
                if saved is not None:
                    self._current_path = saved
                    self._prepend_item(saved, data, pb, self._note)
            else:
                # Stream preview without archiving — still show
                self._current_path = None
        elif path is not None:
            self._current_path = path

        self._capture_gen = int(getattr(self, "_capture_gen", 0)) + 1
        self._capturing = False
        self.btn_shot.set_sensitive(True)
        try:
            self.empty_lab.set_visible(False)
        except Exception:
            pass
        self._render()
        self._set_status(self._note, ok=ok)
        self._update_meta()
        self._update_hist_ui()
        self.set_title(
            f"Remote Viewer - {self._agent_label} · {w}×{h}"
        )

    def set_status(self, msg: str, *, ok: bool | None = None) -> None:
        self._set_status(msg, ok=ok)
        # Surface session notes into Session panel
        if self._mode == "session" or "VNC" in msg or "session" in msg.lower():
            try:
                self.session_lab.set_text(msg)
            except Exception:
                pass

    def set_session_info(self, info: dict[str, Any]) -> None:
        """Update Session panel from desktop_start/stop results."""
        self._session_info = dict(info or {})
        lines = []
        mode = info.get("mode") or info.get("started")
        if mode:
            lines.append(f"mode: {mode}")
        if info.get("session_id"):
            lines.append(f"session_id: {info.get('session_id')}")
        if info.get("port"):
            host = info.get("connect_host") or info.get("host") or "?"
            lines.append(
                f"connect: {host}:{info.get('port')}  "
                f"bind={info.get('bind')}  codec={info.get('codec')}"
            )
        note = info.get("note")
        if note:
            lines.append(str(note))
        if "elevated" in info:
            elev = info.get("elevated")
            if elev is False:
                lines.append(
                    "agent not elevated — Task Manager / admin apps block "
                    "input unless input_provider is set"
                )
            elif elev is True:
                lines.append("agent elevated — can control admin UI")
        ip = info.get("input_provider") if isinstance(info.get("input_provider"), dict) else None
        if ip:
            if ip.get("active"):
                lines.append(
                    f"input_provider: active ({ip.get('kind') or '?'} "
                    f"{ip.get('command') or ip.get('pipe') or ''})".strip()
                )
            elif ip.get("error"):
                lines.append(f"input_provider failed: {ip.get('error')} (local inject)")
            elif ip.get("note"):
                lines.append(f"input_provider: {ip.get('note')}")
        vnc = info.get("vnc") if isinstance(info.get("vnc"), dict) else {}
        if vnc.get("started"):
            lines.append(
                f"VNC: {vnc.get('bind', '127.0.0.1')}:{vnc.get('port')} "
                f"(tunnel via SOCKS → agent loopback)"
            )
        elif vnc.get("error"):
            lines.append(f"VNC: {vnc.get('error')} — {vnc.get('hint') or ''}")
        if info.get("stopped"):
            lines.append("Session stopped.")
        self.session_lab.set_text("\n".join(lines) if lines else "Session updated.")

    def attach_keepstream(self, client: Any) -> None:
        """Attach a live KeepstreamClient; frames already applied via page callbacks."""
        self._keepstream = client
        self.live_badge.set_text("SESSION")
        self.live_badge.add_css_class("rdv-badge-live")
        # Show the picture surface (not the setup form) and accept input
        self._main_stack.set_visible_child_name("view")
        if not self._mode_session.get_active():
            self._mode_session.set_active(True)
        else:
            self._set_mode("session")
        try:
            self.picture.set_cursor_from_name("none")
        except Exception:
            pass
        # Stop task-poll Live — Keepstream owns frames now
        if self.btn_live.get_active() and self._on_live:
            self.btn_live.set_active(False)
        self._set_status("Keepstream Session attached — stream + input over TCP", ok=True)

    def _accepts_remote_input(self) -> bool:
        """Control mode, or Session mode while Keepstream is connected."""
        if self._mode == "control":
            return True
        if self._mode == "session":
            ks = self._keepstream
            return bool(ks is not None and getattr(ks, "connected", False))
        return False

    def set_live_active(self, on: bool) -> None:
        if bool(self.btn_live.get_active()) != bool(on):
            self.btn_live.set_active(bool(on))
        self.live_badge.set_text("LIVE" if on else "")
        if on:
            self.live_badge.add_css_class("rdv-badge-live")
        else:
            self.live_badge.remove_css_class("rdv-badge-live")

    def current_max_side(self) -> int:
        i = int(self.quality_dd.get_selected())
        if i < 0 or i >= len(_QUALITY):
            return 1920
        return _QUALITY[i][1]

    def _on_quality_changed(self) -> None:
        side = self.current_max_side()
        self._max_side = side
        self._set_status(f"Quality set to max_side={side} (next Capture / Live frame)", ok=None)

    def archive_dir(self) -> Path:
        return self._archive_dir

    # ── Modes / control ──────────────────────────────────────────────

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._control_on = mode == "control"
        ks_up = bool(
            self._keepstream is not None
            and getattr(self._keepstream, "connected", False)
        )
        if mode == "session" and not ks_up:
            # Setup panel until Keepstream is connected
            self._main_stack.set_visible_child_name("session")
            self.mode_hint.set_text(
                "Session: Start Keepstream for continuous JPEG + input (not task-poll)"
            )
        else:
            # Stream frames live in the picture view
            self._main_stack.set_visible_child_name("view")
            if mode == "session" and ks_up:
                self.mode_hint.set_text(
                    "Session (Keepstream): continuous stream · click/drag injects over TCP"
                )
                try:
                    self.picture.set_cursor_from_name("none")
                except Exception:
                    pass
            elif mode == "control":
                self.mode_hint.set_text(
                    "Control: host cursor in-frame · input prioritized · "
                    "Live ~300ms @ lighter res (or use Session for true stream)"
                )
                # Snappier Live while controlling (lighter frames + faster poll)
                # Skip task Live if Keepstream already streaming
                if not ks_up:
                    self._set_live_interval(0.3)
                    if not self.btn_live.get_active() and self._on_live:
                        self.btn_live.set_active(True)
                try:
                    self.picture.set_cursor_from_name("none")
                except Exception:
                    self.picture.set_cursor_from_name("default")
            else:
                self.mode_hint.set_text(
                    "View: Capture / Live / archive — host cursor composited into frames"
                )
                if not ks_up:
                    self._set_live_interval(0.8)
                    self.picture.set_cursor_from_name("default")
                self._flush_input(force=True)

    def _set_live_interval(self, sec: float) -> None:
        sec = max(0.25, min(float(sec), 10.0))
        if abs(sec - float(self._live_interval_sec)) < 0.01:
            return
        self._live_interval_sec = sec
        if self._on_live_interval:
            self._on_live_interval(sec)

    def _widget_xy_to_frac(
        self, widget: Gtk.Widget, x: float, y: float
    ) -> tuple[float, float] | None:
        """Map click on Picture (CONTAIN) to 0..1 of remote frame."""
        if self._pixbuf is None:
            return None
        ww = max(1, widget.get_width())
        wh = max(1, widget.get_height())
        iw = max(1, self._pixbuf.get_width())
        ih = max(1, self._pixbuf.get_height())
        scale = min(ww / iw, wh / ih)
        dw, dh = iw * scale, ih * scale
        ox, oy = (ww - dw) / 2.0, (wh - dh) / 2.0
        if x < ox or y < oy or x > ox + dw or y > oy + dh:
            return None
        fx = (x - ox) / dw
        fy = (y - oy) / dh
        return max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy))

    def _queue_input(self, event: dict[str, Any]) -> None:
        """Batch input events; flush on a short timer (lower task spam, snappier feel)."""
        if not event:
            return
        self._input_queue.append(event)
        # Cap queue so a stuck flush can't grow forever
        if len(self._input_queue) > 40:
            self._input_queue = self._input_queue[-40:]
        if self._input_flush_src is None:
            # 12ms batch — coalesce moves, still feel immediate on clicks
            self._input_flush_src = GLib.timeout_add(12, self._flush_input_timer)

    def _flush_input_timer(self) -> bool:
        self._input_flush_src = None
        self._flush_input(force=False)
        return False

    def _flush_input(self, *, force: bool = False) -> None:
        if not self._input_queue:
            return
        # Coalesce consecutive moves — keep last move only, preserve click/key order
        batch: list[dict[str, Any]] = []
        pending_move: dict[str, Any] | None = None
        for ev in self._input_queue:
            if str(ev.get("type") or "") == "move":
                pending_move = ev
            else:
                if pending_move is not None:
                    batch.append(pending_move)
                    pending_move = None
                batch.append(ev)
        if pending_move is not None:
            batch.append(pending_move)
        self._input_queue.clear()
        if not batch:
            return
        # Prefer Keepstream Session (no plane RTT)
        ks = self._keepstream
        if ks is not None and getattr(ks, "connected", False):
            try:
                ks.send_input(batch[:32])
                return
            except Exception as exc:
                self._set_status(f"Keepstream input failed: {exc}", ok=False)
        if not self._on_desktop_input:
            self._set_status("desktop_input not wired on desk", ok=False)
            return
        self._on_desktop_input(batch[:32])
        kind = str(batch[-1].get("type") or "input")
        if kind in ("click", "dblclick", "down", "up"):
            self._set_status(
                f"Input {kind} @ "
                f"({batch[-1].get('fx', 0):.2f}, {batch[-1].get('fy', 0):.2f}) "
                f"×{len(batch)}",
                ok=True,
            )
        elif force or kind == "key":
            self._set_status(f"Input {kind} ×{len(batch)}", ok=True)

    def _send_input(self, events: list[dict[str, Any]]) -> None:
        """Immediate path (keys); still goes through coalesce queue briefly."""
        for ev in events:
            self._queue_input(ev)
        self._flush_input(force=True)

    def _do_socks(self) -> None:
        if self._on_socks_start:
            self._on_socks_start()
            self._set_status("SOCKS start queued…", ok=None)
            self.session_lab.set_text(
                (self.session_lab.get_text() or "")
                + "\nSOCKS start queued — check Agents/Tasks for port."
            )
        else:
            self._set_status("SOCKS callback not available", ok=False)

    def _copy_vnc_hint(self) -> None:
        vnc = self._session_info.get("vnc") if self._session_info else {}
        if not isinstance(vnc, dict):
            vnc = {}
        port = vnc.get("port") or 5901
        text = (
            f"# After socks_start / tunnel to agent:\n"
            f"vncviewer 127.0.0.1:{port}\n"
            f"# Or: Control mode in this viewer (desktop_input) while Live is on\n"
        )
        display = Gdk.Display.get_default()
        if display is None:
            self._set_status("No clipboard", ok=False)
            return
        display.get_clipboard().set(text)
        self._set_status("VNC hint copied", ok=True)

    # ── Archive persistence ──────────────────────────────────────────

    def _load_archive_from_disk(self) -> None:
        self._items.clear()
        try:
            files = sorted(
                [
                    p
                    for p in self._archive_dir.iterdir()
                    if p.is_file()
                    and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".img")
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            files = []
        for p in files[:_ARCHIVE_CAP]:
            try:
                data = p.read_bytes()
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pb = loader.get_pixbuf()
                if pb is None:
                    continue
                st = p.stat()
                self._items.append(
                    {
                        "path": p,
                        "bytes": data,
                        "w": pb.get_width(),
                        "h": pb.get_height(),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "note": p.name,
                        "thumb": pb.scale_simple(
                            _THUMB,
                            max(1, int(_THUMB * pb.get_height() / max(1, pb.get_width()))),
                            GdkPixbuf.InterpType.BILINEAR,
                        ),
                    }
                )
            except Exception:
                continue
        self._rebuild_archive_list()

    def _archive_to_disk(
        self,
        data: bytes,
        pb: GdkPixbuf.Pixbuf,
        *,
        note: str,
        live: bool,
    ) -> Path | None:
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(f"Archive folder error: {exc}", ok=False)
            return None
        ext = _detect_ext(data)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = "live" if live else "shot"
        name = f"{stamp}-{tag}-{pb.get_width()}x{pb.get_height()}.{ext}"
        # Avoid overwrite if same second
        path = self._archive_dir / name
        n = 1
        while path.exists():
            path = self._archive_dir / f"{stamp}-{tag}-{pb.get_width()}x{pb.get_height()}_{n}.{ext}"
            n += 1
        try:
            path.write_bytes(data)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            self._set_status(f"Archive write failed: {exc}", ok=False)
            return None
        self._trim_archive_files()
        return path

    def _trim_archive_files(self) -> None:
        try:
            files = sorted(
                [p for p in self._archive_dir.iterdir() if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for old in files[_ARCHIVE_CAP:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _prepend_item(
        self, path: Path, data: bytes, pb: GdkPixbuf.Pixbuf, note: str
    ) -> None:
        """Insert a new capture at the top without rebuilding every row."""
        path_s = str(path)
        # Drop duplicate path from model + widget map
        self._items = [it for it in self._items if str(it.get("path") or "") != path_s]
        old_btn = self._archive_btns.pop(path_s, None)
        if old_btn is not None:
            try:
                self.archive_list.remove(old_btn)
            except Exception:
                pass
        try:
            thumb = pb.scale_simple(
                _THUMB,
                max(1, int(_THUMB * pb.get_height() / max(1, pb.get_width()))),
                GdkPixbuf.InterpType.BILINEAR,
            )
        except Exception:
            thumb = None
        item = {
            "path": path,
            "bytes": data,
            "w": pb.get_width(),
            "h": pb.get_height(),
            "size": len(data),
            "mtime": path.stat().st_mtime if path.exists() else datetime.now().timestamp(),
            "note": note,
            "thumb": thumb,
        }
        self._items.insert(0, item)
        # Cap: drop oldest items + their buttons
        while len(self._items) > _ARCHIVE_CAP:
            drop = self._items.pop()
            drop_s = str(drop.get("path") or "")
            drop_btn = self._archive_btns.pop(drop_s, None)
            if drop_btn is not None:
                try:
                    self.archive_list.remove(drop_btn)
                except Exception:
                    pass

        self._selected_i = 0
        # Empty-state label → full rebuild is simpler
        first = self.archive_list.get_first_child()
        if first is not None and not isinstance(first, Gtk.Button):
            self._rebuild_archive_list()
            return
        if not self._archive_btns and first is None:
            self._rebuild_archive_list()
            return

        # Soft prepend: only construct the new row widget
        btn = self._archive_row(0, item)
        self.archive_list.prepend(btn)
        self.archive_count.set_text(str(len(self._items)))
        self._mark_archive_selected(0)

    def _rebuild_archive_list(self) -> None:
        while child := self.archive_list.get_first_child():
            self.archive_list.remove(child)
        self._archive_btns.clear()
        self.archive_count.set_text(str(len(self._items)))
        if not self._items:
            empty = Gtk.Label(label="No captures yet", xalign=0.5)
            empty.add_css_class("rdv-empty")
            empty.set_margin_top(16)
            self.archive_list.append(empty)
            return
        for i, it in enumerate(self._items):
            self.archive_list.append(self._archive_row(i, it))

    def _archive_row(self, index: int, it: dict[str, Any]) -> Gtk.Widget:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("rdv-arch-row")
        if index == self._selected_i:
            btn.add_css_class("rdv-arch-row-selected")
        btn.set_halign(Gtk.Align.FILL)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(4)
        row.set_margin_end(4)
        row.set_margin_top(2)
        row.set_margin_bottom(2)

        thumb = it.get("thumb")
        if isinstance(thumb, GdkPixbuf.Pixbuf):
            pic = Gtk.Picture.new_for_pixbuf(thumb)
            pic.set_size_request(_THUMB, max(40, thumb.get_height()))
            pic.set_can_shrink(False)
            pic.add_css_class("rdv-thumb")
            row.append(pic)
        else:
            row.append(_img("image-x-generic", 32))

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        col.set_hexpand(True)
        path: Path = it["path"]
        path_s = str(path)
        name = path.name
        # Short time label
        try:
            ts = datetime.fromtimestamp(float(it.get("mtime") or 0)).strftime(
                "%m/%d %H:%M:%S"
            )
        except (TypeError, ValueError, OSError):
            ts = name[:15]
        tlab = Gtk.Label(label=ts, xalign=0)
        tlab.add_css_class("rdv-arch-time")
        col.append(tlab)
        dim = f"{it.get('w','?')}×{it.get('h','?')}  ·  {_fmt_bytes(int(it.get('size') or 0))}"
        dlab = Gtk.Label(label=dim, xalign=0)
        dlab.add_css_class("rdv-arch-dim")
        dlab.set_ellipsize(Pango.EllipsizeMode.END)
        col.append(dlab)
        row.append(col)

        btn.set_child(row)
        btn.set_tooltip_text(path_s)
        # Path-keyed click — stable across soft prepend (indices would go stale)
        btn.connect("clicked", lambda *_a, p=path_s: self._select_by_path(p))
        self._archive_btns[path_s] = btn
        return btn

    def _select_by_path(self, path_s: str) -> None:
        for i, it in enumerate(self._items):
            if str(it.get("path") or "") == path_s:
                self._select_index(i)
                return

    def _mark_archive_selected(self, index: int) -> None:
        """Update selection CSS without rebuilding the whole archive list."""
        sel = ""
        if 0 <= index < len(self._items):
            sel = str(self._items[index].get("path") or "")
        for path_s, btn in self._archive_btns.items():
            if path_s and path_s == sel:
                btn.add_css_class("rdv-arch-row-selected")
            else:
                btn.remove_css_class("rdv-arch-row-selected")

    def _select_index(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        self._selected_i = index
        it = self._items[index]
        self._loading_archive = True
        try:
            data = it.get("bytes")
            if not data and it.get("path"):
                data = Path(it["path"]).read_bytes()
                it["bytes"] = data
            if data:
                self.apply_frame(
                    data,
                    note=str(it.get("note") or Path(it["path"]).name),
                    ok=True,
                    record_history=False,
                    path=Path(it["path"]) if it.get("path") else None,
                )
                self._current_path = Path(it["path"]) if it.get("path") else None
        except Exception as exc:
            self._set_status(f"Load failed: {exc}", ok=False)
        finally:
            self._loading_archive = False
        # Soft selection paint — do not destroy archive rows (was killing hover)
        path_s = str(it.get("path") or "")
        if self._archive_btns and path_s in self._archive_btns:
            self._mark_archive_selected(index)
        else:
            self._rebuild_archive_list()
        self._update_hist_ui()

    # ── Actions ──────────────────────────────────────────────────────

    def _delete_selected(self) -> None:
        if self._selected_i < 0 or self._selected_i >= len(self._items):
            self._set_status("Nothing selected to delete", ok=False)
            return
        it = self._items[self._selected_i]
        path = it.get("path")
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                self._set_status(f"Delete failed: {exc}", ok=False)
                return
        del self._items[self._selected_i]
        if not self._items:
            self._selected_i = -1
            self._raw = None
            self._pixbuf = None
            self._current_path = None
            self.picture.set_paintable(None)
            self._rebuild_archive_list()
            self._update_hist_ui()
            self._set_status("Archive empty", ok=True)
            self.meta_lab.set_text("")
            return
        self._selected_i = min(self._selected_i, len(self._items) - 1)
        self._select_index(self._selected_i)
        self._set_status(f"Deleted {Path(path).name if path else 'item'}", ok=True)

    def _purge_archive(self) -> None:
        for it in list(self._items):
            p = it.get("path")
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        self._items.clear()
        self._selected_i = -1
        self._raw = None
        self._pixbuf = None
        self._current_path = None
        self.picture.set_paintable(None)
        self._rebuild_archive_list()
        self._update_hist_ui()
        self.meta_lab.set_text("")
        self._set_status("Archive cleared", ok=True)

    def _delete_all(self) -> None:
        if not self._items:
            self._set_status("Archive already empty", ok=None)
            return
        # Gtk.AlertDialog (4.10+); fall back to immediate clear with status
        try:
            dialog = Gtk.AlertDialog()
            dialog.set_message("Clear screenshot archive?")
            dialog.set_detail(
                f"Delete all {len(self._items)} screenshots in:\n{self._archive_dir}"
            )
            dialog.set_buttons(["Cancel", "Delete all"])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)

            def on_done(dlg: Gtk.AlertDialog, result) -> None:
                try:
                    resp = dlg.choose_finish(result)
                except Exception:
                    return
                if resp == 1:
                    self._purge_archive()

            dialog.choose(self, None, on_done)
        except Exception:
            self._purge_archive()

    def _open_folder(self) -> None:
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(f"Cannot create folder: {exc}", ok=False)
            return
        path = str(self._archive_dir)
        # Prefer xdg-open; fall back to Gio
        try:
            subprocess.Popen(
                ["xdg-open", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._set_status(f"Opened {path}", ok=True)
            return
        except OSError:
            pass
        try:
            from gi.repository import Gio

            Gio.AppInfo.launch_default_for_uri(Path(path).as_uri(), None)
            self._set_status(f"Opened {path}", ok=True)
        except Exception as exc:
            self._set_status(f"Open folder failed: {exc}", ok=False)

    def _save_as(self) -> None:
        if not self._raw:
            self._set_status("No frame to save", ok=False)
            return
        ext = _detect_ext(self._raw)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dialog = Gtk.FileDialog(title="Export screenshot")
        dialog.set_initial_name(f"hogwarts-{stamp}.{ext}")
        data = self._raw

        def on_save(dlg: Gtk.FileDialog, result) -> None:
            try:
                gfile = dlg.save_finish(result)
            except Exception:
                return
            if gfile is None:
                return
            path = gfile.get_path()
            if not path:
                self._set_status("Could not resolve path", ok=False)
                return
            try:
                Path(path).write_bytes(data)
                self._set_status(f"Exported {path}", ok=True)
            except OSError as exc:
                self._set_status(f"Export failed: {exc}", ok=False)

        dialog.save(self, None, on_save)

    def _copy_frame(self) -> None:
        if self._pixbuf is None:
            self._set_status("No frame to copy", ok=False)
            return
        display = Gdk.Display.get_default()
        if display is None:
            self._set_status("No display for clipboard", ok=False)
            return
        try:
            texture = Gdk.Texture.new_for_pixbuf(self._pixbuf)
            display.get_clipboard().set(texture)
            self._set_status("Copied image to clipboard", ok=True)
        except Exception as exc:
            self._set_status(f"Copy failed: {exc}", ok=False)

    # ── Capture / live ───────────────────────────────────────────────

    def _do_shot(self) -> None:
        if self._capturing:
            return
        side = self.current_max_side()
        self._capturing = True
        self._capture_gen = int(getattr(self, "_capture_gen", 0)) + 1
        gen = self._capture_gen
        self.btn_shot.set_sensitive(False)
        self._set_status(f"Capturing… (max_side={side})", ok=None)
        if self._on_screenshot:
            self._on_screenshot(side)
        # Re-enable after a short grace if frame never arrives (gen-matched)
        GLib.timeout_add_seconds(12, lambda g=gen: self._clear_capturing(g))

    def _clear_capturing(self, gen: int | None = None) -> bool:
        # Ignore stale timers from a previous Capture
        if gen is not None and gen != getattr(self, "_capture_gen", gen):
            return False
        self._capturing = False
        try:
            self.btn_shot.set_sensitive(True)
        except Exception:
            pass
        return False

    def _do_live(self) -> None:
        on = bool(self.btn_live.get_active())
        if self._on_live:
            self._on_live(on)
        self.set_live_active(on)
        self._set_status(
            "Live view on — polling frames" if on else "Live view off",
            ok=True if on else None,
        )

    def _input_provider_prefs_path(self) -> Path:
        base = Path.home() / ".config" / "hogwarts"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return base / "input_providers.json"

    def _load_input_provider_prefs(self) -> None:
        """Restore per-agent provider path from desk-local prefs."""
        try:
            path = self._input_provider_prefs_path()
            if not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            entry = data.get(self._agent_id) or data.get("default")
            if not isinstance(entry, dict):
                return
            cmd = str(entry.get("command") or "").strip()
            en = bool(entry.get("enabled"))
            if hasattr(self, "input_provider_entry") and cmd:
                self.input_provider_entry.set_text(cmd)
            if hasattr(self, "input_provider_enable"):
                self.input_provider_enable.set_active(en and bool(cmd))
        except Exception:
            pass

    def _save_input_provider_prefs(self) -> None:
        try:
            path = self._input_provider_prefs_path()
            data: dict[str, Any] = {}
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                except Exception:
                    data = {}
            cmd = ""
            en = False
            if hasattr(self, "input_provider_entry"):
                cmd = self.input_provider_entry.get_text().strip()
            if hasattr(self, "input_provider_enable"):
                en = bool(self.input_provider_enable.get_active())
            data[self._agent_id] = {"enabled": en, "command": cmd}
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _fill_default_input_pipe(self) -> None:
        """One-click Windows pipe default for lab input provider."""
        if hasattr(self, "input_provider_entry"):
            self.input_provider_entry.set_text(r"\\.\pipe\hogwarts-input")
        if hasattr(self, "input_provider_enable"):
            self.input_provider_enable.set_active(True)
        self._save_input_provider_prefs()
        self._set_status(
            "Input provider: \\\\.\\pipe\\hogwarts-input (enable + Start Keepstream)",
            ok=True,
        )

    def session_start_options(self) -> dict[str, Any]:
        """Extra session_start payload fields (optional input_provider plug-in)."""
        self._save_input_provider_prefs()
        opts: dict[str, Any] = {}
        if not hasattr(self, "input_provider_entry"):
            return opts
        cmd = self.input_provider_entry.get_text().strip()
        en = bool(self.input_provider_enable.get_active()) if hasattr(
            self, "input_provider_enable"
        ) else False
        if not en:
            # Unchecked: do not send override (agent.json can still enable)
            return opts
        # Enabled: empty path → default Windows named-pipe helper
        if not cmd or cmd in ("pipe", "default", "hogwarts-input"):
            opts["input_provider"] = {
                "enabled": True,
                "kind": "pipe",
                "pipe": r"\\.\pipe\hogwarts-input",
            }
        elif cmd.startswith("\\\\.\\pipe\\") or cmd.startswith("//./pipe/"):
            pipe = cmd.replace("/", "\\")
            opts["input_provider"] = {
                "enabled": True,
                "kind": "pipe",
                "pipe": pipe,
            }
        elif cmd.startswith("pipe:"):
            leaf = cmd.split(":", 1)[1].strip() or "hogwarts-input"
            opts["input_provider"] = {
                "enabled": True,
                "kind": "pipe",
                "pipe": rf"\\.\pipe\{leaf.lstrip(chr(92))}",
            }
        else:
            opts["input_provider"] = {
                "enabled": True,
                "kind": "exec",
                "command": cmd,
                "spawn": True,
            }
        return opts

    def _do_session(self, action: str) -> None:
        opts = self.session_start_options() if action == "start" else {}
        if self._on_session:
            try:
                self._on_session(action, opts)
            except TypeError:
                # Older callback: action only
                self._on_session(action)
        self._set_status(
            "Starting Keepstream…" if action == "start" else "Stopping Session…",
            ok=None,
        )
        if action == "start":
            ip = opts.get("input_provider") if isinstance(opts, dict) else None
            extra = ""
            if isinstance(ip, dict):
                if ip.get("kind") == "pipe":
                    extra = f"\ninput_provider pipe: {ip.get('pipe') or '?'}"
                elif ip.get("command"):
                    extra = f"\ninput_provider exec: {ip.get('command')}"
            self.session_lab.set_text(
                "Starting Keepstream on agent…" + extra
            )
            if not self._mode_session.get_active():
                self._mode_session.set_active(True)
        elif action == "stop":
            self._keepstream = None

    # ── Zoom / display ───────────────────────────────────────────────

    def _set_status(self, msg: str, *, ok: bool | None = None) -> None:
        self.status.set_text(msg)
        self.status.remove_css_class("rdv-status-ok")
        self.status.remove_css_class("rdv-status-fail")
        if ok is True:
            self.status.add_css_class("rdv-status-ok")
        elif ok is False:
            self.status.add_css_class("rdv-status-fail")

    def _update_meta(self) -> None:
        if self._pixbuf is None:
            self.meta_lab.set_text("")
            return
        bits = [
            f"{self._pixbuf.get_width()}×{self._pixbuf.get_height()}",
            _fmt_bytes(len(self._raw or b"")),
        ]
        if self._current_path:
            bits.append(self._current_path.name)
        self.meta_lab.set_text("  ·  ".join(bits))

    def _update_hist_ui(self) -> None:
        n = len(self._items)
        i = self._selected_i + 1 if n and self._selected_i >= 0 else 0
        self.hist_lab.set_text(f"{i} / {n}")
        self.btn_prev.set_sensitive(self._selected_i > 0)
        self.btn_next.set_sensitive(
            self._selected_i >= 0 and self._selected_i < n - 1
        )
        self.btn_delete.set_sensitive(self._selected_i >= 0 and n > 0)
        self.btn_delete_all.set_sensitive(n > 0)

    def _hist_step(self, delta: int) -> None:
        if not self._items:
            return
        # Archive is newest-first; "next" goes to older (higher index)
        ni = self._selected_i + delta
        if ni < 0 or ni >= len(self._items):
            return
        self._select_index(ni)

    def _on_zoom_dd(self) -> None:
        i = int(self.zoom_dd.get_selected())
        if 0 <= i < len(_ZOOM_PRESETS):
            self._zoom_mode = _ZOOM_PRESETS[i][1]
            self._render()

    def _set_zoom(self, scale: float | None) -> None:
        self._zoom_mode = scale
        for i, (_, s) in enumerate(_ZOOM_PRESETS):
            if s == scale:
                if int(self.zoom_dd.get_selected()) != i:
                    self.zoom_dd.set_selected(i)
                break
        self._render()

    def _nudge_zoom(self, direction: int) -> None:
        scales = [s for _, s in _ZOOM_PRESETS if s is not None]
        cur = self._zoom_mode
        if cur is None:
            self._set_zoom(1.0 if direction > 0 else 0.5)
            return
        if direction > 0:
            bigger = [s for s in scales if s > cur + 1e-6]
            self._set_zoom(bigger[0] if bigger else scales[-1])
        else:
            smaller = [s for s in scales if s < cur - 1e-6]
            self._set_zoom(smaller[-1] if smaller else scales[0])

    def _render(self) -> None:
        pb = self._pixbuf
        if pb is None:
            return
        nw, nh = pb.get_width(), pb.get_height()
        if self._zoom_mode is None:
            self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.picture.set_can_shrink(True)
            self.picture.set_pixbuf(pb)
            self.picture.set_size_request(-1, -1)
            return
        scale = float(self._zoom_mode)
        tw = max(1, int(nw * scale))
        th = max(1, int(nh * scale))
        scaled = (
            pb
            if tw == nw and th == nh
            else pb.scale_simple(tw, th, GdkPixbuf.InterpType.BILINEAR)
        )
        self.picture.set_content_fit(Gtk.ContentFit.FILL)
        self.picture.set_can_shrink(False)
        self.picture.set_pixbuf(scaled)
        self.picture.set_size_request(tw, th)

    def _toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self.fullscreen()
        else:
            self.unfullscreen()

    def _on_close(self, *_a) -> bool:
        if self._input_flush_src is not None:
            try:
                GLib.source_remove(self._input_flush_src)
            except Exception:
                pass
            self._input_flush_src = None
        self._capture_gen = int(getattr(self, "_capture_gen", 0)) + 1
        self._capturing = False
        if self._on_closed:
            self._on_closed()
        return False
