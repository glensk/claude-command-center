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

#: Executable basename prefix of the API-key launcher (its sessions bill the API).
APIKEY_EXE_PREFIX = "claude-apikey"
CLAIM_PURPOSE = "ccc-apikey"
CLAIM_TIMEOUT_S = 5


def claim_apikey_session(session_id: str, exe: str) -> bool:
    """File ``ai-log-call session -s <sid> -j <job> -p ccc-apikey`` for an API-key launch.

    Only when *exe* is the API-key launcher (basename ``claude-apikey*``) and the id is
    known. Fail-open by design (tp#1458): ``ai-log-call`` missing, slow (5 s cap), or
    failing never blocks the launch; its exit status is ignored. True when it was run.
    """
    import os
    import subprocess

    from .codex_ledger import launchd_job

    if not session_id or not os.path.basename(exe or "").startswith(APIKEY_EXE_PREFIX):
        return False
    try:
        from extdeps import resolve

        from .external_deps import EXTERNAL_DEPS

        binary = resolve(EXTERNAL_DEPS["ai-log-call"])
        if not binary:
            return False
        subprocess.run(  # noqa: S603
            [binary, "session", "-s", session_id, "-j", launchd_job(), "-p", CLAIM_PURPOSE],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CLAIM_TIMEOUT_S,
            check=False,
        )
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False
    return True


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


def _argv_session_id(argv: Sequence[str]) -> str:
    """The id after ``--resume`` / ``--session-id`` in *argv*, or ``""``."""
    for flag in ("--resume", "--session-id"):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return str(argv[argv.index(flag) + 1])
    return ""


def profile_argv(argv: Sequence[str], launch_profile: str) -> list[str]:
    """*argv* with its leading ``claude`` swapped for the profile's resolved executable.

    ``[resolved executable] + argv[1:]`` — nothing else changes. An empty profile, or an
    *argv* that no longer starts with ``claude`` (already swapped), is returned as is.
    Raises :class:`command_center.config.LaunchProfileError` when the label is unknown or
    its executable is gone: the caller REFUSES the launch, it never runs bare ``claude``.
    """
    out = list(argv)
    if launch_profile and out and out[0] == CLAUDE:
        from .config import resolve_launch_profile

        out[0] = resolve_launch_profile(launch_profile)
        claim_apikey_session(_argv_session_id(out), out[0])
    return out


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
    launch_profile: str = "",
) -> list[str]:
    """``claude [--name N] [--resume|--continue] [--model] [--session-id] [--effort] … [prompt]``.

    *resume* → ``--resume <id>``, *session_id* → ``--session-id <id>`` (a new session with
    a chosen id), *continue_last* → ``--continue``. *name* ``None`` looks the canonical
    name up by the resumed / chosen id; ``""`` means no name. ``--name`` sits right after
    ``claude``; the positional *prompt* is always LAST (a single argv element — the
    string renderer quotes it), behind a ``--`` end-of-options marker: a prompt that
    begins with ``-`` (a role file whose YAML frontmatter opens with ``---``) is otherwise
    parsed as an unknown option and claude exits at once, and a variadic option in
    *extra* (``--add-dir <dirs...>``) would otherwise swallow the prompt.
    A *launch_profile* swaps ``claude`` for that profile's executable (:func:`profile_argv`).
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
        argv += ["--", prompt]
    return profile_argv(argv, launch_profile)


def claude_command(
    target: SessionLaunch, argv: Sequence[str], *, cwd: str = "", subshell: bool = False
) -> str:
    """The shell STRING form of *argv*, with the session's env pin in front.

    ``<env prefix><mem token>[cd <cwd> && ]claude …`` — or, with *subshell* (the relaunch
    typed into the user's own shell), ``[cd <cwd> && ]( <env prefix><mem token>claude … )``
    so the exports do not persist in that shell. ``<mem token>`` is
    :data:`command_center.mem_token.TYPED_SNIPPET`: the shell mints ``MEM_TOKEN`` through
    ``ccc mem-token`` itself, so the token is never part of the typed text. Every argument
    is shell-quoted; the caller owns rejecting control characters (a newline in a TYPED
    line submits it early).
    """
    from .accounts import session_launch_env_prefix, session_launch_profile
    from .mem_token import TYPED_SNIPPET

    # The shared-memory token is minted by the shell at run time (TYPED_SNIPPET), never
    # written into this line: a typed line lands in the shell's history. It sits before
    # the ``cd … &&`` so a failed ``cd`` still skips claude.
    prefix = session_launch_env_prefix(target) + TYPED_SNIPPET
    cd = f"cd {shlex.quote(cwd)} && " if cwd else ""
    command = shlex.join(profile_argv(argv, session_launch_profile(target)))
    if subshell:
        return f"{cd}( {prefix}{command} )"
    return f"{prefix}{cd}{command}"
