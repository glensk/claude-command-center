"""``ccc answer -s ID -j``: decision_id binding, the picker key protocol, verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bridgestub import SID, World, answer, ask, question, run_cli, user

from command_center import bridge_answer
from command_center.adapters import claude_bridge as cb
from command_center.adapters.claude_agents import AgentEntry
from command_center.bridge_json import BridgeError

COLOUR = question("Which colour?", ["red", "green (Recommended)", "blue"])
FRUIT = question("Which fruits?", ["apple", "pear", "plum"], multi=True)
TOOL_ID = "toolu_pick1"


def _world(tmp_path: Path, questions: list[dict[str, Any]], **kw: Any) -> World:
    world = World(tmp_path, status=kw.pop("status", "waiting"), **kw)
    world.write(user("decide"), ask(TOOL_ID, questions))
    return world


def _decision_id(world: World) -> str:
    ins = cb.inspect(cb.load_records(world.transcript))
    assert ins.anchor is not None
    return cb.decision_id(ins.anchor, ins.questions)


def _answer(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: Any,
) -> tuple[int, dict[str, Any] | None, str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return run_cli(monkeypatch, capsys, world, ["answer", "-s", SID, "-j"], stdin=text)


def _qs(*raw: dict[str, Any]) -> list[cb.Question]:
    return cb.ask_questions(cb.PendingAsk("t", list(raw)))


# ------------------------------------------------------------------ the key protocol


def test_single_question_single_select_keys() -> None:
    keys = bridge_answer.picker_keys(_qs(COLOUR), [{"question_index": 0, "option_indices": [2]}])
    assert keys == ["down", "down", "enter"]  # one single-select submits on the pick itself


def test_single_question_multi_select_keys() -> None:
    keys = bridge_answer.picker_keys(_qs(FRUIT), [{"question_index": 0, "option_indices": [2, 0]}])
    # tick 3 then 1 (focus stays on option 1), down × (3 options + 1) → Submit, review enter
    assert keys == ["3", "1", "down", "down", "down", "down", "enter", "wait:0.6", "enter"]


def test_other_text_focuses_type_something() -> None:
    keys = bridge_answer.picker_keys(
        _qs(COLOUR), [{"question_index": 0, "other_text": "purple, please"}]
    )
    assert keys == ["down", "down", "down", "text:purple, please", "enter"]


def test_multi_question_keys_in_question_order() -> None:
    keys = bridge_answer.picker_keys(
        _qs(COLOUR, FRUIT),
        [
            {"question_index": 1, "option_indices": [0, 2]},
            {"question_index": 0, "option_indices": [1]},
        ],
    )
    assert keys == [
        "down",
        "enter",
        "wait:0.6",
        "1",
        "3",
        "down",
        "down",
        "down",
        "down",
        "enter",
        "wait:0.6",
        "enter",
    ]


@pytest.mark.parametrize(
    ("answers", "code"),
    [
        ([{"question_index": 0, "option_indices": [0, 1]}], "invalid_answer"),
        ([{"question_index": 0, "option_indices": [3]}], "invalid_answer"),
        ([{"question_index": 1, "option_indices": [0]}], "invalid_answer"),
        (
            [
                {"question_index": 0, "option_indices": [0]},
                {"question_index": 0, "option_indices": [1]},
            ],
            "invalid_answer",
        ),
    ],
)
def test_bad_answers_are_refused(answers: list[dict[str, Any]], code: str) -> None:
    with pytest.raises(BridgeError) as exc:
        bridge_answer.picker_keys(_qs(COLOUR), answers)
    assert exc.value.code == code


def test_unsupported_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BridgeError) as exc:
        bridge_answer.picker_keys(_qs(FRUIT), [{"question_index": 0, "other_text": "kiwi"}])
    assert exc.value.code == "unsupported_shape"
    monkeypatch.setattr(bridge_answer, "MULTI_SELECT_SUPPORTED", False)
    with pytest.raises(BridgeError) as exc:
        bridge_answer.picker_keys(_qs(FRUIT), [{"question_index": 0, "option_indices": [0]}])
    assert exc.value.code == "unsupported_shape"


# ------------------------------------------------------------------ the CLI


def test_answer_is_typed_and_verified_by_tool_use_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR, FRUIT])

    def on_keys(w: World, _keys: list[str]) -> None:
        # an unrelated tool_result first (another call), then this call's answer
        w.write(answer("toolu_other", [COLOUR], {"Which colour?": "red"}))
        w.write(
            answer(
                TOOL_ID,
                [COLOUR, FRUIT],
                {"Which colour?": "green (Recommended)", "Which fruits?": "plum, apple"},
            )
        )

    world.on_keys = on_keys
    did = _decision_id(world)
    code, env, _err = _answer(
        world,
        monkeypatch,
        capsys,
        {
            "decision_id": did,
            "answers": [
                {"question_index": 0, "option_indices": [1]},
                {"question_index": 1, "option_indices": [2, 0]},
            ],
        },
    )
    assert code == 0 and env is not None and env["ok"] is True
    assert env["data"]["outcome"] == "accepted" and env["data"]["tool_use_id"] == TOOL_ID
    (keys,) = world.keys
    assert keys[:3] == ["down", "enter", "wait:0.6"] and keys[-1] == "enter"


def test_multi_select_answer_on_a_resolved_tab_compares_label_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The stored tab id is stale: the tab is found by the pid's tty (S-IDENT). The
    # recorded multi-select answer lists the labels in tick order (S-PICKER).
    from command_center.store import Store

    world = _world(tmp_path, [FRUIT])
    with Store(world.db) as store:
        store.update_fields(SID, iterm_session_id="w9t9p9:GONE")
    world.on_keys = lambda w, _k: w.write(
        answer(TOOL_ID, [FRUIT], {"Which fruits?": "plum, apple"})
    )
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "option_indices": [0, 2]}],
    }
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert world.keys == [["1", "3", "down", "down", "down", "down", "enter", "wait:0.6", "enter"]]


def test_recorded_answer_that_differs_is_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR])
    world.on_keys = lambda w, _k: w.write(answer(TOOL_ID, [COLOUR], {"Which colour?": "blue"}))
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "option_indices": [0]}],
    }
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None
    assert env["error"]["code"] == "answer_mismatch" and env["data"]["mismatched_questions"] == [0]


def test_other_text_answer_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR])
    world.on_keys = lambda w, _k: w.write(answer(TOOL_ID, [COLOUR], {"Which colour?": "teal"}))
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "other_text": "teal"}],
    }
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert "text:teal" in world.keys[0]


def test_decision_id_mismatch_is_decision_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR])
    payload = {"decision_id": "0" * 64, "answers": [{"question_index": 0, "option_indices": [0]}]}
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "decision_changed"
    assert not world.keys


def test_no_pending_picker_is_decision_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR])
    did = _decision_id(world)
    world.write(answer(TOOL_ID, [COLOUR], {"Which colour?": "red"}))  # answered at the Mac
    payload = {"decision_id": did, "answers": [{"question_index": 0, "option_indices": [0]}]}
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "decision_changed"


def test_not_waiting_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR], status="busy")
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "option_indices": [0]}],
    }
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "not_waiting"


@pytest.mark.parametrize("registered", [True, False])
def test_background_session_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registered: bool,
) -> None:
    """A ``bg`` registry entry — or a job only ``claude agents`` lists — is never typed into."""
    world = _world(tmp_path, [COLOUR], kind="bg", registered=registered)
    world.agents = [AgentEntry(config_dir=str(world.config_dir), session_id=SID, kind="background")]
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "option_indices": [0]}],
    }
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "background"
    assert not world.keys


def test_unsupported_shape_types_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bridge_answer, "MULTI_SELECT_SUPPORTED", False)
    world = _world(tmp_path, [FRUIT])
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "option_indices": [0]}],
    }
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "unsupported_shape"
    assert not world.keys


def test_cancelled_picker_and_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path, [COLOUR])
    payload = {
        "decision_id": _decision_id(world),
        "answers": [{"question_index": 0, "option_indices": [0]}],
    }
    world.on_keys = lambda w, _k: w.write(answer(TOOL_ID, [COLOUR], {}, error=True))
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "answer_failed"

    world2 = _world(tmp_path / "b", [COLOUR])
    payload["decision_id"] = _decision_id(world2)
    code, env, _err = _answer(world2, monkeypatch, capsys, payload)
    assert code == 1 and env is not None and env["error"]["code"] == "answer_unknown"
    assert env["data"]["outcome"] == "unknown" and world2.mono >= 10.0


@pytest.mark.parametrize(
    "payload",
    [
        {"decision_id": "0" * 64, "answers": [], "extra": 1},
        {
            "decision_id": "0" * 64,
            "answers": [{"question_index": 0, "option_indices": [0], "x": 1}],
        },
        {"decision_id": "short", "answers": [{"question_index": 0, "option_indices": [0]}]},
        {"decision_id": "0" * 64, "answers": [{"question_index": True, "option_indices": [0]}]},
        {
            "decision_id": "0" * 64,
            "answers": [{"question_index": 0, "option_indices": [0], "other_text": "a"}],
        },
        "not json",
        "[1, 2]",
    ],
)
def test_malformed_input_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: Any,
) -> None:
    world = _world(tmp_path, [COLOUR])
    code, env, _err = _answer(world, monkeypatch, capsys, payload)
    assert code == 2 and env is not None and env["ok"] is False
    assert not world.keys
