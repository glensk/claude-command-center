#!/usr/bin/env python3
"""Store tables of the voice bridge: ``deliveries`` and ``events`` (a ``Store`` mixin).

Additive only, like every ccc migration: ``CREATE TABLE/INDEX IF NOT EXISTS`` (safe when
two fresh builds open the DB at once), no CHECK constraints (an older reader must be able
to read a state a newer build wrote), and every row is read BY NAME through
:func:`_pick`, so a column a newer ccc added is simply ignored by this build. A column
added to either table later goes into :data:`ADDED_DELIVERY_COLUMNS` /
:data:`ADDED_EVENT_COLUMNS` and is ALTERed in by ``Store._ensure_table_columns``, which
tolerates the duplicate-column race.

``deliveries`` — one row per ``ccc send`` / ``ccc answer``: the anchor (transcript path,
inode, byte size before the first byte was typed), the correlation key (normalised
content sha256), the send-time outcome and the delivery state machine
(:mod:`command_center.bridge_delivery`).

``events`` — the cursor-addressed problem stream ``ccc events --after CURSOR`` serves:
``stop_failure`` (every StopFailure hook), ``needs_input`` / ``delivery_failed`` (written
by the delivery scanner, each terminal state once via ``dedupe_key``). ``cursor`` is the
rowid (INTEGER PRIMARY KEY), so it is indexed by construction.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import json
import sqlite3
from dataclasses import dataclass, fields
from typing import Any

DELIVERY_STATES_OPEN = ("sending", "accepted", "unknown")
DELIVERY_STATES_TERMINAL = ("completed", "needs_input", "failed", "timed_out")
EVENT_KINDS = ("stop_failure", "needs_input", "delivery_failed")

BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id      TEXT    PRIMARY KEY,
    session_id       TEXT    NOT NULL,
    kind             TEXT    NOT NULL DEFAULT 'send',
    iterm_session_id TEXT    NOT NULL DEFAULT '',
    config_dir       TEXT    NOT NULL DEFAULT '',
    pid              INTEGER NOT NULL DEFAULT 0,
    channel          TEXT    NOT NULL DEFAULT '',
    transcript_path  TEXT    NOT NULL DEFAULT '',
    transcript_inode INTEGER NOT NULL DEFAULT 0,
    anchor_offset    INTEGER NOT NULL DEFAULT 0,
    content_sha      TEXT    NOT NULL DEFAULT '',
    alt_sha          TEXT    NOT NULL DEFAULT '',
    expected         TEXT    NOT NULL DEFAULT '',
    status_at_send   TEXT    NOT NULL DEFAULT '',
    state            TEXT    NOT NULL DEFAULT 'sending',
    outcome          TEXT    NOT NULL DEFAULT '',
    matched_kind     TEXT    NOT NULL DEFAULT '',
    matched_offset   INTEGER NOT NULL DEFAULT -1,
    consumed_offset  INTEGER NOT NULL DEFAULT -1,
    reason           TEXT    NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL DEFAULT 0,
    accepted_at      INTEGER NOT NULL DEFAULT 0,
    terminal_at      INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deliveries_session_state ON deliveries(session_id, state);
CREATE TABLE IF NOT EXISTS events (
    cursor     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    session_id TEXT    NOT NULL DEFAULT '',
    at         INTEGER NOT NULL DEFAULT 0,
    detail     TEXT    NOT NULL DEFAULT '{}',
    dedupe_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe
    ON events(dedupe_key) WHERE dedupe_key IS NOT NULL;
"""

#: Columns added to ``deliveries`` / ``events`` after this schema (none yet).
ADDED_DELIVERY_COLUMNS: dict[str, str] = {}
ADDED_EVENT_COLUMNS: dict[str, str] = {}


@dataclass
class Delivery:  # pylint: disable=too-many-instance-attributes  # one row, flat by design
    """One ``deliveries`` row."""

    delivery_id: str
    session_id: str
    kind: str = "send"
    iterm_session_id: str = ""
    config_dir: str = ""
    pid: int = 0
    channel: str = ""
    transcript_path: str = ""
    transcript_inode: int = 0
    anchor_offset: int = 0
    content_sha: str = ""
    alt_sha: str = ""
    expected: str = ""
    status_at_send: str = ""
    state: str = "sending"
    outcome: str = ""
    matched_kind: str = ""
    matched_offset: int = -1
    consumed_offset: int = -1
    reason: str = ""
    created_at: int = 0
    accepted_at: int = 0
    terminal_at: int = 0
    updated_at: int = 0


@dataclass
class Event:
    """One ``events`` row."""

    cursor: int
    kind: str
    session_id: str = ""
    at: int = 0
    detail: str = "{}"
    dedupe_key: str | None = None

    def detail_obj(self) -> dict[str, Any]:
        try:
            value = json.loads(self.detail or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


_DELIVERY_FIELDS = tuple(f.name for f in fields(Delivery))
_EVENT_FIELDS = tuple(f.name for f in fields(Event))


def _pick(row: sqlite3.Row, names: tuple[str, ...]) -> dict[str, Any]:
    """The columns of *row* this build knows (unknown columns are dropped)."""
    keys = set(row.keys())
    return {name: row[name] for name in names if name in keys}


class BridgeStoreMixin:
    """``deliveries`` / ``events`` access, mixed into :class:`command_center.store.Store`."""

    conn: sqlite3.Connection

    # ----------------------------------------------------------------- deliveries

    def insert_delivery(self, delivery: Delivery) -> None:
        values = {name: getattr(delivery, name) for name in _DELIVERY_FIELDS}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.conn.execute(
            f"INSERT INTO deliveries ({cols}) VALUES ({marks})", tuple(values.values())
        )
        self.conn.commit()

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        row = self.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        return Delivery(**_pick(row, _DELIVERY_FIELDS)) if row else None

    def deliveries_for(self, session_id: str) -> list[Delivery]:
        """Every delivery of *session_id*, FIFO (anchor offset, then creation)."""
        rows = self.conn.execute(
            "SELECT * FROM deliveries WHERE session_id = ? "
            "ORDER BY anchor_offset, created_at, rowid",
            (session_id,),
        ).fetchall()
        return [Delivery(**_pick(r, _DELIVERY_FIELDS)) for r in rows]

    def open_delivery_sessions(self) -> list[str]:
        """Session ids with at least one delivery not in a terminal state."""
        marks = ", ".join("?" for _ in DELIVERY_STATES_OPEN)
        rows = self.conn.execute(
            f"SELECT DISTINCT session_id FROM deliveries WHERE state IN ({marks})",
            DELIVERY_STATES_OPEN,
        ).fetchall()
        return [str(r[0]) for r in rows]

    def update_delivery(
        self,
        delivery_id: str,
        expect_state: str,
        event: tuple[str, str, int, dict[str, Any], str | None] | None = None,
        **changes: Any,
    ) -> bool:
        """CAS *changes* onto the row while it is still in *expect_state*.

        *event* (``kind, session_id, at_ms, detail, dedupe_key``) is inserted in the
        SAME transaction, so a terminal state and its one event land together or not at
        all. False when another process moved the row first.
        """
        unknown = set(changes) - set(_DELIVERY_FIELDS)
        if unknown:
            raise ValueError(f"unknown delivery fields: {sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE deliveries SET {sets} WHERE delivery_id = ? AND state = ?",
                (*changes.values(), delivery_id, expect_state),
            )
            if cur.rowcount != 1:
                return False
            if event is not None:
                self._insert_event(*event)
        return True

    # ----------------------------------------------------------------- events

    def _insert_event(
        self,
        kind: str,
        session_id: str,
        at_ms: int,
        detail: dict[str, Any],
        dedupe_key: str | None,
    ) -> int | None:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events (kind, session_id, at, detail, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, session_id, int(at_ms), json.dumps(detail, ensure_ascii=False), dedupe_key),
        )
        return int(cur.lastrowid) if cur.rowcount == 1 and cur.lastrowid else None

    def add_event(
        self,
        kind: str,
        session_id: str,
        at_ms: int,
        detail: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> int | None:
        """Append one event; ``None`` when *dedupe_key* was already recorded."""
        with self.conn:
            return self._insert_event(kind, session_id, at_ms, detail or {}, dedupe_key)

    def events_after(self, cursor: int, limit: int = 200) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?",
            (int(cursor), int(limit)),
        ).fetchall()
        return [Event(**_pick(r, _EVENT_FIELDS)) for r in rows]

    def stop_failures_since(self, session_id: str, since_ms: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE kind = 'stop_failure' AND session_id = ? AND at >= ? "
            "ORDER BY cursor",
            (session_id, int(since_ms)),
        ).fetchall()
        return [Event(**_pick(r, _EVENT_FIELDS)) for r in rows]
