"""Listener battlements — local multi-listener CRUD with evidence gating."""

from __future__ import annotations

import uuid
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from hogwarts.widgets import field, scroll_panel, section_label

_PROTOS = ["TCP", "TLS / 443 face", "PRR / Mirage", "Other"]
_STATES = ["planned", "deployed", "disabled", "burned"]
_EVIDENCE = ["none", "tcp_ok", "process_ok", "plane_managed", "unknown"]


def _new_id() -> str:
    return f"lst_{uuid.uuid4().hex[:10]}"


def _is_green(state: str, evidence: str) -> bool:
    return state == "deployed" and evidence in (
        "tcp_ok",
        "process_ok",
        "plane_managed",
    )


class ListenerPanel(Gtk.Box):
    """CRUD list of listener faces — honest green only with evidence."""

    def __init__(
        self,
        meta: dict[str, Any],
        *,
        on_save: Callable,
        on_copy: Callable,
        on_probe: Callable[[dict[str, Any]], None] | None = None,
        on_plane_pull: Callable[[], None] | None = None,
        on_plane_push: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._on_save = on_save
        self._on_copy = on_copy
        self._on_probe = on_probe
        self._on_plane_pull = on_plane_pull
        self._on_plane_push = on_plane_push
        self._listeners: list[dict[str, Any]] = []
        self._selected_id: str | None = None

        # Migrate legacy single-listener meta → list
        raw = meta.get("listeners")
        if isinstance(raw, list) and raw:
            self._listeners = [dict(x) for x in raw if isinstance(x, dict)]
        elif meta.get("accept_host") or meta.get("accept_port"):
            self._listeners = [
                {
                    "id": _new_id(),
                    "name": "primary",
                    "accept_host": str(meta.get("accept_host") or ""),
                    "accept_port": str(meta.get("accept_port") or "18443"),
                    "proto": str(meta.get("proto") or "TLS / 443 face"),
                    "face": str(meta.get("face") or ""),
                    "agent_id": str(meta.get("agent_id") or ""),
                    "state": "planned",
                    "evidence": "none",
                    "notes": str(meta.get("notes") or ""),
                }
            ]

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("hogwarts-panel")

        intro = Gtk.Label(
            label=(
                "Battlements: accept / reverse faces. Green LED only when "
                "state=deployed and evidence is tcp_ok / process_ok / plane_managed. "
                "Local plugin data + optional plane sync (GET/PUT /api/v1/listeners)."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("hogwarts-muted")
        body.append(intro)

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        split.set_hexpand(True)

        # List
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(220, -1)
        left.append(section_label("Listeners"))
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.append(self.list_box)
        list_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_b = Gtk.Button(label="Add")
        add_b.add_css_class("suggested-action")
        add_b.connect("clicked", lambda *_: self._add_new())
        list_btns.append(add_b)
        del_b = Gtk.Button(label="Delete")
        del_b.add_css_class("flat")
        del_b.connect("clicked", lambda *_: self._delete_selected())
        list_btns.append(del_b)
        left.append(list_btns)
        split.append(left)

        # Editor
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_hexpand(True)
        right.append(section_label("Edit"))
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("hogwarts-card")

        self.name = Gtk.Entry()
        self.name.set_placeholder_text("edge-1 · lab-accept")
        card.append(field("Name", self.name))

        self.accept_host = Gtk.Entry()
        self.accept_host.set_placeholder_text("accept.example.net or IP")
        card.append(field("Accept host", self.accept_host))

        port_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.accept_port = Gtk.Entry()
        self.accept_port.set_placeholder_text("18443")
        self.accept_port.set_hexpand(True)
        port_row.append(field("Port", self.accept_port))
        self.proto = Gtk.DropDown.new_from_strings(_PROTOS)
        self.proto.set_hexpand(True)
        port_row.append(field("Transport", self.proto))
        card.append(port_row)

        self.face = Gtk.Entry()
        self.face.set_placeholder_text("SNI / cover personality")
        card.append(field("Cover face", self.face))

        self.agent_id = Gtk.Entry()
        self.agent_id.set_placeholder_text("linked foothold id (optional)")
        card.append(field("Agent / foothold id", self.agent_id))

        st_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.state = Gtk.DropDown.new_from_strings(_STATES)
        self.state.set_hexpand(True)
        st_row.append(field("State", self.state))
        self.evidence = Gtk.DropDown.new_from_strings(_EVIDENCE)
        self.evidence.set_hexpand(True)
        st_row.append(field("Evidence", self.evidence))
        card.append(st_row)

        self.led = Gtk.Label(label="LED · off", xalign=0)
        self.led.add_css_class("hogwarts-muted")
        card.append(self.led)

        self.notes = Gtk.TextView()
        self.notes.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes.set_top_margin(8)
        self.notes.set_bottom_margin(8)
        self.notes.set_left_margin(10)
        self.notes.set_right_margin(10)
        self.notes.set_size_request(-1, 80)
        card.append(field("Ops notes", self.notes))
        right.append(card)

        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_b = Gtk.Button(label="Save listener")
        save_b.add_css_class("suggested-action")
        save_b.connect("clicked", lambda *_: self._save_editor())
        save_row.append(save_b)
        copy_b = Gtk.Button(label="Copy line")
        copy_b.add_css_class("flat")
        copy_b.connect("clicked", self._copy)
        save_row.append(copy_b)
        probe_b = Gtk.Button(label="Probe TCP")
        probe_b.add_css_class("flat")
        probe_b.set_tooltip_text("TCP connect from this desk; sets evidence tcp_ok/none")
        probe_b.connect("clicked", lambda *_: self._probe())
        save_row.append(probe_b)
        right.append(save_row)

        plane_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pull_b = Gtk.Button(label="Pull from plane")
        pull_b.add_css_class("flat")
        pull_b.set_tooltip_text("Replace local list with GET /api/v1/listeners")
        pull_b.connect(
            "clicked",
            lambda *_: self._on_plane_pull() if self._on_plane_pull else None,
        )
        plane_row.append(pull_b)
        push_b = Gtk.Button(label="Push to plane")
        push_b.add_css_class("flat")
        push_b.set_tooltip_text("Upsert all local listeners to the plane")
        push_b.connect(
            "clicked",
            lambda *_: self._on_plane_push() if self._on_plane_push else None,
        )
        plane_row.append(push_b)
        right.append(plane_row)

        self.status = Gtk.Label(label="", xalign=0, wrap=True)
        self.status.add_css_class("hogwarts-muted")
        right.append(self.status)

        split.append(right)
        body.append(split)
        self.append(scroll_panel(body))

        if self._listeners:
            self._select(self._listeners[0]["id"])
        else:
            self._rebuild_list()
            self._clear_editor()

    def _rebuild_list(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        if not self._listeners:
            empty = Gtk.Label(label="No listeners — Add one.", xalign=0)
            empty.add_css_class("hogwarts-muted")
            self.list_box.append(empty)
            return
        for lst in self._listeners:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_hexpand(True)
            green = _is_green(
                str(lst.get("state") or ""), str(lst.get("evidence") or "")
            )
            title = str(lst.get("name") or lst.get("id") or "?")
            host = str(lst.get("accept_host") or "?")
            port = str(lst.get("accept_port") or "?")
            led = "●" if green else "○"
            lab = Gtk.Label(
                label=f"{led} {title}\n{host}:{port} · {lst.get('state')}",
                xalign=0,
            )
            lab.add_css_class("hogwarts-agent-meta")
            if green:
                lab.add_css_class("hogwarts-ok")
            btn.set_child(lab)
            lid = str(lst.get("id") or "")
            btn.connect("clicked", lambda *_a, i=lid: self._select(i))
            self.list_box.append(btn)

    def _clear_editor(self) -> None:
        self._selected_id = None
        self.name.set_text("")
        self.accept_host.set_text("")
        self.accept_port.set_text("18443")
        self.proto.set_selected(1)
        self.face.set_text("")
        self.agent_id.set_text("")
        self.state.set_selected(0)
        self.evidence.set_selected(0)
        self.notes.get_buffer().set_text("")
        self._update_led()

    def _select(self, lid: str) -> None:
        self._flush_editor_to_model()
        lst = next((x for x in self._listeners if x.get("id") == lid), None)
        if not lst:
            return
        self._selected_id = lid
        self.name.set_text(str(lst.get("name") or ""))
        self.accept_host.set_text(str(lst.get("accept_host") or ""))
        self.accept_port.set_text(str(lst.get("accept_port") or "18443"))
        proto = str(lst.get("proto") or "TLS / 443 face")
        try:
            self.proto.set_selected(_PROTOS.index(proto))
        except ValueError:
            self.proto.set_selected(1)
        self.face.set_text(str(lst.get("face") or ""))
        self.agent_id.set_text(str(lst.get("agent_id") or ""))
        st = str(lst.get("state") or "planned")
        try:
            self.state.set_selected(_STATES.index(st))
        except ValueError:
            self.state.set_selected(0)
        ev = str(lst.get("evidence") or "none")
        try:
            self.evidence.set_selected(_EVIDENCE.index(ev))
        except ValueError:
            self.evidence.set_selected(0)
        self.notes.get_buffer().set_text(str(lst.get("notes") or ""))
        self._update_led()
        self._rebuild_list()

    def _update_led(self) -> None:
        st = _STATES[int(self.state.get_selected())]
        ev = _EVIDENCE[int(self.evidence.get_selected())]
        if _is_green(st, ev):
            self.led.set_text(f"LED · GREEN (deployed + {ev})")
            self.led.remove_css_class("hogwarts-muted")
            self.led.add_css_class("hogwarts-ok")
        else:
            self.led.set_text(f"LED · off ({st} / {ev})")
            self.led.remove_css_class("hogwarts-ok")
            self.led.add_css_class("hogwarts-muted")

    def _editor_snapshot(self) -> dict[str, Any]:
        buf = self.notes.get_buffer()
        notes = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        return {
            "id": self._selected_id or _new_id(),
            "name": self.name.get_text().strip() or "listener",
            "accept_host": self.accept_host.get_text().strip(),
            "accept_port": self.accept_port.get_text().strip() or "18443",
            "proto": _PROTOS[int(self.proto.get_selected())],
            "face": self.face.get_text().strip(),
            "agent_id": self.agent_id.get_text().strip(),
            "state": _STATES[int(self.state.get_selected())],
            "evidence": _EVIDENCE[int(self.evidence.get_selected())],
            "notes": notes.strip(),
        }

    def _flush_editor_to_model(self) -> None:
        if not self._selected_id:
            return
        snap = self._editor_snapshot()
        snap["id"] = self._selected_id
        for i, lst in enumerate(self._listeners):
            if lst.get("id") == self._selected_id:
                self._listeners[i] = snap
                break

    def _add_new(self) -> None:
        self._flush_editor_to_model()
        lid = _new_id()
        self._listeners.append(
            {
                "id": lid,
                "name": f"listener-{len(self._listeners)+1}",
                "accept_host": "127.0.0.1",
                "accept_port": "18443",
                "proto": "TLS / 443 face",
                "face": "",
                "agent_id": "",
                "state": "planned",
                "evidence": "none",
                "notes": "",
            }
        )
        self._select(lid)
        self.status.set_text("Added listener (Save to persist)")

    def _delete_selected(self) -> None:
        if not self._selected_id:
            return
        self._listeners = [
            x for x in self._listeners if x.get("id") != self._selected_id
        ]
        self._selected_id = None
        if self._listeners:
            self._select(str(self._listeners[0]["id"]))
        else:
            self._clear_editor()
            self._rebuild_list()
        self._on_save(quiet=False)
        self.status.set_text("Deleted")

    def _save_editor(self) -> None:
        if not self._selected_id:
            self._add_new()
        self._flush_editor_to_model()
        self._update_led()
        self._rebuild_list()
        self._on_save(quiet=False)
        self.status.set_text("Saved")

    def _copy(self, *_a) -> None:
        self._flush_editor_to_model()
        self._on_copy()

    def _probe(self) -> None:
        self._flush_editor_to_model()
        snap = self._editor_snapshot()
        if self._on_probe:
            self._on_probe(snap)
        else:
            self.status.set_text("Probe not wired")

    def replace_listeners(self, rows: list[dict[str, Any]]) -> None:
        """Replace local list (e.g. after plane pull)."""
        self._listeners = [dict(x) for x in rows if isinstance(x, dict)]
        for lst in self._listeners:
            if not lst.get("id"):
                lst["id"] = _new_id()
        if self._listeners:
            self._select(str(self._listeners[0]["id"]))
        else:
            self._clear_editor()
            self._rebuild_list()

    def set_probe_result(
        self, listener_id: str, *, ok: bool, detail: str = ""
    ) -> None:
        for i, lst in enumerate(self._listeners):
            if lst.get("id") == listener_id:
                lst["evidence"] = "tcp_ok" if ok else "none"
                if ok and lst.get("state") == "planned":
                    lst["state"] = "deployed"
                self._listeners[i] = lst
                if self._selected_id == listener_id:
                    self._select(listener_id)
                break
        self._rebuild_list()
        self.status.set_text(
            f"Probe {'ok' if ok else 'fail'}: {detail}" if detail else ("Probe ok" if ok else "Probe fail")
        )
        self._on_save(quiet=True)

    # ── Host compatibility (page.py) ──────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Persist list + legacy primary fields for older exports."""
        self._flush_editor_to_model()
        primary = self._listeners[0] if self._listeners else {}
        return {
            "listeners": list(self._listeners),
            "accept_host": str(primary.get("accept_host") or ""),
            "accept_port": str(primary.get("accept_port") or ""),
            "face": str(primary.get("face") or ""),
            "agent_id": str(primary.get("agent_id") or ""),
            "proto": str(primary.get("proto") or ""),
            "notes": str(primary.get("notes") or ""),
        }

    def listener_line(self) -> str:
        self._flush_editor_to_model()
        if self._selected_id:
            lst = next(
                (x for x in self._listeners if x.get("id") == self._selected_id),
                None,
            )
        else:
            lst = self._listeners[0] if self._listeners else None
        if not lst:
            return "(no listener)"
        host = str(lst.get("accept_host") or "?")
        port = str(lst.get("accept_port") or "?")
        face = str(lst.get("face") or "—")
        st = str(lst.get("state") or "?")
        ev = str(lst.get("evidence") or "?")
        led = "GREEN" if _is_green(st, ev) else "off"
        return f"{host}:{port} · {lst.get('proto')} · {face} · {st}/{ev} · LED {led}"
