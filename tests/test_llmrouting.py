"""Unit tests for the `ccc llm-routing` overview.

The table's whole job is to answer "what is spending my Codex seat?" correctly, so the
tests pin the resolution rules (auto-expansion, the custom-router escape hatch, the
enabled flags) rather than the cosmetics of the box drawing.
"""

from __future__ import annotations

import pytest

from command_center import llmrouting
from command_center.config import Config


def _cfg(**overrides: object) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _row(cfg: Config, purpose: str) -> llmrouting.Row:
    return next(r for r in llmrouting.rows(cfg) if r.purpose == purpose)


def test_codex_backend_is_reported_as_a_codex_spender() -> None:
    cfg = _cfg(short_aim=True, short_aim_backend="codex", llm_custom_command="")
    row = _row(cfg, "short-aim")
    assert row.cost == llmrouting.CODEX_COST
    assert llmrouting.codex_spenders(llmrouting.rows(cfg)) == [row]


def test_claude_backend_takes_short_aim_off_the_codex_seat() -> None:
    cfg = _cfg(short_aim=True, short_aim_backend="claude", llm_custom_command="")
    assert llmrouting.codex_spenders(llmrouting.rows(cfg)) == []
    assert "Claude subscription" in _row(cfg, "short-aim").cost


def test_disabled_codex_row_is_not_counted_as_a_spender() -> None:
    """`short_aim = false` means the codex backend never runs — it must not be flagged."""
    cfg = _cfg(short_aim=False, short_aim_backend="codex")
    assert llmrouting.codex_spenders(llmrouting.rows(cfg)) == []
    assert _row(cfg, "short-aim").enabled is False


def test_auto_backend_expands_the_way_short_aim_resolves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(short_aim=True, short_aim_backend="auto", llm_custom_command="")
    monkeypatch.setattr(llmrouting.shutil, "which", lambda _name: "/usr/bin/codex")
    assert _row(cfg, "short-aim").cost == llmrouting.CODEX_COST
    monkeypatch.setattr(llmrouting.shutil, "which", lambda _name: None)
    assert _row(cfg, "short-aim").cost != llmrouting.CODEX_COST


def test_custom_router_replaces_the_pinned_claude_account() -> None:
    cfg = _cfg(llm_custom_command="ai.py prompt -i -R judge", llm_account="work")
    row = _row(cfg, "aim-met")
    assert "llm_custom_command" in row.provider
    assert row.cost == "whatever `ai routing` says"
    assert "work" not in row.cost


def test_pinned_claude_names_the_account_it_bills() -> None:
    cfg = _cfg(llm_custom_command="", llm_account="work", llm_model="claude-haiku-4-5")
    row = _row(cfg, "aim-met")
    assert row.cost == "Claude subscription (work)"
    assert "claude-haiku-4-5" in row.provider


def test_unrecognised_router_is_not_claimed_to_be_ai_py() -> None:
    cfg = _cfg(llm_custom_command="/opt/mine/router --go")
    assert _row(cfg, "aim-met").cost == "external router"


def test_score_ladder_lists_every_rung_in_order() -> None:
    cfg = _cfg(score_backends=["copilot", "codex"], copilot_model="gpt-5.4")
    row = _row(cfg, "aim-score")
    assert row.provider.index("opencode") < row.provider.index("codex exec")
    assert llmrouting.CODEX_COST in row.cost


def test_empty_score_ladder_does_not_crash() -> None:
    assert _row(_cfg(score_backends=[]), "aim-score").provider == "(no rungs configured)"


def test_render_is_help_safe_when_config_loading_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ccc -h` embeds this — a broken config must degrade, never raise."""

    def boom(_cfg: Config | None = None) -> list[llmrouting.Row]:
        raise ValueError("bad toml")

    monkeypatch.setattr(llmrouting, "rows", boom)
    out = llmrouting.render()
    assert "unavailable" in out and "ValueError" in out


def test_render_flags_the_codex_seat_only_when_something_bills_it() -> None:
    hot = llmrouting.render(_cfg(short_aim=True, short_aim_backend="codex"))
    cold = llmrouting.render(_cfg(short_aim=True, short_aim_backend="claude"))
    assert "Spending the Codex seat" in hot
    assert "free for /codex-debate" in cold


def test_every_action_names_a_config_key_to_change_it() -> None:
    assert all(r.switch.strip() for r in llmrouting.rows(_cfg()))
