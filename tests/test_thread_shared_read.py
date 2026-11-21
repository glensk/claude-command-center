"""The thread reader reads the shared store when it is on (tp#2372 F3b7).

``thread.py`` and ``Store.thread_of`` take a session's recorded thread id from the shared
store's ``ccc_sessions`` when ``shared_store_path`` is set, and from ``state.db``'s
``sessions.thread_id`` otherwise (and for ids without a shared row). With the key unset
nothing touches any shared file; the standalone reader works on a fresh and an upgraded
``state.db``. Fixture stores only (``tmp_path``); placeholder ids.
"""

from __future__ import annotations

# pylint: disable=redefined-outer-name  # pytest fixtures
import sqlite3
from pathlib import Path

import pytest

from command_center import config, shared_store, thread
from command_center.store import Store

SHARED_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE ccc_threads(thread_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  root_session TEXT NOT NULL, forked_from TEXT,
  current_generation INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE ccc_sessions(session_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES ccc_threads(thread_id), generation INTEGER NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER, end_reason TEXT, cwd TEXT,
  last_beat INTEGER, incarnation INTEGER NOT NULL DEFAULT 0,
  UNIQUE (thread_id, generation));
INSERT INTO meta(key, value) VALUES ('ccc_shared_schema', '1');
INSERT INTO meta(key, value) VALUES ('holder_authority_epoch', '1');
"""


def _chain(store: Store, *sids: str) -> None:
    """``sids[0]`` → ``sids[1]`` → … as an accepted relay chain leaves ``state.db``."""
    for sid in sids:
        store.ensure(sid, cwd="/repo")
    for source, target in zip(sids, sids[1:], strict=False):
        store.update_fields(target, handed_off_from=source)
        store.record_thread(target, source)


def _shared_file(path: Path, rows: dict[str, tuple[str, int]]) -> Path:
    """A fixture shared file with one ``ccc_sessions`` row per ``sid → (thread, gen)``."""
    conn = sqlite3.connect(path)
    conn.executescript(SHARED_DDL)
    for thread_id in dict.fromkeys(t for t, _gen in rows.values()):
        conn.execute(
            "INSERT INTO ccc_threads(thread_id, kind, root_session, created_at) "
            "VALUES (?, 'ccc', ?, 1)",
            (thread_id, thread_id),
        )
    for sid, (thread_id, gen) in rows.items():
        conn.execute(
            "INSERT INTO ccc_sessions(session_id, thread_id, generation, started_at) "
            "VALUES (?, ?, ?, 1)",
            (sid, thread_id, gen),
        )
    conn.commit()
    conn.close()
    return path


def _turn_on(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    cfg = config.Config(shared_store_path=str(path))
    monkeypatch.setattr(config, "load_config", lambda: cfg)


def _tables(store: Store) -> set[str]:
    rows = store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(r[0]) for r in rows.fetchall()}


@pytest.fixture()
def off(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The key unset; any shared read is recorded (and must not happen)."""
    monkeypatch.setattr(config, "load_config", config.Config)
    calls: list[object] = []

    def _spy(*args: object, **_kw: object) -> dict[str, str]:
        calls.append(args)
        return {}

    monkeypatch.setattr(shared_store, "read_thread_ids", _spy)
    return calls


# -- the key unset: state.db only, no shared file touched ----------------------------


def test_off_a_fresh_state_db_reads_its_column(tmp_path: Path, off: list[object]) -> None:
    with Store(tmp_path / "ccc.db") as store:
        _chain(store, "root", "s1", "s2")
        assert store.thread_of("s2") == "root"
        rows = thread._session_rows(store.conn, ["s1", "s2"])  # pylint: disable=protected-access
        assert {sid: r["thread_id"] for sid, r in rows.items()} == {"s1": "root", "s2": "root"}
        facts = thread.facts(store.conn, ["root", "s1", "s2"])
        assert {sid: f.thread_id for sid, f in facts.items()} == dict.fromkeys(
            ("root", "s1", "s2"), "root"
        )
        assert thread.thread_aims(store.conn, "s2")["thread_exact"] is True
    assert off == []
    assert not list(tmp_path.glob("*shared*"))


def test_off_an_upgraded_state_db_gains_the_column_and_reads_it(
    tmp_path: Path, off: list[object]
) -> None:
    path = tmp_path / "old.db"
    with Store(path) as store:
        _chain(store, "root", "s1")
        store.conn.execute("ALTER TABLE sessions DROP COLUMN thread_id")
        store.conn.commit()
    with Store(path) as store:  # the migration re-adds the column (NULL on old rows)
        assert thread.walk(store.conn, "s1").thread_id == "root"  # the walk still answers
        store.ensure("s2", cwd="/repo")
        store.update_fields("s2", handed_off_from="s1")
        assert store.record_thread("s2", "s1") is True
        rows = thread._session_rows(store.conn, ["s2"])  # pylint: disable=protected-access
        assert rows["s2"]["thread_id"] == "root"
        assert store.thread_of("s2") == "root"
    assert off == []


def test_off_shared_thread_ids_is_empty_without_any_read(off: list[object]) -> None:
    assert not thread.shared_thread_ids(["root", "s1"])
    assert off == []


# -- the key set: ccc_sessions first, state.db for ids without a shared row ----------


def test_on_store_thread_of_reads_ccc_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "ccc.db") as store:
        _chain(store, "root", "s1", "s2")
        store.ensure("other", cwd="/repo")
        shared = _shared_file(tmp_path / "shared.sqlite3", {"s1": ("T", 1), "root": ("T", 0)})
        _turn_on(monkeypatch, shared)
        assert store.thread_of("s1") == "T"  # the shared row, not state.db's "root"
        assert store.thread_of("s2") == "T"  # no shared row: the nearest shared ancestor
        assert store.thread_of("other") == "other"  # no shared row anywhere: state.db rule
        path = thread.walk(store.conn, "s2")
        assert (path.thread_id, path.exact, path.path) == ("T", True, ["root", "s1", "s2"])


def test_on_ids_without_a_shared_row_keep_the_state_db_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "ccc.db") as store:
        _chain(store, "root", "s1")
        _turn_on(monkeypatch, _shared_file(tmp_path / "shared.sqlite3", {"x": ("X", 0)}))
        store.conn.execute("DELETE FROM sessions WHERE session_id = 'root'")
        store.conn.commit()
        path = thread.walk(store.conn, "s1")
        assert (path.thread_id, path.exact) == ("root", True)  # the column still vouches


def test_on_the_batched_facts_read_the_shared_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "ccc.db") as store:
        _chain(store, "a0", "a1")
        _chain(store, "b0", "b1")
        rows = {"a0": ("A", 0), "a1": ("A", 1), "b1": ("B", 1)}
        _turn_on(monkeypatch, _shared_file(tmp_path / "shared.sqlite3", rows))
        facts = thread.facts(store.conn, ["a0", "a1", "b0", "b1"])
        got = {sid: f.thread_id for sid, f in facts.items()}
        assert got == {"a0": "A", "a1": "A", "b0": "b0", "b1": "B"}
        assert thread.thread_aims(store.conn, "b1")["thread_id"] == "B"


def test_on_the_read_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = _shared_file(tmp_path / "shared.sqlite3", {"s1": ("T", 1)})
    before = shared.read_bytes()
    with Store(tmp_path / "ccc.db") as store:
        _chain(store, "root", "s1")
        tables = _tables(store)
        _turn_on(monkeypatch, shared)
        assert store.thread_of("s1") == "T"
        assert _tables(store) == tables  # no ccc-private table created in state.db
    assert shared.read_bytes() == before
    assert not list(tmp_path.glob("shared.sqlite3-*"))  # no journal or WAL left


@pytest.mark.parametrize("kind", ["missing", "no_table", "garbage"])
def test_on_an_unreadable_store_falls_back_to_state_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    shared = tmp_path / "shared.sqlite3"
    if kind == "no_table":
        conn = sqlite3.connect(shared)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
    elif kind == "garbage":
        shared.write_bytes(b"not a database" * 100)
    monkeypatch.setattr(shared_store, "log_line", lambda _text: None)
    with Store(tmp_path / "ccc.db") as store:
        _chain(store, "root", "s1")
        _turn_on(monkeypatch, shared)
        assert store.thread_of("s1") == "root"
    assert shared.exists() is (kind != "missing")  # ccc never creates the shared file


def test_read_thread_ids_chunks_and_skips_unknown_ids(tmp_path: Path) -> None:
    rows = {f"s{i}": ("T", i) for i in range(shared_store.READ_CHUNK + 7)}
    shared = _shared_file(tmp_path / "shared.sqlite3", rows)
    asked = [*rows, "unknown", ""]
    got = shared_store.read_thread_ids(shared, asked)
    assert got == {sid: "T" for sid in rows}
    assert not shared_store.read_thread_ids(shared, [])
