#!/usr/bin/env python3
"""When the TUI should rebuild its rows: on a change, never back to back (tp#1427).

The TUI used to rebuild every ``usage_refresh_sec`` whatever happened, and under load the
builds ran one after another on a pinned core. :class:`RefreshGate` is the pure decision
(a clock and three signals in, a reason or ``None`` out) so it is tested without a UI:

- a build may START only ``max(floor_s, 2 × last_build_duration)`` after the previous one
  ENDED, so a slow build can never be followed straight away by another;
- after that gap it is due when the ``PRAGMA data_version`` of a long-lived connection
  moved (another connection committed), the *files signature* (live-session registry entries
  + the transcripts of the live rows) moved, a foreign commit landed DURING the last build,
  or ``force_after_s`` passed (relative ages and background facts have no signal).

The build's OWN commits (every pass rewrites the live rows) must not look like a change:
the data-version baseline is therefore taken when the build ends, and a commit by another
process during the build is caught separately by the worker's own connection (its
``data_version`` does not move for its own commits) and handed to :meth:`RefreshGate.end`.
"""

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim precedes them
from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path

FilesSignature = tuple[tuple[str, int, int], ...]


def data_version(conn: sqlite3.Connection) -> int:
    """``PRAGMA data_version`` of *conn*: changes when ANOTHER connection commits."""
    row = conn.execute("PRAGMA data_version").fetchone()
    return int(row[0]) if row else 0


def files_signature(registry_dirs: Iterable[Path], transcripts: Iterable[Path]) -> FilesSignature:
    """``(path, mtime_ns, size)`` of every registry entry and every given transcript.

    One ``scandir`` per registry directory plus one ``stat`` per transcript — a dozen
    syscalls for a typical TUI — and never a read of any file's content.
    """
    out: list[tuple[str, int, int]] = []
    for directory in registry_dirs:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json"):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    out.append((entry.path, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    for path in transcripts:
        try:
            st = os.stat(path)
        except OSError:
            out.append((str(path), -1, -1))  # a vanished transcript is a change too
            continue
        out.append((str(path), st.st_mtime_ns, st.st_size))
    out.sort()
    return tuple(out)


class RefreshGate:  # pylint: disable=too-many-instance-attributes
    """State of the rebuild cadence; every method is cheap and runs on the UI thread."""

    def __init__(
        self,
        force_after_s: float,
        floor_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.force_after_s = force_after_s
        self.floor_s = floor_s
        self._clock = clock
        self._started = 0.0
        self._last_end: float | None = None  # None = nothing built yet → always due
        self._last_duration = 0.0
        self._files_baseline: FilesSignature | None = None
        self._db_baseline: int | None = None
        self._dirty = False

    @property
    def min_gap_s(self) -> float:
        """The quiet period after a build ends: ``max(floor, 2 × its duration)``."""
        return max(self.floor_s, 2 * self._last_duration)

    def begin(self, files: FilesSignature) -> None:
        """A build starts; *files* is the signature sampled just before it (race-free:
        a build never writes the registry or a transcript, so a change DURING it shows
        as a difference on the next check)."""
        self._started = self._clock()
        self._files_baseline = files
        self._dirty = False

    def end(self, db_version: int, foreign_write: bool = False) -> None:
        """The build ended; *db_version* is read now so its own commits are the baseline."""
        now = self._clock()
        self._last_duration = max(0.0, now - self._started)
        self._last_end = now
        self._db_baseline = db_version
        self._dirty = self._dirty or foreign_write

    def note_foreign_write(self) -> None:
        """Another process committed while a build ran — rebuild once the gap allows."""
        self._dirty = True

    def due(  # pylint: disable=too-many-return-statements
        self, files: FilesSignature, db_version: int
    ) -> str | None:
        """Why a build should start now, or ``None``. Callers pass fresh signals."""
        if self._last_end is None:
            return "first"
        since = self._clock() - self._last_end
        if since < self.min_gap_s:
            return None
        if self._dirty:
            return "dirty"
        if db_version != self._db_baseline:
            return "db"
        if files != self._files_baseline:
            return "files"
        if since >= self.force_after_s:
            return "age"
        return None
