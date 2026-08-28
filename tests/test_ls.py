"""Render tests for the flat ``ccc ls`` view — AIM color keyed off the vagueness score."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import cast

import pytest

from command_center import accounts
from command_center.adapters.base import Adapter
from command_center.core import Row
from command_center.models import Session, Status
from command_center.views import ls as ls_view

_RED = "\x1b[38;5;196m"  # _SEVERITY_COLOR["red"] painted by _paint()
_GREEN = "\x1b[38;5;40m"  # _RESUME_GREEN — the ▶ appended to a || armed for auto-resume
_OAI = "\x1b[1;38;5;16;48;5;15mOAI\x1b[0m"


def _row(aim: str, score: int) -> Row:
    session = Session(session_id="s1", cwd="/repo", aim=aim, aim_score=score)
    return Row(session, None, Status.PARKED, 0, 0)


def test_render_row_paints_only_low_aim_score_red() -> None:
    lines = ls_view._render_row(
        _row("improve things", 20), enabled=True, warn_days=2, aim_threshold=50
    )
    assert f"{_RED}20%\x1b[0m improve things" in lines[0]
    assert f"{_RED}improve things\x1b[0m" not in lines[0]


def test_render_row_shows_first_aim_revision_by_default() -> None:
    """The `/aim` column renders revision (1); `aim_first=False` (config "latest") the current."""
    session = Session(
        session_id="s1",
        cwd="/repo",
        aim="second aim: pytest -q green",
        aim_score=85,
        first_aim="first aim: ccc ls shows the row",
    )
    row = Row(session, None, Status.PARKED, 0, 0)
    default = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    assert "first aim: ccc ls shows the row" in default
    assert "second aim: pytest -q green" not in default
    latest = ls_view._render_row(
        row, enabled=False, warn_days=2, aim_threshold=50, aim_first=False
    )[0]
    assert "second aim: pytest -q green" in latest


def test_render_row_specific_aim_not_red() -> None:
    lines = ls_view._render_row(
        _row("all tests pass", 85), enabled=True, warn_days=2, aim_threshold=50
    )
    assert "all tests pass" in lines[0]
    assert f"{_RED}all tests pass" not in lines[0]


def test_render_row_unscored_aim_not_red() -> None:
    lines = ls_view._render_row(_row("some aim", -1), enabled=True, warn_days=2, aim_threshold=50)
    assert f"{_RED}-1" not in lines[0]
    assert f"{_RED}some aim" not in lines[0]


_DEP = "abcd1234-1234-5678-9abc-def012345678"

# What `render` now hands `_render_row`: the identity-corrected marker map
# (accounts.effective_home_markers), i.e. resolved account dir → model-column glyph.
_PRIV_DIR = "/home/u/.claude"
_WORK_DIR = "/home/u/.claude-work"
_MULTI_MARKERS = {
    str(accounts._resolve(_PRIV_DIR)): accounts._HOME_GLYPH,
    str(accounts._resolve(_WORK_DIR)): accounts._WORK_GLYPH,
}


def test_render_row_home_icon_marks_private_account() -> None:
    """Multi-account: a private-account row carries 🏠, a work-account row carries 💼."""
    priv = Session(session_id="p", cwd="/repo", aim="x", config_dir=_PRIV_DIR)
    work = Session(session_id="w", cwd="/repo", aim="x", config_dir=_WORK_DIR)
    row_args = (True, 2, 50, _MULTI_MARKERS)
    line_priv = ls_view._render_row(Row(priv, None, Status.PARKED, 0, 0), *row_args)[0]
    line_work = ls_view._render_row(Row(work, None, Status.PARKED, 0, 0), *row_args)[0]
    assert accounts._HOME_GLYPH in line_priv and "💼" not in line_priv
    assert accounts._WORK_GLYPH in line_work and "🏠" not in line_work


def test_render_row_home_icon_follows_the_corrected_identity() -> None:
    """A drifted login (the private-*named* dir actually holding the work account) renders
    the TRUE billing glyph — the markers are identity-corrected before they reach a row."""
    drifted = {str(accounts._resolve(_PRIV_DIR)): accounts._WORK_GLYPH}
    priv = Session(session_id="p", cwd="/repo", aim="x", config_dir=_PRIV_DIR)
    line = ls_view._render_row(Row(priv, None, Status.PARKED, 0, 0), True, 2, 50, drifted)[0]
    assert accounts._WORK_GLYPH in line and "🏠" not in line


def test_render_row_no_home_icon_in_single_account() -> None:
    """Single account: no marker at all (it would sit on every row and mean nothing) —
    `effective_home_markers` returns {} there, which `home_marker_from` renders as ""."""
    priv = Session(session_id="p", cwd="/repo", aim="x", config_dir=_PRIV_DIR)
    line = ls_view._render_row(Row(priv, None, Status.PARKED, 0, 0), True, 2, 50, {})[0]
    assert "🏠" not in line


def test_render_row_hoisted_marker_prefix() -> None:
    # A hoisted dependent (dep_depth > 0) leads line1 with the red |--> marker.
    session = Session(session_id="child", cwd="/repo", aim="x", depends_on=_DEP)
    row = Row(session, None, Status.PARKED, 0, 0)
    row.dep_depth = 1
    row.dep_state = "unmet"
    lines = ls_view._render_row(row, enabled=True, warn_days=2, aim_threshold=50)
    assert lines[0].startswith(f"{_RED}|--> \x1b[0m")


def test_render_row_depends_extras_states() -> None:
    # Any row with a dependency notes it on the ↳ line, with the state suffix.
    cases = [
        ("unmet", ""),
        ("satisfied", " (done)"),
        ("missing", " (missing)"),
        ("cancelled", " (cancelled)"),
    ]
    for state, suffix in cases:
        session = Session(session_id="s1", cwd="/repo", aim="x", depends_on=_DEP)
        row = Row(session, None, Status.PARKED, 0, 0)
        row.dep_state = state
        line2 = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[1]
        assert f"depends: abcd{suffix}" in line2


def test_render_row_no_depends_no_extra() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.PARKED, 0, 0)
    line2 = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[1]
    assert "depends:" not in line2


def test_render_row_halted_shows_red_double_bar() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.HALTED, 0, 0)
    lines = ls_view._render_row(row, enabled=True, warn_days=2, aim_threshold=50)
    assert f"{_RED}||\x1b[0m" in lines[0]  # red "||" rate-limit icon leads the row
    assert _GREEN not in lines[0]  # not armed for auto-resume → NO green ▶ (it is stranded)


def test_render_row_halted_armed_appends_green_play() -> None:
    """A halted row ccc will auto-revive wears a green ▶ after the red || (red || + green ▶)."""
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.HALTED, 0, 0)
    lines = ls_view._render_row(
        row,
        enabled=True,
        warn_days=2,
        aim_threshold=50,
        resume_armed_ids=frozenset({"s1"}),
    )
    assert lines[0].startswith(f"{_RED}||\x1b[0m{_GREEN}▶\x1b[0m")  # red ||, then green ▶


def test_render_row_shows_per_repo_badge() -> None:
    """The deterministic per-repo badge appears before the folder cell (matches the TUI)."""
    from command_center import tabsymbol

    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(session, None, Status.PARKED, 0, 0)
    line = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    badge = tabsymbol.symbol_for_repo("/repo")
    assert f"{badge} " in line  # the emoji cell is rendered


def test_render_row_shows_claude_version() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x", version="2.1.193")
    line = ls_view._render_row(
        Row(session, None, Status.IDLE, 0, 0), enabled=False, warn_days=2, aim_threshold=50
    )[0]
    assert "193" in line  # patch part shown
    assert "2.1.193" not in line  # full version is not


def test_render_row_codex_workflow_badge_replaces_version() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x", version="2.1.193")
    row = Row(session, None, Status.IDLE, 0, 0, uses_codex_workflow=True)

    plain = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    assert "OAI" in plain
    assert "193" not in plain
    ansi = ls_view._render_row(row, enabled=True, warn_days=2, aim_threshold=50)[0]
    assert _OAI in ansi


def test_render_row_waiting_codex_shows_sleeping_face_and_reset_hint() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="x")
    row = Row(
        session,
        None,
        Status.WAITING_CODEX,
        0,
        0,
        uses_codex_workflow=True,
        codex_reset_label="5h",
        codex_reset_at=int(time.time()) + 3600,
    )
    lines = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)
    assert lines[0].startswith("😴")
    assert "waiting for Codex 5h reset" in lines[1]


def test_render_row_draft_shows_hash_linked_when_future_file_set() -> None:
    from command_center.future_files import display_hash, obsidian_uri
    from command_center.links import osc8_link

    sid = "3a8b7c12-1111-2222-3333-444444444444"
    session = Session(session_id=sid, cwd="/repo", aim="x", draft=True)
    row = Row(session, None, Status.PARKED, 0, 0)

    # No future_file yet: bare hash, no obsidian:// hyperlink (the folder cell may
    # still carry its own unrelated openterm:// OSC 8 link, hence the narrow check).
    line = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    assert display_hash(sid) in line
    assert "obsidian://" not in line

    # Synced to a future-job file: the hash is wrapped in an OSC 8 link to it.
    session.future_file = "01-llm-tasks/future/home/claude-command-center/3a8b-fix.md"
    linked_line = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[0]
    expected = osc8_link(obsidian_uri(session.future_file), display_hash(sid))
    assert expected in linked_line


def test_render_row_draft_shows_start_when_note() -> None:
    """A draft's free-text start_when note surfaces as a ``when:`` line-2 entry."""
    sid = "3a8b7c12-1111-2222-3333-444444444444"
    session = Session(
        session_id=sid, cwd="/repo", aim="x", draft=True, start_when="during holidays"
    )
    row = Row(session, None, Status.PARKED, 0, 0)
    line2 = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)[1]
    assert "when: during holidays" in line2

    # No start_when → no when: entry.
    plain = Session(session_id=sid, cwd="/repo", aim="x", draft=True)
    plain_line2 = ls_view._render_row(
        Row(plain, None, Status.PARKED, 0, 0), enabled=False, warn_days=2, aim_threshold=50
    )[1]
    assert "when:" not in plain_line2


def test_render_row_draft_shows_models_readout() -> None:
    """A draft's model column (line 1) carries the configured pair; line 2 no longer does."""
    sid = "3a8b7c12-1111-2222-3333-444444444444"
    session = Session(session_id=sid, cwd="/repo", aim="x", draft=True)
    row = Row(session, None, Status.PARKED, 0, 0)
    lines = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)
    # Equal overseer/executor (the fable-5 default) compacts to a single name in the model slot.
    assert "fable-5" in lines[0]
    assert "▸" not in lines[0]  # no redundant "fable-5 ▸ fable-5"
    assert "fable-5" not in lines[1]  # the pair moved off the secondary line

    session.llm_overseer = "fable-5"
    session.llm_exec = "sonnet-5"
    mixed = ls_view._render_row(row, enabled=False, warn_days=2, aim_threshold=50)
    assert "fable-5 ▸ sonnet-5" in mixed[0]  # differing pair shown in full, in the model column
    assert "▸" not in mixed[1]


class _FakeAdapter:
    """Adapter stub exposing only ``transcript_path`` (the probed capability)."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        return self._path


def _fresh_transcript(tmp_path: Path) -> Path:
    """A transcript whose newest line is a fresh assistant turn → warm (green) countdown.

    The cache anchor is the newest main-chain ``"type":"assistant"`` entry, so a fresh
    mtime alone is not enough — the file must carry a recent API turn (see cachettl).
    """
    transcript = tmp_path / "t.jsonl"
    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".000000Z"
    line = json.dumps(
        {"type": "assistant", "isSidechain": False, "timestamp": ts, "requestId": "r"},
        separators=(",", ":"),
    )
    transcript.write_text(line + "\n", encoding="utf-8")
    os.utime(transcript, (now, now))
    return transcript


def test_render_row_cache_countdown_before_home_glyph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live/parked row shows the ♨ TTL countdown BEFORE the 🏠 account glyph."""
    monkeypatch.delenv("CC_CACHE_TTL_S", raising=False)
    session = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
    line = ls_view._render_row(
        Row(session, None, Status.PARKED, 0, 0),
        True,
        2,
        50,
        _MULTI_MARKERS,
        cast(Adapter, _FakeAdapter(_fresh_transcript(tmp_path))),
    )[0]
    assert "♨" in line
    assert accounts._HOME_GLYPH in line
    assert line.index("♨") < line.index("🏠")


def test_render_row_draft_id_cell_matches_live_id_width() -> None:
    """A FUTURE (draft) row's short 4-hex id pads to the live rows' id width.

    Regression: the draft branch emitted the bare hash, so every column after the id sat 4
    screen columns left of the live rows' — the whole line was misaligned.
    """
    sid = "01a1f554-c8a7-446f-82d5-a0a0a0000000"
    lines = [
        ls_view._render_row(
            Row(Session(session_id=sid, cwd="/repo", aim="x", draft=draft), None, st, 0, 0),
            False,
            2,
            50,
        )[0]
        for draft, st in ((False, Status.PARKED), (True, Status.PARKED))
    ]
    assert len({line.index("🟦") for line in lines}) == 1, lines


def test_render_row_cache_countdown_is_play_icon_while_working(tmp_path: Path) -> None:
    """A busy (▶ WORKING) row shows ▶ in the model column, not the ♨ countdown."""
    session = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
    line = ls_view._render_row(
        Row(session, None, Status.WORKING, 0, 0),
        True,
        2,
        50,
        _MULTI_MARKERS,
        cast(Adapter, _FakeAdapter(_fresh_transcript(tmp_path))),
    )[0]
    assert "♨" not in line
    # Twice: the status icon at the start of the line AND the model column's cache cell.
    assert line.count("▶") == 2
    assert accounts._HOME_GLYPH in line
    # Painted in the WORKING status colour, exactly like the status icon.
    assert line.count(f"\x1b[38;5;{ls_view._STATUS_COLOR[Status.WORKING]}m▶") == 2


def test_render_row_cache_cell_is_fixed_width_across_rows(tmp_path: Path) -> None:
    """The cache cell reserves the same width on EVERY row, so the model text stays aligned.

    ♨ M:SS, ❄ cold, ▶ and a skipped cell (draft / done) must all leave the account glyph and
    the model·effort text at the identical column — a column that changes width per row is
    the bug this pins (see README "Column alignment").
    """
    warm = _fresh_transcript(tmp_path)
    cases = [
        (Status.WORKING, warm, False),  # ▶
        (Status.WAITING_INPUT, warm, False),  # ♨ M:SS
        (Status.PARKED, None, False),  # no transcript → blank cell
        (Status.DONE, warm, False),  # skipped on done rows
        (Status.PARKED, warm, True),  # skipped on drafts
    ]
    offsets = set()
    for status, transcript, draft in cases:
        session = Session(
            session_id="p", cwd="/repo", aim="x", draft=draft, config_dir="/home/u/.claude"
        )
        line = ls_view._render_row(
            Row(session, None, status, 0, 0),
            False,  # colour off → plain text, so indices are display columns
            2,
            50,
            _MULTI_MARKERS,
            cast(Adapter, _FakeAdapter(transcript)),
        )[0]
        offsets.add(line.index(accounts._HOME_GLYPH))
    assert len(offsets) == 1, f"model column not aligned across rows: {sorted(offsets)}"


def test_render_row_cache_countdown_kept_on_every_non_working_status(tmp_path: Path) -> None:
    """Every other live/parked status keeps the ♨ countdown (waiting, idle, snoozed, …)."""
    transcript = _fresh_transcript(tmp_path)
    for status in (
        Status.WAITING_INPUT,
        Status.IDLE,
        Status.SNOOZED,
        Status.HALTED,
        Status.WAITING_CODEX,
        Status.PARKED,
    ):
        session = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
        line = ls_view._render_row(
            Row(session, None, status, 0, 0),
            True,
            2,
            50,
            _MULTI_MARKERS,
            cast(Adapter, _FakeAdapter(transcript)),
        )[0]
        assert "♨" in line, status


def test_render_row_cache_countdown_absent_without_transcript() -> None:
    """No transcript → empty cell (no ♨), glyph still rendered."""
    session = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
    line = ls_view._render_row(
        Row(session, None, Status.PARKED, 0, 0),
        True,
        2,
        50,
        _MULTI_MARKERS,
        cast(Adapter, _FakeAdapter(None)),
    )[0]
    assert "♨" not in line
    assert accounts._HOME_GLYPH in line


def test_render_row_cache_countdown_skipped_on_draft(tmp_path: Path) -> None:
    """A draft never ran → no countdown even with a live transcript path."""
    session = Session(
        session_id="p", cwd="/repo", aim="x", draft=True, config_dir="/home/u/.claude"
    )
    line = ls_view._render_row(
        Row(session, None, Status.PARKED, 0, 0),
        True,
        2,
        50,
        _MULTI_MARKERS,
        cast(Adapter, _FakeAdapter(_fresh_transcript(tmp_path))),
    )[0]
    assert "♨" not in line


def test_render_row_cache_countdown_skipped_on_done(tmp_path: Path) -> None:
    """A done row is excluded from the countdown (mirrors the statusline / TUI branch)."""
    session = Session(session_id="p", cwd="/repo", aim="x", config_dir="/home/u/.claude")
    line = ls_view._render_row(
        Row(session, None, Status.DONE, 0, 0),
        True,
        2,
        50,
        _MULTI_MARKERS,
        cast(Adapter, _FakeAdapter(_fresh_transcript(tmp_path))),
    )[0]
    assert "♨" not in line


def test_render_row_shows_blue_drift_dot() -> None:
    flagged = Session(session_id="s1", cwd="/repo", aim="x", drift_severity="high", drift_at=1)
    line = ls_view._render_row(
        Row(flagged, None, Status.IDLE, 0, 0), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "\x1b[38;5;39m ●\x1b[0m" in line  # blue unresolved-drift dot

    acked = Session(
        session_id="s2", cwd="/r", aim="x", drift_severity="high", drift_at=1, drift_ack_at=2
    )
    acked_line = ls_view._render_row(
        Row(acked, None, Status.IDLE, 0, 0), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "\x1b[38;5;39m ●\x1b[0m" not in acked_line  # acknowledged -> no blue drift dot
    # (a bare ● now also appears as the green idle icon, so match the blue drift escape precisely)


def test_render_row_shows_red_done_when_aim_met() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="ship it", aim_score=80, aim_met=True)
    line = ls_view._render_row(
        Row(session, None, Status.IDLE, 3, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    # 3/4 → the bar under DONE is all filled: every letter is black on the SAME palette entry
    # (48;5;214, amber) the solid █ glyphs use as foreground — letter cells and bar cells render
    # pixel-identically, no seam.
    on_fill = "".join(f"\x1b[1;38;5;16;48;5;214m{ch}\x1b[0m" for ch in "DONE")
    assert on_fill in line
    assert "75%" in line  # the exact sub-goal progress is still shown alongside
    assert "█" in line and "▓" not in line  # DONE bar fill is solid, not the dotted shade

    # 2/4 → fill 5 of 10 cells: "DO" sits on filled cells (bg = the fill palette entry),
    # "NE" on empty cells (25 % tint of 214 → rgb 64,44,0 — the ░ track's average).
    half = ls_view._render_row(
        Row(session, None, Status.IDLE, 2, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    mixed = "".join(f"\x1b[1;38;5;16;48;5;214m{ch}\x1b[0m" for ch in "DO") + "".join(
        f"\x1b[1;38;5;196;48;2;64;44;0m{ch}\x1b[0m" for ch in "NE"
    )
    assert mixed in half


def test_render_row_no_done_when_not_met() -> None:
    session = Session(session_id="s1", cwd="/repo", aim="ship it", aim_score=80, aim_met=False)
    line = ls_view._render_row(
        Row(session, None, Status.IDLE, 3, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "DONE" not in line


def test_render_row_no_done_for_human_done_row() -> None:
    # A human-done row (✓ / FINISHED) never also shows the soft red DONE overlay.
    session = Session(session_id="s1", cwd="/repo", aim="ship it", aim_score=80, aim_met=True)
    line = ls_view._render_row(
        Row(session, None, Status.DONE, 4, 4), enabled=True, warn_days=2, aim_threshold=50
    )[0]
    assert "DONE" not in line
