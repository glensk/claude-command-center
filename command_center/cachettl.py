#!/usr/bin/env python3
"""Prompt-cache TTL countdown for the ``model`` column (TUI + ``ccc ls``).

Anthropic's prompt cache is a *prefix* cache that expires ``TTL`` seconds after the
**last** request, not after the session started: every request re-reads the prefix and
refreshes the TTL, so an actively-used session never goes cold. Once it *does* expire the
next message pays a full cache re-write — on ANY account (the cache is per-account, so
switching cpriv↔cwork then costs the same as continuing). Surfacing the remaining warm
time lets you decide whether a parked session is still cheap to resume.

"Last request" has no first-class signal, so we anchor the clock on the session's last
real API interaction: the **timestamp of the newest main-chain ``"type":"assistant"``
transcript entry**. Each assistant entry is the product of one API response (it carries a
``requestId``) and re-reads/refreshes the prompt-cache prefix, so its timestamp is exactly
"when the cache was last warmed". Assistant entries with ``"isSidechain":true`` are
EXCLUDED — subagent traffic refreshes a *different* cache prefix, not the main session's.

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


import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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

# Cheap substring prefilter (bytes) applied before any JSON parse — mirrors the
# statusline's ``grep '"type":"assistant"' | grep -v '"isSidechain":true'``. Claude Code
# writes compact JSON (no spaces after colons), so these literals match verbatim.
_ASSISTANT_MARK = b'"type":"assistant"'
_SIDECHAIN_MARK = b'"isSidechain":true'

# Fallback timestamp extractor for a single (already-qualified) line when JSON parsing
# fails. ``findall()[-1]`` takes the LAST match — the top-level ``timestamp`` is emitted
# after ``message`` in Claude Code's field order, so the last match is the entry's own
# timestamp (mirrors the statusline's greedy ``sed`` capture).
_TS_RE = re.compile(r'"timestamp":"([^"]+)"')

# Module-level anchor read-cache: ``{path: (mtime, anchor_or_None)}``. The TUI renders per
# row per refresh and the daemon is long-lived, so an unchanged transcript must be read at
# most once (same rationale as ``mirrors._PROMPT_CACHE``). Re-read only when mtime moved.
# The fast path (mtime expired) neither consults nor populates this — it is pure stat math.
_ANCHOR_CACHE: dict[Path, tuple[float, float | None]] = {}


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


def _anchor_from_line(line: bytes) -> float | None:
    """Anchor epoch-seconds for one transcript line, or ``None`` when it does not qualify.

    Qualification is the cheap substring prefilter (a main-chain assistant entry); the
    timestamp is then read from the parsed object's top-level ``timestamp`` key (a regex
    grabs the last match when the line will not parse as JSON).
    """
    if _ASSISTANT_MARK not in line or _SIDECHAIN_MARK in line:
        return None
    text = line.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        raw = obj.get("timestamp")
        return _parse_iso(raw) if isinstance(raw, str) else None
    matches = _TS_RE.findall(text)
    return _parse_iso(matches[-1]) if matches else None


def _scan_anchor(path: Path) -> float | None:
    """Newest main-chain assistant entry's anchor by reading *path* backwards, or ``None``.

    Reads bounded chunks from EOF toward the start, doubling the chunk each step (capped at
    :data:`_MAX_CHUNK_BYTES`). Lines are appended chronologically, so the FIRST qualifying
    line found while scanning backwards is the newest — returned immediately. The partial
    first line of each chunk is carried into the next (earlier) chunk so a line that
    straddles — or is longer than — a chunk boundary is reassembled before it is inspected.
    ``None`` when the file holds no qualifying assistant entry (or cannot be read).
    """
    try:
        with path.open("rb") as handle:
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
                else:
                    carry = b""
                for line in reversed(lines):
                    anchor = _anchor_from_line(line)
                    if anchor is not None:
                        return anchor
                chunk = min(chunk * 2, _MAX_CHUNK_BYTES)
    except OSError:
        return None
    return None


def _cached_anchor(path: Path, mtime: float) -> float | None:
    """:func:`_scan_anchor` for *path*, memoized on *mtime* in :data:`_ANCHOR_CACHE`.

    An unchanged transcript (same mtime) is read at most once per process; a moved mtime
    forces a re-scan. Only reached off the fast path, so the cache is populated solely for
    transcripts fresh enough to still be warm.
    """
    cached = _ANCHOR_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    anchor = _scan_anchor(path)
    _ANCHOR_CACHE[path] = (mtime, anchor)
    return anchor


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
    * the **real anchor** — the transcript is fresh AND holds a qualifying assistant entry;
      its timestamp is the last-request clock :func:`countdown` counts down from.
    """
    path = _transcript_path(adapter, session)
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if now - mtime >= ttl:
        return mtime  # fast path: file already older than the TTL → cold, no read
    anchor = _cached_anchor(path, mtime)
    if anchor is None:
        return now - ttl - 1  # fresh mtime but no API turn yet → cold sentinel
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
