#!/usr/bin/env python3
"""Run a user-configured shell predicate; exit 0 means the check passed.

Shared by the session-level done-check (`done_check_cmd`) and the per-sub-goal
machine-check predicates. The command is user-authored (same trust model as
`done_check_cmd`) — auto-derived sub-goals never get one. Never raises.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import subprocess


def run_exit0(command: str, cwd: str | None = None, timeout: int = 30) -> bool:
    """Return True iff *command* (run via the shell in *cwd*) exits 0.

    Output is captured/discarded; a timeout, non-zero exit, or spawn error all
    read as "not satisfied" (False) so callers degrade gracefully.
    """
    try:
        result = subprocess.run(  # noqa: S602  # user-configured command, intentional
            command,
            shell=True,
            cwd=cwd or None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0
