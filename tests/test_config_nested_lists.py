"""``save_config`` keeps list-of-list keys (``handoff_hooks``) intact (tp#1474).

``save_config`` once rendered every list item with ``_toml_str(str(item))``, so a nested
``list[list[str]]`` value became a list of Python ``repr`` strings and the next
``continue-session accept`` refused the hook. Any save (a TUI toggle, ``config set``)
rewrote the key, whatever it was meant to change.
"""

from __future__ import annotations

import tomllib

from command_center import config

HOOK = ["/Users/albert/tp.py", "handoff", "-S", "{old}", "--to", "{new}", "-j"]


def test_handoff_hooks_round_trip() -> None:
    cfg = config.load_config()
    cfg.handoff_hooks = [list(HOOK), ["echo", 'a "quoted" \\ arg']]
    config.save_config(cfg)
    assert config.load_config().handoff_hooks == [HOOK, ["echo", 'a "quoted" \\ arg']]


def test_save_never_stringifies_nested_entries() -> None:
    """Regression: the written TOML must hold arrays, not ``"['...', ...]"`` strings."""
    cfg = config.load_config()
    cfg.handoff_hooks = [list(HOOK)]
    config.save_config(cfg)
    with config.config_path().open("rb") as handle:
        raw = tomllib.load(handle)
    assert raw["handoff_hooks"] == [HOOK]
    assert "['" not in config.config_path().read_text(encoding="utf-8")


def test_unrelated_save_preserves_handoff_hooks() -> None:
    """The original path: saving for another reason must not corrupt the hooks."""
    cfg = config.load_config()
    cfg.handoff_hooks = [list(HOOK)]
    config.save_config(cfg)
    again = config.load_config()
    again.idle_timeout_min = 41
    config.save_config(again)
    assert config.load_config().handoff_hooks == [HOOK]


def test_empty_nested_list_round_trips() -> None:
    cfg = config.load_config()
    cfg.handoff_hooks = []
    config.save_config(cfg)
    assert config.load_config().handoff_hooks == []
