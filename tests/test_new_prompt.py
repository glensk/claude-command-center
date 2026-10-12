"""Tests for Phase 3a capture + lifecycle wiring (``new-prompt`` + start/done archiving).

Hermetic: a tmp ``CLAUDE_HOME`` (store + flock + sidecar), a tmp ``$GIT_BASE`` and a tmp
vault. ``config.load_config`` is monkeypatched to the tmp-pathed cfg so the CLI handlers
resolve the same paths the setup uses; ``CCC_INTERNAL`` suppresses detached ``ccc`` spawns.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from futurestub import FutureEnv, future_env

from command_center import cli, config, futuresync
from command_center.future_files import display_hash, parse_job_file, serialize
from command_center.store import Store


@dataclass
class Env(FutureEnv):
    db: Path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    fe = future_env(tmp_path, monkeypatch, future_files=True)
    cfg = fe.cfg
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    db = tmp_path / "claude" / "command-center" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    yield Env(**vars(fe), db=db)


def _new_draft_with_file(
    env: Env, aim: str, *, repo: str = "home/ccc", prompt: str | None = None
) -> str:
    """Create a draft row and export its canonical file (a synced, file-mirrored draft)."""
    session_id = str(uuid.uuid4())
    with Store(env.db) as store:
        store.create_draft(session_id, str(env.git_base / repo), aim, prompt=prompt)
        futuresync.run_sync(store, env.cfg)
    return session_id


def _file_of(env: Env, sid: str) -> Path:
    """Absolute path of a draft's mirror file (asserts the row has one)."""
    with Store(env.db) as store:
        session = store.get(sid)
    assert session is not None and session.future_file
    return futuresync._abs_path(env.cfg, session.future_file)


# ---------------------------------------------------------------------------
# new-prompt
# ---------------------------------------------------------------------------
def test_new_prompt_creates_valid_draft_file(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_new_prompt(argparse.Namespace(repo=None, open=False)) == 0
    files = list(env.future_dir.glob("new-job-*.md"))
    assert len(files) == 1

    job = parse_job_file(files[0].read_text(encoding="utf-8"))
    assert job.status == "draft"
    assert job.aim == ""
    assert job.prompt is None
    assert job.repo == ""
    assert uuid.UUID(job.session_id)  # a real UUID identity
    assert files[0].name == f"new-job-{display_hash(job.session_id)}.md"
    assert "capture file created" in capsys.readouterr().out


def test_new_prompt_repo_flag_places_under_cat_repo(env: Env) -> None:
    assert cli.cmd_new_prompt(argparse.Namespace(repo="home/ccc", open=False)) == 0
    files = list((env.future_dir / "home" / "ccc").glob("new-job-*.md"))
    assert len(files) == 1
    assert parse_job_file(files[0].read_text(encoding="utf-8")).repo == "home/ccc"


def test_new_prompt_creates_pad_when_missing(env: Env) -> None:
    assert not env.pad.exists()
    assert cli.cmd_new_prompt(argparse.Namespace(repo=None, open=False)) == 0
    assert env.pad.exists()
    padjob = parse_job_file(env.pad.read_text(encoding="utf-8"))
    assert padjob.status == "draft" and padjob.aim == "" and padjob.session_id == ""


def test_new_prompt_regenerates_on_hash_collision(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = str(uuid.uuid4())
    (env.future_dir / f"new-job-{display_hash(existing)}.md").write_text(
        serialize(session_id=existing, aim="already here", status="draft", repo=""),
        encoding="utf-8",
    )
    fresh = str(uuid.uuid4())
    while display_hash(fresh) == display_hash(existing):
        fresh = str(uuid.uuid4())
    # uuid4 first returns the colliding id (must be skipped), then a unique one.
    seq = iter([uuid.UUID(existing), uuid.UUID(fresh)])
    monkeypatch.setattr("uuid.uuid4", lambda: next(seq))

    assert cli.cmd_new_prompt(argparse.Namespace(repo=None, open=False)) == 0
    created = env.future_dir / f"new-job-{display_hash(fresh)}.md"
    assert created.exists()  # the fresh, non-colliding hash was used
    assert parse_job_file(created.read_text(encoding="utf-8")).session_id == fresh


# ---------------------------------------------------------------------------
# start-job launch safety
# ---------------------------------------------------------------------------
def test_start_job_imports_edits_archives_and_clears(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _new_draft_with_file(env, "Ship the widget", prompt="old prompt")
    file_path = _file_of(env, sid)

    # A last-minute file edit just before launch: change the prompt.
    file_path.write_text(
        file_path.read_text(encoding="utf-8").replace("old prompt", "brand new prompt"),
        encoding="utf-8",
    )

    captured: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execvp", lambda _prog, argv: captured.append(argv))
    monkeypatch.setattr(cli.os, "chdir", lambda _p: None)  # don't move the test's cwd

    assert cli.cmd_start_job(argparse.Namespace(session_id=sid)) == 0
    # Targeted import ran → the edited prompt reached the launch argv AND the DB.
    assert captured and any("brand new prompt" in a for a in captured[0])
    with Store(env.db) as store:
        session = store.get(sid)
    assert session is not None and session.draft is False and session.prompt == "brand new prompt"

    archived = list((env.future_dir / "_archive").glob("*.md"))
    assert len(archived) == 1
    assert 'status: "launched"' in archived[0].read_text(encoding="utf-8")
    assert not file_path.exists()  # moved out of the live scan


def test_start_job_execvp_failure_restores_draft_and_file(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = _new_draft_with_file(env, "Ship the widget", prompt="the prompt")
    original = _file_of(env, sid)
    assert original.exists()

    def boom(_prog: str, _argv: list[str]) -> None:
        raise OSError("no claude on PATH")

    monkeypatch.setattr(cli.os, "execvp", boom)
    monkeypatch.setattr(cli.os, "chdir", lambda _p: None)

    assert cli.cmd_start_job(argparse.Namespace(session_id=sid)) == 1
    assert "could not launch" in capsys.readouterr().err

    restored = _file_of(env, sid)
    with Store(env.db) as store:
        session = store.get(sid)
    assert session is not None and session.draft is True  # promotion undone
    assert restored == original  # file moved back to its canonical live path
    assert restored.exists()
    assert 'status: "registered"' in restored.read_text(encoding="utf-8")
    assert not list((env.future_dir / "_archive").glob("*.md"))  # nothing left archived


# ---------------------------------------------------------------------------
# mark-done on a filed draft
# ---------------------------------------------------------------------------
def test_mark_done_on_filed_draft_archives_row_and_file(env: Env) -> None:
    sid = _new_draft_with_file(env, "Wrap it up")
    assert cli.cmd_mark_done(argparse.Namespace(session=sid, undo=False)) == 0

    with Store(env.db) as store:
        session = store.get(sid)  # get() (unlike list_sessions) still returns archived rows
    assert session is not None and session.archived is True and session.done is True

    archived = list((env.future_dir / "_archive").glob("*.md"))
    assert len(archived) == 1
    assert 'status: "archived"' in archived[0].read_text(encoding="utf-8")


def test_mark_done_undo_on_draft_clears_archived_and_restores_file(env: Env) -> None:
    sid = _new_draft_with_file(env, "Wrap it up")
    assert cli.cmd_mark_done(argparse.Namespace(session=sid, undo=False)) == 0
    with Store(env.db) as store:
        assert store.get(sid).archived is True  # type: ignore[union-attr]

    # Undo: clears archived AND re-exports the file into the live future folder.
    assert cli.cmd_mark_done(argparse.Namespace(session=sid, undo=True)) == 0
    with Store(env.db) as store:
        session = store.get(sid)
    assert session is not None
    assert session.archived is False and session.done is False and session.draft is True
    assert session.future_file
    live = futuresync._abs_path(env.cfg, session.future_file)
    assert live.exists() and "_archive" not in str(live)
    assert 'status: "registered"' in live.read_text(encoding="utf-8")
    assert not list((env.future_dir / "_archive").glob("*.md"))  # archived copy removed


# ---------------------------------------------------------------------------
# pad self-heal
# ---------------------------------------------------------------------------
def test_run_sync_recreates_a_deleted_pad(env: Env) -> None:
    assert not env.pad.exists()
    with Store(env.db) as store:
        futuresync.run_sync(store, env.cfg)  # full pass self-heals the missing pad
    assert env.pad.exists()
    assert parse_job_file(env.pad.read_text(encoding="utf-8")).status == "draft"
