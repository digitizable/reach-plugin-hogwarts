"""Handset — C2 desk for Reach: channel, listener, egress, playbooks."""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# ── Inline theme (plugin-local; does not depend on Reach CSS) ─────
_HANDSET_CSS = b"""
.handset-page {
  background-color: #111111;
  color: #e8e8e8;
}
.handset-header {
  background-color: #0d0d0d;
  border-bottom: 1px solid #222222;
  padding: 10px 16px;
  min-height: 44px;
}
.handset-title {
  font-size: 1.05rem;
  font-weight: 700;
  color: #f0f0f0;
}
.handset-sub {
  font-size: 0.82rem;
  color: #8a8a8a;
}
.handset-banner {
  background-color: #1a1814;
  border: 1px solid #3d3520;
  border-radius: 10px;
  padding: 10px 12px;
  color: #c9b27a;
  font-size: 0.82rem;
}
.handset-split {
  min-height: 0;
}
.handset-sidebar {
  background-color: #0f0f0f;
  border-right: 1px solid #222;
  min-width: 200px;
  padding: 12px 10px;
}
.handset-nav-btn {
  border-radius: 10px;
  min-height: 40px;
  padding: 0 12px;
  margin: 2px 0;
  background: transparent;
  color: #a0a0a0;
  border: 1px solid transparent;
}
.handset-nav-btn:hover {
  background-color: #161616;
  color: #e0e0e0;
}
.handset-nav-btn:checked,
.handset-nav-btn:active {
  background-color: #1a1a1c;
  color: #e8e8e8;
  border-color: #2a2a2e;
}
.handset-main {
  background-color: #111111;
  min-width: 0;
}
.handset-panel {
  padding: 20px 24px 28px 24px;
}
.handset-hero {
  background: linear-gradient(145deg, #161a22 0%, #12141a 55%, #0e1014 100%);
  border: 1px solid #2a3140;
  border-radius: 14px;
  padding: 16px 18px;
}
.handset-hero-title {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #6a7a94;
}
.handset-hero-state {
  font-size: 1.4rem;
  font-weight: 700;
  color: #f2f2f2;
}
.handset-hero-meta {
  font-size: 0.88rem;
  color: #a8b0c0;
  font-family: monospace;
}
.handset-dot {
  min-width: 10px;
  min-height: 10px;
  border-radius: 99px;
  background-color: #555;
}
.handset-dot-live {
  background-color: #5fbf70;
  box-shadow: 0 0 8px rgba(95, 191, 112, 0.45);
}
.handset-dot-idle {
  background-color: #707070;
}
.handset-dot-busy {
  background-color: #6aa3e8;
  box-shadow: 0 0 8px rgba(106, 163, 232, 0.4);
}
.handset-dot-off {
  background-color: #e86a6a;
}
.handset-card {
  background-color: #161616;
  border: 1px solid #262626;
  border-radius: 12px;
  padding: 14px 16px;
}
.handset-card-title {
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #707070;
  margin-bottom: 2px;
}
.handset-kv-key {
  font-size: 0.75rem;
  color: #666;
  font-weight: 600;
  min-width: 72px;
}
.handset-kv-val {
  font-size: 0.88rem;
  color: #d4d4d4;
  font-family: monospace;
}
.handset-field-label {
  font-size: 0.78rem;
  font-weight: 600;
  color: #8a8a8a;
}
.handset-section {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #5a5a5a;
}
.handset-muted {
  color: #8a8a8a;
  font-size: 0.85rem;
}
.handset-probe-row {
  background-color: #141414;
  border: 1px solid #222;
  border-radius: 10px;
  padding: 10px 12px;
  margin: 4px 0;
}
.handset-probe-label {
  font-weight: 600;
  font-size: 0.9rem;
  color: #e0e0e0;
}
.handset-probe-target {
  font-size: 0.78rem;
  color: #707070;
  font-family: monospace;
}
.handset-ok {
  color: #8fd19e;
  font-weight: 700;
  font-size: 0.8rem;
}
.handset-fail {
  color: #e89a9a;
  font-weight: 700;
  font-size: 0.8rem;
}
.handset-log {
  font-family: monospace;
  font-size: 0.8rem;
  color: #b0b8c8;
  background-color: #0c0c0c;
  border: 1px solid #222;
  border-radius: 10px;
  padding: 10px 12px;
}
.handset-chip {
  font-size: 0.7rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 2px 8px;
  background-color: #222;
  color: #9a9a9a;
}
.handset-chip-live {
  background-color: #1a2a1c;
  color: #8fd19e;
}
.handset-action-grid {
  margin-top: 4px;
}
"""


def create_page(ctx):
    return HandsetPage(ctx)


def _apply_css(widget: Gtk.Widget) -> None:
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(_HANDSET_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
    except Exception:
        pass


class HandsetPage(Gtk.Box):
    """Two-pane operator desk: Channel · Listener · Egress · Ops · Log."""

    def __init__(self, ctx) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("page")
        self.add_css_class("handset-page")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._ctx = ctx
        self._busy = False
        self._probe_busy = False
        self._log_lines: list[str] = []
        _apply_css(self)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.add_css_class("handset-header")
        header.set_hexpand(True)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        titles.set_hexpand(True)
        titles.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label="Handset", xalign=0)
        t.add_css_class("handset-title")
        titles.append(t)
        s = Gtk.Label(
            label="C2 · channel · listener · egress",
            xalign=0,
        )
        s.add_css_class("handset-sub")
        titles.append(s)
        header.append(titles)

        self._chip = Gtk.Label(label="—")
        self._chip.add_css_class("handset-chip")
        self._chip.set_valign(Gtk.Align.CENTER)
        header.append(self._chip)

        refresh = Gtk.Button()
        refresh.set_icon_name("view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Refresh channel")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self._refresh_all())
        header.append(refresh)
        self.append(header)

        # Split body
        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        split.add_css_class("handset-split")
        split.set_hexpand(True)
        split.set_vexpand(True)

        # Sidebar nav
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.add_css_class("handset-sidebar")
        side.set_vexpand(True)
        side.set_hexpand(False)
        side.set_size_request(210, -1)

        side_lab = Gtk.Label(label="Desk", xalign=0)
        side_lab.add_css_class("handset-section")
        side_lab.set_margin_start(8)
        side_lab.set_margin_bottom(6)
        side.append(side_lab)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(160)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._nav_group: Gtk.ToggleButton | None = None
        for key, label, icon in (
            ("channel", "Channel", "network-wired-symbolic"),
            ("listener", "Listener", "network-server-symbolic"),
            ("egress", "Egress", "network-transmit-receive-symbolic"),
            ("ops", "Ops kit", "folder-symbolic"),
            ("log", "Session log", "utilities-terminal-symbolic"),
        ):
            btn = Gtk.ToggleButton()
            btn.add_css_class("handset-nav-btn")
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
        trust = Gtk.Label(
            label="C2",
            xalign=0.5,
        )
        trust.add_css_class("handset-muted")
        trust.set_margin_bottom(4)
        side.append(trust)

        split.append(side)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.add_css_class("handset-main")
        main.set_hexpand(True)
        main.set_vexpand(True)
        main.append(self._stack)
        split.append(main)
        self.append(split)

        # Build panels
        self._stack.add_named(self._build_channel(), "channel")
        self._stack.add_named(self._build_listener(), "listener")
        self._stack.add_named(self._build_egress(), "egress")
        self._stack.add_named(self._build_ops(), "ops")
        self._stack.add_named(self._build_log(), "log")
        self._stack.set_visible_child_name("channel")

        self._refresh_all()
        self._log("Handset ready")

    # ── Nav ───────────────────────────────────────────────────────

    def _on_nav(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            self._stack.set_visible_child_name(key)

    # ── Channel panel ─────────────────────────────────────────────

    def _build_channel(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("handset-panel")

        banner = Gtk.Label(
            label=(
                "Command-and-control desk: reverse reachback, path-aware "
                "channel status, egress matrix, and session playbooks."
            ),
            wrap=True,
            xalign=0,
        )
        banner.add_css_class("handset-banner")
        body.append(banner)

        # Hero status card
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hero.add_css_class("handset-hero")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._dot = Gtk.Box()
        self._dot.add_css_class("handset-dot")
        self._dot.add_css_class("handset-dot-idle")
        self._dot.set_valign(Gtk.Align.CENTER)
        top.append(self._dot)
        ht = Gtk.Label(label="Channel", xalign=0)
        ht.add_css_class("handset-hero-title")
        ht.set_hexpand(True)
        top.append(ht)
        hero.append(top)

        self._state_lab = Gtk.Label(label="—", xalign=0)
        self._state_lab.add_css_class("handset-hero-state")
        hero.append(self._state_lab)

        self._path_lab = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self._path_lab.add_css_class("handset-hero-meta")
        hero.append(self._path_lab)
        body.append(hero)

        # Quick facts card
        body.append(self._sec("Path facts"))
        facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        facts.add_css_class("handset-card")
        self._fact_socks = self._kv_row("SOCKS")
        self._fact_hops = self._kv_row("Hops")
        self._fact_fp = self._kv_row("Fingerprint")
        facts.append(self._fact_socks[0])
        facts.append(self._fact_hops[0])
        facts.append(self._fact_fp[0])
        body.append(facts)

        # Quick actions
        body.append(self._sec("Quick actions"))
        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_max_children_per_line(3)
        actions.set_min_children_per_line(1)
        actions.set_homogeneous(True)
        actions.set_column_spacing(8)
        actions.set_row_spacing(8)
        actions.add_css_class("handset-action-grid")
        for label, cb in (
            ("Copy SOCKS", self._copy_socks),
            ("Run egress", lambda *_: self._run_probes()),
            ("Open export", self._open_export),
            ("Save listener", lambda *_: self._save_listener(quiet=False)),
            ("Export playbook", self._export_playbook),
            ("Marketplace", lambda *_: self._go_marketplace()),
        ):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            cell = Gtk.FlowBoxChild()
            cell.set_child(b)
            actions.append(cell)
        body.append(actions)

        scroll.set_child(body)
        return scroll

    # ── Listener panel ────────────────────────────────────────────

    def _build_listener(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("handset-panel")

        intro = Gtk.Label(
            label=(
                "Document your accept / reverse face. These notes feed the "
                "playbook export and stay on this machine under plugin data."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("handset-muted")
        body.append(intro)

        meta = self._load_meta()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("handset-card")

        self._accept_host = Gtk.Entry()
        self._accept_host.set_placeholder_text("accept.example.net or IP")
        self._accept_host.set_text(str(meta.get("accept_host") or ""))
        card.append(self._field("Accept host", self._accept_host))

        port_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._accept_port = Gtk.Entry()
        self._accept_port.set_placeholder_text("18443")
        self._accept_port.set_text(str(meta.get("accept_port") or "18443"))
        self._accept_port.set_hexpand(True)
        port_row.append(self._field("Port", self._accept_port))
        self._proto = Gtk.DropDown.new_from_strings(
            ["TCP", "TLS / 443 face", "PRR / Mirage", "Other"]
        )
        proto = str(meta.get("proto") or "TLS / 443 face")
        try:
            idx = ["TCP", "TLS / 443 face", "PRR / Mirage", "Other"].index(proto)
        except ValueError:
            idx = 1
        self._proto.set_selected(idx)
        self._proto.set_hexpand(True)
        port_row.append(self._field("Transport", self._proto))
        card.append(port_row)

        self._face = Gtk.Entry()
        self._face.set_placeholder_text("SNI / cover personality / REALITY dest")
        self._face.set_text(str(meta.get("face") or ""))
        card.append(self._field("Cover face", self._face))

        self._agent_id = Gtk.Entry()
        self._agent_id.set_placeholder_text("peer-1 · lab-vm · travel")
        self._agent_id.set_text(str(meta.get("agent_id") or ""))
        card.append(self._field("Agent / foothold id", self._agent_id))

        self._notes = Gtk.TextView()
        self._notes.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._notes.set_top_margin(8)
        self._notes.set_bottom_margin(8)
        self._notes.set_left_margin(10)
        self._notes.set_right_margin(10)
        self._notes.set_size_request(-1, 100)
        self._notes.get_buffer().set_text(str(meta.get("notes") or ""))
        card.append(self._field("Ops notes", self._notes))

        body.append(card)

        # Presets
        body.append(self._sec("Quick fill"))
        presets = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for name, host, port, face in (
            ("Lab accept", "127.0.0.1", "18443", "localhost PRR"),
            ("Cloud 443", "", "443", "TLS face / CDN"),
            ("Clear", "", "18443", ""),
        ):
            b = Gtk.Button(label=name)
            b.add_css_class("flat")
            b.connect(
                "clicked",
                lambda *_a, h=host, p=port, f=face: self._apply_preset(h, p, f),
            )
            presets.append(b)
        body.append(presets)

        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_row.set_margin_top(4)
        save_b = Gtk.Button(label="Save listener")
        save_b.add_css_class("suggested-action")
        save_b.connect("clicked", lambda *_: self._save_listener(quiet=False))
        save_row.append(save_b)
        copy_b = Gtk.Button(label="Copy listener line")
        copy_b.add_css_class("flat")
        copy_b.connect("clicked", self._copy_listener_line)
        save_row.append(copy_b)
        body.append(save_row)

        scroll.set_child(body)
        return scroll

    # ── Egress panel ──────────────────────────────────────────────

    def _build_egress(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("handset-panel")

        intro = Gtk.Label(
            label=(
                "Probe which callback classes answer from this host — direct "
                "clearnet vs through the live Spectre SOCKS path."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("handset-muted")
        body.append(intro)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._probe_btn = Gtk.Button(label="Run egress matrix")
        self._probe_btn.add_css_class("suggested-action")
        self._probe_btn.connect("clicked", lambda *_: self._run_probes())
        bar.append(self._probe_btn)
        self._probe_status = Gtk.Label(label="", xalign=0)
        self._probe_status.add_css_class("handset-muted")
        self._probe_status.set_hexpand(True)
        bar.append(self._probe_status)
        body.append(bar)

        self._probe_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._probe_box.add_css_class("handset-card")
        empty = Gtk.Label(
            label="No results yet — run the matrix to compare direct vs path.",
            xalign=0,
            wrap=True,
        )
        empty.add_css_class("handset-muted")
        empty.set_margin_top(4)
        empty.set_margin_bottom(4)
        self._probe_empty = empty
        self._probe_box.append(empty)
        body.append(self._probe_box)

        # Custom target
        body.append(self._sec("Custom target"))
        custom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._custom_host = Gtk.Entry()
        self._custom_host.set_placeholder_text("host")
        self._custom_host.set_hexpand(True)
        custom.append(self._custom_host)
        self._custom_port = Gtk.Entry()
        self._custom_port.set_placeholder_text("443")
        self._custom_port.set_width_chars(6)
        self._custom_port.set_text("443")
        custom.append(self._custom_port)
        cust_b = Gtk.Button(label="Probe")
        cust_b.add_css_class("flat")
        cust_b.connect("clicked", self._probe_custom)
        custom.append(cust_b)
        body.append(custom)

        scroll.set_child(body)
        # restore last probe UI if any
        meta = self._load_meta()
        last = meta.get("last_probe_rows")
        if isinstance(last, list) and last:
            self._render_probe_rows(last, ts=str(meta.get("last_probe_ts") or ""))
        return scroll

    # ── Ops kit ───────────────────────────────────────────────────

    def _build_ops(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("handset-panel")

        body.append(self._sec("Agent package"))
        card1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card1.add_css_class("handset-card")
        c1t = Gtk.Label(
            label=(
                "Open Reach’s reverse export folder for Inverse Snowflake / "
                "dial-out agent packages and peer drops."
            ),
            wrap=True,
            xalign=0,
        )
        c1t.add_css_class("handset-muted")
        card1.append(c1t)
        open_exp = Gtk.Button(label="Open export folder")
        open_exp.add_css_class("suggested-action")
        open_exp.set_halign(Gtk.Align.START)
        open_exp.connect("clicked", self._open_export)
        card1.append(open_exp)
        body.append(card1)

        body.append(self._sec("Playbook"))
        card2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card2.add_css_class("handset-card")
        c2t = Gtk.Label(
            label=(
                "Export a JSON snapshot: listener notes, path state, and last "
                "egress results — useful for after-action or handoff notes."
            ),
            wrap=True,
            xalign=0,
        )
        c2t.add_css_class("handset-muted")
        card2.append(c2t)
        exp = Gtk.Button(label="Export playbook JSON")
        exp.add_css_class("suggested-action")
        exp.set_halign(Gtk.Align.START)
        exp.connect("clicked", self._export_playbook)
        card2.append(exp)
        self._playbook_path = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self._playbook_path.add_css_class("handset-kv-val")
        card2.append(self._playbook_path)
        body.append(card2)

        body.append(self._sec("Data"))
        card3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card3.add_css_class("handset-card")
        self._data_lab = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self._data_lab.add_css_class("handset-kv-val")
        self._data_lab.set_text(str(self._ctx.data_path()))
        card3.append(self._data_lab)
        open_data = Gtk.Button(label="Open plugin data folder")
        open_data.add_css_class("flat")
        open_data.set_halign(Gtk.Align.START)
        open_data.connect("clicked", self._open_data)
        card3.append(open_data)
        body.append(card3)

        scroll.set_child(body)
        return scroll

    # ── Log panel ─────────────────────────────────────────────────

    def _build_log(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.add_css_class("handset-panel")
        body.set_hexpand(True)
        body.set_vexpand(True)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lab = Gtk.Label(label="Session log", xalign=0)
        lab.add_css_class("handset-section")
        lab.set_hexpand(True)
        bar.append(lab)
        clr = Gtk.Button(label="Clear")
        clr.add_css_class("flat")
        clr.connect("clicked", self._clear_log)
        bar.append(clr)
        body.append(bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_min_content_height(240)
        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.set_top_margin(10)
        self._log_view.set_bottom_margin(10)
        self._log_view.set_left_margin(12)
        self._log_view.set_right_margin(12)
        self._log_view.add_css_class("handset-log")
        scroll.set_child(self._log_view)
        body.append(scroll)
        return body

    # ── Helpers UI ────────────────────────────────────────────────

    def _sec(self, title: str) -> Gtk.Widget:
        lab = Gtk.Label(label=title, xalign=0)
        lab.add_css_class("handset-section")
        lab.set_margin_top(4)
        return lab

    def _field(self, title: str, child: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        lab = Gtk.Label(label=title, xalign=0)
        lab.add_css_class("handset-field-label")
        box.append(lab)
        box.append(child)
        return box

    def _kv_row(self, key: str) -> tuple[Gtk.Widget, Gtk.Label]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        k = Gtk.Label(label=key, xalign=0)
        k.add_css_class("handset-kv-key")
        k.set_size_request(88, -1)
        row.append(k)
        v = Gtk.Label(label="—", xalign=0, wrap=True, selectable=True)
        v.add_css_class("handset-kv-val")
        v.set_hexpand(True)
        row.append(v)
        return row, v

    # ── Persistence ───────────────────────────────────────────────

    def _meta_path(self) -> Path:
        return self._ctx.data_path("handset.json")

    def _load_meta(self) -> dict:
        p = self._meta_path()
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_meta(self, data: dict) -> None:
        p = self._meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _notes_text(self) -> str:
        buf = self._notes.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _proto_str(self) -> str:
        strings = ["TCP", "TLS / 443 face", "PRR / Mirage", "Other"]
        i = int(self._proto.get_selected())
        return strings[max(0, min(i, len(strings) - 1))]

    # ── Status ────────────────────────────────────────────────────

    def _refresh_all(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        state = "offline"
        state_label = "Core offline"
        path = "—"
        socks = "—"
        hops = "—"
        fp = "—"
        try:
            st = self._ctx.services.core.status(force=True)
            sv = getattr(getattr(st, "state", None), "value", str(getattr(st, "state", "")))
            path = str(getattr(st, "path_summary", None) or "—")
            socks = str(getattr(st, "local_proxy", None) or "—").strip() or "—"
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

        self._state_lab.set_text(state_label)
        self._path_lab.set_text(path)
        self._fact_socks[1].set_text(socks)
        self._fact_hops[1].set_text(hops)
        self._fact_fp[1].set_text(fp if len(fp) < 120 else fp[:117] + "…")

        for cls in (
            "handset-dot-live",
            "handset-dot-idle",
            "handset-dot-busy",
            "handset-dot-off",
        ):
            self._dot.remove_css_class(cls)
        self._dot.add_css_class(
            {
                "live": "handset-dot-live",
                "idle": "handset-dot-idle",
                "busy": "handset-dot-busy",
                "off": "handset-dot-off",
            }.get(state, "handset-dot-idle")
        )

        self._chip.set_text(state_label.upper())
        for c in ("handset-chip-live",):
            self._chip.remove_css_class(c)
        if state == "live":
            self._chip.add_css_class("handset-chip-live")

    # ── Listener actions ──────────────────────────────────────────

    def _apply_preset(self, host: str, port: str, face: str) -> None:
        if host or host == "":
            self._accept_host.set_text(host)
        self._accept_port.set_text(port)
        self._face.set_text(face)
        self._log(f"Preset applied port={port}")

    def _save_listener(self, *, quiet: bool = False) -> None:
        data = self._load_meta()
        data.update(
            {
                "accept_host": self._accept_host.get_text().strip(),
                "accept_port": self._accept_port.get_text().strip(),
                "face": self._face.get_text().strip(),
                "agent_id": self._agent_id.get_text().strip(),
                "proto": self._proto_str(),
                "notes": self._notes_text().strip(),
                "updated": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._save_meta(data)
        self._log("Listener notes saved")
        if not quiet and self._ctx.toast:
            self._ctx.toast("Listener saved")

    def _copy_listener_line(self, *_a) -> None:
        host = self._accept_host.get_text().strip() or "?"
        port = self._accept_port.get_text().strip() or "?"
        face = self._face.get_text().strip() or "—"
        line = f"{host}:{port} · {self._proto_str()} · {face}"
        self._clipboard_set(line)
        self._log(f"Copied listener: {line}")
        if self._ctx.toast:
            self._ctx.toast("Listener line copied")

    # ── Egress ────────────────────────────────────────────────────

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
        self._probe_btn.set_sensitive(False)
        self._probe_status.set_text("Probing…")
        targets = self._targets()
        socks = self._socks_tuple()

        def work() -> None:
            rows: list[dict[str, Any]] = []
            for host, port, label in targets:
                d_ok, d_ms, d_err = _tcp_probe(host, port, timeout=3.0)
                row: dict[str, Any] = {
                    "label": label,
                    "host": host,
                    "port": port,
                    "direct_ok": d_ok,
                    "direct_ms": round(d_ms, 1),
                    "direct_err": d_err,
                }
                if socks:
                    s_ok, s_ms, s_err = _socks_tcp_probe(
                        socks[0], socks[1], host, port, timeout=8.0
                    )
                    row["path_ok"] = s_ok
                    row["path_ms"] = round(s_ms, 1)
                    row["path_err"] = s_err
                rows.append(row)

            ts = datetime.now(timezone.utc).isoformat()
            try:
                meta = self._load_meta()
                meta["last_probe_rows"] = rows
                meta["last_probe_ts"] = ts
                meta["last_probe_socks"] = (
                    f"{socks[0]}:{socks[1]}" if socks else None
                )
                self._save_meta(meta)
            except Exception:
                pass

            def done() -> bool:
                self._probe_busy = False
                self._probe_btn.set_sensitive(True)
                self._render_probe_rows(rows, ts=ts)
                self._log(f"Egress matrix complete ({len(rows)} targets)")
                if self._ctx.toast:
                    self._ctx.toast("Egress matrix complete")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="handset-probe", daemon=True).start()

    def _probe_custom(self, *_a) -> None:
        host = self._custom_host.get_text().strip()
        try:
            port = int(self._custom_port.get_text().strip() or "443")
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
        self._probe_status.set_text(f"Probing {host}:{port}…")
        socks = self._socks_tuple()

        def work() -> None:
            d_ok, d_ms, d_err = _tcp_probe(host, port, timeout=4.0)
            row: dict[str, Any] = {
                "label": "Custom",
                "host": host,
                "port": port,
                "direct_ok": d_ok,
                "direct_ms": round(d_ms, 1),
                "direct_err": d_err,
            }
            if socks:
                s_ok, s_ms, s_err = _socks_tcp_probe(
                    socks[0], socks[1], host, port, timeout=10.0
                )
                row["path_ok"] = s_ok
                row["path_ms"] = round(s_ms, 1)
                row["path_err"] = s_err
            rows = [row]
            # prepend to existing visual list
            meta = self._load_meta()
            prev = meta.get("last_probe_rows")
            if isinstance(prev, list):
                rows = [row] + [
                    r for r in prev if not (
                        r.get("host") == host and r.get("port") == port
                    )
                ][:12]
            ts = datetime.now(timezone.utc).isoformat()
            meta["last_probe_rows"] = rows
            meta["last_probe_ts"] = ts
            self._save_meta(meta)

            def done() -> bool:
                self._probe_busy = False
                self._render_probe_rows(rows, ts=ts)
                self._log(f"Custom probe {host}:{port}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="handset-custom", daemon=True).start()

    def _render_probe_rows(self, rows: list[dict], *, ts: str = "") -> None:
        while child := self._probe_box.get_first_child():
            self._probe_box.remove(child)
        if ts:
            self._probe_status.set_text(f"Last run · {ts[:19].replace('T', ' ')}Z")
        if not rows:
            self._probe_box.append(self._probe_empty)
            return
        for row in rows:
            self._probe_box.append(self._probe_row_widget(row))

    def _probe_row_widget(self, row: dict) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("handset-probe-row")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lab = Gtk.Label(label=str(row.get("label") or "Probe"), xalign=0)
        lab.add_css_class("handset-probe-label")
        lab.set_hexpand(True)
        top.append(lab)
        tgt = Gtk.Label(
            label=f"{row.get('host')}:{row.get('port')}",
            xalign=1,
        )
        tgt.add_css_class("handset-probe-target")
        top.append(tgt)
        box.append(top)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        d_ok = bool(row.get("direct_ok"))
        d_lab = Gtk.Label(
            label=f"direct  {'OK' if d_ok else 'FAIL'}  {row.get('direct_ms', 0):.0f}ms",
            xalign=0,
        )
        d_lab.add_css_class("handset-ok" if d_ok else "handset-fail")
        stats.append(d_lab)
        if "path_ok" in row:
            p_ok = bool(row.get("path_ok"))
            p_lab = Gtk.Label(
                label=f"path  {'OK' if p_ok else 'FAIL'}  {row.get('path_ms', 0):.0f}ms",
                xalign=0,
            )
            p_lab.add_css_class("handset-ok" if p_ok else "handset-fail")
            stats.append(p_lab)
        else:
            n = Gtk.Label(label="path  —  (no SOCKS)", xalign=0)
            n.add_css_class("handset-muted")
            stats.append(n)
        box.append(stats)
        return box

    # ── Ops actions ───────────────────────────────────────────────

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
        self._log(f"Copied SOCKS {proxy}")
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
        self._log(f"Opened export {path}")

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

    def _export_playbook(self, *_a) -> None:
        self._save_listener(quiet=True)
        meta = self._load_meta()
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

        playbook = {
            "handset_version": self._ctx.manifest.version,
            "exported": datetime.now(timezone.utc).isoformat(),
            "listener": {
                "accept_host": meta.get("accept_host"),
                "accept_port": meta.get("accept_port"),
                "face": meta.get("face"),
                "agent_id": meta.get("agent_id"),
                "proto": meta.get("proto"),
            },
            "notes": meta.get("notes"),
            "path": path_info,
            "last_probe": {
                "ts": meta.get("last_probe_ts"),
                "socks": meta.get("last_probe_socks"),
                "rows": meta.get("last_probe_rows"),
            },
        }
        out = self._ctx.data_path(
            f"playbook-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        out.write_text(json.dumps(playbook, indent=2) + "\n", encoding="utf-8")
        self._playbook_path.set_text(str(out))
        self._log(f"Playbook → {out.name}")
        if self._ctx.toast:
            self._ctx.toast(f"Playbook → {out.name}")

    # ── Log ───────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append(f"[{ts}] {msg}")
        self._log_lines = self._log_lines[-200:]
        if getattr(self, "_log_view", None) is not None:
            self._log_view.get_buffer().set_text("\n".join(self._log_lines))

    def _clear_log(self, *_a) -> None:
        self._log_lines.clear()
        if getattr(self, "_log_view", None) is not None:
            self._log_view.get_buffer().set_text("")


def _tcp_probe(host: str, port: int, *, timeout: float) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - t0) * 1000.0, ""
    except OSError as exc:
        return False, (time.perf_counter() - t0) * 1000.0, str(exc)


def _socks_tcp_probe(
    socks_host: str,
    socks_port: int,
    host: str,
    port: int,
    *,
    timeout: float,
) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((socks_host, socks_port), timeout=timeout)
        try:
            sock.settimeout(timeout)
            sock.sendall(b"\x05\x01\x00")
            resp = sock.recv(2)
            if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
                return False, (time.perf_counter() - t0) * 1000.0, "socks auth"
            host_b = host.encode("idna")
            if len(host_b) > 255:
                return False, (time.perf_counter() - t0) * 1000.0, "hostname"
            req = (
                b"\x05\x01\x00\x03"
                + bytes([len(host_b)])
                + host_b
                + struct.pack("!H", port)
            )
            sock.sendall(req)
            hdr = sock.recv(4)
            if len(hdr) < 4 or hdr[1] != 0:
                code = hdr[1] if len(hdr) > 1 else -1
                return False, (time.perf_counter() - t0) * 1000.0, f"socks {code}"
            atyp = hdr[3]
            if atyp == 1:
                sock.recv(4 + 2)
            elif atyp == 3:
                ln = sock.recv(1)
                if ln:
                    sock.recv(ln[0] + 2)
            elif atyp == 4:
                sock.recv(16 + 2)
            return True, (time.perf_counter() - t0) * 1000.0, ""
        finally:
            sock.close()
    except OSError as exc:
        return False, (time.perf_counter() - t0) * 1000.0, str(exc)
