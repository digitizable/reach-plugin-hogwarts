"""ASCII art for the Hogwarts operator console.

High-detail pure ASCII keep (flags attached to towers with |>>>).
Pure ASCII keeps mono-cell widths stable so flags do not "fly" off when
the TextView wraps or fonts substitute glyphs.

Flags problem (braille art): WORD_CHAR wrap + mixed glyph widths split
pennants from the roof line. Fix: pure ASCII + wrap mode NONE in console.
"""

from __future__ import annotations

from hogwarts import __version__

# High-detail multi-tower keep — flags |>>> sit on the same lines as towers.
# Adapted / expanded from classic pure-ASCII castle patterns (asciiart.eu
# castles gallery, community; unknown artists) for a denser Hogwarts desk splash.
_CASTLE = r"""
                              |>>>                                    |>>>
                      |>>>    |               |>>>                    |
                      |       *               |                       *
                     / \                     / \                     / \
                    /___\      _/\_         /___\       _/\_        /___\
                    [   ]     |/  \|        [   ]      |/  \|       [   ]
                    [ I ]   _/ .--. \_      [ I ]    _/ || \_       [ I ]
                    [   ]__/  /||||\  \__   [   ]___/  ||||  \____  [   ]
               /|\  [   ]    /||||||\       [   ]     /||||\        [   ]  /|\
              /|||\ [___]===/||||||||\======[___]====/||||||\=======[___]/|||\
             /||||||\  \\__/||||||||||\____//   \\__/||||||||\____//  /||||||\
            |||||||||\  `===\||||||||||/===`     `===\||||||||/===`  /|||||||||
            ||||||||| \     /||||||||||\             /||||||||\     / ||||||||
            |  ____  | \   |||||||||||||\           /||||||||||\   /  | ____  |
            | |    | |  |  |  GREAT HALL |         |  GREAT HALL |  | |    | |
            | | [] | |  |  |  ||||||||   |         |   ||||||||  |  | | [] | |
            | |____| |  |  |__||||||||___|         |___||||||||__|  | |____| |
            |________| /   |  || GATE ||  |       |  || GATE ||  |   \________|
             \______/ /    |__||______||__|       |__||______||__|    \______/
               ||    /_______/  ||||| |  \_______/  ||||| |  \_______/   ||
               ||  _/           |        |           |        |           \_  ||
              _||_/_____________|________|___________|________|_____________\_||_
             /__________________________________________________________________\
            |##################################################################|
            |##  HOGWARTS  -  C2 keep for Reach  -  defend the walls         ##|
            |##################################################################|
""".strip(
    "\n"
)

_CASTLE_SMALL = r"""
         |>>>          |>>>          |>>>
        /___\   KEEP  /___\   HALL  /___\
        [ I ]=========[ I ]=========[ I ]
        |___|  GATE   |___|  GATE   |___|
          HOGWARTS - C2 - type help
""".strip(
    "\n"
)


def banner(*, version: str | None = None) -> str:
    """Full high-detail splash for console boot / `banner` command."""
    ver = version if version is not None else __version__
    return f"{_CASTLE}\n\n  Hogwarts v{ver}  -  type help - banner | clear"


def banner_short() -> str:
    """Compact mark after `clear`."""
    return _CASTLE_SMALL
