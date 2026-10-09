"""``ccc inspect -j``: the S-INSPECT fixtures parse exactly, plus the CLI contract."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from bridgestub import (
    SID,
    World,
    ask,
    assistant,
    question,
    run_cli,
    task_notice,
    turn_end,
    user,
)

from command_center.adapters import claude_bridge as cb

FIXTURES = Path(__file__).parent / "fixtures" / "inspect"
NAMES = ("todo_decision", "pending_ask", "busy", "answered_ask")
HEX64 = re.compile(r"[0-9a-f]{64}")


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(data))
    if out.get("decision"):
        assert HEX64.fullmatch(out["decision"]["decision_id"])
        out["decision"]["decision_id"] = "<sha256>"
    return out


@pytest.mark.parametrize("name", NAMES)
def test_fixture_parses_exactly(name: str) -> None:
    records = cb.load_records(FIXTURES / f"{name}.jsonl")
    expected = json.loads((FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8"))
    assert _normalise(cb.inspect_records(records)) == expected


@pytest.mark.parametrize("name", ("todo_decision", "pending_ask"))
def test_decision_id_is_a_stable_sha256(name: str) -> None:
    first = cb.inspect_records(cb.load_records(FIXTURES / f"{name}.jsonl"))
    again = cb.inspect_records(cb.load_records(FIXTURES / f"{name}.jsonl"))
    did = first["decision"]["decision_id"]
    assert HEX64.fullmatch(did)
    assert did == again["decision"]["decision_id"]


def test_decision_id_changes_with_the_options() -> None:
    q = cb.Question("Pick?", False, [{"label": "a", "consequence": None}])
    q2 = cb.Question("Pick?", False, [{"label": "b", "consequence": None}])
    assert cb.decision_id("toolu_1", [q]) != cb.decision_id("toolu_1", [q2])
    assert cb.decision_id("toolu_1", [q]) != cb.decision_id("toolu_2", [q])


def _world_from_fixture(tmp_path: Path, name: str, **kw: Any) -> World:
    world = World(tmp_path, **kw)
    shutil.copyfile(FIXTURES / f"{name}.jsonl", world.transcript)
    return world


@pytest.mark.parametrize("name", NAMES)
def test_cli_envelope_matches_the_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    expected = json.loads((FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8"))
    world = _world_from_fixture(tmp_path, name, status=expected["state"])
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 0 and env is not None
    assert env["schema_version"] == 1 and env["ok"] is True and env["error"] is None
    data = env["data"]
    assert data.pop("live") is True
    assert _normalise(data) == expected
    # the fake router answers nothing, so recent_summary is the 3-sentence fallback
    assert len(world.llm_calls) == (1 if expected["decision"] else 0)


def test_malformed_transcript_is_transcript_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.write(user("hello"))
    with world.transcript.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    world.write(*turn_end())
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 1 and env is not None
    assert env == {
        "schema_version": 1,
        "ok": False,
        "data": None,
        "error": {"code": "transcript_unknown", "message": env["error"]["message"]},
    }


def test_torn_last_line_is_not_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.write(user("hello"), *turn_end("all good"))
    with world.transcript.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assistant", "mess')  # Claude Code mid-write
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 0 and env is not None
    assert env["data"]["last_reply"] == "all good"


def test_live_status_overrides_the_transcript_state_and_hides_todo_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world_from_fixture(tmp_path, "todo_decision", status="busy")
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 0 and env is not None
    assert env["data"]["state"] == "busy"
    assert env["data"]["decision"] is None  # todo-line decisions only while idle


def test_caps_keep_the_reply_tail_and_the_prompt_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.write(user("P" * 400 + "Q" * 400), assistant("A" * 1000 + "Z" * 1000))
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 0 and env is not None
    reply, prompt = env["data"]["last_reply"], env["data"]["last_prompt"]
    assert len(reply) == 1500 and reply.startswith("…") and reply.endswith("Z")
    assert len(prompt) == 500 and prompt.startswith("P") and prompt.endswith("…")


def test_voice_brief_llm_summary_is_used_and_cached_per_transcript_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world_from_fixture(tmp_path, "pending_ask", status="waiting")
    world.llm_answer = "The session sets up a service and needs **two** choices."
    with world.store() as store:
        store.update_fields(SID, aim="the example service runs (tp#42)")
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 0 and env is not None
    ctx = env["data"]["decision"]["context"]
    assert ctx == {
        "aim": "the example service runs (tp#42)",
        "ticket": "tp#42",
        "recent_summary": "The session sets up a service and needs two choices.",
    }
    run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert len(world.llm_calls) == 1  # same transcript size → cached
    world.write(task_notice())  # the transcript grew, the picker is still pending
    run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert len(world.llm_calls) == 2


def test_no_llm_flag_and_failed_router_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world_from_fixture(tmp_path, "pending_ask", status="waiting")
    world.llm_answer = "LLM text"
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-N", "-j"])
    assert code == 0 and env is not None and not world.llm_calls
    summary = env["data"]["decision"]["context"]["recent_summary"]
    assert summary == "Two settings need your choice before I continue."


def test_unknown_session_and_missing_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", "nope", "-j"])
    assert code == 1 and env is not None and env["error"]["code"] == "unknown_session"
    world.transcript.unlink()
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-j"])
    assert code == 1 and env is not None and env["error"]["code"] == "transcript_missing"


def test_pending_ask_is_found_behind_queue_operations(tmp_path: Path) -> None:
    q = [question("Colour?", ["red", "green (Recommended)"])]
    world = World(tmp_path)
    world.write(
        user("go"),
        ask("toolu_x", q),
        {"type": "queue-operation", "operation": "enqueue", "content": "<task-notification>x"},
        {"type": "attachment", "attachment": {"type": "hook_success"}},
    )
    ins = cb.inspect(cb.load_records(world.transcript))
    assert ins.transcript_state == "waiting" and ins.source == "ask_user_question"
    assert ins.questions[0].options[1]["label"] == "green"
    assert ins.questions[0].raw_labels[1] == "green (Recommended)"
    assert ins.questions[0].recommendation == "green"


def test_unknown_cli_argument_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    code, _env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "--bogus"])
    assert code == 2
