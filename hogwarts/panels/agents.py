"""Agents roster + tasking + remote filesystem view."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    pass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from hogwarts.backend.client import AgentDTO, TaskDTO
from hogwarts.panels.desktop_viewer import RemoteDesktopViewer
from hogwarts.panels.file_explorer import RemoteFileExplorer
from hogwarts.widgets import scroll_panel, section_label

# "Live" = online + idle (plane thrash between them must not empty the fleet)
_STATUS_FILTERS = ["All", "live", "offline", "archived"]

# (id, label) — ids match agent _resolve_shell_argv
_SHELLS: list[tuple[str, str]] = [
    ("auto", "Auto"),
    ("sh", "sh"),
    ("bash", "bash"),
    ("zsh", "zsh"),
    ("fish", "fish"),
    ("cmd", "cmd"),
    ("powershell", "PowerShell"),
    ("pwsh", "pwsh"),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_last_seen(
    last_seen: datetime | None, *, sleep: float | None = None
) -> tuple[str, str | None]:
    """Return (human age string, optional sleep-wait banner).

    Age buckets are intentionally coarse so fleet/detail polls do not rewrite
    labels every second (GTK hover dies when button children churn).
    """
    ls = _as_utc(last_seen)
    if ls is None:
        return "never", "no last_seen — do not assume online"
    age = max(0.0, (_utc_now() - ls).total_seconds())
    # Very coarse buckets — poll every few seconds must not rewrite labels
    if age < 45:
        human = "just now"
    elif age < 120:
        human = "under 2m"
    elif age < 3600:
        human = f"{int(age // 60)}m ago"
    elif age < 86400:
        human = f"{int(age // 3600)}h ago"
    else:
        human = f"{int(age // 86400)}d ago"
    banner = None
    if sleep is not None and sleep > 0 and age > 2.0 * float(sleep):
        banner = (
            f"WAIT · last_seen {human} (>2× sleep {sleep:g}s) — "
            "task stays queued until check-in"
        )
    return human, banner


def fleet_status_key(status: str | None) -> str:
    """Collapse online/idle for fleet paint — lab agents thrash online↔idle.

    Plane marks idle after ~1.5× sleep; with 1.5s sleep that flips every poll and
    kills GtkButton :hover / click highlight when CSS classes rewrite.
    """
    st = (status or "unknown").lower()
    if st in ("online", "idle"):
        return "online"
    if st == "offline":
        return "offline"
    if st == "archived":
        return "archived"
    return st or "unknown"


def presence_mode_key(agent: AgentDTO | None) -> str:
    """Beacon-class async vs session-class interactive (Sliver/Havoc lesson)."""
    if agent is None:
        return "async"
    # Archived / offline are not interactive — don't paint INTER from leftover sleep
    if fleet_status_key(agent.status) in ("offline", "archived"):
        return "async"
    p = (agent.presence or "").lower().strip()
    if p in ("async", "interactive"):
        return p
    # Fallback from sleep turbo band
    try:
        if agent.sleep is not None and float(agent.sleep) <= 0.4:
            return "interactive"
    except (TypeError, ValueError):
        pass
    return "async"


def presence_label(mode: str) -> str:
    return "INTER" if mode == "interactive" else "ASYNC"


class AgentsPanel(Gtk.Box):
    def __init__(
        self,
        *,
        on_refresh: Callable,
        on_task: Callable[[str, str, dict], None] | None = None,
        on_refresh_tasks: Callable[[str], None] | None = None,
        on_cancel_task: Callable[[str], None] | None = None,
        on_fetch_file: Callable[[str, str], None] | None = None,
        on_push_file: Callable[[str, str, str], None] | None = None,
        on_fs_list: Callable[[str, str, bool], None] | None = None,
        on_fs_preview: Callable[[str, str], None] | None = None,
        on_fs_index_start: Callable[[str, list[str] | None], None] | None = None,
        on_fs_index_status: Callable[[str], None] | None = None,
        on_fs_index_stop: Callable[[str], None] | None = None,
        on_fs_search: Callable[[str, str, dict], None] | None = None,
        on_screenshot: Callable[..., None] | None = None,
        on_live_desktop: Callable[[str, bool], None] | None = None,
        on_desktop_session: Callable[..., None] | None = None,
        on_desktop_input: Callable[[str, list], None] | None = None,
        on_socks_start: Callable[[str], None] | None = None,
        data_dir: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._on_refresh = on_refresh
        self._on_task = on_task
        self._on_refresh_tasks = on_refresh_tasks
        self._on_cancel_task = on_cancel_task
        self._on_fetch_file = on_fetch_file
        self._on_push_file = on_push_file
        self._on_fs_list = on_fs_list
        self._on_fs_preview = on_fs_preview
        self._on_fs_index_start = on_fs_index_start
        self._on_fs_index_status = on_fs_index_status
        self._on_fs_index_stop = on_fs_index_stop
        self._on_fs_search = on_fs_search
        self._on_screenshot = on_screenshot
        self._on_live_desktop = on_live_desktop
        self._on_desktop_session = on_desktop_session
        self._on_desktop_input = on_desktop_input
        self._on_socks_start = on_socks_start
        self._data_dir = data_dir or ""
        self._agents: list[AgentDTO] = []
        self._selected: AgentDTO | None = None
        self._tasks: list[TaskDTO] = []
        self._selected_task: TaskDTO | None = None
        self._remote_path = ""
        self._remote_parent: str | None = None
        self._remote_sep = "/"
        self._remote_entries: list[dict[str, Any]] = []
        self._show_hidden = False
        self._live_on = False
        self._tab_buttons: dict[str, Gtk.ToggleButton] = {}
        # Per-agent filesystem browser cache (survives tab switch, refresh, fleet back)
        self._fs_cache: dict[str, dict[str, Any]] = {}
        self._fs_listing = False
        # FS nav state (for explorer windows + cache; no embedded browser)
        self._nav_history: list[str] = []
        self._nav_index: int = -1
        self._nav_lock: bool = False
        self._explorer: RemoteFileExplorer | None = None
        self._desktop_viewer: RemoteDesktopViewer | None = None
        self._frame_bytes: bytes | None = None
        self._frame_note: str = ""
        self._shot_max_side: int = 1920
        self._live_interval_ms: int = 2000  # Control uses ~500ms for lower latency
        self._keepstream: Any = None  # KeepstreamClient when Session is up
        self._agents_fp: tuple[Any, ...] | None = None
        self._fleet_paint_fp: tuple[Any, ...] | None = None
        self._tasks_fp: tuple[Any, ...] | None = None
        self._task_rows: dict[str, dict[str, Any]] = {}
        # Generation token so stale async refresh results never rebuild fleet
        self._fleet_gen: int = 0

        # ── Sub-pages: full fleet list  ↔  full agent detail (tabs) ──
        # Stable button fleet (no ListBox thrash) + stack navigation for roomy UX.
        self._view_stack = Gtk.Stack()
        self._view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._view_stack.set_transition_duration(140)
        self._view_stack.set_hexpand(True)
        self._view_stack.set_vexpand(True)

        # ── Fleet page (full width) ───────────────────────────────────
        fleet = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        fleet.add_css_class("hogwarts-panel")
        fleet.set_hexpand(True)
        fleet.set_vexpand(True)
        self._fleet_page = fleet

        intro = Gtk.Label(
            label=(
                "Click an agent for the detail desk. "
                "Tasking shows shell + last result inline. "
                "Files / Desktop tabs open their work windows. "
                "Use ← Fleet to return."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("hogwarts-muted")
        fleet.append(intro)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.Entry()
        self.search.set_placeholder_text("Search hostname, user, id…")
        self.search.set_hexpand(True)
        self.search.connect("activate", lambda *_: on_refresh())
        bar.append(self.search)

        self.status_filter = Gtk.DropDown.new_from_strings(_STATUS_FILTERS)
        self.status_filter.set_selected(0)
        # Filter change must invalidate fleet + refresh — otherwise stale polls
        # (or a soft-patch path) leave dead rows that no longer accept hover/click.
        self.status_filter.connect(
            "notify::selected", lambda *_: self._on_status_filter_changed()
        )
        bar.append(self.status_filter)

        refresh = Gtk.Button(label="Refresh")
        refresh.add_css_class("suggested-action")
        refresh.connect("clicked", lambda *_: on_refresh())
        bar.append(refresh)
        fleet.append(bar)

        self.status_lab = Gtk.Label(label="", xalign=0)
        self.status_lab.add_css_class("hogwarts-muted")
        fleet.append(self.status_lab)

        fleet.append(section_label("Fleet"))
        # Stable buttons pinned by agent id; polls only update text if changed.
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.list_box.set_hexpand(True)
        self.list_box.set_vexpand(False)
        self.list_box.add_css_class("hogwarts-fleet-list")
        self._fleet_rows: dict[str, dict[str, Any]] = {}
        self._selected_soft_key: tuple[Any, ...] | None = None
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_vexpand(True)
        list_scroll.set_hexpand(True)
        list_scroll.set_child(self.list_box)
        fleet.append(list_scroll)

        self._view_stack.add_named(fleet, "fleet")

        # ── Detail page (full width, tabbed) ───────────────────────────
        detail_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        detail_page.add_css_class("hogwarts-panel")
        detail_page.add_css_class("hogwarts-agents-detail")
        detail_page.set_hexpand(True)
        detail_page.set_vexpand(True)
        try:
            detail_page.set_can_focus(False)
            detail_page.set_focusable(False)
        except Exception:
            pass
        self._detail_page = detail_page

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.add_css_class("hogwarts-detail-header")
        header.set_margin_bottom(12)
        self.btn_back = Gtk.Button(label="← Fleet")
        self.btn_back.add_css_class("flat")
        self.btn_back.set_tooltip_text("Back to agent roster")
        self.btn_back.connect("clicked", lambda *_: self._show_fleet())
        header.append(self.btn_back)

        self.detail_dot = Gtk.Box()
        self.detail_dot.add_css_class("hogwarts-dot")
        self.detail_dot.add_css_class("hogwarts-dot-idle")
        self.detail_dot.set_valign(Gtk.Align.CENTER)
        header.append(self.detail_dot)

        head_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head_col.set_hexpand(True)
        self.detail_host = Gtk.Label(label="Agent", xalign=0)
        self.detail_host.add_css_class("hogwarts-agent-host")
        try:
            self.detail_host.set_can_focus(False)
        except Exception:
            pass
        head_col.append(self.detail_host)
        # Not selectable: focus+select-all on open painted the whole header blue
        self.detail_sub = Gtk.Label(label="", xalign=0, wrap=True, selectable=False)
        self.detail_sub.add_css_class("hogwarts-agent-meta")
        try:
            self.detail_sub.set_can_focus(False)
        except Exception:
            pass
        head_col.append(self.detail_sub)
        header.append(head_col)

        self.detail_status_chip = Gtk.Label(label="")
        self.detail_status_chip.add_css_class("hogwarts-chip")
        self.detail_status_chip.set_valign(Gtk.Align.CENTER)
        try:
            self.detail_status_chip.set_can_focus(False)
        except Exception:
            pass
        header.append(self.detail_status_chip)

        self.detail_presence_chip = Gtk.Label(label="")
        self.detail_presence_chip.add_css_class("hogwarts-chip")
        self.detail_presence_chip.add_css_class("hogwarts-presence")
        self.detail_presence_chip.set_valign(Gtk.Align.CENTER)
        self.detail_presence_chip.set_tooltip_text(
            "ASYNC = beacon-class check-in · INTER = interactive/turbo"
        )
        try:
            self.detail_presence_chip.set_can_focus(False)
        except Exception:
            pass
        header.append(self.detail_presence_chip)

        # Quick actions — always one click to Files / Desktop / Tasks (Havoc density)
        quick = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        quick.add_css_class("hogwarts-quick-actions")
        self.btn_quick_files = Gtk.Button(label="Files")
        self.btn_quick_files.add_css_class("flat")
        self.btn_quick_files.add_css_class("hogwarts-quick-btn")
        self.btn_quick_files.set_tooltip_text("Open Files tab + File Explorer window")
        self.btn_quick_files.connect(
            "clicked", lambda *_: self._goto_work_surface("files")
        )
        quick.append(self.btn_quick_files)
        self.btn_quick_desktop = Gtk.Button(label="Desktop")
        self.btn_quick_desktop.add_css_class("flat")
        self.btn_quick_desktop.add_css_class("hogwarts-quick-btn")
        self.btn_quick_desktop.set_tooltip_text(
            "Open Desktop tab + Remote Viewer window"
        )
        self.btn_quick_desktop.connect(
            "clicked", lambda *_: self._goto_work_surface("desktop")
        )
        quick.append(self.btn_quick_desktop)
        self.btn_quick_tasks = Gtk.Button(label="Tasks")
        self.btn_quick_tasks.add_css_class("flat")
        self.btn_quick_tasks.add_css_class("hogwarts-quick-btn")
        self.btn_quick_tasks.set_tooltip_text("Open Tasks queue + last result")
        self.btn_quick_tasks.connect(
            "clicked", lambda *_: self._goto_work_surface("tasks")
        )
        quick.append(self.btn_quick_tasks)
        header.append(quick)

        self.btn_refresh_detail = Gtk.Button(label="Refresh")
        self.btn_refresh_detail.add_css_class("flat")
        self.btn_refresh_detail.set_tooltip_text("Refresh task queue")
        self.btn_refresh_detail.connect("clicked", lambda *_: self._refresh_tasks())
        header.append(self.btn_refresh_detail)
        detail_page.append(header)

        # Identity facts — collapsed by default so tabs own the vertical space
        self.detail_body = Gtk.Label(label="", xalign=0, wrap=True, selectable=False)
        self.detail_body.add_css_class("hogwarts-agent-meta")
        try:
            self.detail_body.set_can_focus(False)
        except Exception:
            pass
        self._facts_expander = Gtk.Expander(label="Agent facts")
        self._facts_expander.add_css_class("hogwarts-facts-expander")
        self._facts_expander.set_expanded(False)
        self._facts_expander.set_margin_bottom(6)
        self._facts_expander.set_child(self.detail_body)
        detail_page.append(self._facts_expander)

        self.task_status = Gtk.Label(label="", xalign=0, wrap=True)
        self.task_status.add_css_class("hogwarts-muted")
        self.task_status.set_margin_bottom(6)
        try:
            self.task_status.set_can_focus(False)
        except Exception:
            pass
        detail_page.append(self.task_status)

        # Tab bar
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tabs.add_css_class("hogwarts-tab-bar")
        tabs.set_margin_bottom(8)
        self._tab_group: Gtk.ToggleButton | None = None
        for key, label in (
            ("tasking", "Tasking"),
            ("files", "Files"),
            ("desktop", "Desktop"),
            ("tasks", "Tasks"),
        ):
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("hogwarts-tab")
            if self._tab_group is None:
                self._tab_group = btn
                btn.set_active(True)
            else:
                btn.set_group(self._tab_group)
            btn.connect("toggled", self._on_tab, key)
            self._tab_buttons[key] = btn
            tabs.append(btn)
        detail_page.append(tabs)

        self._detail_stack = Gtk.Stack()
        self._detail_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._detail_stack.set_transition_duration(120)
        self._detail_stack.set_hexpand(True)
        self._detail_stack.set_vexpand(True)

        # —— Tab: Tasking (dense: actions + shell row + inline last result) ——
        tab_task = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab_task.set_hexpand(True)
        tab_task.set_vexpand(True)
        act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        act.add_css_class("hogwarts-action-strip")
        self.btn_ping = Gtk.Button(label="Ping")
        self.btn_ping.add_css_class("flat")
        self.btn_ping.connect("clicked", lambda *_: self._queue("ping", {}))
        act.append(self.btn_ping)
        self.btn_note = Gtk.Button(label="Note")
        self.btn_note.add_css_class("flat")
        self.btn_note.connect("clicked", lambda *_: self._queue_note())
        act.append(self.btn_note)
        self.btn_rekey = Gtk.Button(label="Rekey")
        self.btn_rekey.add_css_class("flat")
        self.btn_rekey.set_tooltip_text("Rotate agent_token on next check-in")
        self.btn_rekey.connect("clicked", lambda *_: self._queue("rekey", {}))
        act.append(self.btn_rekey)
        self.btn_socks_on = Gtk.Button(label="SOCKS ·")
        self.btn_socks_on.add_css_class("flat")
        self.btn_socks_on.set_tooltip_text("Start SOCKS on agent (ephemeral port)")
        self.btn_socks_on.connect(
            "clicked", lambda *_: self._queue("socks_start", {"port": 0})
        )
        act.append(self.btn_socks_on)
        self.btn_socks_off = Gtk.Button(label="SOCKS ×")
        self.btn_socks_off.add_css_class("flat")
        self.btn_socks_off.set_tooltip_text("Stop SOCKS")
        self.btn_socks_off.connect(
            "clicked", lambda *_: self._queue("socks_stop", {})
        )
        act.append(self.btn_socks_off)
        tab_task.append(act)

        # One row: shell picker + command + Run (operator keyboard path)
        shell_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        shell_lab = Gtk.Label(label="Shell", xalign=0)
        shell_lab.add_css_class("hogwarts-field-label")
        shell_row.append(shell_lab)
        self.shell_dd = Gtk.DropDown.new_from_strings([lab for _, lab in _SHELLS])
        self.shell_dd.set_selected(0)
        self.shell_dd.set_size_request(110, -1)
        self.shell_dd.set_tooltip_text(
            "Interpreter (auto = sh on Unix, cmd on Windows)"
        )
        shell_row.append(self.shell_dd)
        self.shell_entry = Gtk.Entry()
        self.shell_entry.set_placeholder_text("command…  Enter to run")
        self.shell_entry.set_hexpand(True)
        self.shell_entry.connect("activate", lambda *_: self._queue_shell())
        shell_row.append(self.shell_entry)
        self.btn_shell = Gtk.Button(label="Run")
        self.btn_shell.add_css_class("suggested-action")
        self.btn_shell.connect("clicked", lambda *_: self._queue_shell())
        shell_row.append(self.btn_shell)
        self.btn_tasks = Gtk.Button(label="Queue…")
        self.btn_tasks.add_css_class("flat")
        self.btn_tasks.set_tooltip_text("Open full Tasks queue")
        self.btn_tasks.connect("clicked", lambda *_: self._select_tab("tasks"))
        shell_row.append(self.btn_tasks)
        tab_task.append(shell_row)

        # Inline last result — stay on Tasking while work returns (Havoc energy)
        res_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        res_lab = section_label("Last result")
        res_lab.set_hexpand(True)
        res_head.append(res_lab)
        self.tasking_result_meta = Gtk.Label(label="", xalign=1)
        self.tasking_result_meta.add_css_class("hogwarts-muted")
        self.tasking_result_meta.add_css_class("hogwarts-agent-meta")
        res_head.append(self.tasking_result_meta)
        tab_task.append(res_head)

        self.tasking_result_view = Gtk.TextView()
        self.tasking_result_view.set_editable(False)
        self.tasking_result_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.tasking_result_view.set_monospace(True)
        self.tasking_result_view.set_vexpand(True)
        self.tasking_result_view.set_hexpand(True)
        self.tasking_result_view.get_buffer().set_text(
            "Shell, ping, and file results land here after the agent checks in.\n"
            "Full queue is under the Tasks tab."
        )
        tasking_res_scroll = Gtk.ScrolledWindow()
        tasking_res_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        tasking_res_scroll.set_vexpand(True)
        tasking_res_scroll.set_hexpand(True)
        tasking_res_scroll.set_min_content_height(160)
        tasking_res_scroll.set_child(self.tasking_result_view)
        tasking_res_scroll.add_css_class("hogwarts-remote-scroll")
        tab_task.append(tasking_res_scroll)
        self._detail_stack.add_named(tab_task, "tasking")

        # —— Tab: Files (auto-opens File Explorer; panel is status + re-open) ——
        tab_files = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_files.set_hexpand(True)
        tab_files.set_vexpand(True)

        files_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        files_card.add_css_class("hogwarts-card")
        files_card.add_css_class("hogwarts-launch-card")
        files_card.set_hexpand(True)
        files_card.set_vexpand(True)

        files_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        files_icon = Gtk.Image.new_from_icon_name("folder-open")
        files_icon.set_pixel_size(28)
        files_icon.set_valign(Gtk.Align.START)
        files_head.append(files_icon)
        files_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        files_text.set_hexpand(True)
        files_title = Gtk.Label(label="File Explorer", xalign=0)
        files_title.add_css_class("hogwarts-launch-title")
        files_text.append(files_title)
        files_blurb = Gtk.Label(
            label=(
                "Work window: browse, download, push, index search. "
                "Selecting this tab opens (or focuses) it."
            ),
            xalign=0,
            wrap=True,
        )
        files_blurb.add_css_class("hogwarts-muted")
        files_text.append(files_blurb)
        files_head.append(files_text)
        files_card.append(files_head)

        self.remote_status = Gtk.Label(
            label="Window closed — select this tab or Open to launch.",
            xalign=0,
            wrap=True,
        )
        self.remote_status.add_css_class("hogwarts-muted")
        files_card.append(self.remote_status)

        self.files_path_lab = Gtk.Label(label="Path  —", xalign=0, selectable=True)
        self.files_path_lab.add_css_class("hogwarts-agent-meta")
        files_card.append(self.files_path_lab)

        files_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_explorer = Gtk.Button(label="Open / focus")
        self.btn_explorer.add_css_class("suggested-action")
        self.btn_explorer.set_tooltip_text("Open or focus the File Explorer window")
        self.btn_explorer.connect("clicked", lambda *_: self._open_file_explorer())
        files_actions.append(self.btn_explorer)
        self.btn_explorer_focus = Gtk.Button(label="Re-list path")
        self.btn_explorer_focus.add_css_class("flat")
        self.btn_explorer_focus.set_tooltip_text(
            "Queue fs_list for the current remote path"
        )
        self.btn_explorer_focus.connect(
            "clicked",
            lambda *_: self._browse_remote(
                path=self._remote_path or self._default_remote_path()
            ),
        )
        files_actions.append(self.btn_explorer_focus)
        files_card.append(files_actions)
        tab_files.append(files_card)
        self._detail_stack.add_named(tab_files, "files")

        # —— Tab: Desktop (auto-opens Remote Viewer) ——
        tab_desk = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_desk.set_hexpand(True)
        tab_desk.set_vexpand(True)

        desk_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        desk_card.add_css_class("hogwarts-card")
        desk_card.add_css_class("hogwarts-launch-card")
        desk_card.set_hexpand(True)
        desk_card.set_vexpand(True)

        desk_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        desk_icon = Gtk.Image.new_from_icon_name("video-display")
        desk_icon.set_pixel_size(28)
        desk_icon.set_valign(Gtk.Align.START)
        desk_head.append(desk_icon)
        desk_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        desk_text.set_hexpand(True)
        desk_title = Gtk.Label(label="Remote Viewer", xalign=0)
        desk_title.add_css_class("hogwarts-launch-title")
        desk_text.append(desk_title)
        desk_blurb = Gtk.Label(
            label=(
                "Stream · Watch / Control live in the work window. "
                "Selecting this tab opens (or focuses) it."
            ),
            xalign=0,
            wrap=True,
        )
        desk_blurb.add_css_class("hogwarts-muted")
        desk_text.append(desk_blurb)
        desk_head.append(desk_text)
        desk_card.append(desk_head)

        self.desktop_status = Gtk.Label(
            label="Window closed — select this tab or Open to launch.",
            xalign=0,
            wrap=True,
        )
        self.desktop_status.add_css_class("hogwarts-muted")
        desk_card.append(self.desktop_status)

        desk_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_desktop_win = Gtk.Button(label="Open / focus")
        self.btn_desktop_win.add_css_class("suggested-action")
        self.btn_desktop_win.set_tooltip_text(
            "Open or focus Remote Viewer (screenshot / Live / Control)"
        )
        self.btn_desktop_win.connect(
            "clicked", lambda *_: self._open_desktop_viewer(auto_shot=False)
        )
        desk_actions.append(self.btn_desktop_win)
        self.btn_desktop_shot = Gtk.Button(label="Open + Capture")
        self.btn_desktop_shot.add_css_class("flat")
        self.btn_desktop_shot.set_tooltip_text(
            "Open Remote Viewer and queue a screenshot"
        )
        self.btn_desktop_shot.connect(
            "clicked", lambda *_: self._open_desktop_viewer(auto_shot=True)
        )
        desk_actions.append(self.btn_desktop_shot)
        desk_card.append(desk_actions)
        tab_desk.append(desk_card)
        self._detail_stack.add_named(tab_desk, "desktop")

        # —— Tab: Tasks + result ——
        tab_tasks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab_tasks.set_hexpand(True)
        tab_tasks.set_vexpand(True)
        cancel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_cancel = Gtk.Button(label="Cancel selected task")
        self.btn_cancel.add_css_class("flat")
        self.btn_cancel.set_tooltip_text("Cancel queued/assigned task")
        self.btn_cancel.connect("clicked", lambda *_: self._cancel_selected())
        cancel_row.append(self.btn_cancel)
        tab_tasks.append(cancel_row)

        tasks_split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tasks_split.set_hexpand(True)
        tasks_split.set_vexpand(True)

        left_tasks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_tasks.set_hexpand(True)
        left_tasks.set_vexpand(True)
        left_tasks.append(section_label("Queue"))
        self.task_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        task_scroll = Gtk.ScrolledWindow()
        task_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        task_scroll.set_vexpand(True)
        task_scroll.set_hexpand(True)
        task_scroll.set_child(self.task_list)
        left_tasks.append(task_scroll)
        tasks_split.append(left_tasks)

        right_res = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right_res.set_hexpand(True)
        right_res.set_vexpand(True)
        right_res.append(section_label("Last result"))
        self.result_view = Gtk.TextView()
        self.result_view.set_editable(False)
        self.result_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.result_view.set_monospace(True)
        self.result_view.set_vexpand(True)
        self.result_view.set_hexpand(True)
        res_scroll = Gtk.ScrolledWindow()
        res_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        res_scroll.set_vexpand(True)
        res_scroll.set_hexpand(True)
        res_scroll.set_child(self.result_view)
        res_scroll.add_css_class("hogwarts-remote-scroll")
        right_res.append(res_scroll)
        tasks_split.append(right_res)
        tab_tasks.append(tasks_split)
        self._detail_stack.add_named(tab_tasks, "tasks")

        detail_page.append(self._detail_stack)
        self._view_stack.add_named(detail_page, "detail")
        self._view_stack.set_visible_child(fleet)
        self.append(self._view_stack)
        self._set_tasking_sensitive(False)
        self._show_fleet()
        self.show_empty("Configure the control plane under Plane, then Refresh.")

    def filter_status(self) -> str | None:
        """Plane status filter.

        ``All`` → active (excludes archived). ``live`` → online|idle.
        ``archived`` → archive-only.
        """
        try:
            i = int(self.status_filter.get_selected())
        except (TypeError, ValueError):
            return None
        # GTK_INVALID_LIST_POSITION is a huge guint; treat as All
        if i <= 0 or i >= len(_STATUS_FILTERS):
            return None  # plane default = active (no archived)
        return _STATUS_FILTERS[i]

    def query(self) -> str:
        return self.search.get_text().strip()

    def invalidate_fleet(self) -> None:
        """Force next set_agents() to hard-rebuild rows (filter/search changed).

        Also bumps ``_fleet_gen`` so in-flight polls/refreshes with the old
        filter cannot paint a dead roster after the user switched back to All.
        """
        self._fleet_gen = int(getattr(self, "_fleet_gen", 0)) + 1
        self._agents_fp = None
        self._fleet_paint_fp = None

    def _on_status_filter_changed(self) -> None:
        """Dropdown changed — drop selection paint, rebuild on next payload."""
        self.invalidate_fleet()
        # Leave detail if the open agent may disappear under a new filter
        try:
            if self._view_stack.get_visible_child_name() == "detail":
                self._selected = None
                self._selected_soft_key = None
                self._set_tasking_sensitive(False)
                self._show_fleet()
        except Exception:
            pass
        self.status_lab.set_text("Loading…")
        try:
            self._on_refresh()
        except Exception:
            pass

    def selected_agent_id(self) -> str | None:
        return self._selected.id if self._selected else None

    def show_error(self, msg: str) -> None:
        """Surface plane error without wiping a healthy roster.

        Spam-refresh used to clear the fleet on the last failure → no hover
        targets until a successful paint. Keep rows if we already have agents.
        """
        short = (msg or "Error").strip()
        if len(short) > 220:
            short = short[:217] + "…"
        self.status_lab.set_text(short)
        self.status_lab.remove_css_class("hogwarts-ok")
        self.status_lab.add_css_class("hogwarts-fail")
        if self._fleet_rows:
            # Keep buttons / hover targets; only banner changes
            return
        self._agents_fp = None
        self._fleet_paint_fp = None
        self._clear_list()
        self.list_box.append(self._placeholder_label(msg, fail=True))
        self._show_fleet()

    def show_empty(self, msg: str) -> None:
        self.status_lab.set_text(msg)
        self._clear_list()
        self.list_box.append(self._placeholder_label(msg, fail=False))
        self._selected = None
        self._agents_fp = None
        self._fleet_paint_fp = None
        self._tasks_fp = None
        self._selected_soft_key = None
        self.detail_host.set_text("Select an agent")
        self.detail_sub.set_text("Open a host from the fleet list")
        self.detail_body.set_text("")
        self._clear_tasks()
        self._set_tasking_sensitive(False)
        self.stop_live_ui()
        self._show_fleet()

    @staticmethod
    def _stable_sort(agents: list[AgentDTO]) -> list[AgentDTO]:
        """Pin list order — never follow plane last_seen reordering."""
        return sorted(
            agents,
            key=lambda a: (
                (a.hostname or "").lower(),
                (a.id or ""),
            ),
        )

    @staticmethod
    def _fleet_membership(agents: list[AgentDTO]) -> tuple[str, ...]:
        """Stable membership key — ids only, sorted. last_seen ignored."""
        return tuple(sorted(a.id for a in agents if a.id))

    @staticmethod
    def _fleet_paint_key(agents: list[AgentDTO]) -> tuple[Any, ...]:
        """What the roster is allowed to repaint for — ignores raw last_seen and idle thrash."""
        rows: list[tuple[Any, ...]] = []
        for a in agents:
            age, _ = format_last_seen(a.last_seen, sleep=a.sleep)
            rows.append(
                (
                    a.id,
                    fleet_status_key(a.status),
                    presence_mode_key(a),
                    age,
                    a.hostname or "",
                    a.username or "",
                    a.os or "",
                    a.external_ip or "",
                    a.group or "",
                )
            )
        return tuple(rows)

    def set_agents(
        self, agents: list[AgentDTO], *, note: str = "", quiet: bool = False
    ) -> None:
        agents = self._stable_sort(list(agents))
        self._agents = agents
        if not agents:
            self._agents_fp = None
            self._fleet_paint_fp = None
            self.show_empty(note or "No agents reported by the control plane.")
            return
        counts: dict[str, int] = {}
        presence_counts = {"async": 0, "interactive": 0}
        for a in agents:
            # Count with collapsed live status so header does not flicker online/idle
            key = fleet_status_key(a.status)
            counts[key] = counts.get(key, 0) + 1
            if key == "online":
                pm = presence_mode_key(a)
                presence_counts[pm] = presence_counts.get(pm, 0) + 1
        parts = [f"{len(agents)} agent" + ("s" if len(agents) != 1 else "")]
        for st in ("online", "offline", "archived"):
            if st in counts:
                label = "live" if st == "online" else st
                parts.append(f"{counts[st]} {label}")
        if presence_counts.get("interactive"):
            parts.append(f"{presence_counts['interactive']} inter")
        if presence_counts.get("async") and counts.get("online"):
            parts.append(f"{presence_counts['async']} async")
        if counts.get("unknown"):
            parts.append(f"{counts['unknown']} other")
        status_txt = " · ".join(parts) + (f" — {note}" if note else "")
        if self.status_lab.get_text() != status_txt:
            self.status_lab.set_text(status_txt)
        self.status_lab.remove_css_class("hogwarts-fail")

        membership = self._fleet_membership(agents)
        paint_fp = self._fleet_paint_key(agents)
        # Soft path only when row map is intact and matches membership
        row_ids = set(self._fleet_rows.keys())
        mem_ids = set(membership)
        rows_ok = bool(self._fleet_rows) and row_ids == mem_ids
        # Also require every row widget still has a live button (desync after filter)
        if rows_ok:
            try:
                rows_ok = all(
                    isinstance(w.get("btn"), Gtk.Button)
                    and w["btn"].get_parent() is not None
                    for w in self._fleet_rows.values()
                )
            except Exception:
                rows_ok = False
        if membership == self._agents_fp and rows_ok:
            by_id = {a.id: a for a in agents}
            # Skip all GTK label/CSS writes when nothing user-visible changed
            if paint_fp != self._fleet_paint_fp:
                self._fleet_paint_fp = paint_fp
                self._patch_fleet_rows(by_id)
            else:
                # Keep DTO pointers fresh without touching widgets
                for aid, widgets in self._fleet_rows.items():
                    if aid in by_id:
                        widgets["agent"] = by_id[aid]
            if self._selected:
                match = by_id.get(self._selected.id)
                if match:
                    # Only refresh detail text while on the detail sub-page
                    if self._view_stack.get_visible_child_name() == "detail":
                        self._soft_fill_if_changed(match)
                else:
                    self._selected = None
                    self._selected_soft_key = None
                    self._set_tasking_sensitive(False)
                    self._mark_fleet_selected(None)
                    self._show_fleet()
            # Re-assert hover after any soft paint (filter thrash can drop :hover)
            self._sync_fleet_pointer_hover()
            return

        # Hard rebuild when agents join/leave OR row map was wiped/desynced
        self._agents_fp = membership
        self._fleet_paint_fp = paint_fp
        keep_id = self._selected.id if self._selected else None
        was_detail = self._view_stack.get_visible_child_name() == "detail"
        self._clear_list()
        for agent in agents:
            self.list_box.append(self._row(agent))
        # Ensure fleet list accepts pointer after rebuild
        try:
            self.list_box.set_sensitive(True)
            self.list_box.set_can_target(True)
        except Exception:
            pass
        # Pointer may still sit on a row after rebuild — re-assert motion hover
        self._sync_fleet_pointer_hover()
        # Idle re-sync: DropDown popover close can steal pointer events for a tick
        GLib.idle_add(self._sync_fleet_pointer_hover_idle)
        if keep_id:
            match = next((a for a in agents if a.id == keep_id), None)
            if match:
                self._fill_detail(match, soft=True)
                self._mark_fleet_selected(keep_id)
                if was_detail:
                    self._view_stack.set_visible_child(self._detail_page)
            else:
                self._selected = None
                self._selected_soft_key = None
                self._set_tasking_sensitive(False)
                self._show_fleet()
        else:
            self._show_fleet()

    def _soft_fill_if_changed(self, agent: AgentDTO) -> None:
        """Avoid thrashing detail labels on every poll (kills hover focus elsewhere)."""
        age, _ = format_last_seen(agent.last_seen, sleep=agent.sleep)
        # Coarse age + collapsed status — omit raw last_seen and online/idle thrash
        key = (
            agent.id,
            fleet_status_key(agent.status),
            presence_mode_key(agent),
            age,
            agent.hostname,
            agent.username,
            agent.external_ip,
        )
        if key == self._selected_soft_key:
            # Still keep selected DTO current for tasking
            self._selected = agent
            return
        self._selected_soft_key = key
        self._fill_detail(agent, soft=True)

    def _patch_fleet_rows(self, by_id: dict[str, AgentDTO]) -> None:
        """Update label text only when it actually changed — widgets stay put.

        Never recreate rows or rewrite identical strings: GTK drops :hover when
        child labels/tooltips are touched every poll (e.g. 'seen just now').
        """
        for aid, widgets in self._fleet_rows.items():
            fresh = by_id.get(aid)
            if fresh is None:
                continue
            widgets["agent"] = fresh
            status = fleet_status_key(fresh.status)
            mode = presence_mode_key(fresh)
            st_lab: Gtk.Label = widgets["status"]
            want_st = "LIVE" if status == "online" else status.upper()
            if st_lab.get_text() != want_st:
                st_lab.set_text(want_st)
                for c in (
                    "hogwarts-status-online",
                    "hogwarts-status-idle",
                    "hogwarts-status-offline",
                    "hogwarts-status-archived",
                    "hogwarts-status-unknown",
                ):
                    st_lab.remove_css_class(c)
                paint = (
                    status
                    if status in ("online", "offline", "archived")
                    else "unknown"
                )
                st_lab.add_css_class(f"hogwarts-status-{paint}")
            # Presence chip (ASYNC / INTER) — only when live
            mode_lab: Gtk.Label | None = widgets.get("presence")
            if mode_lab is not None:
                want_mode = presence_label(mode) if status == "online" else ""
                if mode_lab.get_text() != want_mode:
                    mode_lab.set_text(want_mode)
                    mode_lab.set_visible(bool(want_mode))
                    for c in (
                        "hogwarts-presence-async",
                        "hogwarts-presence-interactive",
                    ):
                        mode_lab.remove_css_class(c)
                    if want_mode:
                        mode_lab.add_css_class(
                            "hogwarts-presence-interactive"
                            if mode == "interactive"
                            else "hogwarts-presence-async"
                        )
            # Busy dot when interactive
            host_lab: Gtk.Label = widgets["host"]
            want_host = fresh.hostname or fresh.id or "?"
            if host_lab.get_text() != want_host:
                host_lab.set_text(want_host)
            age, _ = format_last_seen(fresh.last_seen, sleep=fresh.sleep)
            meta_bits = [
                fresh.username,
                fresh.os,
                fresh.external_ip,
                fresh.group,
                f"seen {age}",
                fresh.package_id or "",
                fresh.id,
            ]
            want_meta = " · ".join(b for b in meta_bits if b) or fresh.id
            meta_lab: Gtk.Label = widgets["meta"]
            # Skip set_text when unchanged — critical for stable hover
            if meta_lab.get_text() != want_meta:
                meta_lab.set_text(want_meta)
            # Dot class: interactive → busy (blue), else live/off/archived
            dot: Gtk.Widget = widgets["dot"]
            if status in ("offline", "archived"):
                want_dot = "hogwarts-dot-off"
            elif mode == "interactive":
                want_dot = "hogwarts-dot-busy"
            elif status == "online":
                want_dot = "hogwarts-dot-live"
            else:
                want_dot = "hogwarts-dot-idle"
            cur = widgets.get("dot_class")
            if cur != want_dot:
                for c in (
                    "hogwarts-dot-live",
                    "hogwarts-dot-busy",
                    "hogwarts-dot-off",
                    "hogwarts-dot-idle",
                ):
                    dot.remove_css_class(c)
                dot.add_css_class(want_dot)
                widgets["dot_class"] = want_dot
            btn: Gtk.Button = widgets["btn"]
            mode_tip = presence_label(mode).lower() if status == "online" else "off"
            want_tip = f"{fresh.hostname or aid} · {want_st.lower()} · {mode_tip}"
            # set_tooltip_text every poll also resets hover styling on some themes
            if widgets.get("tip") != want_tip:
                btn.set_tooltip_text(want_tip)
                widgets["tip"] = want_tip

    def _mark_fleet_selected(self, agent_id: str | None) -> None:
        """Visual selected state via CSS class — no ListBox thrash."""
        for aid, widgets in self._fleet_rows.items():
            btn: Gtk.Button = widgets["btn"]
            if agent_id and aid == agent_id:
                btn.add_css_class("hogwarts-fleet-btn-selected")
            else:
                btn.remove_css_class("hogwarts-fleet-btn-selected")

    def _sync_fleet_pointer_hover(self) -> None:
        """After hard rebuild, re-apply hover if the pointer is still over a row.

        GTK often fires leave without re-enter when widgets are destroyed under
        the cursor — motion class would stick off until the user wiggles.
        """
        for widgets in self._fleet_rows.values():
            btn: Gtk.Button = widgets["btn"]
            try:
                over = bool(btn.contains_pointer())
            except Exception:
                over = False
            if over:
                btn.add_css_class("hogwarts-fleet-btn-hover")
            else:
                btn.remove_css_class("hogwarts-fleet-btn-hover")

    def _sync_fleet_pointer_hover_idle(self) -> bool:
        """GLib.idle_add wrapper — always return False (one-shot)."""
        try:
            self._sync_fleet_pointer_hover()
        except Exception:
            pass
        return False

    def _select_fleet_row(self, agent_id: str) -> None:
        self._mark_fleet_selected(agent_id)

    def _show_fleet(self) -> None:
        """Return to full-width fleet roster sub-page.

        Do not stop Live here — Remote Viewer is a separate window and should
        keep polling until closed or the operator toggles Live off.
        """
        # Drop sticky selected styling — roster is browse-only until next click.
        # (Selected class looked like a stuck hover after ← Fleet.)
        self._mark_fleet_selected(None)
        try:
            self._view_stack.set_visible_child(self._fleet_page)
        except Exception:
            self._view_stack.set_visible_child_name("fleet")

    def _select_tab(self, key: str) -> None:
        btn = self._tab_buttons.get(key)
        if btn is not None and not btn.get_active():
            btn.set_active(True)
        else:
            self._detail_stack.set_visible_child_name(key)
            self._activate_work_surface(key)

    def _goto_work_surface(self, key: str) -> None:
        """Header quick-action: switch tab and open the real work window."""
        if not self._selected:
            self.set_task_note("Select an agent first", ok=False)
            return
        self._select_tab(key)

    def _activate_work_surface(self, key: str) -> None:
        """Make Files / Desktop first-class: tab selection opens the window."""
        if key == "tasks" and self._selected:
            self._refresh_tasks()
            return
        if key == "files" and self._selected:
            # Defer so stack transition finishes before present()
            GLib.idle_add(self._open_file_explorer_idle)
            return
        if key == "desktop" and self._selected:
            GLib.idle_add(self._open_desktop_viewer_idle)
            return

    def _open_file_explorer_idle(self) -> bool:
        try:
            self._open_file_explorer()
        except Exception:
            pass
        return False

    def _open_desktop_viewer_idle(self) -> bool:
        try:
            self._open_desktop_viewer(auto_shot=False)
        except Exception:
            pass
        return False

    def _on_tab(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active():
            return
        self._detail_stack.set_visible_child_name(key)
        self._activate_work_surface(key)
        self._sync_work_tab_labels()

    def _sync_work_tab_labels(self) -> None:
        """Badge Files/Desktop tabs when their work windows are open."""
        files_open = self._explorer is not None
        desk_open = self._desktop_viewer is not None
        mapping = {
            "files": ("Files ●" if files_open else "Files", files_open),
            "desktop": ("Desktop ●" if desk_open else "Desktop", desk_open),
        }
        for key, (label, open_) in mapping.items():
            btn = self._tab_buttons.get(key)
            if btn is None:
                continue
            if btn.get_label() != label:
                btn.set_label(label)
            if open_:
                btn.add_css_class("hogwarts-tab-active-win")
            else:
                btn.remove_css_class("hogwarts-tab-active-win")

    def set_tasks(self, tasks: list[TaskDTO], *, note: str = "") -> None:
        self._tasks = tasks
        if note:
            self.task_status.set_text(note)
        # Fingerprint without `updated` — poll-only timestamp thrash rebuilt
        # the whole task list and killed selection/hover every few seconds.
        shown = list(tasks[:30])
        ids = tuple(t.id for t in shown if t.id)
        fp = tuple(
            (
                t.id,
                t.status or "",
                t.type or "",
                bool(t.result),
                (t.result or {}).get("exit_code") if t.result else None,
                (t.result or {}).get("error") if t.result else None,
            )
            for t in shown
        )
        if fp == self._tasks_fp and self.task_list.get_first_child() is not None:
            return

        # Soft path: same task ids in same order — patch labels only
        if (
            self._task_rows
            and ids
            and ids == tuple(self._task_rows.keys())
            and self.task_list.get_first_child() is not None
        ):
            self._tasks_fp = fp
            for t in shown:
                widgets = self._task_rows.get(t.id)
                if not widgets:
                    continue
                widgets["task"] = t
                lab: Gtk.Label = widgets["lab"]
                want = f"{t.status:10}  {t.type:6}  {t.id}"
                if lab.get_text() != want:
                    lab.set_text(want)
                btn: Gtk.Button = widgets["btn"]
                if self._selected_task and self._selected_task.id == t.id:
                    btn.add_css_class("hogwarts-fleet-btn-selected")
                    # Keep selected task DTO current for cancel
                    self._selected_task = t
                else:
                    btn.remove_css_class("hogwarts-fleet-btn-selected")
            self._maybe_auto_result(tasks)
            return

        # Hard rebuild when membership/order changes
        self._tasks_fp = fp
        keep_sel = self._selected_task.id if self._selected_task else None
        self._clear_tasks()
        if not shown:
            empty = Gtk.Label(label="No tasks yet — Ping or shell first.", xalign=0)
            empty.add_css_class("hogwarts-muted")
            self.task_list.append(empty)
            return
        for t in shown:
            self.task_list.append(self._task_row(t))
        if keep_sel and keep_sel in self._task_rows:
            self._mark_task_selected(keep_sel)
            self._selected_task = self._task_rows[keep_sel]["task"]
        self._maybe_auto_result(tasks)

    def _maybe_auto_result(self, tasks: list[TaskDTO]) -> None:
        for t in tasks:
            if t.result and t.status in ("succeeded", "failed"):
                key = (
                    t.id,
                    t.status,
                    (t.result or {}).get("exit_code"),
                    (t.result or {}).get("error"),
                )
                if key != getattr(self, "_auto_result_key", None):
                    self._auto_result_key = key
                    self._show_result(t)
                break

    def set_task_note(self, msg: str, *, ok: bool | None = None) -> None:
        self.task_status.set_text(msg)
        self.task_status.remove_css_class("hogwarts-ok")
        self.task_status.remove_css_class("hogwarts-fail")
        if ok is True:
            self.task_status.add_css_class("hogwarts-ok")
        elif ok is False:
            self.task_status.add_css_class("hogwarts-fail")

    def prepend_queued_task(self, task_id: str, type_: str) -> None:
        """Optimistic UI after queue — before the next list_tasks round-trip."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        t = TaskDTO(
            id=task_id,
            type=type_,
            status="queued",
            created=now,
            updated=now,
            payload=None,
            result=None,
            agent_id=self._selected.id if self._selected else None,
        )
        # Soft-insert at head without wiping the rest of the list
        self._tasks_fp = None
        first = self.task_list.get_first_child()
        if first is not None and isinstance(first, Gtk.Label):
            self.task_list.remove(first)
        prev_rows = dict(self._task_rows)
        self.task_list.prepend(self._task_row(t))
        # Ordered map: new task first (Python 3.7+ dict order)
        ordered: dict[str, dict[str, Any]] = {}
        if t.id and t.id in self._task_rows:
            ordered[t.id] = self._task_rows[t.id]
        for tid, w in prev_rows.items():
            if tid != t.id:
                ordered[tid] = w
        self._task_rows = ordered
        self._tasks = [t] + list(self._tasks)

    def _set_tasking_sensitive(self, on: bool) -> None:
        for w in (
            self.btn_ping,
            self.btn_note,
            self.btn_rekey,
            self.btn_socks_on,
            self.btn_socks_off,
            self.btn_shell,
            self.btn_tasks,
            self.btn_refresh_detail,
            self.btn_explorer,
            self.btn_explorer_focus,
            self.btn_desktop_win,
            getattr(self, "btn_desktop_shot", None),
            self.btn_cancel,
            self.shell_entry,
            self.shell_dd,
            getattr(self, "btn_quick_files", None),
            getattr(self, "btn_quick_desktop", None),
            getattr(self, "btn_quick_tasks", None),
        ):
            if w is not None:
                w.set_sensitive(on)

    def selected_shell(self) -> str:
        i = int(self.shell_dd.get_selected())
        if i < 0 or i >= len(_SHELLS):
            return "auto"
        return _SHELLS[i][0]

    def set_remote_status(self, msg: str, *, ok: bool | None = None) -> None:
        """Status on Files tab + open File Explorer window."""
        self.remote_status.set_text(msg)
        self.remote_status.remove_css_class("hogwarts-ok")
        self.remote_status.remove_css_class("hogwarts-fail")
        if ok is True:
            self.remote_status.add_css_class("hogwarts-ok")
        elif ok is False:
            self.remote_status.add_css_class("hogwarts-fail")
        if self._explorer is not None:
            try:
                self._explorer.set_status(msg, ok=ok)
            except Exception:
                pass

    def _update_files_path_lab(self) -> None:
        path = self._remote_path or "—"
        n = len(self._remote_entries)
        if self._remote_path:
            self.files_path_lab.set_text(f"Path  {path}  ·  {n} item{'s' if n != 1 else ''}")
        else:
            self.files_path_lab.set_text("Path  —")

    def set_remote_view(
        self,
        path: str,
        entries: list[dict[str, Any]],
        *,
        parent: str | None = None,
        sep: str = "/",
        note: str | None = None,
        ok: bool | None = True,
        replace: bool = True,
    ) -> None:
        """Cache listing and push into the File Explorer window (only UI)."""
        self._fs_listing = False
        was_nav_lock = self._nav_lock
        if replace:
            self._remote_path = path or ""
            self._remote_parent = parent
            self._remote_sep = sep or "/"
            self._remote_entries = list(entries or [])
            if ok is not False and self._remote_path and not self._nav_lock:
                self._push_nav(self._remote_path)
            elif self._nav_lock:
                self._nav_lock = False
        n = len(self._remote_entries)
        msg = note or f"{n} entr{'y' if n == 1 else 'ies'} · {path or self._remote_path}"
        self.set_remote_status(msg, ok=ok)
        self._update_files_path_lab()
        if ok is not False and self._selected:
            self._fs_cache[self._selected.id] = {
                "path": self._remote_path,
                "entries": list(self._remote_entries),
                "parent": self._remote_parent,
                "sep": self._remote_sep,
                "note": msg,
                "history": list(self._nav_history),
                "hist_i": self._nav_index,
            }
        if self._explorer is not None and replace:
            try:
                self._explorer.apply_listing(
                    self._remote_path,
                    list(self._remote_entries),
                    parent=self._remote_parent,
                    sep=self._remote_sep,
                    note=msg,
                    ok=ok,
                    record_history=not was_nav_lock,
                )
            except Exception:
                pass

    def clear_remote_view(self, msg: str = "", *, forget_cache: bool = False) -> None:
        aid = self._selected.id if self._selected else None
        if forget_cache and aid and aid in self._fs_cache:
            del self._fs_cache[aid]
        self._remote_entries = []
        self._remote_path = ""
        self._remote_parent = None
        self._fs_listing = False
        self._update_files_path_lab()
        self.set_remote_status(
            msg or "No folder listed yet — open File Explorer to start."
        )

    def restore_remote_view(self, agent_id: str) -> bool:
        """Restore cached listing for agent. Returns True if restored."""
        snap = self._fs_cache.get(agent_id)
        if not snap:
            return False
        hist = snap.get("history")
        if isinstance(hist, list) and hist:
            self._nav_history = [str(p) for p in hist if p]
            try:
                self._nav_index = int(snap.get("hist_i", len(self._nav_history) - 1))
            except (TypeError, ValueError):
                self._nav_index = len(self._nav_history) - 1
            self._nav_lock = True
        self.set_remote_view(
            str(snap.get("path") or ""),
            list(snap.get("entries") or []),
            parent=snap.get("parent"),
            sep=str(snap.get("sep") or "/"),
            note=str(snap.get("note") or ""),
            ok=True,
            replace=True,
        )
        return True

    def mark_remote_listing(self, path: str) -> None:
        """In-progress status while fs_list runs."""
        self._fs_listing = True
        target = path.strip() or "(default)"
        self.set_remote_status(f"Listing {target}…", ok=None)
        if self._explorer is not None:
            try:
                self._explorer.mark_listing(path)
            except Exception:
                pass

    def _default_remote_path(self) -> str:
        if self._remote_path:
            return self._remote_path
        os_s = ((self._selected.os if self._selected else "") or "").lower()
        if "windows" in os_s or "nt" in os_s:
            return "C:\\"
        return "/"

    def _push_nav(self, path: str) -> None:
        if not path:
            return
        if self._nav_index >= 0 and self._nav_index < len(self._nav_history) - 1:
            self._nav_history = self._nav_history[: self._nav_index + 1]
        if self._nav_history and self._nav_history[-1] == path:
            self._nav_index = len(self._nav_history) - 1
            return
        self._nav_history.append(path)
        if len(self._nav_history) > 80:
            self._nav_history = self._nav_history[-80:]
        self._nav_index = len(self._nav_history) - 1

    def _browse_remote(self, path: str | None = None) -> None:
        if not self._selected or not self._on_fs_list:
            return
        p = (path if path is not None else self._default_remote_path()).strip()
        self.mark_remote_listing(p or "(home/default)")
        self._on_fs_list(self._selected.id, p, self._show_hidden)

    def _open_file_explorer(self) -> None:
        """Open (or focus) the Windows-style File Explorer window."""
        if not self._selected:
            self.set_task_note("Select an agent first", ok=False)
            return
        if self._explorer is not None:
            try:
                self._explorer.present()
                return
            except Exception:
                self._explorer = None

        agent = self._selected
        label = agent.hostname or agent.id

        def on_browse(path: str) -> None:
            self._browse_remote(path=path)

        def on_download(path: str) -> None:
            self._remote_path = path
            self._queue("download", {"path": path, "offset": 0, "length": 256000})

        def on_fetch(path: str) -> None:
            self._remote_path = path
            if self._on_fetch_file:
                self._on_fetch_file(agent.id, path)

        def on_preview(path: str) -> None:
            self._remote_path = path
            if self._on_fs_preview:
                self._select_tab("tasks")
                self._on_fs_preview(agent.id, path)

        def on_push(path: str) -> None:
            self._remote_path = path
            self._push_full(remote_override=path)

        def on_hidden(on: bool) -> None:
            self._show_hidden = on
            if self._remote_path or self._default_remote_path():
                self._browse_remote(path=self._remote_path or self._default_remote_path())

        def on_closed() -> None:
            self._explorer = None
            self.set_remote_status(
                "Window closed — select this tab or Open to launch.", ok=None
            )
            self._sync_work_tab_labels()

        def on_fs_index_start(roots: list[str] | None) -> None:
            if self._on_fs_index_start:
                self._on_fs_index_start(agent.id, roots)

        def on_fs_index_status() -> None:
            if self._on_fs_index_status:
                self._on_fs_index_status(agent.id)

        def on_fs_index_stop() -> None:
            if self._on_fs_index_stop:
                self._on_fs_index_stop(agent.id)

        def on_fs_search(query: str, opts: dict) -> None:
            if self._on_fs_search:
                self._on_fs_search(agent.id, query, opts)

        start = self._remote_path or self._default_remote_path()
        # Prefer rich OS signal for Quick access (Windows vs Linux)
        os_hint = " ".join(
            p
            for p in (
                agent.os or "",
                agent.arch or "",
                "windows" if self._remote_sep == "\\" else "",
                "windows"
                if (self._remote_path or "").startswith(("C:", "D:", "E:", "\\\\"))
                else "",
            )
            if p
        )
        win = RemoteFileExplorer(
            agent_label=label,
            agent_id=agent.id,
            os_hint=os_hint,
            user_hint=agent.username or "",
            initial_path=start,
            initial_entries=list(self._remote_entries),
            initial_parent=self._remote_parent,
            initial_sep=self._remote_sep,
            show_hidden=self._show_hidden,
            on_browse=on_browse,
            on_download=on_download,
            on_fetch=on_fetch,
            on_preview=on_preview,
            on_push=on_push,
            on_hidden_toggle=on_hidden,
            on_closed=on_closed,
            on_fs_index_start=on_fs_index_start,
            on_fs_index_status=on_fs_index_status,
            on_fs_index_stop=on_fs_index_stop,
            on_fs_search=on_fs_search,
        )
        parent = self.get_root()
        if isinstance(parent, Gtk.Window):
            win.set_transient_for(parent)
        self._explorer = win
        win.present()
        self.set_remote_status(
            f"File Explorer open · {label} · path {start or '—'}", ok=True
        )
        self._sync_work_tab_labels()
        if not self._remote_entries:
            self._browse_remote(path=start)

    def desktop_viewer_open(self) -> bool:
        """True while the Remote Viewer window is open for the current session."""
        return self._desktop_viewer is not None

    def attach_keepstream(self, client: Any) -> None:
        """Wire Keepstream Session client for input + status."""
        self._keepstream = client
        if self._desktop_viewer is not None:
            try:
                self._desktop_viewer.attach_keepstream(client)
            except Exception:
                pass

    def keepstream_active(self) -> bool:
        ks = self._keepstream
        return bool(ks is not None and getattr(ks, "connected", False))

    def shot_max_side(self) -> int:
        """Operator quality from open Remote Viewer, else last Capture setting."""
        if self._desktop_viewer is not None:
            try:
                side = int(self._desktop_viewer.current_max_side())
                self._shot_max_side = side
                return side
            except Exception:
                pass
        try:
            return max(320, min(int(self._shot_max_side or 1920), 4096))
        except (TypeError, ValueError):
            return 1920

    def _require_desktop_viewer(self, action: str = "this action") -> bool:
        """Desktop capture/control only while Remote Viewer is open."""
        if self._desktop_viewer is not None:
            return True
        self.set_desktop_note(
            f"Open Remote Viewer first — {action} is not available on this page.",
            ok=False,
        )
        self.set_task_note("Open Remote Viewer first", ok=False)
        return False

    def _take_screenshot(self, max_side: int | None = None) -> None:
        """Capture only when Remote Viewer is open (called from the viewer)."""
        if not self._selected or not self._on_screenshot:
            return
        if not self._require_desktop_viewer("screenshot"):
            return
        side = int(max_side or self._shot_max_side or 1024)
        self._shot_max_side = side
        self.desktop_status.set_text(f"Capturing… (max_side={side})")
        try:
            self._on_screenshot(self._selected.id, max_side=side)
        except TypeError:
            # Older callback signature
            self._on_screenshot(self._selected.id)

    def _set_live(self, on: bool, *, from_viewer: bool = False) -> None:
        """Start/stop Live poll — only while Remote Viewer is open."""
        if on and not self._require_desktop_viewer("Live"):
            self._live_on = False
            return
        if not self._selected or not self._on_live_desktop:
            self._live_on = False
            return
        self._live_on = bool(on)
        self._on_live_desktop(self._selected.id, self._live_on)
        msg = "Live view on — polling screenshots" if self._live_on else "Live view off"
        self.desktop_status.set_text(msg)
        if self._desktop_viewer is None:
            return
        try:
            if not from_viewer:
                # Avoid recursive toggled → on_live → set_live_active loops
                self._desktop_viewer.set_live_active(self._live_on)
            self._desktop_viewer.set_status(msg, ok=True if self._live_on else None)
        except Exception:
            pass

    def _desktop_session(self, action: str, options: dict | None = None) -> None:
        if not self._selected or not self._on_desktop_session:
            return
        if not self._require_desktop_viewer("Stream"):
            return
        try:
            self._on_desktop_session(self._selected.id, action, options or {})
        except TypeError:
            self._on_desktop_session(self._selected.id, action)

    def _screenshot_archive_dir(self, agent_id: str, *, hostname: str = "") -> str:
        """Per-agent screenshot archive under ~/Pictures/Hogwarts/."""
        from pathlib import Path

        label = (hostname or agent_id or "agent").strip()
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:48]
        if not safe:
            safe = "agent"
        base = Path.home() / "Pictures" / "Hogwarts" / safe
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fall back to plugin data if Pictures is unavailable
            if self._data_dir:
                base = Path(self._data_dir) / "screenshots" / safe
            else:
                base = Path.home() / ".local" / "share" / "hogwarts" / "screenshots" / safe
            try:
                base.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        return str(base)

    def _open_desktop_viewer(self, *, auto_shot: bool = False) -> None:
        """Open (or focus) the large remote desktop viewer window.

        Stream / Watch / Control primary controls live inside this window.
        ``auto_shot=True`` queues a screenshot after the window is up (density).
        """
        if not self._selected:
            self.set_task_note("Select an agent first", ok=False)
            return
        if self._desktop_viewer is not None:
            try:
                self._desktop_viewer.present()
                self.set_desktop_note("Remote Viewer focused.", ok=True)
                self._sync_work_tab_labels()
                if auto_shot:
                    self._take_screenshot()
                return
            except Exception:
                self._desktop_viewer = None

        agent = self._selected
        label = agent.hostname or agent.id

        def on_shot(max_side: int) -> None:
            self._shot_max_side = max_side
            self._take_screenshot(max_side=max_side)

        def on_live(on: bool) -> None:
            self._set_live(bool(on), from_viewer=True)

        def on_session(action: str, options: dict | None = None) -> None:
            self._desktop_session(action, options)

        def on_input(events: list) -> None:
            if self._desktop_viewer is None:
                return
            # Prefer Keepstream stream path (no plane lag)
            ks = self._keepstream
            if ks is not None and getattr(ks, "connected", False):
                try:
                    ks.send_input(list(events or []))
                    return
                except Exception:
                    pass
            if self._on_desktop_input:
                self._on_desktop_input(agent.id, events)

        def on_socks() -> None:
            if self._desktop_viewer is None:
                return
            if self._on_socks_start:
                self._on_socks_start(agent.id)
            else:
                # Fallback: queue via task API if wired as generic task
                self._queue("socks_start", {"port": 0})

        def on_live_interval(sec: float) -> None:
            """sec may be fractional (Control uses 0.5)."""
            try:
                ms = int(float(sec) * 1000)
                self._live_interval_ms = max(250, min(ms, 10_000))
            except (TypeError, ValueError):
                self._live_interval_ms = 2000
            # Restart live loop so the new interval takes effect
            if self._live_on and self._on_live_desktop and self._selected:
                aid = self._selected.id
                self._on_live_desktop(aid, False)
                self._on_live_desktop(aid, True)

        def on_closed() -> None:
            was_live = self._live_on
            aid = self._selected.id if self._selected else None
            self._desktop_viewer = None
            self._live_interval_ms = 2000
            self._live_on = False
            ks = self._keepstream
            self._keepstream = None
            if ks is not None:
                try:
                    ks.stop()
                except Exception:
                    pass
            if was_live and aid and self._on_live_desktop:
                try:
                    self._on_live_desktop(aid, False)
                except Exception:
                    pass
            self.desktop_status.set_text(
                "Window closed — select this tab or Open to launch."
            )
            self.desktop_status.remove_css_class("hogwarts-ok")
            self.desktop_status.remove_css_class("hogwarts-fail")
            self._sync_work_tab_labels()

        archive = self._screenshot_archive_dir(
            agent.id, hostname=agent.hostname or ""
        )
        win = RemoteDesktopViewer(
            agent_label=label,
            agent_id=agent.id,
            archive_dir=archive,
            # Never seed a cached still — open blank until Stream/Capture
            initial_bytes=None,
            initial_note="",
            live_on=False,
            max_side=self._shot_max_side,
            on_screenshot=on_shot,
            on_live=on_live,
            on_session=on_session,
            on_desktop_input=on_input,
            on_socks_start=on_socks,
            on_live_interval=on_live_interval,
            on_closed=on_closed,
        )
        # Do NOT set_transient_for(Reach) — transient dialogs often cannot
        # maximize and stay stacked under the main window on some WMs.
        self._desktop_viewer = win
        self.desktop_status.set_text(
            "Remote Viewer open — Stream for live video, Control to interact."
        )
        self.desktop_status.remove_css_class("hogwarts-fail")
        self.desktop_status.add_css_class("hogwarts-ok")
        win.present()
        # Offer a maximized canvas by default (stream is the main use case)
        def _max_once() -> bool:
            try:
                win.maximize()
            except Exception:
                pass
            return False

        GLib.idle_add(_max_once)
        self._sync_work_tab_labels()
        if auto_shot:
            # Density: Open + Capture from Desktop tab
            def _shot_once() -> bool:
                try:
                    self._take_screenshot()
                except Exception:
                    pass
                return False

            GLib.idle_add(_shot_once)

    def set_desktop_frame(
        self,
        data: bytes,
        *,
        note: str = "",
        ok: bool | None = True,
        record_history: bool | None = None,
        pixel_format: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Cache frame and push into Remote Viewer window (only image UI).

        Frames never auto-write ~/Pictures — viewer "Save to disk" is opt-in.
        ``pixel_format``: ``jpeg`` (default) or ``rgb24`` (Keepstream H.264).
        """
        self._frame_bytes = data
        self._frame_note = note or f"Frame {len(data)} bytes"
        msg = self._frame_note
        self.desktop_status.set_text(msg)
        self.desktop_status.remove_css_class("hogwarts-ok")
        self.desktop_status.remove_css_class("hogwarts-fail")
        if ok is True:
            self.desktop_status.add_css_class("hogwarts-ok")
        elif ok is False:
            self.desktop_status.add_css_class("hogwarts-fail")
        # Default: never archive into sidebar/disk (viewer Save to disk is opt-in)
        if record_history is None:
            record_history = False
        # While Keepstream owns the surface, never push Capture/Live stills
        nu = msg.upper()
        looks_ks = (
            nu.startswith(("SESSION", "STREAM", "KEEPSTREAM"))
            or " · #" in msg
            or (pixel_format or "").lower() == "rgb24"
        )
        if self._desktop_viewer is not None:
            try:
                if (
                    getattr(self._desktop_viewer, "_frame_source", None)
                    == "keepstream"
                    and not looks_ks
                ):
                    return
            except Exception:
                pass
        # Frames only display in the Remote Viewer window (not embedded)
        if self._desktop_viewer is not None:
            try:
                self._desktop_viewer.apply_frame(
                    data,
                    note=msg,
                    ok=ok,
                    record_history=bool(record_history),
                    pixel_format=pixel_format,
                    width=width,
                    height=height,
                )
            except Exception:
                pass

    def set_desktop_note(self, msg: str, *, ok: bool | None = None) -> None:
        self.desktop_status.set_text(msg)
        self.desktop_status.remove_css_class("hogwarts-ok")
        self.desktop_status.remove_css_class("hogwarts-fail")
        if ok is True:
            self.desktop_status.add_css_class("hogwarts-ok")
        elif ok is False:
            self.desktop_status.add_css_class("hogwarts-fail")
        if self._desktop_viewer is not None:
            try:
                self._desktop_viewer.set_status(msg, ok=ok)
            except Exception:
                pass

    def set_desktop_session_info(self, info: dict) -> None:
        if self._desktop_viewer is not None:
            try:
                self._desktop_viewer.set_session_info(info)
            except Exception:
                pass

    def stop_live_ui(self) -> None:
        self._live_on = False
        if self._desktop_viewer is not None:
            try:
                self._desktop_viewer.set_live_active(False)
            except Exception:
                pass

    def _clear_list(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        self._fleet_rows.clear()

    def _clear_tasks(self) -> None:
        while child := self.task_list.get_first_child():
            self.task_list.remove(child)
        self._task_rows.clear()

    def _placeholder_label(self, msg: str, *, fail: bool) -> Gtk.Label:
        lab = Gtk.Label(label=msg, xalign=0, wrap=True)
        lab.add_css_class("hogwarts-fail" if fail else "hogwarts-muted")
        lab.set_margin_top(12)
        lab.set_margin_bottom(12)
        lab.set_margin_start(8)
        lab.set_margin_end(8)
        return lab

    def _row(self, agent: AgentDTO) -> Gtk.Widget:
        """One stable fleet button — created once per agent id, never reordered on poll."""
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("hogwarts-fleet-btn")
        btn.set_hexpand(True)
        btn.set_halign(Gtk.Align.FILL)
        # Don't leave keyboard focus on the roster row after click — focus would
        # jump into detail and highlight the whole identity card / stack page.
        btn.set_focus_on_click(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.add_css_class("hogwarts-agent-row")
        outer.set_hexpand(True)
        outer.set_can_target(False)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top.set_can_target(False)
        dot = Gtk.Box()
        dot.set_can_target(False)
        dot.add_css_class("hogwarts-dot")
        status = fleet_status_key(agent.status)
        mode = presence_mode_key(agent)
        if status in ("offline", "archived"):
            dot_class = "hogwarts-dot-off"
        elif mode == "interactive":
            dot_class = "hogwarts-dot-busy"
        elif status == "online":
            dot_class = "hogwarts-dot-live"
        else:
            dot_class = "hogwarts-dot-idle"
        dot.add_css_class(dot_class)
        dot.set_valign(Gtk.Align.CENTER)
        top.append(dot)

        host = Gtk.Label(label=agent.hostname or agent.id or "?", xalign=0)
        host.add_css_class("hogwarts-agent-host")
        host.set_hexpand(True)
        host.set_can_target(False)
        top.append(host)

        # Presence: ASYNC (beacon) / INTER (session-class turbo)
        mode_text = presence_label(mode) if status == "online" else ""
        mode_lab = Gtk.Label(label=mode_text, xalign=1)
        mode_lab.set_can_target(False)
        mode_lab.add_css_class("hogwarts-presence")
        if mode_text:
            mode_lab.add_css_class(
                "hogwarts-presence-interactive"
                if mode == "interactive"
                else "hogwarts-presence-async"
            )
        mode_lab.set_visible(bool(mode_text))
        mode_lab.set_tooltip_text(
            "Interactive — turbo sleep / desktop work pending"
            if mode == "interactive"
            else "Async — beacon-class check-in"
        )
        top.append(mode_lab)

        st_text = "LIVE" if status == "online" else status.upper()
        st = Gtk.Label(label=st_text, xalign=1)
        st.set_can_target(False)
        paint = (
            status
            if status in ("online", "offline", "archived")
            else "unknown"
        )
        st.add_css_class(f"hogwarts-status-{paint}")
        top.append(st)
        outer.append(top)

        age, _ = format_last_seen(agent.last_seen, sleep=agent.sleep)
        meta_bits = [
            agent.username,
            agent.os,
            agent.external_ip,
            agent.group,
            f"seen {age}",
            agent.package_id or "",
            agent.id,
        ]
        meta = " · ".join(b for b in meta_bits if b) or agent.id
        mlab = Gtk.Label(label=meta, xalign=0)
        mlab.add_css_class("hogwarts-agent-meta")
        mlab.set_can_target(False)
        outer.append(mlab)

        btn.set_child(outer)
        mode_tip = presence_label(mode).lower() if status == "online" else "off"
        tip0 = f"{agent.hostname or agent.id} · {st_text.lower()} · {mode_tip}"
        btn.set_tooltip_text(tip0)
        btn.connect("clicked", lambda *_a, aid=agent.id: self._open_agent_id(aid))

        # Explicit motion hover class — GTK4 :hover can stick off after many
        # refresh rebuilds / child label invalidations even with can_target=False.
        # enter(controller, x, y) — accept *args so signature never drops events.
        motion = Gtk.EventControllerMotion()

        def _enter(*_a: Any) -> None:
            btn.add_css_class("hogwarts-fleet-btn-hover")

        def _leave(*_a: Any) -> None:
            btn.remove_css_class("hogwarts-fleet-btn-hover")

        motion.connect("enter", _enter)
        motion.connect("leave", _leave)
        btn.add_controller(motion)

        self._fleet_rows[agent.id] = {
            "btn": btn,
            "host": host,
            "status": st,
            "presence": mode_lab,
            "meta": mlab,
            "dot": dot,
            "dot_class": dot_class,
            "tip": tip0,
            "agent": agent,
        }
        return btn

    def _open_agent_id(self, agent_id: str) -> None:
        """Resolve agent by id from current roster (survives soft patches)."""
        for a in self._agents:
            if a.id == agent_id:
                self._show_detail(a)
                return
        widgets = self._fleet_rows.get(agent_id)
        if widgets and widgets.get("agent"):
            self._show_detail(widgets["agent"])

    def _task_row(self, task: TaskDTO) -> Gtk.Widget:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("hogwarts-fleet-btn")
        btn.set_hexpand(True)
        lab = Gtk.Label(
            label=f"{task.status:10}  {task.type:6}  {task.id}",
            xalign=0,
        )
        lab.add_css_class("hogwarts-agent-meta")
        lab.set_selectable(True)
        lab.set_can_target(False)
        btn.set_child(lab)
        tid = task.id

        def on_click(*_a: Any, task_id: str = tid) -> None:
            widgets = self._task_rows.get(task_id)
            t = widgets["task"] if widgets else None
            if t is None:
                t = next((x for x in self._tasks if x.id == task_id), None)
            if t is None:
                return
            self._select_task(t)
            self._show_result(t)

        btn.connect("clicked", on_click)
        if tid:
            self._task_rows[tid] = {"btn": btn, "lab": lab, "task": task}
        return btn

    def _mark_task_selected(self, task_id: str | None) -> None:
        for tid, widgets in self._task_rows.items():
            btn: Gtk.Button = widgets["btn"]
            if task_id and tid == task_id:
                btn.add_css_class("hogwarts-fleet-btn-selected")
            else:
                btn.remove_css_class("hogwarts-fleet-btn-selected")

    def _select_task(self, task: TaskDTO) -> None:
        self._selected_task = task
        self._mark_task_selected(task.id if task else None)

    def _show_detail(self, agent: AgentDTO) -> None:
        """Open full-width agent detail sub-page."""
        try:
            self._fill_detail(agent)
            # Clear roster selection paint — detail is open; selected class on
            # an off-screen fleet button is useless and confuses focus styling.
            self._mark_fleet_selected(None)
            age, _ = format_last_seen(agent.last_seen, sleep=agent.sleep)
            self._selected_soft_key = (
                agent.id,
                fleet_status_key(agent.status),
                presence_mode_key(agent),
                age,
                agent.hostname,
                agent.username,
                agent.external_ip,
            )
            self._select_tab("tasking")
            try:
                self._view_stack.set_visible_child(self._detail_page)
            except Exception:
                self._view_stack.set_visible_child_name("detail")
            mode = presence_label(presence_mode_key(agent))
            self.status_lab.set_text(
                f"Opened {agent.hostname or agent.id} · {mode} · "
                "Tasking (inline result) · Files/Desktop windows · Tasks"
            )
            self._refresh_tasks()
            # Park focus on the shell entry without selecting its text, so the
            # detail page does not show a big focus/selection highlight.
            GLib.idle_add(self._focus_detail_quietly)
        except Exception as exc:
            self.status_lab.set_text(f"Failed to open agent: {exc}")
            self.set_task_note(str(exc), ok=False)

    def _focus_detail_quietly(self) -> bool:
        try:
            # Prefer shell box: natural next action, no full-page selection chrome
            if hasattr(self.shell_entry, "grab_focus_without_selecting"):
                self.shell_entry.grab_focus_without_selecting()
            else:
                self.shell_entry.grab_focus()
                try:
                    self.shell_entry.select_region(0, 0)
                except Exception:
                    pass
        except Exception:
            try:
                self.btn_back.grab_focus()
            except Exception:
                pass
        return False

    def _fill_detail(self, agent: AgentDTO, *, soft: bool = False) -> None:
        """Update detail header/meta without forcing fleet↔detail navigation.

        soft=True: poll-driven refresh — only update text, never reset paths/desktop.
        """
        prev_id = self._selected.id if self._selected else None
        self._selected = agent
        self._set_tasking_sensitive(True)
        self.detail_host.set_text(agent.hostname or agent.id or "Agent")
        age, wait_banner = format_last_seen(agent.last_seen, sleep=agent.sleep)
        iso = agent.last_seen.isoformat() if agent.last_seen else "—"
        status = (agent.status or "unknown").lower()
        mode = presence_mode_key(agent)
        sub_bits = [
            agent.username or None,
            (agent.os or "").split()[0] if agent.os else None,
            agent.arch or None,
            agent.external_ip or None,
            f"seen {age}",
            presence_label(mode) if fleet_status_key(status) == "online" else None,
        ]
        self.detail_sub.set_text(" · ".join(b for b in sub_bits if b))

        # Status chip + presence chip + header dot
        live = fleet_status_key(status)
        self.detail_status_chip.set_text(
            "LIVE" if live == "online" else status.upper()
        )
        for c in (
            "hogwarts-chip-live",
            "hogwarts-status-online",
            "hogwarts-status-idle",
            "hogwarts-status-offline",
            "hogwarts-status-archived",
            "hogwarts-status-unknown",
        ):
            self.detail_status_chip.remove_css_class(c)
        chip_cls = {
            "online": "hogwarts-status-online",
            "idle": "hogwarts-status-idle",
            "offline": "hogwarts-status-offline",
            "archived": "hogwarts-status-archived",
        }.get(status, "hogwarts-status-unknown")
        if live == "online":
            chip_cls = "hogwarts-status-online"
        self.detail_status_chip.add_css_class(chip_cls)
        if live == "online":
            self.detail_status_chip.add_css_class("hogwarts-chip-live")

        mode_txt = presence_label(mode) if live == "online" else ""
        self.detail_presence_chip.set_text(mode_txt)
        self.detail_presence_chip.set_visible(bool(mode_txt))
        for c in (
            "hogwarts-presence-async",
            "hogwarts-presence-interactive",
        ):
            self.detail_presence_chip.remove_css_class(c)
        if mode_txt:
            self.detail_presence_chip.add_css_class(
                "hogwarts-presence-interactive"
                if mode == "interactive"
                else "hogwarts-presence-async"
            )

        for c in (
            "hogwarts-dot-live",
            "hogwarts-dot-busy",
            "hogwarts-dot-off",
            "hogwarts-dot-idle",
        ):
            self.detail_dot.remove_css_class(c)
        if live in ("offline", "archived"):
            self.detail_dot.add_css_class("hogwarts-dot-off")
        elif mode == "interactive":
            self.detail_dot.add_css_class("hogwarts-dot-busy")
        elif live == "online":
            self.detail_dot.add_css_class("hogwarts-dot-live")
        else:
            self.detail_dot.add_css_class("hogwarts-dot-idle")

        lines = [
            f"id        {agent.id}",
            f"user      {agent.username or '—'}",
            f"os        {agent.os or '—'} {agent.arch or ''}".rstrip(),
            f"external  {agent.external_ip or '—'}",
            f"internal  {agent.internal_ip or '—'}",
            f"group     {agent.group or '—'}",
            f"package   {agent.package_id or '—'}",
            f"presence  {mode}  (async=beacon · interactive=session-class)",
            f"last_seen {age}  ({iso})",
        ]
        if agent.tags:
            lines.append(f"tags      {', '.join(str(t) for t in agent.tags)}")
        if agent.sleep is not None:
            lines.append(f"sleep     {agent.sleep}s  jitter {agent.jitter or 0}")
        if wait_banner:
            lines.append(f"tempo     {wait_banner}")
        elif agent.status == "archived":
            lines.append(
                "tempo     archived — long offline; re-enroll reuses this id"
            )
        elif agent.status == "offline":
            lines.append("tempo     offline — task waits for next check-in")
        elif mode == "interactive":
            lines.append(
                "tempo     interactive — turbo sleep or desktop work pending"
            )
        else:
            lines.append("tempo     async — beacon-class check-in")
        self.detail_body.set_text("\n".join(lines))
        if wait_banner and not soft:
            self.set_task_note(wait_banner, ok=None)
        os_s = (agent.os or "").lower()
        if soft:
            return
        if prev_id != agent.id:
            self.stop_live_ui()
            self.set_desktop_note("Open Remote Viewer to capture or go live.")
            if self._explorer is not None:
                try:
                    self._explorer.close()
                except Exception:
                    pass
                self._explorer = None
            if self._desktop_viewer is not None:
                try:
                    self._desktop_viewer.close()
                except Exception:
                    pass
                self._desktop_viewer = None
            self._frame_bytes = None
            self._frame_note = ""
            self._nav_history = []
            self._nav_index = -1
            self._nav_lock = False
            if "windows" in os_s:
                self.shell_dd.set_selected(5)  # cmd
            else:
                self.shell_dd.set_selected(0)  # auto
            if not self.restore_remote_view(agent.id):
                self._remote_path = (
                    "C:\\" if ("windows" in os_s or "nt" in os_s) else "/"
                )
                self.clear_remote_view(
                    "No folder listed yet — open File Explorer to start.",
                    forget_cache=False,
                )
                self._update_files_path_lab()

    def _show_result(self, task: TaskDTO) -> None:
        self._selected_task = task
        lines = [
            f"task   {task.id}",
            f"type   {task.type}",
            f"status {task.status}",
        ]
        if task.result:
            for k, v in task.result.items():
                # don't dump huge base64 into the pane
                if k == "data" and isinstance(v, str) and len(v) > 120:
                    lines.append(f"data: <base64 {len(v)} chars>")
                elif isinstance(v, str) and len(v) > 400:
                    lines.append(f"{k}: {v[:400]}…")
                elif isinstance(v, (dict, list)):
                    s = str(v)
                    lines.append(f"{k}: {s if len(s) < 300 else s[:297] + '…'}")
                else:
                    lines.append(f"{k}: {v}")
        else:
            lines.append("(no result yet — wait for agent check-in)")
        text = "\n".join(lines)
        # Skip rewrite if identical — reduces TextView churn on failed-task polls
        for view in (
            self.result_view,
            getattr(self, "tasking_result_view", None),
        ):
            if view is None:
                continue
            buf = view.get_buffer()
            start, end = buf.get_bounds()
            if buf.get_text(start, end, False) != text:
                buf.set_text(text)
        meta = getattr(self, "tasking_result_meta", None)
        if meta is not None:
            want = f"{task.type} · {task.status}"
            if meta.get_text() != want:
                meta.set_text(want)

    def _queue(self, type_: str, payload: dict) -> None:
        if not self._selected or not self._on_task:
            return
        self._on_task(self._selected.id, type_, payload)

    def _queue_shell(self) -> None:
        cmd = self.shell_entry.get_text().strip()
        if not cmd:
            self.set_task_note("Enter a shell command", ok=False)
            return
        self._queue(
            "shell",
            {
                "cmd": cmd,
                "shell": self.selected_shell(),
                "timeout_sec": 60,
            },
        )

    def _queue_note(self) -> None:
        self._queue("note", {"text": "operator note from Hogwarts"})

    def _queue_download(self) -> None:
        path = (self._remote_path or "").strip()
        if not path:
            self.set_task_note("Open File Explorer and select a path first", ok=False)
            return
        self._queue("download", {"path": path, "offset": 0, "length": 256000})

    def _fetch_full(self) -> None:
        path = (self._remote_path or "").strip()
        if not path:
            self.set_task_note("Open File Explorer and select a path first", ok=False)
            return
        if not self._selected or not self._on_fetch_file:
            return
        self._on_fetch_file(self._selected.id, path)

    def _push_full(self, remote_override: str | None = None) -> None:
        remote = (remote_override or self._remote_path or "").strip()
        if not remote:
            self.set_task_note("Open File Explorer and set a destination path", ok=False)
            return
        if not self._selected or not self._on_push_file:
            return
        agent_id = self._selected.id
        dialog = Gtk.FileDialog(title="File to push to agent")

        def on_open(dlg: Gtk.FileDialog, result) -> None:
            try:
                gfile = dlg.open_finish(result)
            except Exception:
                return
            if gfile is None:
                return
            local = gfile.get_path()
            if not local:
                self.set_task_note("Could not resolve local path", ok=False)
                return
            if self._on_push_file:
                self._on_push_file(agent_id, local, remote)

        parent = self.get_root()
        dialog.open(parent if isinstance(parent, Gtk.Window) else None, None, on_open)

    def _queue_upload_hello(self) -> None:
        import base64

        path = (self._remote_path or "").strip() or "/tmp/hogwarts-lab-hello.txt"
        data = base64.b64encode(b"hogwarts lab upload\n").decode("ascii")
        self._queue("upload", {"path": path, "data": data, "mode": "write"})

    def _cancel_selected(self) -> None:
        if not self._selected_task:
            self.set_task_note("Select a task in the list first", ok=False)
            return
        if self._selected_task.status not in ("queued", "assigned"):
            self.set_task_note(
                f"Cannot cancel status={self._selected_task.status}", ok=False
            )
            return
        if self._on_cancel_task:
            self._on_cancel_task(self._selected_task.id)

    def _refresh_tasks(self) -> None:
        if self._selected and self._on_refresh_tasks:
            self._on_refresh_tasks(self._selected.id)
