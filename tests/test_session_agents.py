"""Tests for the sessions-agents helpers (tp#1598)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center.adapters.claude import background_launch_scan, background_work_split
from command_center.session_agents import running_agents

FIXTURES = Path(__file__).parent / "fixtures"
PARENT = FIXTURES / "inflight" / "proj" / "sess-0001.jsonl"
B2 = FIXTURES / "session_agents" / "proj" / "sess-0002.jsonl"


@pytest.mark.parametrize("transcript", [PARENT, B2])
def test_helper_matches_background_work_split(transcript: Path) -> None:
    scan = background_launch_scan(transcript)
    assert scan is not None
    assert background_work_split(transcript) == (scan.pending, scan.keepwarm)
    assert scan.pending
    for task_id in scan.pending:
        assert task_id in scan.launches
        assert scan.launches[task_id].record.get("toolUseResult")


def test_running_only_the_running_agents() -> None:
    agents = running_agents(B2)
    assert agents is not None
    assert [a["id"] for a in agents] == ["ag-run", "ag-nometa", "ag-nots"]


def test_running_field_values() -> None:
    agents = running_agents(B2)
    assert agents is not None
    assert list(agents[0]) == [
        "id",
        "description",
        "agent_type",
        "state",
        "started_at",
        "tp_id",
    ]
    assert agents[0] == {
        "id": "ag-run",
        "description": "meta description",
        "agent_type": "meta-type",
        "state": "running",
        "started_at": "2026-11-03T02:51:34Z",
        "tp_id": 42,
    }


def test_running_tp_id_marker_and_null() -> None:
    by_id = {a["id"]: a for a in running_agents(B2) or []}
    assert by_id["ag-run"]["tp_id"] == 42  # the line-start marker, not the inline 7
    assert by_id["ag-nometa"]["tp_id"] is None


def test_running_meta_wins_and_input_is_fallback() -> None:
    by_id = {a["id"]: a for a in running_agents(B2) or []}
    assert by_id["ag-run"]["description"] == "meta description"  # not "input description"
    assert by_id["ag-nometa"]["description"] == "fallback description"
    assert by_id["ag-nometa"]["agent_type"] == "fallback-type"
    assert by_id["ag-nots"]["agent_type"] == "nots-type"


def test_running_started_at_null_without_timestamp() -> None:
    by_id = {a["id"]: a for a in running_agents(B2) or []}
    assert by_id["ag-nots"]["started_at"] is None
    assert by_id["ag-nometa"]["started_at"] == "2026-11-03T02:54:01Z"


def test_running_cap_of_20(tmp_path: Path) -> None:
    lines = []
    for n in range(25):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "toolUseResult": {"status": "async_launched", "agentId": f"ag{n}"},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}"}]},
                }
            )
        )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    agents = running_agents(transcript)
    assert agents is not None
    assert [a["id"] for a in agents] == [f"ag{n}" for n in range(20)]


def test_running_none_for_missing_and_malformed(tmp_path: Path) -> None:
    assert running_agents(tmp_path / "missing.jsonl") is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"user"}\nnot json\n{"type":"user"}\n', encoding="utf-8")
    assert running_agents(bad) is None
    assert background_work_split(bad) is None


def test_running_empty_list_without_launches(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    assert running_agents(transcript) == []


def test_running_parity_with_inflight() -> None:
    from command_center import inflight

    expected = [
        item.id
        for item in inflight._transcript_items(PARENT)  # pylint: disable=protected-access
        if item.kind == inflight.KIND_SUBAGENT
    ]
    agents = running_agents(PARENT)
    assert agents is not None
    assert [a["id"] for a in agents] == expected
    assert expected


def test_helper_launch_carries_tool_use_input(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Agent",'
        '"input":{"description":"d","subagent_type":"x","prompt":"p"}}]}}\n'
        '{"type":"user","timestamp":"2026-11-03T02:51:34.117Z",'
        '"message":{"content":[{"type":"tool_result","tool_use_id":"tu1","content":"ok"}]},'
        '"toolUseResult":{"status":"async_launched","agentId":"ag1"}}\n',
        encoding="utf-8",
    )
    scan = background_launch_scan(transcript)
    assert scan is not None
    assert scan.pending == ["ag1"]
    assert scan.launches["ag1"].tool_input["description"] == "d"
    assert scan.launches["ag1"].record["timestamp"] == "2026-11-03T02:51:34.117Z"


def test_helper_none_when_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    assert background_launch_scan(missing) is None
    assert background_work_split(missing) is None
