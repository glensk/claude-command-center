"""tp#1681 L23: ``start-job`` hands the first prompt to ``claude`` on stdin, never in argv."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from command_center import launch_argv
from command_center.store import Store

SECRET = "HANDOVER: lease token abc, predecessor state …"


class _StopExec(Exception):
    """Raised in place of os.execvp so the test can inspect argv and stdin."""


def _start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transcript: bool = False,
) -> dict[str, object]:
    from command_center.cli import cmd_start_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # no detached sync-mirrors spawn
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-x", str(cwd), "Do the thing", prompt=SECRET)
    store.close()
    if transcript:
        proj = tmp_path / "projects" / str(cwd).replace("/", "-")
        proj.mkdir(parents=True)
        (proj / "job-x.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("command_center.cli._start_job_trust_refusal", lambda *_a, **_k: "")
    seen: dict[str, object] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        seen["argv"] = list(argv)
        info = os.fstat(0)
        seen["stdin_mode"] = info.st_mode
        seen["stdin_links"] = info.st_nlink
        seen["stdin"] = os.pread(0, info.st_size + 1, 0).decode("utf-8") if info.st_size else ""
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    before = os.fstat(0)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id="job-x"))
    after = os.fstat(0)
    seen["stdin_restored"] = (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    return seen


def test_prompt_argv_spawned_argv_carries_no_prompt_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _start(tmp_path, monkeypatch)
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--" not in argv
    assert not any("HANDOVER" in part for part in argv)
    assert str(seen["stdin"]).endswith(SECRET)  # delivered on stdin instead


def test_prompt_argv_stdin_is_private_and_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _start(tmp_path, monkeypatch)
    mode = int(str(seen["stdin_mode"]))
    assert stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600
    assert seen["stdin_links"] == 0  # no path names it: freed when claude exits
    assert seen["stdin_restored"]  # a launch that did not happen gets fd 0 back


def test_prompt_argv_resume_sends_no_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _start(tmp_path, monkeypatch, transcript=True)
    argv = seen["argv"]
    assert isinstance(argv, list) and "--resume" in argv
    assert not any("HANDOVER" in part for part in argv)
    assert "HANDOVER" not in str(seen["stdin"])


def test_prompt_argv_stdin_fd_round_trip() -> None:
    fd = launch_argv.prompt_stdin_fd("é" * 70000)  # larger than a pipe buffer
    try:
        info = os.fstat(fd)
        assert info.st_nlink == 0 and stat.S_IMODE(info.st_mode) == 0o600
        assert os.read(fd, 1 << 20).decode("utf-8") == "é" * 70000
    finally:
        os.close(fd)


def test_prompt_argv_park_now_launches_with_the_prompt_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tp#1681 L24: `ccc park` launches through start-job, so its prompt rides on stdin."""
    from command_center import accounts, park, terminal

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(accounts, "grant_registration_trust", lambda *_a, **_k: None)
    monkeypatch.setattr(accounts, "is_trusted", lambda *_a, **_k: True)  # park's --auto check
    monkeypatch.setattr(terminal, "set_tab", lambda *_a, **_k: None)
    monkeypatch.setattr("command_center.cli._start_job_trust_refusal", lambda *_a, **_k: "")
    seen: dict[str, object] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        size = os.fstat(0).st_size
        seen["argv"] = list(argv)
        seen["stdin"] = os.pread(0, size + 1, 0).decode("utf-8") if size else ""
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    args = argparse.Namespace(prompt=SECRET, now=True, editor=True, grab=False, aim="")
    with pytest.raises(_StopExec):
        park.run_park(args)
    argv = seen["argv"]
    assert isinstance(argv, list) and "--" not in argv
    assert not any("HANDOVER" in part for part in argv)
    assert str(seen["stdin"]).endswith(SECRET)
