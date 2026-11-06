#!/usr/bin/env python3
"""One ``session-close`` line in ``events.log`` for every path that ends a session's Claude.

tp#1785: a resumed job's ``claude`` exited after its first turn and nothing in ``events.log``
said who ended it — most close paths logged only their refusals. Every path that signals a
session's Claude (or closes the pane/tab it runs in) now writes one line here AFTER the act,
so the next unexplained exit is either explained by a line or proven to come from outside
ccc. A line: ``<timestamp>\\t<session id>\\tsession-close\\tpid=<pid> path=<path>
caller=<caller>[ <note>]``.

*path* names the code path (``close-now``, ``close-claim``, ``handoff-close``,
``bridge-close``, ``switch-now``, ``switch-claim``, ``daemon-reap``, ``resume-reap``,
``tui-close``); *caller* is the process that acted (:func:`caller`). Never raises.
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
import signal
import sys
import time

from . import config

EVENT = "session-close"


def caller() -> str:
    """This process as ``"<prog> <subcommand> pid=<pid> ppid=<ppid>"`` (argv beyond it omitted).

    Only the program name and the first argument: later arguments can carry a prompt.
    """
    argv = [os.path.basename(sys.argv[0])] if sys.argv and sys.argv[0] else []
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        argv.append(sys.argv[1])
    prog = " ".join(argv) or "?"
    return f"{prog} pid={os.getpid()} ppid={os.getppid()}"


def sigterm(pid: int, session_id: str, path: str) -> bool:
    """SIGTERM *pid* and log it via *path* — ``False`` (nothing logged) when it was gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    log_close(session_id, pid, path)
    return True


def log_close(session_id: str, pid: int, path: str, *, note: str = "", who: str = "") -> None:
    """Append one ``session-close`` line: *session_id*'s *pid* was ended via *path*.

    *who* overrides the :func:`caller` default; *note* is appended verbatim (one line).
    """
    try:
        detail = f"pid={pid} path={path} caller={who or caller()}"
        if note:
            detail += " " + " ".join(note.split())
        log_path = config.app_home() / "events.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{session_id or '-'}\t{EVENT}\t{detail}\n")
    except OSError:
        pass
