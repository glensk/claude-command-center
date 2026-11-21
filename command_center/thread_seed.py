#!/usr/bin/env python3
"""``ccc thread-seed [-S PATH] [-n] [-J]`` (tp#2372 FB9): import state.db's threads.

Copies every session's thread and generation from ``state.db`` into the shared thread
tables (:mod:`command_center.shared_store`): ``ccc_threads`` and ``ccc_sessions``.

- **Lineage** comes from F1's audit (:func:`thread_backfill.audit`): a clean component is
  one linear chain from its root; its thread id is the root's session id and each member's
  generation is its position along the chain (root = 0), in ``handed_off_at`` order. A
  chain whose ``handed_off_at`` stamps run backwards is a conflict (``handed_off_order``).
- **Quarantined** components (F1: cycle, fork, multi-parent, unaccepted edge, …) are
  skipped and listed with their reasons and sessions. A lone draft (a future job that never
  started) is no session yet and is skipped (counted).
- **Reconcile, never overwrite.** An existing shared row that matches is ``already``; a
  missing one is inserted; one that disagrees with the lineage (another thread or
  generation, the generation slot taken by another session, another root, a pending
  succession on a thread whose generation would move) is a **conflict**: listed, and
  nothing of that component is written. A thread whose shared generation is behind the
  chain is advanced to it (compare-and-set).
- Seeded rows carry ``started_at`` (the row's ``created_at``), ``cwd`` and, for every
  member that handed off, ``ended_at`` = its successor's ``handed_off_at`` with
  ``end_reason = 'handed_off'``; the current member's lifecycle stays the hooks' business.

Idempotent: the plan is read first and a run with nothing to insert opens no write
transaction, so a second run writes nothing. The write re-plans inside one guarded
``BEGIN IMMEDIATE`` (component, maintenance, epoch). ``-S/--store`` names the shared file
(the initial import runs before ``shared_store_path`` is set); default: the configured key.
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
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, thread_backfill
from .bridge_aims import connect_ro

__all__ = ["Member", "SeedPlan", "add_parser", "cmd_thread_seed", "plan_seed", "seed"]


@dataclass(frozen=True)
class Member:
    """One expected ``ccc_sessions`` row."""

    session_id: str
    thread_id: str
    generation: int
    started_at: int
    cwd: str
    ended_at: int | None


@dataclass
class _Chain:
    root: str
    members: list[Member]
    started_at: int


@dataclass
class SeedPlan:  # pylint: disable=too-many-instance-attributes
    """What a seed would write, what is already there and every conflict."""

    threads: list[tuple[str, int, int]] = field(default_factory=list)  # (root, gen, at)
    advance: list[tuple[str, int, int]] = field(default_factory=list)  # (thread, old, new)
    sessions: list[Member] = field(default_factory=list)
    already: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    drafts: int = 0

    @property
    def empty(self) -> bool:
        """True when nothing would be written."""
        return not (self.threads or self.advance or self.sessions)


def _session_facts(conn: sqlite3.Connection) -> dict[str, tuple[int, int, str, int]]:
    """``session_id → (draft, created_at, cwd, handed_off_at)``."""
    return {
        str(r[0]): (int(r[1] or 0), int(r[2] or 0), str(r[3] or ""), int(r[4] or 0))
        for r in conn.execute(
            "SELECT session_id, draft, created_at, cwd, handed_off_at FROM sessions"
        )
    }


def _chain_order(members: list[str], graph: Any) -> list[str]:
    """A clean component's members root first (one parent and one successor each)."""
    wanted = set(members)
    node = next(m for m in sorted(members) if not graph.parents.get(m))
    order = [node]
    while True:
        nxt = sorted(c for c in graph.children.get(node, ()) if c in wanted)
        if not nxt or nxt[0] in order:
            return order
        node = nxt[0]
        order.append(node)


def _chains(state: sqlite3.Connection, out: SeedPlan) -> list[_Chain]:
    """Every clean component of state.db as a chain; quarantined ones go to *out*."""
    audit = thread_backfill.audit(state)
    facts = _session_facts(state)
    # pylint: disable=protected-access  # F1's raw-edge graph, the one lineage source
    graph = thread_backfill._graph(thread_backfill._rows(state), thread_backfill._transfers(state))
    chains: list[_Chain] = []
    for comp in audit.components:
        if comp.quarantined:
            out.quarantined.append(
                {"root": comp.root, "reasons": comp.reasons, "sessions": comp.members}
            )
            continue
        order = [s for s in _chain_order(comp.members, graph) if s in facts]
        if len(order) == 1 and facts[order[0]][0]:
            out.drafts += 1
            continue
        stamps = [facts[s][3] for s in order[1:] if facts[s][3]]
        if stamps != sorted(stamps):
            out.conflicts.append({"root": comp.root, "session": None, "reason": "handed_off_order"})
            continue
        members = []
        for gen, sid in enumerate(order):
            nxt = order[gen + 1] if gen + 1 < len(order) else ""
            ended = facts[nxt][3] or None if nxt else None
            _draft, created, cwd, handed = facts[sid]
            started = created or handed
            members.append(Member(sid, comp.root, gen, started, cwd, ended))
        chains.append(_Chain(comp.root, members, members[0].started_at))
    return chains


def _reconcile(chain: _Chain, shared: sqlite3.Connection) -> tuple[list[dict[str, Any]], Any]:
    """One chain against the shared rows: its conflicts and its ``(thread, advance,
    sessions, already)`` writes."""
    conflicts: list[dict[str, Any]] = []
    last = chain.members[-1].generation
    row = shared.execute(
        "SELECT root_session, current_generation FROM ccc_threads WHERE thread_id = ?",
        (chain.root,),
    ).fetchone()
    new_thread = row is None
    advance = None
    if row is not None:
        if str(row[0]) != chain.root:
            conflicts.append({"reason": "thread_root", "shared_root": str(row[0])})
        elif int(row[1]) < last:
            pending = shared.execute(
                "SELECT op_id FROM ccc_thread_successions WHERE thread_id = ? "
                "AND state IN ('reserved', 'committing') LIMIT 1",
                (chain.root,),
            ).fetchone()
            if pending is not None:
                conflicts.append({"reason": "pending_succession", "op_id": str(pending[0])})
            advance = (chain.root, int(row[1]), last)
    inserts: list[Member] = []
    already = 0
    for m in chain.members:
        have = shared.execute(
            "SELECT thread_id, generation FROM ccc_sessions WHERE session_id = ?",
            (m.session_id,),
        ).fetchone()
        if have is not None:
            if (str(have[0]), int(have[1])) == (m.thread_id, m.generation):
                already += 1
            else:
                conflicts.append(
                    {
                        "reason": "lineage",
                        "session": m.session_id,
                        "state_db": [m.thread_id, m.generation],
                        "shared": [str(have[0]), int(have[1])],
                    }
                )
            continue
        slot = shared.execute(
            "SELECT session_id FROM ccc_sessions WHERE thread_id = ? AND generation = ?",
            (m.thread_id, m.generation),
        ).fetchone()
        if slot is not None:
            conflicts.append(
                {
                    "reason": "generation_taken",
                    "session": m.session_id,
                    "generation": m.generation,
                    "shared_session": str(slot[0]),
                }
            )
            continue
        inserts.append(m)
    return conflicts, (new_thread, advance, inserts, already)


def plan_seed(state: sqlite3.Connection, shared: sqlite3.Connection) -> SeedPlan:
    """The read-only seed plan of *state* (state.db) against *shared* (the shared file)."""
    out = SeedPlan()
    for chain in _chains(state, out):
        conflicts, (new_thread, advance, inserts, already) = _reconcile(chain, shared)
        out.already += already
        if conflicts:
            out.conflicts += [{"root": chain.root, **c} for c in conflicts]
            continue
        if new_thread:
            out.threads.append((chain.root, chain.members[-1].generation, chain.started_at))
        if advance is not None:
            out.advance.append(advance)
        out.sessions += inserts
    return out


def _apply(conn: sqlite3.Connection, plan: SeedPlan) -> tuple[int, int, int]:
    """Write *plan* on *conn* (inside the caller's transaction): rows per table."""
    threads = advanced = sessions = 0
    for root, gen, at in plan.threads:
        threads += conn.execute(
            "INSERT OR IGNORE INTO ccc_threads (thread_id, kind, root_session, "
            "current_generation, created_at) VALUES (?, 'ccc', ?, ?, ?)",
            (root, root, gen, at),
        ).rowcount
    for tid, old, new in plan.advance:
        advanced += conn.execute(
            "UPDATE ccc_threads SET current_generation = ? "
            "WHERE thread_id = ? AND current_generation = ?",
            (new, tid, old),
        ).rowcount
    for m in plan.sessions:
        sessions += conn.execute(
            "INSERT OR IGNORE INTO ccc_sessions (session_id, thread_id, generation, "
            "started_at, ended_at, end_reason, cwd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                m.session_id,
                m.thread_id,
                m.generation,
                m.started_at,
                m.ended_at,
                "handed_off" if m.ended_at else None,
                m.cwd or None,
            ),
        ).rowcount
    return threads, advanced, sessions


class SeedError(Exception):
    """The seed could not run (no store, refused, unavailable); nothing was written."""


def seed(
    state_db: Path, store_path: Path, *, dry_run: bool = False
) -> tuple[SeedPlan, tuple[int, int, int]]:
    """Plan (and unless *dry_run*, write) the seed; the plan and rows written per table."""
    # pylint: disable=import-outside-toplevel
    from . import shared_store
    from .store import Store

    if not store_path.is_file():
        raise SeedError(f"no shared store at {store_path} (ccc never creates it)")
    state = connect_ro(state_db)
    try:
        ro = connect_ro(store_path)
        try:
            plan = plan_seed(state, ro)
        finally:
            ro.close()
        if dry_run or plan.empty:
            return plan, (0, 0, 0)
        with Store() as store:
            shared = shared_store.SharedStore(store_path, private=store.conn)
            with shared:
                why = shared.refusal()
                if why is not None:
                    raise SeedError(f"refused, nothing written: {why}")
                done = shared.write("seed", lambda conn: _apply(conn, plan_seed(state, conn)))
    finally:
        state.close()
    if done is None:
        raise SeedError("refused or busy, nothing written (see shared-store.log)")
    return plan, done


def _payload(plan: SeedPlan, store: Path, dry_run: bool, done: tuple[int, int, int]) -> Any:
    written = (
        {
            "threads": len(plan.threads),
            "advanced": len(plan.advance),
            "sessions": len(plan.sessions),
        }
        if dry_run
        else {"threads": done[0], "advanced": done[1], "sessions": done[2]}
    )
    return {
        "store": str(store),
        "dry_run": dry_run,
        "written": written,
        "already": plan.already,
        "conflicts": plan.conflicts,
        "quarantined": plan.quarantined,
        "drafts_skipped": plan.drafts,
    }


def _print_human(doc: dict[str, Any]) -> None:
    w = doc["written"]
    verb = "would write" if doc["dry_run"] else "wrote"
    print(f"thread-seed  store {doc['store']}")
    print(
        f"  {verb} {w['sessions']} sessions, {w['threads']} threads, "
        f"{w['advanced']} generations advanced; already {doc['already']}"
    )
    print(f"  conflicts {len(doc['conflicts'])} (nothing of their thread written)")
    for c in doc["conflicts"]:
        extra = {k: v for k, v in c.items() if k not in ("root", "reason")}
        print(f"    ⚠ {c['reason']:<18} thread {c['root']}  {json.dumps(extra)}")
    print(f"  quarantined components (F1) skipped {len(doc['quarantined'])}")
    for q in doc["quarantined"]:
        print(f"    ⚠ {q['root']}  {','.join(q['reasons'])}  {len(q['sessions'])} sessions")
    print(f"  lone drafts skipped {doc['drafts_skipped']}")


def cmd_thread_seed(args: argparse.Namespace) -> int:
    """``ccc thread-seed``."""
    from . import shared_store  # pylint: disable=import-outside-toplevel

    raw = str(getattr(args, "store", "") or "").strip()
    store_path = Path(raw).expanduser() if raw else shared_store.configured_path()
    if store_path is None:
        print("❌ thread-seed: give -S/--store PATH or set shared_store_path", file=sys.stderr)
        return 2
    state_db = config.db_path()
    if not state_db.is_file():
        print(f"❌ thread-seed: no state.db at {state_db}", file=sys.stderr)
        return 1
    dry = bool(getattr(args, "dry_run", False))
    try:
        plan, done = seed(state_db, store_path, dry_run=dry)
    except SeedError as exc:
        print(f"❌ thread-seed: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"❌ thread-seed: {type(exc).__name__}: {exc}; nothing written", file=sys.stderr)
        return 1
    doc = _payload(plan, store_path, dry, done)
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=1))
    else:
        _print_human(doc)
    return 0


def add_parser(sub: Any) -> None:
    """Register ``thread-seed`` (internal-style, no TUI key) on the ccc subparsers *sub*."""
    p = sub.add_parser(
        "thread-seed",
        help="import every session's thread and generation from state.db into the shared store",
        description=(
            "Copies every session's thread and generation from state.db into the shared\n"
            "thread tables: generations by handed_off_at order along each clean chain.\n"
            "Existing shared rows are reconciled, never overwritten: missing ones are\n"
            "inserted, every disagreement is listed as a conflict (nothing of that thread\n"
            "is written), F1-quarantined components are skipped and listed. Re-runnable:\n"
            "a second run writes nothing."
        ),
        epilog=(
            "examples:\n"
            "  ccc thread-seed -S PATH -n -J   # dry run against the shared file, JSON\n"
            "  ccc thread-seed -S PATH         # seed before shared_store_path is set\n"
            "  ccc thread-seed                 # re-run against the configured store"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-S", "--store", metavar="PATH", help="the shared file (default: shared_store_path)"
    )
    p.add_argument("-n", "--dry-run", action="store_true", help="plan only, write nothing")
    p.add_argument("-J", "--json", action="store_true", help="print the JSON report")
    p.set_defaults(func=cmd_thread_seed)
