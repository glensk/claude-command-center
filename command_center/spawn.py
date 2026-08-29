#!/usr/bin/env python3
"""Fire-and-forget a detached ``ccc`` subcommand (after-turn grader / AIM scorer).

Spawned from fast paths (the Stop hook, ``set-aim``) where we must NOT block and
the work (a ``claude -p`` call) outlives the caller. Two hard requirements:

* **Detached** — ``start_new_session=True`` puts the child in its own session /
  process group so Claude Code's kill of the hook process tree at the 5 s hook
  timeout does not reap a mid-flight grader.
* **Guarded** — ``CCC_INTERNAL=1`` makes the child's own Claude Code hooks no-op
  (no recursion, no junk session rows); ``AI_NO_AUTOCOMMIT=1`` keeps the
  auto-commit Stop hook from firing inside it.

Never raises. Single function so tests have one place to monkeypatch (and must —
never fork a real ``ccc`` in a unit test).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import shutil
import subprocess


def spawn_ccc(args: list[str]) -> bool:
    """Launch ``ccc <args…>`` detached + non-blocking. Return True if it started."""
    exe = shutil.which("ccc")
    if not exe:
        return False
    env = {**os.environ, "CCC_INTERNAL": "1", "AI_NO_AUTOCOMMIT": "1"}
    try:
        # Deliberately not a `with` block: the child must outlive this call (detached).
        subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
            [exe, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except (OSError, ValueError):
        return False
    return True
