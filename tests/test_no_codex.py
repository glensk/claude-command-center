"""The per-job Codex opt-out (``no_codex``) and the one launch-env helper behind it.

``no_codex`` is a promise that NOTHING in the launched session reaches the Codex seat, so
the flag has to survive every hop: the store column + its migration, the future-job file
round trip, and — the part that actually matters — EVERY ccc-owned launch/resume surface,
each of which must export ``CCC_NO_CODEX=1``. The enumeration test below is the guard: a
new launch surface that forgets the helper is a silent, invisible regression.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

import pytest

from command_center import accounts, future_files
from command_center.models import Session, no_codex_conflict
from command_center.store import Store


# --------------------------------------------------------------------------- #
# domain rule + store
# --------------------------------------------------------------------------- #
def test_no_codex_conflicts_only_with_a_codex_job_type() -> None:
    assert no_codex_conflict("claude", True) == ""
    assert no_codex_conflict("codex", False) == ""
    for job_type in ("codex", "codex-write"):
        assert "cannot be combined" in no_codex_conflict(job_type, True)


def test_create_draft_persists_the_flag_and_refuses_the_conflict(tmp_path: Path) -> None:
    with Store(tmp_path / "c.db") as store:
        job = store.create_draft("j1", "/repo/a", "aim", no_codex=True)
        assert job.no_codex is True
        assert store.create_draft("j2", "/repo/a", "aim").no_codex is False
        with pytest.raises(ValueError, match="cannot be combined"):
            store.create_draft("j3", "/repo/a", "aim", job_type="codex-write", no_codex=True)


def test_no_codex_column_is_added_to_a_pre_existing_db(tmp_path: Path) -> None:
    """An older DB (no ``no_codex`` column) migrates in place; existing rows default False."""
    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy = store_mod._SCHEMA.replace("    no_codex          INTEGER NOT NULL DEFAULT 0,\n", "")
    conn = sqlite3.connect(db)
    conn.executescript(legacy)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        old = store.get("old")
        assert old is not None and old.no_codex is False
        store.update_fields("old", no_codex=True)
        refreshed = store.get("old")
        assert refreshed is not None and refreshed.no_codex is True


# --------------------------------------------------------------------------- #
# future-job file round trip
# --------------------------------------------------------------------------- #
def test_job_file_round_trips_the_flag_and_stays_byte_stable_when_unset() -> None:
    plain = future_files.serialize(session_id="11111111-1111-4111-8111-111111111111", aim="a")
    assert "no_codex" not in plain  # unset ⇒ the key is not emitted at all (no churn)
    assert future_files.parse_job_file(plain).no_codex is False

    flagged = future_files.serialize(
        session_id="11111111-1111-4111-8111-111111111111", aim="a", no_codex=True
    )
    assert "no_codex: true" in flagged
    assert future_files.parse_job_file(flagged).no_codex is True
    # Round-trip stable: re-serializing the parsed job reproduces the same document.
    parsed = future_files.parse_job_file(flagged)
    assert (
        future_files.serialize(
            session_id=parsed.session_id, aim=parsed.aim, no_codex=parsed.no_codex
        )
        == flagged
    )


def test_job_file_validation_refuses_the_codex_combination(tmp_path: Path) -> None:
    repo = tmp_path / "cat" / "repo"
    repo.mkdir(parents=True)
    job = future_files.parse_job_file(
        future_files.serialize(
            session_id="11111111-1111-4111-8111-111111111111",
            aim="do it",
            repo="cat/repo",
            job_type="codex",
            no_codex=True,
        )
    )
    errors = future_files.validate(job, tmp_path)
    assert any("cannot be combined" in err for err in errors)


# --------------------------------------------------------------------------- #
# the launch-env helper itself
# --------------------------------------------------------------------------- #
def test_session_launch_env_adds_the_flag_only_when_set() -> None:
    base = {"PATH": "/usr/bin"}
    off = accounts.session_launch_env(Session(session_id="s"), base)
    assert "CCC_NO_CODEX" not in off
    on = accounts.session_launch_env(Session(session_id="s", no_codex=True), base)
    assert on["CCC_NO_CODEX"] == "1"


def test_session_launch_env_preserves_an_ambient_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent shell that already banned Codex keeps that inheritance for every session."""
    env = accounts.session_launch_env(Session(session_id="s"), {"CCC_NO_CODEX": "1"})
    assert env["CCC_NO_CODEX"] == "1"
    monkeypatch.setenv("CCC_NO_CODEX", "1")
    accounts.session_apply_to_environ(Session(session_id="s"))
    assert os.environ["CCC_NO_CODEX"] == "1"


def test_session_launch_env_prefix_exports_the_flag() -> None:
    prefix = accounts.session_launch_env_prefix(accounts.LaunchTarget("", True))
    assert "export CCC_NO_CODEX=1; " in prefix
    assert "CCC_NO_CODEX" not in accounts.session_launch_env_prefix(
        accounts.LaunchTarget("", False)
    )


def test_session_apply_to_environ_sets_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_NO_CODEX", raising=False)
    accounts.session_apply_to_environ(Session(session_id="s"))
    assert "CCC_NO_CODEX" not in os.environ
    accounts.session_apply_to_environ(Session(session_id="s", no_codex=True))
    assert os.environ["CCC_NO_CODEX"] == "1"


# --------------------------------------------------------------------------- #
# every ccc-owned launch/resume surface
# --------------------------------------------------------------------------- #
@pytest.fixture()
def flagged_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A flagged, resumable session row in the default store + a transcript on disk."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CCC_NO_CODEX", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.create_draft("nc", str(repo), "aim", no_codex=True)
        store.update_fields("nc", config_dir="")
    return "nc"


def _tab_prefixes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the shell command strings ``terminal`` would type into a new tab."""
    from command_center import terminal

    seen: list[str] = []

    def fake(command: str, **_: object) -> bool:
        seen.append(command)
        return True

    monkeypatch.setattr(terminal, "_iterm", fake)
    monkeypatch.setattr(terminal, "_launcher_mode", lambda: "iterm")
    monkeypatch.setattr(terminal, "_iterm_api_tab", lambda *_a, **_k: False)
    monkeypatch.setattr(terminal, "_tmux_window", lambda *_a, **_k: False)
    return seen


def test_resume_in_new_tab_carries_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ccc resume-job` / TUI `r` / `ccc jump` all land here — the typed command must pin it."""
    from command_center import terminal

    seen = _tab_prefixes(monkeypatch)
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    assert terminal.resume_in_new_tab("/repo", "sid", "", no_codex=True)
    assert terminal.resume_in_new_tab("/repo", "sid", "", no_codex=False)
    assert "export CCC_NO_CODEX=1; " in seen[0]
    assert "CCC_NO_CODEX" not in seen[1]


def test_resume_halted_in_new_tab_carries_the_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Halted-session recovery (resume.apply_actions → terminal) keeps the ban."""
    from command_center import terminal

    seen = _tab_prefixes(monkeypatch)
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: True)
    script = tmp_path / "claude-session-continue"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    assert terminal.resume_halted_in_new_tab(str(tmp_path), "sid", str(script), "", no_codex=True)
    assert "export CCC_NO_CODEX=1; " in seen[0]


def test_snapshot_restore_command_carries_the_flag(tmp_path: Path) -> None:
    """A restored pane comes back with the same Codex ban it was captured with."""
    from command_center import snapshot

    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="sid", no_codex=True)
    action = snapshot._claude_action(pane, lambda *_a: [])
    assert action.command is not None and "export CCC_NO_CODEX=1; " in action.command
    plain = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="sid")
    assert "CCC_NO_CODEX" not in (snapshot._claude_action(plain, lambda *_a: []).command or "")


def test_snapshot_pane_round_trips_the_flag(tmp_path: Path) -> None:
    from command_center import snapshot

    flagged = snapshot.SnapPane(kind="claude", cwd="/r", session_id="sid", no_codex=True)
    assert snapshot.pane_to_json(flagged)["no_codex"] is True
    assert snapshot.pane_from_json(snapshot.pane_to_json(flagged)).no_codex is True
    plain = snapshot.SnapPane(kind="claude", cwd="/r", session_id="sid")
    assert "no_codex" not in snapshot.pane_to_json(plain)  # byte-identical old snapshots


def test_cmd_resume_execs_with_the_flag(
    flagged_job: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ccc resume` execs claude IN PLACE — the flag must be in os.environ before exec."""
    from command_center import cli, core

    with Store() as store:
        store.update_fields(flagged_job, draft=False)
    monkeypatch.setattr(core, "resume_blockers", lambda *_a, **_k: [])
    monkeypatch.setattr(cli, "has_terminal", lambda: True)
    seen: dict[str, str] = {}

    def fake_exec(_file: str, _argv: list[str]) -> None:
        seen.update(os.environ)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", fake_exec)
    with pytest.raises(SystemExit):
        cli.cmd_resume(argparse.Namespace(session_id=flagged_job))
    assert seen["CCC_NO_CODEX"] == "1"


def test_cmd_fire_attached_execs_with_the_flag(
    flagged_job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attached-prompt delivery is a launch surface too."""
    from command_center import cli

    with Store() as store:
        store.update_fields(flagged_job, draft=False, prompt="go", fire_at=999)
    seen: dict[str, str] = {}

    def fake_exec(_file: str, _argv: list[str]) -> None:
        seen.update(os.environ)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", fake_exec)
    with pytest.raises(SystemExit):
        cli.cmd_fire_attached(argparse.Namespace(session_id=flagged_job))
    assert seen["CCC_NO_CODEX"] == "1"


def test_cmd_start_job_execs_with_the_flag(
    flagged_job: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ccc start-job` (and every tab that runs it) launches with Codex banned."""
    from command_center import cli

    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    seen: dict[str, str] = {}

    def fake_exec(_file: str, _argv: list[str]) -> None:
        seen.update(os.environ)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", fake_exec)
    with pytest.raises(SystemExit):
        cli.cmd_start_job(argparse.Namespace(session_id=flagged_job, force=True, auto=False))
    assert seen["CCC_NO_CODEX"] == "1"


def test_cmd_start_job_refuses_a_conflicting_row(
    flagged_job: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-corrupted row (codex job type + no_codex) is refused BEFORE the draft claim."""
    from command_center import cli

    with Store() as store:  # bypass create_draft's guard the way a bad import would
        store.update_fields(flagged_job, job_type="codex")
    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")
    monkeypatch.setattr(
        os, "execvp", lambda *_a, **_k: pytest.fail("start-job launched a conflicting row")
    )
    assert (
        cli.cmd_start_job(argparse.Namespace(session_id=flagged_job, force=True, auto=False)) == 1
    )
    assert "cannot be combined" in capsys.readouterr().err
    with Store() as store:  # nothing mutated: it is still a draft
        row = store.get(flagged_job)
        assert row is not None and row.draft is True


def test_no_launch_surface_bypasses_the_session_helper() -> None:
    """Static guard: nothing outside accounts.py may pin an account WITHOUT the session flags.

    ``apply_to_environ`` / ``launch_env_prefix`` are the account-only renderings; a launch
    surface that calls them directly silently drops ``CCC_NO_CODEX``. The two allowed
    exceptions are ccc's OWN headless calls, which launch no session at all: ``llm._run_claude``
    and the per-account rate-limit reset detector in ``resume``.
    """
    package = Path(accounts.__file__).parent
    allowed = {"accounts.py"}
    offenders: list[str] = []
    for module in sorted(package.rglob("*.py")):
        if module.name in allowed:
            continue
        for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
            if "apply_to_environ(" in line or "launch_env_prefix(" in line:
                if "session_apply_to_environ(" in line or "session_launch_env_prefix(" in line:
                    continue
                offenders.append(f"{module.relative_to(package)}:{number}: {line.strip()}")
    assert offenders == [], "launch surfaces bypassing accounts.session_* :\n" + "\n".join(
        offenders
    )


def test_every_resume_in_new_tab_call_passes_the_flag() -> None:
    """Static guard: each ``resume_in_new_tab`` caller must forward the session's flag."""
    package = Path(accounts.__file__).parent
    missing: list[str] = []
    for module in sorted(package.rglob("*.py")):
        if module.name == "terminal.py":  # the definition + its own docstring
            continue
        text = module.read_text(encoding="utf-8")
        for call in text.split("resume_in_new_tab(")[1:]:
            head = call[: call.index(")") + 1] if ")" in call else call
            if "no_codex" not in head:
                missing.append(f"{module.relative_to(package)}: resume_in_new_tab({head}")
    assert missing == [], "resume surfaces dropping no_codex:\n" + "\n".join(missing)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_new_job_flag_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import cli

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    parser = cli.build_parser()
    args = parser.parse_args(["new-job", "-a", "do x", "-c", str(tmp_path), "-N"])
    assert args.no_codex is True
    assert cli.cmd_new_job(args) == 0
    with Store() as store:
        jobs = [s for s in store.list_sessions() if s.draft]
        assert len(jobs) == 1 and jobs[0].no_codex is True
    bad = parser.parse_args(["new-job", "-a", "y", "-c", str(tmp_path), "-N", "-j", "codex"])
    assert cli.cmd_new_job(bad) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_jobs_list_marks_a_no_codex_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import cli

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    with Store() as store:
        store.create_draft("nc", str(tmp_path), "banned", no_codex=True)
        store.create_draft("ok", str(tmp_path), "normal")
    cli.cmd_jobs(argparse.Namespace(json=False))
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert any("[no-codex]" in line for key, line in lines.items() if "banned" in line)
    assert all("[no-codex]" not in line for line in lines.values() if "normal" in line)
