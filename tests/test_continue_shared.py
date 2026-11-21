"""continue-session's Design 3 steps in the shared store and recovery by op id (tp#2372 F3b2).

With ``shared_store_path`` set, a transfer is an operation-bound succession whose op id is
the transfer id: ``start`` reserves the source's generation before the launch, ``accept``
settles its checks and hooks, publishes the generation CAS and only then runs state.db's
accepted CAS. A failure before the publication leaves the source current; a crash after it
is finished by the reconciler with no further prompt; a replay never reactivates an older
successor.
"""

from __future__ import annotations

# pylint: disable=redefined-outer-name,protected-access  # pytest fixtures, module helpers
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import test_continue_session as tcs

# pylint: disable=unused-import  # fixtures re-used by name from test_continue_session
from test_continue_session import _fixture_session as fixture_session  # noqa: F401
from test_continue_session import _harness as _harness_fixture  # noqa: F401
from test_shared_store import SHARED_DDL

# pylint: enable=unused-import
from command_center import config, continue_session, core, hooks, shared_store
from command_center.await_store import AwaitConflict
from command_center.store import Store

SID = tcs.SID


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fixture shared file, ``shared_store_path`` set to it, no handoff hooks."""
    path = tmp_path / "shared.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SHARED_DDL)
    conn.close()
    cfg = config.Config(shared_store_path=str(path), handoff_hooks=[])
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    return path


def _q(path: Path, sql: str, *params: object) -> list[Any]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _x(path: Path, sql: str, *params: object) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _current(path: Path) -> int:
    """The source thread's (``SID``'s) current generation."""
    return int(
        _q(path, "SELECT current_generation FROM ccc_threads WHERE thread_id = ?", SID)[0][0]
    )


def _op(path: Path, op_id: str) -> tuple[str, str, str] | None:
    rows = _q(
        path,
        "SELECT state, join_kind, target_session FROM ccc_thread_successions WHERE op_id = ?",
        op_id,
    )
    return (str(rows[0][0]), str(rows[0][1]), str(rows[0][2])) if rows else None


def _member(path: Path, sid: str) -> tuple[str, int] | None:
    rows = _q(path, "SELECT thread_id, generation FROM ccc_sessions WHERE session_id = ?", sid)
    return (str(rows[0][0]), int(rows[0][1])) if rows else None


def _opened(harness: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """An opened transfer (reserved by ``start``) and its successor as the caller."""
    tid = tcs._opened_transfer(harness)
    target = tcs._as_successor(monkeypatch, harness, tid)
    return tid, target


def _accept(tid: str, target: str) -> int:
    with Store() as store:
        rc: int = continue_session.accept_as(store, tid, target, False, config.load_config())
    return rc


def test_start_reserves_before_the_launch_and_the_successor_start_writes_no_row(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    assert harness.launches == [target]
    assert _op(shared, tid) == ("reserved", "continue", target)
    hooks.handle_session_start({"session_id": target, "cwd": "/repo", "source": "startup"})
    assert _member(shared, target) is None  # the join writes it: pending, no grant
    assert _current(shared) == 0


def test_accept_publishes_then_runs_the_state_db_cas_and_a_duplicate_is_a_no_op(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    assert _accept(tid, target) == 0
    assert tcs._state(tid) == "finalized"
    assert _op(shared, tid) == ("accepted", "continue", target)
    assert _member(shared, target) == (SID, 1) and _current(shared) == 1
    ended = _q(shared, "SELECT end_reason FROM ccc_sessions WHERE session_id = ?", SID)
    assert ended == [("handed_off",)]
    assert _accept(tid, target) == 0  # duplicate accept
    assert _current(shared) == 1 and _op(shared, tid) == ("accepted", "continue", target)


def _fail_conflict(*_a: object, **_k: object) -> object:
    raise AwaitConflict("this session already holds await group 7")


def _fail_sqlite(*_a: object, **_k: object) -> object:
    raise sqlite3.OperationalError("disk I/O error")


def _lost_cas(*_a: object, **_k: object) -> object:
    return None  # the transfer is no longer prepared/opened


@pytest.mark.parametrize("failure", [_fail_conflict, _fail_sqlite, _lost_cas])
def test_a_step_two_failure_leaves_the_source_current(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    tid, target = _opened(harness, monkeypatch)
    calls = {"n": 0}
    real = Store.prepare_accept

    def settled_check_fails(self: Store, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:  # the hooks' accept-prepare passes; the settled check fails
            return real(self, *args, **kwargs)
        return failure(*args, **kwargs)

    monkeypatch.setattr(Store, "prepare_accept", settled_check_fails)
    assert _accept(tid, target) == 1
    assert calls["n"] == 2
    assert _current(shared) == 0 and _member(shared, target) is None
    assert _op(shared, tid) == ("reserved", "continue", target)  # still this op's, pending
    assert tcs._state(tid) == "opened"
    monkeypatch.setattr(Store, "prepare_accept", real)
    assert _accept(tid, target) == 0  # retryable: the next accept publishes
    assert _current(shared) == 1


def test_a_crash_after_step_three_is_finished_by_the_reconciler(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    real = Store.accept_transfer
    monkeypatch.setattr(Store, "accept_transfer", _fail_sqlite)
    assert _accept(tid, target) == 1
    assert _op(shared, tid) == ("accepted", "continue", target) and _current(shared) == 1
    assert tcs._state(tid) == "opened"  # state.db's step never ran
    with Store() as store:  # a published op cannot be aborted any more
        code, message = continue_session.abort_transfer(store, tid, "stuck", 1)
    assert code == 1 and "cannot be aborted" in message
    monkeypatch.setattr(Store, "accept_transfer", real)
    with Store() as store:  # no further prompt: only ccc's reconciler
        lines = continue_session.recover_shared_ops(store, grace_ms=0)
    assert lines == [f"op {tid}: accept finished (source finalized)"]
    assert tcs._state(tid) == "finalized"
    with Store() as store:
        assert not continue_session.recover_shared_ops(store, grace_ms=0)  # idempotent


@pytest.mark.usefixtures("shared")
def test_the_reconciler_respects_the_grace_and_runs_from_the_full_pass(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    real = Store.accept_transfer
    monkeypatch.setattr(Store, "accept_transfer", _fail_sqlite)
    assert _accept(tid, target) == 1
    monkeypatch.setattr(Store, "accept_transfer", real)
    seen: list[str] = []

    def spy(store: Store) -> list[str]:
        del store
        seen.append("full")
        return []

    with Store() as store:
        assert not continue_session.recover_shared_ops(store)  # a fresh op: left alone
        assert tcs._state(tid) == "opened"
        monkeypatch.setattr(continue_session, "recover_shared_ops", spy)
        core._recover_shared_ops(store)
    assert seen == ["full"]


def test_a_committing_op_is_published_and_finished_by_the_reconciler(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    _x(shared, "UPDATE ccc_thread_successions SET state = 'committing' WHERE op_id = ?", tid)
    with Store() as store:
        lines = continue_session.recover_shared_ops(store, grace_ms=0)
    assert lines == [f"op {tid}: accept finished (source finalized)"]
    assert _member(shared, target) == (SID, 1) and tcs._state(tid) == "finalized"


def test_abort_cancels_the_reservation_and_a_failed_transfer_is_cancelled_by_op_id(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    with Store() as store:
        code, _ = continue_session.abort_transfer(store, tid, "stuck", 1)
    assert code == 0 and _op(shared, tid) == ("cancelled", "continue", target)
    assert _current(shared) == 0
    # A reservation whose abort never reached the shared store: the reconciler cancels it.
    _x(shared, "UPDATE ccc_thread_successions SET state = 'reserved' WHERE op_id = ?", tid)
    with Store() as store:
        lines = continue_session.recover_shared_ops(store, grace_ms=0)
    assert lines == [f"op {tid}: transfer failed, reservation cancelled"]
    assert _op(shared, tid) == ("cancelled", "continue", target)


def test_a_replayed_a_to_b_after_b_to_c_does_not_reactivate_b(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, b = _opened(harness, monkeypatch)
    assert _accept(tid, b) == 0
    c = "c0000000-0000-4000-8000-000000000000"
    with Store() as store, shared_store.SharedStore(shared, private=store.conn) as db:
        res = db.reserve(b, c, "continue", 5, op_id="t-b-to-c")
        assert res is not None and res.status == "reserved"
        assert db.commit("t-b-to-c", 6) == "accepted"
    assert _current(shared) == 2 and _member(shared, c) == (SID, 2)
    assert _accept(tid, b) == 0  # the replay acknowledges the op
    with Store() as store:
        assert not continue_session.recover_shared_ops(store, grace_ms=0)
        with shared_store.SharedStore(shared, private=store.conn) as db:
            assert db.commit(tid, 7) == "already"
    assert _current(shared) == 2 and _member(shared, b) == (SID, 1)


def test_an_early_plan_row_leaves_the_target_pending_with_no_grant(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    hooks.handle_session_start({"session_id": target, "cwd": "/repo", "source": "startup"})
    _x(shared, "INSERT INTO holdings (tp_id, session, plan_row) VALUES (9, ?, 'B1')", target)
    assert _member(shared, target) is None  # no row of its own: no thread, no grant
    with Store() as store, shared_store.SharedStore(shared, private=store.conn) as db:
        pending = db.pending_for_target(target)
    assert pending is not None and pending.op_id == tid
    assert _current(shared) == 0
    assert _accept(tid, target) == 0  # the join then writes its row in the source's thread
    assert _member(shared, target) == (SID, 1)


def test_a_stray_root_holding_an_obligation_is_quarantined_and_the_source_stays(
    shared: Path, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, target = _opened(harness, monkeypatch)
    _x(
        shared,
        "INSERT INTO ccc_threads (thread_id, kind, root_session, created_at) "
        "VALUES (?, 'ccc', ?, 1)",
        target,
        target,
    )
    _x(
        shared,
        "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at) "
        "VALUES (?, ?, 0, 1)",
        target,
        target,
    )
    _x(shared, "INSERT INTO holdings (tp_id, session, claim_at) VALUES (9, ?, 'now')", target)
    assert _accept(tid, target) == 1
    assert _current(shared) == 0 and _member(shared, target) == (target, 0)
    assert _op(shared, tid) == ("failed", "continue", target)
    assert tcs._state(tid) == "opened"


def test_with_the_key_unset_nothing_opens_a_shared_file(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("the shared store was opened with the key unset")

    monkeypatch.setattr(config, "load_config", config.Config)
    monkeypatch.setattr(shared_store, "open_shared", never)
    tid, target = _opened(harness, monkeypatch)
    assert _accept(tid, target) == 0
    with Store() as store:
        assert not continue_session.recover_shared_ops(store, grace_ms=0)
        core._recover_shared_ops(store)
