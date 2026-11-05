#!/usr/bin/env python3
"""The pending queue of ccc's LLM work a router SKIPPED (tp#1697 B14, Design §3).

Under ``AI_AUTOMATED`` the purpose router may exit 75 ("skipped: no non-Anthropic rung
answered"). For ccc's ``session-name``, ``short-aim`` and ``voice-brief`` work that is
not a failure: the name or label stays unset (a ``fallback`` name stays provisional and
its retry budget is NOT spent) and the item goes on this queue in ccc state,
``<app_home>/llm-pending.json`` (``{purpose: {session_id: first-skip epoch}}``). The
daemon's loop is the tick: it drops resolved and stale entries and re-runs the work
(:func:`command_center.daemon._drain_llm_pending`). Best-effort: a queue that cannot be
read or written never fails the caller.
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
import fcntl
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from . import config

PURPOSES = ("session-name", "short-aim", "voice-brief")
#: An entry older than this is dropped (a one-shot UI hint is stale by then).
MAX_AGE_SEC = 24 * 3600
QUEUE_NAME = "llm-pending.json"

Queue = dict[str, dict[str, int]]


def queue_path() -> Path:
    """``<app_home>/llm-pending.json``."""
    return config.app_home() / QUEUE_NAME


@contextlib.contextmanager
def _locked() -> Iterator[Path]:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield path


def _read(path: Path) -> Queue:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(purpose): {str(k): int(v) for k, v in items.items() if isinstance(v, int)}
        for purpose, items in raw.items()
        if isinstance(items, dict)
    }


def _write(path: Path, queue: Queue) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({p: i for p, i in queue.items() if i}), encoding="utf-8")
    os.replace(tmp, path)


def load() -> Queue:
    """The whole queue (``{}`` when unreadable)."""
    return _read(queue_path())


def mark(purpose: str, key: str, now: int | None = None) -> None:
    """Put *key* (a session id) on *purpose*'s queue; the first skip's time is kept."""
    if not key:
        return
    try:
        with _locked() as path:
            queue = _read(path)
            queue.setdefault(purpose, {}).setdefault(key, int(time.time()) if now is None else now)
            _write(path, queue)
    except OSError:
        pass


def clear(purpose: str, key: str) -> None:
    """Take *key* off *purpose*'s queue (its work succeeded or is moot)."""
    try:
        with _locked() as path:
            queue = _read(path)
            if key not in queue.get(purpose, {}):
                return
            queue[purpose].pop(key, None)
            _write(path, queue)
    except OSError:
        pass


def prune(now: int | None = None, max_age: int = MAX_AGE_SEC) -> Queue:
    """Drop entries older than *max_age*; the queue that remains."""
    now = int(time.time()) if now is None else now
    try:
        with _locked() as path:
            queue = _read(path)
            kept = {
                p: {k: ts for k, ts in items.items() if now - ts <= max_age}
                for p, items in queue.items()
            }
            kept = {p: items for p, items in kept.items() if items}
            if kept != queue:
                _write(path, kept)
            return kept
    except OSError:
        return {}
