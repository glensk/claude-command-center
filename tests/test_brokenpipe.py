"""Every console script must exit quietly on a closed stdout pipe (tp#176, tp#281).

The reader of the pipe can vanish at three different moments, and only the first is
catchable where the write happens:

* mid-write — the output is larger than the pipe buffer, so ``print`` itself raises;
* at exit — the output still sits in Python's block buffer, so the *shutdown* flush
  raises, long after ``main`` returned and outside every ``try``;
* on a ``SystemExit`` — argparse prints ``--help`` into that same block buffer and then
  exits by exception, walking past any flush that only runs on the success path.

:func:`command_center.brokenpipe.guard` therefore flushes stdout in a ``finally`` and
re-points stdout at ``/dev/null`` so nothing can hit the dead fd a second time. ``ccc``,
``codex-in-claude`` and ``claude-session-continue`` all go through it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from command_center import brokenpipe, cli, codex_in_claude, session_continue

# A producer that (1) waits until the test has closed the read end of its stdout, so the
# pipe is provably dead before the first byte is written, then (2) runs *body* and exits
# with whatever `main` returns. `_dispatch` is stubbed, so no store/config is read.
_PRODUCER = "import sys\nsys.stdin.readline()\n{body}\n"
_STUBBED = (
    "from command_center import cli\n"
    "cli._dispatch = lambda argv: (sys.stdout.write({payload}), 0)[1]\n"
    "raise SystemExit(cli.main([]))"
)
# The real `--help` of each CLI: argparse writes it to the block buffer, then SystemExit.
_HELP = "from command_center import {mod} as m\nraise SystemExit(m.main(['--help']))"


def _run_with_dead_stdout(body: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCC")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    read_fd, write_fd = os.pipe()
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _PRODUCER.format(body=body)],
        stdin=subprocess.PIPE,
        stdout=write_fd,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    ) as proc:
        os.close(write_fd)
        os.close(read_fd)  # nobody holds the read end any more — every write now EPIPEs
        assert proc.stdin is not None  # noqa: S101  # Popen(stdin=PIPE)
        proc.stdin.write("go\n")
        proc.stdin.close()
        _, err = proc.communicate(timeout=120)
    return subprocess.CompletedProcess(proc.args, proc.returncode, "", err)


def _assert_quiet_sigpipe(done: subprocess.CompletedProcess[str], what: str) -> None:
    assert "Traceback" not in done.stderr, what
    assert "BrokenPipeError" not in done.stderr, what
    assert "Exception ignored" not in done.stderr, what
    assert done.stderr == "", what
    assert done.returncode == brokenpipe.EXIT_SIGPIPE, what


@pytest.mark.parametrize(
    ("what", "payload"),
    [
        ("buffered output, dies in the exit flush", "'three short lines\\n' * 3"),
        ("big output, dies in the write itself", "'x' * 2_000_000"),
    ],
)
def test_dead_stdout_exits_quietly(what: str, payload: str) -> None:
    _assert_quiet_sigpipe(_run_with_dead_stdout(_STUBBED.format(payload=payload)), what)


# tp#281: the siblings had no guard at all and exited 120 with
# "Exception ignored while flushing sys.stdout"; `ccc --help` took the same SystemExit
# path straight past tp#176's success-path-only flush.
@pytest.mark.parametrize("mod", ["cli", "codex_in_claude", "session_continue"])
def test_help_on_a_dead_stdout_exits_quietly(mod: str) -> None:
    _assert_quiet_sigpipe(_run_with_dead_stdout(_HELP.format(mod=mod)), f"{mod} --help")


def test_guard_reports_sigpipe_and_parks_stdout_on_devnull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard swallows the exception and hands stdout a live fd to be flushed onto."""
    redirected: list[tuple[int, int]] = []

    def _raise() -> int:
        raise BrokenPipeError(32, "Broken pipe")

    # Real dup2 would clobber pytest's own captured stdout fd for the rest of the run.
    monkeypatch.setattr(brokenpipe.os, "dup2", lambda src, dst: redirected.append((src, dst)))

    assert brokenpipe.guard(_raise) == brokenpipe.EXIT_SIGPIPE
    assert len(redirected) == 1
    assert redirected[0][1] == sys.stdout.fileno()


def test_guard_passes_a_live_exit_code_and_other_exceptions_through() -> None:
    """A healthy stdout must not be turned into a SIGPIPE report."""
    assert brokenpipe.guard(lambda: 7) == 7
    with pytest.raises(SystemExit) as exc:
        brokenpipe.guard(lambda: (_ for _ in ()).throw(SystemExit(3)))
    assert exc.value.code == 3
    with pytest.raises(KeyboardInterrupt):
        brokenpipe.guard(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))


def test_every_console_script_main_goes_through_the_guard() -> None:
    """A fourth entry point added later must not silently skip the guard."""
    for mod in (cli, codex_in_claude, session_continue):
        source = Path(mod.__file__ or "").read_text(encoding="utf-8")
        assert "brokenpipe.guard" in source, mod.__name__
