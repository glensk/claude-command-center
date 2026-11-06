#!/usr/bin/env python3
"""The tab badge palette and the staff role-mark file — stdlib only (tp#1806 B1).

``ccc statusline`` refreshes the role-mark file (:func:`sync_role_mark`) on every render,
so it lives here, importable without :mod:`command_center.tabsymbol`'s heavier title and
AppleScript machinery. :mod:`command_center.tabsymbol` re-exports every name, so existing
imports keep working.

Imports: the standard library and, lazily, :mod:`command_center.staffrole` (stdlib only).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import os
from pathlib import Path

# Assignment order = visual priority. Tabs claim the first free badge in this
# order, so it is front-loaded for maximum distinctness: the first 6 cover all 6
# shapes (circle / square / diamond / triangle / heart / star) before any shape
# repeats, the first 8 are 8 well-separated, high-contrast colors before any hue
# repeats, with only ONE red and ONE warm-yellow up front (the look-alike glyphs
# — extra reds 🔴🔻🟥, warm 🟨🟧🧡, dark ⚫ — are pushed to the tail). No two
# adjacent badges share a shape or a color family. Width-2 emoji throughout
# (uniform cell width); only widely-supported, no-variation-selector glyphs.
#
# Each entry is (emoji, shape, color-family); ``color`` groups look-alike hues
# ("warm" = yellow/gold/orange) so the ordering rules are testable.
BADGES: tuple[tuple[str, str, str], ...] = (
    ("🔺", "triangle", "red"),
    ("🟢", "circle", "green"),
    ("🟪", "square", "purple"),
    ("⭐", "star", "warm"),
    ("🔷", "diamond", "blue"),
    ("🤎", "heart", "brown"),
    ("💠", "diamond", "cyan"),
    ("⚪", "circle", "white"),
    ("💙", "heart", "blue"),
    ("🟩", "square", "green"),
    ("🟣", "circle", "purple"),
    ("🟨", "square", "warm"),
    ("🔵", "circle", "blue"),
    ("🟫", "square", "brown"),
    ("💜", "heart", "purple"),
    ("🟧", "square", "warm"),
    ("🔻", "triangle", "red"),
    ("🤍", "heart", "white"),
    ("🟤", "circle", "brown"),
    ("🟦", "square", "blue"),
    ("🔴", "circle", "red"),
    ("🧡", "heart", "warm"),
    ("⚫", "circle", "black"),
    ("💚", "heart", "green"),
)

PALETTE: tuple[str, ...] = tuple(emoji for emoji, _shape, _color in BADGES)


def role_mark_dir() -> Path:
    """One ``<claude session id>`` file per adopted session (env-overridable for tests)."""
    env = os.environ.get("CCC_ROLE_MARK_DIR")
    return Path(env) if env else Path.home() / ".cache" / "ccc-role-mark"


def sync_role_mark(session_id: str | None) -> str | None:
    """Keep ``role_mark_dir()/<session_id>`` = the adopted agent's terminal-font mark (tp#1288).

    The mark is the agent's Nerd Font ``glyph:``, else its ``symbol:`` (never a palette
    badge). The Claude Code status-line script reads this file instead of the tab's badge
    file (``statusline-command.sh``), so it needs no agent.yaml parsing of its own; ``ccc
    statusline`` refreshes it on every render. Not adopted → the file is removed. Writes
    only on a change (temp file + rename). Returns the mark; never raises.
    """
    from . import staffrole  # lazy

    if not session_id or "/" in session_id or session_id.startswith("."):
        return None
    mark = staffrole.session_glyph(session_id)
    if mark in PALETTE:
        mark = None
    path = role_mark_dir() / session_id
    try:
        if not mark:
            path.unlink(missing_ok=True)
            return None
        try:
            if path.read_text(encoding="utf-8").strip() == mark:
                return mark
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{session_id}.tmp")
        tmp.write_text(mark + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return mark
    return mark
