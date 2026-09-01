#!/usr/bin/env python3
"""``ccc jump`` — a context-aware toggle between the ccc TUI and a session tab.

Bound to a global Karabiner chord (hold ``f``, tap ``j``). What it does depends on
where you are when you fire it:

- **In a Claude session tab** (iTerm frontmost, not the ccc tab) — ask the live TUI
  to move its cursor to *that* session's row, then bring the ccc tab forward.
- **In the ccc tab** — act like the TUI's ``r``: bring the currently-selected
  session's tab forward (or resume it in a new tab if its tab is gone).
- **In another app** (or with ``--no-toggle``) — just bring the ccc tab forward;
  if no TUI is running, open one.

So tapping ``f+j`` repeatedly flips between the command center and the tab you came
from. The TUI itself is located by the bare ``ccc`` process's controlling tty
(title-independent); coordination with the live TUI goes through :mod:`jumpstate`.

Two paths do this:

- **Fast path** (a live TUI is running) — ``ccc jump`` only writes the toggle *verb*
  (:func:`jumpstate.request_toggle`) and returns (~80 ms). The resident TUI, which
  holds a warm iTerm2 API link (see :mod:`iterm_api`), decides context and focuses
  in-process — no ps scan, no osascript walk here.
- **Slow path** (no TUI, or ``--no-toggle``) — this process does the whole toggle
  itself: ``ps`` to find the ccc tty, then AppleScript to read the focused session and
  focus the target tab (~1 s). It must keep working with no TUI at all.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import argparse
import os
import subprocess
import sys

from . import accounts, config, jumpstate, terminal
from .store import Store


def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* exists (signal 0 probe).

    A local copy of adapters.claude's private helper — the fast path only needs to
    know the recorded TUI pid is still around before handing it the toggle.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def find_ccc_tty() -> str | None:
    """Return ``/dev/ttysNNN`` of the running bare ``ccc`` TUI process, or None.

    Matches a tty-attached process whose command line ends in ``/ccc`` or
    ``/ccc tui`` — the interactive TUI — while excluding ``ccc daemon`` and
    one-shot subcommands (``ccc ls``, ``ccc aim …``), which either carry extra
    arguments or run without a controlling tty.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "tty=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        tty, args = parts[0], parts[1].rstrip()
        if not tty.startswith("ttys"):
            continue
        if args.endswith("/ccc") or args.endswith("/ccc tui"):
            return f"/dev/{tty}"
    return None


def _session_for_uuid(uuid: str) -> str | None:
    """The tracked session id whose iTerm tab UUID is *uuid*, or None."""
    try:
        with Store() as store:
            for session in store.list_sessions():
                isid = session.iterm_session_id
                if isid and isid.split(":")[-1] == uuid:
                    return session.session_id
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None
    return None


def _resume_selected() -> int:
    """``r``-equivalent: focus (or resume) the TUI's currently-selected session's tab."""
    sid = jumpstate.get_selected()
    if not sid:
        return 1
    with Store() as store:
        session = store.get(sid)
    if session is None or not session.cwd:
        return 1
    if session.iterm_session_id and terminal.focus_iterm_session(session.iterm_session_id):
        return 0
    # Fail closed like cmd_resume / cmd_resume_job (D8/R1): past this point a NEW
    # process is launched, so an unattributed row or a D9 conflict must refuse rather
    # than silently bill the default seat. jump fires from a global Karabiner chord
    # with no terminal attached — stderr + non-zero exit, never an interactive prompt.
    if not session.config_dir and accounts.is_multi_account():
        print(
            f"error: {sid} has no recorded Claude account (config_dir) and several are "
            "configured — refusing to resume rather than risk billing the wrong account. "
            "Start a turn from the intended account, then retry.",
            file=sys.stderr,
        )
        return 1
    if accounts.live_conflict(sid):
        print(
            f"error: {sid} is live under two Claude accounts at once — close one of "
            "them, then resume.",
            file=sys.stderr,
        )
        return 1
    resumed = terminal.resume_in_new_tab(
        session.cwd, sid, session.config_dir, no_codex=bool(session.no_codex)
    )
    return 0 if resumed else 1


def _focus_ccc(ccc_tty: str | None, no_launch: bool) -> int:
    """Bring the ccc TUI tab to the front (by tty, then title), else launch one."""
    if ccc_tty and terminal.focus_tty(ccc_tty):
        return 0
    title = (config.load_config().tab_title or "").strip()
    if title and terminal.focus_session_name(title):
        return 0
    if no_launch:
        print("ccc TUI not found in any iTerm tab", file=sys.stderr)
        return 1
    return 0 if terminal.launch_ccc_tab() else 1


def run(args: argparse.Namespace) -> int:
    """Toggle between the ccc TUI and the focused session's tab (see module docstring)."""
    # Fast path: a live TUI owns the whole toggle (warm iTerm2 API link — see
    # iterm_api). This process then only writes the request verb (~80 ms total)
    # instead of paying ps + 2-3 osascript walks (~1 s). --no-toggle keeps the
    # old direct path (it must work with no TUI at all).
    if not getattr(args, "no_toggle", False):
        ident = jumpstate.get_tui()
        if ident is not None and _pid_alive(ident[0]):
            jumpstate.request_toggle()
            return 0
    ccc_tty = find_ccc_tty()
    # Context-aware toggle only when the user is actually looking at iTerm.
    if not getattr(args, "no_toggle", False) and terminal.is_iterm_frontmost():
        current = terminal.current_iterm_session()
        if current:
            uuid, tty = current
            if ccc_tty and tty == ccc_tty:
                # We're in the ccc tab → jump to the selected session's tab (like `r`).
                return _resume_selected()
            # We're in a session tab → ask the TUI to select it, then focus ccc.
            sid = _session_for_uuid(uuid)
            if sid:
                jumpstate.request_select(sid)
    return _focus_ccc(ccc_tty, getattr(args, "no_launch", False))
