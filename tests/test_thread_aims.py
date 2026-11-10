"""A thread's AIM history computed on read, relay copies dropped (tp#1989 B3).

Fixture stores only (``tmp_path``); placeholder ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import bridge_cli, bridge_lineage, cli, thread
from command_center.bridge_target import BridgeDeps
from command_center.store import Store

T0 = 1_800_000_000_000
HOUR = 3_600_000


def _row(store: Store, sid: str, aim: str, at: int, by: str | None) -> None:
    store.conn.execute(
        "INSERT INTO aim_history (session_id, aim, score, created_at, set_by) VALUES (?,?,?,?,?)",
        (sid, aim, -1, at, by),
    )
    store.update_fields(sid, aim=aim)
    store.conn.commit()


def _chain(store: Store) -> list[str]:
    """The verified example's shape: root launch + 2 user rows, 4 successors with copies."""
    ids = ["root", "s1", "s2", "s3", "s4"]
    for sid in ids:
        store.ensure(sid, cwd="/repo")
    _row(store, "root", "decide answers", T0, "launch")
    _row(store, "root", "nice tables", T0 + HOUR, "user")
    _row(store, "root", "nice tables ~20:00", T0 + 2 * HOUR, "user")
    for i, (source, target) in enumerate(zip(ids, ids[1:], strict=False), 1):
        store.update_fields(target, handed_off_from=source)
        _row(store, target, "decide answers", T0, "launch")  # carry_first_aim's copy
        _row(store, target, "nice tables ~20:00", T0 + (2 + i) * HOUR, "user")  # carried AIM
        store.record_thread(target, source)
    return ids


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "ccc.db")


def test_the_example_chain_yields_exactly_the_roots_three_entries(store: Store) -> None:
    _chain(store)
    doc = thread.thread_aims(store.conn, "s4")
    assert doc["thread_id"] == "root" and doc["thread_exact"] is True
    assert [(e["aim"], e["by"], e["hop"], e["session_id"]) for e in doc["aims"]] == [
        ("decide answers", "launch", 0, "root"),
        ("nice tables", "user", 0, "root"),
        ("nice tables ~20:00", "user", 0, "root"),
    ]
    assert doc["aims"][0]["at"] == bridge_lineage.iso(T0)
    assert doc["current_aim"] == "nice tables ~20:00"
    assert doc["user_aim"] == "nice tables ~20:00" and doc["truncated"] is False
    assert set(doc["aims"][0]) == {"at", "aim", "aim_full_len", "by", "session_id", "hop"}
    assert thread.facts(store.conn, ["s4"])["s4"].aims_n == 3


def test_a_change_back_to_an_older_wording_is_kept(store: Store) -> None:
    _chain(store)
    _row(store, "s4", "nice tables", T0 + 9 * HOUR, "user")
    aims = thread.thread_aims(store.conn, "s4")["aims"]
    assert [(e["aim"], e["hop"]) for e in aims][-1] == ("nice tables", 4)
    assert len(aims) == 4


def test_tool_and_legacy_rows_keep_their_by(store: Store) -> None:
    store.ensure("a", cwd="/repo")
    _row(store, "a", "legacy", T0, None)
    _row(store, "a", "tooled", T0 + 1, "tool")
    assert [e["by"] for e in thread.thread_aims(store.conn, "a")["aims"]] == [None, "tool"]


def test_a_session_without_rows_gets_one_synthesized_entry(store: Store) -> None:
    store.ensure("a", cwd="/repo")
    store.update_fields("a", aim="pre-history goal", aim_changed_at=T0)
    aims = thread.thread_aims(store.conn, "a")["aims"]
    assert [(e["aim"], e["by"], e["at"]) for e in aims] == [
        ("pre-history goal", None, bridge_lineage.iso(T0))
    ]


def test_more_than_fifty_entries_keep_the_newest(store: Store) -> None:
    store.ensure("a", cwd="/repo")
    for i in range(55):
        _row(store, "a", f"aim {i}", T0 + i, "user")
    doc = thread.thread_aims(store.conn, "a")
    assert len(doc["aims"]) == thread.MAX_ENTRIES and doc["truncated"] is True
    assert doc["aims"][0]["aim"] == "aim 5" and doc["aims"][-1]["aim"] == "aim 54"
    assert thread.facts(store.conn, ["a"])["a"].aims_n == 55


def test_a_multi_line_aim_shows_its_first_line(store: Store) -> None:
    store.ensure("a", cwd="/repo")
    long = "x" * 400
    _row(store, "a", "goal line\nmore detail", T0, "user")
    _row(store, "a", long, T0 + 1, "user")
    aims = thread.thread_aims(store.conn, "a")["aims"]
    assert aims[0]["aim"] == "goal line" and aims[0]["aim_full_len"] == len(
        "goal line\nmore detail"
    )
    assert len(aims[1]["aim"]) == thread.AIM_MAX and aims[1]["aim"].endswith("…")


def test_user_aim_is_the_newest_user_row_not_a_later_tool_row(store: Store) -> None:
    store.ensure("a", cwd="/repo")
    _row(store, "a", "mine", T0, "user")
    _row(store, "a", "the tool's", T0 + 1, "tool")
    doc = thread.thread_aims(store.conn, "a")
    assert doc["user_aim"] == "mine" and doc["current_aim"] == "the tool's"
    assert store.latest_user_aim("a") == ("mine", "user")


def test_a_fork_siblings_rows_are_not_on_the_path(store: Store) -> None:
    _chain(store)
    store.ensure("fork", cwd="/repo")
    store.update_fields("fork", handed_off_from="s1")
    _row(store, "fork", "the other branch", T0 + 20 * HOUR, "user")
    aims = thread.thread_aims(store.conn, "s4")["aims"]
    assert all(e["session_id"] != "fork" for e in aims)
    assert thread.thread_aims(store.conn, "fork")["thread_id"] == "root"


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cli.db"
    with Store(path) as db:
        _chain(db)
    monkeypatch.setattr(
        bridge_cli.bridge_target, "default_deps", lambda: BridgeDeps(store=lambda: Store(path))
    )
    return path


def test_lineage_keeps_every_old_key_and_adds_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _cli(tmp_path, monkeypatch)
    assert cli.main(["lineage", "-s", "s4", "-j"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    with Store(path) as db:
        golden = bridge_lineage.lineage(db, "s4")
    assert {k: v for k, v in data.items() if k != "thread"} == golden
    assert set(data) == {"session_id", "chain", "truncated", "thread"}
    assert data["thread"]["thread_id"] == "root" and len(data["thread"]["aims"]) == 3


def test_the_walk_bound_is_the_lineage_bound() -> None:
    assert thread.MAX_HOPS == bridge_lineage.MAX_HOPS


def test_the_thread_read_writes_nothing(store: Store) -> None:
    _chain(store)
    before = store.conn.total_changes
    thread.thread_aims(store.conn, "s4")
    thread.thread_aims(store.conn, "unknown")
    thread.facts(store.conn, ["s4", "unknown"])
    bridge_lineage.lineage(store, "s4")
    assert store.conn.total_changes == before


def test_aim_history_thread_text_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    with Store() as db:
        _chain(db)
    assert cli.main(["aim-history", "-s", "s4", "-t"]) == 0
    out = capsys.readouterr().out
    assert "thread root" in out and "3 entries" in out and "hop 0" in out
    assert "launch" in out and "← current" in out
    assert cli.main(["aim-history", "-s", "s4", "-j"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["thread_id"] == "root" and len(doc["aims"]) == 3
    assert cli.main(["aim-history", "--session", "s4"]) == 0  # the per-session view stays
    assert "revision" in capsys.readouterr().out
