# Handset

<p align="center">
  <img src="icon.svg" alt="Handset" width="128" height="128"/>
</p>

<p align="center">
  <strong>C2 for Reach</strong> — channel, reverse listener, egress matrix, agent export, playbooks.
</p>

Handset is a **command-and-control desk** plugin for [Reach](https://github.com/digitizable/reach): live path-aware channel status, reverse listener notes, egress probing (direct vs SOCKS path), agent package shortcuts, and session playbooks.

## Install

In Reach → **Plugins** marketplace:

```text
digitizable/reach-plugin-handset
```

Requires Reach ≥ 0.5 (plugin host, `reach-plugin.json` schema 1).

## Features

| Panel | What |
|-------|------|
| **Channel** | Live path hero, SOCKS / hops / fingerprint, quick actions |
| **Listener** | Accept host/port, transport, cover face, agent id, ops notes |
| **Egress** | TCP matrix direct vs path SOCKS; custom targets |
| **Ops kit** | Reverse export folder, playbook JSON, plugin data dir |
| **Session log** | Local activity trail |

## Icons

| File | Use |
|------|-----|
| [`icon.svg`](icon.svg) | Marketplace / README (full color) |
| [`icon-symbolic.svg`](icon-symbolic.svg) | Reach left rail (themed monochrome) |

See [SOURCES.md](./SOURCES.md).

## License

GPL-3.0-or-later
