"""The ``load_config`` memo: parse once per file identity, hand out private copies.

``load_config`` is a HOT path, not a startup-only read: one painted frame of 629 rows
re-entered it 629 times through ``tabsymbol.cell_for`` → ``colors.short_folder`` →
``repos.repo_root``, re-parsing the same TOML every time. These tests pin the three
properties that make memoizing it safe — the file's identity is the key, a failed parse is
never cached, and every caller gets an object it owns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from command_center import config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch ``tomllib.load`` to tally real TOML parses; returns a one-slot counter."""
    calls = [0]
    real_load = config.tomllib.load

    def counting_load(handle: Any) -> Any:
        calls[0] += 1
        return real_load(handle)

    monkeypatch.setattr(config.tomllib, "load", counting_load)
    return calls


def test_repeated_loads_parse_the_toml_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _write(config.config_path(), "idle_timeout_min = 42\n")
    config.invalidate_config_cache()
    calls = _count_parses(monkeypatch)

    first = config.load_config()
    second = config.load_config()

    assert calls[0] == 1  # the second call was served from the memo
    assert first.idle_timeout_min == 42
    assert second.idle_timeout_min == 42


def test_rewriting_the_file_reloads_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed file (different mtime AND size) invalidates the memo by itself."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    path = config.config_path()
    _write(path, "idle_timeout_min = 42\n")
    config.invalidate_config_cache()
    assert config.load_config().idle_timeout_min == 42

    _write(path, "idle_timeout_min = 7\nstale_days = 99\n")
    assert config.load_config().idle_timeout_min == 7
    assert config.load_config().stale_days == 99


def test_invalidate_forces_a_reparse_of_an_unchanged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit hatch: same stat key, but the memo is dropped and the file re-read."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _write(config.config_path(), "idle_timeout_min = 42\n")
    config.invalidate_config_cache()
    calls = _count_parses(monkeypatch)

    config.load_config()
    config.load_config()
    assert calls[0] == 1
    config.invalidate_config_cache()
    config.load_config()
    assert calls[0] == 2


def test_save_config_is_visible_to_the_next_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``save_config`` drops the memo itself — mtime granularity cannot be trusted here."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = config.load_config()
    cfg.idle_timeout_min = 31
    config.save_config(cfg)

    assert config.load_config().idle_timeout_min == 31


def test_each_load_returns_an_independent_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers MUTATE what they get (TUI toggles, the suite's vault-path rewrite)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _write(config.config_path(), 'folder_order = ["alpha", "beta"]\n')
    config.invalidate_config_cache()

    first = config.load_config()
    first.folder_order.append("poison")
    first.idle_timeout_min = 999

    second = config.load_config()
    assert second.folder_order == ["alpha", "beta"]
    assert second.idle_timeout_min != 999
    assert second.folder_order is not first.folder_order


def test_an_unparsable_config_is_never_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fail-closed load must not stick: the repaired file has to take effect at once."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    path = config.config_path()
    _write(path, 'llm_custom_command = "unterminated\n')
    config.invalidate_config_cache()
    calls = _count_parses(monkeypatch)

    assert config.load_config().loaded_from_disk is False
    assert config.load_config().loaded_from_disk is False
    assert calls[0] == 2  # re-read every time — the failure was NOT cached

    _write(path, "idle_timeout_min = 12\n")
    repaired = config.load_config()
    assert repaired.loaded_from_disk is True
    assert repaired.idle_timeout_min == 12


def test_a_missing_file_caches_defaults_and_drops_them_when_it_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install memoizes its DEFAULTS; the key changes the moment a file lands."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    config.invalidate_config_cache()
    calls = _count_parses(monkeypatch)

    fresh = config.load_config()
    assert fresh.loaded_from_disk is True
    assert config.load_config().idle_timeout_min == fresh.idle_timeout_min
    assert calls[0] == 0  # nothing to parse — but the defaults ARE memoized

    _write(config.config_path(), "idle_timeout_min = 3\n")
    assert config.load_config().idle_timeout_min == 3
    assert calls[0] == 1
