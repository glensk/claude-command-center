"""The vault mirror export is gone (tp#2266 B10): pins what its removal must keep true.

* An existing ``config.toml`` still carrying the seven removed mirror keys loads, is not
  flagged as unknown (a writer would otherwise refuse to save), and a save omits them.
* ``ccc --help`` lists no ``sync-mirrors`` verb.
* A lifecycle verb (``mark-done``) spawns nothing — the detached mirror pass is gone.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

from command_center import cli, config, spawn
from command_center.store import Store

#: The keys the export left behind in a live config.toml (Design 9 of the plan).
_REMOVED_KEYS = tuple(
    "mirror_running mirror_done mirror_sessions running_dir done_dir sessions_dir"
    " mirror_allow_unscrubbed".split()
)

_OLD_CONFIG = """\
daemon_interval_sec = 45
mirror_running = false
mirror_done = true
mirror_sessions = false
running_dir = "~/obsidian/01-llm/ccc-tasks/running"
done_dir = "~/obsidian/01-llm/ccc-tasks/done"
sessions_dir = "~/obsidian/01-llm/ccc-tasks/sessions"
mirror_allow_unscrubbed = false
"""


@pytest.fixture(name="old_config")
def _old_config_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_OLD_CONFIG, encoding="utf-8")
    return path


def test_old_config_with_the_removed_keys_loads_and_saves_without_them(
    old_config: Path,
) -> None:
    cfg = config.load_config()
    assert cfg.daemon_interval_sec == 45  # the rest of the file still applies
    for key in _REMOVED_KEYS:
        assert key not in config.DEFAULTS
        assert not hasattr(cfg, key)
    assert config.unknown_config_keys() == []

    config.save_config(cfg)
    with old_config.open("rb") as handle:
        saved = tomllib.load(handle)
    assert not set(_REMOVED_KEYS) & set(saved)
    assert saved.get("daemon_interval_sec") == 45
    # A second round trip re-adds nothing either.
    config.save_config(config.load_config())
    assert not any(key in old_config.read_text(encoding="utf-8") for key in _REMOVED_KEYS)


def test_help_lists_no_sync_mirrors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "sync-mirrors" not in capsys.readouterr().out
    sub = next(
        action
        for action in cli.build_parser()._actions  # pylint: disable=protected-access
        if isinstance(action, argparse._SubParsersAction)  # pylint: disable=protected-access
    )
    assert "sync-mirrors" not in sub.choices
    assert not hasattr(cli, "cmd_sync_mirrors")
    assert not hasattr(cli, "_spawn_sync_mirrors")


def test_mark_done_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("CCC_INTERNAL", raising=False)  # the old spawn's only gate
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    spawned: list[list[str]] = []

    def _record(args: list[str], *, internal: bool = True) -> bool:
        del internal
        spawned.append(list(args))
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", _record)
    with Store() as store:
        store.ensure("s1", cwd=str(tmp_path))
    assert cli.main(["mark-done", "--session", "s1"]) == 0
    assert cli.main(["mark-done", "--session", "s1", "--undo"]) == 0
    assert not spawned
