#!/usr/bin/env python3
"""The hook side of ccc's shared thread rows (tp#2372 F3b, Designs 4 and 7).

Each function here is called by one hook handler, opens the shared store only when
``shared_store_path`` is set (else returns at once, touching no file) and never raises:
a shared-store failure is logged (``shared_store.log_line``) and the hook goes on.

SessionStart, per ``source`` (what the installed Claude Code mints, docs/SPEC.md):

- ``startup`` (or none): generation 0 of its own thread, unless a relay reservation
  targets it (a ``new-job --thread-from`` row or an inbound ``continue-session``
  transfer): the join writes its row then.
- ``resume`` with a known id: a new process incarnation (``ended_at`` cleared).
- ``resume`` with an unknown id (``--fork-session``) and ``fork`` (``/branch``): a new
  thread, ``forked_from`` = the origin's thread when ccc can name it, holding nothing.
- ``clear``: a NEW id; it joins as the next generation of the outgoing session that the
  same process's SessionEnd(``clear``) marked. ``compact`` keeps the id: nothing.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

import sqlite3
from typing import TYPE_CHECKING

from . import config, shared_store

if TYPE_CHECKING:
    from .store import Store


def _open(
    store: Store, *, busy_ms: int = shared_store.LIFECYCLE_BUSY_MS
) -> shared_store.SharedStore | None:
    try:
        cfg = config.load_config()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None
    if shared_store.configured_path(cfg) is None:
        return None
    return shared_store.open_shared(store.conn, cfg=cfg, busy_ms=busy_ms)


def _relay_pending(store: Store, sid: str) -> bool:
    """True when a relay join will write *sid*'s row (draft thread source or transfer)."""
    if store.thread_join_for(sid) is not None:
        return True
    inbound, corrupt = store.inbound_transfers(sid)
    return bool(inbound or corrupt)


def _origin_thread(shared: shared_store.SharedStore, store: Store, sid: str) -> str | None:
    """The thread a fork of *sid* came from: its copied ``handed_off_from``'s thread."""
    row = store.conn.execute(
        "SELECT handed_off_from FROM sessions WHERE session_id = ?", (sid,)
    ).fetchone()
    parent = str(row[0] or "") if row is not None else ""
    return shared.thread_of(parent) if parent else None


def session_start(store: Store, sid: str, source: object, cwd: str = "") -> None:
    """SessionStart: write *sid*'s lifecycle row per *source* (see the module doc)."""
    try:
        shared = _open(store)
        if shared is None:
            return
        with shared:
            _session_start(shared, store, sid, str(source or ""), cwd)
    except (sqlite3.Error, OSError) as exc:
        shared_store.log_line(f"session-start {sid}: {type(exc).__name__}: {exc}")


def _session_start(
    shared: shared_store.SharedStore, store: Store, sid: str, source: str, cwd: str
) -> None:
    now = shared_store.now_ms()
    proc = shared_store.process_key()
    known = shared.session_row(sid) is not None
    if source == "compact":
        if not known:
            shared.start_root(sid, now, cwd=cwd)  # self-heal a start ccc missed
        return
    if source == "clear":
        prev = shared.take_clear(sid, proc, shared_store.tab_key(), now)
        if prev and not known:
            res = shared.reserve(prev, sid, "clear", now)
            if res is not None and res.status == "reserved" and res.op is not None:
                if shared.commit(res.op.op_id, now, cwd=cwd) == "accepted":
                    shared.end(prev, now, "handed_off")
                    shared.map_incarnation(sid, proc, 0, now)
                    return
            shared_store.log_line(f"clear {prev} -> {sid}: no join ({res})")
        if shared.session_row(sid) is None:
            shared.start_root(sid, now, cwd=cwd)
            shared.map_incarnation(sid, proc, 0, now)
        return
    if source == "resume" and known:
        shared.resume(sid, now, proc)
        return
    if source in ("resume", "fork"):
        shared.start_root(sid, now, cwd=cwd, forked_from=_origin_thread(shared, store, sid))
        shared.map_incarnation(sid, proc, 0, now)
        return
    if known or _relay_pending(store, sid):
        return
    if shared.start_root(sid, now, cwd=cwd):
        shared.map_incarnation(sid, proc, 0, now)


def session_end(store: Store, sid: str, reason: object, *, done: bool) -> None:
    """SessionEnd: ``ended_at`` once per incarnation; a ``clear`` end marks the hand-over."""
    try:
        shared = _open(store)
        if shared is None:
            return
        with shared:
            now = shared_store.now_ms()
            proc = shared_store.process_key()
            if str(reason or "") == "clear":
                shared.mark_clear(sid, proc, shared_store.tab_key(), now)
                return
            shared.end(sid, now, "done" if done else "closed", proc=proc)
    except (sqlite3.Error, OSError) as exc:
        shared_store.log_line(f"session-end {sid}: {type(exc).__name__}: {exc}")


def beat(store: Store, sid: str) -> None:
    """Stop / UserPromptSubmit / PostToolUse / statusline: the throttled ``last_beat``."""
    try:
        shared = _open(store, busy_ms=shared_store.BEAT_BUSY_MS)
        if shared is None:
            return
        with shared:
            shared.beat(sid, shared_store.now_ms())
    except (sqlite3.Error, OSError):
        pass  # advisory: a beat never fails a hook


def relay_reserve(store: Store, source: str, target: str, op_id: str, *, staff: bool) -> None:
    """``new-job --thread-from``: the shared reservation (Design 3 step 1), op id shared."""
    try:
        shared = _open(store)
        if shared is None:
            return
        with shared:
            kind = "staff_accept" if staff else "park_start"
            res = shared.reserve(source, target, kind, shared_store.now_ms(), op_id=op_id)
            if res is not None and res.status == "lost":
                won = res.winner
                who = f"{won.target_session} (op {won.op_id})" if won else "?"
                shared_store.log_line(f"reserve {source} -> {target}: lost to {who}")
    except (sqlite3.Error, OSError) as exc:
        shared_store.log_line(f"reserve {source} -> {target}: {type(exc).__name__}: {exc}")


def relay_commit(  # pylint: disable=too-many-arguments
    store: Store, source: str, target: str, op_id: str, *, staff: bool, cwd: str = ""
) -> str | None:
    """Publish a relay join (steps 2-3); reserves first when the reservation is missing.

    The outcome word of :meth:`SharedStore.commit`, ``None`` when off or failed. A lost
    reservation leaves *target* a root of its own thread (a fork sibling, zero grants).
    """
    try:
        shared = _open(store)
        if shared is None:
            return None
        with shared:
            now = shared_store.now_ms()
            if shared.op(op_id) is None:
                kind = "staff_accept" if staff else "park_start"
                res = shared.reserve(source, target, kind, now, op_id=op_id)
                if res is None or res.status == "lost":
                    shared.start_root(target, now, cwd=cwd, forked_from=shared.thread_of(source))
                    return "lost"
            shared.mark_committing(op_id, now)
            outcome = shared.commit(op_id, now, cwd=cwd)
            if outcome in ("lost", "quarantined") and shared.session_row(target) is None:
                shared.start_root(target, now, cwd=cwd, forked_from=shared.thread_of(source))
            return outcome
    except (sqlite3.Error, OSError) as exc:
        shared_store.log_line(f"commit {op_id}: {type(exc).__name__}: {exc}")
        return None


def relay_cancel(store: Store, op_id: str) -> str | None:
    """``thread-handback``: cancel a still pending reservation; the op's state after."""
    try:
        shared = _open(store)
        if shared is None:
            return None
        with shared:
            shared.settle(op_id, "cancelled", shared_store.now_ms())
            op = shared.op(op_id)
            return None if op is None else op.state
    except (sqlite3.Error, OSError) as exc:
        shared_store.log_line(f"cancel {op_id}: {type(exc).__name__}: {exc}")
        return None
