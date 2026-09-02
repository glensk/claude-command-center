#!/usr/bin/env python3
"""Reconciliation between the live agent registry and the store.

Pure orchestration over :class:`Store` and an :class:`Adapter`; kept free of any
rendering so both the CLI views and the daemon can call it.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, deps, repos, usage
from .adapters.base import Adapter
from .models import (
    CODEX_WORKFLOW_JOB_TYPES,
    EFFORT_LEVELS,
    LiveSession,
    Session,
    Status,
    TranscriptScan,
    derive_status,
    model_label,
    now_ms,
    scheduled_date,
)
from .store import Store

_DAY_MS = 86_400_000

# Sentinel for ``reconcile(codex_usage=…)``: "not supplied", so the function reads the
# Codex usage snapshot itself. ``None`` is a legitimate VALUE (no snapshot available), so
# it cannot double as the default.
_UNSET: Any = object()


@dataclass
class Row:
    """A session joined with its live entry and derived presentation fields."""

    session: Session
    live: LiveSession | None
    status: Status
    checked: int
    total: int
    uses_codex_workflow: bool = False
    codex_reset_label: str | None = None
    codex_reset_at: int | None = None
    # Dependency presentation (set by _hoist_dependents post-sort): dep_depth > 0 means the
    # row was hoisted directly under its (unmet, visible) dependency and is indented that
    # many levels; dep_state is "" (no dependency) else deps.dependency_state of the parent
    # ("satisfied"/"unmet"/"cancelled"/"missing"). The red marker shows for the unsatisfied
    # states whether or not the row was hoisted.
    dep_depth: int = 0
    dep_state: str = ""

    @property
    def is_open(self) -> bool:
        """The session's process is alive (its tab/pane is still open)."""
        return self.live is not None and self.live.alive

    @property
    def is_draft(self) -> bool:
        """A not-yet-started future job (saved AIM + prompt, launched on demand)."""
        return self.session.draft

    @property
    def is_finished(self) -> bool:
        """Done **and** closed — the only rows that sink to FINISHED / hide by default.

        A session marked done but still open stays in place (✓ — or ▶ while it is
        still mid-turn); it only becomes "finished" once its process is gone.
        """
        return self.status is Status.DONE and not self.is_open

    @property
    def codex_reset_hint(self) -> str | None:
        """Human hint for a Codex quota wait, or ``None`` when no live reset blocks it."""
        if not (self.uses_codex_workflow and self.codex_reset_label and self.codex_reset_at):
            return None
        return (
            f"waiting for Codex {self.codex_reset_label} reset "
            f"({usage.format_reset(self.codex_reset_at)})"
        )


def headless_leak_ids(store: Store, adapter: Adapter, live_ids: set[str]) -> set[str]:
    """Ids of parked rows whose transcript is a headless ``claude -p`` one-shot.

    These leaked in (stamped with the launching session's AIM) before the hook
    learned to skip the ``sdk`` entrypoint; ``prune`` removes them despite that
    inherited content. Currently-live ids are never included. Shared by ``ccc
    prune`` and the daemon's self-heal pass so both classify identically.
    """
    return {
        session.session_id
        for session in store.list_sessions(include_archived=True)
        if session.session_id not in live_ids
        and adapter.is_oneshot_headless(session.cwd, session.session_id)
    }


def orphan_launched_ids(store: Store, adapter: Adapter, live_ids: set[str]) -> set[str]:
    """Ids of *dead launched* rows: a future job that was started but never had a turn.

    ``ccc start-job`` clears the draft flag and execs ``claude --session-id <id>``. If that
    tab is closed before the first turn (e.g. the work happened in a different session), the
    row is stranded: no longer a FUTURE draft, yet with no ``<id>.jsonl`` on disk it can't be
    resumed either. It carries an AIM *inherited from the launch*, so the contentless guards
    in :meth:`Store.prunable_sessions` spare it and ``prune`` never reaps it — the exact
    phantom the dead-row dialog surfaces. This set lets ``prune`` (and the daemon) delete
    them despite that inherited AIM.

    A row qualifies only when it is unambiguously never-turned junk: a Claude session
    (transcript-path resolution is Claude-specific), **parked** (a dead-launched job's process
    is gone, so reconcile parks it — an ``idle`` row may just be a freshly-created session
    still awaiting its first turn / scoring, which must NOT be reaped), not a live/draft row,
    ``prompt_count`` still 0, AND no transcript resolves under any account. Requiring BOTH
    ``prompt_count==0`` and a missing transcript spares a real session whose transcript was
    merely deleted (it kept a non-zero ``prompt_count``) — those stay for the Archive/dialog
    path. ``done`` / ``keep`` rows are filtered by the caller (``prunable_sessions``).

    ``transcript_path`` is a concrete-adapter capability (like ``has_background_task`` /
    ``observed_model``), NOT part of the :class:`Adapter` protocol — probe it defensively so a
    stub adapter without it reaps nothing rather than raising.
    """
    resolve = getattr(adapter, "transcript_path", None)
    if resolve is None:
        return set()
    return {
        session.session_id
        for session in store.list_sessions(include_archived=True)
        if session.session_id not in live_ids
        and session.agent == "claude"
        and not session.draft
        and session.status == Status.PARKED.value
        and session.prompt_count == 0
        and resolve(session.cwd, session.session_id, session.config_dir) is None
    }


def resume_blockers(session_id: str, cwd: str, config_dir: str, adapter: Any = None) -> list[str]:
    """Why ``claude --resume <session_id>`` must NOT be launched (empty ⇒ clean).

    The ONE shared pre-flight for every resume-shaped surface (``ccc resume`` and the
    snapshot restore planner), so both refuse for exactly the same reasons and with the
    same wording. Three fail-closed checks, in order:

    1. **Unknown account** — ``config_dir`` is blank while several Claude accounts are
       configured: resuming could bill the wrong seat (see :mod:`.accounts`).
    2. **D9 conflict** — the id is live under two account registries at once.
    3. **No transcript** — nothing to resume; ``claude --resume`` would die with "No
       conversation found", so say so instead of launching a doomed process.

    Each entry is a full sentence WITHOUT an ``error: `` prefix, so a caller can print
    it as an error line or fold it into a plan note. *adapter* is injectable (tests, and
    ``cli`` passing its own ``_adapter()``), defaulting to a fresh :class:`ClaudeAdapter`;
    ``transcript_path`` is a concrete-adapter capability, not part of the protocol.
    """
    # Lazy, like the callers: keep core importable without pulling the account layer.
    from . import accounts  # pylint: disable=import-outside-toplevel

    if adapter is None:
        from .adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel

        adapter = ClaudeAdapter()
    blockers: list[str] = []
    if not config_dir and accounts.is_multi_account():
        blockers.append(
            f"{session_id} has no recorded Claude account (config_dir) and several are "
            "configured — refusing to resume rather than risk billing the wrong account. "
            "Start a turn from the intended account, then retry."
        )
    if accounts.live_conflict(session_id):
        blockers.append(
            f"{session_id} is live under two Claude accounts at once — close one of them, "
            "then resume."
        )
    if adapter.transcript_path(cwd, session_id, config_dir) is None:
        blockers.append(
            f"no recorded conversation for {session_id} — it never had a turn (or its "
            "transcript was deleted), so it cannot be resumed."
        )
    return blockers


def _has_background_task(adapter: Adapter, pid: int) -> bool:
    """Whether *adapter* reports a live background task for *pid* (optional capability).

    ``has_background_task`` is a concrete-adapter method (like ``has_subagent``), not part
    of the :class:`Adapter` protocol, so probe it defensively — stub adapters without it
    simply never yield SNOOZED.
    """
    fn = getattr(adapter, "has_background_task", None)
    return bool(fn and fn(pid))


def _uses_codex_workflow(adapter: Adapter, session: Session) -> bool:
    """Whether *session* is using the Codex implementation workflow.

    ``job_type`` covers ccc-launched Codex jobs. Manual slash-command / skill use is
    a concrete-adapter capability, probed defensively like ``has_background_task``;
    stub adapters without it degrade to the job_type signal only.
    """
    if (session.job_type or "claude") in CODEX_WORKFLOW_JOB_TYPES:
        return True
    fn = getattr(adapter, "uses_codex_workflow", None)
    if not fn:
        return False
    try:
        return bool(fn(session.cwd, session.session_id))
    except OSError:
        return False


def _observed_model(adapter: Adapter, session: Session) -> str:
    """The session's OBSERVED model (from its transcript) as a ccc choice label, else "".

    ``observed_model`` is a concrete-adapter capability (like ``has_background_task`` /
    ``uses_codex_workflow``), NOT part of the :class:`Adapter` protocol — probe it
    defensively so a stub adapter (or any read error) degrades to "" rather than raising.
    """
    fn = getattr(adapter, "observed_model", None)
    if fn is None:
        return ""
    try:
        return model_label(fn(session.cwd, session.session_id))
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ""


def _transcript_facts(
    adapter: Adapter,
    session: Session,
    scans: dict[str, TranscriptScan],
    dirty: list[TranscriptScan],
) -> tuple[str, bool]:
    """The session's ``(observed model label, uses-Codex)`` from ONE transcript pass.

    Both facts used to be read separately (``observed_model`` walked every line of every
    transcript, ``uses_codex_workflow`` scanned every whole file for the marker) on every
    build — 34 s of a cold ``ccc ls`` over 677 sessions. ``scan_transcript`` answers both
    from a single ``stat()`` when the file is unchanged since the persisted *scans* row,
    which is the case for every done/parked session.

    *scans* is updated in place and every row whose transcript actually changed is appended
    to *dirty* for one batched write at the end of the pass. ``scan_transcript`` is a
    concrete-adapter capability, not part of the :class:`Adapter` protocol, so an adapter
    without it (stubs in tests) falls back to the legacy per-fact reads.
    """
    job_hit = (session.job_type or "claude") in CODEX_WORKFLOW_JOB_TYPES
    scan_fn = getattr(adapter, "scan_transcript", None)
    if scan_fn is None:
        return _observed_model(adapter, session), _uses_codex_workflow(adapter, session)
    sid = session.session_id
    prior = scans.get(sid)
    try:
        new = scan_fn(session.cwd, sid, prior)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        new = None
    if new is None:  # no transcript (never had a turn, or deleted)
        return "", job_hit
    if new is not prior:  # identity: the same object means the file was untouched
        scans[sid] = new
        dirty.append(new)
    return model_label(new.model), job_hit or new.codex


def _session_effort(adapter: Adapter, pid: int) -> str | None:
    """The ``--effort`` level of the live process *pid* (optional adapter capability).

    Probed defensively like :func:`_observed_model`; ``None`` when unsupported, absent, or
    the read raised. A non-``None`` return is an authoritative, validated level.
    """
    fn = getattr(adapter, "session_effort", None)
    if fn is None:
        return None
    try:
        return fn(pid)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


def _settings_effort_level(config_dir: str = "") -> str:
    """The account's default reasoning effort (``effortLevel`` in its settings.json).

    Best-effort plain JSON read of ``<config_dir>/settings.json`` (the default account
    — ``claude_home()`` — when *config_dir* is ""). Returns "" when the file is
    missing/unreadable, is not a JSON object, or the key is absent / not one of
    :data:`EFFORT_LEVELS`. Used as the fill-once effort for a live session that carries
    no explicit ``--effort`` flag. Callers that read several accounts in one pass should
    realpath-dedupe (see :func:`reconcile`): on this machine every account's
    settings.json is the same stow-managed file.
    """
    base = Path(config_dir).expanduser() if config_dir else config.claude_home()
    try:
        data = json.loads((base / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    level = data.get("effortLevel")
    return level if isinstance(level, str) and level in EFFORT_LEVELS else ""


def _settings_effort_cached(config_dir: str, cache: dict[str, str]) -> str:
    """:func:`_settings_effort_level` for *config_dir*, memoised by REALPATH.

    The cache key is the resolved settings.json path, so two accounts whose
    settings.json symlink to the same stow file (this machine) read it exactly once.
    """
    base = Path(config_dir).expanduser() if config_dir else config.claude_home()
    try:
        key = str((base / "settings.json").resolve())
    except OSError:
        key = str(base / "settings.json")
    if key not in cache:
        cache[key] = _settings_effort_level(config_dir)
    return cache[key]


def reconcile(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    store: Store, adapter: Adapter, codex_usage: Any = _UNSET
) -> dict[str, TranscriptScan]:
    """Sync the store with the live registry; park sessions that are no longer live.

    Returns the transcript scans (session id → :class:`TranscriptScan`) it worked from,
    already updated for this pass and persisted — :func:`build_rows` reuses them instead of
    touching the transcripts a second time. *codex_usage* lets a caller that already read
    the Codex snapshot pass it in rather than have it read twice per build; unset means
    read it here (every other caller is unchanged).
    """
    snapshot = usage.read_codex_usage() if codex_usage is _UNSET else codex_usage
    codex_block = usage.codex_exhausted_window(snapshot)
    codex_exhausted = codex_block is not None
    scans = store.transcript_scans()
    dirty: list[TranscriptScan] = []
    live_by_id: dict[str, LiveSession] = {ls.session_id: ls for ls in adapter.discover()}
    # Per-account settings.json effort, realpath-deduped: on this machine every
    # account's settings.json is the same stow file, so the resolved-path key reads it
    # once and reuses the value for the other account (never process one file twice).
    settings_effort_by_realpath: dict[str, str] = {}
    for live in live_by_id.values():
        store.upsert_from_live(live)
        stored = store.get(live.session_id)
        halted = live.alive and adapter.is_halted(live.cwd, live.session_id)
        background = live.alive and _has_background_task(adapter, live.pid)
        # One transcript pass for BOTH facts (model + Codex marker); see _transcript_facts.
        observed_model, uses_codex = ("", False)
        if stored is not None:
            observed_model, uses_codex = _transcript_facts(adapter, stored, scans, dirty)
        status = derive_status(
            live,
            stored,
            halted=halted,
            background=background,
            codex_waiting=uses_codex and codex_exhausted,
        )
        fields: dict[str, object] = {
            "status": status.value,
            "last_response_at": adapter.last_activity_ms(live),
            "last_seen_pid": live.pid,
        }
        # Alive again — a stale close stamp no longer applies (resume/reopen).
        if stored is not None and stored.closed_at:
            fields["closed_at"] = 0
        # Persist the last-observed live account (D3) — only on change, and NEVER on a
        # D9 conflict (discover leaves config_dir "" then, so this is a no-op and the
        # stored value is left untouched rather than mis-attributed).
        if live.config_dir and live.config_dir != (stored.config_dir if stored else ""):
            fields["config_dir"] = live.config_dir
        # Stamp the Claude Code version from the transcript; only when read (never
        # clobber a good stored value with None on a transient read miss).
        version = adapter.claude_version(live.cwd, live.session_id)
        if version:
            fields["version"] = version
        if stored is not None:
            # OBSERVED model (from the transcript) — persist only when it changed, so an
            # unchanged model adds no key (the status/last_response write happens anyway).
            model = observed_model
            if model and model != (stored.model or ""):
                fields["model"] = model
            # Effort: an explicit --effort flag on the live process is authoritative; with
            # no flag, fill the session's effort ONCE from this account's settings default —
            # never backfill a historical value (a parked session keeps what it ran with).
            effort = _session_effort(adapter, live.pid)
            if effort:
                if effort != (stored.effort or ""):
                    fields["effort"] = effort
            elif not (stored.effort or ""):
                settings_effort = _settings_effort_cached(
                    live.config_dir, settings_effort_by_realpath
                )
                if settings_effort:
                    fields["effort"] = settings_effort
        store.update_fields(live.session_id, **fields)
    # Anything tracked but no longer in the live registry is parked (unless done or a
    # draft future job, which has no process and owns its own status until launched).
    for session in store.list_sessions():
        if session.session_id in live_by_id or session.draft:
            continue
        # OBSERVED model persists for parked/done sessions too (the transcript outlives the
        # process). Effort is NOT touched here — it is a live-only capture (no --effort flag
        # to read once the process is gone, and today's settings default must never backfill
        # a historical row). Accumulate so a no-change pass writes nothing (byte-stable).
        updates: dict[str, object] = {}
        model, _ = _transcript_facts(adapter, session, scans, dirty)
        if model and model != (session.model or ""):
            updates["model"] = model
        if session.done:
            # Self-heal: done always wins. A done-then-closed session can carry a
            # stale PARKED stamp (a pre-fix close wrote it); restore DONE so the
            # row sinks to FINISHED instead of lingering in the active list.
            if session.status != Status.DONE.value:
                updates["status"] = Status.DONE.value
        elif session.status != Status.PARKED.value:
            updates["status"] = Status.PARKED.value
            # The live→gone transition: this pass is the first to see the process gone,
            # so stamp WHEN the session closed. Rows already PARKED (or closed before
            # this field existed) are never stamped — display falls back to
            # last_response_at as the approximate close time.
            updates["closed_at"] = now_ms()
        if updates:
            store.update_fields(session.session_id, **updates)
    # One batched write for every transcript that actually changed (usually a handful of
    # live sessions); an all-frozen pass writes nothing at all.
    store.put_transcript_scans(dirty)
    return scans


DEFAULT_FOLDER_ORDER = ("home", "infra", "llms", "sdsc")


def _category_rank(cwd: str, order: tuple[str, ...], root: str) -> int:
    """Rank of *cwd*'s category within *order*; unknown / outside the tree → last."""
    hit = repos.category_of(cwd, root)
    if hit is None:
        return len(order)
    category = hit[0]
    return order.index(category) if category in order else len(order)


def _sort_key(
    row: Row, folder_order: tuple[str, ...], root: str
) -> tuple[int, int, int, float, int]:
    """Sort key: strict category grouping; FUTURE, FINISHED, SCHEDULED sink last.

    Four top-level buckets: active rows (0), then FUTURE draft jobs (1), then
    FINISHED rows (2), then SCHEDULED draft jobs (3 — drafts with a fixed
    ``start_date``, at the very bottom, soonest date first). Active rows are
    grouped *strictly* by category (``folder_order``, resolved against *root*) so
    each category forms one contiguous block and appears exactly once — never split
    across an AIM / no-AIM divide. Within a category, AIM-defined sessions sort first,
    then most progress (a session with no checklist sorts last), then most-recent
    activity. The FUTURE bucket is grouped by category (newest first); the
    FINISHED bucket is flat, most-recently-finished first. Status no longer
    drives the order — it is read from the first-column icon.
    """
    if row.is_draft:  # not-yet-started future job
        when = scheduled_date(row.session)
        if when is not None:  # fixed start date → SCHEDULED bucket at the very bottom
            return (3, 0, 0, float(when.toordinal()), -row.session.created_at)
        return (
            1,
            _category_rank(row.session.cwd, folder_order, root),
            0,
            0.0,
            -row.session.created_at,
        )
    if row.is_finished:  # done AND closed — sinks to the bottom FINISHED bucket
        return (2, 0, 0, 0.0, -row.session.last_response_at)
    progress = (row.checked / row.total) if row.total else -1.0
    return (
        0,
        _category_rank(row.session.cwd, folder_order, root),
        0 if row.session.aim else 1,  # AIM-first, but only WITHIN the category block
        -progress,
        -row.session.last_response_at,
    )


def _hoist_dependents(rows: list[Row], get_session: Callable[[str], Session | None]) -> list[Row]:
    """Reorder *rows* so a job with an UNMET, visible dependency sits directly under it.

    Post-sort placement pass (adjacency, not a re-sort). For every row with a
    ``depends_on`` it (1) sets ``row.dep_state`` from :func:`deps.dependency_state` (using
    the visible parent row when present, else *get_session* for an archived/pruned parent —
    so the marker renders even when the parent is hidden), and (2) HOISTS the row directly
    after its parent (``dep_depth = parent.dep_depth + 1``) IFF the parent is in the visible
    list AND the state is ``unmet`` AND the chain is cycle-free. Children keep their relative
    sort order under a shared parent; chains nest recursively.

    HARD INVARIANT: the output is a permutation of the input — every row exactly once. A
    cycle / self-dependency / any anomaly degrades to unhoisted placement (the row stays put
    with ``dep_depth = 0``), never dropped or duplicated (a permutation guard falls back to
    the original order if reconstruction ever misses a row).
    """
    by_id = {r.session.session_id: r for r in rows}
    for row in rows:
        row.dep_depth = 0  # rebuilt each call
        dep = (row.session.depends_on or "").strip()
        if not dep:
            row.dep_state = ""
            continue
        parent_row = by_id.get(dep)
        parent = parent_row.session if parent_row is not None else get_session(dep)
        row.dep_state = deps.dependency_state(parent)

    children: dict[str, list[Row]] = {}
    hoisted: set[str] = set()
    for row in rows:
        rid = row.session.session_id
        dep = (row.session.depends_on or "").strip()
        if not dep or dep not in by_id or row.dep_state != deps.UNMET:
            continue
        if row.session.done:  # a done job no longer waits — never hoist it out of FINISHED
            continue
        if deps.would_create_cycle(get_session, rid, dep):
            continue  # cycle / self-dep → leave the row in place (marker still shows)
        children.setdefault(dep, []).append(row)
        hoisted.add(rid)
    if not hoisted:
        return rows  # nothing to reorder

    out: list[Row] = []
    seen: set[str] = set()

    def _emit(row: Row, depth: int) -> None:
        rid = row.session.session_id
        if rid in seen:  # defensive: never emit a row twice (cycle-safety belt)
            return
        seen.add(rid)
        row.dep_depth = depth
        out.append(row)
        for child in children.get(rid, []):
            _emit(child, depth + 1)

    for row in rows:
        if row.session.session_id not in hoisted:  # roots (non-hoisted) drive the walk
            _emit(row, 0)
    if len(out) != len(rows):  # permutation guard — any anomaly → original order
        for row in rows:
            row.dep_depth = 0
        return rows
    return out


def build_rows(
    store: Store,
    adapter: Adapter,
    include_done: bool = True,
    done_max_age_days: int = 0,
    folder_order: tuple[str, ...] = DEFAULT_FOLDER_ORDER,
    include_future: bool = True,
    reconcile_first: bool = True,
) -> list[Row]:
    """Reconcile, then return display rows grouped strictly by repo category.

    Active rows are grouped by *folder_order* (repo category, resolved against the
    configured ``repo_root``) so each category appears exactly once; within a category,
    AIM-defined sessions sort first, then most progress. FUTURE (draft) jobs then
    FINISHED (done) rows sink to the bottom.
    ``done_max_age_days`` > 0 hides done sessions finished more than that many days
    ago (by ``done_at``, falling back to last activity); 0 shows all.
    ``include_future`` toggles whether not-yet-started future jobs are listed.

    ``reconcile_first=False`` builds the rows from the stored state and the PERSISTED
    transcript scans alone — no registry reconcile, no transcript read, no usage read. That
    is the quick first paint (a caller then reconciles in the background and rebuilds); the
    rows are as fresh as the last pass that did reconcile.
    """
    if reconcile_first:
        # Read the Codex snapshot ONCE and hand it to reconcile (it needs the same one).
        codex_usage = usage.read_codex_usage()
        scans = reconcile(store, adapter, codex_usage=codex_usage)
        codex_block = usage.codex_exhausted_window(codex_usage)
    else:  # quick first paint: stored rows + persisted facts only, no transcript/usage work
        scans = store.transcript_scans()
        codex_block = None
    live_by_id: dict[str, LiveSession] = {ls.session_id: ls for ls in adapter.discover()}
    codex_label = codex_block[0] if codex_block else None
    codex_reset_at = codex_block[1].resets_at if codex_block else None
    now = now_ms()
    rows: list[Row] = []
    seen: set[str] = set()  # belt-and-suspenders: never emit a session id twice
    for session in store.list_sessions():
        if session.session_id in seen:
            continue
        seen.add(session.session_id)
        if session.draft and not include_future:  # FUTURE jobs hidden via the `tf` toggle
            continue
        status = Status(session.status)
        live = live_by_id.get(session.session_id)
        is_open = live is not None and live.alive
        # FINISHED = done AND closed. A done session that is still open is never
        # hidden or aged out — it stays in the active list (✓, or ▶ while still
        # mid-turn: derive_status keeps a busy done session WORKING).
        if status is Status.DONE and not is_open:
            if not include_done:
                continue
            if done_max_age_days > 0:
                stamp = session.done_at or session.last_response_at
                if stamp and now - stamp > done_max_age_days * _DAY_MS:
                    continue
        # Weighted progress: essential sub-goals count more. Degenerates to the plain
        # count when every weight is 1, so unweighted checklists are unaffected.
        checked, total = store.progress_weighted(session.session_id)
        # No file access here: the marker was settled by the scan pass (or a previous one).
        if hasattr(adapter, "scan_transcript"):
            sid = session.session_id
            uses_codex = (session.job_type or "claude") in CODEX_WORKFLOW_JOB_TYPES or (
                scans[sid].codex if sid in scans else False
            )
        else:  # stub adapter without the scan capability → the legacy per-session read
            uses_codex = _uses_codex_workflow(adapter, session)
        rows.append(
            Row(
                session,
                live,
                status,
                checked,
                total,
                uses_codex,
                codex_label if uses_codex else None,
                codex_reset_at if uses_codex else None,
            )
        )
    root = repos.repo_root()
    rows.sort(key=lambda r: _sort_key(r, folder_order, root))
    # Post-sort placement: hoist a job with an unmet, visible dependency under its parent
    # (and set every row's dep_state for the marker). A permutation of the sorted rows.
    return _hoist_dependents(rows, store.get)
