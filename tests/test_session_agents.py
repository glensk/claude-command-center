"""Tests for the sessions-agents helpers (tp#1598)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from command_center import session_agents
from command_center.adapters.claude import background_launch_scan, background_work_split
from command_center.session_agents import inventory, running_agents
from command_center.store import Store

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


# ---- inventory (persistent final-list cache) -------------------------------------------

_LAUNCH = (
    '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Agent",'
    '"input":{"description":"in desc","subagent_type":"in-type","prompt":"p"}}]}}\n'
    '{"type":"user","timestamp":"2026-11-03T02:51:34.117Z",'
    '"message":{"content":[{"type":"tool_result","tool_use_id":"tu1","content":"ok"}]},'
    '"toolUseResult":{"status":"async_launched","agentId":"ag1"}}\n'
)
_DONE = (
    '{"type":"user","message":{"content":"<task-notification><task-id>ag1</task-id>'
    '</task-notification>"}}\n'
)


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "state.db") as opened:
        yield opened


def _transcript(tmp_path: Path, text: str = _LAUNCH, name: str = "s1") -> Path:
    path = tmp_path / "proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_meta(transcript: Path, agent_id: str, description: str, agent_type: str) -> Path:
    meta = transcript.parent / transcript.stem / "subagents" / f"agent-{agent_id}.meta.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps({"description": description, "agentType": agent_type}), encoding="utf-8"
    )
    return meta


def _rows(store: Store) -> int:
    return int(store.conn.execute("SELECT COUNT(*) FROM subagent_scan").fetchone()[0])


def test_inventory_hit_with_meta_seen_reads_nothing(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _transcript(tmp_path)
    _write_meta(path, "ag1", "meta d", "meta-t")
    first = inventory(store, [("s1", path)])
    assert first["s1"][0]["description"] == "meta d"

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not read")

    monkeypatch.setattr(session_agents, "running_agents", boom)
    monkeypatch.setattr(session_agents, "_read_meta", boom)
    assert inventory(store, [("s1", path)]) == first


def test_inventory_hit_picks_up_late_meta_once(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _transcript(tmp_path)
    first = inventory(store, [("s1", path)])
    assert first["s1"][0]["description"] == "in desc"
    _write_meta(path, "ag1", "late d", "late-t")
    second = inventory(store, [("s1", path)])
    assert second["s1"][0]["description"] == "late d"
    assert second["s1"][0]["agent_type"] == "late-t"

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not re-read")

    monkeypatch.setattr(session_agents, "_read_meta", boom)
    assert inventory(store, [("s1", path)]) == second


def test_inventory_grown_file_is_a_miss(store: Store, tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    assert [a["id"] for a in inventory(store, [("s1", path)])["s1"]] == ["ag1"]
    path.write_text(_LAUNCH + _DONE, encoding="utf-8")
    assert inventory(store, [("s1", path)])["s1"] == running_agents(path) == []


def test_inventory_shrunk_file_is_a_miss(store: Store, tmp_path: Path) -> None:
    path = _transcript(tmp_path, _LAUNCH + _DONE)
    assert inventory(store, [("s1", path)])["s1"] == []
    path.write_text(_LAUNCH, encoding="utf-8")
    assert [a["id"] for a in inventory(store, [("s1", path)])["s1"]] == ["ag1"]


def test_inventory_malformed_omits_and_persists_nothing(store: Store, tmp_path: Path) -> None:
    path = _transcript(tmp_path, '{"type":"user"}\nnot json\n{"type":"user"}\n')
    targets = [("s1", path), ("s2", None), ("s3", tmp_path / "gone.jsonl")]
    assert not inventory(store, targets)
    assert _rows(store) == 0


def test_inventory_oversized_agents_not_persisted(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _transcript(tmp_path)
    monkeypatch.setattr(session_agents, "MAX_ROW_BYTES", 10)
    assert [a["id"] for a in inventory(store, [("s1", path)])["s1"]] == ["ag1"]
    assert _rows(store) == 0


def test_inventory_prune_age_and_row_cap(store: Store) -> None:
    day = 24 * 3600 * 1000
    now = 10 * day
    rows = [(f"/p/{n}", f"s{n}", 1, 1, "[]", "[]", now - 2 * day + n) for n in range(5)]
    rows.append(("/p/fresh", "sf", 1, 1, "[]", "[]", now - 1000))
    store.put_subagent_scans(rows)
    store.prune_subagent_scans({"/p/0"}, now)
    left = {str(r["path"]) for r in store.conn.execute("SELECT path FROM subagent_scan")}
    assert left == {"/p/0", "/p/fresh"}  # live kept despite age; others older than 24 h gone
    many = [(f"/q/{n:04d}", "s", 1, 1, "[]", "[]", now - n) for n in range(230)]
    store.put_subagent_scans(many)
    store.prune_subagent_scans(set(), now)
    assert _rows(store) == 200
    newest = store.get_subagent_scans(["/q/0000", "/q/0229"])
    assert "/q/0000" in newest
    assert "/q/0229" not in newest


def test_inventory_deadline_reports_only_hits(store: Store, tmp_path: Path) -> None:
    warm = _transcript(tmp_path, name="warm")
    cold = _transcript(tmp_path, name="cold")
    assert set(inventory(store, [("warm", warm)])) == {"warm"}
    got = inventory(store, [("warm", warm), ("cold", cold)], deadline_s=0)
    assert set(got) == {"warm"}
    assert _rows(store) == 1


def test_inventory_store_error_omits_everything(tmp_path: Path) -> None:
    path = _transcript(tmp_path)
    broken = Store(tmp_path / "broken.db")
    broken.close()
    assert not inventory(broken, [("s1", path)])
