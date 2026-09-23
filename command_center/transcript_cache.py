#!/usr/bin/env python3
"""One-pass, incremental transcript collector + the peek panel's transcript cache.

``ccc peek`` used to walk a session transcript twice — once for the prompts tab
(:meth:`ClaudeAdapter.all_user_prompts_in_file`) and once for the session tab
(:func:`sessionmd.segments_for_path` → :func:`events_in_file`). On a 100 MB transcript
that is two ~150 ms walks per peek. This module replaces both with ONE pass:

* :class:`TranscriptCollector` — an incremental JSONL parser. Feed it bytes (the whole
  file, or only what was appended since the last feed); it keeps the byte offset, the
  partial trailing line (a record split across two appends parses once completed), the
  pending ``tool_use`` → ``tool_result`` map, and the accumulated prompts + normalised
  events. The per-line / per-record logic is the adapter's own
  (:func:`~command_center.adapters.claude.parse_transcript_line`,
  :func:`~command_center.adapters.claude.collect_record_events`) — this module holds no
  JSONL schema knowledge, so incremental output == the full walks' output.
* :func:`read_view` — the cold path: one fresh pass over a file, nothing retained.
* :class:`TranscriptCache` — the warm path (the resident panel server): a thread-safe
  LRU of collectors keyed by canonical path, revalidated by ``(st_dev, st_ino,
  mtime_ns, size)``: unchanged → ``hit``, grown in place → read only the appended bytes
  (``append``), anything else → full rebuild (``miss``).

Only transcript-derived data lives here (prompts + session-tab segments); header
fields (AIM history, cwd, badge, tab colour) are always fetched fresh by the caller.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import os
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import BinaryIO

from .adapters.claude import collect_record_events, parse_transcript_line
from .models import SessionEvent
from .sessionmd import session_segments

# Read granularity of a full / appended read (bounded memory for a 100 MB transcript).
_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 8
# Fits the largest known transcript (101.7 MB, tp#70 S3a); resident RSS ≈ 4× cached bytes.
DEFAULT_MAX_BYTES = 128 * 1024 * 1024

CACHE_HIT = "hit"
CACHE_APPEND = "append"
CACHE_MISS = "miss"

PathArg = str | os.PathLike[str]


@dataclass(frozen=True)
class TranscriptView:
    """The transcript-derived half of a peek: prompts tab + session tab.

    ``prompts`` equals :meth:`ClaudeAdapter.all_user_prompts_in_file`, ``segments``
    equals ``sessionmd.session_segments(events_in_file(path))``. Treat both lists as
    read-only — a cached view is handed out again on every ``hit``.
    """

    prompts: list[str] = field(default_factory=list)
    segments: list[tuple[str, str]] = field(default_factory=list)


def empty_view() -> TranscriptView:
    """The view of a missing / unreadable transcript (no prompts, the empty-body note)."""
    return TranscriptView([], session_segments([]))


class TranscriptCollector:
    """Incremental one-pass JSONL parser: prompts + normalised events of one transcript.

    :meth:`feed` accepts consecutive byte ranges of the file (the first feed starts at
    byte 0). Line splitting follows the text-mode walks it replaces: ``\\n``, ``\\r\\n``
    and a lone ``\\r`` all end a line (universal newlines — blank lines are skipped, so
    treating every ``\\r`` as a line break is equivalent). A line that is not valid UTF-8
    is skipped like a malformed JSON line.
    """

    __slots__ = ("_partial", "consumed", "events", "pending", "prompts")

    def __init__(self) -> None:
        self.consumed = 0  # bytes fed so far == the file offset the next feed starts at
        self._partial: list[bytes] = []  # bytes after the last line break (incomplete line)
        self.prompts: list[str] = []
        self.events: list[SessionEvent] = []
        self.pending: dict[str, SessionEvent] = {}  # tool_use_id → unpaired tool event

    @property
    def line_offset(self) -> int:
        """File offset just past the last complete line."""
        return self.consumed - sum(len(part) for part in self._partial)

    def feed(self, data: bytes) -> None:
        """Consume the next *data* bytes of the file (complete lines parse immediately)."""
        if not data:
            return
        self.consumed += len(data)
        if b"\r" in data:
            data = data.replace(b"\r", b"\n")
        last = data.rfind(b"\n")
        if last < 0:
            self._partial.append(data)
            return
        head = data[: last + 1]
        if self._partial:
            head = b"".join(self._partial) + head
            self._partial = []
        tail = data[last + 1 :]
        if tail:
            self._partial.append(tail)
        self._consume_lines(head)

    def _consume_lines(self, block: bytes) -> None:
        """Parse *block* — a run of complete lines (ends with a line break)."""
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError:
            for raw in block.split(b"\n"):
                line = _decode(raw)
                if line is not None:
                    self._line(line)
            return
        for line in text.split("\n"):
            self._line(line)

    def _line(self, line: str) -> None:
        record = parse_transcript_line(line)
        if record is None:
            return
        events = self.events
        before = len(events)
        collect_record_events(record, events, self.pending)
        if len(events) != before:
            # Prompt events carry exactly the _prompt_text of their record, so the
            # prompts list IS the prompt events' text (same filter, same order).
            self.prompts.extend(ev.text for ev in events[before:] if ev.kind == "prompt")

    def _tail_line(self) -> str | None:
        """The trailing unterminated line, if it holds anything parseable."""
        if not self._partial:
            return None
        line = _decode(b"".join(self._partial))
        return line if line is not None and line.strip() else None

    def finish(self) -> tuple[list[str], list[SessionEvent]]:
        """End of file: parse the unterminated last line for good; return the results.

        For a one-shot read only — a collector that may be fed more bytes later uses
        :meth:`view`, which applies the trailing line provisionally.
        """
        tail = self._tail_line()
        self._partial = []
        if tail is not None:
            self._line(tail)
        return self.prompts, self.events

    def view(self) -> TranscriptView:
        """The current :class:`TranscriptView`, as a whole-file walk would see it now.

        A whole-file walk parses a final line even without its line break, so the
        trailing partial line is applied here PROVISIONALLY and rolled back afterwards:
        if more bytes later extend it, it is re-parsed as the longer line it became.
        """
        tail = self._tail_line()
        if tail is None:
            return TranscriptView(list(self.prompts), session_segments(self.events))
        n_events, n_prompts = len(self.events), len(self.prompts)
        saved_pending = dict(self.pending)
        saved_results = {key: ev.tool_result for key, ev in saved_pending.items()}
        try:
            self._line(tail)
            return TranscriptView(list(self.prompts), session_segments(self.events))
        finally:
            del self.events[n_events:]
            del self.prompts[n_prompts:]
            for key, ev in saved_pending.items():
                ev.tool_result = saved_results[key]
            self.pending = saved_pending


def _decode(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _feed_from(handle: BinaryIO, collector: TranscriptCollector, nbytes: int) -> int:
    """Feed up to *nbytes* from *handle* into *collector*; returns the bytes read."""
    done = 0
    while done < nbytes:
        chunk = handle.read(min(_CHUNK_BYTES, nbytes - done))
        if not chunk:
            break
        collector.feed(chunk)
        done += len(chunk)
    return done


def collect_file(path: PathArg) -> tuple[list[str], list[SessionEvent]]:
    """One pass over *path*: ``(prompts, events)``, as the two full walks return them.

    ``prompts`` == :meth:`ClaudeAdapter.all_user_prompts_in_file`, ``events`` ==
    :func:`events_in_file`. A missing file yields ``([], [])``; a read error mid-file
    yields what was collected so far (the same contract as those walks).
    """
    collector = TranscriptCollector()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                collector.feed(chunk)
    except OSError:
        pass
    return collector.finish()


def read_view(path: PathArg) -> TranscriptView:
    """The cold path: one fresh pass over *path* (nothing is retained)."""
    prompts, events = collect_file(path)
    return TranscriptView(prompts, session_segments(events))


@dataclass
class _Entry:
    """One cached transcript: its file identity + the collector state + the memo view."""

    dev: int
    ino: int
    mtime_ns: int
    size: int
    collector: TranscriptCollector
    view: TranscriptView


class TranscriptCache:
    """Thread-safe LRU of incremental transcript collectors (the warm peek path).

    Keyed by ``os.path.realpath``; each entry pins ``(st_dev, st_ino, mtime_ns, size)``.
    :meth:`get` answers ``hit`` for an identical stat (the memoised view object, no
    read), ``append`` when the same inode grew (only the new bytes are read), and
    ``miss`` otherwise (truncation, replacement, same size with a new mtime, first
    sight, a read error on the cached path → full re-read). Bounded by *max_entries*
    AND *max_bytes* (sum of the cached files' sizes); a file larger than *max_bytes*
    is served but not retained.

    One lock guards the map. Hits and appends run under it; a full read (``miss``)
    runs outside it, so a long first read — e.g. :meth:`prewarm` on a background
    thread — never blocks a concurrent hit on another transcript.
    """

    def __init__(
        self, max_entries: int = DEFAULT_MAX_ENTRIES, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def bytes_used(self) -> int:
        """Transcript bytes the retained entries cover (sum of their cached sizes)."""
        with self._lock:
            return sum(entry.size for entry in self._entries.values())

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._entries.clear()

    def prewarm(self, paths: Iterable[PathArg]) -> None:
        """Fill entries for *paths* (meant for a background thread; missing files are skipped)."""
        for path in paths:
            self.get(path)

    def get(self, path: PathArg) -> tuple[TranscriptView, str]:
        """The transcript view of *path* and how it was obtained (hit / append / miss)."""
        key = os.path.realpath(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                refreshed = self._refresh_locked(key, entry)
                if refreshed is not None:
                    return refreshed
        return self._rebuild(key), CACHE_MISS

    def _refresh_locked(self, key: str, entry: _Entry) -> tuple[TranscriptView, str] | None:
        """Hit or append on *entry* (lock held); ``None`` → dropped, caller rebuilds."""
        try:
            st = os.stat(key)
        except OSError:
            self._entries.pop(key, None)
            return None
        if (st.st_dev, st.st_ino) == (entry.dev, entry.ino):
            if st.st_size == entry.size and st.st_mtime_ns == entry.mtime_ns:
                return entry.view, CACHE_HIT
            if st.st_size > entry.size and self._append_locked(key, entry):
                return entry.view, CACHE_APPEND
        self._entries.pop(key, None)
        return None

    def _append_locked(self, key: str, entry: _Entry) -> bool:
        """Read the bytes appended since *entry* was built; ``False`` → rebuild."""
        try:
            with open(key, "rb") as handle:
                st = os.fstat(handle.fileno())
                if (st.st_dev, st.st_ino) != (entry.dev, entry.ino) or st.st_size < entry.size:
                    return False
                handle.seek(entry.collector.consumed)
                want = st.st_size - entry.collector.consumed
                if _feed_from(handle, entry.collector, want) != want:
                    return False  # shrank while reading
            view = entry.collector.view()
        except (OSError, ValueError):
            return False  # the collector may be half-fed: the caller drops the entry
        entry.mtime_ns, entry.size, entry.view = st.st_mtime_ns, st.st_size, view
        if entry.size > self.max_bytes:
            self._entries.pop(key, None)  # outgrew the cap: served, no longer retained
        else:
            self._evict_locked()
        return True

    def _rebuild(self, key: str) -> TranscriptView:
        """Full read of *key* outside the lock; retained when complete and within bounds."""
        collector = TranscriptCollector()
        try:
            with open(key, "rb") as handle:
                st = os.fstat(handle.fileno())
                complete = _feed_from(handle, collector, st.st_size) == st.st_size
        except OSError:
            # Missing file → empty view; a mid-read error → what was read (as the walks do).
            prompts, events = collector.finish()
            return TranscriptView(prompts, session_segments(events))
        view = collector.view()
        if not complete or st.st_size > self.max_bytes:
            return view
        entry = _Entry(st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, collector, view)
        with self._lock:
            current = self._entries.get(key)
            # A concurrent reader may already hold a fresher state of the same file.
            if (
                current is None
                or (current.dev, current.ino) != (entry.dev, entry.ino)
                or current.size < entry.size
            ):
                self._entries[key] = entry
            self._entries.move_to_end(key)
            self._evict_locked()
        return view

    def _evict_locked(self) -> None:
        """Drop least-recently-used entries until both bounds hold (lock held)."""
        total = sum(entry.size for entry in self._entries.values())
        while self._entries and (len(self._entries) > self.max_entries or total > self.max_bytes):
            _key, evicted = self._entries.popitem(last=False)
            total -= evicted.size
