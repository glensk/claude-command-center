#!/usr/bin/env python3
"""``ccc thread-reconcile [-r] [-J]`` (tp#2372 FB4; Design 1 restore, Design 4 lifecycle).

Settles what the shared thread store (:mod:`command_center.shared_store`) and ccc's
``state.db`` left unfinished, in this order:

1. **Epoch.** ccc pins the store's ``holder_authority_epoch`` at its first write and
   writes nothing while the store's epoch differs (a restore mints a new one). A stale pin
   is the restore ccc has not acknowledged yet.
2. **Quarantine** (only while the pin is stale). The restored store is authoritative at
   its recovery point, the newest ccc fact it holds (:meth:`SharedStore.high_water`).
   Every private ``state.db`` operation newer than that point whose outcome the store does
   not show is quarantined: continue-session accepts and aborts (``transfers``), relay
   accepts, hand-backs and reservations (``thread_joins``), and session starts and
   changed-SID ``/clear`` joins ccc recorded (``shared_incarnations``). None is applied:
   a still pending shared reservation of one is settled ``failed`` in the same transaction
   that re-pins the epoch, so no later recovery can publish it; the overseer re-runs each
   as a fresh operation or drops it. A headless admission leaves no private record and is
   not listed.
3. **Recovery by op id**: ``continue_session.recover_shared_ops`` (FB2), the one owner.
4. **Orphans**: a pending reservation older than :data:`ORPHAN_MS` with no private record
   at all (no transfer, no relay row, a ``headless`` kind) is settled ``failed``.

``-r/--report`` lists what each step would do and writes nothing anywhere. With
``shared_store_path`` unset nothing is opened (exit 0). Exit 1 when the store is
unavailable, under maintenance, of an unknown component version, or its epoch moved
between the report and the re-pin.
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
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .shared_store import SharedStore
    from .store import Store

__all__ = ["ORPHAN_MS", "Quarantined", "Report", "add_parser", "cmd_thread_reconcile", "plan"]

#: A pending reservation without any private record is an orphan after this long.
ORPHAN_MS = 3_600_000

#: A private state's shared counterpart: ``accepted``, ``settled`` or ``pending``.
_EXPECT = {
    "accepted": "accepted",
    "finalized": "accepted",
    "failed": "settled",
    "cancelled": "settled",
    "prepared": "pending",
    "opened": "pending",
    "reserved": "pending",
}


@dataclass(frozen=True)
class Quarantined:
    """One private operation newer than the recovery point that the store does not show."""

    op_id: str
    kind: str
    session: str
    private_state: str
    shared_state: str
    at: int


@dataclass
class Report:  # pylint: disable=too-many-instance-attributes
    """What ``thread-reconcile`` found (and, without ``-r``, did)."""

    store: str
    store_epoch: str | None
    pinned_epoch: str | None
    epoch_status: str
    recovery_point: int | None = None
    quarantined: list[Quarantined] = field(default_factory=list)
    recover: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    applied: bool = False
    settled_quarantined: int = 0
    refused: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The JSON report."""
        doc = asdict(self)
        doc["quarantined"] = [asdict(q) for q in self.quarantined]
        return doc


def _category(state: str | None) -> str:
    if state is None:
        return "missing"
    if state in ("reserved", "committing"):
        return "pending"
    return "accepted" if state == "accepted" else "settled"


def _private_ops(store: Store) -> list[tuple[str, str, str, str, int]]:
    """``(op_id, kind, session, private state, at)`` of every private operation."""
    out: list[tuple[str, str, str, str, int]] = []
    for r in store.conn.execute(
        "SELECT transfer_id, target_session, state, created_at, accepted_at, finalized_at, "
        "updated_at FROM transfers"
    ):
        state = str(r["state"])
        at = {"accepted": r["accepted_at"], "finalized": r["accepted_at"]}.get(state)
        at = int(at or r["updated_at"] or r["created_at"] or 0)
        kind = {"failed": "continue-abort", "accepted": "continue-accept"}.get(state, "")
        kind = kind or ("continue-accept" if state == "finalized" else "continue-reserve")
        out.append((str(r["transfer_id"]), kind, str(r["target_session"] or ""), state, at))
    for r in store.conn.execute(
        "SELECT op_id, target_session, join_kind, state, created_at, settled_at FROM thread_joins"
    ):
        state = str(r["state"])
        kind = {"accepted": "relay-accept", "cancelled": "relay-handback"}.get(
            state, "relay-reserve"
        )
        at = int(r["settled_at"] or r["created_at"] or 0)
        out.append((str(r["op_id"]), kind, str(r["target_session"]), state, at))
    return out


def _quarantine(store: Store, shared: SharedStore, point: int) -> list[Quarantined]:
    """Every private operation newer than *point* whose outcome the store does not show."""
    found: list[Quarantined] = []
    for op_id, kind, sid, state, at in _private_ops(store):
        if at <= point or state not in _EXPECT:
            continue
        op = shared.op(op_id)
        shared_state = None if op is None else op.state
        if _category(shared_state) != _EXPECT[state]:
            found.append(Quarantined(op_id, kind, sid, state, shared_state or "missing", at))
    for r in store.conn.execute(
        "SELECT session_id, MIN(at) FROM shared_incarnations WHERE incarnation = 0 "
        "GROUP BY session_id"
    ):
        sid, at = str(r[0]), int(r[1] or 0)
        if at > point and shared.session_row(sid) is None:
            found.append(Quarantined("", "session-start", sid, "started", "missing", at))
    return sorted(found, key=lambda q: (q.at, q.op_id, q.session))


def _orphans(store: Store, shared: SharedStore, now: int, skip: set[str]) -> list[str]:
    """Pending reservations older than :data:`ORPHAN_MS` without any private record."""
    out: list[str] = []
    for op in shared.pending_ops():
        if op.op_id in skip or op.created_at > now - ORPHAN_MS:
            continue
        if op.join_kind == "continue":
            known = store.get_transfer(op.op_id) is not None
        elif op.join_kind in ("park_start", "staff_accept"):
            known = store.thread_join_op(op.op_id) is not None
        else:
            known = op.join_kind == "clear"  # FB2 finishes a /clear join itself
        if not known:
            out.append(op.op_id)
    return out


def plan(store: Store, shared: SharedStore, now: int) -> Report:
    """The read-only report: epoch, quarantine, recovery preview and orphans."""
    from . import continue_session  # pylint: disable=import-outside-toplevel

    epoch, pinned = shared.store_epoch(), shared.pinned_epoch()
    status = "unpinned" if pinned is None else ("ok" if pinned == epoch else "stale")
    rep = Report(str(shared.path), epoch, pinned, status, refused=shared.refusal())
    if status == "stale":
        rep.recovery_point = shared.high_water()
        rep.quarantined = _quarantine(store, shared, rep.recovery_point)
    skip = {q.op_id for q in rep.quarantined if q.op_id}
    for op in shared.pending_ops():
        if op.op_id not in skip:
            line = continue_session.preview_op(store, op, now)
            if line:
                rep.recover.append(line)
    rep.recover += continue_session.preview_published(store, shared, now)
    rep.orphans = _orphans(store, shared, now, skip)
    return rep


def apply(store: Store, shared: SharedStore, rep: Report, now: int) -> Report:
    """Acknowledge the epoch (failing the quarantined reservations), recover, settle orphans.

    Raises :class:`shared_store.Refused` when the store refuses or its epoch moved.
    """
    from . import continue_session  # pylint: disable=import-outside-toplevel
    from .shared_store import Refused  # pylint: disable=import-outside-toplevel

    if rep.refused:
        raise Refused(rep.refused)
    if rep.epoch_status == "stale" and rep.store_epoch is not None:
        fail = [q.op_id for q in rep.quarantined if q.op_id]
        rep.settled_quarantined = shared.acknowledge_epoch(rep.store_epoch, fail, now)
    rep.recover = continue_session.recover_shared_ops(store, now=now)
    rep.orphans = [
        op_id
        for op_id in _orphans(store, shared, now, set())
        if shared.settle(op_id, "failed", now)
    ]
    rep.applied = True
    return rep


def _print_human(rep: Report, *, report_only: bool) -> None:
    mode = "report: nothing written" if report_only else "applied"
    print(f"thread-reconcile ({mode})  store {rep.store}")
    print(f"  epoch: store {rep.store_epoch}  pinned {rep.pinned_epoch}  ({rep.epoch_status})")
    if rep.refused:
        print(f"  ❌ refused: {rep.refused}")
    if rep.epoch_status == "stale":
        verb = "would re-pin" if report_only else "re-pinned"
        print(f"  {verb} epoch {rep.store_epoch}; recovery point {rep.recovery_point}")
    print(f"  quarantined {len(rep.quarantined)} (never applied; re-run fresh or drop)")
    for q in rep.quarantined:
        print(
            f"    ⚠ {q.kind:<16} op {q.op_id or '-'}  session {q.session or '-'}  "
            f"state.db {q.private_state}  shared {q.shared_state}  at {q.at}"
        )
    if not report_only and rep.settled_quarantined:
        print(f"  settled {rep.settled_quarantined} quarantined reservation(s) failed")
    print(f"  recovery by op id: {len(rep.recover)}")
    for line in rep.recover:
        print(f"    {line}")
    verb = "would settle" if report_only else "settled"
    print(f"  orphan reservations ({verb} failed): {len(rep.orphans)}")
    for op_id in rep.orphans:
        print(f"    op {op_id}")


def cmd_thread_reconcile(args: argparse.Namespace) -> int:
    """``ccc thread-reconcile``."""
    # pylint: disable=import-outside-toplevel
    from . import config, shared_store
    from .models import now_ms
    from .store import Store

    report_only = bool(getattr(args, "report", False))
    if shared_store.configured_path(config.load_config()) is None:
        print("thread-reconcile: shared_store_path is unset; nothing to reconcile")
        return 0
    with Store() as store:
        shared = shared_store.open_shared(store.conn)
        if shared is None:
            print("❌ thread-reconcile: the shared store is unavailable", file=sys.stderr)
            return 1
        with shared:
            now = now_ms()
            try:
                rep = plan(store, shared, now)
                if not report_only and not rep.refused:
                    rep = apply(store, shared, rep, now)
            except shared_store.Refused as exc:
                rep.refused = str(exc)
            except sqlite3.Error as exc:
                print(f"❌ thread-reconcile: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
    if getattr(args, "json", False):
        print(json.dumps(rep.to_json(), indent=1))
    else:
        _print_human(rep, report_only=report_only)
    return 1 if rep.refused else 0


def add_parser(sub: Any) -> None:
    """Register ``thread-reconcile`` (internal-style, no TUI key) on the ccc subparsers."""
    p = sub.add_parser(
        "thread-reconcile",
        help="settle stuck thread successions, quarantine and the epoch pin (shared store)",
        description=(
            "Settles the shared thread store against state.db: re-pins a stale authority\n"
            "epoch (a restore), quarantines every private operation newer than the restored\n"
            "store's recovery point (listed, never applied; a still pending reservation of\n"
            "one is failed), finishes or cancels interrupted successions by op id, and fails\n"
            "orphan reservations without any private record. -r/--report lists all of it\n"
            "and writes nothing. A no-op while shared_store_path is unset."
        ),
        epilog=(
            "examples:\n"
            "  ccc thread-reconcile -r        # report only: what would be settled\n"
            "  ccc thread-reconcile -r -J     # the same as JSON\n"
            "  ccc thread-reconcile           # apply: re-pin, fail quarantined, recover"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-r", "--report", action="store_true", help="list what it would do; write nothing"
    )
    p.add_argument("-J", "--json", action="store_true", help="print the JSON report")
    p.set_defaults(func=cmd_thread_reconcile)
