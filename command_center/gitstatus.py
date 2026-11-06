#!/usr/bin/env python3
"""Compact git status for a working directory (lr-style), behind one fair scheduler.

Returns a one-glyph symbol + Rich style: ✓ clean, ↑ ahead, ↓ behind, ⇅ diverged,
● dirty/untracked, and blank for a non-repo.

Reading and refreshing are split (tp#1758). :func:`cached` only reads the last value and
never spawns Git, so a render path (the TUI's ``_add_session_row``) costs a dict lookup.
:func:`refresh_one` is the ONLY place a ``git status`` starts: at most one process-wide
every :data:`_SPACING_SECONDS`, for the least-recently-refreshed eligible cwd, so ~18
session cwds no longer expire together into a burst of ``git status`` runs. A value is
never dropped for being old — the stale glyph stays shown until its turn comes round.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import subprocess
import threading
import time
from collections.abc import Iterable

# At most one `git status` per this many seconds, process-wide.
_SPACING_SECONDS = 3.0
# A cwd refreshed more recently than this is not eligible (the old per-cwd TTL), so a
# TUI with few cwds does not run git more often than before.
_MIN_AGE_SECONDS = 20.0

_lock = threading.Lock()
_values: dict[str, tuple[str, str]] = {}  # cwd -> last (symbol, style)
_refreshed: dict[str, float] = {}  # cwd -> monotonic time of its last refresh attempt
_last_run: float | None = None  # monotonic time of the last git spawn, process-wide

_CLEAN = ("✓", "green")
_AHEAD = ("↑", "yellow")
_BEHIND = ("↓", "yellow")
_DIVERGED = ("⇅", "yellow")
_DIRTY = ("●", "#ff8800")
_NONE = ("", "grey42")


def cached(cwd: str) -> tuple[str, str]:
    """Return the last ``(symbol, style)`` for *cwd*; blank until first refreshed. No Git."""
    if not cwd:
        return _NONE
    with _lock:
        return _values.get(cwd, _NONE)


def reset() -> None:
    """Forget every value and the scheduler's clock (tests; a fresh process starts here)."""
    global _last_run  # pylint: disable=global-statement
    with _lock:
        _values.clear()
        _refreshed.clear()
        _last_run = None


def _pick(cwds: Iterable[str], now: float) -> str | None:
    """The eligible cwd refreshed longest ago (never-refreshed first, ties: given order)."""
    best: str | None = None
    best_at = 0.0
    seen: set[str] = set()
    for cwd in cwds:
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        at = _refreshed.get(cwd)
        if at is None:
            return cwd  # cold: first in display order wins at once
        if now - at < _MIN_AGE_SECONDS:
            continue
        if best is None or at < best_at:
            best, best_at = cwd, at
    return best


def refresh_one(cwds: Iterable[str], now: float | None = None) -> str | None:
    """Run at most ONE ``git status`` for the most overdue of *cwds*; return that cwd.

    Returns None (and spawns nothing) when the last process-wide run was under
    :data:`_SPACING_SECONDS` ago or no cwd is eligible. Duplicates and blanks in *cwds*
    are ignored. A git failure (timeout, no ``git``) keeps the previous value shown and
    still counts as this cwd's turn, so a broken cwd cannot starve the others.
    """
    global _last_run  # pylint: disable=global-statement
    now = time.monotonic() if now is None else now
    with _lock:
        if _last_run is not None and now - _last_run < _SPACING_SECONDS:
            return None
        cwd = _pick(cwds, now)
        if cwd is None:
            return None
        _last_run = now
        _refreshed[cwd] = now
    result = _compute(cwd)
    if result is not None:
        with _lock:
            _values[cwd] = result
    return cwd


def _compute(cwd: str) -> tuple[str, str] | None:  # pylint: disable=too-many-return-statements
    """The status glyph for *cwd*; None when git could not run (keep the last value)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v1", "--branch"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
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
