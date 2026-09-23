"""Daemon self-heal of a stale resident panel server (tp#70 S5b, D8/Q3)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from command_center import daemon, launchd
from command_center import panelserver as ps


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ps.Paths:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    plist = tmp_path / "panel.plist"
    plist.write_text("x")
    monkeypatch.setattr(launchd, "panel_server_plist_path", lambda cfg=None: plist)
    p = ps.paths()
    p.home.mkdir(parents=True)
    return p


def _heal(dry_run: bool) -> daemon.DaemonReport:
    report = daemon.DaemonReport()
    daemon._restart_stale_panel_server(report, dry_run)  # pylint: disable=protected-access
    return report


def test_stale_live_server_gets_a_pid_targeted_restart(home: ps.Paths) -> None:
    ps.write_pidfile(home, os.getpid(), "ready", 0)
    report = _heal(dry_run=False)
    assert "requested restart" in report.panel_restart
    assert home.restart.read_text().split()[0] == str(os.getpid())


def test_dry_run_reports_intent_and_writes_nothing(home: ps.Paths) -> None:
    ps.write_pidfile(home, os.getpid(), "ready", 0)
    report = _heal(dry_run=True)
    assert report.panel_restart.startswith("would restart")
    assert not home.restart.exists()


def test_current_code_or_dead_server_is_left_alone(home: ps.Paths) -> None:
    ps.write_pidfile(home, os.getpid(), "ready", ps.code_stamp())
    assert _heal(dry_run=False).panel_restart == ""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    ps.write_pidfile(home, proc.pid, "ready", 0)
    assert _heal(dry_run=False).panel_restart == ""
    assert not home.restart.exists()


def test_not_installed_is_a_complete_noop(
    home: ps.Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launchd, "panel_server_plist_path", lambda cfg=None: tmp_path / "none")
    ps.write_pidfile(home, os.getpid(), "ready", 0)
    assert _heal(dry_run=False).panel_restart == ""
    assert not home.restart.exists()


def test_modal_panel_defers_and_old_pid_request_is_ignored(home: ps.Paths) -> None:
    """The server consumes only its own pid's request, and only when idle."""
    ps.write_pidfile(home, os.getpid(), "busy", 0)
    _heal(dry_run=False)
    restarts: list[str] = []
    core = ps.Core(
        home,
        os.getpid(),
        0,
        run_park=lambda r, s: None,
        resolve_peek=lambda r: (None, ""),
        show_peek=lambda r, d, s: None,
        post_main=lambda fn: fn(),
        restart_fn=lambda: restarts.append("x"),
    )
    core.state = "busy"
    core.flight = object()  # type: ignore[assignment]  # a panel is open
    core.tick_default()
    assert restarts == [] and core.restart_pending  # deferred
    core.flight = None
    core.state = "ready"
    core.tick_default()
    assert restarts == ["x"]
    # A request left for an OLD (dead) server pid never restarts a new server.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    ps.request_restart(home, proc.pid)
    fresh = ps.Core(
        home,
        os.getpid(),
        0,
        run_park=lambda r, s: None,
        resolve_peek=lambda r: (None, ""),
        show_peek=lambda r, d, s: None,
        post_main=lambda fn: fn(),
        restart_fn=lambda: restarts.append("y"),
    )
    fresh.state = "ready"
    fresh.tick_default()
    assert restarts == ["x"]
