"""Remote filesystem explorer -- Windows File Explorer lookalike.

Light theme, compact Quick access, real themed icons (freedesktop /
Adwaita names that match Windows Explorer roles), details view,
double-click to open.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

# Icon pixel sizes (Windows Explorer details rows use ~16px)
_ICON_ROW = 16
_ICON_NAV = 16
_ICON_SIDE = 16
_ICON_RIBBON = 16


def _img(name: str, size: int = _ICON_ROW) -> Gtk.Image:
    """Themed icon image (falls back gracefully if missing)."""
    image = Gtk.Image.new_from_icon_name(name)
    image.set_pixel_size(size)
    image.add_css_class("wfe-icon")
    return image


def _fmt_size(size: Any, typ: str) -> str:
    if typ == "dir":
        return ""
    if not isinstance(size, int):
        return ""
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _fmt_mtime(ts: Any) -> str:
    try:
        if ts is None:
            return ""
        return datetime.fromtimestamp(int(ts)).strftime("%m/%d/%Y %I:%M %p")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _type_label(typ: str, name: str = "") -> str:
    if typ == "dir":
        return "File folder"
    if typ == "link":
        return "Shortcut"
    lower = (name or "").lower()
    ext_map = {
        ".txt": "Text Document",
        ".md": "Markdown File",
        ".log": "Log File",
        ".pdf": "PDF Document",
        ".png": "PNG Image",
        ".jpg": "JPEG Image",
        ".jpeg": "JPEG Image",
        ".gif": "GIF Image",
        ".bmp": "Bitmap Image",
        ".ico": "Icon",
        ".svg": "SVG Image",
        ".exe": "Application",
        ".msi": "Windows Installer Package",
        ".dll": "Application extension",
        ".zip": "Compressed (zipped) Folder",
        ".tar": "Archive",
        ".gz": "Archive",
        ".7z": "7-Zip Archive",
        ".rar": "WinRAR archive",
        ".py": "Python File",
        ".js": "JavaScript File",
        ".ts": "TypeScript File",
        ".json": "JSON File",
        ".xml": "XML Document",
        ".html": "HTML Document",
        ".htm": "HTML Document",
        ".css": "Cascading Style Sheet",
        ".cs": "C# Source File",
        ".bat": "Windows Batch File",
        ".ps1": "Windows PowerShell Script",
        ".cmd": "Windows Command Script",
        ".mp3": "MP3 File",
        ".wav": "Wave Sound",
        ".mp4": "MP4 Video",
        ".mkv": "MKV Video",
        ".doc": "Microsoft Word Document",
        ".docx": "Microsoft Word Document",
        ".xls": "Microsoft Excel Worksheet",
        ".xlsx": "Microsoft Excel Worksheet",
        ".ppt": "Microsoft PowerPoint Presentation",
        ".pptx": "Microsoft PowerPoint Presentation",
    }
    for ext, label in ext_map.items():
        if lower.endswith(ext):
            return label
    if "." in lower:
        return f"{lower.rsplit('.', 1)[-1].upper()} File"
    return "File"


def _icon_name_for(typ: str, name: str) -> str:
    """Map entry -> freedesktop icon name (Windows Explorer roles)."""
    if typ == "dir":
        return "folder"
    if typ == "link":
        return "emblem-symbolic-link"
    lower = (name or "").lower()
    if lower.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg", ".tif", ".tiff")
    ):
        return "image-x-generic"
    if lower.endswith((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma")):
        return "audio-x-generic"
    if lower.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv")):
        return "video-x-generic"
    if lower.endswith((".zip", ".tar", ".gz", ".7z", ".rar", ".tgz", ".bz2", ".xz")):
        return "package-x-generic"
    if lower.endswith((".exe", ".msi", ".com", ".dll", ".sys", ".scr")):
        return "application-x-executable"
    if lower.endswith((".pdf",)):
        return "application-pdf"
    if lower.endswith((".html", ".htm", ".css")):
        return "text-html"
    if lower.endswith((".doc", ".docx", ".odt", ".rtf")):
        return "x-office-document"
    if lower.endswith((".xls", ".xlsx", ".ods", ".csv")):
        return "x-office-spreadsheet"
    if lower.endswith((".ppt", ".pptx", ".odp")):
        return "x-office-presentation"
    if lower.endswith(
        (".py", ".js", ".ts", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".java")
    ):
        return "text-x-script"
    if lower.endswith((".txt", ".md", ".log", ".cfg", ".ini", ".conf", ".json", ".xml", ".yml", ".yaml")):
        return "text-x-generic"
    return "text-x-generic"


class RemoteFileExplorer(Gtk.Window):
    """Windows File Explorer-style remote directory browser."""

    def __init__(
        self,
        *,
        agent_label: str,
        agent_id: str,
        os_hint: str = "",
        user_hint: str = "",
        initial_path: str = "",
        initial_entries: list[dict[str, Any]] | None = None,
        initial_parent: str | None = None,
        initial_sep: str = "/",
        show_hidden: bool = False,
        on_browse: Callable[[str], None] | None = None,
        on_download: Callable[[str], None] | None = None,
        on_fetch: Callable[[str], None] | None = None,
        on_preview: Callable[[str], None] | None = None,
        on_push: Callable[[str], None] | None = None,
        on_hidden_toggle: Callable[[bool], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        get_root_window: Callable[[], Gtk.Window | None] | None = None,
        on_fs_index_start: Callable[[list[str] | None], None] | None = None,
        on_fs_index_status: Callable[[], None] | None = None,
        on_fs_index_stop: Callable[[], None] | None = None,
        on_fs_search: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(title=f"File Explorer - {agent_label}")
        self.set_default_size(980, 620)
        self.set_modal(False)
        self.add_css_class("wfe-win")

        self._agent_id = agent_id
        self._agent_label = agent_label
        self._os_hint = (os_hint or "").lower()
        self._user_hint = (user_hint or "").strip()
        self._on_browse = on_browse
        self._on_download = on_download
        self._on_fetch = on_fetch
        self._on_preview = on_preview
        self._on_push = on_push
        self._on_hidden_toggle = on_hidden_toggle
        self._on_closed = on_closed
        self._get_root_window = get_root_window
        self._on_fs_index_start = on_fs_index_start
        self._on_fs_index_status = on_fs_index_status
        self._on_fs_index_stop = on_fs_index_stop
        self._on_fs_search = on_fs_search
        self._search_hits: list[dict[str, Any]] = []
        self._index_state = "idle"

        self._path = initial_path or ""
        self._parent: str | None = initial_parent
        self._sep = initial_sep or "/"
        self._entries: list[dict[str, Any]] = list(initial_entries or [])
        self._show_hidden = show_hidden
        self._selected_ent: dict[str, Any] | None = None
        self._selected_row: Gtk.Widget | None = None
        self._filter = ""
        self._listing = False
        self._sort_key = "name"
        self._sort_asc = True
        self._history: list[str] = []
        self._hist_i = -1
        self._hist_lock = False
        # Track last sidebar family so we rebuild when OS detection flips
        self._sidebar_is_windows: bool | None = None
        self._side_box: Gtk.Box | None = None

        if self._path:
            self._history = [self._path]
            self._hist_i = 0

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("wfe")
        root.set_hexpand(True)
        root.set_vexpand(True)
        self.set_child(root)

        # ── Compact title strip ──────────────────────────────────────
        title_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_bar.add_css_class("wfe-titlebar")
        title_bar.append(_img("folder-open", 18))
        tcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        tcol.set_hexpand(True)
        t1 = Gtk.Label(label="File Explorer", xalign=0)
        t1.add_css_class("wfe-title")
        tcol.append(t1)
        fam0 = "Windows" if self._is_windows() else "Linux"
        self._subtitle = Gtk.Label(
            label=f"{agent_label}  ·  {fam0}  ·  remote",
            xalign=0,
        )
        self._subtitle.add_css_class("wfe-subtitle")
        tcol.append(self._subtitle)
        title_bar.append(tcol)
        root.append(title_bar)

        # ── Compact ribbon (icon + short label) ──────────────────────
        ribbon = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        ribbon.add_css_class("wfe-ribbon")

        self.btn_open = self._ribbon_btn("document-open", "Open", "Open (Enter / double-click)", primary=True)
        self.btn_open.connect("clicked", lambda *_: self._open_selected())
        ribbon.append(self.btn_open)

        self.btn_download = self._ribbon_btn("document-save", "Download", "Single-chunk download")
        self.btn_download.connect("clicked", lambda *_: self._act_download())
        ribbon.append(self.btn_download)

        self.btn_fetch = self._ribbon_btn("folder-download", "Fetch", "Multi-chunk fetch full file")
        self.btn_fetch.connect("clicked", lambda *_: self._act_fetch())
        ribbon.append(self.btn_fetch)

        self.btn_preview = self._ribbon_btn("document-open", "Preview", "Preview first chunk as text")
        self.btn_preview.connect("clicked", lambda *_: self._act_preview())
        ribbon.append(self.btn_preview)

        self.btn_push = self._ribbon_btn("document-send", "Push", "Upload local file to address path")
        self.btn_push.connect("clicked", lambda *_: self._act_push())
        ribbon.append(self.btn_push)

        self.btn_copy = self._ribbon_btn("edit-copy", "Copy path", "Copy path to clipboard")
        self.btn_copy.connect("clicked", lambda *_: self._copy_path())
        ribbon.append(self.btn_copy)

        sep_r = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep_r.set_margin_start(4)
        sep_r.set_margin_end(4)
        ribbon.append(sep_r)

        self.chk_hidden = Gtk.CheckButton(label="Hidden items")
        self.chk_hidden.add_css_class("wfe-check")
        self.chk_hidden.set_active(show_hidden)
        self.chk_hidden.set_tooltip_text("Show hidden / dotfiles")
        self.chk_hidden.connect("toggled", self._on_hidden)
        ribbon.append(self.chk_hidden)

        rsp = Gtk.Box()
        rsp.set_hexpand(True)
        ribbon.append(rsp)

        hint = Gtk.Label(label="Double-click to open", xalign=1)
        hint.add_css_class("wfe-hint")
        ribbon.append(hint)
        root.append(ribbon)

        # ── Nav + address + search ───────────────────────────────────
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        nav.add_css_class("wfe-nav")

        self.btn_back = self._icon_btn("go-previous", "Back (Alt+Left)")
        self.btn_back.connect("clicked", lambda *_: self._go_back())
        nav.append(self.btn_back)

        self.btn_forward = self._icon_btn("go-next", "Forward (Alt+Right)")
        self.btn_forward.connect("clicked", lambda *_: self._go_forward())
        nav.append(self.btn_forward)

        self.btn_up = self._icon_btn("go-up", "Up (Backspace)")
        self.btn_up.connect("clicked", lambda *_: self._go_up())
        nav.append(self.btn_up)

        self.btn_refresh = self._icon_btn("view-refresh", "Refresh (F5)")
        self.btn_refresh.connect("clicked", lambda *_: self._refresh())
        nav.append(self.btn_refresh)

        # Address bar
        addr_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        addr_wrap.add_css_class("wfe-address-wrap")
        addr_wrap.set_hexpand(True)
        addr_ico = _img("folder", 14)
        addr_ico.set_margin_start(4)
        addr_ico.set_margin_end(2)
        addr_wrap.append(addr_ico)
        self.address = Gtk.Entry()
        self.address.add_css_class("wfe-address")
        self.address.set_hexpand(True)
        self.address.set_placeholder_text("Address")
        self.address.set_text(self._path)
        self.address.connect("activate", lambda *_: self._go_address())
        addr_wrap.append(self.address)
        self.btn_go = self._icon_btn("go-jump", "Go")
        self.btn_go.add_css_class("wfe-go")
        self.btn_go.connect("clicked", lambda *_: self._go_address())
        addr_wrap.append(self.btn_go)
        nav.append(addr_wrap)

        # Search
        search_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        search_wrap.add_css_class("wfe-search-wrap")
        search_wrap.set_size_request(180, -1)
        search_ico = _img("system-search", 14)
        search_ico.set_margin_start(4)
        search_wrap.append(search_ico)
        self.filter_entry = Gtk.Entry()
        self.filter_entry.add_css_class("wfe-search")
        self.filter_entry.set_hexpand(True)
        self.filter_entry.set_placeholder_text("Filter this folder")
        self.filter_entry.set_tooltip_text(
            "Filters the current folder only. Use the Search tab for full-index (WizFile-style)."
        )
        self.filter_entry.connect("changed", self._on_filter_changed)
        search_wrap.append(self.filter_entry)
        nav.append(search_wrap)
        root.append(nav)

        # ── Mode: Browse | Search (index) ─────────────────────────────
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mode_row.add_css_class("wfe-mode-row")
        self._mode_browse = Gtk.ToggleButton(label="Browse")
        self._mode_browse.add_css_class("wfe-mode-btn")
        self._mode_browse.set_active(True)
        self._mode_search = Gtk.ToggleButton(label="Search (index)")
        self._mode_search.add_css_class("wfe-mode-btn")
        self._mode_search.set_group(self._mode_browse)
        self._mode_browse.connect(
            "toggled", lambda b: b.get_active() and self._set_mode("browse")
        )
        self._mode_search.connect(
            "toggled", lambda b: b.get_active() and self._set_mode("search")
        )
        mode_row.append(self._mode_browse)
        mode_row.append(self._mode_search)
        self.index_status_lab = Gtk.Label(
            label="Index: idle — build once for full-volume search",
            xalign=0,
        )
        self.index_status_lab.add_css_class("wfe-index-status")
        self.index_status_lab.set_hexpand(True)
        self.index_status_lab.set_ellipsize(Pango.EllipsizeMode.END)
        mode_row.append(self.index_status_lab)
        root.append(mode_row)

        # ── Body ─────────────────────────────────────────────────────
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)
        body.add_css_class("wfe-body")

        # Compact Quick access (narrow) — OS-aware (Windows vs Linux)
        side_scroll = Gtk.ScrolledWindow()
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroll.set_size_request(158, -1)
        side_scroll.set_hexpand(False)
        side_scroll.add_css_class("wfe-sidebar-scroll")
        self._side_scroll = side_scroll

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        side.add_css_class("wfe-sidebar")
        side.set_margin_top(4)
        side.set_margin_bottom(4)
        side.set_margin_start(2)
        side.set_margin_end(2)
        self._side_box = side
        self._rebuild_sidebar()
        side_scroll.set_child(side)
        body.append(side_scroll)

        vdiv = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        vdiv.add_css_class("wfe-vdiv")
        body.append(vdiv)

        # Details pane
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_hexpand(True)
        main.set_vexpand(True)
        main.add_css_class("wfe-main")

        headers = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        headers.add_css_class("wfe-headers")
        self._header_labs: dict[str, Gtk.Label] = {}
        for key, text, expand, width in (
            ("name", "Name", True, 260),
            ("mtime", "Date modified", False, 148),
            ("type", "Type", False, 150),
            ("size", "Size", False, 88),
        ):
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("wfe-col-head-btn")
            if expand:
                btn.set_hexpand(True)
            else:
                btn.set_size_request(width, -1)
            lab = Gtk.Label(label=text, xalign=0)
            lab.add_css_class("wfe-col-head")
            lab.set_hexpand(expand)
            btn.set_child(lab)
            btn.connect("clicked", lambda *_a, k=key: self._sort_by(k))
            headers.append(btn)
            self._header_labs[key] = lab
        main.append(headers)

        hdiv = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        hdiv.add_css_class("wfe-hdiv")
        main.append(hdiv)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.list_box.set_hexpand(True)
        self.list_box.add_css_class("wfe-list")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(self.list_box)
        scroll.add_css_class("wfe-scroll")
        main.append(scroll)

        # Search (index) pane — WizFile-style
        search_main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        search_main.set_hexpand(True)
        search_main.set_vexpand(True)
        search_main.add_css_class("wfe-main")

        s_tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        s_tools.add_css_class("wfe-search-tools")
        s_tools.set_margin_top(6)
        s_tools.set_margin_bottom(6)
        s_tools.set_margin_start(8)
        s_tools.set_margin_end(8)

        self.search_query = Gtk.Entry()
        self.search_query.set_hexpand(True)
        self.search_query.set_placeholder_text(
            "Search index… e.g. invoice*.pdf  or  report.docx"
        )
        self.search_query.set_tooltip_text(
            "Queries the agent-local path index (not the current folder). "
            "Build the index first. Supports * wildcards."
        )
        self.search_query.connect("activate", lambda *_: self._run_index_search())
        s_tools.append(self.search_query)

        self.btn_search_go = Gtk.Button(label="Search")
        self.btn_search_go.add_css_class("suggested-action")
        self.btn_search_go.connect("clicked", lambda *_: self._run_index_search())
        s_tools.append(self.btn_search_go)

        self.btn_index_build = Gtk.Button(label="Build index")
        self.btn_index_build.add_css_class("flat")
        self.btn_index_build.set_tooltip_text(
            "Start agent-side walk index (fs_index_start). "
            "Windows may later use MFT/Everything."
        )
        self.btn_index_build.connect("clicked", lambda *_: self._index_build())
        s_tools.append(self.btn_index_build)

        self.btn_index_status = Gtk.Button(label="Status")
        self.btn_index_status.add_css_class("flat")
        self.btn_index_status.connect("clicked", lambda *_: self._index_poll())
        s_tools.append(self.btn_index_status)

        self.btn_index_stop = Gtk.Button(label="Stop")
        self.btn_index_stop.add_css_class("flat")
        self.btn_index_stop.connect("clicked", lambda *_: self._index_stop())
        s_tools.append(self.btn_index_stop)
        search_main.append(s_tools)

        s_hint = Gtk.Label(
            label=(
                "Index stays on the agent. Desk only sends fs_search queries. "
                "Double-click a hit to open its folder (dirs) or select the file."
            ),
            xalign=0,
            wrap=True,
        )
        s_hint.add_css_class("wfe-hint")
        s_hint.set_margin_start(10)
        s_hint.set_margin_end(10)
        s_hint.set_margin_bottom(4)
        search_main.append(s_hint)

        s_headers = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        s_headers.add_css_class("wfe-headers")
        for text, expand, width in (
            ("Name", True, 220),
            ("Path", True, 320),
            ("Type", False, 100),
            ("Size", False, 88),
        ):
            lab = Gtk.Label(label=text, xalign=0)
            lab.add_css_class("wfe-col-head")
            if expand:
                lab.set_hexpand(True)
            else:
                lab.set_size_request(width, -1)
            lab.set_margin_start(6)
            s_headers.append(lab)
        search_main.append(s_headers)

        self.search_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.search_list.add_css_class("wfe-list")
        s_scroll = Gtk.ScrolledWindow()
        s_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        s_scroll.set_vexpand(True)
        s_scroll.set_hexpand(True)
        s_scroll.set_child(self.search_list)
        s_scroll.add_css_class("wfe-scroll")
        search_main.append(s_scroll)

        self._content_stack = Gtk.Stack()
        self._content_stack.set_hexpand(True)
        self._content_stack.set_vexpand(True)
        self._content_stack.add_named(main, "browse")
        self._content_stack.add_named(search_main, "search")
        self._content_stack.set_visible_child_name("browse")
        body.append(self._content_stack)
        root.append(body)

        # Status bar
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_bar.add_css_class("wfe-statusbar")
        self.status = Gtk.Label(label="Ready", xalign=0)
        self.status.add_css_class("wfe-status")
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        status_bar.append(self.status)
        self.count_lab = Gtk.Label(label="0 items", xalign=1)
        self.count_lab.add_css_class("wfe-count")
        status_bar.append(self.count_lab)
        root.append(status_bar)

        # Pull index status once when window opens
        if self._on_fs_index_status:
            GLib.idle_add(lambda: (self._index_poll(), False)[1])

        # Keys
        key = Gtk.EventControllerKey()

        def on_key(
            _c: Gtk.EventControllerKey,
            keyval: int,
            _keycode: int,
            state: Gdk.ModifierType,
        ) -> bool:
            alt = bool(state & Gdk.ModifierType.ALT_MASK)
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._open_selected()
                return True
            if keyval == Gdk.KEY_BackSpace and not ctrl:
                self._go_up()
                return True
            if keyval == Gdk.KEY_F5:
                self._refresh()
                return True
            if keyval == Gdk.KEY_Left and alt:
                self._go_back()
                return True
            if keyval == Gdk.KEY_Right and alt:
                self._go_forward()
                return True
            if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
                self._copy_path()
                return True
            if keyval == Gdk.KEY_Escape:
                self._clear_selection()
                return True
            return False

        key.connect("key-pressed", on_key)
        self.add_controller(key)
        self.connect("close-request", self._on_close_request)

        self._rebuild_list()
        self._update_nav_sensitive()
        self._update_header_labels()
        if self._path and not self._entries:
            GLib.idle_add(lambda: (self._browse(self._path), False)[1])

    # ── Widget helpers ───────────────────────────────────────────────

    def _icon_btn(self, icon: str, tip: str) -> Gtk.Button:
        b = Gtk.Button()
        b.set_child(_img(icon, _ICON_NAV))
        b.add_css_class("flat")
        b.add_css_class("wfe-nav-btn")
        b.set_tooltip_text(tip)
        return b

    def _ribbon_btn(
        self, icon: str, label: str, tip: str, *, primary: bool = False
    ) -> Gtk.Button:
        b = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.append(_img(icon, _ICON_RIBBON))
        lab = Gtk.Label(label=label)
        lab.add_css_class("wfe-ribbon-label")
        box.append(lab)
        b.set_child(box)
        b.add_css_class("wfe-ribbon-btn")
        if primary:
            b.add_css_class("wfe-ribbon-primary")
        b.set_tooltip_text(tip)
        return b

    def _side_header(self, text: str) -> Gtk.Widget:
        lab = Gtk.Label(label=text, xalign=0)
        lab.add_css_class("wfe-side-header")
        lab.set_margin_start(6)
        lab.set_margin_top(6)
        lab.set_margin_bottom(2)
        return lab

    def _side_item(self, label: str, path: str, icon: str) -> Gtk.Widget:
        b = Gtk.Button()
        b.add_css_class("flat")
        b.add_css_class("wfe-side-btn")
        b.set_halign(Gtk.Align.FILL)
        b.set_tooltip_text(path)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_margin_start(2)
        row.append(_img(icon, _ICON_SIDE))
        lab = Gtk.Label(label=label, xalign=0)
        lab.add_css_class("wfe-side-label")
        lab.set_hexpand(True)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        row.append(lab)
        b.set_child(row)
        b.connect("clicked", lambda *_a, p=path: self._browse(p))
        return b

    # ── Public API ───────────────────────────────────────────────────

    def apply_listing(
        self,
        path: str,
        entries: list[dict[str, Any]],
        *,
        parent: str | None = None,
        sep: str = "/",
        note: str | None = None,
        ok: bool | None = True,
        record_history: bool = True,
    ) -> None:
        self._listing = False
        prev = self._path
        self._path = path or ""
        self._parent = parent
        self._sep = sep or "/"
        self._entries = list(entries or [])
        self._clear_selection()
        if self._path:
            self.address.set_text(self._path)
        if self._hist_lock:
            self._hist_lock = False
        elif ok is not False and self._path and record_history:
            self._push_history(self._path)
        n = len(self._entries)
        msg = note or (self._path or "Ready")
        self._set_status(msg, ok=ok if ok is not None else True)
        self.count_lab.set_text(f"{n} item{'s' if n != 1 else ''}")
        self._rebuild_list()
        self._update_nav_sensitive()
        # Path/sep may reveal Windows vs Linux when agent.os was vague
        self._rebuild_sidebar(force=False)
        if prev != self._path:
            short = self._path if len(self._path) < 56 else ("..." + self._path[-53:])
            self.set_title(f"File Explorer - {short}")
            fam = "Windows" if self._is_windows() else "Linux"
            self._subtitle.set_text(f"{self._agent_label}  ·  {fam}  ·  {self._path}")

    def set_status(self, msg: str, *, ok: bool | None = None) -> None:
        self._set_status(msg, ok=ok)

    def mark_listing(self, path: str) -> None:
        self._listing = True
        target = path.strip() or "(default)"
        self._set_status(f"Working on it...  {target}", ok=None)

    def set_show_hidden(self, on: bool) -> None:
        if bool(self.chk_hidden.get_active()) != bool(on):
            self.chk_hidden.set_active(bool(on))

    # ── Internals ────────────────────────────────────────────────────

    def _is_windows(self) -> bool:
        """Detect remote Windows from agent OS string, path sep, or drive path."""
        h = (self._os_hint or "").lower().strip()
        # Common agent reports: "Windows 10", "Windows-10-...", "Microsoft Windows",
        # platform.system() → "Windows"; .NET may send "Win32NT", "win10", etc.
        win_tokens = (
            "windows",
            "win32",
            "win64",
            "winnt",
            "win10",
            "win11",
            "microsoft",
            "cygwin",
            "msys",
            "mingw",
        )
        if h in ("nt", "win", "windows"):
            return True
        if any(t in h for t in win_tokens):
            return True
        # Explicit non-Windows
        if any(t in h for t in ("linux", "darwin", "macos", "freebsd", "openbsd", "unix")):
            return False
        sep = self._sep or ""
        if sep == "\\" or sep == "\\\\":
            return True
        p = self._path or ""
        # Drive letter: C:\ or C:/
        if len(p) >= 2 and p[1] == ":" and (p[0].isalpha()):
            return True
        if p.startswith("\\\\"):  # UNC \\server\share
            return True
        return False

    def _win_user_root(self) -> str:
        """Best-effort Windows user profile path for Quick access."""
        user = self._user_hint
        # Strip domain prefix DOMAIN\user
        if "\\" in user:
            user = user.rsplit("\\", 1)[-1]
        if "/" in user:
            user = user.rsplit("/", 1)[-1]
        user = user.strip()
        if user and user not in (".", ".."):
            return f"C:\\Users\\{user}"
        return "~"

    def _win_join(self, *parts: str) -> str:
        bits = [p.strip("\\/") for p in parts if p]
        if not bits:
            return "C:\\"
        out = bits[0]
        if len(out) == 2 and out[1] == ":":
            out = out + "\\"
        for b in bits[1:]:
            if out.endswith("\\"):
                out = out + b
            else:
                out = out + "\\" + b
        return out

    def _sidebar_entries(self) -> list[tuple[str, str, str]]:
        """Quick access shortcuts — Windows profile vs Linux home."""
        if self._is_windows():
            prof = self._win_user_root()
            # Prefer explicit profile paths (work when ~ expansion differs)
            # and keep ~ variants as agent expanduser fallbacks.
            return [
                ("Desktop", self._win_join(prof, "Desktop"), "user-desktop"),
                ("Downloads", self._win_join(prof, "Downloads"), "folder-download"),
                ("Documents", self._win_join(prof, "Documents"), "folder-documents"),
                ("Pictures", self._win_join(prof, "Pictures"), "folder-pictures"),
                ("Videos", self._win_join(prof, "Videos"), "folder-videos"),
                ("Music", self._win_join(prof, "Music"), "folder-music"),
                ("OneDrive", self._win_join(prof, "OneDrive"), "folder-remote"),
                ("User profile", prof if prof != "~" else "~", "user-home"),
            ]
        # Linux / Unix
        return [
            ("Home", "~", "user-home"),
            ("Desktop", "~/Desktop", "user-desktop"),
            ("Downloads", "~/Downloads", "folder-download"),
            ("Documents", "~/Documents", "folder-documents"),
            ("Pictures", "~/Pictures", "folder-pictures"),
            ("Videos", "~/Videos", "folder-videos"),
            ("tmp", "/tmp", "folder"),
        ]

    def _this_pc_entries(self) -> list[tuple[str, str, str]]:
        """This PC / Computer roots."""
        if self._is_windows():
            entries = [
                ("Local Disk (C:)", "C:\\", "drive-harddisk"),
                ("Local Disk (D:)", "D:\\", "drive-harddisk"),
                ("Local Disk (E:)", "E:\\", "drive-harddisk"),
                ("Users", "C:\\Users", "folder"),
                ("Program Files", "C:\\Program Files", "folder"),
                ("Program Files (x86)", "C:\\Program Files (x86)", "folder"),
                ("Windows", "C:\\Windows", "folder"),
                ("Temp", "C:\\Windows\\Temp", "folder"),
            ]
            # User-specific under Users if we know the name
            user = self._user_hint
            if "\\" in user:
                user = user.rsplit("\\", 1)[-1]
            if user and user not in (".", ".."):
                entries.insert(4, (user, f"C:\\Users\\{user}", "user-home"))
            return entries
        return [
            ("Computer", "/", "computer"),
            ("Root (/)", "/", "drive-harddisk"),
            ("/home", "/home", "folder"),
            ("/var", "/var", "folder"),
            ("/etc", "/etc", "folder"),
            ("/opt", "/opt", "folder"),
            ("/usr", "/usr", "folder"),
            ("/tmp", "/tmp", "folder"),
        ]

    def _rebuild_sidebar(self, *, force: bool = True) -> None:
        """Fill Quick access + This PC for the detected remote OS."""
        if self._side_box is None:
            return
        is_win = self._is_windows()
        if not force and self._sidebar_is_windows is is_win:
            return
        self._sidebar_is_windows = is_win
        side = self._side_box
        while child := side.get_first_child():
            side.remove(child)

        side.append(self._side_header("Quick access"))
        for label, path, icon in self._sidebar_entries():
            side.append(self._side_item(label, path, icon))

        # Windows Explorer says "This PC"; Linux feel = "Computer"
        side.append(self._side_header("This PC" if is_win else "Computer"))
        for label, path, icon in self._this_pc_entries():
            side.append(self._side_item(label, path, icon))

        # Footer hint so operator knows which family we chose
        fam = Gtk.Label(
            label="Windows paths" if is_win else "Linux paths",
            xalign=0,
        )
        fam.add_css_class("wfe-side-footer")
        fam.set_margin_start(6)
        fam.set_margin_top(8)
        side.append(fam)

    def _set_status(self, msg: str, *, ok: bool | None = None) -> None:
        self.status.set_text(msg)
        self.status.remove_css_class("wfe-status-ok")
        self.status.remove_css_class("wfe-status-fail")
        if ok is True:
            self.status.add_css_class("wfe-status-ok")
        elif ok is False:
            self.status.add_css_class("wfe-status-fail")

    def _push_history(self, path: str) -> None:
        if not path:
            return
        if self._hist_i >= 0 and self._hist_i < len(self._history) - 1:
            self._history = self._history[: self._hist_i + 1]
        if self._history and self._history[-1] == path:
            self._hist_i = len(self._history) - 1
            return
        self._history.append(path)
        if len(self._history) > 80:
            self._history = self._history[-80:]
        self._hist_i = len(self._history) - 1

    def _update_nav_sensitive(self) -> None:
        self.btn_back.set_sensitive(self._hist_i > 0)
        self.btn_forward.set_sensitive(
            self._hist_i >= 0 and self._hist_i < len(self._history) - 1
        )
        self.btn_up.set_sensitive(bool(self._parent or self._path))

    def _go_back(self) -> None:
        if self._hist_i <= 0:
            return
        self._hist_i -= 1
        self._hist_lock = True
        self._browse(self._history[self._hist_i])
        self._update_nav_sensitive()

    def _go_forward(self) -> None:
        if self._hist_i < 0 or self._hist_i >= len(self._history) - 1:
            return
        self._hist_i += 1
        self._hist_lock = True
        self._browse(self._history[self._hist_i])
        self._update_nav_sensitive()

    def _go_up(self) -> None:
        if self._parent:
            self._browse(self._parent)
            return
        cur = self.address.get_text().strip() or self._path
        if not cur:
            return
        sep = self._sep or "/"
        stripped = cur.rstrip(sep)
        if sep not in stripped:
            parent = (
                sep
                if sep == "/"
                else (
                    stripped + sep
                    if len(stripped) == 2 and stripped[1] == ":"
                    else sep
                )
            )
            self._browse(parent)
            return
        parent = stripped.rsplit(sep, 1)[0]
        if not parent:
            parent = sep
        if len(parent) == 2 and parent[1] == ":":
            parent = parent + sep
        self._browse(parent)

    def _go_address(self) -> None:
        self._browse(self.address.get_text().strip())

    def _refresh(self) -> None:
        self._browse(self._path or self.address.get_text().strip())

    def _browse(self, path: str) -> None:
        if self._on_browse:
            self.mark_listing(path or "(default)")
            self._on_browse(path)

    def _on_hidden(self, *_a) -> None:
        self._show_hidden = bool(self.chk_hidden.get_active())
        if self._on_hidden_toggle:
            self._on_hidden_toggle(self._show_hidden)

    def _on_filter_changed(self, *_a) -> None:
        self._filter = self.filter_entry.get_text().strip().lower()
        self._rebuild_list()

    def _join(self, base: str, name: str) -> str:
        sep = self._sep or "/"
        if not base:
            return name
        if base.endswith(sep) or (len(base) == 2 and base[1] == ":"):
            return f"{base}{name}" if base.endswith(sep) else f"{base}{sep}{name}"
        return f"{base}{sep}{name}"

    def _sort_by(self, key: str) -> None:
        if self._sort_key == key:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_key = key
            self._sort_asc = True
        self._update_header_labels()
        self._rebuild_list()

    def _update_header_labels(self) -> None:
        names = {
            "name": "Name",
            "mtime": "Date modified",
            "type": "Type",
            "size": "Size",
        }
        for k, base in names.items():
            lab = self._header_labs.get(k)
            if not lab:
                continue
            if k == self._sort_key:
                lab.set_text(f"{base} {'^' if self._sort_asc else 'v'}")
            else:
                lab.set_text(base)

    def _sorted_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rev = not self._sort_asc
        key = self._sort_key

        def sort_key(e: dict[str, Any]) -> tuple:
            typ = str(e.get("type") or "file")
            is_dir = 0 if typ == "dir" else 1
            name = str(e.get("name") or "").lower()
            if key == "name":
                return (is_dir, name)
            if key == "mtime":
                return (is_dir, int(e.get("mtime") or 0), name)
            if key == "size":
                return (is_dir, int(e.get("size") or 0), name)
            if key == "type":
                return (is_dir, _type_label(typ, str(e.get("name") or "")), name)
            return (is_dir, name)

        return sorted(entries, key=sort_key, reverse=rev)

    def _filtered_entries(self) -> list[dict[str, Any]]:
        items = list(self._entries)
        if self._filter:
            items = [
                e for e in items if self._filter in str(e.get("name") or "").lower()
            ]
        return self._sorted_entries(items)

    def _clear_selection(self) -> None:
        if self._selected_row is not None:
            self._selected_row.remove_css_class("wfe-row-selected")
        self._selected_row = None
        self._selected_ent = None

    def _select_row(self, row: Gtk.Widget, ent: dict[str, Any]) -> None:
        if self._selected_row is not None:
            self._selected_row.remove_css_class("wfe-row-selected")
        self._selected_row = row
        self._selected_ent = ent
        row.add_css_class("wfe-row-selected")

    def _rebuild_list(self) -> None:
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        self._selected_row = None
        entries = self._filtered_entries()
        if not entries:
            empty = Gtk.Label(
                label=(
                    "This folder is empty."
                    if self._entries
                    else "Type a path and press Enter, or pick a location on the left."
                ),
                xalign=0,
            )
            empty.add_css_class("wfe-empty")
            empty.set_margin_top(20)
            empty.set_margin_start(12)
            self.list_box.append(empty)
            self.count_lab.set_text("0 items")
            return
        for ent in entries:
            self.list_box.append(self._row(ent))
        shown = len(entries)
        total = len(self._entries)
        if self._filter and shown != total:
            self.count_lab.set_text(f"{shown} of {total} items")
        else:
            self.count_lab.set_text(f"{shown} item{'s' if shown != 1 else ''}")

    def _row(self, ent: dict[str, Any]) -> Gtk.Widget:
        name = str(ent.get("name") or "")
        typ = str(ent.get("type") or "file")
        icon_name = _icon_name_for(typ, name)
        size_s = _fmt_size(ent.get("size"), typ)
        mtime_s = _fmt_mtime(ent.get("mtime"))
        type_s = _type_label(typ, name)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.add_css_class("wfe-row")
        row.set_hexpand(True)
        row.set_overflow(Gtk.Overflow.HIDDEN)

        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_box.set_hexpand(True)
        name_box.set_size_request(260, -1)
        name_box.set_margin_start(6)
        name_box.append(_img(icon_name, _ICON_ROW))
        name_lab = Gtk.Label(label=name, xalign=0)
        name_lab.add_css_class("wfe-name")
        name_lab.set_hexpand(True)
        name_lab.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.append(name_lab)
        row.append(name_box)

        for text, width, css in (
            (mtime_s or " ", 148, "wfe-cell"),
            (type_s, 150, "wfe-cell"),
            (size_s or " ", 88, "wfe-cell-num"),
        ):
            lab = Gtk.Label(label=text, xalign=0)
            lab.add_css_class(css)
            lab.set_size_request(width, -1)
            lab.set_ellipsize(Pango.EllipsizeMode.END)
            row.append(lab)

        tip = f"{name}\nType: {type_s}"
        if size_s:
            tip += f"\nSize: {size_s}"
        if mtime_s:
            tip += f"\nModified: {mtime_s}"
        if ent.get("target"):
            tip += f"\nTarget: {ent.get('target')}"
        row.set_tooltip_text(tip)

        gesture = Gtk.GestureClick()
        gesture.set_button(1)

        def on_pressed(
            _g: Gtk.GestureClick, n_press: int, _x: float, _y: float, e=ent, r=row
        ) -> None:
            self._select_row(r, e)
            name_ = str(e.get("name") or "")
            full = self._join(self._path, name_)
            self.address.set_text(full)
            if n_press == 1:
                self._set_status(
                    f"{name_}  ({_type_label(str(e.get('type') or ''), name_)})",
                    ok=True,
                )
            elif n_press >= 2:
                self._open_entry(e)

        gesture.connect("pressed", on_pressed)
        row.add_controller(gesture)

        rclick = Gtk.GestureClick()
        rclick.set_button(3)

        def on_right(
            _g: Gtk.GestureClick, _n: int, _x: float, _y: float, e=ent, r=row
        ) -> None:
            self._select_row(r, e)
            full = self._join(self._path, str(e.get("name") or ""))
            self.address.set_text(full)
            self._set_status(f"Selected: {full}", ok=True)

        rclick.connect("pressed", on_right)
        row.add_controller(rclick)
        return row

    def _open_entry(self, ent: dict[str, Any]) -> None:
        name = str(ent.get("name") or "")
        typ = str(ent.get("type") or "file")
        full = self._join(self._path, name)
        self.address.set_text(full)
        self._selected_ent = ent
        if typ in ("dir", "link"):
            self._browse(full)
        else:
            self._set_status(
                f"{name}  -  use Download / Fetch / Preview on the ribbon",
                ok=True,
            )

    def _open_selected(self) -> None:
        if self._selected_ent:
            self._open_entry(self._selected_ent)
            return
        self._go_address()

    def _selected_path(self) -> str:
        if self._selected_ent and self._path:
            return self._join(self._path, str(self._selected_ent.get("name") or ""))
        return self.address.get_text().strip()

    def _act_download(self) -> None:
        path = self._selected_path()
        if not path:
            self._set_status("Select a file first", ok=False)
            return
        if self._on_download:
            self._on_download(path)
            self._set_status(f"Download queued: {path}", ok=True)

    def _act_fetch(self) -> None:
        path = self._selected_path()
        if not path:
            self._set_status("Select a file first", ok=False)
            return
        if self._on_fetch:
            self._on_fetch(path)
            self._set_status(f"Fetch full queued: {path}", ok=True)

    def _act_preview(self) -> None:
        path = self._selected_path()
        if not path:
            self._set_status("Select a file first", ok=False)
            return
        if self._on_preview:
            self._on_preview(path)
            self._set_status(f"Preview queued: {path}", ok=True)

    def _act_push(self) -> None:
        path = self.address.get_text().strip() or self._path
        if not path:
            self._set_status("Set destination path in the address bar", ok=False)
            return
        if self._on_push:
            self._on_push(path)

    def _copy_path(self) -> None:
        path = self._selected_path()
        if not path:
            self._set_status("Nothing to copy", ok=False)
            return
        display = Gdk.Display.get_default()
        if display is None:
            self._set_status("No display for clipboard", ok=False)
            return
        display.get_clipboard().set(path)
        self._set_status(f"Copied path: {path}", ok=True)

    # ── Index search (WizFile-class) ─────────────────────────────────

    def _set_mode(self, mode: str) -> None:
        if mode == "search":
            self._content_stack.set_visible_child_name("search")
            self._index_poll()
            self.search_query.grab_focus()
        else:
            self._content_stack.set_visible_child_name("browse")

    def _index_build(self) -> None:
        if not self._on_fs_index_start:
            self._set_status("Index API not wired", ok=False)
            return
        self._set_status("Starting index build on agent…", ok=None)
        self.index_status_lab.set_text("Index: starting…")
        # Default roots: let agent choose; optional current path as extra root later
        self._on_fs_index_start(None)

    def _index_poll(self) -> None:
        if self._on_fs_index_status:
            self._on_fs_index_status()

    def _index_stop(self) -> None:
        if self._on_fs_index_stop:
            self._set_status("Stopping index…", ok=None)
            self._on_fs_index_stop()

    def _run_index_search(self) -> None:
        q = self.search_query.get_text().strip()
        if not q:
            self._set_status("Enter a search query", ok=False)
            return
        if not self._on_fs_search:
            self._set_status("Search API not wired", ok=False)
            return
        self._set_status(f"Searching index for {q!r}…", ok=None)
        self._on_fs_search(q, {"limit": 200, "offset": 0})

    def apply_index_status(self, status: dict[str, Any]) -> None:
        """Update index status line from fs_index_status / start / stop result."""
        state = str(status.get("state") or "idle")
        self._index_state = state
        progress = float(status.get("progress") or 0.0)
        count = int(status.get("count") or 0)
        engine = str(status.get("engine") or "walk")
        err = status.get("error")
        roots = status.get("roots") or []
        pct = int(progress * 100)
        if state == "building":
            msg = f"Index: building {pct}% · {count:,} paths · engine={engine}"
        elif state == "ready":
            msg = f"Index: ready · {count:,} paths · engine={engine}"
            if err == "truncated":
                msg += " (capped)"
            elif err == "stopped":
                msg += " (stopped early)"
        elif state == "stopping":
            msg = f"Index: stopping… · {count:,} paths"
        elif state == "error":
            msg = f"Index: error · {err or 'unknown'}"
        else:
            msg = f"Index: {state} · engine={engine}"
        if roots and state in ("building", "ready"):
            root_s = ", ".join(str(r) for r in roots[:3])
            if len(roots) > 3:
                root_s += f" +{len(roots) - 3}"
            msg += f" · roots: {root_s}"
        self.index_status_lab.set_text(msg)
        ok = True if state == "ready" else (False if state == "error" else None)
        self._set_status(msg, ok=ok)

    def apply_search_results(self, result: dict[str, Any]) -> None:
        """Render fs_search hits."""
        if result.get("error"):
            err = str(result.get("error"))
            hint = result.get("hint") or ""
            self._set_status(
                f"Search failed: {err}" + (f" — {hint}" if hint else ""),
                ok=False,
            )
            if err == "no_index":
                self.index_status_lab.set_text(
                    "Index: none — click Build index, wait for ready, then Search"
                )
            return
        hits = list(result.get("hits") or [])
        self._search_hits = hits
        total = int(result.get("total") or len(hits))
        q = result.get("query") or ""
        idx_state = result.get("index_state") or self._index_state
        while child := self.search_list.get_first_child():
            self.search_list.remove(child)
        if not hits:
            empty = Gtk.Label(
                label=f"No results for {q!r}",
                xalign=0,
            )
            empty.add_css_class("wfe-empty")
            empty.set_margin_top(16)
            empty.set_margin_start(12)
            self.search_list.append(empty)
        else:
            for hit in hits:
                self.search_list.append(self._search_row(hit))
        trunc = " (truncated)" if result.get("truncated") else ""
        self.count_lab.set_text(f"{len(hits)} of {total} hits{trunc}")
        self._set_status(
            f"Search {q!r}: {total} hit(s) · index={idx_state} · "
            f"engine={result.get('engine') or '?'}",
            ok=True,
        )
        # Switch to search mode so user sees results
        if not self._mode_search.get_active():
            self._mode_search.set_active(True)

    def _search_row(self, hit: dict[str, Any]) -> Gtk.Widget:
        name = str(hit.get("name") or "")
        path = str(hit.get("path") or "")
        typ = str(hit.get("type") or "file")
        size_s = _fmt_size(hit.get("size"), typ)
        type_s = _type_label(typ, name)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.add_css_class("wfe-row")
        row.set_hexpand(True)

        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_box.set_hexpand(True)
        name_box.set_size_request(200, -1)
        name_box.set_margin_start(6)
        name_box.append(_img(_icon_name_for(typ, name), _ICON_ROW))
        nlab = Gtk.Label(label=name, xalign=0)
        nlab.add_css_class("wfe-name")
        nlab.set_hexpand(True)
        nlab.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.append(nlab)
        row.append(name_box)

        plab = Gtk.Label(label=path, xalign=0)
        plab.add_css_class("wfe-cell")
        plab.set_hexpand(True)
        plab.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        plab.set_selectable(True)
        row.append(plab)

        for text, width in ((type_s, 100), (size_s or " ", 88)):
            lab = Gtk.Label(label=text, xalign=0)
            lab.add_css_class("wfe-cell")
            lab.set_size_request(width, -1)
            row.append(lab)

        row.set_tooltip_text(path)
        gesture = Gtk.GestureClick()
        gesture.set_button(1)

        def on_pressed(
            _g: Gtk.GestureClick, n_press: int, _x: float, _y: float, h=hit
        ) -> None:
            p = str(h.get("path") or "")
            t = str(h.get("type") or "file")
            self.address.set_text(p)
            self._selected_ent = {
                "name": h.get("name"),
                "type": t,
                "size": h.get("size"),
                "mtime": h.get("mtime"),
            }
            if n_press >= 2:
                if t == "dir":
                    self._set_mode("browse")
                    self._mode_browse.set_active(True)
                    self._browse(p)
                else:
                    # Open parent folder in browse mode
                    parent = str(Path(p).parent) if p else ""
                    if parent:
                        self._set_mode("browse")
                        self._mode_browse.set_active(True)
                        self._browse(parent)
                        self.address.set_text(p)
                    self._set_status(f"Selected {p}", ok=True)
            else:
                self._set_status(f"Selected {p}", ok=True)

        gesture.connect("pressed", on_pressed)
        row.add_controller(gesture)
        return row

    def _on_close_request(self, *_a) -> bool:
        if self._on_closed:
            self._on_closed()
        return False
