"""Additive migration of the session-name columns: old readers, new writers, racing ALTERs."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from command_center import store as store_mod
from command_center.models import Session
from command_center.store import Store

# Spelled independently of store.py on purpose: the test pins the §6 column names.
NEW_COLUMNS = set(
    "canonical_name canonical_name_origin name_source_aim observed_runtime_name "
    "runtime_name_applied_at title_written title_generation title_written_at "
    "name_upgrade_tries".split()
)


def _legacy_schema() -> str:
    """The sessions schema as a build from before the name columns wrote it."""
    lines = store_mod._SCHEMA.splitlines()  # noqa: SLF001
    kept = [ln for ln in lines if ln.strip().split(" ", 1)[0] not in NEW_COLUMNS]
    text = "\n".join(kept)
    return text.replace(
        "updated_at        INTEGER NOT NULL DEFAULT 0,\n);",
        "updated_at        INTEGER NOT NULL DEFAULT 0\n);",
    )


def _columns(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    finally:
        conn.close()


def test_legacy_db_gains_the_columns_and_keeps_its_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(_legacy_schema())
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/r')")
    conn.commit()
    conn.close()
    assert not NEW_COLUMNS & _columns(db)
    with Store(db) as store:
        old = store.get("old")
        assert old is not None and old.canonical_name is None and old.title_generation == 0
        store.update_fields("old", canonical_name="voice bridge", title_generation=2)
        got = store.get("old")
        assert got is not None and got.canonical_name == "voice bridge"
    assert NEW_COLUMNS <= _columns(db)
    conn = sqlite3.connect(db)
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(sessions)")}
    conn.close()
    assert "idx_sessions_canonical_name" in indexes


def test_old_reader_tolerates_the_new_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-lived older build (whose Session lacks the fields) must still read the rows."""
    db = tmp_path / "s.db"
    with Store(db) as store:  # a NEW writer fills the columns
        store.ensure("s1", cwd="/r")
        store.update_fields("s1", canonical_name="voice bridge", title_written="🔺 x")
    # The OLD reader: its Session dataclass does not know the new fields.
    old_fields = frozenset(f.name for f in dataclasses.fields(Session) if f.name not in NEW_COLUMNS)
    monkeypatch.setattr(store_mod, "_SESSION_FIELDS", old_fields)
    with Store(db) as store:
        got = store.get("s1")
        assert got is not None and got.session_id == "s1" and got.canonical_name is None
        assert [s.session_id for s in store.list_sessions()] == ["s1"]


def test_old_writer_leaves_new_columns_untouched(tmp_path: Path) -> None:
    """An older build's whitelisted UPDATE never names the new columns, so they survive."""
    db = tmp_path / "s.db"
    with Store(db) as store:
        store.ensure("s1", cwd="/r")
        store.update_fields("s1", canonical_name="voice bridge")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET status = 'busy', updated_at = 1 WHERE session_id = 's1'")
    conn.commit()
    conn.close()
    with Store(db) as store:
        got = store.get("s1")
        assert got is not None and got.canonical_name == "voice bridge" and got.status == "busy"


def test_concurrent_column_add_is_tolerated(tmp_path: Path) -> None:
    """Two fresh builds racing the ALTER: the loser's duplicate-column error is swallowed."""
    db = tmp_path / "race.db"
    conn = sqlite3.connect(db)
    conn.executescript(_legacy_schema())
    conn.commit()
    conn.close()
    with Store(db) as first, Store(db) as second:
        # Both already migrated in __init__; re-running every ALTER is the race's loser path.
        for column, decl in Store._ADDED_COLUMNS.items():  # noqa: SLF001
            if column in NEW_COLUMNS:
                second._add_column("sessions", column, decl)  # noqa: SLF001  # pylint: disable=protected-access
        first._ensure_columns()  # noqa: SLF001  # pylint: disable=protected-access
    assert NEW_COLUMNS <= _columns(db)
