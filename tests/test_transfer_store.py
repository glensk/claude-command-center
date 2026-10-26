"""The ``transfers`` table, its compare-and-set state machine and the handoff lineage (tp#1300)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from command_center.core import Row
from command_center.models import (
    HANDOFF_MARKER_WIDTH,
    LiveSession,
    Session,
    Status,
    derive_status,
    handoff_marker,
)
from command_center.store import Store
from command_center.transfer_store import (
    ACCEPTED,
    FAILED,
    FINALIZED,
    OPENED,
    PREPARED,
    TRANSFER_STATES,
    TRANSITIONS,
    Transfer,
    TransferConflict,
    new_transfer_id,
)
from command_center.views import ls as ls_view

NOW = 1_800_000_000_000


def _transfer(source: str = "src-1", **kw: object) -> Transfer:
    base: dict[str, object] = {
        "transfer_id": new_transfer_id(),
        "source_session": source,
        "target_session": "tgt-1",
        "target_label": "private",
        "cwd": "/repo",
        "source_aim": "tp#1 done",
        "prompt_sha256": "ab" * 32,
    }
    base.update(kw)
    return Transfer(**base)  # type: ignore[arg-type]


def _force_state(store: Store, transfer_id: str, state: str) -> None:
    store.conn.execute("UPDATE transfers SET state = ? WHERE transfer_id = ?", (state, transfer_id))
    store.conn.commit()


def test_migration_adds_columns_and_table_to_an_old_db(tmp_path: Path) -> None:
    """A DB from before the feature gains the lineage columns and the table on open."""
    path = tmp_path / "old.db"
    with Store(path) as store:
        store.ensure("s1", cwd="/repo")
    conn = sqlite3.connect(path)
    for column in ("handed_off_to", "handed_off_from", "handed_off_at"):
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    conn.execute("DROP TABLE transfers")
    conn.commit()
    conn.close()
    with Store(path) as store:
        session = store.get("s1")
        assert session is not None and session.handed_off_to is None
        store.set_handoff_lineage("s1", "s2", NOW)
        got = store.get("s1")
        assert got is not None
        assert (got.handed_off_to, got.handed_off_at, got.status) == ("s2", NOW, "handed_off")
        created = store.create_transfer(_transfer("s1"), NOW)
        assert created.state == PREPARED and created.created_at == NOW


def test_round_trip_keeps_every_snapshot_field(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as store:
        want = _transfer(no_codex=True, capped=True, llm_overseer="opus-5", source_aim_rev=3)
        got = store.create_transfer(want, NOW)
        assert got.snapshot() == want.snapshot()
        assert got.no_codex is True and got.capped is True
        assert store.active_transfer("src-1") == got
        assert store.transfers_of("tgt-1") == [got]


@pytest.mark.parametrize("start", TRANSFER_STATES)
@pytest.mark.parametrize("target", TRANSFER_STATES)
def test_every_transition_legal_or_refused(tmp_path: Path, start: str, target: str) -> None:
    """Exactly the edges in TRANSITIONS succeed; everything else changes nothing."""
    with Store(tmp_path / "db") as store:
        tid = store.create_transfer(_transfer(), NOW).transfer_id
        _force_state(store, tid, start)
        legal = target in TRANSITIONS[start]
        assert store.transition_transfer(tid, target, NOW + 1, failure="x: y") is legal
        got = store.get_transfer(tid)
        assert got is not None
        assert got.state == (target if legal else start)
        if legal and target == FAILED:
            assert got.failure == "x: y"
        if legal and target == ACCEPTED:
            assert got.accepted_at == NOW + 1


def test_happy_path_stamps_each_state(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as store:
        tid = store.create_transfer(_transfer(), NOW).transfer_id
        assert store.transition_transfer(tid, OPENED, NOW + 1)
        assert store.note_transfer(tid, NOW + 2, launcher="iterm_api", failure="hook: exit 1")
        assert store.transition_transfer(tid, ACCEPTED, NOW + 3)
        assert store.transition_transfer(tid, FINALIZED, NOW + 4)
        got = store.get_transfer(tid)
        assert got is not None
        assert (got.opened_at, got.accepted_at, got.finalized_at) == (NOW + 1, NOW + 3, NOW + 4)
        assert got.launcher == "iterm_api"
        assert not store.note_transfer(tid, NOW + 5, launcher="x")  # terminal: no notes
        assert store.active_transfer("src-1") is None


def test_one_active_transfer_per_source(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as store:
        first = store.create_transfer(_transfer(), NOW)
        with pytest.raises(TransferConflict):
            store.create_transfer(_transfer(), NOW)
        store.create_transfer(_transfer("other-source"), NOW)  # another source is fine
        assert store.transition_transfer(first.transfer_id, FAILED, NOW, failure="launch: x")
        again = store.create_transfer(_transfer(), NOW)  # failed is not active any more
        assert again.state == PREPARED


def test_concurrent_cas_has_exactly_one_winner(tmp_path: Path) -> None:
    """Two processes racing ``opened → accepted`` (and one ``→ failed``): one wins."""
    path = tmp_path / "db"
    with Store(path) as store:
        tid = store.create_transfer(_transfer(), NOW).transfer_id
        assert store.transition_transfer(tid, OPENED, NOW)
    barrier = threading.Barrier(3)
    results: dict[str, bool] = {}

    def race(name: str, target: str) -> None:
        with Store(path, check_same_thread=False) as store:
            barrier.wait()
            results[name] = store.transition_transfer(tid, target, NOW + 1, failure="f: x")

    threads = [
        threading.Thread(target=race, args=("a", ACCEPTED)),
        threading.Thread(target=race, args=("b", ACCEPTED)),
        threading.Thread(target=race, args=("c", FAILED)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results.values()) == 1


def test_lineage_marks_both_sides(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as store:
        store.ensure("old", cwd="/repo")
        store.ensure("new", cwd="/repo")
        store.set_handoff_lineage("old", "new", NOW)
        old, new = store.get("old"), store.get("new")
        assert old is not None and new is not None
        assert old.handed_off_to == "new" and old.status == Status.HANDED_OFF.value
        assert new.handed_off_from == "old" and new.handed_off_to is None


def _live(raw: str, alive: bool = True) -> LiveSession:
    return LiveSession(session_id="old", cwd="/repo", pid=1, alive=alive, raw_status=raw)


def test_derive_status_handed_off_is_terminal_unless_mid_turn() -> None:
    stored = Session(session_id="old", handed_off_to="new-session-id")
    assert derive_status(None, stored) is Status.HANDED_OFF
    assert derive_status(_live("idle"), stored) is Status.HANDED_OFF
    assert derive_status(_live("idle"), stored, halted=True) is Status.HANDED_OFF
    assert derive_status(_live("busy"), stored) is Status.WORKING
    assert derive_status(_live("idle"), Session(session_id="old")) is Status.IDLE


def test_handoff_marker_is_fixed_width() -> None:
    assert handoff_marker(Session(session_id="x")) == ""
    long = handoff_marker(Session(session_id="x", handed_off_to="0123456789abcdef"))
    short = handoff_marker(Session(session_id="x", handed_off_to="abc"))
    assert long == "⇢ 01234567"
    assert len(long) == len(short) == HANDOFF_MARKER_WIDTH


def test_ls_row_keeps_one_offset_across_statuses() -> None:
    """A handed-off row's columns sit where a parked / live row's do; the marker is on ↳."""
    handed = Session(session_id="s1", cwd="/repo", aim="a", handed_off_to="feedbeef0000")
    plain = Session(session_id="s1", cwd="/repo", aim="a")
    lines = {
        status: ls_view._render_row(  # noqa: SLF001
            Row(handed if status is Status.HANDED_OFF else plain, None, status, 0, 0),
            enabled=False,
            warn_days=2,
            aim_threshold=50,
        )
        for status in (Status.HANDED_OFF, Status.PARKED, Status.IDLE, Status.DONE)
    }
    offsets = {status: line[0].index("/repo") for status, line in lines.items()}
    assert len(set(offsets.values())) == 1, offsets
    assert "⇢ feedbeef handed off" in lines[Status.HANDED_OFF][1]
    assert "⇢" not in lines[Status.PARKED][1]


def test_closed_status_keeps_handed_off_terminal() -> None:
    from command_center.models import closed_status  # pylint: disable=import-outside-toplevel

    assert closed_status(Session(session_id="x", handed_off_to="y")) is Status.HANDED_OFF
    assert closed_status(Session(session_id="x", handed_off_to="y"), done=True) is Status.DONE
    assert closed_status(Session(session_id="x")) is Status.PARKED
