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

import argparse
import json
import re
import sys
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
        oauth_fetched_at=NOW,  # Fable evidence is only as fresh as the OAuth fetch
    )
    monkeypatch.setattr(usage, "read_usage", lambda _a: snap)
    assert quota._claude_quota("private", "claude-fable-5", NOW, {}).state == quota.BLOCKED
    assert quota._claude_quota("private", "claude-opus-4-6", NOW, {}).state == quota.AVAILABLE


def test_stale_fable_evidence_governs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh ``captured_at`` + stale ``oauth_fetched_at`` ⇒ the Fable window is stale.

    Statusline writes refresh ``captured_at`` while CARRYING the old Fable value, so a
    days-old 100 % Fable figure used to read as live and block Fable routing. The
    evidence for ``fable_week`` is the OAuth fetch time, with the card's 1 h threshold.
    """
    snap = usage.Usage(
        captured_at=NOW,  # a statusline write seconds ago
        five_hour=_win(4.0),
        seven_day=_win(83.0, 86400),
        fable_week=_win(100.0, 86400),
        oauth_fetched_at=NOW - 7200,  # ...but no OAuth fetch for 2 h
    )
    monkeypatch.setattr(usage, "read_usage", lambda _a: snap)
    got = quota._claude_quota("private", "claude-fable-5", NOW, {})
    assert got.state == quota.AVAILABLE  # stale 100 % must not block
    assert got.windows["fable_week"].stale
    assert got.windows["fable_week"].evidence_at == NOW - 7200


def test_never_fetched_fable_is_stale_not_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """``oauth_fetched_at=0`` (no OAuth fetch ever) can never let Fable block."""
    snap = usage.Usage(
        captured_at=NOW,
        five_hour=_win(4.0),
        seven_day=_win(83.0, 86400),
        fable_week=_win(100.0, 86400),
    )
    monkeypatch.setattr(usage, "read_usage", lambda _a: snap)
    assert quota._claude_quota("private", "claude-fable-5", NOW, {}).state == quota.AVAILABLE


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
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n=None, _h=None: None)
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


# ── administrative holds (kind="hold") ──────────────────────────────────────────


def test_observed_rejection_cannot_overwrite_an_unexpired_hold() -> None:
    """A provider 429 with a shorter retry must not quietly shorten a policy hold."""
    quota.record_block(
        "codex", blocked_until=NOW + 5 * 86400, observed_at=NOW, kind=quota.KIND_HOLD
    )
    quota.record_block("codex", blocked_until=NOW + 60, observed_at=NOW + 10, reason="429")
    entry = quota.read_cooldowns(NOW + 20)["codex"]
    assert entry["kind"] == quota.KIND_HOLD
    assert entry["blocked_until"] == NOW + 5 * 86400


def test_success_clear_skips_holds_but_explicit_clear_removes_them() -> None:
    quota.record_block("codex", blocked_until=NOW + 86400, observed_at=NOW, kind=quota.KIND_HOLD)
    # The success path (ai.py after a rung served) must never lift a reservation.
    assert not quota.clear_block("codex", observed_at=NOW + 10, observed_only=True)
    assert "codex" in quota.read_cooldowns(NOW + 20)
    # A human's explicit `ccc quota -c` removes anything.
    assert quota.clear_block("codex", observed_at=NOW + 30)
    assert "codex" not in quota.read_cooldowns(NOW + 40)


def test_hold_deadline_is_exclusive() -> None:
    """`-U 2026-09-07T00:00` means blocked while now < deadline — free AT the instant."""
    deadline = NOW + 1000
    quota.record_block("codex", blocked_until=deadline, observed_at=NOW, kind=quota.KIND_HOLD)
    assert "codex" in quota.read_cooldowns(deadline - 1)
    assert "codex" not in quota.read_cooldowns(deadline)
    assert "codex" not in quota.read_cooldowns(deadline + 1)


def test_hold_row_reports_scope_and_source() -> None:
    quota.record_block(
        "codex",
        blocked_until=NOW + 86400,
        observed_at=NOW,
        kind=quota.KIND_HOLD,
        reason="team seat reserved",
    )
    row = quota._cooldown_quota("codex", "codex", quota.read_cooldowns(NOW + 1)["codex"])
    assert (row.state, row.source, row.block_scope) == (quota.BLOCKED, "hold", "hold")


# ── codex seats: canonical identity + selection ──────────────────────────────────


def test_canonical_codex_homes_ignore_ambient_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A process running WITH the private home in its env must not relabel the seats."""
    private = tmp_path / "codex-private"
    monkeypatch.setenv("CODEX_HOME", str(private))  # ambient override must not leak in
    monkeypatch.setattr(quota.config, "codex_home_private", lambda: private)
    homes = quota._canonical_codex_homes()
    assert homes["default"] == Path.home() / ".codex"
    assert homes["private"] == private


def test_private_home_equal_to_default_is_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quota.config, "codex_home_private", lambda: Path.home() / ".codex")
    assert list(quota._canonical_codex_homes()) == ["default"]


def test_canonical_codex_homes_include_extras_and_dedupe_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`codex_homes_extra` logins get their own labels — unless they duplicate a seat.

    One billable identity may hold exactly one provider id: a second label for the same
    path would double-count the seat and split its holds across two rows.
    """
    private = tmp_path / "codex-private"
    extra = tmp_path / "codex-de"
    monkeypatch.setattr(quota.config, "codex_home_private", lambda: private)
    monkeypatch.setattr(
        quota.config,
        "codex_homes_extra",
        lambda: {"de": extra, "dup": private, "team": Path.home() / ".codex"},
    )
    homes = quota._canonical_codex_homes()
    assert list(homes) == ["default", "private", "de"]  # `dup`/`team` are the same seats
    assert homes["de"] == extra


def test_codex_quota_rows_carry_one_id_per_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Row ids: `codex`, `codex:private`, then one `codex:<label>` per extra login."""
    monkeypatch.setattr(quota.config, "codex_home_private", lambda: tmp_path / "codex-private")
    monkeypatch.setattr(quota.config, "codex_homes_extra", lambda: {"de": tmp_path / "codex-de"})
    monkeypatch.setattr(usage, "codex_account_email", lambda _h: "")
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n=None, _h=None: None)
    rows = quota._codex_quotas(NOW, {})
    assert [row.id for row in rows] == ["codex", "codex:private", "codex:de"]
    assert [row.account for row in rows] == ["default", "private", "de"]


def test_pin_at_an_extra_home_resolves_to_its_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pin whose path is a `codex_homes_extra` home names that seat, not ``""``.

    Before `codex_homes_extra` existed such a pin fell outside the known homes, mapped
    to ``""`` and was silently ignored by the selector.
    """
    from command_center import codex_in_claude

    extra = tmp_path / "codex-de"
    monkeypatch.setattr(quota.config, "codex_home_private", lambda: None)
    monkeypatch.setattr(quota.config, "codex_homes_extra", lambda: {"de": extra})
    monkeypatch.setattr(codex_in_claude, "pinned_codex_home", lambda *_a, **_k: extra)
    homes = quota._canonical_codex_homes()
    assert quota._codex_pin_label(homes) == "de"
    # A pin at a path ccc knows nothing about still reports "" (absent, not invented).
    monkeypatch.setattr(codex_in_claude, "pinned_codex_home", lambda *_a, **_k: tmp_path / "nope")
    assert quota._codex_pin_label(homes) == ""


def test_selector_honours_an_extra_seat_pin_and_falls_through_when_blocked() -> None:
    """An eligible `codex:<label>` pin wins; a blocked one is excluded before the pin."""
    rows = [
        _codex_row("codex", "default", quota.AVAILABLE),
        _codex_row("codex:private", "private", quota.AVAILABLE),
        _codex_row("codex:de", "de", quota.AVAILABLE),
    ]
    assert quota.select_codex_account(rows, "de") == "codex:de"
    rows[2] = _codex_row("codex:de", "de", quota.BLOCKED)
    assert quota.select_codex_account(rows, "de") == "codex"


def _codex_row(pid: str, label: str, state: str) -> quota.ProviderQuota:
    return quota.ProviderQuota(id=pid, kind="codex", state=state, account=label)


def test_selector_prefers_eligible_pin() -> None:
    rows = [
        _codex_row("codex", "default", quota.AVAILABLE),
        _codex_row("codex:private", "private", quota.AVAILABLE),
    ]
    assert quota.select_codex_account(rows, "private") == "codex:private"


def test_selector_excludes_held_seats_before_the_pin() -> None:
    """A pin on a held/blocked seat must not override 'do not use this seat'."""
    rows = [
        _codex_row("codex", "default", quota.BLOCKED),
        _codex_row("codex:private", "private", quota.AVAILABLE),
    ]
    assert quota.select_codex_account(rows, "default") == "codex:private"


def test_selector_unknown_is_eligible_and_team_first() -> None:
    """UNKNOWN stays usable (fail-open), and the team seat leads without a pin."""
    rows = [
        _codex_row("codex", "default", quota.UNKNOWN),
        _codex_row("codex:private", "private", quota.AVAILABLE),
    ]
    assert quota.select_codex_account(rows, "") == "codex"


def test_selector_returns_empty_when_nothing_is_eligible() -> None:
    rows = [
        _codex_row("codex", "default", quota.BLOCKED),
        _codex_row("codex:private", "private", quota.BLOCKED),
    ]
    assert quota.select_codex_account(rows, "private") == ""


def test_snapshot_has_one_row_per_codex_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(quota.config, "codex_home_private", lambda: tmp_path / "codex-private")
    monkeypatch.setattr(usage, "read_usage", lambda _a: None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n=None, _h=None: None)
    monkeypatch.setattr(usage, "read_copilot_usage", lambda: None)
    snap = quota.snapshot(model="claude-opus-4-6", now=NOW)
    ids = [p["id"] for p in snap["providers"] if p["kind"] == "codex"]
    assert ids == ["codex", "codex:private"]
    assert snap["version"] == quota.SCHEMA_VERSION
    assert "best_codex_account" in snap


# ── a newer healthy reading supersedes a recorded refusal ────────────────────────
# The 2026-09-14 bug: `record_seat_refusal` wrote `codex:private = {scope: quota,
# observed_at: 09-11, blocked_until: 09-15}` and `_codex_seat_quota` consulted the
# cooldown store FIRST, so the entry stood until its own deadline while the seat's own
# live reading said 0 % / 0 % with an empty blocked_reason. Three days of a healthy paid
# seat were reported as "blocked (unblocks in 18h)".


def _quota_entry(observed_at: int, **over: object) -> dict:
    """A cooldown entry shaped exactly as ``record_seat_refusal`` writes a quota refusal."""
    entry = {
        "blocked_until": NOW + 18 * 3600,
        "observed_at": observed_at,
        "reason": "codex exec refused: quota — usage limit reached",
        "status": 0,
        "scope": "quota",
        "source": "codex-exec",
        "kind": quota.KIND_OBSERVED,
    }
    entry.update(over)
    return entry


def _healthy(captured_at: int, five: float = 0.0, seven: float = 0.0, **over: object):
    """A live Codex snapshot with two fresh, non-exhausted windows."""
    return usage.Usage(
        captured_at=captured_at,
        five_hour=usage.Window(used_percentage=five, resets_at=NOW + 3600),
        seven_day=usage.Window(used_percentage=seven, resets_at=NOW + 5 * 86400),
        live=True,
        **over,  # type: ignore[arg-type]
    )


def _superseded(entry: dict, snap: usage.Usage | None, now: int = NOW) -> bool:
    """The predicate under test, with the windows built exactly as the resolver does."""
    windows = quota._codex_windows(snap, now) if snap is not None else {}  # noqa: SLF001
    return quota.observed_block_superseded(entry, snap, windows.values(), now)


def test_newer_healthy_reading_supersedes_a_quota_refusal() -> None:
    assert _superseded(_quota_entry(NOW - 3 * 86400), _healthy(NOW - 600)) is True


def test_a_reading_older_than_the_refusal_supersedes_nothing() -> None:
    """Order of evidence, not its existence: the refusal came AFTER this measurement."""
    assert _superseded(_quota_entry(NOW - 600), _healthy(NOW - 3 * 86400)) is False
    # Equal timestamps prove nothing either — strictly newer is the rule.
    assert _superseded(_quota_entry(NOW - 600), _healthy(NOW - 600)) is False


def test_a_newer_reading_that_is_itself_blocked_or_full_does_not_supersede() -> None:
    old = _quota_entry(NOW - 3 * 86400)
    # `read_codex_usage` staples a rollout refusal newer than the reading, so a stapled
    # snapshot is the "refusal → success → refusal" case: still blocked.
    stapled = _healthy(NOW - 600, blocked_reason="included usage limit reached", blocked_at=NOW)
    assert _superseded(old, stapled) is False
    assert _superseded(old, _healthy(NOW - 600, seven=100.0)) is False  # a full window
    assert _superseded(old, _healthy(NOW - 600, malformed=True)) is False


def test_stale_or_absent_windows_never_supersede() -> None:
    """UNKNOWN is a measurement failure, not evidence — it may not lift a block."""
    old = _quota_entry(NOW - 3 * 86400)
    stale = usage.Usage(
        captured_at=NOW - 600,
        five_hour=usage.Window(used_percentage=0.0, resets_at=NOW - 60),  # reset passed
        seven_day=usage.Window(used_percentage=0.0, resets_at=NOW - 60),
    )
    assert _superseded(old, stale) is False
    windowless = usage.Usage(captured_at=NOW - 600, five_hour=None, seven_day=None)
    assert _superseded(old, windowless) is False
    assert _superseded(old, None) is False


def test_holds_and_non_quota_scopes_are_never_superseded() -> None:
    """A hold is policy, and auth/entitlement say what no usage reading can refute."""
    healthy = _healthy(NOW - 600)
    hold = _quota_entry(NOW - 3 * 86400, kind=quota.KIND_HOLD, scope="hold")
    assert _superseded(hold, healthy) is False
    for scope in ("auth", "entitlement", "", "quota-ish"):
        assert _superseded(_quota_entry(NOW - 3 * 86400, scope=scope), healthy) is (
            scope == "quota"
        )


def test_superseded_row_is_available_names_the_refusal_and_keeps_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole fix, end to end: the row goes AVAILABLE and the store is untouched."""
    home = tmp_path / "seat"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("{}", encoding="utf-8")
    quota.record_block(
        "codex:private",
        blocked_until=NOW + 18 * 3600,
        observed_at=NOW - 3 * 86400,
        reason="codex exec refused: quota — usage limit reached",
        scope="quota",
        source="codex-exec",
    )
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: _healthy(NOW - 7200))
    cooldowns = quota.read_cooldowns(NOW)
    row = quota._codex_seat_quota("codex:private", "private", home, NOW, cooldowns)  # noqa: SLF001
    assert row.state == quota.AVAILABLE
    assert row.note == "refusal 3d old superseded by a reading 2h old"
    # Readers never write: the entry stays until its own deadline, for every other
    # process AND for `ccc quota -c`.
    assert "codex:private" in quota.read_cooldowns(NOW)


def test_an_unmeasurable_seat_keeps_its_recorded_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No auth.json and no snapshot are missing readings — they supersede nothing."""
    entry = {"codex:private": _quota_entry(NOW - 3 * 86400)}
    bare = tmp_path / "bare"
    bare.mkdir()
    row = quota._codex_seat_quota("codex:private", "private", bare, NOW, entry)  # noqa: SLF001
    assert (row.state, row.blocked_by) == (quota.BLOCKED, "observed-rejection")
    home = tmp_path / "seat"
    home.mkdir()
    (home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: None)
    row = quota._codex_seat_quota("codex:private", "private", home, NOW, entry)  # noqa: SLF001
    assert (row.state, row.blocked_by) == (quota.BLOCKED, "observed-rejection")


def _write_rollout(home: Path, name: str, event: dict) -> None:
    """One rollout file carrying one ``rate_limits`` event (the real on-disk shape)."""
    day = home / "sessions" / "2026" / "09" / "11"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-2026-09-11T09-00-00-{name}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {}}) + "\n" + json.dumps(event) + "\n",
        encoding="utf-8",
    )
    usage._codex_cache.clear()  # noqa: SLF001


def _token_count(captured_at: int, reached: str | None = None) -> dict:
    """A ``token_count`` event with two healthy windows (or a refusal, with *reached*)."""
    return {
        "type": "event_msg",
        "timestamp": captured_at,
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": NOW + 3600},
                "secondary": {
                    "used_percent": 20.0,
                    "window_minutes": 10080,
                    "resets_at": NOW + 5 * 86400,
                },
                "rate_limit_reached_type": reached,
            },
        },
    }


def test_rollout_evidence_supersedes_only_when_it_is_newer_and_healthy(tmp_path: Path) -> None:
    """A ``token_count`` block exists only because Codex SERVED a turn on that seat.

    So a healthy rollout event newer than the refusal is a success after it — but a
    refusal event newer still (refusal → success → refusal) blocks again, because
    ``read_codex_usage`` staples that one on.
    """
    home = tmp_path / "seat"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("{}", encoding="utf-8")
    entry = {"codex:private": _quota_entry(NOW - 3600)}

    _write_rollout(home, "served", _token_count(NOW - 1800))  # T+1: a served turn
    row = quota._codex_seat_quota("codex:private", "private", home, NOW, entry)  # noqa: SLF001
    assert row.state == quota.AVAILABLE
    assert "superseded" in row.note

    _write_rollout(home, "served", _token_count(NOW - 7200))  # T−1: older than the refusal
    row = quota._codex_seat_quota("codex:private", "private", home, NOW, entry)  # noqa: SLF001
    assert (row.state, row.blocked_by) == (quota.BLOCKED, "observed-rejection")

    _write_rollout(home, "served", _token_count(NOW - 1800))
    _write_rollout(home, "refused", _token_count(NOW - 900, "usage_limit_reached"))  # T+2
    row = quota._codex_seat_quota("codex:private", "private", home, NOW, entry)  # noqa: SLF001
    assert row.state == quota.BLOCKED


# ── human names ──────────────────────────────────────────────────────────────────
#
# The ids are the wire format (the cooldown store's keys, `ai.py`'s `_ORACLE_IDS`) and
# must not move. What a person reads is a separate vocabulary — the one they type at a
# shell — and the two are joined by `display_id`/`canonical_id`. The round-trip is the
# invariant: if it ever stops holding, `ccc quota -c <name-from-the-report>` clears the
# wrong provider, or nothing at all.


@pytest.mark.parametrize(
    ("pid", "shown"),
    [
        ("copilot", "copilot"),
        ("gemini", "gemini"),
        ("codex", "codex-work"),
        ("codex:private", "codex-priv"),
        ("codex:de", "codex-de"),
        ("claude:work", "claude-work"),
        ("claude:private", "claude-priv"),
    ],
)
def test_display_name_round_trips_to_its_wire_id(pid: str, shown: str) -> None:
    assert quota.display_id(pid) == shown
    assert quota.canonical_id(shown) == pid
    assert quota.canonical_id(pid) == pid, "a wire id must pass through untouched"


def test_seat_command_only_where_the_name_is_not_the_command() -> None:
    """Codex seats are named after their own aliases; the Claude ones are not."""
    assert quota.seat_command("claude:work") == "cwork"
    assert quota.seat_command("claude:private") == "cpriv"
    assert quota.seat_command("codex:de") == ""
    assert quota.seat_command("copilot") == ""


def test_an_extra_codex_label_keeps_its_own_spelling() -> None:
    """Only `default`/`private` are re-spelled; a `codex_homes_extra` label is its own."""
    assert quota.display_id("codex:de-2") == "codex-de-2"
    assert quota.canonical_id("codex-de-2") == "codex:de-2"


def test_snapshot_rows_carry_the_display_name_and_the_command() -> None:
    """Additive v2 fields: a consumer renders a report without re-deriving the rules."""
    snap = quota.snapshot(now=NOW)
    rows = {row["id"]: row for row in snap["providers"]}
    assert snap["version"] == quota.SCHEMA_VERSION
    for pid, row in rows.items():
        assert row["display"] == quota.display_id(pid)
        assert row.get("command", "") == quota.seat_command(pid)


# ── Google Antigravity (`agy`) ────────────────────────────────────────────────


def _agy_snapshot(gemini_pct: float, third_party_pct: float, captured_at: int = NOW) -> None:
    """Write an Antigravity cache with both weekly buckets at the given USED percentages."""
    usage._write_agy_usage(
        usage.AgyUsage(
            captured_at=captured_at,
            buckets=[
                usage.AgyBucket(
                    id="gemini-weekly",
                    group="Gemini Models",
                    label="Weekly Limit Remaining",
                    window="weekly",
                    used_percentage=gemini_pct,
                    resets_at=NOW + 4 * 86400,
                ),
                usage.AgyBucket(
                    id="3p-weekly",
                    group="Claude and GPT models",
                    label="Weekly Limit Remaining",
                    window="weekly",
                    used_percentage=third_party_pct,
                    resets_at=NOW + 5 * 86400,
                ),
            ],
        )
    )


def test_agy_without_a_snapshot_is_unknown_not_blocked() -> None:
    """No meter is a measurement failure — the rung stays runnable (fail-open)."""
    row = quota._agy_quota(NOW, {})
    assert row.id == "agy"
    assert row.state == quota.UNKNOWN
    assert "no usage snapshot" in row.reason


def test_agy_reports_both_weekly_buckets_and_no_session_window() -> None:
    _agy_snapshot(gemini_pct=12.0, third_party_pct=3.0)
    row = quota._agy_quota(NOW, {})
    assert row.state == quota.AVAILABLE
    assert set(row.windows) == {"gemini_week", "claudegpt_week"}
    assert "five_hour" not in row.windows  # Antigravity has no session window at all
    assert row.windows["gemini_week"].used_pct == 12.0


def test_agy_third_party_exhaustion_does_not_block_a_gemini_call() -> None:
    """The two allowances are independent; collapsing them would delete a working rung."""
    _agy_snapshot(gemini_pct=10.0, third_party_pct=100.0)
    assert quota._agy_quota(NOW, {}, "gemini-3.8-flash-low").state == quota.AVAILABLE
    # …and with no model named, the Gemini bucket governs — the family the rung spends.
    assert quota._agy_quota(NOW, {}, "").state == quota.AVAILABLE
    # Naming a Claude/GPT model DOES pick up the exhausted bucket.
    blocked = quota._agy_quota(NOW, {}, "claude-sonnet-4-6")
    assert blocked.state == quota.BLOCKED
    assert blocked.blocked_by == "claudegpt_week"


def test_agy_gemini_exhaustion_blocks_the_default_scope() -> None:
    _agy_snapshot(gemini_pct=100.0, third_party_pct=0.0)
    row = quota._agy_quota(NOW, {}, "")
    assert row.state == quota.BLOCKED
    assert row.blocked_by == "gemini_week"
    assert row.resets_at == NOW + 4 * 86400  # the BLOCKING bucket's reset


def test_agy_stale_snapshot_is_unknown() -> None:
    """A day-old reading of 100 % proves nothing about today's allowance."""
    _agy_snapshot(gemini_pct=100.0, third_party_pct=100.0, captured_at=NOW - 2 * 86400)
    assert quota._agy_quota(NOW, {}, "").state == quota.UNKNOWN


def test_agy_cooldown_outranks_the_meter() -> None:
    """An observed refusal is stricter evidence than any cached percentage."""
    _agy_snapshot(gemini_pct=1.0, third_party_pct=1.0)
    quota.record_block("agy", blocked_until=NOW + 3600, reason="agy refused", observed_at=NOW)
    row = quota._agy_quota(NOW, quota.read_cooldowns(NOW))
    assert row.state == quota.BLOCKED
    assert row.source == "cooldown"


def test_agy_appears_in_the_snapshot_contract() -> None:
    _agy_snapshot(gemini_pct=5.0, third_party_pct=5.0)
    snap = quota.snapshot(now=NOW)
    row = next(p for p in snap["providers"] if p["id"] == "agy")
    assert row["kind"] == "agy"
    assert set(row["windows"]) == {"gemini_week", "claudegpt_week"}
    # `agy` has no seat, so it reads the same in both spellings — the round-trip every
    # id-taking flag relies on.
    assert quota.display_id("agy") == "agy"
    assert quota.canonical_id("agy") == "agy"


def test_bar_slots_name_only_windows_providers_really_have() -> None:
    """The report's two bars are fed from real window names, not aspirational ones.

    A typo here is silent: every row would simply print `—` where its bar belongs, which
    is exactly what happened the first time these were spelled `fivehour`/`sevenday`.
    """
    slots = dict(quota.BAR_SLOTS)
    assert slots["session"] == ("five_hour",)
    known = {*slots["session"], *slots["week"], *quota.BAR_SPAN_WINDOWS, "fable_week"}
    _agy_snapshot(gemini_pct=5.0, third_party_pct=5.0)
    snap = quota.snapshot(now=NOW)
    for prov in snap["providers"]:
        for name in prov.get("windows") or {}:
            assert name in known, name
    # A spanning KIND must not also claim a session/week slot — it would draw twice.
    assert not set(quota.BAR_SPAN_WINDOWS) & {*slots["session"], *slots["week"]}


# ── the report's usage bars ──────────────────────────────────────────────────


def _quota_args(**over: str | bool | None) -> argparse.Namespace:
    """A `ccc quota` Namespace with every flag at its default, then *over* applied."""
    base = dict(
        json=False,
        provider=None,
        best=False,
        model="",
        refresh=False,
        mark=None,
        retry_after=None,
        until=None,
        hold=False,
        reason="",
        scope="",
        clear=None,
        observed_only=False,
        no_bars=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_quota_report_draws_a_session_and_week_bar(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row gets both bars, with the percentage embossed inside them."""
    from command_center import cli

    _agy_snapshot(gemini_pct=40.0, third_party_pct=0.0)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args()) == 0
    out = capsys.readouterr().out
    assert "session" in out and "week" in out
    agy_row = next(line for line in out.splitlines() if " agy " in line)
    # Antigravity has no session window: an EMPTY bar reading 0%, never a dash, so the
    # column keeps its shape.
    assert "░░░░░░░░░░░0%" in agy_row
    assert "—" not in agy_row
    assert "█████░░░░░40%" in agy_row  # the weekly bucket, figure embossed
    # The bar's window is NOT repeated in the textual column; the one with no bar is.
    assert "geminiweek" not in agy_row
    assert "claudegptweek 0%" in agy_row


def test_quota_report_gives_copilot_one_bar_across_both_columns(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copilot's budget is one month-long allowance, not a session/week pair.

    Squeezing it into the week cell and leaving session blank would describe a provider
    with two horizons; the row is drawn with one bar spanning both instead — and it still
    spans when the seat is blocked before any meter was read.
    """
    from command_center import cli

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args()) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if " copilot " in line)
    # ONE run of bar glyphs, the width of both cells plus the space between them —
    # a two-cell row would show two runs separated by a space.
    assert "░" * (cli._BAR_SPAN_WIDTH - 2) + "0%" in row
    assert len(re.findall(r"[░█]+", row)) == 1, row


def test_quota_report_header_lines_up_with_its_rows(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header and the rows are built from the same widths, so columns cannot drift.

    Regression: the header spent `width + 2` columns on the provider name while each row
    spent `cell_len(mark) + 1 + width`, so every column sat one place to the left of its
    heading.
    """
    from rich.cells import cell_len

    from command_center import cli

    _agy_snapshot(gemini_pct=40.0, third_party_pct=0.0)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args()) == 0
    lines = capsys.readouterr().out.splitlines()
    header = lines[0]
    rows = [ln for ln in lines[1:] if ln.startswith("  ")]
    assert rows, lines

    def at_column(text: str, col: int) -> str:
        """The character occupying display column *col* — rows carry double-width marks,
        so a character index is not a column index."""
        seen = 0
        for ch in text:
            if seen == col:
                return ch
            seen += cell_len(ch)
        return ""

    for label in ("state", "data age", "session", "week", "unblocks"):
        col = cell_len(header[: header.index(label)])
        for row in rows:
            # The copilot row draws ONE bar across session+week by design, so it is the
            # one row that legitimately has no field boundary at the `week` heading.
            if label == "week" and " copilot " in row:
                continue
            # A heading sits at the first column of its field, so the column before it is
            # the separator space on every row.
            assert at_column(row, col - 1) == " ", (label, row)
            # …and the field itself is non-empty wherever it is always populated
            # (`unblocks` is blank on an available provider, by design).
            if label != "unblocks":
                assert at_column(row, col) != " ", (label, row)


def test_quota_report_no_bars_flag_restores_the_plain_columns(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import cli

    _agy_snapshot(gemini_pct=40.0, third_party_pct=0.0)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args(no_bars=True)) == 0
    out = capsys.readouterr().out
    assert "session" not in out.splitlines()[0]
    assert "█" not in out and "░" not in out
    # With no bars drawn, every window is back in the textual column.
    assert "geminiweek 40%" in out
