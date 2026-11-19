"""The terminal's colours, the same family as the web page's (web/static/tokens.css).

A periwinkle accent and one hue per speaker, taken at a lightness that reads on dark and
light terminals alike. Output names what a colour means (`err`, `warn`, `ok`, `accent`,
`title`, `line`) instead of naming the colour.
"""

from __future__ import annotations

import zlib

from rich.theme import Theme

ACCENT = "#7f8cd5"
# The page's face hues (275, 200, 25, 150, 80, 330) at OKLCH lightness 0.66, chroma 0.11:
# the first character of a chat takes the first, the others the next ones.
SPEAKERS = ("#7f8cd5", "#10a6ad", "#cd7670", "#5da56e", "#b58a3a", "#b879b2")
OK = "#5da56e"
WARN = "#c29647"
ERR = "#d9706b"
MUTED = "#8e94a0"
LINE = "#565b65"

THEME = Theme({
    "accent": ACCENT,
    "title": f"bold {ACCENT}",
    "ok": OK,
    "warn": WARN,
    "err": ERR,
    "muted": MUTED,
    "line": LINE,
})


def person_color(key: str) -> str:
    """A steady hue for someone who isn't a character, never the first character's."""
    return SPEAKERS[1 + zlib.crc32(key.encode()) % (len(SPEAKERS) - 1)]
