"""``ccc ls`` — a fast, non-interactive, clickable list of every tracked session.

Mirrors the look of a coloured repo list: coloured clickable folders
(``openterm://`` under iTerm2/WezTerm), aligned columns, graceful degradation
to plain text when piped or under ``NO_COLOR``.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

from .. import accounts, cachettl, config, resume, tabsymbol
from ..adapters.base import Adapter
from ..adapters.claude import ClaudeAdapter
from ..core import Row, build_rows
from ..future_files import display_hash, obsidian_uri
from ..links import folder_link, osc8_link
from ..models import (
    HALTED_RESUME_ICON,
    STATUS_ICON,
    Status,
    aim_score_pct,
    deadline_badge,
    display_aim,
    done_bar_parts,
    drift_unresolved,
    effective_progress,
    empty_track_tint,
    humanize_age,
    low_aim_score,
    model_effort_cell,
    models_readout,
    progress_bar,
    short_id,
    version_column_text,
    xterm_rgb,
)
from ..store import Store

# 256-colour codes per status and per deadline severity.
_STATUS_COLOR: dict[Status, int] = {
    Status.WORKING: 40,
    Status.WAITING_INPUT: 214,
    Status.HALTED: 196,  # rate-limit halt — red ||
    Status.WAITING_CODEX: 214,  # Codex quota exhausted — amber sleeping face
    Status.IDLE: 214,  # amber ❯ — waiting for your input, matches TUI #ffaf00
    Status.SNOOZED: 40,  # background task running while the session itself is idle
    Status.PARKED: 244,
    Status.DONE: 35,
    Status.FAILED: 196,
}
_SEVERITY_COLOR = {"green": 35, "amber": 214, "red": 196, "none": 244}
# Prompt-cache TTL countdown colours (see cachettl). Orange is 208 (#ff8700) to match the
# TUI's own #ff8700; green/red reuse the palette's clear bright green / severity red.
_CACHE_COLOR = {
    "green": 40,
    "orange": 208,
    "red": 196,
    # Busy rows show ▶ here — same colour as the ▶ status icon (_STATUS_COLOR[WORKING]).
    "running": _STATUS_COLOR[Status.WORKING],
}
_DIM = 240
_WHITE = 15
_BLACK = 16
_BLUE = 39  # the unresolved-drift dot
# The green ▶ appended to a red || when ccc will auto-revive that halted session on its
# account's reset. 40 = the WORKING green ("this one runs again by itself").
_RESUME_GREEN = 40
_FOLDER_WIDTH = 34
# Width of the id column: short_id's own fixed field, read from it so the two can't drift.
# A draft row shows the shorter 4-hex display hash and pads out to this — every column after
# the id must start at the same screen column on a draft and a live row alike.
_ID_WIDTH = len(short_id(""))
_OAI_BADGE_FG = 16
_OAI_BADGE_BG = 15


def _color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(code: int, text: str, enabled: bool) -> str:
    return f"\x1b[38;5;{code}m{text}\x1b[0m" if enabled else text


def _paint_on(fg: int, bg: tuple[int, int, int], text: str, enabled: bool) -> str:
    """Like :func:`_paint` but bold with an explicit truecolor background."""
    r, g, b = bg
    return f"\x1b[1;38;5;{fg};48;2;{r};{g};{b}m{text}\x1b[0m" if enabled else text


def _paint_on256(fg: int, bg: int, text: str, enabled: bool) -> str:
    """Bold *fg* on a palette background — same palette entry a neighbouring glyph uses as
    foreground, so the two cells render pixel-identically."""
    return f"\x1b[1;38;5;{fg};48;5;{bg}m{text}\x1b[0m" if enabled else text


def _paint_oai_badge(enabled: bool) -> str:
    if not enabled:
        return "OAI"
    return f"\x1b[1;38;5;{_OAI_BADGE_FG};48;5;{_OAI_BADGE_BG}mOAI\x1b[0m"


def _truncate_left(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1) :]


def _collapse_home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def _render_row(
    row: Row,
    enabled: bool,
    warn_days: int,
    aim_threshold: int,
    aim_first: bool = True,
    account_markers: dict[str, str] | None = None,
    adapter: Adapter | None = None,
    resume_armed_ids: frozenset[str] = frozenset(),
) -> list[str]:
    session = row.session
    status = row.status
    icon = _paint(_STATUS_COLOR.get(status, _DIM), STATUS_ICON.get(status, "?"), enabled)
    # A halted session ccc will auto-revive once its account's limit resets gets a green ▶
    # after the red || (models.HALTED_RESUME_ICON). Bare || = stranded until you resume it.
    if session.session_id in resume_armed_ids:
        icon += _paint(_RESUME_GREEN, HALTED_RESUME_ICON, enabled)

    display = _truncate_left(_collapse_home(session.cwd or "?"), _FOLDER_WIDTH)
    folder = folder_link(session.cwd or ".", label=display, width=_FOLDER_WIDTH, color=enabled)
    # Per-repo colored badge before the folder (matches the TUI + the user's tabs): a live
    # row shows its iTerm tab's claimed emoji, every other row the deterministic per-repo one.
    badge_cell = tabsymbol.cell_for(session.iterm_session_id, session.cwd or "", live=row.is_open)

    fraction = effective_progress(session.manual_progress, row.checked, row.total)
    pct = f"{int(round(fraction * 100)):3d}%" if fraction is not None else "  · "
    status_color = _STATUS_COLOR.get(status, _DIM)
    if session.aim_met and not session.done and status is not Status.DONE and not session.draft:
        # Impartial checker judged the AIM fulfilled: stamp a red DONE inside the bar (fill
        # still visible on both sides). Distinct from the human ✓/FINISHED (an active row).
        # The DONE bar's filled cells are SOLID █ (done_bar_parts) and a filled-cell letter's
        # background is the SAME palette entry the █ glyphs use as foreground, so letter cells
        # and bar cells render pixel-identically — no seam. Empty-cell letters get the faint
        # ░-average tint. White letters on a red fill, black on amber, for contrast.
        left, done_word, right, fills = done_bar_parts(fraction, 10)
        red = _SEVERITY_COLOR["red"]
        tint = empty_track_tint(xterm_rgb(status_color))
        fill_fg = {196: _WHITE, 214: _BLACK}.get(status_color, red)
        word = "".join(
            _paint_on256(fill_fg, status_color, ch, enabled)
            if filled
            else _paint_on(red, tint, ch, enabled)
            for ch, filled in zip(done_word, fills, strict=True)
        )
        bar_cell = (
            _paint(status_color, left, enabled)
            + word
            + _paint(status_color, right, enabled)
            + " "
            + pct
        )
    else:
        bar_cell = _paint(status_color, progress_bar(fraction, width=10), enabled) + " " + pct

    age = humanize_age(session.last_response_at).rjust(4)

    badge_text, severity = deadline_badge(session.deadline, warn_days=warn_days)
    badge = _paint(_SEVERITY_COLOR[severity], f" ⏰{badge_text}", enabled) if badge_text else ""

    if not session.aim:
        aim = _paint(_DIM, "(no done-condition set)", enabled)
    else:
        # Leading score chip ('NN%', or '-1' while unscored) so /aim quality is visible.
        chip = aim_score_pct(session.aim, session.aim_score)
        low_score = low_aim_score(session.aim, session.aim_score, aim_threshold)
        chip_str = _paint(_SEVERITY_COLOR["red"] if low_score else _DIM, chip, enabled)
        # Show the compact short-AIM label (cheap-model) when present, else the full AIM —
        # of revision (1) under `aim_column = "first"` (the default), else the current AIM.
        label = display_aim(session, aim_first) or session.aim
        aim = f"{chip_str} {label}"

    if session.draft:
        # Future job: show the 4-hex display hash, clickable once it has synced to
        # a future-job file (mirrors the TUI id column / `oo` chord).
        hash_text = display_hash(session.session_id)
        sid = _paint(_DIM, hash_text, enabled)
        if session.future_file:
            sid = osc8_link(obsidian_uri(session.future_file), sid)
        # Pad OUTSIDE the link (trailing blanks must not be clickable) up to the live rows'
        # id width, so a draft's line does not sit 4 columns left of every other row.
        sid += " " * max(0, _ID_WIDTH - len(hash_text))
    else:
        sid = _paint(_DIM, short_id(session.session_id), enabled)
    # Claude Code version patch (e.g. 193 of 2.1.193), or the OAI badge for Codex workflow rows.
    ver_text = version_column_text(session.version, uses_codex_workflow=row.uses_codex_workflow)
    ver = (
        _paint_oai_badge(enabled)
        if row.uses_codex_workflow
        else _paint(_DIM, ver_text.rjust(3), enabled)
    )
    # Blue dot when an impartial checker flagged the sub-goals as drifting (unresolved).
    dot = _paint(_BLUE, " ●", enabled) if drift_unresolved(session) else ""
    # Model column (before /aim, mirroring the TUI): drafts never ran, so show their
    # CONFIGURED overseer ▸ executor pair (single name when they match) instead of an
    # observed "—"; every other row shows the OBSERVED model·effort it ran on.
    model_text = (
        models_readout(session)
        if session.draft
        else model_effort_cell(session.model, session.effort)
    )
    # A little home icon marks rows billing to the `private` (cpriv) account (multi-account
    # only); other rows get an equal-width blank so the model text stays aligned. *account_markers*
    # is the identity-corrected map `render` resolves once per listing (a drifted login shows the
    # account the row TRULY bills); None re-resolves it here, for direct/one-off callers.
    home = accounts.home_marker_from(
        session.config_dir or "",
        accounts.effective_home_markers() if account_markers is None else account_markers,
    )
    # Prompt-cache TTL countdown, prepended BEFORE the account glyph: how long this
    # session's Anthropic prompt cache stays warm (transcript mtime + TTL). Skipped on
    # drafts (never ran) and done rows, mirroring the statusline's ♨/❄ readout (cachettl);
    # a busy row reads ▶ instead — it re-warms its cache every request, so the countdown is
    # only meaningful on the rows waiting on you (or parked). Mirrors the TUI. The cell is
    # FIXED WIDTH on every row — skipped/empty ones pad to a full-width blank — so the
    # account glyph, the model text and everything after them stay column-aligned. Padding
    # is measured on the RAW text and appended outside _paint (ANSI codes are not width).
    cache_text, cache_level = "", ""
    if adapter is not None and not session.draft and status is not Status.DONE:
        cache_text, cache_level = cachettl.countdown_for(
            adapter, session, running=status is Status.WORKING
        )
    cache_cell = (
        _paint(_CACHE_COLOR[cache_level], cache_text, enabled) if cache_text else ""
    ) + cachettl.cell_padding(cache_text)
    model_cell = cache_cell + home + _paint(_DIM, model_text, enabled)
    line1 = (
        f"{icon} {sid}  {ver}  {badge_cell}{folder}  {bar_cell}  "
        f"{age}{badge}  {model_cell}  {aim}{dot}"
    )
    # A hoisted dependent (dep_depth > 0) leads with the red |--> marker, indented one
    # level per depth. Placement is set by core._hoist_dependents; this just renders it.
    if row.dep_depth > 0:
        indent = "  " * (row.dep_depth - 1)
        line1 = _paint(_SEVERITY_COLOR["red"], f"{indent}|--> ", enabled) + line1

    # Secondary dim line: resume command + blocked-on + next-step preview.
    resume_cmd = f"c --resume {session.session_id}"
    resume = (
        osc8_link(f"openterm://{_quote(session.cwd)}", resume_cmd) if session.cwd else resume_cmd
    )
    extras = [f"↳ {resume}"]
    if session.blocked_on:
        extras.append(f"blocked: {session.blocked_on}")
    if row.status is Status.WAITING_CODEX and row.codex_reset_hint:
        extras.append(row.codex_reset_hint)
    if session.draft and session.start_when:
        # Future job: free-text "when I intend to start" note (moved off the id column).
        extras.append("when: " + session.start_when.splitlines()[0][:60])
    if session.draft and session.start_date:
        # Future job: fixed start date (the SCHEDULED bucket). The model pair lives in the
        # model column now, so line 2 no longer carries it.
        extras.append(f"starts: {session.start_date}")
    if session.next_step:
        # Drafts included — a future job's next_step carries its @tags (e.g. @home).
        extras.append("next: " + session.next_step.splitlines()[0][:60])
    if session.depends_on:
        # Any row with a dependency notes it on the ↳ line — hash + state (unmet reads
        # fine as a bare hash; the others spell it out).
        state_suffix = {
            "satisfied": " (done)",
            "missing": " (missing)",
            "cancelled": " (cancelled)",
        }
        extras.append(
            f"depends: {display_hash(session.depends_on)}{state_suffix.get(row.dep_state, '')}"
        )
    line2 = _paint(_DIM, "    " + "  ·  ".join(extras), enabled)
    return [line1, line2]


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="")


def render(
    store: Store,
    adapter: Adapter,
    warn_days: int = 2,
    folder_order: tuple[str, ...] | None = None,
    aim_threshold: int = 50,
    aim_first: bool = True,
) -> str:
    """Return the full ``ccc ls`` output as a string."""
    rows = (
        build_rows(store, adapter, folder_order=tuple(folder_order))
        if folder_order
        else build_rows(store, adapter)
    )
    enabled = _color_enabled()
    if not rows:
        return _paint(_DIM, "No Claude Code sessions tracked yet.", enabled)
    # Resolved once per listing; drives the home-icon marker. Identity-corrected, so a drifted
    # login marks each row with the account it TRULY bills — one .claude.json read per configured
    # account here, never one per row (see accounts.effective_home_markers).
    account_markers = accounts.effective_home_markers()
    # Which halted rows will auto-revive on their account's reset (green ▶ after the red ||).
    # Evaluated ONLY for halted rows — it stats a transcript — and the config is read once.
    armed: set[str] = set()
    halted = [r for r in rows if r.status is Status.HALTED]
    if halted and isinstance(adapter, ClaudeAdapter):
        cfg = config.load_config()
        queue = resume.load_state()  # one snapshot per listing, not one read per row
        armed = {
            r.session.session_id
            for r in halted
            if resume.will_auto_resume(r.session, adapter, cfg, queue)
        }
    resume_armed_ids = frozenset(armed)
    out: list[str] = []
    counts: dict[Status, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
        out.extend(
            _render_row(
                row,
                enabled,
                warn_days,
                aim_threshold,
                aim_first,
                account_markers,
                adapter,
                resume_armed_ids,
            )
        )
    summary = "  ".join(
        _paint(_STATUS_COLOR.get(st, _DIM), f"{STATUS_ICON.get(st, '?')} {st.value}:{n}", enabled)
        for st, n in sorted(counts.items(), key=lambda kv: kv[0].value)
    )
    out.append("")
    out.append(summary)
    return "\n".join(out)
