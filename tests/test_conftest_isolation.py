"""Regression tests for the suite-wide env isolation in ``tests/conftest.py``."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import NoReturn

import conftest
import pytest
from conftest import _is_ambient_claude_env


def test_ambient_claude_env_is_scrubbed() -> None:
    """No ambient ``CLAUDE_*`` / ``CODEX_IN_CLAUDE_*`` / ``CLAUDECODE`` reaches a test (tp#233)."""
    leaked = sorted(key for key in os.environ if _is_ambient_claude_env(key))
    assert not leaked, f"ambient Claude env leaked into the test: {leaked}"
    assert "CLAUDE_HOME" in os.environ  # pinned by _pin_claude_home, not scrubbed


def test_scrub_pattern() -> None:
    assert _is_ambient_claude_env("CLAUDE_PID")
    assert _is_ambient_claude_env("CLAUDE_SESSION_AIM")
    assert _is_ambient_claude_env("CLAUDECODE")
    assert _is_ambient_claude_env("CODEX_IN_CLAUDE_IGNORE_QUOTA")
    assert not _is_ambient_claude_env("CLAUDE_HOME")
    assert not _is_ambient_claude_env("CODEX_HOME")
    assert not _is_ambient_claude_env("HOME")


# --------------------------------------------------------------------------- #
# The kill guard and its out-of-tmp audit file (tp#1793 B1)
# --------------------------------------------------------------------------- #
DEAD_FIXTURE_PID = 99_999_999  # outside the macOS / Linux pid range: never a live process


@pytest.fixture(name="audit")
def _audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit file at this test's tmp dir (never the real default)."""
    path = tmp_path / "kill-audit.log"
    monkeypatch.setenv("CCC_TEST_KILL_AUDIT", str(path))
    monkeypatch.delenv("CCC_TEST_KILL_STRICT", raising=False)
    return path


def _events(path: Path) -> list[dict]:
    """The audit records of *path*, header lines dropped."""
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [record for record in records if record.get("event") != "header"]


class _FakeKill:
    """Installed UNDER the guard: answers the signal-0 probe "alive", records the rest."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))


def test_kill_guard_allows_signal_zero(audit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKill()
    monkeypatch.setattr(conftest, "_REAL_KILL", fake)
    os.kill(1, 0)
    assert fake.calls == [(1, 0)]
    assert not _events(audit)


def test_kill_guard_refuses_foreign_pid(audit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKill()
    monkeypatch.setattr(conftest, "_REAL_KILL", fake)
    with pytest.raises(ProcessLookupError):
        os.kill(1, signal.SIGTERM)
    assert fake.calls == [(1, 0)]  # the probe only: the real SIGTERM was never sent
    (event,) = _events(audit)
    assert event["verdict"] == "foreign"
    assert event["pid"] == 1
    assert event["sig"] == signal.SIGTERM
    assert event["channel"] == "os.kill"
    assert event["nodeid"].endswith("test_kill_guard_refuses_foreign_pid")
    header = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert header["event"] == "header"
    assert {"os.kill", "os.killpg", "log_close"} <= set(header["channels"])


def test_kill_guard_strict_raises_assertion(audit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKill()
    monkeypatch.setattr(conftest, "_REAL_KILL", fake)
    monkeypatch.setenv("CCC_TEST_KILL_STRICT", "1")
    with pytest.raises(AssertionError, match="foreign pid 1"):
        os.kill(1, signal.SIGTERM)
    assert fake.calls == [(1, 0)]
    assert [event["verdict"] for event in _events(audit)] == ["foreign"]


def test_kill_guard_refuses_kill_all(audit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKill()
    monkeypatch.setattr(conftest, "_REAL_KILL", fake)
    with pytest.raises(ProcessLookupError):
        os.kill(-1, signal.SIGTERM)
    assert not fake.calls
    assert [event["verdict"] for event in _events(audit)] == ["foreign"]


def test_kill_guard_refuses_foreign_group(audit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKill()
    monkeypatch.setattr(conftest, "_REAL_KILLPG", fake)
    with pytest.raises(ProcessLookupError):
        os.killpg(1, signal.SIGTERM)
    assert fake.calls == [(1, 0)]
    (event,) = _events(audit)
    assert (event["channel"], event["verdict"]) == ("os.killpg", "foreign")


def test_kill_guard_allows_own_child(audit: Path) -> None:
    with subprocess.Popen(["sleep", "30"]) as child:
        os.kill(child.pid, signal.SIGTERM)
        assert child.wait(timeout=10) == -signal.SIGTERM
    assert not _events(audit)


def test_kill_guard_dead_fixture_pid(audit: Path) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(DEAD_FIXTURE_PID, signal.SIGTERM)
    (event,) = _events(audit)
    assert event["verdict"] == "would-hit-on-recycle"
    assert event["pid"] == DEAD_FIXTURE_PID


def test_kill_guard_walk_survives_patched_popen(
    audit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("the walk used the patched subprocess.Popen")

    with subprocess.Popen(["sleep", "30"]) as child:
        monkeypatch.setattr(subprocess, "Popen", _refuse)
        os.kill(child.pid, signal.SIGTERM)
        assert child.wait(timeout=10) == -signal.SIGTERM
    assert not _events(audit)


def test_kill_guard_mirrors_log_close(audit: Path) -> None:
    from command_center import closelog, config

    closelog.log_close("sid-1793", 4242, "close-now", note="signalled=yes", who="tester")
    (event,) = _events(audit)
    assert event["verdict"] == "log_close"
    assert event["channel"] == "log_close"
    assert (event["session_id"], event["pid"], event["path"]) == ("sid-1793", 4242, "close-now")
    assert (event["note"], event["who"]) == ("signalled=yes", "tester")
    events_log = (config.app_home() / "events.log").read_text(encoding="utf-8")
    assert "sid-1793\tsession-close\tpid=4242 path=close-now caller=tester" in events_log


@pytest.mark.real_signal
def test_kill_guard_real_signal_opt_out() -> None:
    assert os.kill is conftest._REAL_KILL
    assert os.killpg is conftest._REAL_KILLPG


def test_kill_guard_dead_own_child_is_not_logged(audit: Path) -> None:
    with subprocess.Popen(["true"]) as child:
        child.wait(timeout=10)
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, signal.SIGTERM)
    assert not _events(audit)


def test_kill_guard_sweeps_own_group_after_leader_exit(audit: Path) -> None:
    """A group outliving its leader (a forked grandchild) is still ours to sweep."""
    with subprocess.Popen(
        ["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as leader:
        out, _ = leader.communicate(timeout=10)
    grandchild = int(out.strip())
    os.killpg(leader.pid, signal.SIGTERM)
    assert not _events(audit)
    for _ in range(100):
        if not conftest._exists(conftest._REAL_KILL, grandchild):
            break
        time.sleep(0.05)
    assert not conftest._exists(conftest._REAL_KILL, grandchild)


# --------------------------------------------------------------------------- #
# The Popen, spawn_ccc and iTerm Python API guards (tp#1793 B2)
#
# Every guarded binary is either a path that does not exist or a bare name on an empty
# PATH, and a fake sits UNDER the guard: a broken guard ends in that fake (or a
# FileNotFoundError), never in a real osascript / tmux / kill / ccc.
# --------------------------------------------------------------------------- #
NO_BIN = "/nonexistent-tp1793/bin"


class _NoExec:
    """Installed as ``conftest._REAL_EXECUTE_CHILD``: records a call that got past the guard."""

    def __init__(self) -> None:
        self.argv: list[object] = []

    def __call__(self, _popen: object, args: object, *_rest: object, **_kwargs: object) -> None:
        self.argv.append(args)
        raise FileNotFoundError(errno.ENOENT, "the tp#1793 guard test never execs", str(args))


@pytest.fixture(name="no_exec")
def _no_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _NoExec:
    """An empty PATH and a fake real ``_execute_child`` under the Popen guard."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    fake = _NoExec()
    monkeypatch.setattr(conftest, "_REAL_EXECUTE_CHILD", fake)
    return fake


def _refused(path: Path, channel: str) -> dict:
    """The one audit line of *path*, asserted to be a refusal on *channel*."""
    (event,) = _events(path)
    assert (event["channel"], event["verdict"]) == (channel, "refused")
    assert event["nodeid"].startswith("tests/test_conftest_isolation.py::")
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert channel in header["channels"]
    return event


def test_spawn_guard_spawn_ccc_refused(audit: Path, no_exec: _NoExec) -> None:
    from command_center import spawn

    with pytest.raises(AssertionError, match="spawn_ccc"):
        spawn.spawn_ccc(["close-now", "--pid", "4242"])
    event = _refused(audit, "spawn_ccc")
    assert event["argv"] == ["close-now", "--pid", "4242"]
    assert not no_exec.argv


def test_spawn_guard_ccc_popen_refused(audit: Path, no_exec: _NoExec) -> None:
    with pytest.raises(AssertionError, match="real ccc"):
        subprocess.Popen([f"{NO_BIN}/ccc", "close-now"])  # pylint: disable=consider-using-with
    event = _refused(audit, "popen:ccc")
    assert event["argv"] == [f"{NO_BIN}/ccc", "close-now"]
    assert not no_exec.argv


def test_spawn_guard_lets_own_venv_ccc_through(audit: Path, no_exec: _NoExec) -> None:
    """The interpreter's own console script is this checkout's code, not the live ``ccc``."""
    with pytest.raises(FileNotFoundError):  # raised by the fake under the guard
        subprocess.Popen([conftest._OWN_CCC, "aim", "-h"])  # pylint: disable=consider-using-with
    assert no_exec.argv == [[conftest._OWN_CCC, "aim", "-h"]]
    assert not _events(audit)


def test_spawn_guard_lets_tmp_built_ccc_through(
    audit: Path, no_exec: _NoExec, tmp_path: Path
) -> None:
    """A ``ccc`` a test installed under the pytest basetemp is this checkout's code too."""
    built = str(tmp_path / "venv" / "bin" / "ccc")
    with pytest.raises(FileNotFoundError):  # raised by the fake under the guard
        subprocess.Popen([built, "-h"])  # pylint: disable=consider-using-with
    assert no_exec.argv == [[built, "-h"]]
    assert not _events(audit)


def test_spawn_guard_kill_binary_refused(audit: Path, no_exec: _NoExec) -> None:
    with pytest.raises(AssertionError, match="real kill"):
        subprocess.run(["kill", "-TERM", "4242"], check=False)
    assert _refused(audit, "popen:kill")["argv"] == ["kill", "-TERM", "4242"]
    assert not no_exec.argv


def test_spawn_guard_shell_command_is_judged_by_its_program(audit: Path, no_exec: _NoExec) -> None:
    with pytest.raises(AssertionError, match="real kill"):
        subprocess.run("kill -TERM 4242", shell=True, check=False)  # noqa: S602
    _refused(audit, "popen:kill")
    assert not no_exec.argv


def test_spawn_guard_lets_other_binaries_through(audit: Path) -> None:
    assert subprocess.run(["true"], check=False).returncode == 0
    assert not _events(audit)


def test_osascript_guard_terminal_raw_popen(audit: Path, no_exec: _NoExec) -> None:
    from command_center import terminal

    with pytest.raises(AssertionError, match="real osascript"):
        terminal._dispatch_title_script("set name of s to sid")
    assert _refused(audit, "popen:osascript")["argv"][:2] == ["osascript", "-e"]
    assert not no_exec.argv


def test_osascript_guard_terminal_osascript(
    audit: Path, no_exec: _NoExec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import terminal

    monkeypatch.setattr(terminal.shutil, "which", lambda name, *_a, **_k: f"{NO_BIN}/{name}")
    with pytest.raises(AssertionError, match="real osascript"):
        terminal._osascript('tell application "iTerm2" to get name')
    _refused(audit, "popen:osascript")
    assert not no_exec.argv


def test_osascript_guard_peek_osascript(audit: Path, no_exec: _NoExec) -> None:
    from command_center import peek

    with pytest.raises(AssertionError, match="real osascript"):
        peek._osascript('tell application "iTerm2" to get name')
    _refused(audit, "popen:osascript")
    assert not no_exec.argv


def test_osascript_guard_tmux_kill_pane(audit: Path, no_exec: _NoExec) -> None:
    from command_center import cli

    with pytest.raises(AssertionError, match="real tmux"):
        cli._close_terminal("%tp1793-none", "")
    assert _refused(audit, "popen:tmux")["argv"] == ["tmux", "kill-pane", "-t", "%tp1793-none"]
    assert not no_exec.argv


def test_osascript_guard_iterm_connection(audit: Path) -> None:
    import iterm2

    with pytest.raises(AssertionError, match=r"Connection\.async_create"):
        # Never awaited: an unguarded call would only build a coroutine, never connect.
        _ = iterm2.Connection.async_create()
    event = _refused(audit, "iterm_api:Connection.async_create")
    assert event["target"] == "iTerm2"


def test_osascript_guard_snapshot_connect(audit: Path) -> None:
    from command_center import snapshot

    async def _never(_connection: object) -> None:
        raise AssertionError("the refused connection ran its coroutine")

    with pytest.raises(AssertionError, match=r"via snapshot\._connect"):
        snapshot._connect(_never)
    _refused(audit, "iterm_api:snapshot._connect")


@pytest.mark.real_osascript
def test_osascript_guard_real_osascript_opt_out(audit: Path, no_exec: _NoExec) -> None:
    with pytest.raises(FileNotFoundError):
        subprocess.Popen([f"{NO_BIN}/osascript", "-e", "beep"])  # pylint: disable=consider-using-with
    assert no_exec.argv == [[f"{NO_BIN}/osascript", "-e", "beep"]]
    assert not _events(audit)


@pytest.mark.real_iterm
def test_osascript_guard_real_iterm_opt_out() -> None:
    import iterm2

    from command_center import snapshot

    assert snapshot._connect.__name__ == "_connect"
    assert iterm2.Connection.async_create.__name__ == "async_create"
