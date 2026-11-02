#!/usr/bin/env python3
"""``ccc close-now --handoff-transfer <id> --session <src>`` — the handoff closer (tp#1582).

Closes the SOURCE of a ``finalized`` continue-session transfer: SIGTERM its one live
Claude process and close its tmux pane / iTerm tab, but only once that process is
provably and stably idle. Token-scoped and process-bound:

1. Validate the transfer (``finalized``, not capped, recorded source = ``--session``) and
   the row's lineage (``handed_off_to`` = the transfer's target).
2. Claim the source's close token (:meth:`Store.claim_close`, pending → in flight): of two
   closers — or a closer and the source's own Stop chain — exactly one wins.
3. Bind exactly ONE live registry entry (0 or more than 1 → refuse) and record its PID and
   PID start time.
4. Require registry ``idle``, ``inspect_transcript(...).turn_active is False`` and an
   unchanged transcript identity (inode, mtime, size) over :data:`STABLE_WINDOW_SEC` —
   also with ``stop_barrier_enabled = false``, where only the Stop-chain drain is skipped.
5. Revalidate lineage, token, PID, PID start time, idle state and transcript immediately
   before the SIGTERM.

A refusal before the signal sends nothing, hands its own token back
(:meth:`Store.restore_close`, no TTL: pending again), writes an ``events.log`` line and
exits 1. The token is retired only when the SIGTERM succeeded AND the terminal close
reported success, or the pane/tab is positively gone (the process is gone and no process
holds its recorded tty). Otherwise the debt is kept: the token goes back pending with a
``handoff:`` binding (:class:`Debt`: pid, start, tmux pane, iTerm id, tty), so a retry
closes the pane WITHOUT signalling again.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=cyclic-import  # cli dispatches here lazily; the shared close steps live in cli

import argparse
import dataclasses
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config
from .store import Store

if TYPE_CHECKING:  # pragma: no cover
    from .adapters.claude import ClaudeAdapter
    from .models import LiveSession

#: How long the source must stay idle with an unchanged transcript, and the poll interval.
#: Module-level so tests can shrink the window instead of sleeping.
STABLE_WINDOW_SEC = 10.0
STABLE_POLL_SEC = 2.0
#: How long to wait for the SIGTERM'd process to exit before the terminal close.
EXIT_WAIT_SEC = 10.0
EXIT_POLL_SEC = 0.25
#: ``close_bound`` prefix of a kept close debt (signal sent, terminal close outstanding).
DEBT_PREFIX = "handoff:"


def _sleep(seconds: float) -> None:
    """The one sleep of this module (tests patch it to drive the stability window)."""
    time.sleep(seconds)


class Refused(Exception):
    """A refusal before any signal: nothing was touched."""


@dataclass(frozen=True)
class Claim:
    """The close token this closer won."""

    session_id: str
    transfer_id: str
    token: str
    armed_at: int
    bound: str  # the row's close_bound at claim time


@dataclass(frozen=True)
class Bound:
    """The one source process this closer is bound to."""

    pid: int
    start: str
    cwd: str
    transcript: Path | None


@dataclass(frozen=True)
class Debt:
    """A kept close debt: the process was signalled, its pane/tab is still to close."""

    pid: int
    start: str
    pane: str = ""
    iterm: str = ""
    tty: str = ""

    def encode(self) -> str:
        """The ``close_bound`` value carrying this debt."""
        return DEBT_PREFIX + json.dumps(dataclasses.asdict(self), sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> Debt | None:
        """The debt in a ``close_bound`` value (``None`` for anything else)."""
        if not raw.startswith(DEBT_PREFIX):
            return None
        try:
            data = json.loads(raw[len(DEBT_PREFIX) :])
            return cls(
                pid=int(data["pid"]),
                start=str(data["start"]),
                pane=str(data.get("pane") or ""),
                iterm=str(data.get("iterm") or ""),
                tty=str(data.get("tty") or ""),
            )
        except (ValueError, KeyError, TypeError):
            return None


def lineage_refusal(store: Store, transfer_id: str, session_id: str) -> str:
    """Why *transfer_id* does not license closing *session_id* (``""`` = it does)."""
    from .transfer_store import FINALIZED

    transfer = store.get_transfer(transfer_id)
    if transfer is None:
        return f"transfer {transfer_id} not found"
    if transfer.source_session != session_id:
        return f"transfer {transfer_id} is not this session's handoff"
    if transfer.state != FINALIZED:
        return f"transfer {transfer_id} is {transfer.state}, not finalized"
    if transfer.capped:
        return f"transfer {transfer_id} is capped — its source is never closed"
    row = store.get(session_id)
    target = transfer.target_session
    if not target or row is None or (row.handed_off_to or "") != target:
        return f"session is not handed off to {target[:8] or 'a successor'}"
    return ""


def _live_entry(adapter: ClaudeAdapter, session_id: str) -> LiveSession:
    """The ONE live registry entry of *session_id*; :class:`Refused` for 0, >1 or unreadable."""
    try:
        entries = [e for e in adapter.discover_raw(session_id) if e.alive]
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        raise Refused(f"registry unreadable ({exc})") from exc
    if not entries:
        raise Refused("no live source process")
    if len(entries) > 1:
        raise Refused(f"{len(entries)} live registry entries (conflict) — cannot bind one")
    entry = entries[0]
    if entry.pid <= 0 or entry.conflict:
        raise Refused("the live registry entry has no usable pid")
    return entry


def _start_of(pid: int) -> str:
    """*pid*'s start stamp, when it is a live claude; :class:`Refused` otherwise."""
    from . import terminal

    ident = terminal.probe_identity(pid)
    if ident.kind == terminal.IDENTITY_PS_UNREADABLE:
        raise Refused(f"pid {pid} could not be identified (ps unreadable)")
    if ident.kind != terminal.IDENTITY_OK:
        raise Refused(f"pid {pid} is gone")
    if not ident.is_claude:
        raise Refused(f"pid {pid} is not a claude process")
    return ident.start


def _stamp(path: Path | None) -> tuple[int, int, int] | None:
    """The transcript identity ``(inode, mtime_ns, size)`` (``None`` = unreadable)."""
    try:
        stat = path.stat() if path is not None else None
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size) if stat is not None else None


def observe(adapter: ClaudeAdapter, session_id: str, bound: Bound) -> tuple[int, int, int]:
    """One idle proof of the bound process — its transcript identity, or :class:`Refused`.

    The same single live entry with the bound pid, registry ``idle``, the same PID start
    time, a readable transcript whose inspection shows no open turn, and an identity that
    did not move while it was being read.
    """
    from .adapters.claude_agents import contract_status
    from .bridge_send import BridgeError, inspect_transcript

    entry = _live_entry(adapter, session_id)
    if entry.pid != bound.pid:
        raise Refused(f"the live source pid changed ({bound.pid} → {entry.pid})")
    if contract_status(entry.raw_status) != "idle":
        raise Refused(f"source busy (registry {entry.raw_status or 'unknown'})")
    if _start_of(bound.pid) != bound.start:
        raise Refused(f"pid {bound.pid} was replaced (start time differs)")
    before = _stamp(bound.transcript)
    if before is None or bound.transcript is None:
        raise Refused("source transcript unreadable")
    try:
        active = inspect_transcript(bound.transcript).turn_active
    except BridgeError as exc:
        raise Refused(f"source transcript unreadable ({exc})") from exc
    if active is not False:
        raise Refused("source turn active (transcript)")
    if _stamp(bound.transcript) != before:
        raise Refused("source transcript moved while it was read")
    return before


def await_stable(adapter: ClaudeAdapter, session_id: str, bound: Bound) -> tuple[int, int, int]:
    """Idle proofs across :data:`STABLE_WINDOW_SEC` with ONE transcript identity throughout."""
    first = observe(adapter, session_id, bound)
    deadline = time.monotonic() + STABLE_WINDOW_SEC
    while True:
        _sleep(max(0.0, min(STABLE_POLL_SEC, deadline - time.monotonic())))
        if observe(adapter, session_id, bound) != first:
            raise Refused("source transcript changed during the stability window")
        if time.monotonic() >= deadline:
            return first


def _drain(bound: Bound, cfg: config.Config) -> None:
    """The close-now Stop-chain drain (barrier on only); :class:`Refused` unless drained."""
    from . import cli, terminal

    state, running = cli._wait_for_stop_drain(  # pylint: disable=protected-access
        bound.pid, bound.cwd, cfg.stop_barrier_wait_sec, cfg.stop_barrier_settle_sec
    )
    if state == terminal.DRAIN_RUNNING:
        names = ", ".join(cli._command_names(running))  # pylint: disable=protected-access
        raise Refused(f"Stop hooks still running: {names or 'unnamed hook process(es)'}")
    if state != terminal.DRAIN_DRAINED:
        raise Refused("the Stop chain could not be observed (ps unreadable or pid gone)")


def _revalidate(
    adapter: ClaudeAdapter, claim: Claim, bound: Bound, stamp: tuple[int, int, int]
) -> None:
    """Everything again, immediately before the SIGTERM; :class:`Refused` on any drift."""
    with Store() as store:
        reason = lineage_refusal(store, claim.transfer_id, claim.session_id)
        row = store.get(claim.session_id)
    if reason:
        raise Refused(reason)
    if row is None or row.close_token != claim.token or row.close_requested_at:
        raise Refused("the close token changed hands")
    if observe(adapter, claim.session_id, bound) != stamp:
        raise Refused("source transcript changed before the signal")


def _gone(pid: int, start: str, table: dict[int, Any]) -> bool:
    """True when the readable *table* no longer holds *pid* with start time *start*."""
    from . import terminal

    return bool(table) and (pid not in table or terminal.pid_start(pid) != start)


def _wait_exit(pid: int, start: str) -> bool:
    """Wait (≤ :data:`EXIT_WAIT_SEC`) for the signalled process to be gone."""
    from . import terminal

    deadline = time.monotonic() + EXIT_WAIT_SEC
    while True:
        if _gone(pid, start, terminal.ps_table()):
            return True
        if time.monotonic() >= deadline:
            return False
        _sleep(EXIT_POLL_SEC)


def pane_gone(debt: Debt) -> bool:
    """Positively gone: the process is gone AND no process holds the recorded tty."""
    from . import terminal

    table = terminal.ps_table()
    if not _gone(debt.pid, debt.start, table) or not debt.tty:
        return False
    return not any(terminal.pid_tty(pid, table) == debt.tty for pid in table)


def _restore(claim: Claim, bound: str) -> bool:
    """Hand this closer's own token back, pending (no TTL), bound to *bound*."""
    try:
        with Store() as store:
            return store.restore_close(
                claim.session_id, claim.token, armed_at=claim.armed_at, bound=bound
            )
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False


def _retire(claim: Claim) -> bool:
    try:
        with Store() as store:
            return store.retire_close(claim.session_id, claim.token)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False


def _refuse(claim: Claim | None, sid: str, tid: str, reason: str, *, keep: str = "") -> int:
    """Log a refusal; with a *claim*, hand its token back first (bound to *keep*)."""
    from . import cli

    restored = _restore(claim, keep) if claim is not None else False
    return cli._close_now_abort(  # pylint: disable=protected-access
        sid, f"handoff {tid}: {reason}", claim.armed_at if restored and claim else 0, notify=False
    )


def _finish(claim: Claim, debt: Debt, signalled: bool, closed: bool) -> int:
    """Retire on a proven close; else keep the debt (pane-only retry) and notify."""
    from . import cli, hooks

    sid, tid = claim.session_id, claim.transfer_id
    if signalled and (closed or pane_gone(debt)):
        _retire(claim)
        how = "closed" if closed else "pane/tab already gone"
        hooks._log_event(  # pylint: disable=protected-access
            sid, "close-now", f"handoff {tid}: source pid {debt.pid} reaped, {how}"
        )
        return 0
    restored = _restore(claim, debt.encode())
    what = "terminal close failed after SIGTERM" if signalled else "SIGTERM failed"
    return cli._close_now_abort(  # pylint: disable=protected-access
        sid,
        f"handoff {tid}: {what}",
        claim.armed_at if restored else 0,
        advice="close debt kept: a retry closes the pane without signalling, "
        "or close the tab yourself",
    )


def close_source(args: argparse.Namespace, claim: Claim, cfg: config.Config) -> int:
    """First run: bind, prove stable idle, drain, revalidate, SIGTERM, close the terminal."""
    from . import cli, terminal

    adapter = cli._adapter()  # pylint: disable=protected-access
    sid = claim.session_id
    try:
        entry = _live_entry(adapter, sid)
        start = _start_of(entry.pid)
        with Store() as store:
            row = store.get(sid)
        cwd = entry.cwd or (row.cwd if row else "") or ""
        transcript = adapter.transcript_path(cwd, sid, entry.config_dir or None)
        bound = Bound(entry.pid, start, cwd, transcript)
        stamp = await_stable(adapter, sid, bound)
        if cfg.stop_barrier_enabled:
            _drain(bound, cfg)
        debt = Debt(
            pid=bound.pid,
            start=start,
            pane=cli._tmux_pane_id(sid),  # pylint: disable=protected-access
            iterm=(getattr(args, "iterm", "") or "").strip()
            or ((row.iterm_session_id or "") if row else ""),
            tty=terminal.pid_tty(bound.pid, terminal.ps_table()),
        )
        _revalidate(adapter, claim, bound, stamp)
    except Refused as exc:
        return _refuse(claim, sid, claim.transfer_id, str(exc), keep=claim.bound)
    signalled = cli._sigterm(bound.pid)  # pylint: disable=protected-access
    if signalled:
        _wait_exit(bound.pid, start)
    closed = signalled and cli._close_terminal(  # pylint: disable=protected-access
        debt.pane, "" if debt.pane else debt.iterm
    )
    return _finish(claim, debt, signalled, closed)


def retry_terminal(claim: Claim, debt: Debt) -> int:
    """A kept debt: close the recorded pane/tab only — NEVER signal again."""
    from . import cli, terminal

    sid, tid = claim.session_id, claim.transfer_id
    table = terminal.ps_table()
    if not table:
        return _refuse(claim, sid, tid, "ps unreadable", keep=debt.encode())
    if not _gone(debt.pid, debt.start, table):
        reason = f"source pid {debt.pid} still running after SIGTERM — not re-signalled"
        return _refuse(claim, sid, tid, reason, keep=debt.encode())
    if debt.tty and any(
        terminal.pid_tty(pid, table) == debt.tty and terminal.pid_is_claude(pid, table)
        for pid in table
    ):
        _retire(claim)  # the tab was reused (a claude runs there again): never close it
        return _refuse(None, sid, tid, f"a live claude holds {debt.tty} — close debt dropped")
    closed = cli._close_terminal(  # pylint: disable=protected-access
        debt.pane, "" if debt.pane else debt.iterm
    )
    return _finish(claim, debt, True, closed)


def run(args: argparse.Namespace) -> int:
    """``close-now --handoff-transfer``: claim the source's close token, then close or refuse."""
    sid = str(getattr(args, "session", "") or "")
    tid = str(getattr(args, "handoff_transfer", "") or "")
    if not sid:
        return _refuse(None, "-", tid, "no --session given")
    cfg = config.load_config()
    with Store() as store:
        reason = lineage_refusal(store, tid, sid)
        row = store.get(sid)
        token = row.close_token if row is not None and not reason else ""
        bound = (row.close_bound or "") if row is not None else ""
        if not reason and not token:
            reason = "no close armed"
        armed_at = store.claim_close(sid, token) if not reason else 0
    if not reason and not armed_at:
        reason = "the close is held by another closer"
    if reason:
        return _refuse(None, sid, tid, reason)
    claim = Claim(sid, tid, token, armed_at, bound)
    debt = Debt.decode(bound)
    try:
        return retry_terminal(claim, debt) if debt else close_source(args, claim, cfg)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _refuse(claim, sid, tid, f"internal error ({exc})", keep=bound)
