#!/usr/bin/env python3
"""``ccc user-aims [-j] [-d N | -s SID …]`` — every session's hand-set AIM and done flag (tp#1991).

The bulk read decide's relay backfill needs (closed sessions included): for every session
whose ``updated_at`` lies in the last ``-d`` days (default 400), or for the ``-s`` ids
(at most 200; they override the window), its hand-set AIM by the naming rule
(:func:`aimprov.hand_set_aim`, via :func:`thread.hand_set_aims`), current AIM, ``done``
flag and ``updated_at``, newest ``updated_at`` first. A session with neither AIM is listed
with nulls (its ``done`` still counts); an unknown ``-s`` id is listed with every field
null and ``known: false``.

Read-only in every sense: its own ``sqlite3.connect("file:…?mode=ro", uri=True)``
connection, never ``Store()`` (which migrates and writes). A store from before the
``set_by`` column reads every row unstamped (``user_aim_by`` null); a missing state.db
answers an empty list (every ``-s`` id unknown).
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
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import config, thread
from .bridge_json import EXIT_OK, BridgeError, iso, run_json, usage

__all__ = [
    "DAYS_DEFAULT",
    "DAYS_MAX",
    "SESSIONS_MAX",
    "add_parser",
    "cmd_user_aims",
    "connect_ro",
    "user_aims",
]

#: The default window (days of ``updated_at``) and its bounds.
DAYS_DEFAULT = 400
DAYS_MAX = 3650
#: The most ``-s`` ids one call takes.
SESSIONS_MAX = 200
_DAY_MS = 86_400_000


def _uri(path: Path) -> str:
    """A read-only ``file:`` URI for *path* (``%``, ``?`` and ``#`` escaped)."""
    quoted = str(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    return f"file:{quoted}?mode=ro"


def connect_ro(path: Path) -> sqlite3.Connection:
    """*path* opened read-only (``mode=ro`` URI, 2 s busy timeout, ``sqlite3.Row`` rows)."""
    conn = sqlite3.connect(_uri(path), uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(r["name"]) == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _session_rows(
    conn: sqlite3.Connection, ids: list[str] | None, since_ms: int
) -> list[sqlite3.Row]:
    """The ``sessions`` rows of *ids* (None: every row updated at or after *since_ms*)."""
    cols = "session_id, aim, done, updated_at"
    if ids is None:
        return conn.execute(
            f"SELECT {cols} FROM sessions WHERE updated_at >= ? "
            "ORDER BY updated_at DESC, session_id",
            (since_ms,),
        ).fetchall()
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT {cols} FROM sessions WHERE session_id IN ({marks}) "
        "ORDER BY updated_at DESC, session_id",
        ids,
    ).fetchall()


def _unknown(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "known": False,
        "user_aim": None,
        "user_aim_by": None,
        "user_aim_tier": None,
        "user_aim_at": None,
        "current_aim": None,
        "done": None,
        "updated_at": None,
    }


def user_aims(
    db_path: Path, *, days: int = DAYS_DEFAULT, sessions: list[str] | None = None
) -> dict[str, Any]:
    """The ``ccc user-aims`` data ``{sessions: [...], as_of}``. Raises :class:`BridgeError`."""
    if not 1 <= days <= DAYS_MAX:
        raise usage("usage", f"-d/--days must be 1..{DAYS_MAX} (got {days})")
    ids = list(dict.fromkeys(s.strip() for s in sessions if s.strip())) if sessions else None
    if ids is not None and len(ids) > SESSIONS_MAX:
        raise usage("usage", f"at most {SESSIONS_MAX} -s/--session ids (got {len(ids)})")
    now = int(time.time() * 1000)
    rows: list[sqlite3.Row] = []
    hands: dict[str, Any] = {}
    if db_path.is_file():
        conn = connect_ro(db_path)
        try:
            rows = _session_rows(conn, ids, now - days * _DAY_MS)
            stamped = _has_column(conn, "aim_history", "set_by")
            hands = thread.hand_set_aims(conn, [str(r["session_id"]) for r in rows], set_by=stamped)
        finally:
            conn.close()
    items: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row["session_id"])
        hand = hands.get(sid)
        items.append(
            {
                "session_id": sid,
                "known": True,
                "user_aim": thread.first_line(hand.text, thread.USER_AIM_MAX) if hand else None,
                "user_aim_by": hand.by if hand else None,
                "user_aim_tier": hand.tier if hand else None,
                "user_aim_at": iso(hand.at_ms) if hand and hand.at_ms else None,
                "current_aim": thread.first_line(row["aim"], thread.AIM_MAX),
                "done": bool(row["done"]),
                "updated_at": iso(row["updated_at"]),
            }
        )
    if ids is not None:
        seen = {item["session_id"] for item in items}
        items.extend(_unknown(sid) for sid in ids if sid not in seen)
    return {"sessions": items, "as_of": iso(now)}


def _print_text(data: dict[str, Any]) -> None:
    """One line per session: ``sid8  tier  by  done  aim``."""
    if not data["sessions"]:
        print("(no sessions)")
        return
    for item in data["sessions"]:
        if not item["known"]:
            print(f"{item['session_id'][:8]}  {'unknown':<9}  {'-':<6}  {'-':<4}  -")
            continue
        done = "done" if item["done"] else "-"
        aim = item["user_aim"] or "-"
        print(
            f"{item['session_id'][:8]}  {(item['user_aim_tier'] or '-'):<9}  "
            f"{(item['user_aim_by'] or '-'):<6}  {done:<4}  {aim}"
        )


def cmd_user_aims(args: argparse.Namespace) -> int:
    """``ccc user-aims [-j] [-d N] [-s SID …]``."""

    def body() -> dict[str, Any]:
        return user_aims(config.db_path(), days=args.days, sessions=args.session)

    if getattr(args, "json", False):
        return run_json(body)
    try:
        data = body()
    except BridgeError as err:
        print(f"❌ {err.code}: {err.message}", file=sys.stderr)
        return err.exit_code
    _print_text(data)
    return EXIT_OK


def _days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number of days: {value!r}") from exc
    if not 1 <= days <= DAYS_MAX:
        raise argparse.ArgumentTypeError(f"must be 1..{DAYS_MAX} (got {days})")
    return days


def add_parser(sub: Any) -> None:
    """Register ``user-aims`` on *sub* (a :data:`bridge_cli.JSON_COMMANDS` verb)."""
    p = sub.add_parser(
        "user-aims",
        help="every session's hand-set AIM and done flag, closed ones too (read-only)",
        description=(
            "Lists each session's hand-set AIM (the naming rule: newest user/launch row,\n"
            "else newest relay/unstamped row, else a row-less session's current AIM; never\n"
            "a tool row), its stamp and tier, current AIM, done flag and updated_at, newest\n"
            f"first. Window: sessions updated in the last -d days (default {DAYS_DEFAULT});\n"
            f"-s (repeatable, at most {SESSIONS_MAX}) overrides it; an unknown id is listed\n"
            "with nulls and known: false. Opens state.db read-only (mode=ro).\n"
            "data: {sessions: [{session_id, known, user_aim, user_aim_by, user_aim_tier,\n"
            "user_aim_at, current_aim, done, updated_at}], as_of}.\n"
            "Without -j: one line per session (sid8  tier  by  done  aim)."
        ),
        epilog=(
            "examples:\n"
            "  ccc user-aims -j                 # every session updated in the last 400 days\n"
            "  ccc user-aims -j -d 30           # ... in the last 30 days\n"
            "  ccc user-aims -j -s SID -s SID2  # just these sessions (any age)\n"
            "  ccc user-aims -d 7               # one text line per session"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-d",
        "--days",
        type=_days,
        default=DAYS_DEFAULT,
        metavar="N",
        help=f"sessions updated in the last N days (1..{DAYS_MAX}, default {DAYS_DEFAULT})",
    )
    p.add_argument(
        "-s",
        "--session",
        action="append",
        metavar="SID",
        help=f"a full session id (repeatable, at most {SESSIONS_MAX}; overrides -d)",
    )
    p.add_argument(
        "-j", "--json", action="store_true", help="print the JSON envelope (schema_version 1)"
    )
    p.set_defaults(func=cmd_user_aims)
