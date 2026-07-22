"""Inline CSS for Hogwarts (plugin-local; does not depend on Reach CSS)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

# Plain str (not b"""…""") so comments/labels may use non-ASCII safely;
# apply_css encodes to UTF-8 for Gtk.CssProvider.
HOGWARTS_CSS = """
.hogwarts-page {
  background-color: #111111;
  color: #e8e8e8;
}
.hogwarts-header {
  background-color: #0d0d0d;
  border-bottom: 1px solid #222222;
  padding: 10px 16px;
  min-height: 44px;
}
.hogwarts-title {
  font-size: 1.05rem;
  font-weight: 700;
  color: #f0f0f0;
}
.hogwarts-sub {
  font-size: 0.82rem;
  color: #8a8a8a;
}
.hogwarts-banner {
  background-color: #1a1814;
  border: 1px solid #3d3520;
  border-radius: 10px;
  padding: 10px 12px;
  color: #c9b27a;
  font-size: 0.82rem;
}
.hogwarts-split {
  min-height: 0;
}
.hogwarts-sidebar {
  background-color: #0f0f0f;
  border-right: 1px solid #222;
  min-width: 200px;
  padding: 12px 10px;
}
.hogwarts-stack {
  min-height: 0;
  min-width: 0;
}
.hogwarts-nav-btn {
  border-radius: 10px;
  min-height: 40px;
  padding: 0 12px;
  margin: 2px 0;
  background: transparent;
  color: #a0a0a0;
  border: 1px solid transparent;
}
.hogwarts-nav-btn:hover {
  background-color: #161616;
  color: #e0e0e0;
}
.hogwarts-nav-btn:checked,
.hogwarts-nav-btn:active {
  background-color: #1a1a1c;
  color: #e8e8e8;
  border-color: #2a2a2e;
}
.hogwarts-main {
  background-color: #111111;
  min-width: 0;
  min-height: 0;
}
.hogwarts-panel {
  padding: 20px 24px 28px 24px;
}
.hogwarts-hero {
  background: linear-gradient(145deg, #161a22 0%, #12141a 55%, #0e1014 100%);
  border: 1px solid #2a3140;
  border-radius: 14px;
  padding: 16px 18px;
}
.hogwarts-hero-title {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #6a7a94;
}
.hogwarts-hero-state {
  font-size: 1.4rem;
  font-weight: 700;
  color: #f2f2f2;
}
.hogwarts-hero-meta {
  font-size: 0.88rem;
  color: #a8b0c0;
  font-family: monospace;
}
.hogwarts-dot {
  min-width: 10px;
  min-height: 10px;
  border-radius: 99px;
  background-color: #555;
}
.hogwarts-dot-live {
  background-color: #5fbf70;
  box-shadow: 0 0 8px rgba(95, 191, 112, 0.45);
}
.hogwarts-dot-idle {
  background-color: #707070;
}
.hogwarts-dot-busy {
  background-color: #6aa3e8;
  box-shadow: 0 0 8px rgba(106, 163, 232, 0.4);
}
.hogwarts-dot-off {
  background-color: #e86a6a;
}
.hogwarts-card {
  background-color: #161616;
  border: 1px solid #262626;
  border-radius: 12px;
  padding: 14px 16px;
}
.hogwarts-card-title {
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #707070;
  margin-bottom: 2px;
}
.hogwarts-kv-key {
  font-size: 0.75rem;
  color: #666;
  font-weight: 600;
  min-width: 72px;
}
.hogwarts-kv-val {
  font-size: 0.88rem;
  color: #d4d4d4;
  font-family: monospace;
}
.hogwarts-field-label {
  font-size: 0.78rem;
  font-weight: 600;
  color: #8a8a8a;
}
.hogwarts-section {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #5a5a5a;
}
.hogwarts-muted {
  color: #8a8a8a;
  font-size: 0.85rem;
}
.hogwarts-probe-row {
  background-color: #141414;
  border: 1px solid #222;
  border-radius: 10px;
  padding: 10px 12px;
  margin: 4px 0;
}
.hogwarts-probe-label {
  font-weight: 600;
  font-size: 0.9rem;
  color: #e0e0e0;
}
.hogwarts-probe-target {
  font-size: 0.78rem;
  color: #707070;
  font-family: monospace;
}
.hogwarts-ok {
  color: #8fd19e;
  font-weight: 700;
  font-size: 0.8rem;
}
.hogwarts-fail {
  color: #e89a9a;
  font-weight: 700;
  font-size: 0.8rem;
}
.hogwarts-log {
  font-family: "DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono",
    "Ubuntu Mono", monospace;
  font-size: 0.78rem;
  color: #b0b8c8;
  background-color: #0c0c0c;
  border: 1px solid #222;
  border-radius: 10px;
  padding: 10px 12px;
}
.hogwarts-chip {
  font-size: 0.7rem;
  font-weight: 700;
  border-radius: 999px;
  padding: 2px 8px;
  background-color: #222;
  color: #9a9a9a;
}
.hogwarts-chip-live {
  background-color: #1a2a1c;
  color: #8fd19e;
}
.hogwarts-chip-plane {
  background-color: #1a2230;
  color: #8ab4f8;
}
.hogwarts-action-grid {
  margin-top: 4px;
}
/* Fleet = plain buttons (stable hover; no ListBox thrash) */
.hogwarts-fleet-list {
  background: transparent;
  margin: 0;
}
.hogwarts-fleet-btn {
  border-radius: 12px;
  min-height: 56px;
  padding: 0;
  background-color: #161616;
  border: 1px solid #2a2a2e;
  color: #e8e8e8;
  transition: none;
  /* Own the hit target — children are can_target=false in code */
  outline: none;
}
.hogwarts-fleet-btn:hover {
  background-color: #2a2a34;
  border-color: #5b8def;
}
.hogwarts-fleet-btn:active {
  background-color: #1e2a3c;
}
.hogwarts-fleet-btn:focus-visible {
  border-color: #5b8def;
  box-shadow: 0 0 0 1px #5b8def;
}
.hogwarts-fleet-btn-selected {
  background-color: #1e2a3c;
  border-color: #5b8def;
}
.hogwarts-fleet-btn-selected:hover {
  background-color: #243348;
  border-color: #6a9af5;
}
/* Keep hover visible even if a child label briefly invalidates */
.hogwarts-fleet-btn:hover .hogwarts-agent-host,
.hogwarts-fleet-btn:hover .hogwarts-agent-meta {
  color: #ffffff;
}
.hogwarts-fleet-btn-selected .hogwarts-agent-host {
  color: #ffffff;
}
.hogwarts-agent-row {
  background-color: transparent;
  border: none;
  border-radius: 12px;
  padding: 12px 14px;
  margin: 0;
}
.hogwarts-agent-host {
  font-size: 0.98rem;
  font-weight: 700;
  color: #f0f0f0;
}
.hogwarts-remote-scroll {
  background-color: #0c0c0c;
  border: 1px solid #222;
  border-radius: 10px;
  min-height: 140px;
}
.hogwarts-desktop-frame {
  background-color: #0a0a0c;
  border: 1px solid #2a2a32;
  border-radius: 10px;
  min-height: 200px;
  padding: 4px;
}
.hogwarts-detail-header {
  padding: 4px 0 8px 0;
  border-bottom: 1px solid #222;
}
.hogwarts-tab-bar {
  padding: 2px 0;
}
.hogwarts-tab {
  border-radius: 8px;
  min-height: 32px;
  padding: 0 14px;
  background: transparent;
  color: #9a9a9a;
  border: 1px solid transparent;
}
.hogwarts-tab:hover {
  background-color: #161616;
  color: #e0e0e0;
}
.hogwarts-tab:checked {
  background-color: #1c1c22;
  color: #f0f0f0;
  border-color: #333;
}
.hogwarts-remote-row {
  border-radius: 6px;
  min-height: 28px;
  padding: 2px 6px;
}
.hogwarts-remote-row:hover {
  background-color: #1a1a1e;
}
.hogwarts-agent-meta {
  font-size: 0.8rem;
  color: #8a8a8a;
  font-family: monospace;
}
.hogwarts-status-online { color: #8fd19e; font-weight: 700; font-size: 0.78rem; }
.hogwarts-status-idle { color: #c9b27a; font-weight: 700; font-size: 0.78rem; }
.hogwarts-status-offline { color: #e89a9a; font-weight: 700; font-size: 0.78rem; }
.hogwarts-status-unknown { color: #8a8a8a; font-weight: 700; font-size: 0.78rem; }
.hogwarts-console-input {
  font-family: monospace;
  font-size: 0.9rem;
}
.hogwarts-remote-colhead {
  padding: 2px 6px;
}
.hogwarts-launch-card {
  padding: 18px 20px;
}
.hogwarts-launch-title {
  font-size: 1.15rem;
  font-weight: 700;
  color: #f0f0f0;
}
/* -- Windows File Explorer lookalike (light, compact) -- */
.wfe {
  background-color: #ffffff;
  color: #1a1a1a;
  font-family: "Segoe UI", "Cantarell", "Ubuntu", sans-serif;
}
.wfe-titlebar {
  background-color: #f3f3f3;
  border-bottom: 1px solid #e5e5e5;
  padding: 4px 10px;
  min-height: 0;
}
.wfe-title {
  font-size: 0.88rem;
  font-weight: 600;
  color: #1a1a1a;
}
.wfe-subtitle {
  font-size: 0.7rem;
  color: #605e5c;
}
.wfe-ribbon {
  background-color: #f9f9f9;
  border-bottom: 1px solid #e5e5e5;
  padding: 3px 6px;
}
.wfe-ribbon-btn {
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 3px;
  color: #1a1a1a;
  min-height: 26px;
  padding: 0 6px;
}
.wfe-ribbon-btn:hover {
  background-color: #eef4fb;
  border-color: #c7d9ef;
}
.wfe-ribbon-label {
  font-size: 0.78rem;
  color: inherit;
}
.wfe-ribbon-primary {
  background-color: #0078d4;
  color: #ffffff;
  border-color: #0078d4;
}
.wfe-ribbon-primary:hover {
  background-color: #106ebe;
  border-color: #106ebe;
  color: #ffffff;
}
.wfe-ribbon-primary .wfe-ribbon-label {
  color: #ffffff;
}
.wfe-hint {
  font-size: 0.68rem;
  color: #8a8886;
  margin-right: 4px;
}
.wfe-check {
  color: #1a1a1a;
  font-size: 0.78rem;
}
.wfe-nav {
  background-color: #ffffff;
  border-bottom: 1px solid #e5e5e5;
  padding: 4px 6px;
}
.wfe-nav-btn {
  min-width: 28px;
  min-height: 28px;
  border-radius: 3px;
  color: #323130;
  background-color: transparent;
  border: 1px solid transparent;
  padding: 0;
}
.wfe-nav-btn:hover {
  background-color: #f3f3f3;
  border-color: #e5e5e5;
}
.wfe-nav-btn:disabled {
  opacity: 0.35;
}
.wfe-icon {
  color: #323130;
}
.wfe-address-wrap {
  background-color: #ffffff;
  border: 1px solid #8a8886;
  border-radius: 2px;
  min-height: 28px;
  margin-left: 3px;
  margin-right: 4px;
}
.wfe-address-wrap:focus-within {
  border-color: #0078d4;
  box-shadow: 0 0 0 1px #0078d4;
}
.wfe-address {
  background-color: transparent;
  border: none;
  box-shadow: none;
  color: #1a1a1a;
  font-size: 0.84rem;
  min-height: 24px;
  padding: 0 2px;
}
.wfe-go {
  min-width: 26px;
  min-height: 26px;
  background-color: transparent;
  border: none;
}
.wfe-go:hover {
  background-color: #f3f3f3;
}
.wfe-search-wrap {
  background-color: #ffffff;
  border: 1px solid #8a8886;
  border-radius: 2px;
  min-height: 28px;
  padding: 0 2px;
}
.wfe-search {
  background-color: transparent;
  border: none;
  box-shadow: none;
  color: #1a1a1a;
  font-size: 0.8rem;
  min-height: 24px;
}
.wfe-body {
  min-height: 0;
  background-color: #ffffff;
}
/* Compact Quick access -- intentionally narrow */
.wfe-sidebar-scroll {
  background-color: #f3f3f3;
  min-width: 140px;
  max-width: 160px;
}
.wfe-sidebar {
  background-color: #f3f3f3;
  min-width: 136px;
}
.wfe-side-header {
  font-size: 0.68rem;
  font-weight: 700;
  color: #605e5c;
  letter-spacing: 0.02em;
}
.wfe-side-btn {
  border-radius: 3px;
  min-height: 22px;
  padding: 1px 4px;
  background-color: transparent;
  border: 1px solid transparent;
  color: #1a1a1a;
  margin: 0 1px;
}
.wfe-side-btn:hover {
  background-color: #e5f3ff;
  border-color: #cce8ff;
}
.wfe-side-label {
  font-size: 0.78rem;
  color: #1a1a1a;
}
.wfe-side-footer {
  font-size: 0.68rem;
  color: #8a8886;
}
.wfe-vdiv {
  background-color: #e5e5e5;
  min-width: 1px;
}
.wfe-main {
  background-color: #ffffff;
  min-width: 0;
  min-height: 0;
}
.wfe-headers {
  background-color: #ffffff;
  padding: 0;
  min-height: 24px;
}
.wfe-col-head-btn {
  border-radius: 0;
  min-height: 24px;
  padding: 0 6px;
  background-color: transparent;
  border: none;
  border-right: 1px solid #e5e5e5;
  color: #1a1a1a;
}
.wfe-col-head-btn:hover {
  background-color: #f3f3f3;
}
.wfe-col-head {
  font-size: 0.74rem;
  font-weight: 600;
  color: #323130;
}
.wfe-hdiv {
  background-color: #e5e5e5;
  min-height: 1px;
}
.wfe-scroll {
  background-color: #ffffff;
  min-height: 220px;
}
.wfe-list {
  background-color: #ffffff;
}
.wfe-row {
  background-color: #ffffff;
  min-height: 22px;
  padding: 1px 0;
  border: 1px solid transparent;
}
.wfe-row:hover {
  background-color: #e5f3ff;
  border-color: #cce8ff;
}
.wfe-row-selected {
  background-color: #cce8ff;
  border-color: #99d1ff;
}
.wfe-row-selected:hover {
  background-color: #b8dcff;
  border-color: #99d1ff;
}
.wfe-name {
  font-size: 0.82rem;
  color: #1a1a1a;
}
.wfe-cell {
  font-size: 0.76rem;
  color: #605e5c;
  padding-right: 6px;
}
.wfe-cell-num {
  font-size: 0.76rem;
  color: #605e5c;
  padding-right: 8px;
}
.wfe-empty {
  font-size: 0.85rem;
  color: #8a8886;
}
.wfe-statusbar {
  background-color: #f3f3f3;
  border-top: 1px solid #e5e5e5;
  padding: 2px 10px;
  min-height: 22px;
}
.wfe-status {
  font-size: 0.74rem;
  color: #323130;
}
.wfe-status-ok {
  color: #107c10;
}
.wfe-status-fail {
  color: #a4262c;
}
.wfe-count {
  font-size: 0.74rem;
  color: #605e5c;
}
.wfe-mode-row {
  background-color: #f3f3f3;
  border-bottom: 1px solid #e5e5e5;
  padding: 3px 8px;
}
.wfe-mode-btn {
  min-height: 24px;
  padding: 0 10px;
  border-radius: 3px;
  font-size: 0.8rem;
  background-color: transparent;
  border: 1px solid transparent;
  color: #323130;
}
.wfe-mode-btn:checked {
  background-color: #ffffff;
  border-color: #c8c6c4;
  color: #0078d4;
  font-weight: 600;
}
.wfe-index-status {
  font-size: 0.74rem;
  color: #605e5c;
  margin-left: 10px;
}
.wfe-search-tools {
  background-color: #fafafa;
  border-bottom: 1px solid #e5e5e5;
}
.hogwarts-desktop-frame:hover {
  border-color: #5b8def;
}
/* -- Remote Viewer (ShareX-style archive) -- */
.rdv {
  background-color: #1e1e1e;
  color: #e8e8e8;
  font-family: "Segoe UI", "Cantarell", "Ubuntu", sans-serif;
}
.rdv-titlebar {
  background-color: #2d2d30;
  border-bottom: 1px solid #3e3e42;
  padding: 6px 12px;
}
.rdv-title {
  font-size: 0.92rem;
  font-weight: 700;
  color: #f0f0f0;
}
.rdv-subtitle {
  font-size: 0.7rem;
  color: #9a9a9a;
  font-family: monospace;
}
.rdv-ribbon {
  background-color: #252526;
  border-bottom: 1px solid #3e3e42;
  padding: 4px 8px;
}
.rdv-primary {
  background-color: #0e639c;
  color: #ffffff;
  border-radius: 3px;
  border: 1px solid #1177bb;
  min-height: 28px;
  padding: 0 10px;
}
.rdv-primary:hover {
  background-color: #1177bb;
}
.rdv-tool-btn {
  min-height: 28px;
  min-width: 28px;
  border-radius: 3px;
  padding: 0 6px;
  color: #cccccc;
  background-color: transparent;
  border: 1px solid transparent;
}
.rdv-tool-btn:hover {
  background-color: #3e3e42;
  border-color: #505050;
  color: #ffffff;
}
.rdv-danger:hover {
  background-color: #5a1d1d;
  border-color: #a12626;
  color: #f48771;
}
.rdv-field {
  font-size: 0.75rem;
  color: #9a9a9a;
  margin-right: 4px;
}
.rdv-check {
  font-size: 0.78rem;
  color: #cccccc;
  margin-left: 6px;
}
.rdv-tools {
  background-color: #2d2d30;
  border-bottom: 1px solid #3e3e42;
  padding: 3px 8px;
}
.rdv-hist-lab {
  font-size: 0.75rem;
  color: #9a9a9a;
  font-family: monospace;
  min-width: 48px;
}
.rdv-body {
  min-height: 0;
  background-color: #1e1e1e;
}
.rdv-archive {
  background-color: #252526;
  min-width: 190px;
  max-width: 220px;
}
.rdv-archive-head {
  padding: 6px 8px;
  border-bottom: 1px solid #3e3e42;
  background-color: #2d2d30;
}
.rdv-archive-title {
  font-size: 0.78rem;
  font-weight: 700;
  color: #cccccc;
}
.rdv-archive-count {
  font-size: 0.72rem;
  color: #9a9a9a;
  font-family: monospace;
}
.rdv-archive-scroll {
  background-color: #252526;
  min-height: 200px;
}
.rdv-archive-list {
  background-color: #252526;
  padding: 4px 2px;
}
.rdv-archive-foot {
  font-size: 0.68rem;
  color: #6a6a6a;
  font-family: monospace;
  border-top: 1px solid #3e3e42;
}
.rdv-arch-row {
  border-radius: 3px;
  min-height: 52px;
  padding: 2px;
  background-color: transparent;
  border: 1px solid transparent;
}
.rdv-arch-row:hover {
  background-color: #2a2d2e;
  border-color: #3e3e42;
}
.rdv-arch-row-selected {
  background-color: #094771;
  border-color: #007acc;
}
.rdv-arch-row-selected:hover {
  background-color: #0e639c;
}
.rdv-arch-time {
  font-size: 0.74rem;
  font-weight: 600;
  color: #e0e0e0;
}
.rdv-arch-dim {
  font-size: 0.68rem;
  color: #9a9a9a;
  font-family: monospace;
}
.rdv-thumb {
  background-color: #1e1e1e;
  border: 1px solid #3e3e42;
  border-radius: 2px;
}
.rdv-empty {
  font-size: 0.8rem;
  color: #6a6a6a;
}
.rdv-main {
  background-color: #1e1e1e;
  min-width: 0;
  min-height: 0;
}
.rdv-scroll {
  background-color: #141414;
  min-height: 280px;
}
.rdv-frame {
  background-color: #141414;
  min-height: 200px;
}
.rdv-picture {
  background-color: #141414;
}
.rdv-statusbar {
  background-color: #007acc;
  padding: 3px 10px;
  min-height: 22px;
}
.rdv-status {
  font-size: 0.74rem;
  color: #ffffff;
}
.rdv-status-ok {
  color: #b5f4b5;
}
.rdv-status-fail {
  color: #ffc0c0;
}
.rdv-meta {
  font-size: 0.72rem;
  color: #d0e8ff;
  font-family: monospace;
}
.rdv-mode-row {
  background-color: #2d2d30;
  border-bottom: 1px solid #3e3e42;
  padding: 4px 8px;
}
.rdv-mode-btn {
  min-height: 26px;
  padding: 0 10px;
  border-radius: 3px;
  font-size: 0.8rem;
  background-color: transparent;
  border: 1px solid transparent;
  color: #cccccc;
}
.rdv-mode-btn:checked {
  background-color: #0e639c;
  border-color: #1177bb;
  color: #ffffff;
  font-weight: 600;
}
.rdv-mode-hint {
  font-size: 0.74rem;
  color: #9a9a9a;
  margin-left: 8px;
}
.rdv-badge {
  font-size: 0.7rem;
  font-weight: 700;
  color: #1e1e1e;
  background-color: transparent;
  border-radius: 999px;
  padding: 2px 8px;
  min-width: 0;
}
.rdv-badge-live {
  background-color: #f14c4c;
  color: #ffffff;
}
.rdv-session {
  background-color: #1e1e1e;
  min-height: 200px;
}
.rdv-session-info {
  font-family: monospace;
  font-size: 0.82rem;
  color: #d4d4d4;
  background-color: #252526;
  border: 1px solid #3e3e42;
  border-radius: 6px;
  padding: 12px;
}
.rdv-empty-overlay {
  font-size: 0.9rem;
  color: #6a6a6a;
  padding: 24px;
}
"""


# One provider per process — re-adding on every HogwartsPage construct
# stacks USER-priority CSS and slows style matching over long sessions.
_CSS_PROVIDER: Gtk.CssProvider | None = None
_CSS_DISPLAY_ID: int | None = None


def apply_css(widget: Gtk.Widget) -> None:
    global _CSS_PROVIDER, _CSS_DISPLAY_ID
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return
        disp_id = id(display)
        if _CSS_PROVIDER is not None and _CSS_DISPLAY_ID == disp_id:
            return
        provider = Gtk.CssProvider()
        css = HOGWARTS_CSS if isinstance(HOGWARTS_CSS, bytes) else HOGWARTS_CSS.encode("utf-8")
        provider.load_from_data(css)
        # USER > APPLICATION so Reach theme does not swallow fleet-row hover
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )
        _CSS_PROVIDER = provider
        _CSS_DISPLAY_ID = disp_id
    except Exception:
        pass
