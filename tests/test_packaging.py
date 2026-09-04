"""Packaging smoke test — the wheel must survive a non-editable install.

Builds the wheel ONCE per session, installs it into a scratch venv with a temp
HOME/CLAUDE_HOME, and asserts every console entry point runs and that the package data
(``assets/README.md``) ships and is reachable via ``importlib.resources``. Skipped when
``uv`` is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(_UV is None, reason="uv not available to build/install the wheel")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing output, never raising on a non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300, **kwargs)


@dataclass
class InstalledWheel:
    """A built + installed wheel and the environment its console scripts run under."""

    wheel: Path
    names: set[str]  # everything inside the wheel archive
    bindir: Path  # the venv's console scripts
    python: Path
    env: dict[str, str]


@pytest.fixture(scope="session", name="installed_wheel")
def installed_wheel_fixture(tmp_path_factory: pytest.TempPathFactory) -> InstalledWheel:
    """Build + install the wheel once; every packaging assertion shares it.

    Session-scoped on purpose: `uv build` plus a fresh venv install is the slowest
    thing in the suite, and each extra entry point to smoke-test must not re-pay it.
    """
    assert _UV is not None
    tmp_path = tmp_path_factory.mktemp("packaging")
    dist = tmp_path / "dist"
    build = _run([_UV, "build", "--wheel", "-o", str(dist)], cwd=str(_ROOT))
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())

    venv = tmp_path / "venv"
    assert _run([_UV, "venv", str(venv)]).returncode == 0
    python = venv / "bin" / "python"
    install = _run([_UV, "pip", "install", "--python", str(python), str(wheels[0])])
    assert install.returncode == 0, install.stderr

    home = tmp_path / "home"
    return InstalledWheel(
        wheel=wheels[0],
        names=names,
        bindir=venv / "bin",
        python=python,
        env={**os.environ, "HOME": str(home), "CLAUDE_HOME": str(home / ".claude")},
    )


def test_wheel_ships_entrypoints_and_assets(installed_wheel: InstalledWheel) -> None:
    # The wheel (a zip) carries the package data + the moved-in modules.
    names = installed_wheel.names
    assert "command_center/assets/README.md" in names
    # The onboarding assets ccc init / install-commands / obsidian-setup seed must ship so
    # they survive a non-editable install (importlib.resources reads them at runtime).
    assert "command_center/assets/commands/aim.md" in names
    skill = "command_center/assets/codex/skills/codex-implement-task-and-claude-review/SKILL.md"
    assert skill in names
    # The core skill shipped by default (install-commands) must survive a non-editable install.
    assert "command_center/assets/skills/ccc-mark-done-and-close/SKILL.md" in names
    assert "command_center/assets/obsidian/future.md.tmpl" in names
    assert "command_center/assets/obsidian/plugins.json" in names
    assert "command_center/assets/karabiner/peek-s-p.json" in names
    assert "command_center/codex_in_claude.py" in names
    assert "command_center/session_continue.py" in names

    for exe in ("ccc", "claude-session-continue", "codex-in-claude"):
        result = _run([str(installed_wheel.bindir / exe), "--help"], env=installed_wheel.env)
        assert result.returncode == 0, f"{exe} --help failed: {result.stderr}"

    # assets/README.md reachable via importlib.resources from the INSTALLED package.
    code = (
        "from importlib.resources import files\n"
        "text = (files('command_center') / 'assets' / 'README.md').read_text(encoding='utf-8')\n"
        "assert text.strip(), 'assets/README.md is empty'\n"
        "print('OK')\n"
    )
    probe = _run([str(installed_wheel.python), "-c", code], env=installed_wheel.env)
    assert probe.returncode == 0, probe.stderr
    assert "OK" in probe.stdout


def test_wheel_exposes_codex_in_claude_run_and_order(installed_wheel: InstalledWheel) -> None:
    """External consumers call ``codex-in-claude run|order`` BY NAME, not by path.

    ``codex-review.py`` and sdsc-automations' checker resolve the installed executable
    (the repo-root ``codex-in-claude.py`` is only a source-checkout shim), so both
    subcommands must exist in a non-editable install — a wheel that ships the module but
    not the subcommands would send every consumer back to a bare ``codex exec``.
    """
    for sub in ("run", "order"):
        result = _run(
            [str(installed_wheel.bindir / "codex-in-claude"), sub, "-h"], env=installed_wheel.env
        )
        assert result.returncode == 0, f"codex-in-claude {sub} -h failed: {result.stderr}"
        assert "--json" in result.stdout
