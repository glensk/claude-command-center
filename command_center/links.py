#!/usr/bin/env python3
"""Clickable terminal hyperlinks (OSC 8).

Vendored and lightly adapted from the ``osc8_link`` / ``local_link`` and
``folder_link`` helpers so the look matches a coloured repo list. Under iTerm2 /
WezTerm a folder link uses the ``openterm://``
scheme (click opens a shell rooted there); elsewhere it degrades to ``file://``
or plain coloured text. The escapes are invisible where unsupported.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import hashlib
import os
import urllib.parse
from pathlib import Path

ESC = "\x1b"
_OPENTERM_TERMS = ("iTerm.app", "WezTerm")


def osc8_link(target_uri: str, text: str) -> str:
    """Wrap *text* in an OSC 8 hyperlink pointing at *target_uri*."""
    return f"{ESC}]8;;{target_uri}{ESC}\\{text}{ESC}]8;;{ESC}\\"


def local_link(path: Path | str, text: str | None = None) -> str:
    """Clickable link to a local folder; click opens a shell there under iTerm2/WezTerm."""
    path = Path(path)
    label = text if text is not None else str(path)
    if os.environ.get("TERM_PROGRAM", "") in _OPENTERM_TERMS:
        encoded = urllib.parse.quote(str(path), safe="")
        return osc8_link(f"openterm://{encoded}", label)
    return osc8_link(path.as_uri(), label)


def _folder_color(name: str) -> tuple[int, int, int]:
    """Stable, readable RGB for a folder name (mid-bright so it shows on dark/light)."""
    digest = hashlib.md5(name.encode("utf-8")).digest()
    return (90 + digest[0] % 150, 90 + digest[1] % 150, 90 + digest[2] % 150)


def folder_link(
    path: Path | str, label: str | None = None, width: int = 0, color: bool = True
) -> str:
    """Coloured, padded, clickable folder cell (``openterm://`` under iTerm2/WezTerm)."""
    path = Path(path)
    text = label if label is not None else path.name
    padded = text.ljust(width) if width else text
    if color:
        red, green, blue = _folder_color(text)
        visible = f"{ESC}[38;2;{red};{green};{blue}m{padded}{ESC}[0m"
    else:
        visible = padded
    if os.environ.get("TERM_PROGRAM", "") in _OPENTERM_TERMS:
        encoded = urllib.parse.quote(str(path), safe="")
        return osc8_link(f"openterm://{encoded}", visible)
    return visible
