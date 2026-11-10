"""``ccc restamp-aims [-a] [-r LOG]`` — backfill relay successors' NULL stamps (tp#1991 B4).

Fixture stores only: each test builds ``state.db`` at ``config.db_path()`` (pinned under
``tmp_path`` by conftest), closes it, then runs the command. Placeholder ids.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from command_center import bridge_aims, cli, config, restamp
from command_center.store import Store

T0 = 1_700_000_000_000


def _db() -> Path:
    return config.db_path()


def _session(
    store: Store,
    sid: str,
    rows: tuple[tuple[str, str | None], ...] = (),
    *,
    source: str | None = None,
    at: int = T0,
) -> None:
    """A session (relay successor of *source*) with *rows* ``(aim, set_by)``, oldest first."""
    store.ensure(sid, cwd="/repo")
    store.conn.execute(
        "UPDATE sessions SET handed_off_from = ? WHERE session_id = ?", (source, sid)
    )
    store.conn.executemany(
        "INSERT INTO aim_history (session_id, aim, created_at, set_by) VALUES (?, ?, ?, ?)",
        [(sid, text, at + i, by) for i, (text, by) in enumerate(rows)],
    )
    store.conn.commit()


def _build(*sessions: tuple[Any, ...]) -> Path:
    """A fresh store holding *sessions* ``(sid, rows, source)``."""
    store = Store(_db())
    try:
        for sid, rows, source in sessions:
            _session(store, sid, rows, source=source)
    finally:
        store.close()
    return _db()


def _stamps(db: Path) -> dict[str, list[tuple[str, str | None]]]:
    conn = sqlite3.connect(db)
    try:
        out: dict[str, list[tuple[str, str | None]]] = {}
        for sid, aim, by in conn.execute(
            "SELECT session_id, aim, set_by FROM aim_history ORDER BY session_id, created_at, id"
        ):
            out.setdefault(sid, []).append((aim, by))
        return out
    finally:
        conn.close()


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    rc = cli.main(["restamp-aims", *argv])
    return rc, capsys.readouterr().out


#: A root with a pre-history seed, a 2-hop chain off it and an unrelated session.
CHAIN = (
    ("sess-root", (("seed aim", None), ("launch aim", "launch")), None),
    ("sess-hop1", (("launch aim", None), ("own words", None)), "sess-root"),
    ("sess-hop2", (("launch aim", None),), "sess-hop1"),
    ("sess-solo", (("solo seed", None),), None),
)


def test_dry_run_writes_nothing_and_prints_counts(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(*CHAIN)
    before, mtime = db.read_bytes(), db.stat().st_mtime_ns
    rc, out = _run(capsys)
    assert rc == 0
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime
    assert "dry run" in out
    assert "2 rows" in out
    assert "NULL   → launch      2" in out
    assert not restamp.backup_dir(db).exists()
    assert _stamps(db)["sess-hop2"] == [("launch aim", None)]


def test_apply_writes_private_backup_first(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(*CHAIN)
    rc, out = _run(capsys, "-a")
    assert rc == 0
    backups = sorted(restamp.backup_dir(db).glob("state-pre-restamp-*.db"))
    assert len(backups) == 1
    backup = backups[0]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert str(backup) in out
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("SELECT COUNT(*) FROM aim_history").fetchone()[0] == 6
        # taken before the write: the backup still holds the NULL stamps
        assert (
            conn.execute("SELECT COUNT(*) FROM aim_history WHERE set_by IS NULL").fetchone()[0] == 5
        )
    finally:
        conn.close()
    log = backup.with_suffix(".json")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert {c["new"] for c in json.loads(log.read_text())["changes"]} == {"launch"}
    assert "applied: 2 rows" in out


@pytest.mark.parametrize("reverse", [False, True], ids=["root-first", "leaf-first"])
def test_three_hop_launch_root_stamps_both_successors(
    capsys: pytest.CaptureFixture[str], reverse: bool
) -> None:
    sessions = CHAIN[:3]
    db = _build(*(reversed(sessions) if reverse else sessions))
    rc, _out = _run(capsys, "-a")
    assert rc == 0
    got = _stamps(db)
    assert got["sess-hop1"] == [("launch aim", "launch"), ("own words", None)]
    assert got["sess-hop2"] == [("launch aim", "launch")]


def test_null_root_gives_relay(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(
        ("sess-root", (("legacy aim", None),), None),
        ("sess-hop1", (("legacy aim", None),), "sess-root"),
        ("sess-hop2", (("legacy aim: tp#12", None),), "sess-hop1"),  # words match
    )
    rc, out = _run(capsys, "-a")
    assert rc == 0
    got = _stamps(db)
    assert got["sess-root"] == [("legacy aim", None)]
    assert got["sess-hop1"] == [("legacy aim", "relay")]
    assert got["sess-hop2"] == [("legacy aim: tp#12", "relay")]
    assert "NULL   → relay       2" in out


def test_stamped_middle_hop_beats_relay() -> None:
    """A NULL root, a hand-set middle row: the leaf copies ``user``, never ``relay``."""
    db = _build(
        ("sess-root", (("aim one", None),), None),
        ("sess-hop1", (("aim one", None), ("aim two", "user")), "sess-root"),
        ("sess-hop2", (("aim two", None),), "sess-hop1"),
    )
    conn = bridge_aims.connect_ro(db)
    try:
        news = {(c.session_id, c.new) for c in restamp.plan(conn)}
    finally:
        conn.close()
    assert news == {("sess-hop1", "relay"), ("sess-hop2", "user")}


def test_unmatched_successor_row_untouched(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(*CHAIN)
    _run(capsys, "-a")
    assert ("own words", None) in _stamps(db)["sess-hop1"]


def test_root_and_seed_rows_untouched(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(*CHAIN)
    _run(capsys, "-a")
    got = _stamps(db)
    assert got["sess-root"] == [("seed aim", None), ("launch aim", "launch")]
    assert got["sess-solo"] == [("solo seed", None)]


def test_second_apply_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(*CHAIN)
    _run(capsys, "-a")
    after = _stamps(db)
    rc, out = _run(capsys, "-a")
    assert rc == 0
    assert "applied: 0 rows" in out
    assert _stamps(db) == after
    assert len(list(restamp.backup_dir(db).glob("*.db"))) == 1


def test_failure_mid_way_rolls_back(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build(*CHAIN)
    real = restamp._update  # pylint: disable=protected-access
    calls: list[int] = []

    def flaky(conn: sqlite3.Connection, change: restamp.Change) -> None:
        calls.append(change.row_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error (injected)")
        real(conn, change)

    monkeypatch.setattr(restamp, "_update", flaky)
    rc = cli.main(["restamp-aims", "-a"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "nothing written" in err
    assert len(calls) == 2
    got = _stamps(db)
    assert got["sess-hop1"][0] == ("launch aim", None)
    assert got["sess-hop2"] == [("launch aim", None)]


def test_concurrent_writer_lock_fails_closed(tmp_path: Path) -> None:
    db = _build(*CHAIN)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            restamp.apply(db, folder=tmp_path / "bk", busy_timeout=0.05)
        holder.execute("ROLLBACK")
    finally:
        holder.close()
    assert _stamps(db)["sess-hop2"] == [("launch aim", None)]


def test_revert_restores_only_still_logged_rows(capsys: pytest.CaptureFixture[str]) -> None:
    db = _build(*CHAIN)
    done = restamp.apply(db)
    assert done.log is not None
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE aim_history SET set_by = 'user' WHERE session_id = 'sess-hop2'"
    )  # changed since the apply: the revert leaves it
    conn.commit()
    conn.close()
    rc, out = _run(capsys, "-r", str(done.log))
    assert rc == 0
    assert "would revert" in out
    assert "1 row" in out
    assert _stamps(db)["sess-hop1"][0] == ("launch aim", "launch")
    rc, out = _run(capsys, "-r", str(done.log), "-a")
    assert rc == 0
    assert "reverted: 1 row" in out
    got = _stamps(db)
    assert got["sess-hop1"][0] == ("launch aim", None)
    assert got["sess-hop2"] == [("launch aim", "user")]


@pytest.mark.parametrize(
    "text",
    ["continue the work handed over by session 0a1b2c3d", "handoff accepted by session 9f8e7d6c"],
)
def test_ccc_templates_stamped_tool(capsys: pytest.CaptureFixture[str], text: str) -> None:
    db = _build(
        ("sess-root", (("root aim", "user"),), None),
        ("sess-hop1", ((text, None),), "sess-root"),
    )
    _run(capsys, "-a")
    assert _stamps(db)["sess-hop1"] == [(text, "tool")]


def test_rewrite_tool_stamp_never_copied(capsys: pytest.CaptureFixture[str]) -> None:
    """A source ``tool`` row that only stripped a tp run is looked through."""
    db = _build(
        ("sess-root", (("fix the bar: tp#7", None), ("fix the bar", "tool")), None),
        ("sess-hop1", (("fix the bar", None),), "sess-root"),
    )
    _run(capsys, "-a")
    assert _stamps(db)["sess-hop1"] == [("fix the bar", "relay")]


def test_missing_db_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert not _db().exists()
    assert cli.main(["restamp-aims"]) == 1
    assert "no state.db" in capsys.readouterr().err
    assert not _db().exists()


def test_help_lists_short_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["restamp-aims", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "-a, --apply" in out
    assert "-r, --revert LOG" in out
