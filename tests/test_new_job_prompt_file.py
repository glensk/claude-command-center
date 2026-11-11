"""``ccc new-job -P/--prompt-file``: the first prompt read from a private (0600) file.

A relay handoff carries the predecessor's state; passed with ``-p`` it sits in the argv
of the registering process, readable by every local user through ``ps``. ``-P`` reads it
from a file instead, and only from one nobody else can read: a wider mode, a symlink or a
file of another owner is refused before any job row exists. ``-p`` is unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from command_center import cli, launch_argv
from command_center.store import Store

HANDOFF = "## Current state\n- relay successor of abcd\n\n## First steps\n1. run -I\n"


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME (so the default store path is this test's own DB)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _file(tmp_path: Path, mode: int = 0o600, text: str = HANDOFF) -> Path:
    path = tmp_path / "handoff.md"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _new_job(repo: Path, *extra: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["new-job", "-a", "do x", "-c", str(repo), "-J", *extra])


def _drafts() -> list:
    with Store() as store:
        return [s for s in store.list_sessions() if s.draft]


def test_prompt_file_becomes_the_first_prompt(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _file(tmp_path)
    assert cli.cmd_new_job(_new_job(repo, "-P", str(path))) == 0
    sid = json.loads(capsys.readouterr().out)["session_id"]
    with Store() as store:
        job = store.get(sid)
    assert job is not None and job.prompt == HANDOFF.strip()  # stored like -p: stripped


def test_prompt_file_long_option(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _file(tmp_path)
    assert cli.cmd_new_job(_new_job(repo, "--prompt-file", str(path))) == 0
    capsys.readouterr()
    assert [d.prompt for d in _drafts()] == [HANDOFF.strip()]


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o666])
def test_prompt_file_wider_than_0600_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    path = _file(tmp_path, mode)
    assert cli.cmd_new_job(_new_job(repo, "-P", str(path))) == 1
    err = capsys.readouterr().err
    assert f"{mode:04o}" in err and "0600" in err
    assert not _drafts()  # refused before any state change


def test_prompt_file_owner_only_modes_are_accepted(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _file(tmp_path, 0o400)
    assert cli.cmd_new_job(_new_job(repo, "-P", str(path))) == 0
    capsys.readouterr()


def test_prompt_file_symlink_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = tmp_path / "link.md"
    link.symlink_to(_file(tmp_path))
    assert cli.cmd_new_job(_new_job(repo, "-P", str(link))) == 1
    assert "prompt file" in capsys.readouterr().err
    assert not _drafts()


@pytest.mark.parametrize("problem", ["missing", "empty", "directory", "binary"])
def test_prompt_file_unusable_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], problem: str
) -> None:
    target = tmp_path / "p"
    if problem == "empty":
        _file(tmp_path, text="  \n").rename(target)
    elif problem == "directory":
        target.mkdir(mode=0o700)
    elif problem == "binary":
        target.write_bytes(b"\xff\xfe\x00")
        target.chmod(0o600)
    assert cli.cmd_new_job(_new_job(repo, "-P", str(target))) == 1
    assert "prompt file" in capsys.readouterr().err
    assert not _drafts()


def test_prompt_file_and_prompt_are_exclusive(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _file(tmp_path)
    assert cli.cmd_new_job(_new_job(repo, "-P", str(path), "-p", "inline")) == 2
    assert "-P/--prompt-file" in capsys.readouterr().err
    assert not _drafts()


def test_prompt_file_inline_prompt_unchanged(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_new_job(_new_job(repo, "-p", "inline")) == 0
    capsys.readouterr()
    assert [d.prompt for d in _drafts()] == ["inline"]


def test_prompt_file_reader_names_the_mode(tmp_path: Path) -> None:
    path = _file(tmp_path, 0o644)
    with pytest.raises(launch_argv.PromptFileError, match="0644"):
        launch_argv.read_prompt_file(path)
    path.chmod(0o600)
    assert launch_argv.read_prompt_file(path) == HANDOFF
    assert os.stat(path).st_mode & 0o777 == 0o600  # reading never changes the mode
