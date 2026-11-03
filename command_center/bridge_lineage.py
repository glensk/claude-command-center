#!/usr/bin/env python3
"""``ccc lineage -s ID -j`` — a session's continue-session predecessors (tp#1481 P2).

The bridge verb decide's relay calls for a session's chain of predecessors. Read-only: it
only SELECTs from ccc's store, never writes. It walks BACK from the session, one hop at a
time, newest first:

1. the session row's ``handed_off_from`` (written when the successor accepted the handoff,
   :meth:`TransferStoreMixin.set_handoff_lineage`) — its ``at`` and ``state`` come from the
   matching non-failed ``transfers`` row (source → this session), else ``handed_off_at``
   and ``state: null``;
2. else the best non-failed ``transfers`` row whose ``target_session`` is the session
   (``finalized`` before ``accepted`` before ``opened`` before ``prepared``, then the
   newest). A ``failed`` transfer never was a hop and is skipped.

At most :data:`MAX_HOPS` hops; a predecessor already in the chain (or the session itself)
cuts the walk (cycle guard). Either cut sets ``truncated``. An unknown session id answers
an empty chain, not an error.

``data = {session_id, chain: [{session_id, at, via: "continue-session", state}],
truncated}``; ``at`` is ISO-8601 UTC (when the successor took over: accepted, else
finalized / opened / created; ``null`` when unknown), ``state`` the transfer state
(``prepared`` / ``opened`` / ``accepted`` / ``finalized``) or ``null`` for a hop known only
from ``handed_off_from``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

from typing import TYPE_CHECKING, Any

from .bridge_json import EXIT_USAGE, BridgeError, iso

if TYPE_CHECKING:
    import sqlite3

    from .bridge_target import BridgeDeps
    from .store import Store

#: Upper bound of the hops one call returns (decide keeps at most 20 of them).
MAX_HOPS = 50
#: The ``via`` of every hop this verb reports.
VIA = "continue-session"

# Non-failed transfers into a session, the most settled first, then the newest.
_INTO = (
    "SELECT source_session, state, created_at, opened_at, accepted_at, finalized_at "
    "FROM transfers WHERE target_session = ? AND state != 'failed' "
    "AND COALESCE(source_session, '') != '' {extra}"
    "ORDER BY CASE state WHEN 'finalized' THEN 0 WHEN 'accepted' THEN 1 "
    "WHEN 'opened' THEN 2 ELSE 3 END, created_at DESC, transfer_id LIMIT 1"
)


def _hop_at(row: sqlite3.Row) -> int:
    """When the successor took over: accepted, else finalized, opened, created (epoch-ms)."""
    for column in ("accepted_at", "finalized_at", "opened_at", "created_at"):
        value = int(row[column] or 0)
        if value > 0:
            return value
    return 0


def _transfer_into(store: Store, target: str, source: str = "") -> sqlite3.Row | None:
    """The best non-failed transfer into *target* (from *source* when given)."""
    if source:
        sql = _INTO.format(extra="AND source_session = ? ")
        params: tuple[str, ...] = (target, source)
    else:
        sql = _INTO.format(extra="")
        params = (target,)
    row: sqlite3.Row | None = store.conn.execute(sql, params).fetchone()
    return row


def predecessor(store: Store, session_id: str) -> dict[str, Any] | None:
    """The one hop before *session_id*, or ``None`` when ccc knows none."""
    session = store.get(session_id)
    recorded = ((session.handed_off_from if session else None) or "").strip()
    if recorded:
        row = _transfer_into(store, session_id, recorded)
        if row is not None:
            return {
                "session_id": recorded,
                "at": iso(_hop_at(row)),
                "via": VIA,
                "state": row["state"],
            }
        at = session.handed_off_at if session else None
        return {"session_id": recorded, "at": iso(at), "via": VIA, "state": None}
    row = _transfer_into(store, session_id)
    if row is None:
        return None
    return {
        "session_id": str(row["source_session"]),
        "at": iso(_hop_at(row)),
        "via": VIA,
        "state": row["state"],
    }


def lineage(store: Store, session_id: str, max_hops: int = MAX_HOPS) -> dict[str, Any]:
    """*session_id*'s predecessors, newest first (the envelope's ``data``)."""
    chain: list[dict[str, Any]] = []
    seen = {session_id}
    current = session_id
    truncated = False
    while True:
        hop = predecessor(store, current)
        if hop is None:
            break
        if hop["session_id"] in seen or len(chain) >= max_hops:
            truncated = True  # a cycle, or more hops than one call returns
            break
        chain.append(hop)
        seen.add(hop["session_id"])
        current = hop["session_id"]
    return {"session_id": session_id, "chain": chain, "truncated": truncated}


def run_lineage(deps: BridgeDeps, session_id: str) -> dict[str, Any]:
    """``ccc lineage -s ID -j``: the chain of *session_id* (full id; unknown = empty chain)."""
    sid = (session_id or "").strip()
    if not sid:
        raise BridgeError("usage", "pass -s/--session <full session id>", EXIT_USAGE)
    with deps.store() as store:
        return lineage(store, sid)
