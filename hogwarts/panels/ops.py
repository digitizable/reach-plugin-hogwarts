"""Ops kit — export folder, playbook schema fields, data dir."""

from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from hogwarts.widgets import field, scroll_panel, section_label

# Research playbook v1 — no tokens/secrets
_PLAYBOOK_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "Name", "Eng short name"),
    ("objective", "Objective", "What success looks like"),
    ("operator", "Operator", "Who is on keyboard"),
    ("channel_class", "Channel class", "forward | reverse | path-wrapped | human-only"),
    ("profile", "Agent profile", "lab-plain / edge face name"),
    ("agent_egress", "Agent egress", "direct | system-proxy | socks …"),
    ("path_notes", "Path notes", "Reach path / hops summary (no secrets)"),
    ("plane_url", "Plane URL", "Keep host only — never paste tokens"),
    ("listener_notes", "Listener notes", "Accept host:port · cover · state"),
    ("listener_evidence", "Listener evidence", "none | tcp_ok | process_ok | plane_managed"),
    ("sleep_budget_sec", "Sleep budget (sec)", "Expected agent sleep"),
    ("jitter", "Jitter", "0.0–1.0"),
    ("burn_plan", "Burn plan", "Rotate token / face / laptop steps"),
)

_DRILLS = (
    ("D1", "Keep heartbeat"),
    ("D2", "Road check"),
    ("D3", "Inventory honesty"),
    ("D4", "Event pipeline"),
    ("D5", "Task round-trip"),
    ("D6", "Token burn"),
    ("D7", "Battlement probe"),
    ("D8", "Desk scrape"),
    ("D9", "Compartment tabletop"),
)


class OpsPanel(Gtk.Box):
    def __init__(
        self,
        data_dir: str,
        *,
        on_open_export: Callable,
        on_export_playbook: Callable,
        on_open_data: Callable,
        on_save_playbook: Callable[[], None] | None = None,
        on_export_agent: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._on_save_playbook = on_save_playbook
        self._on_export_agent = on_export_agent
        self._entries: dict[str, Gtk.Entry | Gtk.TextView] = {}
        self._drill_labs: dict[str, Gtk.Label] = {}

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("hogwarts-panel")

        body.append(section_label("Agent package"))
        card1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card1.add_css_class("hogwarts-card")
        c1t = Gtk.Label(
            label=(
                "Build a lab agent zip (agent.py + agent.json with one-shot enroll "
                "secret + Linux/Windows runners). Also open Reach’s reverse export "
                "folder for Inverse Snowflake / dial-out packages."
            ),
            wrap=True,
            xalign=0,
        )
        c1t.add_css_class("hogwarts-muted")
        card1.append(c1t)
        pack_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        exp_agent = Gtk.Button(label="Export agent zip")
        exp_agent.add_css_class("suggested-action")
        exp_agent.set_tooltip_text("Mints enroll secret from plane + packs agent.py")
        exp_agent.connect(
            "clicked",
            lambda *_: self._on_export_agent() if self._on_export_agent else None,
        )
        pack_row.append(exp_agent)
        open_exp = Gtk.Button(label="Open reverse export folder")
        open_exp.add_css_class("flat")
        open_exp.connect("clicked", on_open_export)
        pack_row.append(open_exp)
        card1.append(pack_row)
        self.export_status = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self.export_status.add_css_class("hogwarts-kv-val")
        card1.append(self.export_status)
        body.append(card1)

        body.append(section_label("Playbook fields (no secrets)"))
        card2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card2.add_css_class("hogwarts-card")
        c2t = Gtk.Label(
            label=(
                "Eng handoff schema from research/ops. Stored locally under plugin "
                "data — never paste plane tokens or enroll secrets here."
            ),
            wrap=True,
            xalign=0,
        )
        c2t.add_css_class("hogwarts-muted")
        card2.append(c2t)

        for key, title, hint in _PLAYBOOK_FIELDS:
            if key in ("objective", "burn_plan", "path_notes", "listener_notes"):
                tv = Gtk.TextView()
                tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                tv.set_top_margin(4)
                tv.set_bottom_margin(4)
                tv.set_left_margin(6)
                tv.set_right_margin(6)
                tv.set_size_request(-1, 56)
                frame = Gtk.Frame()
                frame.set_child(tv)
                card2.append(field(title, frame))
                # store placeholder via tooltip
                tv.set_tooltip_text(hint)
                self._entries[key] = tv
            else:
                ent = Gtk.Entry()
                ent.set_placeholder_text(hint)
                ent.set_hexpand(True)
                card2.append(field(title, ent))
                self._entries[key] = ent

        body.append(section_label("Purple drills"))
        drills = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        drills.add_css_class("hogwarts-card")
        for did, name in _DRILLS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn = Gtk.Button(label=did)
            btn.add_css_class("flat")
            btn.set_tooltip_text(f"Stamp {did} · {name} as done now")
            btn.connect("clicked", lambda _b, d=did: self._stamp_drill(d))
            row.append(btn)
            lab = Gtk.Label(label=f"{name} — never", xalign=0)
            lab.add_css_class("hogwarts-agent-meta")
            lab.set_hexpand(True)
            row.append(lab)
            self._drill_labs[did] = lab
            drills.append(row)
        card2.append(drills)

        act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save = Gtk.Button(label="Save playbook fields")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._emit_save())
        act.append(save)
        exp = Gtk.Button(label="Export playbook JSON")
        exp.add_css_class("flat")
        exp.connect("clicked", on_export_playbook)
        act.append(exp)
        card2.append(act)
        self.playbook_path = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self.playbook_path.add_css_class("hogwarts-kv-val")
        card2.append(self.playbook_path)
        self.save_status = Gtk.Label(label="", xalign=0)
        self.save_status.add_css_class("hogwarts-muted")
        card2.append(self.save_status)
        body.append(card2)

        body.append(section_label("Data"))
        card3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card3.add_css_class("hogwarts-card")
        self.data_lab = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.data_lab.add_css_class("hogwarts-kv-val")
        self.data_lab.set_text(data_dir)
        card3.append(self.data_lab)
        open_data = Gtk.Button(label="Open plugin data folder")
        open_data.add_css_class("flat")
        open_data.set_halign(Gtk.Align.START)
        open_data.connect("clicked", on_open_data)
        card3.append(open_data)
        body.append(card3)

        self.append(scroll_panel(body))

    def _emit_save(self) -> None:
        if self._on_save_playbook:
            self._on_save_playbook()

    def _stamp_drill(self, drill_id: str) -> None:
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        lab = self._drill_labs.get(drill_id)
        if lab is not None:
            name = next((n for d, n in _DRILLS if d == drill_id), drill_id)
            lab.set_text(f"{name} — {ts}")
        self._emit_save()

    def load_playbook(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        for key, widget in self._entries.items():
            val = str(data.get(key) or "")
            if isinstance(widget, Gtk.TextView):
                widget.get_buffer().set_text(val)
            else:
                widget.set_text(val)
        drills = data.get("drill_last") if isinstance(data.get("drill_last"), dict) else {}
        for did, name in _DRILLS:
            ts = str(drills.get(did) or "")
            lab = self._drill_labs.get(did)
            if lab is not None:
                lab.set_text(f"{name} — {ts}" if ts else f"{name} — never")

    def snapshot_playbook(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, widget in self._entries.items():
            if isinstance(widget, Gtk.TextView):
                buf = widget.get_buffer()
                start, end = buf.get_start_iter(), buf.get_end_iter()
                out[key] = buf.get_text(start, end, True).strip()
            else:
                out[key] = widget.get_text().strip()
        drills: dict[str, str] = {}
        for did, name in _DRILLS:
            lab = self._drill_labs.get(did)
            text = lab.get_text() if lab else ""
            # "Name — ts" or "Name — never"
            if " — " in text:
                ts = text.split(" — ", 1)[1].strip()
                if ts and ts != "never":
                    drills[did] = ts
        out["drill_last"] = drills
        return out

    def set_save_status(self, msg: str, *, ok: bool | None = None) -> None:
        self.save_status.set_text(msg)
        self.save_status.remove_css_class("hogwarts-ok")
        self.save_status.remove_css_class("hogwarts-fail")
        if ok is True:
            self.save_status.add_css_class("hogwarts-ok")
        elif ok is False:
            self.save_status.add_css_class("hogwarts-fail")

    def set_export_status(self, msg: str, *, ok: bool | None = None) -> None:
        self.export_status.set_text(msg)
        self.export_status.remove_css_class("hogwarts-ok")
        self.export_status.remove_css_class("hogwarts-fail")
        if ok is True:
            self.export_status.add_css_class("hogwarts-ok")
        elif ok is False:
            self.export_status.add_css_class("hogwarts-fail")
