"""Tests for config save/load round-trip and folder shortening."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from command_center import colors, config

# The autouse conftest fixture pins ``config.claude_config_dirs``; capture the real
# implementation at import (before any fixture runs) so these tests can exercise it.
_real_claude_config_dirs = config.claude_config_dirs


def test_config_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = config.load_config()
    cfg.idle_timeout_min = 30
    cfg.nag_every_n_turns = 0
    cfg.daemon_interval_sec = 600
    cfg.reap = False
    config.save_config(cfg)

    reloaded = config.load_config()
    assert reloaded.idle_timeout_min == 30
    assert reloaded.nag_every_n_turns == 0
    assert reloaded.daemon_interval_sec == 600
    assert reloaded.reap is False


def test_usage_refresh_config_keys_defaults_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The usage-card cadence keys exist in BOTH DEFAULTS and the dataclass (two-place
    # pattern), carry the documented defaults, and survive a save/load round-trip.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    for key in (
        "usage_refresh_sec",
        "copilot_usage_refresh_sec",
        "copilot_usage_refresh_active_sec",
    ):
        assert key in config.DEFAULTS
        assert hasattr(config.Config(), key)
    fresh = config.load_config()
    assert fresh.usage_refresh_sec == 5.0
    assert fresh.copilot_usage_refresh_sec == 900
    assert fresh.copilot_usage_refresh_active_sec == 300

    fresh.usage_refresh_sec = 3.0
    fresh.copilot_usage_refresh_active_sec = 120
    config.save_config(fresh)
    reloaded = config.load_config()
    assert reloaded.usage_refresh_sec == 3.0
    assert reloaded.copilot_usage_refresh_active_sec == 120
    assert reloaded.copilot_usage_refresh_sec == 900


def test_multi_account_config_keys_defaults_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The multi-account keys exist in BOTH DEFAULTS and the dataclass, and round-trip.

    ``claude_accounts`` is list[str] precisely so ``save_config`` (which serializes
    bool/int/float/list[str]/str) round-trips it.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    for key in (
        "claude_accounts",
        "usage_card_private",
        "usage_card_work",
        "usage_card_codex",
        "usage_card_copilot",
        "nixos_overseer_dir",
        "card_nixos_overseer_supervised",
        "card_nixos_overseer_tier_a",
        "llm_account",
    ):
        assert key in config.DEFAULTS
        assert hasattr(config.Config(), key)
    fresh = config.load_config()
    assert fresh.claude_accounts == []
    assert fresh.usage_card_private is True
    assert fresh.usage_card_work is True
    assert fresh.usage_card_codex is True
    assert fresh.usage_card_copilot is True
    # The nixos-overseer cards: dir defaults empty (feature off), supervised shown,
    # tier_a hidden — and all three round-trip through save/load.
    assert fresh.nixos_overseer_dir == ""
    assert fresh.card_nixos_overseer_supervised is True
    assert fresh.card_nixos_overseer_tier_a is False
    assert fresh.llm_account == "private"

    fresh.claude_accounts = ["private=~/.claude", "work=~/.claude-work"]
    fresh.usage_card_work = False
    fresh.usage_card_copilot = False
    fresh.nixos_overseer_dir = "~/overseer"
    fresh.card_nixos_overseer_supervised = False
    fresh.card_nixos_overseer_tier_a = True
    fresh.llm_account = "work"
    config.save_config(fresh)
    reloaded = config.load_config()
    assert reloaded.claude_accounts == ["private=~/.claude", "work=~/.claude-work"]
    assert reloaded.usage_card_work is False
    assert reloaded.usage_card_copilot is False
    assert reloaded.usage_card_private is True
    assert reloaded.nixos_overseer_dir == "~/overseer"
    assert reloaded.card_nixos_overseer_supervised is False
    assert reloaded.card_nixos_overseer_tier_a is True
    assert reloaded.llm_account == "work"


def test_claude_config_dirs_parses_validates_and_skips_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty → single private default; valid entries parse+resolve; bad ones are skipped."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    def _cfg_with(accounts: list[str]) -> config.Config:
        cfg = config.Config()
        cfg.claude_accounts = accounts
        return cfg

    # Empty → the single-account default.
    monkeypatch.setattr(config, "load_config", lambda: _cfg_with([]))
    assert _real_claude_config_dirs() == {"private": config.claude_home()}

    # Valid entries are parsed, expanduser()-ed and resolve()-d.
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: _cfg_with([f"private={tmp_path / 'priv'}", f"work={tmp_path / 'work'}"]),
    )
    dirs = _real_claude_config_dirs()
    assert set(dirs) == {"private", "work"}
    assert dirs["work"] == (tmp_path / "work").resolve()

    # Malformed entries are skipped without crashing: no '=', an uppercase label, a
    # label carrying a path separator, and an empty path — only the good one survives.
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: _cfg_with(
            [
                f"good={tmp_path / 'g'}",
                "noequals",
                f"Bad={tmp_path / 'x'}",
                f"pa/th={tmp_path / 'y'}",
                "empty=",
            ]
        ),
    )
    dirs = _real_claude_config_dirs()
    assert set(dirs) == {"good"}

    # If every entry is malformed, fall back to the single private default.
    monkeypatch.setattr(config, "load_config", lambda: _cfg_with(["nope", "Bad=/x"]))
    assert _real_claude_config_dirs() == {"private": config.claude_home()}


def test_unparsable_config_is_never_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.toml that fails to parse loads as fail-closed and can't be overwritten.

    Reproduces the root cause of the real-config wipe: an existing but invalid
    config.toml must load to DEFAULTS with ``loaded_from_disk is False``, and
    ``save_config`` on that Config must raise (leaving the on-disk bytes untouched)
    rather than persist defaults over the broken-but-recoverable file.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg_file = config.config_path()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    bad = "foo = [unclosed\n"
    cfg_file.write_text(bad, encoding="utf-8")

    loaded = config.load_config()
    assert loaded.loaded_from_disk is False
    assert loaded.idle_timeout_min == config.DEFAULTS["idle_timeout_min"]  # fell back to defaults

    with pytest.raises(RuntimeError):
        config.save_config(loaded)
    # The unparsable file is left exactly as-is (fix-by-hand contract).
    assert cfg_file.read_text(encoding="utf-8") == bad


def test_save_config_backs_up_prior_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overwriting a valid config copies the prior bytes to config.toml.bak."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = config.load_config()
    cfg.idle_timeout_min = 42
    config.save_config(cfg)
    original = config.config_path().read_bytes()
    bak = config.config_path().with_name("config.toml.bak")
    assert not bak.exists()  # first write of a fresh install: nothing to back up

    reloaded = config.load_config()
    assert reloaded.loaded_from_disk is True
    reloaded.idle_timeout_min = 99
    config.save_config(reloaded)

    # The backup holds the EXACT prior content; the live file holds the change.
    assert bak.read_bytes() == original
    assert config.load_config().idle_timeout_min == 99


def test_fresh_install_saves_without_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file yet ⇒ loaded_from_disk True, save writes fine, no .bak created."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert not config.config_path().exists()
    cfg = config.load_config()
    assert cfg.loaded_from_disk is True
    config.save_config(cfg)
    assert config.config_path().exists()
    assert not config.config_path().with_name("config.toml.bak").exists()


def test_loaded_from_disk_never_serialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-closed sentinel must never leak into the saved TOML.

    Parse the written file and inspect the TOML KEYS rather than substring-matching the
    raw text — a tmp path can itself contain the string ``loaded_from_disk`` (the vault
    dirs the conftest injects embed the test name).
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = config.load_config()
    config.save_config(cfg)
    with config.config_path().open("rb") as handle:
        written = tomllib.load(handle)
    assert "loaded_from_disk" not in written
    assert "loaded_from_disk" not in config.DEFAULTS


def test_quoted_string_value_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A string value carrying inner double quotes must stay valid TOML.

    Regression for the real bricking: ``llm_custom_command`` (the routing hatch) holds a
    shell command with quoted ``${VAR:+-p "$VAR"}`` expansions. Serialized with a naive
    f-string the inner ``"`` closed the basic string early, ``tomllib`` refused the file,
    ``load_config`` fell back to pure DEFAULTS and every opt-in feature silently died.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    command = (
        'ai.py prompt -i -R judge ${CCC_LLM_PURPOSE:+-p "$CCC_LLM_PURPOSE"} '
        '${CCC_LLM_NOTE:+-N "$CCC_LLM_NOTE"}'
    )
    cfg = config.load_config()
    cfg.llm_custom_command = command
    config.save_config(cfg)

    # (a) the raw file is parsable TOML at all, and (b) holds the exact string.
    with config.config_path().open("rb") as handle:
        written = tomllib.load(handle)
    assert written["llm_custom_command"] == command

    # (c) the loaded Config is the real thing, not the fail-closed defaults fallback.
    reloaded = config.load_config()
    assert reloaded.llm_custom_command == command
    assert reloaded.loaded_from_disk is True


def test_backslash_and_control_chars_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backslashes, newlines and tabs are escaped, not emitted raw."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    gnarly = 'C:\\path\\to\\x\nsecond "line"\tafter tab'
    cfg = config.load_config()
    cfg.resume_continue_script = gnarly
    config.save_config(cfg)

    with config.config_path().open("rb") as handle:
        written = tomllib.load(handle)
    assert written["resume_continue_script"] == gnarly
    reloaded = config.load_config()
    assert reloaded.resume_continue_script == gnarly
    assert reloaded.loaded_from_disk is True


def test_quoted_list_and_dict_entries_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List items and dict keys/values are escaped too, not just scalar strings."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    quoted_item = 'work=~/.claude-"work"'
    cfg = config.load_config()
    cfg.claude_accounts = [quoted_item, "private=~/.claude"]
    cfg.category_colors = {'odd"cat': '#123456 "x"'}
    config.save_config(cfg)

    with config.config_path().open("rb") as handle:
        written = tomllib.load(handle)
    assert written["claude_accounts"] == [quoted_item, "private=~/.claude"]
    assert written["category_colors"] == {'odd"cat': '#123456 "x"'}
    reloaded = config.load_config()
    assert reloaded.claude_accounts == [quoted_item, "private=~/.claude"]
    assert reloaded.category_colors == {'odd"cat': '#123456 "x"'}
    assert reloaded.loaded_from_disk is True


def test_short_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    root = "/home/tester/repo-root"
    assert colors.short_folder(f"{root}/infra/network-apis", root) == "infra/network-apis"
    assert colors.short_folder("/home/tester", root) == "~"
    assert colors.short_folder("/home/tester/scratch", root) == "~/scratch"
    assert colors.short_folder("/etc/hosts", root) == "/etc/hosts"
    assert colors.short_folder("", root) == "?"


def test_folder_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    base = "/home/tester/repo-root"
    # Under the repo tree: first segment is the category, the tail is the leaf.
    assert colors.folder_split(f"{base}/sdsc/runai-cscs", base) == ("sdsc", "runai-cscs")
    assert colors.folder_split(f"{base}/sdsc/runai-cscs/tickets", base) == (
        "sdsc",
        "runai-cscs/tickets",
    )
    # Outside the tree: one "others" category, leaf is the full home-relative path.
    assert colors.folder_split("/home/tester", base) == ("others", "~")
    assert colors.folder_split("/home/tester/scratch", base) == ("others", "~/scratch")
    assert colors.folder_split("/home/tester/a/b/c", base) == ("others", "~/a/b/c")
    assert colors.folder_split("/tmp/elsewhere", base) == ("others", "/tmp/elsewhere")
    # No configured tree ⇒ everything is "others" (leaf collapses $HOME to ~).
    assert colors.folder_split(f"{base}/sdsc/runai-cscs", "") == (
        "others",
        "~/repo-root/sdsc/runai-cscs",
    )


def test_category_color_palette_is_deterministic_and_overridable() -> None:
    # "others" (and the empty category) never get a colour.
    assert colors.category_color("others") is None
    assert colors.category_color("") is None
    # An explicit config override wins.
    cfg = config.Config(category_colors={"sdsc": "#123456"})
    assert colors.category_color("sdsc", cfg) == "#123456"
    # Unknown categories fall back to a stable, palette-bound colour (hash of the name),
    # identical across calls and drawn from the published palette.
    first = colors.category_color("brand-new-cat")
    assert first == colors.category_color("brand-new-cat")
    assert first in colors._CATEGORY_PALETTE


def test_folder_style_falls_back_to_category_palette() -> None:
    # With no tab-colour cache hit, a real category still gets a stable colour (not grey);
    # only an "others" folder falls through to grey70.
    base = "/home/tester/repo-root"
    styled = colors.folder_style(f"{base}/sdsc/repo-x", None, base)
    assert styled == colors.category_color("sdsc")
    assert colors.folder_style("/tmp/elsewhere", None, base) == "grey70"


def test_with_tags_preserves_text() -> None:
    from command_center.views.tui import _with_tags  # pylint: disable=import-outside-toplevel

    rendered = _with_tags("ping @susi re @waiting", "white")
    assert rendered.plain == "ping @susi re @waiting"
    # the @-tags carry a non-default style (a span exists for each)
    assert len(rendered.spans) >= 2


def test_parse_claude_accounts_is_pure_and_shares_semantics_with_claude_config_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`parse_claude_accounts` is the file-read-free half of `claude_config_dirs`.

    The TUI calls it on every 5 s render tick from an already-loaded Config, so it must
    not touch disk and must agree with `claude_config_dirs` on the same entries.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    entries = [f"private={tmp_path}/p", f"work={tmp_path}/w"]
    parsed = config.parse_claude_accounts(entries)
    assert set(parsed) == {"private", "work"}

    # Malformed entries are skipped, not fatal; an all-malformed list falls back.
    assert set(config.parse_claude_accounts(["nosep", "BAD=/x", "=/y", "ok="])) == {"private"}
    assert config.parse_claude_accounts([]) == {"private": config.claude_home()}


def test_parse_claude_account_emails_is_pure_and_tolerant() -> None:
    """`parse_claude_account_emails` — the identity hard-link behind `resolve_card_label`.

    Mirrors `parse_claude_accounts`'s tolerance for malformed entries, but with NO
    non-empty fallback: an absent/all-malformed list means "no hard link", not "assume
    private" (an unconfigured card must resolve as a plain passthrough, never a guess).
    """
    entries = ["work=user@example.org", "private=user@example.com"]
    assert config.parse_claude_account_emails(entries) == {
        "work": "user@example.org",
        "private": "user@example.com",
    }
    # Malformed entries (no "=", bad label, no "@" in the value) are skipped, not fatal.
    assert (
        config.parse_claude_account_emails(
            ["nosep", "BAD=x@y.com", "work=notanemail", "=x@y.com", "ok="]
        )
        == {}
    )
    # Unlike claude_accounts, empty stays empty — no default-private fallback.
    assert config.parse_claude_account_emails([]) == {}


def test_parse_subscription_ends_is_pure_and_tolerant() -> None:
    """`parse_subscription_ends` — the per-card renewal date behind the ``-> D.M`` title.

    Same tolerance rule as its neighbours: one bad entry is skipped, never fatal, so a
    typo in config.toml cannot blank the dates on the other cards. A well-SHAPED but
    impossible day is rejected too — a card must show a real date or none at all.
    """
    entries = ["claude_private=auto", "codex_private=2026-09-30"]
    assert config.parse_subscription_ends(entries) == {
        "claude_private": "auto",
        "codex_private": "2026-09-30",
    }
    assert (
        config.parse_subscription_ends(
            [
                "nosep",  # no "="
                "claude_privat=2026-09-30",  # card outside SUBSCRIPTION_CARDS
                "codex=30.9.2026",  # not ISO-8601
                "codex=2026-02-30",  # well-shaped, but February has no 30th
                "codex_private=",  # blank value
                "claude_work=AUTO",  # `auto` is spelled lowercase
            ]
        )
        == {}
    )
    assert config.parse_subscription_ends([]) == {}
    # Every card the parser accepts is a card the TUI actually draws.
    assert set(config.SUBSCRIPTION_CARD_ACCOUNTS) <= set(config.SUBSCRIPTION_CARDS)
