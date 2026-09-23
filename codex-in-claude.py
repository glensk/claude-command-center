#!/usr/bin/env python3
# pylint: disable=invalid-name  # filename intentionally hyphenated (PATH-compat shim)
"""PATH-compat shim: delegate to ``command_center.codex_in_claude:main``.

The real implementation now lives inside the ``command_center`` package (shipped by
the wheel and exposed as the ``codex-in-claude`` console entry point). This thin
repo-root script keeps the historical ``./codex-in-claude.py`` / ``codex-in-claude.py``
invocation working from a source checkout, where the package may not be installed —
it puts this directory (the repo root) on ``sys.path`` before importing.

It is also on ``PATH``, so it runs under whatever ``python3`` comes first there — in a
direnv/nix directory that is an interpreter without ``rich`` (tp#394). It therefore
re-execs into the repo ``.venv`` or, failing that, the uv-tool venv via the stdlib-only
``command_center._direct``; with neither available a missing dependency is one stderr
line and exit 70 (``EX_SOFTWARE``), never a traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO))

# Unused by codex_in_claude (0, 2-9), so a bootstrap failure never reads as e.g. exit 3.
EX_SOFTWARE = 70


def _bootstrap_failure(exc: ModuleNotFoundError) -> int:
    print(
        f"codex-in-claude.py: missing module {exc.name!r} under {sys.executable}; "
        f"fix: `uv sync` in {_REPO}, `uv tool install --editable {_REPO}`, "
        "or call `codex-in-claude`",
        file=sys.stderr,
    )
    return EX_SOFTWARE


if __name__ == "__main__":
    from command_center import _direct  # stdlib-only: safe under any python3

    _direct.reexec_into_env(
        [_REPO / ".venv", _direct.uv_tool_dir() / "claude-command-center"], __file__
    )
    try:
        from command_center.codex_in_claude import main

        _rc = main()
    except ModuleNotFoundError as _exc:
        _rc = _bootstrap_failure(_exc)
    sys.exit(_rc)
