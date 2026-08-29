#!/usr/bin/env python3
"""Compact git status for a working directory (lr-style), with a short TTL cache.

Returns a one-glyph symbol + Rich style: ✓ clean, ↑ ahead, ↓ behind, ⇅ diverged,
● dirty/untracked, and blank for a non-repo. Cached per directory for a few
seconds so the TUI can call it every refresh without hammering git.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import subprocess
import time

_TTL_SECONDS = 20.0
_cache: dict[str, tuple[float, tuple[str, str]]] = {}

_CLEAN = ("✓", "green")
_AHEAD = ("↑", "yellow")
_BEHIND = ("↓", "yellow")
_DIVERGED = ("⇅", "yellow")
_DIRTY = ("●", "#ff8800")
_NONE = ("", "grey42")


def short(cwd: str) -> tuple[str, str]:
    """Return ``(symbol, style)`` for *cwd*'s git status (cached ~20s)."""
    if not cwd:
        return _NONE
    now = time.monotonic()
    cached = _cache.get(cwd)
    if cached is not None and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    result = _compute(cwd)
    _cache[cwd] = (now, result)
    return result


def _compute(cwd: str) -> tuple[str, str]:  # pylint: disable=too-many-return-statements
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v1", "--branch"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return _NONE
    if proc.returncode != 0:
        return _NONE  # not a git repo (or git unavailable)
    lines = proc.stdout.splitlines()
    if not lines:
        return _CLEAN
    branch_line = lines[0]
    has_changes = len(lines) > 1
    ahead = "ahead " in branch_line
    behind = "behind " in branch_line
    if has_changes:
        return _DIRTY
    if ahead and behind:
        return _DIVERGED
    if ahead:
        return _AHEAD
    if behind:
        return _BEHIND
    return _CLEAN
