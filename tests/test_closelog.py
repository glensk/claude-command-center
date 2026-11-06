"""Every path that ends a session's Claude leaves one ``session-close`` events.log line (tp#1785).

The close-now, bridge-close, handoff-close and switch-now lines are pinned in their own
suites' success tests; this file pins the line format and the remaining paths: the
release-locks close and switch claims, the daemon reaper, the resume-halted reaper and the
TUI close.
"""

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center import closelog, config, daemon, hooks, resume, spawn
from command_center.models import LiveSession, SwitchClaim, now_ms
from command_center.store import Store

SID = "11111111-2222-3333-4444-555555555555"


def _lines() -> list[list[str]]:
    path = config.app_home() / "events.log"
    if not path.exists():
        return []
    rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if len(row) == 4 and row[2] == closelog.EVENT]


def test_line_format_names_session_pid_path_and_caller() -> None:
    closelog.log_close(SID, 4242, "close-now", note="signalled=yes\nterminal=tab")
    [row] = _lines()
    stamp, sid, event, detail = row
    assert len(stamp) == 19 and stamp[10] == "T"
    assert (sid, event) == (SID, "session-close")
    assert detail.startswith("pid=4242 path=close-now caller=")
    assert "ppid=" in detail and detail.endswith("signalled=yes terminal=tab")


def test_caller_omits_arguments_past_the_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["/usr/local/bin/ccc", "close-now", "--prompt", "secret"])
    who = closelog.caller()
    assert who.startswith("ccc close-now pid=") and "secret" not in who


def test_unwritable_log_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "app_home", lambda: blocker / "sub")
    closelog.log_close(SID, 1, "close-now")  # no exception


def _spawned(monkeypatch: pytest.MonkeyPatch, ok: bool) -> list[list[str]]:
    calls: list[list[str]] = []

    def _spawn(args: list[str], **_kw: object) -> bool:
        calls.append(args)
        return ok

    monkeypatch.setattr(spawn, "spawn_ccc", _spawn)
    return calls


@pytest.mark.parametrize("ok", [True, False], ids=["spawned", "spawn_failed"])
def test_release_locks_close_claim_logs_only_a_spawned_closer(
    monkeypatch: pytest.MonkeyPatch, ok: bool
) -> None:
    monkeypatch.setenv("CLAUDE_PID", "5150")
    calls = _spawned(monkeypatch, ok)
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.arm_close(SID, now_ms())
    assert hooks.handle_release_locks({"session_id": SID, "cwd": "/repo"}) == 0
    assert calls and calls[0][0] == "close-now"
    rows = _lines()
    if ok:
        [row] = rows
        assert row[1] == SID
        assert row[3].startswith("pid=5150 path=close-claim caller=")
    else:
        assert not rows


def test_release_locks_switch_claim_logs_the_spawned_relauncher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_PID", "5151")
    calls = _spawned(monkeypatch, True)
    claim = SwitchClaim(target="/tmp/acct", cwd="/repo")
    monkeypatch.setattr(Store, "claim_after_turn", lambda *_a: ("switch", claim))
    with Store() as store:
        store.ensure(SID, cwd="/repo")
    assert hooks.handle_release_locks({"session_id": SID, "cwd": "/repo"}) == 0
    assert calls and calls[0][0] == "switch-now"
    [row] = _lines()
    assert row[3].startswith("pid=5151 path=switch-claim caller=")


def test_daemon_reap_logs_a_delivered_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []

    def _kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(daemon.os, "kill", _kill)
    monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)
    daemon._reap(777, SID, "daemon-reap")  # pylint: disable=protected-access
    assert sent[0] == (777, signal.SIGTERM)
    [row] = _lines()
    assert row[1] == SID and row[3].startswith("pid=777 path=daemon-reap caller=")


def test_daemon_reap_of_a_gone_pid_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _gone(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(daemon.os, "kill", _gone)
    daemon._reap(778, SID)  # pylint: disable=protected-access
    assert not _lines()


def test_resume_reaper_names_its_path(monkeypatch: pytest.MonkeyPatch) -> None:
    reaped: list[tuple[int, str, str]] = []
    monkeypatch.setattr(daemon, "_reap", lambda pid, sid, path: reaped.append((pid, sid, path)))
    monkeypatch.setattr(resume.time, "sleep", lambda _s: None)
    live = LiveSession(session_id=SID, pid=31337, cwd="/repo", kind="interactive", alive=True)
    adapter = SimpleNamespace(discover=lambda: [live])
    with Store() as store:
        resume._reap_fresh(adapter, store, SID)  # type: ignore[arg-type]  # pylint: disable=protected-access
    assert reaped == [(31337, SID, "resume-reap")]


@pytest.mark.parametrize(
    ("pid", "kill_ok", "expect"),
    [(4321, True, "pid=4321 path=tui-close"), (0, False, "pid=0 path=tui-close")],
    ids=["signalled", "pane_only"],
)
def test_tui_close_logs_signal_or_pane_close(
    monkeypatch: pytest.MonkeyPatch, pid: int, kill_ok: bool, expect: str
) -> None:
    from command_center.views import tui

    def _kill(_pid: int, _sig: int) -> None:
        if not kill_ok:
            raise ProcessLookupError

    monkeypatch.setattr(tui.os, "kill", _kill)
    monkeypatch.setattr(tui.terminal, "close_iterm_session", lambda _id: "tab")
    session = SimpleNamespace(session_id=SID, last_seen_pid=pid, iterm_session_id="w0t1p0:X")
    method = tui.CommandCenterApp._signal_and_close_pane  # pylint: disable=protected-access
    assert method(None, session, close_pane=True) == "tab"  # type: ignore[arg-type]
    [row] = _lines()
    assert row[3].startswith(expect + " caller=") and "terminal=tab" in row[3]


def test_tui_close_with_nothing_done_logs_nothing() -> None:
    from command_center.views import tui

    session = SimpleNamespace(session_id=SID, last_seen_pid=0, iterm_session_id="")
    method = tui.CommandCenterApp._signal_and_close_pane  # pylint: disable=protected-access
    assert method(None, session, close_pane=True) == ""  # type: ignore[arg-type]
    assert not _lines()
