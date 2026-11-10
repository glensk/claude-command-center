"""``ccc user-aims [-j] [-d N] [-s SID …]`` — every session's hand-set AIM (tp#1991 B3).

Fixture stores only: each test builds ``state.db`` at ``config.db_path()`` (pinned under
``tmp_path`` by conftest), then closes it; the command reads it read-only. Placeholder ids.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from command_center import bridge_aims, bridge_cli, cli, config
from command_center.bridge_json import iso
from command_center.store import Store

DAY = 86_400_000
NOW = int(time.time() * 1000)


def _db() -> Path:
    return config.db_path()


def _session(
    store: Store,
    sid: str,
    *,
    updated: int,
    aim: str | None = None,
    done: bool = False,
    rows: tuple[tuple[str, int, str | None], ...] = (),
    source: str | None = None,
) -> None:
    """A session row updated at *updated* with *rows* ``(aim, created_at, set_by)``."""
    store.ensure(sid, cwd="/repo")
    store.conn.execute(
        "UPDATE sessions SET aim = ?, done = ?, updated_at = ?, handed_off_from = ? "
        "WHERE session_id = ?",
        (aim, int(done), updated, source, sid),
    )
    store.conn.executemany(
        "INSERT INTO aim_history (session_id, aim, created_at, set_by) VALUES (?, ?, ?, ?)",
        [(sid, text, at, by) for text, at, by in rows],
    )
    store.conn.commit()


@pytest.fixture(name="db")
def _fixture_db() -> Path:
    store = Store(_db())
    try:
        _session(
            store,
            "sess-user",
            updated=NOW - 1 * DAY,
            aim="tool text now",
            rows=(
                ("first launch aim", NOW - 9 * DAY, "launch"),
                ("hand aim\nsecond line", NOW - 5 * DAY, "user"),
                ("tool text now", NOW - 2 * DAY, "tool"),
            ),
        )
        _session(
            store,
            "sess-relay",
            updated=NOW - 3 * DAY // 2,
            aim="carried aim",
            done=True,
            rows=(("carried aim", NOW - 3 * DAY, "relay"),),
        )
        _session(store, "sess-pre", updated=NOW - 3 * DAY, aim="pre-history aim")
        _session(store, "sess-none", updated=NOW - 4 * DAY, done=True)
        _session(
            store,
            "sess-tool",
            updated=NOW - 5 * DAY,
            aim="ccc wrote this",
            rows=(("ccc wrote this", NOW - 6 * DAY, "tool"),),
        )
        _session(store, "sess-old", updated=NOW - 500 * DAY, aim="ancient", done=True)
    finally:
        store.close()
    return _db()


def _items(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["session_id"]: item for item in data["sessions"]}


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(["user-aims", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_window_filters_by_updated_at_newest_first(db: Path) -> None:
    data = bridge_aims.user_aims(db)
    assert [i["session_id"] for i in data["sessions"]] == [
        "sess-user",
        "sess-relay",
        "sess-pre",
        "sess-none",
        "sess-tool",
    ]
    assert data["as_of"]
    short = bridge_aims.user_aims(db, days=2)
    assert [i["session_id"] for i in short["sessions"]] == ["sess-user", "sess-relay"]
    wide = bridge_aims.user_aims(db, days=3650)
    assert wide["sessions"][-1]["session_id"] == "sess-old"


def test_fields_per_tier(db: Path) -> None:
    items = _items(bridge_aims.user_aims(db))
    assert items["sess-user"] == {
        "session_id": "sess-user",
        "known": True,
        "user_aim": "hand aim",
        "user_aim_by": "user",
        "user_aim_tier": "user",
        "user_aim_at": iso(NOW - 5 * DAY),
        "current_aim": "tool text now",
        "done": False,
        "updated_at": iso(NOW - 1 * DAY),
    }
    relay = items["sess-relay"]
    assert (relay["user_aim"], relay["user_aim_by"], relay["user_aim_tier"]) == (
        "carried aim",
        "relay",
        "unstamped",
    )
    pre = items["sess-pre"]
    assert (pre["user_aim"], pre["user_aim_by"], pre["user_aim_tier"], pre["user_aim_at"]) == (
        "pre-history aim",
        None,
        "unstamped",
        None,
    )
    for sid in ("sess-none", "sess-tool"):  # no AIM / only tool rows: listed with nulls
        item = items[sid]
        assert item["known"] is True
        assert item["user_aim"] is None
        assert item["user_aim_by"] is None
        assert item["user_aim_tier"] is None
        assert item["user_aim_at"] is None
    assert items["sess-tool"]["current_aim"] == "ccc wrote this"
    assert items["sess-none"]["current_aim"] is None


def test_done_is_a_bool_from_0_and_1(db: Path) -> None:
    items = _items(bridge_aims.user_aims(db))
    assert items["sess-user"]["done"] is False
    assert items["sess-relay"]["done"] is True
    assert items["sess-none"]["done"] is True


def test_sessions_override_the_window_and_unknown_ids_are_null(db: Path) -> None:
    data = bridge_aims.user_aims(db, days=1, sessions=["missing-id", "sess-old", "sess-user"])
    assert [i["session_id"] for i in data["sessions"]] == ["sess-user", "sess-old", "missing-id"]
    assert data["sessions"][1]["user_aim"] == "ancient"
    unknown = data["sessions"][2]
    assert unknown["known"] is False
    assert all(v is None for k, v in unknown.items() if k not in ("session_id", "known"))


def test_201_sessions_is_a_usage_error_with_an_envelope(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert db.is_file()
    argv: list[str] = []
    for n in range(bridge_aims.SESSIONS_MAX + 1):
        argv += ["-s", f"sess-{n:04d}"]
    code, out, _err = _run(capsys, "-j", *argv)
    assert code == 2
    env = json.loads(out)
    assert env["schema_version"] == 1
    assert env["ok"] is False
    assert env["error"]["code"] == "usage"
    code, out, _err = _run(capsys, "-j", *argv[:-2])
    assert code == 0
    assert len(json.loads(out)["data"]["sessions"]) == bridge_aims.SESSIONS_MAX


def test_201_sessions_without_json_exits_2(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert db.is_file()
    argv: list[str] = []
    for n in range(bridge_aims.SESSIONS_MAX + 1):
        argv += ["-s", f"sess-{n:04d}"]
    code, out, err = _run(capsys, *argv)
    assert code == 2
    assert out == ""
    assert "❌ usage" in err


@pytest.mark.parametrize("days", ["0", "3651", "x"])
def test_days_out_of_range_is_a_usage_envelope(
    db: Path, capsys: pytest.CaptureFixture[str], days: str
) -> None:
    assert db.is_file()
    with pytest.raises(SystemExit) as exc:  # an argv error: the envelope, then exit 2
        cli.main(["user-aims", "-j", "-d", days])
    assert exc.value.code == 2
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error"]["code"] == "usage"


def test_cli_json_envelope(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert db.is_file()
    code, out, err = _run(capsys, "-j", "-s", "sess-user")
    assert code == 0
    assert err == ""
    env = json.loads(out)
    assert env["ok"] is True
    assert env["error"] is None
    assert [i["user_aim"] for i in env["data"]["sessions"]] == ["hand aim"]


def test_connection_is_read_only_and_leaves_the_file_untouched(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uris: list[str] = []
    real = sqlite3.connect

    def spy(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        uris.append(database)
        assert kwargs.get("uri") is True
        return real(database, *args, **kwargs)

    for suffix in ("-wal", "-shm"):  # a cleanly closed store: no -shm beside it
        assert not db.with_name(db.name + suffix).exists()
    before = (db.stat().st_mtime_ns, db.stat().st_size, db.read_bytes())
    monkeypatch.setattr(bridge_aims.sqlite3, "connect", spy)
    data = bridge_aims.user_aims(db)
    assert data["sessions"]
    assert len(uris) == 1
    assert uris[0].startswith("file:")
    assert uris[0].endswith("?mode=ro")
    assert (db.stat().st_mtime_ns, db.stat().st_size, db.read_bytes()) == before


def test_read_only_connection_refuses_writes(db: Path) -> None:
    conn = bridge_aims.connect_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE sessions SET done = 1")
    finally:
        conn.close()


def test_old_schema_without_set_by_reads_unstamped(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE aim_history DROP COLUMN set_by")
    conn.commit()
    conn.close()
    items = _items(bridge_aims.user_aims(db))
    assert all(item["user_aim_by"] is None for item in items.values())
    # every row reads unstamped: the newest row answers, tier unstamped
    assert items["sess-user"]["user_aim"] == "tool text now"
    assert items["sess-user"]["user_aim_tier"] == "unstamped"


def test_missing_state_db_answers_empty_and_unknown(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "state.db"
    assert not bridge_aims.user_aims(missing)["sessions"]
    data = bridge_aims.user_aims(missing, sessions=["sess-x"])
    assert data["sessions"] == [bridge_aims._unknown("sess-x")]  # noqa: SLF001
    assert not missing.exists()


def test_text_output_one_line_per_session(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert db.is_file()
    code, out, _err = _run(capsys, "-s", "sess-user", "-s", "sess-relay", "-s", "missing-id")
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0].split() == ["sess-use", "user", "user", "-", "hand", "aim"]
    assert lines[1].split() == ["sess-rel", "unstamped", "relay", "done", "carried", "aim"]
    assert lines[2].split()[:2] == ["missing-", "unknown"]


def test_registered_as_a_json_command_not_a_hot_one() -> None:
    assert "user-aims" in bridge_cli.JSON_COMMANDS
    assert "user-aims" not in cli._HOT_SUBCOMMANDS  # noqa: SLF001


def test_help_lists_short_options_and_examples(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["user-aims", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for short, long in (("-j", "--json"), ("-d", "--days"), ("-s", "--session")):
        assert short in out
        assert long in out
    assert "ccc user-aims -j -d 30" in out
