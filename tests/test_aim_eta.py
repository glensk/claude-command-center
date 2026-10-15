"""The session ETA kept BESIDE the AIM (``ccc set-aim-eta``, ``Store.set_aim_eta``).

The ETA is not a second done-condition: setting, overwriting or clearing it must leave the
AIM, its history, the short label and the DONE verdict exactly as they were.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from command_center import cli
from command_center.store import Store


def _db(home: Path) -> Path:
    return home / "command-center" / "state.db"


def _aim_state(store: Store, sid: str) -> tuple[object, ...]:
    session = store.get(sid)
    assert session is not None
    history = [(h.aim, h.score, h.short_aim) for h in store.list_aim_history(sid)]
    return (
        session.aim,
        session.short_aim,
        session.aim_score,
        session.aim_met,
        session.aim_met_reason,
        session.aim_prev,
        session.aim_changed_at,
        tuple(history),
    )


@pytest.fixture(name="home")
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _db(tmp_path).parent.mkdir(parents=True)
    with Store(_db(tmp_path)) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "first aim")
        store.set_aim("s1", "pytest -q is green")
        store.set_short_aim("s1", "tests green")
        store.set_aim_met("s1", True, "all green", 123)
    return tmp_path


def test_store_round_trip_leaves_the_aim_untouched(home: Path) -> None:
    with Store(_db(home)) as store:
        before = _aim_state(store, "s1")
        assert store.set_aim_eta("s1", "17:30") is True
        session = store.get("s1")
        assert session is not None and session.aim_eta == "17:30"
        assert isinstance(session.aim_eta_set_at, int) and session.aim_eta_set_at > 0
        assert _aim_state(store, "s1") == before

        assert store.set_aim_eta("s1", "  45 min  ") is True  # overwrite, trimmed
        session = store.get("s1")
        assert session is not None and session.aim_eta == "45 min"
        assert _aim_state(store, "s1") == before

        assert store.set_aim_eta("s1", None) is True  # clear
        session = store.get("s1")
        assert session is not None
        assert session.aim_eta is None and session.aim_eta_set_at is None
        assert _aim_state(store, "s1") == before

        assert store.set_aim_eta("nope", "17:30") is False  # unknown session
        assert store.get("nope") is None


def test_migration_adds_the_columns_to_an_old_db(tmp_path: Path) -> None:
    path = _db(tmp_path)
    path.parent.mkdir(parents=True)
    with Store(path) as store:
        store.ensure("old", cwd="/repo")
        store.set_aim("old", "an aim from before the ETA")
    # Simulate a DB written by a build without the columns.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE sessions DROP COLUMN aim_eta")
    conn.execute("ALTER TABLE sessions DROP COLUMN aim_eta_set_at")
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert "aim_eta" not in cols and "aim_eta_set_at" not in cols

    with Store(path) as store:
        session = store.get("old")
        assert session is not None
        assert session.aim == "an aim from before the ETA"
        assert session.aim_eta is None and session.aim_eta_set_at is None
        assert store.set_aim_eta("old", "tomorrow") is True
        session = store.get("old")
        assert session is not None and session.aim_eta == "tomorrow"


def test_cli_set_get_clear(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["set-aim-eta", "-g", "--session", "s1"]) == 0
    assert capsys.readouterr().out == "\n"  # none stored → empty line

    assert cli.main(["set-aim-eta", "2026-10-20", "-s", "s1"]) == 0
    assert "aim eta set for s1" in capsys.readouterr().out
    assert cli.main(["set-aim-eta", "-g", "-s", "s1"]) == 0
    assert capsys.readouterr().out == "2026-10-20\n"

    assert cli.main(["set-aim-eta", "?", "-s", "s1"]) == 0
    capsys.readouterr()
    assert cli.main(["set-aim-eta", "--get", "-s", "s1"]) == 0
    assert capsys.readouterr().out == "?\n"

    assert cli.main(["set-aim-eta", "-c", "-s", "s1"]) == 0
    assert "aim eta cleared for s1" in capsys.readouterr().out
    assert cli.main(["set-aim-eta", "-g", "-s", "s1"]) == 0
    assert capsys.readouterr().out == "\n"

    with Store(_db(home)) as store:
        session = store.get("s1")
    assert session is not None and session.aim == "pytest -q is green"


@pytest.mark.usefixtures("home")
def test_cli_resolves_the_current_session_from_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert cli.main(["set-aim-eta", "45 min"]) == 0
    capsys.readouterr()
    assert cli.main(["set-aim-eta", "-g"]) == 0
    assert capsys.readouterr().out == "45 min\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["set-aim-eta", "-s", "s1"],  # nothing to do
        ["set-aim-eta", "17:30", "-c", "-s", "s1"],  # ETA and clear
        ["set-aim-eta", "17:30", "-g", "-s", "s1"],  # ETA and get
        ["set-aim-eta", "-c", "-g", "-s", "s1"],  # clear and get
        ["set-aim-eta", "x" * 41, "-s", "s1"],  # too long
        ["set-aim-eta", "17:30\nrm -rf", "-s", "s1"],  # line break
        ["set-aim-eta", "17:30\x1b[2J", "-s", "s1"],  # control character
        ["set-aim-eta", "   ", "-s", "s1"],  # blank
    ],
)
def test_cli_usage_refusals_exit_2(
    home: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 2
    assert "error:" in capsys.readouterr().err
    with Store(_db(home)) as store:
        session = store.get("s1")
    assert session is not None and session.aim_eta is None


@pytest.mark.usefixtures("home")
def test_cli_accepts_exactly_40_characters() -> None:
    assert cli.main(["set-aim-eta", "x" * 40, "-s", "s1"]) == 0


def test_cli_unknown_session_exit_1(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["set-aim-eta", "17:30", "-s", "ghost"]) == 1
    assert "unknown session" in capsys.readouterr().err
    assert cli.main(["set-aim-eta", "-g", "-s", "ghost"]) == 1
    with Store(_db(home)) as store:
        assert store.get("ghost") is None  # never created as a side effect


def test_jobs_json_carries_the_eta(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with Store(_db(home)) as store:
        store.ensure("d1", cwd="/repo")
        store.update_fields("d1", draft=True, aim="a parked job")
        store.set_aim_eta("d1", "tomorrow")
        store.ensure("d2", cwd="/repo")
        store.update_fields("d2", draft=True, aim="no eta")
    assert cli.main(["jobs", "-j"]) == 0
    jobs = {j["session_id"]: j for j in json.loads(capsys.readouterr().out)["jobs"]}
    assert jobs["d1"]["aim_eta"] == "tomorrow"
    assert isinstance(jobs["d1"]["aim_eta_set_at"], int)
    assert jobs["d2"]["aim_eta"] is None and jobs["d2"]["aim_eta_set_at"] is None
