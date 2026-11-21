"""ccc's thread lifecycle rows in the shared store (tp#2372 F3b).

A fixture shared file carries the owner's v16 shape (``meta``, ``holdings`` and the three
``ccc_*`` tables). Off by default: with ``shared_store_path`` unset nothing opens any
shared file. On: roots, re-parenting, fork siblings, staff consent and hand-back,
``/clear`` joins, resume incarnations, the throttled beat and the write guards.
"""

from __future__ import annotations

# pylint: disable=redefined-outer-name  # pytest fixtures
import argparse
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from command_center import cli, config, hooks, shared_lifecycle, shared_store
from command_center.models import Status
from command_center.store import Store
from command_center.thread_join_store import ACCEPTED, CANCELLED, RESERVED

SHARED_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE holdings(id INTEGER PRIMARY KEY, tp_id INTEGER NOT NULL, session TEXT,
  claim_at TEXT, ended TEXT, ended_reason TEXT, drive_at TEXT, via TEXT,
  close_authority TEXT, plan_path TEXT, plan_row TEXT, handed_from TEXT);
CREATE TABLE ccc_threads(thread_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('ccc', 'headless')), root_session TEXT NOT NULL,
  forked_from TEXT, current_generation INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);
CREATE TABLE ccc_sessions(session_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES ccc_threads(thread_id), generation INTEGER NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER,
  end_reason TEXT CHECK (end_reason IN ('handed_off', 'closed', 'done')), cwd TEXT,
  last_beat INTEGER, incarnation INTEGER NOT NULL DEFAULT 0,
  UNIQUE (thread_id, generation));
CREATE TABLE ccc_thread_successions(op_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES ccc_threads(thread_id),
  source_generation INTEGER NOT NULL, source_session TEXT NOT NULL,
  target_session TEXT NOT NULL,
  join_kind TEXT NOT NULL CHECK (join_kind IN
    ('continue', 'park_start', 'staff_accept', 'clear', 'headless')),
  state TEXT NOT NULL CHECK (state IN
    ('reserved', 'committing', 'accepted', 'cancelled', 'failed')),
  inherit_close INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, settled_at INTEGER);
CREATE UNIQUE INDEX ccc_thread_successions_pending
  ON ccc_thread_successions(thread_id, source_generation)
  WHERE state IN ('reserved', 'committing');
INSERT INTO meta(key, value) VALUES ('ccc_shared_schema', '1');
INSERT INTO meta(key, value) VALUES ('holder_authority', 'session');
INSERT INTO meta(key, value) VALUES ('holder_authority_epoch', '1');
INSERT INTO meta(key, value) VALUES ('backend', 'sqlite');
"""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME; the shared store stays OFF unless a test turns it on."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_AIM", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.setenv("TP_TIMING_LOG", str(tmp_path / "timing.jsonl"))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture()
def shared(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture shared file, with ``shared_store_path`` set to it."""
    del home
    path = tmp_path / "shared.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SHARED_DDL)
    conn.close()
    cfg = config.Config(shared_store_path=str(path))
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    return path


def _q(path: Path, sql: str, *params: object) -> list[Any]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _member(path: Path, sid: str) -> Any:
    rows = _q(
        path,
        "SELECT thread_id, generation, ended_at, end_reason, incarnation FROM ccc_sessions "
        "WHERE session_id = ?",
        sid,
    )
    return rows[0] if rows else None


def _current(path: Path, thread_id: str) -> int:
    rows = _q(path, "SELECT current_generation FROM ccc_threads WHERE thread_id = ?", thread_id)
    return int(rows[0][0])


def _sid() -> str:
    return str(uuid.uuid4())


def _start(sid: str, source: str = "startup") -> None:
    with Store() as store:
        if store.get(sid) is not None:
            store.clear_draft(sid)
    hooks.handle_session_start({"session_id": sid, "cwd": "/repo", "source": source})


def _end(sid: str, reason: str = "other") -> None:
    hooks.handle_session_end({"session_id": sid, "cwd": "/repo", "reason": reason})


def _open(path: Path) -> shared_store.SharedStore:
    store = Store()
    return shared_store.SharedStore(path, private=store.conn)


def _verb(*argv: str) -> int:
    args: argparse.Namespace = cli.build_parser().parse_args(list(argv))
    rc: int = args.func(args)
    return rc


def _register(repo: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, object]:
    capsys.readouterr()
    base = ["new-job", "-a", "relay successor", "-c", str(repo), "-J"]
    assert cli.cmd_new_job(cli.build_parser().parse_args([*base, *argv])) == 0
    doc: dict[str, object] = json.loads(capsys.readouterr().out)
    return doc


def _source(repo: Path) -> str:
    sid = _sid()
    with Store() as store:
        store.ensure(sid, cwd=str(repo))
        store.update_fields(sid, status=Status.WORKING.value)
    _start(sid)
    return sid


# --------------------------------------------------------------------------- #
# off by default
# --------------------------------------------------------------------------- #
def test_off_by_default_touches_no_shared_file(home: Path, tmp_path: Path) -> None:
    assert config.Config().shared_store_path == ""
    assert shared_store.configured_path(config.Config()) is None
    sid = _sid()
    _start(sid)
    hooks.handle_stop({"session_id": sid, "cwd": "/repo"})
    _end(sid)
    with Store() as store:
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master")}
        assert shared_store.open_shared(store.conn) is None
    assert not tables & {"shared_store_pins", "shared_incarnations", "shared_clear_marks"}
    assert not (tmp_path / "shared.sqlite3").exists()
    assert not (home.parent / "timing.jsonl").exists()


def test_a_configured_but_missing_file_is_never_created(home: Path, tmp_path: Path) -> None:
    del home
    missing = tmp_path / "nope.sqlite3"
    with Store() as store:
        assert shared_store.open_shared(store.conn, path=missing) is None
    assert not missing.exists()


# --------------------------------------------------------------------------- #
# SessionStart per source, SessionEnd per incarnation
# --------------------------------------------------------------------------- #
def test_startup_writes_a_root_and_a_timing_row(shared: Path, tmp_path: Path) -> None:
    sid = _sid()
    _start(sid)
    assert _member(shared, sid) == (sid, 0, None, None, 0)
    _start(sid, "startup")  # a re-run writes nothing new
    assert _q(shared, "SELECT COUNT(*) FROM ccc_threads") == [(1,)]
    rows = [json.loads(x) for x in (tmp_path / "timing.jsonl").read_text().splitlines()]
    assert rows and rows[0]["kind"] == "ccc_write" and rows[0]["hold_ms"] >= 0


def test_resume_bumps_the_incarnation_and_an_older_end_is_ignored(
    shared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _sid()
    monkeypatch.setattr(shared_store, "process_key", lambda: "ppid:100")
    _start(sid)
    _end(sid)
    assert _member(shared, sid)[2:4] != (None, None)
    monkeypatch.setattr(shared_store, "process_key", lambda: "ppid:200")
    _start(sid, "resume")
    assert _member(shared, sid)[2:] == (None, None, 1)
    monkeypatch.setattr(shared_store, "process_key", lambda: "ppid:100")
    _end(sid)  # the first process's delayed SessionEnd
    assert _member(shared, sid)[2] is None
    monkeypatch.setattr(shared_store, "process_key", lambda: "ppid:200")
    _end(sid)
    _end(sid)  # once per incarnation
    member = _member(shared, sid)
    assert member is not None and member[2] is not None and member[3] == "closed"


def test_fork_and_unknown_resume_start_their_own_thread(shared: Path) -> None:
    for source in ("fork", "resume"):
        sid = _sid()
        _start(sid, source)
        assert _member(shared, sid) == (sid, 0, None, None, 0)


def test_compact_keeps_the_id_and_writes_nothing(shared: Path) -> None:
    sid = _sid()
    _start(sid)
    before = _q(shared, "SELECT * FROM ccc_sessions")
    _start(sid, "compact")
    assert _q(shared, "SELECT * FROM ccc_sessions") == before


def test_clear_with_a_new_id_joins_as_the_next_generation(
    shared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared_store, "process_key", lambda: "ppid:300")
    old, new = _sid(), _sid()
    _start(old)
    _end(old, "clear")
    assert _member(shared, old)[2] is None  # not closed: handed over
    _start(new, "clear")
    assert _member(shared, new)[:2] == (old, 1)
    assert _member(shared, old)[3] == "handed_off"
    assert _current(shared, old) == 1
    assert _q(shared, "SELECT join_kind, state FROM ccc_thread_successions") == [
        ("clear", "accepted")
    ]


def test_the_session_end_of_a_done_session_says_done(shared: Path) -> None:
    sid = _sid()
    _start(sid)
    with Store() as store:
        store.update_fields(sid, done=True)
    _end(sid)
    assert _member(shared, sid)[3] == "done"


# --------------------------------------------------------------------------- #
# successions (Design 3)
# --------------------------------------------------------------------------- #
def test_park_relay_reserves_before_start_and_joins_at_session_start(
    shared: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    new = str(_register(home, capsys, "-F", src)["session_id"])
    assert _q(shared, "SELECT state, join_kind FROM ccc_thread_successions") == [
        ("reserved", "park_start")
    ]
    _start(new)
    assert _member(shared, new)[:2] == (src, 1)
    assert _current(shared, src) == 1
    _start(new)  # a duplicate start is a no-op
    assert _q(shared, "SELECT COUNT(*) FROM ccc_sessions") == [(2,)]


def test_a_rooted_successor_is_reparented_and_its_stray_thread_dropped(shared: Path) -> None:
    src, new = _sid(), _sid()
    _start(src)
    with _open(shared) as s:
        res = s.reserve(src, new, "continue", 1)
        assert res is not None and res.op is not None
        s.start_root(new, 2)  # pending target: no root
        assert s.session_row(new) is None
        s.settle(res.op.op_id, "cancelled", 3)
        s.start_root(new, 4)  # SessionStart before the (new) accept
        res2 = s.reserve(src, new, "continue", 5)
        assert res2 is not None and res2.op is not None
        assert s.commit(res2.op.op_id, 6) == "accepted"
        assert s.commit(res2.op.op_id, 7) == "already"  # a duplicate accept is a no-op
    assert _member(shared, new)[:2] == (src, 1)
    assert _q(shared, "SELECT COUNT(*) FROM ccc_threads WHERE thread_id = ?", new) == [(0,)]


def test_reparenting_is_refused_when_the_stray_root_holds_an_obligation(shared: Path) -> None:
    src, new = _sid(), _sid()
    _start(src)
    with _open(shared) as s:
        s.start_root(new, 1)
        s.conn.execute(
            "INSERT INTO holdings (tp_id, session, claim_at) VALUES (7, ?, '2026-11-21')", (new,)
        )
        res = s.reserve(src, new, "continue", 2)
        assert res is not None and res.op is not None
        assert s.commit(res.op.op_id, 3) == "quarantined"
        assert s.op(res.op.op_id).state == "failed"  # type: ignore[union-attr]
    assert _current(shared, src) == 0  # the source stays current
    assert _member(shared, new)[:2] == (new, 0)


def test_a_fork_sibling_loses_the_reservation_and_learns_the_winner(shared: Path) -> None:
    src, a, b = _sid(), _sid(), _sid()
    _start(src)
    with _open(shared) as s:
        first = s.reserve(src, a, "continue", 1)
        second = s.reserve(src, b, "continue", 2)
        assert first is not None and first.op is not None and second is not None
        assert second.status == "lost" and second.winner is not None
        assert second.winner.target_session == a and second.winner.op_id == first.op.op_id
        assert s.commit(first.op.op_id, 3) == "accepted"
        third = s.reserve(src, b, "continue", 4)  # the source generation is no longer current
        assert third is not None and third.status == "lost"
        assert third.winner is not None and third.winner.target_session == a


def test_a_lost_cas_leaves_the_source_current_and_a_replay_never_reactivates(
    shared: Path,
) -> None:
    a, b, c = _sid(), _sid(), _sid()
    _start(a)
    with _open(shared) as s:
        ab = s.reserve(a, b, "continue", 1)
        assert ab is not None and ab.op is not None
        assert s.commit(ab.op.op_id, 2) == "accepted"
        bc = s.reserve(b, c, "continue", 3)
        assert bc is not None and bc.op is not None
        assert s.commit(bc.op.op_id, 4) == "accepted"
        assert s.commit(ab.op.op_id, 5) == "already"  # replayed A→B after B→C
        # a lost CAS: the generation moved under a reserved op
        d = _sid()
        cd = s.reserve(c, d, "continue", 6)
        assert cd is not None and cd.op is not None
        s.conn.execute("UPDATE ccc_threads SET current_generation = 9 WHERE thread_id = ?", (a,))
        assert s.commit(cd.op.op_id, 7) == "lost"
    assert _current(shared, a) == 9
    assert _member(shared, d) is None


def test_staff_relay_is_not_current_until_thread_accept(
    shared: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    doc = _register(home, capsys, "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "4")
    new = str(doc["session_id"])
    _start(new)
    assert _current(shared, src) == 0 and _member(shared, new) is None  # pending
    accept = ("thread-accept", "-S", new, "-f", src, "-r", "staff/a/x", "-g", "4")
    assert _verb(*accept) == 0
    assert _member(shared, new)[:2] == (src, 1)
    assert _verb(*accept) == 0  # idempotent
    assert _verb("thread-handback", "-S", new, "-O", str(doc["thread_op"])) == 1
    assert _q(shared, "SELECT state FROM ccc_thread_successions") == [("accepted",)]


def test_thread_handback_cancels_a_pending_reservation_by_token(
    shared: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    doc = _register(home, capsys, "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "4")
    new, op = str(doc["session_id"]), str(doc["thread_op"])
    _start(new)
    assert _verb("thread-handback", "-S", new, "-O", op) == 0
    assert _q(shared, "SELECT state FROM ccc_thread_successions") == [("cancelled",)]
    with Store() as store:
        join = store.thread_join_for(new)
    assert join is not None and join.state == CANCELLED
    assert _current(shared, src) == 0


def test_a_shared_accept_wins_over_a_late_state_db_handback(
    shared: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Shared publication done, state.db's step crashed: the hand-back heals, not cancels."""
    assert shared.is_file()
    src = _source(home)
    doc = _register(home, capsys, "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "4")
    new, op = str(doc["session_id"]), str(doc["thread_op"])
    _start(new)
    with Store() as store:
        assert shared_lifecycle.relay_commit(store, src, new, op, staff=True) == "accepted"
        assert store.thread_join_for(new).state == RESERVED  # type: ignore[union-attr]
    assert _verb("thread-handback", "-S", new, "-O", op) == 1
    with Store() as store:
        assert store.thread_join_for(new).state == ACCEPTED  # type: ignore[union-attr]


def test_register_headless_names_the_run_thread_idempotently(shared: Path) -> None:
    sid = _sid()
    with _open(shared) as s:
        assert s.register_headless("42/abc", sid, 1) is True
        assert s.register_headless("42/abc", sid, 2) is False
    assert _member(shared, sid)[:2] == ("run:42/abc", 0)
    assert _q(shared, "SELECT kind FROM ccc_threads") == [("headless",)]


def test_thread_register_verb_registers_ends_and_is_idempotent(shared: Path) -> None:
    sid = _sid()
    assert _verb("thread-register", "-H", "-r", "42/abc", "-S", sid) == 0
    assert _verb("thread-register", "-H", "-r", "42/abc", "-S", sid) == 0
    assert _member(shared, sid)[:2] == ("run:42/abc", 0)
    assert _q(shared, "SELECT count(*) FROM ccc_threads") == [(1,)]
    assert _verb("thread-register", "-e", "-S", sid) == 0
    assert _q(shared, "SELECT end_reason FROM ccc_sessions WHERE session_id = ?", sid) == [
        ("done",)
    ]
    other = _sid()
    assert _verb("thread-register", "-H", "-r", "43/x", "-S", other) == 0
    assert _verb("thread-register", "-x", "-S", other) == 0
    assert _q(shared, "SELECT end_reason FROM ccc_sessions WHERE session_id = ?", other) == [
        ("closed",)
    ]


@pytest.mark.usefixtures("home")
def test_thread_register_verb_is_a_noop_when_off_and_refuses_bad_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _verb("thread-register", "-H", "-r", "1/a", "-S", _sid()) == 0
    assert _verb("thread-register", "-H", "-e", "-S", _sid()) == 2
    assert _verb("thread-register", "-H", "-S", _sid()) == 2
    assert "error" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# guards and the beat
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "value"),
    [("ccc_shared_schema", "2"), ("backend", "maintenance"), ("holder_authority_epoch", "2")],
)
def test_an_unknown_component_maintenance_or_stale_epoch_writes_nothing(
    shared: Path, key: str, value: str
) -> None:
    first = _sid()
    _start(first)  # pins epoch 1
    conn = sqlite3.connect(shared)
    conn.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
    conn.commit()
    conn.close()
    sid = _sid()
    _start(sid)
    hooks.handle_stop({"session_id": first, "cwd": "/repo"})
    assert _member(shared, sid) is None


def test_a_beat_inside_60_s_writes_nothing(shared: Path) -> None:
    sid = _sid()
    _start(sid)
    with _open(shared) as s:
        assert s.beat(sid, 100_000) is True
        assert s.beat(sid, 130_000) is False
        assert s.beat(sid, 161_000) is True
    hooks.handle_stop({"session_id": sid, "cwd": "/repo"})
    hooks.handle_user_prompt({"session_id": sid, "cwd": "/repo", "prompt": "hi"})
    beats = _q(shared, "SELECT last_beat FROM ccc_sessions WHERE session_id = ?", sid)
    assert int(beats[0][0]) > 161_000


def test_a_beat_with_the_write_lock_held_elsewhere_is_skipped(shared: Path) -> None:
    sid = _sid()
    _start(sid)
    holder = sqlite3.connect(shared, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Store() as store:
            s = shared_store.SharedStore(
                shared, busy_ms=shared_store.BEAT_BUSY_MS, private=store.conn
            )
            with s:
                assert s.beat(sid, 10**12) is False
        assert hooks.handle_stop({"session_id": sid, "cwd": "/repo"}) == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _q(shared, "SELECT last_beat FROM ccc_sessions") == [(None,)]


def test_two_writers_never_publish_two_successors(shared: Path) -> None:
    src = _sid()
    _start(src)
    results: list[str | None] = []

    def race(target: str) -> None:
        with Store() as store:
            with shared_store.SharedStore(shared, private=store.conn) as s:
                res = s.reserve(src, target, "continue", 1)
                if res is not None and res.op is not None and res.status == "reserved":
                    results.append(s.commit(res.op.op_id, 2))
                else:
                    results.append("lost")

    threads = [threading.Thread(target=race, args=(_sid(),)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r or "" for r in results) == ["accepted", "lost"]
    assert _current(shared, src) == 1
