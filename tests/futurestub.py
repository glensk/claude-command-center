"""Shared hermetic setup for the future-job tests (``futuresync``, ``new-prompt``).

A tmp ``CLAUDE_HOME`` (store + flock + sidecar), a tmp ``$GIT_BASE`` with two repos
and a tmp vault holding the ``01-llm/ccc-tasks`` tree, so the test modules do not
each re-declare the same fixture body.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from command_center import config


@dataclass
class FutureEnv:
    """The paths and config every future-job test starts from."""

    cfg: config.Config
    git_base: Path
    vault: Path
    future_dir: Path
    pad: Path


def hermetic_ccc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point ccc at a tmp ``CLAUDE_HOME`` and ``$GIT_BASE``; returns (git_base, vault)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress detached score-aim/short-aim spawns
    git_base = tmp_path / "git"
    (git_base / "home" / "ccc").mkdir(parents=True)
    (git_base / "sdsc" / "zoho").mkdir(parents=True)
    monkeypatch.setenv("GIT_BASE", str(git_base))
    return git_base, tmp_path / "vault"


def future_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **cfg_extra: Any) -> FutureEnv:
    """Point ccc at tmp dirs and return the matching config (scoring/short-aim off)."""
    git_base, vault = hermetic_ccc(tmp_path, monkeypatch)
    future_dir = vault / "01-llm" / "ccc-tasks" / "future"
    pad = vault / "01-llm" / "ccc-tasks" / "new-prompt.md"
    future_dir.mkdir(parents=True)
    cfg = config.Config(
        vault_root=str(vault),
        future_dir=str(future_dir),
        future_pad=str(pad),
        future_delete_grace_sec=600,
        aim_score_on_set=False,
        short_aim=False,
        **cfg_extra,
    )
    return FutureEnv(cfg, git_base, vault, future_dir, pad)


def launchd_cfg(tmp_path: Path) -> config.Config:
    """A minimal config for the launchd/plist tests (test label, tmp vault)."""
    vault = tmp_path / "vault"
    return config.Config(
        launchd_label="com.test.ccc",
        vault_root=str(vault),
        future_dir=str(vault / "01-llm" / "ccc-tasks" / "future"),
    )
