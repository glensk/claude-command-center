"""Unit tests for ``ccc jump``: tty discovery, jumpstate, and the toggle logic.

No iTerm / AppleScript / lsappinfo is invoked — every OS boundary is monkeypatched.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import os
import subprocess
from pathlib import Path

import pytest

from command_center import iterm_api, jump, jumpstate

# A realistic `ps -axo tty=,args=` dump: the bare TUI on a tty, the daemon and a
# one-shot subcommand on no tty (??), plus unrelated processes and a `ccc tui`.
_PS = """\
??       /opt/homebrew/.../Python /home/user/.local/bin/ccc daemon
??       /opt/homebrew/.../Python /home/user/.local/bin/ccc aim --session abc --format bar
ttys001  /opt/homebrew/.../Python /home/user/.local/bin/ccc
ttys009  -zsh
ttys016  /opt/homebrew/.../node /some/other/thing
"""


def _patch_ps(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(jump.subprocess, "run", fake_run)


def test_finds_bare_ccc_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare ``ccc`` process attached to a tty is the TUI."""
    _patch_ps(monkeypatch, _PS)
    assert jump.find_ccc_tty() == "/dev/ttys001"


def test_finds_ccc_tui_explicit_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ccc tui`` (explicit subcommand) on a tty also counts."""
    _patch_ps(monkeypatch, "ttys004  /opt/homebrew/.../Python /home/user/.local/bin/ccc tui\n")
    assert jump.find_ccc_tty() == "/dev/ttys004"


def test_ignores_daemon_and_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ccc daemon`` / ``ccc aim …`` (no tty, extra args) are never matched."""
    no_tui = "\n".join(line for line in _PS.splitlines() if not line.startswith("ttys001")) + "\n"
    _patch_ps(monkeypatch, no_tui)
    assert jump.find_ccc_tty() is None


def test_no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ps(monkeypatch, "")
    assert jump.find_ccc_tty() is None


def test_ps_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("ps not found")

    monkeypatch.setattr(jump.subprocess, "run", boom)
    assert jump.find_ccc_tty() is None


# --------------------------------------------------------------------------- #
# jumpstate — cross-process selected/request files
# --------------------------------------------------------------------------- #
@pytest.fixture
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(jumpstate.config, "app_home", lambda: tmp_path)
    return tmp_path


def test_selected_roundtrip(_home: Path) -> None:
    assert jumpstate.get_selected() is None
    jumpstate.set_selected("abc123")
    assert jumpstate.get_selected() == "abc123"
    jumpstate.set_selected(None)  # clearing removes the file
    assert jumpstate.get_selected() is None


def test_request_roundtrip(_home: Path) -> None:
    assert jumpstate.peek_request() is None
    jumpstate.request_select("sess-1")
    assert jumpstate.peek_request() == "sess-1"  # peek does NOT consume
    assert jumpstate.peek_request() == "sess-1"
    jumpstate.clear_request()
    assert jumpstate.peek_request() is None


# --------------------------------------------------------------------------- #
# run() — the context-aware toggle
# --------------------------------------------------------------------------- #
def _args(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**{"no_toggle": False, "no_launch": False, **kw})


def _wire(monkeypatch: pytest.MonkeyPatch, *, frontmost: bool, current: object) -> dict:
    """Stub every OS boundary `run()` touches; return a dict recording calls."""
    calls: dict = {"request": None, "focus_tty": None, "resume": 0}
    monkeypatch.setattr(jump, "find_ccc_tty", lambda: "/dev/ttys001")
    monkeypatch.setattr(jump.terminal, "is_iterm_frontmost", lambda: frontmost)
    monkeypatch.setattr(jump.terminal, "current_iterm_session", lambda: current)
    monkeypatch.setattr(
        jump.jumpstate, "request_select", lambda sid: calls.__setitem__("request", sid)
    )

    def _focus_tty(tty: str) -> bool:
        calls["focus_tty"] = tty
        return True

    monkeypatch.setattr(jump.terminal, "focus_tty", _focus_tty)

    def _resume() -> int:
        calls["resume"] += 1
        return 0

    monkeypatch.setattr(jump, "_resume_selected", _resume)
    return calls


def test_run_from_other_app_focuses_ccc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not in iTerm → no toggle: just bring ccc forward, no select request."""
    calls = _wire(monkeypatch, frontmost=False, current=None)
    assert jump.run(_args()) == 0
    assert calls["focus_tty"] == "/dev/ttys001"
    assert calls["request"] is None
    assert calls["resume"] == 0


def test_run_in_ccc_tab_resumes_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the ccc tab (current tty == ccc tty) → act like `r`; do not re-focus ccc."""
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-X", "/dev/ttys001"))
    assert jump.run(_args()) == 0
    assert calls["resume"] == 1
    assert calls["focus_tty"] is None


def test_run_in_session_tab_requests_then_focuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a session tab → request its row be selected, then focus ccc."""
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-S", "/dev/ttys042"))
    monkeypatch.setattr(
        jump, "_session_for_uuid", lambda uuid: "sess-S" if uuid == "UUID-S" else None
    )
    assert jump.run(_args()) == 0
    assert calls["request"] == "sess-S"
    assert calls["focus_tty"] == "/dev/ttys001"
    assert calls["resume"] == 0


def test_session_tab_reused_by_two_sessions_picks_live_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Slow path, real store: a tab holding a done row AND a live row selects the live one."""
    from command_center.store import Store

    db = tmp_path / "state.db"
    with Store(db) as store:
        store.ensure("dead", cwd="/Users/x/repo")
        store.update_fields("dead", iterm_session_id="w1t13p0:UUID-S", done=True)
        store.ensure("live", cwd="/Users/x/repo")
        store.update_fields("live", iterm_session_id="w1t13p0:UUID-S")
    monkeypatch.setattr(jump, "Store", lambda: Store(db))
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-S", "/dev/ttys042"))
    assert jump.run(_args()) == 0
    assert calls["request"] == "live"


def test_no_toggle_skips_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-toggle → always just focus ccc, even in the ccc tab."""
    calls = _wire(monkeypatch, frontmost=True, current=("UUID-X", "/dev/ttys001"))
    assert jump.run(_args(no_toggle=True)) == 0
    assert calls["resume"] == 0
    assert calls["focus_tty"] == "/dev/ttys001"


# --------------------------------------------------------------------------- #
# run() — the fast path (a live TUI owns the whole toggle)
# --------------------------------------------------------------------------- #
def test_fast_path_hands_toggle_to_live_tui(monkeypatch: pytest.MonkeyPatch, _home: Path) -> None:
    """A live TUI (own pid) → just write the toggle verb; the slow path must not run."""
    jumpstate.set_tui(os.getpid(), "w0t0p0:AAAA")

    def _boom() -> str | None:
        raise AssertionError("slow path must not run")

    monkeypatch.setattr(jump, "find_ccc_tty", _boom)
    assert jump.run(_args()) == 0
    assert jumpstate.peek_toggle() is True


def test_fast_path_dead_pid_falls_through(monkeypatch: pytest.MonkeyPatch, _home: Path) -> None:
    """A dead TUI pid → skip the fast path and take the slow (no-TUI) path."""
    jumpstate.set_tui(4242, "w0t0p0:AAAA")
    monkeypatch.setattr(jump, "_pid_alive", lambda _pid: False)
    calls = _wire(monkeypatch, frontmost=False, current=None)
    assert jump.run(_args()) == 0
    assert calls["focus_tty"] == "/dev/ttys001"  # slow path ran
    assert jumpstate.peek_toggle() is False  # fast path never fired


def test_fast_path_skipped_with_no_toggle(monkeypatch: pytest.MonkeyPatch, _home: Path) -> None:
    """--no-toggle skips the fast path even with a live TUI."""
    jumpstate.set_tui(os.getpid(), "w0t0p0:AAAA")
    calls = _wire(monkeypatch, frontmost=False, current=None)
    assert jump.run(_args(no_toggle=True)) == 0
    assert jumpstate.peek_toggle() is False  # fast path skipped
    assert calls["focus_tty"] == "/dev/ttys001"  # slow path ran


def test_tui_identity_roundtrip(_home: Path) -> None:
    assert jumpstate.get_tui() is None
    jumpstate.set_tui(4242, "w0t0p0:ABCD")
    assert jumpstate.get_tui() == (4242, "w0t0p0:ABCD")
    jumpstate.clear_tui()  # clearing removes the file
    assert jumpstate.get_tui() is None


def test_tui_identity_garbage_returns_none(_home: Path) -> None:
    (_home / "jump_tui").write_text("not-a-pid|w0t0p0:X", encoding="utf-8")
    assert jumpstate.get_tui() is None


def test_toggle_roundtrip(_home: Path) -> None:
    assert jumpstate.peek_toggle() is False
    jumpstate.request_toggle()
    assert jumpstate.peek_toggle() is True  # peek does NOT consume
    assert jumpstate.peek_toggle() is True
    jumpstate.clear_toggle()
    assert jumpstate.peek_toggle() is False


def test_restart_roundtrip(_home: Path) -> None:
    assert jumpstate.peek_restart() is False
    jumpstate.request_restart()
    assert jumpstate.peek_restart() is True  # peek does NOT consume
    assert jumpstate.peek_restart() is True
    jumpstate.clear_restart()
    assert jumpstate.peek_restart() is False


# --------------------------------------------------------------------------- #
# iterm_api — graceful degrade when the iterm2 package / API is unavailable
# --------------------------------------------------------------------------- #
def _block_iterm2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import iterm2`` raise ImportError inside ItermLink's lazy imports."""
    real_import = builtins.__import__

    def _no_iterm2(name: str, *args: object, **kwargs: object) -> object:
        if name == "iterm2":
            raise ImportError("no iterm2")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _no_iterm2)


def test_iterm_link_ensure_false_without_iterm2(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure() returns False (never raises) when `import iterm2` fails."""
    _block_iterm2(monkeypatch)
    link = iterm_api.ItermLink()
    assert asyncio.run(link.ensure()) is False
    assert link.ready is False


def test_iterm_link_ops_degrade_when_unready(monkeypatch: pytest.MonkeyPatch) -> None:
    """current_session_uuid()/focus_session() degrade to None/False on an unready link."""
    _block_iterm2(monkeypatch)
    link = iterm_api.ItermLink()
    assert asyncio.run(link.current_session_uuid()) is None
    assert asyncio.run(link.focus_session("w0t0p0:UUID")) is False
