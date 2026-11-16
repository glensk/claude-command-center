#!/usr/bin/env python3
"""The input ledger: one ``inputs`` row for every text ccc types or launches into a session.

A consumer that reads a Claude transcript cannot tell a prompt ccc injected (a parked
prompt, an ``ccc await`` delivery, a job's first prompt, an auto-resume "continue") from
one a person typed: both are ``type: user`` records with ``origin.kind == "human"``. The
ledger closes that gap without ever storing the text:

- **Write-ahead.** :func:`record` inserts the row (state ``pending``) with the sha256 of
  the normalised text, the AppleScript one-line ``alt_sha`` and the transcript anchor
  BEFORE the caller types or execs, so a crash after the typing still leaves a row. It
  never raises: a store failure appends one line to the gap file
  (:func:`gap_path`, 0600) that makes every listing over that time incomplete.
- **Normalisation ``ccc-text-sha-v1``** (:data:`NORM`): ``claude_bridge.normalise``
  (NFC, CRLF/CR → LF, trailing whitespace per line dropped, outer strip), then sha256 hex
  of the UTF-8 bytes. A consumer reproduces a match from a line's text and from
  ``unwrap_pastes`` of it; a change of the rules is a new ``norm`` value.
- **Matching** (:func:`match_session`, pure): the ``pending`` rows of one transcript,
  FIFO, each claim the first unclaimed prompt record at or after their anchor whose text
  is theirs (exact, ``<pasted_content>``-unwrapped, the one-line form, or behind a stray
  prefix — the voice bridge's own rule, :func:`bridge_delivery.stray_prefix`); an
  ``enqueue`` is followed to the ``user`` / ``queued_command`` record consuming it, whose
  ``uuid`` the row keeps. :func:`advance_session` persists matches (CAS) and moves a row
  unmatched for :data:`MATCH_WINDOW_MS` to ``unmatched`` (final).
- **Coverage** lives in ``bridge_meta`` (:data:`META_COMPLETE_SINCE`,
  :data:`META_COVERAGE`); nothing here sets it — arming is an explicit operator step.
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
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import bridge_delivery as bd
from .adapters import claude_bridge as cb
from .bridge_store import Input

if TYPE_CHECKING:
    from .store import Store

#: The normalisation contract every ``content_sha`` / ``alt_sha`` of a row follows.
NORM = "ccc-text-sha-v1"
#: The set of ledgered injection paths this build covers (P1-P10 of tp#2170). A build that
#: ledgers a new path raises it, so an armed watermark of a lower coverage reads stale.
COVERAGE = 1
#: A row still unmatched this long after it was written → ``unmatched`` (final).
MATCH_WINDOW_MS = bd.TIMEOUT_MS
#: ``bridge_meta`` keys of the coverage watermark.
META_COMPLETE_SINCE = "inputs.complete_since_ms"
META_COVERAGE = "inputs.coverage"
#: The gap file's name under ``config.app_home()``.
GAP_FILE = "input-ledger-gaps.jsonl"

#: A zero-argument factory for a store usable as a context manager (``Store``).
StoreFactory = Callable[[], "Store"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def gap_path() -> Path:
    """Where a failed ledger write is noted (``app_home()/input-ledger-gaps.jsonl``)."""
    from .config import app_home  # pylint: disable=import-outside-toplevel

    return app_home() / GAP_FILE


# --------------------------------------------------------------------------- hashing


def content_sha(text: str) -> str:
    """sha256 hex of the ``ccc-text-sha-v1`` normal form of *text*."""
    return cb.text_sha(text)


def alt_sha(text: str) -> str:
    """The sha of the one-line form the AppleScript paste rung types, or ``""`` when that
    form normalises to the same text."""
    from .terminal import _one_line  # pylint: disable=import-outside-toplevel,protected-access

    one = _one_line(text)
    return "" if cb.normalise(one) == cb.normalise(text) else cb.text_sha(one)


def shas(text: str, *, paste: bool) -> tuple[str, str]:
    """``(content_sha, alt_sha)``; ``alt_sha`` is computed for a paste only (an exec'd or
    argv prompt never goes through the AppleScript rung)."""
    return content_sha(text), (alt_sha(text) if paste else "")


# --------------------------------------------------------------------------- the gap file


def append_gap(
    session_id: str,
    kind: str,
    error_class: str,
    *,
    at_ms: int | None = None,
    path: Path | None = None,
) -> bool:
    """Append one ``{"at_ms", "session_id", "kind", "error_class"}`` line (0600).

    False (and one stderr line, never the text) when even that fails.
    """
    target = path if path is not None else gap_path()
    line = json.dumps(
        {
            "at_ms": int(at_ms if at_ms is not None else _now_ms()),
            "session_id": session_id,
            "kind": kind,
            "error_class": error_class,
        },
        sort_keys=True,
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        print(
            f"input ledger: write failed ({error_class}) and the gap file is not writable "
            f"({type(exc).__name__}); kind={kind} session={session_id or '-'}",
            file=sys.stderr,
        )
        return False


@dataclasses.dataclass(frozen=True)
class Gap:
    """One gap-file entry: a ledger row that could not be written."""

    at_ms: int
    session_id: str
    kind: str
    error_class: str


def read_gaps(
    path: Path | None = None,
    *,
    session_id: str | None = None,
    since_ms: int = 0,
    until_ms: int | None = None,
) -> list[Gap]:
    """The gap entries with ``at_ms`` in ``[since_ms, until_ms)``.

    With *session_id*, only that session's entries and the unbound ones (empty session
    id: they may belong to any session). A missing file is no gap; a malformed line is
    skipped.
    """
    target = path if path is not None else gap_path()
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    out: list[Gap] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
            gap = Gap(
                int(obj.get("at_ms") or 0),
                str(obj.get("session_id") or ""),
                str(obj.get("kind") or ""),
                str(obj.get("error_class") or ""),
            )
        except (ValueError, TypeError, AttributeError):
            continue
        if gap.at_ms < since_ms or (until_ms is not None and gap.at_ms >= until_ms):
            continue
        if session_id is not None and gap.session_id not in ("", session_id):
            continue
        out.append(gap)
    return out


# --------------------------------------------------------------------------- writing


def record(  # pylint: disable=too-many-arguments
    store_factory: StoreFactory,
    *,
    session_id: str,
    kind: str,
    origin: str,
    text: str,
    config_dir: str,
    cwd: str,
    transcript: Path | None,
    paste: bool,
    delivery_id: str = "",
    now_ms: int | None = None,
    gaps: Path | None = None,
) -> str | None:
    """Write one ``pending`` row BEFORE *text* is typed or exec'd; its ``input_id``.

    *transcript* is the session's transcript; ``None`` with a *session_id* anchors on
    ``expected_transcript_path(config_dir, cwd, session_id)`` (a fresh session), without
    one the row is unbound and unanchored. A missing file is a fresh anchor (inode 0).
    Never raises: any failure appends a gap line (*gaps*, default :func:`gap_path`) and
    returns None — the injection must go on.
    """
    at = int(now_ms if now_ms is not None else _now_ms())
    try:
        content, alt = shas(text, paste=paste)
        path = transcript
        if path is None and session_id:
            path = cb.expected_transcript_path(config_dir, cwd, session_id)
        anchor = cb.Anchor.take_or_fresh(path) if path is not None else None
        row = Input(
            input_id=uuid.uuid4().hex,
            session_id=session_id,
            kind=kind,
            origin=origin,
            created_at=at,
            norm=NORM,
            content_sha=content,
            alt_sha=alt,
            config_dir=config_dir,
            cwd=cwd,
            transcript_path=str(path) if path is not None else "",
            transcript_inode=anchor.inode if anchor is not None else 0,
            anchor_offset=anchor.size if anchor is not None else 0,
            delivery_id=delivery_id,
            updated_at=at,
        )
        with store_factory() as store:
            store.insert_input(row)
        return row.input_id
    except Exception as exc:  # pylint: disable=broad-exception-caught  # never block a send
        append_gap(session_id, kind, type(exc).__name__, at_ms=at, path=gaps)
        return None


@dataclasses.dataclass(frozen=True)
class LedgerSpec:
    """What a choke point records for the text it is about to type or exec (D2, debate O5).

    ``kind`` / ``origin`` are the D2 row labels. ``session_id``, ``config_dir`` and
    ``cwd`` name the session an exec'd prompt goes to (:func:`record_exec`; ``""``
    session id = unbound row, D5); ``fresh`` = the exec starts that session, so the anchor
    is ``expected_transcript_path`` rather than the found transcript. A paste
    (``bridge_target.guarded_paste``) reads ``kind`` / ``origin`` only: it records against
    the session it just revalidated.
    """

    kind: str
    origin: str
    session_id: str = ""
    config_dir: str = ""
    cwd: str = ""
    fresh: bool = False


#: Reason codes of a ``failed`` row written by a choke point (never text).
REASON_NOT_TYPED = "not_typed"
REASON_EXEC_FAILED = "exec_failed"


def _session_transcript(spec: LedgerSpec) -> Path | None:
    """The existing transcript of *spec*'s session, or ``None`` (then :func:`record`
    anchors on ``expected_transcript_path``)."""
    from .adapters.claude import ClaudeAdapter  # pylint: disable=import-outside-toplevel

    return ClaudeAdapter().transcript_path(spec.cwd, spec.session_id, spec.config_dir or None)


def record_exec(
    store_factory: StoreFactory, spec: LedgerSpec, text: str, *, now_ms: int | None = None
) -> str | None:
    """Write the ``pending`` row of a prompt about to be exec'd on claude's stdin (P5/P7/P8).

    ``spec.fresh`` anchors on ``expected_transcript_path`` (the new session's file, inode
    0); otherwise ``take_or_fresh`` on the session's found transcript. Never raises.
    """
    transcript: Path | None = None
    if spec.session_id and not spec.fresh:
        try:
            transcript = _session_transcript(spec)
        except Exception:  # pylint: disable=broad-exception-caught  # fall back to the expected path
            transcript = None
    return record(
        store_factory,
        session_id=spec.session_id,
        kind=spec.kind,
        origin=spec.origin,
        text=text,
        config_dir=spec.config_dir,
        cwd=spec.cwd,
        transcript=transcript,
        paste=False,
        now_ms=now_ms,
    )


def mark_failed(
    store_factory: StoreFactory, input_id: str | None, reason: str, *, now_ms: int | None = None
) -> bool:
    """``pending`` → ``failed`` with a reason code (nothing was typed). Never raises; a
    row that cannot be moved stays ``pending`` (fail closed: its window reads incomplete)."""
    if not input_id:
        return False
    at = int(now_ms if now_ms is not None else _now_ms())
    try:
        with store_factory() as store:
            return store.update_input(
                input_id, "pending", state="failed", reason=reason, updated_at=at
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught  # bookkeeping only
        print(f"input ledger: mark_failed {input_id}: {type(exc).__name__}", file=sys.stderr)
        return False


def mark_matched(
    store_factory: StoreFactory, input_id: str | None, match: Match, *, now_ms: int | None = None
) -> bool:
    """``pending`` → ``matched`` with *match* (a caller that correlates itself, e.g.
    ``ccc send``). Never raises."""
    if not input_id:
        return False
    at = int(now_ms if now_ms is not None else _now_ms())
    try:
        with store_factory() as store:
            return store.update_input(input_id, "pending", **_matched_changes(match, at))
    except Exception as exc:  # pylint: disable=broad-exception-caught  # bookkeeping only
        print(f"input ledger: mark_matched {input_id}: {type(exc).__name__}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- matching


@dataclasses.dataclass(frozen=True)
class Match:
    """Where a row's text was seen: the consuming record (the one carrying a ``uuid``)."""

    line_offset: int
    line_uuid: str
    line_kind: str
    stray_len: int = 0


def _matched_changes(match: Match, at: int) -> dict[str, Any]:
    return {
        "state": "matched",
        "line_offset": match.line_offset,
        "line_uuid": match.line_uuid,
        "line_kind": match.line_kind,
        "stray_len": match.stray_len,
        "matched_at": at,
        "updated_at": at,
    }


def _stray_len(row: Input, loc: cb.Located) -> int:
    """The stray-prefix length of a record the matcher already accepted for *row*."""
    found = cb.prompt_record(loc.record)
    head = bd.stray_prefix(row, found[1]) if found is not None else None
    return len(head or "")


def match_session(rows: Iterable[Input], located: list[cb.Located]) -> dict[str, Match]:
    """FIFO-assign the ``pending`` *rows* of ONE transcript to records of *located*.

    Pure. Rows are taken in the order given, stably sorted by ``created_at`` (pass them
    in rowid order). Offsets already persisted on a row stay claimed. A row whose match is
    an ``enqueue`` whose consumer has not been written yet holds the enqueue for this
    pass but is not returned (no ``uuid`` yet).
    """
    ordered = sorted(rows, key=lambda r: r.created_at)
    claimed = {r.line_offset for r in ordered if r.line_offset >= 0}
    out: dict[str, Match] = {}
    for row in ordered:
        if row.state != "pending" or not row.content_sha:
            continue
        hit = bd.first_unclaimed(located, row, row.anchor_offset, bd.PROMPT_KINDS, claimed)
        if hit is None:
            continue
        loc, kind = hit
        claimed.add(loc.start)
        if kind == cb.KIND_ENQUEUE:
            consumer = bd.first_unclaimed(located, row, loc.start + 1, bd.CONSUME_KINDS, claimed)
            if consumer is None:
                continue
            loc, kind = consumer
            claimed.add(loc.start)
        out[row.input_id] = Match(
            loc.start, str(loc.record.get("uuid") or ""), kind, _stray_len(row, loc)
        )
    return out


def _read_group(path: str, rows: list[Input]) -> tuple[list[Input], list[cb.Located]]:
    """The rows of one transcript that can still match, and its records from their
    earliest anchor. A row anchored on an inode the file no longer has cannot match."""
    try:
        inode = os.stat(path).st_ino
    except OSError:
        return [], []
    live = [r for r in rows if r.transcript_inode in (0, inode)]
    anchors = [r.anchor_offset for r in live if r.state == "pending"]
    if not anchors:
        return [], []
    try:
        located = cb.read_appended(Path(path), 0, min(anchors))
    except OSError:
        return [], []
    return live, located


def advance_session(store: Store, session_id: str, now_ms: int) -> list[tuple[str, str]]:
    """Persist the matches of *session_id*'s ``pending`` rows; ``[(input_id, new_state)]``.

    Each transcript is read once from the earliest pending anchor. A match is written by
    CAS (``pending`` → ``matched``); a row still unmatched :data:`MATCH_WINDOW_MS` after
    it was written becomes ``unmatched``. Unbound rows (``session_id == ''``) can only
    expire.
    """
    rows = store.inputs_for(session_id, 0, 2**62)
    if not any(r.state == "pending" for r in rows):
        return []
    by_path: dict[str, list[Input]] = {}
    for row in rows:
        if row.transcript_path:
            by_path.setdefault(row.transcript_path, []).append(row)
    matches: dict[str, Match] = {}
    for path, group in by_path.items():
        if not any(r.state == "pending" for r in group):
            continue
        live, located = _read_group(path, group)
        if live:
            matches.update(match_session(live, located))
    changes: list[tuple[str, str]] = []
    for row in rows:
        if row.state != "pending":
            continue
        match = matches.get(row.input_id)
        if match is not None:
            if store.update_input(row.input_id, "pending", **_matched_changes(match, now_ms)):
                changes.append((row.input_id, "matched"))
        elif now_ms - row.created_at >= MATCH_WINDOW_MS:
            if store.update_input(
                row.input_id,
                "pending",
                state="unmatched",
                reason="match_window",
                updated_at=now_ms,
            ):
                changes.append((row.input_id, "unmatched"))
    return changes
