"""Launch profiles: an opaque label that routes a session's ``claude`` through ONE executable.

The profile is billing-relevant identity, so it has to survive every hop and FAIL CLOSED:
the config parser + resolver, the store column + migration, the env/argv renderings in
``accounts`` / ``launch_argv`` and — the part that matters — EVERY ccc-owned launch/resume
surface, each of which must either start through the profile's executable (exporting
``CCC_LAUNCH_PROFILE``) or REFUSE before any state change. Never a bare ``claude``.
Modelled on ``tests/test_no_codex.py``; every surface is crossed with ``no_codex``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from bridgestub import SID, World, run_cli

from command_center import accounts, cli, config, future_files, launch_argv, snapshot, terminal
from command_center.models import Session
from command_center.store import Store

LABEL = "apiwork"
PURPOSE = "Waiting for the requester to confirm the quota."


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _write_exe(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _set_profiles(entries: list[str]) -> None:
    """Write ``claude_launch_profiles`` into the (throwaway) config file."""
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(json.dumps(e) for e in entries)
    path.write_text(f"claude_launch_profiles = [{rendered}]\n", encoding="utf-8")
    config.invalidate_config_cache()


@pytest.fixture(name="exe")
def exe_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configured profile ``apiwork`` → an executable script; CLAUDE_HOME is throwaway."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CCC_NO_CODEX", "")
    monkeypatch.delenv("CCC_NO_CODEX")
    monkeypatch.setenv(accounts.PROFILE_VAR, "")
    monkeypatch.delenv(accounts.PROFILE_VAR)
    wrapper = _write_exe(tmp_path / "apiwrap")
    _set_profiles([f"{LABEL}={wrapper}"])
    return wrapper


def _vanish(exe_path: Path) -> None:
    """The profile's executable disappears after registration (the launch-time check)."""
    exe_path.unlink()


@pytest.fixture(name="job")
def job_fixture(tmp_path: Path, exe: Path) -> str:  # pylint: disable=unused-argument
    """A resumable profile session (+ no_codex variants set per test) with a transcript."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.create_draft("pj", str(repo), "aim", launch_profile=LABEL)
        store.update_fields("pj", config_dir="", draft=False, prompt="go", fire_at=999)
    return "pj"


class _Exec:
    """Capture the ``os.execvp`` call (file, argv, env snapshot) and stop the process."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: dict[str, str] = {}

    def __call__(self, _file: str, argv: list[str]) -> None:
        self.argv = list(argv)
        self.env = dict(os.environ)
        raise SystemExit(0)


def _no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os, "execvp", lambda *_a, **_k: pytest.fail("a refused launch must not exec anything")
    )


def _tabs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the shell command strings ``terminal`` would type into a new tab."""
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


# --------------------------------------------------------------------------- #
# config: parsing and refusals
# --------------------------------------------------------------------------- #
def test_default_is_inert() -> None:
    assert not config.DEFAULTS["claude_launch_profiles"]
    assert not config.parse_launch_profiles([])
    assert not config.parse_launch_profiles(None)


@pytest.mark.parametrize(
    "entry",
    [
        "noequals",
        "=/bin/true",
        "UPPER=/bin/true",
        "has_underscore=/bin/true",
        f"{'a' * 33}=/bin/true",
        "x=",
        "x=/bin/true --flag",  # arguments
        "x=env FOO=1 /bin/true",  # env + command
        "x=/bin/true;rm",  # still one token — but the next two cover shells and control chars
        "x=/bin/true\nrm",
        "x=/bin/true\x00",
        "x=/bin/true\x1b[0m",
    ],
)
def test_parse_refuses_malformed_entries(entry: str) -> None:
    parsed = config.parse_launch_profiles([entry])
    # ``;`` is not whitespace: one token is all the spec forbids, and resolve() then
    # refuses it because no such file exists. Everything else is dropped at parse time.
    assert parsed in ({}, {"x": "/bin/true;rm"})


def test_parse_accepts_labels_and_one_token() -> None:
    parsed = config.parse_launch_profiles(["a-1=/opt/w/run", "b=wrapper", 7, "c=~/bin/x"])
    assert parsed == {"a-1": "/opt/w/run", "b": "wrapper", "c": "~/bin/x"}


def test_resolve_absolute_path_and_path_name(
    tmp_path: Path, exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert config.resolve_launch_profile(LABEL) == str(exe)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    named = _write_exe(bindir / "named-wrap")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    _set_profiles(["viapath=named-wrap"])
    assert config.resolve_launch_profile("viapath") == str(named)


def test_resolve_refuses_unknown_missing_and_non_executable(tmp_path: Path, exe: Path) -> None:
    with pytest.raises(config.LaunchProfileError, match="not configured"):
        config.resolve_launch_profile("nope")
    with pytest.raises(config.LaunchProfileError, match="not a valid label"):
        config.resolve_launch_profile("Bad Label")
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    _set_profiles([f"plain={plain}", f"gone={tmp_path / 'gone'}", "nowhere=no-such-cmd-xyz"])
    for label in ("plain", "gone", "nowhere"):
        with pytest.raises(config.LaunchProfileError, match="not found or is not executable"):
            config.resolve_launch_profile(label)
    _vanish(exe)  # resolved at launch too: a later deletion refuses
    _set_profiles([f"{LABEL}={exe}"])
    with pytest.raises(config.LaunchProfileError):
        config.resolve_launch_profile(LABEL)


# --------------------------------------------------------------------------- #
# store: column, migration, old reader
# --------------------------------------------------------------------------- #
def test_create_draft_persists_the_profile_and_defaults_to_none(tmp_path: Path) -> None:
    with Store(tmp_path / "c.db") as store:
        assert store.create_draft("a", "/r", "aim", launch_profile=LABEL).launch_profile == LABEL
        assert store.create_draft("b", "/r", "aim").launch_profile is None


def test_launch_profile_column_is_added_to_a_pre_existing_db(tmp_path: Path) -> None:
    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy = store_mod._SCHEMA.replace("    launch_profile    TEXT,\n", "")
    assert "launch_profile" not in legacy
    conn = sqlite3.connect(db)
    conn.executescript(legacy)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        old = store.get("old")
        assert old is not None and old.launch_profile is None
        store.update_fields("old", launch_profile=LABEL)
        refreshed = store.get("old")
        assert refreshed is not None and refreshed.launch_profile == LABEL


def test_an_old_reader_drops_the_unknown_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ccc that predates the column still reads a DB a newer ccc migrated."""
    from command_center import store as store_mod

    with Store(tmp_path / "c.db") as store:
        store.create_draft("a", "/r", "aim", launch_profile=LABEL)
    monkeypatch.setattr(
        store_mod,
        "_SESSION_FIELDS",
        frozenset(store_mod._SESSION_FIELDS - {"launch_profile"}),
    )
    with Store(tmp_path / "c.db") as store:
        row = store.get("a")
        assert row is not None and row.session_id == "a" and row.launch_profile is None


# --------------------------------------------------------------------------- #
# env + argv renderings
# --------------------------------------------------------------------------- #
def test_env_exports_only_the_label_and_never_clears_codex() -> None:
    base = {"PATH": "/usr/bin", accounts.PROFILE_VAR: "stale"}
    on = accounts.session_launch_env(Session(session_id="s", launch_profile=LABEL), base)
    assert on[accounts.PROFILE_VAR] == LABEL
    off = accounts.session_launch_env(Session(session_id="s"), base)
    assert accounts.PROFILE_VAR not in off  # an ambient label never leaks into a bare launch
    both = accounts.session_launch_env(
        Session(session_id="s", launch_profile=LABEL, no_codex=True), {}
    )
    assert both["CCC_NO_CODEX"] == "1" and both[accounts.PROFILE_VAR] == LABEL


def test_prefix_and_in_place_renderings(monkeypatch: pytest.MonkeyPatch) -> None:
    on = accounts.session_launch_env_prefix(accounts.LaunchTarget("", False, LABEL))
    assert f"export {accounts.PROFILE_VAR}={LABEL}; " in on
    off = accounts.session_launch_env_prefix(accounts.LaunchTarget("", False))
    assert accounts.PROFILE_VAR not in off  # typed strings of profile-less sessions: unchanged
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    monkeypatch.setenv(accounts.PROFILE_VAR, "stale")
    accounts.session_apply_to_environ(Session(session_id="s"))
    assert accounts.PROFILE_VAR not in os.environ
    accounts.session_apply_to_environ(Session(session_id="s", launch_profile=LABEL))
    assert os.environ[accounts.PROFILE_VAR] == LABEL
    monkeypatch.setenv(accounts.PROFILE_VAR, "stale")
    accounts.pin_environ("", False)
    assert accounts.PROFILE_VAR not in os.environ
    accounts.pin_environ("", False, LABEL)
    assert os.environ[accounts.PROFILE_VAR] == LABEL


def test_argv_swaps_only_the_executable(exe: Path) -> None:
    plain = launch_argv.claude_argv(resume="sid", name="", prompt="p", model="m")
    profiled = launch_argv.claude_argv(
        resume="sid", name="", prompt="p", model="m", launch_profile=LABEL
    )
    assert plain[0] == "claude" and profiled[0] == str(exe)
    assert profiled[1:] == plain[1:]
    assert launch_argv.profile_argv(profiled, LABEL) == profiled  # idempotent
    with pytest.raises(config.LaunchProfileError):
        launch_argv.claude_argv(name="", launch_profile="nope")


def test_command_string_swaps_the_executable_and_exports_the_label(exe: Path) -> None:
    target = accounts.LaunchTarget("", True, LABEL)
    command = launch_argv.claude_command(target, launch_argv.claude_argv(resume="sid", name=""))
    assert f"export {accounts.PROFILE_VAR}={LABEL}; " in command
    assert "export CCC_NO_CODEX=1; " in command
    assert f"{exe} --resume sid" in command and " claude " not in command
    bare = launch_argv.claude_command(
        accounts.LaunchTarget("", False), launch_argv.claude_argv(resume="sid", name="")
    )
    assert "claude --resume sid" in bare


def test_command_string_refuses_an_unresolvable_profile(exe: Path) -> None:
    _vanish(exe)
    with pytest.raises(config.LaunchProfileError):
        launch_argv.claude_command(
            accounts.LaunchTarget("", False, LABEL), launch_argv.claude_argv(name="")
        )


# --------------------------------------------------------------------------- #
# every launch surface × (profile, no_codex)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("no_codex", [False, True])
def test_resume_in_new_tab_carries_the_profile(
    exe: Path, monkeypatch: pytest.MonkeyPatch, no_codex: bool
) -> None:
    """`ccc resume-job` / TUI `r` + undo-close / `ccc jump` all land here."""
    seen = _tabs(monkeypatch)
    assert terminal.resume_in_new_tab("/repo", "sid", "", no_codex=no_codex, launch_profile=LABEL)
    command = seen[0]
    assert f"export {accounts.PROFILE_VAR}={LABEL}; " in command
    assert f"{exe} --resume sid" in command
    assert ("export CCC_NO_CODEX=1; " in command) is no_codex


def test_resume_in_new_tab_refuses_before_trust_or_tab_when_unresolvable(
    exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _tabs(monkeypatch)
    monkeypatch.setattr(
        accounts, "ensure_trusted", lambda *_a, **_k: pytest.fail("trust granted on a refusal")
    )
    _vanish(exe)
    assert terminal.resume_in_new_tab("/repo", "sid", "", launch_profile=LABEL) is False
    assert not seen


def test_resume_halted_in_new_tab_refuses_a_profile(
    exe: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _tabs(monkeypatch)
    script = _write_exe(tmp_path / "continue")
    assert not terminal.resume_halted_in_new_tab(
        str(tmp_path), "sid", str(script), "", launch_profile=LABEL
    )
    assert not seen and exe.exists()


def test_halted_auto_resume_refuses_before_the_reap(
    job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile session is never reaped/relaunched by the halted-session watcher."""
    from command_center import resume

    calls: list[str] = []
    monkeypatch.setattr(resume, "_reap_fresh", lambda *_a, **_k: calls.append("reap"))

    def launch(*_a: object, **_k: object) -> bool:
        calls.append("launch")
        return True

    monkeypatch.setattr(resume, "_launch_resume", launch)
    state = resume.QueueState()
    adapter: Any = object()  # never consulted: the refusal comes first
    actions = [
        resume.Action(kind="reap", session_id=job, cwd="/repo"),
        resume.Action(kind="launch_resume", session_id=job, cwd="/repo"),
    ]
    with Store() as store:
        resume.apply_actions(actions, state, store, adapter, config.Config())
    assert not calls


def test_snapshot_restore_carries_and_refuses(exe: Path, tmp_path: Path) -> None:
    pane = snapshot.SnapPane(
        kind="claude", cwd=str(tmp_path), session_id="sid", no_codex=True, launch_profile=LABEL
    )
    action = snapshot._claude_action(pane, lambda *_a: [])
    assert action.command is not None and not action.error
    assert f"export {accounts.PROFILE_VAR}={LABEL}; " in action.command
    assert "export CCC_NO_CODEX=1; " in action.command and f"{exe} --resume sid" in action.command
    _vanish(exe)
    refused = snapshot._claude_action(pane, lambda *_a: [])
    assert refused.error and "claude --resume" not in (refused.command or "")


def test_snapshot_pane_round_trips_the_profile() -> None:
    pane = snapshot.SnapPane(kind="claude", cwd="/r", session_id="sid", launch_profile=LABEL)
    assert snapshot.pane_to_json(pane)["launch_profile"] == LABEL
    assert snapshot.pane_from_json(snapshot.pane_to_json(pane)).launch_profile == LABEL
    plain = snapshot.SnapPane(kind="claude", cwd="/r", session_id="sid")
    assert "launch_profile" not in snapshot.pane_to_json(plain)  # old snapshots byte-identical
    row = snapshot.store_row(Session(session_id="s", launch_profile=LABEL))
    assert row.launch_profile == LABEL


@pytest.mark.parametrize("no_codex", [False, True])
def test_cmd_resume_execs_through_the_profile(
    job: str, exe: Path, monkeypatch: pytest.MonkeyPatch, no_codex: bool
) -> None:
    from command_center import core

    with Store() as store:
        store.update_fields(job, no_codex=no_codex)
    monkeypatch.setattr(core, "resume_blockers", lambda *_a, **_k: [])
    monkeypatch.setattr(cli, "has_terminal", lambda: True)
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    seen = _Exec()
    monkeypatch.setattr(os, "execvp", seen)
    with pytest.raises(SystemExit):
        cli.cmd_resume(argparse.Namespace(session_id=job))
    assert "--resume" in seen.argv and seen.argv[0] == str(exe)
    assert seen.env[accounts.PROFILE_VAR] == LABEL
    assert (seen.env.get("CCC_NO_CODEX") == "1") is no_codex


def test_cmd_resume_refuses_a_vanished_profile(
    job: str, exe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import core

    monkeypatch.setattr(core, "resume_blockers", lambda *_a, **_k: [])
    monkeypatch.setattr(cli, "has_terminal", lambda: True)
    _no_exec(monkeypatch)
    _vanish(exe)
    assert cli.cmd_resume(argparse.Namespace(session_id=job)) == 1
    assert "refusing to launch" in capsys.readouterr().err
    assert accounts.PROFILE_VAR not in os.environ


@pytest.mark.parametrize("no_codex", [False, True])
def test_cmd_fire_attached_execs_through_the_profile(
    job: str, exe: Path, monkeypatch: pytest.MonkeyPatch, no_codex: bool
) -> None:
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    with Store() as store:
        store.update_fields(job, no_codex=no_codex)
    seen = _Exec()
    monkeypatch.setattr(os, "execvp", seen)
    with pytest.raises(SystemExit):
        cli.cmd_fire_attached(argparse.Namespace(session_id=job))
    assert seen.argv[0] == str(exe) and seen.argv[-2:] == ["--", "go"]
    assert seen.env[accounts.PROFILE_VAR] == LABEL
    assert (seen.env.get("CCC_NO_CODEX") == "1") is no_codex


def test_cmd_fire_attached_refuses_before_the_claim(
    job: str, exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_exec(monkeypatch)
    _vanish(exe)
    assert cli.cmd_fire_attached(argparse.Namespace(session_id=job)) == 1
    with Store() as store:
        row = store.get(job)
        assert row is not None and row.prompt == "go" and row.fire_at == 0  # parked, disarmed


@pytest.mark.parametrize("no_codex", [False, True])
def test_cmd_start_job_execs_through_the_profile(
    tmp_path: Path, exe: Path, monkeypatch: pytest.MonkeyPatch, no_codex: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.create_draft(
            "sj", str(repo), "aim", no_codex=no_codex, launch_profile=LABEL, config_dir=""
        )
    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    seen = _Exec()
    monkeypatch.setattr(os, "execvp", seen)
    with pytest.raises(SystemExit):
        cli.cmd_start_job(argparse.Namespace(session_id="sj", force=True, auto=False))
    assert seen.argv[0] == str(exe) and "--session-id" in seen.argv
    assert seen.env[accounts.PROFILE_VAR] == LABEL
    assert (seen.env.get("CCC_NO_CODEX") == "1") is no_codex


def test_cmd_start_job_refuses_a_vanished_profile_and_keeps_the_draft(
    tmp_path: Path, exe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.create_draft("sj", str(repo), "aim", launch_profile=LABEL, fire_at=123)
    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")
    _no_exec(monkeypatch)
    _vanish(exe)
    args = argparse.Namespace(session_id="sj", force=True, auto=True)
    assert cli.cmd_start_job(args) == 1
    assert "refusing to launch" in capsys.readouterr().err
    with Store() as store:  # nothing promoted; the auto-arm is disarmed, the job survives
        row = store.get("sj")
        assert row is not None and row.draft is True and row.fire_at == 0


def test_await_arm_refuses_a_profile_session(job: str, capsys: pytest.CaptureFixture[str]) -> None:
    """An await group carries no profile, so a closed-session delivery would be a bare claude."""
    from command_center import await_cli

    parser = cli.build_parser(only="await")
    args = parser.parse_args(
        ["await", "-s", job, "-x", "true", "-u", "1d", "-m", "{event}", "-P", PURPOSE]
    )
    assert await_cli.cmd_await(args) != 0
    assert "launch profile" in capsys.readouterr().err
    with Store() as store:
        assert store.get_await_group(1) is None


def test_fire_await_refuses_a_profile_session(
    job: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a group armed before the profile existed never delivers through a bare claude."""
    from command_center.await_store import SourceSpec

    now = int(time.time())
    with Store() as store:
        gid = store.arm_await(
            job,
            config_dir=str(tmp_path / "acct"),
            cwd=str(tmp_path),
            no_codex=False,
            prompt_template="Reply arrived: {event}. Continue.",
            until_epoch=now + 3600,
            sources=[SourceSpec(kind="zoho-reply", spec={}, watermark="w")],
            now=now,
            purpose="Waiting for the requester to confirm the quota.",
            items=[],
            fresh=False,
        )
        src = store.lease_due_sources(now + 120)[0][0]
        token = store.fire_group(
            gid,
            src.id,
            src.lease_token,
            event_id="e:1",
            payload="{}",
            remote_epoch=now,
            watermark="w2",
            now=now,
        )
        assert token and store.mark_delivering(gid, token, now)
    monkeypatch.setattr(cli, "has_terminal", lambda: True)
    _no_exec(monkeypatch)
    assert cli.cmd_fire_await(argparse.Namespace(group_id=gid, token=token)) == 1
    with Store() as store:
        group = store.get_await_group(gid)
        assert group is not None and group.state == "blocked"


def test_continue_session_refuses_a_profile_source(
    job: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import continue_session

    with Store() as store:
        src = continue_session.Source(session_id=job, cwd="/repo")
        monkeypatch.setattr(continue_session, "_open_source", lambda _a: (None, store, src))
        args = argparse.Namespace(capped=False, file="", json=False, account="work", dry_run=False)
        assert continue_session.cmd_start(args) == 1
    assert "launch profile" in capsys.readouterr().err


def test_switch_account_and_switch_now_refuse_a_profile(
    job: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import spawn

    monkeypatch.setattr(spawn, "spawn_ccc", lambda *_a, **_k: pytest.fail("spawned a relauncher"))
    args = argparse.Namespace(
        session=job,
        config_dir=str(Path("/tmp/other")),
        source_dir="/tmp/src",
        cwd="/repo",
        prompt="",
        iterm="",
        tmux_pane="",
        pid=0,
        force=False,
        no_codex=False,
        auto=False,
    )
    assert cli.cmd_switch_now(args) == 1
    assert "launch profile" in capsys.readouterr().err


def test_limitswitch_declines_a_profile_session(job: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import limitswitch
    from command_center.models import LiveSession

    live = LiveSession(
        pid=1, session_id=job, cwd="/repo", kind="interactive", alive=True, config_dir="/cfg/x"
    )

    class _Adapter:
        def discover(self) -> list[LiveSession]:
            return [live]

    monkeypatch.setattr(limitswitch, "ClaudeAdapter", _Adapter)
    monkeypatch.setattr(
        limitswitch, "observe", lambda *_a, **_k: pytest.fail("a profile session was observed")
    )
    switched, detail = limitswitch._run(job)  # noqa: SLF001  # pylint: disable=protected-access
    assert switched is False and "launch profile" in detail


def test_row_helper_forwards_every_launch_field(exe: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TUI ``r`` / undo-close / jump / resume-job go through the row helper."""
    seen = _tabs(monkeypatch)
    row = Session(session_id="sid", cwd="/repo", config_dir="", no_codex=True, launch_profile=LABEL)
    assert terminal.resume_session_in_new_tab(row)
    assert (
        f"export {accounts.PROFILE_VAR}={LABEL}; " in seen[0] and f"{exe} --resume sid" in seen[0]
    )
    assert "export CCC_NO_CODEX=1; " in seen[0]


def test_no_resume_in_new_tab_caller_drops_the_profile() -> None:
    """Static guard: each direct ``resume_in_new_tab`` caller forwards the session's profile."""
    package = Path(accounts.__file__).parent
    missing: list[str] = []
    for module in sorted(package.rglob("*.py")):
        if module.name == "terminal.py":  # the definition, the row helper + docstrings
            continue
        text = module.read_text(encoding="utf-8")
        for call in text.split("resume_in_new_tab(")[1:]:
            head = call[: call.index("):") + 2] if "):" in call else call[:300]
            if "launch_profile" not in head and "no_codex" in head:
                missing.append(f"{module.relative_to(package)}: resume_in_new_tab({head}")
    assert not missing, "resume surfaces dropping the profile:\n" + "\n".join(missing)


def test_no_exec_surface_builds_a_bare_argv_for_a_session_row() -> None:
    """Static guard: every in-place exec built off a session row passes its profile."""
    text = (Path(cli.__file__)).read_text(encoding="utf-8")
    assert text.count("_profile_argv(") >= 3  # resume, fire-attached (+ the helper itself)
    assert "launch_profile=profile" in text  # start-job's argv


# --------------------------------------------------------------------------- #
# CLI: new-job -L, identity, machine contracts
# --------------------------------------------------------------------------- #
def _new_job(repo: Path, *extra: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["new-job", "-a", "do x", "-c", str(repo), *extra])


def test_new_job_registers_the_profile_and_json_reports_it(
    tmp_path: Path, exe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert exe.exists()
    assert cli.cmd_new_job(_new_job(tmp_path, "-L", LABEL, "-J")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["launch_profile"] == LABEL
    with Store() as store:
        row = store.get(doc["session_id"])
        assert row is not None and row.launch_profile == LABEL
    assert cli.cmd_new_job(_new_job(tmp_path, "-J")) == 0
    assert json.loads(capsys.readouterr().out)["launch_profile"] == ""


def test_new_job_refuses_unknown_and_unresolvable_labels(
    tmp_path: Path, exe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_new_job(_new_job(tmp_path, "-L", "bogus")) == 1
    assert "not configured" in capsys.readouterr().err
    _vanish(exe)
    assert cli.cmd_new_job(_new_job(tmp_path, "-L", LABEL)) == 1
    assert "not found or is not executable" in capsys.readouterr().err
    with Store() as store:
        assert [s for s in store.list_sessions() if s.draft] == []  # no row was created


def test_idempotency_key_conflicts_on_a_different_profile(
    tmp_path: Path, exe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert exe.exists()
    assert cli.cmd_new_job(_new_job(tmp_path, "-K", "k", "-L", LABEL, "-J")) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.cmd_new_job(_new_job(tmp_path, "-K", "k", "-L", LABEL, "-J")) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["created"] is False and again["session_id"] == first["session_id"]
    assert cli.cmd_new_job(_new_job(tmp_path, "-K", "k", "-J")) == 2  # profile dropped
    assert "launch_profile" in capsys.readouterr().err
    other = tmp_path / "other"
    other.mkdir()
    assert cli.cmd_new_job(_new_job(other, "-K", "k2", "-J")) == 0
    capsys.readouterr()
    assert cli.cmd_new_job(_new_job(other, "-K", "k2", "-L", LABEL, "-J")) == 2
    assert "launch_profile" in capsys.readouterr().err


def test_idempotency_claim_stores_the_profile_for_a_racing_loser(tmp_path: Path) -> None:
    with Store(tmp_path / "c.db") as store:
        sid, created = store.claim_idempotency_key("k", "s1", "/r", launch_profile=LABEL)
        row = store.get(sid)
        assert created and row is not None and row.launch_profile == LABEL


def test_jobs_listing_json_and_tag(
    tmp_path: Path, exe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert exe.exists()
    with Store() as store:
        store.create_draft("p1", str(tmp_path), "profiled", launch_profile=LABEL)
        store.create_draft("n1", str(tmp_path), "normal")
    cli.cmd_jobs(argparse.Namespace(json=True))
    jobs = {j["session_id"]: j for j in json.loads(capsys.readouterr().out)["jobs"]}
    assert jobs["p1"]["launch_profile"] == LABEL and jobs["n1"]["launch_profile"] is None
    cli.cmd_jobs(argparse.Namespace(json=False))
    lines = capsys.readouterr().out.splitlines()
    assert any(f"[profile: {LABEL}]" in line for line in lines if "profiled" in line)
    assert all("[profile:" not in line for line in lines if "normal" in line)


def test_sessions_json_reports_the_profile(tmp_path: Path) -> None:
    from command_center import bridge_cli
    from command_center.models import LiveSession

    live = LiveSession(
        pid=1, session_id="s1", cwd="/r", kind="interactive", raw_status="idle", alive=True
    )
    with Store(tmp_path / "c.db") as store:
        store.ensure("s1", cwd="/r")
        store.update_fields("s1", launch_profile=LABEL)
        row = store.get("s1")
    entry = bridge_cli.session_entry(live, row, (None, False), lambda cd: cd)
    assert entry["launch_profile"] == LABEL
    assert (
        bridge_cli.session_entry(live, None, (None, False), lambda cd: cd)["launch_profile"] is None
    )


def test_inspect_json_reports_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from bridgestub import assistant, turn_end, user

    world = World(tmp_path)
    world.write(user("work"), *turn_end("done"))
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-N", "-j"])
    assert code == 0 and env is not None and env["data"]["launch_profile"] is None
    with Store(world.db) as store:
        store.update_fields(SID, launch_profile=LABEL)
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-N", "-j"])
    assert code == 0 and env is not None and env["data"]["launch_profile"] == LABEL
    assert assistant is not None


# --------------------------------------------------------------------------- #
# future-job files carry it (the store stays the authority)
# --------------------------------------------------------------------------- #
def test_job_file_round_trips_the_profile_and_stays_byte_stable_when_unset() -> None:
    sid = "11111111-1111-4111-8111-111111111111"
    plain = future_files.serialize(session_id=sid, aim="a")
    assert "launch_profile" not in plain
    assert future_files.parse_job_file(plain).launch_profile == ""
    flagged = future_files.serialize(session_id=sid, aim="a", launch_profile=LABEL)
    assert f'launch_profile: "{LABEL}"' in flagged
    parsed = future_files.parse_job_file(flagged)
    assert parsed.launch_profile == LABEL
    assert (
        future_files.serialize(session_id=sid, aim="a", launch_profile=parsed.launch_profile)
        == flagged
    )


def test_job_file_validation_requires_a_resolvable_profile(tmp_path: Path, exe: Path) -> None:
    repo = tmp_path / "cat" / "repo"
    repo.mkdir(parents=True)
    sid = "11111111-1111-4111-8111-111111111111"

    def errors(label: str) -> list[str]:
        job = future_files.parse_job_file(
            future_files.serialize(session_id=sid, aim="x", repo="cat/repo", launch_profile=label)
        )
        return future_files.validate(job, tmp_path)

    assert not [e for e in errors(LABEL) if "launch profile" in e]
    assert any("launch profile" in e for e in errors("bogus"))
    _vanish(exe)
    assert any("launch profile" in e for e in errors(LABEL))


def test_registered_draft_serializes_its_stored_profile(tmp_path: Path) -> None:
    """The canonical file text comes from the row, so a hand edit never re-routes billing."""
    from command_center import futuresync

    with Store(tmp_path / "c.db") as store:
        row = store.create_draft("d1", "/r", "aim", launch_profile=LABEL)
    text = futuresync._serialize_registered(  # noqa: SLF001
        row, "cat/repo", [], created="2026-10-29", status="ready"
    )
    assert future_files.parse_job_file(text).launch_profile == LABEL
