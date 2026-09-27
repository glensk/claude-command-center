"""Tests for the Codex DEBATE start caps (tp#619): ``codex-in-claude debate-seat``.

The rule (owner decision, 2026-09-26): a debate may START only on an allowed seat whose FRESH
five-hour reading is below the item's cap — 70 % strict, 75 % default. Everything that
could make the gate allow a debate it should refuse is guarded here: the cap boundaries,
the allowlist, an inherited ``$CODEX_HOME``, a reading from a window that has reset, and
a policy that does not parse.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import config, quota, usage

_HOUR = 3600
_DAY = 86400


def _live(home: Path, five: float, *, now: int, age: int = 0, resets_in: int = 4 * _HOUR) -> None:
    """A live usage cache for one seat: five-hour *five* %, measured *age* s ago."""
    usage._write_codex_usage(  # noqa: SLF001
        home,
        usage.Usage(
            captured_at=now - age,
            five_hour=usage.Window(used_percentage=five, resets_at=now + resets_in),
            seven_day=usage.Window(used_percentage=10.0, resets_at=now + 3 * _DAY),
            live=True,
        ),
        now - age,
    )
    usage._codex_cache.clear()  # noqa: SLF001


def _configure(fixture: SeatFixture, *lines: str) -> None:
    """Append raw TOML lines to the fixture's ccc config.toml."""
    path = fixture.ccc_home / "command-center" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + "\n".join(lines) + "\n", "utf-8")
    config.invalidate_config_cache()


@pytest.fixture(name="seats")
def _seats(three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch) -> SeatFixture:
    """Three seats, no network: the refresh step is a no-op unless a test replaces it."""
    monkeypatch.setattr(cic, "_debate_refresh", lambda *_a, **_k: None)
    return three_seats


def _verdict(tier: str = "default", repo: str = "") -> dict:
    return cic.debate_seat_verdict(tier=tier, repo=repo, now=int(time.time()))


def test_allowed_below_the_default_cap(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 74.0, now=now)
    verdict = _verdict()
    assert verdict["schema_version"] == 1
    assert verdict["state"] == "allowed"
    assert verdict["tier"] == "default"
    assert verdict["cap_percent"] == 75
    assert verdict["used_percent"] == 74.0
    assert verdict["seat"] == verdict["seats"][0]["seat"]


@pytest.mark.parametrize(
    ("tier", "used", "state"),
    [
        ("strict", 69.9, "allowed"),
        ("strict", 70.0, "capped"),
        ("default", 74.9, "allowed"),
        ("default", 75.0, "capped"),
    ],
)
def test_cap_boundaries(seats: SeatFixture, tier: str, used: float, state: str) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], used, now=now)
    verdict = _verdict(tier)
    assert verdict["state"] == state
    if state == "capped":
        assert verdict["resets_at"] == now + 4 * _HOUR
        assert "cap" in verdict["reason"]


def test_big_repo_raises_the_tier(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 72.0, now=now)
    _configure(seats, 'codex_debate_big_repos = ["home/tp", "switch-cloud"]')
    assert _verdict(repo="home/tp")["state"] == "capped"
    assert _verdict(repo="home/tp")["tier"] == "strict"
    assert _verdict(repo="sdsc/switch-cloud")["tier"] == "strict"  # bare entry
    assert _verdict(repo="sdsc/tp")["tier"] == "default"  # full entry, other category
    assert _verdict(repo="home/other")["state"] == "allowed"


def test_per_seat_override(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 60.0, now=now)
    _configure(
        seats,
        'codex_debate_seats = ["private"]',
        'codex_debate_seat_caps = ["private=55/58"]',
    )
    verdict = _verdict()
    assert verdict["state"] == "capped"
    assert verdict["cap_percent"] == 58
    assert _verdict("strict")["cap_percent"] == 55


def test_disallowed_default_seat_is_never_chosen(seats: SeatFixture) -> None:
    now = int(time.time())
    _live(seats.seats["default"], 1.0, now=now)
    _live(seats.seats["private"], 90.0, now=now)
    _live(seats.seats["de"], 90.0, now=now)
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    verdict = _verdict()
    assert verdict["state"] == "capped"
    assert {seat["seat"] for seat in verdict["seats"]} == {"private", "de"}


def test_alias_resolves_in_the_allowlist(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 10.0, now=now)
    _configure(seats, 'codex_seat_aliases = ["gl=private"]', 'codex_debate_seats = ["gl"]')
    assert _verdict()["seat"] == "private"


def test_routing_order_not_config_order_decides(
    seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist FILTERS the ranking; its own order means nothing."""
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 10.0, now=now)
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    real = cic.codex_homes_in_order

    def _ranked(now_ts: int | None = None, *, probe: bool = True) -> list[cic.SeatCandidate]:
        cands = real(now_ts, probe=probe)
        return sorted(cands, key=lambda cand: ["de", "default", "private"].index(cand.label))

    monkeypatch.setattr(cic, "codex_homes_in_order", _ranked)
    assert _verdict()["seat"] == "de"


def test_reading_from_a_reset_window_is_unknown(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 5.0, now=now, resets_in=-60)
    verdict = _verdict()
    assert verdict["state"] == "unknown"
    assert all(seat["state"] == "unknown" for seat in verdict["seats"])


def test_old_reading_is_unknown(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 5.0, now=now, age=3600)
    assert _verdict()["state"] == "unknown"


def test_refresh_that_yields_a_fresh_reading_is_judged_on_it(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    for label in three_seats.seats:
        _live(three_seats.seats[label], 5.0, now=now, age=3600)
    fetched: list[str] = []

    def _fetch(home: Path, _now: int | None = None, *, timeout: float | None = None) -> None:
        fetched.append(str(home))
        _live(home, 40.0, now=int(time.time()))
        assert timeout is not None

    monkeypatch.setattr(usage, "fetch_codex_usage", _fetch)
    assert cic.debate_seat_verdict(tier="strict", now=now)["state"] == "unknown"
    assert not fetched  # no live fetch without the `codex_usage` opt-in
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("codex_usage = false", "codex_usage = true"),
        encoding="utf-8",
    )
    config.invalidate_config_cache()
    verdict = cic.debate_seat_verdict(tier="strict", now=now)
    assert fetched
    assert verdict["state"] == "allowed"
    assert verdict["used_percent"] == 40.0


def test_blocked_seat_is_excluded(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 10.0, now=now)
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    quota.record_block("codex:private", blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="h")
    verdict = _verdict()
    assert verdict["seat"] == "de"
    states = {seat["seat"]: seat["state"] for seat in verdict["seats"]}
    assert states["private"] == "blocked"
    quota.record_block("codex:de", blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="h")
    assert _verdict()["state"] == "blocked"


def test_inherited_codex_home(seats: SeatFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    _live(seats.seats["de"], 30.0, now=now)
    monkeypatch.setenv("CODEX_HOME", str(seats.seats["de"]))
    assert _verdict()["seat"] == "de"
    _live(seats.seats["de"], 71.0, now=now)
    assert _verdict("strict")["state"] == "capped"
    _live(seats.seats["default"], 1.0, now=now)
    monkeypatch.setenv("CODEX_HOME", str(seats.seats["default"]))
    verdict = _verdict()
    assert verdict["state"] == "not_allowed"
    assert "codex_debate_seats" in verdict["reason"]


def test_inherited_unregistered_home_is_not_allowed(
    seats: SeatFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adhoc = tmp_path / "adhoc"
    adhoc.mkdir()
    (adhoc / "auth.json").write_text("{}", encoding="utf-8")
    _live(adhoc, 1.0, now=int(time.time()))
    monkeypatch.setenv("CODEX_HOME", str(adhoc))
    verdict = _verdict()
    assert verdict["state"] == "not_allowed"
    assert "not a registered seat" in verdict["reason"]
    _ = seats


def test_kill_switch_is_disabled(seats: SeatFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_NO_CODEX", "1")
    assert _verdict()["state"] == "disabled"
    _ = seats


def test_absent_allowlist_means_every_registered_seat(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 99.0, now=now)
    _live(seats.seats["default"], 5.0, now=now)
    verdict = _verdict()
    assert verdict["seat"] == "default"
    assert {seat["seat"] for seat in verdict["seats"]} == set(seats.seats)


@pytest.mark.parametrize(
    "line",
    [
        'codex_debate_seat_caps = ["de=70"]',
        'codex_debate_seat_caps = ["de=0/75"]',
        "codex_debate_cap_strict_pct = 101",
        'codex_debate_cap_default_pct = "x"',
        'codex_debate_seats = ["nosuchseat"]',
        'codex_debate_big_repos = "home/tp"',
    ],
)
def test_malformed_policy_is_unknown_exit_3(
    seats: SeatFixture, line: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(seats, line)
    key = line.split(" ", 1)[0]
    verdict = _verdict()
    assert verdict["state"] == "unknown"
    assert key in verdict["reason"]
    args = argparse.Namespace(json=True, tier="default", repo="")
    assert cic.cmd_debate_seat(args) == 3
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"


def test_unparseable_config_is_unknown(seats: SeatFixture) -> None:
    _configure(seats, "this is = = not toml")
    verdict = _verdict()
    assert verdict["state"] == "unknown"
    assert "does not parse" in verdict["reason"]


def test_cli_exit_codes_and_text(seats: SeatFixture, capsys: pytest.CaptureFixture[str]) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 42.0, now=now)
    assert cic.cmd_debate_seat(argparse.Namespace(json=False, tier="strict", repo="")) == 0
    assert "(5h 42 % < cap 70 %, strict)" in capsys.readouterr().out
    for label in seats.seats:
        _live(seats.seats[label], 80.0, now=now)
    assert cic.cmd_debate_seat(argparse.Namespace(json=False, tier="strict", repo="")) == 1
    assert "debate seat: CAPPED" in capsys.readouterr().out


def test_parser_has_short_flags() -> None:
    args = cic.build_parser().parse_args(["debate-seat", "-j", "-t", "strict", "-r", "home/tp"])
    assert (args.json, args.tier, args.repo) == (True, "strict", "home/tp")


# --------------------------------------------------------------------------- #
# MID-debate continuation (tp#620): ``debate-seat -c <seat> [-p tool|human] [-x L]``
# --------------------------------------------------------------------------- #


def _live_full(
    home: Path, five: float, week: float, *, now: int, week_resets_in: int = 3 * _DAY
) -> None:
    """A fresh live reading with an explicit weekly figure (100 % = weekly exhaustion)."""
    usage._write_codex_usage(  # noqa: SLF001
        home,
        usage.Usage(
            captured_at=now,
            five_hour=usage.Window(used_percentage=five, resets_at=now + 4 * _HOUR),
            seven_day=usage.Window(used_percentage=week, resets_at=now + week_resets_in),
            live=True,
        ),
        now,
    )
    usage._codex_cache.clear()  # noqa: SLF001


@pytest.fixture(name="pair")
def _pair(seats: SeatFixture, monkeypatch: pytest.MonkeyPatch) -> SeatFixture:
    """``private`` + ``de`` allowed, no measured round costs (the config pause point)."""
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    monkeypatch.setattr(cic, "_debate_cost_deltas", lambda *_a, **_k: [])
    return seats


def _cont(current: str = "private", **kwargs: object) -> dict:
    kwargs.setdefault("now", int(time.time()))
    return cic.debate_continue_verdict(current, **kwargs)  # type: ignore[arg-type]


def _roles(verdict: dict) -> dict[str, str]:
    return {seat["seat"]: seat["role"] for seat in verdict["seats"]}


def test_continue_below_the_pause_point(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 89.9, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    verdict = _cont()
    assert verdict["schema_version"] == 1
    assert verdict["state"] == "continue"
    assert verdict["seat"] == "private"
    assert verdict["used_percent"] == 89.9
    assert verdict["cap_percent"] == 90.0
    assert (verdict["pause_percent"], verdict["pause_rule"]) == (90.0, "config")
    assert (verdict["pause_config"], verdict["pause_p95"], verdict["pause_samples"]) == (
        90.0,
        None,
        0,
    )
    assert (verdict["continue_from"], verdict["pin"], verdict["excluded"]) == (
        "private",
        "tool",
        [],
    )
    assert (verdict["retry_at"], verdict["retry_seat"]) == (None, "")
    assert _roles(verdict) == {"private": "current"}


def test_failover_at_the_pause_point(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 90.0, now=now)
    _live(pair.seats["de"], 74.0, now=now)
    verdict = _cont()
    assert verdict["state"] == "failover"
    assert verdict["seat"] == "de"
    assert verdict["cap_percent"] == 75  # the target's START cap, default tier
    assert verdict["used_percent"] == 74.0
    assert _roles(verdict) == {"private": "current", "de": "candidate"}
    # the strict tier's start cap (70) refuses the same target
    strict = _cont(tier="strict")
    assert strict["state"] == "paused"


def test_target_over_its_start_cap_pauses_until_the_earliest_reset(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 95.0, now=now, resets_in=3 * _HOUR)
    _live(pair.seats["de"], 80.0, now=now, resets_in=2 * _HOUR)
    verdict = _cont()
    assert verdict["state"] == "paused"
    assert verdict["seat"] == "private"  # the current seat's facts
    assert verdict["used_percent"] == 95.0
    assert verdict["retry_at"] == now + 2 * _HOUR
    assert verdict["retry_seat"] == "de"


def test_exclude_removes_a_seat(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 95.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    verdict = _cont(exclude=["de", "nosuchseat"])
    assert verdict["state"] == "paused"
    assert verdict["excluded"] == ["de", "nosuchseat"]
    assert _roles(verdict)["de"] == "excluded"
    assert verdict["retry_seat"] == "private"  # an excluded seat is no retry candidate
    # -x may name the current seat itself: never continued, even below the pause point
    _live(pair.seats["private"], 10.0, now=now)
    moved = _cont(exclude=["private"])
    assert moved["state"] == "failover"
    assert moved["seat"] == "de"
    assert _roles(moved)["private"] == "excluded"


def test_excluded_current_with_no_other_seat_is_paused_untimed(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    verdict = _cont(exclude=["private", "de"])
    assert verdict["state"] == "paused"
    assert verdict["retry_at"] is None


def test_blocked_current_fails_over(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    quota.record_block("codex:private", blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="h")
    verdict = _cont()
    assert verdict["state"] == "failover"
    assert verdict["seat"] == "de"
    current = next(seat for seat in verdict["seats"] if seat["seat"] == "private")
    assert current["state"] == "blocked"


def test_unknown_current_fails_over_to_a_fresh_seat(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now, age=_HOUR)
    _live(pair.seats["de"], 10.0, now=now)
    verdict = _cont()
    assert verdict["state"] == "failover"
    assert verdict["seat"] == "de"


def test_unknown_current_with_no_other_seat_is_unknown_exit_3(
    seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    _configure(seats, 'codex_debate_seats = ["private"]')
    monkeypatch.setattr(cic, "_debate_cost_deltas", lambda *_a, **_k: [])
    _live(seats.seats["private"], 10.0, now=now, age=_HOUR)
    verdict = _cont()
    assert verdict["state"] == "unknown"
    assert (verdict["retry_at"], verdict["retry_seat"]) == (None, "")
    assert cic.main(["debate-seat", "-j", "-c", "private"]) == 3
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"


def test_failover_never_lands_on_a_stale_reading(pair: SeatFixture) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 95.0, now=now)
    _live(pair.seats["de"], 10.0, now=now, age=_HOUR)
    verdict = _cont()
    assert verdict["state"] == "paused"  # private's known cause (capped) makes it a pause
    assert verdict["retry_seat"] == "private"


def test_refresh_covers_the_current_seat(
    pair: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now, age=_HOUR)
    _live(pair.seats["de"], 10.0, now=now, age=_HOUR)
    path = pair.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("codex_usage = false", "codex_usage = true"),
        encoding="utf-8",
    )
    config.invalidate_config_cache()
    refreshed: list[str] = []

    def _refresh(cands: list[cic.SeatCandidate], *_a: object) -> None:
        refreshed.extend(cand.label for cand in cands)
        _live(pair.seats["private"], 40.0, now=int(time.time()))

    monkeypatch.setattr(cic, "_debate_refresh", _refresh)
    verdict = _cont()
    assert "private" in refreshed
    assert verdict["state"] == "continue"
    assert verdict["used_percent"] == 40.0


def test_tool_pin_ignores_the_inherited_codex_home(
    pair: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 95.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    monkeypatch.setenv("CODEX_HOME", str(pair.seats["private"]))
    tool = _cont(pin="tool")
    assert tool["state"] == "failover"
    assert tool["seat"] == "de"
    assert cic.os.environ["CODEX_HOME"] == str(pair.seats["private"])  # restored
    human = _cont(pin="human")
    assert human["state"] == "paused"
    assert human["pin"] == "human"
    assert human["retry_seat"] == "private"
    assert _roles(human) == {"private": "current"}  # the human pin's pool is itself
    _live(pair.seats["private"], 50.0, now=now)
    assert _cont(pin="human")["state"] == "continue"


def test_human_pin_needs_the_matching_codex_home(
    pair: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now)
    assert _cont(pin="human")["state"] == "unknown"  # no $CODEX_HOME at all
    monkeypatch.setenv("CODEX_HOME", str(pair.seats["de"]))
    verdict = _cont(pin="human")
    assert verdict["state"] == "unknown"
    assert "not the current seat" in verdict["reason"]


def test_retry_at_per_cause(pair: SeatFixture) -> None:
    now = int(time.time())
    # five-hour: the current seat over the pause point, de weekly-exhausted
    _live(pair.seats["private"], 95.0, now=now, resets_in=4 * _HOUR)
    _live_full(pair.seats["de"], 20.0, 100.0, now=now, week_resets_in=2 * _HOUR)
    verdict = _cont()
    assert verdict["state"] == "paused"
    assert (verdict["retry_at"], verdict["retry_seat"]) == (now + 2 * _HOUR, "de")
    # a hold: its own expiry
    _live(pair.seats["de"], 10.0, now=now)
    quota.record_block("codex:de", blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="h")
    verdict = _cont()
    assert (verdict["state"], verdict["retry_at"], verdict["retry_seat"]) == (
        "paused",
        now + _HOUR,
        "de",
    )


@pytest.mark.parametrize("scope", ["auth", "entitlement"])
def test_retry_at_is_null_for_auth_and_entitlement(pair: SeatFixture, scope: str) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    for pid in ("codex:private", "codex:de"):
        quota.record_block(pid, blocked_until=now + _DAY, reason=scope, scope=scope)
    verdict = _cont()
    assert verdict["state"] == "paused"  # a known cause, but no known time
    assert (verdict["retry_at"], verdict["retry_seat"]) == (None, "")


@pytest.mark.parametrize(
    ("samples", "value", "rule", "point"),
    [
        (9, 20.0, "config", 90.0),  # too few samples: the configured point
        (10, 20.0, "p95", 78.0),  # 100 - 1.1 x 20 = 78 < 90
        (10, 5.0, "p95", 90.0),  # 100 - 1.1 x 5 = 94.5: min() keeps the configured 90
        (10, 95.0, "p95", 1.0),  # clamped to >= 1
    ],
)
def test_pause_point_uses_p95_only_with_ten_samples(
    pair: SeatFixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    samples: int,
    value: float,
    rule: str,
    point: float,
) -> None:
    now = int(time.time())
    deltas = [1.0] * (samples - 1) + [value]  # nearest-rank P95 of n <= 20 is the max
    monkeypatch.setattr(cic, "_debate_cost_deltas", lambda *_a, **_k: list(deltas))
    _live(pair.seats["private"], 80.0, now=now)
    _live(pair.seats["de"], 90.0, now=now)
    verdict = _cont()
    assert (verdict["pause_rule"], verdict["pause_percent"]) == (rule, point)
    assert verdict["pause_samples"] == samples
    assert verdict["pause_config"] == 90.0
    if rule == "p95":
        assert verdict["pause_p95"] == round(100.0 - 1.1 * value, 2)
    else:
        assert verdict["pause_p95"] is None
    assert verdict["state"] == ("continue" if 80.0 < point else "paused")


def test_pause_point_survives_a_failing_history(
    pair: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> list[float]:
        raise OSError("history unreadable")

    monkeypatch.setattr(cic, "_debate_cost_deltas", _boom)
    _live(pair.seats["private"], 10.0, now=int(time.time()))
    verdict = _cont()
    assert (verdict["pause_rule"], verdict["pause_percent"]) == ("config", 90.0)


def test_pause_pct_from_config(pair: SeatFixture) -> None:
    now = int(time.time())
    _configure(pair, "codex_debate_pause_pct = 60")
    _live(pair.seats["private"], 65.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    verdict = _cont()
    assert verdict["pause_percent"] == 60.0
    assert verdict["state"] == "failover"


@pytest.mark.parametrize("value", ["0", "101", '"abc"', "true"])
def test_malformed_pause_pct_is_unknown_exit_3(
    pair: SeatFixture, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(pair, f"codex_debate_pause_pct = {value}")
    _live(pair.seats["private"], 10.0, now=int(time.time()))
    verdict = _cont()
    assert verdict["state"] == "unknown"
    assert "codex_debate_pause_pct" in verdict["reason"]
    args = argparse.Namespace(
        json=True, tier="default", repo="", continue_from="private", pin=None, exclude=[]
    )
    assert cic.cmd_debate_seat(args) == 3
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"


def test_pin_or_exclude_without_continue_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["debate-seat", "-p", "tool"], ["debate-seat", "-x", "de"]):
        with pytest.raises(SystemExit) as exc:
            cic.main(argv)
        assert exc.value.code == 2
        assert "need -c/--continue" in capsys.readouterr().err


def test_continue_cli_exit_codes(pair: SeatFixture, capsys: pytest.CaptureFixture[str]) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 50.0, now=now)
    _live(pair.seats["de"], 10.0, now=now)
    assert cic.main(["debate-seat", "-j", "-c", "private"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "continue"
    _live(pair.seats["private"], 95.0, now=now)
    assert cic.main(["debate-seat", "-c", "private", "-p", "tool"]) == 0
    assert "FAILOVER private -> de" in capsys.readouterr().out
    assert cic.main(["debate-seat", "-j", "-c", "private", "-x", "de"]) == 1
    paused = json.loads(capsys.readouterr().out)
    assert paused["state"] == "paused"
    assert cic.main(["debate-seat", "-c", "private", "--exclude", "de"]) == 1
    assert "debate seat: PAUSED" in capsys.readouterr().out


@pytest.mark.usefixtures("pair")
def test_continue_kill_switch_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCC_NO_CODEX", "1")
    assert _cont()["state"] == "disabled"
    assert cic.main(["debate-seat", "-j", "-c", "private"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "disabled"


def test_continue_crash_is_unknown_exit_3(
    pair: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(cic, "debate_continue_verdict", _boom)
    assert cic.main(["debate-seat", "-j", "-c", "private"]) == 3
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["state"] == "unknown"
    assert verdict["continue_from"] == "private"
    _ = pair


def test_continue_parser_short_flags() -> None:
    args = cic.build_parser().parse_args(
        ["debate-seat", "-c", "private", "-p", "human", "-x", "de", "-x", "default"]
    )
    assert (args.continue_from, args.pin, args.exclude) == ("private", "human", ["de", "default"])
