#!/usr/bin/env python3
"""Toggle Claude Code's native idle / "waiting for input" macOS popups.

The `ti` TUI chord and `ccc toggle-idle` both flip a single boolean —
``agentPushNotifEnabled`` — in Claude Code's own ``settings.json``. That key is
the source of the "a session is waiting for your input" desktop notifications, so
gating it directly (rather than adding a parallel ccc notification path) means the
toggle controls the exact popups the user already gets. It is global (every
session, and it also covers permission prompts) and a running session may need a
restart to pick up the change.

``~/.claude/settings.json`` may be a stow-managed symlink into a dotfiles source
tree. Writes therefore go to the symlink's RESOLVED target via
an atomic temp+``os.replace`` so the symlink itself is never clobbered by a plain
file. The common flip is a surgical single-token edit (regex), preserving the
file byte-for-byte except the one boolean, to keep the dotfiles diff minimal; only
an absent/oddly-shaped key falls back to a full JSON round-trip.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import os
import re
from pathlib import Path

from . import config

# The Claude Code setting that enables the idle/permission push notifications.
KEY = "agentPushNotifEnabled"

# Matches the single top-level `"agentPushNotifEnabled": true|false` token so a
# flip rewrites only its value and leaves the rest of settings.json untouched.
_VALUE_RE = re.compile(rf'("{re.escape(KEY)}"\s*:\s*)(true|false)\b')


def settings_path() -> Path:
    """Path to the DEFAULT account's ``settings.json`` (honours ``CLAUDE_HOME``)."""
    return config.claude_home() / "settings.json"


def settings_paths() -> list[Path]:
    """Every configured account's ``settings.json``, realpath-DEDUPED (default first).

    Some setups symlink several accounts' settings.json to the SAME underlying
    file, so a naive "write both" would process one
    file twice; deduping by resolved real path writes it exactly once. The default
    account leads so its file is the one :func:`is_enabled` reads back.
    """
    default = config.claude_home()
    dirs = sorted(config.claude_config_dirs().values(), key=lambda d: 0 if d == default else 1)
    if default not in dirs:
        dirs.insert(0, default)
    seen: set[str] = set()
    out: list[Path] = []
    for directory in dirs:
        path = directory / "settings.json"
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def is_enabled() -> bool:
    """Whether the idle/waiting macOS popups are currently on.

    Defaults to ``True`` (the popups are on by default, and a first ``ti`` press
    should mute) when the file is missing, unreadable, or the key is absent.
    """
    try:
        text = settings_path().read_text(encoding="utf-8")
    except OSError:
        return True
    match = _VALUE_RE.search(text)
    if match is not None:
        return match.group(2) == "true"
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return True
    return bool(data.get(KEY, True)) if isinstance(data, dict) else True


def set_enabled(enabled: bool) -> None:
    """Set ``agentPushNotifEnabled`` to *enabled* across every configured account.

    Writes each account's ``settings.json`` (realpath-DEDUPED via :func:`settings_paths`
    so a shared stow file is written ONCE), preserving every other key. An account whose
    settings.json does not exist yet is skipped — the ``ti`` toggle must never crash on a
    single-account or partially-provisioned machine.
    """
    for path in settings_paths():
        if not path.exists():
            continue  # a configured account with no settings.json yet — nothing to flip
        _set_enabled_one(path, enabled)


def _set_enabled_one(path: Path, enabled: bool) -> None:
    """Flip the flag in a SINGLE settings.json, byte-preserving where possible.

    Raises ``ValueError``/``JSONDecodeError`` on a non-object settings file — callers
    surface that rather than silently no-op.
    """
    text = path.read_text(encoding="utf-8")
    token = "true" if enabled else "false"
    new_text, count = _VALUE_RE.subn(rf"\g<1>{token}", text, count=1)
    if count == 0:
        # Key absent or non-standard form: insert/normalise via a JSON round-trip.
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("settings.json is not a JSON object")
        data[KEY] = bool(enabled)
        new_text = json.dumps(data, indent=2) + "\n"
    _write_through(path, new_text)


def toggle() -> bool:
    """Flip the flag and return the NEW state (``True`` = popups on)."""
    new_state = not is_enabled()
    set_enabled(new_state)
    return new_state


def _write_through(path: Path, text: str) -> None:
    """Atomically write *text* to *path*, following (never replacing) a symlink."""
    target = path.resolve()  # real file behind any symlink chain
    tmp = target.with_name(f".{target.name}.ccc-tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)  # atomic; the symlink at `path` still points here
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
