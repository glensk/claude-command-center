#!/usr/bin/env python3
"""``ccc sent`` and ``ccc input-ledger``: the input ledger's read side and its arming (tp#2170).

``ccc sent -s SESSION [-a SINCE] [-u UNTIL] -j`` (:func:`run_sent`) lists the ``inputs``
rows of one session — every text ccc typed or launched into it, as hashes and transcript
facts, never the text — with the coverage watermark, so a consumer can tell a prompt ccc
injected from one a person typed and fail closed when coverage is not proven:

* read-only in every sense: its own ``sqlite3.connect("file:…?mode=ro", uri=True)``
  connection (never ``Store()``, which migrates and writes), the gap file and the
  transcripts; a ``pending`` row is matched IN MEMORY by the daemon's own matcher
  (:func:`input_ledger.plan_session`) and the stored row stays ``pending``;
* the window is ``[since, until)``; rows written up to :data:`input_ledger.MATCH_WINDOW_MS`
  before ``since`` are looked at too (a row written just before ``since`` can match a line
  inside the window — debate A1), and any ``pending`` row, unbound row or gap entry in that
  lookback makes the answer incomplete;
* ``complete`` is true iff the ledger is armed, its stored coverage is this build's
  :data:`input_ledger.COVERAGE`, ``since >= complete_since`` and no reason above applies;
  ``incomplete_reasons`` names every failing condition (:data:`REASONS`). A store without
  the ledger tables (an older build) is ``not_armed``, never an error.

``ccc input-ledger`` (:func:`ledger_status` / :func:`arm` / :func:`disarm`) shows, sets and
clears the watermark (``bridge_meta`` keys :data:`input_ledger.META_COMPLETE_SINCE`,
:data:`input_ledger.META_COVERAGE`). Arming refuses — listing pids only — while a process
running ccc code that started before the newest ``*.py`` mtime of the installed package
lives (it would inject through old, unledgered code after the watermark), except the
:data:`ARM_ALLOWLIST` long-runners that provably never type; ``-F`` overrides.
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
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import input_ledger as il
from .bridge_aims import connect_ro
from .bridge_json import BridgeError, iso, usage
from .bridge_store import Input, input_from_row

if TYPE_CHECKING:
    from .bridge_target import BridgeDeps

#: Every ``incomplete_reasons`` value, in the order they are reported.
REASONS = (
    "not_armed",
    "coverage_stale",
    "before_watermark",
    "pending_match",
    "unbound_rows",
    "ledger_write_failed",
)
#: The states a row can be in (``ccc input-ledger -S`` counts each).
STATES = ("pending", "matched", "unmatched", "failed")

_SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EPOCH_MS_RE = re.compile(r"^[0-9]{1,16}$")

#: Long-running ccc processes the arm check does not count, each with why it never types
#: into a Claude session: ``(subcommand, flags (any one), reason)``.
ARM_ALLOWLIST: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "tab-symbol",
        ("-w", "--watch"),
        "re-applies iTerm tab badges only; never types into a session",
    ),
    (
        "resume-halted",
        ("--watch",),
        "only opens resume tabs; the claude-session-continue it starts runs fresh code",
    ),
)
#: Executables whose processes run ccc code (matched on the argv token's basename).
_CCC_EXECUTABLES = frozenset({"ccc", "claude-session-continue"})


# --------------------------------------------------------------------------- input parsing


def parse_session(value: str | None) -> str:
    """A full Claude session id (36-char UUID, no prefix matching), else ``invalid_session``."""
    sid = (value or "").strip().lower()
    if not _SESSION_RE.match(sid):
        raise usage("invalid_session", f"not a full session id (36-char UUID): {value!r}")
    return sid


def parse_time_ms(value: str, what: str) -> int:
    """ISO-8601 with ``Z`` or an offset, or epoch milliseconds → epoch ms; else
    ``invalid_time`` (a time without a zone is refused: it would be ambiguous)."""
    text = value.strip()
    if _EPOCH_MS_RE.match(text):
        return int(text)
    try:
        parsed = datetime.fromisoformat(text.replace("z", "Z"))
    except ValueError as exc:
        raise usage(
            "invalid_time", f"{what}: not ISO-8601 with Z/offset or epoch ms: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise usage("invalid_time", f"{what}: ISO-8601 time needs Z or an offset: {value!r}")
    ms = int(parsed.timestamp() * 1000)
    if ms < 0:
        raise usage("invalid_time", f"{what}: before 1970: {value!r}")
    return ms


def window(since: str | None, until: str | None, now_ms: int) -> tuple[int, int]:
    """``(since_ms, until_ms)`` from the ``-a`` / ``-u`` strings (defaults 0 and now);
    ``invalid_window`` when until <= since."""
    since_ms = parse_time_ms(since, "-a/--after") if since is not None else 0
    until_ms = parse_time_ms(until, "-u/--until") if until is not None else now_ms
    if until_ms <= since_ms:
        raise usage("invalid_window", "-u/--until must be after -a/--after")
    return since_ms, until_ms


def iso_ms(ms: int) -> str:
    """Epoch ms → ISO-8601 UTC seconds (``1970-01-01T00:00:00Z`` for 0, unlike :func:`iso`)."""
    return iso(ms) or "1970-01-01T00:00:00Z"


# --------------------------------------------------------------------------- read-only store


def _has_tables(conn: sqlite3.Connection) -> bool:
    names = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('inputs', 'bridge_meta')"
        )
    }
    return names == {"inputs", "bridge_meta"}


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM bridge_meta WHERE key IN (?, ?)",
        (il.META_COMPLETE_SINCE, il.META_COVERAGE),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and value.strip() else None
    except ValueError:
        return None


class _Ledger:  # pylint: disable=too-few-public-methods  # one read-only snapshot
    """What one read-only pass saw: the watermark and the rows asked for."""

    def __init__(self) -> None:
        self.complete_since_ms: int | None = None
        self.coverage: int | None = None
        self.rows: list[Input] = []
        self.unbound: list[Input] = []


def _read(db: Path, session_id: str, lookback_ms: int, until_ms: int) -> _Ledger:
    """The watermark, ALL of *session_id*'s rows (the matcher needs every claimed offset)
    and the unbound pending/unmatched rows of the lookback; a missing db or tables reads
    as an unarmed, empty ledger."""
    out = _Ledger()
    if not db.is_file():
        return out
    conn = connect_ro(db)
    try:
        if not _has_tables(conn):
            return out
        meta = _meta(conn)
        out.complete_since_ms = _int_or_none(meta.get(il.META_COMPLETE_SINCE))
        out.coverage = _int_or_none(meta.get(il.META_COVERAGE))
        out.rows = [
            input_from_row(r)
            for r in conn.execute(
                "SELECT * FROM inputs WHERE session_id = ? ORDER BY created_at, rowid",
                (session_id,),
            )
        ]
        out.unbound = [
            input_from_row(r)
            for r in conn.execute(
                "SELECT * FROM inputs WHERE session_id = '' "
                "AND state IN ('pending', 'unmatched') "
                "AND created_at >= ? AND created_at < ? ORDER BY created_at, rowid",
                (lookback_ms, until_ms),
            )
        ]
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------- ccc sent


def _none(value: str) -> str | None:
    return value or None


def input_entry(row: Input, state: str, in_memory: bool) -> dict[str, Any]:
    """One ``inputs[]`` item of the ``ccc sent`` data (``""`` / ``-1`` read as null)."""
    return {
        "input_id": row.input_id,
        "kind": row.kind,
        "origin": row.origin,
        "created_at": iso_ms(row.created_at),
        "created_at_ms": row.created_at,
        "norm": row.norm,
        "content_sha": row.content_sha,
        "alt_sha": _none(row.alt_sha),
        "state": state,
        "stored_state": row.state,
        "matched_in_memory": in_memory,
        "transcript_path": _none(row.transcript_path),
        "transcript_inode": row.transcript_inode,
        "anchor_offset": row.anchor_offset,
        "line_offset": row.line_offset if row.line_offset >= 0 else None,
        "line_uuid": _none(row.line_uuid),
        "line_kind": _none(row.line_kind),
        "stray_len": row.stray_len,
        "delivery_id": _none(row.delivery_id),
        "reason": _none(row.reason),
    }


def _listed(row: Input, since_ms: int, until_ms: int) -> bool:
    """A row of the lookback that may concern the window: every row written in the window;
    one written before it unless it failed (nothing typed) or matched a line before it."""
    if row.created_at >= until_ms:
        return False
    if row.created_at >= since_ms:
        return True
    if row.state == "failed":
        return False
    return not (row.state == "matched" and 0 < row.matched_at < since_ms)


def _reasons(  # pylint: disable=too-many-arguments
    ledger: _Ledger,
    since_ms: int,
    pending: bool,
    unbound: bool,
    gaps: bool,
) -> list[str]:
    reasons: list[str] = []
    if ledger.complete_since_ms is None:
        reasons.append("not_armed")
    else:
        if ledger.coverage is None or ledger.coverage < il.COVERAGE:
            reasons.append("coverage_stale")
        if since_ms < ledger.complete_since_ms:
            reasons.append("before_watermark")
    if pending:
        reasons.append("pending_match")
    if unbound:
        reasons.append("unbound_rows")
    if gaps:
        reasons.append("ledger_write_failed")
    return reasons


def run_sent(deps: BridgeDeps, session: str, since_ms: int, until_ms: int) -> dict[str, Any]:
    """The ``ccc sent`` data for *session* over ``[since_ms, until_ms)``. Read-only.

    Raises :class:`BridgeError` (``invalid_session`` / ``invalid_window``, exit 2).
    """
    sid = parse_session(session)
    if until_ms <= since_ms:
        raise usage("invalid_window", "-u/--until must be after -a/--after")
    now = int(deps.now_ms())
    lookback = max(0, since_ms - il.MATCH_WINDOW_MS)
    ledger = _read(deps.db_path(), sid, lookback, until_ms)
    planned = {
        row.input_id: (state, match) for row, state, match in il.plan_session(ledger.rows, now)
    }
    items: list[dict[str, Any]] = []
    pending = False
    for row in ledger.rows:
        if row.created_at < lookback or not _listed(row, since_ms, until_ms):
            continue
        shown = row
        state = row.state
        moved = planned.get(row.input_id)
        if moved is not None:
            state, match = moved
            if match is not None:
                shown = dataclasses.replace(
                    row,
                    line_offset=match.line_offset,
                    line_uuid=match.line_uuid,
                    line_kind=match.line_kind,
                    stray_len=match.stray_len,
                )
            else:
                shown = dataclasses.replace(row, reason="match_window")
        if state == "pending":
            pending = True
        items.append(input_entry(shown, state, moved is not None))
    gaps = il.read_gaps(deps.gap_path(), session_id=sid, since_ms=lookback, until_ms=until_ms)
    reasons = _reasons(ledger, since_ms, pending, bool(ledger.unbound), bool(gaps))
    return {
        "session_id": sid,
        "since": iso_ms(since_ms),
        "since_ms": since_ms,
        "until": iso_ms(until_ms),
        "until_ms": until_ms,
        "as_of": iso_ms(now),
        "normalisation": il.NORM,
        "coverage": ledger.coverage,
        "complete_since": iso(ledger.complete_since_ms) if ledger.complete_since_ms else None,
        "complete_since_ms": ledger.complete_since_ms,
        "complete": not reasons,
        "incomplete_reasons": reasons,
        "inputs": items,
    }


def sent_lines(data: dict[str, Any]) -> list[str]:
    """The text form of ``ccc sent``: a header with the watermark, then one line per row
    ``HH:MM:SS  kind  state  sha[:12]  uuid[:8]`` (local time)."""
    mark = data["complete_since"] or "not armed"
    verdict = (
        "complete"
        if data["complete"]
        else "incomplete (" + ", ".join(data["incomplete_reasons"]) + ")"
    )
    lines = [
        f"session {data['session_id']}  {data['since']} .. {data['until']}  "
        f"watermark {mark}  {verdict}"
    ]
    for item in data["inputs"]:
        at = datetime.fromtimestamp(item["created_at_ms"] / 1000).strftime("%H:%M:%S")
        lines.append(
            f"{at}  {item['kind']:<15} {item['state']:<9} {item['content_sha'][:12]:<12}  "
            f"{(item['line_uuid'] or '-')[:8]}"
        )
    if not data["inputs"]:
        lines.append("(no ledger rows)")
    return lines


# --------------------------------------------------------------------------- ccc input-ledger


def ledger_status(deps: BridgeDeps) -> dict[str, Any]:
    """``ccc input-ledger -S``: watermark, coverage, row counts by state, gap entries.

    Read-only (``mode=ro``); a missing db or ledger tables reads as unarmed and empty.
    """
    counts = dict.fromkeys(STATES, 0)
    since: int | None = None
    coverage: int | None = None
    db = deps.db_path()
    if db.is_file():
        conn = connect_ro(db)
        try:
            if _has_tables(conn):
                meta = _meta(conn)
                since = _int_or_none(meta.get(il.META_COMPLETE_SINCE))
                coverage = _int_or_none(meta.get(il.META_COVERAGE))
                for row in conn.execute("SELECT state, COUNT(*) FROM inputs GROUP BY state"):
                    counts[str(row[0])] = int(row[1])
        finally:
            conn.close()
    gaps = il.read_gaps(deps.gap_path())
    return {
        "armed": since is not None,
        "complete_since": iso(since) if since else None,
        "complete_since_ms": since,
        "coverage": coverage,
        "coverage_build": il.COVERAGE,
        "coverage_stale": since is not None and (coverage is None or coverage < il.COVERAGE),
        "normalisation": il.NORM,
        "counts": counts,
        "gaps": len(gaps),
        "last_gap_at": iso(max(g.at_ms for g in gaps)) if gaps else None,
    }


def parse_lstart(value: str) -> float | None:
    """``ps -o lstart=`` (``Tue Nov 17 10:00:00 2026``, local time) → epoch seconds."""
    token = " ".join(value.split())
    if not token:
        return None
    try:
        return time.mktime(time.strptime(token, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def _ccc_argv(command: str) -> list[str] | None:
    """The arguments after the ccc executable in *command*, or ``None`` when *command*
    does not run ccc code (``ccc``, ``claude-session-continue``, ``-m command_center``)."""
    tokens = command.split()
    # Only the executable position counts: argv[0], or the script/module after a python
    # interpreter and its options. A bare word "ccc" deeper in argv (a claude prompt that
    # mentions ccc, `tmux new-session -s ccc`) is not ccc code (tp#2170 B6).
    i = 0
    if tokens and os.path.basename(tokens[0]).lower().startswith("python"):
        i = 1
        while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-m":
            i += 1
        if i < len(tokens) and tokens[i] == "-m":
            i += 1
    if i >= len(tokens):
        return None
    base = os.path.basename(tokens[i])
    if base in _CCC_EXECUTABLES or base.startswith("command_center"):
        return tokens[i + 1 :] if base == "ccc" else [base, *tokens[i + 1 :]]
    return None


def allowlisted(args: list[str]) -> str | None:
    """The :data:`ARM_ALLOWLIST` reason when ``ccc <args>`` never types, else ``None``."""
    sub = next((a for a in args if not a.startswith("-")), "")
    for name, flags, reason in ARM_ALLOWLIST:
        if sub == name and any(f in args for f in flags):
            return reason
    return None


def _own_lineage(table: dict[int, Any]) -> set[int]:
    pids: set[int] = set()
    current = os.getpid()
    while current > 0 and current not in pids:
        pids.add(current)
        row = table.get(current)
        current = int(row.ppid) if row is not None else 0
    return pids


def stale_processes(deps: BridgeDeps) -> list[int]:
    """Pids of live processes running ccc code started before the newest ``*.py`` mtime
    of the installed package (an unreadable start time — gone, ps timed out — counts as
    stale: fail closed).

    Candidates come from ``ps_table`` (pid + command), start times from ``pid_start``
    (debate A2), raised to the pid's ``exec_stamp`` when newer (an in-place re-exec — the
    TUI's ``ccc restart-tui`` — keeps pid and lstart); this process, its parents and
    :data:`ARM_ALLOWLIST` entries are skipped.
    """
    stamp_s = deps.code_stamp() / 1e9
    table = deps.ps_table()
    mine = _own_lineage(table)
    stale: list[int] = []
    for pid, row in sorted(table.items()):
        if pid in mine:
            continue
        args = _ccc_argv(str(getattr(row, "command", "") or ""))
        if args is None or allowlisted(args) is not None:
            continue
        started = parse_lstart(deps.pid_start(pid))
        if started is None:
            stale.append(pid)
            continue
        execed = deps.exec_stamp(pid)
        if max(started, execed or 0.0) < stamp_s:
            stale.append(pid)
    return stale


def arm(deps: BridgeDeps, *, force: bool = False) -> dict[str, Any]:
    """``ccc input-ledger -A``: watermark = now, coverage = this build's.

    Refuses (``stale_processes``, exit 1, pids only) while :func:`stale_processes` finds
    one, unless *force*. Re-arming moves the watermark to now.
    """
    stale = stale_processes(deps)
    if stale and not force:
        raise BridgeError(
            "stale_processes",
            "processes running ccc code older than the installed tree are alive (pids "
            + ", ".join(str(p) for p in stale)
            + "); restart or stop them, or pass -F/--force",
            data={"stale_pids": stale},
        )
    now = int(deps.now_ms())
    with deps.store() as store:
        store.meta_set(il.META_COMPLETE_SINCE, str(now), now)
        store.meta_set(il.META_COVERAGE, str(il.COVERAGE), now)
    return {
        "armed": True,
        "complete_since": iso_ms(now),
        "complete_since_ms": now,
        "coverage": il.COVERAGE,
        "forced": bool(stale) and force,
        "stale_pids": stale,
    }


def disarm(deps: BridgeDeps) -> dict[str, Any]:
    """``ccc input-ledger -D``: delete both watermark keys (every listing then reads
    ``not_armed``)."""
    with deps.store() as store:
        removed = store.meta_delete(il.META_COMPLETE_SINCE)
        removed = store.meta_delete(il.META_COVERAGE) or removed
    return {"armed": False, "removed": removed}
