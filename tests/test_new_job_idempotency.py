"""Idempotent job creation (``ccc new-job -K``) and the machine-readable JSON contract.

The consumer is an automation that retries: it must be able to register a job twice (a
CI re-run, a crashed dispatcher) and get the SAME job back, and it must be able to read
the answer without scraping the human table. Both halves are contracts other repos are
written against, so they are pinned here field by field.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from command_center import cli
from command_center.store import Store


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME (so the default store path is this test's own DB)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _new_job(repo: Path, **kw: object) -> argparse.Namespace:
    args = cli.build_parser().parse_args(["new-job", "-a", "do x", "-c", str(repo)])
    for key, value in kw.items():
        setattr(args, key, value)
    return args


# --------------------------------------------------------------------------- #
# create-or-retrieve
# --------------------------------------------------------------------------- #
def test_reusing_a_key_returns_the_same_job(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k1", json=True)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] is True
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k1", json=True)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] is False
    assert second["session_id"] == first["session_id"]
    with Store() as store:
        drafts = [s for s in store.list_sessions() if s.draft]
        assert len(drafts) == 1 and drafts[0].idempotency_key == "k1"


def test_a_key_reused_for_different_work_exits_2(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k1")) == 0
    capsys.readouterr()
    other = home.parent / "other"
    other.mkdir()
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k1", cwd=str(other))) == 2
    assert "different parameters" in capsys.readouterr().err
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k1", aim="something else")) == 2
    assert "aim" in capsys.readouterr().err
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k1", no_codex=True)) == 2
    assert "no_codex" in capsys.readouterr().err
    with Store() as store:  # no second row was ever created
        assert len([s for s in store.list_sessions() if s.draft]) == 1


def test_a_rejected_registration_never_claims_the_key(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-flight failure (bad --start-date) must leave the key free for the retry."""
    bad = _new_job(home, idempotency_key="k2", start_date="not-a-date")
    assert cli.cmd_new_job(bad) == 1
    capsys.readouterr()
    with Store() as store:
        assert store.list_sessions() == []  # no placeholder row was left behind
    assert cli.cmd_new_job(_new_job(home, idempotency_key="k2", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_keyless_jobs_are_never_deduplicated(home: Path) -> None:
    assert cli.cmd_new_job(_new_job(home)) == 0
    assert cli.cmd_new_job(_new_job(home)) == 0
    with Store() as store:
        drafts = [s for s in store.list_sessions() if s.draft]
        assert len(drafts) == 2
        assert all(s.idempotency_key is None for s in drafts)  # NULL, so UNIQUE allows many


def test_concurrent_creators_with_one_key_produce_one_row(home: Path) -> None:
    """Two threads racing on the same key: exactly one creates, both get the same id."""
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def creator(index: int) -> None:
        start.wait(timeout=10)
        with Store() as store:  # its own connection: sqlite3 objects are per-thread
            session_id, created = store.claim_idempotency_key("race", f"cand-{index}", str(home))
            if created:
                store.create_draft(session_id, str(home), "do x", idempotency_key="race")
        with lock:
            results.append((session_id, created))

    threads = [threading.Thread(target=creator, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 4
    assert sum(1 for _sid, created in results if created) == 1  # exactly one creator
    assert len({sid for sid, _created in results}) == 1  # everyone agrees on the id
    with Store() as store:
        rows = [s for s in store.list_sessions() if s.idempotency_key == "race"]
        assert len(rows) == 1 and rows[0].aim == "do x"


def test_the_unique_index_is_enforced_at_the_sql_level(tmp_path: Path) -> None:
    with Store(tmp_path / "c.db") as store:
        store.create_draft("a", "/r", "one", idempotency_key="dup")
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO sessions (session_id, idempotency_key) VALUES ('b', 'dup')"
            )


# --------------------------------------------------------------------------- #
# migration from the previous schema version
# --------------------------------------------------------------------------- #
def test_existing_db_migrates_and_gains_the_unique_index(tmp_path: Path) -> None:
    """An older DB (no idempotency_key / no_codex) ALTERs in place and gets the index."""
    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy = store_mod._SCHEMA.replace("    idempotency_key   TEXT,\n", "").replace(
        "    no_codex          INTEGER NOT NULL DEFAULT 0,\n", ""
    )
    conn = sqlite3.connect(db)
    conn.executescript(legacy)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old2', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(sessions)")}
        assert {"idempotency_key", "no_codex"} <= columns
        # Both legacy rows kept their NULL key — the partial unique index tolerates that.
        assert [s.idempotency_key for s in store.list_sessions()] == [None, None]
        store.update_fields("old", idempotency_key="k")
        with pytest.raises(sqlite3.IntegrityError):
            store.update_fields("old2", idempotency_key="k")


# --------------------------------------------------------------------------- #
# the JSON contract
# --------------------------------------------------------------------------- #
def test_new_job_json_is_one_line_with_exactly_the_agreed_keys(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_new_job(_new_job(home, json=True, no_codex=True)) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1  # nothing else on stdout
    doc = json.loads(out)
    assert set(doc) == {"version", "session_id", "created", "account", "no_codex"}
    assert doc["version"] == 1 and doc["created"] is True and doc["no_codex"] is True
    assert isinstance(doc["account"], str) and doc["account"]


def test_new_job_json_stays_silent_for_an_armed_job(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--at-reset chatter ("armed: …") must not pollute the JSON document either."""
    from command_center import park

    monkeypatch.setattr(park, "fire_time", lambda *_a, **_k: 2_000_000_000)
    args = _new_job(home, json=True, at_reset=True, fire_window="five_hour")
    assert cli.cmd_new_job(args) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out)["created"] is True


def test_jobs_json_lists_every_draft_with_the_agreed_fields(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store() as store:
        store.create_draft("j-nc", str(home), "banned", no_codex=True, idempotency_key="key-1")
        store.create_draft("j-ok", str(home), "normal")
    assert cli.cmd_jobs(argparse.Namespace(json=True)) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    doc = json.loads(out)
    assert set(doc) == {"version", "jobs"} and doc["version"] == 1
    by_id = {job["session_id"]: job for job in doc["jobs"]}
    assert set(by_id) == {"j-nc", "j-ok"}
    assert set(by_id["j-nc"]) == {
        "session_id",
        "cwd",
        "aim",
        "draft",
        "archived",
        "created_at",
        "account",
        "no_codex",
        "idempotency_key",
    }
    assert by_id["j-nc"]["no_codex"] is True and by_id["j-nc"]["idempotency_key"] == "key-1"
    assert by_id["j-ok"]["no_codex"] is False and by_id["j-ok"]["idempotency_key"] is None
    assert by_id["j-ok"]["draft"] is True and by_id["j-ok"]["archived"] is False
    assert by_id["j-ok"]["cwd"] == str(home) and by_id["j-ok"]["aim"] == "normal"


def test_jobs_json_on_an_empty_store_is_still_a_document(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_jobs(argparse.Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == {"version": 1, "jobs": []}


def test_json_short_options_do_not_collide(home: Path) -> None:
    """`-j` is --job-type on new-job (so JSON takes -J there) and --json on jobs."""
    parser = cli.build_parser()
    assert parser.parse_args(["new-job", "-a", "x", "-j", "codex"]).job_type == "codex"
    assert parser.parse_args(["new-job", "-a", "x", "-J"]).json is True
    assert parser.parse_args(["jobs", "-j"]).json is True
    assert parser.parse_args(["jobs"]).json is False
