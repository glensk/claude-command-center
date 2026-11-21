"""ccc's thread lifecycle rows in the shared SQLite file (tp#2372 F3b, Designs 1, 3, 4, 7).

The shared file belongs to another tool (it creates and migrates it); ccc is the only
writer of three tables in it:

- ``ccc_threads``: one row per thread (root session, fork origin, current generation),
- ``ccc_sessions``: one row per session (thread, generation, start/end, cwd, the
  throttled ``last_beat``, the process ``incarnation``),
- ``ccc_thread_successions``: the operation-bound succession (reservation → committing →
  accepted | cancelled | failed), at most one unsettled reservation per source generation.

Off by default: with ``shared_store_path`` empty, :func:`open_shared` returns ``None`` and
nothing here opens any file. ccc never creates the shared file or its tables.

Every write runs in one ``BEGIN IMMEDIATE`` transaction that first checks, inside the
transaction, the component version (``meta.ccc_shared_schema``), the owner's maintenance
flag (``meta.backend``) and the authority epoch (``meta.holder_authority_epoch``) against
the epoch ccc pinned at its first write: any mismatch writes nothing and logs one line.
Lifecycle writes wait up to 2 s for the write lock; a beat waits not at all (skipped when
busy). Each committed write appends one timing row (how long it held the write lock) to
the JSONL file ``TP_TIMING_LOG`` names, the owner's lock-timing log (a separate file, so a
timing row adds no database write).

ccc-private bookkeeping (the pinned epoch, the process → incarnation map, a pending
``/clear`` hand-over) stays in ``state.db``, in tables created only when the store is on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from . import config

#: The component version of the shared tables this ccc writes (``meta.ccc_shared_schema``).
COMPONENT_VERSION = "1"
COMPONENT_KEY = "ccc_shared_schema"
EPOCH_KEY = "holder_authority_epoch"
#: The owner's maintenance marker: ``meta.backend = 'maintenance'`` fences every writer.
BACKEND_KEY = "backend"
MAINTENANCE = "maintenance"

LIFECYCLE_BUSY_MS = 2000
BEAT_BUSY_MS = 0
#: How long a thread-id read (:func:`read_thread_ids`) waits behind a writer's lock.
READ_BUSY_MS = 250
#: Ids per ``IN (...)`` list of a read (well under SQLite's bound-parameter limit).
READ_CHUNK = 500
#: A session's ``last_beat`` is written at most once per this many milliseconds.
BEAT_INTERVAL_MS = 60_000
#: A ``/clear`` hand-over mark older than this is never consumed.
CLEAR_MARK_TTL_MS = 120_000

TIMING_LOG_ENV = "TP_TIMING_LOG"

KIND_CCC = "ccc"
KIND_HEADLESS = "headless"
JOIN_KINDS = ("continue", "park_start", "staff_accept", "clear", "headless")
PENDING_STATES = ("reserved", "committing")
END_REASONS = ("handed_off", "closed", "done")

PRIVATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_store_pins (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_incarnations (
    session_id  TEXT    NOT NULL,
    process_key TEXT    NOT NULL,
    incarnation INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    PRIMARY KEY (session_id, process_key)
);
CREATE TABLE IF NOT EXISTS shared_clear_marks (
    process_key TEXT    PRIMARY KEY,
    tab_key     TEXT    NOT NULL DEFAULT '',
    session_id  TEXT    NOT NULL,
    at          INTEGER NOT NULL
);
"""

T = TypeVar("T")


class Refused(Exception):
    """A write the guards refuse (component, maintenance, epoch); nothing was written."""


def now_ms() -> int:
    """Wall clock in epoch milliseconds."""
    return int(time.time() * 1000)


def log_line(text: str) -> None:
    """Append one line to ccc's shared-store log; never raises."""
    try:
        path = config.app_home() / "shared-store.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {text}\n")
    except OSError:
        pass


def _timing_row(op: str, hold_ms: float, db: str) -> None:
    """One ``ccc_write`` row in ``TP_TIMING_LOG`` (Decision 19); never raises."""
    raw = os.environ.get(TIMING_LOG_ENV, "").strip()
    if not raw:
        return
    row = {
        "kind": "ccc_write",
        "verb": op,
        "hold_ms": round(hold_ms, 2),
        "db": db,
        "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
    try:
        path = Path(raw).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def configured_path(cfg: config.Config | None = None) -> Path | None:
    """The configured shared file, or ``None`` when the key is unset (the store is off)."""
    raw = (cfg if cfg is not None else config.load_config()).shared_store_path
    raw = str(raw or "").strip()
    return Path(raw).expanduser() if raw else None


def process_key() -> str:
    """This hook's Claude process: the parent pid (hooks are exec'd by the Claude process)."""
    return f"ppid:{os.getppid()}"


def tab_key() -> str:
    """This hook's terminal tab (``ITERM_SESSION_ID``), ``""`` outside iTerm."""
    return os.environ.get("ITERM_SESSION_ID", "").strip()


@dataclass(frozen=True)
class Succession:  # pylint: disable=too-many-instance-attributes
    """One ``ccc_thread_successions`` row."""

    op_id: str
    thread_id: str
    source_generation: int
    source_session: str
    target_session: str
    join_kind: str
    state: str
    inherit_close: int
    created_at: int
    settled_at: int | None

    @property
    def pending(self) -> bool:
        """True while the reservation is unsettled (``reserved`` / ``committing``)."""
        return self.state in PENDING_STATES


def _succession(row: sqlite3.Row | None) -> Succession | None:
    if row is None:
        return None
    return Succession(
        op_id=str(row["op_id"]),
        thread_id=str(row["thread_id"]),
        source_generation=int(row["source_generation"]),
        source_session=str(row["source_session"]),
        target_session=str(row["target_session"]),
        join_kind=str(row["join_kind"]),
        state=str(row["state"]),
        inherit_close=int(row["inherit_close"] or 0),
        created_at=int(row["created_at"]),
        settled_at=None if row["settled_at"] is None else int(row["settled_at"]),
    )


@dataclass(frozen=True)
class ReserveResult:
    """:meth:`SharedStore.reserve`'s outcome: ``reserved`` or ``lost`` (with the winner)."""

    status: str
    op: Succession | None
    winner: Succession | None = None


def ensure_private(private: sqlite3.Connection) -> None:
    """Create ccc's private bookkeeping tables in ``state.db`` (only when the store is on)."""
    private.executescript(PRIVATE_SCHEMA)


class SharedStore:  # pylint: disable=too-many-public-methods  # one table family
    """One connection to the shared file; every write is guarded and timed."""

    def __init__(
        self, path: Path, *, busy_ms: int = LIFECYCLE_BUSY_MS, private: sqlite3.Connection
    ) -> None:
        self.path = path
        self.private = private
        ensure_private(private)
        # isolation_level None: the transactions below are explicit (BEGIN IMMEDIATE).
        self.conn = sqlite3.connect(str(path), timeout=busy_ms / 1000, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        self.conn.execute("PRAGMA foreign_keys = ON")
        fk = self.conn.execute("PRAGMA foreign_keys").fetchone()
        if not fk or int(fk[0]) != 1:
            self.conn.close()
            raise sqlite3.OperationalError("shared store: foreign_keys did not turn on")

    def close(self) -> None:
        """Close the shared connection."""
        self.conn.close()

    def __enter__(self) -> SharedStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- guards ---------------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def pinned_epoch(self) -> str | None:
        """The authority epoch ccc pinned at its first shared write (``None`` = not yet)."""
        row = self.private.execute(
            "SELECT value FROM shared_store_pins WHERE key = 'epoch'"
        ).fetchone()
        return None if row is None else str(row[0])

    def pin_epoch(self, epoch: str) -> None:
        """Pin *epoch* (first contact, or an acknowledged restore: ``thread-reconcile``)."""
        self.private.execute(
            "INSERT OR REPLACE INTO shared_store_pins (key, value) VALUES ('epoch', ?)",
            (epoch,),
        )
        self.private.commit()

    def _guard(self) -> str:
        """Refuse unless component, maintenance flag and epoch allow a write; the epoch."""
        component = self._meta(COMPONENT_KEY)
        if component != COMPONENT_VERSION:
            raise Refused(f"unknown {COMPONENT_KEY} {component!r} (ccc writes {COMPONENT_VERSION})")
        if self._meta(BACKEND_KEY) == MAINTENANCE:
            raise Refused("the shared store is under maintenance")
        epoch = self._meta(EPOCH_KEY)
        if epoch is None:
            raise Refused(f"no {EPOCH_KEY}")
        pinned = self.pinned_epoch()
        if pinned is not None and pinned != epoch:
            raise Refused(
                f"stale epoch: the store is at {epoch}, ccc pinned {pinned} "
                "(run: ccc thread-reconcile --report)"
            )
        return epoch

    @contextmanager
    def _tx(self, op: str) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` + guards; commit and a timing row, or roll back."""
        self.conn.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        try:
            epoch = self._guard()
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        _timing_row(op, (time.perf_counter() - started) * 1000, str(self.path))
        if self.pinned_epoch() is None:
            self.pin_epoch(epoch)

    def write(self, op: str, fn: Callable[[sqlite3.Connection], T]) -> T | None:
        """Run *fn* in one guarded write transaction; ``None`` (logged) when refused/failed."""
        try:
            with self._tx(op) as conn:
                return fn(conn)
        except Refused as exc:
            log_line(f"{op}: refused, nothing written: {exc}")
        except sqlite3.Error as exc:
            log_line(f"{op}: {type(exc).__name__}: {exc}")
        return None

    # -- reads ------------------------------------------------------------------------

    def session_row(self, sid: str) -> sqlite3.Row | None:
        """*sid*'s ``ccc_sessions`` row."""
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM ccc_sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        return row

    def thread_row(self, thread_id: str) -> sqlite3.Row | None:
        """*thread_id*'s ``ccc_threads`` row."""
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM ccc_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row

    def thread_of(self, sid: str) -> str | None:
        """*sid*'s thread id, ``None`` when the store has no row for it."""
        row = self.session_row(sid)
        return None if row is None else str(row["thread_id"])

    def op(self, op_id: str) -> Succession | None:
        """The succession of op id *op_id*."""
        return _succession(
            self.conn.execute(
                "SELECT * FROM ccc_thread_successions WHERE op_id = ?", (op_id,)
            ).fetchone()
        )

    def pending_for_target(self, sid: str) -> Succession | None:
        """The unsettled succession whose target is *sid*, if any."""
        return _succession(
            self.conn.execute(
                "SELECT * FROM ccc_thread_successions WHERE target_session = ? "
                "AND state IN ('reserved', 'committing') ORDER BY created_at LIMIT 1",
                (sid,),
            ).fetchone()
        )

    def pending_ops(self) -> list[Succession]:
        """Every unsettled succession (oldest first), for the reconciler."""
        rows = self.conn.execute(
            "SELECT * FROM ccc_thread_successions "
            "WHERE state IN ('reserved', 'committing') ORDER BY created_at"
        ).fetchall()
        return [s for s in map(_succession, rows) if s is not None]

    # -- lifecycle writes -------------------------------------------------------------

    def start_root(
        self,
        sid: str,
        now: int,
        *,
        cwd: str = "",
        forked_from: str | None = None,
        kind: str = KIND_CCC,
        thread_id: str | None = None,
    ) -> bool | None:
        """``INSERT OR IGNORE`` *sid* as generation 0 of its own thread; True if written.

        *thread_id* defaults to *sid* (a ccc root); a headless run names ``run:<id>``.
        A pending reservation that targets *sid* writes nothing: the join writes its row.
        """
        tid = thread_id or sid

        def fn(conn: sqlite3.Connection) -> bool:
            if self.pending_for_target(sid) is not None:
                return False
            if conn.execute("SELECT 1 FROM ccc_sessions WHERE session_id = ?", (sid,)).fetchone():
                return False
            conn.execute(
                "INSERT OR IGNORE INTO ccc_threads (thread_id, kind, root_session, forked_from, "
                "current_generation, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (tid, kind, sid, forked_from, now),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO ccc_sessions (session_id, thread_id, generation, "
                "started_at, cwd) VALUES (?, ?, 0, ?, ?)",
                (sid, tid, now, cwd or None),
            )
            return cur.rowcount > 0

        return self.write("start_root", fn)

    def resume(self, sid: str, now: int, proc: str) -> int | None:
        """A known *sid* resumed in a new process: bump ``incarnation``, clear the end.

        Returns the new incarnation (and maps *proc* to it), ``None`` for an unknown id.
        """

        def fn(conn: sqlite3.Connection) -> int | None:
            cur = conn.execute(
                "UPDATE ccc_sessions SET incarnation = incarnation + 1, ended_at = NULL, "
                "end_reason = NULL WHERE session_id = ?",
                (sid,),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT incarnation FROM ccc_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            return int(row[0])

        got = self.write("resume", fn)
        if got is not None:
            self.map_incarnation(sid, proc, got, now)
        return got

    def map_incarnation(self, sid: str, proc: str, incarnation: int, now: int) -> None:
        """Record which incarnation process *proc* runs (``state.db``)."""
        self.private.execute(
            "INSERT OR REPLACE INTO shared_incarnations (session_id, process_key, incarnation, "
            "at) VALUES (?, ?, ?, ?)",
            (sid, proc, incarnation, now),
        )
        self.private.commit()

    def incarnation_of(self, sid: str, proc: str) -> int | None:
        """The incarnation process *proc* started for *sid*, ``None`` when unrecorded."""
        row = self.private.execute(
            "SELECT incarnation FROM shared_incarnations WHERE session_id = ? AND process_key = ?",
            (sid, proc),
        ).fetchone()
        return None if row is None else int(row[0])

    def end(self, sid: str, now: int, reason: str, *, proc: str | None = None) -> bool | None:
        """``ended_at`` + ``end_reason`` once per incarnation; True if this call wrote it.

        *proc*'s recorded incarnation fences the write: a delayed end from an older
        process matches no row. Without a record the current incarnation is ended.
        """
        if reason not in END_REASONS:
            raise ValueError(f"end_reason {reason!r}")
        inc = self.incarnation_of(sid, proc) if proc else None

        def fn(conn: sqlite3.Connection) -> bool:
            if inc is None:
                cur = conn.execute(
                    "UPDATE ccc_sessions SET ended_at = ?, end_reason = ? "
                    "WHERE session_id = ? AND ended_at IS NULL",
                    (now, reason, sid),
                )
            else:
                cur = conn.execute(
                    "UPDATE ccc_sessions SET ended_at = ?, end_reason = ? "
                    "WHERE session_id = ? AND ended_at IS NULL AND incarnation = ?",
                    (now, reason, sid, inc),
                )
            return cur.rowcount > 0

        return self.write("end", fn)

    def beat(self, sid: str, now: int) -> bool:
        """Write ``last_beat`` at most once per :data:`BEAT_INTERVAL_MS`; True if written.

        The stored beat is read first without a lock: a fresh one writes nothing. The
        write itself is skipped when the lock is busy (open this store with
        ``busy_ms=BEAT_BUSY_MS``). Never raises.
        """
        try:
            row = self.conn.execute(
                "SELECT last_beat FROM ccc_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
        except sqlite3.Error:
            return False
        if row is None or int(row[0] or 0) >= now - BEAT_INTERVAL_MS:
            return False

        def fn(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE ccc_sessions SET last_beat = ? WHERE session_id = ? "
                "AND COALESCE(last_beat, 0) < ?",
                (now, sid, now - BEAT_INTERVAL_MS),
            )
            return cur.rowcount > 0

        try:
            with self._tx("beat") as conn:
                return fn(conn)
        except (Refused, sqlite3.Error):
            return False  # busy, fenced or broken: a beat is advisory, skip it

    # -- successions (Design 3) -------------------------------------------------------

    def _ensure_member(self, conn: sqlite3.Connection, sid: str, now: int) -> sqlite3.Row:
        """*sid*'s row; a source ccc never recorded becomes generation 0 of its own thread."""
        row = self.session_row(sid)
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO ccc_threads (thread_id, kind, root_session, "
                "current_generation, created_at) VALUES (?, 'ccc', ?, 0, ?)",
                (sid, sid, now),
            )
            conn.execute(
                "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at) "
                "VALUES (?, ?, 0, ?)",
                (sid, sid, now),
            )
            row = self.session_row(sid)
        assert row is not None
        return row

    def reserve(  # pylint: disable=too-many-arguments
        self,
        source: str,
        target: str,
        join_kind: str,
        now: int,
        *,
        op_id: str | None = None,
        inherit_close: bool = False,
    ) -> ReserveResult | None:
        """Step 1: reserve *source*'s generation for *target* (idempotent on *op_id*).

        ``lost`` (with the winner) when another succession from that generation is pending
        or already accepted, or when *source* is no longer its thread's current generation.
        """
        if join_kind not in JOIN_KINDS:
            raise ValueError(f"join_kind {join_kind!r}")
        op = op_id or uuid.uuid4().hex

        def fn(conn: sqlite3.Connection) -> ReserveResult:
            existing = self.op(op)
            if existing is not None:
                return ReserveResult("reserved" if existing.pending else existing.state, existing)
            src = self._ensure_member(conn, source, now)
            tid, gen = str(src["thread_id"]), int(src["generation"])
            current = int(self.thread_row(tid)["current_generation"])  # type: ignore[index]
            if current != gen:
                winner = _succession(
                    conn.execute(
                        "SELECT * FROM ccc_thread_successions WHERE thread_id = ? AND "
                        "source_generation = ? AND state = 'accepted'",
                        (tid, gen),
                    ).fetchone()
                )
                return ReserveResult("lost", None, winner)
            try:
                conn.execute(
                    "INSERT INTO ccc_thread_successions (op_id, thread_id, source_generation, "
                    "source_session, target_session, join_kind, state, inherit_close, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                    (op, tid, gen, source, target, join_kind, int(inherit_close), now),
                )
            except sqlite3.IntegrityError:
                winner = _succession(
                    conn.execute(
                        "SELECT * FROM ccc_thread_successions WHERE thread_id = ? AND "
                        "source_generation = ? AND state IN ('reserved', 'committing')",
                        (tid, gen),
                    ).fetchone()
                )
                return ReserveResult("lost", None, winner)
            return ReserveResult("reserved", self.op(op))

        return self.write("reserve", fn)

    def mark_committing(self, op_id: str, now: int) -> bool | None:
        """Step 2 settled: ``reserved`` → ``committing``; True if this call moved it."""
        del now

        def fn(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE ccc_thread_successions SET state = 'committing' "
                "WHERE op_id = ? AND state = 'reserved'",
                (op_id,),
            )
            return cur.rowcount > 0

        return self.write("committing", fn)

    def settle(self, op_id: str, state: str, now: int) -> bool | None:
        """A pending reservation → ``cancelled`` / ``failed``; True if this call moved it.

        Only that operation's reservation is settled; the source generation is left as it
        was (no compensating decrement exists).
        """
        if state not in ("cancelled", "failed"):
            raise ValueError(f"settle state {state!r}")

        def fn(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE ccc_thread_successions SET state = ?, settled_at = ? "
                "WHERE op_id = ? AND state IN ('reserved', 'committing')",
                (state, now, op_id),
            )
            return cur.rowcount > 0

        return self.write(f"settle_{state}", fn)

    @staticmethod
    def _holds_obligation(conn: sqlite3.Connection, sid: str) -> bool:
        """True when *sid* holds an open claim, drive or plan row in the owner's holdings."""
        try:
            row = conn.execute(
                "SELECT 1 FROM holdings WHERE session = ? AND COALESCE(ended, '') = '' "
                "AND (COALESCE(claim_at, '') != '' OR COALESCE(drive_at, '') != '' "
                "OR plan_row IS NOT NULL) LIMIT 1",
                (sid,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False  # no holdings table (a bare fixture): nothing can be held
        return row is not None

    def commit(self, op_id: str, now: int, *, cwd: str = "") -> str | None:
        """Step 3: publish the successor; the outcome word (``None`` = not written).

        ``accepted`` (this call published it), ``already`` (accepted before: a replay is a
        no-op and never reactivates the target), ``cancelled`` / ``failed`` (settled
        otherwise), ``lost`` (the generation CAS lost; the op is marked failed),
        ``quarantined`` (the target's stray root holds an obligation; the op fails),
        ``missing`` (no such op).
        """
        outcome: dict[str, str] = {}

        def fn(conn: sqlite3.Connection) -> str:
            op = self.op(op_id)
            if op is None:
                return "missing"
            if op.state == "accepted":
                return "already"
            if not op.pending:
                return op.state
            cur = conn.execute(
                "UPDATE ccc_threads SET current_generation = current_generation + 1 "
                "WHERE thread_id = ? AND current_generation = ?",
                (op.thread_id, op.source_generation),
            )
            if cur.rowcount == 0:
                outcome["fail"] = "lost"
                raise _Abort
            gen = op.source_generation + 1
            row = self.session_row(op.target_session)
            if row is None:
                conn.execute(
                    "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at, "
                    "cwd) VALUES (?, ?, ?, ?, ?)",
                    (op.target_session, op.thread_id, gen, now, cwd or None),
                )
            elif str(row["thread_id"]) != op.thread_id or int(row["generation"]) != gen:
                stray = str(row["thread_id"])
                members = conn.execute(
                    "SELECT COUNT(*) FROM ccc_sessions WHERE thread_id = ?", (stray,)
                ).fetchone()[0]
                if (
                    int(row["generation"]) != 0
                    or int(members) != 1
                    or self._holds_obligation(conn, op.target_session)
                ):
                    outcome["fail"] = "quarantined"
                    raise _Abort
                conn.execute(
                    "UPDATE ccc_sessions SET thread_id = ?, generation = ? WHERE session_id = ?",
                    (op.thread_id, gen, op.target_session),
                )
                conn.execute(
                    "DELETE FROM ccc_threads WHERE thread_id = ? AND NOT EXISTS (SELECT 1 FROM "
                    "ccc_sessions WHERE thread_id = ?) AND NOT EXISTS (SELECT 1 FROM "
                    "ccc_thread_successions WHERE thread_id = ?)",
                    (stray, stray, stray),
                )
            conn.execute(
                "UPDATE ccc_thread_successions SET state = 'accepted', settled_at = ? "
                "WHERE op_id = ?",
                (now, op_id),
            )
            return "accepted"

        try:
            with self._tx("commit") as conn:
                return fn(conn)
        except _Abort:
            fail = outcome["fail"]
            self.settle(op_id, "failed", now)
            log_line(f"commit {op_id}: {fail}")
            return fail
        except Refused as exc:
            log_line(f"commit {op_id}: refused, nothing written: {exc}")
        except sqlite3.Error as exc:
            log_line(f"commit {op_id}: {type(exc).__name__}: {exc}")
        return None

    def transfer_end(self, op: Succession, now: int) -> None:
        """After an accepted ``continue``/``clear`` op: the source ended as ``handed_off``."""
        if op.join_kind in ("continue", "clear", "park_start"):
            self.end(op.source_session, now, "handed_off")

    # -- headless runs (Design 6) -----------------------------------------------------

    def register_headless(self, run_id: str, sid: str, now: int, *, cwd: str = "") -> bool | None:
        """The ``headless`` thread ``run:<run_id>`` with *sid* as its generation 0."""
        return self.start_root(sid, now, cwd=cwd, kind=KIND_HEADLESS, thread_id=f"run:{run_id}")

    # -- /clear hand-over marks (state.db) --------------------------------------------

    def mark_clear(self, sid: str, proc: str, tab: str, now: int) -> None:
        """SessionEnd(reason=clear): remember *sid* as the process's outgoing session."""
        self.private.execute(
            "INSERT OR REPLACE INTO shared_clear_marks (process_key, tab_key, session_id, at) "
            "VALUES (?, ?, ?, ?)",
            (proc, tab, sid, now),
        )
        self.private.commit()

    def take_clear(self, sid: str, proc: str, tab: str, now: int) -> str | None:
        """SessionStart(source=clear): the outgoing session of the same process (or tab).

        A fresh mark (< :data:`CLEAR_MARK_TTL_MS`) naming another session is consumed once.
        """
        rows: list[Any] = self.private.execute(
            "SELECT process_key, session_id, at FROM shared_clear_marks "
            "WHERE process_key = ? OR (? != '' AND tab_key = ?) "
            "ORDER BY (process_key = ?) DESC, at DESC",
            (proc, tab, tab, proc),
        ).fetchall()
        for key, prev, at in rows:
            if str(prev) == sid or int(at) < now - CLEAR_MARK_TTL_MS:
                continue
            self.private.execute("DELETE FROM shared_clear_marks WHERE process_key = ?", (key,))
            self.private.commit()
            return str(prev)
        return None


class _Abort(Exception):
    """Roll the commit transaction back (the outcome is recorded by the caller)."""


def open_shared(
    private: sqlite3.Connection,
    *,
    cfg: config.Config | None = None,
    path: Path | None = None,
    busy_ms: int = LIFECYCLE_BUSY_MS,
) -> SharedStore | None:
    """The shared store, or ``None`` when it is off, missing or unopenable (logged).

    *path* overrides the configured key (``thread-seed -S``). ccc never creates the file.
    """
    target = path if path is not None else configured_path(cfg)
    if target is None:
        return None
    if not target.is_file():
        log_line(f"open: no shared store at {target}; nothing written")
        return None
    try:
        return SharedStore(target, busy_ms=busy_ms, private=private)
    except sqlite3.Error as exc:
        log_line(f"open {target}: {type(exc).__name__}: {exc}")
        return None


def read_thread_ids(path: Path, session_ids: Iterable[str]) -> dict[str, str]:
    """Every id's ``ccc_sessions.thread_id`` in the shared file at *path* (tp#2372 F3b7).

    The thread reader's helper (:mod:`command_center.thread`): it opens the file READ-ONLY
    (``mode=ro``), so a read writes nothing anywhere, unlike :class:`SharedStore`, which
    creates ccc's private tables in ``state.db`` and is the write path. Ids without a row
    are absent. A missing or unreadable file, or a store without the table, gives ``{}``
    (one log line per call): the caller falls back to ``state.db``. Never raises
    ``sqlite3.Error``.
    """
    ids = list(dict.fromkeys(s for s in session_ids if s))
    if not ids or not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=READ_BUSY_MS / 1000)
    except sqlite3.Error as exc:
        log_line(f"read {path}: {type(exc).__name__}: {exc}")
        return {}
    try:
        for start in range(0, len(ids), READ_CHUNK):
            batch = ids[start : start + READ_CHUNK]
            marks = ",".join("?" * len(batch))
            rows = conn.execute(
                "SELECT session_id, thread_id FROM ccc_sessions "
                f"WHERE session_id IN ({marks}) AND COALESCE(thread_id, '') != ''",
                batch,
            ).fetchall()
            out.update({str(sid): str(tid) for sid, tid in rows})
    except sqlite3.Error as exc:
        log_line(f"read {path}: {type(exc).__name__}: {exc}")
        return {}
    finally:
        conn.close()
    return out
