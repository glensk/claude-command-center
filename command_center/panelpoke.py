#!/usr/bin/env python3
"""Install / remove the Karabiner-side panel poker (``<app_home>/panel-poke.sh``).

The poker is a POSIX ``sh`` script (never Python: interpreter start-up alone would eat the
panel latency budget) shipped as the package-data template
``command_center/assets/panel-poke.sh``. :func:`install_poker` reads it through
``importlib.resources`` (so a non-editable install works), bakes in the app home, the
``ccc`` executable of the cold path and any ``CCC_HOME`` / ``CLAUDE_HOME`` override —
every value shell-quoted with :func:`shlex.quote` — and writes it atomically (temp file in
the same directory + :func:`os.replace`) with an explicit mode ``0755``.

Placeholders in the template: ``APP_HOME=@APP_HOME@`` and ``COLD_CCC=@COLD_CCC@`` (the
token is replaced by the quoted value) and the whole line ``# @ENV_OVERRIDES@`` (replaced
by one ``export NAME=<quoted>`` line per override, or by ``:`` when there are none).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import os
import re
import shlex
from importlib.resources import files
from pathlib import Path

POKER_NAME = "panel-poke.sh"
_APP_HOME_TOKEN = "@APP_HOME@"
_COLD_CCC_TOKEN = "@COLD_CCC@"
_ENV_LINE = "# @ENV_OVERRIDES@"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def poker_path(app_home: Path) -> Path:
    """Where the installed poker lives: ``<app_home>/panel-poke.sh``."""
    return Path(app_home) / POKER_NAME


def _template() -> str:
    return (files("command_center") / "assets" / POKER_NAME).read_text(encoding="utf-8")


def render_poker(app_home: Path, ccc_path: str, env_overrides: dict[str, str]) -> str:
    """Return the poker text with every placeholder replaced by a shell-quoted value.

    Raises ``ValueError`` on an invalid environment-variable name or when the template
    does not carry each placeholder exactly once (a drifted template must not install).
    """
    text = _template()
    for token in (_APP_HOME_TOKEN, _COLD_CCC_TOKEN):
        if text.count(token) != 1:
            raise ValueError(f"poker template must contain {token} exactly once")
    lines = text.split("\n")
    if lines.count(_ENV_LINE) != 1:
        raise ValueError(f"poker template must contain the line {_ENV_LINE!r} exactly once")
    exports: list[str] = []
    for name, value in env_overrides.items():
        if not _ENV_NAME.match(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        exports.append(f"export {name}={shlex.quote(value)}")
    lines[lines.index(_ENV_LINE)] = "\n".join(exports) if exports else ":"
    text = "\n".join(lines)
    text = text.replace(_APP_HOME_TOKEN, shlex.quote(str(app_home)))
    return text.replace(_COLD_CCC_TOKEN, shlex.quote(ccc_path))


def install_poker(app_home: Path, ccc_path: str, env_overrides: dict[str, str]) -> Path:
    """Write the rendered poker to ``<app_home>/panel-poke.sh`` atomically, mode 0755.

    *env_overrides* (e.g. ``CCC_HOME`` / ``CLAUDE_HOME`` when set — the caller decides)
    become ``export`` lines, so the cold ``ccc`` sees the same home under Karabiner's
    minimal environment. Returns the installed path.
    """
    app_home = Path(app_home)
    text = render_poker(app_home, ccc_path, env_overrides)
    app_home.mkdir(parents=True, exist_ok=True)
    target = poker_path(app_home)
    tmp = target.with_name(f".{POKER_NAME}.ccc-tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o755)  # explicit: the umask must not strip the exec bits
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return target


def remove_poker(app_home: Path) -> bool:
    """Delete the installed poker; ``True`` if a file was removed."""
    try:
        poker_path(app_home).unlink()
    except FileNotFoundError:
        return False
    return True
