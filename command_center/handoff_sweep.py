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

Two more candidate sets (tp#2172), each from its own indexed query:

* a ``prepared``/``opened`` transfer older than ``LAUNCH_GRACE_S`` whose successor is
  ``dead:*`` (``continue_session.successor_state`` against the pass's snapshot) and whose
  ``start`` is not live is aborted (``abort_transfer``, reason ``sweep: successor dead:
  <why>``). A ``start`` is live while its heartbeat (``start_heartbeat_at``) is fresher than
  :data:`HEARTBEAT_STALE_MS` and its ``start_pid`` is not provably gone; a transfer with no
  heartbeat (an older build) is live while a ``continue-session start`` runs among the
  source pid's descendants in the pass's ONE ``ps`` table (any such process when the source
  is unknown). An unreadable ``ps`` counts as live: the transfer is left alone.
* a ``failed`` transfer whose successor recorded an accept refusal (``accept_refused_at``)
  and still runs, idle, as the SAME process that refused (started before the refusal), gets
  that successor's close armed and the token-scoped closer spawned — the orphan close.

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

import functools
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from . import config
from .models import LiveSession, now_ms
from .store import Store
from .transfer_store import ACCEPTED, HandoffCandidate, Transfer

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

#: tp#2172 D5: a ``start`` heartbeat older than this is stale (3 polls + 30 s).
HEARTBEAT_STALE_MS = int((3 * 2.0 + 30) * 1000)


class Tally(Protocol):
    """The ``DaemonReport`` fields this sweep fills."""

    handoff_finalized: list[str]  # transfers moved accepted → finalized
    handoff_closers: list[str]  # source sessions a closer was spawned for
    handoff_reclaimed: list[str]  # source sessions whose dead holder's token went pending
    handoff_aborted: list[str]  # active transfers aborted: successor dead, no live start
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

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        """The real adapter's path resolution (a file lookup, no registry read)."""
        from . import continue_session

        return continue_session._adapter().transcript_path(  # pylint: disable=protected-access
            cwd, session_id, config_dir
        )


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


# ------------------------------------------------------------ dead transfers (tp#2172 D5)


def _is_start_command(command: str) -> bool:
    """A ``… continue-session start …`` command line (ccc itself or a shell running it)."""
    parts = command.split()
    try:
        at = parts.index("continue-session")
    except ValueError:
        return False
    return "start" in parts[at + 1 :]


def _start_live(sweep: _Pass, transfer: Transfer) -> bool:
    """Is the ``start`` driving *transfer* alive? Fails toward "live" on any doubt."""
    rows = sweep.ps_rows()
    if transfer.start_heartbeat_at:
        if sweep.now - transfer.start_heartbeat_at > HEARTBEAT_STALE_MS:
            return False
        return not (rows and transfer.start_pid and transfer.start_pid not in rows)
    if not rows:
        return True  # ps unreadable: leave the transfer
    starts = [pid for pid, row in rows.items() if _is_start_command(str(row.command))]
    entry = sweep.snapshot.get(transfer.source_session)
    if entry is None or entry.pid <= 0:
        return bool(starts)  # the source is unknown: any start may be its own
    from . import terminal

    return any(terminal.pid_descends_from(pid, entry.pid, rows) for pid in starts)


def _abort_dead(sweep: _Pass, transfer: Transfer, watch: Any) -> None:
    """Abort *transfer* when its successor is dead and no ``start`` drives it any more."""
    from . import continue_session

    registry = cast("ClaudeAdapter", _SnapshotRegistry(sweep.snapshot))
    verdict = continue_session.successor_state(
        sweep.store, registry, transfer, sweep.now, watch.fresh()
    )
    if not verdict.startswith(continue_session.DEAD_PREFIX) or _start_live(sweep, transfer):
        return
    tid = transfer.transfer_id
    if sweep.dry_run:
        sweep.report.handoff_aborted.append(tid)
        return
    why = verdict.removeprefix(continue_session.DEAD_PREFIX)
    code, message = continue_session.abort_transfer(
        sweep.store, tid, f"sweep: successor dead: {why}", sweep.now
    )
    if code != 0:
        sweep.report.handoff_errors.append(f"{tid}: {message}")
    elif message.startswith(continue_session.ABORT_PREFIX):
        print(f"ccc daemon: handoff {tid} {message}", file=sys.stderr)
        sweep.report.handoff_aborted.append(tid)


def _orphan_due(sweep: _Pass, transfer: Transfer) -> bool:
    """Is *transfer*'s successor the idle, still-running process that refused its accept,
    with no close in flight and no closer spawned for it in this pacing window?"""
    from .adapters.claude_agents import contract_status

    sid = transfer.target_session
    entry = sweep.snapshot.get(sid)
    if entry is None or not entry.alive or contract_status(entry.raw_status) != "idle":
        return False
    if not entry.started_at or entry.started_at > transfer.accept_refused_at:
        return False  # a later process (the tab reopened) or an unknown start: never closed
    row = sweep.store.get(sid)
    if row is None or (row.close_token and not row.close_requested_at):
        return False  # unknown row, or a close already in flight
    return sweep.now - sweep.pacing.get(sid, 0) >= PACING_MS


def _close_orphan(sweep: _Pass, transfer: Transfer) -> None:
    """Close the live successor of a FAILED transfer that refused its accept (D13).

    Only the process that refused (registry start at or before the refusal): a tab the
    user reopened later is never closed. Idle only, paced, a held token left alone."""
    from . import continue_session

    sid, tid = transfer.target_session, transfer.transfer_id
    if not _orphan_due(sweep, transfer):
        return
    if sweep.dry_run:
        sweep.report.handoff_closers.append(sid)
        return
    sweep.store.ensure_close_armed(sid, sweep.now)
    row = sweep.store.get(sid)
    if row is None or not row.close_token or not row.close_requested_at:
        return  # claimed meanwhile (its own Stop chain): that holder closes it
    if continue_session._spawn_closer(tid, sid):  # pylint: disable=protected-access
        sweep.pacing[sid] = sweep.now
        sweep.report.handoff_closers.append(sid)
    else:
        sweep.report.handoff_errors.append(f"{tid}: orphan closer spawn failed")


def _guarded(sweep: _Pass, tid: str, step: Callable[[], None]) -> None:
    """Run one candidate's *step*; a failure is reported and the rest still run."""
    try:
        step()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        why = f"{type(exc).__name__}: {exc}"
        print(f"ccc daemon: handoff {tid} failed: {why}", file=sys.stderr)
        sweep.report.handoff_errors.append(f"{tid}: {why}")


def _query(report: Tally, name: str, read: Callable[[], list[Any]]) -> list[Any]:
    """One candidate query; a failure is reported and yields nothing."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"ccc daemon: handoff sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        report.handoff_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        return []


def run(store: Store, snapshot: dict[str, LiveSession], report: Tally, dry_run: bool) -> None:
    """One sweep over the three candidate queries; never raises."""
    from . import continue_session

    now = now_ms()
    grace_ms = continue_session.LAUNCH_GRACE_S * 1000
    candidates: list[HandoffCandidate] = _query(report, "query", store.handoff_finalize_candidates)
    dead: list[Transfer] = _query(
        report, "dead query", lambda: store.dead_transfer_candidates(now - grace_ms)
    )
    orphans: list[Transfer] = _query(report, "orphan query", store.orphan_close_candidates)
    if not (candidates or dead or orphans):
        return
    sweep = _Pass(store, snapshot, report, dry_run, now, _load_pacing())
    before = dict(sweep.pacing)
    for cand in candidates:
        step = _finalize_stranded if cand.state == ACCEPTED else _close_finalized
        _guarded(sweep, cand.transfer_id, functools.partial(step, sweep, cand))
    watch = continue_session.SuccessorWatch.from_env() if dead else None
    for transfer in dead:
        _guarded(
            sweep, transfer.transfer_id, functools.partial(_abort_dead, sweep, transfer, watch)
        )
    for transfer in orphans:
        _guarded(sweep, transfer.transfer_id, functools.partial(_close_orphan, sweep, transfer))
    if not dry_run and sweep.pacing != before:
        _save_pacing(sweep.pacing, sweep.now)
