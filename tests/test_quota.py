"""Tests for the cache-first quota oracle (:mod:`command_center.quota`).

The behaviours worth guarding here are the ones whose failure is SILENT and expensive:

* an ``unknown`` verdict must never be mistaken for ``blocked`` — a measurement failure
  that removes a working rung is strictly worse than attempting a doubtful one;
* windows must not be collapsed — 100 % on the 5-hour window and 49 % on the weekly one
  is BLOCKED, not "49 % healthy";
* a Fable-exhausted account must stay usable for an Opus request;
* concurrent writers to the cooldown store must not lose one another's updates, and an
  out-of-order observation must not resurrect a block a later success cleared.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from command_center import quota, usage


@pytest.fixture(autouse=True)
def _pin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own app home, so no real snapshot is read or written."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    (tmp_path / "claude-home" / "command-center").mkdir(parents=True, exist_ok=True)


NOW = 1_800_000_000


def _win(pct: float, resets_in: int = 7200) -> usage.Window:
    return usage.Window(used_percentage=pct, resets_at=NOW + resets_in)


# ── window verdicts ──────────────────────────────────────────────────────────────


def test_exhausted_window_blocks_even_when_another_window_is_healthy() -> None:
    """A 100 % 5-hour window blocks despite a 49 % weekly one — no collapsing."""
    windows = [
        quota.WindowState("five_hour", 100.0, NOW + 600),
        quota.WindowState("seven_day", 49.0, NOW + 86400),
    ]
    state, _reason, blocked_by, resets_at, _risky = quota._verdict_from_windows(windows)
    assert state == quota.BLOCKED
    assert blocked_by == "five_hour"
    assert resets_at == NOW + 600  # the BLOCKING window's reset, not the other one


def test_stale_hundred_percent_is_unknown_not_blocked() -> None:
    """A stale reading of 100 % proves nothing — the window may since have reset."""
    windows = [quota.WindowState("seven_day", 100.0, NOW + 86400, stale=True)]
    state, _reason, _blocked_by, _resets_at, _risky = quota._verdict_from_windows(windows)
    assert state == quota.UNKNOWN


def test_no_windows_is_unknown() -> None:
    state, _reason, _by, _at, _risky = quota._verdict_from_windows([])
    assert state == quota.UNKNOWN


def test_ninety_percent_is_risky_but_available() -> None:
    """routing's 90 % is a RISK threshold; treating it as exhaustion would bin 10 % of a plan."""
    windows = [quota.WindowState("seven_day", 92.0, NOW + 86400)]
    state, _reason, _by, _at, risky = quota._verdict_from_windows(windows)
    assert (state, risky) == (quota.AVAILABLE, True)


def test_window_past_its_reset_is_stale() -> None:
    state = quota._window_state("seven_day", _win(100.0, resets_in=-10), NOW, NOW, 86400)
    assert state is not None and state.stale and not state.exhausted


# ── model scoping ────────────────────────────────────────────────────────────────


def test_fable_window_ignored_for_non_fable_models() -> None:
    """The concrete bug: fable_week at 100 % must not block an Opus request."""
    windows = {
        "seven_day": quota.WindowState("seven_day", 83.0, NOW + 86400),
        "fable_week": quota.WindowState("fable_week", 100.0, NOW + 86400),
    }
    opus = quota._windows_for_model(windows, "claude-opus-4-6")
    assert [w.name for w in opus] == ["seven_day"]
    fable = quota._windows_for_model(windows, "claude-fable-5")
    assert {w.name for w in fable} == {"seven_day", "fable_week"}


def test_claude_account_blocked_for_fable_but_available_for_opus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = usage.Usage(
        captured_at=NOW,
        five_hour=_win(4.0),
        seven_day=_win(83.0, 86400),
        fable_week=_win(100.0, 86400),
    )
    monkeypatch.setattr(usage, "read_usage", lambda _a: snap)
    assert quota._claude_quota("private", "claude-fable-5", NOW, {}).state == quota.BLOCKED
    assert quota._claude_quota("private", "claude-opus-4-6", NOW, {}).state == quota.AVAILABLE


# ── copilot precedence ───────────────────────────────────────────────────────────


def _copilot(**kw: object) -> usage.CopilotUsage:
    base: dict = dict(
        captured_at=NOW,
        year=2026,
        month=8,
        sku="AI Credits",
        unit="AI credits",
        quantity=1500.0,
        gross=15.0,
        net=0.0,
        credit_quota=1500,
        credits_used=1500.0,
        quota_source="api",
        premium_reset_at=NOW + 86400,
    )
    base.update(kw)
    return usage.CopilotUsage(**base)


def test_guessed_denominator_is_unknown_never_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured guess (observed to be 2x the real entitlement) cannot prove exhaustion."""
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: _copilot(quota_source="config"))
    assert quota._copilot_quota(NOW, {}).state == quota.UNKNOWN


def test_fresh_api_meter_at_full_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: _copilot())
    result = quota._copilot_quota(NOW, {})
    assert (result.state, result.blocked_by) == (quota.BLOCKED, "credits")


def test_fresh_api_meter_with_headroom_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: _copilot(credits_used=300.0))
    assert quota._copilot_quota(NOW, {}).state == quota.AVAILABLE


def test_stale_meter_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: _copilot(captured_at=NOW - 90000))
    assert quota._copilot_quota(NOW, {}).state == quota.UNKNOWN


def test_observed_429_outranks_a_healthy_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seat's own rejection beats a billing snapshot that lags by up to a day."""
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: _copilot(credits_used=1.0))
    cooldowns = {"copilot": {"blocked_until": NOW + 600, "observed_at": NOW, "reason": "429"}}
    result = quota._copilot_quota(NOW, cooldowns)
    assert (result.state, result.source) == (quota.BLOCKED, "cooldown")


# ── gemini is a capability state ─────────────────────────────────────────────────


def test_gemini_is_disabled_not_blocked() -> None:
    """A retired tier has no reset to wait for, so ``blocked`` would be a lie."""
    result = quota._gemini_quota({})
    assert result.state == quota.DISABLED
    assert result.resets_at == 0


# ── cooldown store: expiry, ordering, concurrency ────────────────────────────────


def test_block_expires_on_read() -> None:
    quota.record_block("copilot", blocked_until=NOW + 100, observed_at=NOW, reason="429")
    assert "copilot" in quota.read_cooldowns(NOW)
    assert "copilot" not in quota.read_cooldowns(NOW + 101)


def test_older_observation_cannot_overwrite_newer(tmp_path: Path) -> None:
    """A slow process's stale 429 must not clobber a later success."""
    quota.record_block("copilot", blocked_until=NOW + 999, observed_at=NOW + 50, reason="new")
    quota.record_block("copilot", blocked_until=NOW + 10, observed_at=NOW, reason="old")
    assert quota.read_cooldowns(NOW)["copilot"]["reason"] == "new"


def test_stale_clear_cannot_wipe_a_newer_block() -> None:
    quota.record_block("copilot", blocked_until=NOW + 999, observed_at=NOW + 50)
    assert quota.clear_block("copilot", observed_at=NOW) is False
    assert "copilot" in quota.read_cooldowns(NOW)


def test_clear_removes_the_block() -> None:
    quota.record_block("codex", blocked_until=NOW + 999, observed_at=NOW)
    assert quota.clear_block("codex", observed_at=NOW + 60) is True
    assert quota.read_cooldowns(NOW) == {}


def test_concurrent_marks_do_not_lose_updates() -> None:
    """Read-merge-write under one lock: two providers marked in parallel both survive.

    Atomic replacement alone would let the second writer's read-modify-write drop the
    first writer's entry, which is exactly how a recorded block silently vanishes.
    """
    import threading

    def mark(name: str) -> None:
        quota.record_block(name, blocked_until=NOW + 600, observed_at=NOW, reason=name)

    names = [f"p{i}" for i in range(12)]
    threads = [threading.Thread(target=mark, args=(n,)) for n in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(quota.read_cooldowns(NOW)) == set(names)


def test_corrupt_store_degrades_to_empty() -> None:
    quota._cooldowns_path().parent.mkdir(parents=True, exist_ok=True)
    quota._cooldowns_path().write_text("{not json", encoding="utf-8")
    assert quota.read_cooldowns(NOW) == {}


def test_temp_pattern_covers_the_cooldown_store() -> None:
    """A killed writer's orphan must be reclaimable by the existing sweeper."""
    assert "cooldowns.json.*.tmp" in usage._TEMP_PATTERNS


# ── snapshot contract ────────────────────────────────────────────────────────────


def test_snapshot_is_versioned_and_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "read_usage", lambda _a: None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n=None: None)
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: None)
    snap = quota.snapshot(model="claude-opus-4-6", now=NOW)
    assert snap["version"] == quota.SCHEMA_VERSION
    json.dumps(snap)  # must round-trip for the `-j` contract
    # No cross-provider "best": ranking providers is a cost decision, not a quota fact.
    assert "best" not in snap
    assert {p["id"] for p in snap["providers"]} >= {"copilot", "codex", "gemini"}


def test_snapshot_ranks_usable_claude_accounts_by_urgency(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account whose allowance would otherwise evaporate soonest is spent first."""
    snaps = {
        # 17% left over 2h  → 8.5 %/h  (resets soonest, must win)
        "private": usage.Usage(NOW, _win(1.0), _win(83.0, 7200)),
        # 79% left over 5d  → 0.66 %/h
        "work": usage.Usage(NOW, _win(1.0), _win(21.0, 5 * 86400)),
    }
    monkeypatch.setattr(usage, "read_usage", lambda a: snaps.get(a))
    monkeypatch.setattr(
        quota.config, "claude_config_dirs", lambda: {"private": Path("/x"), "work": Path("/y")}
    )
    snap = quota.snapshot(model="claude-opus-4-6", now=NOW)
    assert snap["best_claude_account"] == "claude:private"
    order = [p["id"] for p in snap["providers"] if p["kind"] == "claude"]
    assert order == ["claude:private", "claude:work"]


def test_blocked_claude_account_sorts_after_usable_one(monkeypatch: pytest.MonkeyPatch) -> None:
    snaps = {
        "private": usage.Usage(NOW, _win(1.0), _win(100.0, 7200)),  # exhausted
        "work": usage.Usage(NOW, _win(1.0), _win(21.0, 5 * 86400)),
    }
    monkeypatch.setattr(usage, "read_usage", lambda a: snaps.get(a))
    monkeypatch.setattr(
        quota.config, "claude_config_dirs", lambda: {"private": Path("/x"), "work": Path("/y")}
    )
    snap = quota.snapshot(model="claude-opus-4-6", now=NOW)
    assert snap["best_claude_account"] == "claude:work"


def test_snapshot_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the oracle: consulting it must be free."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("snapshot() must not fetch")

    monkeypatch.setattr(usage, "fetch_claude_usage", _boom)
    monkeypatch.setattr(usage, "fetch_copilot_usage", _boom)
    quota.snapshot(model="claude-opus-4-6", now=NOW)


def test_snapshot_is_fast() -> None:
    """Cache-only means a caller can consult it on every invocation without thinking."""
    start = time.monotonic()
    quota.snapshot(model="claude-opus-4-6", now=NOW)
    assert time.monotonic() - start < 1.0
