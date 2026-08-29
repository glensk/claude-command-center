#!/usr/bin/env python3
"""Cross-process UI state for the ``f+j`` toggle between the ccc TUI and session tabs.

Four one-way signals coordinate the live TUI with out-of-process ``ccc jump`` runs
(fired by the global Karabiner chord). Each is a single tiny file under
:func:`config.app_home` — separate files, so the TUI's writes and ``ccc jump``'s
writes never race on a shared blob:

- **selected** (``jump_selected``) — the session id under the TUI cursor. The TUI
  writes it on every row highlight; ``ccc jump``, when run *from* the ccc tab, reads
  it to know which session to jump to (``f+j`` in ccc acts like ``r``).
- **request** (``jump_request``) — a session id ``ccc jump`` asks the TUI to move its
  cursor to, when run from a *session* tab (focus ccc + select that session's row).
  The TUI consumes (clears) it once it has moved the cursor there.
- **tui** (``jump_tui``) — the live TUI's identity (``pid|iterm_session_id``), written
  on mount. It lets ``ccc jump`` hand the *whole* toggle to a live TUI (the fast path:
  the TUI owns a warm iTerm2 API link, so it decides context and focuses in-process)
  instead of paying its own ps + osascript walks.
- **toggle** (``jump_toggle``) — the request verb for that fast path: ``ccc jump`` just
  writes it and returns; the TUI's fast poll consumes it and runs the toggle itself.
- **restart** (``jump_restart``) — the request verb for ``ccc restart-tui``: an out-of-process
  caller (an automation that changed ccc's code/config) writes it; the TUI's fast poll
  consumes it, exits cleanly and re-execs itself in the same tab. The TUI also clears any
  leftover request on mount so a stale file can never instantly restart a fresh TUI.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


from . import config

_SELECTED = "jump_selected"
_REQUEST = "jump_request"
_TUI = "jump_tui"
_TOGGLE = "jump_toggle"
_RESTART = "jump_restart"


def _read(name: str) -> str | None:
    try:
        value = (config.app_home() / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write(name: str, value: str | None) -> None:
    path = config.app_home() / name
    try:
        if value is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass


def set_selected(session_id: str | None) -> None:
    """Record the session id currently under the TUI cursor (TUI → ``ccc jump``)."""
    _write(_SELECTED, session_id)


def get_selected() -> str | None:
    """The session id last under the TUI cursor, or None."""
    return _read(_SELECTED)


def request_select(session_id: str) -> None:
    """Ask the live TUI to move its cursor to *session_id* (``ccc jump`` → TUI)."""
    _write(_REQUEST, session_id)


def peek_request() -> str | None:
    """The pending cursor-move request, or None (does not clear it)."""
    return _read(_REQUEST)


def clear_request() -> None:
    """Drop the pending cursor-move request (the TUI calls this once it has acted)."""
    _write(_REQUEST, None)


def set_tui(pid: int, iterm_session_id: str) -> None:
    """Publish the live TUI's identity (``pid|iterm_session_id``) — TUI → ``ccc jump``."""
    _write(_TUI, f"{pid}|{iterm_session_id}")


def get_tui() -> tuple[int, str] | None:
    """The live TUI's ``(pid, iterm_session_id)``, or None on missing/garbage."""
    raw = _read(_TUI)
    if not raw:
        return None
    pid_str, _, iterm_session_id = raw.partition("|")
    try:
        return int(pid_str), iterm_session_id
    except ValueError:
        return None


def clear_tui() -> None:
    """Drop the TUI identity (on unmount) so ``ccc jump`` stops using the fast path."""
    _write(_TUI, None)


def request_toggle() -> None:
    """Ask the live TUI to run the whole f+j toggle itself (``ccc jump`` → TUI)."""
    _write(_TOGGLE, "1")


def peek_toggle() -> bool:
    """True if a toggle is pending (does not clear it)."""
    return _read(_TOGGLE) is not None


def clear_toggle() -> None:
    """Drop the pending toggle (the TUI calls this once it has consumed it)."""
    _write(_TOGGLE, None)


def request_restart() -> None:
    """Ask the live TUI to restart itself in its own tab (``ccc restart-tui`` → TUI)."""
    _write(_RESTART, "1")


def peek_restart() -> bool:
    """True if a restart is pending (does not clear it)."""
    return _read(_RESTART) is not None


def clear_restart() -> None:
    """Drop the pending restart (the TUI clears it on consume AND on mount — see module doc)."""
    _write(_RESTART, None)
