#!/usr/bin/env python3
"""Liveness watchdog for the ccc TUI — a wedged app self-heals instead of freezing.

Why: on 2026-09-02 the TUI accepted an exit and Textual's shutdown hung before
``Unmount`` — every timer already stopped, the event loop idle, the last frame left on
screen for hours. Rows, ages and progress bars froze while the store moved on (a session
the status line showed as ``DONE 100%`` sat at ``0%`` in the TUI), and ``ccc restart-tui``
could not help because the 0.1 s poll that consumes its request was dead too.

The watchdog is a plain daemon thread — deliberately *not* a Textual timer, those are the
first casualty of a wedge. It watches two things:

- **heartbeat** — the TUI's fast poll bumps it every 0.1 s while the app is healthy; no
  bump for ``STALL_SEC`` while the app is not exiting means the timers/loop are dead;
- **exit grace** — once the app is exiting, ``App.run()`` must return within
  ``EXIT_GRACE_SEC``; otherwise the shutdown is hung.

On a wedge it appends a report — the app's state flags plus a ``faulthandler`` dump of
EVERY thread's Python stack, the evidence a post-mortem needs — to ``tui-watchdog.log``
under :func:`config.app_home`, restores the terminal, and either re-execs the TUI in
place (a stall, or a restart that hung) or exits (a quit that hung). A generation counter
in ``$CCC_TUI_WATCHDOG_RESTARTS`` caps the re-exec at ``MAX_RESTARTS`` so a TUI that
wedges on every start cannot loop forever — it exits 1 and names the log instead; the
first healthy refresh of a re-exec'd TUI resets the counter.

``kill -USR1 <pid>`` writes the same all-thread stack dump on demand (see
:func:`register_usr1`) — for a TUI that is alive but behaving oddly.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import faulthandler
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from . import config

STALL_SEC = 120.0  # no heartbeat for this long while not exiting → the loop/timers are dead
EXIT_GRACE_SEC = 10.0  # exit requested but App.run() has not returned → shutdown is hung
TICK_SEC = 1.0
MAX_RESTARTS = 3
LOG_NAME = "tui-watchdog.log"
GENERATION_ENV = "CCC_TUI_WATCHDOG_RESTARTS"

# Leave the alternate screen, show the cursor, stop mouse / bracketed-paste reporting —
# what Textual's driver would have undone had its shutdown finished.
_TTY_RESET = "\x1b[?1049l\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\r\n"

_USR1_FILES: list[IO[str]] = []  # faulthandler keeps the fd; keep the file object alive too


def log_path() -> Path:
    """Where wedge reports and USR1 stack dumps go."""
    return config.app_home() / LOG_NAME


def verdict(
    *,
    now: float,
    heartbeat: float,
    exit_since: float | None,
    stall_sec: float = STALL_SEC,
    grace_sec: float = EXIT_GRACE_SEC,
) -> str | None:
    """``"shutdown"`` (exit pending past *grace_sec*), ``"stall"`` (heartbeat older than
    *stall_sec* while not exiting) or ``None`` (healthy). Pure — the unit under test."""
    if exit_since is not None:
        return "shutdown" if now - exit_since > grace_sec else None
    return "stall" if now - heartbeat > stall_sec else None


def write_report(reason: str, state: dict[str, Any], path: Path | None = None) -> Path:
    """Append a wedge report (state flags + every thread's Python stack) to the log."""
    target = path or log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n=== {stamp} pid {os.getpid()} wedged: {reason}\n")
        for key, value in state.items():
            fh.write(f"{key}: {value}\n")
        fh.write("--- python stacks (all threads, most recent call first)\n")
        fh.flush()  # faulthandler writes straight to the fd — order matters
        faulthandler.dump_traceback(fh, all_threads=True)
    return target


def register_usr1(path: Path | None = None) -> Path | None:
    """Make ``kill -USR1 <pid>`` dump every thread's Python stack to the log (idempotent)."""
    if not hasattr(signal, "SIGUSR1"):  # pragma: no cover - Windows
        return None
    target = path or log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fh = target.open("a", encoding="utf-8")
    _USR1_FILES.append(fh)
    faulthandler.register(signal.SIGUSR1, file=fh, all_threads=True, chain=False)
    return target


def saved_tty_attrs() -> list[Any] | None:
    """The terminal's attributes before Textual put it in raw mode (None when not a tty)."""
    try:
        import termios  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        return termios.tcgetattr(sys.__stdin__.fileno())  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


def restore_tty(attrs: list[Any] | None) -> None:
    """Best-effort terminal restore for an exit Textual never finished."""
    try:
        os.write(sys.__stdout__.fileno(), _TTY_RESET.encode())  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass
    if attrs is None:
        return
    try:
        import termios  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        fd = sys.__stdin__.fileno()  # type: ignore[union-attr]
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass


def generation() -> int:
    """How many watchdog re-execs led to this process (``$CCC_TUI_WATCHDOG_RESTARTS``)."""
    try:
        return int(os.environ.get(GENERATION_ENV, "0"))
    except ValueError:
        return 0


def may_restart() -> bool:
    """Bump the generation counter for the process about to be exec'd; False past the cap."""
    current = generation()
    if current >= MAX_RESTARTS:
        return False
    os.environ[GENERATION_ENV] = str(current + 1)
    return True


def mark_healthy() -> None:
    """A refresh applied: this generation is fine, so the next wedge counts from zero."""
    os.environ.pop(GENERATION_ENV, None)


def heal(
    reason: str,
    *,
    restart: bool,
    reexec: Callable[[], None],
    tty_attrs: list[Any] | None,
    report: Path,
) -> None:
    """Restore the terminal, then re-exec in place (*restart*) or exit. Never returns."""
    restore_tty(tty_attrs)
    err = sys.__stderr__ or sys.stderr
    if restart and may_restart():
        err.write(f"ccc: TUI wedged ({reason}) — restarting in place; stacks in {report}\n")
        err.flush()
        reexec()  # replaces the process image; only returns if exec itself failed
    err.write(f"ccc: TUI wedged ({reason}); stacks in {report}\n")
    err.flush()
    os._exit(1 if restart else 0)


class Watchdog:  # pylint: disable=too-many-instance-attributes
    """The daemon thread. The TUI wires it with three cheap callables and beats it."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        is_exiting: Callable[[], bool],
        restart_wanted: Callable[[], bool],
        state: Callable[[], dict[str, Any]],
        reexec: Callable[[], None],
        stall_sec: float = STALL_SEC,
        grace_sec: float = EXIT_GRACE_SEC,
        tick_sec: float = TICK_SEC,
        heal_fn: Callable[..., None] = heal,
        log: Path | None = None,
    ) -> None:
        self.heartbeat = time.monotonic()
        self.exit_since: float | None = None
        self._is_exiting = is_exiting
        self._restart_wanted = restart_wanted
        self._state = state
        self._reexec = reexec
        self._stall_sec = stall_sec
        self._grace_sec = grace_sec
        self._tick_sec = tick_sec
        self._heal = heal_fn
        self._log = log
        self._stop = threading.Event()
        self._tty_attrs = saved_tty_attrs()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """Called from the UI loop's fast poll: proof the loop and its timers are alive."""
        self.heartbeat = time.monotonic()

    def start(self) -> None:
        """Start watching (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ccc-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """App.run() returned on its own — nothing left to watch."""
        self._stop.set()

    def check(self, now: float | None = None) -> str | None:
        """One tick: record when exiting began, return the verdict (None = healthy)."""
        now = time.monotonic() if now is None else now
        if self.exit_since is None and self._is_exiting():
            self.exit_since = now
        return verdict(
            now=now,
            heartbeat=self.heartbeat,
            exit_since=self.exit_since,
            stall_sec=self._stall_sec,
            grace_sec=self._grace_sec,
        )

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_sec):
            reason = self.check()
            if reason is None:
                continue
            # A stall always restarts (the user still wants a TUI); a hung shutdown only
            # when it was a restart — a hung quit just gets finished.
            restart = reason == "stall" or self._restart_wanted()
            try:
                state = self._state()
            except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                state = {"state_error": repr(error)}
            state["heartbeat_age_s"] = round(time.monotonic() - self.heartbeat, 1)
            state["restart"] = restart
            report = write_report(reason, state, self._log)
            self._heal(
                reason,
                restart=restart,
                reexec=self._reexec,
                tty_attrs=self._tty_attrs,
                report=report,
            )
            return
