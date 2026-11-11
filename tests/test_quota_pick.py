"""The relay successor's account pick (``ccc quota -n/--next-from``).

The rule (Decision 1 of the lean-relay plan): a successor STAYS on its predecessor's
account. It switches only when a FRESH source reading is at/over 97 % (or the source is
blocked) AND the other account has a fresh reading below 97 % on every live window.
Stale, missing or unknown evidence — or both accounts full — means stay. The decision
and its evidence are frozen, so a retry never changes account.

Every failure here is silent and expensive: a successor opened on a full account burns a
request and a relay; one that flaps between accounts on stale numbers loses its cache.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pytest

from command_center import cli, quota, usage

NOW = 1_800_000_000
LABELS = {"work": Path("/accounts/work"), "private": Path("/accounts/private")}


@pytest.fixture(autouse=True)
def _pin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own app home, two configured accounts, a frozen clock and no network."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    (tmp_path / "claude-home" / "command-center").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(quota.config, "claude_config_dirs", lambda: dict(LABELS))
    monkeypatch.setattr("time.time", lambda: float(NOW))


class _Usage:
    """Per-account snapshots plus a counting fake of the one networked refresh."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.snaps: dict[str, usage.Usage] = {}
        self.fetched: list[str] = []
        monkeypatch.setattr(usage, "read_usage", self.snaps.get)
        monkeypatch.setattr(usage, "fetch_claude_usage", self._fetch)

    def _fetch(self, account: str, _now: int | None = None) -> usage.Usage | None:
        self.fetched.append(account)
        return self.snaps.get(account)

    def set(
        self,
        account: str,
        five: float | None,
        seven: float | None,
        *,
        age: int = 30,
        five_resets_in: int = 3600,
    ) -> None:
        def win(pct: float | None, resets_in: int) -> usage.Window | None:
            if pct is None:
                return None
            return usage.Window(used_percentage=pct, resets_at=NOW + resets_in)

        self.snaps[account] = usage.Usage(
            captured_at=NOW - age,
            five_hour=win(five, five_resets_in),
            seven_day=win(seven, 3 * 86400),
        )


@pytest.fixture(name="meter")
def _meter(monkeypatch: pytest.MonkeyPatch) -> _Usage:
    return _Usage(monkeypatch)


def _args(**over: object) -> argparse.Namespace:
    """``ccc quota -n …`` as parsed by the real parser, then *over* applied."""
    args = cli.build_parser().parse_args(["quota", "-n", "work", "-j"])
    for key, value in over.items():
        setattr(args, key, value)
    return args


def _pick(capsys: pytest.CaptureFixture[str], **over: object) -> tuple[int, dict]:
    code = cli.cmd_quota(_args(**over))
    return code, json.loads(capsys.readouterr().out)


# ── the 97 % boundary ────────────────────────────────────────────────────────────


def test_quota_pick_stays_at_96_9(meter: _Usage, capsys: pytest.CaptureFixture[str]) -> None:
    meter.set("work", 96.9, 50.0)
    meter.set("private", 5.0, 5.0)
    code, out = _pick(capsys)
    assert code == 0
    assert (out["account"], out["decision"]) == ("work", "stay")
    assert out["base"] == "work"


def test_quota_pick_switches_at_97_0(meter: _Usage, capsys: pytest.CaptureFixture[str]) -> None:
    meter.set("work", 20.0, 97.0)
    meter.set("private", 5.0, 40.0)
    code, out = _pick(capsys)
    assert code == 0
    assert (out["account"], out["decision"]) == ("private", "switch")
    assert out["threshold_pct"] == 97.0
    assert out["evidence"]["work"]["live_max_pct"] == 97.0
    assert out["evidence"]["private"]["fresh"] is True


def test_quota_pick_switches_off_a_blocked_source(
    meter: _Usage, capsys: pytest.CaptureFixture[str]
) -> None:
    """An observed rejection blocks the source whatever its meter says."""
    meter.set("work", 10.0, 10.0)
    meter.set("private", 5.0, 5.0)
    quota.record_block(
        "claude:work", blocked_until=NOW + 3600, reason="weekly limit", observed_at=NOW - 60
    )
    code, out = _pick(capsys)
    assert code == 0
    assert (out["account"], out["decision"]) == ("private", "switch")
    assert out["evidence"]["work"]["state"] == quota.BLOCKED


# ── stale and unknown evidence → stay ────────────────────────────────────────────


def test_quota_pick_stale_source_stays(meter: _Usage, capsys: pytest.CaptureFixture[str]) -> None:
    meter.set("work", 99.0, 99.0, age=quota.PICK_FRESH_SEC + 1)
    meter.set("private", 5.0, 5.0)
    code, out = _pick(capsys)
    assert (out["account"], out["decision"]) == ("work", "stay")
    assert "stale" in out["reason"]
    assert code == 0  # stale evidence proves nothing: not reported as full


def test_quota_pick_stale_alternative_stays_and_exits_1_with_valid_json(
    meter: _Usage, capsys: pytest.CaptureFixture[str]
) -> None:
    """Source full, the other side unproven: stay, exit 1, the JSON still parses."""
    meter.set("work", 98.0, 50.0)
    meter.set("private", 5.0, 5.0, age=quota.PICK_FRESH_SEC + 1)
    code, out = _pick(capsys)
    assert code == 1
    assert (out["account"], out["decision"]) == ("work", "stay")
    assert out["evidence"]["private"]["fresh"] is False


def test_quota_pick_unknown_alternative_stays(
    meter: _Usage, capsys: pytest.CaptureFixture[str]
) -> None:
    meter.set("work", 99.0, 99.0)  # no snapshot at all for private
    code, out = _pick(capsys)
    assert code == 1
    assert (out["account"], out["decision"]) == ("work", "stay")
    assert out["evidence"]["private"]["state"] == quota.UNKNOWN


def test_quota_pick_both_full_stays(meter: _Usage, capsys: pytest.CaptureFixture[str]) -> None:
    meter.set("work", 99.0, 60.0)
    meter.set("private", 10.0, 97.5)
    code, out = _pick(capsys)
    assert code == 1
    assert (out["account"], out["decision"]) == ("work", "stay")
    assert "no alternative" in out["reason"]


def test_quota_pick_ignores_a_window_that_already_reset(
    meter: _Usage, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 99 % five-hour window past its reset is not live: the alternative has room."""
    meter.set("work", 99.0, 50.0)
    meter.set("private", 99.0, 30.0, five_resets_in=-60)
    code, out = _pick(capsys)
    assert code == 0
    assert (out["account"], out["decision"]) == ("private", "switch")


# ── refresh, override, base resolution ───────────────────────────────────────────


def test_quota_pick_refreshes_each_account_once(
    meter: _Usage, capsys: pytest.CaptureFixture[str]
) -> None:
    meter.set("work", 10.0, 10.0)
    meter.set("private", 10.0, 10.0)
    _pick(capsys)
    assert sorted(meter.fetched) == ["private", "work"]


def test_quota_pick_explicit_override_wins_and_is_logged(
    meter: _Usage, capsys: pytest.CaptureFixture[str]
) -> None:
    meter.set("work", 10.0, 10.0)
    meter.set("private", 99.0, 99.0)
    code, out = _pick(capsys, account="private")
    assert code == 1  # honoured, but the caller learns the chosen account is full
    assert out["full"] is True
    assert (out["account"], out["decision"]) == ("private", "override")
    assert "-A" in out["reason"]
    assert meter.fetched == []  # an explicit choice needs no fresh evidence


def test_quota_pick_base_may_be_a_config_dir(
    meter: _Usage, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("command_center.accounts.account_label", lambda _d: "work")
    meter.set("work", 10.0, 10.0)
    code, out = _pick(capsys, next_from="/accounts/work")
    assert code == 0
    assert out["base"] == "work"


@pytest.mark.usefixtures("meter")
def test_quota_pick_unknown_base_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_quota(_args(next_from="nobody")) == 2
    assert "unknown Claude account" in capsys.readouterr().err


def test_quota_pick_text_line(meter: _Usage, capsys: pytest.CaptureFixture[str]) -> None:
    meter.set("work", 98.0, 10.0)
    meter.set("private", 1.0, 1.0)
    assert cli.cmd_quota(_args(json=False)) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("successor account: private (switch: ")


# ── frozen decision: retries never change account ────────────────────────────────


def test_quota_pick_retry_reuses_the_frozen_decision(
    meter: _Usage, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    record = tmp_path / "relay" / "account-pick.json"
    meter.set("work", 98.0, 10.0)
    meter.set("private", 1.0, 1.0)
    code, first = _pick(capsys, freeze=str(record))
    assert code == 0 and first["account"] == "private" and first["frozen"] is False
    assert stat.S_IMODE(os.stat(record).st_mode) == 0o600
    # The world changes: the source drained, the alternative filled up.
    meter.set("work", 1.0, 1.0)
    meter.set("private", 99.0, 99.0)
    meter.fetched.clear()
    code, again = _pick(capsys, freeze=str(record))
    assert code == 0
    assert again["account"] == "private" and again["frozen"] is True
    assert again["decided_at"] == first["decided_at"]
    assert again["evidence"] == first["evidence"]
    assert meter.fetched == []  # a frozen decision is never re-measured


def test_quota_pick_frozen_record_of_another_base_exits_2(
    meter: _Usage, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    record = tmp_path / "account-pick.json"
    meter.set("work", 10.0, 10.0)
    meter.set("private", 10.0, 10.0)
    _pick(capsys, freeze=str(record))
    assert cli.cmd_quota(_args(next_from="private", freeze=str(record))) == 2
    assert "frozen" in capsys.readouterr().err


def test_quota_pick_malformed_frozen_record_exits_2(
    meter: _Usage, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    record = tmp_path / "account-pick.json"
    record.write_text("{not json", encoding="utf-8")
    assert cli.cmd_quota(_args(freeze=str(record))) == 2
    assert "frozen" in capsys.readouterr().err
    assert meter.fetched == []


def test_quota_pick_options_need_next_from(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_quota(_args(next_from=None, account="work")) == 2
    assert "-n/--next-from" in capsys.readouterr().err
