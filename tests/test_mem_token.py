"""The shared-memory attestation token on ccc-started Claude sessions and Codex runs.

Every Claude session ccc launches in-process (``start-job``/``resume``/``fire-*``, the
halted-session auto-resume) and every ``codex exec`` the runner starts must carry
``MEM_TOKEN`` + ``MEM_LAUNCHER`` in the CHILD env, minted by ``mem token -l <launcher> -t``.
A missing or failing ``mem`` must never stop the launch — it only drops the variables.
``mem`` here is a fake shell script on ``MEM_BIN`` that records its argv and env.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from conftest import SeatFixture

from command_center import mem_token
from command_center.store import Store

_TOKEN = "memtok-0123456789abcdef"  # noqa: S105  # a test fixture, not a credential
_MODEL = "gpt-5.6-sol"


class _StopExec(Exception):
    """Raised in place of os.execvp so a test can read the env the exec would inherit."""


def _fake_mem(tmp_path: Path, *, body: str) -> Path:
    """A fake ``mem`` that logs ``argv`` + ``MEM_CLIENT_TIMEOUT``, then runs *body*."""
    log = tmp_path / "mem-calls.log"
    script = tmp_path / "fake-mem"
    script.write_text(
        f'#!/bin/sh\nprintf "%s|%s\\n" "$*" "$MEM_CLIENT_TIMEOUT" >> "{log}"\n{body}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _calls(tmp_path: Path) -> list[str]:
    """Every recorded ``mem`` invocation as ``<argv>|<MEM_CLIENT_TIMEOUT>``."""
    log = tmp_path / "mem-calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.fixture(name="good_mem")
def _good_mem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``MEM_BIN`` → a fake that mints :data:`_TOKEN` (exit 0)."""
    monkeypatch.setenv("MEM_BIN", str(_fake_mem(tmp_path, body=f'echo "{_TOKEN}"')))


@pytest.fixture(name="down_mem")
def _down_mem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``MEM_BIN`` → a fake whose service is down (exit 7, nothing on stdout)."""
    body = 'echo "mem: service down" >&2; exit 7'
    monkeypatch.setenv("MEM_BIN", str(_fake_mem(tmp_path, body=body)))


# ── the helper ───────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("good_mem")
def test_mem_env_mints_with_the_launcher_and_client_timeout(tmp_path: Path) -> None:
    assert mem_token.mem_env("ccc") == {"MEM_TOKEN": _TOKEN, "MEM_LAUNCHER": "ccc"}
    assert _calls(tmp_path) == ["token -l ccc -t|3"]


@pytest.mark.usefixtures("good_mem")
def test_ambient_token_is_kept_and_nothing_is_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEM_TOKEN", "inherited")
    assert not mem_token.mem_env("ccc")
    assert not mem_token.mem_env("codex-in-claude", {"MEM_TOKEN": "x"})
    assert _calls(tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        "exit 3",  # refused
        f'echo "{_TOKEN}"; exit 7',  # non-zero even with output
        "exit 0",  # empty output
        'echo "not a token at all"',  # malformed (whitespace)
    ],
)
def test_any_mint_failure_adds_nothing(
    body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEM_BIN", str(_fake_mem(tmp_path, body=body)))
    assert not mem_token.mem_env("ccc")


def test_mint_timeout_adds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEM_BIN", str(_fake_mem(tmp_path, body="sleep 3; echo late-token-xyz")))
    monkeypatch.setattr(mem_token, "MINT_TIMEOUT_S", 0.3)
    assert not mem_token.mem_env("ccc")


def test_absent_mem_adds_nothing() -> None:
    """conftest points ``MEM_BIN`` at a missing file."""
    assert not mem_token.mem_env("ccc")


# ── a Claude launch path: ccc start-job's in-process exec ────────────────────────


def _start_job_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Run ``ccc start-job`` on a fresh draft up to its exec; the env claude would inherit."""
    from command_center import cli

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress the detached sync-mirrors spawn
    with Store(tmp_path / "command-center" / "state.db") as store:
        store.create_draft("job-mem", "/no/such/dir", "Do the thing", prompt="run it")
    seen: dict[str, dict[str, str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        assert argv[0] == "claude"
        seen["env"] = dict(os.environ)
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):  # the exec WAS reached
        cli.cmd_start_job(argparse.Namespace(session_id="job-mem", force=True, auto=False))
    return seen["env"]


@pytest.mark.usefixtures("good_mem")
def test_start_job_child_env_carries_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _start_job_env(tmp_path, monkeypatch)
    assert env["MEM_TOKEN"] == _TOKEN
    assert env["MEM_LAUNCHER"] == "ccc"


@pytest.mark.usefixtures("down_mem")
def test_start_job_launches_without_token_when_mem_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _start_job_env(tmp_path, monkeypatch)
    assert "MEM_TOKEN" not in env
    assert "MEM_LAUNCHER" not in env
    assert _calls(tmp_path) == ["token -l ccc -t|3"]  # it tried, failed, launched anyway


def test_start_job_launches_without_token_when_mem_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _start_job_env(tmp_path, monkeypatch)
    assert "MEM_TOKEN" not in env


@pytest.mark.usefixtures("good_mem")
def test_typed_launch_prefix_never_carries_the_token(tmp_path: Path) -> None:
    """The tab command string is typed into a shell (history): no token in it, no mint."""
    from command_center import accounts

    prefix = accounts.session_launch_env_prefix(accounts.LaunchTarget("", False))
    assert "MEM_TOKEN" not in prefix
    assert _calls(tmp_path) == []


# ── typed launch lines: the shell mints through `ccc mem-token` (tp#1290) ─────────


def _typed_bin(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH dir with a ``ccc`` running THIS checkout's cli and a ``claude`` env recorder."""
    import sys

    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "claude-env.txt"
    ccc = bindir / "ccc"
    ccc.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -c '
        '"import sys; from command_center.cli import main; sys.exit(main())" "$@"\n',
        encoding="utf-8",
    )
    claude = bindir / "claude"
    claude.write_text(
        f'#!/bin/sh\nprintf "%s|%s|%s\\n" "$MEM_TOKEN" "$MEM_LAUNCHER" "$PWD" > "{out}"\n',
        encoding="utf-8",
    )
    ccc.chmod(0o755)
    claude.chmod(0o755)
    return bindir, out


def _typed_resume_line(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> str:
    """The exact text ``terminal.resume_in_new_tab`` types into a new iTerm tab."""
    from command_center import config, terminal

    monkeypatch.setattr(config, "load_config", lambda: config.Config(launcher="iterm"))
    typed: list[str] = []

    def _fake_iterm(command: str) -> bool:
        typed.append(command)
        return True

    monkeypatch.setattr(terminal, "_iterm", _fake_iterm)
    assert terminal.resume_in_new_tab(str(cwd), "sid-typed") is True
    assert len(typed) == 1
    return typed[0]


def _run_typed(line: str, shell: str, bindir: Path, out: Path) -> list[str]:
    """Run *line* the way the tab's shell would; what the fake claude saw."""
    import shutil
    import subprocess

    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    argv = [exe, "-f", "-c", line] if shell == "zsh" else [exe, "-c", line]
    subprocess.run(argv, env=env, check=True, timeout=60)
    return out.read_text(encoding="utf-8").strip().split("|")


@pytest.mark.parametrize("shell", ["sh", "zsh"])
@pytest.mark.usefixtures("good_mem")
def test_typed_resume_line_has_no_token_but_its_claude_gets_one(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "re po"
    cwd.mkdir()
    line = _typed_resume_line(monkeypatch, cwd)
    assert _TOKEN not in line  # nothing secret in the typed text (shell history, screen)
    assert mem_token.TYPED_SNIPPET in line
    assert _calls(tmp_path) == []  # building the line minted nothing in-process
    bindir, out = _typed_bin(tmp_path)
    token, launcher, pwd = _run_typed(line, shell, bindir, out)
    assert (token, launcher) == (_TOKEN, "ccc")  # MEM_TOKEN reached the claude child env
    assert Path(pwd).resolve() == cwd.resolve()
    assert _calls(tmp_path) == ["token -l ccc -t|3"]


@pytest.mark.usefixtures("down_mem")
def test_typed_resume_line_still_launches_when_the_mint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    bindir, out = _typed_bin(tmp_path)
    assert _run_typed(_typed_resume_line(monkeypatch, cwd), "sh", bindir, out)[:2] == ["", ""]


@pytest.mark.usefixtures("good_mem")
def test_typed_resume_line_keeps_an_ambient_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    line = _typed_resume_line(monkeypatch, cwd)
    bindir, out = _typed_bin(tmp_path)
    monkeypatch.setenv("MEM_TOKEN", "inherited-token")
    monkeypatch.setenv("MEM_LAUNCHER", "claude-wrapper")
    assert _run_typed(line, "sh", bindir, out)[:2] == ["inherited-token", "claude-wrapper"]
    assert _calls(tmp_path) == []


@pytest.mark.usefixtures("good_mem")
def test_relaunch_and_snapshot_lines_carry_the_snippet_not_a_token(tmp_path: Path) -> None:
    from command_center import accounts, snapshot

    relaunch = accounts.relaunch_command(accounts.LaunchTarget(""), "sid", "/repo", "go")
    assert mem_token.TYPED_SNIPPET in relaunch and relaunch.endswith(" )")
    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="sid")
    command = snapshot._claude_action(pane, lambda *_: []).command  # noqa: SLF001
    assert command is not None and mem_token.TYPED_SNIPPET in command
    assert _TOKEN not in relaunch + command
    assert _calls(tmp_path) == []


@pytest.mark.usefixtures("good_mem")
def test_mem_token_command_prints_only_into_a_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from command_center import cli

    assert cli.main(["mem-token"]) == 0
    assert capsys.readouterr().out == f"{_TOKEN}\n"
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main(["mem-token"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "never to a terminal" in captured.err
    assert _calls(tmp_path) == ["token -l ccc -t|3"]  # the refusal minted nothing


@pytest.mark.usefixtures("good_mem")
def test_mem_token_command_keeps_an_ambient_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import cli

    monkeypatch.setenv("MEM_TOKEN", "inherited")
    assert cli.main(["mem-token"]) == 1
    assert capsys.readouterr().out == ""
    assert _calls(tmp_path) == []


# ── a Codex launch path: the one runner ──────────────────────────────────────────


def _codex_run(seats: SeatFixture, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """One ``codex-in-claude run`` against the fake codex; the env its child got."""
    from command_center import codex_in_claude as cic

    monkeypatch.setattr(  # keep the model catalog offline
        cic,
        "list_models",
        lambda **_: [{"slug": _MODEL, "visibility": "list", "default_reasoning_level": "medium"}],
    )
    seats.scenarios(private={"scenario": "ok", "reply": "ok"})
    args = argparse.Namespace(
        prompt="reply OK",
        cwd=str(seats.workdir),
        model=_MODEL,
        effort="low",
        timeout=60,
        idle_timeout=0,
        purpose="test",
        max_attempts=0,
        persist=False,
        ignore_quota=False,
        ephemeral=False,
        headroom=False,
        min_remaining=None,
        json=False,
    )
    assert cic.cmd_run(args) == cic.EX_OK  # the run happened
    env: dict[str, str] = seats.calls()[0]["env"]
    return env


@pytest.mark.usefixtures("good_mem")
def test_codex_child_env_carries_the_token(
    three_seats: SeatFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _codex_run(three_seats, monkeypatch)
    assert env["MEM_TOKEN"] == _TOKEN
    assert env["MEM_LAUNCHER"] == "codex-in-claude"
    assert "MEM_TOKEN" not in os.environ  # the runner's own env is untouched
    assert _calls(tmp_path) == ["token -l codex-in-claude -t|3"]


@pytest.mark.usefixtures("down_mem")
def test_codex_runs_without_token_when_mem_fails(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _codex_run(three_seats, monkeypatch)
    assert env["MEM_TOKEN"] == ""  # the fake codex records an unset variable as ""
    assert env["MEM_LAUNCHER"] == ""
