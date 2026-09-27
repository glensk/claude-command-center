"""Tests for the Codex DEBATE start caps (tp#619): ``codex-in-claude debate-seat``.

The rule (Albert, 2026-09-26): a debate may START only on an allowed seat whose FRESH
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
