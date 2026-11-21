"""``ccc config set|unset`` (tp#2372 F3b5, Design 12)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from command_center import cli, config


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CCC_HOME", str(tmp_path / "ccc"))
    config.invalidate_config_cache()
    return tmp_path


def _run(*argv: str) -> int:
    args: argparse.Namespace = cli.build_parser().parse_args(["config", *argv])
    rc: int = args.func(args)
    return rc


def test_set_and_unset_a_string_key_roundtrip() -> None:
    assert _run("set", "shared_store_path", "/tmp/x/shared.db") == 0
    assert config.load_config().shared_store_path == "/tmp/x/shared.db"
    assert _run("unset", "shared_store_path") == 0
    assert config.load_config().shared_store_path == ""


def test_types_are_coerced_from_the_default() -> None:
    assert _run("set", "kill_closes_tab", "true") == 0
    assert _run("set", "idle_timeout_min", "15") == 0
    assert _run("set", "notify", "a, b") == 0
    cfg = config.load_config()
    assert cfg.kill_closes_tab is True
    assert cfg.idle_timeout_min == 15
    assert cfg.notify == ["a", "b"]
    assert _run("set", "notify", '["x"]') == 0
    assert config.load_config().notify == ["x"]


def test_inert_keys_are_settable_and_unset_restores_the_default() -> None:
    key = config.INERT_DEFAULT_KEYS[0]
    assert key in config.DEFAULTS
    assert _run("set", key, "true") == 0
    assert getattr(config.load_config(), key) is True
    assert _run("unset", key) == 0
    assert getattr(config.load_config(), key) == config.DEFAULTS[key]


def test_unknown_key_and_bad_values_are_refused_and_write_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run("set", "no_such_key", "1") == 2
    assert _run("unset", "no_such_key") == 2
    assert _run("set", "idle_timeout_min", "soon") == 2
    assert _run("set", "kill_closes_tab", "maybe") == 2
    assert _run("set", "idle_timeout_min") == 2
    assert _run("unset", "idle_timeout_min", "5") == 2
    assert "error" in capsys.readouterr().err
    assert not config.config_path().exists()


def test_an_unparsable_config_is_never_overwritten() -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("this is = = not toml\n", encoding="utf-8")
    config.invalidate_config_cache()
    assert _run("set", "idle_timeout_min", "5") == 2
    assert path.read_text(encoding="utf-8") == "this is = = not toml\n"


def test_the_write_is_atomic_and_leaves_no_temp_files() -> None:
    assert _run("set", "idle_timeout_min", "7") == 0
    names = sorted(p.name for p in config.config_path().parent.iterdir())
    assert "config.toml" in names
    assert not [n for n in names if n.endswith(".tmp")]
