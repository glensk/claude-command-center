#!/usr/bin/env python3
"""``ccc close -s ID -j`` — close an IDLE live session from outside it (tp#1481 P1).

The bridge verb decide's Mac applier calls; built on the hardened handoff closer
(:mod:`command_center.handoff_close`), never a second implementation. In order, any failure
refuses with a machine-readable ``reason`` and NOTHING is signalled:

1. live, ``kind: interactive``, one account (:func:`bridge_target.require_live`:
   ``not_running`` / ``background`` / ``conflict``), not a staff session (``staff_bound``);
2. the close token: an in-flight token (an armed ``mark-done --close`` firing, another
   closer) or a handoff-owned one refuses ``close_in_flight``;
3. **idle is mandatory**: registry ``idle``, no pending picker (``picker_pending``), no open
   turn (``busy``), no subagent or background shell in flight (the ``continue-session``
   drain probe, :func:`inflight.inventory`: ``subagents`` / ``background_shell``);
4. the token is taken with :meth:`Store.claim_close` (pending → in flight; a user's pending
   ``mark-done --close`` arm is claimed as is, else a fresh one is armed and claimed) — of
   two closers exactly one wins (``close_in_flight``);
5. ``handoff_close``'s bind → :func:`~handoff_close.await_stable` (registry idle, same pid
   + start time, unchanged transcript identity across the window: ``stale_evidence``) →
   Stop-chain drain (``cli._wait_for_stop_drain``, bounded by :data:`DRAIN_WAIT_SEC`:
   ``stop_chain``) → the probe again → inside the per-tab send lock
   :func:`~handoff_close._revalidate` (token, fresh pid identity, transcript:
   ``identity_changed`` / ``stale_evidence``) → the close debt is bound → SIGTERM.

Then the terminal closes: the hosting tmux pane (``terminal: tmux``), else the iTerm pane or
tab through :func:`terminal.close_iterm_session` (AppleScript ``close`` only — no
``activate``, no selection) — but only a tab VALIDATED against the live pid's tty
(``cli.cmd_close_now``'s stale-evidence guard: never close on a store-only id; else
``terminal: none``). The store status becomes DONE when ccc's DONE flag is set, else PARKED
(HANDED_OFF for a handed-off row, :func:`models.closed_status`); a close never marks a
session done. A pane that did not close keeps the debt: a repeated call closes that pane
only, never signalling again. A session already gone answers ``not_running``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=protected-access  # the shared close steps live in cli / handoff_close

import dataclasses
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from . import handoff_close as hc
from .bridge_json import BridgeError
from .bridge_target import require_live, resolve_tab, tab_lock

if TYPE_CHECKING:
    from pathlib import Path

    from .adapters.claude import ClaudeAdapter
    from .bridge_target import BridgeDeps
    from .config import Config
    from .store import Store

#: Upper bound of the Stop-chain drain wait: an idle session's chain is long drained, so a
#: longer wait only holds decide's applier (``stop_barrier_wait_sec`` may be minutes).
DRAIN_WAIT_SEC = 15
#: Every ``reason`` a refusal can carry (decide's B10 applier maps them).
REASONS = frozenset(
    {
        "busy",
        "subagents",
        "background_shell",
        "picker_pending",
        "not_running",
        "background",
        "conflict",
        "stop_chain",
        "identity_changed",
        "staff_bound",
        "close_in_flight",
        "stale_evidence",
        "unobservable",
    }
)
_LOG_KIND = "close"


def _refusal(session_id: str, reason: str, message: str) -> BridgeError:
    """A ``refused`` result: ``error.code`` = *reason*, ``data`` names it too."""
    from . import hooks

    hooks._log_event(session_id, _LOG_KIND, f"ccc close refused ({reason}): {message}")
    return BridgeError(
        reason,
        message,
        data={"session_id": session_id, "result": "refused", "reason": reason},
    )


def _probe(adapter: ClaudeAdapter, store: Store, sid: str, pid: int, path: Path | None) -> str:
    """The ``continue-session`` in-flight probe: ``""``, ``subagents`` or ``background_shell``."""
    from . import inflight

    inv = inflight.inventory(
        adapter,
        pid,
        path,
        active_subagents=store.active_subagents(sid),
        subagent_started_at=store.subagent_started_at(sid),
    )
    if inv.clear:
        return ""
    agents = any(
        item.kind == inflight.KIND_SUBAGENT
        or item.id == "subagents:counter"
        or item.description == "child claude process"
        for item in inv.items
    )
    return (
        "subagents" if agents or any("subagent" in r for r in inv.reasons) else "background_shell"
    )


def _idle_gate(adapter: ClaudeAdapter, sid: str, live: Any) -> None:
    """Idle is mandatory: registry idle, no picker, no open turn, nothing in flight."""
    from .adapters.claude_agents import contract_status
    from .bridge_send import inspect_transcript
    from .store import Store as _Store

    path = adapter.transcript_path(live.cwd, sid, live.config_dir or None)
    ins = None
    if path is not None:
        try:
            ins = inspect_transcript(path)
        except BridgeError as exc:
            raise _refusal(sid, "unobservable", exc.message) from exc
    if ins is not None and ins.pending:
        raise _refusal(sid, "picker_pending", "the session shows a question picker")
    # A ``shell`` status with no open turn is an idle session holding a background shell
    # (tp#1662); the in-flight probe below still refuses a real one (keep-warm excepted).
    turn_active = ins.turn_active if ins is not None else None
    if contract_status(live.raw_status, turn_active) != "idle":
        raise _refusal(sid, "busy", f"session is {live.raw_status or 'unknown'}, not idle")
    if ins is None:
        raise _refusal(sid, "unobservable", "no transcript found for the session")
    if ins.turn_active is not False:
        raise _refusal(sid, "busy", "a turn is open in the transcript")
    with _Store() as store:
        work = _probe(adapter, store, sid, live.pid, path)
    if work:
        raise _refusal(sid, work, f"{work.replace('_', ' ')} still in flight")


def _claim(sid: str, cwd: str, holder: str) -> tuple[hc.Claim, bool]:
    """Take the close token (:meth:`Store.claim_close`) — ``(claim, own arm)``.

    A user's pending ``mark-done --close`` arm is claimed as is (and handed back pending on
    a refusal); otherwise a fresh arm is created and claimed (and retired on a refusal, so a
    refused close never turns into a close-after-turn). An in-flight token, a handoff-owned
    one or a lost claim refuses ``close_in_flight``.
    """
    from .models import now_ms
    from .store import Store as _Store

    with _Store() as store:
        if store.get(sid) is None:
            store.ensure(sid, cwd=cwd)
        row = store.get(sid)
        token = (row.close_token or "") if row is not None else ""
        bound = (row.close_bound or "") if row is not None else ""
        pending = bool(row is not None and row.close_requested_at)
        if token and (not pending or hc.Debt.decode(bound) or hc.keeps_close_token(store, sid)):
            raise _refusal(sid, "close_in_flight", "another close of this session is in flight")
        own = not token
        if own:
            token = store.ensure_close_armed(sid, now_ms())
            bound = ""
        armed_at = store.claim_close(sid, token, holder)
        if not armed_at:
            if own:
                store.retire_close(sid, token)
            raise _refusal(sid, "close_in_flight", "the close token is held by another closer")
    return hc.Claim(sid, "", token, armed_at, bound, holder), own


def _give_back(claim: hc.Claim, own: bool, keep: str = "") -> None:
    """Undo the claim before any signal: retire an own arm, restore a user's (pending)."""
    if own and not keep:
        hc._retire(claim)
    else:
        hc._restore(claim, keep or claim.bound)


def _terminal(pane: str, iterm: str) -> str:
    """Close the tmux pane, else the validated iTerm pane/tab: ``tmux|tab|pane|none``."""
    from . import cli, terminal

    if pane:
        return "tmux" if cli._close_terminal(pane, "") else "none"
    if not iterm:
        return "none"
    try:
        result = terminal.close_iterm_session(iterm)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return "none"
    return {"tab": "tab", "session": "pane"}.get(result, "none")


def _set_closed_status(sid: str) -> str:
    """DONE stays DONE, else PARKED (HANDED_OFF when handed off) — never marked done."""
    from .models import closed_status
    from .store import Store as _Store

    with _Store() as store:
        row = store.get(sid)
        if row is None:
            return "parked"
        status = closed_status(row)
        store.update_fields(sid, status=status.value)
    return str(status.value)


def _bound_cfg(cfg: Config) -> Config:
    return dataclasses.replace(
        cfg, stop_barrier_wait_sec=min(int(cfg.stop_barrier_wait_sec), DRAIN_WAIT_SEC)
    )


def _close_live(  # pylint: disable=too-many-locals
    deps: BridgeDeps, claim: hc.Claim, own: bool, cfg: Config
) -> dict[str, Any]:
    """Bind, prove stable idle, drain, revalidate under the tab lock, SIGTERM, close."""
    from . import cli, hooks, terminal

    adapter = cli._adapter()
    sid = claim.session_id
    try:
        entry = hc._live_entry(adapter, sid)
        start = hc._start_of(entry.pid)
        with deps.store() as store:
            row = store.get(sid)
        cwd = entry.cwd or (row.cwd if row else "") or ""
        path = adapter.transcript_path(cwd, sid, entry.config_dir or None)
        bound = hc.Bound(entry.pid, start, cwd, path)
        stamp = hc.await_stable(adapter, sid, bound)
        if cfg.stop_barrier_enabled:
            hc._drain(bound, _bound_cfg(cfg))
        with deps.store() as store:
            work = _probe(adapter, store, sid, bound.pid, path)
        if work:
            raise hc.Refused(f"{work.replace('_', ' ')} still in flight", work)
        stored = (row.iterm_session_id or "").strip() if row is not None else ""
        tab, validated = resolve_tab(stored or None, bound.pid, deps.ps_table(), deps.panes())
        iterm = (tab or "") if validated else ""  # never a store-only id (stale evidence)
        debt = hc.Debt(
            pid=bound.pid,
            start=start,
            pane=cli._tmux_pane_id(sid),
            iterm=iterm,
            tty=terminal.pid_tty(bound.pid, terminal.ps_table()),
        )
        lock = tab_lock(deps, iterm) if iterm else nullcontext()
        with lock:
            hc._revalidate(adapter, claim, bound, stamp)
            hc._bind_debt(claim, debt)
            signalled = cli._sigterm(bound.pid)
    except hc.Refused as exc:
        _give_back(claim, own)
        raise _refusal(sid, exc.code or "unobservable", str(exc)) from exc
    except BridgeError as exc:  # tab_locked: a send/answer to this tab is running
        _give_back(claim, own)
        raise _refusal(sid, "busy", exc.message) from exc
    if not signalled:  # gone between the revalidation and the signal: nothing was sent
        _give_back(claim, own)
        raise _refusal(sid, "not_running", f"pid {bound.pid} was gone at the signal")
    from . import closelog

    closelog.log_close(sid, bound.pid, "bridge-close")
    status = _set_closed_status(sid)
    try:
        hc._wait_exit(bound.pid, start)
        kind = _terminal(debt.pane, "" if debt.pane else debt.iterm)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        kind = "none"
    return _settle(claim, debt, kind, status, hooks)


def _settle(claim: hc.Claim, debt: hc.Debt, kind: str, status: str, hooks: Any) -> dict[str, Any]:
    """Retire on a proven close; else keep the debt (a retry closes the pane only)."""
    sid = claim.session_id
    target = debt.pane or debt.iterm
    debt_kept = bool(target) and kind == "none" and not hc.pane_gone(debt)
    if debt_kept:
        hc._restore(claim, debt.encode())
    else:
        hc._retire(claim)
    hooks._log_event(
        sid,
        _LOG_KIND,
        f"ccc close: pid {debt.pid} signalled, terminal {kind}"
        + (" (close debt kept)" if debt_kept else ""),
    )
    return {
        "session_id": sid,
        "result": "closed",
        "terminal": kind,
        "status": status,
        "close_debt": debt_kept,
    }


def _retry_debt(sid: str, holder: str) -> dict[str, Any] | None:
    """A kept ``ccc close`` debt and no live process: close that pane only, never signal."""
    from . import cli, hooks, terminal
    from .store import Store as _Store

    with _Store() as store:
        row = store.get(sid)
        if row is None or not row.close_token or not row.close_requested_at:
            return None
        debt = hc.Debt.decode(row.close_bound or "")
        if debt is None or hc.keeps_close_token(store, sid):
            return None
        try:
            if [e for e in cli._adapter().discover_raw(sid) if e.alive]:
                return None
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return None
        armed_at = store.claim_close(sid, row.close_token, holder)
        if not armed_at:
            raise _refusal(sid, "close_in_flight", "the close debt is held by another closer")
    claim = hc.Claim(sid, "", row.close_token, armed_at, row.close_bound or "", holder)
    table = terminal.ps_table()
    if not table or not hc._gone(debt.pid, debt.start, table):
        hc._restore(claim, debt.encode())
        raise _refusal(sid, "unobservable", "the signalled process cannot be proven gone")
    if debt.tty and any(
        terminal.pid_tty(pid, table) == debt.tty and terminal.pid_is_claude(pid, table)
        for pid in table
    ):
        hc._retire(claim)  # the tab was reused: never close it
        raise _refusal(sid, "not_running", f"a live claude holds {debt.tty} — close debt dropped")
    kind = _terminal(debt.pane, "" if debt.pane else debt.iterm)
    return _settle(claim, debt, kind, _set_closed_status(sid), hooks)


def run_close(deps: BridgeDeps, session_id: str) -> dict[str, Any]:
    """Close *session_id* when it is provably idle; the ``data`` object or a refusal."""
    from . import config, staffrole

    sid = (session_id or "").strip()
    if not sid:
        raise BridgeError("usage", "pass -s/--session <id>", 2)
    holder = hc.own_identity()
    if not holder:
        raise _refusal(sid, "unobservable", "own process could not be identified (ps unreadable)")
    retried = _retry_debt(sid, holder)
    if retried is not None:
        return retried
    try:
        live = require_live(deps, sid)
    except BridgeError as exc:
        reason = "conflict" if exc.code == "account_conflict" else exc.code
        raise _refusal(sid, reason, exc.message) from exc
    if staffrole.adopted_role(sid):
        raise _refusal(sid, "staff_bound", "a staff agent's session is closed by its staff")
    from . import cli

    adapter = cli._adapter()
    with deps.store() as store:
        row = store.get(sid)
        if (
            row is not None
            and row.close_token
            and (not row.close_requested_at or hc.keeps_close_token(store, sid))
        ):
            raise _refusal(sid, "close_in_flight", "another close of this session is in flight")
    _idle_gate(adapter, sid, live)
    claim, own = _claim(sid, live.cwd, holder)
    try:
        return _close_live(deps, claim, own, config.load_config())
    except BridgeError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        _give_back(claim, own)  # unexpected: an own arm is retired, never left pending
        raise
