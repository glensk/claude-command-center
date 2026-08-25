"""Single source of truth for the TUI's commands & keystrokes.

Every command/keystroke is declared ONCE in :data:`COMMANDS`. The TUI derives
*all* of its key-driven surfaces from this registry, so a command can never be
added in one place and forgotten in another:

* Textual key bindings        — :func:`binding_specs`
* the bottom footer hint line  — :func:`footer_commands`
* the column-header mnemonics   — :func:`column_key`
* the help overview + the per-key "Commands & keys" explorer — :func:`sections`

To add a command: append one :class:`Command` to :data:`COMMANDS`. Give it a
``footer_pos`` if it should appear in the bottom hint line, and a ``column`` if
it labels a table column. Nothing else needs touching — the binding, footer,
header mnemonic and both help views all pick it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

# Help groupings, in display order. ``section`` on each Command must be one of these.
NAVIGATE = "Navigate"
PER_SESSION = "Per-session"
GLOBAL = "Global"
SECTION_ORDER: list[str] = [NAVIGATE, PER_SESSION, GLOBAL]

# Optional parenthetical shown after a section title in the help overview.
SECTION_NOTE: dict[str, str] = {PER_SESSION: "(acts on the highlighted session)"}


@dataclass(frozen=True)
class Command:  # pylint: disable=too-many-instance-attributes  # a command legitimately has many facets
    """One TUI command. ``key``/``word``/``gloss``/``section`` are always shown;
    the rest tune where and how the command surfaces."""

    key: str  # keystroke as the user types it: "r", "c", "tf", "space", "!", "↑/↓"
    word: str  # display word, shortcut highlighted in gold: "/aim", "resume", "Keep"
    gloss: str  # one-line summary (footer-style rows + help overview)
    section: str  # help grouping; one of SECTION_ORDER
    explanation: str = ""  # long help (the "Commands & keys" explorer)
    action: str | None = None  # App.action_<action>; None for nav-only doc entries
    bind: str | None = None  # Textual binding key; None → not a plain binding (chord / nav)
    aliases: tuple[str, ...] = ()  # extra binding keys → same action (e.g. "x" → close)
    chord: tuple[str, str] | None = None  # leader+follower for a two-key chord (e.g. tf)
    footer_pos: float | None = None  # sort position in the bottom footer; None → not shown there
    footer_word: str | None = None  # footer label override (else `word`) — e.g. `tf` shows "toggle"
    footer_key: str | None = (
        None  # footer mnemonic override (else `key`) — e.g. `tf` gilds just "t"
    )
    column: str | None = None  # table-header word this command labels with its gold key


# The registry. Ordered for the help views (grouped by section, narrative order
# within each); the footer renders in its own order via ``footer_pos``.
COMMANDS: list[Command] = [
    # ---- Navigate ----
    Command(
        key="↑/↓",
        word="select",
        gloss="move the row highlight",
        section=NAVIGATE,
        explanation=(
            "Move the whole-row highlight up and down the session list. The "
            "highlighted session is the target of every per-session key (r, c, d, …). "
            "In this help and in the Commands & keys list, ↑/↓ move the selection and "
            "Enter opens it."
        ),
    ),
    Command(
        key="←/→",
        word="columns",
        gloss="edit a field inline",
        section=NAVIGATE,
        explanation=(
            "On the selected session, ← and → move a single-cell cursor across the "
            "editable columns — /aim → /next-step, wrapping at either end. Press Enter "
            "to edit the highlighted field; ↑/↓ (or Esc) snap back to whole-row "
            "selection. The word-back / word-forward keys (Option+←/→, i.e. ctrl+←/→) "
            "jump three rows down / up, wrapping around at the top/bottom. /deadline and "
            "/block are no longer columns — set them with the D / b keys directly."
        ),
    ),
    Command(
        key="q",
        word="quit",
        gloss="leave ccc",
        section=NAVIGATE,
        explanation=(
            "Quit the command center TUI. Your Claude Code sessions keep running — ccc "
            "only stops watching them. Reopen any time with `ccc`."
        ),
        action="quit",
        bind="q",
        footer_pos=14,
    ),
    # ---- Per-session (acts on the highlighted session) ----
    Command(
        key="r",
        word="resume",
        gloss="resume / open tab",
        section=PER_SESSION,
        explanation=(
            "Resume or focus the session. If it is LIVE (process running) r focuses its "
            "existing tab — a running session cannot be re-resumed (Claude Code errors), "
            "so r just brings it forward. If it is PARKED (process gone) r resumes it in "
            "a new tab — unless it has no recorded conversation on disk (it never had a "
            "turn, or its transcript was deleted), which can't be resumed: r then offers "
            "to restore the dead row to FUTURE (so it can be re-run), delete it, or keep "
            "it — instead of opening a doomed tab."
            " Enter on the highlighted row does the same (resume / switch to its tab)."
        ),
        action="resume",
        bind="r",
        footer_pos=7,
    ),
    Command(
        key="c",
        word="close",
        gloss="close / park",
        section=PER_SESSION,
        explanation=(
            "Park the session: send SIGTERM to its process and move it to PARKED. On a "
            "LIVE session it also closes the iTerm pane — and the whole tab if Claude "
            "was the only pane (the single process) in it. Press c or x. Nothing is "
            "lost — resume it later by id with r (or `ccc resume <id>`). Closing a "
            "session already marked done keeps it DONE (never demoted to PARKED), so "
            "it sinks to the FINISHED section."
        ),
        action="close",
        bind="c",
        aliases=("x",),
        footer_pos=2.5,  # in the footer, sit right after /done (2)
        # Footer shows "☾lose": the parked-moon glyph stands in for the gilded `c`
        # key (parking is what close does). `_gold_mnemonic` gilds the ☾ in place
        # because it is the single-char `footer_key` and occurs in `footer_word`.
        footer_word="☾lose",
        footer_key="☾",
    ),
    Command(
        key="d",
        word="/done",
        gloss="mark the AIM achieved",
        section=PER_SESSION,
        explanation=(
            "Mark the session's AIM (its done-condition) as achieved. On a LIVE session "
            "it then offers to close the tab/pane (the tab closes too if that was its "
            "only pane). Equivalent to the /done slash command inside the session."
        ),
        action="mark_done",
        bind="d",
        footer_pos=2,
    ),
    Command(
        key="K",
        word="Keep",
        gloss="exempt from auto-close",
        section=PER_SESSION,
        explanation=(
            "Toggle 'keep' on the session so the daemon's auto-close reaper never parks "
            "it for being idle. Use it for long-lived sessions you want to stay LIVE."
        ),
        action="keep",
        bind="K",
        # No footer_pos: deliberately hidden from the bottom "keys:" hint line to
        # keep it short; Keep still appears in the help menu (overview + explorer)
        # because that is driven by COMMANDS membership, not footer_pos.
    ),
    Command(
        key="a",
        word="/aim",
        gloss="set the done-condition",
        section=PER_SESSION,
        explanation=(
            "Set the session's AIM — a one-line 'done when: …' goal shown in gold. It "
            "drives d (/done), the daemon's done-checks, and auto-derived sub-goals. "
            "Same as the /aim slash command."
        ),
        action="edit_aim",
        bind="a",
        footer_pos=0,
        column="/aim",
    ),
    Command(
        key="n",
        word="/next-step",
        gloss="set the next step",
        section=PER_SESSION,
        explanation=(
            "Set or override the session's next step; @tags you type are coloured by "
            "type. Shown in the /next-step column. Same as the /next-step slash command."
        ),
        action="edit_next",
        bind="n",
        footer_pos=1,
        column="/next-step",
    ),
    Command(
        key="b",
        word="/block",
        gloss="record what it's waiting on",
        section=PER_SESSION,
        explanation=(
            "Record what the session is blocked on / waiting for; @tags are coloured "
            "(use @waiting for an 'ok to park' cue). Moves the session into the WAITING "
            "group. Same as the /block slash command. Also reachable from the e edit menu."
        ),
        action="edit_blocked",
        bind="b",
    ),
    Command(
        key="D",
        word="/Deadline",
        gloss="set a finish-by date",
        section=PER_SESSION,
        explanation=(
            "Set (or clear) a finish-by deadline in YYYY-MM-DD form. Shown in the "
            "detail pane (no longer a table column); the daemon fires an alert as it "
            "approaches. Same as the /deadline slash command. Also reachable from the e edit menu."
        ),
        action="edit_deadline",
        bind="D",
    ),
    Command(
        key="!",
        word="important",
        gloss="cycle importance",
        section=PER_SESSION,
        explanation=(
            "Cycle the session's importance marker: ! → !! → !!! → none. Higher "
            "importance sorts the session up and shows more ! marks in the leftmost "
            "(head:) column."
        ),
        action="cycle_importance",
        bind="exclamation_mark",
    ),
    Command(
        key="space",
        word="subgoal",
        gloss="tick the next sub-goal",
        section=PER_SESSION,
        explanation=(
            "Tick the next unchecked sub-goal in the session's checklist. The checked ÷ "
            "total ratio drives the progress bar (unless a manual progress % override is "
            "set via e / Enter on the progress column). The daemon can also tick "
            "auto-derived sub-goals; space only ever ticks — it never unticks. Edit the "
            "whole checklist in the e form or with `ccc subgoals`."
        ),
        action="toggle_subgoal",
        bind="space",
    ),
    Command(
        key="e",
        word="edit",
        gloss="edit all session settings",
        section=PER_SESSION,
        explanation=(
            "Pressing e edits the session's fields IN PLACE — the job-details layout does not "
            "change, the editable lines simply gain a cursor (the focused line is tinted; no "
            "boxes, no popup). ↑/↓ move between the one-line fields (next-step / deadline / "
            "progress % / block); the AIM, the sub-goal checklist (one item per line — "
            "add/delete lines, ticks carry over by text; a manual edit labels the list "
            "'manual') and the future-job prompt are borderless multi-line fields that "
            "grow to fit (Tab leaves them). progress % overrides the sub-goal-derived bar "
            "(blank = auto; cleared on mark-done) — also settable with Enter on the progress "
            "column. Type to edit, Esc saves all and returns to the "
            "table — clearing the AIM warns first (it is never lost: every AIM stays in "
            "aim-history). Future jobs also edit the folder/repo via the picker. The same "
            "fields are also editable directly with a / n / D / b."
        ),
        action="edit_session",
        bind="e",
        footer_pos=3,  # where /Deadline used to sit: after close (2.5), before resume (7)
    ),
    Command(
        key="ah",
        word="aim-history",
        gloss="show the AIM's progression",
        section=PER_SESSION,
        explanation=(
            "Show this session's AIM history — every (re)definition from the first to the "
            "current, with each revision's specificity score, so you can see how the goal got "
            "sharper. Type a then h (a bare a still edits the AIM). Same as the /aim-history "
            "slash command and `ccc aim-history`."
        ),
        action="aim_history",
        chord=("a", "h"),
        footer_pos=13,
    ),
    Command(
        key="sh",
        word="subgoal-history",
        gloss="show the sub-goals' evolution + drift",
        section=PER_SESSION,
        explanation=(
            "Show this session's sub-goal history — every version of the checklist with its "
            "trigger, the AIM revision it tracked, and the impartial drift checker's verdict "
            "(a blue ● means it flagged drift). Type s then h (a bare s still opens settings). "
            "Same as the /subgoal-history slash command and `ccc subgoal-history`."
        ),
        action="subgoal_history",
        chord=("s", "h"),
        footer_pos=13.5,  # right after aim-history (13)
    ),
    Command(
        key="oo",
        word="open-obsidian",
        gloss="open the job's file in Obsidian",
        section=PER_SESSION,
        explanation=(
            "Open the selected job's markdown file in Obsidian — the human editing surface "
            "for a future job (see the Future jobs section). Type o then o. Only works once "
            "the draft has synced to a file (its id-column hash is a clickable link the "
            "instant that happens); until then there is nothing to open and oo just notifies."
        ),
        action="open_obsidian",
        chord=("o", "o"),
        footer_pos=13.75,  # right after subgoal-history (13.5), before quit (14)
    ),
    Command(
        key="os",
        word="open-session",
        gloss="open the full-session file in Obsidian",
        section=PER_SESSION,
        explanation=(
            "Open the selected session's FULL-CONVERSATION markdown mirror in Obsidian — "
            "everything you typed, every Claude reply and the tool calls between, "
            "terminal-like (the same content the peek panel's session tab shows). Type o "
            "then s. Works for parked, running and done sessions once the mirror pass has "
            "written the file (sessions_dir, synced by the daemon and every lifecycle "
            "command); until then os just notifies. The running/done mirror files link to "
            "the same file from their Transcript section."
        ),
        action="open_session_obsidian",
        chord=("o", "s"),
        footer_pos=13.8,  # right after open-obsidian (13.75), before quit (14)
    ),
    Command(
        key="sp",
        word="session-peek",
        gloss="peek the row's prompts / session / aim",
        section=PER_SESSION,
        explanation=(
            "Open the peek panel for the selected session — the SAME floating panel the "
            "global s+p Karabiner chord shows in a session tab, but driven from the TUI "
            "cursor so it always targets the highlighted row (no tty/uuid detection). "
            "Three tabs: prompts (every human prompt) · session (the full conversation, "
            "terminal-like) · aim (the AIM history). Type s then p (a bare s still opens "
            "settings). Works for parked, running and done sessions. Same as "
            "`ccc peek --session <id>`."
        ),
        action="peek",
        chord=("s", "p"),
        footer_pos=13.85,  # right after open-session (13.8), before quit (14)
    ),
    Command(
        key="tp",
        word="private",
        gloss="bill this job under the private (cpriv) account",
        section=PER_SESSION,
        explanation=(
            "Set the highlighted row's Claude account to PRIVATE (cpriv) — the account it "
            "launches / resumes under, and the one the home-icon marks in the model column. "
            "Type t then p. Works on a FUTURE job (draft): it has not run yet, so the switch "
            "is free. On a PARKED session it re-stamps the account only when that session's "
            "transcript already lives under the private account (otherwise resume could not "
            "find it) — else it warns and leaves the account unchanged. A LIVE session can "
            "not be switched (it is already billing its running account). Only meaningful "
            "with more than one Claude account configured."
        ),
        action="account_private",
        chord=("t", "p"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti/tw.
    ),
    Command(
        key="tw",
        word="work",
        gloss="bill this job under the work account",
        section=PER_SESSION,
        explanation=(
            "Set the highlighted row's Claude account to WORK — the account it launches / "
            "resumes under. Type t then w. Works on a FUTURE job (draft): it has not run "
            "yet, so the switch is free. On a PARKED session it re-stamps the account only "
            "when that session's transcript already lives under the work account (otherwise "
            "resume could not find it) — else it warns and leaves the account unchanged. A "
            "LIVE session can not be switched (it is already billing its running account). "
            "Only meaningful with more than one Claude account configured."
        ),
        action="account_work",
        chord=("t", "w"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti/tp.
    ),
    # ---- Global ----
    Command(
        key="fn",
        word="new-job",
        gloss="register a future job (start later)",
        section=GLOBAL,
        explanation=(
            "Register a FUTURE job — a saved Claude Code session you start later. Press f "
            "then n. On a repo row the job targets that repo; on a category header (or "
            "anywhere off the repo tree) a picker lets you choose the repo (or create one, "
            "if a create-repo command is configured). A dialog captures the AIM (required), "
            "an optional prompt "
            "(defaults to the AIM), an optional deadline and an optional FIXED start date "
            "(YYYY-MM-DD). The job appears under a FUTURE section; select it and press r "
            "(resume) or Enter to launch it — Claude Code opens in that repo with the AIM "
            "pre-set and the prompt sent. A job WITH a fixed start date instead sinks into "
            "the SCHEDULED section at the very bottom (soonest first), and launching it "
            "before that date asks for confirmation first. Same as `ccc new-job` (-s sets "
            "the start date)."
        ),
        action="new_job",
        chord=("f", "n"),
        footer_pos=8,
    ),
    Command(
        key="R",
        word="Refresh-now",
        gloss="re-scan immediately",
        section=GLOBAL,
        explanation=(
            "Re-scan all sessions right now. The list already auto-refreshes every 5s, "
            "so you rarely need this — it is for an instant update after acting inside a "
            "session."
        ),
        action="refresh_data",
        bind="R",
        # No footer_pos: deliberately hidden from the bottom "keys:" hint line,
        # while remaining bound and listed in help via COMMANDS membership.
    ),
    Command(
        key="u",
        word="undo",
        gloss="undo the last action",
        section=GLOBAL,
        explanation=(
            "Undo the most recent undoable action, walking further back on each press "
            "(up to 20 steps, remembered for this ccc run only). Covered: close/park "
            "(c/x — including the close offered after marking a live session done), "
            "mark-done/un-done (d), Keep (K), importance (!), a sub-goal tick (space), "
            "an account switch (tp/tw), and every toggle (td / tf / ti, t1–t4, to/ta). "
            "Undoing the close of a LIVE session cannot revive the killed process — it "
            "restores the row and reopens the conversation in a new tab (same as r). "
            "Text edits (a / n / b / D / e) are not undoable here — re-edit the field; "
            "past AIMs live in aim-history (ah)."
        ),
        action="undo",
        bind="u",
        footer_pos=2.7,  # right after ☾lose (2.5) — the most common thing to undo
    ),
    Command(
        key="td",
        word="done",
        gloss="show / hide done sessions",
        section=GLOBAL,
        explanation=(
            "Toggle whether DONE sessions (shown in green) appear in the list. Type t then "
            "d in quick succession. They are hidden by default to declutter; the chord "
            "brings them back, and again hides them."
        ),
        action="toggle_finished",
        chord=("t", "d"),
        # Shown in the footer as the `t` toggle leader ("toggle", t gilded); pressing t alone
        # then waiting pops a menu of the available t-chords (td = done, tf = future).
        footer_pos=9.5,
        footer_word="toggle",
        footer_key="t",
    ),
    Command(
        key="tf",
        word="future",
        gloss="show / hide future jobs",
        section=GLOBAL,
        explanation=(
            "Toggle whether FUTURE jobs (not-yet-started drafts, shown in blue) appear in "
            "the list. Type t then f in quick succession. They are SHOWN by default; the "
            "chord hides them, and again brings them back. Also covers the SCHEDULED "
            "section (drafts with a fixed start date, at the very bottom)."
        ),
        action="toggle_future",
        chord=("t", "f"),
        # No footer_pos: the `t` toggle leader is already shown via the `td` entry above;
        # pressing t alone pops the menu listing td, tf and ti.
    ),
    Command(
        key="ti",
        word="idle-alerts",
        gloss="mute / unmute idle 'waiting' popups",
        section=GLOBAL,
        explanation=(
            "Toggle the macOS 'a session is waiting for your input' popups — Claude Code's "
            "native push notifications (the agentPushNotifEnabled setting). Type t then i. "
            "They are ON by default; the chord mutes them, and again re-enables. Unlike td / "
            "tf (which only show or hide rows in this view) ti persists to Claude Code's own "
            "settings.json, so it affects every session — and a session already running may "
            "need a restart to pick up the change. Same as `ccc toggle-idle`."
        ),
        action="toggle_idle",
        chord=("t", "i"),
        # No footer_pos: like tf, the `t` leader is shown once via td; the t menu lists ti.
    ),
    Command(
        key="t1",
        word="card-private",
        gloss="expand / collapse the Claude Code (private) card",
        section=GLOBAL,
        explanation=(
            "Expand or collapse the Claude Code (private) usage card — the gold-bordered "
            "5h/weekly bars for the private account. Type t then 1. Expanded by default; "
            "the chord collapses it to its titled top border alone (and again expands "
            "it), persisted to ccc's config. Only how much of the card is drawn changes "
            "— the underlying usage capture is untouched."
        ),
        action="toggle_card_private",
        chord=("t", "1"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti.
    ),
    Command(
        key="t2",
        word="card-work",
        gloss="expand / collapse the Claude Code (work) card",
        section=GLOBAL,
        explanation=(
            "Expand or collapse the Claude Code (work) usage card — the blue-bordered "
            "5h/weekly bars for the work account. Type t then 2. Expanded by default; the "
            "chord collapses it to its titled top border alone (and again expands it), "
            "persisted to ccc's config. It reads '—' until a work session runs a turn "
            "(nothing to capture before then). With no `work` account configured the card "
            "is absent entirely, and the chord explains that instead of toggling."
        ),
        action="toggle_card_work",
        chord=("t", "2"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti.
    ),
    Command(
        key="t3",
        word="card-codex",
        gloss="expand / collapse the OpenAI Codex card",
        section=GLOBAL,
        explanation=(
            "Expand or collapse the OpenAI Codex usage card — the green-bordered 5h/weekly "
            "bars read from Codex's newest session rollout. Type t then 3. Expanded by "
            "default; the chord collapses it to its titled top border alone (and again "
            "expands it), persisted to ccc's config."
        ),
        action="toggle_card_codex",
        chord=("t", "3"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti.
    ),
    Command(
        key="t4",
        word="card-copilot",
        gloss="expand / collapse the Copilot card",
        section=GLOBAL,
        explanation=(
            "Expand or collapse the Copilot usage card — the violet-bordered month-to-date "
            "spend bar. Type t then 4. Expanded by default; the chord collapses it to its "
            "titled top border alone (and again expands it). Collapsing it ALSO stops the "
            "periodic `gh` billing fetch (it flips both copilot_usage and "
            "usage_card_copilot), so a collapsed card costs no network call. "
            "Fetch-but-don't-draw stays possible by hand-editing the config."
        ),
        action="toggle_card_copilot",
        chord=("t", "4"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti.
    ),
    Command(
        key="to",
        word="card-nix-supervised",
        gloss="expand / collapse the nixos overseer supervised card",
        section=GLOBAL,
        explanation=(
            "Expand or collapse the 'nixos overseer supervised' card — incidents from the "
            "external homelab overseer daemon that are awaiting a human decision "
            "(orange-bordered). Type t then o. Expanded by default; the chord collapses it "
            "to its titled top border alone (and again expands it), persisted to ccc's "
            "config. Reads a placeholder until nixos_overseer_dir points at the "
            "overseer's directory."
        ),
        action="toggle_card_nixos_overseer_supervised",
        chord=("t", "o"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti.
    ),
    Command(
        key="ta",
        word="card-nix-tier-a",
        gloss="expand / collapse the nixos overseer tier_a card",
        section=GLOBAL,
        explanation=(
            "Expand or collapse the 'nixos overseer tier_a' card — recent AUTOMATIC "
            "(tier-A) activity from the external homelab overseer daemon over the last 7 "
            "days (teal-bordered). Type t then a. COLLAPSED by default (its titled top "
            "border alone); the chord expands it (and again collapses it), persisted to "
            "ccc's config. Reads a placeholder until nixos_overseer_dir points at the "
            "overseer's directory."
        ),
        action="toggle_card_nixos_overseer_tier_a",
        chord=("t", "a"),
        # No footer_pos: surfaced via the `t` leader menu, like tf/ti.
    ),
    Command(
        key="s",
        word="settings",
        gloss="open settings",
        section=GLOBAL,
        explanation=(
            "Open the Settings screen: toggle the background daemon and its behaviours "
            "(auto-close idle, auto-update progress, done-checks) and adjust "
            "colours / thresholds."
        ),
        action="settings",
        bind="s",
        footer_pos=10,
    ),
    Command(
        key="h",
        word="help",
        gloss="this help",
        section=GLOBAL,
        explanation=(
            "Open this help overlay (? or h opens it; Esc, q, or ? closes it). The Topics "
            "list — including this Commands & keys explorer — drills into the longer "
            "explanations."
        ),
        action="help",
        bind="h",
        aliases=("question_mark",),
        footer_pos=11,
    ),
]


# ---- projections used by the TUI -----------------------------------------
def sections() -> list[tuple[str, list[Command]]]:
    """Commands grouped by help section, in :data:`SECTION_ORDER`."""
    return [(s, [c for c in COMMANDS if c.section == s]) for s in SECTION_ORDER]


def footer_commands() -> list[Command]:
    """Commands that appear in the bottom footer hint line, in footer order."""
    shown = [c for c in COMMANDS if c.footer_pos is not None]
    return sorted(shown, key=lambda c: c.footer_pos if c.footer_pos is not None else 0)


def binding_specs() -> list[tuple[str, str, str, bool]]:
    """``(key, action, description, show)`` for every Textual binding.

    Primary bindings are emitted first, then any aliases (shown=False). Chord /
    nav-only commands (no ``bind``) are skipped — they are handled in the App.
    """
    out: list[tuple[str, str, str, bool]] = []
    for c in COMMANDS:
        if c.bind is None or c.action is None:
            continue
        out.append((c.bind, c.action, c.gloss, True))
        out.extend((alias, c.action, c.gloss, False) for alias in c.aliases)
    return out


def column_key(header_word: str) -> str | None:
    """The gold mnemonic key for a table-column header, or None if it has none."""
    for c in COMMANDS:
        if c.column == header_word:
            return c.key
    return None


def by_action(action: str) -> Command:
    """The single command bound to *action* (raises if absent — a wiring bug)."""
    return next(c for c in COMMANDS if c.action == action)


def chords_for_leader(leader: str) -> list[Command]:
    """Every two-key chord whose leader is *leader* (e.g. all `t…` toggles)."""
    return [c for c in COMMANDS if c.chord and c.chord[0] == leader]


# Wiring sanity-checks, run at import so a malformed registry fails loudly/early.
assert all(c.section in SECTION_ORDER for c in COMMANDS), "command in unknown section"
assert all(c.key == "".join(c.chord) for c in COMMANDS if c.chord), "chord key mismatch"
assert len({c.footer_pos for c in COMMANDS if c.footer_pos is not None}) == len(
    [c for c in COMMANDS if c.footer_pos is not None]
), "duplicate footer_pos"
