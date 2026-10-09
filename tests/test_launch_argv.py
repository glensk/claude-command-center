"""Every interactive Claude launch goes through ONE builder and carries ``--name`` + the env var.

PLAN_claude-bridge.md §7.1 / spike S-LAUNCH lists the eight ccc-started launch sites L1–L8.
Each test below drives one site with the builder spied on, and asserts that (a) the site
built its command through :func:`launch_argv.claude_argv`, (b) the session's canonical
name arrived as ``--name <name>``, and (c) ``CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1`` reaches
the launched process — in ``os.environ`` before an ``execvp``, or as an ``export`` in a
typed shell string.
"""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

# The fire-await site needs a claimed ``ccc await`` group; reuse that module's fixture.
from test_await_delivery import (  # noqa: F401  # pylint: disable=unused-import  # registers the fixture
    _claimable,
    _register_env,
    env_fixture,
)

from command_center import accounts, cli, launch_argv, session_continue, snapshot, terminal
from command_center.store import Store

NAME = "voice bridge"
TITLE_EXPORT = "export CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1; "


@pytest.fixture(name="spy")
def spy_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Wrap the builder (recording every call) and make every id resolve to :data:`NAME`."""
    calls: list[dict[str, Any]] = []
    real = launch_argv.claude_argv

    def wrapped(**kwargs: Any) -> list[str]:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(launch_argv, "claude_argv", wrapped)
    monkeypatch.setattr(launch_argv, "lookup_name", lambda sid: NAME if sid else "")
    for var in (accounts.TITLE_VAR, "CCC_NO_CODEX", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(var, "x")
        monkeypatch.delenv(var)
    return calls


def _has_name(argv: list[str]) -> bool:
    return argv[:3] == ["claude", "--name", NAME]


def _tabs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(command: str, **_: object) -> bool:
        seen.append(command)
        return True

    monkeypatch.setattr(terminal, "_iterm", fake)
    monkeypatch.setattr(terminal, "_launcher_mode", lambda: "iterm")
    monkeypatch.setattr(terminal, "_iterm_api_tab", lambda *_a, **_k: False)
    monkeypatch.setattr(terminal, "_tmux_window", lambda *_a, **_k: False)
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    return seen


def _capture_exec(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_exec(_file: str, argv: list[str]) -> None:
        seen["argv"] = list(argv)
        seen["env"] = dict(os.environ)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", fake_exec)
    return seen


@pytest.fixture(name="job")
def job_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.create_draft("job1", str(repo), "aim")
        store.update_fields("job1", config_dir="")
    return "job1"


# --------------------------------------------------------------------------- builder
def test_builder_shapes() -> None:
    assert launch_argv.claude_argv(resume="s1", name="a b", prompt="go") == [
        "claude",
        "--name",
        "a b",
        "--resume",
        "s1",
        "go",
    ]
    assert launch_argv.claude_argv(session_id="s1", model="m", effort="high", name="") == [
        "claude",
        "--model",
        "m",
        "--session-id",
        "s1",
        "--effort",
        "high",
    ]
    assert launch_argv.claude_argv(continue_last=True, extra=("--x",), name="") == [
        "claude",
        "--continue",
        "--x",
    ]


def test_builder_looks_the_name_up_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    with Store() as store:
        store.ensure("sid1", cwd="/r")
        store.update_fields("sid1", canonical_name="voice bridge")
    assert launch_argv.claude_argv(resume="sid1")[:3] == ["claude", "--name", "voice bridge"]
    assert launch_argv.claude_argv(resume="unknown") == ["claude", "--resume", "unknown"]


def test_command_string_quotes_and_prefixes() -> None:
    argv = launch_argv.claude_argv(resume="sid", name=NAME, prompt="it's")
    line = launch_argv.claude_command(accounts.LaunchTarget(""), argv, cwd="/a b")
    assert line.startswith("unset ") and TITLE_EXPORT in line
    assert line.endswith("cd '/a b' && claude --name 'voice bridge' --resume sid 'it'\"'\"'s'")
    sub = launch_argv.claude_command(accounts.LaunchTarget(""), argv, cwd="/r", subshell=True)
    assert sub.startswith("cd /r && ( unset ") and sub.endswith(" )")
    assert shlex.split(sub.split("( ", 1)[1].rsplit(" )", 1)[0].rsplit("; ", 1)[1])[:3] == [
        "claude",
        "--name",
        NAME,
    ]


def test_env_flags_always_disable_the_terminal_title() -> None:
    assert accounts.session_env_flags(accounts.LaunchTarget("")) == {accounts.TITLE_VAR: "1"}
    assert accounts.session_env_flags(accounts.LaunchTarget("", True)) == {
        accounts.TITLE_VAR: "1",
        "CCC_NO_CODEX": "1",
    }


# --------------------------------------------------------------------------- L1–L8
def test_l1_resume_in_new_tab(spy: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _tabs(monkeypatch)
    assert terminal.resume_in_new_tab("/repo", "sid", "")
    assert spy and spy[-1]["resume"] == "sid"
    assert TITLE_EXPORT in seen[0] and "claude --name 'voice bridge' --resume sid" in seen[0]


def test_l1_resume_in_new_tab_tmux(
    spy: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(terminal, "_launcher_mode", lambda: "tmux")
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)

    def fake_tmux(cmd: str, **_k: object) -> bool:
        seen.append(cmd)
        return True

    monkeypatch.setattr(terminal, "_tmux_window", fake_tmux)
    assert terminal.resume_in_new_tab("/repo", "sid", "")
    assert spy and TITLE_EXPORT in seen[0] and "--name 'voice bridge'" in seen[0]


def test_l2_cmd_resume(
    spy: list[dict[str, Any]], job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import core

    with Store() as store:
        store.update_fields(job, draft=False)
    monkeypatch.setattr(core, "resume_blockers", lambda *_a, **_k: [])
    monkeypatch.setattr(cli, "has_terminal", lambda: True)
    seen = _capture_exec(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_resume(argparse.Namespace(session_id=job))
    assert spy and _has_name(seen["argv"]) and seen["env"][accounts.TITLE_VAR] == "1"


def test_l3_cmd_fire_attached(
    spy: list[dict[str, Any]], job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store() as store:
        store.update_fields(job, draft=False, prompt="go", fire_at=999)
    seen = _capture_exec(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_fire_attached(argparse.Namespace(session_id=job))
    assert spy and _has_name(seen["argv"]) and seen["argv"][-1] == "go"
    assert seen["env"][accounts.TITLE_VAR] == "1"


@pytest.mark.parametrize("fresh", [False, True])
def test_l4_cmd_fire_await(
    spy: list[dict[str, Any]], env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fresh: bool
) -> None:
    gid, token = _claimable(env, fresh=fresh)
    _register_env(monkeypatch, env)
    monkeypatch.setattr(cli, "Store", lambda: Store(env["tmp"] / "state.db"))
    seen: dict[str, Any] = {}

    def fake_exec(_file: str, argv: list[str]) -> None:
        seen["argv"] = list(argv)
        seen["env"] = dict(os.environ)

    monkeypatch.setattr(os, "execvp", fake_exec)
    assert cli.cmd_fire_await(argparse.Namespace(group_id=gid, token=token)) == 0
    assert spy and seen["env"][accounts.TITLE_VAR] == "1"
    if fresh:  # a NEW session: no id yet, so no name to carry
        assert seen["argv"][0] == "claude" and "--name" not in seen["argv"]
    else:
        assert _has_name(seen["argv"])


def test_l5_cmd_start_job(
    spy: list[dict[str, Any]], job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    seen = _capture_exec(monkeypatch)
    with pytest.raises(SystemExit):
        cli.cmd_start_job(argparse.Namespace(session_id=job, force=True, auto=False))
    argv = seen["argv"]
    assert spy and _has_name(argv) and "--session-id" in argv and "--model" in argv
    assert argv[-1].endswith("aim")  # the prompt stays the LAST argument
    assert seen["env"][accounts.TITLE_VAR] == "1"


def test_l6_relaunch_command(spy: list[dict[str, Any]]) -> None:
    line = accounts.relaunch_command(accounts.LaunchTarget(""), "sid", "/repo", "go on")
    assert spy and TITLE_EXPORT in line
    assert "claude --name 'voice bridge' --resume sid 'go on' )" in line


def test_l7_snapshot_restore(spy: list[dict[str, Any]], tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="sid")
    action = snapshot._claude_action(pane, lambda *_a: [])
    assert spy and action.command is not None
    assert TITLE_EXPORT in action.command and "claude --name 'voice bridge'" in action.command


def test_l8_session_continue(
    spy: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = session_continue.build_command(session_continue.parse_args(["sid"]), "sid")
    assert spy and _has_name(argv) and argv[-1] == session_continue.DEFAULT_PROMPT
    # L8's env is inherited from the tab T4 opened: its typed prefix carries the var.
    seen = _tabs(monkeypatch)
    script = tmp_path / "claude-session-continue"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    assert terminal.resume_halted_in_new_tab(str(tmp_path), "sid", str(script), "")
    assert TITLE_EXPORT in seen[0]


def test_no_site_hand_rolls_a_claude_argv() -> None:
    """Static guard: no module but the builder spells an interactive ``claude`` launch."""
    package = Path(launch_argv.__file__).parent
    offenders = []
    for module in sorted(package.rglob("*.py")):
        if module.name == "launch_argv.py":
            continue
        for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            # An argv list, or a command string glued to an env prefix / `cd … &&`.
            # Headless calls (`--print` probe, `--version`) are not launches.
            headless = "--print" in code or "--version" in code
            hand_rolled = '["claude", "--' in code and not headless
            if hand_rolled or "&& claude --resume" in code or "}claude --resume" in code:
                offenders.append(f"{module.name}:{number}: {line.strip()}")
    assert not offenders, offenders
