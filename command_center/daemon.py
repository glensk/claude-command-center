#!/usr/bin/env python3
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
# (import-outside-toplevel); the single-pass orchestration is branchy by nature and
# keeps every pass step in one module (too-many-lines).
# pylint: disable=import-outside-toplevel,too-many-branches,too-many-lines

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
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from . import config
from .adapters.claude import ClaudeAdapter
from .core import headless_leak_ids, orphan_launched_ids, reconcile, spare_background_agents
from .models import LiveSession, Session, Status, deadline_badge, now_ms, short_id
from .notify import notify
from .store import Store

if TYPE_CHECKING:
    from .bridge_target import PasteResult


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
    # Attached prompts the paste guard HELD this pass, "<short>:<code>" (tp#1867): the box
    # held a draft, the user was typing, … — retried one daemon pass later, no new tab.
    attached_held: list[str] = field(default_factory=list)
    copilot_refreshed: bool = False  # routine; deliberately excluded from is_empty()
    claude_refreshed: bool = False  # routine Claude /usage OAuth fetch; excluded from is_empty()
    codex_refreshed: bool = False  # routine live Codex usage fetch; excluded from is_empty()
    agy_refreshed: bool = False  # routine Antigravity `/usage` fetch; excluded from is_empty()
    resume_spawned: bool = False  # spawned the resume-halted watcher; excluded from is_empty()
    limit_switched: list[str] = field(default_factory=list)  # sessions moved off a capped seat
    awaited: list[int] = field(default_factory=list)  # `ccc await` groups fired this pass
    temps_swept: int = 0  # orphaned usage temp files reclaimed; excluded from is_empty()
    panel_restart: str = ""  # stale panel server asked to re-exec (tp#70 S5b); routine
    # continue-session handoff sweep (tp#1582): transfers finalized (accepted → finalized),
    # sources a token-scoped closer was spawned for, sources whose dead closer's in-flight
    # token went back pending (tp#1595), and "<transfer>: <why>" failures.
    handoff_finalized: list[str] = field(default_factory=list)
    handoff_closers: list[str] = field(default_factory=list)
    handoff_reclaimed: list[str] = field(default_factory=list)
    handoff_errors: list[str] = field(default_factory=list)
    # tp#1990: "<short id> <short tab UUID>" per row whose gone tab's binding was released.
    tabs_released: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.reaped
            or self.done
            or self.summarized
            or self.progressed
            or self.alerted
            or self.pruned
            or self.limit_switched
            or self.awaited
            or self.handoff_finalized
            or self.handoff_closers
            or self.handoff_reclaimed
            or self.handoff_errors
            or self.scored
            or self.short_aimed
            or self.assessed
            or self.reset_fired
            or self.reset_postponed
            or self.attached_held
            or self.tabs_released
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


def _reap(pid: int, session_id: str = "", path: str = "daemon-reap") -> None:
    """SIGTERM (a ``session-close`` line, tp#1785), then SIGKILL if it lingers. Transcript kept."""
    from . import closelog

    if not closelog.sigterm(pid, session_id, path):
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

    # FIRST, before the store opens: `reconcile` below can raise, and anything placed
    # after it would then never run again. Reclaiming orphaned usage temps is
    # independent of the store, so it must not sit behind a fallible step.
    if not dry_run:
        from . import usage  # pylint: disable=import-outside-toplevel  # module convention

        report.temps_swept = usage.sweep_stale_temps()

    with Store() as store:
        reconcile(store, adapter)
        live = {ls.session_id: ls for ls in adapter.discover()}
        now = now_ms()

        # Rate-limit failover BACKSTOP — deliberately the first thing after reconcile and
        # isolated in its own try/except. The primary trigger is the in-session StopFailure
        # hook; this pass exists for the halts it could not serve (a session started before
        # the hook was wired, a worker killed at the 10 s hook timeout, a switch refused once
        # because background work was in flight and now unblocked). It must not sit behind
        # the summary/usage steps, which are fallible and slow: a launchd StartInterval is a
        # cadence, not a deadline, and a raise upstream would silently drop the recovery for
        # that whole pass (Codex O12).
        if cfg.auto_switch_on_limit and not dry_run:
            _run_limit_switch(store, adapter, live, report)

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
            victims = spare_background_agents(
                store.prunable_sessions(
                    protect_ids=live.keys(),
                    headless_ids=headless_leak_ids(store, adapter, set(live)),
                    orphan_ids=orphan_launched_ids(store, adapter, set(live)),
                )
            )
            report.pruned = [s.session_id for s in victims]
            if victims and not dry_run:
                store.delete_many(s.session_id for s in victims)

        # continue-session handoff repair sweep: every pass, independent of `do_reap`,
        # judged against this pass's registry snapshot; before reaping can park a source.
        _run_handoff_finalize(store, live, report, dry_run)

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
                    _reap(live_session.pid, session.session_id, "daemon-reap")
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
        if cfg.aim_score:
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
        # OAuth usage endpoint (throttled per account).
        _refresh_claude_usage(store, cfg, report, dry_run)

        # Same for each configured CODEX_HOME's live ChatGPT usage endpoint, so the Codex
        # cards do not go stale between Codex turns (throttled per home).
        _refresh_codex_usage(store, cfg, report, dry_run)

        # And the Antigravity account's own weekly meter, so `ccc quota` can rank the
        # `agy` rung without every consumer paying for a CLI spawn of its own.
        _refresh_agy_usage(store, cfg, report, dry_run)

        # Dispatch armed parked-prompt jobs whose fire time has passed (see park.py).
        _fire_reset_jobs(store, cfg, report, dry_run)

        # Deliver parked prompts ATTACHED to existing sessions (fire_at on a
        # non-draft row): typed into the live tab behind the paste guard (held while
        # its input box holds a draft), or a resume tab when the session is not alive.
        _deliver_attached_prompts(store, cfg, report, dry_run, live)

        # `ccc await` BACKSTOP: the same evaluation pass the 60 s poller runs, so a
        # missing poller degrades to this cadence instead of to never. Isolated: an await
        # failure must not cost the rest of the housekeeping pass.
        _run_await_backstop(store, report, dry_run)

        # Auto-resume session-limit-halted sessions: spawn the watcher when work exists.
        _spawn_resume_watcher(cfg, report, dry_run)

        # Self-heal the optional resident panel server after an edit to the editable install.
        _restart_stale_panel_server(report, dry_run)

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
        # tp#1697 B14: work a router SKIPPED waits on the pending queue; this loop is its tick.
        _drain_llm_pending(store, cfg, live, dry_run)

        # tp#1990: release the tab binding + badge of rows whose iTerm tab is gone and whose
        # process is proven dead, BEFORE the title sync so freed palette slots are not
        # re-stolen. Its own isolated step, not gated by `sync_tab_titles`; dry-run reports.
        _run_tab_release(store, live, report, dry_run)

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
    a sync failure (a bad file, a disk error, even an ImportError) is caught and logged so
    it can never break the housekeeping pass.

    The two scrubber outcomes are surfaced as ONE stderr line each (the daemon's log is
    the only place a background pass can speak): a withheld write means the vault is
    stale on purpose and needs attention, a redaction means the scrubber earned its keep.
    Only counts, reasons and LABELS are logged — never a card's content and never the
    path of a secret.
    """
    if not (cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions):
        return
    try:
        from . import mirrors

        report = mirrors.run_mirrors(store, cfg)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"daemon: mirror sync failed: {exc}", file=sys.stderr)
        return
    if report.withheld:
        print(
            f"daemon: mirror scrubber withheld {len(report.withheld)} write(s) — "
            f"{report.withheld[0][1]}",
            file=sys.stderr,
        )
    if report.scrubbed:
        labels = sorted({label for _path, card_labels in report.scrubbed for label in card_labels})
        print(
            f"daemon: mirror scrubber redacted {len(report.scrubbed)} card(s): {', '.join(labels)}",
            file=sys.stderr,
        )


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

    A session whose *first* AIM revision lost its label (``set-aim --first`` rewrote it)
    is pending too: revision (1) is what the ``/aim`` column renders by default, and the
    same generator run backfills it.
    """
    if not cfg.short_aim:
        return
    pending = [
        s
        for s in store.list_sessions()
        if s.aim and not s.done and (not s.short_aim or (s.first_aim and not s.first_short_aim))
    ]
    for session in pending[: cfg.max_summaries_per_run]:
        report.short_aimed.append(session.session_id)
        if dry_run:
            continue
        from . import spawn

        spawn.spawn_ccc(["short-aim", "--session", session.session_id])


def _drain_llm_pending(
    store: Store, cfg: config.Config, live: dict[str, LiveSession], dry_run: bool
) -> list[str]:
    """The daemon tick of the skipped-LLM-work queue (:mod:`.llm_pending`, tp#1697 B14).

    Stale entries are dropped; an entry whose work is done or moot (the session is gone
    or done, it has a short-AIM label, its purpose is no longer queued — e.g. an old
    ``session-name`` entry, tp#1808) is cleared. ``short-aim`` entries are re-run by their
    own backfill above (a skip spends no retry), so only ``voice-brief`` is re-spawned here — a
    detached ``ccc inspect`` that refills the brief cache — capped per pass. Returns the
    session ids it spawned for.
    """
    from . import llm_pending

    queue = llm_pending.prune()
    spawned: list[str] = []
    for purpose, items in queue.items():
        for session_id in list(items):
            session = store.get(session_id)
            alive = session_id in live and live[session_id].alive
            done = (
                purpose not in llm_pending.PURPOSES
                or session is None
                or session.done
                or (purpose == "short-aim" and bool(session.short_aim))
                or (purpose == "voice-brief" and not alive)
            )
            if done:
                if not dry_run:
                    llm_pending.clear(purpose, session_id)
                continue
            if purpose == "voice-brief" and len(spawned) < cfg.max_summaries_per_run:
                spawned.append(session_id)
    if not dry_run:
        from . import spawn

        for session_id in spawned:
            spawn.spawn_ccc(["inspect", "-s", session_id, "-j"])
    return spawned


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
    # Shared claim with the TUI: a failing `gh` is retried once per window, by one of them.
    if not usage.claim_copilot_refresh(throttle):
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
    ``claude``'s own /usage even when no TUI is open and self-heals a rebased window
    boundary. Best-effort: an account with no keychain token simply
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
    if cfg.claude_api_credits and cfg.claude_api_credit_spend:
        # The API-credit spend estimate (`ccc quota`, the TUI's API bars): self-throttled
        # to every 30 min inside, best-effort — a failure is recorded in its cache.
        from . import api_credits  # pylint: disable=import-outside-toplevel

        api_credits.refresh_spend(
            list(cfg.claude_api_credits), list(cfg.claude_api_credit_spend), int(time.time())
        )


def _refresh_codex_usage(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Refresh each ``CODEX_HOME``'s cached live Codex usage when it is stale.

    Mirrors :func:`_refresh_claude_usage`: gated on ``codex_usage``, throttled by
    ``codex_usage_refresh_sec`` (or the shorter ``codex_usage_refresh_active_sec`` via
    :func:`usage.adaptive_interval` while any job works), keyed per home on the cache
    file's mtime (:func:`usage.codex_usage_stale`). Keeps the cards honest even when no
    TUI is open and no Codex turn has run for hours — the rollout files, the offline
    fallback, are only as fresh as the last turn. Best-effort: a home with no ChatGPT
    ``auth.json`` simply yields no write and the last cache stands.
    """
    from . import usage

    if not cfg.codex_usage or dry_run:
        return
    active = usage.has_active_work(s.status for s in store.list_sessions())
    throttle = usage.adaptive_interval(
        cfg.codex_usage_refresh_sec, cfg.codex_usage_refresh_active_sec, active=active
    )
    for home in config.codex_homes().values():
        if usage.codex_usage_stale(home, throttle):
            if usage.fetch_codex_usage(home) is not None:
                report.codex_refreshed = True


def _refresh_agy_usage(
    store: Store, cfg: config.Config, report: DaemonReport, dry_run: bool
) -> None:
    """Refresh the cached Google Antigravity quota when it is stale.

    Mirrors :func:`_refresh_copilot_usage` exactly — gated on ``agy_usage``, throttled by
    ``agy_usage_refresh_sec`` (or the shorter ``agy_usage_refresh_active_sec`` while any
    job works), mtime-keyed (:func:`usage.agy_usage_stale`). The fetch spends no tokens;
    what the throttle guards is the ~3 s ``agy`` process spawn. Best-effort: no binary,
    a timeout or an unparseable answer simply leaves the previous cache standing, which
    the oracle then ages out into ``unknown`` on its own.
    """
    from . import usage

    if not cfg.agy_usage or dry_run:
        return
    active = usage.has_active_work(s.status for s in store.list_sessions())
    throttle = usage.adaptive_interval(
        cfg.agy_usage_refresh_sec, cfg.agy_usage_refresh_active_sec, active=active
    )
    if not usage.agy_usage_stale(throttle):
        return
    report.agy_refreshed = usage.fetch_agy_usage() is not None


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

    A job an ``AI_AUTOMATED`` caller registered (``registered_by = automated``, tp#1698)
    is never fired, even if something armed it: only the user's explicit start runs it.
    """
    from . import accounts, automated, park, terminal, usage

    now = int(time.time())
    due = [
        s
        for s in store.list_sessions()
        if s.draft
        and not s.archived
        and 0 < s.fire_at <= now - park.FIRE_GRACE_SEC
        and not automated.is_stamped(s)
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
                    f"{_label(job)} — {park.normalize_window(job.fire_window)} still exhausted; "
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


#: Pastes an attached prompt into a live session's tab behind the paste guard:
#: ``(live, text, claim)`` → :class:`bridge_target.PasteResult`. *claim* writes the
#: :data:`park.FIRE_RETRY_SEC` lease, inside the tab lock after the guards (tp#1867 D5).
AttachedPaster = Callable[[LiveSession, str, Callable[[], bool]], "PasteResult"]
#: Re-reads the registry: is the session (still) alive? (a refusal's new-tab decision).
StillAlive = Callable[[str], bool]

#: A held attached prompt sends one more notice once held this long (tp#1867 D5).
PROMPT_HELD_NOTICE_SEC = 10 * 60


def _default_attached_paster(
    live: LiveSession, text: str, claim: Callable[[], bool]
) -> PasteResult:
    from .bridge_target import default_deps, guarded_paste

    return guarded_paste(default_deps(), live, text, claim=claim)


def _default_still_alive(session_id: str) -> bool:
    from .bridge_target import default_deps, find_live, is_live

    return is_live(find_live(default_deps(), session_id))


def hold_lease(cfg: config.Config, now: int) -> int:
    """The retry time of a HELD attached prompt: one daemon pass ahead (tp#1867 Q4).

    ``daemon_interval_sec - 60`` so launchd jitter and the strict ``fire_at <= now`` test
    never skip the next pass; never under 60 s.
    """
    return now + max(60, int(cfg.daemon_interval_sec) - 60)


def _deliver_attached_prompts(  # pylint: disable=too-many-arguments
    store: Store,
    cfg: config.Config,
    report: DaemonReport,
    dry_run: bool,
    live: dict[str, LiveSession],
    *,
    paster: AttachedPaster | None = None,
    still_alive: StillAlive | None = None,
) -> None:
    """Deliver parked prompts ATTACHED to existing sessions (``fire_at`` on a non-draft row).

    The "opened the session with its AIM first, defined the real prompt with qp"
    workflow: at fire time the prompt is TYPED into the session's live iTerm tab
    (bracketed paste + CR — same context, same billing, no new session) through the
    paste guard (:func:`bridge_target.guarded_paste`, tp#1867): tab → per-tab lock →
    revalidate → typing guard → input-box guard → the claim (the
    :data:`park.FIRE_RETRY_SEC` lease, written inside the lock after the guards) → paste.
    When the session is not alive, a new tab resumes it WITH the prompt
    (``ccc fire-attached`` → ``claude --resume <id> "<prompt>"``) under the same lease;
    ``fire-attached`` claims it with the one-shot conditional update, so nothing delivers
    twice. No grace offset: no foreground waiter ever competes for an attached prompt.

    A session that is alive never gets a new tab (a second ``claude --resume`` of a live
    session). A HELD paste (the box holds a draft, the user is typing, the box is not
    drawn yet, another delivery holds the tab) and any other refusal or an untyped paste
    while the session is alive keep the prompt with the hold lease (:func:`hold_lease`,
    never the 900 s one), stamp ``prompt_held_since`` on the first held pass and notify
    on that pass and once more after :data:`PROMPT_HELD_NOTICE_SEC`
    (``DaemonReport.attached_held``, ``"<short>:<code>"``). A paste whose Return did not
    go out (sender state ``partial``) is never re-pasted: ``fire_at = 0``, the prompt is
    kept, and a "press Return" notice goes out.
    """
    from . import accounts, park, usage

    now = int(time.time())
    paste = paster or _default_attached_paster
    alive_now = still_alive or _default_still_alive
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
                    f"{_label(job)} — {park.normalize_window(job.fire_window)} still exhausted; "
                    + park.format_fire(postponed, now),
                    cfg.notify,
                )
            continue
        if dry_run:
            report.reset_fired.append(job.session_id)
            continue
        live_session = live.get(job.session_id)
        if live_session is None or not live_session.alive:
            _attached_new_tab(store, cfg, report, job, now)
            continue
        _paste_attached(store, cfg, report, job, live_session, now, paste, alive_now)


def _attached_new_tab(
    store: Store, cfg: config.Config, report: DaemonReport, job: Session, now: int
) -> None:
    """Session not alive: resume it WITH the prompt in a new tab under the 900 s lease;
    ``fire-attached`` consumes the lease via its one-shot claim."""
    from . import park, terminal

    report.reset_fired.append(job.session_id)
    store.update_fields(
        job.session_id,
        fire_at=now + park.FIRE_RETRY_SEC,
        prompt_held_since=0,
        prompt_held_notice_at=0,
    )
    terminal.fire_attached_in_new_tab(job.session_id)
    notify("⏳ parked prompt resuming in a new tab", _label(job), cfg.notify)


def _paste_attached(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    cfg: config.Config,
    report: DaemonReport,
    job: Session,
    live_session: LiveSession,
    now: int,
    paste: AttachedPaster,
    alive_now: StillAlive,
) -> None:
    """A live session: paste behind the guard; D3's P2 column for every outcome."""
    from . import park
    from .bridge_target import held_notice

    def claim() -> bool:
        store.update_fields(job.session_id, fire_at=now + park.FIRE_RETRY_SEC)  # lease
        return True

    short = short_id(job.session_id).strip()
    result = paste(live_session, (job.prompt or "").strip(), claim)
    code = result.refusal
    if code == "claim_lost":
        return  # the claim is a plain write; defensive only
    if code:
        if result.held or code == "account_conflict" or alive_now(job.session_id):
            _hold_attached(
                store, cfg, report, job, now, code, held_notice(code, result.data, short)
            )
        else:  # the session left between the registry read and the lock
            _attached_new_tab(store, cfg, report, job, now)
        return
    if result.state == "sent":
        report.reset_fired.append(job.session_id)
        store.update_fields(job.session_id, fire_at=0, prompt_held_since=0, prompt_held_notice_at=0)
        notify("⏳ parked prompt delivered", _label(job), cfg.notify)
        return
    if result.state == "partial":
        # The paste went out, its Return did not: the prompt sits in the box. Never
        # re-paste (it would hold forever, or deliver twice once Return is pressed).
        report.reset_fired.append(job.session_id)
        store.update_fields(job.session_id, fire_at=0, prompt_held_since=0, prompt_held_notice_at=0)
        notify(
            "⏳ parked prompt not submitted",
            f"prompt pasted but not submitted in {short} — press Return in that tab",
            cfg.notify,
        )
        return
    # Nothing typed into a live session: hold, never a second process for it.
    _hold_attached(store, cfg, report, job, now, "not_typed", f"could not type into {short}'s tab")


def _hold_attached(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    cfg: config.Config,
    report: DaemonReport,
    job: Session,
    now: int,
    code: str,
    wording: str,
) -> None:
    """Keep a held attached prompt with the hold lease; notice on the first held pass and
    once more after :data:`PROMPT_HELD_NOTICE_SEC` held (never the prompt's text)."""
    lease = hold_lease(cfg, now)
    since = job.prompt_held_since or now
    notice = not job.prompt_held_since or (
        now - since >= PROMPT_HELD_NOTICE_SEC
        and job.prompt_held_notice_at < since + PROMPT_HELD_NOTICE_SEC
    )
    fields: dict[str, int] = {"fire_at": lease, "prompt_held_since": since}
    if notice:
        fields["prompt_held_notice_at"] = now
    store.update_fields(job.session_id, **fields)
    report.attached_held.append(f"{short_id(job.session_id).strip()}:{code}")
    if notice:
        retry = time.strftime("%H:%M", time.localtime(lease))
        notify(
            "⏳ parked prompt held",
            f"parked prompt held — {wording}; retrying at {retry}",
            cfg.notify,
        )


def _run_await_backstop(store: Store, report: DaemonReport, dry_run: bool) -> None:
    """One ``await_eval.run_pass`` (dry: probes and delivers nothing); never raises."""
    from . import await_eval  # pylint: disable=import-outside-toplevel  # module convention

    try:
        result = await_eval.run_pass(
            store,
            dry_run=dry_run,
            notifier=None if dry_run else await_eval.config_notifier(),
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"ccc daemon: await pass failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    report.awaited.extend(result.fired)


def _run_tab_release(
    store: Store, live: dict[str, LiveSession], report: DaemonReport, dry_run: bool
) -> None:
    """One ``tabrelease.release_gone`` pass (dry: decides, writes nothing); never raises.

    Fills ``report.tabs_released`` with ``"<short id> <short tab UUID>"`` per released
    (dry: releasable) row — never AIM text.
    """
    from . import tabrelease  # pylint: disable=import-outside-toplevel  # module convention
    from .tab_titles import uuid_of  # pylint: disable=import-outside-toplevel

    try:
        result = tabrelease.release_gone(store, live, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"ccc daemon: tab release failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    report.tabs_released.extend(
        f"{short_id(row.session_id).rstrip()} {uuid_of(row.iterm_session_id)[:8]}"
        for row in result.released
    )


def _run_handoff_finalize(
    store: Store, snapshot: dict[str, LiveSession], report: DaemonReport, dry_run: bool
) -> None:
    """The continue-session handoff repair sweep (``handoff_sweep.run``); never raises.

    Judged against this pass's registry *snapshot*; ``dry_run`` reports, mutates and
    spawns nothing. Each candidate is isolated inside the sweep; this guard only keeps an
    unexpected failure out of the rest of the housekeeping pass.
    """
    from . import handoff_sweep  # pylint: disable=import-outside-toplevel  # module convention

    try:
        handoff_sweep.run(store, snapshot, report, dry_run)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"ccc daemon: handoff sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        report.handoff_errors.append(f"sweep: {type(exc).__name__}: {exc}")


def _run_limit_switch(
    store: Store, adapter: ClaudeAdapter, live: dict, report: DaemonReport
) -> None:
    """Hand every halted live session to the failover decision; never raise into the pass.

    One session per pass at most, like ``resume``'s stagger: a limit that capped several
    sessions at once would otherwise dump all of them onto the other seat in one go.
    """
    from . import limitswitch  # pylint: disable=import-outside-toplevel  # module convention

    for session in store.list_sessions():
        live_session = live.get(session.session_id)
        if live_session is None or not live_session.alive or session.done:
            continue
        if not adapter.is_halted(session.cwd, session.session_id):
            continue
        try:
            switched, detail = limitswitch.run(session.session_id, session.cwd)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            limitswitch.log_event(session.session_id, f"backstop error: {exc}")
            continue
        if switched:
            report.limit_switched.append(session.session_id)
            limitswitch.log_event(session.session_id, f"backstop: {detail}")
            return


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


def _restart_stale_panel_server(  # pylint: disable=too-many-return-statements  # one exit per guard
    report: DaemonReport, dry_run: bool
) -> None:
    """Ask a live panel server running older code than the tree to re-exec (tp#70 D8/S5b).

    A complete no-op unless the opt-in agent is installed and its pidfile names a live
    process. The request targets that pid, so a leftover file can never restart a LATER
    server; the server itself defers the re-exec while a panel is open. ``--dry-run``
    reports the intent and writes nothing. Never raises.
    """
    if sys.platform != "darwin":
        return
    try:
        from . import launchd, panelserver

        if not launchd.panel_server_plist_path().exists():
            return
        p = panelserver.paths()
        info = panelserver.read_pidfile(p)
        if info is None or not panelserver.pid_alive(info.pid):
            return
        if info.stamp >= panelserver.code_stamp():
            return
        if p.restart.exists():
            return  # already requested, not yet consumed (e.g. a panel is open)
        if dry_run:
            report.panel_restart = f"would restart stale panel server pid {info.pid}"
            return
        panelserver.request_restart(p, info.pid)
        report.panel_restart = f"requested restart of stale panel server pid {info.pid}"
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return


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
        summary, next_step = llm.summarize(session.aim, transcript, "", note=note)
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
                store, session.session_id, transcript, model="", full_regrade=True
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
