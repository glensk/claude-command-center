#!/usr/bin/env python3
"""Store table of the session handoff: ``transfers`` (a ``Store`` mixin, tp#1300).

One row per ``ccc continue-session start``: an IMMUTABLE snapshot of what is being handed
over (source / target session and account, cwd, the source AIM and its revision, models,
``no_codex``, the handoff text's sha256 and stored path) plus the state machine::

    prepared ──► opened ──► accepted ──► finalized
        │           │
        └──► failed ◄┘

``prepared`` = validated and snapshot written; ``opened`` = a launcher accepted the
successor's ``start-job``; ``accepted`` = the successor ran ``continue-session accept`` and
every ``handoff_hooks`` entry succeeded; ``finalized`` = the source side observed the
acceptance and retired the source session; ``failed`` = a typed refusal before acceptance.
Every transition is a compare-and-set (``UPDATE … WHERE state IN (…)``), so two racing
callers cannot both win. At most ONE active (prepared / opened / accepted) transfer per
source session — a partial UNIQUE index enforces it.

Additive only, like every ccc migration: ``CREATE … IF NOT EXISTS``, no CHECK constraints,
and rows are read BY NAME (a column a newer build added is ignored here).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import dataclasses
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

PREPARED = "prepared"
OPENED = "opened"
ACCEPTED = "accepted"
FINALIZED = "finalized"
FAILED = "failed"
TRANSFER_STATES = (PREPARED, OPENED, ACCEPTED, FINALIZED, FAILED)
ACTIVE_STATES = (PREPARED, OPENED, ACCEPTED)

# The legal edges of the state machine (from → allowed targets).
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PREPARED: (OPENED, FAILED),
    OPENED: (ACCEPTED, FAILED),
    ACCEPTED: (FINALIZED,),
    FINALIZED: (),
    FAILED: (),
}

# The timestamp column each target state stamps.
_STAMP = {OPENED: "opened_at", ACCEPTED: "accepted_at", FINALIZED: "finalized_at"}

# Snapshot fields a retry must match exactly to reuse the transfer (the rest are generated
# per transfer or are state).
SNAPSHOT_FIELDS = (
    "source_session",
    "source_config_dir",
    "target_config_dir",
    "target_label",
    "cwd",
    "source_aim",
    "source_aim_rev",
    "llm_overseer",
    "llm_exec",
    "no_codex",
    "prompt_sha256",
)

TRANSFER_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id       TEXT    PRIMARY KEY,
    source_session    TEXT    NOT NULL,
    target_session    TEXT    NOT NULL DEFAULT '',
    source_config_dir TEXT    NOT NULL DEFAULT '',
    target_config_dir TEXT    NOT NULL DEFAULT '',
    target_label      TEXT    NOT NULL DEFAULT '',
    cwd               TEXT    NOT NULL DEFAULT '',
    source_aim        TEXT    NOT NULL DEFAULT '',
    source_aim_rev    INTEGER NOT NULL DEFAULT 0,
    llm_overseer      TEXT    NOT NULL DEFAULT '',
    llm_exec          TEXT    NOT NULL DEFAULT '',
    no_codex          INTEGER NOT NULL DEFAULT 0,
    prompt_sha256     TEXT    NOT NULL DEFAULT '',
    handoff_path      TEXT    NOT NULL DEFAULT '',
    launcher          TEXT    NOT NULL DEFAULT '',
    capped            INTEGER NOT NULL DEFAULT 0,
    state             TEXT    NOT NULL DEFAULT 'prepared',
    failure           TEXT    NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL DEFAULT 0,
    opened_at         INTEGER NOT NULL DEFAULT 0,
    accepted_at       INTEGER NOT NULL DEFAULT 0,
    finalized_at      INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_active_source ON transfers(source_session)
    WHERE state IN ('prepared', 'opened', 'accepted');
CREATE INDEX IF NOT EXISTS idx_transfers_target ON transfers(target_session);
"""


class TransferConflict(Exception):
    """Another transfer of the same source session is still active."""


@dataclass
class Transfer:  # pylint: disable=too-many-instance-attributes
    """One ``transfers`` row."""

    transfer_id: str
    source_session: str
    target_session: str = ""
    source_config_dir: str = ""
    target_config_dir: str = ""
    target_label: str = ""
    cwd: str = ""
    source_aim: str = ""
    source_aim_rev: int = 0
    llm_overseer: str = ""
    llm_exec: str = ""
    no_codex: bool = False
    prompt_sha256: str = ""
    handoff_path: str = ""
    launcher: str = ""
    capped: bool = False
    state: str = PREPARED
    failure: str = ""
    created_at: int = 0
    opened_at: int = 0
    accepted_at: int = 0
    finalized_at: int = 0
    updated_at: int = 0

    def snapshot(self) -> dict[str, Any]:
        """The fields a retry must match (:data:`SNAPSHOT_FIELDS`)."""
        return {name: getattr(self, name) for name in SNAPSHOT_FIELDS}

    def differs(self, other: Transfer) -> list[str]:
        """Names of the snapshot fields where *other* differs from this transfer."""
        mine, theirs = self.snapshot(), other.snapshot()
        return [name for name in SNAPSHOT_FIELDS if mine[name] != theirs[name]]

    def to_dict(self) -> dict[str, Any]:
        """JSON shape (``ccc continue-session status -j``)."""
        return dataclasses.asdict(self)


_FIELDS = frozenset(f.name for f in dataclasses.fields(Transfer))


def new_transfer_id() -> str:
    """A fresh transfer id (``t`` + 12 hex: short enough to type, unique enough)."""
    return "t" + uuid.uuid4().hex[:12]


def _row_to_transfer(row: sqlite3.Row) -> Transfer:
    data = {k: row[k] for k in row.keys() if k in _FIELDS}
    data["no_codex"] = bool(data.get("no_codex"))
    data["capped"] = bool(data.get("capped"))
    return Transfer(**data)


class TransferStoreMixin:
    """The transfer transitions; each one is a single compare-and-set statement."""

    conn: sqlite3.Connection

    def create_transfer(self, transfer: Transfer, now: int) -> Transfer:
        """Insert *transfer* as ``prepared``; :class:`TransferConflict` if one is active."""
        data = dataclasses.asdict(transfer)
        data.update(state=PREPARED, created_at=now, updated_at=now)
        data["no_codex"] = int(bool(data["no_codex"]))
        data["capped"] = int(bool(data["capped"]))
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        try:
            self.conn.execute(
                f"INSERT INTO transfers ({cols}) VALUES ({marks})",  # noqa: S608 - fixed names
                tuple(data.values()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise TransferConflict(str(exc)) from exc
        got = self.get_transfer(transfer.transfer_id)
        assert got is not None
        return got

    def get_transfer(self, transfer_id: str) -> Transfer | None:
        """The transfer *transfer_id*, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,)
        ).fetchone()
        return _row_to_transfer(row) if row is not None else None

    def active_transfer(self, source_session: str) -> Transfer | None:
        """The one prepared / opened / accepted transfer of *source_session*, if any."""
        row = self.conn.execute(
            "SELECT * FROM transfers WHERE source_session = ? "
            "AND state IN ('prepared', 'opened', 'accepted')",
            (source_session,),
        ).fetchone()
        return _row_to_transfer(row) if row is not None else None

    def transfers_of(self, session_id: str) -> list[Transfer]:
        """Every transfer *session_id* is the source or the target of, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM transfers WHERE source_session = ? OR target_session = ? "
            "ORDER BY created_at DESC, transfer_id",
            (session_id, session_id),
        ).fetchall()
        return [_row_to_transfer(row) for row in rows]

    def transition_transfer(
        self, transfer_id: str, to_state: str, now: int, *, failure: str = ""
    ) -> bool:
        """CAS *transfer_id* into *to_state* from any state with that legal edge.

        ``False`` when the row is missing or not in a state that may move to *to_state*
        (another caller won, or the edge is illegal). ``failure`` is recorded on ``failed``.
        """
        sources = tuple(s for s, targets in TRANSITIONS.items() if to_state in targets)
        if not sources:
            return False
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [to_state, now]
        if to_state in _STAMP:
            sets.append(f"{_STAMP[to_state]} = ?")
            params.append(now)
        if to_state == FAILED:
            sets.append("failure = ?")
            params.append(failure)
        marks = ", ".join("?" for _ in sources)
        cur = self.conn.execute(
            f"UPDATE transfers SET {', '.join(sets)} "  # noqa: S608 - fixed column names
            f"WHERE transfer_id = ? AND state IN ({marks})",
            (*params, transfer_id, *sources),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def note_transfer(
        self, transfer_id: str, now: int, *, failure: str | None = None, launcher: str | None = None
    ) -> bool:
        """Record a non-terminal *failure* note or the *launcher* on an ACTIVE transfer."""
        sets = ["updated_at = ?"]
        params: list[Any] = [now]
        if failure is not None:
            sets.append("failure = ?")
            params.append(failure)
        if launcher is not None:
            sets.append("launcher = ?")
            params.append(launcher)
        cur = self.conn.execute(
            f"UPDATE transfers SET {', '.join(sets)} "  # noqa: S608 - fixed column names
            "WHERE transfer_id = ? AND state IN ('prepared', 'opened', 'accepted')",
            (*params, transfer_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def set_handoff_lineage(self, source: str, target: str, now: int) -> None:
        """Mark *source* handed off to *target* (status ``handed_off``) and *target*'s origin."""
        self.conn.execute(
            "UPDATE sessions SET handed_off_to = ?, handed_off_at = ?, status = 'handed_off', "
            "updated_at = ? WHERE session_id = ?",
            (target, now, now, source),
        )
        self.conn.execute(
            "UPDATE sessions SET handed_off_from = ?, handed_off_at = ?, updated_at = ? "
            "WHERE session_id = ?",
            (source, now, now, target),
        )
        self.conn.commit()
