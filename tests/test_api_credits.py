"""Monthly Anthropic API-credit grants (`claude_api_credits`) in `ccc quota`.

The grant and its dates come from config only; the balance is never derived, so every
surface must say ``n/a`` for it.
"""

# `quota` reads `cli` through a LAZY import and `cli` imports `quota` — a function-local
# edge, never an import-time cycle (pylint only sees the pair this test pulls in).
# pylint: disable=cyclic-import

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from command_center import api_credits, quota

PRIVATE = "private=usd:200,day:26,from:2026-10-09"
WORK = "work=usd:500,day:20,from:2026-10-20"
# 2026-10-16 12:00 local time — the day the facts below were read.
NOW = int(dt.datetime(2026, 10, 16, 12, 0).timestamp())
TODAY = dt.date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _pin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own app home, so no real config or snapshot is read."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    home = tmp_path / "claude-home" / "command-center"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _write_config(home: Path, entries: list[str]) -> None:
    body = ", ".join(json.dumps(entry) for entry in entries)
    (home / "config.toml").write_text(
        'claude_accounts = ["private=/tmp/x-private", "work=/tmp/x-work"]\n'
        f"claude_api_credits = [{body}]\n",
        encoding="utf-8",
    )


# ── parser ────────────────────────────────────────────────────────────────────


def test_parse_reads_amount_day_and_first_grant() -> None:
    parsed = api_credits.parse_api_credits([PRIVATE, "work=usd:500,day:20"])
    assert parsed["private"] == api_credits.ApiCredit("private", 200.0, 26, dt.date(2026, 10, 9))
    assert parsed["work"] == api_credits.ApiCredit("work", 500.0, 20, None)


@pytest.mark.parametrize(
    "entry",
    [
        "private",  # no "="
        "Private=usd:200,day:26",  # label shape
        "private=usd:200",  # no day
        "private=day:26",  # no usd
        "private=usd:0,day:26",  # not positive
        "private=usd:nan,day:26",  # NaN
        "private=usd:200,day:32",  # day out of range
        "private=usd:200,day:0",
        "private=usd:200,day:26,from:2026-02-30",  # not a real day
        "private=usd:200,day:26,from:26.10.2026",  # not ISO
        "private=usd:200,day:26,until:2026-10-26",  # unknown field
        "private=usd:200,usd:300,day:26",  # repeated field
        "private=usd:200,,day:26",  # empty field
    ],
)
def test_parse_skips_a_bad_entry_without_blanking_the_others(entry: str) -> None:
    parsed = api_credits.parse_api_credits([entry, WORK])
    assert list(parsed) == ["work"]


# ── cycle arithmetic ──────────────────────────────────────────────────────────


def test_cycle_bounds_straddle_today() -> None:
    assert api_credits.cycle_bounds(26, TODAY) == (dt.date(2026, 9, 26), dt.date(2026, 10, 26))
    assert api_credits.cycle_bounds(16, TODAY) == (TODAY, dt.date(2026, 11, 16))


def test_cycle_bounds_clamp_short_months_and_roll_the_year() -> None:
    assert api_credits.cycle_bounds(31, dt.date(2026, 2, 10)) == (
        dt.date(2026, 1, 31),
        dt.date(2026, 2, 28),
    )
    assert api_credits.cycle_bounds(31, dt.date(2026, 2, 28)) == (
        dt.date(2026, 2, 28),
        dt.date(2026, 3, 31),
    )
    assert api_credits.cycle_bounds(5, dt.date(2026, 12, 20)) == (
        dt.date(2026, 12, 5),
        dt.date(2027, 1, 5),
    )
    assert api_credits.cycle_bounds(20, dt.date(2027, 1, 3)) == (
        dt.date(2026, 12, 20),
        dt.date(2027, 1, 20),
    )


def test_status_inside_a_cycle_starts_at_the_first_grant() -> None:
    credit = api_credits.parse_api_credits([PRIVATE])["private"]
    status = api_credits.credit_status(credit, TODAY)
    assert status["granted_on"] == "2026-10-09"  # not the cycle start 2026-09-26
    assert status["expires_on"] == status["next_grant_on"] == "2026-10-26"
    assert status["balance_usd"] is None and status["balance_source"] == "n/a"


def test_status_before_the_first_grant_has_only_a_next_grant() -> None:
    credit = api_credits.parse_api_credits([WORK])["work"]
    status = api_credits.credit_status(credit, TODAY)
    assert status["granted_on"] is None and status["expires_on"] is None
    assert status["next_grant_on"] == "2026-10-20"
    later = api_credits.credit_status(credit, dt.date(2026, 10, 20))
    assert later["granted_on"] == "2026-10-20"
    assert later["expires_on"] == "2026-11-20"


def test_describe_never_states_a_balance() -> None:
    parsed = api_credits.parse_api_credits([PRIVATE, WORK])
    priv = api_credits.describe(api_credits.credit_status(parsed["private"], TODAY), TODAY)
    work = api_credits.describe(api_credits.credit_status(parsed["work"], TODAY), TODAY)
    assert priv == (
        "$200/cycle · granted 9.10. · expires 26.10. (in 10d), next grant then · balance: n/a"
    )
    assert work == "$500/cycle · first grant 20.10. (in 4d) · balance: n/a"


# ── quota snapshot + report ───────────────────────────────────────────────────


def test_snapshot_omits_the_key_when_nothing_is_configured() -> None:
    assert "api_credits" not in quota.snapshot(now=NOW)


def test_snapshot_carries_the_grants_in_account_order(_pin_home: Path) -> None:
    _write_config(_pin_home, [WORK, PRIVATE])
    snap = quota.snapshot(now=NOW)
    rows = snap["api_credits"]
    assert [row["id"] for row in rows] == ["claude:private", "claude:work"]
    assert rows[0]["display"] == "claude-priv" and rows[0]["command"] == "cpriv"
    assert all(row["balance_usd"] is None for row in rows)
    # Not a provider: the grants must not enter any verdict or ranking.
    assert not any(p["id"].startswith("api") for p in snap["providers"])


def test_report_prints_one_line_per_grant(
    _pin_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import cli

    _write_config(_pin_home, [PRIVATE, WORK])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("time.time", lambda: float(NOW))
    args = argparse.Namespace(
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
        no_bars=True,
        credit=None,
        probe=False,
    )
    assert cli.cmd_quota(args) == 0
    lines = capsys.readouterr().out.splitlines()
    head = lines.index(
        "API credits (Claude API, Console, Agent SDK — not interactive Claude Code):"
    )
    assert lines[head + 1].startswith("  claude-priv (cpriv)  $200/cycle")
    assert lines[head + 2].startswith("  claude-work (cwork)  $500/cycle · first grant 20.10.")
    assert all(line.endswith("balance: n/a") for line in lines[head + 1 : head + 3])
