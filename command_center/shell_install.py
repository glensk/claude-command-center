#!/usr/bin/env python3
"""``ccc install-shell`` — opt-in shell rc integration (AIM-at-startup + tab badges).

Writes a single MARKERED block into the user's shell rc (``~/.zshrc`` for zsh,
``~/.bashrc`` for bash — detected from ``$SHELL``, overridable) containing two
independent, individually-skippable pieces:

* **AIM-at-startup wrapper** — a shell function (default name ``c``) that prompts for
  this session's AIM and launches ``claude`` with ``CLAUDE_SESSION_AIM`` set (the exact
  env var the ``SessionStart`` hook reads, see :func:`command_center.hooks`), so a
  session starts already knowing its done-condition. Empty input runs ``claude`` plain.
* **Cross-terminal tab badges** — a precmd/chpwd hook that sets the terminal title to
  ``"<symbol> <repo-leaf>"`` via plain OSC (works on gnome-terminal / any emulator, not
  just iTerm). The symbol comes from ``ccc tab-symbol --print <cwd>`` — cached per
  directory in a shell variable so ``ccc`` is spawned at most once per ``cd``, never per
  prompt.

Everything is reversible: rerun replaces the block (idempotent), ``--uninstall`` removes
only the block, ``--dry-run`` prints it. Easy off = uninstall, or just call ``claude``
directly (the wrapper only shadows the chosen name).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import re
import shutil
import sys
import time
from pathlib import Path

MARKER_START = "# >>> ccc shell integration >>>"
MARKER_END = "# <<< ccc shell integration <<<"
DEFAULT_WRAPPER_NAME = "c"

# The env var the SessionStart hook consumes (command_center/hooks.py) — the wrapper
# MUST export exactly this so a hook-seeded AIM lands on the session.
AIM_ENV_VAR = "CLAUDE_SESSION_AIM"


# --------------------------------------------------------------------------- #
# shell + rc detection
# --------------------------------------------------------------------------- #
def detect_shell() -> str:
    """The user's shell family from ``$SHELL`` — ``"zsh"`` or ``"bash"`` (default bash)."""
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    return "zsh" if "zsh" in shell else "bash"


def default_rc(shell: str) -> Path:
    """The conventional rc file for *shell* (``~/.zshrc`` / ``~/.bashrc``)."""
    return Path.home() / (".zshrc" if shell == "zsh" else ".bashrc")


# --------------------------------------------------------------------------- #
# block content (pure)
# --------------------------------------------------------------------------- #
def wrapper_function(name: str) -> str:
    """The AIM-at-startup wrapper function named *name* (shell-agnostic, zsh + bash)."""
    return "\n".join(
        [
            f"# AIM-at-startup: '{name}' asks this session's done-condition, then runs claude.",
            f"{name}() {{",
            "  local reply",
            "  printf 'AIM of this session (empty to skip): '",
            "  read -r reply",
            '  if [ -n "$reply" ]; then',
            f'    {AIM_ENV_VAR}="$reply" command claude "$@"',
            "  else",
            # blank the var explicitly: a nested launch from inside a session that
            # exported it must not inherit the parent's AIM (empty = "no AIM" to the hook)
            f'    {AIM_ENV_VAR}= command claude "$@"',
            "  fi",
            "}",
        ]
    )


def badges_hook(shell: str) -> str:
    """The cross-terminal tab-badge precmd/chpwd hook for *shell* (plain OSC title)."""
    body = "\n".join(
        [
            "# Tab badges: set the terminal title to '<symbol> <repo-leaf>' on directory change.",
            "# The symbol is cached per directory so `ccc` is spawned at most once per `cd`.",
            "_ccc_tab_badge() {",
            _interactive_guard(shell),
            '  if [ "$PWD" != "$_ccc_badge_dir" ]; then',
            '    _ccc_badge_dir="$PWD"',
            '    _ccc_badge_sym="$(ccc tab-symbol --print "$PWD" 2>/dev/null)"',
            "  fi",
            r"""  printf '\033]0;%s %s\007' "$_ccc_badge_sym" "${PWD##*/}" """.rstrip(),
            "}",
        ]
    )
    if shell == "zsh":
        register = "autoload -Uz add-zsh-hook 2>/dev/null && add-zsh-hook precmd _ccc_tab_badge"
    else:
        register = "\n".join(
            [
                'case ";${PROMPT_COMMAND};" in',
                "  *';_ccc_tab_badge;'*) ;;",
                '  *) PROMPT_COMMAND="_ccc_tab_badge;${PROMPT_COMMAND}" ;;',
                "esac",
            ]
        )
    return f"{body}\n{register}"


def _interactive_guard(shell: str) -> str:
    """A one-line 'return unless interactive' guard for *shell*."""
    if shell == "zsh":
        return "  [[ -o interactive ]] || return"
    return '  case "$-" in *i*) ;; *) return ;; esac'


def build_block(
    shell: str,
    *,
    wrapper_name: str = DEFAULT_WRAPPER_NAME,
    include_wrapper: bool = True,
    include_badges: bool = True,
) -> str:
    """Assemble the full markered rc block (no trailing newline).

    Starts with :data:`MARKER_START` and ends with :data:`MARKER_END`; between them,
    whichever of the wrapper / badges pieces were requested (at least one is expected).
    """
    parts: list[str] = [
        MARKER_START,
        "# Managed by `ccc install-shell` — edit via that command (rerun replaces this block).",
    ]
    if include_wrapper:
        parts.append(wrapper_function(wrapper_name))
    if include_badges:
        parts.append(badges_hook(shell))
    parts.append(MARKER_END)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# block find / replace / strip (pure)
# --------------------------------------------------------------------------- #
def find_block(text: str) -> tuple[int, int] | None:
    """Return ``(start, end)`` char offsets of the markered block, or ``None``.

    ``end`` is the index just past :data:`MARKER_END`.
    """
    start = text.find(MARKER_START)
    if start == -1:
        return None
    end = text.find(MARKER_END, start)
    if end == -1:
        return None
    return (start, end + len(MARKER_END))


def replace_block(text: str, block: str) -> str:
    """Insert or replace the ccc block in *text* (idempotent)."""
    span = find_block(text)
    if span is not None:
        start, end = span
        return text[:start] + block + text[end:]
    prefix = text.rstrip("\n")
    if prefix:
        return f"{prefix}\n\n{block}\n"
    return f"{block}\n"


def strip_block(text: str) -> str:
    """Return *text* with the ccc block removed, collapsing the newlines it leaves behind."""
    span = find_block(text)
    if span is None:
        return text
    start, end = span
    before = text[:start].rstrip("\n")
    after = text[end:].lstrip("\n")
    if before and after:
        return f"{before}\n\n{after}"
    if before:
        return f"{before}\n"
    return after


# --------------------------------------------------------------------------- #
# collision detection (pure over the rc text; PATH check is monkeypatch-friendly)
# --------------------------------------------------------------------------- #
def collision_reason(name: str, rc_text: str, *, check_path: bool = True) -> str:
    """Why installing a wrapper named *name* would clash, or ``""`` when it is free.

    The check scans the rc with ccc's OWN block removed first, so a rerun (whose block
    already defines the wrapper) is never a self-collision. Detects an existing alias, a
    shell function, or — when *check_path* — a same-named binary on ``$PATH``.
    """
    stripped = strip_block(rc_text)
    if re.search(rf"(?m)^\s*alias\s+{re.escape(name)}=", stripped):
        return f"an alias named {name!r} is already defined in the rc file"
    if re.search(rf"(?m)^\s*(?:function\s+)?{re.escape(name)}\s*\(\)", stripped):
        return f"a function named {name!r} is already defined in the rc file"
    if re.search(rf"(?m)^\s*function\s+{re.escape(name)}\b", stripped):
        return f"a function named {name!r} is already defined in the rc file"
    if check_path:
        found = shutil.which(name)
        if found:
            return f"a command named {name!r} is already on your PATH ({found})"
    return ""


# --------------------------------------------------------------------------- #
# install / uninstall / dry-run
# --------------------------------------------------------------------------- #
def _utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def install(  # pylint: disable=too-many-arguments
    *,
    rc_path: str | Path | None = None,
    shell: str | None = None,
    wrapper_name: str = DEFAULT_WRAPPER_NAME,
    include_wrapper: bool = True,
    include_badges: bool = True,
    dry_run: bool = False,
    uninstall: bool = False,
    check_path: bool = True,
) -> int:
    """Install / update (or --uninstall / --dry-run) the ccc shell integration block."""
    shell = shell or detect_shell()
    rc = (Path(rc_path) if rc_path else default_rc(shell)).expanduser()
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""

    if uninstall:
        return _do_uninstall(rc, text, dry_run=dry_run)

    if not include_wrapper and not include_badges:
        print("nothing to install: --no-wrapper and --no-badges skip both pieces.", file=sys.stderr)
        return 1

    if include_wrapper:
        reason = collision_reason(wrapper_name, text, check_path=check_path)
        if reason:
            print(
                f"refusing to install the {wrapper_name!r} wrapper: {reason}.\n"
                "  pass -w/--wrapper-name NAME for a different name, "
                "or --no-wrapper to skip the wrapper.",
                file=sys.stderr,
            )
            return 1

    block = build_block(
        shell,
        wrapper_name=wrapper_name,
        include_wrapper=include_wrapper,
        include_badges=include_badges,
    )
    if dry_run:
        print(f"# target rc: {rc}  (shell: {shell})")
        print(block)
        return 0

    backup = _backup(rc, text)
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text(replace_block(text, block), encoding="utf-8")
    action = "updated" if find_block(text) else "installed"
    print(f"{action} ccc shell integration in {rc}")
    if backup is not None:
        print(f"  backed up previous rc → {backup}")
    print(f"  reload it: source {rc}  (or open a new terminal)")
    return 0


def _do_uninstall(rc: Path, text: str, *, dry_run: bool) -> int:
    if find_block(text) is None:
        print(f"no ccc shell integration block found in {rc}")
        return 0
    if dry_run:
        print(f"# would remove the ccc block from {rc}")
        return 0
    backup = _backup(rc, text)
    rc.write_text(strip_block(text), encoding="utf-8")
    print(f"removed ccc shell integration from {rc}")
    if backup is not None:
        print(f"  backed up previous rc → {backup}")
    return 0


def _backup(rc: Path, text: str) -> Path | None:
    """Timestamped backup of *rc* before a write; ``None`` when the file did not exist."""
    if not text:
        return None
    backup = rc.with_name(f"{rc.name}.ccc-backup-{_utc_stamp()}")
    try:
        backup.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return backup
