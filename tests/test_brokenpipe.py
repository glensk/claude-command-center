"""`ccc ls | head -3` must exit quietly, not print a BrokenPipeError traceback (tp#176).

The reader of the pipe can vanish at two different moments, and only one of them is
catchable where the write happens:

* mid-write — the output is larger than the pipe buffer, so ``print`` itself raises;
* at exit — the output still sits in Python's block buffer, so the *shutdown* flush
  raises, long after ``main`` returned and outside every ``try``.

``main`` therefore flushes stdout inside its own guard, and the guard re-points stdout at
``/dev/null`` so nothing can hit the dead fd a third time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from command_center import cli

# A producer that (1) waits until the test has closed the read end of its stdout, so the
# pipe is provably dead before the first byte is written, then (2) writes *payload* and
# exits with whatever `main` returns. `_dispatch` is stubbed, so no store/config is read.
_PRODUCER = (
    "import sys\n"
    "from command_center import cli\n"
    "sys.stdin.readline()\n"
    "cli._dispatch = lambda argv: (sys.stdout.write({payload}), 0)[1]\n"
    "raise SystemExit(cli.main([]))\n"
)


def _run_with_dead_stdout(payload: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCC")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    read_fd, write_fd = os.pipe()
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _PRODUCER.format(payload=payload)],
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


@pytest.mark.parametrize(
    ("what", "payload"),
    [
        ("buffered output, dies in the exit flush", "'three short lines\\n' * 3"),
        ("big output, dies in the write itself", "'x' * 2_000_000"),
    ],
)
def test_dead_stdout_exits_quietly(what: str, payload: str) -> None:
    done = _run_with_dead_stdout(payload)
    assert "Traceback" not in done.stderr, what
    assert "BrokenPipeError" not in done.stderr, what
    assert "Exception ignored" not in done.stderr, what
    assert done.stderr == "", what
    assert done.returncode == cli._EXIT_SIGPIPE, what


def test_main_reports_sigpipe_and_parks_stdout_on_devnull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard swallows the exception and hands stdout a live fd to be flushed onto."""
    redirected: list[tuple[int, int]] = []

    def _raise(_argv: list[str] | None) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(cli, "_dispatch", _raise)
    # Real dup2 would clobber pytest's own captured stdout fd for the rest of the run.
    monkeypatch.setattr(cli.os, "dup2", lambda src, dst: redirected.append((src, dst)))

    assert cli.main([]) == cli._EXIT_SIGPIPE
    assert len(redirected) == 1
    assert redirected[0][1] == sys.stdout.fileno()
