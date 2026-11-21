#!/usr/bin/env python3
"""Store table of the relay thread joins: ``thread_joins`` (a ``Store`` mixin, tp#2372 F2a).

One row per ``ccc new-job --thread-from SID``: the job (the TARGET) continues SOURCE's
thread. The row is the ``state.db`` side of the operation-bound succession (the shared
reservation in tp's file follows in a later box); its op id is the token a staff
hand-back cancels by. Two join modes:

- ``start`` (default, the park relay; its source tab is closed): the job's first
  SessionStart joins the thread and settles the row ``accepted``.
- ``accept`` (the staff relay; its source keeps draining): SessionStart records the
  lineage only; ``ccc thread-accept`` (the successor's consent moment) settles it
  ``accepted``, ``ccc thread-handback -O <op>`` cancels a still ``reserved`` row. Both
  verbs are bound to the staff relay record, the predecessor and the staff lease
  generation stored here at registration (a known source id alone admits nothing).

The state machine::

    reserved ──► accepted
        │
        └──────► cancelled

Every transition is a compare-and-set on ``state`` (``UPDATE … WHERE state =
'reserved'``), so two racing callers cannot both win and a settled row never moves again.
``joined_at`` stamps the SessionStart join (lineage written), separately from the state.

Additive only, like every ccc migration: ``CREATE … IF NOT EXISTS``, no CHECK
constraints, rows read BY NAME.
"""

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
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .await_store import immediate

if TYPE_CHECKING:
    from .store import Store

JOIN_START = "start"
JOIN_ACCEPT = "accept"
JOIN_MODES = (JOIN_START, JOIN_ACCEPT)

RESERVED = "reserved"
ACCEPTED = "accepted"
CANCELLED = "cancelled"

# A Claude session id: a canonical UUID. Anything else is a malformed --thread-from.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
# The staff relay record is an opaque caller label (ccc never reads the staff state).
RELAY_RECORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")

THREAD_JOIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_joins (
    op_id            TEXT    PRIMARY KEY,
    target_session   TEXT    NOT NULL,
    source_session   TEXT    NOT NULL,
    join_kind        TEXT    NOT NULL DEFAULT 'start',
    relay_record     TEXT    NOT NULL DEFAULT '',
    lease_generation INTEGER,
    state            TEXT    NOT NULL DEFAULT 'reserved',
    joined_at        INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL DEFAULT 0,
    settled_at       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_joins_target ON thread_joins(target_session);
CREATE INDEX IF NOT EXISTS idx_thread_joins_source ON thread_joins(source_session);
"""


@dataclass(frozen=True)
class ThreadJoin:  # pylint: disable=too-many-instance-attributes
    """One ``thread_joins`` row."""

    op_id: str
    target_session: str
    source_session: str
    join_kind: str
    relay_record: str
    lease_generation: int | None
    state: str
    joined_at: int
    created_at: int
    settled_at: int


def _row(row: sqlite3.Row | None) -> ThreadJoin | None:
    if row is None:
        return None
    gen = row["lease_generation"]
    return ThreadJoin(
        op_id=str(row["op_id"]),
        target_session=str(row["target_session"]),
        source_session=str(row["source_session"]),
        join_kind=str(row["join_kind"] or JOIN_START),
        relay_record=str(row["relay_record"] or ""),
        lease_generation=None if gen is None else int(gen),
        state=str(row["state"] or RESERVED),
        joined_at=int(row["joined_at"] or 0),
        created_at=int(row["created_at"] or 0),
        settled_at=int(row["settled_at"] or 0),
    )


class ThreadJoinStoreMixin:
    """The ``thread_joins`` reads and transitions; ``Store`` supplies ``conn``."""

    conn: sqlite3.Connection

    def set_thread_source(self, session_id: str, thread_from: str, thread_join: str) -> None:
        """Stamp the draft row's ``thread_from`` / ``thread_join`` (``new-job --thread-from``)."""
        self.conn.execute(
            "UPDATE sessions SET thread_from = ?, thread_join = ? WHERE session_id = ?",
            (thread_from or None, thread_join or None, session_id),
        )
        self.conn.commit()

    def thread_source(self, session_id: str) -> tuple[str, str]:
        """``(thread_from, thread_join)`` of *session_id*'s row (``("", "")`` = none)."""
        row = self.conn.execute(
            "SELECT thread_from, thread_join FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return "", ""
        return str(row["thread_from"] or ""), str(row["thread_join"] or "")

    def reserve_thread_join(  # pylint: disable=too-many-arguments
        self,
        target: str,
        source: str,
        join_kind: str,
        *,
        relay_record: str = "",
        lease_generation: int | None = None,
        now: int,
    ) -> ThreadJoin:
        """The target's reservation, inserted once (``INSERT OR IGNORE`` on the target).

        A re-run (``new-job -K``, or a crash between the draft and this row) returns the
        existing row unchanged: one target has exactly one op id.
        """
        with immediate(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO thread_joins (op_id, target_session, source_session, "
                "join_kind, relay_record, lease_generation, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    target,
                    source,
                    join_kind,
                    relay_record,
                    lease_generation,
                    RESERVED,
                    now,
                ),
            )
        got = self.thread_join_for(target)
        assert got is not None
        return got

    def thread_join_for(self, target: str) -> ThreadJoin | None:
        """*target*'s reservation row, or ``None``."""
        return _row(
            self.conn.execute(
                "SELECT * FROM thread_joins WHERE target_session = ?", (target,)
            ).fetchone()
        )

    def thread_join_op(self, op_id: str) -> ThreadJoin | None:
        """The reservation row of op id *op_id*, or ``None``."""
        return _row(
            self.conn.execute("SELECT * FROM thread_joins WHERE op_id = ?", (op_id,)).fetchone()
        )

    def mark_thread_joined(self, op_id: str, now: int, *, settle: bool) -> bool:
        """Stamp ``joined_at`` on a still reserved row; *settle* also accepts it (``start``).

        A compare-and-set on ``state = 'reserved' AND joined_at = 0``: True only for the
        one caller that joined (a cancelled row never joins late).
        """
        state_sql = ", state = 'accepted', settled_at = ?" if settle else ""
        params: tuple[object, ...] = (now, now, op_id) if settle else (now, op_id)
        cur = self.conn.execute(
            f"UPDATE thread_joins SET joined_at = ?{state_sql} "  # noqa: S608 - fixed text
            "WHERE op_id = ? AND state = 'reserved' AND joined_at = 0",
            params,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def settle_thread_join(self, op_id: str, state: str, now: int) -> bool:
        """``reserved`` → *state* (``accepted`` / ``cancelled``); True if this call moved it."""
        cur = self.conn.execute(
            "UPDATE thread_joins SET state = ?, settled_at = ? "
            "WHERE op_id = ? AND state = 'reserved'",
            (state, now, op_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_drain_relay_source(self, source: str, target: str) -> bool:
        """True when *source* → *target* is a thread relay whose source keeps prompting.

        An ``accept`` (staff) relay's source keeps draining and a cancelled relay's source
        stays the current one, so the handed-off prompt block does not apply to either.
        """
        row = self.conn.execute(
            "SELECT 1 FROM thread_joins WHERE source_session = ? AND target_session = ? "
            "AND (join_kind = 'accept' OR state = 'cancelled') LIMIT 1",
            (source, target),
        ).fetchone()
        return row is not None

    def cancel_thread_join(self, join: ThreadJoin, now: int) -> bool:
        """``reserved`` → ``cancelled`` for *join*; True if this call moved it.

        The same transaction drops the source side of the lineage when it still names
        this target (a successor that joined at SessionStart before the hand-back), so the
        source, which stays the current session, carries no stale ``⇢`` marker.
        """
        with immediate(self.conn):
            cur = self.conn.execute(
                "UPDATE thread_joins SET state = 'cancelled', settled_at = ? "
                "WHERE op_id = ? AND state = 'reserved'",
                (now, join.op_id),
            )
            if cur.rowcount > 0:
                self.conn.execute(
                    "UPDATE sessions SET handed_off_to = NULL WHERE session_id = ? "
                    "AND handed_off_to = ?",
                    (join.source_session, join.target_session),
                )
        return cur.rowcount > 0


def join_thread(store: Store, join: ThreadJoin, now: int) -> bool:
    """The thread relay's join: thread id, lineage, then the row's ``joined_at`` CAS.

    Called by the job's first SessionStart (and by ``thread-accept`` when that hook never
    ran): :meth:`Store.record_thread` copies the source's thread id onto the target (fill
    only), :meth:`Store.set_thread_lineage` writes the target side and, while empty, the
    source side without touching the source's status. A ``start`` row settles
    ``accepted`` here; an ``accept`` row waits for ``thread-accept``. Idempotent: a settled
    or already joined row writes nothing. A hand-back that won the race meanwhile gets its
    stale source side dropped again. True if this call joined.
    """
    if join.state != RESERVED or join.joined_at:
        return False
    store.record_thread(join.target_session, join.source_session)
    store.set_thread_lineage(join.source_session, join.target_session, now)
    joined = store.mark_thread_joined(join.op_id, now, settle=join.join_kind == JOIN_START)
    if not joined:
        current = store.thread_join_op(join.op_id)
        if current is not None and current.state == CANCELLED:
            store.conn.execute(
                "UPDATE sessions SET handed_off_to = NULL WHERE session_id = ? "
                "AND handed_off_to = ?",
                (join.source_session, join.target_session),
            )
            store.conn.commit()
    return joined
