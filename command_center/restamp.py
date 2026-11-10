#!/usr/bin/env python3
"""``ccc restamp-aims [-a] [-r LOG]`` — stamp the unstamped AIM rows of relay successors (tp#1991).

A one-off backfill of ``aim_history.set_by`` for rows written before every writer stamped
(narrow, opt-in, reversible). Only rows with ``set_by IS NULL`` change:

1. a row holding one of ccc's own templates (``continue the work handed over by session
   <8hex>``, the removed ``handoff accepted by session <8hex>``) → ``tool``, any session;
2. a row of a relay successor (``handed_off_from`` set) whose text matches a row of its
   source (:func:`aimprov.stamp_row`, the rule :meth:`Store.stamp_of` uses) copies that
   row's stamp when it has one; passes repeat in chain order (root first) until nothing
   changes (at most :data:`MAX_PASSES`), so A→B→C stamps B from A, then C from B;
3. only then a successor row still NULL whose source row matched (but is NULL too) →
   ``relay``. A ``relay`` is never assigned before the fixpoint, so it never blocks a
   real stamp further down the chain.

Rows on roots and pre-history seeds keep NULL (their author is unknown), as does a
successor row matching nothing in its source. Idempotent: a second apply changes nothing.

Default is a dry run over a read-only (``mode=ro``) connection printing counts per
``old → new``. ``-a/--apply`` first copies ``state.db`` with the sqlite backup API to
``backups/state-pre-restamp-<YYYYmmdd-HHMMSS>.db`` next to it (0600), then re-plans and
writes in ONE ``BEGIN IMMEDIATE`` transaction (busy timeout; any failure rolls back) and
writes a 0600 JSON log ``(id, old, new)`` beside the backup. ``-r/--revert LOG`` (with
``-a``) puts those rows back, only where the value is still the one the log wrote.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, NamedTuple

from . import aimprov, config
from .bridge_aims import connect_ro
from .models import AIM_SET_BY_RELAY, AIM_SET_BY_TOOL

__all__ = [
    "BUSY_TIMEOUT_S",
    "MAX_PASSES",
    "Applied",
    "Change",
    "add_parser",
    "apply",
    "backup_dir",
    "cmd_restamp_aims",
    "plan",
    "revert",
]

#: The most fixpoint passes (a chain longer than this is not expected).
MAX_PASSES = 50
#: How long ``--apply`` waits for a concurrent writer's lock (seconds).
BUSY_TIMEOUT_S = 5.0
#: ccc's own AIM texts (never a person's words): the generic successor pointer and the
#: removed "handoff accepted" writer, each ending in the source's 8-hex id.
_TEMPLATE_RE = re.compile(
    r"(?:continue the work handed over by session|handoff accepted by session) [0-9a-f]{8}"
)


class Change(NamedTuple):
    """One ``aim_history`` row to restamp."""

    row_id: int
    session_id: str
    old: str | None
    new: str


class _Row(NamedTuple):
    row_id: int
    aim: str


def backup_dir(db_path: Path) -> Path:
    """Where ``--apply`` writes its backup and log: ``backups/`` beside *db_path*."""
    return db_path.parent / "backups"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _chain_order(sources: dict[str, str]) -> list[str]:
    """Successor ids, root-first: by hop depth (cycle-safe), then id."""

    def depth(sid: str) -> int:
        seen = {sid}
        hops = 0
        while (sid := sources.get(sid, "")) and sid not in seen:
            seen.add(sid)
            hops += 1
        return hops

    return sorted(sources, key=lambda s: (depth(s), s))


def _sources(conn: sqlite3.Connection) -> dict[str, str]:
    """``successor id → source id`` for every session with ``handed_off_from`` set."""
    if "handed_off_from" not in _columns(conn, "sessions"):
        return {}
    return {
        str(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT session_id, handed_off_from FROM sessions "
            "WHERE handed_off_from IS NOT NULL AND handed_off_from != ''"
        )
    }


class _Chains:
    """Every session's rows (oldest first), their stamps in memory, and the relay links."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.sources = _sources(conn)
        self.rows: dict[str, list[_Row]] = defaultdict(list)
        self.stamps: dict[int, str | None] = {}
        for r in conn.execute(
            "SELECT id, session_id, aim, set_by FROM aim_history "
            "ORDER BY session_id, created_at, id"
        ):
            self.rows[str(r[1])].append(_Row(int(r[0]), str(r[2] or "")))
            self.stamps[int(r[0])] = r[3]
        self.order = [
            s
            for s in _chain_order(self.sources)
            if self.rows.get(s) and self.rows.get(self.sources[s])
        ]

    def matched(self, succ: str, row: _Row) -> int | None:
        """The source row id *row*'s text came from (its stamp counts), or None."""
        src = self.rows[self.sources[succ]]
        stamps = [self.stamps[r.row_id] for r in src]
        index = aimprov.stamp_row([r.aim for r in src], stamps, row.aim)
        return src[index].row_id if index is not None else None

    def unstamped(self) -> list[tuple[str, _Row]]:
        """The successors' rows still NULL, in chain order."""
        return [
            (succ, row)
            for succ in self.order
            for row in self.rows[succ]
            if self.stamps[row.row_id] is None
        ]

    def copy_pass(self) -> bool:
        """One chain-order pass copying real (non-NULL) source stamps; whether any changed."""
        changed = False
        for succ, row in self.unstamped():
            source_row = self.matched(succ, row)
            if source_row is not None and self.stamps[source_row] is not None:
                self.stamps[row.row_id] = self.stamps[source_row]
                changed = True
        return changed


def plan(conn: sqlite3.Connection) -> list[Change]:
    """The rows to restamp (module rule), computed in memory; writes nothing."""
    if "set_by" not in _columns(conn, "aim_history"):
        return []
    chains = _Chains(conn)
    original = dict(chains.stamps)
    for session_rows in chains.rows.values():  # 1. ccc's own templates, any session
        for row in session_rows:
            if original[row.row_id] is None and _TEMPLATE_RE.fullmatch(row.aim.strip()):
                chains.stamps[row.row_id] = AIM_SET_BY_TOOL
    for _ in range(MAX_PASSES):  # 2. copy only real stamps, to a fixpoint
        if not chains.copy_pass():
            break
    for succ, row in chains.unstamped():  # 3. a matched source row still NULL → relay
        if chains.matched(succ, row) is not None:
            chains.stamps[row.row_id] = AIM_SET_BY_RELAY
    return [
        Change(row.row_id, sid, original[row.row_id], str(chains.stamps[row.row_id]))
        for sid, session_rows in sorted(chains.rows.items())
        for row in session_rows
        if chains.stamps[row.row_id] != original[row.row_id]
    ]


def _update(conn: sqlite3.Connection, change: Change) -> None:
    """Write one stamp (only over a NULL); a row that changed meanwhile is an error."""
    cur = conn.execute(
        "UPDATE aim_history SET set_by = ? WHERE id = ? AND set_by IS NULL",
        (change.new, change.row_id),
    )
    if cur.rowcount != 1:
        raise sqlite3.OperationalError(f"aim_history row {change.row_id} changed meanwhile")


def _private_file(path: Path) -> None:
    """Create *path* empty with mode 0600 (fails when it exists)."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def _fresh_stem(folder: Path) -> Path:
    """``folder/state-pre-restamp-<YYYYmmdd-HHMMSS>[-N]`` with neither ``.db`` nor ``.json``."""
    base = folder / f"state-pre-restamp-{time.strftime('%Y%m%d-%H%M%S')}"
    stem, n = base, 0
    while stem.with_suffix(".db").exists() or stem.with_suffix(".json").exists():
        n += 1
        stem = base.with_name(f"{base.name}-{n}")
    return stem


def _backup(db_path: Path, dest: Path) -> None:
    """Copy *db_path* to *dest* (0600) with the sqlite backup API."""
    _private_file(dest)
    src = connect_ro(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(dest, 0o600)


def _write_log(path: Path, db_path: Path, backup: Path, changes: list[Change]) -> None:
    _private_file(path)
    payload = {
        "db": str(db_path),
        "backup": str(backup),
        "changes": [
            {"id": c.row_id, "session_id": c.session_id, "old": c.old, "new": c.new}
            for c in changes
        ],
    }
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


class Applied(NamedTuple):
    """What ``--apply`` did: the changes written, its backup and log (None: nothing to do)."""

    changes: list[Change]
    backup: Path | None
    log: Path | None


def apply(
    db_path: Path, *, folder: Path | None = None, busy_timeout: float = BUSY_TIMEOUT_S
) -> Applied:
    """Back *db_path* up into *folder* (default :func:`backup_dir`), then restamp it.

    Nothing to change → no backup, no write. Else the backup comes first, then the plan is
    recomputed inside one ``BEGIN IMMEDIATE`` transaction, written, logged and committed;
    any failure rolls the whole transaction back and re-raises.
    """
    probe = connect_ro(db_path)
    try:
        if not plan(probe):
            return Applied([], None, None)
    finally:
        probe.close()
    folder = folder or backup_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = _fresh_stem(folder)
    backup, log = stem.with_suffix(".db"), stem.with_suffix(".json")
    _backup(db_path, backup)
    conn = sqlite3.connect(db_path, timeout=busy_timeout, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changes = plan(conn)
            for change in changes:
                _update(conn, change)
            _write_log(log, db_path, backup, changes)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return Applied(changes, backup, log)


def revert(db_path: Path, log: Path, *, write: bool, busy_timeout: float = BUSY_TIMEOUT_S) -> int:
    """Put *log*'s rows back to their old stamp where they still hold the logged new one.

    Returns how many rows were (``write``) or would be reverted.
    """
    changes = json.loads(log.read_text(encoding="utf-8"))["changes"]
    if not write:
        probe = connect_ro(db_path)
        try:
            return sum(
                int(
                    probe.execute(
                        "SELECT COUNT(*) FROM aim_history WHERE id = ? AND set_by = ?",
                        (int(c["id"]), c["new"]),
                    ).fetchone()[0]
                )
                for c in changes
            )
        finally:
            probe.close()
    conn = sqlite3.connect(db_path, timeout=busy_timeout, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = 0
            for c in changes:
                n += conn.execute(
                    "UPDATE aim_history SET set_by = ? WHERE id = ? AND set_by = ?",
                    (c["old"], int(c["id"]), c["new"]),
                ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return n


def _print_counts(head: str, changes: list[Change]) -> None:
    counts = Counter((c.old or "NULL", c.new) for c in changes)
    print(f"{head}: {len(changes)} row{'s' if len(changes) != 1 else ''}")
    for (old, new), n in sorted(counts.items()):
        print(f"  {old:<6} → {new:<6} {n:>6}")


def _run_revert(db: Path, log: Path, write: bool) -> int:
    logged = json.loads(log.read_text(encoding="utf-8")).get("db")
    if logged and Path(logged).resolve() != db.resolve():
        print(f"❌ the log belongs to {logged}, not {db}", file=sys.stderr)
        return 1
    n = revert(db, log, write=write)
    verb = "reverted" if write else "would revert (dry run: nothing written; -a writes)"
    print(f"restamp-aims {verb}: {n} row{'s' if n != 1 else ''}")
    return 0


def cmd_restamp_aims(args: argparse.Namespace) -> int:
    """``ccc restamp-aims [-a] [-r LOG]``."""
    db = config.db_path()
    if not db.is_file():
        print(f"❌ no state.db at {db}", file=sys.stderr)
        return 1
    write = bool(getattr(args, "apply", False))
    try:
        if getattr(args, "revert", None):
            return _run_revert(db, Path(args.revert), write)
        if not write:
            conn = connect_ro(db)
            try:
                changes = plan(conn)
            finally:
                conn.close()
            _print_counts("restamp-aims (dry run: nothing written; -a/--apply writes)", changes)
            return 0
        done = apply(db)
    except (sqlite3.Error, OSError, ValueError, KeyError) as err:
        print(f"❌ restamp-aims failed, nothing written: {err}", file=sys.stderr)
        return 1
    if done.backup is not None:
        print(f"backup: {done.backup}")
        print(f"log:    {done.log}  (undo: ccc restamp-aims -r <log> -a)")
    _print_counts("restamp-aims applied", done.changes)
    return 0


def add_parser(sub: Any) -> None:
    """Register ``restamp-aims`` (internal-style, no TUI key) on the ccc subparsers *sub*."""
    p = sub.add_parser(
        "restamp-aims",
        help="one-off backfill: stamp relay successors' unstamped AIM rows (dry run by default)",
        description=(
            "Stamps aim_history rows that have set_by NULL (nothing else changes):\n"
            "ccc's own template texts ('continue the work handed over by session <id>',\n"
            "'handoff accepted by session <id>') → tool; a relay successor's row whose\n"
            "text matches a row of its source → that row's stamp (passes repeat in chain\n"
            "order to a fixpoint), else, when the matching source row is unstamped too,\n"
            "relay. Roots, pre-history seeds and unmatched rows keep NULL. Idempotent.\n"
            "Without -a: a dry run (read-only) printing counts per old → new.\n"
            "-a: first a sqlite backup to backups/state-pre-restamp-<YYYYmmdd-HHMMSS>.db\n"
            "beside state.db (0600), then one transaction, plus a 0600 JSON log of the\n"
            "rows changed (for -r)."
        ),
        epilog=(
            "examples:\n"
            "  ccc restamp-aims                 # dry run: what would change\n"
            "  ccc restamp-aims -a              # back up, then write\n"
            "  ccc restamp-aims -r LOG          # dry run of an undo\n"
            "  ccc restamp-aims -r LOG -a       # undo an apply from its log"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-a", "--apply", action="store_true", help="write (after the backup); default: dry run"
    )
    p.add_argument(
        "-r",
        "--revert",
        metavar="LOG",
        help="undo an apply from its JSON log (rows still holding the logged stamp only)",
    )
    p.set_defaults(func=cmd_restamp_aims)
