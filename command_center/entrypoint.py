#!/usr/bin/env python3
"""The ``ccc`` dispatcher: stdlib-only fast paths for ``aim`` and ``statusline``, else ``cli``.

``ccc aim`` and ``ccc statusline --session <sid> --capture-usage`` run on every status-line
render of every live session. Importing :mod:`command_center.cli` (models, config, store,
adapters, ~33 dataclass bodies, argparse) was most of each process, so this module — the
target of the ``ccc`` console script and of ``python -m command_center`` — answers both
itself from a read-only SQLite connection (no schema DDL, no commit, only the columns it
prints), a light read of the config keys it needs and the shared stdlib modules
(:mod:`.bars`, :mod:`.slrender`, :mod:`.rolemark`, :mod:`.livestatus`, :mod:`.usagecache`,
:mod:`.staffrole`) that ``cli`` runs too. It hands the untouched argv to
:func:`command_center.cli.main` instead, before writing anything, whenever it cannot
answer exactly as ``cli`` would. ``CCC_NO_FASTPATH=1`` sends every verb to ``cli`` (the
kill switch and rollback lever).

``aim`` falls through when:

* no session id is given and none is in the environment (the live-registry lookup by
  cwd needs the adapter);
* the DB file is missing (``cli`` creates it), or any SQLite error — a missing column or
  table of an older DB (``cli`` migrates it), a corrupt file (``cli`` raises the error);
* argv is anything but the exact forms :func:`parse_aim_argv` reads by hand
  (``--session X``, ``--session=X``, ``--format F``, ``--format=F``, F a valid format):
  ``-h``, abbreviations, unknown options, invalid formats and missing values all go to
  ``cli``'s argparse parser (built by :func:`add_aim_parser`), so help, errors and exit
  codes are argparse's own bytes. Importing argparse on Python 3.14 alone costs ~100 M
  instructions (``_colorize`` → dataclasses, inspect, ast), a third of a served ``aim``.

``statusline`` (:func:`statusline_output`) falls through when:

1. argv is anything but ``--session X``, ``--session=X`` and ``--capture-usage``
   (:func:`parse_statusline_argv`): ``-h``, abbreviations, ``--print-glyph``, unknown
   options;
2. the DB file is missing, or any SQLite error;
3. the stdin payload carries a valid effort level (its write goes through ``Store``);
4. any session id is LIVE under two accounts (``cli`` warns on stderr for each);
5. config.toml exists but cannot be read or parsed;
6. any other exception while reading or rendering (``cli`` reproduces its own error).

stdin (read once, when ``--capture-usage`` is given or no ``--session``) is put back as an
:class:`io.StringIO` before any fall-through, so ``cli`` reads the same text. Everything is
read and rendered first; only then come the usage-cache write, the role-mark sync and the
single stdout write. No session id at all: the usage write only, no output.

Each output is computed whole (:func:`command_center.bars.aim_line`,
:func:`command_center.slrender.statusline_text`, shared with ``cli``) before the single
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
import io
import os
import sqlite3
import sys
from typing import TYPE_CHECKING, Any

from . import __version__, brokenpipe

if TYPE_CHECKING:
    import argparse

DESCRIPTION = "Command center for parked Claude Code sessions (AIM, progress, next-step)."

AIM_FORMATS = ("statusline", "plain", "bar")
AIM_DEFAULT_FORMAT = "statusline"


def hot_root_parser() -> argparse.ArgumentParser:
    """The root parser of a single-subcommand (hot) ``ccc`` run; see ``cli.build_parser``."""
    import argparse  # pylint: disable=import-outside-toplevel,redefined-outer-name

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
    p_aim.add_argument("--format", choices=list(AIM_FORMATS), default=AIM_DEFAULT_FORMAT)
    return p_aim


class _Unsupported(Exception):
    """The fast path cannot answer exactly like ``cli``; nothing was written yet."""


def parse_aim_argv(argv: list[str]) -> tuple[str | None, str]:
    """``(session, format)`` from ``aim``'s options, or :class:`_Unsupported`.

    Reads only what :func:`add_aim_parser` would parse to the same values: exact long
    options, ``=`` or separate-token values (a separate value never starts with ``-``,
    where argparse may read it as an option), a valid format, last occurrence wins.
    Everything else is argparse's to answer.
    """
    session: str | None = None
    fmt = AIM_DEFAULT_FORMAT
    i = 0
    while i < len(argv):
        token = argv[i]
        name, sep, value = token.partition("=")
        if name not in ("--session", "--format"):
            raise _Unsupported(token)
        if not sep:
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                raise _Unsupported(token)
            i += 1
            value = argv[i]
        if name == "--session":
            session = value
        elif value in AIM_FORMATS:
            fmt = value
        else:
            raise _Unsupported(token)
        i += 1
    return session, fmt


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
    session, fmt = parse_aim_argv(argv[1:])
    session_id = (
        session or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    )
    if not session_id:
        raise _Unsupported("session by cwd needs the live registry")
    text = aim_output(fmt, session_id)
    if text:
        sys.stdout.write(text)
    return 0


# ---- ccc statusline ----------------------------------------------------------------------


def parse_statusline_argv(argv: list[str]) -> tuple[str | None, bool]:
    """``(session, capture_usage)`` from ``statusline``'s options, or :class:`_Unsupported`.

    Reads only ``--session X`` / ``--session=X`` (a separate value never starts with ``-``;
    last one wins) and ``--capture-usage``, in any order. ``-h``, abbreviations,
    ``--print-glyph`` and anything else are argparse's to answer.
    """
    session: str | None = None
    capture = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--capture-usage":
            capture = True
            i += 1
            continue
        name, sep, value = token.partition("=")
        if name != "--session":
            raise _Unsupported(token)
        if not sep:
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                raise _Unsupported(token)
            i += 1
            value = argv[i]
        session = value
        i += 1
    return session, capture


#: The config keys the status line reads, with their ``config.DEFAULTS`` values (a test
#: pins the equality). Values are taken raw from config.toml, as ``Config(**data)`` does.
STATUSLINE_CONFIG_DEFAULTS: dict[str, Any] = {
    "aim_score": True,
    "aim_history": True,
    "aim_score_threshold": 50,
    "claude_accounts": [],
}


class SessionView:  # pylint: disable=too-many-instance-attributes,too-few-public-methods  # a row
    """The :class:`~command_center.models.Session` attributes the status line reads.

    Class attributes are ``Session``'s defaults (a test pins the equality); an instance
    carries one ``sessions`` row read with :data:`STATUSLINE_COLUMNS`.
    """

    aim: str | None = None
    short_aim: str | None = None
    aim_score: int = -1
    aim_prev: str | None = None
    aim_met: bool = False
    manual_progress: int | None = None
    next_step: str | None = None
    drift_severity: str = ""
    drift_reason: str | None = None
    drift_ack_at: int = 0
    fire_at: int = 0
    todos: str | None = None
    status: str = "idle"
    done: bool = False
    handed_off_to: str | None = None
    first_aim: str | None = None
    first_short_aim: str | None = None

    def __init__(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            setattr(self, name, value)


#: The ``sessions`` columns of :class:`SessionView`, in SELECT order (``first_aim`` /
#: ``first_short_aim`` follow from :data:`slrender.FIRST_AIM_SELECT`).
STATUSLINE_COLUMNS: tuple[str, ...] = (
    "aim",
    "short_aim",
    "aim_score",
    "aim_prev",
    "aim_met",
    "manual_progress",
    "next_step",
    "drift_severity",
    "drift_reason",
    "drift_ack_at",
    "fire_at",
    "todos",
    "status",
    "done",
    "handed_off_to",
)
#: Columns ``store._BOOL_COLUMNS`` coerces with ``bool()``.
_VIEW_BOOL_COLUMNS = frozenset({"aim_met", "done"})


def statusline_config(app_home: str) -> dict[str, Any]:
    """:data:`STATUSLINE_CONFIG_DEFAULTS` layered with config.toml, or :class:`_Unsupported`.

    A missing file is the defaults (``load_config`` does the same); an existing file that
    cannot be read or parsed falls through (``load_config`` would silently use defaults).
    """
    import tomllib

    data = {
        key: list(value) if isinstance(value, list) else value
        for key, value in STATUSLINE_CONFIG_DEFAULTS.items()
    }
    path = os.path.join(app_home, "config.toml")
    if not os.path.exists(path):
        return data
    try:
        with open(path, "rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _Unsupported("unreadable config.toml") from exc
    for key in STATUSLINE_CONFIG_DEFAULTS:
        if key in loaded:
            data[key] = loaded[key]
    return data


def _read_statusline_row(
    db: str, session_id: str
) -> tuple[SessionView, int, int, int, tuple[int, int] | None] | None:
    """``(view, checked, total, aim_index, armed)`` of *session_id*, ``None`` for no row.

    The reads of ``Store.get`` / ``progress`` / ``count_aim_history`` /
    ``armed_draft_summary`` over a read-only connection; any SQLite error (a missing column
    or table, a corrupt file) is :class:`_Unsupported`.
    """
    from .slrender import FIRST_AIM_SELECT

    columns = ", ".join(f"s.{name}" for name in STATUSLINE_COLUMNS)
    try:
        con = sqlite3.connect(_sqlite_uri(db), uri=True)
    except sqlite3.Error as exc:
        raise _Unsupported from exc
    try:
        row = con.execute(
            f"SELECT {columns}, {FIRST_AIM_SELECT} FROM sessions s WHERE s.session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        names = (*STATUSLINE_COLUMNS, "first_aim", "first_short_aim")
        values = {
            name: bool(value) if name in _VIEW_BOOL_COLUMNS else value
            for name, value in zip(names, row, strict=True)
        }
        view = SessionView(values)
        checked, total = con.execute(
            "SELECT COALESCE(SUM(checked), 0), COUNT(*) FROM subgoals WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        (history,) = con.execute(
            "SELECT COUNT(*) FROM aim_history WHERE session_id = ?", (session_id,)
        ).fetchone()
        earliest, count = con.execute(
            "SELECT MIN(fire_at), COUNT(*) FROM sessions WHERE archived = 0 AND fire_at > 0"
        ).fetchone()
    except sqlite3.Error as exc:
        raise _Unsupported from exc
    finally:
        con.close()
    aim_index = int(history) or (1 if view.aim else 0)
    armed = (int(earliest), int(count)) if count else None
    return view, int(checked), int(total), aim_index, armed


def _live_status_value(homes: list[Any], session_id: str, view: SessionView) -> str:
    """The status ``cli`` shows: the live-registry ladder when *session_id* runs, else stored.

    ``adapter.discover()`` + ``is_halted`` + ``models.derive_status`` over the shared
    :mod:`.livestatus` rules. An id LIVE under two accounts anywhere in the registry is
    :class:`_Unsupported` (``cli`` warns about it on stderr).
    """
    from . import livestatus

    status_value = view.status
    try:
        groups: dict[Any, list[dict[str, Any]]] = {}
        for home in homes:
            sessions_dir = home / "sessions"
            if not sessions_dir.is_dir():
                continue
            home_str = str(home)
            for path in sessions_dir.glob("*.json"):
                fields = livestatus.parse_registry_entry(path, home_str, livestatus.pid_alive)
                if fields is not None:
                    groups.setdefault(fields["session_id"], []).append(fields)
        chosen: dict[str, Any] | None = None
        for key, entries in groups.items():
            index, conflict = livestatus.resolve_registry_group(
                [(e["alive"], e["updated_at"], e["config_dir"]) for e in entries]
            )
            if conflict is not None:
                raise _Unsupported("session live under two accounts")
            if key == session_id:
                chosen = entries[index]
        if chosen is not None:
            halted = False
            if chosen["alive"]:
                transcript = livestatus.find_transcript(
                    [home / "projects" for home in homes], chosen["cwd"], session_id
                )
                halted = (
                    transcript is not None
                    and livestatus.halt_record(livestatus.tail_records(transcript)) is not None
                )
            status_value = livestatus.derive_status_value(
                (chosen["raw_status"], bool(chosen["alive"])),
                (bool(view.done), view.handed_off_to or ""),
                halted,
            )
    except OSError:
        pass  # cli: the stored status stands
    return status_value


def _statusline_rows(app_home: str, session_id: str, cfg: dict[str, Any], now: int) -> str:
    """The complete ``ccc statusline`` text for *session_id*, or :class:`_Unsupported`."""
    from pathlib import Path

    from . import slrender, staffrole, usagecache

    db = os.path.join(app_home, "state.db")
    if not os.path.isfile(db):
        raise _Unsupported("no DB yet")
    role_marker = staffrole.role_marker(session_id)
    staff_name = (staffrole.session_tab_name(session_id) or "") if role_marker else ""
    found = _read_statusline_row(db, session_id)
    if found is None:
        return slrender.statusline_text(
            None,
            status_value="",
            checked=0,
            total=0,
            aim_index=0,
            armed=None,
            role_marker=role_marker,
            staff_name=staff_name,
            threshold=0,
            aim_score=True,
            aim_history=True,
            now=now,
        )
    view, checked, total, aim_index, armed = found
    homes: list[Path] = list(usagecache.parse_claude_accounts(cfg["claude_accounts"]).values())
    return slrender.statusline_text(
        view,
        status_value=_live_status_value(homes, session_id, view),
        checked=checked,
        total=total,
        aim_index=aim_index,
        armed=armed,
        role_marker=role_marker,
        staff_name=staff_name,
        threshold=cfg["aim_score_threshold"],
        aim_score=bool(cfg["aim_score"]),
        aim_history=bool(cfg["aim_history"]),
        now=now,
    )


def _read_stdin_preserved() -> str:
    """Read stdin once and put the text back as a StringIO, so ``cli`` can read it again.

    A read error (``OSError``/``ValueError``) leaves an empty StringIO: ``cli``'s own read
    would have found nothing usable either. Anything else leaves stdin alone and declines.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    except Exception as exc:  # pylint: disable=broad-exception-caught  # cli reproduces it
        raise _Unsupported("stdin") from exc
    sys.stdin = io.StringIO(raw)
    return raw


def statusline_output(argv: list[str], now: int) -> tuple[str, Any]:
    """Read and render everything ``ccc statusline`` needs; ``(stdout text, writes)``.

    *argv* follows the verb; *now* is the clock every fire time is formatted against.
    *writes* is a no-argument callable doing the side effects (usage-cache write, role-mark
    sync) — nothing is written before it runs. :class:`_Unsupported` (stdin preserved)
    whenever ``cli`` must answer.
    """
    session, capture = parse_statusline_argv(argv)
    payload: dict | None = None
    if capture or not session:
        from . import usagecache

        payload = usagecache.loads_dict(_read_stdin_preserved())
    try:
        return _statusline_plan(session, capture, payload, now)
    except _Unsupported:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught  # rule 6: cli answers
        raise _Unsupported(repr(exc)) from exc


def _statusline_plan(
    session: str | None, capture: bool, payload: dict | None, now: int
) -> tuple[str, Any]:
    """The read/render phase of :func:`statusline_output` (any exception → ``cli``)."""
    from pathlib import Path

    from . import rolemark, slrender, usagecache

    sid = session
    if payload is not None:
        if slrender.effort_from_payload(payload) is not None:
            raise _Unsupported("effort write goes through Store")
        if not sid:
            derived = payload.get("session_id") or payload.get("sessionId")
            if isinstance(derived, str) and derived:
                sid = derived
    app_home = _app_home()
    cfg: dict[str, Any] | None = None
    usage_write: tuple[dict, str, Path] | None = None
    if capture and payload is not None:
        cfg = statusline_config(app_home)
        dirs = usagecache.parse_claude_accounts(cfg["claude_accounts"])
        account = usagecache.account_from_env(dirs)
        rate_limits = payload.get("rate_limits")
        if account is not None and isinstance(rate_limits, dict):
            usage_write = (rate_limits, account, usagecache.account_config_dir(account, dirs))
    text = ""
    if sid:
        text = _statusline_rows(app_home, sid, cfg or statusline_config(app_home), now)

    def writes() -> None:
        if usage_write is not None:
            rate_limits, account, account_dir = usage_write
            try:
                usagecache.write_usage(
                    rate_limits, account=account, app_home=Path(app_home), account_dir=account_dir
                )
            except OSError:
                pass
        if sid:
            rolemark.sync_role_mark(sid)  # tp#1288: the shell status line's leading badge

    return text, writes


def _serve_statusline(argv: list[str]) -> int:
    """Answer ``ccc statusline``; raises :class:`_Unsupported` before any write or output."""
    import time

    text, writes = statusline_output(argv[1:], int(time.time()))
    writes()
    if text:
        sys.stdout.write(text)
    return 0


def _fastpath_disabled() -> bool:
    """``CCC_NO_FASTPATH=1``: every verb goes to ``cli`` (kill switch, rollback lever)."""
    return os.environ.get("CCC_NO_FASTPATH") == "1"


def main(argv: list[str] | None = None) -> int:
    """Console entry point of ``ccc`` (and ``python -m command_center``)."""
    args = sys.argv[1:] if argv is None else list(argv)
    serve = {"aim": _serve_aim, "statusline": _serve_statusline}.get(args[0] if args else "")
    if serve is not None and not _fastpath_disabled():
        try:
            return brokenpipe.guard(lambda: serve(args))
        except _Unsupported:
            pass  # nothing was written: the full CLI answers the same argv
    # cli imports this module for the shared parser builder; the cycle is lazy on purpose.
    from .cli import main as cli_main  # pylint: disable=cyclic-import

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
