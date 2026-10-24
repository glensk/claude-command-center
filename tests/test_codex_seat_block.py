"""A seat the runner refuses is shown blocked everywhere, with until when (tp#1202).

On 2026-10-24 `codex-in-claude order` showed `default ✅ available (5h 94% · wk 15%)`
while `run -S default` answered `seat_unavailable — included usage limit reached (no
credit overflow), unblocks in 2h 24m`. The runner re-measures every candidate with an old
reading right before it ranks (`_pre_selection_refresh`); `order` judged the seat on the
2-hour-old reading it had. What is guarded here:

* `seat_block` turns every BLOCKED row (usage-limit refusal, hold, rota, observed
  refusal) into one compact reason plus the unblock time in Swiss local time, and leaves
  available seats and dead logins (their own `needs login` surface) alone;
* `order`, `headroom` and `usage` print `⛔ blocked — usage limit reached · until HH:MM`
  for such a seat, their `-j` outputs carry it under `blocked`, and the headroom verdict
  never comes from it;
* the three commands run the runner's own re-measure first (`-n` skips it), so a stale
  healthy reading can no longer outvote the block the runner is about to find.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import config, quota, usage

_LABEL = "included usage limit reached (no credit overflow)"


def _swiss(ts: int) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Europe/Zurich")).strftime("%H:%M")


def _live_reading(
    home: Path, *, used: float, blocked: bool, captured: int, resets: int
) -> usage.Usage:
    """Leave *home* a live usage cache entry — what the runner's fetch writes."""
    snap = usage.Usage(
        captured_at=captured,
        five_hour=usage.Window(used_percentage=used, resets_at=resets),
        seven_day=usage.Window(used_percentage=15.0, resets_at=captured + 5 * 86400),
        blocked_reason=_LABEL if blocked else "",
        blocked_at=captured if blocked else 0,
        live=True,
        email="team@example.org",
        plan_type="team",
    )
    usage._write_codex_usage(home, snap, captured)  # noqa: SLF001
    usage._codex_cache.clear()  # noqa: SLF001
    return snap


def _block_default(three_seats: SeatFixture) -> int:
    """Seat ``default`` refused with the usage limit; returns when it unblocks."""
    now = int(time.time())
    resets = now + 2 * 3600 + 18 * 60
    three_seats.measure("private", "de")
    _live_reading(
        three_seats.seats["default"], used=100.0, blocked=True, captured=now, resets=resets
    )
    return resets


def _ns(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {"labels": [], "clear": False, "json": False, "no_refresh": False}
    base.update(kw)
    return argparse.Namespace(**base)


# ── the one reading every surface prints ──────────────────────────────────────────
def _fields(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "label": "default",
        "id": "codex",
        "email": "team@example.org",
        "state": quota.BLOCKED,
        "reason": _LABEL,
        "blocked_by": "five_hour",
        "block_scope": "",
        "resets_at": 0,
    }
    base.update(kw)
    return base


def test_seat_block_names_the_usage_limit_and_the_swiss_unblock_time() -> None:
    now = int(time.time())
    block = cic.seat_block(_fields(resets_at=now + 3600), now)
    assert block is not None
    assert block["reason"] == "usage limit reached"
    assert block["until"] == now + 3600
    assert block["until_local"] == _swiss(now + 3600)
    assert block["text"] == f"usage limit reached · until {_swiss(now + 3600)}"


def test_seat_block_far_deadline_carries_the_day_and_unknown_one_none() -> None:
    now = int(time.time())
    far = now + 3 * 86400
    block = cic.seat_block(_fields(resets_at=far), now)
    assert block is not None
    expected = datetime.fromtimestamp(far, ZoneInfo("Europe/Zurich")).strftime("%a %d.%m. %H:%M")
    assert block["until_local"] == expected
    open_ended = cic.seat_block(_fields(blocked_by="refusal", resets_at=0), now)
    assert open_ended is not None
    assert open_ended["until"] is None and open_ended["text"] == "usage limit reached"


def test_seat_block_prefixes_holds_rota_and_observed_refusals() -> None:
    now = int(time.time())
    hold = cic.seat_block(_fields(blocked_by="hold", reason="private held"), now)
    rota = cic.seat_block(_fields(blocked_by="rota", reason="rota: 19.10.–25.10. used by a"), now)
    seen = cic.seat_block(
        _fields(blocked_by="observed-rejection", reason="codex exec refused: quota"), now
    )
    assert hold is not None and hold["text"] == "hold: private held"
    assert rota is not None and rota["text"] == "rota: 19.10.–25.10. used by a"  # no "rota: rota:"
    assert seen is not None and seen["text"] == "observed-rejection: codex exec refused: quota"


def test_seat_block_ignores_available_seats_and_dead_logins() -> None:
    now = int(time.time())
    assert cic.seat_block(_fields(state=quota.AVAILABLE, blocked_by="", reason=""), now) is None
    assert cic.seat_block(_fields(block_scope="auth", blocked_by="observed-rejection"), now) is None


# ── what order / headroom / usage show ────────────────────────────────────────────
def test_order_shows_the_runners_block_with_until(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    resets = _block_default(three_seats)
    assert cic.cmd_order(_ns()) == cic.EX_OK
    line = next(row for row in capsys.readouterr().out.splitlines() if "default" in row)
    assert "⛔ blocked" in line and "available" not in line
    assert f"usage limit reached · until {_swiss(resets)}" in line

    assert cic.cmd_order(_ns(json=True)) == cic.EX_OK
    payload = json.loads(capsys.readouterr().out)
    assert [b["label"] for b in payload["blocked"]] == ["default"]
    assert payload["blocked"][0]["until"] == resets
    assert payload["blocked"][0]["until_local"] == _swiss(resets)
    assert "default" not in [c["label"] for c in payload["candidates"]]


def test_headroom_lists_the_block_and_never_answers_from_it(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    resets = _block_default(three_seats)
    decision = cic.codex_headroom()
    assert decision["seat"] != "default"
    assert "default" not in [seat["label"] for seat in decision["seats"]]
    assert [b["label"] for b in decision["blocked"]] == ["default"]

    cic.cmd_headroom(_ns())
    text = capsys.readouterr().out
    assert f"default: ⛔ blocked — usage limit reached · until {_swiss(resets)}" in text
    cic.cmd_headroom(_ns(json=True))
    assert json.loads(capsys.readouterr().out)["blocked"][0]["until"] == resets


def test_headroom_blocked_candidate_carries_until(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under an explicit $CODEX_HOME the blocked seat IS the candidate: its verdict says so."""
    resets = _block_default(three_seats)
    monkeypatch.setenv("CODEX_HOME", str(three_seats.seats["default"]))
    decision = cic.codex_headroom()
    assert decision["state"] == "blocked" and decision["blocked"] == []
    seat = decision["seats"][0]
    assert seat["until"] == resets and seat["until_local"] == _swiss(resets)
    assert decision["reason"] == f"usage limit reached · until {_swiss(resets)}"


def test_usage_prints_and_carries_the_block(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    resets = _block_default(three_seats)
    cic.cmd_usage(_ns())
    assert f"default: ⛔ blocked — usage limit reached · until {_swiss(resets)}" in (
        capsys.readouterr().out
    )
    cic.cmd_usage(_ns(json=True))
    assert json.loads(capsys.readouterr().out)["blocked"][0]["label"] == "default"


def test_usage_json_has_no_blocked_key_when_nothing_is_blocked(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.measure()
    cic.cmd_usage(_ns(json=True))
    assert "blocked" not in json.loads(capsys.readouterr().out)


def test_runner_status_report_uses_the_same_wording(three_seats: SeatFixture) -> None:
    resets = _block_default(three_seats)
    lines, earliest = cic.seat_status_report()
    line = next(row for row in lines if row.startswith("default:"))
    assert f"usage limit reached · until {_swiss(resets)}" in line and "unblocks in" in line
    assert earliest == resets


# ── the runner's re-measure, shared by the displays ───────────────────────────────
def test_displays_remeasure_a_stale_reading_like_the_runner(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-10-24 case: an old 94 % reading said available; the fetch says blocked."""
    now = int(time.time())
    resets = now + 2 * 3600
    three_seats.measure("private", "de")
    old = now - 20 * 60  # older than codex_usage_refresh_sec, so the runner refetches
    _live_reading(
        three_seats.seats["default"], used=94.0, blocked=False, captured=old, resets=resets
    )
    fetched: list[Path] = []

    def _fetch(home: Path, now: int | None = None, *, timeout: float | None = None) -> usage.Usage:
        del timeout
        fetched.append(home)
        at = int(time.time()) if now is None else now
        return _live_reading(home, used=100.0, blocked=True, captured=at, resets=resets)

    monkeypatch.setattr(cic, "_usage_feedback_on", lambda: True)
    monkeypatch.setattr(usage, "fetch_codex_usage", _fetch)
    assert config.load_config().codex_usage_refresh_sec == 600  # 20 min old = stale

    assert cic.cmd_order(_ns(no_refresh=True)) == cic.EX_OK
    line = next(row for row in capsys.readouterr().out.splitlines() if "default" in row)
    assert "✅ available" in line and not fetched  # -n: the cached reading, as before

    assert cic.cmd_order(_ns()) == cic.EX_OK
    line = next(row for row in capsys.readouterr().out.splitlines() if "default" in row)
    assert three_seats.seats["default"] in fetched
    assert "⛔ blocked" in line and f"until {_swiss(resets)}" in line


def test_runner_and_displays_share_one_refresh_step(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del three_seats
    calls: list[list[str]] = []
    monkeypatch.setattr(cic, "_usage_feedback_on", lambda: True)
    monkeypatch.setattr(
        cic, "_pre_selection_refresh", lambda cands, _budget: calls.append([c.label for c in cands])
    )
    cic.cmd_order(_ns())
    cic.cmd_headroom(_ns())
    cic.cmd_usage(_ns())
    capsys.readouterr()
    expected = [c.label for c in cic.codex_homes_in_order()]
    assert calls == [expected, expected, expected]
    cic.cmd_order(_ns(no_refresh=True))
    cic.cmd_headroom(_ns(no_refresh=True))
    cic.cmd_usage(_ns(no_refresh=True))
    assert len(calls) == 3
    monkeypatch.setattr(cic, "_usage_feedback_on", lambda: False)
    cic.refresh_seat_readings()
    assert len(calls) == 3  # codex_usage off: nothing is fetched, as in the runner
