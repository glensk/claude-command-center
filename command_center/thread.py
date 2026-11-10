#!/usr/bin/env python3
"""A session's thread: its handoff chain's root id and the chain's real AIM changes (tp#1989).

A *thread* is every session one piece of work ran in across ``ccc continue-session``
relays. Its id is the chain root's session id: ``sessions.thread_id`` when ``accept``
recorded it, else the root reached by walking back (the rule of ``ccc lineage``,
:mod:`command_center.bridge_lineage`: the row's ``handed_off_from``, else the best
non-failed transfer into the session; at most :data:`MAX_HOPS` hops, cycle guard).

The walk is batched level by level (one ``sessions`` and at most one ``transfers`` query
per hop depth for every asked id at once), so ``ccc sessions -j`` pays a few queries per
call, not a few per session and hop.

The thread's AIM history is computed on read from the per-session ``aim_history`` rows of
the path root → session, dropping the rows ccc itself copied at a relay
(:func:`carry_entries`). Read-only: this module only SELECTs.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position  # the direct-run shim comes first
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import aimtext
from .bridge_json import iso
from .models import AIM_SET_BY_USER

if TYPE_CHECKING:
    import sqlite3

__all__ = ["MAX_HOPS", "AimEntry", "ThreadFacts", "ThreadPath", "facts", "thread_aims", "walk"]

#: Max characters of a ``user_aim`` (the tp#1986 naming rule).
USER_AIM_MAX = 200
#: Max characters of an entry's ``aim`` and of ``current_aim`` (first line).
AIM_MAX = 300
#: Upper bound of the hops one walk follows (``bridge_lineage.MAX_HOPS``, pinned by a test).
MAX_HOPS = 50
#: The newest entries one thread view keeps (``truncated`` when more exist).
MAX_ENTRIES = 50
#: Ids per ``IN (…)`` list (well under SQLite's bound-parameter limit).
_CHUNK = 500

# The transfer states, most settled first: the order ``bridge_lineage._INTO`` sorts by.
_STATE_RANK = {"finalized": 0, "accepted": 1, "opened": 2}


@dataclass
class ThreadPath:
    """Where *session_id* sits in its thread."""

    session_id: str
    #: The sessions root → *session_id* (oldest first; the last one is *session_id*).
    path: list[str] = field(default_factory=list)
    thread_id: str = ""
    #: False when the walk was cut (cycle, > :data:`MAX_HOPS`) or a session on the path
    #: has no row, and no recorded ``thread_id`` column vouches for the id.
    exact: bool = True


@dataclass(frozen=True)
class AimEntry:
    """One real AIM change of a thread (a ccc relay copy is never one)."""

    at_ms: int  # epoch ms the AIM became current (0 = unknown)
    aim: str  # the full text
    by: str | None  # aim_history.set_by: user / launch / tool / None (legacy)
    session_id: str
    hop: int  # the session's 0-based position on the path (the status line's ``N:``)


def _chunks(ids: Iterable[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    for sid in ids:
        batch.append(sid)
        if len(batch) >= _CHUNK:
            yield batch
            batch = []
    if batch:
        yield batch


def _marks(ids: list[str]) -> str:
    return ",".join("?" * len(ids))


def _session_rows(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for batch in _chunks(ids):
        rows = conn.execute(
            "SELECT session_id, handed_off_from, thread_id, aim, aim_changed_at, created_at "
            f"FROM sessions WHERE session_id IN ({_marks(batch)})",
            batch,
        ).fetchall()
        out.update({str(r["session_id"]): r for r in rows})
    return out


def _transfer_parents(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, str]:
    """The source of the best non-failed transfer into each id (``bridge_lineage`` order)."""
    best: dict[str, tuple[tuple[int, int, str], str]] = {}
    for batch in _chunks(ids):
        rows = conn.execute(
            "SELECT target_session, source_session, state, created_at, transfer_id "
            f"FROM transfers WHERE target_session IN ({_marks(batch)}) AND state != 'failed' "
            "AND COALESCE(source_session, '') != ''",
            batch,
        ).fetchall()
        for row in rows:
            key = (
                _STATE_RANK.get(str(row["state"]), 3),
                -int(row["created_at"] or 0),
                str(row["transfer_id"]),
            )
            target = str(row["target_session"])
            if target not in best or key < best[target][0]:
                best[target] = (key, str(row["source_session"]))
    return {target: source for target, (_key, source) in best.items()}


def _walk(
    conn: sqlite3.Connection, session_ids: Iterable[str], max_hops: int = MAX_HOPS
) -> tuple[dict[str, ThreadPath], dict[str, sqlite3.Row]]:
    """Walk every id back to its root at once; returns the paths and the session rows read."""
    chains = {sid: [sid] for sid in dict.fromkeys(s for s in session_ids if s)}
    seen = {sid: {sid} for sid in chains}
    cut: set[str] = set()
    active = set(chains)
    rows: dict[str, sqlite3.Row] = {}
    asked: dict[str, str | None] = {}  # id → its transfer parent (None = none), once read
    while active:
        currents = {chains[sid][-1] for sid in active}
        rows.update(_session_rows(conn, [c for c in currents if c not in rows]))
        recorded = {
            c: str((rows[c]["handed_off_from"] if c in rows else "") or "").strip()
            for c in currents
        }
        need = [c for c in currents if not recorded[c] and c not in asked]
        if need:
            found = _transfer_parents(conn, need)
            asked.update({c: found.get(c) for c in need})
        for sid in list(active):
            current = chains[sid][-1]
            parent = recorded[current] or asked.get(current)
            if not parent:
                active.discard(sid)
            elif parent in seen[sid] or len(chains[sid]) - 1 >= max_hops:
                cut.add(sid)  # a cycle, or more hops than one walk follows
                active.discard(sid)
            else:
                chains[sid].append(parent)
                seen[sid].add(parent)
    paths: dict[str, ThreadPath] = {}
    for sid, chain in chains.items():
        column = next(
            (str(rows[c]["thread_id"]) for c in chain if c in rows and rows[c]["thread_id"]),
            "",
        )
        exact = bool(column) or (sid not in cut and all(c in rows for c in chain))
        paths[sid] = ThreadPath(sid, list(reversed(chain)), column or chain[-1], exact)
    return paths, rows


def walk(conn: sqlite3.Connection, session_id: str) -> ThreadPath:
    """*session_id*'s thread path and id (an unknown id is its own, inexact, thread)."""
    paths, _rows = _walk(conn, [session_id])
    return paths.get(session_id) or ThreadPath(session_id, [session_id], session_id, False)


def _history(
    conn: sqlite3.Connection, ids: Iterable[str]
) -> dict[str, list[tuple[str, int, str | None]]]:
    """``aim_history`` rows ``(aim, created_at, set_by)`` per session, oldest first."""
    out: dict[str, list[tuple[str, int, str | None]]] = {}
    for batch in _chunks(ids):
        rows = conn.execute(
            "SELECT session_id, aim, created_at, set_by FROM aim_history "
            f"WHERE session_id IN ({_marks(batch)}) ORDER BY session_id, created_at, id",
            batch,
        ).fetchall()
        for r in rows:
            out.setdefault(str(r["session_id"]), []).append(
                (str(r["aim"]), int(r["created_at"] or 0), r["set_by"])
            )
    return out


def _session_entries(
    sid: str,
    hop: int,
    history: dict[str, list[tuple[str, int, str | None]]],
    rows: dict[str, sqlite3.Row],
) -> list[AimEntry]:
    """*sid*'s own revisions; a session with an AIM but no rows gets one synthesized entry."""
    revisions = history.get(sid)
    if revisions:
        return [AimEntry(at, aim, by, sid, hop) for aim, at, by in revisions]
    row = rows.get(sid)
    aim = str((row["aim"] if row is not None else "") or "")
    if not aim.strip() or row is None:
        return []
    at = int(row["aim_changed_at"] or 0) or int(row["created_at"] or 0)
    return [AimEntry(at, aim, None, sid, hop)]  # as `ccc aim-history` (synthesize_aim_revision)


def carry_entries(
    path: list[str],
    history: dict[str, list[tuple[str, int, str | None]]],
    rows: dict[str, sqlite3.Row],
) -> list[AimEntry]:
    """The thread's real AIM changes along *path* (oldest first), relay copies dropped.

    The carry rule (deterministic, works on legacy rows): every session after the root
    skips (1) its FIRST row when the text equals the thread's first kept entry (the anchor
    ``carry_first_aim`` copies) and (2) any row whose text equals the last kept entry (the
    carried current AIM, a no-op re-set). A real change back to an older wording is kept:
    it differs from the last entry.
    """
    kept: list[AimEntry] = []
    for hop, sid in enumerate(path):
        for index, entry in enumerate(_session_entries(sid, hop, history, rows)):
            text = entry.aim.strip()
            if hop > 0 and kept:
                if index == 0 and text == kept[0].aim.strip():
                    continue
                if text == kept[-1].aim.strip():
                    continue
            kept.append(entry)
    return kept


def first_line(text: str | None, limit: int) -> str | None:
    """*text*'s first non-blank line, clipped to *limit* characters (``…``); None if blank."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 1] + "…"


def user_aim_text(revisions: list[tuple[str, int, str | None]], current: str | None) -> str | None:
    """The full text :meth:`Store.latest_user_aim` answers, from already-read rows.

    The newest ``user`` row; else the current AIM (its trailing tp-id run stripped unless
    multi-line); else the first row; else None.
    """
    users = [aim for aim, _at, by in revisions if by == AIM_SET_BY_USER]
    if users:
        return users[-1]
    cur = (current or "").strip()
    text = cur if "\n" in cur else aimtext.words(cur).strip()
    if text:
        return text
    if revisions and revisions[0][0].strip():
        return revisions[0][0]
    return None


@dataclass(frozen=True)
class ThreadFacts:
    """The per-session thread fields ``ccc sessions -j`` reports."""

    thread_id: str
    user_aim: str | None  # first line, ≤ USER_AIM_MAX
    aims_n: int  # the number of the thread's real AIM changes (before any truncation)


def facts(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, ThreadFacts]:
    """:class:`ThreadFacts` of every id, batched (the walk, then ONE ``aim_history`` read)."""
    paths, rows = _walk(conn, session_ids)
    members = dict.fromkeys(sid for p in paths.values() for sid in p.path)
    history = _history(conn, members)
    out: dict[str, ThreadFacts] = {}
    for sid, path in paths.items():
        row = rows.get(sid)
        current = str((row["aim"] if row is not None else "") or "")
        user = user_aim_text(history.get(sid, []), current) if row is not None else None
        out[sid] = ThreadFacts(
            thread_id=path.thread_id,
            user_aim=first_line(user, USER_AIM_MAX),
            aims_n=len(carry_entries(path.path, history, rows)),
        )
    return out


def entry_json(entry: AimEntry) -> dict[str, Any]:
    """One ``aims[]`` item: ``{at, aim, aim_full_len, by, session_id, hop}``."""
    return {
        "at": iso(entry.at_ms) if entry.at_ms > 0 else None,
        "aim": first_line(entry.aim, AIM_MAX) or "",
        "aim_full_len": len(entry.aim),
        "by": entry.by,
        "session_id": entry.session_id,
        "hop": entry.hop,
    }


def thread_aims(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """*session_id*'s thread and its AIM history (``ccc lineage -j`` ``data.thread``).

    ``{thread_id, thread_exact, current_aim, user_aim, aims: [entry…], truncated}``:
    ``aims`` oldest first, the :data:`MAX_ENTRIES` newest (``truncated`` when cut);
    ``current_aim`` the session's AIM and ``user_aim`` the latest one the user set (first
    lines, ≤ :data:`AIM_MAX` / :data:`USER_AIM_MAX`; null when none). Read-only.
    """
    paths, rows = _walk(conn, [session_id])
    path = paths.get(session_id) or ThreadPath(session_id, [session_id], session_id, False)
    history = _history(conn, path.path)
    entries = carry_entries(path.path, history, rows)
    row = rows.get(session_id)
    current = str((row["aim"] if row is not None else "") or "")
    user = user_aim_text(history.get(session_id, []), current) if row is not None else None
    return {
        "thread_id": path.thread_id,
        "thread_exact": path.exact,
        "current_aim": first_line(current, AIM_MAX),
        "user_aim": first_line(user, USER_AIM_MAX),
        "aims": [entry_json(e) for e in entries[-MAX_ENTRIES:]],
        "truncated": len(entries) > MAX_ENTRIES,
    }
