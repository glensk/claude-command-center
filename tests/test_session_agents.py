"""Tests for the sessions-agents helpers (tp#1598)."""

from __future__ import annotations

from pathlib import Path

from command_center.adapters.claude import background_launch_scan, background_work_split

FIXTURES = Path(__file__).parent / "fixtures"
PARENT = FIXTURES / "inflight" / "proj" / "sess-0001.jsonl"


def test_helper_matches_background_work_split() -> None:
    scan = background_launch_scan(PARENT)
    assert scan is not None
    assert background_work_split(PARENT) == (scan.pending, scan.keepwarm)
    assert scan.pending
    for task_id in scan.pending:
        assert task_id in scan.launches
        assert scan.launches[task_id].record.get("toolUseResult")


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
