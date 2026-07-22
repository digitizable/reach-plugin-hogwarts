# Hogwarts

<p align="center">
  <img src="https://raw.githubusercontent.com/digitizable/reach-plugin-hogwarts/main/hogwarts.png?v=stars" alt="Hogwarts" width="128" height="128"/>
</p>

<p align="center">
  <strong>C2 for Reach</strong> — a well-defended operator desk.<br/>
  Named for the castle: halls for ops, walls for the keep.
</p>

<p align="center">
  channel · agents · plane · reverse · egress · playbooks
</p>

**Hogwarts** is the command-and-control plugin for [Reach](https://github.com/digitizable/reach): path-aware channel status, implant roster against your control plane, reverse listener notes, egress probing (direct vs SOCKS path), interactive console, and session playbooks.

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

Restart Reach after changes.

## Features

| Panel | What |
|-------|------|
| **Channel** | Live path hero, SOCKS / hops / fingerprint / plane |
| **Agents** | Tasking + shell/FS; **Remote Viewer** window for screenshot / Live / Control |
| **Agent (lab)** | Stable loop, spool, multi-URL; screenshot + optional x11vnc session |
| **Listener** | CRUD + evidence LED + TCP probe + plane pull/push |
| **Egress** | TCP matrix direct vs path SOCKS |
| **Console** | Ops shell; `pull` / auto-poll; `task …` |
| **Plane** | Control-plane URL + token + poll interval + health + **Start plane** (local lab) |
| **Ops kit** | Playbook fields, drills, **export agent zip** (runners + optional PyInstaller) |
| **Session log** | Local activity trail |

## Control plane

Hogwarts does **not** host implants. Point **Plane** at your API — see [hogwarts/backend/CONTRACT.md](hogwarts/backend/CONTRACT.md).

```text
~/.local/share/reach/plugin-data/com__digitizable__hogwarts/plane.json
```

### Lab plane + agent (stdlib)

```bash
# terminal 1 — mock plane (T1–T4 + agent routes)
PLANE_OPERATOR_TOKEN=dev PLANE_HTTP_ADDR=127.0.0.1:8080 python3 plane/server.py

# mint enroll secret
curl -s -X POST http://127.0.0.1:8080/api/v1/operator/enroll-secrets \
  -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  -d '{"max_uses":1}' 

# terminal 2 — reference agent
python3 agent/agent.py once -c agent.json   # after writing enroll_secret into agent.json
```

Hogwarts **Plane** panel: `http://127.0.0.1:8080` · token `dev` · then **Agents** → shell.

### Personal lab (desk config + multi mock agents)

```bash
bash lab/personal_setup.sh
```

Writes Hogwarts `plane.json` (`http://127.0.0.1:8080` · token `dev`), starts three Docker agents, a **host** agent on this machine, and a **Win11 pack** under plugin-data `personal/win11-agent` (plane URL `http://192.168.122.1:8080` for libvirt NAT). Restart Reach after running.

### Docker lab

```bash
cd lab && ./run_lab.sh
# builds plane + agent images, enrolls, shells uname/id, leaves containers up
# Plane for Hogwarts UI: http://127.0.0.1:8080  token=dev
# Extra agents:
#   docker run -d --network hogwarts-lab -e PLANE_URL=http://hogwarts-plane:8080 \
#     -e PLANE_OPERATOR_TOKEN=dev hogwarts-agent:lab
```

## Layout

```
ui.py
hogwarts/          # Reach plugin UI
  backend/         # operator client + CONTRACT
  panels/
plane/server.py    # lab control plane (not shipped in GTK)
agent/agent.py     # reference agent
lab/               # docker compose + smoke test
```

## Purple stance

Operate the tasking loop **and** defend the keep. Whitepaper: [anguish.sh — Hogwarts](https://anguish.sh/studies/hogwarts). Working notes: [/studies/hogwarts/notes](https://anguish.sh/studies/hogwarts/notes).

## License

[GNU General Public License v3.0 or later](LICENSE) (`GPL-3.0-or-later`).
