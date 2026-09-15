#!/usr/bin/env python3
"""One broken-pipe guard for every console script in this package (tp#176, tp#281).

`ccc ls | head -3`, `codex-in-claude --help | head -1`: the reader closes the pipe while
we are still writing. Python's default SIGPIPE disposition (ignored, so the write raises
BrokenPipeError) is left alone on purpose — these CLIs also write to *subprocess* stdin
pipes that catch that very exception (``usage._appserver_exchange``,
``codex_in_claude.feed_stdin``), and a global ``signal(SIGPIPE, SIG_DFL)`` would kill the
process outright there instead of letting those handlers run. The dead stdout is therefore
handled once per CLI, at its entry point, by wrapping it in :func:`guard`.

The reader can vanish at three different moments, and only the first is catchable where
the write happens:

* mid-write — the output is larger than the pipe buffer, so ``print`` itself raises;
* at exit — the output still sits in Python's block buffer, so the *shutdown* flush
  raises, long after ``main`` returned and outside every ``try``;
* on a ``SystemExit`` — argparse prints ``--help`` (or a usage error) into that same
  block buffer and then exits by exception, walking past any flush that only runs on the
  success path.

:func:`guard` therefore flushes in a ``finally``: the last two cases both become a
BrokenPipeError raised *inside* the guard, where it can be swallowed.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

EXIT_SIGPIPE = 141  # what a shell reports for a SIGPIPE-killed process (128 + 13)


def quiet_broken_pipe() -> int:
    """Swallow a reader that closed our stdout early, and report it like SIGPIPE would.

    stdout is re-pointed at ``/dev/null`` first: without that, the interpreter's shutdown
    flush hits the dead fd a second time and prints
    ``Exception ignored in: <_io.TextIOWrapper …> BrokenPipeError`` after we returned.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):  # pragma: no cover - stdout already unusable
        pass
    return EXIT_SIGPIPE


def guard(run: Callable[[], int]) -> int:
    """Run *run* with a dead stdout turned into a silent ``EXIT_SIGPIPE``.

    The flush sits in a ``finally`` so that output still in the block buffer dies HERE,
    inside the guard, on every exit path — including the ``SystemExit`` argparse raises
    for ``--help``, whose code is then deliberately replaced by ``EXIT_SIGPIPE`` (there
    is no one left to read the help text). Any other exception propagates untouched.
    """
    try:
        try:
            return run()
        finally:
            sys.stdout.flush()
    except BrokenPipeError:
        return quiet_broken_pipe()
