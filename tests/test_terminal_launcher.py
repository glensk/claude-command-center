"""Launcher selection (iterm vs tmux) and tmux command construction — no real tmux."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from command_center import config, terminal

_BOTH_TOOLS: dict[str, str | None] = {
    "tmux": "/usr/bin/tmux",
    "osascript": "/usr/bin/osascript",
}

# The account-pin prefix every launch command now carries (D8). Under the single-account
# test fixture the account is the default, so the prefix unsets both Claude env vars.
_PIN = "unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR; "


def _cfg(launcher: str) -> config.Config:
    return config.Config(launcher=launcher)


def _which(mapping: dict[str, str | None]) -> Any:
    return lambda name: mapping.get(name)


class _RunRecorder:
    """Record subprocess.run argv lists; scripted has-session return codes."""

    def __init__(self, has_session_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.has_session_rc = has_session_rc

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        rc = self.has_session_rc if args[1:2] == ["has-session"] else 0
        return subprocess.CompletedProcess(args, rc)


@pytest.fixture(name="no_applescript")
def _no_applescript(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the AppleScript path is ever taken."""

    def boom(_command: str) -> bool:
        raise AssertionError("AppleScript path must not be used")

    monkeypatch.setattr(terminal, "_iterm", boom)
    monkeypatch.setattr(terminal, "_iterm_api_tab", boom)
    assert not hasattr(terminal, "_terminal_app")  # Terminal.app fallback is gone for good


def test_launcher_tmux_resume_builds_new_window(
    monkeypatch: pytest.MonkeyPatch, no_applescript: None
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    monkeypatch.setattr(terminal.shutil, "which", _which(_BOTH_TOOLS))
    rec = _RunRecorder(has_session_rc=0)
    monkeypatch.setattr(terminal.subprocess, "run", rec)

    assert terminal.resume_in_new_tab("/tmp/repo", "abc-123") is True
    new_window = rec.calls[-1]
    assert new_window[:3] == ["/usr/bin/tmux", "new-window", "-t"]
    assert terminal._tmux_session() in new_window
    assert ["-c", "/tmp/repo"] == new_window[4:6]
    assert new_window[-1] == f"{_PIN}claude --resume abc-123"


def test_launcher_tmux_creates_session_when_absent(
    monkeypatch: pytest.MonkeyPatch, no_applescript: None
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    monkeypatch.setattr(terminal.shutil, "which", _which({"tmux": "/usr/bin/tmux"}))
    rec = _RunRecorder(has_session_rc=1)  # no `ai` session yet
    monkeypatch.setattr(terminal.subprocess, "run", rec)

    assert terminal.resume_in_new_tab("/tmp/repo", "abc") is True
    verbs = [call[1] for call in rec.calls]
    assert verbs == ["has-session", "new-session", "new-window"]
    assert rec.calls[1][:4] == ["/usr/bin/tmux", "new-session", "-d", "-s"]


def test_iterm_missing_osascript_falls_back_to_tmux(
    monkeypatch: pytest.MonkeyPatch, no_applescript: None
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("iterm"))
    monkeypatch.setattr(
        terminal.shutil, "which", _which({"tmux": "/usr/bin/tmux", "osascript": None})
    )
    rec = _RunRecorder()
    monkeypatch.setattr(terminal.subprocess, "run", rec)

    assert terminal.resume_in_new_tab("/tmp/repo", "abc") is True
    assert rec.calls[-1][1] == "new-window"


def test_default_iterm_path_unchanged_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("iterm"))
    monkeypatch.setattr(terminal.shutil, "which", _which(_BOTH_TOOLS))
    seen: list[str] = []

    def fake_iterm(command: str) -> bool:
        seen.append(command)
        return True

    monkeypatch.setattr(terminal, "_iterm", fake_iterm)
    monkeypatch.setattr(
        terminal.subprocess, "run", lambda *a, **k: pytest.fail("tmux must not be used")
    )

    assert terminal.resume_in_new_tab("/tmp/re po", "abc") is True
    assert seen == [f"{_PIN}cd '/tmp/re po' && claude --resume abc"]


def test_tmux_missing_returns_false(monkeypatch: pytest.MonkeyPatch, no_applescript: None) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    monkeypatch.setattr(terminal.shutil, "which", _which({"tmux": None}))

    assert terminal.resume_in_new_tab("/tmp/repo", "abc") is False


def test_start_job_tmux_command(monkeypatch: pytest.MonkeyPatch, no_applescript: None) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    monkeypatch.setattr(terminal.shutil, "which", _which({"tmux": "/usr/bin/tmux"}))
    rec = _RunRecorder()
    monkeypatch.setattr(terminal.subprocess, "run", rec)

    assert terminal.start_job_in_new_tab("deadbeef") is True
    assert rec.calls[-1][-1] == "ccc start-job deadbeef"
    assert "-c" not in rec.calls[-1]


def test_resume_halted_tmux_command(
    monkeypatch: pytest.MonkeyPatch, no_applescript: None, tmp_path: Any
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    monkeypatch.setattr(terminal.shutil, "which", _which({"tmux": "/usr/bin/tmux"}))
    rec = _RunRecorder()
    monkeypatch.setattr(terminal.subprocess, "run", rec)

    assert (
        terminal.resume_halted_in_new_tab(str(tmp_path), "abc", "/x/claude-session-continue.py")
        is True
    )
    assert rec.calls[-1][-1] == f"{_PIN}/x/claude-session-continue.py abc now"
    assert ["-c", str(tmp_path)] == rec.calls[-1][4:6]
