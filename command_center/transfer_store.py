#!/usr/bin/env python3
"""Store table of the session handoff: ``transfers`` (a ``Store`` mixin, tp#1300).

One row per ``ccc continue-session start``: an IMMUTABLE snapshot of what is being handed
over (source / target session and account, cwd, the source AIM and its revision, models,
``no_codex``, the handoff's digest and stored path, and the id of the facts snapshot it was composed
from) plus the state machine::

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

The source's active ``ccc await`` group moves WITH the transfer (``await_store``): it is
reserved in :meth:`TransferStoreMixin.create_transfer`'s transaction, released in the
``failed`` transition's, and rebound to the successor in :meth:`accept_transfer`'s — so a
wake is never lost and never delivered to both sessions. A group the source arms later
joins the reservation at arm time; :meth:`prepare_accept` (before any hook) sweeps any
straggler in and refuses an await collision while nothing has moved yet.

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

from .await_store import (
    AwaitBusy,
    AwaitConflict,
    AwaitGroup,
    Rebind,
    fresh_deliveries,
    immediate,
    rebind_groups,
    release_groups,
    reserve_groups,
    reserved_ids,
    target_conflict,
)

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
    "launch_profile",
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
    launch_profile    TEXT,
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
    updated_at        INTEGER NOT NULL DEFAULT 0,
    facts_snapshot    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_active_source ON transfers(source_session)
    WHERE state IN ('prepared', 'opened', 'accepted');
CREATE INDEX IF NOT EXISTS idx_transfers_target ON transfers(target_session);
"""


class TransferConflict(Exception):
    """Another transfer of the same source session is still active."""


class _CasLost(Exception):
    """Internal: the transfer was not acceptable any more; roll the transaction back."""


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
    launch_profile: str = ""  # config label of the source's launch profile; "" = bare claude
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
    facts_snapshot: str = ""  # id of the frozen facts snapshot the handoff was composed from

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

#: ``close_bound`` prefix of a kept handoff close debt (signal sent, pane still open) — the
#: same value as ``handoff_close.DEBT_PREFIX``, repeated here to keep this module leaf-level.
HANDOFF_DEBT_PREFIX = "handoff:"


@dataclass(frozen=True)
class HandoffCandidate:  # pylint: disable=too-many-instance-attributes  # one query row
    """One row of :meth:`TransferStoreMixin.handoff_finalize_candidates` (tp#1582)."""

    transfer_id: str
    source_session: str
    target_session: str
    state: str  # accepted | finalized
    capped: bool
    close_token: str  # "" = no close armed on the source row
    close_requested_at: int  # > 0 = the token is PENDING, 0 with a token = in flight
    close_bound: str  # "handoff:{…}" = a kept post-SIGTERM close debt
    close_claimed_at: int = 0  # epoch-ms of the in-flight claim (0 = pending/none/legacy)
    close_holder: str = ""  # the in-flight holder's "pid:start" ("" = none registered)

    @property
    def token_state(self) -> str:
        """``none`` | ``pending`` | ``in_flight`` — the source row's close token."""
        if not self.close_token:
            return "none"
        return "pending" if self.close_requested_at > 0 else "in_flight"

    @property
    def debt(self) -> bool:
        """A kept post-SIGTERM debt: the pane/tab still to close, never re-signalled."""
        return self.close_bound.startswith(HANDOFF_DEBT_PREFIX)


def new_transfer_id() -> str:
    """A fresh transfer id (``t`` + 12 hex: short enough to type, unique enough)."""
    return "t" + uuid.uuid4().hex[:12]


def _row_to_transfer(row: sqlite3.Row) -> Transfer:
    data = {k: row[k] for k in row.keys() if k in _FIELDS}
    data["no_codex"] = bool(data.get("no_codex"))
    data["capped"] = bool(data.get("capped"))
    data["launch_profile"] = str(data.get("launch_profile") or "")  # NULL (old row) = none
    data["facts_snapshot"] = str(data.get("facts_snapshot") or "")
    return Transfer(**data)


class TransferStoreMixin:
    """The transfer transitions; each one is a single compare-and-set statement."""

    conn: sqlite3.Connection

    def create_transfer(self, transfer: Transfer, now: int) -> Transfer:
        """Insert *transfer* as ``prepared``; :class:`TransferConflict` if one is active.

        The source's active ``ccc await`` group is reserved under the new transfer in the
        SAME transaction: either both land or neither does.
        """
        data = dataclasses.asdict(transfer)
        data.update(state=PREPARED, created_at=now, updated_at=now)
        data["no_codex"] = int(bool(data["no_codex"]))
        data["capped"] = int(bool(data["capped"]))
        data["launch_profile"] = data["launch_profile"] or None
        data["facts_snapshot"] = data["facts_snapshot"] or None
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        try:
            with immediate(self.conn):
                self.conn.execute(
                    f"INSERT INTO transfers ({cols}) VALUES ({marks})",  # noqa: S608 - fixed names
                    tuple(data.values()),
                )
                reserve_groups(self.conn, transfer.source_session, transfer.transfer_id)
        except sqlite3.IntegrityError as exc:
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

    def inbound_transfers(self, target_session: str) -> tuple[list[Transfer], int]:
        """The ``prepared`` / ``opened`` transfers whose successor is *target_session*
        (oldest first), and how many such rows are unreadable (tp#1681 L18).

        A row is unreadable when it does not build a :class:`Transfer` or lacks its id, its
        source or its target account — the SessionStart auto-accept never acts on it.
        """
        rows = self.conn.execute(
            "SELECT * FROM transfers WHERE target_session = ? "
            "AND state IN ('prepared', 'opened') ORDER BY created_at, transfer_id",
            (target_session,),
        ).fetchall()
        found: list[Transfer] = []
        corrupt = 0
        for row in rows:
            try:
                transfer = _row_to_transfer(row)
            except (TypeError, ValueError):
                corrupt += 1
                continue
            if transfer.transfer_id and transfer.source_session and transfer.target_config_dir:
                found.append(transfer)
            else:
                corrupt += 1
        return found, corrupt

    def transition_transfer(
        self, transfer_id: str, to_state: str, now: int, *, failure: str = ""
    ) -> bool:
        """CAS *transfer_id* into *to_state* from any state with that legal edge.

        ``False`` when the row is missing or not in a state that may move to *to_state*
        (another caller won, or the edge is illegal). ``failure`` is recorded on ``failed``,
        and a ``failed`` transition releases the transfer's await reservations in the same
        transaction (a fired group is then delivered to the source as usual).
        """
        if to_state == FAILED:
            with immediate(self.conn):
                won = self._cas_transfer(transfer_id, to_state, now, failure=failure)
                if won:
                    release_groups(self.conn, transfer_id, now // 1000)
            return won
        won = self._cas_transfer(transfer_id, to_state, now, failure=failure)
        self.conn.commit()
        return won

    def prepare_accept(self, transfer_id: str, now: int, *, stale_sec: int) -> list[int] | None:
        """Accept-prepare: ONE transaction that runs BEFORE any ``handoff_hooks`` entry.

        Sweeps the source's still-unreserved active await groups into *transfer_id*'s
        reservation (a group armed before the arm-time join existed), then refuses — the
        whole transaction rolled back, so nothing changed — with ``AwaitConflict`` when
        the target already holds an active group while a reserved one would move there,
        and with ``AwaitBusy`` (retryable) while a reserved group is ``delivering`` for
        less than *stale_sec*. Returns the swept group ids, or ``None`` when the transfer
        is no longer ``prepared``/``opened``.
        """
        try:
            with immediate(self.conn):
                row = self.conn.execute(
                    "SELECT * FROM transfers WHERE transfer_id = ? "
                    "AND state IN ('prepared', 'opened')",
                    (transfer_id,),
                ).fetchone()
                if row is None:
                    raise _CasLost
                transfer = _row_to_transfer(row)
                swept = reserve_groups(self.conn, transfer.source_session, transfer_id)
                reserved = reserved_ids(self.conn, transfer_id, transfer.source_session)
                if reserved:
                    held = target_conflict(self.conn, transfer.target_session)
                    if held is not None:
                        raise AwaitConflict(
                            f"this session already holds await group {held.id} ({held.state}) "
                            f"and the source's await group {reserved[0]} moves here on "
                            "accept — two waits are never merged"
                        )
                busy = fresh_deliveries(self.conn, transfer_id, now // 1000, stale_sec)
                if busy:
                    raise AwaitBusy(
                        f"await group {busy[0].id} is being delivered to the source right now"
                    )
        except _CasLost:
            return None
        return swept

    def accept_transfer(self, transfer_id: str, now: int) -> list[AwaitGroup] | None:
        """``prepared|opened → accepted`` WITH the await rebind, in ONE transaction.

        Every active group reserved under *transfer_id* moves to the transfer's target
        (session, account dir, cwd, ``no_codex``, launch profile; a stale ``delivering``
        row back to ``fired`` first), the reservation is cleared, then the CAS. Returns the
        moved groups, or ``None`` when the CAS lost (nothing changed). Raises
        ``AwaitConflict`` when the target already holds an active group, or any
        ``sqlite3.Error`` — every failure rolls the whole transaction back, so accept stays
        retryable.
        """
        moved: list[AwaitGroup] = []
        try:
            with immediate(self.conn):
                row = self.conn.execute(
                    "SELECT * FROM transfers WHERE transfer_id = ? "
                    "AND state IN ('prepared', 'opened')",
                    (transfer_id,),
                ).fetchone()
                if row is None:
                    raise _CasLost
                transfer = _row_to_transfer(row)
                rebind = Rebind(
                    source_session=transfer.source_session,
                    target_session=transfer.target_session,
                    config_dir=transfer.target_config_dir,
                    cwd=transfer.cwd,
                    no_codex=transfer.no_codex,
                    launch_profile=transfer.launch_profile,
                )
                moved = rebind_groups(self.conn, transfer_id, rebind, now // 1000)
                self._cas_transfer(transfer_id, OPENED, now)  # a launcher race
                if not self._cas_transfer(transfer_id, ACCEPTED, now):
                    raise _CasLost
        except _CasLost:
            return None
        return moved

    def _cas_transfer(
        self, transfer_id: str, to_state: str, now: int, *, failure: str = ""
    ) -> bool:
        """The CAS of :meth:`transition_transfer`, without a commit (the caller owns it)."""
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

    def handoff_done_sources(self) -> set[str]:
        """Not-done sessions whose handoff is done: the successor took over.

        A source qualifies when its lineage names a successor (``handed_off_to``) and the
        transfer to THAT successor is ``accepted`` or ``finalized``. Process liveness is the
        caller's half of the condition (``core.reconcile_pass`` knows the registry). One
        query, so a reconcile pass pays for it once, never per row.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT s.session_id FROM sessions s JOIN transfers t "
            "ON t.source_session = s.session_id AND t.target_session = s.handed_off_to "
            "WHERE s.done = 0 AND COALESCE(s.handed_off_to, '') != '' "
            "AND t.state IN ('accepted', 'finalized')"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def handoff_finalize_candidates(self) -> list[HandoffCandidate]:
        """What the daemon's handoff sweep may act on — ONE indexed query (tp#1582).

        * every ``accepted`` transfer (finalized by nobody yet: a crash after the accepted
          CAS) with its source row's close token, through the partial active-source index;
        * every NON-capped ``finalized`` transfer whose source row still carries a close
          token (pending, in flight, or a ``handoff:`` debt) and still names THAT transfer's
          target as its successor, through the partial close-token index and the target
          index. A retired token (a completed close) never comes back, so a tab reopened
          after its close is never closed again — there is no age-based selection.

        Process liveness is the caller's half (the daemon's registry snapshot), like
        :meth:`handoff_done_sources`.
        """
        cols = (
            "t.transfer_id, t.source_session, t.target_session, t.state, t.capped, "
            "COALESCE(s.close_token, ''), COALESCE(s.close_requested_at, 0), "
            "COALESCE(s.close_bound, ''), COALESCE(s.close_claimed_at, 0), "
            "COALESCE(s.close_holder, '')"
        )
        rows = self.conn.execute(
            f"SELECT {cols} FROM transfers t "  # noqa: S608 - fixed column names
            "LEFT JOIN sessions s ON s.session_id = t.source_session "
            "WHERE t.state IN ('prepared', 'opened', 'accepted') AND t.state = 'accepted' "
            "UNION ALL "
            # CROSS JOIN pins the loop order: the few token rows drive, not a transfers scan.
            f"SELECT {cols} FROM sessions s "
            "CROSS JOIN transfers t ON t.target_session = s.handed_off_to "
            "AND t.source_session = s.session_id "
            "WHERE s.close_token != '' AND t.state = 'finalized' AND t.capped = 0"
        ).fetchall()
        found = [
            HandoffCandidate(
                transfer_id=str(row[0]),
                source_session=str(row[1]),
                target_session=str(row[2] or ""),
                state=str(row[3]),
                capped=bool(row[4]),
                close_token=str(row[5] or ""),
                close_requested_at=int(row[6] or 0),
                close_bound=str(row[7] or ""),
                close_claimed_at=int(row[8] or 0),
                close_holder=str(row[9] or ""),
            )
            for row in rows
        ]
        return sorted(found, key=lambda c: c.transfer_id)  # stable order, no ORDER BY scan

    def lineage_transfer_id(self, source_session: str) -> str:
        """Id of the transfer to *source_session*'s recorded successor (``""`` = none).

        The transfer whose source is *source_session* and whose target is the row's
        ``handed_off_to`` — a finalized one first, then the newest. Only a lookup: the
        caller still judges it (``handoff_close.lineage_refusal``). Indexed: the session
        primary key, then the target index.
        """
        row = self.conn.execute(
            "SELECT t.transfer_id FROM sessions s "
            "CROSS JOIN transfers t ON t.target_session = s.handed_off_to "
            "AND t.source_session = s.session_id "
            "WHERE s.session_id = ? AND COALESCE(s.handed_off_to, '') != '' "
            "ORDER BY (t.state = 'finalized') DESC, t.created_at DESC, t.transfer_id LIMIT 1",
            (source_session,),
        ).fetchone()
        return str(row[0]) if row is not None else ""

    def set_handoff_lineage(self, source: str, target: str, now: int) -> None:
        """Mark *source* handed off to *target* (status ``handed_off``) and *target*'s origin.

        A source the reconcile pass already closed as done keeps its ``done`` status, so an
        idempotent ``finalize`` re-run never lifts it back out of FINISHED.
        """
        self.conn.execute(
            "UPDATE sessions SET handed_off_to = ?, handed_off_at = ?, "
            "status = CASE WHEN done = 1 THEN status ELSE 'handed_off' END, "
            "updated_at = ? WHERE session_id = ?",
            (target, now, now, source),
        )
        self.conn.execute(
            "UPDATE sessions SET handed_off_from = ?, handed_off_at = ?, updated_at = ? "
            "WHERE session_id = ?",
            (source, now, now, target),
        )
        self.conn.commit()
