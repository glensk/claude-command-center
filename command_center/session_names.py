#!/usr/bin/env python3
"""Hand-set session names (``ccc name``) and ccc's tab-title bookkeeping.

ccc generates no session names itself (no LLM name, no deterministic fallback; the
automatic namer and its ``session_names`` key are gone, tp#1808). What stays:

* **``ccc name -s ID "x y"``** stores a name the user (or an agent on its behalf, e.g. a
  staff agent's ``tab_name``) chose — origin ``manual``. A name another ACTIVE session
  (not done, not archived) already holds is refused (:class:`NameTaken`), casefolded,
  inside one ``BEGIN IMMEDIATE`` transaction. Running Claude processes are never renamed
  by ccc — the command prints ``/rename <name>`` for the user to type; ccc launches pass
  it as ``claude --name`` (:mod:`command_center.launch_argv`).
* **``ccc name -s ID --reset``** drops a hand-set name (``manual``, or ``manual-tab`` from
  an older ccc that adopted hand-typed tab titles) and unfreezes the tab title.
* **Tab-title bookkeeping** for :mod:`command_center.tabsymbol`: what ccc last wrote
  (:func:`record_title_write`) and whether a tab is frozen (:func:`titles_frozen`).

Import-light: the store and config layers are imported lazily where used.
"""

# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import re
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Session
    from .store import Store

ORIGIN_MANUAL = "manual"
ORIGIN_MANUAL_TAB = "manual-tab"
# Origins the user authored; only these are dropped by ``ccc name --reset``.
MANUAL_ORIGINS = frozenset({ORIGIN_MANUAL, ORIGIN_MANUAL_TAB})

# A name the user types (``ccc name``) stays a name, not a sentence.
MAX_MANUAL_CHARS = 48

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WS_RE = re.compile(r"\s+")


class NameTaken(Exception):
    """``ccc name`` asked for a name another active session already holds."""

    def __init__(self, name: str, holder: str) -> None:
        super().__init__(f"name {name!r} is already used by session {holder[:8]}")
        self.name = name
        self.holder = holder


def clean_manual(name: str) -> str:
    """A user-typed name (``ccc name``): control characters removed, whitespace collapsed."""
    return _WS_RE.sub(" ", _CONTROL_RE.sub("", name or "")).strip()


def _key(name: str) -> str:
    return name.casefold()


def _taken(conn: sqlite3.Connection, exclude: str) -> dict[str, str]:
    """``{casefolded name: session id}`` of every ACTIVE named row except *exclude*."""
    rows = conn.execute(
        "SELECT session_id, canonical_name FROM sessions "
        "WHERE canonical_name IS NOT NULL AND canonical_name != '' "
        "AND done = 0 AND archived = 0 AND session_id != ?",
        (exclude,),
    ).fetchall()
    return {_key(str(r[1])): str(r[0]) for r in rows}


def rename(store: Store, session_id: str, name: str) -> str:
    """``ccc name``: set *name* (origin ``manual``). Raises :class:`NameTaken` / ``ValueError``.

    Clears ``title_written`` so ccc's next title write lands unconditionally — the user
    asked for this name, so it may replace even a hand-set tab title.
    """
    cleaned = clean_manual(name)
    if not cleaned:
        raise ValueError("a name needs at least one visible character")
    if len(cleaned) > MAX_MANUAL_CHARS:
        raise ValueError(f"a name is at most {MAX_MANUAL_CHARS} characters")
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"no session {session_id}")
        taken = _taken(conn, session_id)
        if _key(cleaned) in taken:
            raise NameTaken(cleaned, taken[_key(cleaned)])
        conn.execute(
            "UPDATE sessions SET canonical_name = ?, canonical_name_origin = ?, "
            "name_source_aim = NULL, title_written = NULL, updated_at = ? "
            "WHERE session_id = ?",
            (cleaned, ORIGIN_MANUAL, int(time.time() * 1000), session_id),
        )
        conn.commit()
        return cleaned
    except BaseException:
        conn.rollback()
        raise


def reset_name(store: Store, session_id: str) -> str | None:
    """``ccc name --reset``: drop a hand-set name and unfreeze the session's tab title.

    Only a ``manual`` / ``manual-tab`` name is dropped (``None`` is returned); any other
    stored name (from an older ccc's automatic namer) is returned as it is. ``title_written``
    is cleared so the next title sync writes the tab unconditionally. Raises
    ``ValueError`` for an unknown session.
    """
    session = store.get(session_id)
    if session is None:
        raise ValueError(f"no session {session_id}")
    if session.canonical_name_origin not in MANUAL_ORIGINS:
        return session.canonical_name or None
    store.conn.execute(
        "UPDATE sessions SET canonical_name = NULL, canonical_name_origin = '', "
        "name_source_aim = NULL, name_upgrade_tries = 0, title_written = NULL, "
        "updated_at = ? WHERE session_id = ? AND canonical_name_origin = ?",
        (int(time.time() * 1000), session_id, session.canonical_name_origin),
    )
    store.conn.commit()
    return None


# --------------------------------------------------------------------------- #
# Tab-title bookkeeping
# --------------------------------------------------------------------------- #
def record_title_write(store: Store, session_id: str, core: str, now_ms: int | None = None) -> None:
    """Remember that ccc just wrote *core* as this session's tab title (generation += 1)."""
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    store.conn.execute(
        "UPDATE sessions SET title_written = ?, title_generation = title_generation + 1, "
        "title_written_at = ? WHERE session_id = ?",
        (core, stamp, session_id),
    )
    store.conn.commit()


def titles_frozen(session: Session) -> bool:
    """True when ccc must never write this session's tab title (an older ccc adopted a
    title the user typed by hand; ``ccc name --reset`` unfreezes it)."""
    return session.canonical_name_origin == ORIGIN_MANUAL_TAB
