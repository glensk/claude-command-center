"""``ccc`` / ``ccc tui`` — the interactive Textual command center.

Top pane: a session table grouped strictly by repo category (each category
shown once as a header with its repos nested beneath; AIM-defined sessions sort
first within their category); the ``done`` section sinks to the bottom. ``done`` is
hidden by default (toggle with the ``td`` chord).
``↑/↓`` move whole rows; ``←/→`` cycle a per-column cursor on the selected session
(``/aim`` → ``/next-step`` → ``/deadline`` → ``/block``, wrapping) so ``Enter``
edits that one field — ``↑/↓`` (or ``Esc``) snap back to whole-row selection, and
the word-back/forward keys (Option+←/→ → ``ctrl+←/→``) jump three rows down/up.
The single keys ``a`` ``n`` ``D`` ``b`` still edit a field directly, and the
editors autocomplete defined ``@tags``. Bottom pane: a read-only header (status,
sub-goals, live TodoWrite list, summary). Served to a browser by ``ccc serve``.
"""

# The full interactive TUI legitimately exceeds the default module-length limit.
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from rich.cells import cell_len
from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TextArea,
)
from textual.worker import get_current_worker

from .. import (
    accounts,
    cachettl,
    colors,
    config,
    deps,
    future_files,
    gitstatus,
    idlenotify,
    iterm_api,
    jumpstate,
    launchd,
    nixos_overseer,
    repos,
    resume,
    routing,
    tabcolor,
    tabsymbol,
    tags,
    terminal,
    usage,
)
from ..adapters.claude import ClaudeAdapter
from ..core import Row, build_rows
from ..models import (
    DEFAULT_LLM,
    DEP_MARKER,
    HALTED_RESUME_HELP,
    HALTED_RESUME_ICON,
    JOB_TYPE_LABELS,
    LLM_ARROW,
    LLM_CHOICES,
    STATUS_HELP,
    STATUS_ICON,
    Session,
    Status,
    aim_column_first,
    aim_score_pct,
    days_until_start,
    display_aim,
    done_bar_parts,
    drift_unresolved,
    effective_progress,
    empty_track_tint,
    expand_llm_choice,
    humanize_age,
    importance_marks,
    iso_date,
    iso_datetime,
    loads_todos,
    low_aim_score,
    model_effort_cell,
    now_ms,
    parse_iso_date,
    parse_manual_progress,
    progress_bar,
    scheduled_date,
    short_date_label,
    short_id,
    subgoal_provenance,
    todo_box,
    todos_counts,
    version_column_text,
)
from ..store import Store
from . import commands


def _chord_map() -> tuple[dict[str, dict[str, str]], dict[str, str | None]]:
    """Two-key chords from the registry, supporting several followers per leader.

    Returns ``(followers, fallback)`` where ``followers[leader]`` maps each follower
    key to its action (so leader ``t`` can carry both ``td`` and ``tf``), and
    ``fallback[leader]`` is the leader's own standalone binding action (e.g. ``a`` =
    edit_aim) fired when no valid follower arrives. A pure leader (``t``, ``f``) has a
    ``None`` fallback and is a transparent prefix.
    """
    binds = {c.bind: c.action for c in commands.COMMANDS if c.bind and c.action}
    followers: dict[str, dict[str, str]] = {}
    for cmd in commands.COMMANDS:
        if cmd.chord and cmd.action:
            leader, follower = cmd.chord
            followers.setdefault(leader, {})[follower] = cmd.action
    fallback = {leader: binds.get(leader) for leader in followers}
    return followers, fallback


# Leader → {follower: action} (e.g. `t` → {d: toggle_finished, f: toggle_future}) and
# leader → standalone-fallback action (e.g. `a` → edit_aim). See on_key.
_CHORDS, _CHORD_FALLBACK = _chord_map()

# The usage-card render-gate toggles (`t1`…`t5`): action → the config bool it
# flips. Drives both the toggle handlers and the `t` menu's live state annotation.
_CARD_TOGGLE_KEYS: dict[str, str] = {
    "toggle_card_private": "usage_card_private",
    "toggle_card_work": "usage_card_work",
    "toggle_card_codex": "usage_card_codex",
    "toggle_card_codex_private": "usage_card_codex_private",
    "toggle_card_copilot": "usage_card_copilot",
    "toggle_card_nixos_overseer_supervised": "card_nixos_overseer_supervised",
    "toggle_card_nixos_overseer_tier_a": "card_nixos_overseer_tier_a",
}

# Cells a card's top border spends on everything that is not the title itself:
# ``╭─ `` + title + `` ─╮``. A collapsed card is sized to exactly this + its title.
_CARD_TITLE_BORDER_CELLS = 6
# The #usage* cards' CSS min-width, mirrored here because _set_card_expanded has to
# floor its own computed widths by it. Keep in sync with the CSS rules below.
_CARD_MIN_WIDTH = 38

# Border-title chord suffixes for the two nixos-overseer cards. The other four cards
# build their title once in on_mount; these two are REBUILT every render tick (the title
# carries a live incident count), so the suffix is appended there instead. Keys come from
# the registry, never hard-coded.
_NIXOS_SUPERVISED_CHORD = commands.by_action("toggle_card_nixos_overseer_supervised").key
_NIXOS_TIER_A_CHORD = commands.by_action("toggle_card_nixos_overseer_tier_a").key


def _set_card_expanded(panel: Static, expanded: bool, *, visible: bool = True) -> None:
    """Expand or COLLAPSE one usage card — what its `t…` render gate now does.

    Collapsed is not hidden: the card keeps its border-TOP row — the titled
    ``╭─ Claude (private) 🏠 / t1 ───╮`` line, which is also where the toggle's own
    chord is advertised — and drops everything below it. The ``.card-collapsed`` class
    does that (``height: 1`` + ``border-bottom: none``).

    A collapsed card also **stops paying for its content's width**. The cards sit in an
    auto-width column that is as wide as its widest card and is pinned to the right edge,
    so every cell a card claims is a cell taken off the job-details pane beside it — and
    the widest card is easily one nobody is looking at (the nixos supervised card runs
    ~79 cells with real incidents, vs 38 for the usage cards). So collapsing blanks the
    content and pins the width to what the TITLE needs — ``╭─ <title> ─╮`` = title + 6
    cells, floored by each card's CSS ``min-width`` — and expanding hands the width back
    to CSS (``width: auto``, i.e. the content again). The content itself is re-rendered
    on the next tick either way, so nothing is lost by blanking it.

    An EXPANDED card gets the opposite treatment for the same reason: Textual computes
    ``width: auto`` from the CONTENT alone and then truncates the border title to what
    is left (``render_border_label`` clips at ``width - 4``, so 32 cells on a 38-wide
    card, verified). A title that outgrows its card therefore loses its TAIL — the chord
    and the subscription date, the two things the title exists to say. So the floor is
    raised to whatever the title needs; :func:`usage.codex_card_title` has already
    squeezed the address as far as it will go, making this at most a cell or two.

    *visible* is the separate, harder gate for a card that has nothing to say at all (no
    ``work`` account): ``display = False`` removes it outright, title line included.
    """
    panel.display = visible
    panel.set_class(not expanded, "card-collapsed")
    title_cells = cell_len(str(panel.border_title or "")) + _CARD_TITLE_BORDER_CELLS
    if expanded:
        panel.styles.width = None  # drop the inline pin → back to the CSS `width: auto`
        panel.styles.min_width = max(_CARD_MIN_WIDTH, title_cells)
        return
    panel.update("")
    panel.styles.width = title_cells


# How long a leader stays pending before its timeout. A leader with a standalone action
# (`a` → edit_aim, `s` → settings) stays snappy — the timeout just fires that action — so
# 250 ms. That is safe against the Karabiner vi-mode simultaneous layers because every
# pair carries `key_down_order: strict`: a FOLLOWER key (`h`, `p`) pressed alone never
# arms a chord detector and lands on key-DOWN, so the leader-release → follower-arrival
# gap is just the typing gap (~80–200 ms). Only the LEADER keys are withheld until
# key-up. A *pure* leader (`t`, `f`: no standalone action) only pops an info menu on
# timeout, so it can afford to wait longer — and it MUST, because `f` heads the vi
# movement layer and the global `f+j` jump chord
# (`basic.simultaneous_threshold_milliseconds`, 500 ms): `f` is delivered on key-UP.
# 700 ms comfortably beats it so the `tf` / `fn` chords register instead of timing out.
_CHORD_WINDOW_FALLBACK = 0.25
_CHORD_WINDOW_PURE = 0.7
# Min seconds between TUI-spawned `ccc claude-usage` warmers. The Claude OAuth fetch, unlike
# the Copilot fetch, writes NOTHING on failure (no keychain token ⇒ oauth_fetched_at never
# advances), so its file-mtime staleness stays true forever; this in-process guard stops a
# persistently-failing fetch from respawning on every render tick.
_CLAUDE_USAGE_SPAWN_MIN_SEC = 60.0

_STATUS_STYLE: dict[Status, str] = {
    Status.WORKING: "bold green",
    Status.WAITING_INPUT: "bold red",  # input required — stands out red (the ⏸ icon + status)
    Status.HALTED: "bold red",  # rate-limit halt — red || icon + status
    Status.WAITING_CODEX: "bold yellow",  # Codex quota exhausted — amber 😴
    Status.IDLE: "bold #ffaf00",  # amber ❯ — waiting for you; not green (vs running ▶ / done ✓)
    Status.SNOOZED: "bold green",  # background task running while the session itself is idle
    Status.PARKED: "grey62",
    Status.DONE: "green3",
    Status.FAILED: "bold red",
}
# The green ▶ appended to a red || when that halted session WILL be auto-revived on its
# account's reset. Same green as WORKING: "this one is going to run again by itself".
_RESUME_ARMED_STYLE = "bold green"
_SEVERITY_STYLE = {"green": "green", "amber": "yellow", "red": "bold red", "none": "grey62"}
_DONE_STYLE = "green3"
_GOLD = "#ffaf00"
# Prompt-cache TTL countdown colours (see cachettl). Hex values equal xterm-256 40/208/196
# so this column renders pixel-identically to the ``ccc ls`` version; orange is #ff8700.
_CACHE_STYLE = {
    "green": "#00d700",
    "orange": "#ff8700",
    "red": "#ff0000",
    # Busy rows show ▶ here — in the SAME green as the ▶ status icon at the start of the
    # line (one source: _STATUS_STYLE), which is also what the recede pass repaints it to.
    "running": _STATUS_STYLE[Status.WORKING],
}
_DRAFT_BLUE = "#5fafff"  # FUTURE (draft) jobs: section header, ✎ icon, prompt preview
# Per-model colours for the draft `<overseer> ▸ <executor>` readout (the /next-step cell).
_LLM_STYLE: dict[str, str] = {
    "fable-5": "#ff9f43",  # orange
    "opus-4.8": "#2ecc71",  # green
    "sonnet-5": "#5fafff",  # blue
}
_FUTURE_LABEL = "FUTURE"  # separator above the not-yet-started future-job block
_SCHEDULED_LABEL = "SCHEDULED"  # separator above future jobs with a FIXED start date (very bottom)
_NEW_REPO_SENTINEL = "\x00new-repo"  # RepoPickerScreen dismiss value → run create_repo_command
_AT_TAG = re.compile(r"(@[\w-]+)")

_UNDO_MAX = 20  # undo stack depth — plenty for "oops", small enough to never matter


@dataclass
class _UndoEntry:
    """One reversible action: a toast label + the closure that reverts it."""

    label: str  # e.g. "close (myrepo)" — shown as "Undid: <label>"
    apply: Callable[[], str | None]  # returns an overriding toast message, or None


# Separator-row labels. Only the DONE label (now "done") is still emitted as a status
# separator (the active list groups by repo category instead); the rest are kept for
# help text. DONE is lowercase because it renders as the gilded ``── done ──`` divider.
_GROUP_LABEL: dict[Status, str] = {
    Status.WAITING_INPUT: "WAITING FOR INPUT",
    Status.HALTED: "HALTED (RATE LIMIT)",
    Status.WAITING_CODEX: "WAITING FOR CODEX RESET",
    Status.WORKING: "WORKING NOW",
    Status.IDLE: "ACTIVE TABS",
    Status.SNOOZED: "SNOOZED (BACKGROUND TASK)",
    Status.PARKED: "PARKED",
    Status.DONE: "done",
    Status.FAILED: "FAILED",
}

# Names the whole column-header line — the table's counterpart to the footer's
# `keys:` label — so it can be referred to by name ("the head: line"). It rides in
# the version column's header slot: that column is already 5 cells wide ("  193"),
# exactly fitting "head:", so the label costs no extra width. The importance column
# before it carries no heading, so it stays at its natural (narrow) width instead of
# being stretched to hold the label.
_HEAD_LABEL = "head:"
_OAI_BADGE_STYLE = "bold black on white"

# Column headers: (leading spaces matching the data cells, word). A header whose
# word labels a command column (see commands.column_key) gets its shortcut shown
# gold; everything else is plain. Order/length defines the table's columns.
_HEADERS: list[tuple[str, str]] = [
    ("", " "),
    ("", ""),  # importance (!/!!/!!!) — no heading; keeps this column narrow
    ("", _HEAD_LABEL),  # Claude Code version (e.g. 193 of 2.1.193); also names the head: line
    # No leading space: the `folder` heading starts at the folder column's left edge,
    # aligning with the category divider word (── private ──) which sits there too.
    ("", "folder"),
    ("  ", "id"),  # tab badge + 4-char id, one chip painted on the tab colour
    (" ", "model"),  # OBSERVED model·effort the session ran on (not the job config)
    ("  ", "/aim"),
    ("  ", "/next-step"),
    ("  ", "age"),
    ("  ", "⎇"),  # git: single-glyph branch symbol keeps the column tight
    ("  ", "progress"),
]

# The /aim column is stretched at runtime to soak up all leftover horizontal space
# (see SessionTable.fit_aim_column) so the trailing `progress` column sits flush
# against the right edge instead of floating mid-table.
_AIM_COL = next(i for i, (_lead, word) in enumerate(_HEADERS) if word == "/aim")
# Column indices derived from _HEADERS so inserting/reordering a column never desyncs
# the separator-row placement (_FOLDER_COL) or the editable-column cursor (_EDIT_COLS).
_FOLDER_COL = next(i for i, (_lead, word) in enumerate(_HEADERS) if word == "folder")
_ID_COL = next(i for i, (_lead, word) in enumerate(_HEADERS) if word == "id")
_MODEL_COL = next(i for i, (_lead, word) in enumerate(_HEADERS) if word == "model")
_NEXT_COL = next(i for i, (_lead, word) in enumerate(_HEADERS) if word == "/next-step")
_PROGRESS_COL = next(i for i, (_lead, word) in enumerate(_HEADERS) if word == "progress")
_AIM_MIN_WIDTH = 30  # never let the stretch shrink /aim below this many cells
_AIM_MAX_CHARS = 200  # cap the stored /aim text; the fixed-width column crops to fit
# How often the detail-pane usage cards (and the rest of the TUI) re-render is the
# configurable ``usage_refresh_sec`` (default 5.0): it drives the refresh timer. (Cheap
# cache re-reads only — the *expensive* Copilot fetch has its own, separately adaptive,
# throttle. See config.py. The cadence is no longer shown in a card title.)
# How often the TUI checks for `ccc jump` signals. peek is a ~0.1 ms file read, so 10 Hz
# is free; the poll now also carries the whole-toggle verb (the f+j fast path), so its
# cadence — not a slow osascript walk — bounds the perceived f+j latency.
_JUMP_POLL_SEC = 0.1


def _current_session_uuid_fallback() -> str | None:
    """AppleScript fallback for the warm link's current-session lookup (uuid or None)."""
    current = terminal.current_iterm_session()
    return current[0] if current else None


def _gold_mnemonic(word: str, key: str, base: str) -> Text:
    """Render *word* with the shortcut shown in gold.

    A single character that occurs in the word is highlighted in place (``/aim``
    → the ``a``; ``Refresh-now`` → the capital ``R``; ``☾lose`` → the ``☾`` glyph
    that stands in for the ``c`` close key). A two-letter chord whose letters both
    occur in order highlights both in place (``ah`` → "**a**im-**h**istory").
    Otherwise the key is appended in parentheses (``finished(tf)``,
    ``subgoal(spc)``) so a shift/symbol key is never confused with a lowercase
    letter inside the word (``r`` resume vs ``R`` refresh).
    """
    out = Text()
    gold = f"bold {_GOLD}"
    # Single-character mnemonic highlighted in place — a letter (r, R), or a symbol
    # glyph that literally appears in the label (the ☾ standing in for c in ☾lose).
    if len(key) == 1 and (idx := word.find(key)) >= 0:
        out.append(word[:idx], style=base)
        out.append(word[idx], style=gold)
        out.append(word[idx + 1 :], style=base)
        return out
    # Two-letter chord (e.g. `ah`) — gild both letters in place when they appear in order.
    if len(key) == 2 and key.isalpha():
        i = word.find(key[0])
        j = word.find(key[1], i + 1) if i >= 0 else -1
        if 0 <= i < j:
            out.append(word[:i], style=base)
            out.append(word[i], style=gold)
            out.append(word[i + 1 : j], style=base)
            out.append(word[j], style=gold)
            out.append(word[j + 1 :], style=base)
            return out
    out.append(word, style=base)
    out.append(f"({'spc' if key == 'space' else key})", style=gold)
    return out


def _keyhints_text() -> Text:
    """The bottom footer hint line, built from the command registry (footer order).

    A command may override its footer label/mnemonic (e.g. the `tf` chord shows as
    "toggle" with just the leader `t` gilded — the `t…` toggle family's hint).
    """
    out = Text()
    out.append("keys: ", style="grey42")  # label the line — it lists the keystrokes
    for index, cmd in enumerate(commands.footer_commands()):
        if index:
            out.append("  ", style="grey37")
        out.append_text(
            _gold_mnemonic(cmd.footer_word or cmd.word, cmd.footer_key or cmd.key, "grey58")
        )
    return out


def _header_text(leading: str, word: str, suffix: str = "") -> Text:
    """A column header; if *word* labels a command column, its shortcut shows gold.

    *suffix* is appended dimmed and OUTSIDE the mnemonic lookup (the `/aim (1)` marker
    that says the column shows the ORIGINAL AIM), so it never breaks the shortcut match.
    """
    if word == _HEAD_LABEL:
        # The header-line label (mirrors the footer's `keys:`): dimmed so it reads
        # as a line name, not a column name.
        return Text(leading + word, style="grey42")
    key = commands.column_key(word)
    if key is None:
        out = Text(leading + word, style="bold")
    else:
        out = Text(leading, style="bold")
        out.append_text(_gold_mnemonic(word, key, "bold"))
    if suffix:
        out.append(suffix, style="grey42")
    return out


def _first_line(text: str | None) -> str:
    return text.splitlines()[0] if text else ""


def _with_tags(text: str, base_style: str) -> Text:
    """Render *text*, colouring known @tags by type; unknown @tags in a warning style."""
    out = Text()
    for part in _AT_TAG.split(text):
        if part.startswith("@"):
            out.append(part, style=tags.tag_style(part[1:]) or tags.UNKNOWN_STYLE)
        else:
            out.append(part, style=base_style)
    return out


def _id_bg_rgb(session: Session) -> tuple[int, int, int] | None:
    """Tab colour for the id chip's background: live tab cache, else the repo colour.

    Same resolution chain as the status line (and ``peek._id_rgb``), so a row's id is
    painted with the background the user already sees in that tab's Claude Code. Only
    ever called for a row with an OPEN tab — a row with no tab is left unpainted, so
    the background reads as "this one is on screen right now".
    """
    return colors.tab_rgb(session.iterm_session_id) or colors.folder_rgb(session.cwd)


def _bg_style(rgb: tuple[int, int, int]) -> str:
    """``<fg> on #rrggbb`` for *rgb*, foreground picked by luminance (statusline rule)."""
    red, green, blue = rgb
    lum = (299 * red + 587 * green + 114 * blue) // 1000
    fore = "black" if lum > 128 else "bright_white"
    return f"{fore} on #{red:02x}{green:02x}{blue:02x}"


def _id_badge(session: Session, *, live: bool) -> str:
    """The row's tab badge — live tab cache, else the deterministic per-repo symbol.

    Two spaces stand in when there is nothing to key on (no cwd), so the id lands at the
    same column on every row (an emoji is 2 cells wide).
    """
    return tabsymbol.cell_for(session.iterm_session_id, session.cwd, live=live).strip() or "  "


def _draft_id_cell(session: Session) -> Text:
    """A draft (future job) row's id-column cell: the bare 4-hex display hash.

    The 4-hex display hash (``future_files.display_hash``) is shown alone; the
    free-text "when I intend to start" note now rides the next-step (tags/notes)
    column (see :func:`_draft_next_cell`). A SCHEDULED draft (fixed ``start_date``)
    still has its date span the importance + ver cells at the row's start, so this
    column stays a narrow hash throughout. Once the draft has synced to a
    future-job file (``session.future_file`` set), the hash span carries a Rich
    ``Style(link=...)`` so Textual (8.2.7+) emits an OSC 8 hyperlink that opens the
    file in Obsidian — the same guaranteed path as the `oo` chord.

    Carries no leading padding of its own: the caller composes the badge part of the id
    chip, so the hash starts at the same column as a session id (see _add_session_row).
    """
    hash_ = future_files.display_hash(session.session_id)
    text = Text(hash_, style=_DRAFT_BLUE)
    if session.future_file:
        uri = future_files.obsidian_uri(session.future_file)
        text.stylize(Style(link=uri), 0, len(hash_))
    return text


def _draft_next_cell(session: Session, base_style: str) -> Text:
    """A draft row's next-step cell: ``@tags · <start_when note>`` (tags/notes column)."""
    prefix = "  "
    next_step = _first_line(session.next_step)
    note = _first_line(session.start_when)
    if next_step and note:
        head = next_step[:45]  # keep the head + " · " within the 48-char cell cap
        out = Text(prefix)
        out.append_text(_with_tags(head, base_style))
        out.append(" · ", style="grey50")
        budget = 48 - (len(head) + 3)
        out.append(note[:budget] if budget > 0 else "", style=_DRAFT_BLUE)
        return out
    if next_step:
        return _with_tags(prefix + next_step[:48], base_style)
    if note:
        return Text(prefix + note[:48], style=_DRAFT_BLUE)
    return Text(prefix + "—", style=base_style)


def _models_cell(session: Session, prefix: str = "  ") -> Text:
    """A draft row's ``model`` cell: its configured overseer/executor model pair.

    When overseer and executor match (the common case) a single colour-coded name is shown;
    otherwise the ``<overseer> ▸ <executor>`` pair, each name colour-coded per
    :data:`_LLM_STYLE` (orange / green / blue) with a dim arrow between. The readout is
    bounded (each name ≤ 8 chars) so it never approaches the column's truncation width —
    the cell renders in full.
    """
    text = Text(prefix)
    text.append(session.llm_overseer, style=_LLM_STYLE.get(session.llm_overseer, "white"))
    if session.llm_exec != session.llm_overseer:
        text.append(f" {LLM_ARROW} ", style="grey50")
        text.append(session.llm_exec, style=_LLM_STYLE.get(session.llm_exec, "white"))
    return text


class TagSuggester(Suggester):
    """Autocomplete the current ``@token`` from the defined tag registry."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=False)

    async def get_suggestion(self, value: str) -> str | None:
        at_pos = value.rfind("@")
        if at_pos == -1:
            return None
        prefix = value[at_pos:].lower()
        for tag in tags.known_tags():
            if tag.lower().startswith(prefix) and tag.lower() != prefix:
                return value[:at_pos] + tag
        return None


class InputScreen(ModalScreen[str | None]):
    """A modal asking for text. Two shapes:

    * single-line (default) — a one-row ``Input``; **Enter** submits, ``Esc``
      cancels. Used for short, strict values (e.g. a deadline date).
    * ``multiline=True`` — a large, soft-wrapping ``TextArea`` so a long /
      multi-line value (a whole AIM or next-step) is **fully visible without
      scrolling**. Here **Enter inserts a newline**; **Ctrl+S** submits and
      ``Esc`` cancels (the prompt label spells this out). When a ``suggester``
      is supplied, **Tab** completes the current ``@tag`` in place — the
      multiline analogue of the single-line ghost autocomplete.
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "complete_tag", "Complete @tag", show=False),
    ]

    DEFAULT_CSS = """
    InputScreen { align: center middle; }
    #box {
        width: 70%;
        max-width: 90;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    /* Multiline mode: a big editor so even a long, wrapped AIM fits at a glance. */
    #box.multiline {
        width: 90%;
        max-width: 140;
        height: 80%;
    }
    #box.multiline #field {
        height: 1fr;
        border: round $accent;
    }
    #box Label { margin-bottom: 1; }
    """

    def __init__(
        self,
        prompt: str,
        initial: str = "",
        suggester: Suggester | None = None,
        *,
        multiline: bool = False,
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial
        self._suggester = suggester
        self._multiline = multiline

    def compose(self) -> ComposeResult:
        with Vertical(id="box", classes="multiline" if self._multiline else ""):
            if self._multiline:
                hint = "Ctrl+S save · Esc cancel"
                if self._suggester is not None:
                    hint += " · Tab completes @tag"
                yield Label(f"{self._prompt}  [dim]({hint})[/dim]")
                yield TextArea(self._initial, id="field", soft_wrap=True)
            else:
                yield Label(self._prompt)
                yield Input(value=self._initial, id="field", suggester=self._suggester)

    def on_mount(self) -> None:
        self.query_one("#field").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_submit(self) -> None:
        """Ctrl+S in multiline mode — single-line mode submits on Enter instead."""
        if self._multiline:
            self.dismiss(self.query_one("#field", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_complete_tag(self) -> None:
        """Tab in multiline mode: complete the ``@token`` left of the cursor.

        Mirrors :class:`TagSuggester` (Input's ghost autocomplete) for the
        ``TextArea``, which has no suggester support. No-op when there is no
        suggester, no ``@`` token, or no matching tag.
        """
        if not self._multiline or self._suggester is None:
            return
        field = self.query_one("#field", TextArea)
        before = field.get_text_range((0, 0), field.cursor_location)
        at_pos = before.rfind("@")
        if at_pos == -1:
            return
        prefix = before[at_pos:].lower()
        for tag in tags.known_tags():
            if tag.lower().startswith(prefix) and tag.lower() != prefix:
                field.insert(tag[len(prefix) :])
                return


class ConfirmScreen(ModalScreen[bool]):
    """A confirmation modal (``y``/Enter = yes, ``n``/Esc = no).

    Pass ``no_label=None`` for a single-button variant: only the primary
    *yes* button is shown (focused, so Enter presses it) and ``Esc`` (or
    ``n``) is the way to decline — no second button to land on.
    """

    BINDINGS = [
        Binding("escape", "no", show=False),
        Binding("n", "no", show=False),
        Binding("y", "yes", show=False),
    ]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #box {
        width: 70%;
        max-width: 84;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #box Label { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """

    def __init__(
        self, prompt: str, yes_label: str = "Close", no_label: str | None = "Keep open"
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._prompt)
            with Horizontal(id="buttons"):
                if self._no_label is not None:
                    yield Button(self._no_label, id="no")
                yield Button(self._yes_label, id="yes", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class DeadRowScreen(ModalScreen[str | None]):
    """Triage a dead row (a launched job that never had a turn → no resumable transcript).

    Three real outcomes, returned as a choice string: ``"restore"`` (put it back in FUTURE so
    it can be re-run — only offered when *allow_restore*), ``"delete"`` (remove it from the
    command center), or ``None`` (Keep, the safe default on Esc). Keys: ``r`` restore,
    ``d`` delete, ``Esc``/``k`` keep.
    """

    BINDINGS = [
        Binding("escape", "keep", show=False),
        Binding("k", "keep", show=False),
        Binding("d", "delete", show=False),
        Binding("r", "restore", show=False),
    ]
    DEFAULT_CSS = """
    DeadRowScreen { align: center middle; }
    #box {
        width: 70%;
        max-width: 84;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #box Label { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """

    def __init__(self, prompt: str, *, allow_restore: bool) -> None:
        super().__init__()
        self._prompt = prompt
        self._allow_restore = allow_restore

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self._prompt)
            with Horizontal(id="buttons"):
                yield Button("Keep", id="keep")
                if self._allow_restore:
                    yield Button("Restore to FUTURE", id="restore")
                yield Button("Delete", id="delete", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "keep" else event.button.id)

    def action_keep(self) -> None:
        self.dismiss(None)

    def action_delete(self) -> None:
        self.dismiss("delete")

    def action_restore(self) -> None:
        if self._allow_restore:
            self.dismiss("restore")


# --------------------------------------------------------------------------- #
# Future-job (draft) creation modals: pick a category → pick/create a repo →
# capture AIM + prompt + deadline. Chained from `action_new_job` (the `fn` chord).
# --------------------------------------------------------------------------- #
_PICKER_CSS = """
$screen { align: center middle; }
#box {
    width: 70%; max-width: 90; height: auto; max-height: 80%;
    padding: 1 2; background: $surface; border: round $accent;
}
#box Label { margin-bottom: 1; }
#box ListView { height: auto; max-height: 22; }
"""


class CategoryPickerScreen(ModalScreen[str | None]):
    """Pick a repo category (off-grid ``fn``). Enter selects, Esc cancels."""

    BINDINGS = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = _PICKER_CSS.replace("$screen", "CategoryPickerScreen")

    def __init__(self, categories: list[str]) -> None:
        super().__init__()
        self._categories = categories

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("New future job — pick a category:")
            yield ListView(*[ListItem(Label(c)) for c in self._categories])

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._categories):
            self.dismiss(self._categories[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)


class RepoPickerScreen(ModalScreen[str | None]):
    """Pick a repo in a category, or choose to create one. Enter selects, Esc cancels.

    Dismisses with an absolute repo path, the :data:`_NEW_REPO_SENTINEL` (create a
    new repo), or ``None`` (cancelled).
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = _PICKER_CSS.replace("$screen", "RepoPickerScreen")

    def __init__(self, category: str, repo_names: list[str], can_create: bool = False) -> None:
        super().__init__()
        self._category = category
        self._repos = repo_names
        self._can_create = can_create  # show the "create new repo" item (create_repo_command set)

    def compose(self) -> ComposeResult:
        items = [ListItem(Label(name)) for name in self._repos]
        if self._can_create:
            items.append(ListItem(Label("➕ create new repo…", classes="newrepo")))
        with Vertical(id="box"):
            yield Label(f"New future job in [b]{self._category}[/] — pick a repo:")
            yield ListView(*items)

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None:
            return
        if idx < len(self._repos):
            self.dismiss(str(repos.repo_path(self._category, self._repos[idx])))
        elif self._can_create and idx == len(self._repos):
            self.dismiss(_NEW_REPO_SENTINEL)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DependencyPickerScreen(ModalScreen[str | None]):
    """Pick the job this one depends on (or clear it). Enter selects, Esc cancels.

    Dismisses with ``""`` (the ``— none —`` item — clear the dependency), a candidate
    job's full session UUID, or ``None`` (cancelled — no change). Candidates are supplied
    already filtered (not done, not archived, not self, no cycle) as ``(session_id, label)``
    pairs — same ``ListView`` pattern as :class:`CategoryPickerScreen`.
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = _PICKER_CSS.replace("$screen", "DependencyPickerScreen")

    def __init__(self, candidates: list[tuple[str, str]]) -> None:
        super().__init__()
        self._candidates = candidates

    def compose(self) -> ComposeResult:
        items = [ListItem(Label("— none —"))]
        items.extend(ListItem(Label(label)) for _sid, label in self._candidates)
        with Vertical(id="box"):
            yield Label("Depends on which job? (— none — clears it)")
            yield ListView(*items)

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None:
            return
        if idx == 0:
            self.dismiss("")  # — none — → clear the dependency
        elif 1 <= idx <= len(self._candidates):
            self.dismiss(self._candidates[idx - 1][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditForm(Vertical):
    """Inline editor mounted in the job details pane — no boxes, no layout change.

    The editable lines are borderless, transparent inputs you type into directly,
    navigated with ``↑/↓`` or ``Tab`` (the focused line is tinted — that tint IS the
    cursor). The draft-only rows (``/overseer`` ``/executor`` ``/account``
    ``Scheduled for``) come FIRST, mirroring the read-only head where the models sit
    on the ``Status:`` line and the scheduled date right under it — while editing,
    the head drops those readouts so every option renders exactly once. The first
    AIM (``/aim (1):``) is editable too — rewritten in place, so correcting how the
    original goal was stated never invents a new revision; the importance line is a
    read-only ``Static`` the focus cursor skips. AIM / prompt / sub-goals are
    borderless, grow-to-fit ``TextArea`` s
    (multi-line, no border). Two edit-only rows have no read-only twin (their values
    render elsewhere outside edit mode): ``progress %`` (a manual bar override,
    blank = auto from sub-goals; shows in the head bar) and ``sub-goals`` (one item
    per line — add/delete lines, ticks carry over by text; the checklist itself sits
    at the pane bottom).
    """

    BINDINGS = [
        Binding("escape", "app.exit_edit", show=False),
        Binding("up", "focus_previous", show=False),
        Binding("down", "focus_next", show=False),
    ]

    def compose(self) -> ComposeResult:
        """Compose borderless, in-place field lines that mirror the read-only view."""
        # Draft-only rows FIRST (shown/hidden like the prompt & folder rows below), in
        # the read-only head's own order — the models/account sit on the Status: line
        # and Scheduled-for right under it, so their edit rows are reachable at the
        # very top of the form rather than buried below the AIM.
        # Clickable dropdowns: pick from LLM_CHOICES with the mouse or arrow keys — invalid
        # input is impossible, and the Selects stay in the Tab/↑↓ focus chain.
        model_opts = [(choice, choice) for choice in LLM_CHOICES]
        with Horizontal(id="edit-overseer-row", classes="fieldrow"):
            yield Label("/overseer: ", classes="fieldlabel")
            yield Select(model_opts, value=DEFAULT_LLM, allow_blank=False, id="edit-overseer")
        with Horizontal(id="edit-executor-row", classes="fieldrow"):
            yield Label("/executor: ", classes="fieldlabel")
            yield Select(model_opts, value=DEFAULT_LLM, allow_blank=False, id="edit-executor")
        # Draft-only launch (billing) account — shown only in multi-account mode
        # (action_edit_session gates the row's display on len(claude_config_dirs) > 1).
        account_opts = [(label, label) for label in config.claude_config_dirs()] or [
            ("private", "private")
        ]
        with Horizontal(id="edit-account-row", classes="fieldrow"):
            yield Label("/account: ", classes="fieldlabel")
            yield Select(
                account_opts, value=account_opts[0][0], allow_blank=False, id="edit-account"
            )
        # Draft-only fixed start date (the SCHEDULED bucket / the head's blue line).
        with Horizontal(id="edit-scheduled-row", classes="fieldrow"):
            yield Label("Scheduled for: ", classes="fieldlabel")
            yield Input(id="edit-scheduled", placeholder="YYYY-MM-DD — sinks to SCHEDULED")
        # First AIM — editable (rewritten in place, never appended as a revision). Shown
        # only when ≥2 revisions exist; with a single one the `/aim (N)` row below IS it.
        with Horizontal(id="edit-aim1-row", classes="fieldrow"):
            yield Label("/aim (1): ", classes="fieldlabel")
            yield TextArea("", id="edit-aim1", tab_behavior="focus", soft_wrap=True)
        with Horizontal(classes="fieldrow"):
            yield Label("/aim: ", id="edit-aim-label", classes="fieldlabel")
            yield TextArea("", id="edit-aim", tab_behavior="focus", soft_wrap=True)
        with Horizontal(classes="fieldrow"):
            yield Label("/next-step: ", classes="fieldlabel")
            yield Input(id="edit-next", suggester=TagSuggester())
        with Horizontal(classes="fieldrow"):
            yield Label("/deadline: ", classes="fieldlabel")
            yield Input(id="edit-deadline")
        with Horizontal(classes="fieldrow"):
            yield Label("progress %: ", classes="fieldlabel")
            yield Input(id="edit-progress", placeholder="auto (from sub-goals)")
        with Horizontal(classes="fieldrow"):
            yield Label("/block: ", classes="fieldlabel")
            yield Input(id="edit-block", suggester=TagSuggester())
        # Dependency (this job waits on another finishing first) — a Button opening a
        # picker over candidate jobs; visible for EVERY session (not draft-gated).
        with Horizontal(id="edit-depends-row", classes="fieldrow"):
            yield Label("/depends-on: ", classes="fieldlabel")
            yield Button("— none —", id="edit-depends")
        with Horizontal(id="edit-prompt-row", classes="fieldrow"):
            yield Label("prompt: ", classes="fieldlabel")
            yield TextArea("", id="edit-prompt", tab_behavior="focus", soft_wrap=True)
        with Horizontal(id="edit-folder-row", classes="fieldrow"):
            yield Label("folder/repo: ", classes="fieldlabel")
            yield Button("", id="edit-folder")
        yield Static("", id="edit-important")  # read-only importance line
        # Sub-goals last — closest to the read-only checklist at the pane bottom.
        with Horizontal(classes="fieldrow"):
            yield Label("sub-goals: ", classes="fieldlabel")
            yield TextArea("", id="edit-subgoals", tab_behavior="focus", soft_wrap=True)

    def action_focus_previous(self) -> None:
        """Move focus to the previous form control on the active screen."""
        self.screen.focus_previous()

    def action_focus_next(self) -> None:
        """Move focus to the next form control on the active screen."""
        self.screen.focus_next()


class NewRepoScreen(ModalScreen[str | None]):
    """Capture the new repo's ``<category> <name>`` (a gray example shows the form).

    The first two tokens fill the ``create_repo_command`` template's ``{category}`` and
    ``{name}`` placeholders; Enter runs it.
    """

    BINDINGS = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = _PICKER_CSS.replace("$screen", "NewRepoScreen")

    def __init__(self, category: str) -> None:
        super().__init__()
        self._category = category

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(
                "Create a repo — runs [b]create_repo_command[/] as [b]<category> <name>[/]:"
            )
            yield Input(
                placeholder=f"{self._category} my-new-repo",
                id="args",
            )
            yield Label("[dim]Enter creates the repo · Esc cancel[/]")

    def on_mount(self) -> None:
        self.query_one("#args").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewJobScreen(ModalScreen[dict[str, str] | None]):
    """Capture a future job: AIM (required) + optional prompt + optional deadline.

    Enter in the AIM/deadline fields registers the job immediately ("hit enter →
    the line is registered"); inside the prompt box Enter inserts a newline and
    Ctrl+S registers. Esc cancels. A blank AIM is rejected (it is mandatory); a
    blank prompt defaults to the AIM at launch.
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]
    DEFAULT_CSS = """
    NewJobScreen { align: center middle; }
    #box {
        width: 90%; max-width: 120; height: auto; max-height: 90%;
        padding: 1 2; background: $surface; border: round $accent;
    }
    #box Label { margin-top: 1; }
    #box #prompt { height: 10; border: round $accent; }
    """

    def __init__(self, repo_label: str, cwd: str) -> None:
        super().__init__()
        self._repo_label = repo_label
        self._cwd = cwd

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            head = Text("New future job  ", style="bold")
            head.append(self._repo_label, style=_GOLD)
            yield Label(head)
            yield Label("Done when (AIM — required):")
            yield Input(
                placeholder="e.g. Zendesk tickets exported and imported into Zoho", id="aim"
            )
            yield Label(
                "When do you intend to start it? (shown in the next-step column — optional):"
            )
            yield Input(placeholder="e.g. during holidays", id="start_when")
            yield Label("Fixed start date (YYYY-MM-DD — optional; sinks to SCHEDULED, guards r):")
            yield Input(placeholder="blank = none", id="start_date")
            yield Label("Deadline (YYYY-MM-DD — optional):")
            yield Input(placeholder="blank = none", id="deadline")
            yield Label("Run as (default Claude Code; Codex implements + Claude verifies):")
            yield Select(
                [(label, value) for value, label in JOB_TYPE_LABELS.items()],
                value="claude",
                allow_blank=False,
                id="job_type",
            )
            accounts = list(config.claude_config_dirs())
            if len(accounts) > 1:  # only worth asking when >1 Claude account is configured
                yield Label("Claude account to launch (bill) under:")
                yield Select(
                    [(label, label) for label in accounts],
                    value=accounts[0],
                    allow_blank=False,
                    id="account",
                )
            yield Label("Prompt to run when started (optional — defaults to the AIM):")
            yield TextArea(id="prompt", soft_wrap=True)
            yield Label(
                "[dim]Tab next field · Enter (in a field) or Ctrl+S registers · Esc cancel[/]"
            )

    def on_mount(self) -> None:
        self.query_one("#aim").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter from any single-line field registers the job (matches "hit enter → registered").
        self.action_submit()

    def action_submit(self) -> None:
        aim = self.query_one("#aim", Input).value.strip()
        if not aim:
            self.app.bell()
            self.notify("AIM is required to register a future job.", severity="error")
            self.query_one("#aim").focus()
            return
        start_date = self.query_one("#start_date", Input).value.strip()
        if start_date and parse_iso_date(start_date) is None:
            self.app.bell()
            self.notify("Fixed start date must be YYYY-MM-DD (or blank).", severity="error")
            self.query_one("#start_date").focus()
            return
        account_widgets = self.query("#account")
        account = str(account_widgets.first(Select).value) if account_widgets else ""
        self.dismiss(
            {
                "aim": aim,
                "prompt": self.query_one("#prompt", TextArea).text.strip(),
                "deadline": self.query_one("#deadline", Input).value.strip(),
                "start_when": self.query_one("#start_when", Input).value.strip(),
                "start_date": start_date,
                "job_type": str(self.query_one("#job_type", Select).value),
                "account": account,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[bool]):
    """Settings: daemon auto-close, intervals, nag frequency, pane split.

    ``up``/``down`` move between fields; ``Enter`` on Save persists to
    config.toml (and installs/removes the launchd agent).
    """

    BINDINGS = [
        Binding("up", "focus_previous", show=False),
        Binding("down", "focus_next", show=False),
    ]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    #panel {
        width: 80%;
        max-width: 84;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #panel Label { margin-top: 1; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """

    def compose(self) -> ComposeResult:
        cfg = config.load_config()
        with Vertical(id="panel"):
            yield Label("[b]Settings[/b]  (↑/↓ to move, Enter on Save, Esc to cancel)")
            yield Checkbox(
                f"Run the background daemon (launchd) — {launchd.state_badge(launchd.is_loaded())}",
                value=launchd.is_installed(),
                id="daemon",
            )
            yield Checkbox(
                "  ↳ auto-update progress (derive & tick sub-goals)",
                value=cfg.autoprogress,
                id="autoprogress",
            )
            yield Checkbox("  ↳ auto-close idle sessions (reaper)", value=cfg.reap, id="reap")
            yield Label("launchd interval — seconds between daemon passes:")
            yield Input(value=str(cfg.daemon_interval_sec), id="interval", type="integer")
            yield Label("Idle timeout — minutes before a session is auto-closed:")
            yield Input(value=str(cfg.idle_timeout_min), id="idle", type="integer")
            yield Label("Remind to set an AIM every N turns (1=every turn, 0=never):")
            yield Input(value=str(cfg.nag_every_n_turns), id="nag", type="integer")
            yield Label("Show done sessions from last N days (0 = show all):")
            yield Input(value=str(cfg.done_max_age_days), id="donedays", type="integer")
            yield Label("Top pane height (0.0–1.0 of the screen):")
            yield Input(value=str(cfg.split_ratio), id="split", type="number")
            yield Label("Tab title set when ccc starts (blank = leave alone):")
            yield Input(value=cfg.tab_title, id="tabtitle")
            yield Label("Tab color (name like red/blue, or #rrggbb; blank = none):")
            yield Input(value=cfg.tab_color, id="tabcolor")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def action_focus_next(self) -> None:
        self.focus_next()

    def key_escape(self) -> None:
        self.dismiss(False)

    def _int(self, widget_id: str, fallback: int) -> int:
        try:
            return int(self.query_one(f"#{widget_id}", Input).value)
        except (ValueError, TypeError):
            return fallback

    def _float(self, widget_id: str, fallback: float) -> float:
        try:
            return float(self.query_one(f"#{widget_id}", Input).value)
        except (ValueError, TypeError):
            return fallback

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(False)
            return
        cfg = config.load_config()
        cfg.daemon_interval_sec = max(60, self._int("interval", cfg.daemon_interval_sec))
        cfg.idle_timeout_min = max(1, self._int("idle", cfg.idle_timeout_min))
        cfg.nag_every_n_turns = max(0, self._int("nag", cfg.nag_every_n_turns))
        cfg.split_ratio = min(0.95, max(0.05, self._float("split", cfg.split_ratio)))
        cfg.tab_title = self.query_one("#tabtitle", Input).value
        cfg.tab_color = self.query_one("#tabcolor", Input).value
        cfg.autoprogress = self.query_one("#autoprogress", Checkbox).value
        cfg.reap = self.query_one("#reap", Checkbox).value
        cfg.done_max_age_days = max(0, self._int("donedays", cfg.done_max_age_days))
        try:
            config.save_config(cfg)
        except RuntimeError as err:
            self.notify(str(err), severity="error")
            self.dismiss(False)
            return
        terminal.set_tab(cfg.tab_title or None, terminal.color_rgb(cfg.tab_color))
        if self.query_one("#daemon", Checkbox).value:
            launchd.install()  # writes the plist with the new interval and (re)loads it
        else:
            launchd.uninstall()
        self.dismiss(True)


# Narrative tail of the help overview (everything that is NOT a key reference).
# The Navigate / Per-session / Global key lists above it are generated from the
# command registry by _help_body(), so they cannot drift from the real bindings.
_HELP_PROSE = """[b]Named parts[/b] (so each can be referred to by name)
  head:        the column-header line at the top of the table
  keys:        the bottom footer hint line
  job details: the divider above the detail pane (bottom half)
  usage cards: Claude / Codex / Copilot (top-right of detail)

[b]Columns[/b] (named by the [b]head:[/b] line)
  ▶ (1st)  the session is running — working, or awaiting another agent/subagent
  running rows gray out whole-line (not actionable); ❯ (amber) = Claude is waiting for your input
  importance (!/!!/!!!) · ver = Claude Code version (the [b]head:[/b]-labelled column) ·
  folder (tab-coloured) · aim (gold) · next-step (@tags) · age ·
  ⎇ git (✓clean ↑ahead ↓behind ⇅diverged ●dirty) · progress (sub-goals done)
  (/deadline & /block moved off the table — set them with D / b, full values show below)

[b]Order[/b] Grouped strictly by repo category (each category is a single header with
its repos nested beneath; AIM-defined sessions sort first within their category);
the done section sinks to the bottom. Status (working / waiting / parked …) is read
from the first-column icon, not from group separators.

[b]@tags[/b] people=yellow, places=blue, @waiting=green, unknown=red.
Define with `ccc tag add @name type`.

[b]The daemon[/b] a launchd background agent (runs every few minutes) that auto-closes
idle sessions, runs done-checks, regenerates summaries / next-steps, derives & ticks
sub-goals, and fires deadline / stale alerts. Toggle it here in Settings (s), or from
the shell: `ccc daemon --install` / `--uninstall` (one pass now: `ccc daemon`). Check it
with `launchctl list | grep claude-command-center`; logs in ~/.claude/command-center/."""


def _icon_text(glyph: str, style: str, resume_armed: bool = False) -> Text:
    """The first-column icon, two-tone when a halted row is armed for auto-resume.

    *resume_armed* appends a green ``▶`` to the (red) ``||``: this session comes back on
    its own when its account's rate limit resets. Bare ``||`` = stranded until you (r)esume.
    """
    out = Text(glyph, style=style)
    if resume_armed:
        out.append(HALTED_RESUME_ICON, style=_RESUME_ARMED_STYLE)
    return out


def _status_legend() -> Text:
    """The first-column status legend, generated from the Status registry.

    Built straight from ``models`` (the ``Status`` order, ``STATUS_ICON`` and
    ``STATUS_HELP``) and the TUI's own ``_STATUS_STYLE`` colours, so it always
    matches the icons painted in the table and can never go stale: add or remove
    a status and this legend updates itself (models' asserts force icon + help).

    HALTED is the one status with TWO renderings — ``||▶`` (ccc will auto-revive it on
    that account's reset) and a bare ``||`` (nothing will) — so it gets two lines, both
    fed from the same ``models`` constants the table paints from.
    """
    out = Text()
    out.append("Status", style="bold")
    out.append("  (the first-column icon on every row)\n", style="grey58")
    width = 4  # widest icon is ||▶ (3 cells) + a space

    def _line(icon: Text, label: str, help_text: str, style: str) -> None:
        out.append("  ")
        out.append_text(icon)
        out.append(" " * max(1, width - icon.cell_len))
        out.append(f"{label:<14}", style=style)
        out.append(help_text, style="grey70")
        out.append("\n")

    for status in Status:
        style = _STATUS_STYLE.get(status, "grey70")
        if status is Status.HALTED:
            _line(
                _icon_text(STATUS_ICON[status], style, resume_armed=True),
                status.value,
                HALTED_RESUME_HELP,
                style,
            )
        _line(_icon_text(STATUS_ICON[status], style), status.value, STATUS_HELP[status], style)
    out.append("\n")
    out.append("  ▶ ", style=_RESUME_ARMED_STYLE)
    out.append(
        "on a || needs resume_halted=on (Settings s) + a known account + a transcript;\n"
        "    each account waits for its OWN limit reset, and is revived on its own seat.",
        style="grey58",
    )
    out.append("\n")
    return out


def _help_body() -> Text:
    """The scrollable help overview: a registry-driven key reference + prose.

    Each command's shortcut is shown gold in a left gutter (exactly like the
    footer and column headers), then its one-line gloss. Drilling in via the
    Commands & keys topic gives the full per-key explanation.
    """
    out = Text()
    out.append("Claude Command Center — help", style="bold")
    out.append(
        "   (Esc/q/? close · PgUp/PgDn or Home/End scroll · Enter a topic below for detail)\n",
        style="grey58",
    )
    for section, cmds in commands.sections():
        out.append("\n")
        out.append(section, style="bold")
        note = commands.SECTION_NOTE.get(section)
        if note:
            out.append(f"  {note}", style="grey58")
        out.append("\n")
        for cmd in cmds:
            label = "spc" if cmd.key == "space" else cmd.key
            out.append("  ")
            out.append(label, style=f"bold {_GOLD}")
            out.append(" " * max(2, 9 - len(label)))
            out.append(cmd.gloss, style="grey70")
            out.append("\n")
    out.append("\n")
    out.append_text(_status_legend())
    out.append("\n")
    out.append_text(Text.from_markup(_HELP_PROSE))
    return out


_HELP_TOPICS: dict[str, str] = {
    "Progress bar — how it's determined": (
        "Progress = checked ÷ total of the session's sub-goal checklist.\n"
        "  ▓ = done, ░ = remaining. No checklist yet → '—' (unknown, not 0%).\n\n"
        "The table bar is 8 cells wide; the detail pane shows a 12-cell bar plus the\n"
        "count (e.g. 3/4). Ticking a sub-goal (space, or by the daemon) moves the bar."
    ),
    "Sub-goals": (
        "A checklist of concrete steps toward the AIM. Each is either:\n"
        "  • source='user'  — you wrote it; never changed automatically\n"
        "  • source='auto'  — derived by auto-progress, which may tick it\n\n"
        'Add them via the /aim flow or `ccc subgoals "step a" "step b" …`.\n'
        "In the table, `space` ticks the next unchecked sub-goal; the ratio drives the bar."
    ),
    "Fully-automatic progress (the daemon)": (
        "Enable it in Settings: 'Run the background daemon' + 'auto-update progress'.\n\n"
        "Every few minutes the daemon:\n"
        "  • derives 3–6 sub-goals from the AIM if the session has none;\n"
        "  • reads ONLY new user input + final assistant text since last pass\n"
        "    (never tool calls), to stay cheap;\n"
        "  • ticks sub-goals it judges done, with a cheap model.\n\n"
        "It never touches sub-goals you set manually. Keep 'auto-close idle sessions'\n"
        "OFF to update progress without the reaper closing sessions.\n\n"
        "Control it: this Settings screen toggles the launchd agent, or from a shell\n"
        "  ccc daemon              run one pass now\n"
        "  ccc daemon --install    load the recurring launchd agent\n"
        "  ccc daemon --uninstall  unload + remove it\n"
        "State now: {daemon_state}  (live from "
        "`launchctl list | grep {launchd_label}`;\n"
        "a periodic job shows PID `-` between passes — that still counts as loaded/running).\n"
        "Logs: ~/.claude/command-center/daemon.log (and daemon.err)."
    ),
    "@tags": (
        "@tags in next-step / blocked are coloured by type:\n"
        "  people = yellow,  places = blue,  @waiting = green (an 'ok to park' cue),\n"
        "  unknown = red (so a typo stands out).\n\n"
        "Define them with `ccc tag add @name type` and `ccc tag type place '#5599ff'`.\n"
        "The editors autocomplete defined tags as you type '@'."
    ),
    "Update boxes (usage cards) — data & cadence": (
        "The stacked cards top-right of the detail pane, each an account-wide\n"
        "subscription-usage readout (NOT the selected session):\n\n"
        "[b]Claude (private)[/b] and [b]Claude (work)[/b]\n"
        "  Session (5h) + Week (7d) + Fable (weekly-scoped) bars — each account's own\n"
        "  Claude /usage windows. Two sources, complementary:\n"
        "   • Fast path: captured passively from each live session's status line\n"
        "     (`ccc statusline --capture-usage` pipes its rate_limits JSON), merged\n"
        "     across sessions — the between-fetches update while a job works.\n"
        "   • Authoritative: `ccc claude-usage` fetches each account's OAuth /usage\n"
        "     endpoint out-of-band (throttled by claude_usage_refresh_sec / _active_sec),\n"
        "     so the cards no longer lag `claude`'s own /usage and pick up the Fable\n"
        "     weekly window (the status line never carries it). The fetch authoritatively\n"
        "     replaces the snapshot, self-healing a window boundary Anthropic rebased.\n"
        "  A statusline write is routed by the session's CLAUDE_CONFIG_DIR, so the two\n"
        "  accounts' windows are never merged into one bar. The Fable row shows only\n"
        "  after the first OAuth fetch. The work card is hidden unless a `work` account\n"
        "  is listed in claude_accounts.\n"
        "  Session/Week each go stale (bare '?%', no bar) once their snapshot is older\n"
        "  than the window's own lifetime (5h / 7d) — see claude_account_emails below\n"
        "  for the identity hard-link that keeps this from ever reading the wrong\n"
        "  account's numbers.\n"
        "[b]Codex[/b]  Session (5h) + Week (7d) bars, one card per ChatGPT login\n"
        "  (the second card needs codex_home_private set to another CODEX_HOME).\n"
        "  Source: with codex_usage on, the LIVE chatgpt.com usage endpoint — the same\n"
        "  numbers the web Settings → Usage page shows — fetched out-of-band per home\n"
        "  (codex_usage_refresh_sec / _active_sec) with the token in $CODEX_HOME/auth.json.\n"
        "  Off (the default), or whenever that fetch is older than the last Codex turn,\n"
        "  the card falls back to a cheap, mtime-cached read of the newest rollout file's\n"
        "  rate_limits block — as fresh as the last Codex token_count event. Each card\n"
        "  titles itself with its account e-mail so two logins are never confused.\n"
        "[b]{copilot_title}[/b]  one monthly premium-request bar (resets on the 1st).\n"
        "  Source: the `gh` billing API, cached in copilot_usage.json. The only card\n"
        "  whose data costs a network call, so the only one that is throttled.\n\n"
        "[b]Cadence[/b]\n"
        "  • Re-read + re-render every usage_refresh_sec (default 5.0s): re-reads all\n"
        "    three cheap caches and counts the relative resets down. This is the only\n"
        "    cadence for Claude & Codex — their data is as fresh as their source.\n"
        "  • Copilot fetch is throttled: at most once per copilot_usage_refresh_sec\n"
        "    (default 900s), run out-of-band by the daemon and by a detached\n"
        "    `ccc copilot-usage` the TUI spawns when the cache is stale.\n\n"
        "[b]Adaptive (more often while jobs work)[/b]\n"
        "  While ANY tracked session is WORKING or SNOOZED, the Copilot fetch throttle\n"
        "  drops to copilot_usage_refresh_active_sec (default 300s, ~1/3 of idle) so the\n"
        "  card tracks reality more closely during active work. Set it to 0 to disable\n"
        "  the speed-up. (Claude & Codex need no speed-up: their sources already update\n"
        "  every few seconds while a job runs.)\n\n"
        "[b]Expand / collapse a card[/b]\n"
        "  t1 = Claude (private)   t3 = Codex   to = nixos supervised\n"
        "  t2 = Claude (work)      t4 = Copilot   ta = nixos tier_a\n"
        "  t5 = Codex (second login)\n"
        "  Collapsed keeps the card's titled top border (which names its own chord) and\n"
        "  drops the rest of the box. Unlike td/tf (view-local), these PERSIST to\n"
        "  config.toml. t2 on a machine with no `work` account says so instead of\n"
        "  toggling an empty box.\n\n"
        "[b]Config keys[/b] (~/.claude/command-center/config.toml)\n"
        "  usage_refresh_sec                  card re-read / render cadence (5.0)\n"
        "  copilot_usage_refresh_sec          idle Copilot gh-fetch throttle (900)\n"
        "  copilot_usage_refresh_active_sec   active-work Copilot throttle (300; 0=off)\n"
        "  copilot_usage / copilot_model      show the Copilot card / its title model\n"
        "  claude_usage                       fetch the Claude /usage OAuth endpoint (on)\n"
        "  claude_usage_refresh_sec           idle Claude OAuth-fetch throttle (600)\n"
        "  claude_usage_refresh_active_sec    active-work Claude throttle (200; 0=off)\n"
        "  codex_usage                        fetch the live chatgpt.com Codex endpoint\n"
        "  codex_usage_refresh_sec            idle live-Codex fetch throttle (600)\n"
        "  codex_usage_refresh_active_sec     active-work Codex throttle (200; 0=off)\n"
        "  codex_home_private                 second CODEX_HOME ('' = no second card)\n"
        "  usage_card_private/_work/_codex/_codex_private/_copilot   the t1..t5 toggles\n"
        "  card_nixos_overseer_supervised/_tier_a     the to / ta toggles\n"
        "  claude_accounts                    ['private=~/.claude', 'work=~/.claude-work']\n"
        "  claude_account_emails    identity hard-link, e.g. ['work=you@company.com'] —\n"
        "                           the card shows whichever configured account is\n"
        "                           CURRENTLY logged in as that email, even if a bare\n"
        "                           /login swapped which physical dir holds it. Empty\n"
        "                           (default) = no hard link, path-based as before.\n"
        "  subscription_ends        when each paid plan renews, so a card can advertise\n"
        "                           its own cancel-by date, e.g. ['claude_private=auto',\n"
        "                           'codex_private=2026-09-30'] -> the title gains\n"
        "                           ' -> 30.9' ('!' = already past). Cards:\n"
        "                           claude_private/claude_work/codex/codex_private.\n"
        "                           'auto' derives it (Claude's billing anniversary /\n"
        "                           the ChatGPT id_token claim, only as fresh as the\n"
        "                           last `codex login`). Empty (default) = no dates.\n\n"
        "[b]Freshness vs `claude` /usage[/b]\n"
        "  The Claude cards now track `claude`'s own /usage: `ccc claude-usage` fetches\n"
        "  each account's OAuth /usage endpoint (the daemon + a detached TUI spawn, both\n"
        "  throttled) and authoritatively replaces the snapshot. Between fetches the\n"
        "  status-line capture is the fast path (a working session refreshes it ~every\n"
        "  3s). The old gap — a card lagging because idle sessions only replay their last\n"
        "  API response's headers — is closed by the periodic authoritative fetch."
    ),
}


def _key_row(key: str, word: str, gloss: str) -> Text:
    """One selectable key row, styled like the footer: shortcut letter in gold."""
    out = _gold_mnemonic(word, key, "grey85")
    out.append(f"   {gloss}", style="grey58")
    return out


class WrappingListView(ListView):
    """A ``ListView`` whose ``↑/↓`` wrap around at the ends (modulo).

    Matches the session table's circular navigation: ``↓`` past the last item lands
    on the first and ``↑`` past the first lands on the last. Disabled items (e.g. the
    non-selectable section headers in the keys list) are skipped, exactly as Textual's
    default cursor does. The word-back/forward keys (Option+←/→ → ``ctrl+←/→``) jump
    three selectable items at a time, also wrapping — the same fast-jump as the table.
    (``Esc`` is left alone so a modal that binds it to close still does.)
    """

    _JUMP = 3  # selectable items moved by the word-back/forward fast jump
    _JUMP_DOWN = ("ctrl+left", "alt+left")
    _JUMP_UP = ("ctrl+right", "alt+right")

    def action_cursor_down(self) -> None:
        self._step(1)

    def action_cursor_up(self) -> None:
        self._step(-1)

    def on_key(self, event: events.Key) -> None:
        if event.key in self._JUMP_DOWN:
            event.prevent_default()
            event.stop()
            self._step(self._JUMP)
        elif event.key in self._JUMP_UP:
            event.prevent_default()
            event.stop()
            self._step(-self._JUMP)

    def _step(self, delta: int) -> None:
        """Move the highlight by *delta* selectable items, wrapping at the ends."""
        nodes = list(self._nodes)
        enabled = [i for i, item in enumerate(nodes) if not item.disabled]
        if not enabled:
            return
        # Derive the current index from highlighted_child (a cleanly-typed property)
        # rather than reading the `index` reactive — the latter trips mypy/pylint on the
        # descriptor's type through the subclass. Writing self.index is fine.
        child = self.highlighted_child
        current = nodes.index(child) if child is not None else None
        if current is None or current not in enabled:
            self.index = enabled[0] if delta > 0 else enabled[-1]
            return
        self.index = enabled[(enabled.index(current) + delta) % len(enabled)]


class TopicScreen(ModalScreen[None]):
    """A single help topic (Esc / q to close)."""

    BINDINGS = [Binding("escape", "close", show=False), Binding("q", "close", show=False)]
    DEFAULT_CSS = """
    TopicScreen { align: center middle; }
    #topic {
        width: 80%;
        max-width: 92;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #topic-title { margin-bottom: 1; }
    """

    def __init__(self, title: str | Text, body: str | Text) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        # A str title is rendered bold; a Text title (e.g. a gold key mnemonic) is
        # passed through verbatim so it looks exactly like it does in the main window.
        title = f"[b]{self._title}[/b]" if isinstance(self._title, str) else self._title
        with VerticalScroll(id="topic"):
            yield Static(title, id="topic-title")
            yield Static(self._body)

    def action_close(self) -> None:
        self.dismiss(None)


class KeysScreen(ModalScreen[None]):
    """Selectable list of every command / key; Enter explains one (Esc / q back).

    This is the "Commands & keys" topic: it turns the reference at the top of the
    help into a navigable list. Each row is rendered exactly like the footer (the
    shortcut letter in gold); picking one opens its full explanation.
    """

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("q", "close", show=False),
    ]
    DEFAULT_CSS = """
    KeysScreen { align: center middle; }
    #keys-box {
        width: 84%;
        max-width: 96;
        height: 85%;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #keys-intro { margin-bottom: 1; }
    #keys { height: 1fr; border: round $accent; }
    """

    def __init__(self) -> None:
        super().__init__()
        # (key, word, explanation) for each selectable row, indexed by the row's id.
        self._flat: list[tuple[str, str, str]] = [
            (cmd.key, cmd.word, cmd.explanation) for _, cmds in commands.sections() for cmd in cmds
        ]

    def compose(self) -> ComposeResult:
        items: list[ListItem] = []
        row = 0
        for section_title, cmds in commands.sections():
            # A disabled ListItem is a non-selectable section header (Textual's cursor skips it).
            items.append(ListItem(Label(Text(section_title, style="bold")), disabled=True))
            for cmd in cmds:
                items.append(
                    ListItem(Label(_key_row(cmd.key, cmd.word, cmd.gloss)), id=f"key-{row}")
                )
                row += 1
        with Vertical(id="keys-box"):
            yield Static(
                "[b]Commands & keys[/b]  (↑/↓ to a command · Enter explains it · Esc back)",
                id="keys-intro",
            )
            yield WrappingListView(*items, id="keys")

    def on_mount(self) -> None:
        self.query_one("#keys", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if item is None or not item.id or not item.id.startswith("key-"):
            return
        key, word, explanation = self._flat[int(item.id.split("-", 1)[1])]
        self.app.push_screen(TopicScreen(_gold_mnemonic(word, key, "bold"), explanation))

    def action_close(self) -> None:
        self.dismiss(None)


class _HelpTopics(WrappingListView):
    """Topics list whose PgUp/PgDn/Home/End scroll the sibling #help pane.

    A focused ListView would otherwise swallow those keys (it inherits scroll
    bindings from ScrollableContainer) and scroll itself — which does nothing, as
    the list is auto-height. Overriding them here lets the long reference scroll
    while ↑/↓ still move the topic selection.
    """

    BINDINGS = [
        Binding("pageup", "help_scroll('page_up')", show=False),
        Binding("pagedown", "help_scroll('page_down')", show=False),
        Binding("home", "help_scroll('home')", show=False),
        Binding("end", "help_scroll('end')", show=False),
    ]

    def action_help_scroll(self, what: str) -> None:
        box = self.screen.query_one("#help", VerticalScroll)
        getattr(box, f"scroll_{what}")()


class HelpScreen(ModalScreen[None]):
    """Help overlay: scrollable key reference + topics you open with Enter.

    The reference (#help) scrolls with PgUp/PgDn or Home/End; ↑/↓ move the topic
    selection and Enter opens it. The first topic, "Commands & keys", opens a
    KeysScreen that explains every shortcut one at a time. (Esc / q / ? to close.)
    """

    _KEYS_TOPIC = "Commands & keys — explain each ▸"

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("q", "close", show=False),
        Binding("question_mark", "close", show=False),
    ]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 90%;
        max-width: 96;
        height: 85%;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    #help { height: 1fr; }
    #topics-label { margin-top: 1; }
    #topics { height: auto; max-height: 45%; border: round $accent; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            with VerticalScroll(id="help"):
                yield Static(_help_body())
            yield Static(
                "[b]Topics[/b]  (↑/↓ select · Enter open · PgUp/PgDn or Home/End scroll above):",
                id="topics-label",
            )
            yield _HelpTopics(
                ListItem(Label(self._KEYS_TOPIC), id="topic-keys"),
                *(ListItem(Label(title), id=f"topic-{i}") for i, title in enumerate(_HELP_TOPICS)),
                id="topics",
            )

    def on_mount(self) -> None:
        self.query_one("#topics", _HelpTopics).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if item is None or not item.id:
            return
        if item.id == "topic-keys":
            self.app.push_screen(KeysScreen())
        elif item.id.startswith("topic-"):
            title = list(_HELP_TOPICS)[int(item.id.split("-", 1)[1])]
            body = _HELP_TOPICS[title]
            # Fill live/config values into any topic that asks for them.
            if "{daemon_state}" in body:
                body = body.replace("{daemon_state}", launchd.state_badge(launchd.is_loaded()))
            if "{launchd_label}" in body:
                body = body.replace("{launchd_label}", launchd.label())
            if "{copilot_title}" in body:
                body = body.replace("{copilot_title}", config.load_config().copilot_card_title)
            self.app.push_screen(TopicScreen(title, body))

    def action_close(self) -> None:
        self.dismiss(None)


class SessionTable(DataTable):
    """The session list, with two navigation modes.

    *Row mode* (default): ``↑/↓`` move the whole-row highlight, wrapping top↔bottom
    so the cursor never sticks at the first or last row. *Column mode*:
    pressing ``←``/``→`` drops a single-cell cursor onto the selected session and
    cycles it across the editable columns — ``/aim`` → ``/next-step`` → ``progress``,
    wrapping at either end — so ``Enter`` edits that one field (on the progress bar it
    sets/clears a manual percentage). ``↑/↓`` (or ``Esc``) leave column mode and
    resume whole-row selection on the adjacent row.

    Fast row jump: iTerm's "word back / word forward" keys (Option+←/→ in its
    natural-text-editing keymap) send ``Esc b`` / ``Esc f``, which Textual
    delivers as ``ctrl+left`` / ``ctrl+right`` (the CSI form ``\\x1b[1;3D`` arrives
    as ``alt+left``/``alt+right``). Back-a-word jumps three rows DOWN, forward-a-
    word three UP — the user's mapping — wrapping around at the top/bottom so it
    never sticks on the last row. An ``Esc``-then-bare-arrow path also covers
    terminals in "Esc+" Option mode.
    """

    # Table column index → the App action that edits it, in left-to-right order.
    # /deadline & /block are no longer table columns (edit them with the D/b keys).
    _EDIT_COLS: tuple[tuple[int, str], ...] = (
        (_AIM_COL, "action_edit_aim"),
        (_NEXT_COL, "action_edit_next"),
        (_PROGRESS_COL, "action_edit_progress"),
    )
    _ROW_JUMP = 3  # rows moved by the word-back/forward fast jump
    _CHORD_WINDOW = 0.05  # s; a bare arrow within this of an Esc is an Option+arrow
    # iTerm "word back/forward" (Option+←/→ → Esc-b/Esc-f) reach Textual as
    # ctrl+left/right; the CSI form arrives as alt+left/right. Back = down, fwd = up.
    _JUMP_DOWN = ("ctrl+left", "alt+left")
    _JUMP_UP = ("ctrl+right", "alt+right")

    _col_mode: bool = False
    _esc_at: float = -1.0  # monotonic() of the last Esc, for the ⌥-arrow chord

    class DraftModelsClicked(Message):
        """A DRAFT row's ``model`` (overseer ▸ executor) cell was clicked.

        Posted by :meth:`_on_click` so the App can open the inline model editor with
        the overseer Select focused. Non-draft rows / other columns never post this.
        """

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            super().__init__()

    @property
    def _cols(self) -> list[int]:
        return [col for col, _ in self._EDIT_COLS]

    def _row_key_at(self, row_index: int) -> str | None:
        """The row key (session id) at *row_index*, or None if it can't be resolved."""
        try:
            cell_key = self.coordinate_to_cell_key(Coordinate(row_index, _NEXT_COL))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return None
        return cell_key.row_key.value

    async def _on_click(self, event: events.Click) -> None:
        """Intercept a click on a DRAFT row's ``model`` cell → open the model editor.

        The cell shows the ``overseer ▸ executor`` pair, so clicking it should edit those
        models rather than select/launch the row. Only that column, and only on a draft,
        is intercepted (``event.stop()``); every other click falls through to the normal
        DataTable behaviour (row highlight / select), so row-highlight and jump are intact.
        """
        meta = event.style.meta
        if meta.get("column") == _MODEL_COL and meta.get("row", -1) >= 0:
            row_key = self._row_key_at(meta["row"])
            store = getattr(self.app, "store", None)
            if row_key and not row_key.startswith("__sep__") and store is not None:
                session = store.get(row_key)
                if session is not None and session.draft:
                    self.move_cursor(row=meta["row"])
                    self.post_message(self.DraftModelsClicked(row_key))
                    event.stop()
                    return
        await super()._on_click(event)

    def _enter_col(self, col: int) -> None:
        self._col_mode = True
        self.cursor_type = "cell"
        self.move_cursor(row=self.cursor_row, column=col)

    def _leave_col(self) -> None:
        if self._col_mode:
            self._col_mode = False
            self.cursor_type = "row"

    def _jump_rows(self, delta: int) -> None:
        self._leave_col()
        if self.row_count:  # circle past either end — % wraps for both +delta and -delta
            self.move_cursor(row=(self.cursor_row + delta) % self.row_count)

    def on_key(self, event: events.Key) -> None:
        key = event.key
        # Fast row jump: word-back → down, word-forward → up.
        if key in self._JUMP_DOWN:
            event.prevent_default()
            event.stop()
            self._jump_rows(self._ROW_JUMP)
            return
        if key in self._JUMP_UP:
            event.prevent_default()
            event.stop()
            self._jump_rows(-self._ROW_JUMP)
            return
        # Esc leaves column mode; it also arms the "Esc+"-option-mode fallback
        # (some terminals send Option+arrow as Esc then a bare ←/→).
        if key == "escape":
            self._esc_at = monotonic()
            event.prevent_default()
            event.stop()
            self._leave_col()
            return
        if key in ("left", "right") and monotonic() - self._esc_at < self._CHORD_WINDOW:
            self._esc_at = -1.0
            event.prevent_default()
            event.stop()
            self._jump_rows(self._ROW_JUMP if key == "left" else -self._ROW_JUMP)
        # Plain ←/→ fall through to action_cursor_left/right (the column cursor).

    def action_cursor_left(self) -> None:
        cols = self._cols
        if not self._col_mode:
            self._enter_col(cols[-1])  # enter from the right edge (progress)
            return
        idx = cols.index(self.cursor_column) if self.cursor_column in cols else 0
        self._enter_col(cols[(idx - 1) % len(cols)])  # wrap: /aim ← progress

    def action_cursor_right(self) -> None:
        cols = self._cols
        if not self._col_mode:
            self._enter_col(cols[0])  # enter from the left edge (/aim)
            return
        idx = cols.index(self.cursor_column) if self.cursor_column in cols else 0
        self._enter_col(cols[(idx + 1) % len(cols)])  # wrap: progress → /aim

    def action_cursor_up(self) -> None:
        self._jump_rows(-1)  # one row up, wrapping top → bottom (never sticks at row 0)

    def action_cursor_down(self) -> None:
        self._jump_rows(1)  # one row down, wrapping bottom → top (never sticks at the last row)

    def action_select_cursor(self) -> None:
        if not self._col_mode:
            super().action_select_cursor()
            return
        for col, action in self._EDIT_COLS:
            if self.cursor_column == col:
                getattr(self.app, action)()
                return

    def on_resize(self, _event: events.Resize) -> None:
        # Width changed → re-stretch /aim so progress stays pinned to the right edge.
        self.call_after_refresh(self.fit_aim_column)

    def fit_aim_column(self) -> None:
        """Stretch the ``/aim`` column to fill the leftover row width.

        Textual packs columns at their content width and never stretches, so the
        trailing ``progress`` column would otherwise float mid-table with dead space
        to its right. We size every other column to its content, then give all the
        slack to ``/aim`` (a fixed width that crops its text) so the row spans the
        full table and ``progress`` lands flush against the right edge.
        """
        cols = list(self.columns.values())
        if len(cols) <= _AIM_COL:
            return
        avail = self.scrollable_content_region.width
        if avail <= 0:
            return
        pad = 2 * self.cell_padding
        others = sum(c.get_render_width(self) for i, c in enumerate(cols) if i != _AIM_COL)
        target = max(_AIM_MIN_WIDTH, avail - others - pad)
        aim = cols[_AIM_COL]
        if aim.auto_width or aim.width != target:
            aim.auto_width = False
            aim.width = target
            self._require_update_dimensions = True
            self.refresh(layout=True)


class DetailHead(Static):
    """The read-only Status head of the detail pane; a Tab stop while editing.

    Outside edit mode this is a plain Static. ``action_edit_session`` flips
    ``can_focus`` on so Tab / ↑↓ can land here too — the focus tint marks it — giving
    the top of the pane (Status, Scheduled for, the full ``Prompt to run``) a
    reachable stop even when a long prompt pushes the edit fields below the fold.
    It stays read-only: every key except Tab/Shift+Tab is swallowed (↑/↓ move focus
    like the form's fields, Esc saves-and-exits) so a stray letter pressed on it can
    never fire a table or app action mid-edit.
    """

    can_focus = False  # flipped on only while inline edit mode is active

    def on_key(self, event: events.Key) -> None:
        if event.key in ("tab", "shift+tab"):
            return  # Screen's focus_next / focus_previous take these
        event.prevent_default()
        event.stop()
        if event.key == "escape":
            exit_edit = getattr(self.app, "action_exit_edit", None)
            if callable(exit_edit):
                exit_edit()
        elif event.key == "up":
            self.screen.focus_previous()
        elif event.key == "down":
            self.screen.focus_next()


def _build_bindings() -> list[Binding | tuple[str, str] | tuple[str, str, str]]:
    """Textual key bindings, generated from the command registry.

    Chord (``tf``) and nav-only (``↑/↓``, ``←/→``) commands have no plain binding
    and are handled in the App / SessionTable; everything else is wired here.
    """
    return [
        Binding(key, action, description, show=show)
        for key, action, description, show in commands.binding_specs()
    ]


class CommandCenterApp(App[None]):
    """The terminal command center."""

    TITLE = "Claude Command Center"
    CSS = """
    #sessions { width: 1fr; }
    #sessions > .datatable--cursor { background: $accent 35%; }
    #detail-wrap {
        width: 1fr; border-top: solid $accent; padding: 0 1;
        border-title-align: left;
    }
    #detail-top { height: auto; }
    #detail-left { width: 1fr; height: auto; }
    #detail-head { width: 1fr; height: auto; }
    /* Edit mode makes the head focusable (a read-only Tab stop on the Status line). */
    #detail-head:focus { background: $accent 20%; }
    #detail-fields-view { width: 1fr; height: auto; }
    #detail-bottom { width: 1fr; height: auto; }
    /* Inline editor: swaps in place of #detail-fields-view, rendering the SAME lines so
       the layout does not change. Everything is borderless + transparent (no boxes); the
       FOCUSED field is tinted — that tint is the edit cursor. AIM / prompt grow to fit. */
    #detail-edit { display: none; height: auto; width: 1fr; }
    #detail-edit .fieldrow { height: auto; width: 1fr; }
    #detail-edit .fieldlabel { width: auto; color: white; }
    #detail-edit Input { border: none; background: transparent; height: 1; padding: 0; width: 1fr; }
    #detail-edit Input:focus { background: $accent 30%; }
    /* Draft-only model dropdowns: clickable Selects that stay compact inline. */
    #detail-edit Select { width: 1fr; height: auto; }
    #detail-edit Select > SelectCurrent { border: none; background: transparent; padding: 0; }
    #detail-edit Select:focus > SelectCurrent { background: $accent 30%; }
    #detail-edit TextArea {
        border: none; background: transparent; height: auto; padding: 0; width: 1fr;
    }
    #detail-edit TextArea:focus { background: $accent 30%; }
    #detail-edit Button {
        border: none; background: transparent; height: 1; min-width: 0; padding: 0;
    }
    #detail-edit Button:focus { background: $accent 30%; }
    #detail-edit #edit-important { color: white; }
    #usage-col { width: auto; height: auto; }
    #usage { width: auto; min-width: 38; height: auto; padding: 0 1; border: round $accent; }
    #usage-work {
        width: auto; min-width: 38; height: auto; padding: 0 1;
        border: round #6cb6ff;
    }
    #usage-codex {
        width: auto; min-width: 38; height: auto; padding: 0 1;
        border: round #19c37d;
    }
    /* The second ChatGPT login's card — same product, so the same green box; the two
       are told apart by the account e-mail in their border titles. */
    #usage-codex-private {
        width: auto; min-width: 38; height: auto; padding: 0 1;
        border: round #19c37d;
    }
    #usage-copilot {
        width: auto; min-width: 38; height: auto; padding: 0 1;
        border: round #a371f7;
    }
    #usage-nixos-supervised {
        width: auto; min-width: 38; height: auto; padding: 0 1;
        border: round #ff8700;
    }
    #usage-nixos-tier-a {
        width: auto; min-width: 38; height: auto; padding: 0 1;
        border: round #2bb2b2;
    }
    /* A card whose `t…` gate is off (see _set_card_expanded): keep the titled
       border-top row, drop the rest of the box. `!important` because the per-card
       `#usage…` id rules out-specify a plain class selector. The WIDTH is pinned
       inline there (to the title), not here — each card keeps its own min-width. */
    .card-collapsed { height: 1 !important; border-bottom: none !important; }
    #keyhints { dock: bottom; height: 1; background: $panel; }
    """
    BINDINGS = _build_bindings()

    def __init__(self) -> None:
        super().__init__()
        self.store: Store | None = None
        self.adapter = ClaudeAdapter()
        self.cfg = config.load_config()
        # The configured tab title (default "!!!") so this tab stands out; the app
        # name lives in the sub-title for context.
        self.title = self.cfg.tab_title or "Claude Command Center"
        self.sub_title = "Claude Command Center" if self.cfg.tab_title else ""
        self._current: str | None = None
        self._rows: dict[str, Row] = {}
        # Account map cached per render tick (see _apply_rows) → the model column's home marker.
        self._account_dirs: dict[str, Path] = {}
        # …and the identity-corrected marker map derived from it (resolved dir → 🏠/💼/blank).
        # Empty until the first _apply_rows, which suppresses the marker (single-account rule).
        self._account_markers: dict[str, str] = {}
        self._sep_seq = 0  # monotonic counter giving each header/separator row a unique key
        # Last time we spawned a detached `ccc claude-usage` warmer (monotonic seconds); an
        # in-process throttle so a failing OAuth fetch can't respawn on every render tick.
        self._last_claude_usage_spawn = 0.0
        self._last_codex_usage_spawn = 0.0
        # The raw highlighted row key (incl. separators) and a map of category-splitter
        # keys → category name, so `fn` (new_job) knows which category header it is on.
        self._highlight_key: str | None = None
        self._sep_category: dict[str, str] = {}
        # Category-divider rows that _fit_rule_separators paints as a full-width blue
        # rule once the real column widths are known: sep row key → category name.
        self._rule_seps: dict[str, str] = {}
        self._show_finished = False  # DONE (green) hidden by default; toggled by `td`
        self._show_future = True  # FUTURE jobs (blue) shown by default; toggled by `tf`
        # Leader-chord state (`td`/`tf` = toggles, `ah` = aim-history): the pending leader
        # key is held until the next key (see _CHORDS / on_key). None when no chord in flight.
        self._chord_pending: str | None = None
        self._last_leader: str | None = None
        self._chord_timer: Timer | None = None
        # Inline edit-mode state (the `e` key): selected session + original field values.
        self._editing = False
        self._edit_sid: str | None = None
        self._edit_original: dict[str, str] = {}
        # Pending /depends-on value chosen in the picker (committed by _commit_edit):
        # "" = clear, else the dependency's full session UUID. None until edit mode opens.
        self._edit_depends_pending: str | None = None
        # Last pushed (iterm_session_id, badge) mapping, so _sync_tab_badges only
        # spawns AppleScript when the badge↔tab mapping actually moves (not every 5 s).
        self._last_badge_sig: tuple[tuple[str, str | None], ...] | None = None
        # Warm iTerm2 API link backing the resident f+j toggle (see iterm_api). Created
        # unconditionally so attribute access is always safe; only *pre-warmed* (a real
        # websocket) under iTerm — on_mount gates the connect on $ITERM_SESSION_ID.
        self._iterm_link = iterm_api.ItermLink()
        # Undo stack (the `u` key): most recent last, capped at _UNDO_MAX, this run only.
        self._undo_stack: list[_UndoEntry] = []
        self._undoing = False  # True while action_undo runs an entry — suppresses re-push
        # Set by the fast poll when `ccc restart-tui` asks us to restart: run() re-execs
        # the process in place (same tab) once the app has exited and the terminal restored.
        self.restart_requested = False

    # ---- layout (top: table, bottom: detail) ----------------------------
    def compose(self) -> ComposeResult:
        # No textual Header bar — the table (with its `head:`-labelled header line)
        # is the topmost widget, so the screen opens straight onto the sessions.
        yield SessionTable(id="sessions", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="detail-wrap"):
            with Horizontal(id="detail-top"):
                # Left column: read-only context (always shown), then the editable
                # field lines — rendered read-only in #detail-fields-view, or, in edit
                # mode, swapped IN PLACE for the inline editor (#detail-edit). The
                # context above never moves, so `e` edits the lines where they sit.
                with Vertical(id="detail-left"):
                    yield DetailHead("", id="detail-head")
                    yield Static("", id="detail-fields-view")
                    yield EditForm(id="detail-edit")
                    # Todos / summary / flags and the sub-goal checklist sit at the
                    # very bottom, below the editable field lines.
                    yield Static("", id="detail-bottom")
                # Stacked, border-titled usage cards: Claude (private) on top,
                # Claude (work), Codex (one card per configured ChatGPT
                # login), then GitHub Copilot (each a distinct border + figure colour).
                # The two Claude cards share the periwinkle fill (same product);
                # private = gold, work = blue.
                with Vertical(id="usage-col"):
                    yield Static("", id="usage")
                    yield Static("", id="usage-work")
                    yield Static("", id="usage-codex")
                    yield Static("", id="usage-codex-private")
                    yield Static("", id="usage-copilot")
                    # Two read-only cards fed by the EXTERNAL homelab overseer daemon
                    # (a separate project): supervised = incidents awaiting the human
                    # (orange border), tier_a = recent automatic activity (teal border,
                    # hidden by default). Both toggle via the `to`/`ta` chords.
                    yield Static("", id="usage-nixos-supervised")
                    yield Static("", id="usage-nixos-tier-a")
        yield Static(id="keyhints")

    @property
    def _aim_first(self) -> bool:
        """Whether the ``/aim`` column (and the job labels built from it) shows revision (1).

        A property, not a cached flag: ``action_settings`` reloads ``self.cfg`` in place.
        """
        return aim_column_first(self.cfg.aim_column)

    def on_mount(self) -> None:
        self.store = Store(check_same_thread=False)
        table = self.query_one("#sessions", DataTable)
        table.cell_padding = 0  # minimise the gaps between status / ! / folder
        # `/aim (1)` names what the column shows when it renders the ORIGINAL AIM.
        aim_suffix = " (1)" if self._aim_first else ""
        table.add_columns(
            *[
                _header_text(lead, word, aim_suffix if word == "/aim" else "")
                for lead, word in _HEADERS
            ]
        )
        # Pin /aim to a fixed width up front (it is stretched to fill in fit_aim_column);
        # this stops the long, capped aim text from ballooning the column for one frame.
        aim_col = list(table.columns.values())[_AIM_COL]
        aim_col.auto_width = False
        aim_col.width = 38
        self.query_one("#keyhints", Static).update(_keyhints_text())
        # Name the detail pane on its top divider (mirrors the footer's `keys:` and
        # the header line's `head:`), so the bottom half can be referred to by name.
        self.query_one("#detail-wrap").border_title = (
            f"[white] job details ([{_GOLD}]e[/{_GOLD}] to edit then: Tab or "
            "↑/↓ to jump between items): [/white]"
        )
        # Border titles distinguish the four stacked usage cards. The two Claude cards
        # name their account (the refresh cadence is no longer in the title) and carry
        # the same 🏠/💼 glyph as the statusline (accounts.card_glyph) so the two
        # surfaces read as the same convention; the Copilot card names the model it
        # delegates to (the `copilot_model` config). Each title ends with its
        # show/hide chord (" / t1" …) so the toggle is discoverable on the card
        # itself; keys come from commands.by_action, never hard-coded here. The Claude
        # and Codex titles are then REBUILT on every render tick (an account or a
        # subscription date can move under a running TUI); the Copilot one cannot move.
        self._set_claude_card_titles()
        self._set_codex_card_titles()
        self.query_one("#usage-copilot", Static).border_title = (
            f"{self.cfg.copilot_card_title} {self.cfg.copilot_model}"
            f" / {commands.by_action('toggle_card_copilot').key}"
        )
        self.query_one(
            "#usage-nixos-supervised", Static
        ).border_title = f"nixos overseer supervised / {_NIXOS_SUPERVISED_CHORD}"
        self.query_one(
            "#usage-nixos-tier-a", Static
        ).border_title = f"nixos overseer tier_a / {_NIXOS_TIER_A_CHORD}"
        self._apply_split()
        self.refresh_data()
        self.set_interval(self.cfg.usage_refresh_sec, self.refresh_data)
        # Publish this TUI's identity so `ccc jump` hands us the whole f+j toggle (the
        # fast path — see jump / iterm_api). Clear any stale toggle first: one left by a
        # dead TUI must not fire on startup.
        jumpstate.set_tui(os.getpid(), os.environ.get("ITERM_SESSION_ID", ""))
        jumpstate.clear_toggle()
        # Drop any leftover restart request too: a file left by a crashed/killed TUI (or by
        # the previous instance's own restart) must NOT instantly re-restart this fresh one.
        jumpstate.clear_restart()
        # Pre-warm the iTerm2 API websocket so the first f+j is already sub-ms — but only
        # under iTerm (a real $ITERM_SESSION_ID). Headless tests / Linux / tmux must never
        # open the socket, so the gate is the env var, not a try/except.
        if os.environ.get("ITERM_SESSION_ID"):
            self.run_worker(
                self._iterm_link.ensure(), group="iterm-link", description="warm iTerm2 API link"
            )
        # Fast, cheap poll for `ccc jump` signals (the f+j toggle): the whole-toggle verb
        # and the cursor-move request, so a jump lands near-instantly, not after 5 s.
        self.set_interval(_JUMP_POLL_SEC, self._poll_jump_request)

    def on_unmount(self) -> None:
        # Retract the identity so `ccc jump` stops handing this (now gone) TUI the toggle.
        try:
            jumpstate.clear_tui()
        except OSError:
            pass

    def _apply_split(self) -> None:
        top = max(5, min(95, round(self.cfg.split_ratio * 100)))
        self.query_one("#sessions").styles.height = f"{top}%"
        self.query_one("#detail-wrap").styles.height = f"{100 - top}%"

    # ---- data -----------------------------------------------------------
    def refresh_data(self) -> None:
        """Schedule an off-loop rebuild of the session rows (coalesced).

        build_rows() (reconcile + transcript scans) can take hundreds of ms, so it
        runs in a thread worker; the widget updates land back on the UI thread via
        _apply_rows. exclusive=True coalesces bursts: a newer refresh cancels the
        pending one, whose stale rows are then discarded (see _refresh_worker).
        """
        if self._editing:
            return
        self.run_worker(
            self._refresh_worker,
            thread=True,
            exclusive=True,
            group="data-refresh",
            description="rebuild session rows",
        )

    def _refresh_worker(self) -> None:
        # Worker thread, with its OWN per-run Store (~14 ms): sharing the UI thread's
        # connection across threads intermittently dies with InterfaceError('bad
        # parameter or other API misuse') — pysqlite does not reliably serialize
        # cross-thread use of ONE connection, whatever sqlite3.threadsafety claims.
        # Concurrent *connections* are safe (WAL + busy_timeout), including against a
        # superseded refresh worker that is still mid-build when the next one starts.
        if self.store is None:
            return  # not mounted yet / shutting down
        with Store() as store:
            rows = build_rows(
                store,
                self.adapter,
                include_done=self._show_finished,
                done_max_age_days=self.cfg.done_max_age_days,
                folder_order=tuple(self.cfg.folder_order),
                include_future=self._show_future,
            )
            self._sync_tab_badges(rows, store)  # AppleScript spawn — belongs off-loop too
        if get_current_worker().is_cancelled:
            return  # superseded by a newer refresh — let that one repaint
        self.call_from_thread(self._apply_rows, rows)

    def _apply_rows(self, rows: list[Row]) -> None:
        # UI thread: everything the old refresh_data did AFTER build_rows, minus
        # _sync_tab_badges (now done in the worker).
        if self._editing:  # edit mode opened while the worker ran — don't clobber it
            return
        table = self.query_one("#sessions", DataTable)
        previous = self._current
        table.clear()
        # Resolve the account map once per render — drives the model column's home-icon
        # marker (see _add_session_row). One cheap read per tick, like the detail pane.
        self._account_dirs = config.claude_config_dirs()
        # Identity-correct those markers ONCE per render, never per row: each account costs
        # one .claude.json read, so a drifted login shows the TRUE billing glyph on every row
        # (accounts.effective_home_markers) without re-reading it per session.
        self._account_markers = accounts.effective_home_markers(self._account_dirs)
        # One resume-queue snapshot per rebuild — drives the halted rows' ||▶-vs-||
        # icon (will_auto_resume) without a queue-file read per row.
        self._resume_queue = (
            resume.load_state()
            if any(r.status is Status.HALTED for r in rows)
            else resume.QueueState()
        )
        # Two open tabs resolving to the SAME id-chip colour (typically the same repo) defeat
        # the colour, so all but one are reassigned an unused one BEFORE the rows are drawn —
        # the row, the tab and the status line then all read the same rewritten cache. A few
        # small file reads per rebuild; it writes only on a real collision. Best-effort: a
        # dedupe failure must never cost a repaint.
        try:
            tabcolor.dedupe_live([r.session for r in rows if r.is_open])
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass
        self._rows = {r.session.session_id: r for r in rows}
        self._sep_seq = 0
        self._sep_category = {}
        self._rule_seps = {}
        current_category: str | None = None
        finished_started = False
        future_started = False
        scheduled_started = False
        has_future = any(row.is_draft and scheduled_date(row.session) is None for row in rows)
        for row in rows:
            if row.dep_depth > 0:
                # Hoisted dependent: render it immediately, continuing whatever section its
                # (already-emitted) parent opened — BEFORE the draft/finished/scheduled/
                # category branches, so a hoisted FUTURE draft never triggers the FUTURE
                # separator mid-active-section (nor a stray category splitter).
                self._add_session_row(table, row, indent_repo=not (row.is_draft or row.is_finished))
                continue
            if row.is_draft and scheduled_date(row.session) is not None:
                # SCHEDULED — future jobs with a FIXED start date, the very bottom block
                # (they sort after FINISHED). Keep the FUTURE divider above it so the
                # `tf` toggle always has its line even with zero undated drafts.
                if self._show_future and not future_started:
                    future_started = True
                    self._add_future_separator(table, has_jobs=has_future)
                if not scheduled_started:
                    scheduled_started = True
                    self._add_scheduled_separator(table)
                self._add_session_row(table, row, indent_repo=False)
                continue
            if row.is_finished:  # done AND closed — only these sink to FINISHED
                # The FUTURE divider always precedes FINISHED while future is toggled
                # on, even with zero future jobs, so `tf` always has a line to toggle.
                if self._show_future and not future_started:
                    future_started = True
                    self._add_future_separator(table, has_jobs=has_future)
                if not finished_started:
                    finished_started = True
                    self._add_finished_separator(table)
                self._add_session_row(table, row, indent_repo=False)
                continue
            if row.is_draft:  # not-yet-started future jobs — their own FUTURE block
                if not future_started:
                    future_started = True
                    self._add_future_separator(table, has_jobs=True)
                self._add_session_row(table, row, indent_repo=False)
                continue
            # One header per repo category; rows are sorted so a category is one
            # contiguous block (AIM-first within it), so it can never recur.
            category, _leaf = colors.folder_split(row.session.cwd, repos.repo_root(self.cfg))
            if category != current_category:
                current_category = category
                self._add_category_splitter(table, category)
            self._add_session_row(table, row, indent_repo=True)
        # No drafts and no FINISHED block above to anchor it → still surface an empty
        # FUTURE line at the bottom so `tf` always toggles at least this one line.
        if self._show_future and not future_started:
            self._add_future_separator(table, has_jobs=has_future)
        # Keep selection if possible, else land on the first real session.
        target = (
            previous if previous in self._rows else (rows[0].session.session_id if rows else None)
        )
        if target:
            try:
                table.move_cursor(row=table.get_row_index(target))
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                pass
        self.update_detail()
        self._update_usage()
        # Re-stretch /aim now that column content widths are known (deferred until the
        # table has laid out, so the other columns report their final content widths).
        if isinstance(table, SessionTable):
            table.call_after_refresh(table.fit_aim_column)
            # Paint the category dividers as full-width blue rules now that the per-
            # column render widths are settled (same deferral as fit_aim_column).
            table.call_after_refresh(self._fit_rule_separators)

    def _sync_tab_badges(self, rows: list[Row], store: Store) -> None:
        """Re-push each live tab's iTerm title so its badge matches the row we just drew.

        The folder column and the status line both read the badge straight from the
        ``~/.cache/iterm-tab-symbol`` cache (the source of truth); the iTerm tab title
        is a *pushed* copy set via AppleScript on ``cd`` / ``SessionStart`` / each
        daemon pass. When no daemon is loaded those pushes go stale and the tab badge
        drifts from the row — the "tab symbols disagree" bug. Re-converging here keeps
        the two in lock-step whenever the TUI is open (the surface where the user
        compares them), even with no daemon. Gated on a change signature so AppleScript
        is spawned only when the badge↔tab mapping actually moves, not every refresh.

        Runs on the refresh worker thread — *store* is the worker's own per-run
        connection (never ``self.store``, see :meth:`_refresh_worker`).
        """
        if not self.cfg.sync_tab_titles:
            return
        sig = tuple(
            (r.session.iterm_session_id, tabsymbol.read(r.session.iterm_session_id))
            for r in rows
            if r.session.iterm_session_id and not r.session.done
        )
        if sig == self._last_badge_sig:
            return
        self._last_badge_sig = sig
        try:
            tabsymbol.sync_live(store)
        except OSError:
            pass

    def _update_usage(self) -> None:  # pylint: disable=too-many-locals,too-many-statements
        """Refresh every account-usage card (top-right of the detail pane).

        Account-global and independent of the selected row; reset times are relative,
        so re-rendering each ``usage_refresh_sec`` tick makes them count down. The two
        Claude cards read their per-account snapshot (``read_usage`` / ``read_usage
        ("work")``, private gold vs work blue accent) — WHICH account label backs each
        card is first resolved by :func:`accounts.resolve_card_label` (a no-op unless
        ``claude_account_emails`` hard-links that card to an email); each Codex card is
        a cheap cached read (the live ``wham/usage`` snapshot or that home's newest
        session rollout, whichever is newer — ``read_codex_usage``); Copilot's is the
        cached ``gh`` figure (``read_copilot_usage``) — all reads are cheap. Each card's
        visibility follows its own ``usage_card_*`` render gate. Only the Copilot and
        Codex *fetches* hit the network, gated separately on ``copilot_usage`` /
        ``codex_usage`` and throttled (adaptively: tighter while a job works).
        """
        try:
            private_panel = self.query_one("#usage", Static)
            work_panel = self.query_one("#usage-work", Static)
            codex_panel = self.query_one("#usage-codex", Static)
            codex_private_panel = self.query_one("#usage-codex-private", Static)
            copilot_panel = self.query_one("#usage-copilot", Static)
            nixos_supervised_panel = self.query_one("#usage-nixos-supervised", Static)
            nixos_tier_a_panel = self.query_one("#usage-nixos-tier-a", Static)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return
        # Render all four cards from their (cheap) caches; the Copilot cache read is
        # cheap too — only its FETCH hits the network (gated separately below).
        # accounts.resolve_card_label swaps in whichever configured account's cache
        # actually matches this card's hard-linked email (claude_account_emails), so a
        # drifted CLAUDE_CONFIG_DIR login never mislabels which account's numbers show
        # under "Claude (private)" vs "(work)". A label with no hard link
        # configured passes through unchanged (today's pure path-based behaviour); one
        # WITH a hard link but no current match resolves to None → the card renders
        # empty rather than a stale/wrong-account guess.
        private_label = accounts.resolve_card_label("private")
        work_label = accounts.resolve_card_label("work")
        private_panel.update(
            usage.render_usage(usage.read_usage(private_label) if private_label else None)
        )
        work_panel.update(
            usage.render_work_usage(usage.read_usage(work_label) if work_label else None)
        )
        codex_panel.update(usage.render_codex_usage(usage.read_codex_usage()))
        # The second ChatGPT login (codex_home_private) is read the same way, just from
        # its own CODEX_HOME; unconfigured, the card is not drawn at all (see below).
        codex_private_home = self._codex_private_home()
        if codex_private_home is not None:
            codex_private_panel.update(
                usage.render_codex_usage(usage.read_codex_usage(home=codex_private_home))
            )
        # Both Codex titles carry their account's e-mail, and a `codex login` can swap
        # it, so they are rebuilt every tick — the lookup is mtime-cached, hence cheap.
        # All four titles can also carry a subscription-end date that rolls at midnight
        # (or lands when an out-of-band profile fetch returns), so the Claude pair is
        # rebuilt here too.
        self._set_claude_card_titles()
        self._set_codex_card_titles()
        copilot_panel.update(usage.render_copilot_usage(usage.read_copilot_usage()))
        # The two nixos-overseer cards read an EXTERNAL sqlite DB read-only; each read is
        # one cheap query and NEVER raises (a sentinel → placeholder on any failure), so
        # it is safe on this render tick. A "" nixos_overseer_dir short-circuits before
        # any disk touch.
        nixos_supervised = nixos_overseer.read_supervised(self.cfg)
        nixos_supervised_panel.update(nixos_overseer.render_supervised(nixos_supervised))
        nixos_supervised_panel.border_title = (
            f"{nixos_overseer.card_title(nixos_supervised, 'nixos overseer supervised')}"
            f" / {_NIXOS_SUPERVISED_CHORD}"
        )
        nixos_tier_a = nixos_overseer.read_tier_a(self.cfg)
        nixos_tier_a_panel.update(nixos_overseer.render_tier_a(nixos_tier_a))
        nixos_tier_a_panel.border_title = (
            f"{nixos_overseer.card_title(nixos_tier_a, 'nixos overseer tier_a')}"
            f" / {_NIXOS_TIER_A_CHORD}"
        )
        # Render gates: each card is expanded/collapsed by its own config flag — a card
        # that is off keeps its titled border-top line and drops the rest of the box (see
        # _set_card_expanded). The Copilot RENDER gate is `usage_card_copilot` ALONE; its
        # network FETCH stays gated on `copilot_usage` ALONE (below), so the two are
        # independently settable by hand.
        _set_card_expanded(private_panel, self.cfg.usage_card_private)
        # A machine with no `work` account configured must not show a permanently empty
        # work card — not even a collapsed title line — so that one is gated on `visible`
        # and disappears entirely. Parsed from the already-loaded Config: no file read.
        _set_card_expanded(work_panel, self.cfg.usage_card_work, visible=self._has_work_account())
        _set_card_expanded(codex_panel, self.cfg.usage_card_codex)
        # …and, like the work card, the second Codex card disappears outright (title line
        # included) on a machine with no codex_home_private — there is nothing to show.
        _set_card_expanded(
            codex_private_panel,
            self.cfg.usage_card_codex_private,
            visible=self._has_codex_private(),
        )
        _set_card_expanded(copilot_panel, self.cfg.usage_card_copilot)
        _set_card_expanded(nixos_supervised_panel, self.cfg.card_nixos_overseer_supervised)
        _set_card_expanded(nixos_tier_a_panel, self.cfg.card_nixos_overseer_tier_a)
        if self.cfg.copilot_usage:
            # Keep the (network-sourced) figure warm without blocking render: when the
            # cache is stale, fire a detached `ccc copilot-usage` to refresh it for the
            # next tick. The daemon does the same when no TUI is open. The staleness
            # threshold tightens (copilot_usage_refresh_active_sec) while any job is
            # actively working so the card tracks reality more closely during active work.
            active = usage.has_active_work(r.status.value for r in self._rows.values())
            throttle = usage.adaptive_interval(
                self.cfg.copilot_usage_refresh_sec,
                self.cfg.copilot_usage_refresh_active_sec,
                active=active,
            )
            if usage.copilot_usage_stale(throttle):
                from .. import spawn  # pylint: disable=import-outside-toplevel

                spawn.spawn_ccc(["copilot-usage"])
        if self.cfg.claude_usage:
            # Keep the (OAuth-sourced) Claude cards warm the same way: when ANY account's
            # snapshot is stale per the adaptive throttle, fire ONE detached
            # `ccc claude-usage` (fetches every account) to refresh for the next tick. The
            # daemon does the same when no TUI is open. An in-process min-interval guard
            # (>=_CLAUDE_USAGE_SPAWN_MIN_SEC) stops a persistently-failing fetch (e.g. no
            # keychain in this env, so oauth_fetched_at never advances) from respawning
            # every 5s tick — the file-mtime throttle the Copilot block relies on does not
            # protect us here since a failed fetch writes nothing.
            active = usage.has_active_work(r.status.value for r in self._rows.values())
            throttle = usage.adaptive_interval(
                self.cfg.claude_usage_refresh_sec,
                self.cfg.claude_usage_refresh_active_sec,
                active=active,
            )
            account_dirs = config.parse_claude_accounts(self.cfg.claude_accounts)
            stale = any(usage.claude_usage_stale(label, throttle) for label in account_dirs)
            if (
                stale
                and (monotonic() - self._last_claude_usage_spawn) >= _CLAUDE_USAGE_SPAWN_MIN_SEC
            ):
                from .. import spawn  # pylint: disable=import-outside-toplevel

                spawn.spawn_ccc(["claude-usage"])
                self._last_claude_usage_spawn = monotonic()
        if self.cfg.codex_usage:
            # Same shape for the live Codex endpoint: when ANY configured CODEX_HOME's
            # cache is stale per the adaptive throttle, fire ONE detached `ccc codex-usage`
            # (it fetches every home). The same in-process guard applies — a fetch that
            # keeps failing (an expired token no `codex` run has refreshed) writes nothing,
            # so its file mtime never advances and the staleness stays true forever.
            active = usage.has_active_work(r.status.value for r in self._rows.values())
            throttle = usage.adaptive_interval(
                self.cfg.codex_usage_refresh_sec,
                self.cfg.codex_usage_refresh_active_sec,
                active=active,
            )
            stale = any(
                usage.codex_usage_stale(home, throttle) for home in self._codex_homes().values()
            )
            if (
                stale
                and (monotonic() - self._last_codex_usage_spawn) >= _CLAUDE_USAGE_SPAWN_MIN_SEC
            ):
                from .. import spawn  # pylint: disable=import-outside-toplevel

                spawn.spawn_ccc(["codex-usage"])
                self._last_codex_usage_spawn = monotonic()

    def _next_sep_key(self) -> str:
        """Unique key for a non-selectable separator/header row (``__sep__`` prefix)."""
        self._sep_seq += 1
        return f"__sep__{self._sep_seq}"

    def _add_finished_separator(self, table: DataTable) -> None:
        """The single ``── done ──`` header (the ``d`` gilded gold) above the done block.

        Hosted in the ``folder`` column (like ``_add_category_splitter``), NOT the narrow
        ``ver`` column: a long divider in ``ver`` auto-widens it (DataTable has no colspan,
        so a banner lives inside one column's width). ``folder`` is already wide and aligns
        the divider with the category headers.
        """
        cells = [Text("") for _ in _HEADERS]
        dim = "bold grey42"
        label = Text("── ", style=dim)
        label.append_text(_gold_mnemonic(_GROUP_LABEL[Status.DONE], "d", dim))
        label.append(" ──", style=dim)
        cells[_FOLDER_COL] = label
        table.add_row(*cells, key=self._next_sep_key())

    def _add_future_separator(self, table: DataTable, has_jobs: bool = True) -> None:
        """The single ``FUTURE`` header above the flat not-yet-started job block.

        In the ``folder`` column (see ``_add_finished_separator``) so it aligns with the
        category headers and keeps the ``ver`` column narrow. Shown even when there are
        no future jobs (``has_jobs=False`` → the hint points at ``fn`` to add one) so the
        ``tf`` toggle always has at least this line to show/hide. Registered in
        ``_rule_seps`` so ``_fit_rule_separators`` repaints it as a full-width blue
        rule — same format as the category dividers (dashes from the very left to the
        right edge of the screen).
        """
        cells = [Text("") for _ in _HEADERS]
        hint = "(r / Enter launches)" if has_jobs else "(fn adds one)"
        # Pre-layout fallback = the bare word only (like the category splitters): the
        # full label is painted across ALL columns post-layout (update_width=False),
        # so the long hint never widens the folder column.
        cells[_FOLDER_COL] = Text(_FUTURE_LABEL, style=f"bold {_DRAFT_BLUE}")
        key = self._next_sep_key()
        self._rule_seps[key] = f"{_FUTURE_LABEL}  {hint}"
        table.add_row(*cells, key=key)

    def _add_scheduled_separator(self, table: DataTable) -> None:
        """The ``SCHEDULED`` header above future jobs with a FIXED start date.

        The very bottom block (these drafts sort below FINISHED). Rendered as a
        full-width blue rule via ``_rule_seps`` like the category dividers and
        FUTURE. Only shown when at least one dated job exists.
        """
        cells = [Text("") for _ in _HEADERS]
        # Bare word as the pre-layout fallback (see _add_future_separator) — the full
        # hint is painted across all columns post-layout, never widening folder.
        cells[_FOLDER_COL] = Text(_SCHEDULED_LABEL, style=f"bold {_DRAFT_BLUE}")
        key = self._next_sep_key()
        self._rule_seps[key] = f"{_SCHEDULED_LABEL}  (fixed start date · r / Enter asks first)"
        table.add_row(*cells, key=key)

    def _add_category_splitter(self, table: DataTable, category: str) -> None:
        """A full-line header naming one repo category; its repos nest beneath it.

        The header's row key is recorded in ``_sep_category`` so the ``fn`` (new_job)
        chord, fired while the cursor sits on a category header, knows which category
        to open the repo picker for, and in ``_rule_seps`` so ``_fit_rule_separators``
        repaints it as a full-width blue rule (``──── private ────``) once the real
        column widths are known. The plain blue word here is the pre-layout fallback.
        """
        cells = [Text("") for _ in _HEADERS]
        cells[_FOLDER_COL] = Text(category, style=f"bold {_DRAFT_BLUE}")
        key = self._next_sep_key()
        self._sep_category[key] = category
        self._rule_seps[key] = category
        table.add_row(*cells, key=key)

    def _fit_rule_separators(self) -> None:
        """Repaint each registered divider (category / FUTURE / SCHEDULED) as a
        full-width blue rule with its name starting exactly at the folder column
        (``──────── private ────────────``): dashes from the very left edge all
        the way to the right edge of the screen.

        Deferred until after layout (like ``fit_aim_column``) so each column's real
        render width is known. ``cell_padding`` is 0, so the columns abut: a single
        rule string is built spanning ALL columns — dashes, a gap, the name at the
        folder edge, a gap, trailing dashes to the table's right edge — then sliced
        per column so the pieces join into one continuous line. The name and dashes
        share the FUTURE blue (``_DRAFT_BLUE``).
        """
        if not self._rule_seps:
            return
        table = self.query_one("#sessions", SessionTable)
        cols = list(table.columns.values())
        if len(cols) <= _FOLDER_COL:
            return
        widths = [c.get_render_width(table) for c in cols]
        left = sum(widths[:_FOLDER_COL])  # display width of the columns before folder
        span = sum(widths)  # full table width — the rule runs to the right edge
        style = f"bold {_DRAFT_BLUE}"
        for key, word in self._rule_seps.items():
            try:
                row = table.get_row_index(key)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                continue  # row gone (cleared mid-refresh) — skip it
            lead = ("─" * (left - 1) + " ") if left else ""  # gap before the name
            after = span - len(lead) - len(word)  # cells remaining right of the name
            tail = (" " + "─" * (after - 1)) if after >= 1 else ""  # gap then dashes
            full = (lead + word + tail)[:span].ljust(span)
            start = 0
            for col in range(len(cols)):  # slice the rule across every column
                width = widths[col]
                table.update_cell_at(
                    Coordinate(row, col),
                    Text(full[start : start + width], style=style),
                    update_width=False,
                )
                start += width

    def _add_session_row(self, table: DataTable, row: Row, indent_repo: bool = True) -> None:
        session = row.session
        root = repos.repo_root(self.cfg)
        done = row.status is Status.DONE
        base = _DONE_STYLE if done else _STATUS_STYLE.get(row.status, "white")
        live = row.live
        if session.draft:
            icon_glyph, icon_style = "✎", _DRAFT_BLUE  # not-yet-started future job
        elif done:
            # ✓ — but only once the session stops being busy: derive_status keeps a
            # done-flagged session WORKING (▶) while it is still mid-turn, so a busy
            # done row never reaches this branch.
            icon_glyph, icon_style = STATUS_ICON[Status.DONE], base
        elif live is not None and live.alive and self.adapter.has_subagent(live.pid):
            # A subagent is running: the session is active (working, or awaiting another
            # agent) → show the green ▶ "running" play icon, same as a busy session.
            icon_glyph, icon_style = STATUS_ICON[Status.WORKING], _STATUS_STYLE[Status.WORKING]
        else:
            icon_glyph, icon_style = STATUS_ICON.get(row.status, "?"), base
        # Busy (green ▶, incl. the subagent branch above). Drives BOTH the model column's
        # ▶-instead-of-♨ cell (a running session re-warms its cache every request, so the
        # countdown says nothing — see cachettl) and the whole-line recede further down.
        working = icon_glyph == STATUS_ICON[Status.WORKING] and not session.draft
        # A row with an UNSATISFIED dependency wears the red |--> marker starting at column
        # 0 (col0 = "|", col1 = "--", col2 = ">" + the status icon), pushing the icon into
        # the ver cell; cell_padding=0 renders them contiguous ("|-->✎"). Placement (whether
        # this row was hoisted) is separate — the marker shows whenever the dep is unmet/
        # cancelled/missing (see deps.is_unsatisfied) — except on a done row: a job that
        # already finished no longer waits on anything, so it never wears the marker.
        marked = deps.is_unsatisfied(row.dep_state) and not done
        # A halted session ccc will auto-revive on its account's reset wears a green ▶ after
        # the red || (see models.HALTED_RESUME_ICON) — a bare || means it is stranded.
        resume_armed = row.status is Status.HALTED and resume.will_auto_resume(
            session, self.adapter, self.cfg, getattr(self, "_resume_queue", None)
        )
        icon = (
            Text("|", style="bold red")
            if marked
            else _icon_text(icon_glyph, icon_style, resume_armed)
        )
        sched = scheduled_date(session)
        if marked:
            imp = Text("--", style="bold red")
            ver = Text(">", style="bold red")
            ver.append_text(_icon_text(icon_glyph, icon_style, resume_armed))
            # Append the OAI badge only when the composed ver cell stays ≤5 cells — the
            # 2-cell status icons (||, 😴, 💤) push it over, so it is dropped there.
            if row.uses_codex_workflow and ver.cell_len + 3 <= 5:
                ver.append("OAI", style=_OAI_BADGE_STYLE)
        elif sched is not None:
            # SCHEDULED draft: the compact start date (``11.8.26``) SPANS the importance
            # and ver columns — cell_padding is 0, so the two cells abut into one
            # contiguous label. The head slice is ≤3 cells (the importance column's
            # ``!!!`` width) and the tail ≤5 (the ver column's ``head:`` width), so
            # neither column widens and the id cell stays a bare hash (_draft_id_cell).
            label = short_date_label(sched)
            imp = Text(label[:3], style=f"bold {_DRAFT_BLUE}")
            ver = Text(label[3:], style=f"bold {_DRAFT_BLUE}")
        else:
            imp = Text(
                importance_marks(session.importance), style=_DONE_STYLE if done else "bold red"
            )
            ver_text = version_column_text(
                session.version, uses_codex_workflow=row.uses_codex_workflow
            )
            ver = Text("  ")
            ver.append(ver_text, style=_OAI_BADGE_STYLE if row.uses_codex_workflow else "grey50")
            if done and not row.uses_codex_workflow:
                ver.stylize(_DONE_STYLE, 0, len(ver))
        folder_style = _DONE_STYLE if done else colors.folder_style(session.cwd, self.cfg, root)
        if indent_repo:
            # Nested under its category header: indent and show only the repo (+ sub-path).
            _cat, leaf = colors.folder_split(session.cwd, root)
            folder = Text("  ")
            folder.append(leaf, style=folder_style)
        else:
            # Flat done/draft block: full ``category/repo``, at the nested rows' left edge.
            folder = Text("  ")
            folder.append(colors.short_folder(session.cwd, root), style=folder_style)
        # The badge shares the id cell so both ride ONE background run — the statusline's
        # ` <badge> <id> ` chip, with no uncoloured gap between them. It also keeps
        # same-folder sessions distinguishable and makes a screenshot match the user's tabs.
        # ONLY a row with an open tab is painted: a parked/finished/draft row has no tab on
        # screen to match a colour to, so it stays receded.
        id_rgb = _id_bg_rgb(session) if (row.is_open and not done) else None
        badge = _id_badge(session, live=row.is_open)
        if row.dep_depth > 0:
            # Hoisted dependent: indent the folder cell 2 spaces per nesting level so the
            # tree structure reads (the row already sits directly under its parent).
            indented = Text("  " * row.dep_depth)
            indented.append_text(folder)
            folder = indented
        sid = Text("  ")
        if session.draft:
            # Future job: the internal UUID is meaningless to the user — show the bare
            # display hash instead (the start_when note rides the next-step column).
            sid.append(f" {badge} ")
            sid.append_text(_draft_id_cell(session))
        elif id_rgb is not None:
            # Badge + 4 id chars in ONE painted run, mirroring the Claude Code status line.
            sid.append(f" {badge} {short_id(session.session_id, 4)} ", style=_bg_style(id_rgb))
        else:
            # No open tab: the badge keeps the id at the painted rows' column, unpainted.
            sid.append(f" {badge} ")
            sid.append(short_id(session.session_id, 4), style=_DONE_STYLE if done else "grey50")
        # A little home icon marks rows billing to the `private` (cpriv) account (multi-account
        # only); every other row gets an equal-width blank so the model text stays aligned. The
        # marker map is identity-corrected per render, so a drifted login shows the account the
        # row TRULY bills, not the one its config dir is named after.
        home = accounts.home_marker_from(session.config_dir or "", self._account_markers)
        # Cache-cell span (start, end) inside model_cell, when this row shows the ▶ busy
        # glyph — the recede pass below repaints exactly that slice back to the status ▶'s
        # green. None on every other row (nothing to exempt from the recede).
        running_span: tuple[int, int] | None = None
        if session.draft:
            # Future job: it never ran, so show the CONFIGURED overseer ▸ executor model
            # pair (colour-coded, single name when they match) instead of an observed "—".
            # The (empty) cache cell is still reserved so the pair starts at the same column
            # as every live row's model·effort text.
            model_cell = _models_cell(session, prefix=" " + cachettl.cell_padding("") + home)
        else:
            # OBSERVED model·effort the session ran on, with the prompt-cache TTL countdown
            # prepended BEFORE the account glyph (how long the session's Anthropic prompt
            # cache stays warm — transcript mtime + TTL). Done rows skip it, mirroring the
            # statusline's ♨/❄ readout (see cachettl); a busy row reads ▶ instead, since a
            # running session re-warms its cache continuously (the ♨ is for rows waiting on
            # you, where "still cheap to resume?" is a real question). The cell is FIXED
            # WIDTH on every row — blank included — so the account glyph and model text
            # never shift between a ♨ 59:26, a ❄ cold, a ▶ and an empty cell.
            style = _DONE_STYLE if done else "grey50"
            model_cell = Text(" ", style=style)
            cache_text, cache_level = (
                ("", "") if done else cachettl.countdown_for(self.adapter, session, running=working)
            )
            if cache_text:
                start = len(model_cell.plain)
                model_cell.append(cache_text, style=_CACHE_STYLE[cache_level])
                if cache_level == "running":
                    running_span = (start, len(model_cell.plain))
            model_cell.append(cachettl.cell_padding(cache_text), style=style)
            model_cell.append(home + model_effort_cell(session.model, session.effort), style=style)
        low_score = low_aim_score(session.aim, session.aim_score, self.cfg.aim_score_threshold)
        aim_style = _DONE_STYLE if done else (_GOLD if session.aim else "grey50")
        # Leading score chip ('NN%', or '-1' while unscored) so /aim quality is visible.
        # Right-aligned in a fixed 4-cell field (and blanked to the same width when there
        # is no chip) so a 2- ('-1') or 4-char ('100%') chip never shifts the AIM text.
        chip = aim_score_pct(session.aim, session.aim_score)
        chip_style = "bold red" if low_score else (_DONE_STYLE if done else "grey46")
        aim = Text("  ")
        if chip:
            aim.append(f"{chip:>4} ", style=chip_style)
        else:
            aim.append(" " * 5)
        # Show the compact short-AIM label (cheap-model) when present, else the full AIM.
        aim.append(
            (_first_line(display_aim(session, self._aim_first)) or "—")[:_AIM_MAX_CHARS],
            style=aim_style,
        )
        if session.draft:
            # Future job: the next-step column doubles as a tags/notes column, carrying
            # any @tags plus the free-text start_when note (moved off the id column).
            nxt = _draft_next_cell(session, base)
        else:
            next_value = (
                row.codex_reset_hint
                if row.status is Status.WAITING_CODEX and row.codex_reset_hint
                else _first_line(session.next_step)
            )
            nxt = _with_tags("  " + (next_value or "—")[:48], base)
        if marked and sched is not None:
            # A marked SCHEDULED draft can't span the importance+ver cells (the marker
            # owns them), so its compact start date rides the tags/notes column instead,
            # composed the way _draft_next_cell appends parts (blue, " · " separator).
            nxt.append(" · ", style="grey50")
            nxt.append(f"starts {short_date_label(sched)}", style=_DRAFT_BLUE)
        age = Text("  " + humanize_age(session.last_response_at), style=base if done else "grey62")
        if session.draft:
            # Future job: it typically has no next_step (→ "—"); its model pair lives in the
            # model column now. Age is from when the draft was saved.
            age = Text("  " + humanize_age(session.created_at), style="grey62")
        git_symbol, git_style = gitstatus.short(session.cwd)
        git_cell = Text("  " + git_symbol, style=_DONE_STYLE if done else git_style)
        frac = effective_progress(session.manual_progress, row.checked, row.total)
        pct = f" {int(round(frac * 100))}%" if frac is not None else ""
        if session.aim_met and not session.done and not done and not session.draft:
            # Impartial checker judged the AIM fulfilled → red DONE stamped inside the bar
            # (fill still visible on both sides). Distinct from the human ✓/FINISHED. The DONE
            # bar's filled cells are SOLID █ (done_bar_parts) and a filled-cell letter gets the
            # very same colour spec as its background, so letter cells and bar cells are
            # pixel-identical — no seam. Empty-cell letters get the faint ░-average tint.
            # White letters on a red fill, black on yellow, for contrast.
            left, done_word, right, fills = done_bar_parts(frac, 8)
            fill_color = base.removeprefix("bold ")
            glyph = Color.parse(fill_color).get_truecolor()
            tr, tg, tb = empty_track_tint((glyph.red, glyph.green, glyph.blue))
            tint = f"#{tr:02x}{tg:02x}{tb:02x}"
            progress = Text("  ")
            progress.append(left, style=base)
            for ch, filled in zip(done_word, fills, strict=True):
                if filled:
                    fg = {"red": "white", "yellow": "black"}.get(fill_color, "red")
                    progress.append(ch, style=f"bold {fg} on {fill_color}")
                else:
                    progress.append(ch, style=f"bold red on {tint}")
            progress.append(right, style=base)
            progress.append(pct, style=base)
        else:
            progress = Text("  " + progress_bar(frac, 8) + pct, style=base)
        if drift_unresolved(session):  # impartial checker flagged sub-goal drift (unresolved)
            progress.append(" ●", style="#5fafff")
        # A running session (green ▶) isn't actionable, so the whole line recedes to gray;
        # only the green ▶ keeps its colour ("running, hands off") — BOTH of them: the status
        # icon (never receded, it is not in the loop) and the model column's ▶, repainted to
        # the identical green right after. Covers Status.WORKING plus the has_subagent branch
        # (both paint ▶); marked (red-dep) rows are excluded so their dependency marker stays
        # untouched. stylize() appends an overriding span by design, so the later green wins.
        running = working and not marked
        if running:
            for cell in (
                imp,
                ver,
                folder,
                sid,
                model_cell,
                aim,
                nxt,
                age,
                git_cell,
                progress,
            ):
                cell.stylize("not bold grey42")
        if running_span is not None:
            # Applied AFTER the recede so it overrides it: the model column's ▶ wears the
            # very same green as the status-icon ▶ at the start of the line.
            model_cell.stylize(_STATUS_STYLE[Status.WORKING], *running_span)
        table.add_row(
            icon,
            imp,
            ver,
            folder,
            sid,
            model_cell,
            aim,
            nxt,
            age,
            git_cell,
            progress,
            key=session.session_id,
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._highlight_key = key  # raw key incl. separators — drives the `fn` (new_job) context
        self._current = None if (key or "").startswith("__sep__") else key
        # Publish the cursor's session so `ccc jump` (from the ccc tab) knows which
        # session's tab to flip back to — the `f+j` toggle's "act like r" half.
        jumpstate.set_selected(self._current)
        self.update_detail()

    def _poll_jump_request(self) -> None:
        """Honour pending `ccc jump` signals (the f+j toggle), polled fast (sub-second).

        First the *whole-toggle* verb: a live TUI owns the toggle (the fast path — see
        jump / iterm_api), so ``ccc jump`` just writes it and we run the toggle here on
        the UI loop. Then the cursor-move *request*: ``ccc jump`` fired from a session
        tab (the slow no-TUI path) writes that session id; we move the cursor onto its
        row and clear the request. A request for a session not (yet) in the table is
        left pending for a later tick.

        A pending *restart* verb (``ccc restart-tui``) takes precedence: we consume it,
        flag the app and exit cleanly; run() re-execs the process in place (same tab).
        """
        if jumpstate.peek_restart():
            jumpstate.clear_restart()
            self.restart_requested = True
            self.exit()
            return
        if jumpstate.peek_toggle():
            jumpstate.clear_toggle()
            self.run_worker(self._handle_jump_toggle(), exclusive=True, group="jump-toggle")
        req = jumpstate.peek_request()
        if not req:
            return
        if req not in self._rows:
            return
        table = self.query_one("#sessions", DataTable)
        try:
            table.move_cursor(row=table.get_row_index(req))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass
        jumpstate.clear_request()

    def _session_for_uuid(self, uuid: str) -> str | None:
        """The tracked session id whose iTerm tab UUID is *uuid*, or None (from the store)."""
        if (store := self.store) is None:
            return None
        try:
            for session in store.list_sessions():
                isid = session.iterm_session_id
                if isid and isid.split(":")[-1] == uuid:
                    return session.session_id
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return None
        return None

    async def _handle_jump_toggle(self) -> None:
        """Run the whole f+j toggle in-process (the fast path — see jump / iterm_api).

        Runs on the UI loop (async worker), so widget/state access is safe; the only
        blocking calls (lsappinfo / any osascript fallback) are pushed to a thread. The
        warm iTerm2 API link does the focus; :mod:`terminal`'s AppleScript helpers are
        the fallback when the API is unavailable or the session can't be located.
        """
        front = await asyncio.to_thread(terminal.is_iterm_frontmost)  # lsappinfo, ~14 ms
        own = (os.environ.get("ITERM_SESSION_ID", "").split(":")[-1]).strip()
        cur = await self._iterm_link.current_session_uuid() if front else None
        if cur is None and front:
            cur = await asyncio.to_thread(_current_session_uuid_fallback)  # osascript fallback
        if front and cur and own and cur == own:
            # f+j in the ccc tab → same as pressing r on the selected row.
            self.action_resume()
            return
        if front and cur:
            # f+j in a session tab → land the cursor on that row before focusing ccc.
            if (sid := self._session_for_uuid(cur)) is not None:
                jumpstate.request_select(sid)
                self._poll_jump_request()  # consume immediately when the row is present
        # Bring the ccc tab forward (own session): warm link first, AppleScript fallback.
        focused = False
        if own:
            focused = await self._iterm_link.focus_session(own)
            if not focused:
                focused = await asyncio.to_thread(
                    terminal.focus_iterm_session, os.environ.get("ITERM_SESSION_ID", "")
                )
        if not focused and not own:
            # No $ITERM_SESSION_ID (unusual for a live TUI) — fall back to the tab title,
            # like jump._focus_ccc does.
            title = (self.cfg.tab_title or "").strip()
            if title:
                await asyncio.to_thread(terminal.focus_session_name, title)

    def _head_text(
        self, session: Session, status: Status, checked: int, total_subs: int, *, editing: bool
    ) -> Text:
        """The detail pane's head block (the ``Status:`` line and its context lines).

        With ``editing=True`` the head is compacted to what the inline editor does NOT
        already show as an editable row — the models/account readout, the Scheduled-for
        line, the ``Prompt to run`` body and the launch hint all drop out so every
        option renders exactly once in the pane while editing.
        """
        # --- line 1: Status + age, with the progress bar trailing on the same line ---
        text = Text()
        text.append("Status: ", style="bold")
        text.append(f"{status.value}", style=_STATUS_STYLE.get(status, "white"))
        text.append(f"   {humanize_age(session.last_response_at)} ago", style="grey62")
        if status is Status.PARKED:
            # WHEN the session was closed, absolute + relative. A row parked before
            # closed_at existed has no stamp — approximate with last activity (~).
            closed = session.closed_at or session.last_response_at
            approx = "" if session.closed_at else "~"
            text.append(
                f"   closed {approx}{iso_datetime(closed) or '—'} ({humanize_age(closed)} ago)",
                style="grey62",
            )
        if session.draft and not editing:
            # A draft's model pair + billing account live on the Status line (their
            # editable twins are the top rows of the `e` form).
            over = session.llm_overseer or ""
            ex = session.llm_exec or ""
            text.append("   /overseer: ", style="bold")
            text.append(over or "—", style=_LLM_STYLE.get(over, "white"))
            text.append("  /executor: ", style="bold")
            text.append(ex or "—", style=_LLM_STYLE.get(ex, "white"))
            if len(config.claude_config_dirs()) > 1:
                text.append("  /account: ", style="bold")
                text.append(accounts.account_label(session.config_dir or ""), style="white")
        # Manual override (set via `e` or Enter on the progress column) wins over the
        # sub-goal ratio and is labelled so its origin is never ambiguous.
        head_frac = effective_progress(session.manual_progress, checked, total_subs)
        if head_frac is not None:
            count = (
                f"{int(round(head_frac * 100))}% (manual)"
                if session.manual_progress is not None
                else f"{checked}/{total_subs}"
            )
            text.append(f"   {progress_bar(head_frac, 12)} {count}", "cyan")
        text.append("\n")
        if session.draft and not editing:
            # The date the job is supposed to run (the SCHEDULED column's date), right
            # under Status: — same compact D.M.YY form and blue as the table rows. An
            # unscheduled draft shows a grey — so the field is discoverable (set it via e).
            text.append("Scheduled for: ", style=f"bold {_DRAFT_BLUE}")
            if (when := scheduled_date(session)) is not None:
                text.append(short_date_label(when), style=_DRAFT_BLUE)
                if (early := days_until_start(session)) is not None:
                    text.append(f"  (in {early}d — launching earlier asks first)", style="grey62")
            else:
                text.append("—  (none — set it via e)", style="grey62")
            text.append("\n")
        if session.aim_met and not session.done and not session.draft:
            # The impartial per-turn checker judged the AIM fulfilled (the red DONE in the bar).
            text.append("model self-assessment: ", style="bold")
            text.append("DONE", style="bold red")
            text.append(
                f" — {session.aim_met_reason or 'the AIM looks fulfilled'}\n", style="grey62"
            )
        row = self._rows.get(session.session_id)
        if status is Status.WAITING_CODEX and row and row.codex_reset_hint:
            text.append(row.codex_reset_hint + "\n", style=_STATUS_STYLE[Status.WAITING_CODEX])
        if session.done and session.done_at:
            text.append(f"Done: {iso_date(session.done_at)}\n", style="green3")
        if session.draft:
            text.append("\n✎ FUTURE JOB — not started yet\n", style=f"bold {_DRAFT_BLUE}")
            if session.start_when:
                text.append("Intend to start: ", style="bold")
                text.append(f"{session.start_when}\n", style=_DRAFT_BLUE)
            if not editing:  # the form's `prompt:` TextArea shows (and edits) it instead
                text.append("Prompt to run: ", style="bold")
                text.append(f"{session.prompt or session.aim or '—'}\n", style="grey70")
                text.append("Press r (resume) or Enter to launch it in its repo.\n", style="grey50")
        return text

    def update_detail(self) -> None:
        if getattr(self, "_editing", False):
            return
        assert self.store is not None
        head = self.query_one("#detail-head", Static)
        fview = self.query_one("#detail-fields-view", Static)
        bottom = self.query_one("#detail-bottom", Static)
        if not self._current:
            head.update("Select a session.")
            fview.update("")
            bottom.update("")
            return
        session = self.store.get(self._current)
        if session is None:
            head.update("Select a session.")
            fview.update("")
            bottom.update("")
            return
        status = Status(session.status)
        subs = self.store.list_subgoals(self._current)
        checked = sum(1 for s in subs if s.checked)
        head.update(self._head_text(session, status, checked, len(subs), editing=False))

        # --- fields (editable lines): aim(1), aim(N), next-step, deadline, block, important.
        # Their own static so edit mode can swap just them for the inline editor in place. ---
        ftext = Text()
        self._append_fields(ftext, session)
        ftext.append(
            "\n— Enter resume · ←/→ column then Enter edits · ↑/↓ rows (⌥←/⌥→ ±3) · "
            "e edit inline (Esc saves) · a/n/D/b direct —",
            style="grey50",
        )
        fview.update(ftext)

        # --- bottom: todos / summary / flags, then the sub-goal checklist at the very bottom ---
        btext = Text()
        # Prefer the forwarded snapshot in the store (works for parked sessions and
        # over `ccc serve`); fall back to the live on-disk list for untracked ones.
        todos = loads_todos(session.todos) or self.adapter.todos(self._current, session.config_dir)
        if todos:
            done, total = todos_counts(todos)
            btext.append(f"This turn — todos ({done}/{total} done):\n", style="bold")
            for todo_status, subject in todos:
                mark = todo_box(todo_status)
                style = (
                    "green"
                    if todo_status == "completed"
                    else ("yellow" if "progress" in todo_status else "white")
                )
                btext.append(f"  {mark} {subject}\n", style=style)
        if session.summary:
            btext.append("\nSummary: ", style="bold")
            btext.append(f"{session.summary}\n", style="grey70")
        flags = [f for f, on in (("KEEP", session.keep), ("DONE", session.done)) if on]
        if flags:
            btext.append("\n" + "  ".join(flags) + "\n", style="bold yellow")
        if subs:
            prov = subgoal_provenance(subs)
            btext.append("\nSub-goals", style="bold")
            if prov:
                btext.append(f" · {prov}", style="grey50")
            btext.append("\n")
            for sub in subs:
                box = "[x]" if sub.checked else "[ ]"
                btext.append(f"  {box} {sub.text}", style="green" if sub.checked else "white")
                if sub.check_cmd:  # machine-gated: show the predicate that ticks it
                    btext.append(f"  ⚙ {sub.check_cmd}", style="grey50")
                btext.append("\n")
        bottom.update(btext)

    def _append_fields(self, text: Text, session: Session) -> None:
        """Render the session's editable fields (untruncated, read-only) into *text*.

        These used to live in a focusable bottom pane; ``←/→`` now edit them inline
        in the table, so the detail pane just shows their full current values — and
        flags a low AIM score on the score chip, matching the table.
        """
        aim_val = getattr(session, "aim", None)
        aim_score = getattr(session, "aim_score", -1)
        aim_reason = getattr(session, "aim_score_reason", None)
        # Show ONLY the first AIM ever defined and the last (current) one — never the
        # middle revisions. The `ah` chord / `ccc aim-history` has the full progression.
        revisions = self.store.list_aim_history(session.session_id) if self.store else []
        if not revisions:
            # Pre-history session: the live AIM is the sole revision (first == last).
            self._append_aim_line(text, 1 if aim_val else 0, aim_val, aim_score, aim_reason)
        else:
            first = revisions[0]
            self._append_aim_line(text, 1, first.aim, first.score, None)
            if len(revisions) > 1:
                # The last row's aim == session.aim; prefer the session's live LLM score
                # (history rows carry only a cheap lexical score) and its vague reason.
                text.append("\n")
                self._append_aim_line(text, len(revisions), aim_val, aim_score, aim_reason)
        text.append("\n/next-step: ", style="white")
        text.append_text(
            _with_tags(_first_line(getattr(session, "next_step", None)) or "—", "white")
        )
        text.append(f"\n/deadline: {getattr(session, 'deadline', None) or '—'}", style="white")
        # A draft's /overseer /executor /account readouts render on the Status line in
        # the head (update_detail), not here — edit mode still shows them as Select rows.
        text.append("\n/block: ", style="white")
        text.append_text(_with_tags(getattr(session, "blocked_on", None) or "—", "white"))
        dep = getattr(session, "depends_on", None)
        if dep:
            # Read-only dependency line (shown whenever set): the |--> marker is red when
            # unsatisfied, dim otherwise, then the parent's hash · repo · aim · (state).
            parent = self.store.get(dep) if self.store else None
            state = deps.dependency_state(parent)
            marker_style = "bold red" if deps.is_unsatisfied(state) else "grey50"
            text.append("\n/depends-on: ", style="white")
            text.append(DEP_MARKER + " ", style=marker_style)
            repo = colors.short_folder(parent.cwd) if parent else "?"
            aim = (_first_line(display_aim(parent, self._aim_first)) if parent else "") or "—"
            text.append(f"{future_files.display_hash(dep)} {repo} — {aim} ({state})", style="white")
        marks = importance_marks(getattr(session, "importance", 0)) or "—"
        text.append(f"\n! important: {marks}\n", style="white")

    def _append_aim_line(
        self, text: Text, index: int, aim: str | None, score: int, reason: str | None
    ) -> None:
        """Render one ``/aim (N): <chip> <aim>`` line into *text*.

        *index* is the AIM's 1-based running position (0 → no AIM, label drops the number).
        Used by :meth:`_append_fields` for the first and last revisions only.
        """
        label = f"/aim ({index}): " if index >= 1 else "/aim: "
        text.append(label, style="white")
        chip = aim_score_pct(aim, score)
        low_score = low_aim_score(aim, score, self.cfg.aim_score_threshold)
        if chip:
            text.append(chip + " ", style="bold red" if low_score else "grey46")
        text.append(aim or "—", style=_GOLD if aim else "grey50")
        if low_score:
            text.append(f"  ⚠ vague{' — ' + reason if reason else ''}", style="grey50")

    # ---- actions --------------------------------------------------------
    def action_resume(self) -> None:
        """Live session → focus its tab; parked session → resume in a new tab.

        A live session must NOT be ``claude --resume``d (that errors with "session
        is currently running") — so we only resume a session whose process is gone.
        """
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is not None and session.draft:  # a future job → launch it (not a resume)
            self._start_job(sid)
            return
        if session is None or not session.cwd:
            self.notify("No working directory for this session.", severity="warning")
            return
        row = self._rows.get(sid)
        live = row.live if row else None
        if live is not None and live.alive:
            if session.iterm_session_id:
                # Focus off-loop: the AppleScript window walk is the ~620 ms UI-freeze
                # class we removed from d/close — the worker tries the warm iTerm2 link
                # first, then that fallback (see _focus_session_task).
                self.run_worker(
                    self._focus_session_task(
                        session.iterm_session_id, sid, colors.short_folder(session.cwd)
                    ),
                    group="jump-toggle",
                    description="focus session tab",
                )
                return
            if live.kind == "bg":
                self.notify(
                    "Live background agent — attach with `claude agents` (can't open a tab).",
                    severity="warning",
                    timeout=10,
                )
            elif terminal.focus_tmux_window(sid):
                # Launchd future-sync jobs land in a tmux window (no iterm_session_id) —
                # locate + surface it rather than dead-ending on "switch to it manually".
                self.notify("Focused tmux window (attached in iTerm).")
            else:
                self.notify(
                    "Session is live but its tab can't be located — switch to it manually.",
                    severity="warning",
                    timeout=10,
                )
            return
        # Parked: the process is gone, so resuming in a fresh tab is safe — but only
        # if Claude Code actually has a conversation on disk for it. A session that
        # never had a turn (or whose transcript was deleted) has no `<id>.jsonl`, so
        # `claude --resume <id>` would fail with "No conversation found"; don't open a
        # doomed tab — surface why and offer to archive the dead row instead.
        if self.adapter.transcript_path(session.cwd, sid, session.config_dir) is None:
            self._offer_archive_orphan(sid)
            return
        if terminal.resume_in_new_tab(session.cwd, sid, session.config_dir):
            self.notify(f"Resuming in a new tab: {colors.short_folder(session.cwd)}")
        else:
            self.notify(f"Run: c --resume {sid}", severity="warning", timeout=10)

    async def _focus_session_task(self, iterm_session_id: str, session_id: str, label: str) -> None:
        """Bring a live session's iTerm tab forward off the UI loop.

        Warm iTerm2 link first (sub-ms), then the AppleScript walk as fallback (slow —
        why it runs in a worker, not inline in action_resume). If the iTerm tab can't be
        located at all (a launchd future-sync job whose ``iterm_session_id`` is stale, or
        one that only ever lived in a tmux window), fall back to locating + surfacing the
        tmux window hosting *session_id* — also off the UI loop, same worker pattern.
        """
        focused = await self._iterm_link.focus_session(iterm_session_id)
        if not focused:
            focused = await asyncio.to_thread(terminal.focus_iterm_session, iterm_session_id)
        if focused:
            self.notify(f"Focused live tab: {label}")
        elif await asyncio.to_thread(terminal.focus_tmux_window, session_id):
            self.notify("Focused tmux window (attached in iTerm).")
        else:
            self.notify(
                "Session is live but its tab can't be located — switch to it manually.",
                severity="warning",
                timeout=10,
            )

    def _offer_archive_orphan(self, sid: str) -> None:
        """A parked session with no transcript can't be resumed — triage the dead row.

        Such a row is almost always a future job that ``start-job`` launched but that never
        had a turn (its tab was closed first, or the work happened in another session), so
        there is no ``<id>.jsonl`` to resume. Offer the three real outcomes: restore it to
        FUTURE so it can be re-run (only when it still has launched-draft provenance), delete
        it from the command center outright, or keep it as-is.
        """
        if (store := self.store) is None or (session := store.get(sid)) is None:
            return
        from .. import cli, mirrors  # pylint: disable=import-outside-toplevel

        label = colors.short_folder(session.cwd)
        allow_restore = bool(session.future_file) or (
            cli._archived_job_file(self.cfg, sid) is not None  # pylint: disable=protected-access
        )

        def handle(choice: str | None) -> None:
            if (st := self.store) is None:
                return
            if choice == "delete":
                st.delete(sid)
                mirrors.remove_mirror(self.cfg, sid)  # drop any mirror the launch left behind
                cli._spawn_sync_mirrors(self.cfg)  # pylint: disable=protected-access
                self.notify(f"Deleted dead row (no conversation): {label}")
                self.refresh_data()
            elif choice == "restore":
                ok, msg = cli.unlaunch_job(st, self.cfg, sid, set())
                if ok:
                    cli._spawn_sync_mirrors(self.cfg)  # pylint: disable=protected-access
                self.notify(
                    f"Restored to FUTURE: {label}" if ok else msg,
                    severity="information" if ok else "warning",
                    timeout=6 if ok else 10,
                )
                self.refresh_data()

        action_line = (
            "Restore to FUTURE re-lists it as a job to run; Delete removes it."
            if allow_restore
            else "Delete removes it from the command center."
        )
        self.push_screen(
            DeadRowScreen(
                f"{label} {short_id(sid)} has no recorded conversation on disk — it never "
                "had a turn (or its transcript was deleted), so it can't be resumed.\n"
                f"{action_line}",
                allow_restore=allow_restore,
            ),
            handle,
        )

    # ---- undo (the `u` key) --------------------------------------------
    def _push_undo(self, label: str, apply: Callable[[], str | None]) -> None:
        """Record the inverse of a just-performed action (no-op while undoing)."""
        if self._undoing:
            return
        self._undo_stack.append(_UndoEntry(label, apply))
        del self._undo_stack[:-_UNDO_MAX]

    def action_undo(self) -> None:
        """Undo the most recent undoable action — the `u` key. LIFO; repeat to go further."""
        if isinstance(self.screen, ModalScreen):
            return  # a modal owns its keys — never mutate state under a dialog
        if not self._undo_stack:
            self.notify("Nothing to undo.")
            return
        entry = self._undo_stack.pop()
        self._undoing = True
        try:
            message = entry.apply()
        finally:
            self._undoing = False
        self.refresh_data()
        self.notify(message or f"Undid: {entry.label}")

    def _reopen_session(self, sid: str, label: str) -> str:
        """Reopen a closed session's conversation in a new tab (the undo of a live close).

        The SIGTERMed process can't be revived — resuming the transcript in a fresh
        tab (same as `r` on a parked row) is the real inverse.
        """
        if (store := self.store) is None or (session := store.get(sid)) is None:
            return "Can't undo — session no longer exists."
        if self.adapter.transcript_path(session.cwd, sid, session.config_dir) is None:
            return f"Undid close: {label} restored (no conversation on disk to reopen)."
        if terminal.resume_in_new_tab(session.cwd, sid, session.config_dir):
            return f"Undid close: resuming {label} in a new tab."
        return f"Undid close: {label} restored — reopen it with r."

    def action_close(self) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is None:
            return
        # Liveness as currently displayed — decides whether there's a tab to close.
        row = self._rows.get(sid)
        live = row.live if row else None
        was_live = live is not None and live.alive
        prev_status = session.status
        # Park: SIGTERM the process and, for a LIVE session, close its iTerm pane —
        # and the whole tab if Claude was the only pane (the single process) in it.
        # A done session keeps DONE (never demoted to PARKED) so closing it sinks
        # the row to the FINISHED section instead of leaving it in the active list.
        store.update_fields(sid, status=Status.DONE.value if session.done else Status.PARKED.value)
        label = colors.short_folder(session.cwd)
        verb = "Finished" if session.done else "Parked"
        self.refresh_data()  # the status flip repaints immediately
        # SIGTERM + the osascript pane close (walks all iTerm windows) is slow — run it
        # off-loop. NON-exclusive and NOT the data-refresh group: two different sessions'
        # closes must never cancel each other.
        self.run_worker(
            lambda: self._close_worker(
                session, close_pane=was_live, label=label, verb=verb, done=session.done
            ),
            thread=True,
            group="close-pane",
            description=f"close pane {label}",
        )

        def undo_close(
            sid: str | None = sid,
            prev_status: str = prev_status,
            was_live: bool = was_live,
            label: str = label,
        ) -> str | None:
            if sid is None or (st := self.store) is None or st.get(sid) is None:
                return "Can't undo — session no longer exists."
            st.update_fields(sid, status=prev_status)
            if was_live:
                return self._reopen_session(sid, label)
            return None

        self._push_undo(f"close ({label})", undo_close)

    def action_mark_done(self) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is None:
            return
        # Liveness as currently displayed — decides whether there's a tab to close.
        row = self._rows.get(sid)
        live = row.live if row else None
        was_live = live is not None and live.alive
        prev_done, prev_status, prev_done_at = session.done, session.status, session.done_at
        new = not session.done
        store.update_fields(
            sid,
            done=new,
            status=Status.DONE.value if new else Status.IDLE.value,
            done_at=now_ms() if new else 0,
        )
        draft_archived = new and session.draft
        # A future-job draft can't be "done"-graded: archive its mirror file and drop it
        # out of the FUTURE list (archived), matching cmd_mark_done / futuresync semantics.
        if new and session.draft:
            from .. import futuresync  # pylint: disable=import-outside-toplevel

            if session.future_file:
                futuresync.archive_file(store, self.cfg, session, "archived")
            store.update_fields(sid, archived=True)
        # The done flag lands in the store immediately; the session's own status line
        # (`ccc statusline`) reads it and shows "done" on its next render. Marking done
        # on a *live* session also offers to close its tab/pane (never on un-done) —
        # pushed here, before refresh_data, so the confirm dialog appears synchronously
        # in the keypress rather than waiting out the off-loop row rebuild.
        if new and was_live:
            self._confirm_close_after_done(sid)
        self.refresh_data()
        label = colors.short_folder(session.cwd)

        def undo_done(
            sid: str | None = sid,
            prev_done: bool = prev_done,
            prev_status: str = prev_status,
            prev_done_at: int = prev_done_at,
            draft_archived: bool = draft_archived,
        ) -> str | None:
            if sid is None or (st := self.store) is None or (sess := st.get(sid)) is None:
                return "Can't undo — session no longer exists."
            st.update_fields(sid, done=prev_done, status=prev_status, done_at=prev_done_at)
            if draft_archived:
                from .. import futuresync  # pylint: disable=import-outside-toplevel

                st.update_fields(sid, archived=False)
                futuresync.unarchive_file(st, self.cfg, sess)
            return None

        self._push_undo(f"mark {'done' if new else 'not-done'} ({label})", undo_done)

    def _confirm_close_after_done(self, sid: str) -> None:
        """Ask whether to close the just-finished live session's tab/pane."""
        if (store := self.store) is None or (session := store.get(sid)) is None:
            return
        label = colors.short_folder(session.cwd)

        def undo_close(sid: str = sid, label: str = label) -> str | None:
            return self._reopen_session(sid, label)

        def handle(confirmed: bool | None) -> None:
            if confirmed:
                self._push_undo(f"close ({label})", undo_close)
                self._close_live_session(session)

        self.push_screen(
            ConfirmScreen(
                f"Marked done: {label}\nClose its session now (and the tab, if it's the only pane)?"
                "\n(Esc to leave it open.)",
                no_label=None,
            ),
            handle,
        )

    def _signal_and_close_pane(self, session: Session, *, close_pane: bool) -> str:
        """SIGTERM the session's process; when *close_pane*, close its iTerm pane/tab.

        SIGTERM is always attempted (defensive — the row's liveness may be stale).
        The pane is only closed when *close_pane* is set (the session is live and so
        has a tab worth closing). Returns ``"tab"`` if the whole tab closed (Claude
        was the only pane in it), ``"session"`` if just the pane closed, else ``""``.
        """
        if session.last_seen_pid:
            try:
                os.kill(session.last_seen_pid, signal.SIGTERM)
            except OSError:
                pass
        if close_pane and session.iterm_session_id:
            return terminal.close_iterm_session(session.iterm_session_id)
        return ""

    def _close_worker(
        self, session: Session, *, close_pane: bool, label: str, verb: str, done: bool
    ) -> None:
        # Worker thread: SIGTERM + the osascript pane close (slow — walks iTerm windows).
        closed = self._signal_and_close_pane(session, close_pane=close_pane)
        self.call_from_thread(self._notify_closed, closed, label, verb, done)

    def _notify_closed(self, closed: str, label: str, verb: str, done: bool) -> None:
        # UI thread: report what the park closed (action_close's Parked/Finished verb).
        if closed == "tab":
            self.notify(f"{verb} and closed tab: {label}")
        elif closed == "session":
            self.notify(f"{verb} and closed pane: {label}")
        elif done:
            self.notify("Session closed (done — moved to FINISHED).")
        else:
            self.notify("Session parked (process signalled; resume by id).")
        self.refresh_data()  # liveness changed once the pane is gone — repaint

    def _close_live_session(self, session: Session) -> None:
        """SIGTERM the session's process, then close its iTerm pane/tab (off-loop)."""
        label = colors.short_folder(session.cwd)
        # Same off-loop treatment as action_close: the osascript pane close is slow.
        self.run_worker(
            lambda: self._close_live_worker(session, label),
            thread=True,
            group="close-pane",
            description=f"close pane {label}",
        )

    def _close_live_worker(self, session: Session, label: str) -> None:
        # Worker thread (see _close_worker): the pane close walks every iTerm window.
        closed = self._signal_and_close_pane(session, close_pane=True)
        self.call_from_thread(self._notify_live_closed, closed, label)

    def _notify_live_closed(self, closed: str, label: str) -> None:
        # UI thread: the mark-done confirm's plain close wording, then repaint.
        if closed == "tab":
            self.notify(f"Closed tab: {label}")
        elif closed == "session":
            self.notify(f"Closed pane: {label}")
        else:
            self.notify(
                "Process signalled; couldn't locate its iTerm tab to close.",
                severity="warning",
            )
        self.refresh_data()  # liveness changed once the pane is gone — repaint

    def action_keep(self) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is None:
            return
        prev = session.keep
        store.update_fields(sid, keep=not prev)
        self.update_detail()
        label = colors.short_folder(session.cwd)

        def undo_keep(sid: str | None = sid, prev: bool = prev) -> str | None:
            if sid is None or (st := self.store) is None or st.get(sid) is None:
                return "Can't undo — session no longer exists."
            st.update_fields(sid, keep=prev)
            return None

        self._push_undo(f"Keep {'on' if not prev else 'off'} ({label})", undo_keep)

    def action_cycle_importance(self) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is None:
            return
        prev = session.importance
        store.update_fields(sid, importance=(prev + 1) % 4)
        self.refresh_data()

        def undo_importance(sid: str | None = sid, prev: int = prev) -> str | None:
            if sid is None or (st := self.store) is None or st.get(sid) is None:
                return "Can't undo — session no longer exists."
            st.update_fields(sid, importance=prev)
            return None

        self._push_undo("importance", undo_importance)

    def _transcript_under(self, cwd: str, session_id: str, account_dir: Path) -> bool:
        """True if *session_id*'s transcript lives specifically under *account_dir*.

        Mirrors ClaudeAdapter's layout (``<account>/projects/<cwd-slashes-as-dashes>/<id>.jsonl``)
        but pinned to ONE account — the public ``transcript_path`` searches EVERY account
        (the D14 fallback), which would defeat the "is it under the TARGET account?" guard
        the ``tp``/``tw`` switch needs for a parked session.
        """
        projects = Path(account_dir) / "projects"
        if (projects / cwd.replace("/", "-") / f"{session_id}.jsonl").exists():
            return True
        try:
            return any(projects.glob(f"*/{session_id}.jsonl"))
        except OSError:
            return False

    def _set_account(self, label: str) -> None:
        """Set the highlighted row's Claude account to *label* — the `tp`/`tw` chords.

        Flips a FUTURE job (draft) freely (it never ran); re-stamps a PARKED session only
        when its transcript already lives under the target account (else resume would find
        nothing); refuses a LIVE session (it already bills the account its process runs
        under); a no-op with a single account configured.
        """
        if not (sid := self._current) or (store := self.store) is None:
            return
        dirs = config.claude_config_dirs()
        if len(dirs) <= 1:
            self.notify("Only one Claude account is configured — nothing to switch.")
            return
        target = dirs.get(label)
        if target is None:
            self.notify(f"No Claude account labelled {label!r} is configured.", severity="warning")
            return
        session = store.get(sid)
        if session is None:
            return
        if session.config_dir and accounts.same_config_dir(str(target), session.config_dir):
            self.notify(f"Already billing the {label} account.")
            return
        row = self._rows.get(sid)
        if row is not None and row.is_open:
            self.notify(
                "That session is live — it already bills the account its process runs "
                "under; close it (c) first to change the account.",
                severity="warning",
            )
            return
        if not session.draft and not self._transcript_under(session.cwd, sid, target):
            # A parked/finished session's conversation is account-bound: re-stamping to an
            # account that holds no transcript would only make resume find nothing.
            self.notify(
                f"Can't switch to {label}: this session's conversation isn't stored under "
                "that account (resuming there would find nothing).",
                severity="warning",
            )
            return
        prev_dir = session.config_dir
        store.update_fields(sid, config_dir=str(target))
        self.refresh_data()
        self.notify(f"Account set to {label}.")

        def undo_account(sid: str | None = sid, prev_dir: str = prev_dir) -> str | None:
            if sid is None or (st := self.store) is None or st.get(sid) is None:
                return "Can't undo — session no longer exists."
            st.update_fields(sid, config_dir=prev_dir)
            return None

        self._push_undo(f"account → {label}", undo_account)

    def action_account_private(self) -> None:
        """Bill the highlighted row under the private (cpriv) account — the `tp` chord."""
        self._set_account("private")

    def action_account_work(self) -> None:
        """Bill the highlighted row under the work account — the `tw` chord."""
        self._set_account("work")

    def action_toggle_subgoal(self) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        subs = store.list_subgoals(sid)
        if not subs:
            self.notify("No sub-goals; add them with /aim or `ccc subgoals`.")
            return
        target = next((s for s in subs if not s.checked), subs[-1])
        target_id, prev_checked = target.id, target.checked
        store.set_subgoal_checked(target_id, not prev_checked)
        self.refresh_data()

        def undo_subgoal(
            target_id: int = target_id, prev_checked: bool = prev_checked
        ) -> str | None:
            if (st := self.store) is None:
                return "Can't undo — session no longer exists."
            st.set_subgoal_checked(target_id, prev_checked)
            return None

        self._push_undo("sub-goal tick", undo_subgoal)

    # ---- future jobs (the `fn` chord: register now, start later) --------
    def action_new_job(self) -> None:
        """Register a future job from the cursor's context (the `fn` chord).

        On a real session row the job targets that session's repo; on a category
        header it opens the repo picker for that category; anywhere else it opens
        the full category → repo picker.
        """
        if self.store is None:
            return
        key = self._highlight_key
        if key and key in self._rows:  # on a real session row → target its repo directly
            cwd = self._rows[key].session.cwd
            if cwd:
                self._open_new_job_dialog(cwd)
                return
        category = self._sep_category.get(key or "")
        if category and category in set(repos.categories()):  # on a category header
            self._pick_repo_in(category)
            return
        self._pick_category()  # off-grid (FUTURE/done/others header, or nothing selected)

    def _pick_category(self) -> None:
        cats = repos.categories()
        if not cats:
            self.notify(
                f"No repo categories under {repos.repo_root(self.cfg) or '<no repo_root set>'}.",
                severity="warning",
            )
            return

        def chosen(category: str | None) -> None:
            if category:
                self._pick_repo_in(category)

        self.push_screen(CategoryPickerScreen(cats), chosen)

    def _pick_repo_in(self, category: str) -> None:
        def chosen(result: str | None) -> None:
            if result is None:
                return
            if result == _NEW_REPO_SENTINEL:
                self._new_repo_then_job(category)
            else:
                self._open_new_job_dialog(result)  # result is an absolute repo path

        can_create = bool(self.cfg.create_repo_command.strip())
        self.push_screen(RepoPickerScreen(category, repos.repos_in(category), can_create), chosen)

    def _new_repo_then_job(self, category: str) -> None:
        def got_args(arg_string: str | None) -> None:
            if not arg_string or not arg_string.strip():
                return
            path = repos.parse_repo_path(arg_string)
            if path is None:
                self.notify("Need '<category> <name>' to create a repo.", severity="error")
                return
            self._spawn_repo_create(arg_string, path)
            self._open_new_job_dialog(str(path))  # capture the job while the repo is created

        self.push_screen(NewRepoScreen(category), got_args)

    def _spawn_repo_create(self, arg_string: str, path: Path) -> None:
        """Run the create_repo_command in a background thread (it may hit the network)."""

        def work() -> None:
            ok, out = repos.create_repo(arg_string, self.cfg)
            msg = f"Repo created: {path.name}" if ok else f"repo create failed: {out[:120]}"
            self.call_from_thread(
                self.notify, msg, severity="information" if ok else "error", timeout=8
            )

        self.notify(f"Creating repo {path.name} …", timeout=5)
        threading.Thread(target=work, daemon=True).start()

    def _open_new_job_dialog(self, cwd: str) -> None:
        def got(fields: dict[str, str] | None) -> None:
            if fields:
                self._create_job(
                    cwd,
                    fields["aim"],
                    fields["prompt"],
                    fields["deadline"],
                    fields.get("start_when", ""),
                    fields.get("job_type", "claude"),
                    fields.get("start_date", ""),
                    fields.get("account", ""),
                )

        self.push_screen(NewJobScreen(colors.short_folder(cwd), cwd), got)

    def _create_job(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        cwd: str,
        aim: str,
        prompt: str,
        deadline: str,
        start_when: str = "",
        job_type: str = "claude",
        start_date: str = "",
        account: str = "",
    ) -> None:
        if (store := self.store) is None:
            return
        sid = str(uuid.uuid4())
        if account:
            found = config.claude_config_dirs().get(account)
            config_dir = str(found) if found else ""
        else:
            # No account chosen ⇒ route this NEW job per the job_account policy.
            config_dir = routing.pick_job_account()[1]
        store.create_draft(
            sid,
            cwd,
            aim,
            prompt=prompt or None,
            deadline=deadline or None,
            start_when=start_when or None,
            start_date=start_date or None,
            job_type=job_type,
            config_dir=config_dir,
        )
        from .. import spawn  # pylint: disable=import-outside-toplevel

        if self.cfg.aim_score_on_set:
            spawn.spawn_ccc(["score-aim", "--session", sid])
        # Mirror the new draft into its Obsidian file out-of-band (never block the UI).
        if self.cfg.future_files:
            spawn.spawn_ccc(["sync-future"])
        self._current = sid  # land the cursor on the new job after refresh
        self.refresh_data()
        self.notify(
            f"Future job saved: {colors.short_folder(cwd)} — select it and press r to launch."
        )

    def _start_job(self, sid: str) -> None:
        """Launch a saved future job: open a new tab running ``ccc start-job <id>``.

        A job whose FIXED start date is still ahead OR whose dependency is unsatisfied
        asks first (ConfirmScreen, one message covering both) — only an explicit "Start
        anyway" launches it, and then with ``--force`` so the ``ccc start-job`` in the new
        tab (which also launches with ``--force``) never asks a second time.
        """
        if (store := self.store) is None or (session := store.get(sid)) is None:
            return
        if not (session.cwd and os.path.isdir(session.cwd)):
            self.notify(
                f"Repo not found: {session.cwd or '?'} — create it first.",
                severity="error",
                timeout=10,
            )
            return
        early = days_until_start(session)
        blocker = deps.launch_blocker(store, session)
        if early is not None or blocker is not None:
            parts: list[str] = []
            if early is not None:
                parts.append(
                    f"⚠ Start date {session.start_date} not reached — "
                    f"{early} day{'s' if early != 1 else ''} early."
                )
            if blocker is not None:
                aim = (blocker.parent_aim or "?")[:60]
                parts.append(f'⚠ Depends on {blocker.parent_hash} "{aim}" — {blocker.state}.')
            parts.append("Start this job anyway?")

            def handle(confirmed: bool | None) -> None:
                if confirmed:
                    self._open_job_tab(sid, session.cwd, force=True)

            self.push_screen(
                ConfirmScreen("\n".join(parts), yes_label="Start anyway", no_label="Cancel"),
                handle,
            )
            return
        self._open_job_tab(sid, session.cwd)

    def _open_job_tab(self, sid: str, cwd: str, force: bool = False) -> None:
        """Open the new tab running ``ccc start-job`` (``--force`` skips its re-ask)."""
        if terminal.start_job_in_new_tab(sid, force=force):
            self.notify(f"Starting job in a new tab: {colors.short_folder(cwd)}")
            self.refresh_data()
        else:
            self.notify(f"Run: ccc start-job {sid}", severity="warning", timeout=10)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a session row resumes / switches to its tab — same as ``r``.

        A FUTURE job launches, a LIVE session's tab is focused, a PARKED session
        resumes in a new tab (``action_resume`` handles each case). In column-edit
        mode Enter never reaches here — ``SessionTable.action_select_cursor`` edits
        the field instead — so this only fires in whole-row mode.
        """
        key = event.row_key.value
        if not key or key.startswith("__sep__") or self.store is None:
            return
        if self.store.get(key) is None:
            return
        self.action_resume()  # reads self._current, which is this highlighted row

    def on_session_table_draft_models_clicked(self, event: SessionTable.DraftModelsClicked) -> None:
        """A click on a draft's ``model`` (overseer ▸ exec) cell opens the inline editor.

        Non-draft rows and other columns never post this (the table filters). Ignored while
        already editing so a stray click can't re-enter edit mode mid-edit.
        """
        if getattr(self, "_editing", False):
            return
        sid = event.session_id
        if (store := self.store) is None or (session := store.get(sid)) is None:
            return
        if not session.draft:
            return
        self._current = sid  # the click already moved the table cursor here
        self.action_edit_session(focus_id="edit-overseer")

    # ---- two-key leader chords (`td`/`tf` = toggles, `ah` = aim-history) ----
    def on_key(self, event: events.Key) -> None:
        """Dispatch the registry's leader chords (see :data:`_CHORDS`).

        A leader key (``t``, ``a``, ``f``) is held until the next key. A matching
        follower fires that chord's action (a leader may carry several — ``td`` and
        ``tf`` both hang off ``t``); anything else (or a timeout) falls back to the
        leader's own action if it has one — so a bare ``a`` still edits the AIM and a
        bare ``t`` is a transparent prefix — and the key propagates.
        """
        if isinstance(self.screen, ModalScreen):
            return  # a modal owns its keys; never start a chord over it
        if self._chord_pending is not None:
            followers = _CHORDS[self._chord_pending]
            self._cancel_chord()
            if (action := followers.get(event.key)) is not None:
                event.prevent_default()
                event.stop()
                getattr(self, f"action_{action}")()
                return
            self._fire_chord_fallback(self._last_leader)
            return  # let the non-follower key propagate normally
        if event.key in _CHORDS:
            event.prevent_default()
            event.stop()
            self._chord_pending = event.key
            self._last_leader = event.key
            # Pure leaders wait longer so a Karabiner-delayed `f` follower still lands (see consts).
            window = (
                _CHORD_WINDOW_FALLBACK if _CHORD_FALLBACK.get(event.key) else _CHORD_WINDOW_PURE
            )
            self._chord_timer = self.set_timer(window, self._chord_timeout)

    def _fire_chord_fallback(self, leader: str | None) -> None:
        """Run a leader's standalone action (e.g. `a` → edit the AIM), if it has one."""
        if leader and (fallback := _CHORD_FALLBACK.get(leader)):
            getattr(self, f"action_{fallback}")()

    def _cancel_chord(self) -> None:
        self._chord_pending = None
        if self._chord_timer is not None:
            self._chord_timer.stop()
            self._chord_timer = None

    def _chord_timeout(self) -> None:
        leader = self._chord_pending
        self._cancel_chord()
        if leader is None:
            return
        if _CHORD_FALLBACK.get(leader):  # leader has a standalone action → run it
            self._fire_chord_fallback(leader)
        else:  # a pure leader (e.g. `t`) released alone → show its menu of available chords
            self._notify_chord_menu(leader)

    def _toggle_state_label(self, action: str | None) -> str | None:
        """Live state of a stateful toggle for the `t` menu (None if it has no readable state).

        Lets the menu answer "is it on right now?" — most useful for `ti`, whose on/silent
        state is otherwise invisible (unlike td/tf, where the rows themselves show/hide).
        """
        if action == "toggle_finished":
            return "shown" if self._show_finished else "hidden"
        if action == "toggle_future":
            return "shown" if self._show_future else "hidden"
        if action == "toggle_idle":
            return "on" if idlenotify.is_enabled() else "silent"
        card_key = _CARD_TOGGLE_KEYS.get(action or "")
        if card_key is not None:
            # Off is "collapsed", not "hidden": the card's titled border-top line stays.
            return "expanded" if getattr(self.cfg, card_key) else "collapsed"
        return None

    def _notify_chord_menu(self, leader: str) -> None:
        """Toast (2s) the chords under *leader*, one per line, each with its live state, e.g.
        `t` → "td — … (now: hidden)\\ntf — … (now: shown)\\nti — … (now: on)"."""
        options: list[str] = []
        for cmd in commands.chords_for_leader(leader):
            state = self._toggle_state_label(cmd.action)
            options.append(f"{cmd.key} — {cmd.gloss}" + (f"  (now: {state})" if state else ""))
        if options:
            self.notify("Press:\n" + "\n".join(options), timeout=2.0)

    def action_toggle_finished(self) -> None:
        """Show or hide DONE (green) sessions — the `td` chord."""
        prev = self._show_finished
        self._show_finished = not prev
        self.refresh_data()
        self.notify("Done sessions shown." if self._show_finished else "Done hidden.")

        def undo_finished(prev: bool = prev) -> str | None:
            self._show_finished = prev
            return f"Undid: done sessions {'shown' if prev else 'hidden'} again."

        self._push_undo("show/hide done", undo_finished)

    def action_toggle_future(self) -> None:
        """Show or hide FUTURE (not-yet-started, blue) jobs — the `tf` chord."""
        prev = self._show_future
        self._show_future = not prev
        self.refresh_data()
        self.notify("Future jobs shown." if self._show_future else "Future jobs hidden.")

        def undo_future(prev: bool = prev) -> str | None:
            self._show_future = prev
            return f"Undid: future jobs {'shown' if prev else 'hidden'} again."

        self._push_undo("show/hide future", undo_future)

    def action_toggle_idle(self) -> None:
        """Mute or unmute Claude Code's idle 'waiting for input' popups — the `ti` chord.

        Flips ``agentPushNotifEnabled`` in Claude Code's settings.json (see
        :mod:`command_center.idlenotify`); global, and a running session may need a
        restart to apply it.
        """
        try:
            now_on = idlenotify.toggle()  # JSONDecodeError subclasses ValueError
        except (OSError, ValueError) as exc:
            self.notify(f"Could not toggle idle popups: {exc}", severity="error")
            return
        self.notify(
            "Idle popups ON — you'll be pinged when a session goes idle."
            if now_on
            else "Idle popups OFF — no more 'waiting' popups (restart a live session to apply)."
        )

        def undo_idle() -> str | None:
            try:
                now_on = idlenotify.toggle()
            except (OSError, ValueError) as exc:
                return f"Couldn't undo the idle-popup toggle: {exc}"
            return "Undid: idle popups " + ("ON again." if now_on else "OFF again.")

        self._push_undo("idle-popups toggle", undo_idle)

    def _has_work_account(self) -> bool:
        """True when a non-default ``work`` account is configured in ``claude_accounts``.

        Without one there is nothing to populate the work card (the statusline SKIPS a
        write whose ``CLAUDE_CONFIG_DIR`` matches no configured account), so the card is
        hidden rather than shown permanently empty.
        """
        return "work" in config.parse_claude_accounts(self.cfg.claude_accounts)

    def _codex_private_home(self) -> Path | None:
        """The second configured ``CODEX_HOME`` (``codex_home_private``), or ``None``.

        Read off the already-loaded ``self.cfg`` (which ``action_settings`` and
        ``_toggle_usage_card`` refresh in place) rather than via
        :func:`config.codex_home_private`, so the 5 s render tick costs no config read.
        """
        raw = self.cfg.codex_home_private.strip()
        return Path(raw).expanduser() if raw else None

    def _codex_homes(self) -> dict[str, Path]:
        """Label → ``CODEX_HOME`` for every configured Codex login (see config.codex_homes)."""
        homes = {"default": config.codex_home()}
        private = self._codex_private_home()
        if private is not None:
            homes["private"] = private
        return homes

    def _has_codex_private(self) -> bool:
        """True when a second ``CODEX_HOME`` is configured — otherwise that card is hidden.

        Without one there is nothing to populate it (no auth.json, no rollout files), so
        the card is removed outright rather than shown permanently empty — the same rule
        the Claude work card follows.
        """
        return self._codex_private_home() is not None

    def _subscription_ends(self) -> dict[str, str]:
        """Card → its configured subscription end (``subscription_ends``); ``{}`` by default.

        Parsed off the already-loaded ``self.cfg`` on each render tick — four entries of
        pure string work, no file read — so a config edit lands without a restart.
        """
        return config.parse_subscription_ends(self.cfg.subscription_ends)

    def _set_claude_card_titles(self) -> None:
        """(Re)build both Claude cards' border titles.

        Rebuilt every tick rather than pinned once in ``on_mount`` because the optional
        subscription-end suffix moves under a running TUI: an ``auto`` date rolls to the
        next anniversary at midnight, and its underlying profile fetch lands out-of-band
        minutes after launch.
        """
        ends = self._subscription_ends()
        for card_id, label, action, card in (
            ("#usage", "private", "toggle_card_private", "claude_private"),
            ("#usage-work", "work", "toggle_card_work", "claude_work"),
        ):
            try:
                panel = self.query_one(card_id, Static)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                continue
            panel.border_title = (
                f"Claude ({label}) {accounts.card_glyph(label)}"
                f" / {commands.by_action(action).key}{usage.subscription_suffix(card, ends)}"
            )

    def _set_codex_card_titles(self) -> None:
        """(Re)build both Codex cards' border titles — each names its own ChatGPT account.

        Two cards for the same product would be indistinguishable without the account, so
        the title carries an abbreviated e-mail (``Codex first…@example.org / t3``)
        read from that home's ``auth.json``. The lookup is mtime-cached, so this is cheap
        enough to re-run on every render tick — which it must be, since a `codex login`
        can change the account under a running TUI.
        """
        ends = self._subscription_ends()
        for card_id, home, action, card in (
            ("#usage-codex", config.codex_home(), "toggle_card_codex", "codex"),
            (
                "#usage-codex-private",
                self._codex_private_home(),
                "toggle_card_codex_private",
                "codex_private",
            ),
        ):
            try:
                panel = self.query_one(card_id, Static)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                continue
            panel.border_title = usage.codex_card_title(
                home,
                commands.by_action(action).key,
                usage.subscription_suffix(card, ends, home),
            )

    def _toggle_usage_card(
        self, key: str, label: str, *, also: str | None = None, announce: bool = True
    ) -> None:
        """Expand/collapse a usage card, persist it, re-render — reload-modify-save.

        ``self.cfg`` is cached at ``__init__`` and ``save_config`` writes EVERY key, so
        saving that stale snapshot would clobber any Settings-screen edit made since
        launch. So reload the config fresh, flip *key* (and *also*, when a card gates a
        second key), save, adopt it as ``self.cfg``, then re-render the cards.
        """
        cfg = config.load_config()
        new_value = not getattr(cfg, key)
        setattr(cfg, key, new_value)
        if also is not None:
            setattr(cfg, also, new_value)
        try:
            config.save_config(cfg)
        except RuntimeError as err:
            self.notify(str(err), severity="error")
            return
        self.cfg = cfg
        self._update_usage()

        def undo_card(key: str = key, label: str = label, also: str | None = also) -> str | None:
            self._toggle_usage_card(key, label, also=also, announce=False)
            state = "expanded" if getattr(self.cfg, key) else "collapsed"
            return f"{label} card {state} again."

        self._push_undo(f"{label} card toggle", undo_card)
        if announce:
            self.notify(f"{label} card {'expanded' if new_value else 'collapsed'}.")

    def action_toggle_card_private(self) -> None:
        """Expand/collapse the Claude (private) usage card — the `t1` chord."""
        self._toggle_usage_card("usage_card_private", "Claude (private)")

    def action_toggle_card_work(self) -> None:
        """Expand/collapse the Claude (work) usage card — the `t2` chord.

        Flipping the flag on a machine with no ``work`` account would show nothing (that
        card is hidden outright, title line included), so say why instead of no-oping.
        """
        if not self._has_work_account():
            # markup=False: the example contains ``[...]`` which Textual would otherwise
            # parse as console markup (``~/.claude"`` is not a valid tag → MarkupError
            # crashes the toast render). Render it as literal text instead.
            self.notify(
                "No `work` account configured. Add it to claude_accounts, e.g. "
                '["private=~/.claude", "work=~/.claude-work"]',
                severity="warning",
                markup=False,
            )
            return
        self._toggle_usage_card("usage_card_work", "Claude (work)")

    def action_toggle_card_codex(self) -> None:
        """Expand/collapse the Codex usage card — the `t3` chord."""
        self._toggle_usage_card("usage_card_codex", "Codex")

    def action_toggle_card_codex_private(self) -> None:
        """Expand/collapse the second Codex usage card — the `t5` chord.

        Flipping the flag on a machine with no ``codex_home_private`` would show nothing
        (that card is hidden outright, title line included), so say why instead of
        no-oping — exactly like `t2` without a `work` Claude account.
        """
        if not self._has_codex_private():
            # markup=False: the example path contains no markup today, but the same
            # ``[...]``-in-a-toast crash guard as action_toggle_card_work applies.
            self.notify(
                "No codex_home_private configured. Point it at a second CODEX_HOME, e.g. "
                'codex_home_private = "~/.codex-private" (create that login with '
                "CODEX_HOME=~/.codex-private codex login).",
                severity="warning",
                markup=False,
            )
            return
        self._toggle_usage_card("usage_card_codex_private", "Codex (second login)")

    def action_toggle_card_copilot(self) -> None:
        """Expand/collapse the Copilot usage card — the `t4` chord.

        Flips BOTH the render gate (``usage_card_copilot``) AND the network-fetch gate
        (``copilot_usage``) to the same value, so hiding the card also stops paying for
        the `gh` billing call. The ``copilot_usage=true, usage_card_copilot=false`` mix
        (fetch but do not show) stays expressible by hand-editing the config.
        """
        self._toggle_usage_card("usage_card_copilot", "Copilot", also="copilot_usage")

    def action_toggle_card_nixos_overseer_supervised(self) -> None:
        """Expand/collapse the nixos overseer supervised card — the `to` chord."""
        self._toggle_usage_card("card_nixos_overseer_supervised", "nixos overseer supervised")

    def action_toggle_card_nixos_overseer_tier_a(self) -> None:
        """Expand/collapse the nixos overseer tier_a card — the `ta` chord."""
        self._toggle_usage_card("card_nixos_overseer_tier_a", "nixos overseer tier_a")

    def action_aim_history(self) -> None:
        """Show the selected session's AIM progression — the `ah` chord / /aim-history."""
        from datetime import datetime  # pylint: disable=import-outside-toplevel

        if not (sid := self._current) or (store := self.store) is None:
            return
        revisions = store.list_aim_history(sid)
        if not revisions and (session := store.get(sid)) and session.aim:
            # Pre-history session — show the live AIM as the sole revision.
            from ..models import AimRevision  # pylint: disable=import-outside-toplevel

            revisions = [
                AimRevision(
                    session.aim,
                    session.aim_score,
                    session.aim_changed_at or session.created_at,
                    session.short_aim,
                )
            ]
        if not revisions:
            self.notify("No AIM set for this session yet.")
            return
        body = Text()
        for index, rev in enumerate(revisions, 1):
            when = (
                datetime.fromtimestamp(rev.created_at / 1000).strftime("%Y-%m-%d %H:%M")
                if rev.created_at
                else "—"
            )
            score = f"{rev.score}%" if rev.score >= 0 else "—"
            current = index == len(revisions)
            row_style = f"bold {_GOLD}" if current else "grey62"
            if index > 1:
                body.append("\n")
            body.append(f"{index}. {when}  ·  ", style=row_style)
            score_style = (
                "bold red"
                if low_aim_score(rev.aim, rev.score, self.cfg.aim_score_threshold)
                else row_style
            )
            body.append(score, style=score_style)
            body.append(f"  {rev.aim}", style=row_style)
            if current:
                body.append("  ← current", style=row_style)
            if rev.short_aim:  # the cheap-model short label tracked for this revision
                body.append(f"\n      ↳ short: {rev.short_aim}", style="grey50")
        plural = "s" if len(revisions) != 1 else ""
        self.push_screen(TopicScreen(f"AIM history — {len(revisions)} revision{plural}", body))

    def action_subgoal_history(self) -> None:
        """Show the selected session's sub-goal evolution — the `sh` chord / /subgoal-history."""
        from datetime import datetime  # pylint: disable=import-outside-toplevel

        from rich.markup import escape  # pylint: disable=import-outside-toplevel

        if not (sid := self._current) or (store := self.store) is None:
            return
        revisions = store.list_subgoal_history(sid)
        if not revisions:
            self.notify("No sub-goal history for this session yet.")
            return
        lines: list[str] = []
        for index, rev in enumerate(revisions, 1):
            when = (
                datetime.fromtimestamp(rev.created_at / 1000).strftime("%Y-%m-%d %H:%M")
                if rev.created_at
                else "—"
            )
            checked = sum(1 for _, done in rev.items if done)
            if rev.drift_severity in ("low", "medium", "high"):
                why = f" — {escape(rev.drift_reason)}" if rev.drift_reason else ""
                drift = f"  [b #5fafff]● drift:{rev.drift_severity}[/]{why}"
            elif rev.drift_severity == "none":
                drift = "  [green]✓ no drift[/]"
            else:
                drift = "  [grey50]· pending[/]"
            row = (
                f"{index}. {when}  ·  {escape(rev.trigger)}  ·  AIM v{rev.aim_rev}  ·  "
                f"{checked}/{len(rev.items)}"
            )
            head_style = f"b {_GOLD}" if index == len(revisions) else "grey62"
            current = f"  [b {_GOLD}]← current[/]" if index == len(revisions) else ""
            lines.append(f"[{head_style}]{row}[/]{drift}{current}")
            for text, done in rev.items:
                color, mark = ("green", "✓") if done else ("grey50", "·")
                lines.append(f"     [{color}]{mark} {escape(text)}[/]")
        plural = "s" if len(revisions) != 1 else ""
        self.push_screen(
            TopicScreen(f"Sub-goal history — {len(revisions)} version{plural}", "\n".join(lines))
        )

    def action_open_obsidian(self) -> None:
        """Open the selected session's future-job file in Obsidian — the `oo` chord.

        Works on any row with a synced future-job file (drafts, primarily). A
        draft that hasn't synced yet (``future_file`` still unset) has nothing to
        open — this is the guaranteed fallback for the id-column link, which
        depends on terminal OSC 8 support (see `_draft_id_cell`).
        """
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is None or not session.future_file:
            self.notify("No Obsidian file for this session yet.", severity="warning")
            return
        uri = future_files.obsidian_uri(session.future_file)
        try:
            subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
                ["open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except (OSError, ValueError):
            self.notify("Could not open Obsidian.", severity="warning")

    def action_open_session_obsidian(self) -> None:
        """Open the selected session's full-conversation mirror in Obsidian — `os` chord.

        Works for parked, running and done sessions once the mirror pass has written
        the file under ``sessions_dir`` (the daemon / any lifecycle command syncs it);
        until then there is nothing to open and ``os`` just notifies.
        """
        from .. import mirrors  # lazy: keep TUI startup free of the mirror machinery

        if not (sid := self._current):
            return
        hit = mirrors.session_file_path(self.cfg, sid)
        if hit is None:
            self.notify("No session file yet (mirror sync pending).", severity="warning")
            return
        uri = future_files.obsidian_uri(hit.vault_relpath)
        try:
            subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
                ["open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except (OSError, ValueError):
            self.notify("Could not open Obsidian.", severity="warning")

    def action_peek(self) -> None:
        """Open the peek panel for the selected session — the `sp` chord.

        The SAME floating panel the global s+p Karabiner chord shows in a session
        tab, but driven from the TUI cursor: it spawns `ccc peek --session <id>` for
        the highlighted row, so it always targets that exact row without any tty/uuid
        detection. Works for parked, running and done sessions.
        """
        from .. import spawn  # lazy: keep TUI startup free of the spawn helper

        if not (sid := self._current):
            return
        if not spawn.spawn_ccc(["peek", "--session", sid]):
            self.notify("Could not open the peek panel.", severity="warning")

    def action_settings(self) -> None:
        def done(_changed: bool | None) -> None:
            self.cfg = config.load_config()
            self._apply_split()
            self.refresh_data()

        self.push_screen(SettingsScreen(), done)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def _edit(  # pylint: disable=too-many-arguments  # a field editor legitimately has many facets
        self,
        prompt: str,
        initial: str,
        apply: Callable[[str], None],
        suggester: Suggester | None = None,
        *,
        multiline: bool = False,
        after: Callable[[], None] | None = None,
    ) -> None:
        """Push the field editor; on close apply (if saved) + refresh, then run *after*.

        *after* runs whether the editor was saved or cancelled, so callers can continue
        a multi-step flow.
        """

        def callback(value: str | None) -> None:
            if value is not None:
                apply(value)
                self.refresh_data()
            if after is not None:
                after()

        self.push_screen(InputScreen(prompt, initial, suggester, multiline=multiline), callback)

    def action_edit_aim(self, after: Callable[[], None] | None = None) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)

        def _save(value: str) -> None:
            # Route through the chokepoint: clears the stale auto checklist + sets an
            # instant score; then refine the score out-of-band (detached, non-blocking).
            if store.set_aim(sid, value) and self.cfg.aim_score_on_set:
                from .. import spawn  # pylint: disable=import-outside-toplevel

                spawn.spawn_ccc(["score-aim", "--session", sid])

        original = (session.first_aim or "") if session else ""
        prompt = "This session is done when:"
        if self._aim_first and original and original != (session.aim if session else None):
            # The column renders revision (1) but this editor writes the CURRENT AIM (a new
            # revision) — say so, and point at the `e` form's `/aim (1):` line for the other.
            prompt += "  (sets the CURRENT aim; rewrite (1) in the `e` form)"
        self._edit(
            prompt,
            (session.aim or "") if session else "",
            _save,
            multiline=True,
            after=after,
        )

    def action_edit_next(self) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        self._edit(
            "Next step (type @ for tags like @waiting, @susi):",
            (session.next_step or "") if session else "",
            lambda v: store.update_fields(sid, next_step=v, next_step_source="user"),
            TagSuggester(),
            multiline=True,
        )

    def action_edit_blocked(self, after: Callable[[], None] | None = None) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        self._edit(
            "Blocked on / waiting for (type @ for tags):",
            (session.blocked_on or "") if session else "",
            lambda v: store.update_fields(sid, blocked_on=v or None),
            TagSuggester(),
            multiline=True,
            after=after,
        )

    def action_edit_deadline(self, after: Callable[[], None] | None = None) -> None:
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        self._edit(
            "Deadline (YYYY-MM-DD, blank to clear):",
            (session.deadline or "") if session else "",
            lambda v: store.update_fields(sid, deadline=v or None),
            after=after,
        )

    def action_edit_subgoals(self, after: Callable[[], None] | None = None) -> None:
        """Edit the whole sub-goal checklist as newline-separated text.

        One sub-goal per line; ticks carry over to unchanged lines (merge by text). A
        manual edit pins the list (non-adaptive, provenance ``manual``) and, on a real
        change, spawns the impartial drift checker — mirroring ``ccc subgoals``.
        """
        if not (sid := self._current) or (store := self.store) is None:
            return
        initial = "\n".join(s.text for s in store.list_subgoals(sid))
        self._edit(
            "Sub-goals — one per line (ticks carry over by text):",
            initial,
            lambda value: self._commit_subgoals(sid, value),
            multiline=True,
            after=after,
        )

    def action_edit_progress(self, after: Callable[[], None] | None = None) -> None:
        """Set/clear the manual progress-bar percentage (Enter on the progress column).

        A set percentage overrides the sub-goal-derived bar everywhere it renders;
        blank returns the bar to auto (the checklist ratio). Marking the session done
        clears the override.
        """
        if not (sid := self._current) or (store := self.store) is None:
            return
        session = store.get(sid)
        current = ""
        if session is not None and session.manual_progress is not None:
            current = str(session.manual_progress)
        self._edit(
            "Progress % (0-100; blank = auto from sub-goals):",
            current,
            lambda value: self._commit_progress(sid, value),
            after=after,
        )

    def _commit_progress(self, sid: str, raw: str) -> None:
        """Save a manual progress-bar percentage (invalid → notify, value unchanged)."""
        if (store := self.store) is None:
            return
        try:
            pct = parse_manual_progress(raw)
        except ValueError:
            self.notify(
                f"Invalid progress {raw!r} — expected 0-100 (blank = auto from "
                "sub-goals); kept unchanged.",
                severity="warning",
            )
            return
        store.update_fields(sid, manual_progress=pct)

    def _commit_subgoals(self, sid: str, raw: str) -> None:
        """Replace the checklist with *raw*'s non-empty lines (ticks carry over by text).

        The single save path behind both the inline ``e`` form and the ``s`` editor: a
        manual edit marks the list ``source="user"`` (provenance ``manual``) and, on a
        real change, spawns the impartial drift checker — mirroring ``ccc subgoals``.
        """
        if (store := self.store) is None:
            return
        items = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        changed = store.set_subgoals(sid, items, source="user", merge=True)
        if changed and self.cfg.drift_check:
            from .. import spawn  # pylint: disable=import-outside-toplevel

            spawn.spawn_ccc(["check-drift", "--session", sid])

    def action_edit_session(self, focus_id: str | None = None) -> None:
        """Enter inline edit mode for the selected session.

        *focus_id* lands the cursor on a specific field (e.g. ``edit-overseer`` from a
        click on the models cell); ``None`` keeps the default (AIM / column-cursor field).
        """
        if not (sid := self._current) or (store := self.store) is None:
            return
        if (session := store.get(sid)) is None:
            return
        self._edit_sid = sid
        # Model choices must be valid LLM_CHOICES for the Selects to accept them (a legacy
        # or non-draft row may carry None) — coerce to the default so the Select never errors.
        overseer = session.llm_overseer if session.llm_overseer in LLM_CHOICES else DEFAULT_LLM
        executor = session.llm_exec if session.llm_exec in LLM_CHOICES else DEFAULT_LLM
        # The account Select only accepts a configured label; coerce an unknown/legacy
        # config_dir to the first configured label so it never raises InvalidSelectValue.
        account_dirs = config.claude_config_dirs()
        account = accounts.account_label(session.config_dir or "")
        if account not in account_dirs:
            account = next(iter(account_dirs), "private")
        self._edit_original = {
            "aim": session.aim or "",
            "next": session.next_step or "",
            "deadline": session.deadline or "",
            "progress": "" if session.manual_progress is None else str(session.manual_progress),
            "block": session.blocked_on or "",
            "prompt": session.prompt or "",
            "overseer": overseer,
            "executor": executor,
            "account": account,
            "scheduled": session.start_date or "",
            "depends": session.depends_on or "",
            "subgoals": "\n".join(s.text for s in store.list_subgoals(sid)),
            "aim1": "",  # filled below when a distinct first revision exists
        }
        # Dependency picker: seed the button label + the pending value from the row.
        self._edit_depends_pending = session.depends_on or ""
        self.query_one("#edit-depends", Button).label = self._dep_button_label(
            session.depends_on or ""
        )
        # First AIM line + the `/aim (N):` label mirror the read-only view, so entering
        # edit mode shows the same lines. `/aim (1)` is its own editable field: it rewrites
        # revision 1 in place (see Store.set_first_aim) instead of setting the current AIM.
        revisions = store.list_aim_history(sid)
        aim1_row = self.query_one("#edit-aim1-row", Horizontal)
        if len(revisions) >= 2:
            self._edit_original["aim1"] = revisions[0].aim or ""
            self.query_one("#edit-aim1", TextArea).text = self._edit_original["aim1"]
            aim1_row.styles.display = "block"
            current_index = len(revisions)
        else:
            # Clear it too: a hidden field left holding the PREVIOUS session's first AIM
            # would read as an edit against this session's blank original on commit.
            self.query_one("#edit-aim1", TextArea).text = ""
            aim1_row.styles.display = "none"
            current_index = 1
        self.query_one("#edit-aim-label", Label).update(f"/aim ({current_index}): ")
        self.query_one("#edit-aim", TextArea).text = self._edit_original["aim"]
        self.query_one("#edit-next", Input).value = self._edit_original["next"]
        self.query_one("#edit-deadline", Input).value = self._edit_original["deadline"]
        self.query_one("#edit-progress", Input).value = self._edit_original["progress"]
        self.query_one("#edit-block", Input).value = self._edit_original["block"]
        self.query_one("#edit-prompt", TextArea).text = self._edit_original["prompt"]
        self.query_one("#edit-overseer", Select).value = self._edit_original["overseer"]
        self.query_one("#edit-executor", Select).value = self._edit_original["executor"]
        self.query_one("#edit-account", Select).value = self._edit_original["account"]
        self.query_one("#edit-scheduled", Input).value = self._edit_original["scheduled"]
        self.query_one("#edit-subgoals", TextArea).text = self._edit_original["subgoals"]
        marks = importance_marks(session.importance) or "—"
        self.query_one("#edit-important", Static).update(f"! important: {marks}")

        folder_button = self.query_one("#edit-folder", Button)
        folder_button.label = colors.short_folder(session.cwd)
        self.query_one("#edit-folder-row", Horizontal).styles.display = (
            "block" if session.draft else "none"
        )
        self.query_one("#edit-prompt-row", Horizontal).styles.display = (
            "block" if session.draft else "none"
        )
        # Model pair is a future-job concept — hide (and skip in the Tab chain) otherwise.
        for row_id in ("edit-overseer-row", "edit-executor-row"):
            self.query_one(f"#{row_id}", Horizontal).styles.display = (
                "block" if session.draft else "none"
            )
        # Account row is draft-only AND only meaningful when >1 account is configured.
        self.query_one("#edit-account-row", Horizontal).styles.display = (
            "block" if session.draft and len(config.claude_config_dirs()) > 1 else "none"
        )
        # Fixed start date (draft-only, like the model rows above it).
        self.query_one("#edit-scheduled-row", Horizontal).styles.display = (
            "block" if session.draft else "none"
        )

        # Swap ONLY the read-only field lines for the inline editor (same lines, now
        # editable); the Status/progress above and the sub-goal checklist below stay put.
        self.query_one("#detail-fields-view").styles.display = "none"
        self.query_one("#detail-edit").styles.display = "block"
        self._editing = True
        # Compact the head while editing: the models/account readout, the Scheduled-for
        # line and the Prompt-to-run body all have editable rows in the form now — the
        # head keeps only what the form doesn't show, so each option renders once.
        subs = store.list_subgoals(sid)
        checked = sum(1 for s in subs if s.checked)
        self.query_one("#detail-head", DetailHead).update(
            self._head_text(session, Status(session.status), checked, len(subs), editing=True)
        )
        # Confine the Tab cycle to the detail pane: the read-only Status head joins it
        # (a focusable stop) while the table and the scroll container drop out, so
        # Tab wraps head → fields → head instead of escaping into the session list.
        self.query_one("#detail-head", DetailHead).can_focus = True
        self.query_one("#sessions", SessionTable).can_focus = False
        self.query_one("#detail-wrap", VerticalScroll).can_focus = False
        # Land on the caller's field, else the field the table's column cursor was on
        # (else the AIM line). A click on the models cell passes focus_id="edit-overseer".
        if focus_id is None:
            focus_id = "edit-aim"
            table = self.query_one("#sessions", SessionTable)
            if getattr(table, "_col_mode", False):
                if table.cursor_column == _NEXT_COL:
                    focus_id = "edit-next"
                elif table.cursor_column == _PROGRESS_COL:
                    focus_id = "edit-progress"
        # Focus only after the display swap has painted: focusing while #detail-edit is
        # still hidden skips the scroll-into-view, leaving the tinted field below the
        # fold — no visible cursor until the first Tab.
        self.call_after_refresh(self.query_one(f"#{focus_id}").focus)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Keep the focused edit field's whole row — its label included — in view.

        The default focus scroll only reveals the focused widget itself; a taller-
        than-the-pane TextArea (a long ``prompt:``) would leave its label and all
        context above off-screen. Scrolling the row with ``top=True`` pins the row's
        first line (where the label sits) when the row cannot fit; the Status-head
        stop scrolls home so Tab alone reaches the very top of the pane.
        """
        if not self._editing:
            return
        widget = event.widget
        if isinstance(widget, DetailHead):
            self.query_one("#detail-wrap", VerticalScroll).scroll_home(animate=False)
            return
        form = self.query_one("#detail-edit", EditForm)
        row = widget
        while row.parent is not None and row.parent is not form:
            parent = row.parent
            if not isinstance(parent, Vertical | Horizontal | EditForm):
                return  # focus moved outside the inline editor (e.g. a modal)
            row = parent
        if row.parent is not form:
            return
        wrap = self.query_one("#detail-wrap", VerticalScroll)
        oversized = row.outer_size.height >= wrap.scrollable_content_region.height
        row.scroll_visible(animate=False, top=oversized)

    def action_exit_edit(self) -> None:
        """Save changed inline-edit fields and return to the session table.

        Guards against accidentally clearing the AIM: if the edit would blank a
        previously-set AIM, a confirm dialog pops first (the only warning case) —
        the previous AIM stays in aim-history either way.
        """
        if not self._editing:
            return
        sid = self._edit_sid
        if sid is None or self.store is None or self.store.get(sid) is None:
            self._finish_edit()
            return
        aim = self.query_one("#edit-aim", TextArea).text
        if not aim.strip() and self._edit_original.get("aim", "").strip():

            def decided(confirmed: bool | None) -> None:
                if confirmed:
                    self._commit_edit(sid)
                    self._finish_edit()
                else:  # keep editing — land back on the AIM so it can be restored
                    self.query_one("#edit-aim").focus()

            self.push_screen(
                ConfirmScreen(
                    "Save an empty AIM? This clears the done-condition so progress can no "
                    "longer be graded. The previous AIM is kept in aim-history.",
                    yes_label="Save empty",
                    no_label="Keep editing",
                ),
                decided,
            )
            return
        self._commit_edit(sid)
        self._finish_edit()

    def _commit_edit(self, sid: str) -> None:
        """Write every changed inline-edit field for *sid* (no view changes)."""
        if (store := self.store) is None or (session := store.get(sid)) is None:
            return
        original = self._edit_original
        aim = self.query_one("#edit-aim", TextArea).text
        aim1 = self.query_one("#edit-aim1", TextArea).text
        next_step = self.query_one("#edit-next", Input).value
        deadline = self.query_one("#edit-deadline", Input).value
        progress = self.query_one("#edit-progress", Input).value
        block = self.query_one("#edit-block", Input).value
        prompt = self.query_one("#edit-prompt", TextArea).text
        subgoals = self.query_one("#edit-subgoals", TextArea).text

        if aim != original.get("aim", ""):
            if store.set_aim(sid, aim) and self.cfg.aim_score_on_set:
                from .. import spawn  # pylint: disable=import-outside-toplevel

                spawn.spawn_ccc(["score-aim", "--session", sid])
        # The first AIM is rewritten in place (no new revision, current AIM untouched).
        # Blanking it is refused rather than confirmed: an empty history row would erase
        # where the goal started, and the current AIM above already covers "no AIM".
        if aim1 != original.get("aim1", ""):
            if aim1.strip():
                # On a real rewrite the current AIM's short label went stale with it (it is
                # generated from the original as a hint, and it is what the `/aim` COLUMN
                # renders) — set_first_aim dropped it, so regenerate it here.
                if store.set_first_aim(sid, aim1) and self.cfg.short_aim:
                    from .. import spawn  # pylint: disable=import-outside-toplevel

                    spawn.spawn_ccc(["short-aim", "--session", sid])
            else:
                self.notify(
                    "The first AIM cannot be emptied — /aim (1) kept unchanged.",
                    severity="warning",
                )
        if next_step != original.get("next", ""):
            store.update_fields(sid, next_step=next_step, next_step_source="user")
        if deadline != original.get("deadline", ""):
            store.update_fields(sid, deadline=deadline or None)
        if progress != original.get("progress", ""):
            self._commit_progress(sid, progress)
        if block != original.get("block", ""):
            store.update_fields(sid, blocked_on=block or None)
        if subgoals != original.get("subgoals", ""):
            self._commit_subgoals(sid, subgoals)
        if session.draft and prompt != original.get("prompt", ""):
            store.update_fields(sid, prompt=prompt or None)
        # Model pair (drafts only): the Selects only ever hold a valid LLM_CHOICES value,
        # so _commit_model (which still validates via expand_llm_choice, kept for the CLI)
        # simply saves it. The isinstance guard also narrows Select.value off Select.BLANK.
        if session.draft:
            overseer = self.query_one("#edit-overseer", Select).value
            executor = self.query_one("#edit-executor", Select).value
            if isinstance(overseer, str) and overseer != original.get("overseer", ""):
                self._commit_model(sid, "llm_overseer", "overseer", overseer)
            if isinstance(executor, str) and executor != original.get("executor", ""):
                self._commit_model(sid, "llm_exec", "executor", executor)
            account = self.query_one("#edit-account", Select).value
            if isinstance(account, str) and account != original.get("account", ""):
                account_dir = accounts.account_config_dir(account)
                if account_dir:
                    store.update_fields(sid, config_dir=account_dir)
            # Fixed start date: an ISO date sinks the job into SCHEDULED, blank clears
            # it back to plain FUTURE; anything unparseable is rejected, not saved.
            scheduled = self.query_one("#edit-scheduled", Input).value.strip()
            if scheduled != original.get("scheduled", ""):
                if scheduled and parse_iso_date(scheduled) is None:
                    self.notify(
                        f"Invalid scheduled date {scheduled!r} — expected YYYY-MM-DD "
                        "(kept unchanged).",
                        severity="warning",
                    )
                else:
                    store.update_fields(sid, start_date=scheduled or None)
        # Dependency (every session): commit the picker's pending value if it changed.
        # Belt-and-suspenders re-check the cycle guard (the picker already pre-filters).
        pending = self._edit_depends_pending
        if pending is not None and pending != original.get("depends", ""):
            if pending and deps.would_create_cycle(store.get, sid, pending):
                self.notify(
                    "That dependency would create a cycle — kept unchanged.", severity="warning"
                )
            else:
                store.update_fields(sid, depends_on=pending or None)

    def _dep_button_label(self, dep_id: str) -> str:
        """The ``/depends-on`` button label for *dep_id* (``— none —`` when unset)."""
        dep = (dep_id or "").strip()
        if not dep or self.store is None:
            return "— none —"
        parent = self.store.get(dep)
        if parent is None:
            return f"{future_files.display_hash(dep)} — (missing)"
        aim = _first_line(display_aim(parent, self._aim_first)) or "—"
        return f"{future_files.display_hash(dep)} {colors.short_folder(parent.cwd)} — {aim[:36]}"

    def _dep_option_label(self, session: Session) -> str:
        """One candidate's row label in the dependency picker (hash · repo · status · aim)."""
        status = "FUTURE" if session.draft else session.status
        aim = _first_line(display_aim(session, self._aim_first)) or "—"
        return (
            f"{future_files.display_hash(session.session_id)}  "
            f"{colors.short_folder(session.cwd)}  {status}  — {aim[:36]}"
        )

    def _commit_model(self, sid: str, column: str, label: str, raw: str) -> None:
        """Validate an edited model field and save it (invalid → notify, value unchanged)."""
        if (store := self.store) is None:
            return
        canonical = expand_llm_choice(raw)
        if canonical is None:
            self.notify(
                f"Invalid {label} model {raw!r} — expected one of "
                f"{', '.join(LLM_CHOICES)} (kept unchanged).",
                severity="warning",
            )
            return
        store.update_fields(sid, **{column: canonical})

    def _finish_edit(self) -> None:
        """Leave edit mode, refresh, and return focus to the session table."""
        self._leave_edit_mode()
        if self.store is not None:
            self.refresh_data()
        self.query_one("#sessions", DataTable).focus()

    def _leave_edit_mode(self) -> None:
        """Hide the inline editor and clear its session-local state."""
        self._editing = False
        self._edit_sid = None
        self._edit_original = {}
        self._edit_depends_pending = None
        self.query_one("#detail-fields-view").styles.display = "block"
        self.query_one("#detail-edit").styles.display = "none"
        # Undo the edit-mode focus fences: head back to read-only, table + scroll
        # container focusable again (action_edit_session flipped all three).
        self.query_one("#detail-head", DetailHead).can_focus = False
        self.query_one("#sessions", SessionTable).can_focus = True
        self.query_one("#detail-wrap", VerticalScroll).can_focus = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Open the folder / dependency picker from the inline edit form."""
        if event.button.id == "edit-folder":
            event.stop()
            self._choose_edit_folder()
        elif event.button.id == "edit-depends":
            event.stop()
            self._choose_edit_depends()

    def _choose_edit_depends(self) -> None:
        """Open the dependency picker for the session currently edited inline.

        Candidates = every session that is not done, not archived, not the edited session,
        and would not create a cycle (:func:`deps.would_create_cycle`). The picker returns
        "" (clear), a candidate UUID, or None (cancel — no change); a pick updates the
        pending value + button label, committed later by :meth:`_commit_edit`.
        """
        if not self._editing or not (sid := self._edit_sid) or (store := self.store) is None:
            return
        candidates: list[tuple[str, str]] = []
        for cand in store.list_sessions():  # excludes archived
            if cand.session_id == sid or cand.done:
                continue
            if deps.would_create_cycle(store.get, sid, cand.session_id):
                continue
            candidates.append((cand.session_id, self._dep_option_label(cand)))

        def got(result: str | None) -> None:
            if result is None or self._edit_sid != sid:
                self._focus_edit_depends()
                return
            self._edit_depends_pending = result  # "" clears, else a candidate UUID
            self.query_one("#edit-depends", Button).label = self._dep_button_label(result)
            self._focus_edit_depends()

        self.push_screen(DependencyPickerScreen(candidates), got)

    def _focus_edit_depends(self) -> None:
        """Return focus to the inline dependency button if edit mode is still active."""
        if self._editing:
            self.query_one("#edit-depends", Button).focus()

    def _choose_edit_folder(self) -> None:
        """Choose a replacement folder for the draft currently edited inline."""
        if not self._editing or not (sid := self._edit_sid) or (store := self.store) is None:
            return
        session = store.get(sid)
        if session is None or not session.draft:
            return
        cats = repos.categories()
        if not cats:
            self.notify(
                f"No repo categories under {repos.repo_root(self.cfg) or '<no repo_root set>'}.",
                severity="warning",
            )
            self._focus_edit_folder()
            return

        def got_category(category: str | None) -> None:
            if not category:
                self._focus_edit_folder()
                return

            def got_repo(result: str | None) -> None:
                if (
                    result
                    and result != _NEW_REPO_SENTINEL
                    and os.path.isabs(result)
                    and self._edit_sid == sid
                    and self.store is not None
                ):
                    self.store.update_fields(sid, cwd=result)
                    self.query_one("#edit-folder", Button).label = colors.short_folder(result)
                self._focus_edit_folder()

            self.push_screen(RepoPickerScreen(category, repos.repos_in(category)), got_repo)

        self.push_screen(CategoryPickerScreen(cats), got_category)

    def _focus_edit_folder(self) -> None:
        """Return focus to the inline folder button if edit mode is still active."""
        if self._editing:
            self.query_one("#edit-folder", Button).focus()


def _reexec_argv() -> list[str]:
    """The argv to re-exec ccc in place after a ``restart-tui`` request.

    Prefer the exact same program image (``sys.argv[0]``) when it is an absolute,
    executable path (the ``ccc`` console entry point) so the identical binary re-runs;
    otherwise fall back to resolving ``ccc`` on ``$PATH`` (e.g. launched via
    ``python -m command_center``, whose ``argv[0]`` is a non-executable module path).
    """
    argv0 = sys.argv[0]
    if os.path.isabs(argv0) and os.access(argv0, os.X_OK):
        return list(sys.argv)
    return ["ccc", *sys.argv[1:]]


def _reexec_in_place() -> None:
    """Replace this process image to restart the TUI in the same terminal tab.

    Runs only after ``App.run()`` has returned — Textual has restored the terminal by
    then — so the re-exec'd ccc starts on a clean screen. Does not return on success.
    """
    argv = _reexec_argv()
    if os.path.isabs(argv[0]):
        os.execv(argv[0], argv)  # exact same program image (path exists)
    else:
        os.execvp(argv[0], argv)  # resolve the bare `ccc` on $PATH


def run() -> int:
    """Launch the TUI, re-exec'ing in place if it asked to restart itself."""
    app = CommandCenterApp()
    app.run()
    if app.restart_requested:
        _reexec_in_place()  # replaces this process — does not return on success
    return 0
