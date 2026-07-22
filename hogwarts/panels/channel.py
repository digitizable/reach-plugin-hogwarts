"""Channel panel — path-aware C2 channel status."""

from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from hogwarts.widgets import kv_row, scroll_panel, section_label


class ChannelPanel(Gtk.Box):
    def __init__(
        self,
        *,
        on_copy_socks: Callable,
        on_run_egress: Callable,
        on_open_export: Callable,
        on_save_listener: Callable,
        on_export_playbook: Callable,
        on_marketplace: Callable,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("hogwarts-panel")

        banner = Gtk.Label(
            label=(
                "Command-and-control desk: reverse reachback, path-aware "
                "channel status, agent roster, egress matrix, and playbooks."
            ),
            wrap=True,
            xalign=0,
        )
        banner.add_css_class("hogwarts-banner")
        body.append(banner)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hero.add_css_class("hogwarts-hero")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.dot = Gtk.Box()
        self.dot.add_css_class("hogwarts-dot")
        self.dot.add_css_class("hogwarts-dot-idle")
        self.dot.set_valign(Gtk.Align.CENTER)
        top.append(self.dot)
        ht = Gtk.Label(label="Channel", xalign=0)
        ht.add_css_class("hogwarts-hero-title")
        ht.set_hexpand(True)
        top.append(ht)
        hero.append(top)

        self.state_lab = Gtk.Label(label="—", xalign=0)
        self.state_lab.add_css_class("hogwarts-hero-state")
        hero.append(self.state_lab)

        self.path_lab = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        self.path_lab.add_css_class("hogwarts-hero-meta")
        hero.append(self.path_lab)
        body.append(hero)

        body.append(section_label("Path facts"))
        facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        facts.add_css_class("hogwarts-card")
        self.fact_socks = kv_row("SOCKS")
        self.fact_hops = kv_row("Hops")
        self.fact_fp = kv_row("Fingerprint")
        self.fact_plane = kv_row("Plane")
        facts.append(self.fact_socks[0])
        facts.append(self.fact_hops[0])
        facts.append(self.fact_fp[0])
        facts.append(self.fact_plane[0])
        body.append(facts)

        body.append(section_label("Quick actions"))
        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_max_children_per_line(3)
        actions.set_min_children_per_line(1)
        actions.set_homogeneous(True)
        actions.set_column_spacing(8)
        actions.set_row_spacing(8)
        actions.add_css_class("hogwarts-action-grid")
        for label, cb in (
            ("Copy SOCKS", on_copy_socks),
            ("Run egress", on_run_egress),
            ("Open export", on_open_export),
            ("Save listener", on_save_listener),
            ("Export playbook", on_export_playbook),
            ("Marketplace", on_marketplace),
        ):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            cell = Gtk.FlowBoxChild()
            cell.set_child(b)
            actions.append(cell)
        body.append(actions)

        self.append(scroll_panel(body))

    def set_path_status(
        self,
        *,
        state: str,
        state_label: str,
        path: str,
        socks: str,
        hops: str,
        fp: str,
        plane: str,
    ) -> None:
        fp_short = fp if len(fp) < 120 else fp[:117] + "…"
        key = (state, state_label, path, socks, hops, fp_short, plane)
        if key == getattr(self, "_path_status_fp", None):
            return
        self._path_status_fp = key

        if self.state_lab.get_text() != state_label:
            self.state_lab.set_text(state_label)
        if self.path_lab.get_text() != path:
            self.path_lab.set_text(path)
        if self.fact_socks[1].get_text() != socks:
            self.fact_socks[1].set_text(socks)
        if self.fact_hops[1].get_text() != hops:
            self.fact_hops[1].set_text(hops)
        if self.fact_fp[1].get_text() != fp_short:
            self.fact_fp[1].set_text(fp_short)
        if self.fact_plane[1].get_text() != plane:
            self.fact_plane[1].set_text(plane)

        want_dot = {
            "live": "hogwarts-dot-live",
            "idle": "hogwarts-dot-idle",
            "busy": "hogwarts-dot-busy",
            "off": "hogwarts-dot-off",
        }.get(state, "hogwarts-dot-idle")
        if getattr(self, "_dot_class", None) != want_dot:
            for cls in (
                "hogwarts-dot-live",
                "hogwarts-dot-idle",
                "hogwarts-dot-busy",
                "hogwarts-dot-off",
            ):
                self.dot.remove_css_class(cls)
            self.dot.add_css_class(want_dot)
            self._dot_class = want_dot
