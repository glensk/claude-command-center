#!/usr/bin/env python3
"""Auto-resume session-limit-halted sessions once the rate limit resets.

When the shared Claude account hits its session/rate limit, tracked sessions stall
(``Status.HALTED``: their last main-chain assistant turn is a rate-limit error).
This module resumes them automatically once the limit resets, via the existing
``claude-session-continue.py`` script — **staggered ~2 min across repos** and
**strictly serial within a repo** (the next starts only after the prior session's
turn produces a completed transcript turn and goes idle).

Design (kept testable): a **pure planner** ``plan()`` maps observed live/transcript
state + the persisted queue to a list of effect-free :class:`Action`s and the next
:class:`QueueState`; an effectful executor :func:`apply_actions` performs them
(reap a stuck REPL, open a resume tab, spawn the reset detector, notify). The
``--watch`` loop is an flock singleton spawned by the daemon when work exists.

Reset detection is explicit, not transcript-inferred: a single headless
``claude-session-continue.py --wait-only --signal-file <f>`` reuses the script's
verified probe/verify, then touches ``<f>``; that file is the reset gate.

Reset evidence is only valid for halts that PRECEDE it: a fresh halt proves the
account's limit is active right now, so it invalidates a confirmation persisted by
an earlier resume cycle (and a leftover signal file) instead of dispatching an
immediate — premature — resume into the still-active limit.

Every dispatched resume and queue/gate transition is appended to
``app_home()/resume.log`` (TSV: timestamp, event, session id, detail), so "which
sessions did ccc restart, and when?" is a grep.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import copy
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .adapters.claude import ClaudeAdapter
from .core import reconcile
from .models import LiveSession, Session, now_ms
from .notify import notify
from .store import Store

# Terminal entry states never re-launched; live ones drive dispatch.
_TERMINAL = ("done", "failed")
_IN_FLIGHT = ("launching", "running")

# Barren-re-halt backoff when the transcript's own reset time is unparseable:
# 15 min · 2^attempts, capped at one 5h window.
_BACKOFF_BASE_MS = 15 * 60 * 1000
_BACKOFF_CAP_MS = 5 * 3600 * 1000

# Reset-gate account keys. Each Claude account has its own rate-limit window, so the
# gate (detector process + signal file + confirmed-reset stamp) is keyed per account.
# ``_DEFAULT_KEY`` ("") is single-account mode — it also keeps the historical signal
# filename. ``UNATTRIBUTED`` marks a session whose account cannot be resolved: never
# resumed (we will not guess which seat to bill). Neither can collide with a real
# account label, which is a non-empty key of ``config.claude_config_dirs()``.
_DEFAULT_KEY = ""
UNATTRIBUTED = "?"

_NOTIFIED: set[str] = set()  # one-shot notify keys for this watcher process
_REPO_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# data records
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """A halted session eligible for auto-resume."""

    session_id: str
    cwd: str
    repo: str
    account: str = _DEFAULT_KEY  # reset-gate key (see account_key)


@dataclass
class Observation:
    """The live + transcript facts the planner needs for one session this tick."""

    alive: bool
    raw_status: str  # busy | idle | waiting | "" (parked)
    halted: bool
    transcript_size: int
    cwd: str
    repo: str
    account: str = _DEFAULT_KEY  # reset-gate key (see account_key)
    # Filled ONLY for an in-flight entry observed halted (the re-halt branch is the
    # sole consumer; computing them costs a transcript scan/parse):
    progressed: bool = False  # a successful main-chain response landed past the launch baseline
    reset_hint_ms: int = 0  # reset epoch ms parsed from the halting error itself (0 = unknown)


@dataclass
class Entry:
    """One session's slot in the resume queue."""

    session_id: str
    repo: str
    cwd: str
    state: str = "queued"  # queued | launching | running | done | failed
    launched_at: int = 0  # epoch ms a resume was dispatched
    baseline_offset: int = 0  # transcript size at launch (progress is growth past it)
    attempts: int = 0
    fail_reason: str = ""
    account: str = _DEFAULT_KEY  # which account's reset gates this entry (see account_key)
    retry_not_before: int = 0  # epoch ms a barren-re-halt backoff blocks dispatch until (0 = none)


@dataclass
class QueueState:
    """Persisted orchestration state (single-writer: the flock watcher).

    The reset bookkeeping is keyed PER ACCOUNT: rate-limit windows are per-seat, so a
    halted ``work`` session must wait for ``work``'s reset and must not be held back
    by — or released early by — ``private``'s. ``last_launch_at`` stays global: it is
    an anti-herd stagger across the machine, not an account concern.
    """

    last_launch_at: int = 0  # epoch ms of the last real resume (global stagger gate)
    # account key -> epoch ms that account's limit was confirmed reset (absent = waiting)
    reset_confirmed_at: dict[str, int] = field(default_factory=dict)
    # account key -> pid of that account's headless --wait-only detector (absent = none)
    reset_wait_pid: dict[str, int] = field(default_factory=dict)
    entries: dict[str, Entry] = field(default_factory=dict)


@dataclass
class Action:
    """A side effect the executor performs (effect-free in the planner)."""

    kind: (
        str  # reap | launch_resume | ensure_reset_wait | confirm_reset | invalidate_reset | notify
    )
    session_id: str = ""
    cwd: str = ""
    detail: str = ""
    account: str = _DEFAULT_KEY  # which account's gate the reset-wait/confirm/invalidate acts on


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _state_path() -> Path:
    return config.app_home() / "resume_queue.json"


def _signal_path(account: str = _DEFAULT_KEY) -> Path:
    """That account's reset-signal file (one detector touches one file).

    The default/single-account key keeps the historical unsuffixed name; a named
    account gets a slugged suffix so two seats' detectors can never share a file.
    """
    if not account or account == _DEFAULT_KEY:
        return config.app_home() / "resume_reset.signal"
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", account)
    return config.app_home() / f"resume_reset.{slug}.signal"


def _lock_path() -> Path:
    return config.app_home() / "resume_watch.lock"


def _resume_log_path() -> Path:
    return config.app_home() / "resume.log"


def _log(event: str, session_id: str = "", detail: str = "") -> None:
    """Append one TSV line to ``app_home()/resume.log`` — the restart audit trail.

    Records every dispatched resume plus the queue/gate transitions around it, so a
    premature or missing restart can be reconstructed after the fact (the watcher's
    stdout is discarded by the detached daemon spawn). Append-only; never raises.
    """
    try:
        path = _resume_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{event}\t{session_id}\t{detail}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def repo_of(cwd: str) -> str:
    """Git toplevel of *cwd* (the repo key for serialization); fallback *cwd*."""
    if not cwd:
        return ""
    if cwd in _REPO_CACHE:
        return _REPO_CACHE[cwd]
    top = cwd
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            top = proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    _REPO_CACHE[cwd] = top
    return top


def account_key(config_dir: str) -> str:
    """The reset-gate key for *config_dir* — ``UNATTRIBUTED`` when it cannot be resolved.

    Every Claude account has its OWN rate-limit window, so auto-resume runs one reset
    detector (and one signal file) PER account and a session is revived under the very
    account it was started from — a halted ``work`` session comes back on ``work``, a
    ``private`` one on ``private``.

    Single-account mode collapses to one key (``_DEFAULT_KEY``, ""), which also keeps
    the historical single-account signal-file name. In multi-account mode the key is
    the account's configured label (``private`` / ``work``). What stays fail-closed is
    the genuinely UNKNOWN case: a session with no stamped ``config_dir`` (or one naming
    a dir no configured account owns) cannot be attributed, and reviving it would probe
    and bill an arbitrary seat — so it is skipped rather than guessed.
    """
    from . import accounts

    if not accounts.is_multi_account():
        return _DEFAULT_KEY
    if not config_dir:
        return UNATTRIBUTED
    label = accounts.account_label(config_dir)
    return label if label in config.claude_config_dirs() else UNATTRIBUTED


def _config_dir_for_key(key: str) -> str:
    """Invert :func:`account_key` — the config dir to pin for that account ("" = default)."""
    if key in (_DEFAULT_KEY, UNATTRIBUTED):
        return ""
    from . import accounts

    return accounts.account_config_dir(key)


def is_resumable(session: Session, adapter: ClaudeAdapter) -> bool:
    """Per-session auto-resume eligibility, EXCLUDING haltedness and the config gate.

    Shared by :func:`candidates` (what the watcher actually queues) and
    :func:`will_auto_resume` (what the ``||▶`` icon promises), so the icon can never
    advertise a revival the watcher would not perform.

    Excludes done / draft / archived; requires a real cwd, an attributable account and
    a transcript on disk (``claude --resume`` needs a recorded conversation).
    """
    if session.done or session.draft or session.archived:
        return False
    if account_key(session.config_dir) == UNATTRIBUTED:
        return False
    if not session.cwd or not os.path.isdir(session.cwd):
        return False
    return adapter.transcript_path(session.cwd, session.session_id, session.config_dir) is not None


def will_auto_resume(
    session: Session,
    adapter: ClaudeAdapter,
    cfg: config.Config,
    queue: QueueState | None = None,
) -> bool:
    """True when a HALTED *session* will be auto-revived once its rate limit resets.

    Precondition: the caller already established the session is halted (its row status
    is ``Status.HALTED``) — this adds the ``resume_halted`` config gate on top of the
    same eligibility the watcher applies. Drives the green ``▶`` suffix on the red
    ``||`` icon, so a bare red ``||`` means "stranded: nothing will revive this".

    A terminal-``failed`` queue entry (launch-infrastructure fault, retries exhausted)
    downgrades the promise — the watcher will NOT revive that session. Queued/backoff
    entries, and legacy rate-limit ``failed`` entries (which the next watcher tick
    revives), keep the ``▶``: they WILL retry. Pass *queue* (one ``load_state()``
    snapshot per render pass) so the TUI does not re-read the queue file per row;
    ``None`` falls back to a lazy single load.
    """
    if not (cfg.resume_halted and is_resumable(session, adapter)):
        return False
    state = queue if queue is not None else load_state()
    entry = state.entries.get(session.session_id)
    if entry is None or entry.state != "failed":
        return True
    return entry.fail_reason == "re-halted on the limit"  # legacy → revived next tick


def candidates(store: Store, adapter: ClaudeAdapter) -> list[Candidate]:
    """Halted sessions eligible for auto-resume (alive HALTED or parked-after-429)."""
    out: list[Candidate] = []
    for session in store.list_sessions():
        if not is_resumable(session, adapter):
            continue
        if not adapter.is_halted(session.cwd, session.session_id):
            continue
        out.append(
            Candidate(
                session.session_id,
                session.cwd,
                repo_of(session.cwd),
                account_key(session.config_dir),
            )
        )
    return out


def purge_unattributable_entries(store: Store, state: QueueState) -> None:
    """Drop any queued entry whose Claude account cannot be attributed.

    ``resume.py`` observes ``candidate_ids | set(state.entries)``, so an entry queued
    while single-account (or before an account was attributed) can reach ``plan()``
    with no resolvable account and dispatch against an arbitrary seat's reset gate.
    Purge those BEFORE ``_observe()``/``plan()``. Logged.
    """
    for session_id in list(state.entries):
        session = store.get(session_id)
        config_dir = session.config_dir if session else ""
        if account_key(config_dir) == UNATTRIBUTED:
            del state.entries[session_id]
            print(
                f"resume-halted: dropped {session_id[:8]} — its Claude account is "
                "unknown; auto-resume will not guess which seat to revive it on."
            )


def reconcile_failed_entries(store: Store, state: QueueState) -> list[tuple[str, str, str]]:
    """Prune finished ``failed`` tombstones; revive legacy rate-limit failures.

    A ``failed`` entry whose session is missing/done/archived is dead weight — the
    session is over, the tombstone serves nobody; drop it. A ``failed`` entry whose
    reason is "re-halted on the limit" predates recoverable backoff (rate-limit
    re-halts can no longer become terminal): revive it to ``queued`` so the normal
    gate machinery retries it. Terminal launch-infrastructure failures of still-open
    sessions are kept (visible as a bare ``||``).

    Mutates *state* in memory only and returns ``(event, session_id, detail)`` rows —
    the caller logs them ONLY on the non-dry-run path, so ``--dry-run`` never touches
    ``resume_queue.json`` or ``resume.log``.
    """
    rows: list[tuple[str, str, str]] = []
    for session_id in list(state.entries):
        entry = state.entries[session_id]
        if entry.state != "failed":
            continue
        session = store.get(session_id)
        if session is None or session.done or session.archived:
            why = (
                "session gone"
                if session is None
                else "session done"
                if session.done
                else "session archived"
            )
            del state.entries[session_id]
            rows.append(("pruned-failed", session_id, f"cwd={entry.cwd} {why}"))
        elif entry.fail_reason == "re-halted on the limit":
            entry.state = "queued"
            entry.launched_at = 0
            entry.baseline_offset = 0
            entry.retry_not_before = 0
            rows.append(
                (
                    "revived-legacy-failed",
                    session_id,
                    f"cwd={entry.cwd} rate-limit re-halts are recoverable now",
                )
            )
    return rows


def _transcript_size(adapter: ClaudeAdapter, cwd: str, session_id: str) -> int:
    path = adapter.transcript_path(cwd, session_id)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _observe(
    adapter: ClaudeAdapter,
    store: Store,
    ids: set[str],
    entries: dict[str, Entry] | None = None,
) -> dict[str, Observation]:
    """Build the per-session :class:`Observation` map the planner consumes.

    For an IN-FLIGHT entry observed halted (a re-halt), two extra facts are read so
    the planner can tell a productive resume from a barren one: whether a successful
    main-chain response landed past the launch baseline, and the reset time the
    halting error itself names. Computed only in that case — never per idle poll.
    """
    live: dict[str, LiveSession] = {ls.session_id: ls for ls in adapter.discover()}
    observed: dict[str, Observation] = {}
    for session_id in ids:
        session = store.get(session_id)
        cwd = session.cwd if session else ""
        live_session = live.get(session_id)
        halted = adapter.is_halted(cwd, session_id) if cwd else False
        entry = (entries or {}).get(session_id)
        rehalt = bool(halted and cwd and entry and entry.state in _IN_FLIGHT)
        observed[session_id] = Observation(
            alive=bool(live_session and live_session.alive),
            raw_status=(live_session.raw_status if live_session else ""),
            halted=halted,
            transcript_size=_transcript_size(adapter, cwd, session_id) if cwd else 0,
            cwd=cwd,
            repo=repo_of(cwd) if cwd else "",
            account=account_key(session.config_dir if session else ""),
            progressed=(
                adapter.successful_response_since(cwd, session_id, entry.baseline_offset)
                if rehalt and entry is not None
                else False
            ),
            reset_hint_ms=adapter.halt_reset_at_ms(cwd, session_id) if rehalt else 0,
        )
    return observed


# ---------------------------------------------------------------------------
# pure planner
# ---------------------------------------------------------------------------
def _is_idle(raw_status: str) -> bool:
    return raw_status in ("idle", "waiting") or raw_status.startswith("wait")


def _fail_or_requeue(entry: Entry, cfg: config.Config, reason: str, actions: list[Action]) -> None:
    """Bounded retry for LAUNCH-INFRASTRUCTURE faults (a resume that never took):
    requeue until ``resume_max_attempts``, then fail + notify. Rate-limit re-halts
    never come through here — they back off and retry instead (see the re-halt
    branch in :func:`plan`)."""
    entry.attempts += 1
    if entry.attempts >= cfg.resume_max_attempts:
        entry.state = "failed"
        entry.fail_reason = reason
        actions.append(Action("notify", entry.session_id, detail=f"resume failed: {reason}"))
    else:
        entry.state = "queued"


def plan(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    observed: dict[str, Observation],
    candidate_ids: set[str],
    state: QueueState,
    now: int,
    cfg: config.Config,
    reset_signals: set[str],
) -> tuple[QueueState, list[Action]]:
    """Pure: given observed state + the queue, return the next queue + actions.

    No side effects — the executor performs the returned actions and persists the
    returned state. This is the unit-tested heart of the feature.

    *reset_signals* is the set of account keys whose limit is confirmed reset (their
    detector touched its signal file) — the gate is per account, not global.
    """
    state = copy.deepcopy(state)
    actions: list[Action] = []

    # 1. Enqueue new candidates. A previously-done session that halted AGAIN in a
    #    later window is re-queued fresh; failed entries stay suppressed. Accounts
    #    gaining a FRESH halt this tick are tracked: that halt proves the account's
    #    limit is active right now.
    fresh_halts: set[str] = set()
    for session_id in candidate_ids:
        entry = state.entries.get(session_id)
        obs = observed.get(session_id)
        if entry is None:
            state.entries[session_id] = Entry(
                session_id=session_id,
                repo=obs.repo if obs else "",
                cwd=obs.cwd if obs else "",
                account=obs.account if obs else _DEFAULT_KEY,
            )
            fresh_halts.add(obs.account if obs else _DEFAULT_KEY)
            continue
        # Re-stamp the account from the live observation every tick. This backfills entries
        # persisted BEFORE the gate was per-account (they carry the default key regardless of
        # which seat they actually ran on), so a legacy queue file cannot park a work session
        # on the private gate forever.
        if obs is not None:
            entry.account = obs.account
        if entry.state == "done":
            entry.state = "queued"
            entry.attempts = 0
            entry.launched_at = 0
            entry.baseline_offset = 0
            fresh_halts.add(entry.account)

    # 1b. A fresh halt invalidates that account's PRE-halt reset evidence — both a
    #    confirmation persisted by an earlier resume cycle (the queue file outlives
    #    drained cycles) and a leftover signal file. Without this, the stale gate
    #    dispatched an immediate premature resume into the still-active limit (seen
    #    live 2026-08-21: halted 17:00, "resumed" 17:03 against a 19:20 reset). The
    #    detector ensured in step 3 re-verifies; a genuinely-reset account simply
    #    re-confirms one probe later.
    reset_signals = reset_signals - fresh_halts
    for account in sorted(fresh_halts):
        state.reset_confirmed_at.pop(account, None)
        actions.append(
            Action(
                "invalidate_reset",
                account=account,
                detail="fresh halt invalidates pre-halt reset evidence",
            )
        )

    # 2. Reconcile + classify. A queued entry the user already resumed (alive, not
    #    halted) is adopted as in-flight rather than relaunched (no double resume).
    rehalted: set[str] = set()
    for session_id, entry in list(state.entries.items()):
        if entry.state in _TERMINAL:
            continue
        obs = observed.get(session_id) or Observation(False, "", False, 0, entry.cwd, entry.repo)

        if entry.state == "queued":
            if obs.alive and not obs.halted:  # someone resumed it out-of-band (no double launch)
                entry.state = "running"
                entry.launched_at = entry.launched_at or now
                entry.baseline_offset = 0  # finish hinges on idle, not further growth
            continue

        # in-flight (launching | running). "resumed" = transcript grew past the launch
        # baseline (a real resume produced content) — the persistent, poll-timing-
        # independent signal (Codex O5), not a transient "seen busy" flag.
        resumed = obs.transcript_size > entry.baseline_offset
        if obs.halted:  # THIS account's limit is back → requeue + re-gate that account only
            rehalted.add(entry.account)
            entry.state = "queued"
            entry.launched_at = 0
            entry.baseline_offset = 0
            if obs.progressed:
                # The resume WORKED — real model output landed before the next
                # window's limit. A fresh halt, not a failed attempt: full reset.
                entry.attempts = 0
                entry.retry_not_before = 0
                entry.fail_reason = ""
            else:
                # Barren re-halt (e.g. a weekly/Opus cap the haiku probe cannot see):
                # NEVER terminal — back off until the reset time the halting error
                # itself names, else an escalating fallback, then retry. `attempts`
                # keeps counting (telemetry + fallback exponent) but "re-halted on
                # the limit" cannot reach state="failed".
                entry.attempts += 1
                fallback = now + min(_BACKOFF_CAP_MS, _BACKOFF_BASE_MS * 2**entry.attempts)
                hint = obs.reset_hint_ms
                entry.retry_not_before = hint if hint > now else fallback
                entry.fail_reason = "re-halted on the limit"  # informational, not terminal
            continue
        if resumed and (_is_idle(obs.raw_status) or not obs.alive):  # turn completed → free repo
            entry.state = "done"
            actions.append(Action("notify", session_id, detail="resumed and finished its turn"))
            continue
        if resumed and entry.state == "launching":
            entry.state = "running"  # the resume took; its turn is in progress
        # Fail only when the resume never took: no progress AND no live process past the
        # grace window. A live-but-slow turn is left alone (never reap a working session).
        if (
            not resumed
            and not obs.alive
            and now - entry.launched_at > cfg.resume_launch_timeout_sec * 1000
        ):
            _fail_or_requeue(entry, cfg, "no resume progress before timeout", actions)
            continue
        # else: still launching/running — leave in place

    for account in rehalted:  # that seat's limit returned; wait for ITS next reset
        state.reset_confirmed_at.pop(account, None)

    # 3. Reset gate — PER ACCOUNT. Rate-limit windows are per-seat, so each account with
    #    queued work gets its own headless --wait-only detector (pinned to that account's
    #    CLAUDE_CONFIG_DIR) and its own signal file. A `work` session waits for `work`'s
    #    reset; it neither blocks nor is released by `private`'s.
    for account in sorted({e.account for e in state.entries.values() if e.state == "queued"}):
        if state.reset_confirmed_at.get(account):
            continue
        if account in reset_signals:
            state.reset_confirmed_at[account] = now
            actions.append(Action("confirm_reset", account=account))
        else:
            actions.append(Action("ensure_reset_wait", account=account))

    # 4. Dispatch — one launch per tick (global stagger), one in-flight per repo, and only
    #    for an account whose OWN limit is confirmed reset (others stay queued, waiting on
    #    their detector).
    busy_repos = {e.repo for e in state.entries.values() if e.state in _IN_FLIGHT}
    if now - state.last_launch_at >= cfg.resume_stagger_sec * 1000:
        for session_id, entry in state.entries.items():
            if entry.state != "queued" or entry.repo in busy_repos:
                continue
            if not state.reset_confirmed_at.get(entry.account):
                continue  # this seat is still rate-limited
            if entry.retry_not_before > now:
                continue  # barren-re-halt backoff: its own limit has not reset yet
            obs = observed.get(session_id)
            if obs and obs.alive:  # stuck live REPL: kill it before re-resuming
                actions.append(Action("reap", session_id))
            actions.append(
                Action("launch_resume", session_id, cwd=entry.cwd, account=entry.account)
            )
            entry.state = "launching"
            entry.launched_at = now
            entry.baseline_offset = obs.transcript_size if obs else 0
            state.last_launch_at = now
            break  # global stagger: at most one resume dispatched per tick

    state.entries = {s: e for s, e in state.entries.items() if e.state != "done"}
    return state, actions


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _notify_once(cfg: config.Config, message: str) -> None:
    if message in _NOTIFIED:
        return
    _NOTIFIED.add(message)
    notify("ccc resume-halted", message, cfg.notify)


def _resolve_continue_script(cfg: config.Config) -> str:
    """Path to claude-session-continue (config override → entry point → legacy ``.py``).

    The packaged ``claude-session-continue`` console script is preferred over the
    historical ``claude-session-continue.py`` so a wheel install resolves to its own
    entry point; ``resume_continue_script`` overrides both.
    """
    import shutil

    if cfg.resume_continue_script:
        return cfg.resume_continue_script
    return (
        shutil.which("claude-session-continue") or shutil.which("claude-session-continue.py") or ""
    )


def _reap_fresh(adapter: ClaudeAdapter, store: Store, session_id: str) -> None:
    """SIGTERM the session's *fresh* live pid (never a stored one), then close its pane.

    Re-reads the live registry at kill time so a stale/reused stored pid is never
    signalled (Codex O4). The pane is closed only after the process is confirmed gone.
    """
    live = {ls.session_id: ls for ls in adapter.discover()}
    live_session = live.get(session_id)
    if live_session is None or not live_session.alive or live_session.pid <= 0:
        return
    from .daemon import _reap  # SIGTERM → SIGKILL; reuse the daemon's reaper

    _reap(live_session.pid)
    for _ in range(15):  # wait up to ~3s for the registry entry to disappear
        time.sleep(0.2)
        if not any(ls.session_id == session_id and ls.alive for ls in adapter.discover()):
            break
    session = store.get(session_id)
    if session and session.iterm_session_id:
        from . import terminal

        terminal.close_iterm_session(session.iterm_session_id)


def _launch_resume(
    session_id: str,
    cwd: str,
    cfg: config.Config,
    config_dir: str = "",
    *,
    no_codex: bool = False,
) -> bool:
    from . import terminal

    script = _resolve_continue_script(cfg)
    if not script:
        return False
    return terminal.resume_halted_in_new_tab(cwd, session_id, script, config_dir, no_codex=no_codex)


def _consume_reset_signal(state: QueueState, account: str) -> None:
    """*account*'s reset confirmed: remove its signal file and stop its detector."""
    try:
        _signal_path(account).unlink(missing_ok=True)
    except OSError:
        pass
    pid = state.reset_wait_pid.get(account, 0)
    if pid and _pid_alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    state.reset_wait_pid.pop(account, None)


def _invalidate_reset(state: QueueState, account: str) -> None:
    """A fresh halt proved *account*'s limit is active NOW — drop pre-halt evidence.

    Removes a leftover signal file and forgets a dead detector so step 3 spawns a
    fresh one. A LIVE detector is left alone: it has not fired yet, so it is still
    validly waiting for the real reset (killing it would only re-bill a probe).
    """
    pid = state.reset_wait_pid.get(account, 0)
    if pid and _pid_alive(pid):
        return
    try:
        _signal_path(account).unlink(missing_ok=True)
    except OSError:
        pass
    state.reset_wait_pid.pop(account, None)


def _ensure_reset_wait(state: QueueState, cfg: config.Config, account: str) -> None:
    """Make sure exactly one ``--wait-only`` reset detector runs FOR *account*.

    One detector per account, each probing its own seat: the child's env pins that
    account's ``CLAUDE_CONFIG_DIR`` (via :func:`accounts.launch_env`), so the ``claude
    -p`` probe inside ``claude-session-continue`` reads the rate-limit window of the
    very account whose sessions this gate releases — never an ambient/wrong seat.
    """
    pid = state.reset_wait_pid.get(account, 0)
    if pid and _pid_alive(pid):
        return  # a detector is already waiting for this account — leave its signal alone
    signal = _signal_path(account)
    try:
        signal.unlink(missing_ok=True)  # clear any stale signal before a fresh wait
    except OSError:
        pass
    script = _resolve_continue_script(cfg)
    if not script:
        _notify_once(cfg, "claude-session-continue not found — set resume_continue_script")
        return
    from . import accounts

    try:
        config.app_home().mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(  # noqa: S603  # detached headless reset detector
            [script, "auto", "--wait-only", "--signal-file", str(signal)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            # Pin the detector to THIS account's config dir, so its `claude -p` probe reads
            # the rate-limit window of the seat whose sessions this gate releases — and an
            # ambient CLAUDE_CONFIG_DIR can never make it probe/bill the wrong one (D8).
            env=accounts.launch_env(_config_dir_for_key(account)),
        )
        state.reset_wait_pid[account] = proc.pid
        _log("detector-spawned", detail=f"account={account or 'default'} pid={proc.pid}")
    except OSError:
        state.reset_wait_pid.pop(account, None)


def reset_signals_present(accounts_in_play: set[str]) -> set[str]:
    """The subset of *accounts_in_play* whose reset detector has fired (signal on disk)."""
    return {account for account in accounts_in_play if _signal_path(account).exists()}


def apply_actions(
    actions: list[Action],
    state: QueueState,
    store: Store,
    adapter: ClaudeAdapter,
    cfg: config.Config,
) -> None:
    """Perform the planner's effects (mutates *state* for reset-wait bookkeeping)."""
    for action in actions:
        if action.kind == "reap":
            _reap_fresh(adapter, store, action.session_id)
            _log("reap", action.session_id, "killed stuck REPL before relaunch")
        elif action.kind == "launch_resume":
            # Revive on the SAME seat the session was started from: the stored config_dir
            # is prefixed onto the resume command (terminal.launch_env_prefix), so a work
            # session comes back on work and a private one on private.
            resumed = store.get(action.session_id)
            config_dir = resumed.config_dir if resumed else _config_dir_for_key(action.account)
            launched = _launch_resume(
                action.session_id,
                action.cwd,
                cfg,
                config_dir,
                no_codex=bool(resumed.no_codex) if resumed else False,
            )
            _log(
                "launch",
                action.session_id,
                f"cwd={action.cwd} account={action.account or 'default'} ok={launched}",
            )
            if not launched:
                _notify_once(
                    cfg, "cannot open a terminal to resume — is iTerm/osascript available?"
                )
        elif action.kind == "ensure_reset_wait":
            _ensure_reset_wait(state, cfg, action.account)
        elif action.kind == "confirm_reset":
            _consume_reset_signal(state, action.account)
            _log("reset-confirmed", detail=f"account={action.account or 'default'}")
        elif action.kind == "invalidate_reset":
            _invalidate_reset(state, action.account)
            _log(
                "reset-invalidated",
                detail=f"account={action.account or 'default'} — {action.detail}",
            )
        elif action.kind == "notify":
            notify("ccc resume-halted", action.detail, cfg.notify)
            _log("notify", action.session_id, action.detail)


# ---------------------------------------------------------------------------
# tick / watch / cli
# ---------------------------------------------------------------------------
def _is_drained(state: QueueState, now: int | None = None) -> bool:
    """No work the WATCHER should stay alive for.

    A queued entry whose ``retry_not_before`` lies in the future is deliberately NOT
    counted: the watcher exits instead of poll-looping through an hours-long (or
    week-long) backoff, and the daemon respawns it once the retry is due
    (:func:`has_work`).
    """
    if now is None:
        now = now_ms()
    return not any(
        e.state in _IN_FLIGHT or (e.state == "queued" and e.retry_not_before <= now)
        for e in state.entries.values()
    )


def _log_transitions(old: QueueState, new: QueueState) -> None:
    """Append per-session queue transitions (enqueue, requeue, fail, done) to the log."""
    for sid, entry in new.entries.items():
        before = old.entries.get(sid)
        if before is None:
            _log("queued", sid, f"cwd={entry.cwd} account={entry.account or 'default'}")
        elif before.state != entry.state:
            detail = f"{before.state}->{entry.state} attempts={entry.attempts}"
            if entry.fail_reason:
                detail += f" reason={entry.fail_reason}"
            _log("state", sid, detail)
    for sid, before in old.entries.items():
        if sid not in new.entries:  # done entries are pruned the tick they finish
            _log("finished", sid, f"cwd={before.cwd} resumed turn completed")


def _summary(state: QueueState, actions: list[Action]) -> str:
    by_state: dict[str, int] = {}
    for entry in state.entries.values():
        by_state[entry.state] = by_state.get(entry.state, 0) + 1
    kinds = ", ".join(a.kind + (f":{a.session_id[:8]}" if a.session_id else "") for a in actions)
    # One reset flag PER account in play: `private=reset✓ work=waiting-reset`.
    in_play = sorted({e.account for e in state.entries.values()} | set(state.reset_confirmed_at))
    reset = (
        " ".join(
            f"{account or 'default'}="
            + ("reset✓" if state.reset_confirmed_at.get(account) else "waiting-reset")
            for account in in_play
        )
        or "no-accounts"
    )
    states = " ".join(f"{k}={v}" for k, v in sorted(by_state.items())) or "(empty)"
    return f"[{reset}] {states}" + (f" | actions: {kinds}" if kinds else "")


def tick(cfg: config.Config, *, dry_run: bool = False) -> bool:
    """One orchestration step. Returns True when the queue is drained (watch can exit)."""
    adapter = ClaudeAdapter()
    with Store() as store:
        reconcile(store, adapter)
        cands = candidates(store, adapter)
        candidate_ids = {c.session_id for c in cands}
        state = load_state()
        # Fail closed BEFORE observe/plan — an entry whose Claude account cannot be
        # attributed must never dispatch against some other seat's reset gate.
        purge_unattributable_entries(store, state)
        # Prune finished failed tombstones / revive legacy rate-limit failures — in
        # memory here so the revived entries are observed and planned this same tick;
        # the log rows are written only on the non-dry-run path below.
        maintenance_rows = reconcile_failed_entries(store, state)
        observed = _observe(adapter, store, candidate_ids | set(state.entries), state.entries)
        accounts_in_play = {c.account for c in cands} | {e.account for e in state.entries.values()}
        now = now_ms()
        new_state, actions = plan(
            observed,
            candidate_ids,
            state,
            now,
            cfg,
            reset_signals_present(accounts_in_play),
        )
        if dry_run:
            print(f"[dry-run] candidates={len(cands)} {_summary(new_state, actions)}")
            return True
        for row in maintenance_rows:
            _log(*row)
        _log_transitions(state, new_state)
        apply_actions(actions, new_state, store, adapter, cfg)
        save_state(new_state)
        print(f"candidates={len(cands)} {_summary(new_state, actions)}")
        return _is_drained(new_state, now)


def watch(cfg: config.Config) -> int:
    """Run :func:`tick` on a poll loop until drained. flock singleton (exit if held)."""
    import fcntl

    config.app_home().mkdir(parents=True, exist_ok=True)
    lock_file = open(_lock_path(), "w", encoding="utf-8")  # noqa: SIM115  # held for the loop
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another resume-halted watcher is already running")
        lock_file.close()
        return 0
    try:
        while True:
            if tick(cfg):
                print("resume-halted: queue drained — exiting")
                return 0
            time.sleep(max(5, cfg.resume_poll_sec))
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def has_work() -> bool:
    """Daemon spawn gate: is there anything a watcher tick would act on?

    True when a halted candidate is not suppressed by a terminal-failed entry, when a
    ``failed`` entry needs maintenance (prune of a finished session, or revival of a
    legacy rate-limit failure — see :func:`reconcile_failed_entries`), or when a
    queued/in-flight entry is live work. A queued entry in backoff counts only once
    its retry is due, so a week-long backoff never keeps respawning idle watchers.

    Replaces the old candidates-only gate, which (a) never spawned the cleanup tick
    for finished sessions — their halted transcripts are excluded from
    ``candidates()`` — and (b) spawn/exit-churned every daemon pass on a still-open
    failed session (a candidate whose failed-only queue counts as drained).
    """
    adapter = ClaudeAdapter()
    with Store() as store:
        state = load_state()
        failed_ids = {sid for sid, e in state.entries.items() if e.state == "failed"}
        if any(c.session_id not in failed_ids for c in candidates(store, adapter)):
            return True
        now = now_ms()
        for session_id, entry in state.entries.items():
            if entry.state in _IN_FLIGHT:
                return True
            if entry.state == "queued" and entry.retry_not_before <= now:
                return True
            if entry.state == "failed":
                if entry.fail_reason == "re-halted on the limit":
                    return True  # legacy terminal rate-limit entry → revivable
                session = store.get(session_id)
                if session is None or session.done or session.archived:
                    return True  # prunable tombstone
    return False


# ---------------------------------------------------------------------------
# state persistence (atomic; single-writer)
# ---------------------------------------------------------------------------
def _int_map(raw: object) -> dict[str, int]:
    """Coerce a persisted reset field to the per-account ``{key: int}`` map.

    Tolerates the pre-multi-account scalar shape (a bare int meaning "the one gate"):
    a non-zero legacy value is adopted as the default account's, anything else starts
    empty. A stale queue file therefore upgrades in place instead of crashing.
    """
    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(raw, int) and raw:
        return {_DEFAULT_KEY: raw}
    return {}


def load_state() -> QueueState:
    """Load the queue state (empty on missing / corrupt — readers see whole files)."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return QueueState()
    if not isinstance(data, dict):
        return QueueState()
    entries: dict[str, Entry] = {}
    raw_entries = data.get("entries")
    if isinstance(raw_entries, dict):
        for session_id, raw in raw_entries.items():
            if not isinstance(raw, dict):
                continue
            entries[session_id] = Entry(
                session_id=str(raw.get("session_id", session_id)),
                repo=str(raw.get("repo", "")),
                cwd=str(raw.get("cwd", "")),
                state=str(raw.get("state", "queued")),
                launched_at=int(raw.get("launched_at", 0) or 0),
                baseline_offset=int(raw.get("baseline_offset", 0) or 0),
                attempts=int(raw.get("attempts", 0) or 0),
                fail_reason=str(raw.get("fail_reason", "")),
                account=str(raw.get("account", _DEFAULT_KEY)),
                retry_not_before=int(raw.get("retry_not_before", 0) or 0),
            )
    return QueueState(
        last_launch_at=int(data.get("last_launch_at", 0) or 0),
        reset_confirmed_at=_int_map(data.get("reset_confirmed_at")),
        reset_wait_pid=_int_map(data.get("reset_wait_pid")),
        entries=entries,
    )


def save_state(state: QueueState) -> None:
    """Persist the queue atomically (tmp + ``os.replace`` → readers never see a partial)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "reset_confirmed_at": state.reset_confirmed_at,
        "last_launch_at": state.last_launch_at,
        "reset_wait_pid": state.reset_wait_pid,
        "entries": {sid: dataclasses.asdict(entry) for sid, entry in state.entries.items()},
    }
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"resume-halted: could not persist state: {exc}", file=sys.stderr)
