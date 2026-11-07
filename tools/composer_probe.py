#!/usr/bin/env python3
"""Capture a masked, read-only fixture of a throwaway Claude Code session's input box (tp#1844).

One ``async_get_screen_contents`` read of an iTerm session over the Python API (through
``terminal.iterm_screen_tail``); nothing is ever sent to the session. The capture keeps
only the lines from ``--lines`` rows above the input box's top rule downward (the bottom
20 lines when no rule pair is found) and masks every cell that is not a rule glyph, a
prompt/mode glyph or a blank: letters become ``a``, digits ``9``, anything else ``x``.
There is no option to turn masking off.

It refuses (exit 1, nothing read) any session whose current directory is not under
``/tmp/tp1844-``: fixtures come from a throwaway session only, never a working one.

Usage:
  tools/composer_probe.py -s ITERM_SESSION_ID [-n N] [-o PATH] [-c VERSION]

Options:
  -s, --session ID       iTerm session id (``w0t1p0:UUID`` or the bare UUID).
  -n, --lines N          Rows above the top box rule to keep (default: 3).
  -o, --out PATH         Write the fixture JSON here (default: stdout).
  -c, --cc-version VER   Claude Code version to record (default: ``claude --version``).
  -h, --help             Show this help and exit.

Exit codes: 0 written, 1 refused or unreadable, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
from command_center import screenread, terminal  # noqa: E402

#: The only directory prefix a probed session may run in.
ALLOWED_PREFIX = "/tmp/tp1844-"


def cwd_allowed(path: str) -> bool:
    """True when *path* lies under ``/tmp/tp1844-*`` (``/private/tmp`` too)."""
    if not path:
        return False
    real = os.path.realpath(path)  # resolves ``..`` and symlinks before the prefix test
    prefixes = {ALLOWED_PREFIX, "/private" + ALLOWED_PREFIX, os.path.realpath("/tmp") + "/tp1844-"}
    return any(real.startswith(p) for p in prefixes)


def claude_version() -> str:
    """``claude --version``'s ``X.Y.Z`` (``unknown`` when it cannot be read)."""
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    m = re.search(r"\d+\.\d+\.\d+", out or "")
    return m.group(0) if m else "unknown"


def capture(session: str, above: int, cc_version: str) -> tuple[dict[str, object] | None, str]:
    """The masked fixture dict, or ``None`` and the reason it was refused."""
    found = terminal.iterm_session_vars(session, ("path",))
    if found is None:
        return None, "iTerm Python API unreachable"
    if not found:
        return None, "no such iTerm session"
    if not cwd_allowed(found.get("path", "")):
        return None, f"refused: the session's cwd is not under {ALLOWED_PREFIX}*"
    read = terminal.iterm_screen_tail(session)
    if read is None:
        return None, "screen unreadable"
    kept = screenread.masked(screenread.tail_from_top_rule(read, above))
    return screenread.to_json(kept, cc_version), ""


def dumps(data: dict[str, object]) -> str:
    """The fixture as JSON with one screen line per text line (diff-friendly)."""
    head = {k: v for k, v in data.items() if k != "lines"}
    lines = data.get("lines") or []
    assert isinstance(lines, list)
    body = ",\n  ".join(json.dumps(row, ensure_ascii=False) for row in lines)
    head_text = json.dumps(head, ensure_ascii=False)[:-1]
    return f'{head_text}, "lines": [\n  {body}\n]}}\n'


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        prog="composer_probe.py",
        description="Masked read-only fixture of a throwaway session's input box (tp#1844).",
    )
    ap.add_argument("-s", "--session", required=True, help="iTerm session id")
    ap.add_argument("-n", "--lines", type=int, default=3, help="rows above the top rule")
    ap.add_argument("-o", "--out", help="fixture path (default: stdout)")
    ap.add_argument("-c", "--cc-version", help="Claude Code version to record")
    args = ap.parse_args(argv)
    if args.lines < 0:
        ap.error("--lines must be >= 0")
    data, reason = capture(args.session, args.lines, args.cc_version or claude_version())
    if data is None:
        print(f"composer_probe: {reason}", file=sys.stderr)
        return 1
    text = dumps(data)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
