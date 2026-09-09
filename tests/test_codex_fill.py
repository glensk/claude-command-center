"""Tests for the Codex seat ROUTING POLICY `fill` (tp#212, PLAN_codex-seat-fill.md).

The rule set on 2026-09-09 is "the closer a seat comes to its weekly reset, the more we
must make sure it is used up — never waste tokens", and the failure it replaces
was concrete: with a strict order, a completely fresh team seat sat at 0 %/0 % while
`headroom` answered DENIED (unknown) because it only ever read ONE home's rollout files.

What is guarded here, each because its failure mode is silent:

* the cohort rule, which is what keeps "resets soonest" from picking a seat with nothing
  left: 92 % used resetting in 2 h beats 0 % used resetting in 20 h;
* filling a cohort EQUALLY — a 5 % usage bucket plus the attempt ledger, because two
  short runs in a row leave no new measurement to re-rank on;
* one read-only probe per unmeasured seat per day, CLAIMED under a lock so two runners
  never probe the same seat;
* the offload gate answering for the whole seat POOL;
* the runner's write floor, `--headroom` filter and quota-row preflight.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import SeatFixture

from command_center import cli, config, quota, usage
from command_center import codex_in_claude as cic

_NOW = 1_800_000_000
_HOUR = 3600
_DAY = 86400


def _win(name: str, used_pct: float, resets_in: int, *, stale: bool = False) -> quota.WindowState:
    """One resolved window, `resets_in` seconds from `_NOW`."""
    return quota.WindowState(name=name, used_pct=used_pct, resets_at=_NOW + resets_in, stale=stale)


def _row(
    label: str,
    *,
    week: tuple[float, int] | None = None,
    five: tuple[float, int] | None = None,
    state: str = quota.AVAILABLE,
    stale_week: bool = False,
) -> quota.ProviderQuota:
    """A Codex row with hand-built windows — the ranker's whole input."""
    windows: dict[str, quota.WindowState] = {}
    if five is not None:
        windows["five_hour"] = _win("five_hour", five[0], five[1])
    if week is not None:
        windows["seven_day"] = _win("seven_day", week[0], week[1], stale=stale_week)
    pid = "codex" if label == "default" else f"codex:{label}"
    return quota.ProviderQuota(
        id=pid, kind="codex", state=state, account=label, windows=windows, captured_at=_NOW
    )


def _rank(
    rows: list[quota.ProviderQuota],
    *,
    policy: str = "fill",
    now: int = _NOW,
    attempts: dict[str, int] | None = None,
    pin: str = "",
    order: list[str] | None = None,
    probe: bool = True,
) -> list[quota.SeatRank]:
    """`rank_codex_seats` with the clock and the ledger injected — a PURE call."""
    return quota.rank_codex_seats(
        rows,
        pin,
        ["private", "de", "default"] if order is None else order,
        policy=policy,
        now=now,
        attempts={} if attempts is None else attempts,
        probe=probe,
    )


def _labels(ranks: list[quota.SeatRank]) -> list[str]:
    return [rank.row.account for rank in ranks]


# ── the cohort rule ───────────────────────────────────────────────────────────────
def test_a_nearly_full_seat_resetting_in_two_hours_beats_an_empty_one_in_twenty() -> None:
    """Codex's counterexample (debate O1): urgency is the reset, not the remainder.

    92 % used with 2 h to go is 8 % about to evaporate; 0 % used with 20 h to go loses
    nothing by waiting. They are in DIFFERENT cohorts, so the reset decides.
    """
    rows = [_row("private", week=(92.0, 2 * _HOUR)), _row("de", week=(0.0, 20 * _HOUR))]
    ranks = _rank(rows)
    assert _labels(ranks) == ["private", "de"]
    assert [rank.cohort for rank in ranks] == [1, 2]
    assert "weekly resets in 2h" in ranks[0].reason


def test_cohorts_group_resets_within_twelve_hours_and_fill_the_least_used_first() -> None:
    rows = [
        _row("private", week=(40.0, 1 * _HOUR)),
        _row("de", week=(10.0, 10 * _HOUR)),
        _row("default", week=(0.0, 30 * _HOUR)),
    ]
    ranks = _rank(rows)
    # +1h and +10h are 9h apart => one cohort; +30h opens the next.
    assert [rank.cohort for rank in ranks] == [1, 1, 2]
    assert _labels(ranks) == ["de", "private", "default"]  # inside cohort 1: less used first

    # …and it is genuinely "fill equally", not a fixed ranking: push `de` past `private`
    # and the pair swaps back.
    rows[1] = _row("de", week=(70.0, 10 * _HOUR))
    assert _labels(_rank(rows))[:2] == ["private", "de"]


def test_a_session_window_about_to_renew_unused_leads_its_cohort() -> None:
    """A 5h allowance renewing in 40 min with 80 % unused is spent first (plan D2)."""
    rows = [
        _row("private", week=(20.0, 5 * _DAY)),
        _row("de", week=(20.0, 5 * _DAY), five=(20.0, 40 * 60)),
    ]
    ranks = _rank(rows)
    assert _labels(ranks) == ["de", "private"]
    assert "session renews in 40m, 80% unused" in ranks[0].reason
    # a 5h window that is already 70 % spent has nothing left to waste
    rows[1] = _row("de", week=(20.0, 5 * _DAY), five=(70.0, 40 * 60))
    assert _labels(_rank(rows)) == ["private", "de"]


def test_identical_windows_fall_back_to_the_configured_order() -> None:
    rows = [_row("default", week=(20.0, _DAY)), _row("de", week=(20.0, _DAY))]
    assert _labels(_rank(rows, order=["de", "default"])) == ["de", "default"]
    assert _labels(_rank(rows, order=["default", "de"])) == ["default", "de"]


def test_five_percent_buckets_hand_the_decision_to_the_attempt_ledger() -> None:
    """Two seats within 5 % are equally used: the OLDEST attempt goes next (debate O2)."""
    rows = [_row("private", week=(20.0, _DAY)), _row("de", week=(23.0, _DAY))]
    attempts = {"codex:private": _NOW - 60, "codex:de": _NOW - 7200}
    ranks = _rank(rows, attempts=attempts)
    assert _labels(ranks) == ["de", "private"]  # de was billed longer ago
    assert "last attempt 2h ago" in ranks[0].reason
    # a difference bigger than one bucket is a real difference again
    rows[1] = _row("de", week=(40.0, _DAY))
    assert _labels(_rank(rows, attempts=attempts)) == ["private", "de"]


def test_unmeasured_seats_rank_last_and_blocked_ones_not_at_all() -> None:
    rows = [
        _row("private"),  # no windows at all
        _row("de", week=(50.0, _DAY)),
        _row("default", week=(0.0, _DAY), state=quota.BLOCKED),
    ]
    ranks = _rank(rows, probe=False)
    assert _labels(ranks) == ["de", "private"]
    assert ranks[1].reason == "unmeasured" and ranks[1].measured is False


def test_a_stale_weekly_window_is_unmeasured_not_a_zero() -> None:
    """A reading whose window already reset proves nothing — never a 'fresh 0 %' seat."""
    rows = [
        _row(
            "private",
            week=(
                0.0,
                -_HOUR,
            ),
            stale_week=True,
        ),
        _row("de", week=(80.0, _DAY)),
    ]
    assert _labels(_rank(rows, probe=False)) == ["de", "private"]


def test_the_order_policy_ignores_cohorts_entirely() -> None:
    rows = [_row("default", week=(90.0, 2 * _HOUR)), _row("de", week=(0.0, 30 * _HOUR))]
    ranks = _rank(rows, policy="order", order=["de", "default"])
    assert _labels(ranks) == ["de", "default"]
    assert [rank.reason for rank in ranks] == ["order", "order"]
    assert [rank.cohort for rank in ranks] == [None, None]


def test_the_pin_leads_regardless_of_cohorts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "codex_seat_order", lambda: ["de", "default"])
    rows = [_row("default", week=(90.0, _HOUR)), _row("de", week=(0.0, 30 * _HOUR))]
    ranks = _rank(rows, pin="de")
    assert _labels(ranks) == ["de", "default"]
    assert ranks[0].reason == "pin" and ranks[0].cohort is None


# ── the probe ─────────────────────────────────────────────────────────────────────
def test_one_unmeasured_seat_is_promoted_to_a_daily_probe() -> None:
    rows = [_row("private", week=(10.0, _DAY)), _row("de"), _row("default")]
    ranks = _rank(rows, order=["private", "de", "default"])
    assert _labels(ranks) == ["de", "private", "default"]  # exactly ONE probe (debate O9)
    assert ranks[0].probe is True
    assert ranks[0].reason == "probe: unmeasured, no attempt in 24h"
    assert [rank.probe for rank in ranks[1:]] == [False, False]


def test_a_recently_attempted_seat_is_not_probed_again() -> None:
    rows = [_row("private", week=(10.0, _DAY)), _row("de")]
    assert _labels(_rank(rows, attempts={"codex:de": _NOW - _HOUR})) == ["private", "de"]
    # …and a day later it is due again
    assert _labels(_rank(rows, attempts={"codex:de": _NOW - 25 * _HOUR})) == ["de", "private"]


def test_probe_false_never_promotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write runs pass probe=False: an unmeasured seat is an experiment (plan D5/D7)."""
    rows = [_row("private", week=(10.0, _DAY)), _row("de")]
    assert _labels(_rank(rows, probe=False)) == ["private", "de"]
    _ = monkeypatch


def test_the_pin_is_never_also_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "codex_seat_order", lambda: [])
    rows = [_row("private"), _row("de")]
    ranks = _rank(rows, pin="private")
    assert _labels(ranks) == ["private", "de"]
    assert ranks[0].reason == "pin"
    assert ranks[1].probe is True  # the probe goes to the OTHER unmeasured seat


# ── the attempt ledger ────────────────────────────────────────────────────────────
def test_seat_attempts_round_trip_and_survive_a_corrupt_file(three_seats: SeatFixture) -> None:
    assert quota.read_seat_attempts() == {}
    quota.record_seat_attempt("codex:de", now=_NOW)
    quota.record_seat_attempt("", now=_NOW)  # an unregistered home records nothing
    assert quota.read_seat_attempts() == {"codex:de": _NOW}
    quota._seat_attempts_path().write_text("{not json", encoding="utf-8")  # noqa: SLF001
    assert quota.read_seat_attempts() == {}
    _ = three_seats


def test_claim_probe_is_won_exactly_once_per_interval(three_seats: SeatFixture) -> None:
    """Two runners racing for one fresh seat must produce ONE probe (plan D7)."""
    assert quota.claim_probe("codex:de", now=_NOW) is True
    assert quota.claim_probe("codex:de", now=_NOW) is False  # the loser re-ranks
    assert quota.read_seat_attempts()["codex:de"] == _NOW  # the claim IS the record
    assert quota.claim_probe("codex:de", now=_NOW + 25 * _HOUR) is True
    assert quota.claim_probe("", now=_NOW) is False
    _ = three_seats


# ── refusal expiry (plan D8, debate O12) ──────────────────────────────────────────
def _refused_seat(tmp_path: Path, *, age: int, reset_in: int | None) -> tuple[Path, usage.Usage]:
    """A seat home plus a snapshot carrying a stapled refusal `age` seconds old."""
    home = tmp_path / "seat"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("{}", encoding="utf-8")
    five = (
        usage.Window(used_percentage=100.0, resets_at=_NOW + reset_in)
        if reset_in is not None
        else None
    )
    snap = usage.Usage(
        captured_at=_NOW - age,
        five_hour=five,
        seven_day=None,
        blocked_reason="included usage limit reached (no credit overflow)",
        blocked_at=_NOW - age,
    )
    return home, snap


def test_a_refusal_expires_once_its_window_has_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An 8-day-old refusal whose windows all reset must not hold a paid seat forever."""
    home, snap = _refused_seat(tmp_path, age=8 * _DAY, reset_in=-7 * _DAY)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: snap)
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: None)
    row = quota._codex_seat_quota("codex:de", "de", home, _NOW, {})  # noqa: SLF001
    assert row.state == quota.UNKNOWN  # eligible again, and remeasurable
    assert "refusal 8d old — remeasure" in row.note
    assert quota.codex_seat_candidates([row], "", ["de"], policy="fill", now=_NOW)


def test_a_refusal_whose_window_resets_in_two_hours_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, snap = _refused_seat(tmp_path, age=600, reset_in=2 * _HOUR)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: snap)
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: None)
    row = quota._codex_seat_quota("codex:de", "de", home, _NOW, {})  # noqa: SLF001
    assert row.state == quota.BLOCKED
    assert row.resets_at == _NOW + 2 * _HOUR


def test_a_windowless_refusal_expires_after_five_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, snap = _refused_seat(tmp_path, age=6 * _HOUR, reset_in=None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: snap)
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: None)
    assert quota._codex_seat_quota("codex:de", "de", home, _NOW, {}).state == (  # noqa: SLF001
        quota.UNKNOWN
    )
    home, fresh = _refused_seat(tmp_path, age=2 * _HOUR, reset_in=None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: fresh)
    assert quota._codex_seat_quota("codex:de", "de", home, _NOW, {}).state == (  # noqa: SLF001
        quota.BLOCKED
    )


# ── select_codex_account + snapshot ───────────────────────────────────────────────
def test_select_codex_account_returns_the_fill_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "codex_seat_policy", lambda: "fill")
    monkeypatch.setattr(quota, "read_seat_attempts", dict)
    rows = [_row("default", week=(10.0, 30 * _HOUR)), _row("private", week=(90.0, 2 * _HOUR))]
    assert quota.select_codex_account(rows, "") == "codex:private"


def test_snapshot_carries_the_policy_and_the_fill_fields(three_seats: SeatFixture) -> None:
    """`ccc quota -j` gains cohort/measured/probe/rank_reason — additively (plan D9)."""
    now = int(time.time())
    for label, used in (("private", 10.0), ("de", 60.0), ("default", 30.0)):
        usage._write_codex_usage(  # noqa: SLF001
            three_seats.seats[label],
            usage.Usage(
                captured_at=now,
                five_hour=usage.Window(used_percentage=used, resets_at=now + _HOUR),
                seven_day=usage.Window(used_percentage=used, resets_at=now + 3 * _DAY),
                live=True,
            ),
            now,
        )
    usage._codex_cache.clear()  # noqa: SLF001
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": None})
    snap = quota.snapshot(now=now)
    rows = {row["label"]: row for row in snap["codex_seat_order"]}
    assert snap["codex_seat_policy"] == "fill"
    assert [row["label"] for row in snap["codex_seat_order"]] == ["private", "de", "default"]
    assert rows["de"]["attempt_rank"] == 1 and rows["de"]["rank_reason"] == "pin"
    assert all(row["measured"] for row in rows.values())
    assert rows["private"]["cohort"] == 1 and "weekly resets in" in rows["private"]["rank_reason"]
    assert rows["private"]["malformed"] is False
    assert snap["codex_pin"] == {"account": "de", "until": ""}


# ── the headroom POOL (plan D4) ───────────────────────────────────────────────────
def _live(home: Path, *, five: float, week: float, now: int, age: int = 0) -> None:
    """Write a live usage cache for one seat."""
    usage._write_codex_usage(  # noqa: SLF001
        home,
        usage.Usage(
            captured_at=now - age,
            five_hour=usage.Window(used_percentage=five, resets_at=now + 4 * _HOUR),
            seven_day=usage.Window(used_percentage=week, resets_at=now + 3 * _DAY),
            live=True,
        ),
        now - age,
    )
    usage._codex_cache.clear()  # noqa: SLF001


def test_headroom_allows_via_a_fresh_seat_while_the_others_are_blocked(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tp#212 bug itself: one idle 0 %/0 % seat must make the gate say ALLOWED."""
    now = int(time.time())
    for pid in ("codex:private", "codex:de"):
        quota.record_block(pid, blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="held")
    _live(three_seats.seats["default"], five=0.0, week=0.0, now=now)
    decision = cic.codex_headroom(now=now)
    assert decision["state"] == "allowed"
    assert decision["seat"] == "default"
    assert decision["reason"] == "seat default has headroom"
    assert {seat["label"] for seat in decision["seats"]} == {"default"}  # blocked = not a candidate
    assert cic.cmd_headroom(argparse.Namespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "seat: default (team@example.org)" in out
    assert "offload: ALLOWED" in out


def test_headroom_reports_reserve_when_every_seat_is_inside_it(three_seats: SeatFixture) -> None:
    now = int(time.time())
    for label in three_seats.seats:
        _live(three_seats.seats[label], five=90.0, week=90.0, now=now)
    decision = cic.codex_headroom(now=now)
    assert decision["state"] == "reserve"
    assert decision["reason"] == "every measurable seat is inside its reserve"
    assert len(decision["seats"]) == 3


def test_headroom_is_unknown_without_data_and_blocked_without_a_seat(
    three_seats: SeatFixture,
) -> None:
    now = int(time.time())
    assert cic.codex_headroom(now=now)["state"] == "unknown"  # no snapshots anywhere
    for pid in ("codex:private", "codex:de", "codex"):
        quota.record_block(pid, blocked_until=now + _HOUR, kind=quota.KIND_HOLD, reason="held")
    decision = cic.codex_headroom(now=now)
    assert decision["state"] == "blocked"
    assert decision["reason"] == "no eligible seat"
    assert decision["seat"] == "" and decision["seats"] == []


def test_headroom_is_blocked_and_seatless_under_the_kill_switch(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCC_NO_CODEX", "1")
    decision = cic.codex_headroom(now=int(time.time()))
    assert decision["state"] == "blocked"
    assert decision["reason"] == "Codex disabled (CCC_NO_CODEX)"
    _ = three_seats


def test_headroom_fails_closed_on_a_malformed_window(three_seats: SeatFixture) -> None:
    """A window whose duration could not be read may be the very one that is full."""
    now = int(time.time())
    day = three_seats.seats["private"] / "sessions" / "2026" / "09" / "09"
    day.mkdir(parents=True, exist_ok=True)
    (day / "rollout-malformed.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": now,
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 5.0, "resets_at": now + _HOUR},
                        "secondary": None,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    usage._codex_cache.clear()  # noqa: SLF001
    row = quota._codex_seat_quota(  # noqa: SLF001
        "codex:private", "private", three_seats.seats["private"], now, {}
    )
    assert row.malformed is True
    verdict = cic.seat_headroom(row, now)
    assert verdict["state"] == "unknown"
    assert verdict["reason"] == "usage snapshot contains a malformed window"
    # routing still fails OPEN on the same row — the asymmetry is deliberate
    assert quota.codex_seat_candidates([row], "", ["private"], policy="fill", now=now)


def test_headroom_evaluates_an_unregistered_explicit_home(
    three_seats: SeatFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`$CODEX_HOME` narrows the pool to one seat — including one ccc does not know."""
    now = int(time.time())
    adhoc = tmp_path / "adhoc"
    adhoc.mkdir()
    (adhoc / "auth.json").write_text("{}", encoding="utf-8")
    _live(adhoc, five=1.0, week=1.0, now=now)
    monkeypatch.setenv("CODEX_HOME", str(adhoc))
    decision = cic.codex_headroom(now=now)
    assert decision["state"] == "allowed"
    assert [seat["label"] for seat in decision["seats"]] == ["explicit"]
    _ = three_seats


# ── the runner ────────────────────────────────────────────────────────────────────
def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": "gpt-5.6-sol",
        "effort": "low",
        "timeout": 60,
        "idle_timeout": 0,
        "purpose": "test",
        "max_attempts": 0,
        "persist": False,
        "ignore_quota": False,
        "ephemeral": False,
        "headroom": False,
        "min_remaining": None,
        "json": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _delegate_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "prompt": "do the thing",
        "write": False,
        "scout": False,
        "cwd": str(seats.workdir),
        "round": 1,
        "feedback": None,
        "model": "gpt-5.6-sol",
        "purpose": "delegate",
        "effort": "low",
        "timeout": 60,
        "idle_timeout": 0,
        "max_concurrent": 0,
        "resume": None,
        "no_repo_map": True,
        "repo_map": None,
        "show_prompt": False,
        "max_attempts": 0,
        "ignore_quota": False,
        "headroom": False,
        "min_remaining": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline — `valid_slug` must not shell out to codex."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [
            {"slug": "gpt-5.6-sol", "visibility": "list", "default_reasoning_level": "medium"}
        ],
    )


def test_a_rising_weekly_figure_moves_the_next_run_to_the_twin_seat(
    three_seats: SeatFixture,
) -> None:
    """With the opt-in OFF the rollout is the measurement — and it re-ranks the pair."""
    now = int(time.time())
    three_seats.reorder("private", "de")
    week = now + 3 * _DAY
    three_seats.scenarios(
        private={"scenario": "ok_rollout", "used_pct": 60, "week_resets_at": week},
        de={"scenario": "ok_rollout", "used_pct": 5, "week_resets_at": week},
        default={"scenario": "ok_rollout", "used_pct": 5, "week_resets_at": week},
    )
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    usage._codex_cache.clear()  # noqa: SLF001
    assert "--ephemeral" not in three_seats.calls()[0]["argv"]  # the rollout must survive
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    homes = three_seats.call_homes()
    assert homes[0] != homes[1]  # the seat that measured 60 % is no longer the leader
    assert homes[1] in ("de", "default")


def test_unmeasurable_runs_alternate_through_the_attempt_ledger(
    three_seats: SeatFixture,
) -> None:
    """A short exec writes a WINDOWLESS block: no measurement, so round-robin decides."""
    three_seats.reorder("private", "de", "default")
    three_seats.scenarios(private="premium_only", de="premium_only", default="premium_only")
    for _ in range(3):
        assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
        usage._codex_cache.clear()  # noqa: SLF001
    assert len(set(three_seats.call_homes())) == 3  # every seat billed once, not one thrice


def test_a_probe_happens_once_a_day_not_once_a_run(three_seats: SeatFixture) -> None:
    """An unmeasured seat is worth one read-only experiment per day (plan D7)."""
    three_seats.scenarios(private="premium_only", de="premium_only", default="premium_only")
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    first = three_seats.call_homes()[0]
    attempts = quota.read_seat_attempts()
    assert len(attempts) == 1  # exactly one physical attempt was recorded
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    assert three_seats.call_homes()[1] != first  # the claimed seat is not re-probed today


def test_the_attempt_is_recorded_before_the_seat_answers(three_seats: SeatFixture) -> None:
    three_seats.scenarios(private={"scenario": "ok", "reply": "hi"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    billed = three_seats.call_homes()[0]
    pid = "codex" if billed == "default" else f"codex:{billed}"
    assert pid in quota.read_seat_attempts()


def test_headroom_mode_skips_a_seat_inside_its_reserve(three_seats: SeatFixture) -> None:
    """`-H` filters every candidate through the same per-seat verdict `headroom` prints."""
    now = int(time.time())
    three_seats.reorder("private", "de", "default")
    _live(three_seats.seats["private"], five=99.0, week=99.0, now=now)
    _live(three_seats.seats["de"], five=5.0, week=5.0, now=now)
    _live(three_seats.seats["default"], five=5.0, week=5.0, now=now)
    # Pin `private` so it really is candidate 1: the fill ranking would otherwise put
    # the two near-empty seats first, and the filter under test would never fire.
    cic.save_config({"codex_home": str(three_seats.seats["private"]), "codex_home_until": None})
    result = cic.run_with_fallback(
        prompt="hi",
        build_cmd=lambda cand, out, perm, mcp: _fake_cmd(cand, out, perm, mcp, three_seats),
        write=False,
        workdir=str(three_seats.workdir),
        total_timeout=60,
        idle_timeout=0,
        purpose="test",
        model="gpt-5.6-sol",
        effort="low",
        heartbeat_meta={},
        headroom=True,
    )
    assert result.ok is True
    assert result.attempts[0].outcome == "skipped:reserve"
    assert result.attempts[0].seat == "private"
    assert result.seat is not None and result.seat.label in ("de", "default")


def _fake_cmd(
    cand: cic.SeatCandidate,
    out_path: str,
    perm_args: list[str],
    mcp_args: list[str],
    seats: SeatFixture,
) -> list[str]:
    """The minimal read-only argv the fake codex understands."""
    from command_center import codex_launch

    return [
        codex_launch.resolve_codex(),
        "exec",
        "--json",
        *perm_args,
        *mcp_args,
        "-o",
        out_path,
        "-C",
        str(seats.workdir),
        "--skip-git-repo-check",
        "-",
    ]


def test_a_write_run_skips_unmeasured_seats_and_seats_below_the_floor(
    three_seats: SeatFixture,
) -> None:
    now = int(time.time())
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    three_seats.reorder("private", "de")  # only `de` declares hardened-rw
    _live(three_seats.seats["private"], five=1.0, week=97.0, now=now)  # measured, nearly full
    _live(three_seats.seats["de"], five=1.0, week=1.0, now=now)
    three_seats.scenarios(de={"scenario": "ok", "reply": "de wrote"})
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True, min_remaining=10.0)) == cic.EX_OK
    outcomes = {call: None for call in three_seats.call_homes()}
    assert list(outcomes) == ["de"]  # private was below the floor, default unmeasured


def test_a_write_run_refuses_a_seat_with_no_weekly_reading(three_seats: SeatFixture) -> None:
    """`skipped:unmeasured`: a seat that may refuse mid-edit costs a worktree review.

    The skip exists so the write floor can be checked; `-F 0` (no floor) therefore
    waives it too — otherwise a fresh install with no usage data anywhere could never
    start a write run.
    """
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    three_seats.reorder("de", "private", "default")
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True)) == cic.EX_QUOTA
    assert three_seats.calls() == []
    three_seats.scenarios(de={"scenario": "ok", "reply": "de wrote"})
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True, min_remaining=0.0)) == cic.EX_OK
    assert three_seats.call_homes() == ["de"]


def test_min_remaining_zero_disables_the_write_floor(three_seats: SeatFixture) -> None:
    now = int(time.time())
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    three_seats.reorder("de", "private", "default")
    _live(three_seats.seats["de"], five=1.0, week=99.0, now=now)  # inside every floor
    three_seats.scenarios(de={"scenario": "ok", "reply": "de wrote"})
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True)) == cic.EX_QUOTA
    assert three_seats.calls() == []
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True, min_remaining=0.0)) == cic.EX_OK
    assert three_seats.call_homes() == ["de"]


def test_a_newer_live_reading_beats_an_old_exhausted_rollout(three_seats: SeatFixture) -> None:
    """The preflight regression debate O4 named: selection and preflight share ONE row."""
    now = int(time.time())
    day = three_seats.seats["private"] / "sessions" / "2026" / "09" / "08"
    day.mkdir(parents=True, exist_ok=True)
    (day / "rollout-old.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": now - 4 * _HOUR,
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 100.0,
                            "resets_at": now + _HOUR,
                            "window_minutes": 300,
                        },
                        "secondary": None,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _live(three_seats.seats["private"], five=40.0, week=40.0, now=now)
    three_seats.reorder("private", "de", "default")
    # Pinned so the ranking cannot move off it: what is under test is that the OLD 100 %
    # rollout no longer vetoes the seat its newer live reading says is fine.
    cic.save_config({"codex_home": str(three_seats.seats["private"]), "codex_home_until": None})
    three_seats.scenarios(private={"scenario": "ok", "reply": "private served"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    assert three_seats.call_homes() == ["private"]


def test_the_opt_in_refreshes_the_seat_after_every_attempt(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codex_usage = true`: ephemeral again, and the runner measures the seat itself."""
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("codex_usage = false", "codex_usage = true"),
        encoding="utf-8",
    )
    config.invalidate_config_cache()
    refreshed: list[str] = []
    monkeypatch.setattr(cic, "_pre_selection_refresh", lambda cands, budget: None)
    monkeypatch.setattr(
        cic, "_post_attempt_refresh", lambda cand, budget: refreshed.append(cand.label)
    )
    three_seats.scenarios(private={"scenario": "ok", "reply": "hi"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    assert refreshed == three_seats.call_homes()
    assert "--ephemeral" in three_seats.calls()[0]["argv"]


def test_a_failed_refresh_still_alternates_the_seats(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch that returns nothing must degrade to the round-robin, not to one seat."""
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("codex_usage = false", "codex_usage = true"),
        encoding="utf-8",
    )
    config.invalidate_config_cache()
    monkeypatch.setattr(cic, "_pre_selection_refresh", lambda cands, budget: None)
    monkeypatch.setattr(cic, "_post_attempt_refresh", lambda cand, budget: None)
    three_seats.scenarios()
    for _ in range(3):
        assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    assert len(set(three_seats.call_homes())) == 3


def test_the_refresh_budget_caps_each_fetch(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every best-effort fetch is timeout-capped; the aggregate is capped too (O13)."""
    seen: list[float | None] = []

    def _fetch(home: Path, now: int | None = None, *, timeout: float | None = None) -> None:
        seen.append(timeout)
        return None

    monkeypatch.setattr(usage, "fetch_codex_usage", _fetch)
    cands = cic.codex_homes_in_order()
    cic._pre_selection_refresh(cands, cic._REFRESH_BUDGET_SEC)  # noqa: SLF001
    assert seen and all(t is not None and t <= cic._REFRESH_FETCH_TIMEOUT_SEC for t in seen)
    assert len(seen) == len(cands)  # at most one fetch per candidate
    _ = three_seats


def test_the_post_attempt_refresh_is_skipped_without_budget(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Path] = []
    monkeypatch.setattr(
        usage, "fetch_codex_usage", lambda home, now=None, *, timeout=None: called.append(home)
    )
    cand = cic.codex_homes_in_order()[0]
    cic._post_attempt_refresh(cand, 5.0)  # noqa: SLF001
    assert called == []
    cic._post_attempt_refresh(cand, 60.0)  # noqa: SLF001
    assert called == [cand.home]
    _ = three_seats


def test_both_parsers_take_the_new_routing_flags_together() -> None:
    """`-m … -F … -H` must coexist on BOTH `run` and `delegate` (no short-option clash)."""
    parser = cic.build_parser()
    run = parser.parse_args(["run", "-m", "gpt-5.6-sol", "-F", "12", "-H", "-E", "hi"])
    assert (run.model, run.min_remaining, run.headroom, run.ephemeral) == (
        "gpt-5.6-sol",
        12.0,
        True,
        True,
    )
    delegate = parser.parse_args(["delegate", "-m", "gpt-5.6-sol", "-F", "12", "-H", "do x"])
    assert (delegate.model, delegate.min_remaining, delegate.headroom) == (
        "gpt-5.6-sol",
        12.0,
        True,
    )
    # the default is "no explicit floor", so a read-only run keeps none and --write picks
    # the learned one up
    assert parser.parse_args(["run", "hi"]).min_remaining is None


# ── the `policy` CLI (plan D9) ────────────────────────────────────────────────────
def _policy(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {"policy": None, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_policy_shows_sets_and_refuses(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cic.cmd_policy(_policy()) == cic.EX_OK
    assert "policy: fill —" in capsys.readouterr().out

    assert cic.cmd_policy(_policy(policy="order")) == cic.EX_OK
    assert "policy: order —" in capsys.readouterr().out
    assert config.codex_seat_policy() == "order"

    assert cic.cmd_policy(_policy(policy="fill", json=True)) == cic.EX_OK
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1, "policy": "fill"}

    assert cic.cmd_policy(_policy(policy="bogus")) == cic.EX_USAGE
    assert "unknown policy 'bogus'" in capsys.readouterr().err
    assert config.codex_seat_policy() == "fill"  # unchanged
    _ = three_seats


def test_policy_refuses_to_drop_unknown_config_keys(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + 'my_own_key = "keep"\n', encoding="utf-8")
    config.invalidate_config_cache()
    assert cic.cmd_policy(_policy(policy="order")) == cic.EX_USAGE
    assert "refusing to rewrite config.toml" in capsys.readouterr().err
    assert "my_own_key" in path.read_text(encoding="utf-8")


def test_an_unrecognised_policy_value_degrades_to_fill(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + 'codex_seat_policy = "sideways"\n', encoding="utf-8"
    )
    config.invalidate_config_cache()
    monkeypatch.setattr(config, "_SEAT_POLICY_WARNED", False)
    assert config.codex_seat_policy() == "fill"
    assert "is not fill|order — using fill" in capsys.readouterr().err
    assert config.codex_seat_policy() == "fill"
    assert capsys.readouterr().err == ""  # warned once per process, not per call


def test_the_policy_key_round_trips_through_save_config(three_seats: SeatFixture) -> None:
    cfg = config.load_config()
    cfg.codex_seat_policy = "order"
    config.save_config(cfg)
    assert config.unknown_config_keys() == []
    assert config.load_config().codex_seat_policy == "order"
    _ = three_seats


def test_pin_active_follows_the_policy_and_the_registry(
    three_seats: SeatFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": None})
    assert cic.pin_active() is True  # fill + an order + a registered pin
    monkeypatch.setattr(config, "codex_seat_policy", lambda: "order")
    assert cic.pin_active() is False  # order + an explicit order
    monkeypatch.setattr(config, "codex_seat_order", lambda: [])
    assert cic.pin_active() is True
    monkeypatch.undo()

    stray = tmp_path / "stray"
    stray.mkdir()
    cic.save_config({"codex_home": str(stray), "codex_home_until": None})
    assert cic.pin_active() is False  # unregistered under BOTH policies
    monkeypatch.setattr(config, "codex_seat_policy", lambda: "order")
    monkeypatch.setattr(config, "codex_seat_order", lambda: [])
    assert cic.pin_active() is False

    monkeypatch.undo()
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": "2000-01-01"})
    assert cic.pin_active() is False  # expired


def test_the_quota_footer_names_the_policy(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["quota"]) == 0
    assert "codex seats [fill]:" in capsys.readouterr().out
    _ = three_seats
