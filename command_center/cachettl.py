#!/usr/bin/env python3
"""Prompt-cache TTL countdown for the ``model`` column (TUI + ``ccc ls``).

Anthropic's prompt cache is a *prefix* cache that expires ``TTL`` seconds after the
**last** request, not after the session started: every request re-reads the prefix and
refreshes the TTL, so an actively-used session never goes cold. Once it *does* expire the
next message pays a full cache re-write — on ANY account (the cache is per-account, so
switching cpriv↔cwork then costs the same as continuing). Surfacing the remaining warm
time lets you decide whether a parked session is still cheap to resume.

"Last request" has no first-class signal, so we anchor the clock on the session's last
real cache request: the **timestamp of the newest main-chain assistant transcript entry that
read or wrote the prompt cache** (:func:`is_cache_anchor`: a real model response whose
``message.usage`` carries a positive ``cache_read_input_tokens`` or
``cache_creation_input_tokens``). Refused, synthetic or zero-cache replies never count — a
usage-limit refusal is written as an ``isApiErrorMessage`` row with model ``<synthetic>``
and zero usage, and anchoring on it once showed a 30-hour-cold cache as warm (tp#2243).
Assistant entries with ``"isSidechain":true`` are EXCLUDED too — subagent traffic refreshes
a *different* cache prefix, not the main session's. The predicates mirror the hooks'
``cc_warm.py`` and decide's; ``tests/fixtures/warm_anchor_cases.jsonl`` is the shared case
list every implementation asserts.

**Another seat's cache is cold.** The prompt cache is per account, so an anchor written on
one seat (say work) does not warm a tab now running on the other (private). The anchor's
seat is the ``ownerAccountUuid`` of the newest ``bridge-session`` row at or before it, else
the ``accountUuid`` of the newest ``session-owner`` row, read at most
:data:`_SEAT_SCAN_BYTES` back from the anchor; the running seat is
:func:`accounts.account_uuid` of the session's ``config_dir`` (the account it last ran
under). With more than one account configured the cell is warm only when both seats are
known and equal; a different or unknown seat reads ``❄ cold`` (fail closed). A
single-account install skips the check.

The transcript **mtime is only a fast path, never the anchor.** Claude Code appends LOCAL
entries on session open — ``mode``, ``permission-mode``, ``bridge-session``,
``attachment``, ``last-prompt``, ``file-history-snapshot`` — with NO API call, so a
freshly resumed but days-old session has a warm-looking mtime yet a cold cache. We only
trust mtime to prove *coldness*: if the file itself is already older than the TTL then
every entry (≤ mtime) is expired, so we render ``❄ cold`` without opening the file. When
mtime is fresh we read the transcript tail *backwards* to find the true anchor; a fresh
mtime with no qualifying assistant entry (only local entries) is still cold.

This mirrors the user's statusline (``statusline-command.sh`` "Prompt-cache TTL
countdown" block); the thresholds/colours here are authoritative:

* remaining ≥ 1200 s → ``♨ M:SS`` **green**
* 600 ≤ remaining < 1200 s → ``♨ M:SS`` **orange**
* 0 < remaining < 600 s → ``♨ M:SS`` **red**
* remaining ≤ 0 → ``❄ cold`` **red**
* no transcript (missing / draft / done row) → empty cell

A **busy** row is the exception: a session that is mid-turn re-warms its cache on every
request, so its countdown just idles near the full TTL and answers a question nobody is
asking (you are not deciding whether to resume a session that is already running). Busy
rows therefore read ``▶`` (level ``"running"``) — the countdown is reserved for the rows
that are waiting on you (or parked), where "is this still cheap to resume?" is the
actual question. See :func:`countdown_for`'s *running* flag.

:func:`countdown` is a pure function of ``(anchor, now)`` — it returns a ``(text, level)``
pair where ``level`` is a colour-agnostic name (``"green"``/``"orange"``/``"red"``/``""``).
Each view maps that level to its own palette (``ccc ls`` uses xterm-256 codes, the TUI uses
Rich style strings) so the two stay pixel-identical without this module knowing either
colour system.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from . import accounts

if TYPE_CHECKING:
    from .models import Session

# TTL default and its env override. The cache is nominally 1 h (drops to 5 m under a usage
# overage); ``CC_CACHE_TTL_S`` overrides it. Junk (non-integer, zero, negative) → the
# default, so a stray shell value never breaks the column.
_ENV_TTL = "CC_CACHE_TTL_S"
_DEFAULT_TTL_S = 3600

# Colour boundaries (seconds remaining). ``>=`` semantics: 1200 is green, 600 is orange.
_GREEN_MIN_S = 1200
_ORANGE_MIN_S = 600

# Warm/cold glyphs (kept identical to the statusline's ♨ / ❄).
_WARM_GLYPH = "♨"
_COLD_TEXT = "❄ cold"

# Busy-row cell: the WORKING status glyph, with its own colour level so each view can paint
# it like its ▶ status icon (models.STATUS_ICON[Status.WORKING] — kept in sync by
# test_cachettl.py rather than imported, to keep this module free of a models import).
_RUNNING_GLYPH = "▶"
_RUNNING_LEVEL = "running"

# The cell is FIXED-WIDTH: every row reserves the same space so the account glyph and the
# model·effort text after it start at the identical column, whatever this cell says (or does
# not say — an empty cell pads to a full-width blank). 8 = the widest content, ``♨ 59:26``
# (7), plus the 1-space separator. Longer content (a custom CC_CACHE_TTL_S past 99 min) is
# never truncated — it keeps the bare separator space and that one row runs wide.
# ``len()`` measures it because every glyph this cell can hold (♨ ❄ ▶, digits, ':') is a
# single-cell code point — ``test_cachettl.py`` pins that against ``rich.cells.cell_len``.
CELL_WIDTH = 8

# Backwards-read chunk sizing. We scan the transcript tail from EOF toward the start in
# chunks, doubling each step (capped) until a qualifying assistant line is found or the
# file start is reached — so a multi-MB transcript is never loaded whole just to read its
# newest turn.
_CHUNK_BYTES = 64 * 1024
_MAX_CHUNK_BYTES = 1024 * 1024

# Cheap substring prefilters (bytes) applied before any JSON parse. Claude Code writes
# compact JSON (no spaces after colons), so these literals match verbatim. A line without
# the assistant mark can never anchor; one with it is parsed and judged by
# :func:`is_cache_anchor` (an unparsable line never anchors: its usage cannot be checked).
_ASSISTANT_MARK = b'"type":"assistant"'
_BRIDGE_MARK = b'"type":"bridge-session"'
_OWNER_MARK = b'"type":"session-owner"'

# The model name Claude Code stamps on rows it wrote itself (refusals, API errors).
_SYNTHETIC_MODEL = "<synthetic>"

# The anchor-seat back-scan reads at most this many bytes before the anchor line; past it
# (or at the file start) without a seat row the anchor's seat is unknown.
_SEAT_SCAN_BYTES = 256 * 1024

# A scanned anchor: its epoch seconds and the ``accountUuid`` of the seat that wrote it
# (``None`` = unknown).
_Anchor = tuple[float, str | None]

# Module-level anchor read-cache: ``{path: ((st_mtime_ns, st_size), anchor_or_None)}``. The
# TUI renders per row per refresh and the daemon is long-lived, so an unchanged transcript
# must be read at most once (same rationale as ``sessioncard.VIEW_CACHE``). Re-read when the
# mtime OR the size moved (a same-tick append, e.g. a seat switch, keeps the float mtime).
# The fast path (mtime expired) neither consults nor populates this — it is pure stat math.
_ANCHOR_CACHE: dict[Path, tuple[tuple[int, int], _Anchor | None]] = {}


def cache_ttl_seconds() -> int:
    """The prompt-cache TTL in seconds — ``CC_CACHE_TTL_S`` when a sane positive int, else 3600.

    Junk values (non-numeric, empty, zero, negative) fall back to :data:`_DEFAULT_TTL_S`
    rather than raising, so a misconfigured env var degrades quietly.
    """
    raw = os.environ.get(_ENV_TTL)
    if raw is None:
        return _DEFAULT_TTL_S
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL_S
    return value if value > 0 else _DEFAULT_TTL_S


def countdown(anchor: float | None, now: float, *, ttl: int | None = None) -> tuple[str, str]:
    """The ``(text, level)`` cache-TTL cell for a cache last warmed at *anchor*.

    *anchor* is the epoch-seconds timestamp of the session's last real API interaction (its
    newest main-chain assistant entry), *now* the current epoch seconds; *ttl* overrides
    :func:`cache_ttl_seconds` (tests pass it explicitly). ``level`` is a colour name —
    ``"green"`` / ``"orange"`` / ``"red"`` — that the caller maps to its own palette. A
    ``None`` *anchor* (no transcript) yields ``("", "")`` — an empty cell. Warm rows read
    ``♨ M:SS`` (whole minutes, zero-padded seconds); an expired cache reads ``❄ cold``.
    """
    if anchor is None:
        return ("", "")
    ttl_s = cache_ttl_seconds() if ttl is None else ttl
    remaining = int(ttl_s - (now - anchor))
    if remaining <= 0:
        return (_COLD_TEXT, "red")
    minutes, seconds = divmod(remaining, 60)
    text = f"{_WARM_GLYPH} {minutes}:{seconds:02d}"
    if remaining >= _GREEN_MIN_S:
        return (text, "green")
    if remaining >= _ORANGE_MIN_S:
        return (text, "orange")
    return (text, "red")


def cell_padding(text: str) -> str:
    """The spaces that pad a rendered cell *text* out to :data:`CELL_WIDTH`.

    Returned separately from the text (never merged into it) so each view can paint the
    glyph and leave the padding unstyled — ``ccc ls`` wraps its text in ANSI codes, which
    would defeat any width measurement done after painting. Always at least one space (the
    column separator), so over-wide content is padded, not truncated.
    """
    return " " * max(1, CELL_WIDTH - len(text))


def _parse_iso(ts: str) -> float | None:
    """ISO-8601 UTC timestamp (``…Z``) → epoch seconds, or ``None`` when unparseable."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError):
        return None


def _positive_int(value: object) -> bool:
    """True for a positive ``int`` token count (``bool``, strings, floats, null → False)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_real_response(row: object) -> bool:
    """True when transcript *row* is a real main-chain model response.

    ``type == "assistant"``, not ``isSidechain``, not ``isApiErrorMessage``, a
    ``message.model`` that is a non-empty string other than ``<synthetic>``, and a parsable
    ``timestamp``. A refusal Claude Code wrote itself (usage limit, API error) fails here.
    """
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return False
    if row.get("isSidechain") or row.get("isApiErrorMessage"):
        return False
    message = row.get("message")
    if not isinstance(message, dict):
        return False
    model = message.get("model")
    if not isinstance(model, str) or not model or model == _SYNTHETIC_MODEL:
        return False
    stamp = row.get("timestamp")
    return isinstance(stamp, str) and _parse_iso(stamp) is not None


def is_cache_anchor(row: object) -> bool:
    """True when *row* read or wrote the prompt cache, so it anchors the warm countdown.

    :func:`is_real_response` AND, read ONLY from the ``message.usage`` dict, a positive
    ``int`` ``cache_read_input_tokens`` or ``cache_creation_input_tokens``. A valid
    zero-cache response, every local row and a cache-token key inside a tool payload never
    count.
    """
    if not is_real_response(row):
        return False
    assert isinstance(row, dict)  # narrowed by is_real_response
    usage = row["message"].get("usage")
    if not isinstance(usage, dict):
        return False
    return _positive_int(usage.get("cache_read_input_tokens")) or _positive_int(
        usage.get("cache_creation_input_tokens")
    )


def _parse_line(line: bytes) -> dict[str, object] | None:
    """*line* parsed as a JSON object, or ``None`` (unparsable, or not an object)."""
    try:
        obj = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _anchor_from_line(line: bytes) -> float | None:
    """Anchor epoch-seconds for one transcript line, or ``None`` when it does not qualify.

    A cheap substring prefilter (an assistant entry) runs first; the parsed object must
    then pass :func:`is_cache_anchor`. A line that will not parse returns ``None``: its
    usage cannot be checked, so it never anchors.
    """
    if _ASSISTANT_MARK not in line:
        return None
    obj = _parse_line(line)
    if obj is None or not is_cache_anchor(obj):
        return None
    stamp = obj.get("timestamp")
    return _parse_iso(stamp) if isinstance(stamp, str) else None


def _seat_from_line(line: bytes) -> tuple[str, str] | None:
    """``(kind, accountUuid)`` for a ``bridge-session`` / ``session-owner`` row, else ``None``.

    *kind* is ``"bridge"`` (``ownerAccountUuid``: the seat of the process that wrote it, so
    of the requests after it) or ``"owner"`` (``accountUuid``: sticky, the seat the session
    started on).
    """
    if _BRIDGE_MARK in line:
        kind, type_name, key = "bridge", "bridge-session", "ownerAccountUuid"
    elif _OWNER_MARK in line:
        kind, type_name, key = "owner", "session-owner", "accountUuid"
    else:
        return None
    obj = _parse_line(line)
    if obj is None or obj.get("type") != type_name:
        return None
    uuid = obj.get(key)
    return (kind, uuid) if isinstance(uuid, str) and uuid else None


def _reversed_lines(handle: BinaryIO) -> Iterator[tuple[int, bytes]]:
    """Yield ``(start offset, line)`` for every line of *handle*, newest (EOF) first.

    Reads bounded chunks from EOF toward the start, doubling the chunk each step (capped at
    :data:`_MAX_CHUNK_BYTES`). The partial first line of each chunk is carried into the
    next (earlier) chunk so a line that straddles — or is longer than — a chunk boundary is
    reassembled before it is yielded.
    """
    handle.seek(0, os.SEEK_END)
    pos = handle.tell()
    carry = b""
    chunk = _CHUNK_BYTES
    while pos > 0:
        read = min(chunk, pos)
        pos -= read
        handle.seek(pos)
        data = handle.read(read) + carry
        lines = data.split(b"\n")
        if pos > 0:
            # ``lines[0]`` starts earlier in the file — carry it to the next chunk.
            carry = lines[0]
            lines = lines[1:]
            offset = pos + len(carry) + 1
        else:
            carry = b""
            offset = 0
        starts = []
        for line in lines:
            starts.append(offset)
            offset += len(line) + 1
        yield from zip(reversed(starts), reversed(lines), strict=True)
        chunk = min(chunk * 2, _MAX_CHUNK_BYTES)


def _scan_anchor(path: Path) -> _Anchor | None:
    """Newest cache anchor of *path* and its seat, read backwards, or ``None``.

    Lines are appended chronologically, so the FIRST qualifying line found while scanning
    backwards is the newest. From there the scan keeps going back for the anchor's seat:
    the newest ``bridge-session`` row at or before it wins; failing that, the newest
    ``session-owner`` row; neither within :data:`_SEAT_SCAN_BYTES` before the anchor (or
    before the file start) → seat ``None`` (unknown). ``None`` when the file holds no
    qualifying assistant entry (or cannot be read).
    """
    anchor: float | None = None
    limit = 0
    owner: str | None = None
    try:
        with path.open("rb") as handle:
            for start, line in _reversed_lines(handle):
                if anchor is None:
                    anchor = _anchor_from_line(line)
                    limit = start - _SEAT_SCAN_BYTES
                    continue
                if start < limit:
                    break
                seat = _seat_from_line(line)
                if seat is None:
                    continue
                if seat[0] == "bridge":
                    return (anchor, seat[1])
                if owner is None:
                    owner = seat[1]
    except OSError:
        return None
    return None if anchor is None else (anchor, owner)


def _cached_anchor(path: Path, key: tuple[int, int]) -> _Anchor | None:
    """:func:`_scan_anchor` for *path*, memoized on *key* in :data:`_ANCHOR_CACHE`.

    *key* is the transcript's ``(st_mtime_ns, st_size)``: an unchanged transcript is read
    at most once per process; a moved mtime or size forces a re-scan. Only reached off the
    fast path, so the cache is populated solely for transcripts fresh enough to still be
    warm.
    """
    cached = _ANCHOR_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    anchor = _scan_anchor(path)
    _ANCHOR_CACHE[path] = (key, anchor)
    return anchor


def _running_seat(session: Session) -> str | None:
    """The ``accountUuid`` *session*'s next request bills, from its ``config_dir``.

    ``config_dir`` is the account the session last ran under (the live registry's, stamped
    by reconcile); ``""`` means unknown and fails closed (``None``), as on every
    multi-account surface.
    """
    config_dir = getattr(session, "config_dir", "") or ""
    return accounts.account_uuid(config_dir) if config_dir else None


def _transcript_path(adapter: object, session: Session) -> Path | None:
    """Resolve *session*'s transcript ``Path``, or ``None`` when it can't be.

    ``transcript_path`` is a concrete-adapter capability (not part of the ``Adapter``
    protocol), so it is probed defensively via ``getattr`` — a stub adapter without it (or
    one that raises / returns a non-``Path``) degrades to ``None`` (an empty cell).
    """
    getter = getattr(adapter, "transcript_path", None)
    if getter is None:
        return None
    try:
        path = getter(session.cwd, session.session_id)
    except OSError:
        return None
    return path if isinstance(path, Path) else None


def transcript_anchor(adapter: object, session: Session, now: float, ttl: int) -> float | None:
    """Epoch-seconds cache anchor for *session* to feed :func:`countdown`, or ``None``.

    Exactly one ``os.stat`` per call (the transcript is read only when it might be warm).
    The returned value is what :func:`countdown` turns into the cell:

    * ``None`` — no transcript at all (missing / unresolved / unstat-able): an empty cell.
    * the transcript **mtime** — FAST PATH: the file itself is already older than *ttl*, so
      every entry (≤ mtime) is expired. We return without reading the file (and without
      touching :data:`_ANCHOR_CACHE`), and mtime feeds :func:`countdown` to ``❄ cold``.
    * ``now - ttl - 1`` — the transcript is fresh but carries no qualifying assistant entry
      yet (a freshly resumed/opened session whose only new lines are local ``mode`` /
      ``bridge-session`` / … entries with no API call): a deliberately-expired sentinel so
      :func:`countdown` renders ``❄ cold`` — the warm cache the fresh mtime implied is a lie.
    * the same sentinel when more than one account is configured and the anchor's seat is
      not the session's running seat (:func:`_running_seat`), or either is unknown: that
      cache belongs to the other seat, so this session's next request is a full write.
    * the **real anchor** — the transcript is fresh AND holds a qualifying assistant entry
      of the running seat; its timestamp is the last-request clock :func:`countdown`
      counts down from.
    """
    path = _transcript_path(adapter, session)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    mtime = stat.st_mtime
    if now - mtime >= ttl:
        return mtime  # fast path: file already older than the TTL → cold, no read
    cold = now - ttl - 1
    scanned = _cached_anchor(path, (stat.st_mtime_ns, stat.st_size))
    if scanned is None:
        return cold  # fresh mtime but no cache request yet → cold sentinel
    anchor, anchor_seat = scanned
    if accounts.is_multi_account():
        seat_now = _running_seat(session)
        if seat_now is None or anchor_seat is None or seat_now != anchor_seat:
            return cold  # another (or an unknown) seat's cache → cold sentinel
    return anchor


def countdown_for(
    adapter: object, session: Session, now: float | None = None, *, running: bool = False
) -> tuple[str, str]:
    """:func:`countdown` for *session* — resolve its cache anchor, then format.

    A convenience the views call once per (non-draft, non-done) row; *now* defaults to the
    wall clock and is injectable for tests. The draft/done gating stays in the views (they
    already branch on it) — this only turns a live/parked/waiting row into a cell.

    *running* (the row is mid-turn — the green ▶ status icon) short-circuits to
    ``("▶", "running")``: a busy session keeps re-warming its cache, so the countdown is
    both meaningless and misleading there. It returns before any ``stat``/read, so busy
    rows also cost no transcript I/O per refresh.
    """
    if running:
        return (_RUNNING_GLYPH, _RUNNING_LEVEL)
    now = time.time() if now is None else now
    ttl = cache_ttl_seconds()
    return countdown(transcript_anchor(adapter, session, now, ttl), now, ttl=ttl)
