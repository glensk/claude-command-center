"""``ccc thread-seed`` (tp#2372 FB9): state.db's threads into the shared tables."""

from __future__ import annotations

# pylint: disable=redefined-outer-name  # pytest fixtures
import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_shared_store import SHARED_DDL

from command_center import cli
from command_center.store import Store

A, B, C = (f"{n}aaaaaaa-0000-4000-8000-000000000000" for n in "abc")
F1, F2, F3 = (f"{n}bbbbbbb-0000-4000-8000-000000000000" for n in "def")
LONE = "1ccccccc-0000-4000-8000-000000000000"


@pytest.fixture()
def store_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME and an empty fixture shared file (the key stays unset)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TP_TIMING_LOG", str(tmp_path / "timing.jsonl"))
    path = tmp_path / "shared.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SHARED_DDL)
    conn.close()
    return path


def _row(store: Store, sid: str, created: int, parent: str = "", at: int = 0) -> None:
    store.ensure(sid, cwd="/repo")
    store.conn.execute(
        "UPDATE sessions SET created_at = ?, handed_off_from = ?, handed_off_at = ? "
        "WHERE session_id = ?",
        (created, parent or None, at or None, sid),
    )
    store.conn.commit()


def _transfer(store: Store, tid: str, src: str, tgt: str, accepted_at: int) -> None:
    store.conn.execute(
        "INSERT INTO transfers (transfer_id, source_session, target_session, state, "
        "created_at, accepted_at) VALUES (?, ?, ?, 'finalized', ?, ?)",
        (tid, src, tgt, accepted_at - 1, accepted_at),
    )
    store.conn.commit()


def _fixture() -> None:
    """A clean chain A → B → C, a fork F1 → {F2, F3} (F1 quarantine) and a lone root."""
    with Store() as store:
        _row(store, A, 100)
        _row(store, B, 200, A, 210)
        _row(store, C, 300, B, 310)
        _transfer(store, "t1", A, B, 205)
        _transfer(store, "t2", B, C, 305)
        _row(store, F1, 400)
        _row(store, F2, 500, F1, 510)
        _row(store, F3, 600, F1, 610)
        _transfer(store, "t3", F1, F2, 505)
        _transfer(store, "t4", F1, F3, 605)
        _row(store, LONE, 700)


def _seed(capsys: pytest.CaptureFixture[str], path: Path, *extra: str) -> tuple[int, Any]:
    capsys.readouterr()
    args: argparse.Namespace = cli.build_parser().parse_args(
        ["thread-seed", "-S", str(path), "-J", *extra]
    )
    rc: int = args.func(args)
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def _dump(path: Path) -> list[Any]:
    conn = sqlite3.connect(path)
    try:
        return [
            list(conn.execute(f"SELECT * FROM {t} ORDER BY 1"))
            for t in ("ccc_threads", "ccc_sessions", "ccc_thread_successions")
        ]
    finally:
        conn.close()


def _members(path: Path) -> dict[str, tuple[str, int, int | None, str | None]]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT session_id, thread_id, generation, ended_at, end_reason FROM ccc_sessions"
        )
        return {str(r[0]): (str(r[1]), int(r[2]), r[3], r[4]) for r in rows}
    finally:
        conn.close()


def test_seed_copies_the_chain_skips_quarantine_and_a_rerun_writes_nothing(
    store_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fixture()
    rc, dry = _seed(capsys, store_file, "-n")
    assert rc == 0 and dry["dry_run"] and dry["written"]["sessions"] == 4
    assert _dump(store_file) == [[], [], []]  # the dry run writes nothing
    rc, doc = _seed(capsys, store_file)
    assert rc == 0 and doc["written"] == {"threads": 2, "advanced": 0, "sessions": 4}
    assert doc["conflicts"] == []
    assert [q["root"] for q in doc["quarantined"]] == [F1]
    assert doc["quarantined"][0]["reasons"] == ["fork"]
    got = _members(store_file)
    assert got[A] == (A, 0, 210, "handed_off")
    assert got[B] == (A, 1, 310, "handed_off")
    assert got[C] == (A, 2, None, None)
    assert got[LONE] == (LONE, 0, None, None)
    assert F1 not in got and F2 not in got
    conn = sqlite3.connect(store_file)
    assert conn.execute(
        "SELECT current_generation FROM ccc_threads WHERE thread_id = ?", (A,)
    ).fetchone() == (2,)
    conn.close()
    before = _dump(store_file)
    timing = (tmp_path / "timing.jsonl").read_text().count("\n")
    rc, again = _seed(capsys, store_file)
    assert rc == 0 and again["written"] == {"threads": 0, "advanced": 0, "sessions": 0}
    assert again["already"] == 4
    assert _dump(store_file) == before
    assert (tmp_path / "timing.jsonl").read_text().count("\n") == timing  # no write tx


def test_seed_inserts_missing_rows_advances_and_lists_every_conflict(
    store_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fixture()
    conn = sqlite3.connect(store_file)
    # A hook rooted A (generation 0) before the seed: B and C are inserted, gen advanced.
    conn.execute("INSERT INTO ccc_threads VALUES (?, 'ccc', ?, NULL, 0, 1)", (A, A))
    conn.execute(
        "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at) "
        "VALUES (?, ?, 0, 1)",
        (A, A),
    )
    # LONE already sits in another thread: a lineage conflict, never overwritten.
    conn.execute("INSERT INTO ccc_threads VALUES ('other', 'ccc', 'other', NULL, 1, 1)")
    conn.execute(
        "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at) "
        "VALUES ('other', 'other', 0, 1)"
    )
    conn.execute(
        "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at) "
        "VALUES (?, 'other', 1, 1)",
        (LONE,),
    )
    conn.commit()
    conn.close()
    rc, doc = _seed(capsys, store_file)
    assert rc == 0 and doc["written"] == {"threads": 0, "advanced": 1, "sessions": 2}
    assert doc["already"] == 1
    assert doc["conflicts"] == [
        {
            "root": LONE,
            "reason": "lineage",
            "session": LONE,
            "state_db": [LONE, 0],
            "shared": ["other", 1],
        }
    ]
    got = _members(store_file)
    assert got[B][:2] == (A, 1) and got[C][:2] == (A, 2)
    assert got[LONE][:2] == ("other", 1)
    rc, again = _seed(capsys, store_file)  # the conflict is listed again, nothing written
    assert again["written"] == {"threads": 0, "advanced": 0, "sessions": 0}
    assert len(again["conflicts"]) == 1


def test_seed_refuses_without_a_store_and_under_maintenance(
    store_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fixture()
    args = cli.build_parser().parse_args(["thread-seed"])
    assert args.func(args) == 2  # no -S and no configured key
    conn = sqlite3.connect(store_file)
    conn.execute("UPDATE meta SET value = 'maintenance' WHERE key = 'backend'")
    conn.commit()
    conn.close()
    rc, doc = _seed(capsys, store_file)
    assert rc == 1 and doc is None
    assert _dump(store_file) == [[], [], []]
