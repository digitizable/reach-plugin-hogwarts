# Handset — Reach plugin

**C2-esque operator desk** for [Reach](https://github.com/digitizable/reach).

Path-aware channel status, reverse listener notes, egress matrix (direct vs SOCKS path), agent export shortcuts, and session playbooks.

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

## Logo

`icon.svg` — Lucide `radar` (ISC). See [SOURCES.md](./SOURCES.md).

## License

GPL-3.0-or-later
