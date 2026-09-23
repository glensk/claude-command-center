"""The repo-root ``codex-in-claude.py`` shim under a foreign ``python3`` (tp#394).

From a direnv/nix directory the shim's ``#!/usr/bin/env python3`` lands on an
interpreter without ``rich``; it must re-exec into the repo ``.venv`` or the uv-tool
venv, and fail with one clean line (exit 70) when neither exists. ``cmd_headroom``
must report a crash as ``unknown`` / exit 3, the gate's fail-closed code.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from command_center import _direct, codex_in_claude

REPO = Path(__file__).resolve().parent.parent


def _venv(root: Path) -> Path:
    """A fake venv root with an executable ``bin/python``."""
    (root / "bin").mkdir(parents=True)
    py = root / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    return root


class _Exec:
    """Captures ``os.execv`` calls; optionally raises ``OSError`` for given paths."""

    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.failing = failing

    def __call__(self, path: str, argv: list[str]) -> None:
        self.calls.append((path, argv))
        if path in self.failing:
            raise OSError("exec format error")
        raise SystemExit("exec'd")


@pytest.fixture(name="execv")
def _execv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Exec:
    rec = _Exec()
    monkeypatch.setattr(os, "execv", rec)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(sys, "argv", ["shim", "headroom", "--json"])
    return rec


# ---- uv_tool_dir -------------------------------------------------------------------


def test_uv_tool_dir_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert _direct.uv_tool_dir() == tmp_path / "tools"


def test_uv_tool_dir_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert _direct.uv_tool_dir() == tmp_path / "xdg" / "uv" / "tools"


def test_uv_tool_dir_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _direct.uv_tool_dir() == tmp_path / ".local" / "share" / "uv" / "tools"


# ---- reexec_into_env ---------------------------------------------------------------


def test_reexec_prefers_first_candidate(tmp_path: Path, execv: _Exec) -> None:
    first, second = _venv(tmp_path / "venv"), _venv(tmp_path / "tool")
    with pytest.raises(SystemExit):
        _direct.reexec_into_env([first, second], "/x/shim.py")
    assert execv.calls == [
        (
            str(first / "bin" / "python"),
            [str(first / "bin" / "python"), "/x/shim.py", "headroom", "--json"],
        )
    ]


@pytest.mark.parametrize("kind", ["missing", "directory", "broken-symlink", "not-executable"])
def test_reexec_skips_invalid_candidate(tmp_path: Path, execv: _Exec, kind: str) -> None:
    bad = tmp_path / "bad"
    (bad / "bin").mkdir(parents=True)
    py = bad / "bin" / "python"
    if kind == "directory":
        py.mkdir()
    elif kind == "broken-symlink":
        py.symlink_to(tmp_path / "nowhere")
    elif kind == "not-executable":
        py.write_text("")
        py.chmod(0o644)
    good = _venv(tmp_path / "tool")
    with pytest.raises(SystemExit):
        _direct.reexec_into_env([bad, good], "/x/shim.py")
    assert [call[0] for call in execv.calls] == [str(good / "bin" / "python")]


def test_reexec_oserror_falls_through(tmp_path: Path, execv: _Exec) -> None:
    first, second = _venv(tmp_path / "venv"), _venv(tmp_path / "tool")
    execv.failing = (str(first / "bin" / "python"),)
    with pytest.raises(SystemExit):
        _direct.reexec_into_env([first, second], "/x/shim.py")
    assert [call[0] for call in execv.calls] == [
        str(first / "bin" / "python"),
        str(second / "bin" / "python"),
    ]


def test_reexec_all_invalid_returns(tmp_path: Path, execv: _Exec) -> None:
    _direct.reexec_into_env([tmp_path / "a", tmp_path / "b"], "/x/shim.py")
    assert not execv.calls


def test_reexec_all_exec_fail_returns(tmp_path: Path, execv: _Exec) -> None:
    only = _venv(tmp_path / "venv")
    execv.failing = (str(only / "bin" / "python"),)
    _direct.reexec_into_env([only], "/x/shim.py")
    assert len(execv.calls) == 1


def test_reexec_noop_when_already_inside(
    tmp_path: Path, execv: _Exec, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _venv(tmp_path / "venv"), _venv(tmp_path / "tool")
    monkeypatch.setattr(sys, "prefix", str(second))
    _direct.reexec_into_env([first, second], "/x/shim.py")
    assert not execv.calls


def test_reexec_tells_venvs_sharing_a_base_interpreter_apart(
    tmp_path: Path, execv: _Exec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv venvs symlink ``bin/python`` to one shared base; ``sys.prefix`` still differs."""
    base = tmp_path / "base" / "python3"
    base.parent.mkdir()
    base.write_text("#!/bin/sh\n")
    base.chmod(0o755)
    venvs = []
    for name in ("venv", "tool"):
        (tmp_path / name / "bin").mkdir(parents=True)
        (tmp_path / name / "bin" / "python").symlink_to(base)
        venvs.append(tmp_path / name)
    monkeypatch.setattr(sys, "executable", str(base))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
    with pytest.raises(SystemExit):
        _direct.reexec_into_env(venvs, "/x/shim.py")
    assert execv.calls[0][0] == str(venvs[0] / "bin" / "python")


def test_direct_run_never_consults_tool_venv(
    tmp_path: Path, execv: _Exec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_reexec_in_venv`` keeps its repo-``.venv``-only contract."""
    tools = tmp_path / "tools"
    _venv(tools / "claude-command-center")
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    repo = tmp_path / "repo"
    (repo / "command_center").mkdir(parents=True)
    _direct._reexec_in_venv(str(repo / "command_center" / "mod.py"))  # pylint: disable=protected-access
    assert not execv.calls


# ---- the shim end to end -----------------------------------------------------------


def _bare_python(tmp_path: Path) -> Path:
    """A dependency-empty venv's interpreter (no rich, no yaml, no textual)."""
    env = tmp_path / "empty"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env)], check=True)
    return env / "bin" / "python"


def test_shim_reexecs_from_foreign_python(tmp_path: Path) -> None:
    if not (REPO / ".venv" / "bin" / "python").exists():
        pytest.skip("repo .venv absent")
    py = _bare_python(tmp_path)
    env = {**os.environ, "CCC_NO_CODEX": "1"}
    proc = subprocess.run(
        [str(py), str(REPO / "codex-in-claude.py"), "headroom", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert proc.returncode in {0, 1, 3}, proc.stderr
    assert "state" in json.loads(proc.stdout)


def test_shim_bootstrap_failure_is_one_line(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "command_center", repo / "command_center")
    shutil.copy2(REPO / "codex-in-claude.py", repo / "codex-in-claude.py")
    # The CLI degrades on most lazy imports by itself; force an import-time miss.
    mod = repo / "command_center" / "codex_in_claude.py"
    mod.write_text(mod.read_text() + "\nimport rich  # noqa: E402,F401\n")
    (tmp_path / "tools").mkdir()
    py = _bare_python(tmp_path)
    env = {**os.environ, "UV_TOOL_DIR": str(tmp_path / "tools"), "CCC_NO_CODEX": "1"}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [str(py), str(repo / "codex-in-claude.py"), "headroom"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 70
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    assert "missing module" in lines[0]
    assert "Traceback" not in proc.stderr


# ---- cmd_headroom: a crash is "unknown" -------------------------------------------


def _crash(*_a: object, **_k: object) -> dict[str, object]:
    raise ModuleNotFoundError("No module named 'rich'", name="rich")


def test_headroom_crash_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_in_claude, "codex_headroom", _crash)
    rc = codex_in_claude.cmd_headroom(argparse.Namespace(json=True))
    decision = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert decision["state"] == "unknown"
    assert decision["offload_allowed"] is False
    assert decision["reason"].startswith("unknown: ModuleNotFoundError")


def test_headroom_crash_human(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_in_claude, "codex_headroom", _crash)
    rc = codex_in_claude.cmd_headroom(argparse.Namespace(json=False))
    assert rc == 3
    assert "DENIED (unknown" in capsys.readouterr().out
