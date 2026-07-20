"""ASCII marks for the Hogwarts operator console."""

from __future__ import annotations

from hogwarts import __version__

# Standard FIGlet-ish block (monospace).
_WORDMARK = r"""
 _   _                                  _
| | | | ___   __ ___      ____ _ _ __| |_ ___
| |_| |/ _ \ / _` \ \ /\ / / _` | '__| __/ __|
|  _  | (_) | (_| |\ V  V / (_| | |  | |_\__ \
|_| |_|\___/ \__, | \_/\_/ \__,_|_|   \__|___/
             |___/
""".strip(
    "\n"
)

_GLYPH = r"""
              /\
       /\    /  \    /\
      /  \__/ || \__/  \
     |  []    ||    []  |
     |______  ||  ______|
    /  ___  \ || /  ___  \
   |  |   |  ||||  |   |  |
   |__|___|__||||__|___|__|
        C2 desk for Reach
   channel · agents · plane · keep
""".strip(
    "\n"
)


def banner(*, version: str | None = None) -> str:
    ver = version if version is not None else __version__
    return "\n".join(
        [
            _WORDMARK,
            "",
            _GLYPH,
            "",
            f"  Hogwarts v{ver}  ·  type help",
        ]
    )


def banner_short() -> str:
    return (
        "  ┌─────────── HOGWARTS ───────────┐\n"
        "  │  ⚔  keep  ·  C2 desk for Reach │\n"
        "  └────────────────────────────────┘"
    )
