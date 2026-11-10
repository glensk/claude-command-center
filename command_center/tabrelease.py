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

The badge cache gets the same treatment (:func:`sweep_badges`): a ``<slug>`` file whose
UUID is gone past the grace and that no alive-or-unknown row protects is removed with its
``.dir`` sidecar, under the ``.lock`` :func:`tabsymbol.assign` takes, so its palette slot is
free again instead of ``assign`` stealing a live tab's badge.
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
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .models import LiveSession, TabbedRow
from .tab_titles import uuid_of

if TYPE_CHECKING:
    from .store import Store

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


def protected_uuids(
    rows: Iterable[TabbedRow], live_ids: set[str], alive: Callable[[int], bool] | None = None
) -> set[str]:
    """Tab UUIDs some row still guards: its process is alive, live or unknown (not dead)."""
    probe = alive or _default_alive
    return {
        uuid_of(row.iterm_session_id)
        for row in rows
        if uuid_of(row.iterm_session_id) and not pid_dead(row, live_ids, probe)
    }


# ---- badge cache sweep ---------------------------------------------------------------
# A badge file is ``<slug>`` = ``$ITERM_SESSION_ID`` with ``:`` → ``_`` (``w0t1p0_UUID``),
# with an optional ``<slug>.dir`` folder sidecar (see :mod:`tabsymbol`). Only names of
# exactly that shape are ever touched; ``.lock`` and anything else is left alone.
_BADGE_NAME_RE = re.compile(
    r"^[^_./]+_([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$"
)


def badge_uuid(name: str) -> str | None:
    """The upper-cased tab UUID of badge file *name*, or ``None`` for any other name."""
    match = _BADGE_NAME_RE.match(name)
    return match.group(1).upper() if match else None


def badge_files(directory: Path) -> dict[str, list[Path]]:
    """``tab UUID → badge files`` in *directory* (sidecars, ``.lock`` and odd names skipped)."""
    out: dict[str, list[Path]] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return out
    for entry in entries:
        uuid = badge_uuid(entry.name)
        if uuid is None or not entry.is_file():
            continue
        out.setdefault(uuid, []).append(entry)
    return out


def stale_badge_files(
    directory: Path,
    missing: dict[str, int],
    protected: set[str],
    now_ms: int,
    grace_ms: int = TAB_GONE_GRACE_MS,
) -> list[Path]:
    """The badge files whose tab is gone past the grace and that no row protects.

    A file of a present pane (never in *missing*, row or not) is never listed.
    """
    guarded = {uuid_of(u) for u in protected}
    return [
        path
        for uuid, paths in badge_files(directory).items()
        if uuid not in guarded and is_gone(uuid, missing, now_ms, grace_ms)
        for path in paths
    ]


@contextlib.contextmanager
def _badge_lock(directory: Path) -> Iterator[bool]:
    """Hold the badge cache's ``.lock`` (the one ``tabsymbol.assign`` takes); yields success."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = (directory / ".lock").open("w", encoding="utf-8")
    except OSError:
        yield False
        return
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sweep_badges(
    directory: Path,
    missing: dict[str, int],
    protected: set[str],
    now_ms: int,
    grace_ms: int = TAB_GONE_GRACE_MS,
) -> list[Path]:
    """Unlink every :func:`stale_badge_files` entry (and its ``.dir``) under the ``.lock``.

    The stale set is recomputed while the lock is held, so a concurrent ``assign`` never
    sees a half-swept cache. Returns the badge files removed; no lock → nothing removed.
    """
    if not directory.is_dir():
        return []
    removed: list[Path] = []
    with _badge_lock(directory) as locked:
        if not locked:
            return []
        for path in stale_badge_files(directory, missing, protected, now_ms, grace_ms):
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path)
            with contextlib.suppress(OSError):
                path.with_name(path.name + ".dir").unlink()
    return removed


# ---- the effectful pass (daemon housekeeping step, ``ccc tab-symbol -g/-G``) ---------
#: :attr:`RowVerdict.reason` of a row whose tab binding is released (else: why it is kept).
RELEASE = "gone"
KEEP_PRESENT = "tab present"
KEEP_GRACE = "missing < grace"
KEEP_DRAFT = "draft"
KEEP_PID_UNKNOWN = "pid unknown"
KEEP_LIVE = "live in registry"
KEEP_PID_ALIVE = "pid alive"

#: :attr:`ReleaseResult.skipped` reasons: no reliable pane read, so nothing is judged.
SKIP_NOT_RUNNING = "iTerm not running"
SKIP_READ_FAILED = "iTerm pane read failed"
SKIP_EMPTY_READ = "iTerm listed no panes"


@dataclass(frozen=True)
class RowVerdict:
    """One tabbed row's outcome: :data:`RELEASE` or the keep reason, plus its missing age."""

    row: TabbedRow
    reason: str
    missing_ms: int | None = None  # how long the tab has been missing; None = present

    @property
    def release(self) -> bool:
        """True when the row's tab binding is (or, dry, would be) released."""
        return self.reason == RELEASE


def row_verdict(  # pylint: disable=too-many-arguments,too-many-return-statements
    row: TabbedRow,
    missing: dict[str, int],
    live_ids: set[str],
    now_ms: int,
    grace_ms: int = TAB_GONE_GRACE_MS,
    *,
    alive: Callable[[int], bool] | None = None,
) -> RowVerdict:
    """Pure (given *alive*): the same rule as :func:`gone_rows`, with the reason spelled out."""
    first = missing.get(uuid_of(row.iterm_session_id))
    age = None if first is None else max(0, now_ms - first)
    if row.draft or not uuid_of(row.iterm_session_id):
        return RowVerdict(row, KEEP_DRAFT, age)
    if age is None:
        return RowVerdict(row, KEEP_PRESENT, age)
    if age < grace_ms:
        return RowVerdict(row, KEEP_GRACE, age)
    pid = row.last_seen_pid
    if pid is None or pid <= 0:
        return RowVerdict(row, KEEP_PID_UNKNOWN, age)
    if row.session_id in live_ids:
        return RowVerdict(row, KEEP_LIVE, age)
    if (alive or _default_alive)(pid):
        return RowVerdict(row, KEEP_PID_ALIVE, age)
    return RowVerdict(row, RELEASE, age)


@dataclass
class ReleaseResult:
    """What one :func:`release_gone` pass decided and (unless dry) did."""

    skipped: str = ""  # a SKIP_* reason: no reliable pane read, nothing judged or written
    dry_run: bool = False
    verdicts: list[RowVerdict] = field(default_factory=list)
    released: list[TabbedRow] = field(default_factory=list)  # unbound (dry: would be)
    refused: list[TabbedRow] = field(default_factory=list)  # the CAS saw changed evidence
    removed: list[Path] = field(default_factory=list)  # badge files removed (dry: would be)


def _live_ids(live: Mapping[str, LiveSession] | Iterable[str]) -> set[str]:
    """Session ids alive in the registry (a mapping of :class:`LiveSession` or bare ids)."""
    if isinstance(live, Mapping):
        return {sid for sid, entry in live.items() if getattr(entry, "alive", True)}
    return set(live)


def _reliable_panes() -> tuple[set[str], str]:
    """``(present tab UUIDs, "")`` from ONE reliable pane read, else ``(set(), SKIP_*)``.

    iTerm's running state is checked first WITHOUT an Apple event: ``tell application``
    would launch an absent iTerm. A failed read and an empty pane list (iTerm starting or
    restoring windows) are both unknown, never "every tab is gone".
    """
    from . import iterm_api, tab_titles  # lazy

    if not iterm_api.iterm_running():
        return set(), SKIP_NOT_RUNNING
    panes = tab_titles.read_panes()
    if panes is None:
        return set(), SKIP_READ_FAILED
    present = {u for u in (uuid_of(p.uuid) for p in panes) if u}
    if not present:
        return set(), SKIP_EMPTY_READ
    return present, ""


def release_gone(  # pylint: disable=too-many-arguments,too-many-locals
    store: Store,
    live: Mapping[str, LiveSession] | Iterable[str],
    *,
    dry_run: bool,
    record: bool | None = None,
    now: int | None = None,
    alive: Callable[[int], bool] | None = None,
    directory: Path | None = None,
    log: MissingLog | None = None,
) -> ReleaseResult:
    """Release the tab binding and badge of every row whose tab is gone and process dead.

    Fail closed: no reliable pane read → ``skipped`` and nothing written (DB, badge cache,
    missing log). Otherwise the read is folded into the missing log (when *record*, which
    defaults to ``not dry_run``), each tabbed row gets a :class:`RowVerdict`, releasable rows
    go through :meth:`Store.release_tab` (a CAS that re-probes the pid) and the badge cache
    is swept under its ``.lock``. *dry_run* writes neither the DB nor the cache and reports
    what would happen. The rows themselves always stay (parked rows stay resumable).
    """
    from .models import now_ms as _now_ms  # lazy
    from .tabsymbol import cache_dir  # lazy

    result = ReleaseResult(dry_run=dry_run)
    present, skipped = _reliable_panes()
    if skipped:
        result.skipped = skipped
        return result
    stamp = _now_ms() if now is None else now
    probe = alive or _default_alive
    cache = cache_dir() if directory is None else directory
    live_ids = _live_ids(live)
    rows = store.list_tabbed_sessions()
    referenced = set(iter_uuids(rows)) | set(badge_files(cache))
    missing = (log or MissingLog()).observe(
        referenced, present, stamp, write=(not dry_run) if record is None else record
    )
    result.verdicts = [row_verdict(row, missing, live_ids, stamp, alive=probe) for row in rows]
    releasable = [v.row for v in result.verdicts if v.release]
    if dry_run:
        result.released = releasable
        guarded = protected_uuids(rows, live_ids, probe)
        result.removed = stale_badge_files(cache, missing, guarded, stamp)
        return result
    for row in releasable:
        pid = row.last_seen_pid or 0
        if store.release_tab(row.session_id, row.iterm_session_id, pid):
            result.released.append(row)
        else:
            result.refused.append(row)
    # Re-read after the CAS writes: a row that re-bound or came alive meanwhile protects
    # its badge, and so does every refused row (its evidence changed under us).
    guarded = protected_uuids(store.list_tabbed_sessions(), live_ids, probe)
    guarded |= set(iter_uuids(result.refused))
    result.removed = sweep_badges(cache, missing, guarded, stamp)
    return result


# ---- ``ccc tab-symbol -g/--gone`` (preview) and ``-G/--release-gone`` (run now) --------
def _age(missing_ms: int | None) -> str:
    """``12m`` / ``3h05m`` for a missing age; ``-`` for a present tab."""
    if missing_ms is None:
        return "-"
    minutes = missing_ms // 60_000
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def report_lines(result: ReleaseResult) -> list[str]:
    """The human report of one :func:`release_gone` pass (short ids only, never AIM text).

    One line per row whose tab is missing (released / releasable, or why it is kept), then
    a counts line. Rows on a present tab are only counted.
    """
    from .models import short_id  # lazy

    if result.skipped:
        return [f"skipped: {result.skipped} (fail closed: nothing judged or written)"]
    refused = {r.session_id for r in result.refused}
    lines: list[str] = []
    for verdict in result.verdicts:
        if verdict.reason == KEEP_PRESENT:
            continue
        row = verdict.row
        reason = verdict.reason
        if row.session_id in refused:
            reason = "refused (evidence changed)"
        elif verdict.release:
            reason = "would release" if result.dry_run else "released"
        lines.append(
            f"{short_id(row.session_id)}  {row.status or '-':<13}  "
            f"{uuid_of(row.iterm_session_id)[:8]}  missing {_age(verdict.missing_ms):>6}  {reason}"
        )
    present = sum(1 for v in result.verdicts if v.reason == KEEP_PRESENT)
    verb = "would release" if result.dry_run else "released"
    removed = "would remove" if result.dry_run else "removed"
    lines.append(
        f"{verb} {len(result.released)} tab binding(s), {removed} {len(result.removed)} "
        f"badge file(s); {len(result.refused)} refused, {present} row(s) on a present tab"
    )
    return lines


def cli_report(*, release: bool) -> int:
    """``ccc tab-symbol -g`` (``release=False``) / ``-G`` (``release=True``); always exit 0.

    ``-g`` is a preview: it writes neither the DB nor the badge cache, but DOES record the
    pane read in the missing log, so two previews ≥ the grace apart show what ``-G`` (or
    the daemon) would release. ``-G`` runs the release now, exactly like a daemon pass.
    """
    from .adapters.claude import ClaudeAdapter  # lazy
    from .store import Store  # lazy

    live = {ls.session_id: ls for ls in ClaudeAdapter().discover()}
    with Store() as store:
        result = release_gone(store, live, dry_run=not release, record=True)
    for line in report_lines(result):
        print(line)
    return 0
