"""The CLI job lifecycle verbs: ``focus-job``, ``resume-job``, ``unlaunch`` and the
resume-aware / guarded ``start-job`` (moved from the retired ``test_mirrors.py``, tp#2266 B10).

Hermetic: a tmp ``CLAUDE_HOME`` (store + flock), a tmp ``$GIT_BASE`` and a tmp vault. No
Obsidian, no real ``ccc`` spawns (``CCC_INTERNAL``), no ``os.execvp``.
"""

from __future__ import annotations

import argparse
import os
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from futurestub import hermetic_ccc

from command_center import config, futuresync
from command_center.models import LiveSession, Session, Status, now_ms
from command_center.store import Store


@dataclass
class Env:
    store: Store
    cfg: config.Config
    git_base: Path


@pytest.fixture(name="env")
def _env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    git_base, vault = hermetic_ccc(tmp_path, monkeypatch)
    tasks = vault / "01-llm" / "ccc-tasks"
    future = tasks / "future"
    future.mkdir(parents=True)
    cfg = config.Config(
        vault_root=str(vault),
        future_dir=str(future),
        future_pad=str(tasks / "new-prompt.md"),
        aim_score_on_set=False,
        short_aim=False,
    )
    store = Store(tmp_path / "claude" / "command-center" / "state.db")
    yield Env(store, cfg, git_base)
    store.close()


def _get(env: Env, sid: str) -> Session:
    session = env.store.get(sid)
    assert session is not None
    return session


def _running_session(env: Env, aim: str, *, repo: str = "home/ccc") -> str:
    """An active (draft=0, archived=0, done=0) session with an AIM."""
    sid = str(uuid.uuid4())
    env.store.ensure(sid, cwd=str(env.git_base / repo))
    env.store.set_aim(sid, aim)
    return sid


def _mark_done(env: Env, sid: str) -> None:
    env.store.update_fields(sid, done=True, status=Status.DONE.value, done_at=now_ms())


# ---------------------------------------------------------------------------
# CLI: start-job guards (decision 13)
# ---------------------------------------------------------------------------
class _StopExec(Exception):
    """Stand-in for os.execvp so a test can inspect the built argv."""


def _no_execvp(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    captured: dict[str, list[str]] = {}

    def _cap(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        info = os.fstat(0)  # start-job's first prompt rides on stdin (tp#1681 L23)
        regular = stat.S_ISREG(info.st_mode) and not info.st_nlink
        captured["stdin"] = [os.pread(0, info.st_size, 0).decode("utf-8") if regular else ""]
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _cap)
    monkeypatch.setattr("command_center.cli.os.chdir", lambda *_a, **_k: None)
    return captured


def test_start_job_refuses_non_draft(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    env.store.ensure("live-1", cwd=str(env.git_base / "home" / "ccc"))  # not a draft
    _no_execvp(monkeypatch)  # must never be reached
    assert cmd_start_job(argparse.Namespace(session_id="live-1")) == 1
    assert "not a future job" in capsys.readouterr().err
    assert _get(env, "live-1").draft is False  # unchanged


def test_start_job_refuses_archived_draft(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Archived one")
    env.store.update_fields(sid, archived=True)
    _no_execvp(monkeypatch)
    assert cmd_start_job(argparse.Namespace(session_id=sid)) == 1
    assert "archived" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI: resume-aware + cwd-scoped transcript (decision 14)
# ---------------------------------------------------------------------------
def _make_transcript(env: Env, cwd: str, session_id: str) -> None:
    munged = cwd.replace("/", "-")
    proj = env.store.path.parents[1] / "projects" / munged  # CLAUDE_HOME/projects/<munged>
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")


def test_start_job_resumes_when_cwd_transcript_exists(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    cwd = str(env.git_base / "home" / "ccc")
    sid = "11111111-1111-1111-1111-111111111111"
    env.store.create_draft(sid, cwd, "Do the thing", prompt="run it")
    _make_transcript(env, cwd, sid)  # transcript in THIS cwd's project dir
    captured = _no_execvp(monkeypatch)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id=sid))
    # Bare resume — NO prompt argument; --model still applied, effort still explicit.
    assert captured["argv"] == [
        "claude",
        "--resume",
        sid,
        "--model",
        "claude-opus-5-5",
        "--effort",
        "xhigh",
    ]


def test_start_job_first_launch_when_transcript_only_in_other_project_dir(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    cwd = str(env.git_base / "home" / "ccc")
    sid = "22222222-2222-2222-2222-222222222222"
    env.store.create_draft(sid, cwd, "Do the thing", prompt="run it")
    # Transcript exists, but under a DIFFERENT cwd's project dir — must NOT trigger resume.
    _make_transcript(env, str(env.git_base / "sdsc" / "zoho"), sid)
    captured = _no_execvp(monkeypatch)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id=sid))
    assert captured["argv"] == [
        "claude",
        "--model",
        "claude-opus-5-5",
        "--session-id",
        sid,
        "--effort",
        "xhigh",
    ]
    assert captured["stdin"] == ["run it"]  # the first prompt rides on stdin (tp#1681 L23)


# ---------------------------------------------------------------------------
# CLI: focus-job live check (decision 11)
# ---------------------------------------------------------------------------
class _FakeAdapter:
    def __init__(self, live_ids: list[str], *, transcript: bool = True) -> None:
        self._ids = live_ids
        self._transcript = transcript

    def discover(self) -> list[LiveSession]:
        return [LiveSession(pid=1, session_id=i, cwd="/x", alive=True) for i in self._ids]

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        del config_dir  # the adapter signature; the fake ignores it
        return Path(cwd) / f"{session_id}.jsonl" if self._transcript else None


def test_focus_job_refuses_when_not_live(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_focus_job

    env.store.ensure("s1", cwd="/x")
    env.store.update_fields("s1", iterm_session_id="w0t1p0:UUID")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # nothing live
    assert cmd_focus_job(argparse.Namespace(session_id="s1")) == 1
    assert "not live" in capsys.readouterr().err


def test_focus_job_focuses_live_tab(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_focus_job

    env.store.ensure("s1", cwd="/x")
    env.store.update_fields("s1", iterm_session_id="w0t1p0:UUID")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter(["s1"]))
    focused: dict[str, str] = {}

    def _focus(sid: str) -> bool:
        focused["sid"] = sid
        return True

    monkeypatch.setattr("command_center.terminal.focus_iterm_session", _focus)
    assert cmd_focus_job(argparse.Namespace(session_id="s1")) == 0
    assert focused["sid"] == "w0t1p0:UUID"
    assert "focused" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI: resume-job — parked → new tab, live → focus
# ---------------------------------------------------------------------------
def test_resume_job_parked_with_transcript_opens_new_tab(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = _running_session(env, "Resume me")  # active (not draft), process gone → parked
    cwd = _get(env, sid).cwd
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # not live
    resumed: dict[str, str] = {}

    def _resume(c: str, s: str, config_dir: str = "", **_: object) -> bool:
        del config_dir
        resumed["cwd"], resumed["sid"] = c, s
        return True

    monkeypatch.setattr("command_center.terminal.resume_in_new_tab", _resume)
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 0
    assert resumed == {"cwd": cwd, "sid": sid}
    assert "resuming in a new tab" in capsys.readouterr().out


def test_resume_job_live_focuses_tab_without_resuming(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = _running_session(env, "Live one")
    env.store.update_fields(sid, iterm_session_id="w0t1p0:UUID")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([sid]))  # live
    focused: dict[str, str] = {}
    resumed: dict[str, str] = {}

    def _focus(iterm_id: str) -> bool:
        focused["iterm"] = iterm_id
        return True

    def _resume(c: str, s: str, config_dir: str = "", **_: object) -> bool:
        del c, config_dir
        resumed["called"] = s
        return True

    monkeypatch.setattr("command_center.terminal.focus_iterm_session", _focus)
    monkeypatch.setattr("command_center.terminal.resume_in_new_tab", _resume)
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 0
    assert focused["iterm"] == "w0t1p0:UUID"
    assert not resumed  # never opened a second REPL
    assert "focused live tab" in capsys.readouterr().out


def test_resume_job_parked_without_transcript_refuses(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = _running_session(env, "No transcript")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([], transcript=False))
    resumed: dict[str, str] = {}
    monkeypatch.setattr(
        "command_center.terminal.resume_in_new_tab",
        lambda c, s, config_dir="", **_: resumed.setdefault("called", s) or True,
    )
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 1
    assert not resumed  # never tried to open a tab
    assert "no recorded conversation" in capsys.readouterr().err


def test_resume_job_refuses_draft(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Future job")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 1
    assert "FUTURE job" in capsys.readouterr().err


@pytest.mark.usefixtures("env")
def test_resume_job_unknown_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))
    assert cmd_resume_job(argparse.Namespace(session_id="nope")) == 1
    assert "no such session" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI: unlaunch guards + provenance (decision 12)
# ---------------------------------------------------------------------------
def test_unlaunch_refuses_when_live(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = _running_session(env, "Live one")
    env.store.update_fields(sid, future_file="01-llm/ccc-tasks/future/home/ccc/x.md")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([sid]))  # live
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1
    assert "still live" in capsys.readouterr().err
    assert _get(env, sid).draft is False


def test_unlaunch_refuses_without_provenance(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = _running_session(env, "No provenance")  # no future_file, no archive file
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # not live
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1
    assert "provenance" in capsys.readouterr().err
    assert _get(env, sid).draft is False


def test_unlaunch_refuses_when_done(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = _running_session(env, "Done one")
    env.store.update_fields(sid, future_file="01-llm/ccc-tasks/future/home/ccc/x.md")
    _mark_done(env, sid)
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1
    assert "done" in capsys.readouterr().err


def test_unlaunch_returns_launched_job_to_future(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.cli import cmd_unlaunch

    env.cfg.future_files = True  # the job-file lifecycle (tp#2266 gate)
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    # A launched draft: created, file exported, then promoted + archived (what start-job does).
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Relaunch me")
    futuresync.run_sync(env.store, env.cfg)  # exports the future file
    session = env.store.get(sid)
    env.store.clear_draft(sid)
    futuresync.archive_file(env.store, env.cfg, session, "launched")  # → _archive/

    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # not live
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 0

    restored = _get(env, sid)
    assert restored.draft is True
    assert restored.status == Status.PARKED.value
    assert restored.future_file is not None
    live_file = futuresync._abs_path(env.cfg, restored.future_file)
    assert live_file.exists() and "_archive" not in str(live_file)  # moved back out of archive
