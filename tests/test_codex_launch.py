"""Tests for the single codex launch policy (:mod:`command_center.codex_launch`).

No live Codex call anywhere: the binary is faked on ``PATH`` and ``_exec_codex`` is
monkeypatched, so these only ever exercise argv construction, the refusals, and the
session journal.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from command_center import codex_launch

_FAKE_CATALOG = [
    {"slug": "gpt-5.6-sol", "visibility": "list", "default_reasoning_level": "medium"},
]


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _git_repo(path: Path) -> Path:
    """Initialise a throwaway git work tree at *path* and return it."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _write_config(home: Path, *, hardened_rw: bool = False, mcp: tuple[str, ...] = ()) -> Path:
    """Write a minimal ``$CODEX_HOME/config.toml`` and return the home dir."""
    home.mkdir(parents=True, exist_ok=True)
    text = (
        'default_permissions = "hardened-ro"\n\n[permissions.hardened-ro]\nextends = ":read-only"\n'
    )
    if hardened_rw:
        text += '\n[permissions.hardened-rw]\nextends = ":workspace"\n'
    for name in mcp:
        text += f'\n[mcp_servers.{name}]\ncommand = "/bin/echo"\n'
    (home / "config.toml").write_text(text, encoding="utf-8")
    return home


@pytest.fixture()
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway ``CODEX_HOME`` with a read-only profile and no MCP servers."""
    home = _write_config(tmp_path / "codex-home")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture()
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``codex`` executable first on ``PATH`` (never invoked, only resolved)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    binary = bindir / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return binary


@pytest.fixture()
def delegate_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path
) -> Callable[..., list[str] | int]:
    """Run ``cmd_delegate`` with codex faked out; return its argv (or the exit code)."""
    import command_center.codex_in_claude as cic

    monkeypatch.setenv("CODEX_IN_CLAUDE_CONFIG", str(tmp_path / "cic-config.json"))
    monkeypatch.setattr(cic, "list_models", lambda **_: list(_FAKE_CATALOG))
    monkeypatch.setattr(cic, "_git_status", lambda _cwd: [])
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)

    def run(**kw: object) -> list[str] | int:
        args = argparse.Namespace(
            prompt="do x",
            write=False,
            scout=False,
            cwd=None,
            round=1,
            feedback=None,
            model=None,
            effort=None,
            timeout=600,
            idle_timeout=0,
            max_concurrent=0,  # 0 disables the flock gate (never touches the real slot dir)
            no_repo_map=True,
        )
        for key, value in kw.items():
            setattr(args, key, value)
        rc = cic.cmd_delegate(args)
        return captured["cmd"] if rc == cic.EX_OK else rc

    return run


def _engine() -> ModuleType:
    import command_center.codex_in_claude as cic

    return cic


# --------------------------------------------------------------------------- #
# resolve_codex
# --------------------------------------------------------------------------- #
def test_resolve_codex_finds_the_binary(fake_codex: Path) -> None:
    assert Path(codex_launch.resolve_codex()).resolve() == fake_codex.resolve()


def test_resolve_codex_missing_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_launch.shutil, "which", lambda _name: None)
    with pytest.raises(codex_launch.CodexMissing, match="not found on PATH"):
        codex_launch.resolve_codex()


# --------------------------------------------------------------------------- #
# resolve_workdir
# --------------------------------------------------------------------------- #
def test_resolve_workdir_explicit_git_repo(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    got = codex_launch.resolve_workdir(str(repo), write=False)
    assert got.path == repo.resolve() and got.path.is_absolute()
    assert got.skip_git_check is False and got.write is False
    assert os.fspath(got) == str(repo.resolve()) and str(got) == str(repo.resolve())


def test_resolve_workdir_implicit_cwd_inside_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "repo")
    sub = repo / "pkg"
    sub.mkdir()
    monkeypatch.chdir(sub)
    got = codex_launch.resolve_workdir(None, write=True)
    assert got.path == sub.resolve() and got.skip_git_check is False and got.write is True


def test_resolve_workdir_implicit_non_git_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(codex_launch.CodexLaunchError, match="not inside a git work tree"):
        codex_launch.resolve_workdir(None, write=False)


def test_resolve_workdir_explicit_non_git_is_accepted_with_skip_flag(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    got = codex_launch.resolve_workdir(str(plain), write=False)
    assert got.path == plain.resolve() and got.skip_git_check is True


def test_resolve_workdir_refuses_home_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _git_repo(home)  # even a git HOME is refused — it is still the whole account
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(codex_launch.CodexLaunchError, match=r"rooted at \$HOME"):
        codex_launch.resolve_workdir(str(home), write=True)


def test_resolve_workdir_refuses_an_ancestor_of_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _git_repo(tmp_path / "users")
    home = parent / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(codex_launch.CodexLaunchError, match="CONTAINS .HOME"):
        codex_launch.resolve_workdir(str(parent), write=False)


def test_resolve_workdir_refuses_a_symlink_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked path to $HOME resolves to $HOME — the guard must not be side-steppable."""
    home = _git_repo(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    link = tmp_path / "shortcut"
    link.symlink_to(home, target_is_directory=True)
    with pytest.raises(codex_launch.CodexLaunchError, match=r"rooted at \$HOME"):
        codex_launch.resolve_workdir(str(link), write=False)


def test_resolve_workdir_refuses_a_missing_or_non_directory_root(tmp_path: Path) -> None:
    with pytest.raises(codex_launch.CodexLaunchError, match="does not resolve"):
        codex_launch.resolve_workdir(str(tmp_path / "nope"), write=False)
    file = tmp_path / "a.txt"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(codex_launch.CodexLaunchError, match="is not a directory"):
        codex_launch.resolve_workdir(str(file), write=False)


def test_resolve_workdir_names_the_mode_in_its_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(codex_launch.CodexLaunchError, match="refusing a write codex run"):
        codex_launch.resolve_workdir(str(home), write=True)
    with pytest.raises(codex_launch.CodexLaunchError, match="refusing a read-only codex run"):
        codex_launch.resolve_workdir(str(home), write=False)


# --------------------------------------------------------------------------- #
# permission_args / mcp_disable_args
# --------------------------------------------------------------------------- #
def test_permission_args_read_is_the_hardened_ro_profile(codex_home: Path) -> None:
    assert codex_launch.permission_args(False) == ["-c", 'default_permissions="hardened-ro"']
    assert "-s" not in codex_launch.permission_args(False)


def test_permission_args_write_needs_a_configured_profile(tmp_path: Path) -> None:
    home = _write_config(tmp_path / "ro-only", hardened_rw=False)
    with pytest.raises(codex_launch.CodexLaunchError, match="no hardened-rw profile configured"):
        codex_launch.permission_args(True, codex_home=home)
    rw_home = _write_config(tmp_path / "rw", hardened_rw=True)
    assert codex_launch.permission_args(True, codex_home=rw_home) == [
        "-c",
        'default_permissions="hardened-rw"',
    ]


def test_permission_profiles_survives_a_missing_or_corrupt_config(tmp_path: Path) -> None:
    assert codex_launch.permission_profiles(tmp_path / "absent") == set()
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "config.toml").write_text("[[[not toml", encoding="utf-8")
    assert codex_launch.permission_profiles(broken) == set()


def test_mcp_disable_args_lists_every_configured_server(tmp_path: Path) -> None:
    assert codex_launch.mcp_disable_args(_write_config(tmp_path / "none")) == []
    home = _write_config(tmp_path / "two", mcp=("zeta", "alpha"))
    assert codex_launch.mcp_disable_args(home) == [
        "-c",
        "mcp_servers.alpha.enabled=false",
        "-c",
        "mcp_servers.zeta.enabled=false",
    ]


def test_active_codex_home_prefers_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "seat"))
    assert codex_launch.active_codex_home() == tmp_path / "seat"
    assert codex_launch.journal_path() == tmp_path / "seat" / codex_launch.JOURNAL_NAME


# --------------------------------------------------------------------------- #
# Session journal
# --------------------------------------------------------------------------- #
def test_journal_round_trip_and_permissions(codex_home: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    workdir = codex_launch.resolve_workdir(str(repo), write=False)
    record = codex_launch.record_launch("sess-1", workdir, write=False, now=1700)
    assert record is not None and record.resolved_cwd == str(repo.resolve())
    path = codex_launch.journal_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row == {
        "permission_profile": "hardened-ro",
        "resolved_cwd": str(repo.resolve()),
        "session_id": "sess-1",
        "ts": 1700,
        "write": False,
    }
    codex_launch.record_launch("sess-2", workdir, write=True, now=1800)
    got = codex_launch.read_journal()
    assert [r.session_id for r in got] == ["sess-1", "sess-2"]
    assert got[1].write is True and got[1].permission_profile == "hardened-rw"


def test_journal_ignores_blank_ids_and_corrupt_lines(codex_home: Path, tmp_path: Path) -> None:
    assert codex_launch.record_launch("  ", str(tmp_path), write=False) is None
    path = codex_launch.journal_path()
    path.write_text('{"bad json\n{"ts": 1}\n{"session_id": "ok", "ts": 5}\n', encoding="utf-8")
    assert [r.session_id for r in codex_launch.read_journal()] == ["ok"]


def test_journal_missing_file_reads_empty(codex_home: Path) -> None:
    assert codex_launch.read_journal() == []


# --------------------------------------------------------------------------- #
# resolve_resume
# --------------------------------------------------------------------------- #
def test_resolve_resume_allows_a_matching_record(codex_home: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    codex_launch.record_launch("a", str(repo), write=False, now=10)
    codex_launch.record_launch("b", str(repo), write=False, now=20)
    assert codex_launch.resolve_resume("a", write=False).session_id == "a"
    assert codex_launch.resolve_resume("last", write=False).session_id == "b"


def test_resolve_resume_refuses_unknown_blank_and_empty_journal(codex_home: Path) -> None:
    with pytest.raises(codex_launch.CodexLaunchError, match="needs a codex session id"):
        codex_launch.resolve_resume("  ", write=False)
    with pytest.raises(codex_launch.CodexLaunchError, match="no such session"):
        codex_launch.resolve_resume("last", write=False)
    with pytest.raises(codex_launch.CodexLaunchError, match="no such session"):
        codex_launch.resolve_resume("ghost", write=False)


def test_resolve_resume_refuses_a_mode_switch(codex_home: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    codex_launch.record_launch("ro", str(repo), write=False)
    with pytest.raises(codex_launch.CodexLaunchError, match="launched read-only"):
        codex_launch.resolve_resume("ro", write=True)
    codex_launch.record_launch("rw", str(repo), write=True)
    with pytest.raises(codex_launch.CodexLaunchError, match="launched write"):
        codex_launch.resolve_resume("rw", write=False)


def test_resolve_resume_refuses_a_root_that_no_longer_passes(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded root is re-validated at resume time, not trusted from the journal."""
    home = _git_repo(tmp_path / "home")
    codex_launch.record_launch("moved", str(home), write=False)
    monkeypatch.setenv("HOME", str(home))  # the old root is $HOME today → refuse
    with pytest.raises(codex_launch.CodexLaunchError, match="no longer acceptable"):
        codex_launch.resolve_resume("moved", write=False)


# --------------------------------------------------------------------------- #
# argv construction through `codex-in-claude delegate`
# --------------------------------------------------------------------------- #
def test_argv_read_run_uses_profile_and_explicit_root(
    delegate_cmd: Callable[..., list[str] | int], codex_home: Path, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path / "repo")
    cmd = delegate_cmd(cwd=str(repo))
    assert isinstance(cmd, list)
    assert Path(cmd[0]).name == "codex" and cmd[1] == "exec"
    assert 'default_permissions="hardened-ro"' in cmd
    assert cmd[cmd.index("-C") + 1] == str(repo.resolve())
    assert "--skip-git-repo-check" not in cmd  # it IS a repo
    assert "-s" not in cmd and "--sandbox" not in cmd


def test_argv_write_run_uses_the_write_profile(
    delegate_cmd: Callable[..., list[str] | int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(_write_config(tmp_path / "rw", hardened_rw=True)))
    repo = _git_repo(tmp_path / "repo")
    cmd = delegate_cmd(cwd=str(repo), write=True)
    assert isinstance(cmd, list)
    assert 'default_permissions="hardened-rw"' in cmd
    assert "workspace-write" not in cmd


def test_argv_explicit_non_git_root_adds_the_skip_flag(
    delegate_cmd: Callable[..., list[str] | int], codex_home: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    cmd = delegate_cmd(cwd=str(plain))
    assert isinstance(cmd, list)
    assert cmd[cmd.index("-C") + 1] == str(plain.resolve())
    assert "--skip-git-repo-check" in cmd


def test_argv_implicit_non_git_root_refuses(
    delegate_cmd: Callable[..., list[str] | int],
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert delegate_cmd() == _engine().EX_USAGE


def test_argv_home_and_home_ancestor_refuse(
    delegate_cmd: Callable[..., list[str] | int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(_write_config(tmp_path / "rw", hardened_rw=True)))
    parent = _git_repo(tmp_path / "users")
    home = _git_repo(parent / "user")
    monkeypatch.setenv("HOME", str(home))
    link = tmp_path / "shortcut"
    link.symlink_to(home, target_is_directory=True)
    engine = _engine()
    assert delegate_cmd(cwd=str(home), write=True) == engine.EX_USAGE
    assert delegate_cmd(cwd=str(parent)) == engine.EX_USAGE
    assert delegate_cmd(cwd=str(link)) == engine.EX_USAGE


def test_argv_missing_codex_binary_reports_its_own_code(
    delegate_cmd: Callable[..., list[str] | int],
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_launch.shutil, "which", lambda _name: None)
    assert delegate_cmd(cwd=str(_git_repo(tmp_path / "repo"))) == _engine().EX_NO_CODEX


def test_successful_run_is_journalled_for_resume(
    delegate_cmd: Callable[..., list[str] | int],
    codex_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `### SESSION` id codex prints is what a later --resume is validated against."""
    import command_center.codex_in_claude as cic

    uuid = "019ff5b3-7bea-7c80-ad5e-21cc5b7c64bd"
    repo = _git_repo(tmp_path / "repo")

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", f"session id: {uuid}\n")

    # Replaces the fixture's capturing stub, so the run is exercised for its side effect
    # (the journal write) rather than for the argv it returns.
    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    with pytest.raises(KeyError):  # nothing captured — this stub does not record argv
        delegate_cmd(cwd=str(repo))
    records = codex_launch.read_journal()
    assert [r.session_id for r in records] == [uuid]
    assert records[0].resolved_cwd == str(repo.resolve()) and records[0].write is False
    assert codex_launch.resolve_resume(uuid, write=False).session_id == uuid
