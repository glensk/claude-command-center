"""Additive migrations of the voice-bridge tables ``deliveries`` and ``events``."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from command_center import bridge_store
from command_center.bridge_store import Delivery
from command_center.store import Store


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


def test_fresh_store_has_the_tables_and_indexes(tmp_path: Path) -> None:
    Store(tmp_path / "s.db").close()
    names = _tables(tmp_path / "s.db")
    assert {"deliveries", "events", "idx_deliveries_session_state", "idx_events_dedupe"} <= names
    with sqlite3.connect(tmp_path / "s.db") as conn:
        cols = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(events)")}
        idx = [r[2] for r in conn.execute("PRAGMA index_info(idx_deliveries_session_state)")]
    assert cols["cursor"] == 1  # the INTEGER PRIMARY KEY (rowid) — indexed by construction
    assert idx == ["session_id", "state"]


def test_an_old_db_gains_the_tables_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with Store(path) as store:
        store.ensure("s1", cwd="/repo")
    with sqlite3.connect(path) as conn:  # what a pre-bridge build left behind
        conn.execute("DROP TABLE deliveries")
        conn.execute("DROP TABLE events")
    assert "deliveries" not in _tables(path)
    with Store(path) as store:
        assert store.get("s1") is not None
        store.insert_delivery(Delivery(delivery_id="d1", session_id="s1"))
        assert store.get_delivery("d1") is not None


def test_columns_a_newer_build_added_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with Store(path) as store:
        store.insert_delivery(Delivery(delivery_id="d1", session_id="s1", state="accepted"))
        store.add_event("stop_failure", "s1", 5, {"error": "x"})
    with sqlite3.connect(path) as conn:  # a NEWER ccc writes columns this build lacks
        conn.execute("ALTER TABLE deliveries ADD COLUMN future_col TEXT DEFAULT 'z'")
        conn.execute("ALTER TABLE events ADD COLUMN future_col INTEGER DEFAULT 7")
        conn.execute(
            "INSERT INTO deliveries (delivery_id, session_id, state, future_col) "
            "VALUES ('d2', 's1', 'unknown', 'new')"
        )
    with Store(path) as store:
        d1, d2 = store.deliveries_for("s1")
        assert (d1.delivery_id, d2.delivery_id) == ("d1", "d2")
        assert store.update_delivery("d2", "unknown", state="failed")
        (event,) = store.events_after(0)
        assert event.detail_obj() == {"error": "x"}


def test_a_later_added_column_is_altered_in_and_the_race_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.db"
    Store(path).close()
    monkeypatch.setitem(bridge_store.ADDED_DELIVERY_COLUMNS, "extra", "TEXT NOT NULL DEFAULT ''")
    with Store(path) as store:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(deliveries)")}
        assert "extra" in cols
        store._add_column("deliveries", "extra", "TEXT")  # a peer won the race: no error


def test_concurrent_first_opens_do_not_fail(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    errors: list[BaseException] = []

    def _open() -> None:
        try:
            Store(path, check_same_thread=False).close()
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors.append(exc)

    threads = [threading.Thread(target=_open) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert {"deliveries", "events"} <= _tables(path)


def test_update_delivery_is_a_compare_and_swap(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        store.insert_delivery(Delivery(delivery_id="d1", session_id="s1", state="accepted"))
        event = ("delivery_failed", "s1", 9, {"delivery_id": "d1"}, "delivery:d1:failed")
        assert store.update_delivery("d1", "accepted", event=event, state="failed")
        assert not store.update_delivery("d1", "accepted", event=event, state="completed")
        assert [e.kind for e in store.events_after(0)] == ["delivery_failed"]
        with pytest.raises(ValueError):
            store.update_delivery("d1", "failed", bogus=1)
