#!/usr/bin/env python3
"""``ccc send -s ID -j`` — type a message into a live Claude Code tab and correlate it.

The message comes on stdin only (never argv: an argv message is readable by every ``ps``
on the machine), at most 4000 characters, with no C0/C1 control characters except
``\\n`` and ``\\t`` — ESC (and so ``ESC[201~``, the end of a bracketed paste), NUL and
CR are rejected.

Inside the per-tab mutation lock and immediately before the first byte, the target is
revalidated (:func:`bridge_target.revalidate`: registry pid/tty == tab tty, the tab's
foreground job is that claude, fresh raw status mapped to §6 — a registry ``shell``, a
shell tool running, is ``busy``) and the transcript must show no pending picker.
``idle`` expects a ``user`` record, ``busy`` a ``queue-operation/enqueue`` (the
expectation is recorded; any of the three prompt shapes with the right sha proves receipt,
since the session may turn busy between the check and the paste);
``waiting`` / ``blocked`` / any other status, background sessions and a pending picker
are refused.

Correlation: the transcript's inode + byte size are taken before sending; only records
appended after that are scanned (a FRESH session has no transcript until its first
prompt — spike S-SEND — so its anchor is ``(inode 0, size 0)`` on the path Claude Code
will create, :func:`claude_bridge.expected_transcript_path`, and the file appearing with
any inode is accepted), and the first unclaimed record whose normalised text
sha256 matches is this delivery's (FIFO across the session's deliveries — no marker is
ever added to the text). The outcome — ``accepted`` / ``failed`` (nothing typed) /
``unknown`` (typed, but no matching record within :data:`CORRELATE_SEC`, or a paste
whose submitting CR did not go out) — is known within 8 s and never auto-retried. Every
send leaves a ``deliveries`` row the state machine (``ccc delivery``) follows up.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters import claude_bridge as cb
from .bridge_delivery import assign, finish
from .bridge_json import BridgeError, validate_message
from .bridge_store import Delivery
from .bridge_target import require_live, revalidate, tab_for, tab_lock

if TYPE_CHECKING:
    from .bridge_target import BridgeDeps

#: The send-time correlation window (seconds from the first byte).
CORRELATE_SEC = 8.0
#: Poll interval of the correlation scan.
POLL_SEC = 0.1
#: §6 status (``Target.status``) → the record kind a delivered prompt must produce.
EXPECTED_BY_STATUS = {"idle": cb.KIND_USER, "busy": cb.KIND_ENQUEUE}
#: Refusal code per §6 status that is never typed into.
REFUSED_STATUS = {"waiting": "waiting", "blocked": "blocked"}


def transcript_of(deps: BridgeDeps, session_id: str, cwd: str, config_dir: str) -> Path:
    """The session's transcript (multi-account resolution), or a refusal."""
    path = deps.transcript(cwd, session_id, config_dir)
    if path is None or not path.is_file():
        raise BridgeError("transcript_missing", f"no transcript found for session {session_id}")
    return path


def send_transcript(
    deps: BridgeDeps, session_id: str, cwd: str, config_dir: str
) -> tuple[Path, bool]:
    """``(path, fresh)``: the transcript, or — for a session that has not been prompted
    yet — the path Claude Code will create (``fresh`` = it does not exist yet)."""
    path = deps.transcript(cwd, session_id, config_dir)
    if path is not None and path.is_file():
        return path, False
    expected = cb.expected_transcript_path(config_dir, cwd, session_id)
    return expected, not expected.is_file()


def inspect_transcript(path: Path) -> cb.Inspection:
    """Parse the transcript (``transcript_unknown`` refusal when it does not parse)."""
    try:
        return cb.inspect(cb.load_records(path))
    except cb.TranscriptUnknown as exc:
        raise BridgeError("transcript_unknown", f"transcript does not parse: {exc}") from exc
    except OSError as exc:
        raise BridgeError("transcript_missing", f"transcript unreadable: {exc}") from exc


def _alt_sha(text: str) -> str:
    """The sha of the one-line form the AppleScript rung types (``""`` when identical)."""
    from .terminal import _one_line

    flat = _one_line(text)
    return cb.text_sha(flat) if cb.normalise(flat) != cb.normalise(text) else ""


def _inode(path: Path) -> int:
    """The inode a fresh-session transcript appeared with (0 when it is gone again)."""
    try:
        return path.stat().st_ino
    except OSError:
        return 0


def _correlate(deps: BridgeDeps, d: Delivery, deadline: float) -> tuple[cb.Located, str] | None:
    """Poll the appended records until this delivery's record shows up (or *deadline*)."""
    path = Path(d.transcript_path)
    while True:
        try:
            located = cb.read_appended(path, d.transcript_inode, d.anchor_offset)
        except (cb.TranscriptReplaced, OSError):
            return None
        with deps.store() as store:
            deliveries = store.deliveries_for(d.session_id)
        hit = assign(deliveries, located).get(d.delivery_id)
        if hit is not None and hit.matched is not None:
            return hit.matched, hit.matched_kind
        if deps.monotonic() >= deadline:
            return None
        deps.sleep(POLL_SEC)


def run_send(deps: BridgeDeps, session_id: str, text: str) -> dict[str, Any]:
    """Deliver *text*; the §6 ``data`` object (raises :class:`BridgeError` on refusal).

    A delivery that was attempted but not accepted raises with ``data`` attached, so
    the caller still gets the ``delivery_id`` to follow up.
    """
    validate_message(text)
    live = require_live(deps, session_id)
    tab = tab_for(deps, live)
    with tab_lock(deps, tab):
        target = revalidate(deps, session_id, tab)
        path, fresh = send_transcript(deps, session_id, target.live.cwd, target.live.config_dir)
        ins = None if fresh else inspect_transcript(path)
        if ins is not None and ins.pending:
            raise BridgeError(
                "picker_pending", "the session shows a question picker — use `ccc answer`"
            )
        status = target.status
        if status in REFUSED_STATUS:
            raise BridgeError(REFUSED_STATUS[status], f"session {session_id} is {status}")
        expected = EXPECTED_BY_STATUS.get(status)
        if expected is None:
            raise BridgeError(
                "unknown_status",
                f"session {session_id} reports status {target.raw_status!r}",
            )
        anchor = cb.Anchor.take_or_fresh(path)
        now = deps.now_ms()
        d = Delivery(
            delivery_id=uuid.uuid4().hex,
            session_id=session_id,
            kind="send",
            iterm_session_id=tab,
            config_dir=live.config_dir,
            pid=target.live.pid,
            transcript_path=str(path),
            transcript_inode=anchor.inode,
            anchor_offset=anchor.size,
            content_sha=cb.text_sha(text),
            alt_sha=_alt_sha(text),
            expected=expected,
            status_at_send=status,
            state="sending",
            created_at=now,
            updated_at=now,
        )
        with deps.store() as store:
            store.insert_delivery(d)
        deadline = deps.monotonic() + CORRELATE_SEC
        channel, sent = deps.send_text(tab, text)
        data: dict[str, Any] = {
            "delivery_id": d.delivery_id,
            "outcome": "failed",
            "channel": channel or None,
        }
        if sent == "none":
            with deps.store() as store:
                finish(
                    store,
                    d,
                    "failed",
                    "nothing_sent",
                    deps.now_ms(),
                    columns={"outcome": "failed", "channel": channel},
                )
            raise BridgeError("delivery_failed", "nothing reached the tab", data=data)
        hit = _correlate(deps, d, deadline)
        done = deps.now_ms()
        with deps.store() as store:
            if hit is not None:
                loc, kind = hit
                store.update_delivery(
                    d.delivery_id,
                    "sending",
                    # a fresh-session anchor (inode 0) is pinned to the file that appeared
                    transcript_inode=d.transcript_inode or _inode(path),
                    state="accepted",
                    outcome="accepted",
                    channel=channel,
                    matched_kind=kind,
                    matched_offset=loc.start,
                    accepted_at=done,
                    updated_at=done,
                )
                data["outcome"] = "accepted"
                return data
            reason = "partial_paste" if sent == "partial" else "no_matching_record"
            store.update_delivery(
                d.delivery_id,
                "sending",
                state="unknown",
                outcome="unknown",
                channel=channel,
                reason=reason,
                updated_at=done,
            )
        data["outcome"] = "unknown"
        raise BridgeError(
            "delivery_unknown",
            f"no matching transcript record within {CORRELATE_SEC:g} s ({reason})",
            data=data,
        )
