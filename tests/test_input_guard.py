"""The input-box guard of ``ccc send`` (``input_not_empty`` / ``input_not_ready``, tp#1844).

Every test drives ``ccc send`` through :func:`bridgestub.run_cli` with a fake :class:`World`
whose tab "shows" one of the masked screen captures in ``tests/fixtures/composer``.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

import pytest
from bridgestub import (
    SID,
    TAB,
    World,
    answer,
    ask,
    enqueue,
    fixture_screen,
    question,
    run_cli,
    turn_end,
    user,
)

from command_center import composer
from command_center.adapters import claude_bridge as cb
from command_center.screenread import ScreenCell, ScreenRead, line_text, rule_rows
from command_center.store import Store

TOOL_ID = "toolu_input_guard"
COLOUR = question("Which colour?", ["red", "green"])
#: A screen with no full-width rule: the parser cannot find the box.
UNRECOGNIZED = ScreenRead(80, (0, 0), ((ScreenCell("x"), ScreenCell("y")),))


def _idle_world(tmp_path: Path, screen: str | None = "empty_idle", **kw: Any) -> World:
    world = World(tmp_path, **kw)
    world.screen = fixture_screen(screen) if screen is not None else None
    world.write(user("earlier"), *turn_end())
    world.on_text = lambda w, text: w.write(user(text))
    return world


def _fresh_world(tmp_path: Path, screen: ScreenRead | None) -> World:
    """A session that has not been prompted yet (no transcript on disk)."""
    world = World(tmp_path)
    world.screen = screen
    world.transcript.unlink()
    world.transcript_resolves_missing = True
    return world


def _send(
    world: World, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str]
) -> tuple[int, Any, str]:
    return run_cli(mp, cap, world, ["send", "-s", SID, "-j"], stdin="hello")


def _deliveries(world: World) -> list[Any]:
    with Store(world.db) as store:
        return list(store.deliveries_for(SID))


def _box_text(name: str) -> str:
    """The masked draft text of a fixture's input box (the lines between the two rules)."""
    read = fixture_screen(name)
    rows = rule_rows(read)
    lines = [line_text(read.lines[y]) for y in range(rows[-2] + 1, rows[-1])]
    return " ".join(line.replace("\x00", " ").strip(" ❯ ") for line in lines).strip()


def _assert_no_draft(name: str, env: Any, err: str) -> None:
    draft = _box_text(name)
    assert len(draft) >= 5  # the check below means something
    dumped = json.dumps(env, ensure_ascii=False)
    for blob in (dumped, err):
        assert draft not in blob
        for word in draft.split():
            if len(word) >= 5:
                assert word not in blob


@pytest.mark.parametrize(
    ("name", "chars"), [("draft_one_line", 28), ("draft_multi_line", 23), ("pasted_marker", 22)]
)
def test_input_guard_refuses_a_draft_without_typing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    chars: int,
) -> None:
    world = _idle_world(tmp_path, name)
    code, env, err = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["ok"] is False
    assert env["error"]["code"] == "input_not_empty"
    assert env["data"] == {"input_chars": chars, "input_mode": "prompt"}
    assert f"{chars} character(s)" in env["error"]["message"]
    assert not world.texts and not _deliveries(world)  # nothing typed, no delivery row
    _assert_no_draft(name, env, err)


def test_input_guard_refuses_one_stray_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "stray_one_char")
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["error"]["code"] == "input_not_empty"
    assert env["data"]["input_chars"] == 1
    assert not world.texts


def test_input_guard_refuses_bash_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "bash_mode")
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["error"]["code"] == "input_not_empty"
    assert env["data"] == {"input_chars": 0, "input_mode": "bash"}
    assert "in bash mode" in env["error"]["message"]
    assert not world.texts


@pytest.mark.parametrize("name", ["empty_idle", "fresh_placeholder", "suggestion_ghost"])
def test_input_guard_lets_an_empty_box_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    world = _idle_world(tmp_path, name)
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert "input_check" not in env["data"]
    assert world.texts == ["hello"] and world.screen_reads == 1


def test_input_guard_lets_a_busy_session_with_a_queued_message_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "busy_queued", status="busy")
    world.write(user("long job"))
    world.on_text = lambda w, text: w.write(enqueue(text))
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert "input_check" not in env["data"]


def test_input_guard_unreadable_screen_on_an_established_session_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, None)
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert env["data"]["input_check"] == "unreadable"
    assert world.texts == ["hello"]


def test_input_guard_unrecognized_box_on_an_established_session_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    world.screen = UNRECOGNIZED
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert env["data"]["input_check"] == "unrecognized"
    assert world.texts == ["hello"]


@pytest.mark.parametrize("screen", [UNRECOGNIZED, None], ids=["unrecognized", "unreadable"])
def test_input_guard_fresh_session_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    screen: ScreenRead | None,
) -> None:
    world = _fresh_world(tmp_path, screen)
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["error"]["code"] == "input_not_ready"
    assert env["data"] is None
    assert not world.texts and not _deliveries(world)


def test_input_guard_fresh_session_with_an_empty_box_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _fresh_world(tmp_path, fixture_screen("fresh_placeholder"))

    def first_prompt(w: World, text: str) -> None:
        w.write(user(text))

    world.on_text = first_prompt
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert "input_check" not in env["data"]


@pytest.mark.parametrize("screen", [UNRECOGNIZED, None], ids=["unrecognized", "unreadable"])
def test_input_guard_just_started_session_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    screen: ScreenRead | None,
) -> None:
    world = _idle_world(tmp_path)
    world.screen = screen
    world.started_at = world.clock_ms - 5_000  # 5 s ago, inside the 30 s grace
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["error"]["code"] == "input_not_ready"
    assert not world.texts

    world.started_at = world.clock_ms - 31_000  # past the grace: fail open again
    code, env, _err = _send(world, monkeypatch, capsys)
    assert (
        code == 0
        and env is not None
        and env["data"]["input_check"]
        in (
            "unrecognized",
            "unreadable",
        )
    )


def test_input_guard_parser_exception_fails_open_without_leaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "draft_one_line")
    draft = _box_text("draft_one_line")

    def boom(_read: ScreenRead) -> composer.Composer:
        raise ValueError(f"cannot parse {draft!r}")

    monkeypatch.setattr(composer, "read_composer", boom)
    code, env, err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert env["data"]["input_check"] == "unreadable"
    assert "ValueError" in err
    _assert_no_draft("draft_one_line", env, err)


def test_input_guard_screen_read_exception_fails_open_without_leaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "draft_one_line")
    draft = _box_text("draft_one_line")

    def boom(_tab: str) -> ScreenRead | None:
        raise RuntimeError(f"screen said {draft!r}")

    deps = world.deps()
    deps.screen = boom
    monkeypatch.setattr(world, "deps", lambda: deps)
    code, env, err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["input_check"] == "unreadable"
    assert "RuntimeError" in err
    _assert_no_draft("draft_one_line", env, err)


def test_input_guard_switch_off_skips_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "draft_one_line", input_guard=False)
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert "input_check" not in env["data"]
    assert world.screen_reads == 0


def test_input_guard_runs_after_the_typing_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, "draft_one_line", frontmost=True, key_idle=2.0, focused=TAB)
    code, env, _err = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["error"]["code"] == "user_active"
    assert world.screen_reads == 0


def test_input_guard_reads_inside_the_per_tab_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    held: list[bool] = []
    screen = world.screen

    def screen_tail(_tab: str) -> ScreenRead | None:
        lock = next((tmp_path / "ccc" / "locks").glob("tab-*.lock"))
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(fd)
        return screen

    world.screen_tail = screen_tail  # type: ignore[assignment]
    assert _send(world, monkeypatch, capsys)[0] == 0
    assert held == [True]


def test_input_guard_answer_never_reads_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, status="waiting")
    world.screen = fixture_screen("draft_one_line")
    world.write(user("decide"), ask(TOOL_ID, [COLOUR]))
    ins = cb.inspect(cb.load_records(world.transcript))
    assert ins.anchor is not None
    payload = {
        "decision_id": cb.decision_id(ins.anchor, ins.questions),
        "answers": [{"question_index": 0, "option_indices": [1]}],
    }
    world.on_keys = lambda w, _k: w.write(answer(TOOL_ID, [COLOUR], {"Which colour?": "green"}))
    code, env, _err = run_cli(
        monkeypatch, capsys, world, ["answer", "-s", SID, "-j"], stdin=json.dumps(payload)
    )
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert world.screen_reads == 0


def test_input_guard_config_switch_defaults_on_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    assert config.DEFAULTS["bridge_input_guard"] is True
    assert config.Config().bridge_input_guard is True
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: path)
    for raw, want in (("false", False), ("true", True), ('"no"', True)):
        path.write_text(f"bridge_input_guard = {raw}\n", encoding="utf-8")
        config.invalidate_config_cache()
        assert config.load_config().bridge_input_guard is want
    config.invalidate_config_cache()
