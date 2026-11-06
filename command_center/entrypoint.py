#!/usr/bin/env python3
"""The ``ccc`` dispatcher: a stdlib-only fast path for ``ccc aim``, else ``cli.main``.

``ccc aim`` runs once per status-line render of every live session. Importing
:mod:`command_center.cli` (models, config, store, adapters, ~33 dataclass bodies) was ~75 %
of that process, so this module — the target of the ``ccc`` console script and of
``python -m command_center`` — answers ``aim`` itself from a read-only SQLite connection
(no schema DDL, no commit, only the columns it prints) and a light read of one config
key. It hands the untouched argv to :func:`command_center.cli.main` instead, before
writing anything, whenever it cannot answer exactly as ``cli.cmd_aim`` would:

* the verb is not ``aim``;
* no session id is given and none is in the environment (the live-registry lookup by
  cwd needs the adapter);
* the DB file is missing (``cli`` creates it), or any SQLite error — a missing column or
  table of an older DB (``cli`` migrates it), a corrupt file (``cli`` raises the error).

The parser is the one :func:`add_aim_parser` builds for ``cli`` too, so ``-h``, option
errors and their exit codes are the same bytes; the output is computed whole by
:func:`command_center.bars.aim_line` (shared with ``cli.cmd_aim``) before the single
write, inside the same broken-pipe guard every console script uses.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import argparse
import os
import sqlite3
import sys
from typing import Any

from . import __version__, brokenpipe

DESCRIPTION = "Command center for parked Claude Code sessions (AIM, progress, next-step)."

AIM_FORMATS = ("statusline", "plain", "bar")


def hot_root_parser() -> argparse.ArgumentParser:
    """The root parser of a single-subcommand (hot) ``ccc`` run; see ``cli.build_parser``."""
    root = argparse.ArgumentParser(
        prog="ccc",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=DESCRIPTION,
    )
    root.add_argument("--version", action="version", version=f"ccc {__version__}")
    return root


def add_aim_parser(sub: Any) -> argparse.ArgumentParser:
    """`ccc aim` — the status line's progress-bar lookup, once per render."""
    p_aim: argparse.ArgumentParser = sub.add_parser(
        "aim", help="status-line lookup of a session's done-condition"
    )
    p_aim.add_argument("--session")
    p_aim.add_argument("--format", choices=list(AIM_FORMATS), default="statusline")
    return p_aim


class _Unsupported(Exception):
    """The fast path cannot answer exactly like ``cli``; nothing was written yet."""


def _app_home() -> str:
    """``config.app_home()`` without importing config: ``$CCC_HOME`` > ``$CLAUDE_HOME``."""
    home = os.environ.get("CCC_HOME") or os.environ.get("CLAUDE_HOME")
    if not home:
        home = os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(home, "command-center")


def _sqlite_uri(path: str) -> str:
    """A read-only ``file:`` URI for *path* (``%``, ``?`` and ``#`` escaped)."""
    quoted = path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    return f"file:{quoted}?mode=ro"


def aim_on_main_line(app_home: str) -> bool:
    """``load_config().aim_on_main_line`` from config.toml alone (unreadable → default)."""
    import tomllib

    try:
        with open(os.path.join(app_home, "config.toml"), "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False  # missing → DEFAULTS; load_config falls back to DEFAULTS when unreadable
    return bool(data.get("aim_on_main_line", False))


def _read_session(db: str, session_id: str) -> tuple[Any, ...] | None:
    """``(aim, manual_progress, aim_met, deadline, blocked_on, checked, total)`` or None."""
    try:
        con = sqlite3.connect(_sqlite_uri(db), uri=True)
    except sqlite3.Error as exc:
        raise _Unsupported from exc
    try:
        row = con.execute(
            "SELECT aim, manual_progress, aim_met, deadline, blocked_on "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or not row[0]:
            return None
        done, total = con.execute(
            "SELECT COALESCE(SUM(checked), 0), COUNT(*) FROM subgoals WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise _Unsupported from exc
    finally:
        con.close()
    return (*row, int(done), int(total))


def aim_output(fmt: str, session_id: str) -> str:
    """The complete stdout of ``ccc aim`` for *session_id*, or :class:`_Unsupported`."""
    from .bars import aim_line, effective_progress

    app_home = _app_home()
    db = os.path.join(app_home, "state.db")
    if not os.path.isfile(db):
        raise _Unsupported("no DB yet")
    found = _read_session(db, session_id)
    if found is None:
        return aim_line(fmt, None)
    aim, manual, aim_met, deadline, blocked_on, checked, total = found
    if fmt == "plain":
        return aim_line(fmt, aim)
    return aim_line(
        fmt,
        aim,
        fraction=effective_progress(manual, checked, total),
        aim_met=bool(aim_met),
        deadline=deadline,
        blocked_on=blocked_on,
        main_line=fmt == "bar" and aim_on_main_line(app_home),
    )


def _serve_aim(argv: list[str]) -> int:
    """Parse and answer ``ccc aim``; raises :class:`_Unsupported` before any output."""
    parser = hot_root_parser()
    add_aim_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    session_id = (
        args.session
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
    )
    if not session_id:
        raise _Unsupported("session by cwd needs the live registry")
    text = aim_output(args.format, session_id)
    if text:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console entry point of ``ccc`` (and ``python -m command_center``)."""
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] == "aim":
        try:
            return brokenpipe.guard(lambda: _serve_aim(args))
        except _Unsupported:
            pass  # nothing was written: the full CLI answers the same argv
    # cli imports this module for the shared parser builder; the cycle is lazy on purpose.
    from .cli import main as cli_main  # pylint: disable=cyclic-import

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
