"""Idle-reaper + housekeeping daemon (one pass per invocation).

Designed for launchd ``StartInterval`` (run ``ccc daemon`` every few minutes);
``--loop`` runs it continuously for manual use. Each pass:

1. reconcile the store with the live registry,
2. reap interactive sessions idle past the timeout (SIGTERM, keep the tab),
3. run done-check commands and flip finished sessions to done,
4. regenerate summary + next-step for stale sessions (best-effort LLM),
5. fire deadline / stale-goal alerts (throttled to once per day per session).

Reaping is deliberately conservative: only ``interactive``, only ``idle`` (never
busy/waiting), never ``keep``/``done``, and never while a child process (a
running tool) is alive.
"""

# Lazy `subprocess`/`.llm` imports keep import cost off the fast paths
# (import-outside-toplevel); the single-pass orchestration is branchy by nature.
# pylint: disable=import-outside-toplevel,too-many-branches

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config
from .adapters.claude import ClaudeAdapter
from .core import headless_leak_ids, orphan_launched_ids, reconcile
from .models import Session, Status, deadline_badge, now_ms
from .notify import notify
from .store import Store


@dataclass
class DaemonReport:  # pylint: disable=too-many-instance-attributes  # pure per-pass tally
    """What a single daemon pass did (also used for --dry-run preview)."""

    reaped: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    summarized: list[str] = field(default_factory=list)
    progressed: list[str] = field(default_factory=list)
    alerted: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    scored: list[str] = field(default_factory=list)
    short_aimed: list[str] = field(default_factory=list)
    assessed: list[str] = field(default_factory=list)  # AIM-met self-assessments this pass
    reset_fired: list[str] = field(default_factory=list)  # armed parked prompts dispatched
    reset_postponed: list[str] = field(default_factory=list)  # armed jobs pushed to a later reset
    copilot_refreshed: bool = False  # routine; deliberately excluded from is_empty()
    claude_refreshed: bool = False  # routine Claude /usage OAuth fetch; excluded from is_empty()
    resume_spawned: bool = False  # spawned the resume-halted watcher; excluded from is_empty()

    def is_empty(self) -> bool:
        return not (
            self.reaped
            or self.done
            or self.summarized
            or self.progressed
            or self.alerted
            or self.pruned
            or self.scored
            or self.short_aimed
            or self.assessed
            or self.reset_fired
            or self.reset_postponed
        )


def _label(session: Session) -> str:
    if session.name:
        return session.name
    return os.path.basename(session.cwd.rstrip("/")) or session.session_id[:8]


def _has_child_process(pid: int) -> bool:
    """True if *pid* has any child process (a running tool) — be conservative on error."""
    import subprocess

    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5, check=False
        )
    except (subprocess.SubprocessError, OSError):
        return True
    return bool(result.stdout.strip())


def _reap(pid: int) -> None:
    """SIGTERM a session process, then SIGKILL if it lingers. Transcript persists."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(10):
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_check(command: str, cwd: str) -> bool:
    from . import checks

    return checks.run_exit0(command, cwd, timeout=60)


def _alert_state_path() -> Path:
    return config.app_home() / "alerts.json"


def _load_alert_state() -> dict[str, str]:
    try:
        data = json.loads(_alert_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alert_state(state: dict[str, str]) -> None:
    try:
        _alert_state_path().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def run_once(  # pylint: disable=too-many-locals,too-many-statements  # linear pass, one step per concern
    *,
    dry_run: bool = False,
    do_reap: bool = True,
    do_summary: bool = True,
    do_progress: bool = True,
    do_alerts: bool = True,
) -> DaemonReport:
    """Execute one housekeeping pass and return what it did."""
    cfg = config.load_config()
    adapter = ClaudeAdapter()
    report = DaemonReport()

    with Store() as store:
        reconcile(store, adapter)
        live = {ls.session_id: ls for ls in adapter.discover()}
        now = now_ms()

        # Mirror each FUTURE job (draft) to its Obsidian markdown file and import file edits
        # back (in-process backstop for the launchd WatchPaths trigger). Placed after
        # reconcile, before the self-heal backfills; skipped on dry-run (it writes files).
        if not dry_run:
            _sync_future_files(store, cfg)

        # Self-heal: delete leftover rows from headless `claude -p` runs (e.g.
        # ai.py commit-message generation that leaked a row stamped with the
        # launching session's AIM, or contentless junk a pre-fix ccc reconciled
        # in). Conservative — never a row that is live, done, or kept.
        if cfg.prune_headless:
            victims = store.prunable_sessions(
                protect_ids=live.keys(),
                headless_ids=headless_leak_ids(store, adapter, set(live)),
                orphan_ids=orphan_launched_ids(store, adapter, set(live)),
            )
            report.pruned = [s.session_id for s in victims]
            if victims and not dry_run:
                store.delete_many(s.session_id for s in victims)

        if do_reap and cfg.reap:
            for session in store.list_sessions():
                live_session = live.get(session.session_id)
                if live_session is None or not live_session.alive:
                    continue
                if live_session.kind != "interactive" or session.keep or session.done:
                    continue
                if Status(session.status) is not Status.IDLE:
                    continue
                idle_min = (
                    (now - session.last_response_at) / 60000 if session.last_response_at else 0
                )
                if idle_min < cfg.idle_timeout_min:
                    continue
                if _has_child_process(live_session.pid):
                    continue
                report.reaped.append(session.session_id)
                if not dry_run:
                    _reap(live_session.pid)
                    store.update_fields(
                        session.session_id, status=Status.PARKED.value, auto_closed=True
                    )

        for session in store.list_sessions():
            if dry_run or session.done or not session.done_check_cmd:
                continue
            if _run_check(session.done_check_cmd, session.cwd):
                report.done.append(session.session_id)
                if not dry_run:
                    store.update_fields(
                        session.session_id, done=True, status=Status.DONE.value, done_at=now_ms()
                    )
                    notify("✅ Session done", _label(session), cfg.notify if do_alerts else [])

        # Self-heal: score any AIM that slipped in unscored (aim_score < 0), so the
        # "vague → sharpen" machinery and the /aim chip always have a number. New
        # AIMs are scored at their source now; this backfills legacy/edge rows.
        _backfill_aim_scores(store, cfg, report, dry_run)

        # Self-heal: generate a short-AIM label for any AIM session still missing one
        # (legacy rows from before the feature, or a set-aim whose detached codex run died).
        _backfill_short_aims(store, cfg, report, dry_run)

        # Self-heal: fill the Claude Code version for parked/legacy rows. reconcile stamps
        # every live session each pass; this reads the transcript once for the rest.
        _backfill_versions(store, adapter, dry_run)

        # Keep the GitHub Copilot usage card warm (throttled gh billing API call; the
        # throttle tightens while any job is actively working — see _refresh_copilot_usage).
        _refresh_copilot_usage(store, cfg, report, dry_run)

        # Keep each Claude account's usage card in step with `claude`'s own /usage via the
        # OAuth usage endpoint (throttled per account; adds the Fable weekly window).
        _refresh_claude_usage(store, cfg, report, dry_run)

        # Dispatch armed parked-prompt jobs whose fire time has passed (see park.py).
        _fire_reset_jobs(store, cfg, report, dry_run)

        # Deliver parked prompts ATTACHED to existing sessions (fire_at on a
        # non-draft row): typed into the live tab, or a resume tab as fallback.
        _deliver_attached_prompts(store, cfg, report, dry_run, live)

        # Auto-resume session-limit-halted sessions: spawn the watcher when work exists.
        _spawn_resume_watcher(cfg, report, dry_run)

        if do_summary and cfg.summarize:
            _regenerate_summaries(store, adapter, cfg, report, dry_run)

        if do_progress and cfg.autoprogress:
            _run_autoprogress(store, adapter, report, dry_run)

        # Fallback "is the AIM fulfilled?" self-assessment for eligible sessions whose Stop-hook
        # spawn was missed (the hook is the primary, per-turn trigger). Bounded per pass.
        if do_progress and cfg.assess_aim_on_turn:
            _run_assess_aim(store, adapter, cfg, report, dry_run)

        if do_alerts:
            _run_alerts(store, cfg, report, dry_run)

        # Converge every live tab's iTerm title onto its badge (marker-preserving), so a
        # symbol assigned mid-session — which the `cd`-driven zsh hook can't reach while a
        # CLI holds the foreground — still reaches the tab. Side-effect only (no report
        # noise every pass); skipped on dry-run since it assigns badges + sets titles.
        if cfg.sync_tab_titles and not dry_run:
            from . import tabsymbol

            tabsymbol.sync_live(store)

        # Export-only mirrors of RUNNING + DONE sessions to the vault — the LAST pass so it
        # reflects every state change made above (done-checks, autoprogress, alerts). Skipped
        # on dry-run (it writes files); a failure must never break housekeeping.
        if not dry_run:
            _sync_mirrors(store, cfg)

    return report


def _sync_mirrors(store: Store, cfg: config.Config) -> None:
    """Reconcile the RUNNING/DONE/SESSION mirrors, in-process (best-effort).

    Gated on the ``mirror_running`` / ``mirror_done`` / ``mirror_sessions`` kill-switches;
    a sync failure (a bad file, a disk error) is caught and logged so it can never break
    the housekeeping pass.
    """
    if not (cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions):
        return
    from . import mirrors

    try:
        mirrors.run_mirrors(store, cfg)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"daemon: mirror sync failed: {exc}", file=sys.stderr)


def _sync_future_files(store: Store, cfg: config.Config) -> None:
    """Reconcile FUTURE-job draft rows with their Obsidian files, in-process (best-effort).

    A backstop for the launchd WatchPaths trigger: even with no file-system event each pass
    converges the store and the vault. A sync failure (a bad file, a disk error) must never
    break the housekeeping pass, so it is caught and logged.
    """
    if not cfg.future_files:
        return
    from . import futuresync

    try:
        futuresync.run_sync(store, cfg)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"daemon: future-file sync failed: {exc}", file=sys.stderr)


def _backfill_aim_scores(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Score any session whose AIM is still unscored (``aim_score < 0``).

    Sets the instant lexical score now, then (if enabled) spawns the cheap LLM
    refine detached. Idempotent: once a session is scored (``>= 0``) it is skipped,
    so the LLM refine fires at most once per AIM.
    """
    from . import aimscore

    for session in store.list_sessions():
        if not session.aim or session.aim_score >= 0:
            continue
        report.scored.append(session.session_id)
        if dry_run:
            continue
        store.update_fields(session.session_id, aim_score=aimscore.score_aim_lexical(session.aim))
        if cfg.aim_score_on_set:
            from . import spawn

            spawn.spawn_ccc(["score-aim", "--session", session.session_id])


def _backfill_short_aims(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Generate a short-AIM label for any AIM session that still lacks one.

    Spawns the cheap ``ccc short-aim`` generator detached (the codex run is slow). Idempotent:
    once a session has a ``short_aim`` it is skipped, so it fires at most once per AIM (and is
    re-cleared on the next AIM change by ``set_aim``). Capped per pass to bound codex usage.
    """
    if not cfg.short_aim:
        return
    pending = [s for s in store.list_sessions() if s.aim and not s.short_aim and not s.done]
    for session in pending[: cfg.max_summaries_per_run]:
        report.short_aimed.append(session.session_id)
        if dry_run:
            continue
        from . import spawn

        spawn.spawn_ccc(["short-aim", "--session", session.session_id])


def _refresh_copilot_usage(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Refresh the cached GitHub Copilot usage when it is stale, so the card stays warm.

    Best-effort and throttled by ``copilot_usage_refresh_sec`` (the ``gh`` call is the only
    network hit in a pass) — but while any tracked session is actively working the throttle
    drops to ``copilot_usage_refresh_active_sec`` (via :func:`usage.adaptive_interval`) so the
    card tracks reality more closely during active work. Keeps the card current even when no
    TUI is open; if ``gh`` is unavailable in the daemon's environment the fetch simply returns
    ``None`` and the last cache stands (the interactive TUI also self-refreshes).
    """
    from . import usage

    if not cfg.copilot_usage or dry_run:
        return
    active = usage.has_active_work(s.status for s in store.list_sessions())
    throttle = usage.adaptive_interval(
        cfg.copilot_usage_refresh_sec, cfg.copilot_usage_refresh_active_sec, active=active
    )
    if not usage.copilot_usage_stale(throttle):
        return
    report.copilot_refreshed = usage.fetch_copilot_usage() is not None


def _refresh_claude_usage(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Refresh each account's cached Claude ``/usage`` (OAuth endpoint) when it is stale.

    Mirrors :func:`_refresh_copilot_usage`: gated on ``claude_usage``, throttled by
    ``claude_usage_refresh_sec`` (or the shorter ``claude_usage_refresh_active_sec`` via
    :func:`usage.adaptive_interval` while any job works), keyed per account on
    ``oauth_fetched_at`` (:func:`usage.claude_usage_stale`). Keeps the cards in step with
    ``claude``'s own /usage even when no TUI is open; self-heals a rebased window boundary
    and adds the Fable weekly window. Best-effort: an account with no keychain token simply
    yields no write and the last cache stands.
    """
    from . import usage

    if not cfg.claude_usage or dry_run:
        return
    active = usage.has_active_work(s.status for s in store.list_sessions())
    throttle = usage.adaptive_interval(
        cfg.claude_usage_refresh_sec, cfg.claude_usage_refresh_active_sec, active=active
    )
    for label in config.claude_config_dirs():
        if usage.claude_usage_stale(label, throttle):
            if usage.fetch_claude_usage(label) is not None:
                report.claude_refreshed = True


def _fire_reset_jobs(store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool) -> None:
    """Dispatch armed parked-prompt jobs (``fire_at`` reached) in new tabs.

    Fallback dispatcher behind the foreground ``ccc park`` waiter: only jobs at least
    :data:`park.FIRE_GRACE_SEC` past their fire time qualify, so a live waiter (which
    fires exactly on time in its own tab) always wins its job. Per job, the job's OWN
    window may postpone when a fresh snapshot still shows it exhausted
    (:func:`park.postpone_until` — `_refresh_claude_usage` just ran, so the snapshot
    is as fresh as it gets). Otherwise RE-ARM FORWARD (``fire_at = now + retry``)
    BEFORE dispatching — a single-column lease: the launched ``start-job --auto``
    consumes it via the atomic claim; a crash mid-dispatch or a tab that never ran
    simply retries one lease later. Never a config key: the per-job ``--at-reset`` /
    ``ccc park`` arming is the opt-in (inert on a fresh install — no armed jobs).
    """
    from . import accounts, park, terminal, usage

    now = int(time.time())
    due = [
        s
        for s in store.list_sessions()
        if s.draft and not s.archived and 0 < s.fire_at <= now - park.FIRE_GRACE_SEC
    ]
    for job in due:
        label = accounts.effective_account_label(job.config_dir or "")
        postponed = park.postpone_until(usage.read_usage(label), job.fire_window, now)
        if postponed is not None and postponed > job.fire_at:
            report.reset_postponed.append(job.session_id)
            if not dry_run:
                store.update_fields(job.session_id, fire_at=postponed)
                notify(
                    "⏳ parked prompt postponed",
                    f"{_label(job)} — {job.fire_window} still exhausted; "
                    + park.format_fire(postponed, now),
                    cfg.notify,
                )
            continue
        report.reset_fired.append(job.session_id)
        if dry_run:
            continue
        store.update_fields(job.session_id, fire_at=now + park.FIRE_RETRY_SEC)
        terminal.start_job_in_new_tab(job.session_id, auto=True)
        notify("⏳ parked prompt fired", _label(job), cfg.notify)


def _deliver_attached_prompts(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool, live: dict
) -> None:
    """Deliver parked prompts ATTACHED to existing sessions (``fire_at`` on a non-draft row).

    The "opened the session with its AIM first, defined the real prompt with qp"
    workflow: at fire time the prompt is TYPED into the session's live iTerm tab
    (bracketed paste + CR — same context, same billing, no new session). When the
    tab or process is gone, a new tab resumes the session WITH the prompt
    (``ccc fire-attached`` → ``claude --resume <id> "<prompt>"``). Same
    re-arm-forward lease as :func:`_fire_reset_jobs`; the injection path zeroes
    ``fire_at`` on success and ``fire-attached`` claims it with the one-shot
    conditional update, so nothing delivers twice. No grace offset: no foreground
    waiter ever competes for an attached prompt.
    """
    from . import accounts, park, terminal, usage

    now = int(time.time())
    due = [
        s
        for s in store.list_sessions()
        if not s.draft and not s.archived and (s.prompt or "").strip() and 0 < s.fire_at <= now
    ]
    for job in due:
        label = accounts.effective_account_label(job.config_dir or "")
        postponed = park.postpone_until(usage.read_usage(label), job.fire_window, now)
        if postponed is not None and postponed > job.fire_at:
            report.reset_postponed.append(job.session_id)
            if not dry_run:
                store.update_fields(job.session_id, fire_at=postponed)
                notify(
                    "⏳ parked prompt postponed",
                    f"{_label(job)} — {job.fire_window} still exhausted; "
                    + park.format_fire(postponed, now),
                    cfg.notify,
                )
            continue
        report.reset_fired.append(job.session_id)
        if dry_run:
            continue
        store.update_fields(job.session_id, fire_at=now + park.FIRE_RETRY_SEC)  # lease
        live_session = live.get(job.session_id)
        if (
            live_session is not None
            and getattr(live_session, "alive", False)
            and job.iterm_session_id
            and terminal.send_text_to_session(job.iterm_session_id, (job.prompt or "").strip())
        ):
            store.update_fields(job.session_id, fire_at=0)
            notify("⏳ parked prompt delivered", _label(job), cfg.notify)
            continue
        # Tab/process gone (or injection failed): resume WITH the prompt in a new
        # tab; fire-attached consumes the lease via its one-shot claim.
        terminal.fire_attached_in_new_tab(job.session_id)
        notify("⏳ parked prompt resuming in a new tab", _label(job), cfg.notify)


def _spawn_resume_watcher(cfg: config.Config, report: DaemonReport, dry_run: bool) -> None:
    """Spawn the resume-halted watcher (detached) when it has work to do.

    "Work" (resume.has_work) covers live candidates, due backoff retries, and
    failed-entry maintenance (prune/revive) — not just halted candidates, so cleanup
    runs even when every affected session is already finished. No lock precheck (it
    would be racy): the watcher self-singletons via flock and exits immediately if
    another holds it, so a redundant spawn is harmless. Skipped when the feature is
    off or there is nothing to act on.
    """
    from . import resume

    if not cfg.resume_halted or dry_run:
        return
    if not resume.has_work():
        return
    from . import spawn

    report.resume_spawned = spawn.spawn_ccc(["resume-halted", "--watch"])


def _backfill_versions(store: Store, adapter: ClaudeAdapter, dry_run: bool) -> None:
    """Fill the Claude Code ``version`` for any session still missing one.

    ``reconcile`` stamps every *live* session's version each pass; this self-heals
    parked/legacy rows (which reconcile never touches) by reading their transcript
    once. Cheap — a tail read, no LLM — and idempotent: once a row has a version it
    is skipped, so only the first pass after a session appears does any work.
    Side-effect only, so no report noise; skipped on dry-run.
    """
    if dry_run:
        return
    for session in store.list_sessions():
        if session.version or session.draft:
            continue
        version = adapter.claude_version(session.cwd, session.session_id)
        if version:
            store.update_fields(session.session_id, version=version)


def _run_autoprogress(
    store: Store, adapter: ClaudeAdapter, report: DaemonReport, dry_run: bool
) -> None:
    from . import autoprogress

    for res in autoprogress.run_pass(store, adapter, dry_run=dry_run):
        if res.changed() or (dry_run and res.note.startswith("would")):
            report.progressed.append(res.session_id)


def _run_assess_aim(
    store: Store, adapter: ClaudeAdapter, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Fallback AIM-met assessment for eligible sessions with a new turn since last assessed.

    The Stop hook is the primary, per-turn trigger (detached); this is the safety net for a
    session whose hook spawn was missed. Capped at ``max_aim_assess_per_run`` (oldest new-turn
    first) so a pass stays bounded; ``aimmet.run_for_session`` re-checks eligibility, the
    new-turn gate and the stale-write guard authoritatively.
    """
    if dry_run:
        return
    from . import aimmet

    due = [
        s
        for s in store.list_sessions()
        if aimmet.eligible(s, cfg) and s.last_response_at > s.aim_assessed_at
    ]
    due.sort(key=lambda s: s.last_response_at)
    for session in due[: cfg.max_aim_assess_per_run]:
        if aimmet.run_for_session(store, adapter, session, cfg) is not None:
            report.assessed.append(session.session_id)


def _regenerate_summaries(
    store: Store, adapter: ClaudeAdapter, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    from . import autoprogress, llm

    stale = [s for s in store.list_sessions() if s.needs_summary and not s.done]
    stale.sort(key=lambda s: s.last_response_at)
    for session in stale[: cfg.max_summaries_per_run]:
        report.summarized.append(session.session_id)
        if dry_run:
            continue
        transcript = adapter.transcript_path(session.cwd, session.session_id)
        # note = the session's first/original AIM (log context for ai.py's call log).
        note = llm.concise_note(
            next((r.aim for r in store.list_aim_history(session.session_id)), "") or session.aim
        )
        summary, next_step = llm.summarize(session.aim, transcript, cfg.llm_model, note=note)
        fields: dict[str, object] = {"needs_summary": False}
        if summary:
            fields["summary"] = summary
        # Never overwrite a next step the user authored via /next.
        if next_step and session.next_step_source != "user":
            fields["next_step"] = next_step
            fields["next_step_source"] = "auto"
        store.update_fields(session.session_id, **fields)
        # The session just went idle: re-grade the whole transcript so the bar catches
        # up to behavioural goals whose evidence was split across turns (the per-turn
        # delta grader is conservative). Never derives; only ticks an auto checklist.
        if cfg.autoprogress:
            res = autoprogress.run_for_session(
                store, session.session_id, transcript, model=cfg.llm_model, full_regrade=True
            )
            if res.changed():
                report.progressed.append(session.session_id)


def _run_alerts(store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool) -> None:
    today = date.today()
    today_iso = today.isoformat()
    state = _load_alert_state()
    changed = False

    for session in store.list_sessions():
        if session.done or session.draft:  # a not-yet-started future job can't be stale/overdue
            continue
        reason = _alert_reason(session, cfg, today)
        if reason is None:
            continue
        if state.get(session.session_id) == today_iso:  # already alerted today
            continue
        report.alerted.append(session.session_id)
        if not dry_run:
            notify(f"⏰ {reason}", f"{_label(session)} — {session.aim or '(no aim)'}", cfg.notify)
            state[session.session_id] = today_iso
            changed = True

    if changed and not dry_run:
        _save_alert_state(state)


def _alert_reason(session: Session, cfg: config.Config, today: date) -> str | None:
    """Return a short alert reason for a session, or None if nothing to flag."""
    if session.deadline:
        badge, severity = deadline_badge(session.deadline, cfg.deadline_warn_days, today)
        if severity in ("red", "amber"):
            return badge
    if session.last_response_at:
        parked_days = (now_ms() - session.last_response_at) / 86_400_000
        if parked_days >= cfg.stale_days:
            return f"parked {int(parked_days)}d, goal unmet"
    return None


def run_loop(interval_sec: int) -> None:
    """Run passes forever, sleeping *interval_sec* between them (manual use)."""
    while True:
        run_once()
        time.sleep(interval_sec)
