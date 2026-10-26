"""``ccc continue-session`` (tp#1300): the read-only verbs on fixture transcripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import cli, continue_session, handoff_doc, inflight
from command_center.adapters import claude as claude_adapter
from command_center.models import LiveSession
from command_center.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "inflight"
PARENT = FIXTURES / "proj" / "sess-0001.jsonl"
SID = "sess-0001"


def _ps_table() -> dict[int, list[tuple[int, str]]]:
    children: dict[int, list[tuple[int, str]]] = {}
    for line in (FIXTURES / "ps.txt").read_text(encoding="utf-8").splitlines():
        pid, ppid, command = line.split(None, 2)
        children.setdefault(int(ppid), []).append((int(pid), command))
    return children


class _Adapter(claude_adapter.ClaudeAdapter):
    """The real adapter on fixture data: one live session, the fixture transcript."""

    transcript: Path | None = PARENT

    def discover(self) -> list[LiveSession]:
        return [
            LiveSession(pid=4242, session_id=SID, cwd="/repo", alive=True, config_dir="/cfg/work")
        ]

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        del cwd, session_id, config_dir
        return self.transcript


@pytest.fixture(name="fixture_session")
def _fixture_session(monkeypatch: pytest.MonkeyPatch) -> _Adapter:
    adapter = _Adapter()
    monkeypatch.setattr(continue_session, "_adapter", lambda: adapter)
    monkeypatch.setattr(claude_adapter, "_children_map", _ps_table)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.set_aim(SID, "tp#1 shipped")
        store.set_subgoals(SID, ["build", "test", "ship"])
        first = store.list_subgoals(SID)[0]
        store.set_subgoal_checked(first.id, True)
        store.update_fields(SID, no_codex=True, model="opus-5")
    return adapter


@pytest.mark.usefixtures("fixture_session")
def test_state_json_reports_everything_the_handoff_needs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["continue-session", "state", "-s", SID, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session_id"] == SID and data["cwd"] == "/repo"
    assert data["live_pid"] == 4242
    assert data["aim"] == "tp#1 shipped" and data["aim_rev"] == 1
    assert data["subgoals"] == {"unchecked": ["test", "ship"], "checked": ["build"]}
    assert data["no_codex"] is True and data["model"] == "opus-5"
    assert data["active_transfer"] is None
    inv = data["inflight"]
    assert inv["clear"] is False
    kinds = {item["id"]: item["kind"] for item in inv["items"]}
    assert kinds["a1"] == inflight.KIND_SUBAGENT
    assert kinds["b1"] == inflight.KIND_SHELL
    assert kinds["k1"] == inflight.KIND_KEEPWARM


@pytest.mark.usefixtures("fixture_session")
def test_state_text_lists_items(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["continue-session", "state", "-s", SID]) == 0
    out = capsys.readouterr().out
    assert "in flight: NOT clear" in out
    assert "[ ] test" in out and "[x] build" in out
    assert "subagent  a1" in out


def test_template_prints_the_package_template(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["continue-session", "template"]) == 0
    assert capsys.readouterr().out == handoff_doc.template()


def test_drain_times_out_while_work_is_in_flight(
    fixture_session: _Adapter, capsys: pytest.CaptureFixture[str]
) -> None:
    del fixture_session
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "0", "-j"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["drained"] is False and data["reasons"]


def test_drain_returns_once_clear(
    fixture_session: _Adapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean = tmp_path / "clean.jsonl"
    clean.write_text('{"type": "user", "message": {"content": "hi"}}\n', encoding="utf-8")
    fixture_session.transcript = clean
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "5"]) == 0
    assert "in flight: clear" in capsys.readouterr().out


def test_unreadable_transcript_never_drains(
    fixture_session: _Adapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture_session.transcript = tmp_path / "missing.jsonl"
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "0"]) == 1


@pytest.mark.parametrize("verb", [[], ["state"], ["template"], ["drain"]])
def test_help_exits_zero(verb: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["continue-session", *verb, "-h"])
    assert exc.value.code == 0
    assert "usage: ccc continue-session" in capsys.readouterr().out
