#!/usr/bin/env python3
"""Dataclasses, the status enum, and small pure formatting helpers.

Kept dependency-free so the model layer is trivial to unit-test.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

# Session/LiveSession are data records — many fields is the point.
# pylint: disable=too-many-instance-attributes


# NB: `str, Enum` (not `StrEnum`) — the installed mypy types StrEnum members as
# plain `str`, breaking `.value` and `dict[Status, ...]` keys. ruff's UP042 is
# ignored project-wide for this reason (see pyproject.toml).
class Status(str, Enum):
    """Derived session status shown on every card."""

    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    HALTED = "halted"  # blocked by a Claude rate limit ("You've hit your … limit ·")
    WAITING_CODEX = "waiting_codex"  # idle, but Codex quota is exhausted until reset
    IDLE = "idle"
    SNOOZED = "snoozed"  # alive & idle, but a background task it spawned is still running
    PARKED = "parked"
    DONE = "done"
    FAILED = "failed"


STATUS_ICON: dict[Status, str] = {
    Status.WORKING: "▶",
    Status.WAITING_INPUT: "⏸",
    Status.HALTED: "||",  # rate-limit halt — rendered red; distinct from the ⏸ pause
    Status.WAITING_CODEX: "😴",  # Codex quota exhausted; waiting for its reset window
    Status.IDLE: "❯",  # amber prompt chevron — turn done, awaiting YOUR input (not running ▶)
    Status.SNOOZED: "💤",  # background task still running while the session itself is idle
    Status.PARKED: "☾",
    Status.DONE: "✓",
    Status.FAILED: "✗",
}

# Sort weight for the flat list / kanban ordering: attention first.
STATUS_ORDER: dict[Status, int] = {
    Status.WAITING_INPUT: 0,
    Status.HALTED: 1,  # stuck on a rate limit — surfaces high, just under input-needed
    Status.WAITING_CODEX: 2,  # stuck on Codex quota; lower than Claude halt, above normal work
    Status.WORKING: 3,
    Status.IDLE: 4,
    Status.SNOOZED: 5,  # alive but quietly busy in the background — low attention, below idle
    Status.PARKED: 6,
    Status.DONE: 7,
    Status.FAILED: 8,
}

# One-line meaning per status. Co-located with STATUS_ICON / STATUS_ORDER so the
# help legend (TUI ``_status_legend``) is generated from code and can never drift:
# the asserts below force every Status to carry an icon, a sort weight AND a help
# line, so adding or removing a status fails import until all three are updated.
STATUS_HELP: dict[Status, str] = {
    Status.WORKING: "live — the agent is busy right now",
    Status.WAITING_INPUT: "live — paused, waiting for your input",
    Status.HALTED: "live — stopped on a Claude rate limit; nothing will revive it",
    Status.WAITING_CODEX: "live — idle, waiting for OpenAI Codex quota reset",
    Status.IDLE: "live — idle, waiting for your input",
    Status.SNOOZED: "live — idle, waiting on a background task to finish",
    Status.PARKED: "closed — process gone; resume with r",
    Status.DONE: "AIM marked achieved (done)",
    Status.FAILED: "ended in failure",
}

assert set(STATUS_ICON) == set(Status), "every Status needs a STATUS_ICON entry"
assert set(STATUS_ORDER) == set(Status), "every Status needs a STATUS_ORDER entry"
assert set(STATUS_HELP) == set(Status), "every Status needs a STATUS_HELP entry"

# A HALTED row wears a green ▶ AFTER its red || when ccc will auto-revive it once that
# account's rate limit resets (resume.will_auto_resume: `resume_halted` on, account
# attributable, transcript on disk). So the icon is honest at a glance:
#   ||▶  the limit reset will bring this session back by itself — nothing to do
#   ||   stranded: it will sit here until YOU resume it (r)
# The suffix is per-session, not a static icon, so it never promises a revival that the
# resume watcher would not actually perform.
HALTED_RESUME_ICON = "▶"
HALTED_RESUME_HELP = "live — rate-limit halt; auto-resumes when that account's limit resets"


@dataclass
class LiveSession:
    """A session currently registered in ``~/.claude/sessions/<pid>.json``."""

    pid: int
    session_id: str
    cwd: str
    kind: str = "interactive"  # interactive | bg | fleet
    entrypoint: str = "cli"  # cli (real user session) | sdk-cli (headless `claude -p`)
    raw_status: str = "idle"  # busy | idle | waiting
    name: str | None = None
    agent: str = "claude"
    started_at: int = 0  # epoch ms
    updated_at: int = 0  # epoch ms
    status_updated_at: int = 0  # epoch ms
    alive: bool = False
    # Absolute config dir of the account whose registry this entry came from
    # (stamped by ``ClaudeAdapter.discover``). "" when the id is live in TWO account
    # registries at once — a D9 conflict the adapter refuses to attribute (see
    # ``conflict``), so reconcile must not persist it.
    config_dir: str = ""
    # True when this id was found live in more than one account registry (D9): the
    # adapter leaves ``config_dir`` blank and callers refuse resume/focus until one exits.
    conflict: bool = False


@dataclass
class SessionEvent:
    """One normalized conversation event of a session transcript.

    Emitted by :meth:`ClaudeAdapter.session_events` (the ONLY place that knows the
    transcript JSONL schema) and consumed by :mod:`command_center.sessionmd` (the
    renderer). ``kind`` is one of:

    * ``"prompt"`` — a human-typed prompt (``text``); same filter as
      ``all_user_prompts``, so prompt events align 1:1 with the prompts tab.
    * ``"text"`` — an assistant text block (``text``).
    * ``"tool"`` — a tool call: ``tool_name`` + raw ``tool_input`` dict, with the
      paired ``tool_result`` text once the transcript's matching result record was
      seen (``None`` while/if unpaired). Thinking blocks are never emitted.
    """

    kind: str  # "prompt" | "text" | "tool"
    text: str = ""  # prompt / assistant text (kind prompt|text)
    tool_name: str = ""  # kind tool
    tool_input: dict = field(default_factory=dict)  # kind tool — raw input payload
    tool_result: str | None = None  # kind tool — paired result text (None = unpaired)


@dataclass
class AimRevision:
    """One entry in a session's AIM history — the progression of its done-condition."""

    aim: str
    score: int  # the AIM's specificity score when it became current (-1 if unknown)
    created_at: int  # epoch ms when this AIM became current (0 = unknown)
    short_aim: str | None = None  # the cheap-model short label for this AIM (None = not generated)


@dataclass
class FileLock:
    """A cross-session advisory lock a session holds on a file while it edits it."""

    file_path: str  # absolute path — one holder at a time
    session_id: str
    acquired_at: int  # epoch ms when first taken this turn
    refreshed_at: int  # epoch ms of the last edit; the TTL is measured from here


@dataclass
class FileLockWaiter:
    """A session waiting to edit a file another session currently holds."""

    file_path: str
    session_id: str  # the waiting session
    since: int  # epoch ms it started waiting


@dataclass
class Subgoal:
    """One checklist item toward a session's AIM."""

    id: int
    session_id: str
    position: int
    text: str
    checked: bool
    source: str = "user"  # "user" (manual) | "auto" (cheap-model derive) | "agent" (in-session)
    weight: int = 1  # importance multiplier for weighted progress (essential=2, optional=1)
    check_cmd: str | None = None  # optional shell predicate; exit 0 auto-ticks this sub-goal
    model: str | None = None  # model that authored it (auto/agent); None for manual edits
    derived_aim_rev: int = 0  # the AIM revision (1-based) this checklist was built against


@dataclass
class SubgoalRevision:
    """One entry in a session's sub-goal history — how the checklist evolved.

    Mirrors :class:`AimRevision`. ``items`` is the snapshot at this version; the
    drift fields hold the impartial checker's verdict on the change into it.
    """

    items: list[tuple[str, bool]]  # [(text, checked), …] at this version
    aim: str | None  # the AIM the checklist tracked at this version
    aim_rev: int  # the AIM revision (1-based) it was built for
    trigger: str  # auto-derive | user-edit | agent-merge | aim-change
    model: str | None  # model that authored it (None = manual)
    drift_severity: str  # '' | pending | none | low | medium | high
    drift_reason: str | None
    created_at: int  # epoch ms this version became current


# Future-job model choices: which Claude model a job's overseer / executor runs on.
# The session runs ON the overseer's model; when the executor differs, the overseer is
# told to delegate implementation to Agent-tool subagents on the executor's model.
LLM_CHOICES: tuple[str, ...] = (
    "fable-5",
    "opus-5",
    "opus-4.8",
    "opus-4.8-1m",
    "sonnet-5",
    "haiku-4.5",
)
DEFAULT_LLM = "fable-5"
# Full model ids for ``claude --model`` (the overseer the session runs on).
# ``opus-4.8-1m`` is the 1M-context beta form; fable-5 and sonnet-5 are natively 1M.
LLM_MODEL_IDS: dict[str, str] = {
    "fable-5": "claude-fable-5",
    "opus-5": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4.8-1m": "claude-opus-4-8[1m]",
    "sonnet-5": "claude-sonnet-5",
    "haiku-4.5": "claude-haiku-4-5-20251001",
}
# The Agent-tool ``model`` enum used in the delegation instruction (executor subagents).
# The enum has no 1M variant, so opus-4.8-1m delegates to plain "opus".
LLM_AGENT_ALIAS: dict[str, str] = {
    "fable-5": "fable",
    "opus-5": "opus",
    "opus-4.8": "opus",
    "opus-4.8-1m": "opus",
    "sonnet-5": "sonnet",
    "haiku-4.5": "haiku",
}
# Shorthands whose prefix match is genuinely ambiguous and so must be pinned explicitly.
# ``"opus"`` prefixes three choices (opus-5, opus-4.8, opus-4.8-1m) and none of them is a
# prefix of the others, so :func:`expand_llm_choice`'s shortest-wins rule cannot decide and
# would reject the input. Bare ``"opus"`` means the CURRENT Opus generation; an older
# generation needs its version (``"opus-4"`` → opus-4.8).
LLM_PREFIX_ALIASES: dict[str, str] = {"opus": "opus-5"}
# Separator glyph between the overseer and executor model in a draft's models readout.
LLM_ARROW = "▸"
# Valid ``claude --effort`` levels. Job launches pass the config ``launch_effort``
# (default xhigh) explicitly so the session's effort never silently depends on the
# user's ~/.claude/settings.json ``effortLevel`` (whose absence means "high").
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")


def model_label(model_id: str | None) -> str:
    """Reverse-map a raw ``claude --model`` id to its ccc short choice name.

    ``"claude-fable-5"`` → ``"fable-5"`` via :data:`LLM_MODEL_IDS` (values are unique, so
    the reverse is unambiguous). An id not in the map is returned unchanged — an
    unknown/newer model still surfaces its raw id rather than vanishing. ``None`` or an
    empty id → ``""``. Used to label a session's OBSERVED model in the vault mirrors.
    """
    if not model_id:
        return ""
    for choice, raw in LLM_MODEL_IDS.items():
        if raw == model_id:
            return choice
    return model_id


def model_effort_cell(model: str, effort: str) -> str:
    """Compact ``model·effort`` label for the ccc ``model`` column (TUI + ``ccc ls``).

    *model* is a session's OBSERVED model (already a :func:`model_label` result): a known
    :data:`LLM_CHOICES` name is shown as-is; any other (unknown/newer) raw id has a leading
    ``"claude-"`` stripped (``"claude-brand-new-9"`` → ``"brand-new-9"``). *effort* is the
    reasoning level. The two are joined with ``"·"`` when both are present; either alone
    stands on its own; both empty → ``"—"`` (a draft that never ran, or nothing observed yet).
    """
    model_part = model if model in LLM_CHOICES else model.removeprefix("claude-") if model else ""
    parts = [part for part in (model_part, effort) if part]
    return "·".join(parts) if parts else "—"


def expand_llm_choice(value: str) -> str | None:
    """Canonicalize a model choice, accepting a unique prefix.

    ``"fable"`` → ``"fable-5"``, ``"opus"`` → ``"opus-5"``, ``"sonnet"`` → ``"sonnet-5"``;
    an exact choice passes through unchanged. When one choice is itself a prefix of the
    others it matches (``"opus-4"`` hits both ``opus-4.8`` and ``opus-4.8-1m``), the shortest
    wins — the longer variant needs its own longer prefix (``"opus-4.8-"``). Shorthands whose
    match set has no such common prefix are pinned in :data:`LLM_PREFIX_ALIASES` (bare
    ``"opus"`` → the current generation) since shortest-wins cannot decide them. Returns
    ``None`` when *value* is empty or genuinely ambiguous/unknown — the caller then
    rejects the edit and keeps the previous value. Matching is case-insensitive and
    whitespace-trimmed.
    """
    needle = value.strip().lower()
    if not needle:
        return None
    if needle in LLM_CHOICES:
        return needle
    if needle in LLM_PREFIX_ALIASES:
        return LLM_PREFIX_ALIASES[needle]
    matches = [choice for choice in LLM_CHOICES if choice.startswith(needle)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        shortest = min(matches, key=len)
        if all(choice.startswith(shortest) for choice in matches):
            return shortest
    return None


@dataclass
class Session:
    """A tracked session card (one row in the ``sessions`` table)."""

    session_id: str
    cwd: str = ""
    agent: str = "claude"
    # Absolute config dir of the account this session last ran under (the LAST OBSERVED
    # live account — stamped by discover/reconcile and the in-session hooks, never
    # silently flipped). "" means UNKNOWN: in multi-account mode that REFUSES
    # resume/start rather than defaulting to private (D3/O3/O12). Legacy rows are
    # backfilled to ``claude_home()`` by the store migration, so "" only ever means a
    # freshly-created, not-yet-observed row.
    config_dir: str = ""
    version: str | None = None  # Claude Code version last seen in the transcript (e.g. "2.1.193")
    name: str | None = None
    aim: str | None = None  # "this session is done when: ..."
    short_aim: str | None = (
        None  # cheap-model ≤10-word label for the /aim column (None = use full aim)
    )
    aim_score: int = -1  # AIM specificity 0..100 (-1 = unscored); < threshold => vague/red
    aim_score_reason: str | None = None  # one-line "why", from the LLM refine pass
    aim_prev: str | None = None  # the prior AIM, set on a change; cleared next turn (statusline →)
    aim_changed_at: int = 0  # epoch ms the AIM last changed (0 = never)
    aim_met: bool = False  # impartial "is the AIM fulfilled?" verdict (out-of-band, latest-wins)
    aim_assessed_at: int = 0  # epoch ms of the last self-assessment (drives the new-turn gate)
    aim_met_reason: str | None = None  # one-line "why", from the AIM-met checker
    status: str = Status.IDLE.value
    done: bool = False
    done_at: int = 0  # epoch ms when marked done (0 = not done / unknown)
    next_step: str | None = None
    next_step_source: str = "auto"  # auto | user
    summary: str | None = None
    blocked_on: str | None = None
    deadline: str | None = None  # ISO-8601 date YYYY-MM-DD
    done_check_cmd: str | None = None  # exit 0 => done
    importance: int = 0  # 0..3 -> "", "!", "!!", "!!!"
    iterm_session_id: str | None = None  # $ITERM_SESSION_ID, to focus the live tab
    prompt_count: int = 0  # user prompts seen (for nag throttling)
    last_response_at: int = 0  # epoch ms
    # Epoch ms reconcile first saw the process gone after being alive (0 = alive, or
    # closed before this field existed — display falls back to last_response_at then).
    # Cleared back to 0 the moment the session is observed live again (resume/reopen).
    closed_at: int = 0
    # Epoch-ms when a close-after-turn was requested (`mark-done --close`); 0 = none.
    # The release-locks Stop hook atomically claims it and spawns the detached closer.
    close_requested_at: int = 0
    last_seen_pid: int | None = None
    keep: bool = False  # exempt from the idle reaper
    auto_closed: bool = False
    needs_summary: bool = False
    context_offset: int = 0  # bytes of the transcript consumed by auto-progress
    last_progress_at: int = 0  # epoch ms of the last real grading pass (grade-on-turn debounce)
    subgoals_adaptive: bool = False  # checklist re-derives to track the AIM when it changes
    subgoals_aim_rev: int = 0  # AIM revision (1-based) the current checklist was built for
    # Manual progress-bar override, 0..100 (None = auto: the bar shows the sub-goal ratio).
    # Set from the TUI (`e` form / Enter on the progress column); cleared on mark-done.
    manual_progress: int | None = None
    drift_severity: str = ""  # impartial drift verdict: '' (unchecked) | none | low | medium | high
    drift_reason: str | None = None  # one-line "why", from the drift checker
    drift_at: int = 0  # epoch ms drift was flagged (0 = none)
    drift_ack_at: int = 0  # epoch ms drift was acknowledged/resolved (>= drift_at => cleared)
    todos: str | None = (
        None  # JSON snapshot of the live TodoWrite/Task list, [[status, subject], …]
    )
    todos_updated_at: int = 0  # epoch ms of the last todo snapshot
    draft: bool = False  # a not-yet-started "future job": a saved AIM+prompt, launched on demand
    prompt: str | None = None  # the prompt to send when a draft is launched (defaults to the AIM)
    start_when: str | None = (
        None  # draft: free-text "when I intend to start" (e.g. during holidays)
    )
    # Draft: FIXED start date (ISO YYYY-MM-DD). Unlike the free-text start_when, this is
    # machine-readable: it sinks the job into the SCHEDULED bucket (below FINISHED) and
    # makes `ccc start-job` warn + ask before launching ahead of the date.
    start_date: str | None = None
    # Parked-prompt auto-fire (see park.py): epoch SECONDS the armed draft dispatches at
    # (0 = not armed) and the rate-limit window that produced that timestamp
    # ('five_hour' | 'seven_day' | 'fable_week'; '' = none). Consumed atomically by
    # Store.claim_draft on launch; not mirrored into the Obsidian job file.
    fire_at: int = 0
    fire_window: str = ""
    # The full session UUID of another job this one depends on finishing first (NULL/'' =
    # none; a single dependency). Drives the red |--> marker + hoisting (see deps.py) and
    # the launch guard; round-trips through the future-job file + all three mirrors.
    depends_on: str | None = None
    # How a future job launches: 'claude' (normal session), 'codex'
    # (/codex-implement-task-and-claude-review, Codex implements via patch, Claude verifies),
    # 'codex-write' (Codex edits files directly).
    job_type: str = "claude"
    # Which models a future job runs on. The session runs ON the overseer's model
    # (`claude --model LLM_MODEL_IDS[llm_overseer]`); when llm_exec differs, the launch
    # prompt tells the overseer to delegate implementation to Agent-tool subagents on the
    # exec model (Fable-5 oversees, Opus executes — see cli.cmd_start_job).
    llm_overseer: str = DEFAULT_LLM
    llm_exec: str = DEFAULT_LLM
    # OBSERVED runtime values (distinct from the llm_overseer/llm_exec job config, which are
    # pure DB defaults for a session that was never launched as a ccc job): the model the
    # session actually ran on (reverse-mapped label via model_label) and its --effort
    # reasoning level. Captured by core.reconcile (model always; effort for live sessions,
    # flag-or-settings-default) and cli.cmd_statusline. "" until observed.
    model: str = ""
    effort: str = ""
    # Future-job file mirror (see future_files.py / futuresync.py): the vault-relative
    # path of the draft's markdown file, the sha256 at last sync (echo suppression), when
    # it last synced, and when its file first went missing (0 = present; starts the grace).
    future_file: str | None = None
    future_sync_hash: str | None = None
    future_synced_at: int = 0
    future_missing_since: int = 0
    archived: bool = False
    created_at: int = 0
    updated_at: int = 0
    # DERIVED, not columns: the session's FIRST recorded AIM — revision (1) — and that
    # revision's cheap-model short label, joined off ``aim_history`` by every Store read
    # (see store._SESSION_SELECT). Both are None when the AIM predates history tracking;
    # callers fall back to the live AIM. This is what ``aim_column = "first"`` renders.
    first_aim: str | None = None
    first_short_aim: str | None = None


# Future-job launch types: how `ccc start-job` turns a draft's prompt into a launch command.
JOB_TYPES: tuple[str, ...] = ("claude", "codex", "codex-write")
JOB_TYPE_LABELS: dict[str, str] = {
    "claude": "Claude Code (normal)",
    "codex": "Codex delegate (patch)",
    "codex-write": "Codex delegate (write)",
}
CODEX_WORKFLOW_NAME = "codex-implement-task-and-claude-review"
CODEX_WORKFLOW_JOB_TYPES = frozenset({"codex", "codex-write"})
CODEX_WORKFLOW_BADGE = "OAI"

# The red left-edge marker on a row whose dependency is UNSATISFIED (see deps.py). Single
# source: the TUI paints it across cols 0-2 (``|`` + ``--`` + ``>``); ``ccc ls`` prefixes a
# hoisted row's line with it; the detail pane shows it before the /depends-on value.
DEP_MARKER = "|-->"


def job_launch_prefix(job_type: str) -> str:
    """Prompt prefix for a future job at launch (empty = a normal Claude job).

    A 'codex'/'codex-write' draft launches into ``/codex-implement-task-and-claude-review`` so
    Codex does the implementation and Claude only verifies (see that skill).
    """
    if job_type == "codex":
        return "/codex-implement-task-and-claude-review "
    if job_type == "codex-write":
        return "/codex-implement-task-and-claude-review --write "
    return ""


def now_ms() -> int:
    """Current time in epoch milliseconds (matches Claude Code's timestamps)."""
    return int(time.time() * 1000)


def subgoal_provenance(subgoals: list[Subgoal]) -> str:
    """Human label of who authored a checklist + which AIM revision it tracks.

    e.g. ``auto (claude-haiku-4-5) · from AIM v2`` or ``manual`` (a user-edited list).
    Empty for an empty list. Used by the TUI header so a checklist's origin is never
    ambiguous.
    """
    if not subgoals:
        return ""
    sources = {s.source for s in subgoals}
    models = sorted({s.model for s in subgoals if s.model})
    model = models[0] if len(models) == 1 else (", ".join(models) if models else "")
    src = next(iter(sources)) if len(sources) == 1 else "mixed"
    if src == "user":
        who = "manual"
    elif src == "auto":
        who = f"auto ({model or 'cheap model'})"
    elif src == "agent":
        who = f"agent ({model or 'in-session'})"
    else:
        who = f"mixed ({model})" if model else "mixed"
    rev = max((s.derived_aim_rev for s in subgoals), default=0)
    return f"{who} · from AIM v{rev}" if rev else who


def drift_unresolved(session: Session) -> bool:
    """True if the session has a flagged, not-yet-acknowledged sub-goal drift (blue dot).

    A new flag resets ``drift_ack_at`` to 0; ``ack_drift`` sets it > 0; a clean check
    clears the severity. So "unresolved" = flagging severity AND unacknowledged.
    """
    return session.drift_severity in ("low", "medium", "high") and session.drift_ack_at == 0


def short_id(session_id: str, width: int = 8) -> str:
    """First *width* chars of a session id — enough to eyeball/disambiguate a row.

    The full id always lives in the row's ``c --resume <id>`` command; this is the
    scannable short form shown on every line so duplicates are obvious at a glance.
    """
    return (session_id or "")[:width].ljust(width)


def humanize_age(epoch_ms: int, now: int | None = None) -> str:
    """Compact age such as ``45s``, ``12m``, ``3h``, ``5d``, ``2w``; ``—`` if unknown."""
    if not epoch_ms:
        return "—"
    now = now if now is not None else now_ms()
    seconds = max(0, (now - epoch_ms) // 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    return f"{days // 7}w"


def iso_date(epoch_ms: int) -> str:
    """Render an epoch-ms timestamp as a local ``YYYY-MM-DD`` date (empty if 0)."""
    if not epoch_ms:
        return ""

    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%Y-%m-%d")


def iso_datetime(epoch_ms: int) -> str:
    """Render an epoch-ms timestamp as a local ``YYYY-MM-DD HH:MM`` (empty if 0)."""
    if not epoch_ms:
        return ""

    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%Y-%m-%d %H:%M")


def importance_marks(level: int) -> str:
    """Render an importance level as exclamation marks (0..3 -> '', '!', '!!', '!!!')."""
    return "!" * max(0, min(3, level))


def progress_fraction(checked: int, total: int) -> float | None:
    """Fraction of checklist items done, or ``None`` when there is no checklist."""
    if total <= 0:
        return None
    return checked / total


def effective_progress(manual: int | None, checked: int, total: int) -> float | None:
    """The fraction every progress bar shows: manual override else the checklist ratio.

    *manual* is the session's ``manual_progress`` (0..100, clamped; ``None`` = auto).
    A set value wins over the sub-goal ratio at EVERY bar site (TUI table + detail
    head, ``ccc ls``, the statusline and ``ccc aim``) — keep this the single chokepoint
    so the sites can never disagree.
    """
    if manual is not None:
        return min(100, max(0, manual)) / 100
    return progress_fraction(checked, total)


def parse_manual_progress(value: str) -> int | None:
    """Parse a manual progress-bar edit: blank → ``None`` (auto), ``"40"``/``"40%"`` → 40.

    Raises :class:`ValueError` on anything else (non-numeric, out of 0..100) so the
    caller can reject the edit and keep the stored value unchanged.
    """
    text = value.strip().removesuffix("%").strip()
    if not text:
        return None
    pct = int(text)
    if not 0 <= pct <= 100:
        raise ValueError(f"progress % out of range 0..100: {pct}")
    return pct


def progress_bar(fraction: float | None, width: int = 10) -> str:
    """Render a fixed-width bar; ``—`` (padded) when no checklist exists yet."""
    if fraction is None:
        return "—".ljust(width)
    fraction = min(1.0, max(0.0, fraction))
    filled = round(fraction * width)
    return "▓" * filled + "░" * (width - filled)


DONE_WORD = "DONE"

_XTERM_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_XTERM_BASE_16 = {
    0: (0, 0, 0),
    1: (205, 0, 0),
    2: (0, 205, 0),
    3: (205, 205, 0),
    4: (0, 0, 238),
    5: (205, 0, 205),
    6: (0, 205, 205),
    7: (229, 229, 229),
    8: (127, 127, 127),
    9: (255, 0, 0),
    10: (0, 255, 0),
    11: (255, 255, 0),
    12: (92, 92, 255),
    13: (255, 0, 255),
    14: (0, 255, 255),
    15: (255, 255, 255),
}


def xterm_rgb(code: int) -> tuple[int, int, int]:
    """RGB of an xterm-256 colour index (6×6×6 cube, grayscale ramp, classic 16)."""
    if 16 <= code <= 231:
        idx = code - 16
        return (
            _XTERM_CUBE_LEVELS[idx // 36],
            _XTERM_CUBE_LEVELS[(idx % 36) // 6],
            _XTERM_CUBE_LEVELS[idx % 6],
        )
    if 232 <= code <= 255:
        v = 8 + 10 * (code - 232)
        return (v, v, v)
    return _XTERM_BASE_16.get(code, (128, 128, 128))


def empty_track_tint(glyph_rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Background RGB for a DONE letter over the *empty* (``░``) track: a faint 25 % tint.

    ``░`` inks 25 % of its cell in the track colour, so over a dark terminal a ░ cell averages
    to a quarter of the glyph colour — the closest a letter's (necessarily solid) background
    can get to the dotted empty texture around it.
    """
    r, g, b = glyph_rgb
    return (round(r * 0.25), round(g * 0.25), round(b * 0.25))


def done_bar_parts(
    fraction: float | None, width: int = 10
) -> tuple[str, str, str, tuple[bool, ...]]:
    """Split a progress bar into ``(left, "DONE", right, fills)`` — "model thinks done" overlay.

    ``DONE`` is stamped into the centre cells of the bar — so the fill proportion still reads
    on both sides — and *fills* says, per ``DONE`` letter, whether the cell it covers belongs
    to the filled part. The DONE bar renders its filled cells as SOLID ``█`` (not the ordinary
    bar's dotted ``▓``): a letter's background is one flat colour, so no shade glyph next to it
    can ever match exactly — with a solid fill, painting the letters on the very same fill
    colour makes letter cells and bar cells pixel-identical (empty-track letters get
    :func:`empty_track_tint`). When there is no checklist yet (``fraction is None``) the track
    is all empty cells so the verdict still renders (rather than the padded ``—``). The three
    string pieces always concatenate to exactly *width* characters, preserving column
    alignment; a *width* below ``len("DONE")`` degrades to a truncated centre with empty sides.
    """
    base = progress_bar(fraction, width) if fraction is not None else "░" * width
    base = base.replace("▓", "█")
    if width <= len(DONE_WORD):
        word = DONE_WORD[:width]
        return ("", word, "", tuple(ch == "█" for ch in base[: len(word)]))
    pos = (width - len(DONE_WORD)) // 2
    covered = base[pos : pos + len(DONE_WORD)]
    return (
        base[:pos],
        DONE_WORD,
        base[pos + len(DONE_WORD) :],
        tuple(ch == "█" for ch in covered),
    )


def dumps_todos(todos: list[tuple[str, str]]) -> str:
    """Serialize a ``[(status, subject), …]`` todo list to a JSON string for the store."""
    return json.dumps([[status, subject] for status, subject in todos])


def loads_todos(raw: str | None) -> list[tuple[str, str]]:
    """Parse a stored todo JSON string back to ``[(status, subject), …]`` (empty on junk)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[tuple[str, str]] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return out


def todos_counts(todos: list[tuple[str, str]]) -> tuple[int, int]:
    """Return ``(completed, total)`` counts for a todo list."""
    return (sum(1 for status, _ in todos if status == "completed"), len(todos))


def todo_box(status: str) -> str:
    """Checkbox glyph for a todo *status*: ``☒`` done, ``◧`` in-progress, ``☐`` pending.

    Shared by the one-line status-line strip and the TUI detail list so the two
    surfaces show the same boxes.
    """
    if status == "completed":
        return "☒"
    if "progress" in status:
        return "◧"
    return "☐"


def first_json_object(raw: str | None) -> dict[str, object]:
    """Pull the first ``{...}`` JSON object out of a (possibly chatty) reply; ``{}`` on failure.

    Shared by the LLM-reply parsers (auto-progress sub-goals, AIM scoring) so the
    extraction logic lives in exactly one place.
    """
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def deadline_badge(
    iso: str | None, warn_days: int = 2, today: date | None = None
) -> tuple[str, str]:
    """Return ``(text, severity)`` where severity is green | amber | red | none."""
    if not iso:
        return ("", "none")
    try:
        due = date.fromisoformat(iso)
    except ValueError:
        return (iso, "none")
    today = today if today is not None else date.today()
    days = (due - today).days
    if days < 0:
        return (f"overdue {-days}d", "red")
    if days == 0:
        return ("due today", "red")
    if days <= warn_days:
        return (f"due {days}d", "amber")
    return (f"due {days}d", "green")


def parse_iso_date(iso: str | None) -> date | None:
    """*iso* as a :class:`date`, or ``None`` when unset / not a valid ``YYYY-MM-DD``."""
    if not iso:
        return None
    try:
        return date.fromisoformat(iso.strip())
    except ValueError:
        return None


def scheduled_date(session: Session) -> date | None:
    """A draft's fixed start date (``None`` for non-drafts and unset/invalid dates).

    The single predicate deciding whether a future job belongs to the SCHEDULED
    bucket (core sort, TUI section, dashboard) — an unparseable ``start_date``
    degrades to plain FUTURE behaviour rather than erroring.
    """
    if not session.draft:
        return None
    return parse_iso_date(session.start_date)


def days_until_start(session: Session, today: date | None = None) -> int | None:
    """Days until a draft's fixed start date, when that date is still ahead.

    ``None`` when there is no (valid) start date or the date has arrived — so a
    non-``None`` return means "launching now is N days premature" (drives the
    ``cmd_start_job`` guard and the TUI confirm dialog).
    """
    when = scheduled_date(session)
    if when is None:
        return None
    days = (when - (today if today is not None else date.today())).days
    return days if days > 0 else None


def aim_score_badge(score: int, threshold: int) -> tuple[str, str]:
    """Return ``(text, severity)`` for an AIM specificity score; mirrors ``deadline_badge``.

    ``score`` is 0..100 (or -1 when unscored). Severity drives the AIM color: ``red``
    when the AIM is too vague (below *threshold*), ``green`` when specific, ``none``
    (no paint) when unscored. ``text`` is a short badge ("vague") only for the red
    case — callers paint the AIM text itself; this just carries the severity.
    """
    if score < 0:
        return ("", "none")
    if score < threshold:
        return ("vague", "red")
    return ("", "green")


def low_aim_score(aim: str | None, score: int, threshold: int) -> bool:
    """True when an AIM has a known concreteness score below the configured threshold."""
    return bool(aim) and 0 <= score < threshold


def aim_score_pct(aim: str | None, score: int) -> str:
    """Numeric AIM-score chip for the ``/aim`` column.

    ``""`` when no AIM is set OR while the AIM is still unscored (``score < 0``) — the
    raw ``-1`` sentinel is never surfaced. With ``aim_score_on_set`` disabled the LLM
    refine never fires, so a bare ``-1`` would otherwise read as a permanently stuck
    "pending" state; a blank chip degrades gracefully instead. Else ``"NN%"`` (0–100).
    """
    if not aim or score < 0:
        return ""
    return f"{score}%"


def short_version(version: str | None) -> str:
    """The patch component of a Claude Code version, for the narrow ``ver`` column.

    ``"2.1.193"`` → ``"193"`` — the bit after the last dot. The ``2.1`` prefix is
    near-constant across sessions, so the column shows only the part that varies.
    Empty string when the version is unknown.
    """
    if not version:
        return ""
    return version.rsplit(".", 1)[-1]


def version_column_text(version: str | None, *, uses_codex_workflow: bool = False) -> str:
    """Text for the narrow ``ver`` column.

    Codex-delegated sessions use a fixed ``OAI`` badge in place of the Claude Code
    patch version. Both strings are three cells wide, preserving existing alignment.
    (A SCHEDULED draft row doesn't use this at all — its compact start date spans
    the importance + ver cells, see ``short_date_label`` / ``tui._add_session_row``.)
    """
    return CODEX_WORKFLOW_BADGE if uses_codex_workflow else short_version(version)


def short_date_label(when: date) -> str:
    """Compact ``D.M.YY`` form of a date (2026-08-11 → ``11.8.26``).

    Used where horizontal space is scarce: the SCHEDULED row's leftmost cells,
    where the label spans the importance (!!!) and ver columns without widening
    either beyond what their normal content needs.
    """
    return f"{when.day}.{when.month}.{when.year % 100}"


def models_readout(session: Session) -> str:
    """Plain model label of a draft's configured pair for the ``model`` column.

    When overseer and executor are the same model (the common case — ``fable-5 ▸ fable-5``
    is redundant noise in a narrow column) the single name is returned; otherwise the
    ``<overseer> ▸ <executor>`` pair. Shown in the ``model`` column for future-job (draft)
    rows — the TUI colour-codes each model name per
    :data:`command_center.views.tui._LLM_STYLE`; ``ccc ls`` renders it plain. Both names
    come straight from the row (default ``fable-5`` for each).
    """
    if session.llm_overseer == session.llm_exec:
        return session.llm_overseer
    return f"{session.llm_overseer} {LLM_ARROW} {session.llm_exec}"


def aim_column_first(aim_column: str) -> bool:
    """Whether the ``/aim`` column renders revision (1), from the ``aim_column`` config.

    Only the explicit ``"latest"`` opts back into the current-AIM column; anything else
    (including an empty or misspelled value) keeps the default first-AIM rendering.
    """
    return (aim_column or "").strip().lower() != "latest"


def display_aim(session: Session, first: bool = False) -> str | None:
    """The compact AIM label: the cheap-model short label if one was generated, else the
    full AIM verbatim. The full AIM is always kept intact in the store and shown in the
    detail pane / aim-history; this is what the narrow ``/aim`` column AND the in-session
    status line render. ``store.set_aim`` clears the label on every AIM change, so this is
    always the LATEST revision's label (or the full new AIM until the generator backfills).

    With *first* (config ``aim_column = "first"``, the default) it renders revision **(1)**
    instead — the done-condition as originally typed. That keeps the ``/aim`` column, and
    every "which job is this?" label built from it, stable while the AIM is sharpened over
    the session's life; the current AIM stays visible in the status line, the detail pane
    and ``ccc aim-history``. Sessions whose AIM predates history tracking have no recorded
    revision (1) — they fall back to the live AIM.
    """
    if first and session.first_aim:
        return session.first_short_aim or session.first_aim
    return session.short_aim or session.aim


def synthesize_aim_revision(session: Session) -> AimRevision:
    """One :class:`AimRevision` from a session's live AIM — the pre-history fallback.

    A session whose AIM predates ``aim_history`` tracking has no recorded revisions; the
    peek panel, ``ccc aim-history`` and the running/done mirrors all fall back to this one
    synthetic revision so the progression is never shown empty. Callers guard on a truthy
    ``session.aim`` before calling (the ``or ""`` only satisfies the type checker).
    """
    return AimRevision(
        session.aim or "",
        session.aim_score,
        session.aim_changed_at or session.created_at,
        session.short_aim,
    )


def derive_status(
    live: LiveSession | None,
    stored: Session | None,
    halted: bool = False,
    background: bool = False,
    codex_waiting: bool = False,
) -> Status:
    """Combine the live raw status, process liveness, the done flag and a rate-limit halt.

    ``halted`` is the transcript-derived signal (``Adapter.is_halted``) that the
    session's last turn ended in a Claude rate-limit error ("You've hit your … limit ·").
    It only applies to a still-open (alive) session — a closed one is PARKED regardless —
    and ranks below ``done`` (a finished session is finished even if its last turn 429'd).

    ``done`` wins over everything EXCEPT a live busy turn: a session marked done while
    the agent is literally mid-turn shows WORKING (▶) until that turn ends — the ✓
    appears the moment it stops being busy. Like SNOOZED / WAITING_CODEX this is
    derived live, never sticky: the next reconcile after the turn flips it to DONE.

    ``background`` is the live signal (``Adapter.has_background_task``) that the session
    has a still-running background task it spawned (e.g. a ``run_in_background`` shell).
    It only refines an otherwise-IDLE alive session into SNOOZED — never overrides
    working / waiting-for-input / halted — so it auto-clears the moment the task exits.

    ``codex_waiting`` is the account-level Codex quota signal, already gated by the
    caller to sessions using the Codex implementation workflow. It occupies the same
    otherwise-IDLE slot as SNOOZED, but with explicit precedence: SNOOZED wins when a
    live background task exists; otherwise an idle Codex-workflow session whose quota
    is exhausted becomes WAITING_CODEX until the reset window passes.
    """
    if stored is not None and stored.done:
        if live is not None and live.alive and (live.raw_status or "").lower() == "busy":
            return Status.WORKING  # done, but still mid-turn — ✓ waits for the turn to end
        return Status.DONE
    if live is None or not live.alive:
        return Status.PARKED
    if halted:
        return Status.HALTED
    raw = (live.raw_status or "").lower()
    if raw == "busy":
        return Status.WORKING
    if raw.startswith("wait"):
        return Status.WAITING_INPUT
    if background:
        return Status.SNOOZED  # idle, but a background task is still running
    if raw == "idle" and codex_waiting:
        return Status.WAITING_CODEX
    return Status.IDLE
