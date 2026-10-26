#!/usr/bin/env python3
"""Seat-switch slash commands (``/cwork-to-cpriv-cache-write-hit`` & co.) from ``UserPromptSubmit``.

The hook runs before any model request, so the switch works on a seat that hit its limit.
The ``*-cache-write-hit`` names are the zero-token, same-session relaunch (``claude
--resume`` on the other seat — its first turn re-writes the whole conversation into the
new organization's prompt cache). The plain ``/cwork-to-cpriv`` / ``/cpriv-to-cwork`` names
are the handoff to a NEW session (``ccc continue-session``, tp#1300) and are NOT switch
commands here.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import json
import os
import sys

# Seat-switch slash commands → the `claude_accounts` label they move the session to.
_SWITCH_COMMANDS = {
    "/cwork-to-cpriv-cache-write-hit": "private",
    "/cpriv-to-cwork-cache-write-hit": "work",
}


def switch_command_args(prompt: str) -> tuple[str, list[str]] | None:
    """``(label, extra argv)`` for a typed ``/cwork-to-cpriv-cache-write-hit [args]``, else None.

    Unparseable arguments (an unbalanced quote) come back as one ``--invalid`` token, so
    ``switch-account`` refuses them loudly instead of the prompt reaching the model.
    """
    import re  # local: only a typed prompt pays for these
    import shlex

    m = re.match(r"^(\S+)(?:\s+(.*))?$", prompt.strip(), re.DOTALL)
    label = _SWITCH_COMMANDS.get(m.group(1)) if m else None
    if m is None or label is None:
        return None
    try:
        return label, shlex.split(m.group(2) or "")
    except ValueError:
        return label, ["--invalid"]


def _run_switch(sid: str, label: str, extra: list[str]) -> None:
    """Run ``ccc switch-account <label> -N -K`` for *sid* and swallow the prompt.

    The slash command's own ``!`` inline expansion is no longer guaranteed to run
    client-side (Claude Code may hand it to the model instead), and a capped seat rejects
    that model request — the one moment the switch is needed. This hook runs before any
    model request, so the switch works with the current seat out of quota.
    """
    import subprocess  # local: only the intercepted prompt pays for it

    argv = [sys.argv[0] if sys.argv and sys.argv[0] else "ccc"]
    cmd = [*argv, "switch-account", label, "-s", sid, "-N", "-K", *extra]
    env = {**os.environ, "CLAUDE_CODE_SESSION_ID": sid}
    try:
        res = subprocess.run(  # noqa: S603
            cmd, env=env, capture_output=True, text=True, timeout=90, check=False
        )
        out = (res.stdout + res.stderr).strip() or f"switch-account exited {res.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        out = f"error: could not run switch-account ({exc})"
    json.dump({"decision": "block", "reason": out}, sys.stdout)
    sys.stdout.write("\n")


def intercept(sid: str, prompt: str) -> bool:
    """Run the switch and block the prompt when *prompt* is a seat-switch command."""
    parsed = switch_command_args(prompt)
    if parsed is None:
        return False
    _run_switch(sid, *parsed)
    return True
