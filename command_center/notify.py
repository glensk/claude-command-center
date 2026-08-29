#!/usr/bin/env python3
"""Best-effort desktop / Slack notifications (never raises).

Channels are configured via ``notify`` (see :mod:`command_center.config`). The
special channel ``"auto"`` resolves to the current platform's native desktop
notifier — ``macos`` (``osascript``) on macOS, ``linux`` (``notify-send`` /
libnotify) elsewhere — so the public default works out of the box on both. A
missing notifier is a silent no-op, matching the existing failure style.
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
import sys


def _auto_channel() -> str:
    """The native desktop channel for this platform (``macos`` on darwin, else ``linux``)."""
    return "macos" if sys.platform == "darwin" else "linux"


def resolve_channels(channels: list[str]) -> list[str]:
    """Expand any ``"auto"`` entry to the platform-native channel; pass others through."""
    return [_auto_channel() if channel == "auto" else channel for channel in channels]


def notify(title: str, message: str, channels: list[str]) -> None:
    """Send *message* to each configured channel; failures are swallowed."""
    for channel in resolve_channels(channels):
        if channel == "macos":
            _macos(title, message)
        elif channel == "linux":
            _linux(title, message)
        elif channel == "slack":
            _slack(title, message)


def _macos(title: str, message: str) -> None:
    if not shutil.which("osascript"):
        return
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5, check=False)
    except (subprocess.SubprocessError, OSError):
        pass


def _linux(title: str, message: str) -> None:
    """Desktop notification via libnotify's ``notify-send`` (Ubuntu/GNOME etc.)."""
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(
            ["notify-send", "--", title, message], capture_output=True, timeout=5, check=False
        )
    except (subprocess.SubprocessError, OSError):
        pass


def _slack(title: str, message: str) -> None:  # pylint: disable=unused-argument
    """Placeholder — wire to the user's Slack tooling (e.g. slack-api) later."""
    # Intentionally a no-op for now; `macos`/`linux` are the default channels.
    return
