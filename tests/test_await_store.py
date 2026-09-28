"""Storage of ``ccc await`` groups and sources (``command_center.await_store``).

The transitions are compare-and-swaps; the races that matter (two passes firing the
same group, two deliverers claiming one outbox row) are driven through two real
connections to the same DB file.
"""

# pylint: disable=unbalanced-tuple-unpacking  # `[(src, _)] = …` asserts exactly one lease
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from awaitstub import seeded_store as _store

from command_center import store as store_mod
from command_center.await_store import GRACE_SEC, AwaitConflict, SourceSpec
from command_center.store import Store

NOW = 1_800_000_000


def _arm(store: Store, session_id: str = "s1", *, kinds: tuple[str, ...] = ("zoho-reply",)) -> int:
    return store.arm_await(
        session_id,
        config_dir="/acct",
        cwd="/repo",
        no_codex=False,
        prompt_template="got {event}",
        until_epoch=NOW + 3600,
        sources=[
            SourceSpec(kind=k, spec={"schema_version": 1, "n": i}, watermark=f"w{i}")
            for i, k in enumerate(kinds)
        ],
        now=NOW,
    )


def test_tables_appear_on_an_older_db(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(store_mod._SCHEMA)  # the schema WITHOUT the await tables
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('s1', '/repo')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        assert not store.has_active_awaits()
        gid = _arm(store)
        assert store.get_await_group(gid) is not None


def test_two_concurrent_opens_both_create_the_tables(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _open() -> None:
        try:
            barrier.wait(timeout=10)
            with Store(db) as store:
                store.list_awaits()
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    threads = [threading.Thread(target=_open) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors


def test_a_newer_builds_extra_column_is_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.conn.execute("ALTER TABLE await_groups ADD COLUMN from_the_future TEXT")
    store.conn.execute("ALTER TABLE await_sources ADD COLUMN also_new INTEGER")
    gid = _arm(store)
    [(group, sources)] = store.list_awaits("s1")
    assert group.id == gid and sources[0].kind == "zoho-reply"


def test_arm_inserts_group_and_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, kinds=("zoho-reply", "slack-dm"))
    group = store.get_await_group(gid)
    assert group is not None
    assert (group.state, group.config_dir, group.cwd) == ("armed", "/acct", "/repo")
    assert group.grace_until_epoch == NOW + 3600 + GRACE_SEC
    sources = store.await_sources_of(gid)
    assert [s.kind for s in sources] == ["zoho-reply", "slack-dm"]
    assert sources[0].spec_dict() == {"n": 0, "schema_version": 1}
    assert sources[1].watermark == "w1"
    assert all(s.next_check_at == NOW + 120 for s in sources)


def test_one_active_group_per_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    with pytest.raises(AwaitConflict):
        _arm(store)
    assert len(store.list_awaits("s1")) == 1  # the loser wrote nothing
    assert store.disarm_group(gid, NOW)
    _arm(store)  # a disarmed group frees the slot


def test_an_empty_source_list_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.arm_await(
            "s1",
            config_dir="",
            cwd="",
            no_codex=False,
            prompt_template="{event}",
            until_epoch=NOW,
            sources=[],
            now=NOW,
        )


def test_interval_below_minimum_is_raised_to_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = store.arm_await(
        "s1",
        config_dir="",
        cwd="",
        no_codex=False,
        prompt_template="{event}",
        until_epoch=NOW + 10,
        sources=[SourceSpec(kind="cmd", spec={}, interval_sec=5)],
        now=NOW,
    )
    assert store.await_sources_of(gid)[0].interval_sec == 60


def test_arm_and_close_is_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid, token = store.arm_await_and_close(
        "s1",
        config_dir="",
        cwd="",
        no_codex=False,
        prompt_template="{event}",
        until_epoch=NOW + 10,
        sources=[SourceSpec(kind="cmd", spec={})],
        now=NOW,
        close_now_ms=NOW * 1000,
    )
    session = store.get("s1")
    assert session is not None
    assert (session.close_requested_at, session.close_token) == (NOW * 1000, token)
    assert store.get_await_group(gid) is not None
    # A conflict leaves the close arm untouched, not half-written.
    store.disarm_close("s1")
    with pytest.raises(AwaitConflict):
        store.arm_await_and_close(
            "s1",
            config_dir="",
            cwd="",
            no_codex=False,
            prompt_template="{event}",
            until_epoch=NOW + 10,
            sources=[SourceSpec(kind="cmd", spec={})],
            now=NOW,
            close_now_ms=NOW * 1000,
        )
    assert store.get("s1").close_requested_at == 0  # type: ignore[union-attr]


def test_session_delete_cascades(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    store.delete("s1")
    assert store.get_await_group(gid) is None
    assert store.await_sources_of(gid) == []


def test_lease_due_sources_caps_and_excludes_leased(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(12):
        store.ensure(f"x{i}")
        _arm(store, f"x{i}")
    assert not store.lease_due_sources(NOW)  # not due yet
    due = NOW + 120
    first = store.lease_due_sources(due)
    assert len(first) == 10
    second = store.lease_due_sources(due)
    assert len(second) == 2  # the leased ten are skipped
    assert not store.lease_due_sources(due)
    # An expired lease (a crashed pass) is leasable again.
    assert len(store.lease_due_sources(due + 46)) == 10


def test_release_lease_needs_the_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _arm(store)
    [(src, _grp)] = store.lease_due_sources(NOW + 120)
    assert not store.release_lease(src.id, "wrong")
    assert store.release_lease(src.id, src.lease_token)
    assert len(store.lease_due_sources(NOW + 120)) == 1


def test_finish_probe_records_and_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    [(src, _)] = store.lease_due_sources(NOW + 120)
    assert store.finish_probe(
        src.id, src.lease_token, next_check_at=NOW + 999, fail_count=2, fail_class="transient"
    )
    got = store.get_await_source(src.id)
    assert got is not None
    assert (got.next_check_at, got.fail_count, got.lease_token) == (NOW + 999, 2, "")
    assert not store.finish_probe(src.id, src.lease_token, next_check_at=0, fail_count=0)
    [(src, _)] = store.lease_due_sources(NOW + 999)
    assert store.finish_probe(
        src.id, src.lease_token, next_check_at=NOW, fail_count=1, blocked=True
    )
    assert store.await_sources_of(gid)[0].state == "blocked"
    assert store.viable_source_count(gid) == 0


def test_fire_group_wins_once_and_disarms_siblings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, kinds=("zoho-reply", "slack-dm"))
    leased = store.lease_due_sources(NOW + 120)
    (a, _), (b, _) = leased
    token = store.fire_group(
        gid,
        a.id,
        a.lease_token,
        event_id="e1",
        payload="{}",
        remote_epoch=5,
        watermark="wa",
        now=NOW,
    )
    assert token
    assert (
        store.fire_group(
            gid,
            b.id,
            b.lease_token,
            event_id="e2",
            payload="{}",
            remote_epoch=6,
            watermark="wb",
            now=NOW,
        )
        is None
    )
    group = store.get_await_group(gid)
    assert group is not None
    assert (group.state, group.winner_source_id, group.event_id) == ("fired", a.id, "e1")
    assert group.delivery_token == token
    states = {s.id: (s.state, s.watermark) for s in store.await_sources_of(gid)}
    assert states == {a.id: ("done", "wa"), b.id: ("disarmed", "w1")}


def test_fire_needs_the_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    [(src, _)] = store.lease_due_sources(NOW + 120)
    assert (
        store.fire_group(
            gid,
            src.id,
            "stale",
            event_id="e",
            payload="",
            remote_epoch=0,
            watermark="",
            now=NOW,
        )
        is None
    )


def test_two_connections_race_to_fire(tmp_path: Path) -> None:
    db = tmp_path / "race.db"
    with Store(db) as setup:
        setup.ensure("s1")
        gid = _arm(setup, kinds=("zoho-reply", "slack-dm"))
        (a, _), (b, _) = setup.lease_due_sources(NOW + 120)
    barrier = threading.Barrier(2)
    tokens: list[str | None] = []
    errors: list[BaseException] = []

    def _fire(src_id: int, lease: str) -> None:
        try:
            with Store(db) as store:
                barrier.wait(timeout=10)
                tokens.append(
                    store.fire_group(
                        gid,
                        src_id,
                        lease,
                        event_id=str(src_id),
                        payload="{}",
                        remote_epoch=0,
                        watermark="",
                        now=NOW,
                    )
                )
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    threads = [
        threading.Thread(target=_fire, args=(a.id, a.lease_token)),
        threading.Thread(target=_fire, args=(b.id, b.lease_token)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    assert sum(1 for t in tokens if t) == 1


def _fired(store: Store) -> tuple[int, str]:
    gid = _arm(store)
    [(src, _)] = store.lease_due_sources(NOW + 120)
    token = store.fire_group(
        gid,
        src.id,
        src.lease_token,
        event_id="e",
        payload='{"x":1}',
        remote_epoch=1,
        watermark="w",
        now=NOW,
    )
    assert token
    return gid, token


def test_delivery_outbox_cas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid, token = _fired(store)
    assert not store.mark_delivering(gid, "other", NOW)
    assert store.mark_delivering(gid, token, NOW)
    assert not store.mark_delivering(gid, token, NOW)  # already delivering
    assert not store.mark_delivered(gid, "other", NOW)
    assert store.mark_delivered(gid, token, NOW)
    assert not store.mark_delivered(gid, token, NOW)
    assert store.active_await("s1") is None  # delivered frees the slot


def test_two_connections_race_to_claim_delivery(tmp_path: Path) -> None:
    db = tmp_path / "claim.db"
    with Store(db) as setup:
        setup.ensure("s1")
        gid, token = _fired(setup)
        assert setup.mark_delivering(gid, token, NOW)
    barrier = threading.Barrier(2)
    wins: list[bool] = []

    def _claim() -> None:
        with Store(db) as store:
            barrier.wait(timeout=10)
            wins.append(store.mark_delivered(gid, token, NOW))

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(wins) == [False, True]


def test_revert_delivery_retries_then_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid, token = _fired(store)
    for attempt in (1, 2):
        assert store.mark_delivering(gid, token, NOW)
        assert store.revert_delivery(gid, token, NOW, max_attempts=3, reason="x") == "fired"
        assert store.get_await_group(gid).delivery_attempts == attempt  # type: ignore[union-attr]
    assert store.mark_delivering(gid, token, NOW)
    assert store.revert_delivery(gid, token, NOW, max_attempts=3, reason="no tab") == "blocked"
    group = store.get_await_group(gid)
    assert group is not None
    assert (group.state, group.blocked_reason) == ("blocked", "no tab")
    assert store.revert_delivery(gid, token, NOW, max_attempts=3, reason="x") == ""


def test_retry_of_a_fired_blocked_group_keeps_payload_with_a_new_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid, token = _fired(store)
    assert store.block_group(gid, "untrusted", NOW, from_states=("fired",))
    assert store.retry_group(gid, NOW) == "fired"
    group = store.get_await_group(gid)
    assert group is not None
    assert group.event_payload == '{"x":1}'
    assert group.delivery_token and group.delivery_token != token
    assert not store.mark_delivering(gid, token, NOW)  # the old token is dead
    assert store.retry_group(gid, NOW) == ""  # not blocked any more


def test_retry_of_a_sourceless_group_rearms_blocked_sources_at_their_watermark(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    [(src, _)] = store.lease_due_sources(NOW + 120)
    store.finish_probe(src.id, src.lease_token, next_check_at=NOW, fail_count=1, blocked=True)
    assert store.block_group(gid, "no viable source", NOW, from_states=("armed", "grace"))
    assert store.retry_group(gid, NOW + 200) == "armed"
    again = store.await_sources_of(gid)[0]
    assert (again.state, again.watermark, again.fail_count) == ("armed", "w0", 0)
    assert again.next_check_at == NOW + 200
    # Past `until` a retried group comes back in grace.
    store.finish_probe(
        *[(s.id, s.lease_token) for s, _ in store.lease_due_sources(NOW + 200)][0],
        next_check_at=NOW,
        fail_count=1,
        blocked=True,
    )
    store.block_group(gid, "again", NOW, from_states=("armed",))
    assert store.retry_group(gid, NOW + 4000) == "grace"


def test_disarm_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, kinds=("zoho-reply", "cmd"))
    assert store.disarm_group(gid, NOW, reason="user")
    assert not store.disarm_group(gid, NOW)
    assert {s.state for s in store.await_sources_of(gid)} == {"disarmed"}
    assert store.list_awaits("s1") == []
    assert len(store.list_awaits("s1", include_inactive=True)) == 1


def test_disarming_a_delivering_group_defeats_the_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid, token = _fired(store)
    assert store.mark_delivering(gid, token, NOW)
    assert store.disarm_group(gid, NOW)
    assert not store.mark_delivered(gid, token, NOW)


def test_advance_deadlines(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    assert store.advance_deadlines(NOW + 3599) == []
    assert store.get_await_group(gid).state == "armed"  # type: ignore[union-attr]
    assert store.advance_deadlines(NOW + 3600) == []
    assert store.get_await_group(gid).state == "grace"  # type: ignore[union-attr]
    expired = store.advance_deadlines(NOW + 3600 + GRACE_SEC)
    assert [g.id for g in expired] == [gid]
    assert expired[0].state == "expired"
    assert {s.state for s in store.await_sources_of(gid)} == {"disarmed"}


def test_a_fired_group_never_expires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid, _token = _fired(store)
    assert store.advance_deadlines(NOW + 10 * GRACE_SEC) == []
    assert store.get_await_group(gid).state == "fired"  # type: ignore[union-attr]


def test_notice_claims_are_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    src = store.await_sources_of(gid)[0]
    assert store.claim_group_notice(gid, NOW)
    assert not store.claim_group_notice(gid, NOW)
    assert store.claim_source_notice(src.id, NOW)
    assert not store.claim_source_notice(src.id, NOW)
