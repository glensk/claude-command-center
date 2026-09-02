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


# --------------------------- rung reporting (tp#90) --------------------------- #
def test_open_tab_reports_the_rung_that_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal, "_iterm", lambda _c: False)
    monkeypatch.setattr(terminal, "_iterm_api_tab", lambda _c: False)
    monkeypatch.setattr(terminal, "_tmux_window", lambda _c, cwd=None: True)
    assert terminal._open_tab("x", tmux_fallback=True) == terminal.LAUNCHER_TMUX
    assert terminal._open_tab("x", tmux_fallback=False) == ""  # resume paths never tmux
    monkeypatch.setattr(terminal, "_iterm_api_tab", lambda _c: True)
    assert terminal._open_tab("x", tmux_fallback=False) == terminal.LAUNCHER_ITERM_API
    monkeypatch.setattr(terminal, "_iterm", lambda _c: True)
    assert terminal._open_tab("x", tmux_fallback=False) == terminal.LAUNCHER_ITERM_APPLESCRIPT
    assert terminal.TAB_LAUNCHERS == {"iterm_applescript", "iterm_api"}
    assert terminal.LAUNCHERS == terminal.TAB_LAUNCHERS | {"tmux"}


def test_start_job_launch_names_the_rung_and_the_bool_twin_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("iterm"))
    monkeypatch.setattr(terminal.shutil, "which", _which(_BOTH_TOOLS))
    seen: list[str] = []

    def fake_iterm(command: str) -> bool:
        seen.append(command)
        return False

    monkeypatch.setattr(terminal, "_iterm", fake_iterm)
    monkeypatch.setattr(terminal, "_iterm_api_tab", lambda _c: False)
    monkeypatch.setattr(terminal.subprocess, "run", _RunRecorder())

    assert terminal.start_job_launch("deadbeef") == terminal.LAUNCHER_TMUX
    assert seen == ["ccc start-job deadbeef"]
    assert terminal.start_job_in_new_tab("deadbeef") is True
    assert terminal.start_job_launch("deadbeef", force=True, auto=True) == terminal.LAUNCHER_TMUX
    assert seen[-1] == "ccc start-job --force --auto deadbeef"


def test_api_rung_never_connects_unless_iterm_auth_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-cookie regression (tp#90).

    With ITERM2_COOKIE/KEY inherited, the ``iterm2`` package would 401 and force a fresh
    AppleScript cookie request with NO timeout — the exact hang the ladder must never
    reach. So the rung must not even connect unless iTerm2's auth-disable switch is set.
    """
    import sys
    import types

    monkeypatch.setenv("ITERM2_COOKIE", "stale")
    monkeypatch.setenv("ITERM2_KEY", "stale")
    connects: list[int] = []

    class _Conn:
        @staticmethod
        async def async_create() -> None:
            connects.append(1)
            raise RuntimeError("401 — with auth disabled the package raises at once, no re-auth")

    fake_auth = types.ModuleType("iterm2.auth")
    fake_auth.applescript_auth_disabled = lambda: False  # type: ignore[attr-defined]
    fake = types.ModuleType("iterm2")
    fake.Connection = _Conn  # type: ignore[attr-defined]
    fake.auth = fake_auth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "iterm2", fake)
    monkeypatch.setitem(sys.modules, "iterm2.auth", fake_auth)

    assert terminal._iterm_api_tab("echo x") is False
    assert connects == []  # gated BEFORE any connection attempt

    fake_auth.applescript_auth_disabled = lambda: True  # type: ignore[attr-defined]
    assert terminal._iterm_api_tab("echo x") is False  # connect fails fast → False, no hang
    assert connects == [1]


def test_send_text_pre_checks_the_grant_with_a_bounded_apple_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt injection has no AppleScript rung ahead of it: a 5 s version query stands in."""
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: False)
    scripts: list[tuple[str, float]] = []

    def fake_osascript(script: str, timeout: float = 10) -> str | None:
        scripts.append((script, timeout))
        return None  # denied / pending prompt / no osascript

    monkeypatch.setattr(terminal, "_osascript", fake_osascript)
    monkeypatch.setattr(
        terminal.subprocess, "run", lambda *a, **k: pytest.fail("must not reach the API connect")
    )
    assert terminal.send_text_to_session("w0t0p0:ABC", "hello") is False
    assert scripts == [('tell application "iTerm2" to version', 5)]


def test_degraded_launch_warning_only_on_a_mac_set_to_iterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("iterm"))
    monkeypatch.setattr(terminal.shutil, "which", _which(_BOTH_TOOLS))
    monkeypatch.setattr(terminal.sys, "platform", "darwin")
    warning = terminal.degraded_launch_warning(terminal.LAUNCHER_TMUX)
    assert warning.startswith("warning:") and "tmux attach -t" in warning
    assert terminal.degraded_launch_warning(terminal.LAUNCHER_ITERM_APPLESCRIPT) == ""
    assert terminal.degraded_launch_warning("") == ""
    # Linux: the default "iterm" config auto-falls to tmux — intended, silent.
    monkeypatch.setattr(terminal.sys, "platform", "linux")
    assert terminal.degraded_launch_warning(terminal.LAUNCHER_TMUX) == ""
    # A Mac configured for tmux: intended, silent.
    monkeypatch.setattr(terminal.sys, "platform", "darwin")
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    assert terminal.degraded_launch_warning(terminal.LAUNCHER_TMUX) == ""
    # A Mac without osascript: automatic tmux mode — intended, silent.
    monkeypatch.setattr(config, "load_config", lambda: _cfg("iterm"))
    monkeypatch.setattr(
        terminal.shutil, "which", _which({"tmux": "/usr/bin/tmux", "osascript": None})
    )
    assert terminal.degraded_launch_warning(terminal.LAUNCHER_TMUX) == ""


def test_probe_launch_prints_only_a_framed_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    import shlex

    monkeypatch.setattr(config, "load_config", lambda: _cfg("iterm"))
    monkeypatch.setattr(terminal.shutil, "which", _which(_BOTH_TOOLS))
    seen: list[str] = []

    def fake_iterm(command: str) -> bool:
        seen.append(command)
        return True

    monkeypatch.setattr(terminal, "_iterm", fake_iterm)
    marker, launcher = terminal.probe_launch()
    assert launcher == terminal.LAUNCHER_ITERM_APPLESCRIPT
    prefix, nonce = marker.split(" ")
    assert prefix == terminal.PROBE_MARKER_PREFIX and len(nonce) == 12
    assert seen == [f"printf '%s\\n' {shlex.quote(marker)}"]  # nothing but the marker
    # tmux mode: a window that closes itself, reported as tmux.
    monkeypatch.setattr(config, "load_config", lambda: _cfg("tmux"))
    monkeypatch.setattr(terminal.subprocess, "run", _RunRecorder())
    assert terminal.probe_launch()[1] == terminal.LAUNCHER_TMUX
