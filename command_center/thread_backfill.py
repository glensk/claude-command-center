#!/usr/bin/env python3
"""``ccc thread-backfill [-n|-w] [-j] [-o PATH] [-T TPDB] [-d DB] [-F FP]`` (tp#2372 F1a).

Fills the empty ``sessions.thread_id`` columns with each session's thread root, but only
where the lineage is unambiguous. Read :func:`audit` for the rules; in short:

1. **Raw edges first.** Every candidate parent edge is collected BEFORE any parent is
   chosen: a row's ``handed_off_from`` and every non-failed transfer (``prepared`` and
   ``opened`` included). :func:`thread._walk` picks one of several incoming transfers and
   lets a recorded id vouch for a cut walk; this audit does neither.
2. **Acceptance evidence.** An edge counts as a succession only when a transfer of the same
   pair was accepted (``accepted_at`` set, or state ``accepted`` / ``finalized``: the
   ``handed_off_from`` of a finalized transfer). ``continue-session start`` writes the
   target's ``handed_off_from`` long before any acceptance, so the column alone is no proof.
3. **Components.** Every session connected to another by an edge (either direction) is one
   component; a session without edges is a component of its own (a root). A component with
   any of these is **quarantined** whole (listed with its reasons and sessions, never
   written): ``cycle``, ``max_hops`` (a chain longer than :data:`thread.MAX_HOPS`),
   ``missing_ancestor`` (an edge's source has no row), ``multi_parent`` (a target with two
   or more candidate parents), ``fork`` (a source with two or more successors),
   ``conflict`` (a recorded ``thread_id`` that is not the structural root, which is computed
   without looking at any recorded id), ``unaccepted`` (an edge without acceptance
   evidence), ``walk_mismatch`` (no other reason, yet today's batched walk reaches another
   root: a safety net).
4. **Writes.** A clean component's root id goes onto every member row whose ``thread_id``
   is empty (``UPDATE … WHERE COALESCE(thread_id, '') = ''``, the ``Store.record_thread``
   rule): a root gets its own id, a recorded id is never overwritten, a re-run writes
   nothing.

``-T PATH`` reads a tp DB (``mode=ro``) and lists, per quarantined component, every open
obligation (``holdings`` row with ``ended IS NULL``: claims, drives, plan-only and blocked
plan rows) its sessions hold; ``tp_claims`` holds the claim rows.

The default is a dry run over a read-only connection. ``-w/--write`` needs ``-F/--fingerprint``
(the dry run's ``fingerprint``): it re-audits inside one ``BEGIN IMMEDIATE`` transaction and
refuses (exit 2, nothing written) when the audited rows changed since the dry run. ``-o PATH``
writes the full report (with every planned or written ``(session_id, thread_id)``, the narrow
undo list) as 0600 JSON.
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
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, thread
from .bridge_aims import connect_ro

__all__ = [
    "BUSY_TIMEOUT_S",
    "REASONS",
    "Audit",
    "Component",
    "add_parser",
    "apply",
    "audit",
    "cmd_thread_backfill",
    "obligations",
    "report",
]

#: How long ``--write`` waits for a concurrent writer's lock (seconds).
BUSY_TIMEOUT_S = 5.0
#: The quarantine reasons, most structural first (``reason`` is the first that applies).
REASONS = (
    "cycle",
    "max_hops",
    "missing_ancestor",
    "multi_parent",
    "fork",
    "conflict",
    "unaccepted",
    "walk_mismatch",
)
_ACCEPTED_STATES = ("accepted", "finalized")
#: Exit code of a refused write (fingerprint missing or changed).
EXIT_REFUSED = 2


@dataclass
class Component:
    """Every session connected by a raw edge to one root (or a lone session)."""

    root: str
    members: list[str]
    reasons: list[str] = field(default_factory=list)
    #: ``(session_id, thread_id)`` rows a clean component writes (empty column only).
    writes: list[tuple[str, str]] = field(default_factory=list)
    #: Member rows whose recorded id already equals the root.
    already: int = 0
    #: Members without a ``sessions`` row (a missing ancestor or an absent target).
    missing: list[str] = field(default_factory=list)

    @property
    def quarantined(self) -> bool:
        """True when any check failed: the component is listed, never written."""
        return bool(self.reasons)


@dataclass
class Audit:
    """The whole store's audit: its components and the raw-edge figures."""

    components: list[Component]
    fingerprint: str
    #: Store-wide counts: ``sessions``, ``edges``, ``unaccepted_edges``, ``fork_sources``,
    #: ``multi_parent_targets``, ``walk_conflicts`` (a recorded id that is not today's walk
    #: root), ``walk_threads``, ``walk_inexact``.
    figures: dict[str, int]

    @property
    def writes(self) -> list[tuple[str, str]]:
        """Every planned ``(session_id, thread_id)`` of the clean components."""
        return [w for c in self.components for w in c.writes]


class _Graph:
    """The raw edges: candidate parents per target, successors per source, acceptance."""

    def __init__(self) -> None:
        self.parents: dict[str, set[str]] = {}
        self.children: dict[str, set[str]] = {}
        self.accepted: dict[tuple[str, str], bool] = {}

    def add(self, source: str, target: str, accepted: bool) -> None:
        """Record *source* → *target* (acceptance evidence sticks once seen)."""
        self.parents.setdefault(target, set()).add(source)
        self.children.setdefault(source, set()).add(target)
        key = (source, target)
        self.accepted[key] = self.accepted.get(key, False) or accepted


def _rows(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """``session_id → (handed_off_from, thread_id)`` of every ``sessions`` row."""
    out: dict[str, tuple[str, str]] = {}
    for r in conn.execute("SELECT session_id, handed_off_from, thread_id FROM sessions"):
        out[str(r[0])] = (str(r[1] or "").strip(), str(r[2] or "").strip())
    return out


def _transfers(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, int]]:
    """``(transfer_id, source, target, state, accepted_at)`` of every transfer, sorted."""
    rows = conn.execute(
        "SELECT transfer_id, source_session, target_session, state, accepted_at FROM transfers "
        "ORDER BY transfer_id"
    ).fetchall()
    return [
        (str(r[0]), str(r[1] or "").strip(), str(r[2] or "").strip(), str(r[3]), int(r[4] or 0))
        for r in rows
    ]


def _fingerprint(
    rows: dict[str, tuple[str, str]], transfers: list[tuple[str, str, str, str, int]]
) -> str:
    """sha256 over every audited input (rows and transfers), order-independent."""
    digest = hashlib.sha256()
    for sid in sorted(rows):
        digest.update(json.dumps([sid, *rows[sid]]).encode())
    digest.update(b"\x00transfers\x00")
    for t in transfers:
        digest.update(json.dumps(list(t)).encode())
    return digest.hexdigest()


def _graph(
    rows: dict[str, tuple[str, str]], transfers: list[tuple[str, str, str, str, int]]
) -> _Graph:
    accepted_pairs = {
        (src, tgt)
        for _tid, src, tgt, state, at in transfers
        if src and tgt and (at > 0 or state in _ACCEPTED_STATES)
    }
    graph = _Graph()
    for _tid, src, tgt, state, _at in transfers:
        if state != "failed" and src and tgt:
            graph.add(src, tgt, (src, tgt) in accepted_pairs)
    for sid, (source, _tid) in rows.items():
        if source:
            graph.add(source, sid, (source, sid) in accepted_pairs)
    return graph


def _components(nodes: set[str], graph: _Graph) -> list[list[str]]:
    """The weakly connected components of *nodes* under *graph* (each sorted)."""
    parent = {n: n for n in nodes}

    def find(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for target, sources in graph.parents.items():
        for source in sources:
            a, b = find(source), find(target)
            if a != b:
                parent[max(a, b)] = min(a, b)
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return [sorted(g) for g in groups.values()]


def _topo(members: list[str], graph: _Graph) -> list[str] | None:
    """*members* parents first (Kahn), or None when they hold a cycle."""
    indeg = {n: len(graph.parents.get(n, ())) for n in members}
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop()
        order.append(n)
        for child in sorted(graph.children.get(n, ())):
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    return order if len(order) == len(members) else None


def _edge_reasons(members: list[str], graph: _Graph, missing: list[str]) -> set[str]:
    """The raw-edge reasons: a missing ancestor, two parents, two successors, no acceptance."""
    reasons: set[str] = set()
    if any(graph.children.get(n) for n in missing):
        reasons.add("missing_ancestor")
    if any(len(graph.parents.get(n, ())) >= 2 for n in members):
        reasons.add("multi_parent")
    if any(len(graph.children.get(n, ())) >= 2 for n in members):
        reasons.add("fork")
    if any(not graph.accepted[(p, n)] for n in members for p in graph.parents.get(n, ())):
        reasons.add("unaccepted")
    return reasons


def _shape(
    members: list[str], graph: _Graph, rows: dict[str, tuple[str, str]]
) -> tuple[str, set[str]]:
    """The structural root and the shape reasons: a cycle, too many hops, a conflict.

    The root is computed from the edges alone; a recorded ``thread_id`` is only compared
    against it afterwards (a conflict when it is not the member's one structural root).
    """
    order = _topo(members, graph)
    if order is None:
        return members[0], {"cycle"}
    reasons: set[str] = set()
    roots_of: dict[str, set[str]] = {}
    depth: dict[str, int] = {}
    for n in order:
        ps = graph.parents.get(n, set())
        roots_of[n] = set().union(*(roots_of[p] for p in ps)) if ps else {n}
        depth[n] = max((depth[p] + 1 for p in ps), default=0)
    if max(depth.values(), default=0) > thread.MAX_HOPS:
        reasons.add("max_hops")
    if any(n in rows and rows[n][1] and roots_of[n] != {rows[n][1]} for n in members):
        reasons.add("conflict")
    return min(n for n in members if not graph.parents.get(n)), reasons


def _check(
    members: list[str],
    graph: _Graph,
    rows: dict[str, tuple[str, str]],
    walk_roots: dict[str, str],
) -> Component:
    """One component's structural root, reasons and planned writes."""
    missing = [n for n in members if n not in rows]
    root, reasons = _shape(members, graph, rows)
    reasons |= _edge_reasons(members, graph, missing)
    # The safety net only: every other reason already explains a walk that differs.
    if not reasons and any(walk_roots.get(n, root) != root for n in members if n in rows):
        reasons.add("walk_mismatch")
    comp = Component(root, members, [r for r in REASONS if r in reasons], missing=missing)
    if not comp.quarantined:
        for n in members:
            if n in rows and rows[n][1]:
                comp.already += 1
            elif n in rows:
                comp.writes.append((n, root))
    return comp


def audit(conn: sqlite3.Connection) -> Audit:
    """The read-only audit of every ``sessions`` row (see the module docstring)."""
    rows = _rows(conn)
    transfers = _transfers(conn)
    graph = _graph(rows, transfers)
    nodes = set(rows) | set(graph.parents) | set(graph.children)
    paths, _read = thread._walk(conn, list(rows))  # pylint: disable=protected-access
    walk_roots = {sid: p.path[0] for sid, p in paths.items() if p.path}
    components = sorted(
        (_check(m, graph, rows, walk_roots) for m in _components(nodes, graph)),
        key=lambda c: c.root,
    )
    return Audit(
        components=[c for c in components if any(m in rows for m in c.members)],
        fingerprint=_fingerprint(rows, transfers),
        figures={
            "sessions": len(rows),
            "edges": len(graph.accepted),
            "unaccepted_edges": sum(1 for ok in graph.accepted.values() if not ok),
            "fork_sources": sum(1 for c in graph.children.values() if len(c) >= 2),
            "multi_parent_targets": sum(1 for p in graph.parents.values() if len(p) >= 2),
            "walk_conflicts": sum(
                1 for sid, (_s, tid) in rows.items() if tid and walk_roots.get(sid, sid) != tid
            ),
            "walk_threads": len({p.thread_id for p in paths.values()}),
            "walk_inexact": sum(1 for p in paths.values() if not p.exact),
        },
    )


def obligations(tp_db: Path, sessions: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Every open ``holdings`` row of *sessions* in the tp DB at *tp_db* (``mode=ro``).

    ``session → [{tp_id, kinds, item_status}]``; ``kinds`` names what the row holds:
    ``claim``, ``drive``, ``plan`` (a plan row, plan-only when no ``claim``).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not sessions:
        return out
    conn = connect_ro(tp_db)
    try:
        ids = sorted(sessions)
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            marks = ",".join("?" * len(batch))
            for r in conn.execute(
                "SELECT h.session, h.tp_id, h.claim_at, h.drive_at, h.plan_row, h.plan_path, "
                "i.status FROM holdings h LEFT JOIN items i ON i.tp_id = h.tp_id "
                f"WHERE h.ended IS NULL AND h.session IN ({marks}) ORDER BY h.tp_id, h.id",
                batch,
            ):
                kinds = [
                    kind
                    for kind, value in (
                        ("claim", r["claim_at"]),
                        ("drive", r["drive_at"]),
                        ("plan", r["plan_row"] or r["plan_path"]),
                    )
                    if value
                ]
                out.setdefault(str(r["session"]), []).append(
                    {"tp_id": int(r["tp_id"]), "kinds": kinds, "item_status": r["status"]}
                )
    finally:
        conn.close()
    return out


def report(
    result: Audit,
    *,
    held: dict[str, list[dict[str, Any]]] | None,
    write: bool,
    written: int | None = None,
) -> dict[str, Any]:
    """The JSON report: ``{written, already, quarantined: [...]}`` plus the audit figures."""
    quarantined = []
    for comp in result.components:
        if not comp.quarantined:
            continue
        entry: dict[str, Any] = {
            "root": comp.root,
            "reason": comp.reasons[0],
            "reasons": comp.reasons,
            "sessions": comp.members,
            "missing": comp.missing,
            "tp_claims": None,
            "obligations": None,
        }
        if held is not None:
            rows = [{"session": s, **h} for s in comp.members for h in held.get(s, [])]
            entry["obligations"] = rows
            entry["tp_claims"] = [
                {"session": h["session"], "tp_id": h["tp_id"]}
                for h in rows
                if "claim" in h["kinds"]
            ]
        quarantined.append(entry)
    by_reason = Counter(r for q in quarantined for r in q["reasons"])
    holders = {o["session"] for q in quarantined if q["obligations"] for o in q["obligations"]}
    claimers = {c["session"] for q in quarantined if q["tp_claims"] for c in q["tp_claims"]}
    return {
        "dry_run": not write,
        "written": len(result.writes) if written is None else written,
        "already": sum(c.already for c in result.components),
        "quarantined": quarantined,
        "summary": {
            "sessions": result.figures["sessions"],
            "threads": len(result.components),
            "threads_multi": sum(1 for c in result.components if len(c.members) > 1),
            "quarantined_components": len(quarantined),
            "quarantined_sessions": sum(len(q["sessions"]) for q in quarantined),
            "quarantined_by_reason": dict(sorted(by_reason.items())),
            "quarantined_holding_sessions": len(holders) if held is not None else None,
            "quarantined_claiming_sessions": len(claimers) if held is not None else None,
            **{k: v for k, v in result.figures.items() if k != "sessions"},
        },
        "fingerprint": result.fingerprint,
    }


class Refused(Exception):
    """``--write`` refused: no fingerprint, or the audited rows changed since the dry run."""


def apply(
    db_path: Path, fingerprint: str, *, busy_timeout: float = BUSY_TIMEOUT_S
) -> tuple[Audit, int]:
    """Re-audit *db_path* under ``BEGIN IMMEDIATE`` and write the clean components.

    Refuses (:class:`Refused`, nothing written) when *fingerprint* is empty or differs from
    the re-audit's. Returns the audit and the number of rows written.
    """
    if not fingerprint:
        raise Refused("--write needs -F/--fingerprint from a dry run")
    conn = sqlite3.connect(db_path, timeout=busy_timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = audit(conn)
            if result.fingerprint != fingerprint:
                raise Refused(
                    f"the audited rows changed since the dry run "
                    f"(fingerprint {result.fingerprint[:12]} != {fingerprint[:12]})"
                )
            n = 0
            for sid, tid in result.writes:
                n += conn.execute(
                    "UPDATE sessions SET thread_id = ? WHERE session_id = ? "
                    "AND COALESCE(thread_id, '') = ''",
                    (tid, sid),
                ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return result, n


def _write_out(path: Path, payload: dict[str, Any], writes: list[tuple[str, str]]) -> None:
    """The full report plus every ``(session_id, thread_id)`` as 0600 JSON at *path*."""
    full = {**payload, "writes": [{"session_id": s, "thread_id": t} for s, t in writes]}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(full, indent=1) + "\n")
    os.chmod(path, 0o600)


def _print_human(payload: dict[str, Any]) -> None:
    s = payload["summary"]
    verb = "would write" if payload["dry_run"] else "wrote"
    mode = "dry run: nothing written; -w/--write writes" if payload["dry_run"] else "written"
    print(f"thread-backfill ({mode})")
    print(f"  sessions {s['sessions']}  threads {s['threads']} ({s['threads_multi']} multi)")
    print(f"  {verb} {payload['written']}  already {payload['already']}")
    print(
        f"  quarantined {s['quarantined_components']} components / "
        f"{s['quarantined_sessions']} sessions"
    )
    for reason, n in s["quarantined_by_reason"].items():
        print(f"    {reason:<16} {n:>5}")
    for q in payload["quarantined"]:
        claims = q["tp_claims"]
        held = "" if claims is None else f"  claims {len(claims)}"
        print(f"  ⚠ {q['root']}  {','.join(q['reasons'])}  {len(q['sessions'])} sessions{held}")
    print(f"  fingerprint {payload['fingerprint']}")


def cmd_thread_backfill(args: argparse.Namespace) -> int:
    """``ccc thread-backfill``."""
    db = Path(args.db).expanduser() if getattr(args, "db", None) else config.db_path()
    if not db.is_file():
        print(f"❌ no state.db at {db}", file=sys.stderr)
        return 1
    write = bool(getattr(args, "write", False))
    written: int | None = None
    try:
        if write:
            result, written = apply(db, str(getattr(args, "fingerprint", "") or ""))
        else:
            conn = connect_ro(db)
            try:
                result = audit(conn)
            finally:
                conn.close()
        tp_db = getattr(args, "tp_db", None)
        held = None
        if tp_db:
            members = {m for c in result.components if c.quarantined for m in c.members}
            held = obligations(Path(tp_db).expanduser(), members)
        payload = report(result, held=held, write=write, written=written)
        if getattr(args, "out", None):
            _write_out(Path(args.out).expanduser(), payload, result.writes)
    except Refused as err:
        print(f"❌ thread-backfill refused, nothing written: {err}", file=sys.stderr)
        return EXIT_REFUSED
    except (sqlite3.Error, OSError) as err:
        print(f"❌ thread-backfill failed, nothing written: {err}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=1))
    else:
        _print_human(payload)
    return 0


def add_parser(sub: Any) -> None:
    """Register ``thread-backfill`` (internal-style, no TUI key) on the ccc subparsers *sub*."""
    p = sub.add_parser(
        "thread-backfill",
        help="one-off backfill: record each session's thread root (dry run by default)",
        description=(
            "Audits every sessions row's raw lineage (handed_off_from and every non-failed\n"
            "transfer) and fills an empty thread_id with its thread root, only for a clean\n"
            "component: one parent per session, one successor per source, every edge\n"
            "accepted, no cycle, <= 50 hops, every ancestor row present, no recorded id that\n"
            "disagrees with the structural root. Any other component is quarantined: listed\n"
            "with its reasons and sessions, never written. A recorded id is never\n"
            "overwritten; a re-run writes nothing. Dry run by default (read-only)."
        ),
        epilog=(
            "examples:\n"
            "  ccc thread-backfill -j                    # dry run, JSON report\n"
            "  ccc thread-backfill -j -T TPDB -o out.json  # + open tp obligations, full file\n"
            "  ccc thread-backfill -w -F <fingerprint>   # write, refused on any change"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "-n", "--dry-run", action="store_true", help="read-only audit, write nothing (default)"
    )
    mode.add_argument(
        "-w", "--write", action="store_true", help="write the clean components (needs -F)"
    )
    p.add_argument("-j", "--json", action="store_true", help="print the JSON report")
    p.add_argument(
        "-o", "--out", metavar="PATH", help="write the full report (with every write) as JSON"
    )
    p.add_argument(
        "-T",
        "--tp-db",
        metavar="PATH",
        help="a tp DB to read (mode=ro): open obligations of quarantined sessions",
    )
    p.add_argument("-d", "--db", metavar="PATH", help="the state.db to audit (default: ccc's)")
    p.add_argument(
        "-F",
        "--fingerprint",
        metavar="HEX",
        help="the dry run's fingerprint; -w refuses when the audited rows changed",
    )
    p.set_defaults(func=cmd_thread_backfill)
