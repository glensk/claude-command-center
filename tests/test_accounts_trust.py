"""`accounts.ensure_trusted` — pre-trusting the launch cwd per account (2026-09-01)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from command_center import accounts


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def test_default_account_trust_lives_next_to_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude-home"  # the autouse CLAUDE_HOME pin
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    cfg = home.parent / ".claude.json"
    _write(cfg, {"theme": "dark", "projects": {"/repo/x": {"allowedTools": []}}})
    assert accounts.ensure_trusted(str(home), "/repo/x") is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["projects"]["/repo/x"] == {"allowedTools": [], "hasTrustDialogAccepted": True}
    assert accounts.ensure_trusted(str(home), "/repo/x") is False  # idempotent


def test_other_account_trust_lives_inside_its_config_dir(tmp_path: Path) -> None:
    work = tmp_path / ".claude-work"
    _write(work / ".claude.json", {"projects": {}})
    assert accounts.ensure_trusted(str(work), "/repo/y") is True
    data = json.loads((work / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"]["/repo/y"] == {"hasTrustDialogAccepted": True}


def test_missing_config_is_left_alone(tmp_path: Path) -> None:
    assert accounts.ensure_trusted(str(tmp_path / "never-ran"), "/repo/z") is False
    assert not (tmp_path / "never-ran" / ".claude.json").exists()


def test_apply_to_environ_trusts_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / ".claude-work"
    _write(work / ".claude.json", {"projects": {}})
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    accounts.apply_to_environ(str(work))
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(work) or os.environ["CLAUDE_CONFIG_DIR"].endswith(
        ".claude-work"
    )
    data = json.loads((work / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"][str(repo.resolve())] == {"hasTrustDialogAccepted": True}
