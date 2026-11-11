"""``ccc continue-session lean-check`` (tp#1681 L19c): the read-only code-relay verdict."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_continue_session import SID

# pylint: disable=unused-import  # the fixture is re-used by name from test_continue_session
from test_continue_session import _fixture_session as fixture_session  # noqa: F401

# pylint: enable=unused-import
from command_center import cli, config, handoff_facts, inflight, relay_state
from command_center.store import Store


def _providers(monkeypatch: pytest.MonkeyPatch, *specs: dict[str, Any]) -> None:
    cfg = config.Config(handoff_facts=list(specs))
    monkeypatch.setattr(config, "load_config", lambda: cfg)


_REQUIRED = {"argv": [sys.executable, "-c", "pass"], "name": "tp"}


def _recent_next_step(text: str = "land L19c", age_s: float = 60) -> None:
    with Store() as store:
        store.update_fields(SID, next_step=text, last_response_at=int((time.time() - age_s) * 1000))


def _running_agents() -> list[str]:
    from test_continue_session import PARENT  # pylint: disable=import-outside-toplevel

    items = inflight._transcript_items(PARENT)
    return [i.id for i in items if i.kind == inflight.KIND_SUBAGENT]


def _capsule(tmp_path: Path, agent_id: str) -> None:
    capsule = tmp_path / f"{agent_id}.md"
    capsule.write_text("## Status\ncontinue\n", encoding="utf-8")
    path = relay_state.record_path(SID, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "capsule_path": str(capsule), "call": {"prompt": "go"}}
    path.write_text(json.dumps(body), encoding="utf-8")


def _run(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, dict[str, Any]]:
    code = cli.main(["continue-session", "lean-check", "-s", SID, "-j", *extra])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_lists_every_missing_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _providers(monkeypatch)
    code, data = _run(capsys)
    assert code == 1 and data["ready"] is False
    agents = _running_agents()
    assert agents  # the fixture transcript has a running subagent
    assert data["missing"] == ["next_action", "facts_providers"] + [f"capsule:{a}" for a in agents]
    assert data["aim"] is True and data["next_action"] == ""
    assert set(data) == {
        "version",
        "session_id",
        "ready",
        "missing",
        "aim",
        "next_action",
        "providers",
        "agents",
    }


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_ready_with_aim_next_step_provider_and_capsules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _providers(monkeypatch, _REQUIRED)
    _recent_next_step()
    for agent in _running_agents():
        _capsule(tmp_path, agent)
    code, data = _run(capsys)
    assert (code, data["ready"], data["missing"]) == (0, True, [])
    assert data["next_action"] == "next_step" and data["providers"] == ["tp"]
    assert all(a["capsule"] for a in data["agents"])


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_stale_next_step_and_optional_only_providers_are_not_enough(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _providers(monkeypatch, {**_REQUIRED, "optional": True})
    _recent_next_step(age_s=7200)
    for agent in _running_agents():
        _capsule(tmp_path, agent)
    code, data = _run(capsys)
    assert code == 1 and data["missing"] == ["next_action", "facts_providers"]
    _code, data = _run(capsys, "-r", "9000")
    assert data["next_action"] == "next_step"


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_a_recent_capture_counts_as_the_checkpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _providers(monkeypatch, _REQUIRED)
    path = handoff_facts.capture_path(SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": handoff_facts.VERSION, "facts": []}), encoding="utf-8")
    _code, data = _run(capsys)
    assert data["next_action"] == "capture"
    old = time.time() - 7200
    os.utime(path, (old, old))
    _code, data = _run(capsys)
    assert data["next_action"] == ""


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_a_consumed_relaunch_record_is_no_capsule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _providers(monkeypatch, _REQUIRED)
    _recent_next_step()
    agents = _running_agents()
    for agent in agents:
        _capsule(tmp_path, agent)
    path = relay_state.record_path(SID, agents[0])
    body = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**body, "consumed": "launched: x"}), encoding="utf-8")
    code, data = _run(capsys)
    assert code == 1 and data["missing"] == [f"capsule:{agents[0]}"]


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _providers(monkeypatch, _REQUIRED)
    with Store() as store:
        store.update_fields(SID, active_subagents=5, subagent_started_at=1)  # a stale counter
        before = store.get(SID)
    _run(capsys)
    with Store() as store:
        assert store.get(SID) == before  # `state` would have reset the stale counter
    assert not handoff_facts.capture_path(SID).exists()


@pytest.mark.usefixtures("fixture_session")
def test_lean_check_unknown_session_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(Path("/"))
    code = cli.main(["continue-session", "lean-check", "-j"])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["ready"] is False and data["missing"] == ["session"]


def test_lean_check_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["continue-session", "lean-check", "-h"])
    assert exc.value.code == 0
    assert "lean-check -s <sid> -j" in capsys.readouterr().out
