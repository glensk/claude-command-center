"""Unit tests for the pure model helpers."""

from __future__ import annotations

from datetime import date

import pytest

from command_center.models import (
    LLM_AGENT_ALIAS,
    LLM_CHOICES,
    LLM_MODEL_IDS,
    LiveSession,
    Session,
    Status,
    Subgoal,
    aim_column_first,
    aim_score_badge,
    aim_score_pct,
    deadline_badge,
    derive_status,
    display_aim,
    done_bar_parts,
    dumps_todos,
    effective_progress,
    empty_track_tint,
    expand_llm_choice,
    humanize_age,
    importance_marks,
    loads_todos,
    model_effort_cell,
    model_label,
    models_readout,
    parse_manual_progress,
    progress_bar,
    progress_fraction,
    short_id,
    short_version,
    subgoal_provenance,
    todos_counts,
    version_column_text,
    xterm_rgb,
)


def test_expand_llm_choice() -> None:
    """A canonical value or a unique prefix expands; empty/ambiguous/unknown → None.

    Still used by the CLI paths (and the TUI's _commit_model); the clickable Select
    makes invalid input impossible in the TUI, but the function must stay correct.
    """
    assert expand_llm_choice("opus-4.8") == "opus-4.8"  # exact
    assert expand_llm_choice("opus-5") == "opus-5"  # exact
    assert expand_llm_choice("fable") == "fable-5"  # unique prefix
    assert expand_llm_choice("SONNET") == "sonnet-5"  # case-insensitive
    # Bare "opus" prefixes opus-5, opus-4.8 AND opus-4.8-1m, so shortest-wins cannot
    # decide it — LLM_PREFIX_ALIASES pins it to the current Opus generation.
    assert expand_llm_choice("  opus ") == "opus-5"  # trimmed; explicit alias
    assert expand_llm_choice("haiku") == "haiku-4.5"  # unique prefix
    assert expand_llm_choice("opus-4") == "opus-4.8"  # shortest wins over -1m
    assert expand_llm_choice("opus-4.8-") == "opus-4.8-1m"  # longer prefix → 1M variant
    assert expand_llm_choice("") is None  # empty
    assert expand_llm_choice("gpt-9") is None  # unknown


def test_llm_choice_maps_are_complete() -> None:
    """Every LLM_CHOICES entry launches (`--model` id) and delegates (Agent alias).

    ``cmd_start_job`` indexes both maps by the stored choice — a choice added to
    LLM_CHOICES without its mappings would KeyError at launch time.
    """
    assert set(LLM_MODEL_IDS) == set(LLM_CHOICES)
    assert set(LLM_AGENT_ALIAS) == set(LLM_CHOICES)
    # The delegation aliases must stay within the Agent tool's model enum.
    assert set(LLM_AGENT_ALIAS.values()) <= {"fable", "opus", "sonnet", "haiku"}


def test_model_effort_cell() -> None:
    # Both present → joined with "·".
    assert model_effort_cell("fable-5", "xhigh") == "fable-5·xhigh"
    # A known LLM_CHOICES label is shown as-is; effort alone or model alone stands alone.
    assert model_effort_cell("opus-4.8", "") == "opus-4.8"
    assert model_effort_cell("", "high") == "high"
    # An unknown/newer raw id has a leading "claude-" stripped.
    assert model_effort_cell("claude-brand-new-9", "low") == "brand-new-9·low"
    assert model_effort_cell("claude-brand-new-9", "") == "brand-new-9"
    # Both empty → em dash.
    assert model_effort_cell("", "") == "—"


def test_models_readout_compacts_on_equal() -> None:
    """Equal overseer/executor → a single name; a differing pair → ``overseer ▸ executor``."""
    equal = Session(session_id="s1", llm_overseer="fable-5", llm_exec="fable-5")
    assert models_readout(equal) == "fable-5"  # no redundant "fable-5 ▸ fable-5"
    mixed = Session(session_id="s2", llm_overseer="opus-4.8", llm_exec="sonnet-5")
    assert models_readout(mixed) == "opus-4.8 ▸ sonnet-5"


def test_model_label() -> None:
    """A raw model id reverse-maps to its ccc choice; unknown → unchanged; empty → ""."""
    assert model_label("claude-fable-5") == "fable-5"
    assert model_label("claude-opus-4-8") == "opus-4.8"
    assert model_label("claude-opus-4-8[1m]") == "opus-4.8-1m"
    assert model_label("claude-brand-new-9") == "claude-brand-new-9"  # unknown → unchanged
    assert model_label(None) == ""
    assert model_label("") == ""


def test_short_version() -> None:
    assert short_version("2.1.193") == "193"  # patch part only — drops the near-constant 2.1
    assert short_version("2.10.5") == "5"
    assert short_version("193") == "193"  # no dot → unchanged
    assert short_version(None) == ""
    assert short_version("") == ""


def test_version_column_text_uses_oai_badge_for_codex_workflow() -> None:
    assert version_column_text("2.1.193") == "193"
    assert version_column_text("2.1.193", uses_codex_workflow=True) == "OAI"
    assert version_column_text(None, uses_codex_workflow=True) == "OAI"


def test_todo_box() -> None:
    from command_center.models import todo_box

    assert todo_box("completed") == "☒"
    assert todo_box("in_progress") == "◧"
    assert todo_box("pending") == "☐"
    assert todo_box("") == "☐"  # unknown / empty -> pending box


def test_aim_score_badge() -> None:
    assert aim_score_badge(-1, 50) == ("", "none")  # unscored => no paint
    assert aim_score_badge(0, 50)[1] == "red"
    assert aim_score_badge(49, 50)[1] == "red"  # below threshold
    assert aim_score_badge(50, 50)[1] == "green"  # at threshold is not vague
    assert aim_score_badge(90, 50)[1] == "green"
    assert aim_score_badge(20, 50)[0] == "vague"  # red carries a short badge label


def test_aim_score_pct() -> None:
    assert aim_score_pct(None, 50) == ""  # no AIM => no chip
    assert aim_score_pct("", 50) == ""
    assert aim_score_pct("ship it", -1) == ""  # unscored => blank (no stuck "-1" pending state)
    assert aim_score_pct("ship it", 0) == "0%"
    assert aim_score_pct("ship it", 65) == "65%"
    assert aim_score_pct("ship it", 100) == "100%"


def test_display_aim_prefers_short_label() -> None:
    # The column shows the short label when present, else the full AIM verbatim, else None.
    both = Session("s", aim="the long full aim", short_aim="implement x")
    assert display_aim(both) == "implement x"
    assert display_aim(Session("s", aim="the long full aim")) == "the long full aim"
    assert display_aim(Session("s")) is None


def test_display_aim_first_renders_revision_one() -> None:
    """``first=True`` (the `aim_column = "first"` default) renders revision (1), not the current."""
    session = Session(
        "s",
        aim="sharpened aim",
        short_aim="sharpened",
        first_aim="the aim as first typed",
        first_short_aim="as first typed",
    )
    assert display_aim(session, first=True) == "as first typed"  # revision (1)'s short label
    assert display_aim(session) == "sharpened"  # unchanged default: the current AIM

    # Revision (1) with no short label of its own falls back to its full text …
    unlabelled = Session("s", aim="sharpened aim", short_aim="sharpened", first_aim="first typed")
    assert display_aim(unlabelled, first=True) == "first typed"
    # … and an AIM that predates history tracking (no revision recorded) to the live AIM.
    assert display_aim(Session("s", aim="only aim"), first=True) == "only aim"


def test_aim_column_first_only_latest_opts_out() -> None:
    """Only an explicit "latest" turns the first-AIM column off; typos keep the default."""
    assert aim_column_first("first") is True
    assert aim_column_first("") is True
    assert aim_column_first("frist") is True  # a typo must not silently flip the column
    assert aim_column_first("latest") is False
    assert aim_column_first("  LATEST  ") is False


HOUR_MS = 3_600_000


def test_humanize_age_buckets() -> None:
    now = 100 * 24 * HOUR_MS
    assert humanize_age(0) == "—"
    assert humanize_age(now - 5_000, now) == "5s"
    assert humanize_age(now - 5 * 60_000, now) == "5m"
    assert humanize_age(now - 3 * HOUR_MS, now) == "3h"
    assert humanize_age(now - 4 * 24 * HOUR_MS, now) == "4d"
    assert humanize_age(now - 21 * 24 * HOUR_MS, now) == "3w"


def test_short_id() -> None:
    assert short_id("ef42ba0c-2c35-46c2-9c74") == "ef42ba0c"
    assert short_id("abc") == "abc     "  # padded to width for column alignment
    assert short_id("") == "        "
    assert short_id("ef42ba0c-2c35", width=4) == "ef42"


def test_importance_marks() -> None:
    assert importance_marks(0) == ""
    assert importance_marks(1) == "!"
    assert importance_marks(2) == "!!"
    assert importance_marks(3) == "!!!"
    assert importance_marks(5) == "!!!"  # clamped


def test_progress() -> None:
    assert progress_fraction(0, 0) is None
    assert progress_fraction(3, 6) == 0.5
    assert progress_bar(None, 4) == "—   "
    assert progress_bar(0.5, 10) == "▓▓▓▓▓░░░░░"
    assert progress_bar(2.0, 4) == "▓▓▓▓"  # clamped


def test_effective_progress() -> None:
    # No manual override → the sub-goal ratio (None when no checklist).
    assert effective_progress(None, 0, 0) is None
    assert effective_progress(None, 3, 6) == 0.5
    # A manual percentage wins over the ratio, even with no checklist; clamped 0..100.
    assert effective_progress(40, 3, 6) == 0.4
    assert effective_progress(40, 0, 0) == 0.4
    assert effective_progress(0, 3, 6) == 0.0
    assert effective_progress(150, 0, 0) == 1.0
    assert effective_progress(-5, 0, 0) == 0.0


def test_parse_manual_progress() -> None:
    assert parse_manual_progress("") is None  # blank clears → auto
    assert parse_manual_progress("  ") is None
    assert parse_manual_progress("%") is None  # a bare % is blank too
    assert parse_manual_progress("40") == 40
    assert parse_manual_progress(" 40% ") == 40
    assert parse_manual_progress("0") == 0
    assert parse_manual_progress("100") == 100
    for bad in ("101", "-1", "abc", "40.5"):
        with pytest.raises(ValueError):
            parse_manual_progress(bad)


def test_done_bar_parts() -> None:
    # DONE stamped into the middle of the bar; fill still visible on both sides. The DONE
    # bar's filled cells are SOLID █ (not the ordinary ▓) so a filled-cell letter's
    # background — the same colour — matches the surrounding cells exactly; ``fills`` says
    # per letter whether it covers a filled cell.
    left, word, right, fills = done_bar_parts(0.5, 8)
    assert word == "DONE"
    assert left + word + right == "██DONE░░"  # progress_bar(0.5,8) fill=4, centre → DONE
    assert len(left) + len(word) + len(right) == 8  # width invariant
    assert fills == (True, True, False, False)  # "DO" over ██, "NE" over ░░

    # No checklist yet (fraction None): a full empty track so the verdict still renders.
    left, word, right, fills = done_bar_parts(None, 8)
    assert left + word + right == "░░DONE░░"
    assert len(left) + len(word) + len(right) == 8
    assert fills == (False, False, False, False)

    # ls width (10): centred DONE, width preserved; 100% → every letter over fill.
    left, word, right, fills = done_bar_parts(1.0, 10)
    assert word == "DONE" and len(left + word + right) == 10
    assert left + word + right == "███DONE███"
    assert fills == (True, True, True, True)

    # Degenerate width (≤ len("DONE")): truncated word, fills still per remaining letter.
    left, word, right, fills = done_bar_parts(0.5, 3)
    assert (left, word, right) == ("", "DON", "")
    assert fills == (True, True, False)  # fill=round(1.5)=2 cells of 3


def test_empty_track_tint_and_xterm_rgb() -> None:
    # xterm-256 → RGB: colour cube (84 = spring green), grayscale ramp, classic 16.
    assert xterm_rgb(84) == (95, 255, 135)
    assert xterm_rgb(196) == (255, 0, 0)
    assert xterm_rgb(231) == (255, 255, 255)
    assert xterm_rgb(250) == (188, 188, 188)  # grayscale ramp
    assert xterm_rgb(9) == (255, 0, 0)  # classic 16

    # Empty-track letter background = 25 % of the glyph colour, the ░ cell's average
    # (a ░ inks a quarter of its cell; the dark terminal shows through the rest).
    assert empty_track_tint((95, 255, 135)) == (24, 64, 34)
    assert empty_track_tint((255, 255, 255)) == (64, 64, 64)


def test_deadline_badge() -> None:
    today = date(2026, 6, 23)
    assert deadline_badge(None, today=today) == ("", "none")
    assert deadline_badge("not-a-date", today=today)[1] == "none"
    assert deadline_badge("2026-06-20", today=today) == ("overdue 3d", "red")
    assert deadline_badge("2026-06-23", today=today) == ("due today", "red")
    assert deadline_badge("2026-06-24", warn_days=2, today=today) == ("due 1d", "amber")
    assert deadline_badge("2026-07-30", warn_days=2, today=today)[1] == "green"


def test_todos_serialization() -> None:
    todos = [("completed", "first"), ("in_progress", "second"), ("pending", "third")]
    assert loads_todos(dumps_todos(todos)) == todos
    assert loads_todos(None) == []
    assert loads_todos("") == []
    assert loads_todos("not json") == []
    assert loads_todos('{"oops": 1}') == []  # not a list
    assert todos_counts(todos) == (1, 3)
    assert todos_counts([]) == (0, 0)


def _sg(text: str, *, source: str = "user", model: str | None = None, rev: int = 0) -> Subgoal:
    return Subgoal(0, "s", 0, text, False, source, 1, None, model, rev)


def test_subgoal_provenance() -> None:
    assert subgoal_provenance([]) == ""
    assert subgoal_provenance([_sg("a")]) == "manual"
    auto = [_sg("a", source="auto", model="claude-haiku-4-5", rev=2)]
    assert subgoal_provenance(auto) == "auto (claude-haiku-4-5) · from AIM v2"
    agent = [_sg("a", source="agent", model="claude-opus-4-8", rev=3)]
    assert subgoal_provenance(agent) == "agent (claude-opus-4-8) · from AIM v3"
    mixed = [_sg("a", source="user"), _sg("b", source="auto", model="m")]
    assert subgoal_provenance(mixed).startswith("mixed")


def _live(raw: str, alive: bool = True) -> LiveSession:
    return LiveSession(pid=1, session_id="s", cwd="/x", raw_status=raw, alive=alive)


def test_derive_status() -> None:
    assert derive_status(_live("busy"), Session("s")) is Status.WORKING
    assert derive_status(_live("waiting"), Session("s")) is Status.WAITING_INPUT
    assert derive_status(_live("idle"), Session("s")) is Status.IDLE
    assert derive_status(_live("idle", alive=False), Session("s")) is Status.PARKED
    assert derive_status(None, Session("s")) is Status.PARKED
    # Done, but the agent is still mid-turn → WORKING (▶) until the turn ends …
    assert derive_status(_live("busy"), Session("s", done=True)) is Status.WORKING
    # … then the ✓ takes over the moment it stops being busy (idle / waiting / closed).
    assert derive_status(_live("idle"), Session("s", done=True)) is Status.DONE
    assert derive_status(_live("waiting"), Session("s", done=True)) is Status.DONE
    assert derive_status(_live("busy", alive=False), Session("s", done=True)) is Status.DONE
    assert derive_status(None, Session("s", done=True)) is Status.DONE
    # A rate-limit halt on an open session wins over the live raw status …
    assert derive_status(_live("idle"), Session("s"), halted=True) is Status.HALTED
    # … but done still wins (a finished session whose last turn 429'd is done) …
    assert derive_status(_live("idle"), Session("s", done=True), halted=True) is Status.DONE
    # … and a closed session is PARKED regardless of a stale halt.
    assert derive_status(_live("idle", alive=False), Session("s"), halted=True) is Status.PARKED


def test_derive_status_snoozed() -> None:
    # A live background task refines an otherwise-IDLE session into SNOOZED …
    assert derive_status(_live("idle"), Session("s"), background=True) is Status.SNOOZED
    # … but never overrides working / waiting-for-input / halted (higher attention) …
    assert derive_status(_live("busy"), Session("s"), background=True) is Status.WORKING
    assert derive_status(_live("waiting"), Session("s"), background=True) is Status.WAITING_INPUT
    assert derive_status(_live("idle"), Session("s"), halted=True, background=True) is Status.HALTED
    # … and it is live-only: a closed session is PARKED even with a (stale) bg flag.
    assert derive_status(_live("idle", alive=False), Session("s"), background=True) is Status.PARKED
    # No background task → plain IDLE (state derived purely from session/live facts).
    assert derive_status(_live("idle"), Session("s"), background=False) is Status.IDLE


def test_derive_status_waiting_codex() -> None:
    # Codex quota waiting occupies the otherwise-idle slot.
    assert derive_status(_live("idle"), Session("s"), codex_waiting=True) is Status.WAITING_CODEX
    # It never overrides active / waiting / halted statuses.
    assert derive_status(_live("busy"), Session("s"), codex_waiting=True) is Status.WORKING
    assert derive_status(_live("waiting"), Session("s"), codex_waiting=True) is Status.WAITING_INPUT
    assert (
        derive_status(_live("idle"), Session("s"), halted=True, codex_waiting=True) is Status.HALTED
    )
    # Explicit precedence: a live background task remains SNOOZED; otherwise Codex waits.
    assert (
        derive_status(_live("idle"), Session("s"), background=True, codex_waiting=True)
        is Status.SNOOZED
    )


def test_parse_iso_date_and_scheduled_date() -> None:
    from command_center.models import parse_iso_date, scheduled_date

    assert parse_iso_date("2026-08-11") == date(2026, 8, 11)
    assert parse_iso_date(" 2026-08-11 ") == date(2026, 8, 11)  # trimmed
    assert parse_iso_date("") is None
    assert parse_iso_date(None) is None
    assert parse_iso_date("next tuesday") is None  # free text is not a fixed date

    # Only DRAFTS with a valid start_date are "scheduled"; bad dates degrade to FUTURE.
    assert scheduled_date(Session("s", draft=True, start_date="2026-08-11")) == date(2026, 8, 11)
    assert scheduled_date(Session("s", draft=True, start_date="soonish")) is None
    assert scheduled_date(Session("s", draft=True)) is None
    assert scheduled_date(Session("s", draft=False, start_date="2026-08-11")) is None


def test_days_until_start() -> None:
    from command_center.models import days_until_start

    today = date(2026, 7, 3)
    ahead = Session("s", draft=True, start_date="2026-08-11")
    assert days_until_start(ahead, today=today) == 39  # premature by 39 days
    # Arrived or passed → no guard (None), same for unset/invalid/non-draft.
    assert days_until_start(Session("s", draft=True, start_date="2026-07-03"), today=today) is None
    assert days_until_start(Session("s", draft=True, start_date="2026-01-01"), today=today) is None
    assert days_until_start(Session("s", draft=True), today=today) is None
    assert days_until_start(Session("s", draft=False, start_date="2026-08-11"), today=today) is None
