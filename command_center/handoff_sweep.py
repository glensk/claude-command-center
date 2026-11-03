#!/usr/bin/env python3
"""The daemon's continue-session handoff sweep — the repair path (tp#1582).

``accept`` is the primary finalizer and the source's ``start`` an idempotent race
participant; this sweep heals what they left behind, judged against the daemon pass's
registry snapshot (never a per-candidate registry read). Its candidates come from ONE
indexed query (:meth:`TransferStoreMixin.handoff_finalize_candidates`):

* a stranded ``accepted`` transfer (a crash after the accepted CAS) goes through
  ``continue_session.finalize_handoff_source`` — non-capped: finalize and, for a live
  source, the token-scoped closer; capped: finalize the metadata, never a close;
* a non-capped ``finalized`` transfer whose source row still holds a PENDING close token
  gets the B3 closer (``close-now --handoff-transfer``) when the source is live and idle,
  or — a kept ``handoff:`` debt (signal sent, pane still open) — regardless of liveness:
  the closer's ``retry_terminal`` closes the pane without signalling again.

* such a transfer whose token is IN FLIGHT (claimed by a closer or the source's Stop
  chain) is left alone unless its holder is dead (tp#1595): claimed at least
  :data:`STALE_MS` ago AND either holderless or a recorded ``"pid:start"`` that a
  readable ``ps`` shows gone or replaced. Such a token goes back PENDING
  (:meth:`Store.reclaim_stale_close`, ``close_bound`` kept, so a ``handoff:`` debt stays
  a debt) and a later pass spawns the closer through the pending branch. A live holder or
  an unreadable ``ps`` is never stale; a legacy token without a claim time is first
  stamped "seen now" (:meth:`Store.mark_in_flight_seen`).

One closer spawn per source per :data:`PACING_MS`. Nothing is ever selected by age: the
age floor only gates reverting an existing in-flight token of a candidate the query already
returns, never a close. A source whose close completed (token retired) is never a candidate
again, so a tab the user reopened afterwards is never closed by the daemon. ``dry_run``
reports what it would do and mutates and spawns nothing. Each candidate is isolated: one
failure is reported and the rest still run. The daemon's entry point is
``daemon._run_handoff_finalize``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=import-outside-toplevel  # continue_session / spawn stay lazy (module convention)

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from . import config
from .models import LiveSession, now_ms
from .store import Store
from .transfer_store import ACCEPTED, HandoffCandidate

if TYPE_CHECKING:  # pragma: no cover
    from .adapters.claude import ClaudeAdapter

#: One closer spawn per source per window: a closer that refuses (busy source, a moved
#: transcript) hands its token back PENDING, so without pacing every pass would spawn again.
PACING_MS = 10 * 60 * 1000

#: An in-flight close token younger than this is never reclaimed (tp#1595). Longer than the
#: longest legitimate hold (10 s stability + up to 900 s drain + 10 s exit wait + the
#: terminal close); it matters for holderless tokens (the hook → ``close-now`` start gap,
#: legacy rows) and is a belt for a recorded dead holder.
STALE_MS = 20 * 60 * 1000


class Tally(Protocol):
    """The ``DaemonReport`` fields this sweep fills."""

    handoff_finalized: list[str]  # transfers moved accepted → finalized
    handoff_closers: list[str]  # source sessions a closer was spawned for
    handoff_reclaimed: list[str]  # source sessions whose dead holder's token went pending
    handoff_errors: list[str]  # "<transfer>: <why>"


def pacing_path() -> Path:
    """``{source session: last closer spawn (epoch ms)}``, persisted across daemon passes."""
    return config.app_home() / "handoff_close_spawns.json"


def _load_pacing() -> dict[str, int]:
    try:
        data = json.loads(pacing_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, int)}


def _save_pacing(pacing: dict[str, int], now: int) -> None:
    """Keep the stamps still inside the window (atomic replace; a failure only costs pacing)."""
    keep = {sid: at for sid, at in pacing.items() if now - at < PACING_MS}
    path = pacing_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(keep, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class _SnapshotRegistry:  # pylint: disable=too-few-public-methods  # the one method B5 reads
    """The pass's registry snapshot behind ``discover()`` — no per-candidate registry read."""

    def __init__(self, snapshot: dict[str, LiveSession]) -> None:
        self._entries = list(snapshot.values())

    def discover(self) -> list[LiveSession]:
        return list(self._entries)


@dataclass
class _Pass:
    """The shared state of one :func:`run`."""

    store: Store
    snapshot: dict[str, LiveSession]
    report: Tally
    dry_run: bool
    now: int
    pacing: dict[str, int]
    ps: dict[int, Any] | None = None  # one ps_table() per pass, read on first need

    def ps_rows(self) -> dict[int, Any]:
        """The pass's shared ``ps`` table (an empty dict = unreadable)."""
        if self.ps is None:
            from . import terminal

            self.ps = terminal.ps_table() or {}
        return self.ps


def _finalize_stranded(sweep: _Pass, cand: HandoffCandidate) -> None:
    """An ``accepted`` transfer nobody finalized: the B5 helper."""
    from . import continue_session

    sid, tid = cand.source_session, cand.transfer_id
    if sweep.dry_run:
        sweep.report.handoff_finalized.append(tid)
        entry = sweep.snapshot.get(sid)
        alive = entry is not None and entry.alive
        if not cand.capped and alive and cand.token_state != "in_flight":
            sweep.report.handoff_closers.append(sid)
        return
    transfer = sweep.store.get_transfer(tid)
    if transfer is None:
        return
    registry = cast("ClaudeAdapter", _SnapshotRegistry(sweep.snapshot))
    record = continue_session.finalize_handoff_source(sweep.store, registry, transfer)
    sweep.report.handoff_finalized.append(tid)
    if record.get("closer") == "spawned":
        sweep.pacing[sid] = sweep.now
        sweep.report.handoff_closers.append(sid)
    elif record.get("closer") == "spawn_failed":
        sweep.report.handoff_errors.append(f"{tid}: closer spawn failed")


def _holder_dead(sweep: _Pass, holder: str) -> bool:
    """True only when a readable ``ps`` proves the ``"pid:start"`` *holder* gone or replaced.

    Fails closed: an unparsable holder, an unreadable ``ps`` or the same live process all
    answer ``False``.
    """
    from . import terminal

    pid_text, sep, start = holder.partition(":")
    if not sep or not pid_text.isdigit() or not start:
        return False
    pid = int(pid_text)
    ident = terminal.probe_identity(pid, sweep.ps_rows())
    if ident.kind == terminal.IDENTITY_PID_GONE:
        return True
    return ident.kind == terminal.IDENTITY_OK and bool(ident.start) and ident.start != start


def _reclaim_stale(sweep: _Pass, cand: HandoffCandidate) -> None:
    """An in-flight token: put it back pending when its holder is provably dead (tp#1595).

    Never spawns: the pending branch does that on a later pass, paced as usual.
    """
    sid = cand.source_session
    if not cand.close_claimed_at:
        if not sweep.dry_run:
            sweep.store.mark_in_flight_seen(sid, cand.close_token, sweep.now)
        return  # legacy: its age counts from this pass
    if sweep.now - cand.close_claimed_at < STALE_MS:
        return
    if cand.close_holder and not _holder_dead(sweep, cand.close_holder):
        return  # alive (however old), ps unreadable, or unparsable: fail closed
    if sweep.dry_run:
        sweep.report.handoff_reclaimed.append(sid)
        return
    if sweep.store.reclaim_stale_close(
        sid,
        cand.close_token,
        holder=cand.close_holder,
        claimed_at=cand.close_claimed_at,
        now=sweep.now,
    ):
        sweep.report.handoff_reclaimed.append(sid)


def _close_finalized(sweep: _Pass, cand: HandoffCandidate) -> None:
    """A ``finalized`` transfer whose source still holds its close token: the B3 closer."""
    from . import continue_session
    from .adapters.claude_agents import contract_status

    sid, tid = cand.source_session, cand.transfer_id
    if not cand.debt:
        entry = sweep.snapshot.get(sid)
        if entry is None or not entry.alive:
            return  # gone: reconcile closes the row as done; nothing to close
        if contract_status(entry.raw_status) != "idle":
            return  # busy: the closer would refuse; a later pass (or its own Stop) tries
    if cand.token_state != "pending":
        if cand.token_state == "in_flight":
            _reclaim_stale(sweep, cand)  # a dead holder's token goes pending; spawn next pass
        return
    if sweep.now - sweep.pacing.get(sid, 0) < PACING_MS:
        return
    if sweep.dry_run:
        sweep.report.handoff_closers.append(sid)
        return
    # Ensure the token: still THIS pending one. Never mint a fresh token for a finalized
    # handoff — a token retired since the query is a completed close, and a new arm would
    # close a tab the user reopened afterwards.
    row = sweep.store.get(sid)
    if row is None or row.close_token != cand.close_token or not row.close_requested_at:
        return
    if continue_session._spawn_closer(tid, sid):  # pylint: disable=protected-access
        sweep.pacing[sid] = sweep.now
        sweep.report.handoff_closers.append(sid)
    else:
        sweep.report.handoff_errors.append(f"{tid}: closer spawn failed")


def run(store: Store, snapshot: dict[str, LiveSession], report: Tally, dry_run: bool) -> None:
    """One sweep over :meth:`~TransferStoreMixin.handoff_finalize_candidates`; never raises."""
    try:
        candidates = store.handoff_finalize_candidates()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"ccc daemon: handoff sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        report.handoff_errors.append(f"query: {type(exc).__name__}: {exc}")
        return
    if not candidates:
        return
    sweep = _Pass(store, snapshot, report, dry_run, now_ms(), _load_pacing())
    before = dict(sweep.pacing)
    for cand in candidates:
        try:
            if cand.state == ACCEPTED:
                _finalize_stranded(sweep, cand)
            else:
                _close_finalized(sweep, cand)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            why = f"{type(exc).__name__}: {exc}"
            print(f"ccc daemon: handoff {cand.transfer_id} failed: {why}", file=sys.stderr)
            report.handoff_errors.append(f"{cand.transfer_id}: {why}")
    if not dry_run and sweep.pacing != before:
        _save_pacing(sweep.pacing, sweep.now)
