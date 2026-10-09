#!/usr/bin/env python3
"""Read every iTerm2 session's tty + title in one pass, and write titles compare-and-swap.

Two primitives the session-name feature needs on top of :mod:`command_center.terminal`'s
fire-and-forget title writers:

* :func:`read_panes` — ONE synchronous ``osascript`` run listing ``(uuid, tty, name)`` for
  every iTerm session. ``ccc sessions -j`` validates a session's tab with the tty (the
  registry pid's tty must equal the tab's tty, S-IDENT); the title sync compares the live
  name with the one ccc last wrote to spot a title the user typed by hand (D3).
* :func:`set_titles_cas` — rewrite a tab's title only while it still shows what ccc last
  wrote (or nothing ccc knows of yet). A title the user changed in the meantime is left
  alone even before the detector has adopted it, so ccc can never overwrite a hand-set
  title in the window between the user's edit and the next detection pass.

macOS / iTerm2 only; both degrade to a no-op (``None`` / nothing written) elsewhere.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import shutil
import subprocess
from dataclasses import dataclass

# Field / record separators in the reader's output: ASCII unit + record separators, which
# never occur in a tty path and are stripped from titles anyway.
_FS = "\x1f"
_RS = "\x1e"
READ_TIMEOUT_SEC = 2.0

# Three bulk property reads (one Apple event each) instead of three per session: a
# per-session walk took ~1.9 s for 24 sessions, this ~0.5 s — inside a 3 s budget.
_READ_SCRIPT = """
tell application "iTerm2"
    set ids to id of every session of every tab of every window
    set ttys to tty of every session of every tab of every window
    set nms to name of every session of every tab of every window
end tell
set fs to (character id 31)
set rs to (character id 30)
set out to ""
repeat with w from 1 to count of ids
    set wi to item w of ids
    repeat with t from 1 to count of wi
        set ti to item t of wi
        repeat with s from 1 to count of ti
            set aTty to item s of item t of item w of ttys
            set aName to item s of item t of item w of nms
            set out to out & (item s of ti) & fs & aTty & fs & aName & rs
        end repeat
    end repeat
end repeat
return out
"""


@dataclass(frozen=True)
class ItermPane:
    """One iTerm2 session (pane): its UUID, controlling tty and current title (name)."""

    uuid: str
    tty: str
    name: str


def uuid_of(iterm_session_id: str | None) -> str:
    """The upper-cased UUID tail of ``$ITERM_SESSION_ID`` (``w0t1p0:UUID`` → ``UUID``)."""
    return (iterm_session_id or "").split(":")[-1].strip().upper()


def parse_panes(raw: str) -> list[ItermPane]:
    """Parse :data:`_READ_SCRIPT` output (pure — the tests feed it fixtures)."""
    panes: list[ItermPane] = []
    for record in raw.split(_RS):
        record = record.strip("\r\n")
        if not record:
            continue
        parts = record.split(_FS)
        if len(parts) < 3 or not parts[0].strip():
            continue
        panes.append(ItermPane(uuid=parts[0].strip().upper(), tty=parts[1].strip(), name=parts[2]))
    return panes


def read_panes(timeout: float = READ_TIMEOUT_SEC) -> list[ItermPane] | None:
    """Every iTerm session's ``(uuid, tty, name)``, or ``None`` when iTerm cannot be read.

    Bounded by *timeout* (an Automation prompt or a busy iTerm must not stall a caller
    with a 3 s budget). Never raises.
    """
    if not shutil.which("osascript"):
        return None
    try:
        result = subprocess.run(
            ["osascript", "-e", _READ_SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return parse_panes(result.stdout)


def cas_script_checks(entries: dict[str, tuple[str, str]], marker: str) -> str:
    """The per-session AppleScript body of :func:`set_titles_cas` (pure, for tests).

    *entries* maps ``$ITERM_SESSION_ID`` → ``(expected, core)``: the title body is set to
    *core* (keeping a leading wait *marker*) only when the current body is *expected*.
    """
    from .terminal import _as_quote  # pylint: disable=import-outside-toplevel,protected-access

    marker_q = _as_quote(marker)
    blocks = []
    for iid, (expected, core) in entries.items():
        uuid = iid.split(":")[-1].strip()
        if not uuid:
            continue
        core_q, exp_q = _as_quote(core), _as_quote(expected)
        blocks.append(
            f'if sid is "{_as_quote(uuid)}" then\n'
            f"  set n to name of s\n"
            f'  set pre to ""\n'
            f'  if n starts with "{marker_q}" then set pre to "{marker_q}"\n'
            f"  set body to n\n"
            f'  if pre is not "" then\n'
            f"    try\n"
            f"      set body to text (mlen + 1) thru -1 of n\n"
            f"    on error\n"
            f'      set body to ""\n'
            f"    end try\n"
            f"  end if\n"
            f"  considering case\n"
            f'    if body is not "{core_q}" and body is "{exp_q}" then '
            f'set name of s to (pre & "{core_q}")\n'
            f"  end considering\n"
            f"end if"
        )
    return "\n".join(blocks)


def set_titles_cas(entries: dict[str, tuple[str, str]], marker: str = "🔴 ") -> None:
    """Compare-and-swap tab titles: write *core* only where the body still is *expected*.

    Detached, like every ccc title write (see ``terminal._dispatch_title_script``).
    """
    if not entries or not shutil.which("osascript"):
        return
    checks = cas_script_checks(entries, marker)
    if not checks:
        return
    from .terminal import (  # pylint: disable=import-outside-toplevel,protected-access
        _as_quote,
        _dispatch_title_script,
    )

    _dispatch_title_script(checks, setup=f'set mlen to (count of "{_as_quote(marker)}")')
