#!/usr/bin/env python3
"""SQLite-backed store — the single source of truth for session cards.

WAL mode so the hooks, the daemon, the TUI and the browser can all read/write
concurrently. User-authored fields (aim, next_step, blocked_on, deadline, …)
are never clobbered by the automatic reconcile from the live registry.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import dataclasses
import json
import re
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import config
from .aimscore import score_aim_lexical
from .models import (
    DEFAULT_LLM,
    JOB_TYPES,
    LLM_CHOICES,
    AimRevision,
    FileLock,
    FileLockWaiter,
    LiveSession,
    MirrorHealth,
    MirrorVouch,
    Session,
    Status,
    Subgoal,
    SubgoalRevision,
    SwitchClaim,
    TranscriptScan,
    no_codex_conflict,
    now_ms,
    short_id,
)

_JOB_TYPES = frozenset(JOB_TYPES)
_LLM_CHOICES = frozenset(LLM_CHOICES)


def _llm_or_default(value: str | None) -> str:
    """A valid future-job model choice, falling back to :data:`DEFAULT_LLM`."""
    return value if value in _LLM_CHOICES else DEFAULT_LLM


# Normalize a sub-goal's text for tick-carryover matching (case/space/punctuation-insensitive).
_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", text.lower()).strip()


_SESSION_COLUMNS = (
    "session_id",
    "cwd",
    "agent",
    "config_dir",
    "version",
    "name",
    "aim",
    "short_aim",
    "aim_score",
    "aim_score_reason",
    "aim_prev",
    "aim_changed_at",
    "aim_met",
    "aim_assessed_at",
    "aim_met_reason",
    "status",
    "done",
    "done_at",
    "next_step",
    "next_step_source",
    "summary",
    "blocked_on",
    "deadline",
    "done_check_cmd",
    "importance",
    "iterm_session_id",
    "prompt_count",
    "last_response_at",
    "closed_at",
    "close_requested_at",
    "switch_requested_at",
    "switch_config_dir",
    "switch_force",
    "active_subagents",
    "last_seen_pid",
    "keep",
    "auto_closed",
    "needs_summary",
    "context_offset",
    "last_progress_at",
    "subgoals_adaptive",
    "subgoals_aim_rev",
    "manual_progress",
    "drift_severity",
    "drift_reason",
    "drift_at",
    "drift_ack_at",
    "todos",
    "todos_updated_at",
    "draft",
    "prompt",
    "start_when",
    "start_date",
    "fire_at",
    "fire_window",
    "depends_on",
    "job_type",
    "no_codex",
    "llm_overseer",
    "llm_exec",
    "model",
    "effort",
    "idempotency_key",
    "future_file",
    "future_sync_hash",
    "future_synced_at",
    "future_missing_since",
    "archived",
    "created_at",
    "updated_at",
)
_BOOL_COLUMNS = frozenset(
    {
        "done",
        "keep",
        "auto_closed",
        "needs_summary",
        "archived",
        "subgoals_adaptive",
        "draft",
        "aim_met",
        "no_codex",
    }
)
# Columns the automatic reconcile is allowed to touch (never user-authored fields).
# ``config_dir`` is the last-observed live account, stamped by core.reconcile.
_RECONCILE_COLUMNS = frozenset(
    {"cwd", "agent", "config_dir", "name", "status", "last_response_at", "last_seen_pid"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    cwd               TEXT    NOT NULL DEFAULT '',
    agent             TEXT    NOT NULL DEFAULT 'claude',
    config_dir        TEXT    NOT NULL DEFAULT '',
    version           TEXT,
    name              TEXT,
    aim               TEXT,
    short_aim         TEXT,
    aim_score         INTEGER NOT NULL DEFAULT -1,
    aim_score_reason  TEXT,
    aim_prev          TEXT,
    aim_changed_at    INTEGER NOT NULL DEFAULT 0,
    aim_met           INTEGER NOT NULL DEFAULT 0,
    aim_assessed_at   INTEGER NOT NULL DEFAULT 0,
    aim_met_reason    TEXT,
    status            TEXT    NOT NULL DEFAULT 'idle',
    done              INTEGER NOT NULL DEFAULT 0,
    done_at           INTEGER NOT NULL DEFAULT 0,
    next_step         TEXT,
    next_step_source  TEXT    NOT NULL DEFAULT 'auto',
    summary           TEXT,
    blocked_on        TEXT,
    deadline          TEXT,
    done_check_cmd    TEXT,
    importance        INTEGER NOT NULL DEFAULT 0,
    iterm_session_id  TEXT,
    prompt_count      INTEGER NOT NULL DEFAULT 0,
    last_response_at  INTEGER NOT NULL DEFAULT 0,
    closed_at         INTEGER NOT NULL DEFAULT 0,
    close_requested_at INTEGER NOT NULL DEFAULT 0,
    switch_requested_at INTEGER NOT NULL DEFAULT 0,
    switch_config_dir TEXT    NOT NULL DEFAULT '',
    switch_force      INTEGER NOT NULL DEFAULT 0,
    active_subagents  INTEGER NOT NULL DEFAULT 0,
    last_seen_pid     INTEGER,
    keep              INTEGER NOT NULL DEFAULT 0,
    auto_closed       INTEGER NOT NULL DEFAULT 0,
    needs_summary     INTEGER NOT NULL DEFAULT 0,
    context_offset    INTEGER NOT NULL DEFAULT 0,
    last_progress_at  INTEGER NOT NULL DEFAULT 0,
    subgoals_adaptive INTEGER NOT NULL DEFAULT 0,
    subgoals_aim_rev  INTEGER NOT NULL DEFAULT 0,
    manual_progress   INTEGER,
    drift_severity    TEXT    NOT NULL DEFAULT '',
    drift_reason      TEXT,
    drift_at          INTEGER NOT NULL DEFAULT 0,
    drift_ack_at      INTEGER NOT NULL DEFAULT 0,
    todos             TEXT,
    todos_updated_at  INTEGER NOT NULL DEFAULT 0,
    draft             INTEGER NOT NULL DEFAULT 0,
    prompt            TEXT,
    start_when        TEXT,
    start_date        TEXT,
    fire_at           INTEGER NOT NULL DEFAULT 0,
    fire_window       TEXT    NOT NULL DEFAULT '',
    depends_on        TEXT,
    job_type          TEXT    NOT NULL DEFAULT 'claude',
    no_codex          INTEGER NOT NULL DEFAULT 0,
    llm_overseer      TEXT    NOT NULL DEFAULT 'fable-5',
    llm_exec          TEXT    NOT NULL DEFAULT 'fable-5',
    model             TEXT    NOT NULL DEFAULT '',
    effort            TEXT    NOT NULL DEFAULT '',
    idempotency_key   TEXT,
    future_file          TEXT,
    future_sync_hash     TEXT,
    future_synced_at     INTEGER NOT NULL DEFAULT 0,
    future_missing_since INTEGER NOT NULL DEFAULT 0,
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subgoals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    position        INTEGER NOT NULL DEFAULT 0,
    text            TEXT    NOT NULL,
    checked         INTEGER NOT NULL DEFAULT 0,
    source          TEXT    NOT NULL DEFAULT 'user',
    weight          INTEGER NOT NULL DEFAULT 1,
    check_cmd       TEXT,
    model           TEXT,
    derived_aim_rev INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_subgoals_session ON subgoals(session_id);
CREATE TABLE IF NOT EXISTS aim_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    aim         TEXT    NOT NULL,
    score       INTEGER NOT NULL DEFAULT -1,
    short_aim   TEXT,
    created_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_aim_history_session ON aim_history(session_id);
CREATE TABLE IF NOT EXISTS subgoal_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    created_at     INTEGER NOT NULL DEFAULT 0,
    items_json     TEXT    NOT NULL DEFAULT '[]',
    aim            TEXT,
    aim_rev        INTEGER NOT NULL DEFAULT 0,
    trigger        TEXT    NOT NULL DEFAULT '',
    model          TEXT,
    drift_severity TEXT    NOT NULL DEFAULT '',
    drift_reason   TEXT,
    drift_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_subgoal_history_session ON subgoal_history(session_id);
CREATE TABLE IF NOT EXISTS file_locks (
    file_path    TEXT    PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    acquired_at  INTEGER NOT NULL DEFAULT 0,
    refreshed_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_file_locks_session ON file_locks(session_id);
CREATE TABLE IF NOT EXISTS file_lock_waiters (
    file_path   TEXT    NOT NULL,
    session_id  TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    since       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_path, session_id)
);
CREATE INDEX IF NOT EXISTS idx_file_lock_waiters_session ON file_lock_waiters(session_id);
-- Per-session transcript facts (observed model, Codex-workflow marker) cached across
-- PROCESSES, keyed by the transcript's (path, mtime_ns, size) identity. Deliberately NO
-- foreign key on session_id: every hook/daemon/TUI process upserts sessions concurrently,
-- and a scan row must never fail (or cascade) on who wrote the session row first.
CREATE TABLE IF NOT EXISTS transcript_scan (
    session_id       TEXT    PRIMARY KEY,
    path             TEXT    NOT NULL,
    mtime_ns         INTEGER NOT NULL,
    size             INTEGER NOT NULL,
    model            TEXT,
    codex            INTEGER NOT NULL DEFAULT 0,
    codex_scanned_to INTEGER NOT NULL DEFAULT 0,
    scanned_at       INTEGER NOT NULL DEFAULT 0,
    headless         INTEGER
);
-- One row per mirror file a scrubber vouched for (see command_center.scrub): the pass
-- that finds all four identities intact — regenerated document, bytes on disk, the
-- mirror_scrub_cmd they came from, and the row's age — skips the subprocess entirely.
-- Deliberately NO foreign key on session_id, for the same reason transcript_scan has
-- none: the row is about a FILE, and a mirror pass must never fail (or cascade) on who
-- wrote the session row first. A row whose file is gone simply stops matching.
CREATE TABLE IF NOT EXISTS mirror_vouch (
    path        TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    raw_sha     TEXT    NOT NULL,
    out_sha     TEXT    NOT NULL,
    policy      TEXT    NOT NULL,
    vouched_at  INTEGER NOT NULL
);
-- Counters of the LAST mirror pass (single row, id = 1) so `ccc doctor` can report a
-- withheld write without re-running the pass.
CREATE TABLE IF NOT EXISTS mirror_health (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    at        INTEGER NOT NULL,
    vouched   INTEGER NOT NULL DEFAULT 0,
    scrubbed  INTEGER NOT NULL DEFAULT 0,
    withheld  INTEGER NOT NULL DEFAULT 0,
    deferred  INTEGER NOT NULL DEFAULT 0,
    reason    TEXT    NOT NULL DEFAULT ''
);
"""


# Every Session read goes through this SELECT: the sessions row PLUS two derived fields —
# the session's FIRST recorded AIM (revision (1)) and that revision's short label, read
# straight off `aim_history`. Derived rather than denormalized into columns so there is
# nothing for set_aim/set_first_aim/set_short_aim to keep in sync; the correlated
# sub-selects hit idx_aim_history_session and cost nothing at TUI table sizes. Both are
# NULL for an AIM that predates history tracking — display_aim falls back to the live AIM.
_SESSION_SELECT = """
SELECT s.*,
       (SELECT h.aim FROM aim_history h WHERE h.session_id = s.session_id
         ORDER BY h.created_at, h.id LIMIT 1) AS first_aim,
       (SELECT h.short_aim FROM aim_history h WHERE h.session_id = s.session_id
         ORDER BY h.created_at, h.id LIMIT 1) AS first_short_aim
FROM sessions s
"""


_SESSION_FIELDS = frozenset(field.name for field in dataclasses.fields(Session))


def _session_columns(row: sqlite3.Row) -> list[tuple[str, int]]:
    """The ``(field name, positional index)`` pairs of *row* this build's Session knows.

    A property of the SELECT, not of the row, so one call per result set is enough (see
    :func:`_row_to_session`).
    """
    return [(name, i) for i, name in enumerate(row.keys()) if name in _SESSION_FIELDS]


def _row_to_session(row: sqlite3.Row, columns: list[tuple[str, int]] | None = None) -> Session:
    # Keep only the columns THIS process's Session knows. The DB is shared by every ccc
    # process on the machine and the code is an editable install, so a long-lived TUI
    # keeps reading rows that a NEWER ccc (another session's hook, the daemon) has just
    # widened with an ALTER TABLE: `Session(**row)` then raised "unexpected keyword
    # argument 'no_codex'" inside the refresh worker (2026-09-02) and the TUI froze on
    # its last frame. A column this build has but the row lacks keeps the dataclass default.
    #
    # *columns* is that intersection precomputed by the caller: every row of one SELECT has
    # the same columns, so list_sessions() derives it once instead of re-intersecting ~70
    # column names per row (it does this for every session on every refresh).
    #
    # The values are read POSITIONALLY (`tuple(row)` once, then indexed): `row["name"]`
    # is a linear scan over the row's ~70 column names, which cost 29 ms of the 88 ms
    # `list_sessions()` at 637 rows — 18 ms read this way.
    columns = _session_columns(row) if columns is None else columns
    values = tuple(row)
    data: dict[str, Any] = {name: values[i] for name, i in columns}
    for col in _BOOL_COLUMNS:
        if col in data:
            data[col] = bool(data[col])
    return Session(**data)


def _row_to_scan(row: sqlite3.Row) -> TranscriptScan:
    """Build a :class:`TranscriptScan` from a ``transcript_scan`` row (bools coerced).

    ``headless`` is TRI-STATE: absent (a row an older ccc wrote before the column
    existed) or NULL both mean "not determined yet" and stay ``None``, so the next scan
    of that transcript re-probes rather than inheriting a bogus ``False``.
    """
    return TranscriptScan(
        session_id=str(row["session_id"]),
        path=str(row["path"]),
        mtime_ns=int(row["mtime_ns"]),
        size=int(row["size"]),
        model=row["model"],
        codex=bool(row["codex"]),
        codex_scanned_to=int(row["codex_scanned_to"]),
        scanned_at=int(row["scanned_at"]),
        headless=(
            None
            if ("headless" not in row.keys() or row["headless"] is None)
            else bool(row["headless"])
        ),
    )


# ``PRAGMA journal_mode=WAL`` ignores the busy timeout (see Store._enable_wal), so it
# gets its own small backoff: 5 tries with a growing 30ms step ≈ 0.45s worst case, far
# more than the microseconds a peer holds the mode switch, and bounded so a genuinely
# stuck DB still fails loudly instead of hanging a hook.
_WAL_RETRIES = 5
_WAL_RETRY_SLEEP = 0.03


class Store:  # pylint: disable=too-many-public-methods
    """Thin wrapper over the SQLite database."""

    def __init__(self, path: Path | None = None, *, check_same_thread: bool = True) -> None:
        self.path = Path(path) if path is not None else config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        # Concurrent sessions/daemon/TUI all write; wait out a peer's write lock
        # instead of erroring (matters for the atomic BEGIN IMMEDIATE in acquire_file_lock).
        self.conn.execute("PRAGMA busy_timeout=3000")
        self._enable_wal()
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._ensure_columns()

    def _enable_wal(self) -> None:
        """Put the DB in WAL mode, retrying briefly — the one pragma the busy timeout misses.

        Switching journal mode takes an exclusive lock, and SQLite does NOT run the busy
        handler for ``PRAGMA journal_mode``: it returns SQLITE_BUSY straight away. Two ccc
        processes opening the same DB in the same instant (a hook firing while the TUI
        starts) therefore made one of them raise ``database is locked`` out of the
        constructor. A DB already in WAL needs no lock at all, so the read below skips the
        write entirely on every open after the first, and the retry only ever runs while a
        peer holds the mode switch.
        """
        row = self.conn.execute("PRAGMA journal_mode").fetchone()
        if row is not None and str(row[0]).lower() == "wal":
            return
        for attempt in range(_WAL_RETRIES):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if attempt == _WAL_RETRIES - 1:
                    raise
                time.sleep(_WAL_RETRY_SLEEP * (attempt + 1))

    # Columns added after the initial schema; ALTER existing DBs in place.
    _ADDED_COLUMNS = {
        "config_dir": "TEXT NOT NULL DEFAULT ''",
        "version": "TEXT",
        "short_aim": "TEXT",
        "importance": "INTEGER NOT NULL DEFAULT 0",
        "iterm_session_id": "TEXT",
        "prompt_count": "INTEGER NOT NULL DEFAULT 0",
        "context_offset": "INTEGER NOT NULL DEFAULT 0",
        "done_at": "INTEGER NOT NULL DEFAULT 0",
        "todos": "TEXT",
        "todos_updated_at": "INTEGER NOT NULL DEFAULT 0",
        "draft": "INTEGER NOT NULL DEFAULT 0",
        "prompt": "TEXT",
        "start_when": "TEXT",
        "start_date": "TEXT",
        # Parked-prompt auto-fire: epoch seconds a draft dispatches at (0 = not armed)
        # and the rate-limit window that timestamp came from ('' = none). Deliberately
        # NOT mirrored into the Obsidian job file (futuresync's targeted imports never
        # touch them) — arming is a CLI/daemon concern, not a file-editable field.
        "fire_at": "INTEGER NOT NULL DEFAULT 0",
        "fire_window": "TEXT NOT NULL DEFAULT ''",
        "depends_on": "TEXT",
        "job_type": "TEXT NOT NULL DEFAULT 'claude'",
        # Per-session Codex opt-out: CCC_NO_CODEX=1 in every launch/resume env.
        "no_codex": "INTEGER NOT NULL DEFAULT 0",
        "llm_overseer": "TEXT NOT NULL DEFAULT 'fable-5'",
        "llm_exec": "TEXT NOT NULL DEFAULT 'fable-5'",
        # OBSERVED runtime values (distinct from the llm_overseer/llm_exec job config):
        # the model the session actually ran on + its --effort reasoning level.
        "model": "TEXT NOT NULL DEFAULT ''",
        "effort": "TEXT NOT NULL DEFAULT ''",
        # Caller-supplied create-or-retrieve key for `ccc new-job -K` (NULL = none).
        # UNIQUE via a partial index built in _ensure_columns (SQLite cannot ALTER in
        # a UNIQUE column), so many NULLs coexist and one key owns exactly one row.
        "idempotency_key": "TEXT",
        "future_file": "TEXT",
        "future_sync_hash": "TEXT",
        "future_synced_at": "INTEGER NOT NULL DEFAULT 0",
        "future_missing_since": "INTEGER NOT NULL DEFAULT 0",
        "aim_score": "INTEGER NOT NULL DEFAULT -1",
        "aim_score_reason": "TEXT",
        "aim_prev": "TEXT",
        "aim_changed_at": "INTEGER NOT NULL DEFAULT 0",
        "aim_met": "INTEGER NOT NULL DEFAULT 0",
        "aim_assessed_at": "INTEGER NOT NULL DEFAULT 0",
        "aim_met_reason": "TEXT",
        "last_progress_at": "INTEGER NOT NULL DEFAULT 0",
        "subgoals_adaptive": "INTEGER NOT NULL DEFAULT 0",
        "subgoals_aim_rev": "INTEGER NOT NULL DEFAULT 0",
        "manual_progress": "INTEGER",
        "drift_severity": "TEXT NOT NULL DEFAULT ''",
        "drift_reason": "TEXT",
        "drift_at": "INTEGER NOT NULL DEFAULT 0",
        "drift_ack_at": "INTEGER NOT NULL DEFAULT 0",
        # When reconcile first saw the process gone (0 = alive / closed pre-feature).
        "closed_at": "INTEGER NOT NULL DEFAULT 0",
        # Epoch-ms a `mark-done --close` armed a close-after-turn request (0 = unarmed).
        "close_requested_at": "INTEGER NOT NULL DEFAULT 0",
        # `ccc switch-account`: epoch-ms the relaunch-after-turn was armed (0 = unarmed) and
        # the target account's config dir it relaunches under (claimed together).
        "switch_requested_at": "INTEGER NOT NULL DEFAULT 0",
        "switch_config_dir": "TEXT NOT NULL DEFAULT ''",
        "switch_force": "INTEGER NOT NULL DEFAULT 0",
        # In-flight IN-PROCESS Agent-tool subagents, kept by the SubagentStart/
        # SubagentStop hook pair (see bump_subagents): the one signal a subagent with
        # no child process and no transcript record yet still shows up in.
        "active_subagents": "INTEGER NOT NULL DEFAULT 0",
    }
    # Same, for the subgoals table (auto-progress marks its rows source='auto').
    _ADDED_SUBGOAL_COLUMNS = {
        "source": "TEXT NOT NULL DEFAULT 'user'",
        "weight": "INTEGER NOT NULL DEFAULT 1",
        "check_cmd": "TEXT",
        "model": "TEXT",
        "derived_aim_rev": "INTEGER NOT NULL DEFAULT 0",
    }
    # Same, for the aim_history table (the per-revision short label, added with this feature).
    _ADDED_AIM_HISTORY_COLUMNS = {
        "short_aim": "TEXT",
    }
    # Same, for the transcript_scan table. Nullable on purpose: NULL = "not determined
    # yet" (a row written before the column existed, or a transcript whose first record
    # could not be read), which the next scan re-probes — see :func:`_row_to_scan`.
    _ADDED_TRANSCRIPT_SCAN_COLUMNS = {
        "headless": "INTEGER",
    }

    def _add_column(self, table: str, column: str, decl: str) -> None:
        """``ALTER TABLE`` *table* to add *column*, tolerating a peer that just did it.

        Every ccc process (hooks, daemon, TUI) opens the store and runs this migration,
        and the DDL runs in autocommit (Python's legacy transaction control only opens an
        implicit transaction before DML), so two fresh builds can both read the PRAGMA
        before either ALTER lands and the loser gets "duplicate column name". That is the
        migration already being done — swallow exactly that error and nothing else.
        """
        try:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def _ensure_columns(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(sessions)")}
        for column, decl in self._ADDED_COLUMNS.items():
            if column not in existing:
                self._add_column("sessions", column, decl)
                if column == "config_dir":
                    self._backfill_config_dir()
        sg_existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(subgoals)")}
        for column, decl in self._ADDED_SUBGOAL_COLUMNS.items():
            if column not in sg_existing:
                self._add_column("subgoals", column, decl)
        ah_existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(aim_history)")}
        for column, decl in self._ADDED_AIM_HISTORY_COLUMNS.items():
            if column not in ah_existing:
                self._add_column("aim_history", column, decl)
        ts_existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(transcript_scan)")
        }
        for column, decl in self._ADDED_TRANSCRIPT_SCAN_COLUMNS.items():
            if column not in ts_existing:
                self._add_column("transcript_scan", column, decl)
        # Partial index for the armed-fire scan (statusline chip + daemon dispatch).
        # Created HERE, not in _SCHEMA: an old DB only gains fire_at via the ALTER
        # loop above, and an index referencing a missing column would fail the open.
        # The first cut (idx_sessions_armed_fire) was draft-scoped; attached prompts
        # (fire_at on a LIVE session row) widened the predicate, hence the new name —
        # CREATE IF NOT EXISTS cannot redefine an existing index in place.
        self.conn.execute("DROP INDEX IF EXISTS idx_sessions_armed_fire")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_fire_pending ON sessions(fire_at) "
            "WHERE archived = 0 AND fire_at > 0"
        )
        # The UNIQUE half of `idempotency_key`. A PARTIAL index (WHERE NOT NULL) because
        # every keyless row is NULL and SQLite would otherwise treat those as distinct
        # anyway — this states the intent and keeps the index tiny. Created HERE for the
        # same reason as the one above: an old DB only gains the column in the ALTER loop.
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_idempotency_key "
            "ON sessions(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        self.conn.commit()

    def _backfill_config_dir(self) -> None:
        """One-shot: stamp every pre-existing row with the default account (D3).

        Runs exactly once — the tick the ``config_dir`` column is first ALTERed in.
        Before multi-account, every tracked session ran under the single default
        account (``claude_home()``), so backfill them to it; thereafter an empty
        ``config_dir`` means UNKNOWN (a freshly-created, not-yet-observed row), which
        refuses resume/start in multi-account mode rather than defaulting to private.
        """
        self.conn.execute(
            "UPDATE sessions SET config_dir = ? WHERE config_dir = ''",
            (str(config.claude_home()),),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- sessions -------------------------------------------------------
    def get(self, session_id: str) -> Session | None:
        cur = self.conn.execute(_SESSION_SELECT + " WHERE s.session_id = ?", (session_id,))
        row = cur.fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, include_archived: bool = False) -> list[Session]:
        sql = _SESSION_SELECT
        if not include_archived:
            sql += " WHERE s.archived = 0"
        rows = self.conn.execute(sql).fetchall()
        if not rows:
            return []
        # One intersection for the whole result set (see _session_columns): the columns are
        # a property of the SELECT, not of the row.
        columns = _session_columns(rows[0])
        return [_row_to_session(r, columns) for r in rows]

    def delete(self, session_id: str) -> None:
        """Remove a session, its sub-goals (FK cascade) and its transcript-scan row.

        ``transcript_scan`` carries no foreign key (see :data:`_SCHEMA`), so its row is
        deleted explicitly — otherwise a re-created session id would inherit the facts of
        the transcript the deleted row was scanned from. ``mirror_vouch`` is keyless for
        the same reason and goes with it: a stale vouch must never speak for a file a
        re-created session id would write.
        """
        self.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM transcript_scan WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM mirror_vouch WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def delete_many(self, session_ids: Iterable[str]) -> int:
        """Remove several sessions (sub-goals, scans, vouches); return the count deleted."""
        ids = [(sid,) for sid in session_ids]
        if not ids:
            return 0
        self.conn.executemany("DELETE FROM sessions WHERE session_id = ?", ids)
        self.conn.executemany("DELETE FROM transcript_scan WHERE session_id = ?", ids)
        self.conn.executemany("DELETE FROM mirror_vouch WHERE session_id = ?", ids)
        self.conn.commit()
        return len(ids)

    def prunable_sessions(
        self,
        protect_ids: Iterable[str] = (),
        headless_ids: Iterable[str] = (),
        orphan_ids: Iterable[str] = (),
    ) -> list[Session]:
        """Sessions that look like leftover headless/SDK junk.

        Three kinds qualify, and none is ever live (id in *protect_ids*), done, or
        kept (a user deliberately marked those):

        * **Contentless** — no signal of its own at all: no aim, prompts,
          summary/next-step, sub-goals, importance, or blocked/deadline tag. That is
          the shape of a ``claude -p`` row that leaked in at cwd ``/`` before the
          adapter skipped ``entrypoint=sdk-cli``; a genuine user session trips at
          least one guard, so this never deletes real work.
        * **Headless one-shot** (*headless_ids*) — a row whose transcript is a
          ``claude -p`` one-shot (e.g. ``ai.py``'s commit-message generation). These
          carry an env-inherited aim / auto next-step / ``prompt_count=1`` from the
          launching session, so they slip past the contentless guards; we prune them
          regardless of that spurious content. The caller supplies the set (it owns
          transcript classification — see ``ClaudeAdapter.is_oneshot_headless``).
        * **Dead launched** (*orphan_ids*) — a future job that ``start-job`` launched
          (draft flag cleared) but that never had a turn, so no transcript exists and
          it can't be resumed. It carries an AIM inherited from the launch, so the
          contentless guards spare it too; we prune it regardless. The caller owns
          transcript classification — see ``core.orphan_launched_ids``.

        Transcripts persist either way — a pruned id is still resumable (an
        orphan/dead-launched one had none to begin with).
        """
        protect = set(protect_ids)
        headless = set(headless_ids)
        orphans = set(orphan_ids)
        out: list[Session] = []
        for session in self.list_sessions(include_archived=True):
            if session.session_id in protect or session.done or session.keep:
                continue
            if session.session_id in headless or session.session_id in orphans:
                out.append(session)
                continue
            if session.aim or session.summary:
                continue
            if session.next_step or session.blocked_on or session.deadline:
                continue
            if session.importance or session.prompt_count:
                continue
            if self.progress(session.session_id)[1]:  # has sub-goals
                continue
            out.append(session)
        return out

    def ensure(self, session_id: str, cwd: str = "", agent: str = "claude") -> Session:
        """Create the row if missing; return the current Session."""
        existing = self.get(session_id)
        if existing is not None:
            return existing
        ts = now_ms()
        self.conn.execute(
            "INSERT INTO sessions (session_id, cwd, agent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, cwd, agent, ts, ts),
        )
        self.conn.commit()
        got = self.get(session_id)
        assert got is not None
        return got

    def claim_idempotency_key(
        self, key: str, session_id: str, cwd: str, *, no_codex: bool = False
    ) -> tuple[str, bool]:
        """Atomically reserve *key* for a new draft; return ``(owning id, created)``.

        The create-or-retrieve half of ``ccc new-job -K``. One ``BEGIN IMMEDIATE``
        transaction takes the write lock before anyone reads, so N concurrent creators
        with the same key produce exactly ONE row: the winner INSERTs the placeholder
        (which the partial UNIQUE index on ``idempotency_key`` protects), every loser
        trips the constraint and reads the winner's id back. ``created`` is ``True`` for
        the single creator, ``False`` for everyone who retrieved.

        The placeholder carries the *identity* fields a caller compares on (``cwd``,
        ``no_codex``) so a loser can check them immediately; the AIM and the rest arrive
        with the winner's :meth:`create_draft` call a moment later (a loser that races
        in between simply sees ``aim = NULL`` and skips that comparison).
        """
        ts = now_ms()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO sessions (session_id, cwd, agent, draft, status, no_codex, "
                "idempotency_key, created_at, updated_at) "
                "VALUES (?, ?, 'claude', 1, ?, ?, ?, ?, ?)",
                (session_id, cwd, Status.PARKED.value, int(bool(no_codex)), key, ts, ts),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            row = self.conn.execute(
                "SELECT session_id FROM sessions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is None:  # the constraint that fired was the primary key, not the key
                raise
            return str(row["session_id"]), False
        self.conn.commit()
        return session_id, True

    def create_draft(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self,
        session_id: str,
        cwd: str,
        aim: str,
        prompt: str | None = None,
        deadline: str | None = None,
        start_when: str | None = None,
        start_date: str | None = None,
        depends_on: str | None = None,
        job_type: str = "claude",
        no_codex: bool = False,
        llm_overseer: str = DEFAULT_LLM,
        llm_exec: str = DEFAULT_LLM,
        config_dir: str = "",
        fire_at: int = 0,
        fire_window: str = "",
        idempotency_key: str = "",
    ) -> Session:
        """Register a *future job*: a draft row holding an AIM + prompt, launched on demand.

        *session_id* is a freshly-generated UUID so that, when the job is later started
        via ``claude --session-id <id>``, the real session reuses this id and the AIM
        stored here carries over unchanged. A blank prompt stays ``NULL`` — the launcher
        (``cmd_start_job``) falls back to the AIM, so ``NULL`` means "defaults to the AIM
        at launch" and the mirrored job file's empty ``# Prompt`` section round-trips to it.
        *start_when* is free-text shown in the next-step (tags/notes) column
        (e.g. "during holidays");
        *start_date* is the FIXED start date (ISO YYYY-MM-DD — SCHEDULED bucket +
        premature-launch guard). *config_dir* pins the Claude account the job will
        launch under (absolute path; "" ⇒ the default account, stamped explicitly so
        ``start-job`` never hits the multi-account "unknown ⇒ refuse" guard).
        *depends_on* is the full session UUID of another job this one must wait for
        (NULL when blank — see :mod:`command_center.deps`). Routing the AIM through
        :meth:`set_aim` records its history + lexical score.
        *fire_at*/*fire_window* arm the parked-prompt auto-fire: the epoch second the
        job dispatches at and the rate-limit window that produced it (see park.py).
        *no_codex* bans every Codex integration for the launched session (``CCC_NO_CODEX=1``
        in its env); it is REFUSED on a codex job type — see :func:`no_codex_conflict`.
        *idempotency_key* records the caller-supplied create-or-retrieve key ("" = none);
        the atomic claim itself is :meth:`claim_idempotency_key`.
        """
        job_type = job_type if job_type in _JOB_TYPES else "claude"
        conflict = no_codex_conflict(job_type, no_codex)
        if conflict:
            raise ValueError(conflict)
        self.ensure(session_id, cwd=cwd)
        self.update_fields(
            session_id,
            draft=True,
            config_dir=(config_dir.strip() or str(config.claude_home())),
            prompt=(prompt.strip() if prompt and prompt.strip() else None),
            deadline=deadline or None,
            start_when=(start_when.strip() if start_when and start_when.strip() else None),
            start_date=(start_date.strip() if start_date and start_date.strip() else None),
            depends_on=(depends_on.strip() if depends_on and depends_on.strip() else None),
            job_type=job_type,
            no_codex=bool(no_codex),
            llm_overseer=_llm_or_default(llm_overseer),
            llm_exec=_llm_or_default(llm_exec),
            fire_at=max(0, int(fire_at)),
            fire_window=fire_window.strip(),
            idempotency_key=(idempotency_key.strip() or None),
            status=Status.PARKED.value,
        )
        self.set_aim(session_id, aim)
        got = self.get(session_id)
        assert got is not None
        return got

    def clear_draft(self, session_id: str) -> None:
        """Promote a draft to a real session: drop the draft flag as it launches."""
        self.update_fields(session_id, draft=False, status=Status.IDLE.value)

    def claim_draft(self, session_id: str) -> bool:
        """One-shot atomic launch claim of a draft: ``True`` for exactly one claimant.

        The conditional ``UPDATE ... WHERE draft = 1`` is the claim itself (same
        pattern as :meth:`claim_close_request`): with several concurrent launchers —
        the foreground ``ccc park`` waiter, a daemon dispatch tab, a manual
        ``ccc start-job`` — SQLite serializes the writes and only the first sees
        ``rowcount == 1``; everyone else finds the draft flag already gone and must
        not launch. Claiming also consumes any armed auto-fire (``fire_at = 0``).
        """
        cur = self.conn.execute(
            "UPDATE sessions SET draft = 0, fire_at = 0, status = ?, updated_at = ? "
            "WHERE session_id = ? AND draft = 1",
            (Status.IDLE.value, now_ms(), session_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def armed_draft_summary(self, now: int | None = None) -> tuple[int, int] | None:
        """``(earliest fire_at, count)`` over ALL armed fire times, or ``None``.

        Covers armed future-job drafts AND parked prompts attached to live sessions
        (``fire_at`` on a non-draft row). One aggregate over the
        ``idx_sessions_fire_pending`` partial index — cheap enough for the
        per-second statusline chip. *now* is unused for filtering on purpose (an
        overdue job must stay visible, not vanish from the summary).
        """
        del now  # kept for signature stability; overdue rows must remain included
        row = self.conn.execute(
            "SELECT MIN(fire_at) AS earliest, COUNT(*) AS n FROM sessions "
            "WHERE archived = 0 AND fire_at > 0"
        ).fetchone()
        if not row or not row["n"]:
            return None
        return int(row["earliest"]), int(row["n"])

    def claim_fire(self, session_id: str) -> bool:
        """One-shot claim of a pending fire time: ``True`` for exactly one deliverer.

        The attached-prompt twin of :meth:`claim_draft`: the conditional
        ``fire_at → 0 WHERE fire_at > 0`` update is the claim itself, so a second
        delivery tab (or an overlapping daemon pass) finds it already consumed and
        must not deliver the prompt twice.
        """
        cur = self.conn.execute(
            "UPDATE sessions SET fire_at = 0, updated_at = ? WHERE session_id = ? AND fire_at > 0",
            (now_ms(), session_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def update_fields(self, session_id: str, **fields: Any) -> None:
        """Update an explicit whitelist of columns on one session."""
        cols = [c for c in fields if c in _SESSION_COLUMNS and c != "session_id"]
        if not cols:
            return
        values: list[Any] = []
        for col in cols:
            val = fields[col]
            values.append(int(val) if col in _BOOL_COLUMNS and isinstance(val, bool) else val)
        assignments = ", ".join(f"{c} = ?" for c in cols)
        values.extend([now_ms(), session_id])
        self.conn.execute(
            f"UPDATE sessions SET {assignments}, updated_at = ? WHERE session_id = ?", values
        )
        self.conn.commit()

    def claim_close_request(self, session_id: str, now: int, ttl_ms: int) -> bool:
        """One-shot atomic claim of a pending close-after-turn request for *session_id*.

        Returns ``True`` exactly once for a FRESH request (armed within *ttl_ms*): the
        claiming ``UPDATE`` clears the stamp so no later caller can re-fire it, and
        ``rowcount == 1`` means this caller won. An expired stamp (older than the TTL) is
        never claimed (``False``) but is still cleared so it can't linger into a resumed
        session; an unarmed row (``close_requested_at == 0``) returns ``False``. At most
        one caller ever wins the fresh-request race.
        """
        cur = self.conn.execute(
            "UPDATE sessions SET close_requested_at = 0 "
            "WHERE session_id = ? AND close_requested_at != 0 AND close_requested_at > ?",
            (session_id, now - ttl_ms),
        )
        claimed = cur.rowcount == 1
        # Clear any remaining non-zero-but-expired stamp for this session (never claimed).
        self.conn.execute(
            "UPDATE sessions SET close_requested_at = 0 "
            "WHERE session_id = ? AND close_requested_at != 0",
            (session_id,),
        )
        self.conn.commit()
        return claimed

    def claim_after_turn(
        self, session_id: str, now: int, ttl_ms: int
    ) -> tuple[str, SwitchClaim | None]:
        """Atomically claim THE after-turn action armed for *session_id* — close beats switch.

        Returns ``("close", None)``, ``("switch", SwitchClaim)`` or ``("", None)``,
        exactly once per arm: the read and the clearing writes run inside ONE ``BEGIN
        IMMEDIATE`` transaction, so two concurrent Stop-hook callers can never both win
        nor split a close and a switch between them. A tab about to close has nowhere
        to relaunch, so a switch armed alongside a close is dropped. A switch claim
        returns the immutable launch snapshot (target, force, the row's ``no_codex`` and
        cwd) and keeps ``switch_config_dir`` as the account the resumed session is
        EXPECTED to start under (consumed by :meth:`pop_switch_expectation`); every other
        outcome clears all switch state. Stamps older than *ttl_ms* are cleared without
        being claimed, so a stale arm can never fire in a later, unrelated turn.
        """
        threshold = now - ttl_ms
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT close_requested_at, switch_requested_at, switch_config_dir, "
                "switch_force, no_codex, cwd FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                self.conn.commit()
                return "", None
            close_at, switch_at = int(row[0] or 0), int(row[1] or 0)
            target = str(row[2] or "")
            kind: str = ""
            claim: SwitchClaim | None = None
            if close_at > threshold:
                kind = "close"
            elif switch_at > threshold and target:
                kind = "switch"
                claim = SwitchClaim(
                    target=target,
                    force=bool(row[3]),
                    no_codex=bool(row[4]),
                    cwd=str(row[5] or ""),
                )
            if kind == "switch":
                self.conn.execute(
                    "UPDATE sessions SET close_requested_at = 0, switch_requested_at = 0, "
                    "switch_force = 0 WHERE session_id = ?",
                    (session_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE sessions SET close_requested_at = 0, switch_requested_at = 0, "
                    "switch_config_dir = '', switch_force = 0 WHERE session_id = ?",
                    (session_id,),
                )
        except sqlite3.Error:
            self.conn.rollback()
            raise
        self.conn.commit()
        return kind, claim

    def pop_switch_expectation(self, session_id: str) -> str:
        """Return and clear the account a claimed ``switch-account`` expected to land on.

        ``""`` when none is pending. Consumed by the SessionStart hook of the relaunched
        session (an intentional switch must not print the account-drift heads-up) and by
        ``ccc switch-account --undo``.
        """
        row = self.conn.execute(
            "SELECT switch_config_dir FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        target = str(row[0] or "") if row is not None else ""
        if target:
            self.conn.execute(
                "UPDATE sessions SET switch_config_dir = '' WHERE session_id = ?", (session_id,)
            )
            self.conn.commit()
        return target

    # ---- in-process subagents (SubagentStart/SubagentStop) ---------------
    def bump_subagents(self, session_id: str, delta: int) -> int:
        """Add *delta* to the session's in-flight subagent count; return the new value.

        The counter the ``SubagentStart``/``SubagentStop`` hook pair keeps, and the only
        evidence ``switch-account`` has of an IN-PROCESS Agent-tool subagent: it spawns no
        child ``claude`` process and its launch record can reach the transcript later than
        the switch check reads it. One statement does the read-modify-write (SQLite
        serializes it), so two hooks firing at once cannot lose an increment, and the
        ``MAX(0, …)`` floor keeps a stray ``SubagentStop`` (one whose start predates the
        column, or a reset in between) from driving the count negative. ``0`` when the row
        does not exist — an untracked session vetoes nothing.
        """
        self.conn.execute(
            "UPDATE sessions SET active_subagents = MAX(0, active_subagents + ?), "
            "updated_at = ? WHERE session_id = ?",
            (delta, now_ms(), session_id),
        )
        self.conn.commit()
        return self.active_subagents(session_id)

    def active_subagents(self, session_id: str) -> int:
        """In-flight in-process subagents for *session_id* (``0`` when the row is absent)."""
        row = self.conn.execute(
            "SELECT active_subagents FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0

    def reset_subagents(self, session_id: str) -> None:
        """Zero the counter — a crash/restart must never leave a phantom subagent behind.

        Called from the SessionStart and SessionEnd hooks: a session that died mid-subagent
        never fired the matching ``SubagentStop``, and a stuck count would veto every later
        ``switch-account`` for that id.
        """
        self.conn.execute(
            "UPDATE sessions SET active_subagents = 0, updated_at = ? WHERE session_id = ?",
            (now_ms(), session_id),
        )
        self.conn.commit()

    def upsert_from_live(self, live: LiveSession) -> None:
        """Reconcile a live registry entry, preserving user-authored fields.

        A NON-draft row that was soft-hidden by ``ccc archive`` (tp lists it instead) is
        un-archived the moment it is seen live again: a running session must show up in
        the TUI, whatever hid it while parked. Draft rows keep their flag — an archived
        draft is a trashed FUTURE job, and only ``restore-job`` may revive that.
        """
        existing = self.ensure(live.session_id, cwd=live.cwd, agent=live.agent)
        patch: dict[str, Any] = {"cwd": live.cwd, "agent": live.agent, "last_seen_pid": live.pid}
        if live.name:
            patch["name"] = live.name
        self.update_fields(
            live.session_id, **{k: v for k, v in patch.items() if k in _RECONCILE_COLUMNS}
        )
        if existing.archived and not existing.draft:
            self.update_fields(live.session_id, archived=False)

    # ---- transcript scans -----------------------------------------------
    def get_transcript_scan(self, session_id: str) -> TranscriptScan | None:
        """The persisted transcript facts for *session_id*, or ``None`` if never scanned."""
        row = self.conn.execute(
            "SELECT * FROM transcript_scan WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _row_to_scan(row) if row else None

    def transcript_scans(self) -> dict[str, TranscriptScan]:
        """Every persisted transcript scan, keyed by session id — ONE select.

        The whole table is a few hundred short rows and :func:`core.reconcile` needs it
        for every session, so one read beats a per-session lookup.
        """
        rows = self.conn.execute("SELECT * FROM transcript_scan").fetchall()
        return {str(row["session_id"]): _row_to_scan(row) for row in rows}

    def put_transcript_scans(self, scans: Iterable[TranscriptScan]) -> None:
        """Persist *scans* (UPSERT on ``session_id``) in ONE transaction, ONE commit.

        Called once per reconcile pass with only the rows whose transcript actually
        changed, so an all-frozen pass writes (and commits) nothing at all.

        An UPSERT, not ``INSERT OR REPLACE``: a REPLACE deletes and re-inserts the row,
        which resets every column THIS build does not know about (a newer ccc's — the DB
        is shared and the code is an editable install) to its default. The explicit
        ``DO UPDATE SET`` touches only the columns this build owns and leaves the rest of
        the row intact. ``headless`` NULL is written as NULL (None = undetermined, so the
        next scan of that file re-probes) — never coerced to 0.

        No ordering / stale-writer guard on purpose: see ``ClaudeAdapter.scan_transcript``
        § Property P — whichever of two concurrent scanners writes last, the next pass
        re-derives every fact from the current file, so a stale write costs one bounded
        re-read and never a wrong sticky fact.
        """
        payload = [
            (
                scan.session_id,
                scan.path,
                int(scan.mtime_ns),
                int(scan.size),
                scan.model,
                int(scan.codex),
                int(scan.codex_scanned_to),
                int(scan.scanned_at),
                None if scan.headless is None else int(scan.headless),
            )
            for scan in scans
        ]
        if not payload:
            return
        self.conn.executemany(
            "INSERT INTO transcript_scan (session_id, path, mtime_ns, size, model, "
            "codex, codex_scanned_to, scanned_at, headless) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET path=excluded.path, "
            "mtime_ns=excluded.mtime_ns, size=excluded.size, model=excluded.model, "
            "codex=excluded.codex, codex_scanned_to=excluded.codex_scanned_to, "
            "scanned_at=excluded.scanned_at, headless=excluded.headless",
            payload,
        )
        self.conn.commit()

    # ---- mirror vouches + health ----------------------------------------
    def mirror_vouches(self) -> dict[str, MirrorVouch]:
        """Every scrubber vouch, keyed by mirror path — ONE select per mirror pass.

        The table has one row per mirror file and the pass needs all of them, so one
        read beats a per-card lookup (same rationale as :meth:`transcript_scans`).
        """
        rows = self.conn.execute("SELECT * FROM mirror_vouch").fetchall()
        return {
            str(row["path"]): MirrorVouch(
                path=str(row["path"]),
                session_id=str(row["session_id"]),
                raw_sha=str(row["raw_sha"]),
                out_sha=str(row["out_sha"]),
                policy=str(row["policy"]),
                vouched_at=int(row["vouched_at"]),
            )
            for row in rows
        }

    def put_mirror_vouches(self, rows: Iterable[MirrorVouch]) -> None:
        """Persist *rows* (UPSERT on ``path``) in ONE transaction, ONE commit.

        Called once per mirror pass with only the cards a scrubber actually vouched for,
        so a steady-state pass writes (and commits) nothing at all.

        An UPSERT, not ``INSERT OR REPLACE``: a REPLACE deletes and re-inserts the row,
        resetting every column THIS build does not know about (a newer ccc's — the DB is
        shared and the code is an editable install) to its default. The explicit
        ``DO UPDATE SET`` touches only the columns this build owns.
        """
        payload = [
            (row.path, row.session_id, row.raw_sha, row.out_sha, row.policy, int(row.vouched_at))
            for row in rows
        ]
        if not payload:
            return
        self.conn.executemany(
            "INSERT INTO mirror_vouch (path, session_id, raw_sha, out_sha, policy, vouched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET session_id=excluded.session_id, "
            "raw_sha=excluded.raw_sha, out_sha=excluded.out_sha, policy=excluded.policy, "
            "vouched_at=excluded.vouched_at",
            payload,
        )
        self.conn.commit()

    def drop_mirror_vouches(self, paths: Iterable[str] | None = None) -> None:
        """Forget the vouches of *paths* (``None`` = all of them → the next pass re-scrubs).

        ``ccc sync-mirrors --rescrub`` drops everything; the cleanup pass drops the rows
        of the files it removed, so the table does not accumulate orphans.
        """
        if paths is None:
            self.conn.execute("DELETE FROM mirror_vouch")
            self.conn.commit()
            return
        ids = [(path,) for path in paths]
        if not ids:
            return
        self.conn.executemany("DELETE FROM mirror_vouch WHERE path = ?", ids)
        self.conn.commit()

    def put_mirror_health(self, health: MirrorHealth) -> None:
        """Overwrite the single ``mirror_health`` row with the last pass's counters."""
        self.conn.execute(
            "INSERT INTO mirror_health (id, at, vouched, scrubbed, withheld, deferred, reason) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET at=excluded.at, vouched=excluded.vouched, "
            "scrubbed=excluded.scrubbed, withheld=excluded.withheld, "
            "deferred=excluded.deferred, reason=excluded.reason",
            (
                int(health.at),
                int(health.vouched),
                int(health.scrubbed),
                int(health.withheld),
                int(health.deferred),
                health.reason,
            ),
        )
        self.conn.commit()

    def mirror_health(self) -> MirrorHealth | None:
        """Counters of the last mirror pass, or ``None`` when none has run yet."""
        row = self.conn.execute("SELECT * FROM mirror_health WHERE id = 1").fetchone()
        if row is None:
            return None
        return MirrorHealth(
            at=int(row["at"]),
            vouched=int(row["vouched"]),
            scrubbed=int(row["scrubbed"]),
            withheld=int(row["withheld"]),
            deferred=int(row["deferred"]),
            reason=str(row["reason"]),
        )

    # ---- subgoals -------------------------------------------------------
    def list_subgoals(self, session_id: str) -> list[Subgoal]:
        rows = self.conn.execute(
            "SELECT * FROM subgoals WHERE session_id = ? ORDER BY position, id", (session_id,)
        ).fetchall()
        return [
            Subgoal(
                r["id"],
                r["session_id"],
                r["position"],
                r["text"],
                bool(r["checked"]),
                r["source"] if "source" in r.keys() else "user",
                r["weight"] if "weight" in r.keys() else 1,
                r["check_cmd"] if "check_cmd" in r.keys() else None,
                r["model"] if "model" in r.keys() else None,
                r["derived_aim_rev"] if "derived_aim_rev" in r.keys() else 0,
            )
            for r in rows
        ]

    def set_subgoals(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        session_id: str,
        items: list[str],
        source: str = "user",
        weights: list[int] | None = None,
        *,
        model: str | None = None,
        aim_rev: int | None = None,
        trigger: str | None = None,
        adaptive: bool | None = None,
        merge: bool = False,
        drift_severity: str = "",
    ) -> bool:
        """Replace a session's checklist with *items*; return whether it changed.

        *source* is ``"user"`` (manual), ``"auto"`` (cheap-model derive) or ``"agent"``
        (the in-session agent). *weights* (parallel, default 1) set per-item importance.

        Provenance: *model* records who authored the list (shown in the header);
        *aim_rev* (default: the current AIM revision) ties the checklist to an AIM
        version; *trigger* (default derived from *source*) labels the history entry.
        *adaptive* (default: ``source != 'user'``) marks the list to re-derive on AIM
        change. With *merge*, ticks carry over to any new item whose normalized text
        matches a previously-checked one (smart-merge preserving progress).

        On a real change (membership/text/weight differs) this snapshots a
        ``subgoal_history`` entry; identical content is a no-op and returns ``False``.
        """
        old = self.list_subgoals(session_id)
        new_weights = [(weights[i] if weights else 1) for i in range(len(items))]
        changed = [(s.text, s.weight) for s in old] != list(zip(items, new_weights, strict=False))
        checked_norms = {_norm(s.text) for s in old if s.checked} if merge else set()
        session = self.get(session_id)
        if aim_rev is None:
            aim_rev = self.count_aim_history(session_id) or (1 if session and session.aim else 0)
        if trigger is None:
            trigger = {"auto": "auto-derive", "agent": "agent-merge"}.get(source, "user-edit")
        if adaptive is None:
            adaptive = source != "user"
        checks = [int(_norm(text) in checked_norms) for text in items]
        self.conn.execute("DELETE FROM subgoals WHERE session_id = ?", (session_id,))
        self.conn.executemany(
            "INSERT INTO subgoals "
            "(session_id, position, text, checked, source, weight, model, derived_aim_rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (session_id, pos, text, checks[pos], source, new_weights[pos], model, aim_rev)
                for pos, text in enumerate(items)
            ],
        )
        self.conn.execute(
            "UPDATE sessions SET subgoals_adaptive = ?, subgoals_aim_rev = ? WHERE session_id = ?",
            (int(adaptive), aim_rev, session_id),
        )
        self.conn.commit()
        if changed and items:
            # First-ever checklist has nothing to drift from; later versions await the checker.
            severity = drift_severity or ("none" if not old else "")
            self._record_subgoal_history(
                session_id,
                list(zip(items, [bool(c) for c in checks], strict=False)),
                aim=session.aim if session else None,
                aim_rev=aim_rev,
                trigger=trigger,
                model=model,
                drift_severity=severity,
            )
        return changed

    def clear_auto_subgoals(self, session_id: str) -> None:
        """Delete only the auto-derived checklist (leave user-authored goals intact)."""
        self.conn.execute(
            "DELETE FROM subgoals WHERE session_id = ? AND source = 'auto'", (session_id,)
        )
        self.conn.commit()

    # ---- subgoal history + drift verdict --------------------------------
    def _record_subgoal_history(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        items_checked: list[tuple[str, bool]],
        *,
        aim: str | None,
        aim_rev: int,
        trigger: str,
        model: str | None,
        drift_severity: str = "",
    ) -> None:
        """Append a snapshot of the checklist (with checked state) to the history."""
        payload = json.dumps([[text, bool(checked)] for text, checked in items_checked])
        self.conn.execute(
            "INSERT INTO subgoal_history "
            "(session_id, created_at, items_json, aim, aim_rev, trigger, model, drift_severity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, now_ms(), payload, aim, int(aim_rev), trigger, model, drift_severity),
        )
        self.conn.commit()

    def list_subgoal_history(self, session_id: str) -> list[SubgoalRevision]:
        """The session's sub-goal evolution, oldest first (the last is current)."""
        rows = self.conn.execute(
            "SELECT * FROM subgoal_history WHERE session_id = ? ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
        out: list[SubgoalRevision] = []
        for r in rows:
            try:
                raw = json.loads(r["items_json"]) or []
            except (ValueError, TypeError):
                raw = []
            items = [(str(x[0]), bool(x[1])) for x in raw if isinstance(x, list) and x]
            out.append(
                SubgoalRevision(
                    items,
                    r["aim"],
                    int(r["aim_rev"]),
                    r["trigger"],
                    r["model"],
                    r["drift_severity"],
                    r["drift_reason"],
                    int(r["created_at"]),
                )
            )
        return out

    def latest_subgoal_history_id(self, session_id: str) -> int | None:
        """Row id of the most recent sub-goal version (the one the checker grades)."""
        row = self.conn.execute(
            "SELECT id FROM subgoal_history WHERE session_id = ? ORDER BY created_at DESC, id DESC "
            "LIMIT 1",
            (session_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def set_subgoal_history_drift(
        self, history_id: int, severity: str, reason: str | None, verdict_json: str | None = None
    ) -> None:
        """Write the impartial checker's verdict onto a sub-goal history row."""
        self.conn.execute(
            "UPDATE subgoal_history SET drift_severity = ?, drift_reason = ?, drift_json = ? "
            "WHERE id = ?",
            (severity, reason, verdict_json, history_id),
        )
        self.conn.commit()

    def set_drift(self, session_id: str, severity: str, reason: str | None) -> None:
        """Record the session-level drift verdict; non-flagging severities clear the marker."""
        flagged = severity in ("low", "medium", "high")
        # A new verdict always resets the ack so a fresh flag reads as unresolved; a
        # clean check additionally drops the severity so the dot clears.
        self.update_fields(
            session_id,
            drift_severity=severity,
            drift_reason=reason or None,
            drift_at=now_ms() if flagged else 0,
            drift_ack_at=0,
        )

    def ack_drift(self, session_id: str) -> None:
        """Acknowledge (resolve) a flagged drift so the blue dot clears."""
        self.update_fields(session_id, drift_ack_at=now_ms())

    def subgoals_stale(self, session_id: str) -> bool:
        """True if an adaptive checklist was built for an older AIM than the current one.

        Drives the "re-align your sub-goals" nudge: an adaptive list whose
        ``subgoals_aim_rev`` lags the AIM revision count needs regenerating.
        """
        session = self.get(session_id)
        if session is None or not session.subgoals_adaptive:
            return False
        return session.subgoals_aim_rev < self.count_aim_history(session_id)

    def set_aim(self, session_id: str, aim: str | None) -> bool:
        """Set the AIM through the single chokepoint; return whether it changed.

        On a real change this also (a) drops the auto-derived checklist and resets
        ``context_offset`` so a fresh, AIM-aligned checklist re-derives, (b) sets an
        instant lexical ``aim_score`` (clearing the stale reason) so the UI is never
        blank — an async LLM refine can overwrite the score later, and (c) clears the
        stale ``short_aim`` label so the column shows the new full AIM until the cheap
        codex generator (spawned by ``cmd_set_aim``) backfills a fresh short label.
        """
        current = self.get(session_id)
        old = current.aim if current else None
        new = aim if (aim and aim.strip()) else None
        if (new or None) == (old or None):
            return False
        self.update_fields(
            session_id,
            aim=new,
            short_aim=None,
            aim_score=score_aim_lexical(new) if new else -1,
            aim_score_reason=None,
            # A new AIM invalidates any prior "is it done?" verdict — clear it so a
            # stale DONE can never linger against a changed goal (also closes the O2
            # race: a detached assessor mid-flight is discarded on write).
            aim_met=False,
            aim_assessed_at=0,
            aim_met_reason=None,
            context_offset=0,
            # Remember where we came from so the status line can show old ====> new this turn
            # (only a real prior AIM — the initial set, old=None, shows no transition).
            aim_prev=old,
            aim_changed_at=now_ms() if old else 0,
        )
        self.clear_auto_subgoals(session_id)
        if new is not None:
            self._record_aim_history(session_id, old, current, new)
        return True

    def set_first_aim(self, session_id: str, aim: str) -> bool:
        """Rewrite the FIRST recorded AIM (``/aim (1)``) **in place**; True if it changed.

        Corrects how the *original* done-condition was stated — a typo, a wording the
        first `/aim` never captured well — WITHOUT touching the current AIM and WITHOUT
        appending a revision, so the running index (`(1)`, `(2)`, …) stays stable. This
        is what the TUI's ``e`` form `/aim (1):` line and ``ccc set-aim --first`` write.

        A blank *aim* is refused (history rows are never emptied). When the first
        revision is ALSO the current one (a single recorded revision, or an AIM that
        predates history tracking) the session's live AIM is rewritten too, so the two
        can never disagree; the stale "is it done?" verdict is dropped in that case.
        """
        new = (aim or "").strip()
        if not new:
            return False
        row = self.conn.execute(
            "SELECT id, aim FROM aim_history WHERE session_id = ? ORDER BY created_at, id LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            # Pre-history session: the live AIM is the sole (unrecorded) revision — rewrite
            # it in place, still as revision 1 (recording it here would invent a revision 2).
            session = self.get(session_id)
            if session is None or not (session.aim or "").strip():
                return False  # nothing to adapt — there is no first AIM yet
            if session.aim == new:
                return False
            self._mirror_first_aim_onto_session(session_id, new)
            return True
        if row["aim"] == new:
            return False
        self.conn.execute(
            "UPDATE aim_history SET aim = ?, score = ?, short_aim = NULL WHERE id = ?",
            (new, score_aim_lexical(new), row["id"]),
        )
        self.conn.commit()
        if self.count_aim_history(session_id) == 1:
            self._mirror_first_aim_onto_session(session_id, new)
        else:
            # The CURRENT revision's short label is generated with the original AIM as its
            # hint (short_aim._original_hint), so rewriting the original makes that label
            # stale too — and the label IS what the narrow `/aim` column and the status line
            # render. Drop it so the column falls back to the full current AIM until the
            # cheap generator (spawned by the caller) backfills a label built on the new original.
            self.set_short_aim(session_id, None)
        return True

    def _mirror_first_aim_onto_session(self, session_id: str, new: str) -> None:
        """Copy a rewritten first AIM onto the session (it is also the current AIM)."""
        self.update_fields(
            session_id,
            aim=new,
            short_aim=None,  # the cheap label is stale → the generator backfills a fresh one
            aim_score=score_aim_lexical(new),
            aim_score_reason=None,
            # Same reasoning as set_aim: never let a DONE verdict outlive the wording it judged.
            aim_met=False,
            aim_assessed_at=0,
            aim_met_reason=None,
        )

    def set_aim_met(self, session_id: str, met: bool, reason: str | None, assessed_at: int) -> None:
        """Record the impartial "is the AIM fulfilled?" verdict (latest wins).

        Written out-of-band by ``ccc assess-aim`` (never the session agent). ``assessed_at``
        stamps when the verdict was formed and drives the new-turn gate (re-assess only once
        ``last_response_at`` has advanced past it). Not monotonic — a later turn can flip
        ``met`` back to False.
        """
        self.update_fields(
            session_id,
            aim_met=met,
            aim_met_reason=reason or None,
            aim_assessed_at=assessed_at,
        )

    def _record_aim_history(
        self, session_id: str, old: str | None, current: Session | None, new: str
    ) -> None:
        """Append *new* to the AIM history (the full first→current progression).

        Seeds the pre-existing original once, so a session whose AIM predates this
        table still shows where it started rather than only its post-upgrade life.
        """
        empty = (
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM aim_history WHERE session_id = ?", (session_id,)
            ).fetchone()["n"]
            == 0
        )
        if empty and old and current is not None:
            seeded_at = current.aim_changed_at or current.created_at or 0
            # Carry the prior short label onto the seeded original row (set_aim already
            # cleared it off the session) so the original's short-aim shows in history.
            self._insert_aim_history(
                session_id, old, current.aim_score, seeded_at, current.short_aim
            )
        self._insert_aim_history(session_id, new, score_aim_lexical(new), now_ms())
        self.conn.commit()

    def _insert_aim_history(
        self, session_id: str, aim: str, score: int, created_at: int, short_aim: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO aim_history (session_id, aim, score, short_aim, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, aim, int(score), short_aim, int(created_at)),
        )

    def set_short_aim(self, session_id: str, short_aim: str | None) -> None:
        """Store the cheap-model short label on the session AND its latest AIM revision.

        Written by the detached ``ccc short-aim`` generator (and the daemon backfill).
        Mirroring onto the most recent ``aim_history`` row is what makes the short label
        appear per-revision in ``ccc aim-history``. A blank label clears back to ``NULL``.
        """
        label = (short_aim or "").strip() or None
        self.update_fields(session_id, short_aim=label)
        self.conn.execute(
            "UPDATE aim_history SET short_aim = ? WHERE id = ("
            "  SELECT id FROM aim_history WHERE session_id = ? ORDER BY created_at DESC, id DESC "
            "  LIMIT 1)",
            (label, session_id),
        )
        self.conn.commit()

    def set_first_short_aim(self, session_id: str, short_aim: str | None) -> None:
        """Store the cheap-model short label on the FIRST recorded AIM revision.

        The counterpart of :meth:`set_short_aim` for revision (1): with
        ``aim_column = "first"`` that revision is what the narrow ``/aim`` column renders,
        so it needs a label of its own once the current AIM has moved on. Written by the
        detached ``ccc short-aim`` generator; a no-op for a session with no recorded
        history (its live AIM *is* revision 1, and carries the session's own label).
        """
        label = (short_aim or "").strip() or None
        self.conn.execute(
            "UPDATE aim_history SET short_aim = ? WHERE id = ("
            "  SELECT id FROM aim_history WHERE session_id = ? ORDER BY created_at, id LIMIT 1)",
            (label, session_id),
        )
        self.conn.commit()

    def list_aim_history(self, session_id: str) -> list[AimRevision]:
        """The session's AIM progression, oldest first (the last is the current AIM)."""
        rows = self.conn.execute(
            "SELECT aim, score, short_aim, created_at FROM aim_history WHERE session_id = ? "
            "ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
        return [
            AimRevision(r["aim"], int(r["score"]), int(r["created_at"]), r["short_aim"])
            for r in rows
        ]

    def count_aim_history(self, session_id: str) -> int:
        """Number of recorded AIM revisions (the current AIM's 1-based running index).

        Cheaper than :meth:`list_aim_history` for the once-per-second status line, which
        only needs the count to label the current AIM ``/aim (N)``. Returns 0 when the AIM
        predates history tracking (no rows yet) — callers treat a set-but-unrecorded AIM as 1.
        """
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM aim_history WHERE session_id = ?", (session_id,)
            ).fetchone()["n"]
        )

    # ---- cross-session file locks --------------------------------------
    def acquire_file_lock(
        self, session_id: str, file_path: str, now: int, live_ids: set[str], ttl_ms: int
    ) -> str | None:
        """Try to acquire (or refresh) the lock on *file_path* for *session_id*.

        Returns ``None`` when the caller now holds it — freshly taken, reclaimed from an
        invalid holder, or already held by the caller (TTL refreshed). Otherwise returns the
        **live** holder's session id (contention; the caller must queue/wait).

        A held lock is honoured only when its holder is in *live_ids* AND fresh
        (``now - refreshed_at < ttl_ms``); a stale or dead-holder row is reclaimed. The
        check-then-write runs inside ``BEGIN IMMEDIATE`` so concurrent acquirers from other
        processes serialise (one wins, the rest see contention) rather than both "winning".
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT session_id, refreshed_at FROM file_locks WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            if row is not None and row["session_id"] != session_id:
                holder = str(row["session_id"])
                fresh = (now - int(row["refreshed_at"])) < ttl_ms
                if holder in live_ids and fresh:
                    self.conn.commit()
                    return holder
            # Free, mine, or reclaimable: upsert me as holder (keep acquired_at if already mine).
            self.conn.execute(
                "INSERT INTO file_locks (file_path, session_id, acquired_at, refreshed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(file_path) DO UPDATE SET session_id = excluded.session_id, "
                "refreshed_at = excluded.refreshed_at, acquired_at = CASE "
                "WHEN file_locks.session_id = excluded.session_id "
                "THEN file_locks.acquired_at ELSE excluded.acquired_at END",
                (file_path, session_id, now, now),
            )
            self.conn.execute(
                "DELETE FROM file_lock_waiters WHERE file_path = ? AND session_id = ?",
                (file_path, session_id),
            )
            self.conn.commit()
            return None
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def release_file_lock(self, session_id: str, file_path: str) -> bool:
        """Drop *session_id*'s lock on *file_path*; return whether a row was removed."""
        cur = self.conn.execute(
            "DELETE FROM file_locks WHERE file_path = ? AND session_id = ?",
            (file_path, session_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def release_all_file_locks(self, session_id: str) -> int:
        """Drop every lock held by *session_id* and clear its own pending waits.

        Waiters parked on the files it *held* are deliberately left in place so they can
        re-acquire on their next attempt. Returns the number of locks released.
        """
        cur = self.conn.execute("DELETE FROM file_locks WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM file_lock_waiters WHERE session_id = ?", (session_id,))
        self.conn.commit()
        return cur.rowcount

    def add_waiter(self, session_id: str, file_path: str, now: int) -> None:
        """Record that *session_id* is waiting to edit *file_path* (idempotent)."""
        self.conn.execute(
            "INSERT INTO file_lock_waiters (file_path, session_id, since) VALUES (?, ?, ?) "
            "ON CONFLICT(file_path, session_id) DO NOTHING",
            (file_path, session_id, now),
        )
        self.conn.commit()

    def waiters_on_my_locks(self, session_id: str) -> list[FileLockWaiter]:
        """Sessions waiting on files *session_id* currently holds (drives the handoff nudge)."""
        rows = self.conn.execute(
            "SELECT w.file_path, w.session_id, w.since FROM file_lock_waiters w "
            "JOIN file_locks l ON l.file_path = w.file_path "
            "WHERE l.session_id = ? AND w.session_id != ? ORDER BY w.since",
            (session_id, session_id),
        ).fetchall()
        return [FileLockWaiter(r["file_path"], str(r["session_id"]), int(r["since"])) for r in rows]

    def list_file_locks(self, live_ids: set[str], ttl_ms: int, now: int) -> list[FileLock]:
        """Every currently-valid lock (held by a live session, not past its TTL)."""
        rows = self.conn.execute(
            "SELECT file_path, session_id, acquired_at, refreshed_at FROM file_locks "
            "ORDER BY refreshed_at"
        ).fetchall()
        return [
            FileLock(
                r["file_path"], str(r["session_id"]), int(r["acquired_at"]), int(r["refreshed_at"])
            )
            for r in rows
            if str(r["session_id"]) in live_ids and (now - int(r["refreshed_at"])) < ttl_ms
        ]

    def set_subgoal_checked(self, subgoal_id: int, checked: bool) -> None:
        self.conn.execute(
            "UPDATE subgoals SET checked = ? WHERE id = ?", (int(checked), subgoal_id)
        )
        self.conn.commit()

    def check_all_subgoals(self, session_id: str) -> int:
        """Tick every still-unchecked sub-goal of a session; return how many flipped.

        Used when a session is marked done: the human's done verdict is authoritative,
        so the checklist is reconciled to 100% rather than left stranded mid-way. A
        manual progress-bar override is cleared for the same reason — a done session
        must never read e.g. 40%.
        """
        cur = self.conn.execute(
            "UPDATE subgoals SET checked = 1 WHERE session_id = ? AND checked = 0",
            (session_id,),
        )
        self.conn.execute(
            "UPDATE sessions SET manual_progress = NULL WHERE session_id = ?", (session_id,)
        )
        self.conn.commit()
        return cur.rowcount

    def set_subgoal_check(self, subgoal_id: int, command: str | None) -> None:
        """Attach (or clear, when ``None``/empty) a shell predicate to one sub-goal."""
        self.conn.execute(
            "UPDATE subgoals SET check_cmd = ? WHERE id = ?", (command or None, subgoal_id)
        )
        self.conn.commit()

    def progress(self, session_id: str) -> tuple[int, int]:
        """Return ``(checked, total)`` checklist counts for a session."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(checked), 0) AS done, COUNT(*) AS total "
            "FROM subgoals WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return (int(row["done"]), int(row["total"]))

    def progress_weighted(self, session_id: str) -> tuple[int, int]:
        """Return weighted ``(done, total)`` = ``(SUM(checked*weight), SUM(weight))``.

        Degenerates to :meth:`progress` when every item has the default weight 1.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(checked * weight), 0) AS done, COALESCE(SUM(weight), 0) AS total "
            "FROM subgoals WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return (int(row["done"]), int(row["total"]))


class AmbiguousJobId(Exception):
    """A job-id prefix that matches more than one session id.

    Carries the offending *given* prefix and every full id it matched; ``str(exc)``
    is the ready-to-print ``error: ambiguous job id …`` message (8-char short forms).
    """

    def __init__(self, given: str, matches: list[str]) -> None:
        self.given = given
        self.matches = matches
        shorts = " ".join(short_id(m).strip() for m in matches)
        super().__init__(f"error: ambiguous job id {given}: matches {shorts}")


def _resolve_job_ids(store_or_jobs: Store | Iterable[Session | str]) -> list[str]:
    """The candidate session ids from a :class:`Store` (all rows, incl. archived) or an
    iterable of :class:`Session`/str ids."""
    if isinstance(store_or_jobs, Store):
        return [s.session_id for s in store_or_jobs.list_sessions(include_archived=True)]
    return [item if isinstance(item, str) else item.session_id for item in store_or_jobs]


def resolve_job_id(store_or_jobs: Store | Iterable[Session | str], given: str) -> str | None:
    """Resolve *given* — a full session id or a unique id prefix — to a full session id.

    Case-insensitive. An exact match wins outright; otherwise the ids whose start
    matches *given*: exactly one → that id; several (and no exact hit) → raise
    :class:`AmbiguousJobId`; none → ``None`` (the caller emits its own "no such job").
    """
    needle = (given or "").strip().lower()
    if not needle:
        return None
    ids = _resolve_job_ids(store_or_jobs)
    for sid in ids:
        if sid.lower() == needle:
            return sid
    prefixed = [sid for sid in ids if sid.lower().startswith(needle)]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        raise AmbiguousJobId(given, prefixed)
    return None
