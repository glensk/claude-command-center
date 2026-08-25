"""Paths and user-tunable configuration.

Paths are resolved at call time (reading ``CLAUDE_HOME`` from the environment)
so tests can point the whole tool at a temporary directory. User tunables live
in ``~/.claude/command-center/config.toml`` and fall back to ``DEFAULTS``.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# A Claude account label feeds usage-cache filenames, so it must never contain a
# path separator: lowercase alphanumerics / dash / underscore, not starting with -/_.
_ACCOUNT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Config is a flat settings record — many fields is expected.
# pylint: disable=too-many-instance-attributes

DEFAULTS: dict[str, object] = {
    "idle_timeout_min": 60,  # auto-close interactive sessions idle longer than this
    "kill_closes_tab": False,  # also close the iTerm tab / tmux pane when reaping
    # Terminal launcher for resume/start-job: "iterm" (AppleScript tabs, macOS) or "tmux"
    # (windows in the persistent "ai" tmux session). With "iterm" but no osascript on
    # PATH (Linux/Termux/SSH-only box) the tmux path engages automatically.
    "launcher": "iterm",
    # ``claude --effort`` passed at job launch (start-job, both first-launch and resume
    # paths) so effort never depends on settings.json's effortLevel (unset there = high).
    # One of low|medium|high|xhigh; "" omits the flag (settings.json decides again).
    "launch_effort": "xhigh",
    "stale_days": 7,  # alert when a goal is parked this long with done unmet
    "deadline_warn_days": 2,  # amber badge / alert this many days before a deadline
    "llm_model": "claude-haiku-4-5",  # model for summary / next-step regeneration
    "score_model": "",  # model for the INDEPENDENT AIM rubric checker ("" = use llm_model)
    # Ordered fallback ladder for the AIM-score LLM call. Allowed rungs: "copilot", "gemini",
    # "codex", "claude", "custom"; the first that returns non-empty text serves, the rest are
    # tried in order. Unknown entries are skipped with a stderr warning at use time. This is a
    # DELIBERATE behaviour change (see docs/reference.md): with copilot/gemini/codex ahead of
    # claude the score call moves OFF Anthropic tokens when those CLIs are available.
    "score_backends": ["claude"],
    # shell cmd for the "custom" score rung (full prompt on stdin, model text on stdout)
    "score_custom_command": "",
    # Escape hatch for EVERY other headless LLM call ccc makes (summaries, drift,
    # AIM-met, sub-goal derive/grade, short-aim): when set, this shell command runs
    # instead of `claude -p` — full prompt on stdin, model text on stdout, with
    # CCC_LLM_PURPOSE / CCC_LLM_NOTE exported so a router can log or route per action.
    # Non-zero exit / empty output falls back to `claude -p`. "" = disabled.
    "llm_custom_command": "",
    "gemini_model": "",  # model flag for the gemini score rung ("" = the gemini CLI's own default)
    # alert channels: "auto" (native desktop notifier per platform), "macos", "linux", "slack".
    # Default "auto" -> osascript on macOS, notify-send (libnotify) on Linux.
    "notify": ["auto"],
    "statusline_enabled": True,  # render the "Done when:" status line
    "reap": False,  # daemon auto-closes idle sessions (INERT: off until `ccc init`)
    "summarize": False,  # daemon regenerates summary + next-step via the LLM (INERT: off)
    "max_summaries_per_run": 3,  # cap LLM calls per daemon pass (cost guard)
    "autoprogress": False,  # daemon auto-derives + auto-checks sub-goals for AIM sessions (INERT)
    "max_autoprogress_per_run": 3,  # cap auto-progress LLM passes per daemon run (cost guard)
    "grade_on_turn": False,  # grade progress right after each turn (detached) (INERT: off)
    "grade_debounce_sec": 30,  # min seconds between after-turn grader spawns per session
    "assess_aim_on_turn": False,  # self-assess "is the AIM fulfilled?" after each turn (INERT)
    "assess_aim_model": "",  # model for the AIM-met checker ("" = use llm_model); never the session
    "max_aim_assess_per_run": 3,  # cap AIM-met assessments per daemon fallback pass (cost guard)
    "aim_score_threshold": 50,  # AIM specificity < this (0..100) => vague: red + sharpen nudge
    "aim_score_on_set": False,  # refine the AIM score with an LLM call when the AIM changes (INERT)
    "verify_subgoals_llm": False,  # also LLM-verify each derived sub-goal is checkable (extra cost)
    "sharpen_every_n_turns": 1,  # agent re-sharpens a vague AIM every Nth prompt (0 = start only)
    "adapt_subgoals_on_aim_change": True,  # nudge the agent to re-align an adaptive checklist
    "drift_check": False,  # run the impartial drift checker after a sub-goal change (INERT: off)
    "drift_model": "",  # model for the drift checker ("" = use llm_model); never the session agent
    "short_aim": False,  # derive a short scannable AIM label for the /aim column (INERT: off)
    # generator: "auto" (codex if on PATH else claude) | "codex" (saves Claude tokens) | "claude"
    "short_aim_backend": "auto",
    "short_aim_model": "",  # model for the generator ("" = backend default; codex picks its own)
    "usage_refresh_sec": 5.0,  # TUI usage-card re-read/render cadence (drives the refresh timer)
    "copilot_usage": False,  # show a GitHub Copilot month-to-date usage card (gh API) (INERT: off)
    "copilot_usage_refresh_sec": 900,  # min sec between idle gh billing refreshes (cost guard)
    # While any job is WORKING/SNOOZED the Copilot fetch throttle drops to this shorter
    # "active" interval so the card tracks reality more closely; 0 or ≥ idle disables it.
    "copilot_usage_refresh_active_sec": 300,  # active-work gh billing throttle (~1/3 of idle)
    "copilot_model": "gpt-5.4",  # default model for /copilot delegation + Copilot card title
    "copilot_card_title": "Copilot",  # Copilot usage-card title prefix (before the model)
    # Monthly AI-Credit budget the card's bar is drawn against once the seat is on
    # usage-based (AI Credits) billing — premium requests were retired 2026-06, so
    # that meter reads 0. GitHub's API exposes no allowance figure, so this is a
    # chosen budget: 3,000 matches GitHub's current promo allowance (through
    # 2026-09-01); the documented Copilot Business per-user baseline is 1,900
    # credits/user/mo. Set it to whatever your seat is actually allotted.
    "copilot_credit_quota": 3000,
    # Claude /usage OAuth fetch: keep each account's usage card in step with `claude`'s
    # own /usage (incl. any weekly model-scoped window the status line never carries) by
    # fetching the OAuth usage endpoint out-of-band (reads the CLI's keychain token).
    # RENDER stays gated by usage_card_private/_work; this gates only the FETCH.
    "claude_usage": False,  # fetch the Claude /usage OAuth endpoint (INERT: off)
    "claude_usage_refresh_sec": 600,  # min sec between idle OAuth usage fetches per account
    # While any job is WORKING/SNOOZED the fetch throttle drops to this shorter "active"
    # interval so the cards track reality more closely; 0 or ≥ idle disables the speed-up.
    "claude_usage_refresh_active_sec": 200,  # active-work OAuth usage throttle (~1/3 of idle)
    # Multi-account Claude Code. ``claude_accounts`` maps labels to config dirs, one
    # ``"label=path"`` entry per line (list[str] so save_config round-trips it). Empty
    # (the default) ⇒ a single ``{"private": claude_home()}`` account, i.e. today's
    # behaviour. Labels are validated ``^[a-z0-9][a-z0-9_-]*$``.
    "claude_accounts": [],
    # Identity hard-link for the usage cards: ``"label=email"`` entries, same shape as
    # ``claude_accounts``. WHICH config dir a label points at is a path; WHICH Claude
    # account is actually logged into that dir can drift (a bare `/login` in the wrong
    # shell silently swaps it) — this pins the "Claude Code (work)"/"(private)" cards to
    # an ACTUAL email address instead, so the right numbers show under the right card
    # even after such a drift. Empty (the default) ⇒ no hard link, today's pure
    # path-based behaviour (see ``accounts.resolve_card_label``).
    "claude_account_emails": [],
    # Which account a NEW job (no explicit -A / account select) bills to: "" = default
    # account, a label = pin, "auto" = saturate-earliest-reset routing (see routing.py).
    "job_account": "",
    # The card render gates (t1..t4 / to / ta). False does NOT remove a card: it
    # collapses it to its titled top border, which is where its own chord is named.
    "usage_card_private": True,  # expand the Claude Code (private) usage card
    "usage_card_work": True,  # expand the Claude Code (work) usage card
    "usage_card_codex": True,  # expand the OpenAI Codex usage card
    "usage_card_copilot": True,  # EXPAND the Copilot card (copilot_usage gates the FETCH)
    # External homelab "overseer" alert-triage daemon (a SEPARATE project — unrelated to
    # ccc's own future-job plumbing). Its incidents feed two read-only TUI cards. Empty
    # (the default) = feature OFF: the cards render a placeholder and touch no disk. Point
    # it at the overseer's root dir; the DB is read at <dir>/state/overseer.sqlite (ro).
    "nixos_overseer_dir": "",
    "card_nixos_overseer_supervised": True,  # expand the "nixos overseer supervised" card
    "card_nixos_overseer_tier_a": False,  # expand the "nixos overseer tier_a" card (off by default)
    "llm_account": "private",  # account ccc's own headless `claude -p` calls bill to
    "prune_headless": True,  # daemon deletes contentless leftover rows (headless `claude -p` junk)
    "sync_tab_titles": True,  # daemon keeps every live tab's iTerm title in sync with its badge
    "daemon_interval_sec": 300,  # launchd StartInterval for `ccc daemon`
    "resume_halted": False,  # auto-resume session-limit-halted sessions on reset (INERT: off)
    "resume_stagger_sec": 120,  # min seconds between resumes across different repos (anti-herd)
    "resume_poll_sec": 30,  # resume-halted watcher poll interval
    "resume_max_attempts": 3,  # give up auto-resuming a session after this many failed tries
    "resume_launch_timeout_sec": 900,  # launched resume idle this long with no progress => retry
    "resume_continue_script": "",  # claude-session-continue.py path ("" = auto-resolve)
    "nag_every_n_turns": 1,  # remind to set an AIM every Nth prompt (1=every, 0=never)
    "nudge_unchecked_every_n_turns": 4,  # remind agent to tick finished sub-goals (0 = never)
    "file_lock_enabled": True,  # serialize same-file edits across sessions (PreToolUse lock)
    "file_lock_ttl_sec": 1800,  # a held lock past this with no edit is stale -> reclaimable
    "file_lock_wait_sec": 0,  # >0: PreToolUse polls a held lock this long before denying (0 = deny)
    "split_ratio": 0.6,  # TUI: left (table) fraction of the width, 0..1
    "tab_title": "!!!",  # iTerm tab title set when ccc starts ("" = leave alone)
    "tab_color": "red",  # iTerm tab color when ccc starts (name or #rrggbb; "" = none)
    "done_max_age_days": 3,  # hide done sessions older than this many days (0 = show all)
    "future_files": False,  # mirror each FUTURE job (draft) as an Obsidian md file (INERT: off)
    "vault_root": "~/obsidian",  # Obsidian vault root; sessions.future_file is relative to it
    "future_dir": "~/obsidian/01-llm-tasks/future",  # root of the future-job files
    "delete_dir": "~/obsidian/01-llm-tasks/delete",  # trash for deleted future jobs (restorable)
    "future_pad": "~/obsidian/01-llm-tasks/new-prompt.md",  # persistent manual capture pad
    "future_delete_grace_sec": 600,  # missing job file grace before its draft is archived
    "mirror_running": False,  # export-only markdown mirror of RUNNING sessions (INERT: off)
    "mirror_done": False,  # export-only markdown mirror of DONE sessions (INERT: off)
    "running_dir": "~/obsidian/01-llm-tasks/running",  # root of the RUNNING session mirrors
    "done_dir": "~/obsidian/01-llm-tasks/done",  # root of the DONE session mirrors
    "mirror_sessions": False,  # export-only full-conversation mirror per session (INERT: off)
    "sessions_dir": "~/obsidian/01-llm-tasks/sessions",  # root of the full-session mirrors
    "vault_name": "",  # Obsidian vault name for obsidian:// URIs ("" = basename of vault_root)
    # Root of the category/repo tree (layout <repo_root>/<category>/<repo>). Resolution:
    # this value → $GIT_BASE env → "" (no tree: every session falls into the "others" bucket).
    "repo_root": "",
    # Optional category → colour (name or #rrggbb) overrides for the session list. Empty means
    # colours come from the tab-colour cache, with a deterministic hashed-palette fallback.
    "category_colors": {},
    # Shell template to scaffold a new repo, with {category} and {name} placeholders (run via
    # the shell). "" hides/disables the TUI "create new repo" affordance.
    "create_repo_command": "",
    # launchd agent label prefix (macOS). The periodic daemon agent uses this label; the
    # WatchPaths future-sync agent derives "<launchd_label>-future-sync".
    "launchd_label": "com.claude-command-center",
    # Persistent tmux session hosting launcher="tmux" windows (resume/start-job).
    "tmux_session": "ccc",
    # The session list groups strictly by this category order (see repo_root); within a
    # category, AIM-defined sessions sort first, then by progress.
    "folder_order": ["home", "infra", "llms", "sdsc"],
}


# Fresh-install INERT contract: every key below defaults to False so a bare `ccc`
# install spends NO LLM tokens, spawns NO external tools (gh / codex / claude -p /
# resume watcher), auto-closes NOTHING, and writes ONLY under CLAUDE_HOME until the
# user opts in via `ccc init`. `ccc init` will present these as its consent checklist
# (the LLM-token checkers — score/grade/assess/drift/summarize/autoprogress/short-aim —
# recommended ON). Keep this list and the DEFAULTS above in lockstep: each member MUST
# be False in DEFAULTS (a test enforces both the membership and the values).
INERT_DEFAULT_KEYS: tuple[str, ...] = (
    "future_files",  # no vault writes (FUTURE-job markdown mirror)
    "mirror_running",  # no vault writes (RUNNING session mirror)
    "mirror_done",  # no vault writes (DONE session mirror)
    "mirror_sessions",  # no vault writes (full-conversation mirror)
    "copilot_usage",  # no `gh` billing calls
    "claude_usage",  # no keychain read / Claude OAuth /usage fetch
    "resume_halted",  # no resume watcher / continue-script spawns
    "reap",  # never auto-close a stranger's sessions un-asked
    "short_aim",  # no codex/claude short-label generation
    "aim_score_on_set",  # no LLM AIM-score refine
    "grade_on_turn",  # no after-turn progress grader spawn
    "assess_aim_on_turn",  # no AIM-met self-assessment spawn
    "drift_check",  # no impartial drift-checker spawn
    "summarize",  # no summary / next-step LLM regeneration
    "autoprogress",  # no sub-goal auto-derive / auto-check LLM passes
    "verify_subgoals_llm",  # no per-sub-goal LLM verification (already off; kept for the contract)
)


def claude_home() -> Path:
    """Root of Claude Code's state (``~/.claude`` unless ``CLAUDE_HOME`` is set)."""
    env = os.environ.get("CLAUDE_HOME")
    return Path(env) if env else Path.home() / ".claude"


def ccc_home() -> Path:
    """Root of ccc's OWN state (``$CCC_HOME`` if set, else :func:`claude_home`).

    Deliberately distinct from :func:`claude_home`: a *work* Claude process that
    exports ``CLAUDE_HOME`` in its environment must not be able to split ccc's SQLite
    DB / config across two directories. ccc's state is anchored here instead. Defaults
    to ``claude_home()`` so today's behaviour (DB under ``~/.claude/command-center``)
    is unchanged when ``CCC_HOME`` is unset.
    """
    env = os.environ.get("CCC_HOME")
    return Path(env) if env else claude_home()


def codex_home() -> Path:
    """Root of OpenAI Codex CLI's state (``~/.codex`` unless ``CODEX_HOME`` is set)."""
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def claude_config_dirs() -> dict[str, Path]:
    """Map each configured Claude account label → its resolved config directory.

    Parses the ``claude_accounts`` config key — a ``list[str]`` of ``"label=path"``
    entries. Empty (the default) ⇒ ``{"private": claude_home()}`` (today's single
    account). Each path is ``expanduser()``-ed and ``resolve()``-d. A label must match
    ``^[a-z0-9][a-z0-9_-]*$`` (it feeds usage-cache filenames, so it may never carry a
    path separator); any malformed entry — a bad label, a missing ``=``, or an empty
    path — is SKIPPED without crashing. When nothing valid survives, falls back to the
    single-``private`` default.
    """
    return parse_claude_accounts(load_config().claude_accounts)


def parse_claude_accounts(entries: list[str]) -> dict[str, Path]:
    """Pure ``"label=path"`` parser behind :func:`claude_config_dirs`.

    Split out so callers holding an already-loaded ``Config`` (e.g. the TUI's 5 s
    render tick) can resolve the accounts without re-reading the config file.
    """
    dirs: dict[str, Path] = {}
    for entry in entries:
        label, sep, raw = entry.partition("=")
        if not sep:
            continue  # no "=" → not a "label=path" entry
        label, raw = label.strip(), raw.strip()
        if not raw or not _ACCOUNT_LABEL_RE.match(label):
            continue  # blank path or a label that could smuggle a path separator
        dirs[label] = Path(raw).expanduser().resolve()
    return dirs or {"private": claude_home()}


def claude_account_email_map() -> dict[str, str]:
    """Map each Claude account label → its hard-linked expected email (see DEFAULTS).

    Parses the ``claude_account_emails`` config key. Empty ⇒ ``{}`` (no hard link
    configured — deliberately NO ``{"private": ...}`` fallback the way
    :func:`claude_config_dirs` has one: an absent hard link means "resolve by path
    only", not "assume private").
    """
    return parse_claude_account_emails(load_config().claude_account_emails)


def parse_claude_account_emails(entries: list[str]) -> dict[str, str]:
    """Pure ``"label=email"`` parser behind :func:`claude_account_email_map`.

    Mirrors :func:`parse_claude_accounts`'s tolerance: an entry with no ``=``, a
    label failing ``_ACCOUNT_LABEL_RE``, or a value with no ``@`` is SKIPPED without
    crashing. Unlike ``claude_accounts`` there is no non-empty fallback — an empty
    result means "no hard link configured" for every label.
    """
    emails: dict[str, str] = {}
    for entry in entries:
        label, sep, raw = entry.partition("=")
        if not sep:
            continue  # no "=" → not a "label=email" entry
        label, raw = label.strip(), raw.strip()
        if not raw or "@" not in raw or not _ACCOUNT_LABEL_RE.match(label):
            continue  # blank/malformed email, or a label that could smuggle a path separator
        emails[label] = raw
    return emails


def guard_vault_path(path: Path) -> Path:
    """Fail loudly when a TEST resolves a vault path under the real ``$HOME``.

    The future/running/done roots default to the user's actual Obsidian vault.
    Under pytest every resolved root must live in a tmp dir (the autouse
    ``_isolate_vault_dirs`` conftest fixture rewrites loaded configs) — a test
    that still reaches a ``$HOME`` path would silently export fixture sessions
    into the real vault, so raise instead. No-op outside pytest.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        expanded = path.expanduser()
        if expanded.is_relative_to(Path.home()):
            raise RuntimeError(
                f"test-isolation breach: vault path {expanded} resolves under $HOME; "
                "point the config's vault dirs at a tmp_path (see tests/conftest.py)"
            )
    return path


def app_home() -> Path:
    """This tool's state directory (``$CCC_HOME/command-center``)."""
    return ccc_home() / "command-center"


def db_path() -> Path:
    """Path to the SQLite store."""
    return app_home() / "state.db"


def config_path() -> Path:
    """Path to the optional user config TOML."""
    return app_home() / "config.toml"


@dataclass
class Config:
    """User-tunable settings (see ``DEFAULTS`` for meanings)."""

    idle_timeout_min: int = 60
    kill_closes_tab: bool = False
    launcher: str = "iterm"
    launch_effort: str = "xhigh"
    stale_days: int = 7
    deadline_warn_days: int = 2
    llm_model: str = "claude-haiku-4-5"
    score_model: str = ""
    score_backends: list[str] = field(default_factory=lambda: ["claude"])
    score_custom_command: str = ""
    llm_custom_command: str = ""
    gemini_model: str = ""
    notify: list[str] = field(default_factory=lambda: ["auto"])
    statusline_enabled: bool = True
    reap: bool = False
    summarize: bool = False
    max_summaries_per_run: int = 3
    autoprogress: bool = False
    max_autoprogress_per_run: int = 3
    grade_on_turn: bool = False
    grade_debounce_sec: int = 30
    assess_aim_on_turn: bool = False
    assess_aim_model: str = ""
    max_aim_assess_per_run: int = 3
    aim_score_threshold: int = 50
    aim_score_on_set: bool = False
    verify_subgoals_llm: bool = False
    sharpen_every_n_turns: int = 1
    adapt_subgoals_on_aim_change: bool = True
    drift_check: bool = False
    drift_model: str = ""
    short_aim: bool = False
    short_aim_backend: str = "auto"
    short_aim_model: str = ""
    usage_refresh_sec: float = 5.0
    copilot_usage: bool = False
    copilot_usage_refresh_sec: int = 900
    copilot_usage_refresh_active_sec: int = 300
    copilot_model: str = "gpt-5.4"
    copilot_card_title: str = "Copilot"
    copilot_credit_quota: int = 3000  # promo value; GitHub's documented baseline is 1900
    claude_usage: bool = False  # fetch the Claude /usage OAuth endpoint (INERT: off)
    claude_usage_refresh_sec: int = 600
    claude_usage_refresh_active_sec: int = 200
    claude_accounts: list[str] = field(default_factory=list)  # "label=path" per Claude account
    claude_account_emails: list[str] = field(default_factory=list)  # "label=email" hard link
    job_account: str = ""  # "" = default account, a label = pin, "auto" = burn-rate routing
    usage_card_private: bool = True
    usage_card_work: bool = True
    usage_card_codex: bool = True
    usage_card_copilot: bool = True  # render gate (copilot_usage stays the fetch gate)
    nixos_overseer_dir: str = ""  # external overseer root ("" = feature off)
    card_nixos_overseer_supervised: bool = True
    card_nixos_overseer_tier_a: bool = False
    llm_account: str = "private"
    prune_headless: bool = True
    sync_tab_titles: bool = True
    daemon_interval_sec: int = 300
    resume_halted: bool = False
    resume_stagger_sec: int = 120
    resume_poll_sec: int = 30
    resume_max_attempts: int = 3
    resume_launch_timeout_sec: int = 900
    resume_continue_script: str = ""
    nag_every_n_turns: int = 1
    nudge_unchecked_every_n_turns: int = 4
    file_lock_enabled: bool = True
    file_lock_ttl_sec: int = 1800
    file_lock_wait_sec: int = 0
    split_ratio: float = 0.6
    tab_title: str = "!!!"
    tab_color: str = "red"
    done_max_age_days: int = 3
    future_files: bool = False
    vault_root: str = "~/obsidian"
    future_dir: str = "~/obsidian/01-llm-tasks/future"
    delete_dir: str = "~/obsidian/01-llm-tasks/delete"
    future_pad: str = "~/obsidian/01-llm-tasks/new-prompt.md"
    future_delete_grace_sec: int = 600
    mirror_running: bool = False
    mirror_done: bool = False
    running_dir: str = "~/obsidian/01-llm-tasks/running"
    done_dir: str = "~/obsidian/01-llm-tasks/done"
    mirror_sessions: bool = False
    sessions_dir: str = "~/obsidian/01-llm-tasks/sessions"
    vault_name: str = ""
    repo_root: str = ""
    category_colors: dict[str, str] = field(default_factory=dict)
    create_repo_command: str = ""
    launchd_label: str = "com.claude-command-center"
    tmux_session: str = "ccc"
    folder_order: list[str] = field(default_factory=lambda: ["home", "infra", "llms", "sdsc"])
    # Fail-closed sentinel — DELIBERATELY absent from ``DEFAULTS`` so it is never
    # serialized (``save_config`` iterates ``DEFAULTS`` keys). ``load_config`` sets it
    # False when an EXISTING config.toml failed to read/parse and we fell back to pure
    # defaults; ``save_config`` then refuses to overwrite that file (see both docstrings).
    loaded_from_disk: bool = True


def load_config() -> Config:
    """Load config from TOML, layered over ``DEFAULTS``.

    Fail-closed guard against the "failed load → save defaults" clobber that once
    silently wiped a real config: when an EXISTING config.toml cannot be read or parsed
    (OSError / ``tomllib.TOMLDecodeError``) we still return the DEFAULTS-populated Config
    as before, but flag it ``loaded_from_disk=False``. :func:`save_config` refuses to
    persist such a Config over the existing file. A MISSING file (fresh install) keeps
    ``loaded_from_disk=True`` — saving defaults there is expected and safe.
    """
    data: dict[str, object] = dict(DEFAULTS)
    path = config_path()
    loaded_from_disk = True
    if path.exists():
        try:
            with path.open("rb") as handle:
                data.update(tomllib.load(handle))
        except (OSError, tomllib.TOMLDecodeError):
            loaded_from_disk = False
    cfg = Config(**{key: data[key] for key in DEFAULTS if key in data})  # type: ignore[arg-type]
    cfg.loaded_from_disk = loaded_from_disk
    return cfg


# The escapes TOML requires inside a basic ("…") string, per the spec.
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_str(value: str) -> str:
    """Render *value* as a valid TOML basic string, escaping quotes/backslashes/controls.

    Every string ``save_config`` emits must go through here. Naive ``f'"{value}"'``
    interpolation once bricked a real config: an ``llm_custom_command`` containing an
    inner ``"`` (``... ${CCC_LLM_PURPOSE:+-p "$CCC_LLM_PURPOSE"} ...``) terminated the
    basic string early, so ``tomllib`` raised ``TOMLDecodeError``, ``load_config`` fell
    back to pure DEFAULTS and the whole multi-account feature set silently switched off.
    """
    out: list[str] = []
    for ch in value:
        esc = _TOML_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def save_config(cfg: Config) -> None:  # pylint: disable=too-many-branches
    """Write the config back to the TOML file (flat key = value).

    Fail-closed guard: if config.toml already exists but *cfg* is flagged
    ``loaded_from_disk=False`` (its :func:`load_config` hit an OSError /
    TOMLDecodeError and fell back to pure DEFAULTS), raise ``RuntimeError`` instead of
    overwriting the file with those defaults — the exact path that once wiped a real
    config. The user must fix or delete the unparsable file by hand first.

    The write is atomic and keeps a one-deep backup: before replacing an existing,
    non-empty file whose content differs from the new text, the current bytes are copied
    to ``config.toml.bak`` (same directory); the new text is written to a unique
    ``tempfile.mkstemp`` file in that directory and swapped in with ``os.replace``, so a
    concurrent reader never observes a truncated/empty file. There is no lock —
    concurrent whole-file saves are last-wins; the atomicity only guarantees no partial
    read.

    Every emitted string — scalar, list item, dict key AND dict value — goes through
    :func:`_toml_str`, so a value carrying a quote or backslash stays parsable TOML
    instead of poisoning the file into the fail-closed path above.
    """
    path = config_path()
    if path.exists() and cfg.loaded_from_disk is False:
        raise RuntimeError(
            "refusing to overwrite existing config.toml: it could not be parsed when "
            "loaded; fix it by hand or delete it first"
        )
    app_home().mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key in DEFAULTS:
        value = getattr(cfg, key)
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}")
        elif isinstance(value, float):
            lines.append(f"{key} = {value}")
        elif isinstance(value, list):
            items = ", ".join(_toml_str(str(item)) for item in value)
            lines.append(f"{key} = [{items}]")
        elif isinstance(value, dict):
            if value:
                items = ", ".join(
                    f"{_toml_str(str(k))} = {_toml_str(str(v))}" for k, v in value.items()
                )
                lines.append(f"{key} = {{ {items} }}")
            else:
                lines.append(f"{key} = {{}}")
        else:
            lines.append(f"{key} = {_toml_str(str(value))}")
    text = "\n".join(lines) + "\n"

    # One-deep backup: preserve the prior content before an overwrite that changes it.
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError:
            current = b""
        if current and current != text.encode("utf-8"):
            with contextlib.suppress(OSError):
                path.with_name(path.name + ".bak").write_bytes(current)

    # Atomic replace: write to a unique temp in the same dir, then os.replace into place.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
