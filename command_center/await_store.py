#!/usr/bin/env python3
"""Storage for ``ccc await``: await GROUPS (one per waiting session) and their SOURCES.

A session that waits on a person arms ONE group holding one or more sources (a Zoho
Desk reply, a Slack DM, a shell predicate). The poller probes due sources; the first
that fires wins the group in ONE ``BEGIN IMMEDIATE`` transaction, which also disarms
every sibling. Delivery then moves the group through an outbox keyed by a fresh
``delivery_token``, so two deliverers can never both resume the session.

Group states (``await_groups.state``)::

    armed ──until──▶ grace ──grace_until──▶ expired
      │                │
      └──first fire────┴──▶ fired ──CAS──▶ delivering ──claim──▶ delivered
                              ▲                 │
                              └──retry (≤ N)────┘──▶ blocked ──-R──▶ fired | armed
    any active state ──-d / session done──▶ disarmed

Source states: ``armed`` (probed when due), ``blocked`` (a permanent probe failure; the
group only blocks when no viable source is left), ``done`` (the winner), ``disarmed``.

Every timestamp in these two tables is epoch SECONDS (the sessions table uses ms).
Rows are read by column NAME and unknown columns are ignored, so a newer ccc that adds
a column never breaks an older long-lived reader (the TUI) — the same rule as
``store._row_to_session``. The tables are created with ``IF NOT EXISTS`` inside the
store's schema script, which is safe under concurrent opens.

This module is a mixin: :class:`AwaitStoreMixin` needs only ``self.conn``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import dataclasses
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

GROUP_STATES = (
    "armed",
    "grace",
    "fired",
    "delivering",
    "delivered",
    "blocked",
    "expired",
    "disarmed",
)
#: States that hold the session's single active slot (the partial UNIQUE index).
ACTIVE_GROUP_STATES = ("armed", "grace", "fired", "delivering", "blocked")
#: States in which sources are still probed.
POLLING_GROUP_STATES = ("armed", "grace")
SOURCE_KINDS = ("zoho-reply", "slack-dm", "cmd")
SOURCE_STATES = ("armed", "blocked", "done", "disarmed")

MIN_INTERVAL_SEC = 60
DEFAULT_INTERVAL_SEC = 120
GRACE_SEC = 24 * 3600
LEASE_SEC = 45

# Quoted lists for the DDL / queries (constants, never user input).
_Q = "', '"
_ACTIVE_SQL = "('" + _Q.join(ACTIVE_GROUP_STATES) + "')"
_POLLING_SQL = "('" + _Q.join(POLLING_GROUP_STATES) + "')"

AWAIT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS await_groups (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    config_dir         TEXT    NOT NULL DEFAULT '',
    cwd                TEXT    NOT NULL DEFAULT '',
    no_codex           INTEGER NOT NULL DEFAULT 0,
    prompt_template    TEXT    NOT NULL,
    until_epoch        INTEGER NOT NULL,
    grace_until_epoch  INTEGER NOT NULL,
    state              TEXT    NOT NULL DEFAULT 'armed'
                       CHECK (state IN ('{_Q.join(GROUP_STATES)}')),
    winner_source_id   INTEGER,
    event_id           TEXT    NOT NULL DEFAULT '',
    event_payload      TEXT    NOT NULL DEFAULT '',
    event_remote_epoch INTEGER NOT NULL DEFAULT 0,
    delivery_token     TEXT    NOT NULL DEFAULT '',
    delivery_attempts  INTEGER NOT NULL DEFAULT 0,
    blocked_reason     TEXT    NOT NULL DEFAULT '',
    notified_at        INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL DEFAULT 0,
    updated_at         INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_await_groups_active
    ON await_groups(session_id) WHERE state IN {_ACTIVE_SQL};
CREATE INDEX IF NOT EXISTS idx_await_groups_state ON await_groups(state);
CREATE TABLE IF NOT EXISTS await_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL REFERENCES await_groups(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL CHECK (kind IN ('{_Q.join(SOURCE_KINDS)}')),
    spec          TEXT    NOT NULL,
    watermark     TEXT    NOT NULL DEFAULT '',
    interval_sec  INTEGER NOT NULL DEFAULT {DEFAULT_INTERVAL_SEC}
                  CHECK (interval_sec >= {MIN_INTERVAL_SEC}),
    next_check_at INTEGER NOT NULL DEFAULT 0,
    lease_token   TEXT    NOT NULL DEFAULT '',
    lease_until   INTEGER NOT NULL DEFAULT 0,
    state         TEXT    NOT NULL DEFAULT 'armed'
                  CHECK (state IN ('{_Q.join(SOURCE_STATES)}')),
    fail_count    INTEGER NOT NULL DEFAULT 0,
    fail_class    TEXT    NOT NULL DEFAULT '',
    last_error    TEXT    NOT NULL DEFAULT '',
    notified_at   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_await_sources_group ON await_sources(group_id);
CREATE INDEX IF NOT EXISTS idx_await_sources_due
    ON await_sources(next_check_at) WHERE state = 'armed';
"""


class AwaitConflict(Exception):
    """The session already holds an active await group (one per session)."""


@dataclasses.dataclass
class SourceSpec:
    """One source to arm: its kind, persisted spec, baseline watermark and interval."""

    kind: str
    spec: dict[str, Any]
    watermark: str = ""
    interval_sec: int = DEFAULT_INTERVAL_SEC


@dataclasses.dataclass
class AwaitSource:  # pylint: disable=too-many-instance-attributes  # one row, flat
    """One ``await_sources`` row."""

    id: int
    group_id: int
    kind: str
    spec: str = "{}"
    watermark: str = ""
    interval_sec: int = DEFAULT_INTERVAL_SEC
    next_check_at: int = 0
    lease_token: str = ""
    lease_until: int = 0
    state: str = "armed"
    fail_count: int = 0
    fail_class: str = ""
    last_error: str = ""
    notified_at: int = 0

    def spec_dict(self) -> dict[str, Any]:
        """The parsed ``spec`` JSON (``{}`` when unreadable)."""
        try:
            data = json.loads(self.spec or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


@dataclasses.dataclass
class AwaitGroup:  # pylint: disable=too-many-instance-attributes  # one row, flat
    """One ``await_groups`` row."""

    id: int
    session_id: str
    config_dir: str = ""
    cwd: str = ""
    no_codex: bool = False
    prompt_template: str = ""
    until_epoch: int = 0
    grace_until_epoch: int = 0
    state: str = "armed"
    winner_source_id: int | None = None
    event_id: str = ""
    event_payload: str = ""
    event_remote_epoch: int = 0
    delivery_token: str = ""
    delivery_attempts: int = 0
    blocked_reason: str = ""
    notified_at: int = 0
    created_at: int = 0
    updated_at: int = 0


_GROUP_FIELDS = frozenset(f.name for f in dataclasses.fields(AwaitGroup))
_SOURCE_FIELDS = frozenset(f.name for f in dataclasses.fields(AwaitSource))


def _row_to_group(row: sqlite3.Row) -> AwaitGroup:
    data = {k: row[k] for k in row.keys() if k in _GROUP_FIELDS}
    data["no_codex"] = bool(data.get("no_codex"))
    return AwaitGroup(**data)


def _row_to_source(row: sqlite3.Row) -> AwaitSource:
    return AwaitSource(**{k: row[k] for k in row.keys() if k in _SOURCE_FIELDS})


def new_token() -> str:
    """A fresh opaque token (lease / delivery)."""
    return uuid.uuid4().hex


class AwaitStoreMixin:  # pylint: disable=too-many-public-methods  # one per transition
    """The await transitions. Every multi-row change runs in ONE ``BEGIN IMMEDIATE``."""

    conn: sqlite3.Connection

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` … commit, rolling back on any error."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    # ------------------------------------------------------------------ arm
    def _insert_group(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        *,
        config_dir: str,
        cwd: str,
        no_codex: bool,
        prompt_template: str,
        until_epoch: int,
        sources: list[SourceSpec],
        now: int,
    ) -> int:
        if not sources:
            raise ValueError("an await group needs at least one source")
        try:
            cur = self.conn.execute(
                "INSERT INTO await_groups (session_id, config_dir, cwd, no_codex, "
                "prompt_template, until_epoch, grace_until_epoch, state, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'armed', ?, ?)",
                (
                    session_id,
                    config_dir,
                    cwd,
                    int(no_codex),
                    prompt_template,
                    until_epoch,
                    until_epoch + GRACE_SEC,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "unique" in str(exc).lower():
                raise AwaitConflict(
                    f"session {session_id[:8]} already has an active await group"
                ) from exc
            raise
        group_id = int(cur.lastrowid or 0)
        for src in sources:
            interval = max(MIN_INTERVAL_SEC, int(src.interval_sec))
            self.conn.execute(
                "INSERT INTO await_sources (group_id, kind, spec, watermark, interval_sec, "
                "next_check_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    src.kind,
                    json.dumps(src.spec, sort_keys=True),
                    src.watermark,
                    interval,
                    now + interval,
                ),
            )
        return group_id

    def arm_await(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        *,
        config_dir: str,
        cwd: str,
        no_codex: bool,
        prompt_template: str,
        until_epoch: int,
        sources: list[SourceSpec],
        now: int,
    ) -> int:
        """Insert one group + its sources atomically; :class:`AwaitConflict` if one is active."""
        with self._immediate():
            return self._insert_group(
                session_id,
                config_dir=config_dir,
                cwd=cwd,
                no_codex=no_codex,
                prompt_template=prompt_template,
                until_epoch=until_epoch,
                sources=sources,
                now=now,
            )

    def arm_await_and_close(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        *,
        config_dir: str,
        cwd: str,
        no_codex: bool,
        prompt_template: str,
        until_epoch: int,
        sources: list[SourceSpec],
        now: int,
        close_now_ms: int,
    ) -> tuple[int, str]:
        """:meth:`arm_await` plus the close-after-turn arm in the SAME transaction.

        The close columns are written exactly as ``Store.arm_close`` writes them (stamp,
        fresh token, no binding), so the Stop hook's ``claim_after_turn`` closes the tab
        after this turn. Either both land or neither does. Returns ``(group_id, token)``.
        """
        token = new_token()
        with self._immediate():
            group_id = self._insert_group(
                session_id,
                config_dir=config_dir,
                cwd=cwd,
                no_codex=no_codex,
                prompt_template=prompt_template,
                until_epoch=until_epoch,
                sources=sources,
                now=now,
            )
            cur = self.conn.execute(
                "UPDATE sessions SET close_requested_at = ?, close_token = ?, close_bound = '', "
                "updated_at = ? WHERE session_id = ?",
                (close_now_ms, token, close_now_ms, session_id),
            )
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError(f"no session row {session_id}")
        return group_id, token

    # ------------------------------------------------------------------ reads
    def get_await_group(self, group_id: int) -> AwaitGroup | None:
        """One group by id."""
        row = self.conn.execute("SELECT * FROM await_groups WHERE id = ?", (group_id,)).fetchone()
        return _row_to_group(row) if row else None

    def await_sources_of(self, group_id: int) -> list[AwaitSource]:
        """Every source of *group_id*, in arm order."""
        rows = self.conn.execute(
            "SELECT * FROM await_sources WHERE group_id = ? ORDER BY id", (group_id,)
        ).fetchall()
        return [_row_to_source(r) for r in rows]

    def get_await_source(self, source_id: int) -> AwaitSource | None:
        """One source by id."""
        row = self.conn.execute("SELECT * FROM await_sources WHERE id = ?", (source_id,)).fetchone()
        return _row_to_source(row) if row else None

    def list_awaits(
        self, session_id: str | None = None, *, include_inactive: bool = False
    ) -> list[tuple[AwaitGroup, list[AwaitSource]]]:
        """Groups (newest first) with their sources; active ones only unless asked."""
        where: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if not include_inactive:
            where.append(f"state IN {_ACTIVE_SQL}")
        sql = "SELECT * FROM await_groups"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        groups = [_row_to_group(r) for r in self.conn.execute(sql, params).fetchall()]
        return [(g, self.await_sources_of(g.id)) for g in groups]

    def active_await(self, session_id: str) -> AwaitGroup | None:
        """The session's single active group, if any."""
        row = self.conn.execute(
            f"SELECT * FROM await_groups WHERE session_id = ? AND state IN {_ACTIVE_SQL}",
            (session_id,),
        ).fetchone()
        return _row_to_group(row) if row else None

    def await_groups_in(self, *states: str) -> list[AwaitGroup]:
        """Every group in one of *states* (oldest first)."""
        marks = ", ".join("?" for _ in states)
        rows = self.conn.execute(
            f"SELECT * FROM await_groups WHERE state IN ({marks}) ORDER BY id", states
        ).fetchall()
        return [_row_to_group(r) for r in rows]

    def has_active_awaits(self) -> bool:
        """Cheap: does ANY group need the poller (the poller's < 50 ms exit)?"""
        row = self.conn.execute(
            f"SELECT 1 FROM await_groups WHERE state IN {_ACTIVE_SQL} LIMIT 1"
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ leases
    def lease_due_sources(
        self, now: int, *, limit: int = 10, lease_sec: int = LEASE_SEC
    ) -> list[tuple[AwaitSource, AwaitGroup]]:
        """Lease up to *limit* due sources of polling groups, oldest-due first.

        A source is due when armed, ``next_check_at <= now`` and not leased (or its
        lease expired — a crashed pass). The lease token is written before any probe
        runs, so an overlapping pass (poller + daemon backstop) never probes the same
        source twice.
        """
        leased: list[tuple[AwaitSource, AwaitGroup]] = []
        with self._immediate():
            rows = self.conn.execute(
                "SELECT s.id FROM await_sources s JOIN await_groups g ON g.id = s.group_id "
                f"WHERE s.state = 'armed' AND g.state IN {_POLLING_SQL} "
                "AND s.next_check_at <= ? AND s.lease_until <= ? "
                "ORDER BY s.next_check_at, s.id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for (source_id,) in rows:
                token = new_token()
                self.conn.execute(
                    "UPDATE await_sources SET lease_token = ?, lease_until = ? WHERE id = ?",
                    (token, now + lease_sec, source_id),
                )
            for (source_id,) in rows:
                src = _row_to_source(
                    self.conn.execute(
                        "SELECT * FROM await_sources WHERE id = ?", (source_id,)
                    ).fetchone()
                )
                grp = _row_to_group(
                    self.conn.execute(
                        "SELECT * FROM await_groups WHERE id = ?", (src.group_id,)
                    ).fetchone()
                )
                leased.append((src, grp))
        return leased

    def release_lease(self, source_id: int, lease_token: str) -> bool:
        """Give an unprobed lease back (the pass deadline hit before it started)."""
        cur = self.conn.execute(
            "UPDATE await_sources SET lease_token = '', lease_until = 0 "
            "WHERE id = ? AND lease_token = ?",
            (source_id, lease_token),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finish_probe(  # pylint: disable=too-many-arguments
        self,
        source_id: int,
        lease_token: str,
        *,
        next_check_at: int,
        fail_count: int,
        fail_class: str = "",
        last_error: str = "",
        blocked: bool = False,
    ) -> bool:
        """Record a NOT-fired probe outcome under the lease; ``False`` if the lease is gone."""
        cur = self.conn.execute(
            "UPDATE await_sources SET next_check_at = ?, fail_count = ?, fail_class = ?, "
            "last_error = ?, state = CASE WHEN ? THEN 'blocked' ELSE state END, "
            "lease_token = '', lease_until = 0 "
            "WHERE id = ? AND lease_token = ? AND state = 'armed'",
            (
                next_check_at,
                fail_count,
                fail_class,
                last_error,
                int(blocked),
                source_id,
                lease_token,
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------------------------ fire
    def fire_group(  # pylint: disable=too-many-arguments
        self,
        group_id: int,
        source_id: int,
        lease_token: str,
        *,
        event_id: str,
        payload: str,
        remote_epoch: int,
        watermark: str,
        now: int,
    ) -> str | None:
        """THE winner transaction: the first fired source takes the group, once.

        Proceeds only while the group is still polling (``armed``/``grace``) and the
        source still holds *lease_token*; then the group becomes ``fired`` with the event
        and a fresh delivery token, the winner ``done`` (its watermark advanced) and every
        sibling ``disarmed``. Returns the delivery token, or ``None`` for a loser.
        """
        token = new_token()
        with self._immediate():
            grp = self.conn.execute(
                "SELECT state FROM await_groups WHERE id = ?", (group_id,)
            ).fetchone()
            src = self.conn.execute(
                "SELECT lease_token, state FROM await_sources WHERE id = ? AND group_id = ?",
                (source_id, group_id),
            ).fetchone()
            if (
                grp is None
                or src is None
                or grp["state"] not in POLLING_GROUP_STATES
                or src["state"] != "armed"
                or src["lease_token"] != lease_token
            ):
                return None
            self.conn.execute(
                "UPDATE await_groups SET state = 'fired', winner_source_id = ?, event_id = ?, "
                "event_payload = ?, event_remote_epoch = ?, delivery_token = ?, "
                "delivery_attempts = 0, blocked_reason = '', updated_at = ? WHERE id = ?",
                (source_id, event_id, payload, remote_epoch, token, now, group_id),
            )
            self.conn.execute(
                "UPDATE await_sources SET state = 'done', watermark = ?, lease_token = '', "
                "lease_until = 0 WHERE id = ?",
                (watermark, source_id),
            )
            self.conn.execute(
                "UPDATE await_sources SET state = 'disarmed', lease_token = '', lease_until = 0 "
                "WHERE group_id = ? AND id != ? AND state IN ('armed', 'blocked')",
                (group_id, source_id),
            )
        return token

    # ------------------------------------------------------------------ delivery outbox
    def mark_delivering(self, group_id: int, token: str, now: int) -> bool:
        """CAS ``fired → delivering`` under *token* (one deliverer wins)."""
        cur = self.conn.execute(
            "UPDATE await_groups SET state = 'delivering', updated_at = ? "
            "WHERE id = ? AND state = 'fired' AND delivery_token = ?",
            (now, group_id, token),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_delivered(self, group_id: int, token: str, now: int) -> bool:
        """CAS ``delivering → delivered`` under *token* (``fire-await``'s claim)."""
        cur = self.conn.execute(
            "UPDATE await_groups SET state = 'delivered', updated_at = ? "
            "WHERE id = ? AND state = 'delivering' AND delivery_token = ?",
            (now, group_id, token),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def revert_delivery(
        self, group_id: int, token: str, now: int, *, max_attempts: int, reason: str
    ) -> str:
        """A failed launch: back to ``fired`` (same token) or ``blocked`` after *max_attempts*.

        Accepts ``delivering`` (the launcher returned False) and ``delivered`` claimed by
        *token* whose exec then failed. Returns the new state, ``""`` when *token* no
        longer owns the group.
        """
        with self._immediate():
            row = self.conn.execute(
                "SELECT delivery_attempts FROM await_groups WHERE id = ? "
                "AND state IN ('delivering', 'delivered') AND delivery_token = ?",
                (group_id, token),
            ).fetchone()
            if row is None:
                return ""
            attempts = int(row[0]) + 1
            state = "blocked" if attempts >= max_attempts else "fired"
            self.conn.execute(
                "UPDATE await_groups SET state = ?, delivery_attempts = ?, blocked_reason = ?, "
                "notified_at = CASE WHEN ? = 'blocked' THEN 0 ELSE notified_at END, "
                "updated_at = ? WHERE id = ?",
                (
                    state,
                    attempts,
                    reason if state == "blocked" else "",
                    state,
                    now,
                    group_id,
                ),
            )
        return state

    def block_group(
        self, group_id: int, reason: str, now: int, *, from_states: tuple[str, ...]
    ) -> bool:
        """Move a group to ``blocked`` (with *reason*) from one of *from_states*."""
        marks = ", ".join("?" for _ in from_states)
        cur = self.conn.execute(
            "UPDATE await_groups SET state = 'blocked', blocked_reason = ?, notified_at = 0, "
            f"updated_at = ? WHERE id = ? AND state IN ({marks})",
            (reason, now, group_id, *from_states),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def retry_group(self, group_id: int, now: int) -> str:
        """``-R``: re-open a BLOCKED group, keeping its payload and every watermark.

        A group that already has a winner goes back to ``fired`` with a fresh delivery
        token (the old token dies, so a stale ``fire-await`` tab cannot claim it). One
        blocked for lack of viable sources goes back to polling (``armed``, or ``grace``
        past its deadline) with every blocked source re-armed at its ORIGINAL watermark
        — events that arrived during the outage are still caught. Returns the new state,
        ``""`` when the group is not blocked.
        """
        with self._immediate():
            row = self.conn.execute(
                "SELECT winner_source_id, until_epoch, grace_until_epoch, delivery_token "
                "FROM await_groups WHERE id = ? AND state = 'blocked'",
                (group_id,),
            ).fetchone()
            if row is None:
                return ""
            old_token = str(row["delivery_token"] or "")
            if row["winner_source_id"] is not None:
                state = "fired"
                self.conn.execute(
                    "UPDATE await_groups SET state = 'fired', delivery_token = ?, "
                    "delivery_attempts = 0, blocked_reason = '', notified_at = 0, "
                    "updated_at = ? WHERE id = ? AND state = 'blocked' AND delivery_token = ?",
                    (new_token(), now, group_id, old_token),
                )
                return state
            if now >= int(row["grace_until_epoch"]):
                return ""  # nothing left to wait for: the next pass expires it
            state = "grace" if now >= int(row["until_epoch"]) else "armed"
            self.conn.execute(
                "UPDATE await_groups SET state = ?, blocked_reason = '', notified_at = 0, "
                "updated_at = ? WHERE id = ? AND state = 'blocked' AND delivery_token = ?",
                (state, now, group_id, old_token),
            )
            self.conn.execute(
                "UPDATE await_sources SET state = 'armed', fail_count = 0, fail_class = '', "
                "last_error = '', notified_at = 0, next_check_at = ?, lease_token = '', "
                "lease_until = 0 WHERE group_id = ? AND state = 'blocked'",
                (now, group_id),
            )
        return state

    def disarm_group(self, group_id: int, now: int, *, reason: str = "") -> bool:
        """Disarm one active group and its sources. A ``delivering`` group is disarmed
        too: ``fire-await``'s token claim then fails and nothing resumes."""
        with self._immediate():
            cur = self.conn.execute(
                "UPDATE await_groups SET state = 'disarmed', blocked_reason = ?, "
                f"updated_at = ? WHERE id = ? AND state IN {_ACTIVE_SQL}",
                (reason, now, group_id),
            )
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "UPDATE await_sources SET state = 'disarmed', lease_token = '', lease_until = 0 "
                "WHERE group_id = ? AND state IN ('armed', 'blocked')",
                (group_id,),
            )
        return True

    def viable_source_count(self, group_id: int) -> int:
        """Sources of *group_id* that can still fire."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM await_sources WHERE group_id = ? AND state = 'armed'",
            (group_id,),
        ).fetchone()
        return int(row[0] if row else 0)

    def advance_deadlines(self, now: int) -> list[AwaitGroup]:
        """``armed → grace`` at ``until``; ``armed|grace|blocked-without-winner → expired``
        at ``grace_until``. Returns the groups that EXPIRED in this call."""
        with self._immediate():
            self.conn.execute(
                "UPDATE await_groups SET state = 'grace', updated_at = ? "
                "WHERE state = 'armed' AND until_epoch <= ?",
                (now, now),
            )
            rows = self.conn.execute(
                "SELECT id FROM await_groups WHERE grace_until_epoch <= ? AND ("
                "state IN ('armed', 'grace') OR "
                "(state = 'blocked' AND winner_source_id IS NULL))",
                (now,),
            ).fetchall()
            ids = [int(r[0]) for r in rows]
            for group_id in ids:
                self.conn.execute(
                    "UPDATE await_groups SET state = 'expired', notified_at = 0, updated_at = ? "
                    "WHERE id = ?",
                    (now, group_id),
                )
                self.conn.execute(
                    "UPDATE await_sources SET state = 'disarmed', lease_token = '', "
                    "lease_until = 0 WHERE group_id = ? AND state IN ('armed', 'blocked')",
                    (group_id,),
                )
        groups = [self.get_await_group(i) for i in ids]
        return [g for g in groups if g is not None]

    # ------------------------------------------------------------------ notifications
    def claim_group_notice(self, group_id: int, now: int) -> bool:
        """One notification per group state: ``True`` for the first claimer only."""
        cur = self.conn.execute(
            "UPDATE await_groups SET notified_at = ? WHERE id = ? AND notified_at = 0",
            (now, group_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def claim_source_notice(self, source_id: int, now: int) -> bool:
        """One notification per blocked source: ``True`` for the first claimer only."""
        cur = self.conn.execute(
            "UPDATE await_sources SET notified_at = ? WHERE id = ? AND notified_at = 0",
            (now, source_id),
        )
        self.conn.commit()
        return cur.rowcount == 1
