#!/usr/bin/env python3
"""Minimal hogwarts-input/1 helper — protocol sample, not a UAC bypass.

Wire: agent spawns this with kind=exec and forwards Control/Session events
on stdin. This sample ACKs HELLO and prints each batch (and can optionally
re-inject via the agent's own tools if you extend it).

  agent.json:
    "input_provider": {
      "enabled": true,
      "kind": "exec",
      "command": "python3",
      "args": ["/path/to/input_provider_echo.py"]
    }

Or Session panel → Custom input provider → path to this script's launcher.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    # Env from agent (kind=exec)
    proto = os.environ.get("HOGWARTS_INPUT_PROTOCOL", "")
    sid = os.environ.get("HOGWARTS_SESSION_ID", "")
    # First line: HELLO hogwarts-input/1 <session_id> <psk>
    hello = sys.stdin.readline()
    if not hello:
        return 1
    parts = hello.strip().split()
    if len(parts) < 2 or parts[0] != "HELLO":
        sys.stderr.write(f"bad hello: {hello!r}\n")
        return 2
    sys.stdout.write("HELLO_OK\n")
    sys.stdout.flush()
    sys.stderr.write(
        f"[echo-provider] up proto={proto or parts[1]} session={sid or (parts[2] if len(parts) > 2 else '')}\n"
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line.upper() == "BYE":
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write(f"[echo-provider] bad json: {line[:80]}\n")
            continue
        evs = obj.get("events") if isinstance(obj, dict) else None
        n = len(evs) if isinstance(evs, list) else 0
        sys.stderr.write(f"[echo-provider] batch events={n}\n")
        # Plug your elevated inject here (SendInput as High IL, pipe to service, …)
    sys.stderr.write("[echo-provider] bye\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
