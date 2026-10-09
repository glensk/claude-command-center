#!/usr/bin/env python3
"""The ONE builder of an interactive ``claude`` launch command line.

Every ccc surface that starts or resumes an interactive Claude Code session builds its
command here (PLAN_claude-bridge.md §7.1, spike S-LAUNCH): ``claude_argv`` for the five
``os.execvp`` sites (``ccc resume``, ``fire-attached``, ``fire-await``, ``start-job``, the
``claude-session-continue`` auto-resume) and ``claude_command`` for the three that TYPE a
shell string into a tab (``terminal.resume_in_new_tab``, ``accounts.relaunch_command``,
snapshot restore). One builder means one place that adds ``--name <canonical name>`` —
the session's memorable name (:mod:`command_center.session_names`) shows in Claude Code's
prompt box and ``/resume`` picker. The env half (account pin, ``CCC_NO_CODEX``,
``CLAUDE_CODE_DISABLE_TERMINAL_TITLE``) stays in :mod:`command_center.accounts`
(``session_env_flags`` and its renderings), which ``claude_command`` prepends.

Headless calls (``claude -p`` probes, ``llm_custom_command``) never come through here and
never get a name. Stdlib-only at import time: ``claude-session-continue`` imports it.
"""

# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import shlex
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accounts import SessionLaunch

CLAUDE = "claude"


def lookup_name(session_id: str) -> str:
    """The stored canonical name of *session_id*, or ``""`` (no row, no name, any error).

    Best-effort by design: a launch must never fail because the name could not be read.
    """
    if not session_id:
        return ""
    try:
        from .store import Store

        with Store() as store:
            session = store.get(session_id)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ""
    return (session.canonical_name or "") if session is not None else ""


def claude_argv(  # pylint: disable=too-many-arguments
    *,
    resume: str = "",
    session_id: str = "",
    continue_last: bool = False,
    model: str = "",
    effort: str = "",
    name: str | None = None,
    extra: Sequence[str] = (),
    prompt: str | None = None,
) -> list[str]:
    """``claude [--name N] [--resume|--continue] [--model] [--session-id] [--effort] … [prompt]``.

    *resume* → ``--resume <id>``, *session_id* → ``--session-id <id>`` (a new session with
    a chosen id), *continue_last* → ``--continue``. *name* ``None`` looks the canonical
    name up by the resumed / chosen id; ``""`` means no name. ``--name`` sits right after
    ``claude``; the positional *prompt* is always LAST (a single argv element — the
    string renderer quotes it).
    """
    if name is None:
        name = lookup_name(resume or session_id)
    argv = [CLAUDE]
    if name:
        argv += ["--name", name]
    if resume:
        argv += ["--resume", resume]
    elif continue_last:
        argv.append("--continue")
    if model:
        argv += ["--model", model]
    if not resume and session_id:
        argv += ["--session-id", session_id]
    if effort:
        argv += ["--effort", effort]
    argv += list(extra)
    if prompt is not None:
        argv.append(prompt)
    return argv


def claude_command(
    target: SessionLaunch, argv: Sequence[str], *, cwd: str = "", subshell: bool = False
) -> str:
    """The shell STRING form of *argv*, with the session's env pin in front.

    ``<env prefix>[cd <cwd> && ]claude …`` — or, with *subshell* (the relaunch typed into
    the user's own shell), ``[cd <cwd> && ]( <env prefix>claude … )`` so the exports do not
    persist in that shell. Every argument is shell-quoted; the caller owns rejecting
    control characters (a newline in a TYPED line submits it early).
    """
    from .accounts import session_launch_env_prefix

    prefix = session_launch_env_prefix(target)
    cd = f"cd {shlex.quote(cwd)} && " if cwd else ""
    command = shlex.join(argv)
    if subshell:
        return f"{cd}( {prefix}{command} )"
    return f"{prefix}{cd}{command}"
