# Hogwarts

<p align="center">
  <img src="https://raw.githubusercontent.com/digitizable/reach-plugin-hogwarts/main/hogwarts.png?v=noshadow" alt="Hogwarts" width="128" height="128"/>
</p>

<p align="center">
  <strong>C2 for Reach</strong> — a well-defended operator desk.<br/>
  Named for the castle: halls for ops, walls for the keep.
</p>

<p align="center">
  channel · agents · plane · reverse · egress · playbooks
</p>

**Hogwarts** is the command-and-control plugin for [Reach](https://github.com/digitizable/reach): path-aware channel status, implant roster against your control plane, reverse listener notes, egress probing (direct vs SOCKS path), interactive console, and session playbooks.

Formerly **Handset** / **Malbork** — same role, castle name and mark.

> Unofficial name. Not affiliated with Warner Bros., J.K. Rowling, or the Harry Potter franchise.

## Install

In Reach → **Plugins** marketplace:

```text
digitizable/reach-plugin-hogwarts
```

Requires Reach ≥ 0.5.

### Local dev

```bash
rsync -a --delete \
  --exclude .git --exclude __pycache__ \
  ./ ~/.local/share/reach/plugins/com__digitizable__hogwarts/
```

Restart Reach after changes. Remove old `com.digitizable.malbork` / `handset` installs if both appear.

## Features

| Panel | What |
|-------|------|
| **Channel** | Live path hero, SOCKS / hops / fingerprint / plane |
| **Agents** | Fleet roster from `GET /api/v1/agents` |
| **Listener** | Accept host/port, transport, cover face, ops notes |
| **Egress** | TCP matrix direct vs path SOCKS |
| **Console** | Ops shell (status, plane, pull, notes) |
| **Plane** | Control-plane URL + token + health |
| **Ops kit** | Reverse export, playbook JSON, data dir |
| **Session log** | Local activity trail |

## Control plane

Hogwarts does **not** host implants. Point **Plane** at your API — see [hogwarts/backend/CONTRACT.md](hogwarts/backend/CONTRACT.md).

```text
~/.local/share/reach/plugin-data/com__digitizable__hogwarts/plane.json
```

## Layout

```
ui.py
hogwarts/
  page.py · banner.py · theme.py · net.py · store.py · widgets.py
  backend/   # control-plane client + contract
  panels/    # Channel · Agents · Listener · …
```

## Purple stance

Operate the tasking loop **and** defend the keep. Research: [anguish.sh — Hogwarts / C2](https://anguish.sh/studies/hogwarts/notes).

## License

GPL-3.0-or-later
