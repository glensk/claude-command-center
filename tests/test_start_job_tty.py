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
import json
import os
from pathlib import Path

import pytest

from command_center.store import Store


class _StopExec(Exception):
    """Raised in place of os.execvp so a test can tell that the exec was reached."""


def _make_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sid: str = "job-tty",
    cwd: str = "/no/such/dir",
    trusted: bool = True,
) -> Store:
    """A minimal launchable draft in an isolated store (no future_file → no file sync).

    *trusted* pre-trusts *cwd* for the default account (as its registration would), so a
    test of another guard is not stopped by the tp#1512 trust gate first.
    """
    from command_center import accounts

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress the detached sync-mirrors spawn
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(sid, cwd, "Do the thing", prompt="run it")
    store.close()
    if trusted:
        assert accounts.ensure_trusted("", cwd) is True
    return Store(tmp_path / "command-center" / "state.db")


def _claude_json() -> dict:
    """The pinned default account's ``.claude.json`` (the conftest seeds ``{}``)."""
    return json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))


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

    def _fake_tab(
        session_id: str, force: bool = False, auto: bool = False, no_grant: bool = False
    ) -> bool:
        opened.update(session_id=session_id, force=force, auto=auto, no_grant=no_grant)
        return True

    monkeypatch.setattr("command_center.terminal.start_job_in_new_tab", _fake_tab)

    def _boom(_file: str, _argv: list[str]) -> None:
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _boom)

    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=True, auto=False))
    assert rc == 0
    assert opened == {"session_id": "job-tty", "force": True, "auto": False, "no_grant": False}
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    assert "needs a terminal" in capsys.readouterr().err  # the tab guard, not the trust gate
    session = store.get("job-tty")
    assert session is not None
    assert session.fire_at == 0  # disarmed
    assert session.draft == 1  # but still launchable by hand
    store.close()


# ------------------------------------------------------- tp#1512: the trust gate


def _exec_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Record the exec (and the cwd it would run in) instead of replacing the process."""
    seen: dict[str, object] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        seen.update(argv=argv, cwd=os.getcwd())
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    return seen


def test_start_job_trusts_an_untrusted_cwd_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual launch trusts the job's folder itself, then execs (no dialog to park on)."""
    from command_center import accounts, cli

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_draft(tmp_path, monkeypatch, cwd=str(repo), trusted=False).close()
    assert not accounts.is_trusted("", repo)
    _a_tty(monkeypatch)
    seen = _exec_stub(monkeypatch)
    with pytest.raises(_StopExec):
        cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=False))
    assert accounts.is_trusted("", repo)
    assert seen["cwd"] == str(repo.resolve())
    # pin_environ, not apply_to_environ: the LAUNCHER's cwd is never trusted on the way
    assert set(_claude_json()["projects"]) == {str(repo.resolve())}


def test_start_job_auto_refuses_an_untrusted_cwd_and_disarms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--auto`` only checks: an untrusted folder disarms, keeps the draft, writes nothing."""
    from command_center import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    store = _make_draft(tmp_path, monkeypatch, cwd=str(repo), trusted=False)
    store.update_fields("job-tty", fire_at=1234)
    _a_tty(monkeypatch)
    seen = _exec_stub(monkeypatch)
    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=True))
    assert rc == 1 and not seen
    assert "not trusted" in capsys.readouterr().err
    session = store.get("job-tty")
    assert session is not None and session.draft == 1 and session.fire_at == 0
    assert _claude_json() == {}  # no trust written
    store.close()


def test_start_job_auto_untrusted_opens_no_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--auto`` pre-check runs BEFORE the no-terminal branch: no error tab opens."""
    from command_center import cli

    store = _make_draft(tmp_path, monkeypatch, cwd=str(tmp_path), trusted=False)
    store.update_fields("job-tty", fire_at=1234)
    _no_tty(monkeypatch)

    def _no_tab(*_a: object, **_k: object) -> bool:
        raise AssertionError("an untrusted --auto dispatch must not open a tab")

    monkeypatch.setattr("command_center.terminal.start_job_in_new_tab", _no_tab)
    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=True))
    assert rc == 1
    session = store.get("job-tty")
    assert session is not None and session.fire_at == 0 and session.draft == 1
    store.close()


def test_start_job_refusal_happens_before_claim_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trust refusal never reaches ``claim_draft`` (the draft is not consumed)."""
    from command_center import accounts, cli

    store = _make_draft(tmp_path, monkeypatch, cwd=str(tmp_path), trusted=False)
    _a_tty(monkeypatch)
    _exec_stub(monkeypatch)
    monkeypatch.setattr(accounts, "is_trusted", lambda *_a, **_k: False)

    def _no_claim(*_a: object, **_k: object) -> bool:
        raise AssertionError("claim_draft reached on a trust refusal")

    monkeypatch.setattr(Store, "claim_draft", _no_claim)
    for auto in (False, True):
        rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=auto))
        assert rc == 1
    session = store.get("job-tty")
    assert session is not None and session.draft == 1
    store.close()


def test_start_job_manual_refuses_when_the_account_has_no_claude_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``.claude.json``: ``ensure_trusted`` writes nothing, so the manual path refuses."""
    from command_center import cli

    store = _make_draft(tmp_path, monkeypatch, cwd=str(tmp_path), trusted=False)
    (Path.home() / ".claude.json").unlink()
    _a_tty(monkeypatch)
    seen = _exec_stub(monkeypatch)
    rc = cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=False))
    assert rc == 1 and not seen
    assert "has no .claude.json; log in once with it" in capsys.readouterr().err
    session = store.get("job-tty")
    assert session is not None and session.draft == 1
    store.close()


def test_start_job_auto_accepts_a_trusted_parent_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code honours a trusted parent (outside a git repo), so the gate does too."""
    from command_center import accounts, cli

    sub = tmp_path / "parent" / "child"
    sub.mkdir(parents=True)
    store = _make_draft(tmp_path, monkeypatch, cwd=str(sub), trusted=False)
    assert accounts.ensure_trusted("", tmp_path / "parent") is True
    _a_tty(monkeypatch)
    seen = _exec_stub(monkeypatch)
    with pytest.raises(_StopExec):
        cli.cmd_start_job(argparse.Namespace(session_id="job-tty", force=False, auto=True))
    assert seen["cwd"] == str(sub.resolve())
    assert str(sub.resolve()) not in _claude_json()["projects"]  # --auto granted nothing
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

    def _fake_tab(cwd: str, session_id: str, config_dir: str = "", **_: object) -> bool:
        opened.update(cwd=cwd, session_id=session_id, config_dir=config_dir)
        return True

    monkeypatch.setattr("command_center.terminal.resume_in_new_tab", _fake_tab)

    def _boom(_file: str, _argv: list[str]) -> None:
        raise AssertionError("resume must not exec headless")

    monkeypatch.setattr("command_center.cli.os.execvp", _boom)

    assert cli.cmd_resume(argparse.Namespace(session_id="sess-1")) == 0
    assert opened["session_id"] == "sess-1"


# ------------------------------------------------- tp#1528: start-job --no-grant


def test_start_job_no_grant_refuses_an_untrusted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--no-grant`` only checks: its own wording, nothing trusted, draft and fire_at kept."""
    from command_center import accounts, cli

    repo = tmp_path / "repo"
    repo.mkdir()
    store = _make_draft(tmp_path, monkeypatch, cwd=str(repo), trusted=False)
    store.update_fields("job-tty", fire_at=1234)
    _a_tty(monkeypatch)
    seen = _exec_stub(monkeypatch)
    rc = cli.cmd_start_job(
        argparse.Namespace(session_id="job-tty", force=True, auto=False, no_grant=True)
    )
    err = capsys.readouterr().err
    assert rc == 1 and not seen
    assert "the launch toggle never trusts a folder" in err
    assert "auto-fire blocked" not in err and "could not be trusted" not in err
    assert not accounts.is_trusted("", repo, ancestors=True)
    assert _claude_json() == {}
    session = store.get("job-tty")
    assert session is not None and session.draft == 1 and session.fire_at == 1234
    store.close()


def test_start_job_no_grant_passes_a_trusted_cwd_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted folder passes the ``--no-grant`` gate; ``.claude.json`` is not rewritten."""
    from command_center import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_draft(tmp_path, monkeypatch, cwd=str(repo), trusted=True).close()
    before = (Path.home() / ".claude.json").read_text(encoding="utf-8")
    _a_tty(monkeypatch)
    seen = _exec_stub(monkeypatch)
    with pytest.raises(_StopExec):
        cli.cmd_start_job(
            argparse.Namespace(session_id="job-tty", force=True, auto=False, no_grant=True)
        )
    assert seen["cwd"] == str(repo.resolve())
    assert (Path.home() / ".claude.json").read_text(encoding="utf-8") == before


def test_start_job_no_grant_survives_the_tab_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No terminal: the re-opened tab carries ``no_grant`` (or it would grant there)."""
    from command_center import cli

    _make_draft(tmp_path, monkeypatch).close()
    _no_tty(monkeypatch)
    opened: dict[str, object] = {}

    def _fake_tab(session_id: str, **kwargs: object) -> bool:
        opened.update(kwargs, session_id=session_id)
        return True

    monkeypatch.setattr("command_center.terminal.start_job_in_new_tab", _fake_tab)
    _exec_stub(monkeypatch)
    rc = cli.cmd_start_job(
        argparse.Namespace(session_id="job-tty", force=True, auto=False, no_grant=True)
    )
    assert rc == 0
    assert opened == {"session_id": "job-tty", "force": True, "auto": False, "no_grant": True}
