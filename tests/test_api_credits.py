"""Monthly Anthropic API-credit grants (`claude_api_credits`) in `ccc quota`.

The grant and its dates come from config. The balance is never a measured figure: without
a spend source every surface says ``n/a``; with one (``claude_api_credit_spend``) it is an
ESTIMATE — grant minus spend since the grant — and labelled as one.
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
from typing import Any, Self

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
        "  API credits (Claude API, Console, Agent SDK — not interactive Claude Code):"
    )
    # Table rows: the account named as its provider row names it, then `api` (no
    # estimate) in the state column, the detail last.
    assert lines[head + 1].split()[:3] == ["claude-priv", "(cpriv)", "api"]
    assert "$200/cycle" in lines[head + 1]
    assert "$500/cycle · first grant 20.10." in lines[head + 2]
    assert all(line.endswith("balance: n/a") for line in lines[head + 1 : head + 3])


def _quota_args(*, no_bars: bool) -> argparse.Namespace:
    return argparse.Namespace(
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
        no_bars=no_bars,
        credit=None,
        probe=False,
    )


# ── spend sources + estimate ──────────────────────────────────────────────────


def _ledger(path: Path, rows: list[dict[str, object]], *, junk: bool = True) -> Path:
    lines = [json.dumps(row) for row in rows]
    if junk:
        lines += ["not json", json.dumps({"ts": "2026-10-12T10:00:00+02:00"})]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_spend_sources_skips_bad_entries() -> None:
    parsed = api_credits.parse_spend_sources(
        [
            "work=admin_api:~/x.env",
            "private=runs_jsonl:/tmp/runs.jsonl",
            "other=admin_api",
            "bad=runs_jsonl",  # no path
            "worse=s3:/x",  # unknown kind
            "Nope=admin_api",  # bad label
        ]
    )
    assert parsed["work"] == api_credits.SpendSource("work", "admin_api", "~/x.env")
    assert parsed["private"].kind == "runs_jsonl"
    assert parsed["other"].path == ""
    assert set(parsed) == {"work", "private", "other"}


def test_runs_spend_sums_the_current_grant_only(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path / "runs.jsonl",
        [
            {"ts": "2026-10-08T23:00:00+02:00", "cost_usd": 5.0},  # before the grant
            {"ts": "2026-10-09T08:00:00+02:00", "cost_usd": 1.25},
            {"ts": "2026-10-15T17:53:57+02:00", "cost_usd": 0.75},
        ],
    )
    spent, mtime = api_credits.runs_spend(ledger, dt.date(2026, 10, 9)) or (None, 0)
    assert spent == pytest.approx(2.0) and mtime > 0
    assert api_credits.runs_spend(tmp_path / "missing.jsonl", TODAY) is None


def test_parse_spend_sources_item_filter() -> None:
    parsed = api_credits.parse_spend_sources(
        [
            "private=runs_jsonl:/tmp/a#b.jsonl#anthropic-api-private",
            "work=runs_jsonl:/tmp/runs.jsonl",
            "bad=runs_jsonl:/tmp/runs.jsonl#",  # empty item
            "bad2=runs_jsonl:#x",  # no path
        ]
    )
    assert parsed["private"] == api_credits.SpendSource(
        "private", "runs_jsonl", "/tmp/a#b.jsonl", "anthropic-api-private"
    )
    assert parsed["work"].item == "" and parsed["work"].path == "/tmp/runs.jsonl"
    assert set(parsed) == {"private", "work"}


def _mixed_ledger(tmp_path: Path) -> Path:
    return _ledger(
        tmp_path / "mixed.jsonl",
        [
            {"ts": "2026-10-10T08:00:00+02:00", "cost_usd": 1.0, "item": "anthropic-api-private"},
            {"ts": "2026-10-11T08:00:00+02:00", "cost_usd": 10.0, "item": "anthropic-api-sdsc"},
            {"ts": "2026-10-12T08:00:00+02:00", "cost_usd": 100.0},
        ],
    )


def test_runs_spend_filters_by_item(tmp_path: Path) -> None:
    ledger = _mixed_ledger(tmp_path)
    since = dt.date(2026, 10, 9)
    assert (api_credits.runs_spend(ledger, since) or (0,))[0] == pytest.approx(111.0)
    assert (api_credits.runs_spend(ledger, since, "anthropic-api-private") or (0,))[
        0
    ] == pytest.approx(1.0)
    assert (api_credits.runs_spend(ledger, since, "anthropic-api-sdsc") or (0,))[
        0
    ] == pytest.approx(10.0)
    assert (api_credits.runs_spend(ledger, since, "nobody") or (1,))[0] == 0.0


def test_with_spend_item_filter_end_to_end(tmp_path: Path) -> None:
    ledger = _mixed_ledger(tmp_path)
    status = api_credits.credit_status(api_credits.parse_api_credits([PRIVATE])["private"], TODAY)
    source = api_credits.parse_spend_sources([f"private=runs_jsonl:{ledger}#anthropic-api-sdsc"])[
        "private"
    ]
    row = api_credits.with_spend(status, source)
    assert row["est_spent_usd"] == pytest.approx(10.0)
    assert row["spend_source"] == "run ledger (anthropic-api-sdsc)"


def test_with_spend_from_the_ledger_is_an_estimate(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "runs.jsonl", [{"ts": "2026-10-10T10:00:00+02:00", "cost_usd": 50}])
    status = api_credits.credit_status(api_credits.parse_api_credits([PRIVATE])["private"], TODAY)
    row = api_credits.with_spend(
        status, api_credits.SpendSource("private", "runs_jsonl", str(ledger))
    )
    assert row["est_spent_usd"] == 50 and row["est_left_usd"] == 150
    assert row["est_used_pct"] == pytest.approx(25.0)
    assert row["balance_usd"] is None  # an estimate is not a balance
    assert row["spend_scope"] == "machine"
    assert api_credits.describe(row, TODAY).startswith(
        "≈$150 of $200 left (est.) · $50 spent since 9.10. (run ledger, this machine only)"
    )


def test_used_pct_is_clamped() -> None:
    assert api_credits.used_pct({"usd_per_cycle": 100, "est_spent_usd": 250}) == 100.0
    assert api_credits.used_pct({"usd_per_cycle": 100, "est_spent_usd": -5}) == 0.0
    assert api_credits.used_pct({"usd_per_cycle": 100}) is None


def test_with_spend_reads_the_admin_cache_for_the_current_grant_only() -> None:
    status = api_credits.credit_status(api_credits.parse_api_credits([PRIVATE])["private"], TODAY)
    source = api_credits.SpendSource("private", "admin_api")
    fresh = {"private": {"since": "2026-10-09", "spent_usd": 20.0, "fetched_at": NOW}}
    row = api_credits.with_spend(status, source, cache=fresh)
    assert row["est_left_usd"] == 180 and row["spend_scope"] == "org"
    # A figure for the PREVIOUS grant must not be subtracted from this one.
    old = {"private": {"since": "2026-09-09", "spent_usd": 20.0, "fetched_at": NOW}}
    pending = api_credits.with_spend(status, source, cache=old)
    assert "est_left_usd" not in pending and pending["spend_pending"] is True
    assert "spend not read yet" in api_credits.describe(pending, TODAY)
    failed = {"private": {"since": "2026-10-09", "error": "HTTPError 401", "fetched_at": NOW}}
    row = api_credits.with_spend(status, source, cache=failed)
    assert "spend fetch failed: HTTPError 401" in api_credits.describe(row, TODAY)


def test_refresh_spend_throttles_and_keeps_the_last_good_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "admin.env"
    env.write_text("OTHER=1\nANTHROPIC_ADMIN_API_KEY='sk-test'\n", encoding="utf-8")
    calls: list[tuple[str, dt.date]] = []

    def fake(key: str, since: dt.date, timeout: float = 0) -> float:
        del timeout
        calls.append((key, since))
        if len(calls) > 1:
            raise OSError("down")
        return 12.5

    monkeypatch.setattr(api_credits, "admin_cost_usd", fake)
    cache = tmp_path / "spend.json"
    spend = [f"private=admin_api:{env}"]
    assert api_credits.refresh_spend([PRIVATE], spend, NOW, cache=cache) == [
        "private: $12.50 since 2026-10-09"
    ]
    assert calls == [("sk-test", dt.date(2026, 10, 9))]
    # Inside the refresh interval: no second call.
    assert not api_credits.refresh_spend([PRIVATE], spend, NOW + 60, cache=cache)
    later = NOW + api_credits.SPEND_REFRESH_SEC + 1
    assert api_credits.refresh_spend([PRIVATE], spend, later, cache=cache) == [
        "private: failed (OSError)"
    ]
    entry = json.loads(cache.read_text(encoding="utf-8"))["private"]
    assert entry["spent_usd"] == 12.5 and entry["error"] == "OSError"
    assert "sk-test" not in cache.read_text(encoding="utf-8")


def test_refresh_spend_without_an_admin_source_makes_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> float:
        raise AssertionError("network")

    monkeypatch.setattr(api_credits, "admin_cost_usd", boom)
    cache = tmp_path / "spend.json"
    assert not api_credits.refresh_spend([PRIVATE], ["private=runs_jsonl:/x"], NOW, cache=cache)
    assert not cache.exists()


def test_admin_cost_usd_pages_and_converts_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[dict[str, object]] = [
        {
            "data": [{"results": [{"currency": "USD", "amount": "1234.5"}]}],
            "has_more": True,
            "next_page": "p2",
        },
        {"data": [{"results": [{"currency": "USD", "amount": "65.5"}]}, {"results": []}]},
    ]
    seen: list[str] = []

    class _Resp:
        def __init__(self, body: dict[str, object]) -> None:
            self.body = json.dumps(body).encode()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, *_a: object) -> bytes:
            return self.body

    def fake_urlopen(request: object, timeout: float = 0) -> _Resp:
        del timeout
        seen.append(str(getattr(request, "full_url", "")))
        return _Resp(pages[len(seen) - 1])

    monkeypatch.setattr(api_credits.urllib.request, "urlopen", fake_urlopen)
    assert api_credits.admin_cost_usd("k", dt.date(2026, 9, 1)) == pytest.approx(13.0)
    assert "starting_at=2026-09-01T00%3A00%3A00Z" in seen[0] and "page=p2" in seen[1]
    # A grant that started today has no complete UTC day yet: zero, and no call.
    today_utc = dt.datetime.now(dt.UTC).date()
    assert api_credits.admin_cost_usd("k", today_utc) == 0.0 and len(seen) == 2


def test_report_draws_an_estimate_bar(
    _pin_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from command_center import cli

    ledger = _ledger(tmp_path / "runs.jsonl", [{"ts": "2026-10-10T10:00:00+02:00", "cost_usd": 50}])
    (_pin_home / "config.toml").write_text(
        'claude_accounts = ["private=/tmp/x-private", "work=/tmp/x-work"]\n'
        f"claude_api_credits = [{json.dumps(PRIVATE)}, {json.dumps(WORK)}]\n"
        f'claude_api_credit_spend = ["private=runs_jsonl:{ledger}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("time.time", lambda: float(NOW))
    assert cli.cmd_quota(_quota_args(no_bars=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    priv = next(line for line in lines if "claude-priv (cpriv)   api est." in line)
    assert "API PROMO ≈" in priv and "25%" in priv
    assert "≈$150 of $200 left (est.)" in priv
    work = next(line for line in lines if "claude-work (cwork)   api " in line)
    # No spend source for work: the bar says n/a and claims no percentage.
    assert "API n/a" in work and "0%" not in work


# ── TUI card row + hidden cards ───────────────────────────────────────────────


def test_card_row_is_a_bar_with_an_estimate(tmp_path: Path) -> None:
    from command_center import usage

    ledger = _ledger(tmp_path / "runs.jsonl", [{"ts": "2026-10-10T10:00:00+02:00", "cost_usd": 50}])
    status = api_credits.credit_status(api_credits.parse_api_credits([PRIVATE])["private"], TODAY)
    row = api_credits.with_spend(
        status, api_credits.SpendSource("private", "runs_jsonl", str(ledger))
    )
    text = usage.render_api_credit_row(row).plain
    assert text.startswith("API PROMO: ≈$150 left →26.10.") and text.rstrip().endswith("25%")
    assert len(text.rstrip("\n")) == usage._CARD_INNER_WIDTH  # noqa: SLF001
    assert usage.render_api_credit_row(status).plain == "API PROMO: $200 →26.10. · balance n/a\n"
    work = api_credits.credit_status(api_credits.parse_api_credits([WORK])["work"], TODAY)
    assert usage.render_api_credit_row(work).plain == "API PROMO: first grant 20.10.\n"
    assert usage.render_api_credit_row(None).plain == ""


def test_hidden_cards_by_name_or_chord() -> None:
    from command_center import config
    from command_center.views import tui

    assert config.load_config().usage_cards_hidden == ["copilot"]
    assert tui.card_hidden(["copilot"], "copilot", action="toggle_card_copilot")
    assert tui.card_hidden([" T4 "], "copilot", action="toggle_card_copilot")
    assert not tui.card_hidden(["copilot"], "claude_work", action="toggle_card_work")
    assert tui.card_hidden(["t6"], "codex_de", chord="t6")
    assert not tui.card_hidden([], "copilot", action="toggle_card_copilot")


def test_api_row_is_appended_below_the_card() -> None:
    from rich.text import Text

    from command_center.views import tui

    card = Text("Session\nWeek")  # the card renderers rstrip their body
    assert tui._with_api_row(card, Text("")).plain == "Session\nWeek"  # noqa: SLF001
    assert tui._with_api_row(card, Text("API\n")).plain == "Session\nWeek\nAPI"  # noqa: SLF001


# ── measured balance (balance_json) ───────────────────────────────────────────

# The reading: 2026-10-16 11:48 local, 12 minutes before NOW.
MEASURED = NOW - 12 * 60


def _balance_file(path: Path, **over: object) -> Path:
    """A ``balance_json`` file shaped like the Console scraper's output."""
    data: dict[str, object] = {
        "schema": 1,
        "status": "ok",
        "error": "",
        "measured_at": MEASURED,
        "checked_at": MEASURED,
        "currency": "USD",
        "purchased_usd": 0.0,
        "promo": [
            {
                "remaining_usd": 185.88,
                "granted_usd": 200.0,
                "expires_at": "2026-11-26T00:00:00Z",
                "granted_at": "2026-10-23T09:12:00Z",
                "name": "API credit (Max)",
            }
        ],
        "cap": {"spent_usd": 153.89, "limit_usd": 500.0, "resets_at": "2026-11-01T00:00:00Z"},
    }
    data.update(over)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_parse_keeps_balance_and_spend_sources_apart() -> None:
    entries = [
        "work=balance_json:~/state/work.json",
        "work=admin_api:~/a.env",
        "private=balance_json:",  # no path: skipped
        "Bad=balance_json:/x.json",
    ]
    balances = api_credits.parse_balance_sources(entries)
    assert balances == {"work": api_credits.BalanceSource("work", "~/state/work.json")}
    # The balance file never counts as a spend source, so the estimate stays available.
    assert api_credits.parse_spend_sources(entries)["work"].kind == "admin_api"


def test_parse_reads_name_and_card_label() -> None:
    parsed = api_credits.parse_api_credits(
        ["work=usd:500,day:20,name:apiwork,label:API TEAM", "x=usd:1,day:1,name:has space"]
    )
    assert parsed["work"].name == "apiwork" and parsed["work"].label == "API TEAM"
    assert "x" not in parsed  # a name with a space is skipped
    status = api_credits.credit_status(parsed["work"], TODAY)
    assert status["name"] == "apiwork" and status["label"] == "API TEAM"


def test_fresh_reading_replaces_the_estimate_and_the_dates(tmp_path: Path) -> None:
    path = _balance_file(tmp_path / "private.json")
    rows = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{path}"], TODAY, now=NOW
    )
    row = rows[0]
    assert row["balance_source"] == "measured" and row["balance_usd"] == 185.88
    assert row["balance_grant_usd"] == 200.0 and round(row["balance_used_pct"], 2) == 7.06
    # The Console's own dates win over the configured day-of-month cycle.
    assert row["expires_on"] == "2026-11-26" and row["granted_on"] == "2026-10-23"
    assert row["cap_spent_usd"] == 153.89 and row["cap_limit_usd"] == 500.0
    assert row["cap_resets_on"] == "2026-11-01" and round(row["cap_used_pct"]) == 31
    # $346.11 of room under the cap > $185.88 of money left: the money runs out first.
    assert row["cap_room_usd"] == 346.11 and row["cap_binds"] is False
    text = api_credits.describe(row, TODAY)
    assert text.startswith("$185.88 of $200 left (Console, measured 12m ago)")
    assert "expires 26.11. (in 41d)" in text and "est." not in text
    cap = api_credits.describe_cap(row, TODAY)
    assert cap.startswith("$153.89 of $500 monthly spend limit") and "not a balance" in cap


def test_stale_or_failed_reading_falls_back_to_the_estimate(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "runs.jsonl", [{"ts": "2026-10-10T10:00:00+02:00", "cost_usd": 50}])
    spend = [f"private=runs_jsonl:{ledger}"]
    stale = _balance_file(tmp_path / "stale.json", measured_at=NOW - 7 * 3600)
    row = api_credits.statuses_with_spend(
        [PRIVATE], [*spend, f"private=balance_json:{stale}"], TODAY, now=NOW
    )[0]
    assert row.get("balance_source") == "n/a" and row["balance_usd"] is None
    assert row["est_left_usd"] == 150 and "cap_used_pct" not in row
    assert row["balance_note"] == "Console reading 7h old"
    assert api_credits.describe(row, TODAY).endswith("· Console reading 7h old")
    # Never read: the status says so, nothing is invented.
    never = _balance_file(tmp_path / "never.json", measured_at=None, status="logged_out")
    row = api_credits.statuses_with_spend(
        [PRIVATE], [*spend, f"private=balance_json:{never}"], TODAY, now=NOW
    )[0]
    assert row["balance_note"] == "Console read failed: logged_out"
    missing = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{tmp_path / 'nope.json'}"], TODAY, now=NOW
    )[0]
    assert missing["balance_note"] == "no Console reading" and missing["balance_usd"] is None


def test_failed_latest_attempt_keeps_a_fresh_reading(tmp_path: Path) -> None:
    path = _balance_file(tmp_path / "p.json", status="logged_out")
    row = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{path}"], TODAY, now=NOW
    )[0]
    assert row["balance_source"] == "measured" and row["balance_error"] == "logged_out"
    assert "last read failed: logged_out" in api_credits.describe(row, TODAY)


def test_expired_or_malformed_rows_are_dropped(tmp_path: Path) -> None:
    path = _balance_file(
        tmp_path / "p.json",
        promo=[
            {"remaining_usd": 5, "granted_usd": 10, "expires_at": "2026-10-01T00:00:00Z"},
            {"remaining_usd": "x", "granted_usd": 10, "expires_at": "2026-12-01T00:00:00Z"},
        ],
        cap={"spent_usd": 1, "limit_usd": 0},
    )
    row = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{path}"], TODAY, now=NOW
    )[0]
    assert row["balance_usd"] is None and "cap_used_pct" not in row
    assert row["balance_note"] == "Console shows no current promotional credit"


# Spent $153.89 of a $300 limit: $146.11 of room < $185.88 + $10 of money — the cap binds.
BINDING_CAP = {"spent_usd": 153.89, "limit_usd": 300.0, "resets_at": "2026-11-01T00:00:00Z"}


def test_cap_binds_only_when_its_room_is_smaller_than_the_money(tmp_path: Path) -> None:
    path = _balance_file(tmp_path / "p.json", cap=BINDING_CAP, purchased_usd=10.0)
    row = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{path}"], TODAY, now=NOW
    )[0]
    assert row["cap_room_usd"] == 146.11 and row["cap_binds"] is True
    # Purchased credit counts as money too: with $400 bought the cap binds even at $500.
    rich = _balance_file(tmp_path / "r.json", purchased_usd=400.0)
    row = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{rich}"], TODAY, now=NOW
    )[0]
    assert row["cap_binds"] is True


def test_card_rows_with_a_measured_balance_and_cap(tmp_path: Path) -> None:
    from command_center import usage

    path = _balance_file(tmp_path / "p.json")
    row = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{path}"], TODAY, now=NOW
    )[0]
    # The cap does not bind ($346 of room > $186 of money): no cap row.
    (promo,) = usage.render_api_credit_row(row).plain.splitlines()
    assert promo.startswith("API PROMO: $186 left →26.11.") and promo.endswith("7%")
    labelled = dict(row, label="API MAX")
    assert usage.render_api_credit_row(labelled).plain.startswith("API MAX: $186 left")
    binding = _balance_file(tmp_path / "b.json", cap=BINDING_CAP)
    row = api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{binding}"], TODAY, now=NOW
    )[0]
    promo, cap = usage.render_api_credit_row(row).plain.splitlines()
    assert cap.startswith("API cap: $154 of $300 →1.11.") and cap.endswith("51%")
    assert len(promo) == len(cap) == usage._CARD_INNER_WIDTH  # noqa: SLF001


def test_card_row_says_why_the_reading_is_not_used(tmp_path: Path) -> None:
    from command_center import usage

    ledger = _ledger(tmp_path / "runs.jsonl", [{"ts": "2026-10-10T10:00:00+02:00", "cost_usd": 50}])
    stale = _balance_file(tmp_path / "s.json", measured_at=NOW - 7 * 3600)
    row = api_credits.statuses_with_spend(
        [PRIVATE],
        [f"private=runs_jsonl:{ledger}", f"private=balance_json:{stale}"],
        TODAY,
        now=NOW,
    )[0]
    promo, note = usage.render_api_credit_row(row).plain.splitlines()
    assert promo.startswith("API PROMO: ≈$150 left →26.10.") and note == "Console reading 7h old"


def test_report_draws_measured_and_cap_rows(
    _pin_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from command_center import cli

    path = _balance_file(tmp_path / "p.json", cap=BINDING_CAP)
    (_pin_home / "config.toml").write_text(
        'claude_accounts = ["private=/tmp/x-private", "work=/tmp/x-work"]\n'
        f'claude_api_credits = ["{PRIVATE},name:apipriv", {json.dumps(WORK)}]\n'
        f'claude_api_credit_spend = ["private=balance_json:{path}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("time.time", lambda: float(NOW))
    assert cli.cmd_quota(_quota_args(no_bars=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    promo = next(line for line in lines if line.lstrip().startswith("apipriv ") and " api " in line)
    assert "API PROMO" in promo and "≈" not in promo
    assert "$185.88 of $200 left (Console, measured 12m ago)" in promo
    assert "12m" in promo
    cap = next(line for line in lines if "apipriv cap" in line)
    assert "api cap" in cap and "API cap" in cap and "51%" in cap
    # Both rows start their name in the same column.
    assert promo.index("apipriv") == cap.index("apipriv")
    snap = json.loads(json.dumps(quota.snapshot(now=NOW)))
    row = snap["api_credits"][0]
    assert row["balance_usd"] == 185.88 and row["name"] == "apipriv"
    # A cap that does not bind draws no row.
    _balance_file(path)
    assert cli.cmd_quota(_quota_args(no_bars=False)) == 0
    assert "apipriv cap" not in capsys.readouterr().out


CLOUD = {
    "remaining_usd": 118.4,
    "granted_usd": 250.0,
    "expires_at": "2026-11-05T07:59:00Z",
    "measured_at": MEASURED,
}


def _cloud_row(tmp_path: Path, **over: object) -> dict[str, Any]:
    path = _balance_file(tmp_path / "c.json", **over)
    return api_credits.statuses_with_spend(
        [PRIVATE], [f"private=balance_json:{path}"], TODAY, now=NOW
    )[0]


def test_cloud_credit_present_absent_stale_expired(tmp_path: Path) -> None:
    row = _cloud_row(tmp_path, cloud_credit=CLOUD)
    assert row["cloud_left_usd"] == 118.4 and row["cloud_granted_usd"] == 250.0
    assert round(row["cloud_used_pct"], 1) == 52.6 and row["cloud_expires_on"] == "2026-11-05"
    assert row["cloud_age_sec"] == 12 * 60
    text = api_credits.describe_cloud(row, TODAY)
    assert text.startswith("$118.40 of $250 left (claude.ai, measured 12m ago)")
    assert "expires 5.11. (in 20d)" in text
    # No credit (null, or the key of an older writer missing): no cloud_* field at all.
    for over in ({"cloud_credit": None}, {}):
        assert not [k for k in _cloud_row(tmp_path, **over) if k.startswith("cloud_")]
    # Its own reading aged out (the claude.ai read kept failing), or the credit expired.
    old = dict(CLOUD, measured_at=NOW - 4 * 3600)
    assert "cloud_left_usd" not in _cloud_row(tmp_path, cloud_credit=old)
    gone = dict(CLOUD, expires_at="2026-10-01T00:00:00Z")
    assert "cloud_left_usd" not in _cloud_row(tmp_path, cloud_credit=gone)
    # Unusable figures are dropped, never half-drawn.
    for bad in (dict(CLOUD, granted_usd=0), dict(CLOUD, remaining_usd="x"), "x"):
        assert "cloud_left_usd" not in _cloud_row(tmp_path, cloud_credit=bad)
    # A stale Console reading shows no cloud row either (the same freshness rule).
    assert "cloud_left_usd" not in _cloud_row(
        tmp_path, cloud_credit=CLOUD, measured_at=NOW - 7 * 3600
    )


def test_card_draws_the_cloud_bar_under_the_promo_bar(tmp_path: Path) -> None:
    from command_center import usage

    row = _cloud_row(tmp_path, cloud_credit=CLOUD)
    promo, cloud = usage.render_api_credit_row(row).plain.splitlines()
    assert promo.startswith("API PROMO: $186 left →26.11.")
    assert cloud.startswith("CLOUD: $118 left →5.11.") and cloud.endswith("53%")
    assert len(promo) == len(cloud) == usage._CARD_INNER_WIDTH  # noqa: SLF001
    # Order with a binding cap: promo, cloud, cap.
    binding = _cloud_row(tmp_path, cloud_credit=CLOUD, cap=BINDING_CAP)
    lines = usage.render_api_credit_row(binding).plain.splitlines()
    assert [ln.split(":")[0] for ln in lines] == ["API PROMO", "CLOUD", "API cap"]
    # Without the credit the card is the old one.
    assert len(usage.render_api_credit_row(_cloud_row(tmp_path)).plain.splitlines()) == 1


def test_report_draws_the_cloud_row(
    _pin_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from command_center import cli

    path = _balance_file(tmp_path / "p.json", cloud_credit=CLOUD)
    (_pin_home / "config.toml").write_text(
        'claude_accounts = ["private=/tmp/x-private", "work=/tmp/x-work"]\n'
        f'claude_api_credits = ["{PRIVATE},name:apipriv", {json.dumps(WORK)}]\n'
        f'claude_api_credit_spend = ["private=balance_json:{path}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("time.time", lambda: float(NOW))
    assert cli.cmd_quota(_quota_args(no_bars=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    promo = next(line for line in lines if line.lstrip().startswith("apipriv ") and " api " in line)
    cloud = next(line for line in lines if "apipriv cloud" in line)
    assert "api cloud" in cloud and "CLOUD" in cloud and "53%" in cloud
    assert "$118.40 of $250 left (claude.ai, measured 12m ago)" in cloud
    assert promo.index("apipriv") == cloud.index("apipriv")
    # No credit → no row.
    _balance_file(path)
    assert cli.cmd_quota(_quota_args(no_bars=False)) == 0
    assert "apipriv cloud" not in capsys.readouterr().out
    snap = json.loads(json.dumps(quota.snapshot(now=NOW)))
    assert "api_credits" in snap
