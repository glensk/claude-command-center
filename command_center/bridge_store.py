#!/usr/bin/env python3
"""Store tables of the voice bridge: ``deliveries``, ``events``, ``inputs`` and
``bridge_meta`` (a ``Store`` mixin).

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

``inputs`` — the input ledger (:mod:`command_center.input_ledger`): one row per text ccc
types or launches into a Claude session, written BEFORE the text goes out. It holds the
sha256 of the normalised text (never the text), the transcript anchor and, once matched,
the transcript line's byte offset and ``uuid``. ``bridge_meta`` — small key/value facts
of the bridge (the ledger's coverage watermark).
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
INPUT_STATES = ("pending", "matched", "unmatched", "failed")

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
CREATE TABLE IF NOT EXISTS inputs (
    input_id         TEXT    PRIMARY KEY,
    session_id       TEXT    NOT NULL DEFAULT '',
    kind             TEXT    NOT NULL DEFAULT '',
    origin           TEXT    NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL DEFAULT 0,
    norm             TEXT    NOT NULL DEFAULT 'ccc-text-sha-v1',
    content_sha      TEXT    NOT NULL DEFAULT '',
    alt_sha          TEXT    NOT NULL DEFAULT '',
    config_dir       TEXT    NOT NULL DEFAULT '',
    cwd              TEXT    NOT NULL DEFAULT '',
    transcript_path  TEXT    NOT NULL DEFAULT '',
    transcript_inode INTEGER NOT NULL DEFAULT 0,
    anchor_offset    INTEGER NOT NULL DEFAULT 0,
    state            TEXT    NOT NULL DEFAULT 'pending',
    line_offset      INTEGER NOT NULL DEFAULT -1,
    line_uuid        TEXT    NOT NULL DEFAULT '',
    line_kind        TEXT    NOT NULL DEFAULT '',
    stray_len        INTEGER NOT NULL DEFAULT 0,
    delivery_id      TEXT    NOT NULL DEFAULT '',
    reason           TEXT    NOT NULL DEFAULT '',
    matched_at       INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inputs_session_created ON inputs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_inputs_state ON inputs(state);
CREATE TABLE IF NOT EXISTS bridge_meta (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""

#: Columns added to ``deliveries`` / ``events`` / ``inputs`` after this schema (none yet).
ADDED_DELIVERY_COLUMNS: dict[str, str] = {}
ADDED_EVENT_COLUMNS: dict[str, str] = {}
ADDED_INPUT_COLUMNS: dict[str, str] = {}


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


@dataclass
class Input:  # pylint: disable=too-many-instance-attributes  # one row, flat by design
    """One ``inputs`` row (the input ledger): hashes and transcript facts, never the text.

    ``session_id == ""`` is an unbound row (ccc could not name the session);
    ``transcript_inode == 0`` a fresh anchor (the transcript did not exist yet);
    ``line_offset == -1`` not matched yet.
    """

    input_id: str
    session_id: str = ""
    kind: str = ""
    origin: str = ""
    created_at: int = 0
    norm: str = "ccc-text-sha-v1"
    content_sha: str = ""
    alt_sha: str = ""
    config_dir: str = ""
    cwd: str = ""
    transcript_path: str = ""
    transcript_inode: int = 0
    anchor_offset: int = 0
    state: str = "pending"
    line_offset: int = -1
    line_uuid: str = ""
    line_kind: str = ""
    stray_len: int = 0
    delivery_id: str = ""
    reason: str = ""
    matched_at: int = 0
    updated_at: int = 0


_DELIVERY_FIELDS = tuple(f.name for f in fields(Delivery))
_EVENT_FIELDS = tuple(f.name for f in fields(Event))
_INPUT_FIELDS = tuple(f.name for f in fields(Input))


def _pick(row: sqlite3.Row, names: tuple[str, ...]) -> dict[str, Any]:
    """The columns of *row* this build knows (unknown columns are dropped)."""
    keys = set(row.keys())
    return {name: row[name] for name in names if name in keys}


def input_from_row(row: sqlite3.Row) -> Input:
    """An :class:`Input` from an ``inputs`` row read by any connection (``sqlite3.Row``
    rows; unknown columns dropped) — the read-only ``ccc sent`` listing's reader."""
    return Input(**_pick(row, _INPUT_FIELDS))


class BridgeStoreMixin:
    """``deliveries`` / ``events`` / ``inputs`` / ``bridge_meta`` access, mixed into
    :class:`command_center.store.Store`."""

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

    # ----------------------------------------------------------------- inputs (ledger)

    def insert_input(self, row: Input) -> None:
        values = {name: getattr(row, name) for name in _INPUT_FIELDS}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO inputs ({cols}) VALUES ({marks})", tuple(values.values())
            )

    def get_input(self, input_id: str) -> Input | None:
        row = self.conn.execute("SELECT * FROM inputs WHERE input_id = ?", (input_id,)).fetchone()
        return Input(**_pick(row, _INPUT_FIELDS)) if row else None

    def inputs_for(self, session_id: str, since_ms: int, until_ms: int) -> list[Input]:
        """Rows of *session_id* created in ``[since_ms, until_ms)``, FIFO (time, then rowid)."""
        rows = self.conn.execute(
            "SELECT * FROM inputs WHERE session_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at, rowid",
            (session_id, int(since_ms), int(until_ms)),
        ).fetchall()
        return [Input(**_pick(r, _INPUT_FIELDS)) for r in rows]

    def unbound_inputs(self, since_ms: int, until_ms: int) -> list[Input]:
        """Unbound (``session_id == ''``) ``pending`` / ``unmatched`` rows created in
        ``[since_ms, until_ms)``, whatever their config dir: one transcript can be shared
        across accounts, so any such row leaves every window of that time unproven."""
        rows = self.conn.execute(
            "SELECT * FROM inputs WHERE session_id = '' AND state IN ('pending', 'unmatched') "
            "AND created_at >= ? AND created_at < ? ORDER BY created_at, rowid",
            (int(since_ms), int(until_ms)),
        ).fetchall()
        return [Input(**_pick(r, _INPUT_FIELDS)) for r in rows]

    def pending_input_sessions(self) -> list[str]:
        """Session ids with at least one ``pending`` row; ``''`` is included while an
        unbound row is pending (:func:`command_center.input_ledger.advance_session`
        expires it)."""
        rows = self.conn.execute(
            "SELECT DISTINCT session_id FROM inputs WHERE state = 'pending'"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def update_input(self, input_id: str, expect_state: str, **changes: Any) -> bool:
        """CAS *changes* onto the row while it is still in *expect_state*.

        False when another process moved the row first (or it does not exist).
        """
        unknown = set(changes) - set(_INPUT_FIELDS)
        if unknown:
            raise ValueError(f"unknown input fields: {sorted(unknown)}")
        if not changes:
            raise ValueError("update_input needs at least one change")
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE inputs SET {sets} WHERE input_id = ? AND state = ?",
                (*changes.values(), input_id, expect_state),
            )
        return cur.rowcount == 1

    # ----------------------------------------------------------------- bridge_meta

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM bridge_meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def meta_set(self, key: str, value: str, now_ms: int = 0) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO bridge_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, str(value), int(now_ms)),
            )

    def meta_delete(self, key: str) -> bool:
        """Delete *key*; False when it was not set."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM bridge_meta WHERE key = ?", (key,))
        return cur.rowcount == 1
