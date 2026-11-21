"""Tests for the Codex DEBATE start caps (tp#619): ``codex-in-claude debate-seat``.

The rule (owner decision, 2026-09-26): a debate never STARTS on a seat whose five-hour
reading is at/above the item's cap — 70 % strict, 75 % default — nor outside the allowlist.
Missing five-hour evidence (tp#2494) stays a per-seat ``unknown``, but after the refresh
such a seat is tried in routing order (codex order), the first tryable ``seats[]`` entry;
only evaluator, policy, input and crash failures remain a top-level
fail-closed ``unknown``. Mid-debate (``-c``) an unmeasured current pin is kept and a
tool pin fails over to the first tryable candidate. Guarded here: the cap boundaries,
the allowlist, an inherited ``$CODEX_HOME``, a reading from a window that has reset, a
policy that does not parse, and the first-tryable rule (start and ``-c``).
"""
# pylint: disable=too-many-lines  # one module covers the debate start, continue and reserve gates

from __future__ import annotations

import argparse
import json
import threading
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


def _assert_unmeasured_first(verdict: dict) -> None:
    """The first routed seat wins unmeasured: top-level allowed, per-seat still unknown."""
    assert verdict["state"] == "allowed"
    assert verdict["seat"] == verdict["seats"][0]["seat"]
    assert verdict["used_percent"] is None
    assert "unmeasured" in verdict["reason"]


def test_reading_from_a_reset_window_is_unknown(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 5.0, now=now, resets_in=-60)
    verdict = _verdict()
    _assert_unmeasured_first(verdict)
    assert all(seat["state"] == "unknown" for seat in verdict["seats"])


def test_old_reading_is_unknown(seats: SeatFixture) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 5.0, now=now, age=3600)
    verdict = _verdict()
    _assert_unmeasured_first(verdict)
    assert all(seat["state"] == "unknown" for seat in verdict["seats"])


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
    stale = cic.debate_seat_verdict(tier="strict", now=now)
    _assert_unmeasured_first(stale)
    assert stale["seats"][0]["state"] == "unknown"
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


def test_unknown_current_is_kept_over_a_fresh_seat(pair: SeatFixture) -> None:
    """tp#2494: an unmeasured pin continues (tried), never fails over to a measured seat."""
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now, age=_HOUR)
    _live(pair.seats["de"], 10.0, now=now)
    verdict = _cont()
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == (
        "continue",
        "private",
        None,
    )
    assert verdict["reason"] == (
        "seat private unmeasured (no fresh five-hour reading); kept in codex order"
    )
    assert verdict["seats"][0]["state"] == "unknown"  # the per-seat state is kept
    assert _roles(verdict) == {"private": "current"}


def test_unknown_current_with_no_other_seat_continues_exit_0(
    seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    _configure(seats, 'codex_debate_seats = ["private"]')
    monkeypatch.setattr(cic, "_debate_cost_deltas", lambda *_a, **_k: [])
    _live(seats.seats["private"], 10.0, now=now, age=_HOUR)
    verdict = _cont()
    assert (verdict["state"], verdict["used_percent"]) == ("continue", None)
    assert (verdict["retry_at"], verdict["retry_seat"]) == (None, "")
    assert cic.main(["debate-seat", "-j", "-c", "private"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "continue"
    assert cic.main(["debate-seat", "-c", "private"]) == 0
    assert "debate seat: continue private (5h unmeasured; seat private unmeasured" in (
        capsys.readouterr().out
    )


def test_failover_may_land_on_an_unmeasured_seat(pair: SeatFixture) -> None:
    """tp#2494: the first tryable candidate, an unmeasured one included, takes over."""
    now = int(time.time())
    _live(pair.seats["private"], 95.0, now=now)
    _live(pair.seats["de"], 10.0, now=now, age=_HOUR)
    verdict = _cont()
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == (
        "failover",
        "de",
        None,
    )
    assert verdict["reason"].endswith(
        "failover to de unmeasured (no fresh five-hour reading); tried in codex order"
    )
    assert _entry(verdict, "de")["state"] == "unknown"


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


# --------------------------------------------------------------------------- #
# The weekly reserve (tp#2495): a seat whose KNOWN seven-day reading leaves less than
# the learned weekly reserve (one full debate) is ``capped`` — at the start, for a
# failover candidate, and (at one round's worth) for the current seat mid-debate.
# --------------------------------------------------------------------------- #
_RESERVE = 16.5  # the learned weekly reserve the tests pin
_WEEKLY_FIELDS = frozenset(
    {"window", "weekly_resets_at", "weekly_reserve_source", "weekly_reserve_percent"}
) | {"weekly_used_percent"}


def _week(  # pylint: disable=too-many-arguments
    home: Path,
    five: float,
    week: float | None,
    *,
    now: int,
    age: int = 0,
    five_resets_in: int = 4 * _HOUR,
    week_resets_in: int = 3 * _DAY,
) -> None:
    """A live reading: five-hour *five* %, weekly *week* % (``None`` = no weekly window)."""
    usage._write_codex_usage(  # noqa: SLF001
        home,
        usage.Usage(
            captured_at=now - age,
            five_hour=usage.Window(used_percentage=five, resets_at=now + five_resets_in),
            seven_day=(
                None
                if week is None
                else usage.Window(used_percentage=week, resets_at=now + week_resets_in)
            ),
            live=True,
        ),
        now - age,
    )
    usage._codex_cache.clear()  # noqa: SLF001


@pytest.fixture(name="reserve_calls")
def _reserve_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Pin the learned weekly reserve to 16.5 %; the list records every lookup."""
    calls: list[int] = []

    def _reserve(minutes: int) -> tuple[float, str]:
        calls.append(minutes)
        return (_RESERVE, "learned")

    monkeypatch.setattr(cic, "_headroom_reserve", _reserve)
    return calls


@pytest.fixture(name="weekly")
def _weekly(
    pair: SeatFixture, reserve_calls: list[int], monkeypatch: pytest.MonkeyPatch
) -> SeatFixture:
    """``pair`` with the weekly reserve pinned and the routing order fixed (private, de)."""
    _ = reserve_calls
    real = cic.codex_homes_in_order

    def _ranked(now_ts: int | None = None, *, probe: bool = True) -> list[cic.SeatCandidate]:
        cands = real(now_ts, probe=probe)
        return sorted(cands, key=lambda cand: ["private", "de", "default"].index(cand.label))

    monkeypatch.setattr(cic, "codex_homes_in_order", _ranked)
    return pair


def _entry(verdict: dict, label: str) -> dict:
    return next(seat for seat in verdict["seats"] if seat["seat"] == label)


def test_weekly_reserve_caps_a_seat_and_the_next_seat_wins(
    weekly: SeatFixture, reserve_calls: list[int]
) -> None:
    """(a) 5h 20 % but 7d 99 %: capped on the weekly reset; the next seat in order wins."""
    now = int(time.time())
    _week(weekly.seats["private"], 20.0, 99.0, now=now, week_resets_in=2 * _DAY)
    _week(weekly.seats["de"], 30.0, 50.0, now=now)
    verdict = _verdict()
    assert [seat["seat"] for seat in verdict["seats"]][:2] == ["private", "de"]
    first = _entry(verdict, "private")
    assert first["state"] == "capped"
    assert first["resets_at"] == now + 2 * _DAY
    assert first["used_percent"] == 20.0
    assert (first["weekly_used_percent"], first["weekly_resets_at"]) == (99.0, now + 2 * _DAY)
    assert (first["weekly_reserve_percent"], first["weekly_reserve_source"]) == (
        _RESERVE,
        "learned",
    )
    assert first["window"] == "seven_day"
    assert first["reason"] == "7d 99 % — inside weekly reserve 16.5 %"
    assert (verdict["state"], verdict["seat"]) == ("allowed", "de")
    assert verdict["weekly_used_percent"] == 50.0
    assert verdict["window"] == ""
    assert reserve_calls == [cic._SEVEN_DAY_MINUTES]  # noqa: SLF001  # once per verdict


def test_weekly_reserve_caps_an_unmeasured_five_hour_seat(weekly: SeatFixture) -> None:
    """(b) no fresh 5h reading but a known 7d 99 %: ``capped``, never ``unknown``."""
    now = int(time.time())
    _week(weekly.seats["private"], 20.0, 99.0, now=now, age=_HOUR)
    _week(weekly.seats["de"], 20.0, 99.0, now=now, age=_HOUR)
    verdict = _verdict()
    entry = _entry(verdict, "private")
    assert entry["state"] == "capped"
    assert entry["used_percent"] is None
    assert entry["window"] == "seven_day"
    assert verdict["state"] == "capped"


@pytest.mark.parametrize("week", ["missing", "stale"])
def test_missing_or_stale_weekly_never_denies(weekly: SeatFixture, week: str) -> None:
    """(c) no weekly window, or one past its reset: fresh stays allowed, unfresh unknown."""
    now = int(time.time())
    used = None if week == "missing" else 99.0
    week_in = -60 if week == "stale" else 3 * _DAY
    _week(weekly.seats["private"], 20.0, used, now=now, week_resets_in=week_in)
    _week(weekly.seats["de"], 20.0, used, now=now, age=_HOUR, week_resets_in=week_in)
    verdict = _verdict()
    assert _entry(verdict, "private")["state"] == "allowed"
    assert _entry(verdict, "private")["weekly_used_percent"] is None
    assert _entry(verdict, "de")["state"] == "unknown"
    assert verdict["seat"] == "private"


def test_weekly_reserve_has_no_reset_is_near_exception(weekly: SeatFixture) -> None:
    """(d) 7d 99 % resetting in five minutes is still capped until the reset passes."""
    now = int(time.time())
    for label in ("private", "de"):
        _week(weekly.seats[label], 20.0, 99.0, now=now, week_resets_in=300)
    verdict = _verdict()
    assert verdict["state"] == "capped"
    assert verdict["resets_at"] == now + 300


@pytest.mark.parametrize(("week", "state"), [(83.5, "allowed"), (83.6, "capped")])
def test_weekly_reserve_boundary(weekly: SeatFixture, week: float, state: str) -> None:
    """(e) remaining == reserve (16.5 %) is eligible; 16.4 % is capped."""
    now = int(time.time())
    _week(weekly.seats["private"], 20.0, week, now=now)
    _week(weekly.seats["de"], 20.0, 99.0, now=now)
    assert _entry(_verdict(), "private")["state"] == state


@pytest.mark.parametrize("weekly_later", [True, False])
def test_both_limits_name_both_and_take_the_later_reset(
    weekly: SeatFixture, *, weekly_later: bool
) -> None:
    """(f) 5h over its cap AND 7d inside the reserve: both reasons, the LATER reset."""
    now = int(time.time())
    five_in, week_in = (2 * _HOUR, 3 * _HOUR) if weekly_later else (3 * _HOUR, 2 * _HOUR)
    for label in ("private", "de"):
        _week(
            weekly.seats[label],
            80.0,
            99.0,
            now=now,
            five_resets_in=five_in,
            week_resets_in=week_in,
        )
    verdict = _verdict()
    entry = _entry(verdict, "private")
    assert entry["state"] == "capped"
    assert entry["window"] == "both"
    assert "5h 80 % >= cap 75 %" in entry["reason"]
    assert "7d 99 % — inside weekly reserve 16.5 %" in entry["reason"]
    assert entry["resets_at"] == now + 3 * _HOUR
    assert verdict["resets_at"] == now + 3 * _HOUR


def test_weekly_capped_first_seat_keeps_the_order_with_an_unknown_next(
    weekly: SeatFixture,
) -> None:
    """(g) a weekly-capped first seat and an unmeasured second keep the routing order;
    the unmeasured second is tried (tp#2494)."""
    now = int(time.time())
    _week(weekly.seats["private"], 20.0, 99.0, now=now)
    _week(weekly.seats["de"], 20.0, 10.0, now=now, age=_HOUR)
    verdict = _verdict()
    assert [(seat["seat"], seat["state"]) for seat in verdict["seats"]][:2] == [
        ("private", "capped"),
        ("de", "unknown"),
    ]
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == ("allowed", "de", None)


def test_current_pin_uses_the_one_round_floor(weekly: SeatFixture) -> None:
    """(h) mid-debate the current seat continues while one round (16.5 / 3.3 = 5 %) fits."""
    now = int(time.time())
    floor = _RESERVE / cic._ROUNDS_PER_RESERVE  # noqa: SLF001
    _week(weekly.seats["private"], 20.0, 90.0, now=now)
    _week(weekly.seats["de"], 20.0, 10.0, now=now)
    verdict = _cont()
    assert verdict["state"] == "continue"
    assert verdict["weekly_reserve_percent"] == pytest.approx(floor)
    assert verdict["weekly_used_percent"] == 90.0
    # 97 %: 3 % left < one round → fail over to the allowed candidate
    _week(weekly.seats["private"], 20.0, 97.0, now=now, week_resets_in=_DAY)
    moved = _cont()
    assert (moved["state"], moved["seat"]) == ("failover", "de")
    cur = _entry(moved, "private")
    assert (cur["state"], cur["window"]) == ("capped", "seven_day")
    assert "inside weekly reserve 5 %" in cur["reason"]
    # no candidate left → paused until the weekly reset
    paused = _cont(exclude=["de"])
    assert paused["state"] == "paused"
    assert (paused["retry_at"], paused["retry_seat"]) == (now + _DAY, "private")
    assert paused["window"] == "seven_day"


def test_failover_candidate_inside_the_weekly_reserve_is_skipped(weekly: SeatFixture) -> None:
    """(i) a candidate with 5 % of its week left cannot take over a debate (full reserve)."""
    now = int(time.time())
    _week(weekly.seats["private"], 95.0, 10.0, now=now, five_resets_in=4 * _HOUR)
    _week(weekly.seats["de"], 20.0, 95.0, now=now, week_resets_in=2 * _HOUR)
    verdict = _cont()
    assert verdict["state"] == "paused"
    cand = _entry(verdict, "de")
    assert (cand["state"], cand["window"]) == ("capped", "seven_day")
    assert cand["weekly_reserve_percent"] == _RESERVE
    assert (verdict["retry_at"], verdict["retry_seat"]) == (now + 2 * _HOUR, "de")


def test_every_seat_in_its_reserve_exits_1_with_the_weekly_reason(
    weekly: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """(j) all seats inside the weekly reserve → ``debate-seat`` exit 1, the reason says so."""
    now = int(time.time())
    for label in ("private", "de"):
        _week(weekly.seats[label], 20.0, 99.0, now=now)
    assert cic.cmd_debate_seat(argparse.Namespace(json=False, tier="default", repo="")) == 1
    out = capsys.readouterr().out
    assert "debate seat: CAPPED" in out
    assert "inside weekly reserve 16.5 %" in out
    assert cic.main(["debate-seat", "-j"]) == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["state"] == "capped"
    assert "inside its weekly reserve" in verdict["reason"]


def test_top_level_weekly_fields_on_every_result(weekly: SeatFixture) -> None:
    """(k) start, continue, failover and paused results all carry the weekly fields."""
    now = int(time.time())
    _week(weekly.seats["private"], 20.0, 40.0, now=now)
    _week(weekly.seats["de"], 20.0, 30.0, now=now)
    start = _verdict()
    cont = _cont()
    _week(weekly.seats["private"], 95.0, 40.0, now=now)
    failover = _cont()
    paused = _cont(exclude=["de"])
    states = [result["state"] for result in (start, cont, failover, paused)]
    assert states == ["allowed", "continue", "failover", "paused"]
    for result in (start, cont, failover, paused):
        assert all(key in result for key in _WEEKLY_FIELDS)
        assert result["weekly_reserve_source"] == "learned"
    assert (start["weekly_used_percent"], start["weekly_reserve_percent"]) == (40.0, _RESERVE)
    assert start["weekly_resets_at"] == now + 3 * _DAY
    assert failover["weekly_used_percent"] == 30.0  # the target's facts
    assert failover["weekly_reserve_percent"] == _RESERVE
    assert paused["weekly_used_percent"] == 40.0  # the current seat's facts
    assert paused["window"] == "five_hour"
    # the empty shape (a refusal before any seat is judged) carries them too
    empty = cic._debate_result("unknown", "default", "x")  # noqa: SLF001
    assert all(key in empty for key in _WEEKLY_FIELDS)


# --------------------------------------------------------------------------- #
# Unmeasured seats are tried in codex order (tp#2494): the first ``allowed`` or
# ``unknown`` seat with a home wins (the rule codex-review.py relies on, tp#2515).
# --------------------------------------------------------------------------- #


def _first_tryable(
    verdict: dict, *, skip: tuple[str, ...] | list[str] = (), role: str = ""
) -> dict:
    """The tp#2494 rule: first ``seats[]`` entry with a label, a home, a tryable state."""
    seats = verdict.get("seats")
    for entry in seats if isinstance(seats, list) else []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("seat") or "")
        if not label or label in skip or not entry.get("home"):
            continue
        if role and entry.get("role") != role:
            continue
        if entry.get("state") in ("allowed", "unknown"):
            return entry
    return {}


def test_stale_seat_ahead_of_a_measured_one_wins(weekly: SeatFixture) -> None:
    now = int(time.time())
    _week(weekly.seats["private"], 5.0, 10.0, now=now, age=_HOUR)
    _week(weekly.seats["de"], 10.0, 10.0, now=now)
    verdict = _verdict()
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == (
        "allowed",
        "private",
        None,
    )
    assert verdict["reason"] == (
        "seat private unmeasured (no fresh five-hour reading); tried in codex order"
    )
    assert _entry(verdict, "private")["state"] == "unknown"
    assert _entry(verdict, "de")["state"] == "allowed"


def test_capped_seat_ahead_of_an_unmeasured_one_gives_the_unmeasured_one(
    weekly: SeatFixture,
) -> None:
    now = int(time.time())
    _week(weekly.seats["private"], 90.0, 10.0, now=now)
    _week(weekly.seats["de"], 10.0, 10.0, now=now, age=_HOUR)
    verdict = _verdict()
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == ("allowed", "de", None)
    assert _entry(verdict, "private")["state"] == "capped"


def test_unmeasured_seat_inside_its_weekly_reserve_is_skipped(weekly: SeatFixture) -> None:
    now = int(time.time())
    _week(weekly.seats["private"], 5.0, 99.0, now=now, age=_HOUR)
    _week(weekly.seats["de"], 5.0, 10.0, now=now, age=_HOUR)
    verdict = _verdict()
    assert _entry(verdict, "private")["state"] == "capped"
    assert (verdict["state"], verdict["seat"]) == ("allowed", "de")
    assert verdict["weekly_used_percent"] == 10.0  # projected through _debate_project


def test_inherited_stale_codex_home_is_tried(
    seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    _live(seats.seats["de"], 5.0, now=now, age=_HOUR)
    _live(seats.seats["private"], 5.0, now=now)
    monkeypatch.setenv("CODEX_HOME", str(seats.seats["de"]))
    verdict = _verdict()
    assert [seat["seat"] for seat in verdict["seats"]] == ["de"]
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == ("allowed", "de", None)
    assert cic.main(["debate-seat", "-j"]) == 0
    assert json.loads(capsys.readouterr().out)["seat"] == "de"
    # a forbidden pin stays a refusal, stale or not
    _live(seats.seats["default"], 5.0, now=now, age=_HOUR)
    monkeypatch.setenv("CODEX_HOME", str(seats.seats["default"]))
    assert _verdict()["state"] == "not_allowed"
    assert cic.main(["debate-seat", "-j"]) == 1
    capsys.readouterr()


def test_inherited_stale_unregistered_home_stays_not_allowed(
    seats: SeatFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adhoc = tmp_path / "adhoc"
    adhoc.mkdir()
    (adhoc / "auth.json").write_text("{}", encoding="utf-8")
    _live(adhoc, 1.0, now=int(time.time()), age=_HOUR)
    monkeypatch.setenv("CODEX_HOME", str(adhoc))
    assert _verdict()["state"] == "not_allowed"
    _ = seats


def _synthetic(seat: str, state: str, home: str = "/h") -> dict:
    return {
        "seat": seat,
        "home": home,
        "email": "",
        "state": state,
        "cap_percent": 75.0,
        "used_percent": 10.0 if state == "allowed" else None,
        "resets_at": None,
        "weekly_used_percent": None,
        "weekly_reserve_percent": 16.5,
        "weekly_reserve_source": "learned",
        "weekly_resets_at": None,
        "window": "",
        "reason": state,
    }


def test_entry_without_a_home_is_never_selected() -> None:
    seats = [_synthetic("a", "unknown", home=""), _synthetic("b", "allowed", home="")]
    verdict = cic._debate_pick(seats, "default")  # noqa: SLF001
    assert verdict["state"] != "allowed"
    assert verdict["seat"] != "b"
    seats.append(_synthetic("c", "unknown"))
    assert cic._debate_pick(seats, "default")["seat"] == "c"  # noqa: SLF001


def test_blocked_only_stays_blocked_exit_1(
    seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    _configure(seats, 'codex_debate_seats = ["private", "de"]')
    for label in ("private", "de"):
        _live(seats.seats[label], 5.0, now=now, age=_HOUR)
        quota.record_block(
            f"codex:{label}", blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="h"
        )
    assert _verdict()["state"] == "blocked"
    assert cic.main(["debate-seat", "-j"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"


@pytest.mark.parametrize(
    ("case", "state", "code"),
    [
        ("policy", "unknown", 3),
        ("tier", "unknown", 3),
        ("kill", "disabled", 1),
        ("crash", "unknown", 3),
    ],
)
def test_fail_closed_verdicts_survive_unmeasured_seats(  # pylint: disable=too-many-arguments
    seats: SeatFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    case: str,
    state: str,
    code: int,
) -> None:
    now = int(time.time())
    for label in seats.seats:
        _live(seats.seats[label], 5.0, now=now, age=_HOUR)  # every seat unmeasured
    tier = "default"
    if case == "policy":
        _configure(seats, "codex_debate_cap_strict_pct = 101")
    elif case == "tier":
        tier = "nosuchtier"
    elif case == "kill":
        monkeypatch.setenv("CCC_NO_CODEX", "1")
    else:

        def _boom(*_a: object, **_k: object) -> dict:
            raise RuntimeError("boom")

        monkeypatch.setattr(cic, "debate_seat_verdict", _boom)
    args = argparse.Namespace(json=True, tier=tier, repo="")
    assert cic.cmd_debate_seat(args) == code
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_text_output_for_an_unmeasured_winner(
    weekly: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    _week(weekly.seats["private"], 5.0, 10.0, now=now, age=_HOUR)
    _week(weekly.seats["de"], 5.0, 10.0, now=now)
    assert cic.cmd_debate_seat(argparse.Namespace(json=False, tier="default", repo="")) == 0
    out = capsys.readouterr().out
    assert "debate seat: private (5h unmeasured, default)" in out
    assert "  private: unknown — no fresh five-hour reading" in out


# One table row = (private, de) per-seat setups in routing order: a fresh reading below
# the cap, over the cap, inside the weekly reserve (fresh and unmeasured 5h), unmeasured,
# or held (blocked).
_PARITY_STATES = ("allowed", "capped", "weekly", "weekly_stale", "unknown", "blocked")


def _parity_setup(fixture: SeatFixture, label: str, setup: str, now: int) -> None:
    """Give seat *label* one of the :data:`_PARITY_STATES` setups."""
    home = fixture.seats[label]
    if setup == "allowed":
        _week(home, 10.0, 10.0, now=now)
    elif setup == "capped":
        _week(home, 90.0, 10.0, now=now)
    elif setup == "weekly":
        _week(home, 10.0, 99.0, now=now)
    elif setup == "weekly_stale":
        _week(home, 10.0, 99.0, now=now, age=_HOUR)
    elif setup == "unknown":
        _week(home, 10.0, 10.0, now=now, age=_HOUR)
    else:
        _week(home, 10.0, 10.0, now=now)
        quota.record_block(
            cic._seat_pid(label),  # noqa: SLF001
            blocked_until=now + _HOUR,
            kind=quota.KIND_HOLD,
            reason="h",
        )


@pytest.mark.parametrize("first", _PARITY_STATES)
@pytest.mark.parametrize("second", _PARITY_STATES)
def test_verdict_is_first_tryable_seat(weekly: SeatFixture, first: str, second: str) -> None:
    now = int(time.time())
    for label, setup in (("private", first), ("de", second)):
        _parity_setup(weekly, label, setup, now)
    verdict = _verdict()
    pick = _first_tryable(verdict)
    if pick:
        assert verdict["state"] == "allowed"
        assert verdict["seat"] == pick["seat"]
        assert verdict["used_percent"] == pick["used_percent"]
    else:
        assert verdict["state"] in ("capped", "blocked")
        assert cic._DEBATE_EXIT[verdict["state"]] == 1  # noqa: SLF001


def test_first_tryable_table_on_synthetic_entries() -> None:
    """Homeless and label-less entries, mixed states: ccc picks the first tryable one."""
    rows = [
        [_synthetic("a", "capped"), _synthetic("b", "unknown"), _synthetic("c", "allowed")],
        [_synthetic("a", "blocked"), _synthetic("b", "allowed")],
        [_synthetic("a", "unknown", home=""), _synthetic("b", "capped")],
        [_synthetic("", "allowed"), _synthetic("b", "unknown")],
        [_synthetic("a", "capped"), _synthetic("b", "blocked")],
        [],
    ]
    for seats in rows:
        verdict = cic._debate_pick(seats, "default")  # noqa: SLF001
        pick = _first_tryable(verdict)
        assert (verdict["state"] == "allowed") == bool(pick)
        if pick:
            assert verdict["seat"] == pick["seat"]


# --------------------------------------------------------------------------- #
# Mid-debate (tp#2494 B2): an unmeasured current pin is kept, and a tool pin fails over
# to the first TRYABLE candidate in routing order (codex-review.py relies on it, tp#2515).
# --------------------------------------------------------------------------- #


@pytest.fixture(name="trio")
def _trio(
    seats: SeatFixture, reserve_calls: list[int], monkeypatch: pytest.MonkeyPatch
) -> SeatFixture:
    """All three seats allowed, the weekly reserve pinned, routing order private, de, default."""
    _ = reserve_calls
    _configure(seats, 'codex_debate_seats = ["private", "de", "default"]')
    monkeypatch.setattr(cic, "_debate_cost_deltas", lambda *_a, **_k: [])
    real = cic.codex_homes_in_order

    def _ranked(now_ts: int | None = None, *, probe: bool = True) -> list[cic.SeatCandidate]:
        cands = real(now_ts, probe=probe)
        return sorted(cands, key=lambda cand: ["private", "de", "default"].index(cand.label))

    monkeypatch.setattr(cic, "codex_homes_in_order", _ranked)
    return seats


def test_excluded_unknown_current_fails_over_to_the_first_unmeasured_candidate(
    trio: SeatFixture,
) -> None:
    now = int(time.time())
    _week(trio.seats["private"], 10.0, 10.0, now=now, age=_HOUR)
    _week(trio.seats["de"], 10.0, 10.0, now=now, age=_HOUR)
    _week(trio.seats["default"], 10.0, 10.0, now=now)
    verdict = _cont(exclude=["private"])
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == (
        "failover",
        "de",
        None,
    )
    assert _roles(verdict) == {"private": "excluded", "de": "candidate", "default": "candidate"}
    assert _first_tryable(verdict, skip=["private"], role="candidate")["seat"] == "de"


def test_human_pin_unknown_current_continues(
    pair: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    _live(pair.seats["private"], 10.0, now=now, age=_HOUR)
    _live(pair.seats["de"], 10.0, now=now)
    monkeypatch.setenv("CODEX_HOME", str(pair.seats["private"]))
    verdict = _cont(pin="human")
    assert (verdict["state"], verdict["seat"], verdict["used_percent"]) == (
        "continue",
        "private",
        None,
    )
    assert _roles(verdict) == {"private": "current"}


def test_unmeasured_current_inside_the_one_round_floor_does_not_continue(
    weekly: SeatFixture,
) -> None:
    """A known 7d 97 % leaves 3 % < one round (5 %): ``capped``, even with no fresh 5h."""
    now = int(time.time())
    _week(weekly.seats["private"], 10.0, 97.0, now=now, age=_HOUR, week_resets_in=_DAY)
    _week(weekly.seats["de"], 10.0, 10.0, now=now)
    moved = _cont()
    cur = _entry(moved, "private")
    assert (cur["state"], cur["window"], cur["used_percent"]) == ("capped", "seven_day", None)
    assert (moved["state"], moved["seat"]) == ("failover", "de")
    paused = _cont(exclude=["de"])
    assert paused["state"] == "paused"
    assert (paused["retry_at"], paused["retry_seat"]) == (now + _DAY, "private")


def test_all_candidates_capped_or_blocked_still_pauses_with_retry_at(trio: SeatFixture) -> None:
    now = int(time.time())
    _week(trio.seats["private"], 95.0, 10.0, now=now, five_resets_in=4 * _HOUR)
    _week(trio.seats["de"], 80.0, 10.0, now=now, five_resets_in=2 * _HOUR)
    _week(trio.seats["default"], 10.0, 10.0, now=now)
    quota.record_block("codex", blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="h")
    verdict = _cont()
    assert verdict["state"] == "paused"
    assert [_entry(verdict, label)["state"] for label in ("private", "de", "default")] == [
        "capped",
        "capped",
        "blocked",
    ]
    assert (verdict["retry_at"], verdict["retry_seat"]) == (now + _HOUR, "default")
    assert _first_tryable(verdict, skip=["private"], role="candidate") == {}


# The current seat's setups: the parity states plus an unmeasured seat the caller excluded.
_CURRENT_STATES = ("allowed", "unknown", "unknown_excluded", "capped", "weekly_stale", "blocked")


@pytest.mark.parametrize("current", _CURRENT_STATES)
@pytest.mark.parametrize("second", _PARITY_STATES)
@pytest.mark.parametrize("third", _PARITY_STATES)
def test_continue_verdict_is_first_tryable_candidate(
    trio: SeatFixture, current: str, second: str, third: str
) -> None:
    """``-c``: the pin is kept when tryable, else ccc fails over to what
    ``_first_tryable(role="candidate")`` picks, else it pauses (exit 1)."""
    now = int(time.time())
    excluded = current == "unknown_excluded"
    _parity_setup(trio, "private", "unknown" if excluded else current, now)
    _parity_setup(trio, "de", second, now)
    _parity_setup(trio, "default", third, now)
    verdict = _cont(exclude=["private"] if excluded else [])
    cur = _entry(verdict, "private")
    if cur["role"] == "current" and cur["state"] in ("allowed", "unknown"):
        assert (verdict["state"], verdict["seat"]) == ("continue", "private")
        assert verdict["used_percent"] == cur["used_percent"]
        return
    pick = _first_tryable(verdict, skip=["private"], role="candidate")
    if pick:
        assert (verdict["state"], verdict["seat"]) == ("failover", pick["seat"])
        assert verdict["used_percent"] == pick["used_percent"]
    else:
        assert verdict["state"] == "paused"
        assert cic._DEBATE_CONTINUE_EXIT["paused"] == 1  # noqa: SLF001


# --------------------------------------------------------------------------- #
# tp#2513 B3: ``_debate_refresh`` fetches in parallel daemon threads under ONE deadline.
# --------------------------------------------------------------------------- #
_SHRUNK_BUDGET = 0.2


def _opt_in(fixture: SeatFixture) -> None:
    """Turn the live-usage opt-in (``codex_usage``) on in the fixture's config."""
    path = fixture.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("codex_usage = false", "codex_usage = true"),
        encoding="utf-8",
    )
    config.invalidate_config_cache()


def _thread_spy(monkeypatch: pytest.MonkeyPatch) -> list[threading.Thread]:
    """Record every thread ``_debate_refresh`` creates (in creation order)."""
    created: list[threading.Thread] = []
    real = threading.Thread

    class _Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(cic.threading, "Thread", _Spy)
    return created


def test_refresh_returns_at_the_deadline_and_the_seat_stays_unknown(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    for label in three_seats.seats:
        _live(three_seats.seats[label], 5.0, now=now, age=_HOUR)
    _opt_in(three_seats)
    release = threading.Event()
    daemons: list[bool] = []

    def _blocked(_home: Path, _now: int | None = None, *, timeout: float | None = None) -> None:
        daemons.append(threading.current_thread().daemon)
        assert timeout is not None
        release.wait(30)

    monkeypatch.setattr(usage, "fetch_codex_usage", _blocked)
    monkeypatch.setattr(cic, "_DEBATE_REFRESH_BUDGET_SEC", _SHRUNK_BUDGET)
    created = _thread_spy(monkeypatch)
    cands = cic.codex_homes_in_order(now)
    try:
        start = time.monotonic()
        cic._debate_refresh(cands, 600, _SHRUNK_BUDGET)  # noqa: SLF001
        assert time.monotonic() - start < _SHRUNK_BUDGET + 0.5
        assert [worker.name for worker in created] == [
            f"debate-refresh-{cand.label}" for cand in cands
        ]
        assert all(worker.daemon for worker in created)
        assert daemons and all(daemons)
        start = time.monotonic()
        verdict = cic.debate_seat_verdict(tier="strict", now=now)
        assert time.monotonic() - start < _SHRUNK_BUDGET + 2.0
    finally:
        release.set()
        for worker in created:
            worker.join(5)
    assert not any(worker.is_alive() for worker in created)
    _assert_unmeasured_first(verdict)
    assert verdict["seats"][0]["state"] == "unknown"
    assert [seat["seat"] for seat in verdict["seats"]] == [cand.label for cand in cands]


def test_refresh_fetches_run_in_parallel(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    budget = 0.5
    done: list[str] = []
    never = threading.Event()

    def _slow(home: Path, _now: int | None = None, *, timeout: float | None = None) -> None:
        never.wait(0.6 * budget)  # 60 % of the budget: three in a row would overrun it
        done.append(str(home))
        assert timeout is not None

    monkeypatch.setattr(usage, "fetch_codex_usage", _slow)
    created = _thread_spy(monkeypatch)
    cands = cic.codex_homes_in_order(now)
    assert len(cands) == 3
    cic._debate_refresh(cands, 600, budget)  # noqa: SLF001
    for worker in created:
        worker.join(5)
    assert sorted(done) == sorted(str(cand.home) for cand in cands)
    _ = three_seats


def test_fresh_seat_starts_no_thread(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    _live(three_seats.seats["private"], 10.0, now=now)
    _live(three_seats.seats["de"], 10.0, now=now, age=_HOUR)
    _live(three_seats.seats["default"], 10.0, now=now, age=_HOUR)
    fetched: list[str] = []

    def _fetch(home: Path, _now: int | None = None, *, timeout: float | None = None) -> None:
        fetched.append(str(home))
        assert timeout is not None

    monkeypatch.setattr(usage, "fetch_codex_usage", _fetch)
    created = _thread_spy(monkeypatch)
    cic._debate_refresh(cic.codex_homes_in_order(now), 600, 5.0)  # noqa: SLF001
    for worker in created:
        worker.join(5)
    assert sorted(worker.name for worker in created) == [
        "debate-refresh-de",
        "debate-refresh-default",
    ]
    assert str(three_seats.seats["private"]) not in fetched
    for label in three_seats.seats:
        _live(three_seats.seats[label], 10.0, now=now)
    created.clear()
    cic._debate_refresh(cic.codex_homes_in_order(now), 600, 5.0)  # noqa: SLF001
    assert not created


def test_no_opt_in_or_kill_switch_starts_no_thread(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    for label in three_seats.seats:
        _live(three_seats.seats[label], 10.0, now=now, age=_HOUR)
    _configure(three_seats, 'codex_debate_seats = ["private", "de"]')
    monkeypatch.setattr(cic, "_debate_cost_deltas", lambda *_a, **_k: [])
    monkeypatch.setattr(
        usage, "fetch_codex_usage", lambda *_a, **_k: pytest.fail("fetch without opt-in")
    )
    created = _thread_spy(monkeypatch)
    cic.debate_seat_verdict(tier="default", now=now)
    _cont()
    assert not created
    _opt_in(three_seats)
    monkeypatch.setenv("CCC_NO_CODEX", "1")
    assert cic.debate_seat_verdict(tier="default", now=now)["state"] == "disabled"
    assert _cont()["state"] == "disabled"
    assert not created
