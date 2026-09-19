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
from dataclasses import replace
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
    assert {p["id"] for p in snap["providers"]} >= {"copilot", "codex", "agy"}


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


def _agy_rows(now: int = NOW, cooldowns: dict | None = None) -> dict[str, quota.ProviderQuota]:
    return {row.id: row for row in quota._agy_quotas(now, cooldowns or {})}


def test_agy_without_a_snapshot_is_unknown_not_blocked() -> None:
    """No meter is a measurement failure — both rungs stay runnable (fail-open)."""
    rows = _agy_rows()
    assert set(rows) == {"agy", "agy:gpt"}
    for row in rows.values():
        assert row.state == quota.UNKNOWN
        assert "no usage snapshot" in row.reason


def test_each_agy_row_owns_exactly_one_weekly_bucket() -> None:
    """One account, two INDEPENDENT allowances — so two rows, one window each."""
    _agy_snapshot(gemini_pct=12.0, third_party_pct=3.0)
    rows = _agy_rows()
    assert list(rows["agy"].windows) == ["gemini_week"]
    assert list(rows["agy:gpt"].windows) == ["claudegpt_week"]
    assert rows["agy"].windows["gemini_week"].used_pct == 12.0
    assert rows["agy:gpt"].windows["claudegpt_week"].used_pct == 3.0
    # Neither has a session window at all.
    assert all("five_hour" not in row.windows for row in rows.values())
    assert all(row.state == quota.AVAILABLE for row in rows.values())


def test_an_exhausted_bucket_blocks_only_its_own_rung() -> None:
    """The failure this split exists to prevent: one dead week deleting the other rung."""
    _agy_snapshot(gemini_pct=10.0, third_party_pct=100.0)
    rows = _agy_rows()
    assert rows["agy"].state == quota.AVAILABLE
    assert rows["agy:gpt"].state == quota.BLOCKED
    assert rows["agy:gpt"].blocked_by == "claudegpt_week"

    _agy_snapshot(gemini_pct=100.0, third_party_pct=0.0)
    rows = _agy_rows()
    assert rows["agy"].state == quota.BLOCKED
    assert rows["agy"].blocked_by == "gemini_week"
    assert rows["agy"].resets_at == NOW + 4 * 86400  # the BLOCKING bucket's reset
    assert rows["agy:gpt"].state == quota.AVAILABLE


def test_agy_stale_snapshot_is_unknown() -> None:
    """A day-old reading of 100 % proves nothing about today's allowance."""
    _agy_snapshot(gemini_pct=100.0, third_party_pct=100.0, captured_at=NOW - 2 * 86400)
    assert all(row.state == quota.UNKNOWN for row in _agy_rows().values())


def test_a_cooldown_blocks_only_the_row_it_names() -> None:
    """An observed refusal is stricter evidence than the meter — for ITS bucket alone."""
    _agy_snapshot(gemini_pct=1.0, third_party_pct=1.0)
    quota.record_block("agy:gpt", blocked_until=NOW + 3600, reason="agy refused", observed_at=NOW)
    rows = _agy_rows(cooldowns=quota.read_cooldowns(NOW))
    assert rows["agy:gpt"].state == quota.BLOCKED
    assert rows["agy:gpt"].source == "cooldown"
    # …and the blocked row still carries the meter it was measured with.
    assert rows["agy:gpt"].windows["claudegpt_week"].used_pct == 1.0
    assert rows["agy"].state == quota.AVAILABLE


def test_both_agy_rows_appear_in_the_snapshot_contract() -> None:
    _agy_snapshot(gemini_pct=5.0, third_party_pct=5.0)
    snap = quota.snapshot(now=NOW)
    rows = {p["id"]: p for p in snap["providers"] if p["kind"] == "agy"}
    assert set(rows) == {"agy", "agy:gpt"}
    assert rows["agy:gpt"]["display"] == "agy-gpt"
    # Both spellings address the same row — the round-trip every id-taking flag needs.
    assert quota.display_id("agy:gpt") == "agy-gpt"
    assert quota.canonical_id("agy-gpt") == "agy:gpt"
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
        credit=None,
        probe=False,
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
    # Antigravity meters ONE weekly allowance per row and no session at all, so each row
    # draws a single spanning bar rather than leaving a permanently empty session cell.
    agy_row = next(line for line in out.splitlines() if " agy " in line)
    # One bar: the glyph runs are split only by the embossed `weekly` label, never by the
    # gap between two cells, so the whole width is a single allowance.
    assert "weekly" in agy_row
    assert re.sub(r"[░█]+", "#", agy_row).count("#") <= 2, agy_row
    assert "40%" in agy_row  # its weekly bucket, figure embossed inside the bar
    # The bar's window is NOT repeated in the textual column, and the OTHER allowance is
    # a row of its own now, so nothing trails the bars at all.
    assert "geminiweek" not in agy_row
    assert "claudegptweek" not in agy_row
    gpt_row = next(line for line in out.splitlines() if " agy-gpt " in line)
    assert "weekly" in gpt_row
    # An untouched bucket: track glyphs the full span, broken only by its own label.
    bar = "".join(re.findall(r"[░█]+|weekly", gpt_row))
    assert bar == "░" * 9 + "weekly" + "░" * 10, bar


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
        """The character occupying display column *col*.

        Rows carry double-width marks, so a character index is not a column index. Rows
        are right-stripped, so a column past the end reads as the blank it would be.
        """
        seen = 0
        for ch in text:
            if seen == col:
                return ch
            seen += cell_len(ch)
        return " "

    spanning = (" copilot ", " agy ", " agy-gpt ", " opencode-free ", " opencode-priv ")
    for label in ("state", "data age", "session", "week", "renew"):
        col = cell_len(header[: header.index(label)])
        for row in rows:
            # A single-allowance provider draws ONE bar across session+week by design,
            # so those rows legitimately have no field boundary at the `week` heading.
            if label == "week" and any(name in row for name in spanning):
                continue
            # A heading sits at the first column of its field, so the column before it is
            # the separator space on every row.
            assert at_column(row, col - 1) == " ", (label, row)
            # …and the field itself is non-empty on EVERY row: `renew` states a dash when
            # a row has no renewing allowance, so no column here is ever legitimately
            # blank (the old `unblocks` field was, which is what hid drift in it).
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


# ── a blocked row still carries what the meter measured ──────────────────────
#
# The cooldown store decides the VERDICT — that is its whole job. It must not decide what
# the row knows: `ccc quota` drew empty 0% bars for a seat whose weekly window the TUI
# card beside it showed at 100%, because every cooldown path returned before reading the
# snapshot. A row and a card must never disagree about what was measured.


def test_blocked_claude_row_still_reports_its_windows() -> None:
    usage.write_usage(
        {
            "five_hour": {"used_percentage": 12, "resets_at": NOW + 3600},
            "seven_day": {"used_percentage": 100, "resets_at": NOW + 86400},
        },
        account="private",
        now=NOW,
    )
    quota.record_block(
        "claude:private", blocked_until=NOW + 7200, reason="rate-limit halt", observed_at=NOW
    )
    row = quota._claude_quota("private", "", NOW, quota.read_cooldowns(NOW))
    assert row.state == quota.BLOCKED
    assert row.source == "cooldown"  # the verdict is still the entry's
    assert row.reason == "rate-limit halt"
    # …and the meter travels with it.
    assert row.windows["seven_day"].used_pct == 100.0
    assert row.windows["five_hour"].used_pct == 12.0
    # The BLOCKING signal stays the rejection, not a window — a cooldown is not a meter
    # reading and its reset is the entry's deadline.
    assert row.blocked_by == "observed-rejection"
    assert row.resets_at == NOW + 7200


def test_blocked_copilot_row_still_reports_its_credit_window() -> None:
    usage._write_copilot_usage(
        usage.CopilotUsage(
            captured_at=NOW,
            year=2026,
            month=9,
            sku="AI Credits",
            unit="AI credits",
            quantity=1500.0,
            gross=0.0,
            net=0.0,
            credit_quota=1500,
            credits_used=1500.0,
            premium_reset_at=NOW + 86400,
            quota_source="api",
        )
    )
    quota.record_block("copilot", blocked_until=NOW + 7200, reason="429", observed_at=NOW)
    row = quota._copilot_quota(NOW, quota.read_cooldowns(NOW))
    assert row.state == quota.BLOCKED
    assert row.windows["credits"].used_pct == 100.0


def test_a_guessed_denominator_is_shown_but_never_blocks() -> None:
    """The safety rule is about the VERDICT, not about hiding the figure.

    A configured `copilot_credit_quota` has been observed at 2x the real entitlement, so
    it may not establish exhaustion — but the card draws it, so the row must show it too.
    """
    usage._write_copilot_usage(
        usage.CopilotUsage(
            captured_at=NOW,
            year=2026,
            month=9,
            sku="AI Credits",
            unit="AI credits",
            quantity=1500.0,
            gross=0.0,
            net=0.0,
            credit_quota=1500,
            credits_used=1500.0,
            premium_reset_at=NOW + 86400,
            quota_source="config",
        )
    )
    row = quota._copilot_quota(NOW, {})
    assert row.state == quota.UNKNOWN
    assert row.windows["credits"].used_pct == 100.0


# ── OpenCode Zen (`ofree` / `opriv`) ─────────────────────────────────────────
#
# The behaviour worth guarding: Zen publishes NO meter (researched, not assumed — every
# usage/billing endpoint 404s and the Go one is 403 without a subscription), so neither
# row may claim proven headroom from a measurement nobody could take. The paid rung is a
# prepaid WALLET reconstructed from a human's console reading plus this machine's spend,
# and it never renews. The free rung's only meter is asking it.


def _opencode_db(path: Path, messages: list[dict], table: str = "message") -> None:
    """Write a miniature opencode store: one row per *messages* entry, ms timestamps."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        f"create table {table} (id text primary key, session_id text, "  # noqa: S608
        "time_created integer, time_updated integer, data text)"
    )
    for index, message in enumerate(messages):
        con.execute(
            f"insert into {table} values (?,?,?,?,?)",  # noqa: S608
            (
                f"m{index}",
                "s1",
                int(message.pop("at", NOW)) * 1000,
                int(NOW) * 1000,
                json.dumps(message),
            ),
        )
    con.commit()
    con.close()


def _zen(cost: float, model: str = "glm-5.3-flash", at: int = NOW) -> dict:
    return {
        "role": "assistant",
        "providerID": "opencode",
        "modelID": model,
        "cost": cost,
        "at": at,
    }


def _opencode_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, messages: list[dict], **kw: str
) -> Path:
    path = tmp_path / "opencode.db"
    _opencode_db(path, messages, **kw)
    monkeypatch.setenv(usage._OPENCODE_DB_ENV, str(path))
    return path


def _opencode_rows(now: int = NOW, cooldowns: dict | None = None) -> dict[str, quota.ProviderQuota]:
    return {row.id: row for row in quota._opencode_quotas(now, cooldowns or {})}


def _wallet(monkeypatch: pytest.MonkeyPatch, usd: float, **over: object) -> None:
    """Pin the wallet size (and any other knob) without writing the user's config file."""
    from command_center import config

    live = config.load_config()
    pinned = replace(live, opencode_credit_usd=usd, **over)  # type: ignore[arg-type]
    monkeypatch.setattr(config, "load_config", lambda: pinned)


def test_opencode_ids_round_trip_and_name_their_shell_commands() -> None:
    """A user must be able to paste either spelling back, and see how to open the rung."""
    for pid, shown, command in (
        ("opencode:free", "opencode-free", "ofree"),
        ("opencode:priv", "opencode-priv", "opriv"),
    ):
        assert quota.display_id(pid) == shown
        assert quota.canonical_id(shown) == pid
        assert quota.canonical_id(pid) == pid  # already canonical passes through
        assert quota.seat_command(pid) == command


def test_opencode_without_a_store_is_unknown_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store we cannot read is a measurement failure — both rungs stay runnable."""
    monkeypatch.setenv(usage._OPENCODE_DB_ENV, str(tmp_path / "absent.db"))
    rows = _opencode_rows()
    assert set(rows) == {"opencode:free", "opencode:priv"}
    for row in rows.values():
        assert row.state == quota.UNKNOWN
        assert row.windows == {}


def test_the_wallet_is_an_anchor_plus_a_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$20 loaded, $15 read off the console, $2 spent here since → $13 left, 35 % used."""
    _opencode_store(tmp_path, monkeypatch, [_zen(5.0, at=NOW - 86400), _zen(2.0, at=NOW + 60)])
    _wallet(monkeypatch, 20.0)
    usage.record_opencode_credit(15.0, NOW)
    priv = _opencode_rows(now=NOW + 120)["opencode:priv"]
    assert priv.windows["wallet"].used_pct == pytest.approx(35.0)  # (20 - 13) / 20
    assert "≈$13.00 of $20.00 left" in priv.reason
    assert "$2.00 spent here since" in priv.reason
    # Spend from BEFORE the reading is already inside the number the human read.
    assert "$5.00" not in priv.reason


def test_a_wallet_does_not_renew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It refills when money is added, never on a clock — so it has no reset instant."""
    from command_center import cli

    _opencode_store(tmp_path, monkeypatch, [_zen(1.0)])
    _wallet(monkeypatch, 20.0)
    usage.record_opencode_credit(15.0, NOW)
    priv = _opencode_rows()["opencode:priv"]
    assert priv.windows["wallet"].resets_at == 0
    assert "wallet" not in cli._RENEW_WINDOWS
    assert cli._quota_renew(quota._provider_dict(priv), NOW) == ("—", 0, "")


def test_without_an_anchor_the_row_states_a_lower_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local spend cannot see other machines, so it can only floor what has been used."""
    _opencode_store(tmp_path, monkeypatch, [_zen(3.0), _zen(1.0)])
    _wallet(monkeypatch, 20.0)
    priv = _opencode_rows()["opencode:priv"]
    assert "≤$16.00 of $20.00 left" in priv.reason
    assert "LOWER bound" in priv.reason
    assert priv.windows["wallet"].used_pct == pytest.approx(20.0)


def test_no_wallet_configured_means_no_bar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spend figure with no denominator is prose, not a percentage."""
    _opencode_store(tmp_path, monkeypatch, [_zen(3.5), _zen(1.5)])
    _wallet(monkeypatch, 0.0)
    priv = _opencode_rows()["opencode:priv"]
    assert priv.windows == {}
    assert priv.state == quota.UNKNOWN
    assert "$5.00 spent here all-time" in priv.reason


def test_only_an_empty_wallet_blocks_the_paid_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconstructed headroom cannot prove availability; an empty wallet does prove a block."""
    _opencode_store(tmp_path, monkeypatch, [_zen(4.0, at=NOW + 60)])
    _wallet(monkeypatch, 20.0)
    usage.record_opencode_credit(10.0, NOW)
    assert _opencode_rows(now=NOW + 120)["opencode:priv"].state == quota.UNKNOWN

    usage.record_opencode_credit(3.0, NOW)  # …and now the delta exceeds what was left
    empty = _opencode_rows(now=NOW + 120)["opencode:priv"]
    assert empty.state == quota.BLOCKED
    assert empty.blocked_by == "wallet"
    assert "wallet empty" in empty.reason


def test_spend_is_summed_per_message_not_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that ends on a free model still owes what its paid messages cost.

    `session.cost` would attribute the whole session to its LAST model and its LAST
    activity — so one resumed conversation could move months of spend into today.
    """
    _opencode_store(
        tmp_path,
        monkeypatch,
        [
            _zen(2.0, "gpt-5.4"),
            _zen(0.0, "muse-spark-1.3-free"),
            {"role": "user", "content": "hi", "at": NOW},
            {"role": "assistant", "providerID": "github-copilot", "cost": 9.0, "at": NOW},
        ],
    )
    snap = usage.fetch_opencode_usage(NOW)
    assert snap is not None
    assert snap.paid_usd_total == pytest.approx(2.0)  # not 11.0 — copilot is not Zen
    assert snap.free_messages == 1


def test_an_unrecognised_payload_is_unknown_not_zero_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a schema migration, summing a field that is gone would report $0.00 surely."""
    _opencode_store(tmp_path, monkeypatch, [{"kind": "assistant", "price": 4.0, "at": NOW}])
    assert usage.fetch_opencode_usage(NOW) is None
    _wallet(monkeypatch, 20.0)
    assert _opencode_rows()["opencode:priv"].state == quota.UNKNOWN


def test_a_table_rename_is_followed_not_reported_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store has migrated before; the reader picks the table that has the rows."""
    path = tmp_path / "opencode.db"
    _opencode_db(path, [], table="message")  # the old, now-empty table
    _opencode_db(path, [_zen(7.0)], table="session_message")
    monkeypatch.setenv(usage._OPENCODE_DB_ENV, str(path))
    snap = usage.fetch_opencode_usage(NOW)
    assert snap is not None and snap.paid_usd_total == pytest.approx(7.0)


def test_a_store_with_no_known_table_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    path = tmp_path / "opencode.db"
    sqlite3.connect(path).execute("create table something_else (id text)")
    monkeypatch.setenv(usage._OPENCODE_DB_ENV, str(path))
    assert usage.fetch_opencode_usage(NOW) is None


def test_the_free_row_is_whatever_the_last_probe_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A served request is the ONE thing that can prove an unmetered rung is up."""
    _opencode_store(tmp_path, monkeypatch, [_zen(0.0, "muse-spark-1.3-free")])
    _wallet(monkeypatch, 20.0)

    free = _opencode_rows()["opencode:free"]
    assert free.state == quota.UNKNOWN and "ccc quota -P" in free.reason

    usage.record_opencode_probe(True, "", NOW)
    served = _opencode_rows()["opencode:free"]
    assert served.state == quota.AVAILABLE
    assert served.windows == {}  # proof of service is not a percentage

    usage.record_opencode_probe(False, "Rate limit exceeded. Please try again later.", NOW)
    refused = _opencode_rows()["opencode:free"]
    assert refused.state == quota.BLOCKED
    assert refused.blocked_by == "free-tier"
    assert "Rate limit exceeded" in refused.reason
    # No invented deadline: Zen states none, and the row says where a real one comes from.
    assert refused.resets_at == 0
    assert "ccc quota -m opencode-free" in refused.reason


def test_a_stale_probe_decides_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The free tier turns over within hours; yesterday's answer is not today's."""
    _opencode_store(tmp_path, monkeypatch, [_zen(0.0)])
    _wallet(monkeypatch, 20.0, opencode_probe_ttl_sec=3600)
    usage.record_opencode_probe(False, "Rate limit exceeded", NOW - 2 * 3600)
    free = _opencode_rows()["opencode:free"]
    assert free.state == quota.UNKNOWN
    assert "stale" in free.reason


def test_a_cooldown_outranks_both_opencode_readings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ccc quota -m opencode-free -U …` is how the TUI's countdown gets into the table."""
    _opencode_store(tmp_path, monkeypatch, [_zen(1.0)])
    _wallet(monkeypatch, 20.0)
    usage.record_opencode_probe(True, "", NOW)  # …the probe says the tier is up
    quota.record_block(
        "opencode:free", blocked_until=NOW + 9 * 3600, reason="free usage exceeded", observed_at=NOW
    )
    rows = _opencode_rows(cooldowns=quota.read_cooldowns(NOW))
    assert rows["opencode:free"].state == quota.BLOCKED
    assert rows["opencode:free"].source == "cooldown"
    assert rows["opencode:free"].resets_at == NOW + 9 * 3600
    assert rows["opencode:free"].account == "free"  # the seat label survives the override
    assert rows["opencode:priv"].state == quota.UNKNOWN  # untouched


def test_a_stale_wallet_reading_cannot_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A day-old reading of an empty wallet was taken before today's top-up could land."""
    aged = usage.OpencodeUsage(
        captured_at=NOW - 2 * 86400,
        paid_usd_total=50.0,
        paid_usd_since_credit=50.0,
        free_messages=0,
        credit=usage.OpencodeCredit(usd=1.0, at=NOW - 3 * 86400),
    )
    monkeypatch.setattr(usage, "read_opencode_usage", lambda *_a, **_k: aged)
    _wallet(monkeypatch, 20.0)
    priv = _opencode_rows()["opencode:priv"]
    assert priv.windows["wallet"].stale is True
    assert priv.state == quota.UNKNOWN


@pytest.mark.parametrize(
    ("chunk", "expected"),
    [
        ('level=ERROR error.error="AI_APICallError: Rate limit exceeded. Try later."', False),
        ("Free usage exceeded, subscribe to Go", False),
        ("\x1b[0m\n> build · muse-spark-1.3-contributor-free\n", None),
        ("timestamp=2026-09-19T14:20:20.764Z level=INFO message=started", None),
        ("hallo", True),
    ],
)
def test_the_probe_reads_a_verdict_out_of_the_cli_stream(chunk: str, expected: bool | None) -> None:
    """Served, refused, or not yet decisive — the three answers, off partial output.

    The probe streams because a spent tier makes `opencode run` retry rather than fail:
    it must decide on the first line that says something, not on the exit code.
    """
    verdict = usage._opencode_probe_verdict(chunk)
    assert (verdict[0] if verdict else None) is expected


# ── the report's renew field ─────────────────────────────────────────────────
#
# One field per row answering "by when must I spend this?". It REPLACED the old
# `unblocks` field rather than joining it, so the deadline of a blocked row and the
# renewal of its allowance can no longer be printed as two separate facts when they are
# the same instant — which is the case for every window that blocks by being full.


def _renew_of(prov: dict, now: int = NOW) -> tuple[str, int, str]:
    from command_center import cli

    return cli._quota_renew(prov, now)


def _window(name: str, resets_in: int, pct: float = 10.0) -> dict:
    return {"windows": {name: {"used_pct": pct, "resets_at": NOW + resets_in}}}


@pytest.mark.parametrize(
    ("resets_in", "color"),
    [
        (3600, "\033[31m"),  # under a day: red, spend it today
        (86400 - 1, "\033[31m"),
        (86400, "\033[38;5;208m"),  # exactly a day is no longer "today"
        (2 * 86400 - 1, "\033[38;5;208m"),
        (2 * 86400, "\033[32m"),  # exactly two days is calm
        (9 * 86400, "\033[32m"),
    ],
)
def test_renew_urgency_boundaries(resets_in: int, color: str) -> None:
    """The colour boundaries are AT 24 h and 48 h, not near them."""
    text, at, shown = _renew_of(_window("seven_day", resets_in))
    assert shown == color
    assert at == NOW + resets_in
    assert text.startswith("renew ")


def test_a_used_up_quota_keeps_the_date_but_loses_the_colour() -> None:
    """Red means "spend this". A quota you have already spent has nothing to spend.

    The date still matters — it is when the rung comes back — but colouring it would
    shout about the one thing the reader cannot act on, and would drown out the rows that
    really do have allowance about to expire.
    """
    full = {
        "state": "blocked",
        "windows": {"seven_day": {"used_pct": 100.0, "resets_at": NOW + 3600}},
    }
    text, at, color = _renew_of(full)
    assert (text, at) == ("renew 1h 0m", NOW + 3600)
    assert color == ""
    # …and the same row while merely NEARLY spent: past quota's own "risky" line there
    # is too little left to be worth a warning.
    nearly = {"windows": {"seven_day": {"used_pct": 95.0, "resets_at": NOW + 3600}}}
    assert _renew_of(nearly)[2] == ""
    assert _renew_of({"windows": {"seven_day": {"used_pct": 89.0, "resets_at": NOW + 3600}}})[
        2
    ] == ("\033[31m")


def test_a_blocked_row_states_when_it_comes_back_even_with_no_window() -> None:
    """The free tier's refusal carries a deadline but no meter — the field still shows it."""
    held = {"state": "blocked", "resets_at": NOW + 9 * 3600, "windows": {}}
    text, at, color = _renew_of(held)
    assert (text, at) == ("renew 9h 0m", NOW + 9 * 3600)
    assert color == ""


def test_renew_ignores_the_session_window() -> None:
    """A 5-hour window renews all day; it is not the allowance you plan around."""
    assert _renew_of(_window("five_hour", 3600)) == ("—", 0, "")


def test_renew_prefers_the_longest_horizon() -> None:
    """A row with both states its WEEKLY renewal, never its session one."""
    prov = {
        "windows": {
            "five_hour": {"used_pct": 0.0, "resets_at": NOW + 600},
            "seven_day": {"used_pct": 0.0, "resets_at": NOW + 3 * 86400},
        }
    }
    text, at, _color = _renew_of(prov)
    assert (text, at) == ("renew 3d 0h", NOW + 3 * 86400)


def test_a_past_reset_is_a_dash_not_a_countdown_to_yesterday() -> None:
    """A stale window's reset already happened; `renew 0m` would invite a pointless wait."""
    assert _renew_of(_window("seven_day", -3600)) == ("—", 0, "")


def test_a_second_allowance_states_its_own_renewal() -> None:
    """`fable_week` is a second deadline; the single renew field cannot speak for it."""
    from command_center import cli

    same = {"used_pct": 13.0, "resets_at": NOW + 3 * 86400}
    assert cli._window_renew(same, NOW + 3 * 86400, NOW) == ""  # same event, stays quiet
    assert cli._window_renew(same, NOW + 3 * 86400 + 600, NOW) == ""  # within the hour
    assert cli._window_renew(same, NOW + 6 * 86400, NOW) == " (renew 3d 0h)"


def test_the_report_states_a_renewal_for_every_row(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row carries the field — a dash where there is nothing to renew."""
    from command_center import cli

    _agy_snapshot(gemini_pct=40.0, third_party_pct=0.0)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "renew" in lines[0] and "unblocks" not in lines[0]
    rows = [ln for ln in lines[1:] if ln.startswith("  ")]
    assert rows
    for row in rows:
        assert "renew " in row or "—" in row, row
    # Its weekly bucket, stated as a span. The fixture's clock is not the report's, so
    # the SHAPE is what matters: a field that always says "—" would pass a laxer check.
    agy_row = next(ln for ln in rows if " agy " in ln)
    assert re.search(r"renew \d+d \d+h", agy_row), agy_row


def test_a_full_window_states_its_reset_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exhausted bar no longer embosses a reset the renew field already prints."""
    from command_center import cli

    _agy_snapshot(gemini_pct=100.0, third_party_pct=0.0)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args()) == 0
    row = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("  ⛔ agy ")
    )
    assert "resets" not in row  # the bar's old embossed label is gone
    assert row.count("renew") == 1
    assert "unblocks" not in row  # the block and the renewal are the same instant


def test_a_deadline_the_renewal_does_not_cover_is_still_stated(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cooldown that expires before the window renews is a SECOND fact — keep it."""
    from command_center import cli

    _agy_snapshot(gemini_pct=1.0, third_party_pct=1.0)
    quota.record_block("agy", blocked_until=int(time.time()) + 900, reason="429", observed_at=0)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert cli.cmd_quota(_quota_args()) == 0
    row = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("  ⛔ agy ")
    )
    assert "unblocks in 15m" in row
    assert "renew" in row
