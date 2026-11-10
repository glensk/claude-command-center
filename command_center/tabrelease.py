#!/usr/bin/env python3
"""Release the tab binding and badge of session rows whose iTerm tab is gone (tp#1990).

A row keeps its ``iterm_session_id`` forever: nothing cleared it when the tab closed, so
dozens of parked rows held tab ids (and through them badge-cache files, i.e. palette
slots). This module decides — fail closed — which of those bindings are provably dead.

Two proofs are needed per row:

* **the process is dead** — a positive ``last_seen_pid`` that :func:`store.pid_alive`
  reports dead, and the session is not in the daemon's live registry. A NULL / 0 pid is
  "unknown", never "dead";
* **the tab is gone** — its UUID has been absent from every RELIABLE pane read (iTerm
  running, a non-empty :func:`tab_titles.read_panes` answer) for at least
  :data:`TAB_GONE_GRACE_SEC`. The first absence is recorded in :class:`MissingLog`
  (``tab-missing.json`` in the app home); seeing the pane again drops the entry, so a
  partial restore snapshot that omits a pane once releases nothing.

``closed_at``, ``last_response_at`` and file mtimes are deliberately not used for the
grace: they say when the process went, not when the tab did. Releasing never marks a row
``done`` — the row stays and is resumable; the next hook event in a new tab re-stamps it.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from .models import TabbedRow
from .tab_titles import uuid_of

#: How long a tab UUID must stay absent from reliable pane reads before it counts as gone.
TAB_GONE_GRACE_SEC = 600
TAB_GONE_GRACE_MS = TAB_GONE_GRACE_SEC * 1000

#: File name of the missing-since log inside the app home.
MISSING_LOG_NAME = "tab-missing.json"


def _default_alive(pid: int) -> bool:
    from .store import pid_alive  # lazy: keep this module's import cheap

    return pid_alive(pid)


class MissingLog:
    """``tab UUID → first_missing_ms``, persisted as a small JSON object.

    A missing, corrupt or unreadable file reads as an empty log and is rewritten by the
    next :meth:`observe` / :meth:`save`. Writes are atomic (temp file + rename).
    """

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from .config import app_home  # lazy

            path = app_home() / MISSING_LOG_NAME
        self.path = path
        self._clean = False  # True once a load parsed a well-formed file

    def load(self) -> dict[str, int]:
        """The log's entries; ``{}`` (and marked for rewrite) when absent or corrupt."""
        self._clean = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        entries: dict[str, int] = {}
        for key, value in raw.items():
            uuid = uuid_of(str(key))
            if not uuid or isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return {}
            entries[uuid] = value
        self._clean = True
        return entries

    def save(self, entries: dict[str, int]) -> None:
        """Atomically replace the file with *entries* (parent created on demand)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(sorted(entries.items())), handle, indent=0)
            os.replace(tmp_name, self.path)
            replaced = True
        finally:
            if not replaced:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
        self._clean = True

    def observe(
        self,
        referenced: Iterable[str],
        present: Iterable[str],
        now_ms: int,
        *,
        write: bool = True,
    ) -> dict[str, int]:
        """Fold ONE reliable pane read into the log and return the updated entries.

        *referenced* are the tab UUIDs some row or badge file still points at, *present*
        the UUIDs the read listed. Only call this with a reliable read (iTerm running, a
        non-empty pane list). ``write=False`` (dry run) computes without touching the file.
        """
        prior = self.load()
        entries = update_missing(prior, referenced, present, now_ms)
        if write and (entries != prior or not self._clean):
            self.save(entries)
        return entries


def update_missing(
    prior: dict[str, int], referenced: Iterable[str], present: Iterable[str], now_ms: int
) -> dict[str, int]:
    """Pure: the missing log after one reliable pane read.

    A referenced UUID absent from *present* keeps its first-missing stamp or gets *now_ms*;
    a present UUID is dropped (seeing it again restarts any later clock); an UUID nobody
    references any more is pruned.
    """
    seen = {u for u in (uuid_of(p) for p in present) if u}
    out: dict[str, int] = {}
    for uuid in sorted({u for u in (uuid_of(r) for r in referenced) if u}):
        if uuid in seen:
            continue
        out[uuid] = prior.get(uuid, now_ms)
    return out


def is_gone(uuid: str, missing: dict[str, int], now_ms: int, grace_ms: int) -> bool:
    """True when *uuid* has been missing from reliable reads for at least *grace_ms*."""
    first = missing.get(uuid_of(uuid))
    return first is not None and now_ms - first >= grace_ms


def pid_dead(row: TabbedRow, live_ids: set[str], alive: Callable[[int], bool]) -> bool:
    """The process proof: a positive pid that is not running, and not in the live registry."""
    pid = row.last_seen_pid
    if pid is None or pid <= 0:
        return False  # unknown is not dead
    if row.session_id in live_ids:
        return False
    return not alive(pid)


def gone_rows(  # pylint: disable=too-many-arguments
    rows: Iterable[TabbedRow],
    missing: dict[str, int],
    live_ids: set[str],
    now_ms: int,
    grace_ms: int = TAB_GONE_GRACE_MS,
    *,
    alive: Callable[[int], bool] | None = None,
) -> list[TabbedRow]:
    """Pure (given *alive*): the rows whose tab binding may be released.

    All must hold: not a draft, a non-empty tab id, the process proven dead
    (:func:`pid_dead`), and the tab UUID gone per *missing* (:func:`is_gone`). Open, done
    and archived rows are all in scope.
    """
    probe = alive or _default_alive
    out: list[TabbedRow] = []
    for row in rows:
        if row.draft or not uuid_of(row.iterm_session_id):
            continue
        if not is_gone(row.iterm_session_id, missing, now_ms, grace_ms):
            continue
        if not pid_dead(row, live_ids, probe):
            continue
        out.append(row)
    return out


def iter_uuids(rows: Iterable[TabbedRow]) -> Iterator[str]:
    """The non-empty tab UUIDs *rows* point at."""
    for row in rows:
        uuid = uuid_of(row.iterm_session_id)
        if uuid:
            yield uuid
