#!/usr/bin/env python3
"""Path-scoped commit + push for the file-lock handoff (``ccc handoff``).

The handoff invariant: a file is **committed before its lock is released**, so a second
session never starts editing another session's uncommitted work. This module performs only
that path-scoped commit — it never does a whole-tree ``git add -A`` (that is what mixed
concurrent sessions' edits in the first place).

Prefers the user's ``ai.py push -m <msg> <paths>`` when on PATH (it carries the protected-
``main`` MR autopilot and commit conventions); falls back to plain path-scoped git otherwise.
Never raises — returns ``(ok, detail)``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import shutil
import subprocess


def _run(cmd: list[str], cwd: str, timeout: float) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{cmd[0]}: {exc}"
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def commit_and_push(
    repo: str, paths: list[str], message: str, *, timeout: float = 120.0
) -> tuple[bool, str]:
    """Commit *paths* (only) in *repo* with *message*, then push. Return ``(ok, detail)``.

    "Nothing to commit" (the file is already clean/committed) counts as success — the
    invariant "the file is committed" holds either way.
    """
    if not paths:
        return True, "no paths"
    ai = shutil.which("ai.py")
    if ai:
        # ai.py push is path-scoped to the listed files and self-heals protected-main pushes.
        return _run([ai, "push", "-m", message, *paths], repo, timeout)
    return _git_commit_and_push(repo, paths, message, timeout)


def _git_commit_and_push(
    repo: str, paths: list[str], message: str, timeout: float
) -> tuple[bool, str]:
    ok, detail = _run(["git", "add", "--", *paths], repo, 30)
    if not ok:
        return ok, detail
    # Commit only when these paths actually have staged changes (else "nothing to commit").
    staged = subprocess.run(  # noqa: S603
        ["git", "diff", "--cached", "--quiet", "--", *paths],  # noqa: S607
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if staged.returncode != 0:  # non-zero == there are staged changes
        ok, detail = _run(["git", "commit", "-m", message, "--", *paths], repo, 30)
        if not ok:
            return ok, detail
    return _run(["git", "push"], repo, timeout)
