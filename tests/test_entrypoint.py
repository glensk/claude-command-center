"""The lean ``ccc aim`` path imports no heavy module (tp#1758 B4).

The byte-for-byte behaviour is pinned by ``test_aim_contract.py``; this file proves the
point of the fast path: a served ``ccc aim`` never imports ``cli``, ``config``,
``models``, ``store``, rich or textual, and a case it cannot serve still reaches ``cli``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from command_center.store import Store

ROOT = Path(__file__).resolve().parents[1]
SID = "sess-entrypoint"
HEAVY = (
    "command_center.cli",
    "command_center.config",
    "command_center.models",
    "command_center.store",
    "rich",
    "textual",
)

_PROBE = """
import json, sys
from command_center import entrypoint
code = entrypoint.main(sys.argv[2:])
mods = sorted(m for m in sys.modules if m.split(".")[0] in ("command_center", "rich", "textual"))
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"code": code, "modules": mods}, fh)
"""


def _home(root: Path) -> Path:
    app = root / "command-center"
    app.mkdir(parents=True)
    (app / "config.toml").write_text("aim_on_main_line = true\n", encoding="utf-8")
    with Store(app / "state.db") as store:
        store.ensure(SID, cwd="/repo")
        store.set_aim(SID, "ship it: tp#1758")
        store.set_subgoals(SID, ["one", "two"])
    return root


def _probe(home: Path, tmp_path: Path, *argv: str) -> tuple[dict[str, object], bytes]:
    report = tmp_path / "report.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CCC_HOME", "CLAUDE_HOME"}
    }
    env.update(PYTHONPATH=str(ROOT), HOME=str(home), CCC_HOME=str(home), CLAUDE_HOME=str(home))
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(report), *argv],
        capture_output=True,
        env=env,
        cwd=home,
        timeout=60,
        check=True,
    )
    return json.loads(report.read_text(encoding="utf-8")), proc.stdout


@pytest.mark.parametrize("fmt", ["statusline", "plain", "bar"])
def test_served_aim_imports_no_heavy_module(tmp_path: Path, fmt: str) -> None:
    home = _home(tmp_path / "home")
    report, out = _probe(home, tmp_path, "aim", "--session", SID, "--format", fmt)
    assert report["code"] == 0
    assert out.strip()  # it answered
    modules = report["modules"]
    assert isinstance(modules, list)
    loaded = [m for m in modules if m.startswith(HEAVY)]
    assert loaded == []


def test_unknown_session_db_missing_falls_back_to_cli(tmp_path: Path) -> None:
    """No DB: the fast path declines, ``cli`` answers (and creates the DB)."""
    home = tmp_path / "empty"
    home.mkdir()
    report, out = _probe(home, tmp_path, "aim", "--session", SID, "--format", "bar")
    assert report["code"] == 0
    assert out == "\x1b[38;5;244m🎯 /aim\x1b[0m\n".encode()
    modules = report["modules"]
    assert isinstance(modules, list)
    assert "command_center.cli" in modules


def test_session_by_cwd_falls_back_to_cli(tmp_path: Path) -> None:
    """No id given or in the env: the registry lookup is ``cli``'s (nothing live here)."""
    home = _home(tmp_path / "home")
    report, out = _probe(home, tmp_path, "aim")
    assert (report["code"], out) == (0, b"")
    modules = report["modules"]
    assert isinstance(modules, list)
    assert "command_center.cli" in modules
