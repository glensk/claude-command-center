"""The terminal guard on ``ccc start-job`` / ``ccc resume``.

Both commands ``execvp`` claude IN PLACE. Run without a TTY (a pipe, ``</dev/null``,
an agent's ``run_in_background`` shell) the launched process gets no stdin, consumes its
argv prompt as a headless ONE-SHOT and exits after a single turn. Its transcript then
opens with a ``queue-operation`` record — precisely what ``is_oneshot_headless`` matches
— so the daemon's ``prune_headless`` self-heal deletes the row and the job disappears
from ccc with no tab and no error anywhere. Job 42fc3505 was lost exactly that way on
2026-08-28 (``ccc start-job -u`` from a background shell instead of ``ccc open-job``).

The guard turns that into: open a real tab, or refuse — never a silent headless run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from command_center.store import Store


class _StopExec(Exception):
    """Raised in place of os.execvp so a test can tell that the exec was reached."""


def _make_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sid: str = "job-tty") -> Store:
    """A minimal launchable draft in an isolated store (no future_file → no file sync)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress the detached sync-mirrors spawn
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(sid, "/no/such/dir", "Do the thing", prompt="run it")
    store.close()
    return Store(tmp_path / "command-center" / "state.db")


def _no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the suite-wide opt-out and report no terminal on either stream."""
    monkeypatch.delenv("CCC_START_JOB_HEADLESS", raising=False)
    monkeypatch.setattr("command_center.cli.sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setattr("command_center.cli.sys.stdout.isatty", lambda: False, raising=False)


def _a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the opt-out but report a real terminal, so the exec path is taken."""
    monkeypatch.delenv("CCC_START_JOB_HEADLESS", raising=False)
    monkeypatch.setattr("command_center.cli.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("command_center.cli.sys.stdout.isatty", lambda: True, raising=False)


def test_has_terminal_env_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented escape hatch forces True even with both streams redirected."""
    from command_center.cli import has_terminal

    _no_tty(monkeypatch)
    assert has_terminal() is False
    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")
    assert has_terminal() is True


def test_has_terminal_needs_both_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTY on only one stream is not enough — `ccc start-job | tee` must not exec."""
    from command_center.cli import has_terminal

    monkeypatch.delenv("CCC_START_JOB_HEADLESS", raising=False)
    monkeypatch.setattr("command_center.cli.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("command_center.cli.sys.stdout.isatty", lambda: False, raising=False)
    assert has_terminal() is False


def test_start_job_without_tty_opens_a_tab_instead_of_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No terminal → hand the launch to a real tab, and do NOT exec here."""
    from command_center import cli

    store = _make_draft(tmp_path, monkeypatch)
    _no_tty(monkeypatch)

    opened: dict[str, object] = {}

    def _fake_tab(session_id: str, force: bool = False, auto: bool = False) -> bool:
        opened.update(session_id=session_id, force=force, auto=auto)
        return True

    monkeypatch.setattr("command_center.terminal.start_job_in_new_tab", _fake_tab)

    def _boom(_file: str, _argv: list[str]) -> None:
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _boom)

    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=True, auto=False))
    assert rc == 0
    assert opened == {"session_id": "job-tty", "force": True, "auto": False}
    # The draft must be untouched: the tab's own `ccc start-job` is what claims it.
    session = store.get("job-tty")
    assert session is not None
    assert session.draft == 1
    assert session.archived == 0
    store.close()


def test_start_job_without_tty_refuses_when_no_tab_can_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tab launch failed → exit 1 rather than exec a doomed headless one-shot."""
    from command_center import cli

    store = _make_draft(tmp_path, monkeypatch)
    _no_tty(monkeypatch)
    monkeypatch.setattr(
        "command_center.terminal.start_job_in_new_tab",
        lambda *a, **k: False,  # noqa: ARG005
    )

    def _boom(_file: str, _argv: list[str]) -> None:
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _boom)

    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=False))
    assert rc == 1
    assert "needs a terminal" in capsys.readouterr().err
    session = store.get("job-tty")
    assert session is not None
    assert session.draft == 1  # nothing mutated on the refusal path
    store.close()


def test_start_job_auto_disarms_when_no_tab_can_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unattended dispatch that cannot get a tab disarms instead of retrying forever."""
    from command_center import cli

    store = _make_draft(tmp_path, monkeypatch)
    store.update_fields("job-tty", fire_at=1234)
    _no_tty(monkeypatch)
    monkeypatch.setattr(
        "command_center.terminal.start_job_in_new_tab",
        lambda *a, **k: False,  # noqa: ARG005
    )

    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=True))
    assert rc == 1
    session = store.get("job-tty")
    assert session is not None
    assert session.fire_at == 0  # disarmed
    assert session.draft == 1  # but still launchable by hand
    store.close()


def test_start_job_with_tty_still_execs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal in-tab path is unchanged: a real terminal execs claude as before."""
    from command_center import cli

    _make_draft(tmp_path, monkeypatch).close()
    _a_tty(monkeypatch)

    def _tab_must_not_run(*_a: object, **_k: object) -> bool:
        raise AssertionError("start_job_in_new_tab must not be used when a TTY is present")

    monkeypatch.setattr("command_center.terminal.start_job_in_new_tab", _tab_must_not_run)

    captured: dict[str, list[str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):
        cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=False))
    assert captured["argv"][0] == "claude"


def test_resume_without_tty_opens_a_tab_instead_of_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ccc resume` carries the same guard — it execs in place too."""
    from command_center import cli

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("sess-1", str(tmp_path))
    store.close()
    _no_tty(monkeypatch)
    monkeypatch.setattr("command_center.accounts.is_multi_account", lambda: False)
    monkeypatch.setattr("command_center.accounts.live_conflict", lambda _sid: False)
    monkeypatch.setattr(
        "command_center.cli._adapter",
        lambda: type("A", (), {"transcript_path": lambda *a, **k: Path("/x.jsonl")})(),
    )

    opened: dict[str, object] = {}

    def _fake_tab(cwd: str, session_id: str, config_dir: str = "") -> bool:
        opened.update(cwd=cwd, session_id=session_id, config_dir=config_dir)
        return True

    monkeypatch.setattr("command_center.terminal.resume_in_new_tab", _fake_tab)

    def _boom(_file: str, _argv: list[str]) -> None:
        raise AssertionError("resume must not exec headless")

    monkeypatch.setattr("command_center.cli.os.execvp", _boom)

    assert cli.cmd_resume(argparse.Namespace(session_id="sess-1")) == 0
    assert opened["session_id"] == "sess-1"
