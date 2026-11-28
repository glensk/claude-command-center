"""``seatfull`` restates the relay pick's seat-full rule stdlib-only (tp#2877 B7).

Every restated constant and rule is pinned against :mod:`.quota` / :mod:`.config`, so the
status line's seat row, ``seat-full.json`` and ``ccc quota``'s ``seat_full`` cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import config, quota, seatfull, usagecache

NOW = 1_900_000_000

# pylint: disable=protected-access


def test_constants_equal_their_sources() -> None:
    assert seatfull.FRESH_SEC == quota.PICK_FRESH_SEC
    assert seatfull.STALE_AFTER_SEC == {
        "five_hour": quota._SESSION_STALE_AFTER_SEC,
        "seven_day": quota._WEEK_STALE_AFTER_SEC,
    }
    assert seatfull.WINDOW_NAMES == tuple(config.RELAY_SWITCH_WINDOW_NAMES)
    assert seatfull.COOLDOWNS_NAME == quota._COOLDOWNS_NAME
    assert seatfull.EXHAUSTED_PCT == quota._EXHAUSTED_PCT
    assert seatfull.DISPLAY_SEAT == quota._DISPLAY_SEAT
    for label in ("private", "work", "extra"):
        assert seatfull.seat_name(label) == quota.seat_command(f"claude:{label}")


@pytest.mark.parametrize(
    ("pct", "windows"),
    [
        (98, ["seven_day"]),
        (97.5, ["seven_day", "five_hour", "seven_day"]),
        (True, ["seven_day"]),
        ("98", ["seven_day"]),
        (0, ["seven_day"]),
        (101, ["seven_day"]),
        (98, []),
        (98, "seven_day"),
        (98, ["daily"]),
    ],
)
def test_policy_equals_config(pct: object, windows: object) -> None:
    cfg = config.Config(
        relay_switch_weekly_pct=pct,  # type: ignore[arg-type]
        relay_switch_windows=windows,  # type: ignore[arg-type]
    )
    try:
        expected: tuple[float | None, tuple[str, ...]] = config.relay_switch_policy(cfg)
    except config.RelaySwitchError:
        expected = (None, ())
    assert seatfull.policy(pct, windows) == expected


#: (seven_day pct, reading age s, five_hour pct, cooldown entry or None)
FIXTURES: list[tuple[float, int, float, dict | None]] = [
    (40.0, 60, 1.0, None),
    (97.9, 60, 1.0, None),
    (98.0, 60, 1.0, None),
    (99.5, 899, 1.0, None),
    (99.5, 901, 1.0, None),
    (100.0, 60, 1.0, None),
    (50.0, 60, 100.0, None),
    (50.0, 6 * 3600, 1.0, None),
    (10.0, 60, 1.0, {"blocked_until": NOW + 600, "kind": "hold", "observed_at": NOW - 5}),
    (10.0, 60, 1.0, {"blocked_until": NOW + 600, "scope": "quota", "observed_at": NOW - 5}),
    (10.0, 60, 1.0, {"blocked_until": NOW - 1, "scope": "quota"}),
]


@pytest.mark.parametrize(("week", "age", "five", "cooldown"), FIXTURES)
@pytest.mark.parametrize("gate", [("seven_day",), ("seven_day", "five_hour")])
def test_judgement_equals_quota(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    week: float,
    age: int,
    five: float,
    cooldown: dict | None,
    gate: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dirs = {"private": (tmp_path / "p").resolve(), "work": (tmp_path / "w").resolve()}
    monkeypatch.setattr(config, "claude_config_dirs", lambda: dict(dirs))
    app = config.app_home()
    app.mkdir(parents=True, exist_ok=True)
    usagecache.write_usage(
        {
            "five_hour": {"used_percentage": five, "resets_at": NOW + 7200},
            "seven_day": {"used_percentage": week, "resets_at": NOW + 86400},
        },
        account="work",
        app_home=app,
        account_dir=dirs["work"],
        now=NOW - age,
    )
    if cooldown is not None:
        (app / "cooldowns.json").write_text(
            json.dumps({"providers": {"claude:work": cooldown}}), encoding="utf-8"
        )
    doc = seatfull.judge(app, dirs, 98, list(gate), NOW)
    for label in dirs:
        row = quota._claude_quota_row(label, NOW, quota.read_cooldowns(NOW))
        evidence = quota._pick_evidence(row, NOW, gate)
        mine = doc["accounts"][label]
        assert mine["full"] == quota._source_full(evidence, 98.0), label
        assert mine["room"] == quota._has_room(evidence, 98.0), label
        top = evidence["gate_max_pct"]
        assert mine["pct"] == (None if top is None else round(top, 1)), label


def test_invalid_config_fails_open(tmp_path: Path) -> None:
    doc = seatfull.judge(tmp_path, {"private": tmp_path}, 0, ["seven_day"], NOW)
    assert doc["threshold_pct"] is None
    assert doc["accounts"]["private"]["full"] is False
    assert seatfull.row_text(doc, "private") == ""


def test_keep_file_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert seatfull.keep_file(blocker / "app", {"accounts": {}}, NOW) is False


def test_row_for_an_observed_block_without_reading() -> None:
    doc = {
        "threshold_pct": 98.0,
        "accounts": {
            "private": {"full": True, "pct": None, "room": False, "window": None},
            "work": {"full": False, "pct": 6.0, "room": True, "window": "seven_day"},
        },
    }
    assert "⇄ cpriv blocked: the next relay moves to cwork (6 %)" in seatfull.row_text(
        doc, "private"
    )
    assert seatfull.row_text(doc, "work") == ""
    assert seatfull.row_text(doc, "nobody") == ""
    assert seatfull.row_text({"threshold_pct": 98.0}, "private") == ""
