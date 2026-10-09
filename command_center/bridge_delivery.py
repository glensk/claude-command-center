#!/usr/bin/env python3
"""The delivery state machine behind ``ccc delivery -i ID`` and ``ccc events``.

A delivery is one ``ccc send`` (or ``ccc answer``) row. States::

    sending ─► accepted(anchor) ─► completed | needs_input | failed | timed_out
        │          ▲
        └► unknown ┘  (a later matching record promotes it)
        └► failed     (nothing was typed)

- **Matching** (FIFO): every delivery of a session claims the FIRST record at or after
  its anchor offset whose normalised text sha256 matches and that no earlier delivery
  claimed — a ``user`` record (typed while idle), a ``queue-operation/enqueue`` (typed
  while busy) or an ``attachment/queued_command``. Two identical messages therefore
  claim two records in order; a prompt typed by hand in between (another sha) is
  ignored; ``<task-notification>`` enqueues are never a match.
- **Consumption**: a ``user`` / ``queued_command`` match is consumed where it stands; an
  ``enqueue`` is consumed by the first later unclaimed ``user`` / ``queued_command``
  record with the same sha (the queue draining it, or absorbing it mid-turn).
- **Terminal states**, each written once (CAS) together with its event:
  ``completed`` — the first turn end (an end-of-turn assistant record followed by
  ``system/stop_hook_summary`` or ``turn_duration``; or an interrupt) after the
  consumption; ``needs_input`` — an ``AskUserQuestion`` after the match that has no
  answer yet; ``failed`` — a StopFailure hook event after the delivery was created, the
  process gone from the registry, or the transcript replaced / gone; ``timed_out`` — no
  turn end within :data:`TIMEOUT_MS`. ``completed`` emits no event (successes are
  silent); ``needs_input`` → a ``needs_input`` event, ``failed`` / ``timed_out`` → a
  ``delivery_failed`` event.

The transcript is authoritative for completion and needs_input; the hooks only add the
StopFailure evidence.
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
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters import claude_bridge as cb
from .bridge_store import DELIVERY_STATES_OPEN, Delivery

if TYPE_CHECKING:
    from .bridge_target import BridgeDeps
    from .models import LiveSession
    from .store import Store

#: No turn end within this long after the delivery was created → ``timed_out``.
TIMEOUT_MS = 6 * 3600 * 1000
#: A row still ``sending`` this long after creation belongs to a crashed sender.
SENDING_STALE_MS = 60 * 1000

PROMPT_KINDS = (cb.KIND_USER, cb.KIND_ENQUEUE, cb.KIND_QUEUED)
CONSUME_KINDS = (cb.KIND_USER, cb.KIND_QUEUED)


@dataclasses.dataclass
class Assignment:
    """Where one delivery's message was seen in the transcript."""

    matched: cb.Located | None = None
    matched_kind: str = ""
    consumed: cb.Located | None = None


def _sha_matches(delivery: Delivery, text: str) -> bool:
    sha = cb.text_sha(text)
    return sha == delivery.content_sha or (bool(delivery.alt_sha) and sha == delivery.alt_sha)


def _first_unclaimed(
    located: list[cb.Located],
    delivery: Delivery,
    after: int,
    kinds: tuple[str, ...],
    claimed: set[int],
) -> tuple[cb.Located, str] | None:
    for loc in located:
        if loc.start < after or loc.start in claimed:
            continue
        found = cb.prompt_record(loc.record)
        if found is not None and found[0] in kinds and _sha_matches(delivery, found[1]):
            return loc, found[0]
    return None


def assign(deliveries: list[Delivery], located: list[cb.Located]) -> dict[str, Assignment]:
    """FIFO-assign transcript records to *deliveries* (one session, any states).

    Offsets already persisted on a delivery stay claimed by it; only records in
    *located* can be newly claimed.
    """
    by_start = {loc.start: loc for loc in located}
    claimed: set[int] = set()
    for d in deliveries:
        claimed.update(o for o in (d.matched_offset, d.consumed_offset) if o >= 0)
    out: dict[str, Assignment] = {}
    for d in deliveries:
        a = Assignment(
            matched=by_start.get(d.matched_offset) if d.matched_offset >= 0 else None,
            matched_kind=d.matched_kind,
            consumed=by_start.get(d.consumed_offset) if d.consumed_offset >= 0 else None,
        )
        has_match = d.matched_offset >= 0
        if not has_match and d.state in ("sending", "unknown"):
            hit = _first_unclaimed(located, d, d.anchor_offset, PROMPT_KINDS, claimed)
            if hit is not None:
                a.matched, a.matched_kind = hit
                claimed.add(hit[0].start)
                has_match = True
        if has_match and d.consumed_offset < 0 and a.consumed is None:
            if a.matched_kind in CONSUME_KINDS and a.matched is not None:
                a.consumed = a.matched
            elif a.matched_kind == cb.KIND_ENQUEUE:
                after = (a.matched.start if a.matched else d.matched_offset) + 1
                hit = _first_unclaimed(located, d, after, CONSUME_KINDS, claimed)
                if hit is not None:
                    a.consumed = hit[0]
                    claimed.add(hit[0].start)
        out[d.delivery_id] = a
    return out


@dataclasses.dataclass(frozen=True)
class Verdict:
    """What the transcript says about one accepted delivery right now."""

    state: str  # "" (still running) | completed | needs_input
    reason: str = ""
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)


def transcript_verdict(
    located: list[cb.Located], match_start: int, consume_start: int | None
) -> Verdict:
    """``completed`` / ``needs_input`` / still running, from records after the match.

    Asks are tracked from the match on (a question in the current turn blocks a queued
    prompt too); a turn end only counts after the consumption.
    """
    pending: list[str] = []
    saw_end_stop = False
    for loc in located:
        if loc.start <= match_start:
            continue
        rec = loc.record
        for block in cb.ask_tool_uses(rec):
            pending.append(str(block.get("id")))
        answered = cb.tool_result_ids(rec)
        if answered:
            pending = [p for p in pending if p not in answered]
        if consume_start is None or loc.start <= consume_start:
            continue
        if cb.is_interrupt(rec):
            return Verdict("completed", "interrupted")
        if cb.is_end_stop(rec):
            saw_end_stop = True
        elif cb.is_turn_end_marker(rec) and saw_end_stop:
            return Verdict("completed")
    if pending:
        return Verdict("needs_input", "ask_user_question", {"tool_use_id": pending[-1]})
    return Verdict("")


def terminal_event(
    d: Delivery, state: str, reason: str, now: int, detail: dict[str, Any]
) -> tuple[str, str, int, dict[str, Any], str | None] | None:
    if state == "completed":
        return None
    kind = "needs_input" if state == "needs_input" else "delivery_failed"
    body = {"delivery_id": d.delivery_id, "state": state, "reason": reason, **detail}
    return (kind, d.session_id, now, body, f"delivery:{d.delivery_id}:{state}")


def finish(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    d: Delivery,
    state: str,
    reason: str,
    now: int,
    columns: dict[str, Any] | None = None,
    **detail: Any,
) -> bool:
    """CAS *d* into terminal *state* with its event (False when someone else moved it).

    *columns* are extra row fields written in the same CAS; *detail* goes into the event.
    """
    return store.update_delivery(
        d.delivery_id,
        d.state,
        event=terminal_event(d, state, reason, now, detail),
        state=state,
        reason=reason,
        terminal_at=now,
        updated_at=now,
        **(columns or {}),
    )


def _read(d: Delivery, start: int) -> list[cb.Located] | str:
    """Records from *start*, or the failure reason (``transcript_missing`` / ``…replaced``)."""
    try:
        return cb.read_appended(Path(d.transcript_path), d.transcript_inode, start)
    except cb.TranscriptReplaced:
        return "transcript_replaced"
    except OSError:
        return "transcript_missing"


def advance_session(
    store: Store, session_id: str, now: int, live: dict[str, LiveSession] | None
) -> list[tuple[str, str]]:
    """Move every open delivery of *session_id* forward; ``[(delivery_id, new_state)]``.

    *live* maps session id → registry entry (``None`` = the registry could not be read:
    process death is then not judged this pass).
    """
    deliveries = store.deliveries_for(session_id)
    open_ = [d for d in deliveries if d.state in DELIVERY_STATES_OPEN]
    if not open_:
        return []
    changes: list[tuple[str, str]] = []
    start = min(d.anchor_offset for d in open_)
    # One read per transcript: the deliveries of one session share it (unless a
    # resume moved it to another account's tree — then each path is read separately).
    reads: dict[tuple[str, int], list[cb.Located] | str] = {}
    for d in open_:
        key = (d.transcript_path, d.transcript_inode)
        if key not in reads:
            reads[key] = _read(d, start)
    located_all: list[cb.Located] = []
    for value in reads.values():
        if isinstance(value, list):
            located_all.extend(value)
    assignments = assign(deliveries, located_all)
    entry = live.get(session_id) if live is not None else None
    for d in open_:
        new = _advance_one(store, d, assignments[d.delivery_id], reads, now, live, entry)
        if new:
            changes.append((d.delivery_id, new))
    return changes


def _advance_one(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-branches
    store: Store,
    d: Delivery,
    a: Assignment,
    reads: dict[tuple[str, int], list[cb.Located] | str],
    now: int,
    live: dict[str, LiveSession] | None,
    entry: LiveSession | None,
) -> str:
    read = reads[(d.transcript_path, d.transcript_inode)]
    if isinstance(read, str):
        return "failed" if finish(store, d, "failed", read, now) else ""
    if d.state == "sending":
        if now - d.created_at <= SENDING_STALE_MS:
            return ""  # the sender is still correlating; it records its own outcome
        if not store.update_delivery(
            d.delivery_id, "sending", state="unknown", reason="sender_vanished", updated_at=now
        ):
            return ""
        d = dataclasses.replace(d, state="unknown")
    if d.state == "unknown" and a.matched is not None:
        if not store.update_delivery(
            d.delivery_id,
            "unknown",
            state="accepted",
            outcome="accepted",
            matched_kind=a.matched_kind,
            matched_offset=a.matched.start,
            accepted_at=now,
            updated_at=now,
        ):
            return ""
        d = dataclasses.replace(
            d, state="accepted", matched_kind=a.matched_kind, matched_offset=a.matched.start
        )
        changes_state = "accepted"
    else:
        changes_state = ""
    if d.state == "accepted":
        if a.consumed is not None and d.consumed_offset < 0:
            if store.update_delivery(
                d.delivery_id, "accepted", consumed_offset=a.consumed.start, updated_at=now
            ):
                d = dataclasses.replace(d, consumed_offset=a.consumed.start)
        verdict = transcript_verdict(
            read, d.matched_offset, d.consumed_offset if d.consumed_offset >= 0 else None
        )
        if verdict.state:
            ok = finish(store, d, verdict.state, verdict.reason, now, **verdict.detail)
            return verdict.state if ok else changes_state
    if d.state not in ("accepted", "unknown"):
        return changes_state
    failures = store.stop_failures_since(d.session_id, d.created_at)
    if failures:
        error = str(failures[0].detail_obj().get("error") or "unknown")
        ok = finish(store, d, "failed", "stop_failure", now, error=error)
        return "failed" if ok else changes_state
    if live is not None and (entry is None or not entry.alive):
        ok = finish(store, d, "failed", "process_exited", now)
        return "failed" if ok else changes_state
    if now - d.created_at > TIMEOUT_MS:
        reason = "no_turn_end" if d.state == "accepted" else "unconfirmed"
        ok = finish(store, d, "timed_out", reason, now)
        return "timed_out" if ok else changes_state
    return changes_state


def live_map(deps: BridgeDeps) -> dict[str, LiveSession] | None:
    """Registry entries by session id, or ``None`` when the registry read failed."""
    try:
        return {s.session_id: s for s in deps.discover()}
    except Exception:  # pylint: disable=broad-exception-caught  # never block the scan
        return None


def advance_all(
    store: Store, deps: BridgeDeps, session_ids: Iterable[str] | None = None
) -> list[tuple[str, str]]:
    """Advance the open deliveries of *session_ids* (default: every session with one)."""
    ids = list(session_ids) if session_ids is not None else store.open_delivery_sessions()
    if not ids:
        return []
    live = live_map(deps)
    now = deps.now_ms()
    out: list[tuple[str, str]] = []
    for sid in ids:
        out.extend(advance_session(store, sid, now, live))
    return out


def public(d: Delivery) -> dict[str, Any]:
    """The ``ccc delivery -j`` data object of *d*."""
    from .bridge_json import iso

    return {
        "delivery_id": d.delivery_id,
        "session_id": d.session_id,
        "kind": d.kind,
        "state": d.state,
        "outcome": d.outcome or None,
        "channel": d.channel or None,
        "matched_kind": d.matched_kind or None,
        "reason": d.reason or None,
        "created_at": iso(d.created_at),
        "accepted_at": iso(d.accepted_at),
        "terminal_at": iso(d.terminal_at),
    }
