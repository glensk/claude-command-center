#!/usr/bin/env python3
"""The Claude Code hook wiring ccc owns — one source for the installer AND the CLI.

Deliberately import-cheap (nothing but ``__future__``): ``cli.build_parser`` needs the
valid ``ccc hook <event>`` names on EVERY spawn, and reading them from
:mod:`command_center.install` would drag ``json`` / ``shlex`` / ``shutil`` /
``config`` into that hot path. :mod:`command_center.install` re-exports these names, so
``install.HOOK_SPEC`` / ``install.ALL_HOOK_ARGS`` stay the addresses everything else uses.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# The wiring ccc owns, in the order it is appended to each event's hook list — the
# SINGLE source for both the installer and the recognizer. Each entry is
# ``(settings-event-key, matcher-or-None, ccc-hook-arg)``. Order matters for ``Stop``:
# ``stop`` then ``release-locks`` are appended LAST so release-locks runs after any
# foreign Stop hooks (e.g. a user's commit hook) — files are committed before their
# locks release.
HOOK_SPEC: tuple[tuple[str, str | None, str], ...] = (
    ("SessionStart", None, "session-start"),
    ("UserPromptSubmit", None, "user-prompt"),
    ("SessionEnd", None, "session-end"),
    ("PreCompact", None, "pre-compact"),
    # SubagentStart/SubagentStop bracket every IN-PROCESS Agent-tool subagent: the pair
    # keeps the per-session `active_subagents` counter `switch-account` refuses on (a
    # running in-process subagent has no child process and may not be in the transcript yet).
    ("SubagentStart", None, "subagent-start"),
    ("SubagentStop", None, "subagent-stop"),
    ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit", "pre-tool-use"),
    ("PostToolUse", "Edit|Write|MultiEdit|NotebookEdit", "post-tool-use"),
    ("PostToolUse", "TodoWrite|TaskCreate|TaskUpdate", "post-tool-use"),
    ("Stop", None, "stop"),
    ("Stop", None, "release-locks"),
)

# Every ccc hook-arg, in spec order (for doctor's "how many of ours are wired" readout).
ALL_HOOK_ARGS: tuple[str, ...] = tuple(arg for _, _, arg in HOOK_SPEC)

# The DISTINCT hook events, first-seen order — the ``ccc hook <event>`` choices. One
# event can be wired several times (``post-tool-use`` has two PostToolUse matchers), so
# ALL_HOOK_ARGS is a valid *set* source but NOT a valid argparse ``choices=``.
HOOK_EVENTS: tuple[str, ...] = tuple(dict.fromkeys(ALL_HOOK_ARGS))
