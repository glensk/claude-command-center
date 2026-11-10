"""The thread id: one id per piece of work across continue-session relays (tp#1989 B2).

Fixture stores only (``tmp_path``); placeholder ids. The thread id is the chain root's
session id: the ``sessions.thread_id`` column ``accept`` writes, else the ``ccc lineage``
walk over ``handed_off_from`` / transfers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from command_center import bridge_lineage, thread
from command_center.store import Store
from command_center.transfer_store import FAILED, FINALIZED, Transfer

NOW = 1_800_000_000_000
MIN = 60_000


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "ccc.db")
    for sid in ("root", "s1", "s2", "s3", "draft"):
        db.ensure(sid, cwd="/repo")
    return db


def _relay(store: Store, source: str, target: str, *, at: int = NOW, accept: bool = True) -> None:
    """*source* → *target* as ``continue-session`` leaves it (accept records the thread)."""
    store.create_transfer(
        Transfer(transfer_id=f"t-{source}-{target}", source_session=source, target_session=target),
        at - MIN,
    )
    store.update_fields(target, handed_off_from=source)  # written at `start`
    if accept:
        store.conn.execute(
            "UPDATE transfers SET state = ?, accepted_at = ? WHERE transfer_id = ?",
            (FINALIZED, at, f"t-{source}-{target}"),
        )
        store.conn.commit()
        store.record_thread(target, source)
        store.set_handoff_lineage(source, target, at)


def _column(store: Store, sid: str) -> str | None:
    row = store.conn.execute("SELECT thread_id FROM sessions WHERE session_id = ?", (sid,))
    value = row.fetchone()["thread_id"]
    return None if value is None else str(value)


def test_an_existing_db_gains_the_column_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with Store(path) as store:
        store.ensure("root", cwd="/repo")
        store.conn.execute("ALTER TABLE sessions DROP COLUMN thread_id")
        store.conn.commit()
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert "thread_id" not in cols
    with Store(path) as store:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(sessions)")}
        assert "thread_id" in cols
        row = store.get("root")
        assert row is not None and row.thread_id is None
        assert store.thread_of("root") == "root"


def test_a_root_is_its_own_thread(store: Store) -> None:
    assert store.thread_of("root") == "root"
    assert thread.walk(store.conn, "root").exact is True
    assert _column(store, "root") is None


def test_every_hop_of_an_accepted_chain_carries_the_root(store: Store) -> None:
    _relay(store, "root", "s1", at=NOW)
    _relay(store, "s1", "s2", at=NOW + MIN)
    _relay(store, "s2", "s3", at=NOW + 2 * MIN)
    assert [_column(store, s) for s in ("root", "s1", "s2", "s3")] == [None, "root", "root", "root"]
    assert {store.thread_of(s) for s in ("root", "s1", "s2", "s3")} == {"root"}
    path = thread.walk(store.conn, "s3")
    assert path.path == ["root", "s1", "s2", "s3"] and path.exact is True
    row = store.get("s3")
    assert row is not None and row.thread_id == "root"


def test_a_rerun_accept_writes_nothing_new(store: Store) -> None:
    _relay(store, "root", "s1")
    assert store.record_thread("s1", "root") is False
    assert _column(store, "s1") == "root"


def test_a_failed_transfers_draft_keeps_the_column_null(store: Store) -> None:
    _relay(store, "root", "draft", accept=False)
    store.conn.execute("UPDATE transfers SET state = ? WHERE target_session = 'draft'", (FAILED,))
    store.conn.commit()
    assert _column(store, "draft") is None
    # `handed_off_from` is written at `start`, so the walk (= `ccc lineage`) still reaches it
    assert store.thread_of("draft") == "root"


def test_a_legacy_chain_without_the_column_walks_to_the_root(store: Store) -> None:
    for source, target in (("root", "s1"), ("s1", "s2")):
        store.set_handoff_lineage(source, target, NOW)
    assert _column(store, "s2") is None
    assert store.thread_of("s2") == "root"
    assert thread.walk(store.conn, "s2").exact is True


def test_a_transfers_only_hop_is_followed_like_ccc_lineage(store: Store) -> None:
    store.create_transfer(
        Transfer(transfer_id="t-x", source_session="s1", target_session="s2"), NOW
    )
    store.set_handoff_lineage("root", "s1", NOW)
    assert store.thread_of("s2") == "root"
    chain = [h["session_id"] for h in bridge_lineage.lineage(store, "s2")["chain"]]
    assert thread.walk(store.conn, "s2").path == list(reversed(["s2", *chain]))


def test_a_cycle_is_cut_and_marked_inexact(store: Store) -> None:
    store.update_fields("s1", handed_off_from="s2")
    store.update_fields("s2", handed_off_from="s1")
    path = thread.walk(store.conn, "s2")
    assert path.exact is False and path.thread_id == "s1"
    assert bridge_lineage.lineage(store, "s2")["truncated"] is True


def test_more_than_fifty_hops_is_cut_and_marked_inexact(store: Store) -> None:
    ids = [f"h{i:02d}" for i in range(60)]
    for sid in ids:
        store.ensure(sid, cwd="/repo")
    for parent, child in zip(ids, ids[1:], strict=False):
        store.update_fields(child, handed_off_from=parent)
    path = thread.walk(store.conn, ids[-1])
    assert path.exact is False
    assert len(path.path) == thread.MAX_HOPS + 1
    assert path.thread_id == ids[-1 - thread.MAX_HOPS]
    lineage = bridge_lineage.lineage(store, ids[-1])
    assert lineage["truncated"] is True and lineage["chain"][-1]["session_id"] == path.thread_id


def test_a_missing_ancestor_row_gives_the_oldest_reachable_id(store: Store) -> None:
    store.update_fields("s1", handed_off_from="gone")  # its row was deleted
    store.update_fields("s2", handed_off_from="s1")
    path = thread.walk(store.conn, "s2")
    assert path.thread_id == "gone" and path.exact is False
    assert path.path == ["gone", "s1", "s2"]


def test_a_recorded_column_survives_a_deleted_root(store: Store) -> None:
    _relay(store, "root", "s1")
    _relay(store, "s1", "s2")
    store.conn.execute("DELETE FROM sessions WHERE session_id = 'root'")
    store.conn.commit()
    path = thread.walk(store.conn, "s2")
    assert path.thread_id == "root" and path.exact is True


def test_an_ancestor_column_names_the_thread_of_an_unrecorded_hop(store: Store) -> None:
    _relay(store, "root", "s1")
    store.update_fields("s2", handed_off_from="s1")  # started, not yet accepted
    assert _column(store, "s2") is None
    assert store.thread_of("s2") == "root"


def test_an_unknown_session_is_its_own_inexact_thread(store: Store) -> None:
    path = thread.walk(store.conn, "never-seen")
    assert path.thread_id == "never-seen" and path.path == ["never-seen"]
    assert path.exact is False
    assert store.thread_of("never-seen") == "never-seen"


def test_the_batched_walk_answers_every_id_like_the_single_one(store: Store) -> None:
    _relay(store, "root", "s1")
    _relay(store, "s1", "s2")
    store.update_fields("s3", handed_off_from="gone")
    got = thread.facts(store.conn, ["s2", "s3", "root", "never-seen"])
    for sid in ("s2", "s3", "root", "never-seen"):
        assert got[sid].thread_id == thread.walk(store.conn, sid).thread_id
    assert got["never-seen"].aims_n == 0 and got["never-seen"].user_aim is None
