#!/usr/bin/env python3
"""Platform seam for the periodic ``ccc daemon`` service.

One place that decides whether the background daemon (and the future-sync watcher)
are managed by **launchd** (macOS, :mod:`command_center.launchd`) or **systemd user
units** (Linux, :mod:`command_center.systemdunit`), so ``cli.py`` and ``doctor.py``
stay platform-agnostic. On any other platform the operations report "unsupported"
and return non-zero rather than raising.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import sys

from . import config

# The launchd / systemdunit imports are deliberately lazy (inside each function) so the
# macOS-only and Linux-only modules never load on the other platform.
# pylint: disable=import-outside-toplevel


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def manager_name() -> str:
    """Human label for the platform's service manager (``launchd`` / ``systemd`` / ``none``)."""
    if _is_macos():
        return "launchd"
    if _is_linux():
        return "systemd"
    return "none"


def supported() -> bool:
    """True when this platform has a service manager ccc can drive."""
    return _is_macos() or _is_linux()


def install() -> int:
    """Install + start the periodic daemon service on this platform."""
    if _is_macos():
        from . import launchd

        return launchd.install()
    if _is_linux():
        from . import systemdunit

        return systemdunit.install()
    print(f"no supported service manager for platform {sys.platform!r}")
    return 1


def uninstall() -> int:
    """Uninstall the periodic daemon service on this platform."""
    if _is_macos():
        from . import launchd

        return launchd.uninstall()
    if _is_linux():
        from . import systemdunit

        return systemdunit.uninstall()
    print(f"no supported service manager for platform {sys.platform!r}")
    return 1


def status() -> int:
    """Print the daemon service status on this platform."""
    if _is_macos():
        from . import launchd

        loaded = launchd.is_loaded()
        print(f"launchd agent {launchd.label()}: {launchd.state_badge(loaded)}")
        return 0 if loaded else 1
    if _is_linux():
        from . import systemdunit

        return systemdunit.status()
    print(f"no supported service manager for platform {sys.platform!r}")
    return 1


def is_installed(cfg: config.Config | None = None) -> bool:
    """True when the daemon service unit(s)/agent are present on disk for this platform."""
    if _is_macos():
        from . import launchd

        return launchd.is_installed()
    if _is_linux():
        from . import systemdunit

        return systemdunit.is_installed(cfg)
    return False


def is_active(cfg: config.Config | None = None) -> bool:
    """True when the daemon is loaded (launchd) / its timer is active (systemd)."""
    if _is_macos():
        from . import launchd

        return launchd.is_loaded()
    if _is_linux():
        from . import systemdunit

        return systemdunit.is_active(cfg)
    return False
