"""The session-start choke point for ``AI_AUTOMATED`` callers (tp#1698 B6).

An automated launcher never starts a Claude session: under ``AI_AUTOMATED=1`` every start
path refuses (CLI verbs exit 75, the in-process seam in :mod:`terminal` opens nothing),
``new-job`` still registers but strips fire arming and stamps ``registered_by=automated``,
and no auto-start path (daemon reset fire, the job file's launch toggle) ever launches a
stamped job. ``observe`` only logs. The classifier keys ONLY on the explicit env value:
no tty + no ``CLAUDECODE`` + no ``AI_AUTOMATED`` is the user's Obsidian/phone click and
stays allowed.
"""

# pylint: disable=redefined-outer-name  # pytest fixtures are injected by name
from __future__ import annotations

import argparse
import importlib
import json
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from futurestub import future_env

from command_center import automated, cli, config, futuresync, terminal, usage
from command_center.store import Store
from command_center.views import tui

# Loaded by name: a static import of these two re-reports the package's old
# cli <-> park cycle against this file (pylint R0401 cannot be silenced inline).
daemon = importlib.import_module("command_center.daemon")
park = importlib.import_module("command_center.park")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME + repo dir, no AI_AUTOMATED / CLAUDECODE inherited."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(automated.ENV, raising=False)
    monkeypatch.delenv(automated.LAUNCHER_ENV, raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _recorder(sink: list[str], fixed: str = "") -> Callable[..., bool]:
    """A fake launcher: record its first argument (or *fixed*) and report success."""

    def record(*args: object, **_kwargs: object) -> bool:
        sink.append(fixed or str(args[0]))
        return True

    return record


@pytest.fixture()
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every command the launch ladder would type into a new tab (nothing opens)."""
    calls: list[str] = []

    def fake_open_tab(command: str, *, tmux_fallback: bool) -> str:
        del tmux_fallback
        calls.append(command)
        return terminal.LAUNCHER_ITERM_APPLESCRIPT

    monkeypatch.setattr(terminal, "_launcher_mode", lambda: "iterm")
    monkeypatch.setattr(terminal, "_open_tab", fake_open_tab)
    monkeypatch.setattr(terminal, "_tmux_window", _recorder(calls, "tmux"))
    return calls


def _draft(repo: Path, *, registered_by: str = "", key: str = "") -> str:
    session_id = str(uuid.uuid4())
    with Store() as store:
        store.create_draft(
            session_id, str(repo), "do x", registered_by=registered_by, idempotency_key=key
        )
    return session_id


def _events() -> list[tuple[str, dict[str, object]]]:
    path = config.app_home() / "events.log"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if parts[2].startswith("automated-"):
            out.append((parts[2], json.loads(parts[3])))
    return out


def _new_job(repo: Path, *extra: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["new-job", "-a", "do x", "-c", str(repo), *extra])


# --------------------------------------------------------------------------- #
# the classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", automated.MODE_ENFORCE),
        ("enforce", automated.MODE_ENFORCE),
        ("observe", automated.MODE_OBSERVE),
        ("", automated.MODE_OFF),
        ("0", automated.MODE_OFF),
    ],
)
def test_mode_keys_only_on_the_explicit_value(value: str, expected: str) -> None:
    assert automated.mode({automated.ENV: value}) == expected
    assert automated.mode({}) == automated.MODE_OFF


# --------------------------------------------------------------------------- #
# refuse (enforce)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("verb", ["open-job", "start-job"])
def test_enforce_refuses_job_start_verbs_with_75(
    repo: Path,
    opened: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    sid = _draft(repo, key="staff-start:x:pending")
    monkeypatch.setenv(automated.ENV, "1")
    monkeypatch.setenv(automated.LAUNCHER_ENV, "test-launcher")
    execs: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execvp", lambda f, argv: execs.append(argv))
    assert cli.main([verb, sid]) == automated.EXIT_SKIPPED
    assert automated.SKIP_MESSAGE in capsys.readouterr().err
    assert not opened and not execs
    with Store() as store:
        job = store.get(sid)
        assert job is not None and job.draft  # untouched: still a draft for the user
    events = _events()
    assert [e for e, _ in events] == [automated.EVENT_REFUSED]
    detail = events[0][1]
    assert detail["launcher"] == "test-launcher"
    assert detail["job_key"] == "staff-start:x:pending"
    assert detail["cwd"] == str(repo)
    assert isinstance(detail["ts"], int)


def test_enforce_refuses_resume_job(
    repo: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _draft(repo)
    with Store() as store:
        store.update_fields(sid, draft=False)

    class _NoLiveSessions:  # pylint: disable=too-few-public-methods
        def discover(self) -> list[object]:
            return []

    monkeypatch.setattr(cli, "_adapter", _NoLiveSessions)
    monkeypatch.setenv(automated.ENV, "1")
    assert cli.main(["resume-job", sid]) == automated.EXIT_SKIPPED
    assert cli.main(["resume", sid]) == automated.EXIT_SKIPPED
    assert not opened


def test_enforce_refuses_park(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(automated.ENV, "1")
    monkeypatch.chdir(repo)
    assert park.run_park(argparse.Namespace(grab=False)) == automated.EXIT_SKIPPED
    with Store() as store:
        assert [s for s in store.list_sessions() if s.draft] == []


@pytest.mark.parametrize(
    "launch",
    [
        terminal.start_job_launch,  # open-job / continue-session seam
        lambda sid: terminal.start_job_in_new_tab(sid, force=True),  # the TUI's `r`
        lambda sid: terminal.start_job_in_new_tab(sid, auto=True),  # daemon / park
        lambda sid: terminal.start_job_in_new_tab(sid, force=True, no_grant=True),  # toggle
    ],
)
def test_enforce_in_process_seam_opens_nothing(
    repo: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch, launch: object
) -> None:
    sid = _draft(repo)
    monkeypatch.setenv(automated.ENV, "1")
    assert not launch(sid)  # type: ignore[operator]
    assert not opened
    assert [e for e, _ in _events()] == [automated.EVENT_REFUSED]


def test_enforce_resume_seams_open_nothing(
    repo: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(automated.ENV, "1")
    assert terminal.resume_in_new_tab(str(repo), "s1") is False
    assert terminal.resume_halted_in_new_tab(str(repo), "s1", "/bin/true") is False
    assert not opened


# --------------------------------------------------------------------------- #
# observe
# --------------------------------------------------------------------------- #
def test_observe_logs_but_starts(
    repo: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _draft(repo)
    monkeypatch.setenv(automated.ENV, "observe")
    assert cli.main(["open-job", sid]) == 0
    assert len(opened) == 1 and f"ccc start-job {sid}" in opened[0]
    assert {e for e, _ in _events()} == {automated.EVENT_OBSERVED}
    assert automated.EVENT_REFUSED not in {e for e, _ in _events()}


def test_observe_new_job_changes_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(automated.ENV, "observe")
    monkeypatch.setattr(park, "fire_time", lambda *_a, **_k: 2_000_000_000)
    assert cli.cmd_new_job(_new_job(repo, "-R", "-K", "k-observe")) == 0
    with Store() as store:
        (job,) = [s for s in store.list_sessions() if s.draft]
    assert job.registered_by == "" and job.fire_at == 2_000_000_000
    assert not tui._draft_next_cell(job, "").plain.strip().startswith(automated.WAITING_NOTE)
    assert [e for e, _ in _events()] == [automated.EVENT_OBSERVED]


# --------------------------------------------------------------------------- #
# new-job: allowed, fire arming stripped, stamped
# --------------------------------------------------------------------------- #
def test_enforce_new_job_registers_stamped_and_strips_arming(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(automated.ENV, "1")
    monkeypatch.setattr(park, "fire_time", lambda *_a, **_k: 2_000_000_000)
    assert cli.cmd_new_job(_new_job(repo, "-R", "-K", "k1", "-J")) == 0
    out = capsys.readouterr()
    assert "--at-reset ignored" in out.err
    first = json.loads(out.out)
    assert cli.cmd_new_job(_new_job(repo, "-K", "k1", "-J")) == 0  # the next tick
    second = json.loads(capsys.readouterr().out)
    assert second["session_id"] == first["session_id"] and second["created"] is False
    with Store() as store:
        drafts = [s for s in store.list_sessions() if s.draft]
    assert len(drafts) == 1
    job = drafts[0]
    assert job.registered_by == automated.REGISTERED_BY_AUTOMATED
    assert job.fire_at == 0 and job.fire_window == ""
    assert automated.WAITING_NOTE in tui._draft_next_cell(job, "").plain


def test_enforce_retrieval_stamps_an_older_unstamped_job(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_new_job(_new_job(repo, "-K", "k2")) == 0
    monkeypatch.setenv(automated.ENV, "1")
    assert cli.cmd_new_job(_new_job(repo, "-K", "k2")) == 0
    capsys.readouterr()
    with Store() as store:
        (job,) = [s for s in store.list_sessions() if s.draft]
    assert job.registered_by == automated.REGISTERED_BY_AUTOMATED


# --------------------------------------------------------------------------- #
# stamped jobs: never auto-started; an explicit start by the user does start them
# --------------------------------------------------------------------------- #
def test_daemon_never_fires_a_stamped_job(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[str] = []
    monkeypatch.setattr(terminal, "start_job_in_new_tab", _recorder(launched))
    monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    due = int(time.time()) - park.FIRE_GRACE_SEC - 5
    stamped = _draft(repo, registered_by=automated.REGISTERED_BY_AUTOMATED)
    plain = _draft(repo)
    with Store() as store:
        store.update_fields(stamped, fire_at=due, fire_window="five_hour")
        store.update_fields(plain, fire_at=due, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=False
        )
    assert launched == [plain] and report.reset_fired == [plain]


@pytest.fixture()
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[object, Store]]:
    fe = future_env(tmp_path, monkeypatch)
    monkeypatch.delenv(automated.ENV, raising=False)
    store = Store(tmp_path / "claude" / "command-center" / "state.db")
    yield fe, store
    store.close()


def test_launch_toggle_never_starts_a_stamped_job(
    vault: tuple[object, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import accounts  # pylint: disable=import-outside-toplevel

    fe, store = vault
    spawned: list[str] = []
    monkeypatch.setattr(futuresync, "_spawn_launch", _recorder(spawned))
    folder = fe.git_base / "home" / "ccc"  # type: ignore[attr-defined]
    folder.mkdir(parents=True, exist_ok=True)
    accounts.ensure_trusted("", folder)
    sid = str(uuid.uuid4())
    store.create_draft(
        sid, str(folder), "Automated draft", registered_by=automated.REGISTERED_BY_AUTOMATED
    )
    cfg = fe.cfg  # type: ignore[attr-defined]
    futuresync.run_sync(store, cfg)
    job = store.get(sid)
    assert job is not None and job.future_file
    path = futuresync._abs_path(cfg, job.future_file)  # pylint: disable=protected-access
    path.write_text(
        path.read_text(encoding="utf-8").replace("launch: false", "launch: true"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(store, cfg)
    assert not spawned and sid not in report.launched
    assert any("automated draft" in d for d in report.details)
    assert "never starts an automated job" in path.read_text(encoding="utf-8")


def test_explicit_start_of_a_stamped_job_is_allowed(repo: Path, opened: list[str]) -> None:
    """The TUI's `r`, `ccc open-job` and the Obsidian button (open-job -f) start it."""
    sid = _draft(repo, registered_by=automated.REGISTERED_BY_AUTOMATED)
    assert terminal.start_job_in_new_tab(sid, force=True)  # TUI
    assert cli.main(["open-job", sid]) == 0  # CLI / Obsidian button
    assert len(opened) == 2
    assert not _events()


def test_no_tty_start_without_ai_automated_is_allowed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tty + no CLAUDECODE + no AI_AUTOMATED = the user's Obsidian/phone click."""
    monkeypatch.delenv("CCC_START_JOB_HEADLESS", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    hops: list[str] = []
    monkeypatch.setattr(terminal, "start_job_in_new_tab", _recorder(hops))
    sid = _draft(repo)
    assert not cli.has_terminal()
    assert cli.main(["start-job", sid]) == 0
    assert hops == [sid]  # handed to a real tab, not refused
    assert not _events()
