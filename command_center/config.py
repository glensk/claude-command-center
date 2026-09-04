#!/usr/bin/env python3
"""Paths and user-tunable configuration.

Paths are resolved at call time (reading ``CLAUDE_HOME`` from the environment)
so tests can point the whole tool at a temporary directory. User tunables live
in ``~/.claude/command-center/config.toml`` and fall back to ``DEFAULTS``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import contextlib
import copy
import datetime
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
    # Which AIM revision the narrow /aim column (TUI + `ccc ls`) renders: "first" = the
    # done-condition as ORIGINALLY typed (revision (1)) so the column is a stable job
    # identity while the AIM is sharpened; "latest" = the current AIM. Either way the
    # current AIM stays in the status line, the detail pane and `ccc aim-history`.
    "aim_column": "first",
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
    # chosen budget: 1,900 credits/user/mo, the documented Copilot Business
    # per-user baseline. Set it to whatever your seat is actually allotted.
    "copilot_credit_quota": 1900,
    # Claude /usage OAuth fetch: keep each account's usage card in step with `claude`'s
    # own /usage (incl. any weekly model-scoped window the status line never carries) by
    # fetching the OAuth usage endpoint out-of-band (reads the CLI's keychain token).
    # RENDER stays gated by usage_card_private/_work; this gates only the FETCH.
    "claude_usage": False,  # fetch the Claude /usage OAuth endpoint (INERT: off)
    "claude_usage_refresh_sec": 600,  # min sec between idle OAuth usage fetches per account
    # While any job is WORKING/SNOOZED the fetch throttle drops to this shorter "active"
    # interval so the cards track reality more closely; 0 or ≥ idle disables the speed-up.
    "claude_usage_refresh_active_sec": 200,  # active-work OAuth usage throttle (~1/3 of idle)
    # Live OpenAI Codex usage: fetch ChatGPT's own usage endpoint (the numbers its
    # Settings -> Usage page shows) with the token in `$CODEX_HOME/auth.json`, instead of
    # only replaying the rate_limits blocks Codex leaves in its rollout files (which are
    # as old as the last Codex turn). RENDER stays gated by usage_card_codex/_codex_private.
    "codex_usage": False,  # fetch the live chatgpt.com Codex usage endpoint (INERT: off)
    "codex_usage_refresh_sec": 600,  # min sec between idle live Codex usage fetches per home
    # While any job is WORKING/SNOOZED the fetch throttle drops to this shorter "active"
    # interval so the cards track reality more closely; 0 or >= idle disables the speed-up.
    "codex_usage_refresh_active_sec": 200,  # active-work Codex usage throttle (~1/3 of idle)
    # A SECOND CODEX_HOME holding another ChatGPT login, e.g. "~/.codex-private" (create
    # it with `CODEX_HOME=~/.codex-private codex login`). Empty (the default) => no second
    # Codex card at all: it is absent, not merely collapsed.
    "codex_home_private": "",
    # THIRD and further ChatGPT logins, one ``"label=path"`` entry each (same shape as
    # ``claude_accounts``), e.g. ``["de=~/.codex-de"]``. Each entry adds its own green
    # usage card (chords t6..t8, in config order) and its own ``codex:<label>`` quota row,
    # so an account pin on that home is honoured. Labels are validated
    # ``^[a-z0-9][a-z0-9_-]*$`` and may not re-use the fixed ``default``/``private``.
    "codex_homes_extra": [],
    # The ORDER every Codex consumer tries the seats in: labels of
    # ``quota._canonical_codex_homes`` (``default`` / ``private`` / a
    # ``codex_homes_extra`` label), e.g. ``["private", "de", "default"]``. Empty (the
    # default) = the canonical order default -> private -> extras. Unknown labels are
    # ignored; a configured seat missing from the list is appended in canonical order.
    # A non-empty order makes the ``codex-in-claude home`` account pin INERT for
    # selection — an explicit order is the stronger statement.
    "codex_seat_order": [],
    # Multi-account Claude Code. ``claude_accounts`` maps labels to config dirs, one
    # ``"label=path"`` entry per line (list[str] so save_config round-trips it). Empty
    # (the default) ⇒ a single ``{"private": claude_home()}`` account, i.e. today's
    # behaviour. Labels are validated ``^[a-z0-9][a-z0-9_-]*$``.
    "claude_accounts": [],
    # Identity hard-link for the usage cards: ``"label=email"`` entries, same shape as
    # ``claude_accounts``. WHICH config dir a label points at is a path; WHICH Claude
    # account is actually logged into that dir can drift (a bare `/login` in the wrong
    # shell silently swaps it) — this pins the "Claude (work)"/"(private)" cards to
    # an ACTUAL email address instead, so the right numbers show under the right card
    # even after such a drift. Empty (the default) ⇒ no hard link, today's pure
    # path-based behaviour (see ``accounts.resolve_card_label``).
    "claude_account_emails": [],
    # When each paid subscription renews, so a card can advertise its own cancel-by date:
    # ``"card=YYYY-MM-DD"`` entries over the four cards in ``SUBSCRIPTION_CARDS``, e.g.
    # ``["claude_private=auto", "codex_private=2026-09-30"]``. The date is appended to
    # that card's border title as `` -> 30.9`` (Swiss D.M; a ``!`` marks one already
    # past). ``auto`` derives it instead of pinning it — from the billing anniversary in
    # Claude's OAuth profile, or from the ChatGPT id_token's subscription claim. Empty
    # (the default) ⇒ no card carries a date and no profile endpoint is ever called.
    "subscription_ends": [],
    # Which account a NEW job (no explicit -A / account select) bills to: "" = default
    # account, a label = pin, "auto" = saturate-earliest-reset routing (see routing.py).
    "job_account": "",
    # The card render gates (t1..t4 / to / ta). False does NOT remove a card: it
    # collapses it to its titled top border, which is where its own chord is named.
    "usage_card_private": True,  # expand the Claude (private) usage card
    "usage_card_work": True,  # expand the Claude (work) usage card
    "usage_card_codex": True,  # expand the Codex usage card
    "usage_card_codex_private": True,  # expand the SECOND Codex card (codex_home_private)
    # Labels of the ``codex_homes_extra`` cards that start COLLAPSED. Inverted relative to
    # the booleans above (which are True = expanded) because the extra cards are dynamic:
    # an unlisted label is expanded, so a newly added login needs no second key.
    "usage_card_codex_extra_collapsed": [],
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
    # Mirror scrubber — every byte the three export-only mirror roots receive passes through
    # it first (docs/reference.md § mirrors). First token = the scrubber executable, resolved
    # through the external-deps registry ($SECRET_BROKER_CLIENT → $PATH; a token containing
    # "/" is used verbatim and must exist + be executable); the rest are its arguments. The
    # document goes in on stdin, the VOUCHED document comes back on stdout; exit 0 = vouched,
    # anything else = that write is WITHHELD (previous file kept). "" = no scrubber = every
    # mirror write withheld — fail closed.
    "mirror_scrub_cmd": "secret-broker-client.py scrub --shapes",
    # The ONLY passthrough: write mirrors without a scrubber verdict. `ccc doctor` FAILs while
    # this is on together with any mirror switch. Not an inert-defaults key: False is the safe
    # value, True is the deliberate opt-OUT of the protection.
    "mirror_allow_unscrubbed": False,
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
    # `ccc restore-snapshot`: which programs a non-Claude pane may RE-RUN from its
    # captured argv. A pane whose argv[0] basename is listed here is relaunched with the
    # exact argv `ccc snapshot` read out of the kernel (re-quoted per element); anything
    # else restores as a plain shell at the pane's cwd that merely PRINTS what used to
    # run there. Nothing here runs without an explicit `ccc restore-snapshot`, so this is
    # not an inert-defaults key — it only narrows what that command may relaunch.
    "snapshot_restore_commands": [
        "vi",
        "vim",
        "nvim",
        "less",
        "man",
        "tail",
        "htop",
        "btop",
        "top",
        "ccc",
        "ssh",
    ],
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
    "codex_usage",  # no Codex auth.json read / chatgpt.com usage fetch
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


def codex_home_private() -> Path | None:
    """The optional SECOND ``CODEX_HOME`` (a different ChatGPT login), or ``None``.

    Configured as ``codex_home_private`` (e.g. ``"~/.codex-private"``, created by
    ``CODEX_HOME=~/.codex-private codex login``). Empty — the default — means the
    feature is off: no second card is drawn at all, exactly like the Claude work card
    on a machine with no ``work`` account.
    """
    raw = load_config().codex_home_private.strip()
    return Path(raw).expanduser() if raw else None


# The labels the two built-in Codex homes own. A ``codex_homes_extra`` entry may not
# re-use one: it would silently shadow the seat every other module names by that label.
RESERVED_CODEX_LABELS = ("default", "private")


def codex_homes_extra() -> dict[str, Path]:
    """Label → ``CODEX_HOME`` for every THIRD-and-further ChatGPT login, in config order.

    Parses the ``codex_homes_extra`` config key — a ``list[str]`` of ``"label=path"``
    entries. Empty (the default) ⇒ ``{}``: only the two built-in homes exist, which is
    today's behaviour.
    """
    return parse_codex_homes_extra(load_config().codex_homes_extra)


def parse_codex_homes_extra(entries: list[str]) -> dict[str, Path]:
    """Pure ``"label=path"`` parser behind :func:`codex_homes_extra`.

    Split out so callers holding an already-loaded ``Config`` (the TUI's render tick)
    can resolve the extra homes without re-reading the config file. Mirrors
    :func:`parse_claude_accounts`'s tolerance — an entry with no ``=``, a blank path, a
    label failing ``_ACCOUNT_LABEL_RE``, or a label in :data:`RESERVED_CODEX_LABELS` is
    SKIPPED without crashing, and a repeated label keeps its FIRST entry. The path is
    ``expanduser()``-ed but deliberately NOT ``resolve()``-d, exactly like
    :func:`codex_home_private` (``quota._canonical_codex_homes`` resolves where it needs
    identity).
    """
    homes: dict[str, Path] = {}
    for entry in entries:
        label, sep, raw = entry.partition("=")
        if not sep:
            continue  # no "=" → not a "label=path" entry
        label, raw = label.strip(), raw.strip()
        if not raw or not _ACCOUNT_LABEL_RE.match(label):
            continue  # blank path or a label that could smuggle a path separator
        if label in RESERVED_CODEX_LABELS or label in homes:
            continue  # never shadow a built-in seat; a duplicate keeps the first entry
        homes[label] = Path(raw).expanduser()
    return homes


def codex_seat_order() -> list[str]:
    """The configured Codex seat order, raw — stripped, deduped, strings only.

    The labels of :func:`command_center.quota._canonical_codex_homes` in the order every
    Codex consumer should TRY them (``["private", "de", "default"]``). Empty — the
    default — means the canonical order (``default`` -> ``private`` -> extras).

    Deliberately unvalidated here: ``quota.resolve_seat_order`` decides which of these
    labels exist on this machine (and reports the rest as ``unknown``), so a login that
    is temporarily unconfigured never turns the whole order into a hard error.
    """
    seen: list[str] = []
    for raw in load_config().codex_seat_order:
        label = str(raw).strip()
        if label and label not in seen:
            seen.append(label)
    return seen


def unknown_config_keys() -> list[str]:
    """Keys in the on-disk ``config.toml`` that :data:`DEFAULTS` does not know, sorted.

    :func:`save_config` re-emits ONLY ``DEFAULTS`` keys, so any other key in the file —
    a hand-added setting, a typo, a key from a newer ccc — is silently DROPPED by a
    save. A writer that rewrites the config on the user's behalf checks this first and
    refuses rather than deleting something it never read. ``[]`` when the file is
    missing or unparsable (nothing would be lost that is not already lost).
    """
    path = config_path()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return sorted(str(key) for key in data if key not in DEFAULTS)


def codex_homes() -> dict[str, Path]:
    """Label -> ``CODEX_HOME`` for every configured Codex (ChatGPT) login.

    Always ``{"default": codex_home()}``, plus ``{"private": …}`` when
    ``codex_home_private`` is set, then one entry per ``codex_homes_extra`` login in
    config order. The labels name the account in ``ccc codex-usage -a LABEL`` output and
    key the per-home live usage caches: ``default`` and ``private`` are FIXED strings,
    while the extra labels are whatever ``codex_homes_extra`` spells them.
    """
    homes = {"default": codex_home()}
    private = codex_home_private()
    if private is not None:
        homes["private"] = private
    homes.update(codex_homes_extra())
    return homes


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


# The four FIXED usage cards a subscription-end date can be pinned to. These are CARD
# keys, not account labels: the two Claude cards are keyed by the account label they
# render (see SUBSCRIPTION_CARD_ACCOUNTS), the two Codex ones by which CODEX_HOME they
# read. A ``codex_<label>`` key is accepted on top of these four — one per
# ``codex_homes_extra`` card, whose labels are only known at runtime (see
# :func:`is_subscription_card`).
SUBSCRIPTION_CARDS = ("claude_private", "claude_work", "codex", "codex_private")
# The Claude subscription cards → the account label whose OAuth profile carries their
# billing anniversary. The two Codex cards have no entry: their date comes from a
# CODEX_HOME's id_token, not from an account label.
SUBSCRIPTION_CARD_ACCOUNTS = {"claude_private": "private", "claude_work": "work"}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def subscription_end_map() -> dict[str, str]:
    """Map each usage card → its configured subscription end (``"YYYY-MM-DD"``/``"auto"``).

    Parses the ``subscription_ends`` config key. Empty ⇒ ``{}``: no card advertises a
    renewal date, which is also the fresh-install default (an ``auto`` entry is what
    authorizes the extra profile fetch, so an unconfigured install makes no such call).
    """
    return parse_subscription_ends(load_config().subscription_ends)


def is_subscription_card(card: str) -> bool:
    """True for a card key a subscription-end date may be pinned to.

    The four fixed :data:`SUBSCRIPTION_CARDS`, plus ``codex_<label>`` for any extra
    Codex card — its label comes from ``codex_homes_extra``, so it cannot be enumerated
    here; the same ``_ACCOUNT_LABEL_RE`` that gates the label there gates it here, which
    keeps a typo (``codex_De``, ``codex_``) rejected exactly as before.
    """
    if card in SUBSCRIPTION_CARDS:
        return True
    if not card.startswith("codex_"):
        return False
    return _ACCOUNT_LABEL_RE.match(card[len("codex_") :]) is not None


def parse_subscription_ends(entries: list[str]) -> dict[str, str]:
    """Pure ``"card=YYYY-MM-DD"`` / ``"card=auto"`` parser behind :func:`subscription_end_map`.

    Mirrors :func:`parse_claude_account_emails`'s tolerance — an entry with no ``=``, a
    card :func:`is_subscription_card` rejects, or a value that is neither ``auto`` nor a
    REAL ISO-8601 date (``2026-02-30`` is rejected, not just mis-shaped strings) is
    SKIPPED without crashing, so one typo in the config never blanks the other cards.
    """
    ends: dict[str, str] = {}
    for entry in entries:
        card, sep, raw = entry.partition("=")
        if not sep:
            continue  # no "=" → not a "card=value" entry
        card, raw = card.strip(), raw.strip()
        if not is_subscription_card(card):
            continue
        if raw == "auto":
            ends[card] = raw
            continue
        if not _ISO_DATE_RE.match(raw):
            continue
        try:
            datetime.date.fromisoformat(raw)
        except ValueError:
            continue  # well-shaped but not a real day (2026-02-30)
        ends[card] = raw
    return ends


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
    aim_column: str = "first"  # /aim column revision: "first" (revision (1)) | "latest"
    short_aim: bool = False
    short_aim_backend: str = "auto"
    short_aim_model: str = ""
    usage_refresh_sec: float = 5.0
    copilot_usage: bool = False
    copilot_usage_refresh_sec: int = 900
    copilot_usage_refresh_active_sec: int = 300
    copilot_model: str = "gpt-5.4"
    copilot_card_title: str = "Copilot"
    # Fallback only: the seat's real AI-Credit entitlement is read live from
    # `/copilot_internal/user` (usage._fetch_copilot_quota) and wins whenever it answers.
    # This guess is used solely when that endpoint is unreachable; 1,900 is the
    # documented Copilot Business per-user baseline.
    copilot_credit_quota: int = 1900
    claude_usage: bool = False  # fetch the Claude /usage OAuth endpoint (INERT: off)
    claude_usage_refresh_sec: int = 600
    claude_usage_refresh_active_sec: int = 200
    codex_usage: bool = False  # fetch the live chatgpt.com Codex usage endpoint (INERT: off)
    codex_usage_refresh_sec: int = 600
    codex_usage_refresh_active_sec: int = 200
    codex_home_private: str = ""  # second CODEX_HOME ("" = no second Codex card)
    codex_homes_extra: list[str] = field(default_factory=list)  # "label=path" per extra login
    codex_seat_order: list[str] = field(default_factory=list)  # seat labels, "" = canonical order
    claude_accounts: list[str] = field(default_factory=list)  # "label=path" per Claude account
    claude_account_emails: list[str] = field(default_factory=list)  # "label=email" hard link
    subscription_ends: list[str] = field(default_factory=list)  # "card=YYYY-MM-DD|auto"
    job_account: str = ""  # "" = default account, a label = pin, "auto" = burn-rate routing
    usage_card_private: bool = True
    usage_card_work: bool = True
    usage_card_codex: bool = True
    usage_card_codex_private: bool = True  # render gate for the second Codex card
    # Labels of the codex_homes_extra cards that are COLLAPSED (unlisted = expanded).
    usage_card_codex_extra_collapsed: list[str] = field(default_factory=list)
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
    mirror_scrub_cmd: str = "secret-broker-client.py scrub --shapes"
    mirror_allow_unscrubbed: bool = False
    vault_name: str = ""
    repo_root: str = ""
    category_colors: dict[str, str] = field(default_factory=dict)
    create_repo_command: str = ""
    launchd_label: str = "com.claude-command-center"
    tmux_session: str = "ccc"
    folder_order: list[str] = field(default_factory=lambda: ["home", "infra", "llms", "sdsc"])
    # Programs `ccc restore-snapshot` may re-run from a captured pane's exact argv.
    snapshot_restore_commands: list[str] = field(
        default_factory=lambda: [
            "vi",
            "vim",
            "nvim",
            "less",
            "man",
            "tail",
            "htop",
            "btop",
            "top",
            "ccc",
            "ssh",
        ]
    )
    # Fail-closed sentinel — DELIBERATELY absent from ``DEFAULTS`` so it is never
    # serialized (``save_config`` iterates ``DEFAULTS`` keys). ``load_config`` sets it
    # False when an EXISTING config.toml failed to read/parse and we fell back to pure
    # defaults; ``save_config`` then refuses to overwrite that file (see both docstrings).
    loaded_from_disk: bool = True


# One-entry memo for :func:`load_config`, keyed on the config file's identity
# ``(path, st_mtime_ns, st_size)``. See the load_config docstring for the contract.
_CONFIG_MEMO: tuple[tuple[str, int, int], Config] | None = None


def _config_key(path: Path) -> tuple[str, int, int]:
    """Identity of the config file: ``(path, mtime_ns, size)``, ``-1``/``-1`` when missing.

    A missing file is a real, cacheable state (a fresh install runs on pure DEFAULTS),
    and the sentinel differs from every stat of a real file, so the memo drops itself the
    moment the file appears.
    """
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def invalidate_config_cache() -> None:
    """Drop the :func:`load_config` memo — call after writing ``config.toml``.

    ``st_mtime_ns`` has real granularity, so a save landing within the same tick as the
    previous load would otherwise re-serve the pre-save Config. :func:`save_config` calls
    this itself; a test (or any other writer that bypasses ``save_config``) must too.
    """
    global _CONFIG_MEMO  # pylint: disable=global-statement
    _CONFIG_MEMO = None


def load_config() -> Config:
    """Load config from TOML, layered over ``DEFAULTS`` (memoized on the file's identity).

    Fail-closed guard against the "failed load → save defaults" clobber that once
    silently wiped a real config: when an EXISTING config.toml cannot be read or parsed
    (OSError / ``tomllib.TOMLDecodeError``) we still return the DEFAULTS-populated Config
    as before, but flag it ``loaded_from_disk=False``. :func:`save_config` refuses to
    persist such a Config over the existing file. A MISSING file (fresh install) keeps
    ``loaded_from_disk=True`` — saving defaults there is expected and safe.

    **Why the memo:** this is a hot path, not a startup-only read. Painting one TUI/``ccc
    ls`` frame of 629 rows re-entered it 629 times (``tabsymbol.cell_for`` →
    ``symbol_for_repo`` → ``colors.short_folder`` → ``repos.repo_root``), re-parsing the
    same TOML every time — 5.2 s of pure parsing under load. The memo is keyed on
    ``(path, st_mtime_ns, st_size)``, so an edit by any process (or by ``ccc`` itself) is
    picked up on the next call without a watcher; ``st_size`` catches the rare same-tick
    rewrite that keeps the mtime.

    **Deep-copy contract:** every call returns a Config the caller OWNS. The TUI's
    toggles mutate ``self.cfg`` in place and the test suite rewrites vault paths on the
    loaded object, so handing out the cached instance would poison every later reader.

    A failed parse is NEVER cached (``loaded_from_disk=False``): a repaired file must
    take effect on the very next call, whatever its stat says.
    """
    global _CONFIG_MEMO  # pylint: disable=global-statement
    path = config_path()
    memo_key = _config_key(path)
    memo = _CONFIG_MEMO
    if memo is not None and memo[0] == memo_key:
        return copy.deepcopy(memo[1])
    data: dict[str, object] = dict(DEFAULTS)
    loaded_from_disk = True
    if path.exists():
        try:
            with path.open("rb") as handle:
                data.update(tomllib.load(handle))
        except (OSError, tomllib.TOMLDecodeError):
            loaded_from_disk = False
    cfg = Config(**{key: data[key] for key in DEFAULTS if key in data})  # type: ignore[arg-type]
    cfg.loaded_from_disk = loaded_from_disk
    if loaded_from_disk:
        _CONFIG_MEMO = (memo_key, copy.deepcopy(cfg))
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

    The :func:`load_config` memo is dropped right after the write: the stat key alone
    cannot be trusted here, because a save landing in the same ``st_mtime_ns`` tick as
    the load that preceded it would keep serving the pre-save Config.
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
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
        replaced = True
    finally:
        # `except OSError` leaked the temp on any other exception; only unlink when the
        # replace did NOT happen, or we would delete the file we just wrote.
        if not replaced:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    invalidate_config_cache()  # mtime granularity: never serve the pre-save Config again
