"""The shared paste guard (:func:`bridge_target.guarded_paste`, :func:`held_notice`, tp#1867).

Every test drives the helper directly with a fake :class:`bridgestub.World` whose tab "shows"
one of the masked screen captures in ``tests/fixtures/composer``.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from bridgestub import TAB, World, fixture_screen, turn_end, user

from command_center.bridge_target import (
    HELD_CODES,
    PasteResult,
    guarded_paste,
    held_notice,
)
from command_center.screenread import ScreenCell, ScreenRead, line_text, rule_rows
from command_center.snapshot import PsTimedOut

TEXT = "tp1867 pasted prompt"
#: A screen with no full-width rule: the parser cannot find the box.
UNRECOGNIZED = ScreenRead(80, (0, 0), ((ScreenCell("x"), ScreenCell("y")),))


def _world(tmp_path: Path, screen: str | None = "empty_idle", **kw: Any) -> World:
    """An established session (a transcript with one finished turn) showing *screen*."""
    world = World(tmp_path, **kw)
    world.screen = fixture_screen(screen) if screen is not None else None
    world.write(user("earlier"), *turn_end())
    return world


def _fresh_world(tmp_path: Path, screen: ScreenRead | None) -> World:
    """A session that has not been prompted yet (no transcript on disk)."""
    world = World(tmp_path)
    world.screen = screen
    world.transcript.unlink()
    world.transcript_resolves_missing = True
    return world


def _paste(world: World, **kw: Any) -> PasteResult:
    return guarded_paste(world.deps(), world.live(), TEXT, **kw)


def _lock_path(world: World) -> Path:
    uuid = TAB.rsplit(":", maxsplit=1)[-1].upper()
    return world.tmp / "ccc" / "locks" / f"tab-{uuid}.lock"


def _typing(world: World) -> None:
    """The typing-guard probes say: iTerm frontmost, a key 1 s ago, THE tab focused."""
    world.frontmost = True
    world.key_idle = 1.0
    world.focused = TAB


def _box_text(name: str) -> str:
    """The masked draft text of a fixture's input box (the lines between the two rules)."""
    read = fixture_screen(name)
    rows = rule_rows(read)
    lines = [line_text(read.lines[y]) for y in range(rows[-2] + 1, rows[-1])]
    return " ".join(line.replace("\x00", " ").strip(" ❯") for line in lines).strip()


def test_paste_guard_empty_box_types_once(tmp_path: Path) -> None:
    world = _world(tmp_path)
    result = _paste(world)
    assert result.refusal == "" and result.state == "sent" and result.via == "python-api"
    assert result.input_check is None and result.tab == TAB and not result.held
    assert world.texts == [TEXT] and world.screen_reads == 1


@pytest.mark.parametrize(
    ("name", "chars"), [("draft_one_line", 28), ("draft_multi_line", 23), ("pasted_marker", 22)]
)
def test_paste_guard_draft_is_held_without_typing(tmp_path: Path, name: str, chars: int) -> None:
    world = _world(tmp_path, name)
    result = _paste(world)
    assert result.refusal == "input_not_empty" and result.held
    assert result.data == {"input_chars": chars, "input_mode": "prompt"}
    assert result.state == "" and result.via == "" and result.tab == TAB
    assert not world.texts


def test_paste_guard_bash_mode_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path, "bash_mode")
    result = _paste(world)
    assert result.refusal == "input_not_empty"
    assert result.data == {"input_chars": 0, "input_mode": "bash"}
    assert not world.texts


def test_paste_guard_fresh_unrecognized_box_is_not_ready(tmp_path: Path) -> None:
    world = _fresh_world(tmp_path, UNRECOGNIZED)
    result = _paste(world)
    assert result.refusal == "input_not_ready" and result.held and not result.data
    assert not world.texts


def test_paste_guard_established_unreadable_box_types(tmp_path: Path) -> None:
    world = _world(tmp_path, None)
    result = _paste(world)
    assert result.refusal == "" and result.state == "sent"
    assert result.input_check == "unreadable"
    assert world.texts == [TEXT]


def test_paste_guard_typing_guard_refuses_before_the_screen_read(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _typing(world)
    result = _paste(world)
    assert result.refusal == "user_active" and result.held
    assert world.screen_reads == 0 and not world.texts


def test_paste_guard_typing_guard_off_types_with_the_same_probes(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _typing(world)
    result = _paste(world, typing_guard=False)
    assert result.refusal == "" and result.state == "sent"
    assert world.focus_reads == 0 and world.screen_reads == 1
    assert world.texts == [TEXT]


def test_paste_guard_held_lock_is_tab_locked(tmp_path: Path) -> None:
    world = _world(tmp_path)
    path = _lock_path(world)
    path.parent.mkdir(parents=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        result = _paste(world)
    finally:
        os.close(fd)
    assert result.refusal == "tab_locked" and result.held and result.tab == TAB
    assert not world.texts and world.screen_reads == 0
    assert _paste(world).state == "sent"  # released → goes through


def test_paste_guard_tab_tty_mismatch_is_stale_tab(tmp_path: Path) -> None:
    world = _world(tmp_path, "draft_one_line", tab_tty="/dev/ttys099")
    result = _paste(world)
    assert result.refusal == "stale_tab" and not result.held
    assert not world.texts and world.screen_reads == 0


def test_paste_guard_foreign_job_pid_is_foreground_not_claude(tmp_path: Path) -> None:
    world = _world(tmp_path, "draft_one_line", job_pid=9999)
    result = _paste(world)
    assert result.refusal == "foreground_not_claude" and not result.held
    assert not world.texts and world.screen_reads == 0


def test_paste_guard_partial_send_is_reported(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.send_result = ("", "partial")
    result = _paste(world)
    assert result.refusal == "" and result.state == "partial" and result.via == ""
    assert world.texts == [TEXT]


def test_paste_guard_nothing_sent_is_state_none(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.send_result = ("", "none")
    result = _paste(world)
    assert result.refusal == "" and result.state == "none"


def test_paste_guard_claim_lost_types_nothing(tmp_path: Path) -> None:
    world = _world(tmp_path)
    seen: dict[str, Any] = {}

    def claim() -> bool:
        seen["focus_reads"] = world.focus_reads
        seen["screen_reads"] = world.screen_reads
        fd = os.open(_lock_path(world), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen["locked"] = False
        except BlockingIOError:
            seen["locked"] = True
        finally:
            os.close(fd)
        return False

    _typing(world)
    world.focused = "w0t9p0:OTHER-TAB"  # the typing guard runs and lets it through
    result = _paste(world, claim=claim)
    assert result.refusal == "claim_lost" and not result.held and result.state == ""
    assert seen == {"focus_reads": 1, "screen_reads": 1, "locked": True}
    assert not world.texts


class _Claim:
    """A claim that always wins and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return True


def test_paste_guard_claim_true_types(tmp_path: Path) -> None:
    world = _world(tmp_path)
    claim = _Claim()
    result = _paste(world, claim=claim)
    assert claim.calls == 1 and result.state == "sent" and world.texts == [TEXT]


def test_paste_guard_claim_is_not_called_when_a_guard_holds(tmp_path: Path) -> None:
    world = _world(tmp_path, "draft_one_line")
    claim = _Claim()
    result = _paste(world, claim=claim)
    assert result.refusal == "input_not_empty" and claim.calls == 0 and not world.texts


def test_paste_guard_input_guard_off_types_without_a_screen_read(tmp_path: Path) -> None:
    world = _world(tmp_path, "draft_one_line")
    world.input_guard = False
    result = _paste(world)
    assert result.refusal == "" and result.state == "sent"
    assert world.screen_reads == 0 and world.texts == [TEXT]


def test_paste_guard_not_running_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path)
    live = world.live()
    world.registered = False
    result = guarded_paste(world.deps(), live, TEXT)
    assert result.refusal == "not_running" and not world.texts


def test_paste_guard_held_notice_wording() -> None:
    data = {"input_chars": 28, "input_mode": "prompt"}
    assert held_notice("input_not_empty", data, "s1") == (
        "the input box of s1 holds 28 chars — clear it in that tab"
    )
    assert held_notice("user_active", data, "s1") == (
        "you are typing in s1's tab — held until the keyboard is idle"
    )
    assert held_notice("input_not_ready", data, "s1") == "the input box of s1 is not drawn yet"
    assert held_notice("tab_locked", data, "s1") == ("another delivery to s1's tab is in progress")
    for code in HELD_CODES - {"input_not_empty"}:
        assert "28" not in held_notice(code, data, "s1")
    assert held_notice("no_tab", None, "s1") == "no iTerm tab is known for s1"
    assert held_notice("ps_unreadable", data, "s1") == (
        "the process table could not be read (machine under load) for s1"
    )
    assert HELD_CODES == {
        "user_active",
        "input_not_empty",
        "input_not_ready",
        "tab_locked",
        "ps_unreadable",
    }


@pytest.mark.parametrize("unreadable_read", [0, 1])
def test_paste_guard_unreadable_ps_is_held_not_stale_tab(
    tmp_path: Path, unreadable_read: int
) -> None:
    # tp#1868: a timed-out ps (tab_for's read or revalidate's) holds; nothing typed.
    world = _world(tmp_path)
    world.ps_queue = [world.table()] * unreadable_read + [PsTimedOut()]
    result = _paste(world)
    assert result.refusal == "ps_unreadable" and result.held
    assert not world.texts and world.screen_reads == 0


def test_paste_guard_failed_ps_is_a_definite_stale_tab(tmp_path: Path) -> None:
    # tp#1868 B5: a ps that FAILS (a plain {}) is not load: no hold, stale_tab as before.
    world = _world(tmp_path)
    world.ps_queue = [{}, {}]
    result = _paste(world)
    assert result.refusal == "stale_tab" and not result.held and not world.texts


def test_paste_guard_draft_text_appears_nowhere(tmp_path: Path) -> None:
    name = "draft_one_line"
    draft = _box_text(name)
    assert len(draft) >= 5  # the check below means something
    world = _world(tmp_path, name)
    result = _paste(world)
    blob = repr(asdict(result)) + held_notice(result.refusal, result.data, "s1")
    assert draft not in blob
    for word in draft.split():
        if len(word) >= 5:
            assert word not in blob
