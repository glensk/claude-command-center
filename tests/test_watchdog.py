"""The TUI liveness watchdog (watchdog.py): verdicts, wedge reports, self-heal plumbing."""

from __future__ import annotations

import faulthandler
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from command_center import watchdog


def test_verdict_is_healthy_within_both_windows() -> None:
    assert watchdog.verdict(now=100.0, heartbeat=99.0, exit_since=None) is None
    assert (
        watchdog.verdict(now=100.0, heartbeat=0.0, exit_since=95.0) is None
    )  # exiting: grace rules


def test_verdict_stall_needs_a_stale_heartbeat_while_not_exiting() -> None:
    assert watchdog.verdict(now=200.0, heartbeat=0.0, exit_since=None, stall_sec=120.0) == "stall"
    assert watchdog.verdict(now=100.0, heartbeat=0.0, exit_since=None, stall_sec=120.0) is None


def test_verdict_shutdown_once_the_exit_grace_is_over() -> None:
    assert watchdog.verdict(now=100.0, heartbeat=100.0, exit_since=80.0, grace_sec=10.0) == (
        "shutdown"
    )
    # A hung exit never counts as a stall, however old the heartbeat is.
    assert watchdog.verdict(now=100.0, heartbeat=0.0, exit_since=99.0, grace_sec=10.0) is None


def test_write_report_appends_state_and_every_threads_stack(tmp_path: Path) -> None:
    log = tmp_path / "tui-watchdog.log"
    first = watchdog.write_report("stall", {"running": True, "workers": ["a=RUNNING"]}, log)
    second = watchdog.write_report("shutdown", {"running": False}, log)
    assert first == second == log
    text = log.read_text(encoding="utf-8")
    assert text.count("wedged: ") == 2  # appended, not truncated
    assert "wedged: stall" in text and "wedged: shutdown" in text
    assert "running: True" in text and "workers: ['a=RUNNING']" in text
    # faulthandler's dump: at least this thread, named by its id, with a frame line.
    assert "Thread 0x" in text or "Current thread 0x" in text
    assert "test_watchdog.py" in text


def test_register_usr1_dumps_stacks_on_demand(tmp_path: Path) -> None:
    log = tmp_path / "tui-watchdog.log"
    assert watchdog.register_usr1(log) == log
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "test_watchdog.py" not in log.read_text():
            time.sleep(0.05)
        assert "test_watchdog.py" in log.read_text(encoding="utf-8")
    finally:
        faulthandler.unregister(signal.SIGUSR1)


def test_generation_counter_caps_restarts_and_resets_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(watchdog.GENERATION_ENV, raising=False)
    assert watchdog.generation() == 0
    seen = [watchdog.may_restart() for _ in range(watchdog.MAX_RESTARTS + 2)]
    assert seen == [True] * watchdog.MAX_RESTARTS + [False, False]
    assert watchdog.generation() == watchdog.MAX_RESTARTS
    watchdog.mark_healthy()
    assert watchdog.generation() == 0
    monkeypatch.setenv(watchdog.GENERATION_ENV, "garbage")
    assert watchdog.generation() == 0  # never crash on a mangled counter


def _run_until_heal(
    dog: watchdog.Watchdog, healed: list[dict[str, Any]], timeout: float = 5.0
) -> None:
    dog.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not healed:
        time.sleep(0.01)
    dog.stop()


def test_thread_heals_a_stall_with_a_restart_and_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(watchdog.GENERATION_ENV, raising=False)
    log = tmp_path / "tui-watchdog.log"
    healed: list[dict[str, Any]] = []

    def fake_heal(reason: str, **kwargs: Any) -> None:
        healed.append({"reason": reason, **kwargs})

    dog = watchdog.Watchdog(
        is_exiting=lambda: False,
        restart_wanted=lambda: False,
        state=lambda: {"running": True},
        reexec=lambda: None,
        stall_sec=0.05,
        tick_sec=0.01,
        heal_fn=fake_heal,
        log=log,
    )
    dog.heartbeat = time.monotonic() - 1.0  # already stale
    _run_until_heal(dog, healed)
    assert len(healed) == 1
    assert healed[0]["reason"] == "stall"
    assert healed[0]["restart"] is True  # a stall always brings the TUI back
    assert healed[0]["report"] == log
    text = log.read_text(encoding="utf-8")
    assert "wedged: stall" in text and "restart: True" in text and "heartbeat_age_s:" in text


def test_thread_finishes_a_hung_quit_but_restarts_a_hung_restart(tmp_path: Path) -> None:
    for wanted, expect_restart in ((False, False), (True, True)):
        healed: list[dict[str, Any]] = []

        def fake_heal(reason: str, healed: list[dict[str, Any]] = healed, **kw: Any) -> None:
            healed.append({"reason": reason, **kw})

        def restart_wanted(wanted: bool = wanted) -> bool:
            return wanted

        dog = watchdog.Watchdog(
            is_exiting=lambda: True,
            restart_wanted=restart_wanted,
            state=lambda: {},
            reexec=lambda: None,
            grace_sec=0.05,
            tick_sec=0.01,
            heal_fn=fake_heal,
            log=tmp_path / f"log-{wanted}.log",
        )
        _run_until_heal(dog, healed)
        assert healed and healed[0]["reason"] == "shutdown"
        assert healed[0]["restart"] is expect_restart


def test_thread_stays_quiet_while_beaten_and_stops_cleanly(tmp_path: Path) -> None:
    healed: list[str] = []
    dog = watchdog.Watchdog(
        is_exiting=lambda: False,
        restart_wanted=lambda: False,
        state=lambda: {},
        reexec=lambda: None,
        stall_sec=0.1,
        tick_sec=0.01,
        heal_fn=lambda reason, **kw: healed.append(reason),
        log=tmp_path / "log",
    )
    dog.start()
    for _ in range(20):
        dog.beat()
        time.sleep(0.01)
    dog.stop()
    assert healed == []
    assert dog.check(now=dog.heartbeat + 0.05) is None
    assert dog.check(now=dog.heartbeat + 1.0) == "stall"


def test_heal_exits_zero_for_a_hung_quit_without_touching_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exits: list[int] = []
    execs: list[bool] = []
    monkeypatch.setattr(watchdog.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(watchdog, "restore_tty", lambda attrs: None)
    watchdog.heal(
        "shutdown",
        restart=False,
        reexec=lambda: execs.append(True),
        tty_attrs=None,
        report=tmp_path / "log",
    )
    assert exits == [0] and execs == []


def test_heal_reexecs_until_the_cap_then_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(watchdog.GENERATION_ENV, raising=False)
    exits: list[int] = []
    execs: list[bool] = []
    monkeypatch.setattr(watchdog.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(watchdog, "restore_tty", lambda attrs: None)
    for _ in range(watchdog.MAX_RESTARTS):
        watchdog.heal(
            "stall",
            restart=True,
            reexec=lambda: execs.append(True),
            tty_attrs=None,
            report=tmp_path,
        )
    assert execs == [True] * watchdog.MAX_RESTARTS
    # exec "returned" (our fake) → heal still terminates the process, with a failure code
    assert exits == [1] * watchdog.MAX_RESTARTS
    watchdog.heal(
        "stall", restart=True, reexec=lambda: execs.append(True), tty_attrs=None, report=tmp_path
    )
    assert len(execs) == watchdog.MAX_RESTARTS  # past the cap: no more re-execs
    assert exits[-1] == 1


def test_watchdog_thread_is_a_daemon_named_for_the_stack_dump(tmp_path: Path) -> None:
    dog = watchdog.Watchdog(
        is_exiting=lambda: False,
        restart_wanted=lambda: False,
        state=lambda: {},
        reexec=lambda: None,
        tick_sec=0.01,
        log=tmp_path / "log",
    )
    dog.start()
    dog.start()  # idempotent
    thread = next(t for t in threading.enumerate() if t.name == "ccc-watchdog")
    assert thread.daemon
    dog.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
