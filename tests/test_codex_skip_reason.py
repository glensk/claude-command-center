"""`ccc quota`, `headroom` and `order` say WHY a Codex seat is skipped (tp#2672 B4).

The live eligibility verdict (B1/B2) leaves a seat out for funding (a newest plan
reading of ``free``) or a fresh same-model ``refused:model``. Every surface renders the
same phrase: ``codex-priv (free) skipped — plan free (live 3m)`` and
``refused:model gpt-x 2h ago, retry in 22h``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import SeatFixture

from command_center import cli, codex_ledger, quota, usage
from command_center import codex_in_claude as cic

_H = 3600
_SLUG = "gpt-x"
_FREE = "codex-priv (free) skipped — plan free (live 3m)"
_REFUSED = f"refused:model {_SLUG} 2h ago, retry in 22h"


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [{"slug": _SLUG, "visibility": "list", "default_reasoning_level": "medium"}],
    )


def _plan(home: Path, plan: str, *, age: int = 0) -> None:
    """A live windowed reading of *plan* for *home*, *age* seconds old (10 % / 10 %)."""
    now = int(time.time())
    at = now - age
    usage._write_codex_usage(  # noqa: SLF001
        home,
        usage.Usage(
            captured_at=at,
            five_hour=usage.Window(used_percentage=10.0, resets_at=now + 4 * _H),
            seven_day=usage.Window(used_percentage=10.0, resets_at=now + 72 * _H),
            live=True,
            plan_type=plan,
        ),
        at,
    )
    usage._codex_cache.clear()  # noqa: SLF001


def _refused(seat: str, age: int) -> None:
    """One ``refused:model`` attempt of :data:`_SLUG` on *seat*, *age* seconds old."""
    row = {
        "ts": codex_ledger._iso(time.time() - age),  # noqa: SLF001
        "provider": "codex",
        "seat": seat,
        "id": "codex" if seat == "default" else f"codex:{seat}",
        "purpose": "test",
        "model": _SLUG,
        "outcome": "refused:model",
        "ok": False,
        "ms": 10,
    }
    path = codex_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _line(out: str, needle: str) -> str:
    """The one output line holding *needle* (fails loudly with the whole output)."""
    hits = [line for line in out.splitlines() if needle in line]
    assert hits, out
    return hits[0]


# ── pure ──────────────────────────────────────────────────────────────────────────
def test_skip_text_appends_retry_only_to_a_live_refusal() -> None:
    now = 1_800_000_000
    refusal = {"kind": "model_refusal", "reason": "refused:model m 2h ago", "resets_at": now}
    funding = {"kind": "funding", "reason": "plan free (live 3m)", "resets_at": 0}
    live = {**refusal, "resets_at": now + 22 * _H - 30}  # rounded UP to the minute
    assert quota.skip_text("refused:model m 2h ago", [live], now) == (
        "refused:model m 2h ago, retry in 22h"
    )
    assert quota.skip_text("plan free (live 3m)", [funding], now) == "plan free (live 3m)"
    composed = "plan free (live 3m); refused:model m 2h ago"
    assert quota.skip_text(composed, [funding, live], now) == (
        "plan free (live 3m); refused:model m 2h ago, retry in 22h"
    )
    assert quota.skip_text("refused:model m 2h ago", [refusal], now) == "refused:model m 2h ago"
    assert quota.eligibility_skipped([funding]) and quota.eligibility_skipped([live])
    assert not quota.eligibility_skipped([{"kind": "quota", "reason": "x", "resets_at": 1}])
    assert not quota.eligibility_skipped(None)


# ── funding: plan free ────────────────────────────────────────────────────────────
def test_free_seat_skip_reason_in_quota_headroom_and_order(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _plan(three_seats.seats["private"], "free", age=180)
    _plan(three_seats.seats["de"], "plus")
    _plan(three_seats.seats["default"], "team")

    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert _FREE in _line(out, "codex-priv (free) skipped")

    assert cli.main(["quota", "-j"]) in (0, 1, 2)
    rows = {r["label"]: r for r in json.loads(capsys.readouterr().out)["codex_seat_order"]}
    assert rows["private"]["plan_type"] == "free"
    assert [b["kind"] for b in rows["private"]["blockers"]] == ["funding"]
    assert rows["de"]["plan_type"] == "plus" and rows["de"]["blockers"] == []

    cic.main(["headroom", "-n"])
    out = capsys.readouterr().out
    assert _line(out, "skipped —").startswith("private: ⛔ ")
    assert _FREE in out

    cic.main(["headroom", "-n", "-j"])
    blocked = json.loads(capsys.readouterr().out)["blocked"]
    private = next(b for b in blocked if b["label"] == "private")
    assert private["skipped"] is True and private["plan_type"] == "free"
    assert private["text"] == "plan free (live 3m)" and private["until"] is None

    assert cic.main(["order", "-n"]) == cic.EX_OK
    out = capsys.readouterr().out
    assert _FREE in _line(out, "private")


# ── model refusal ─────────────────────────────────────────────────────────────────
def test_refused_seat_skip_reason_carries_retry_time(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    for label in ("private", "de", "default"):
        _plan(three_seats.seats[label], "plus")
    _refused("private", 2 * _H)

    assert cli.main(["quota", "-M", _SLUG]) == 0
    out = capsys.readouterr().out
    assert f"codex-priv (plus) skipped — {_REFUSED}" in out

    assert cli.main(["quota", "-j", "-M", _SLUG]) in (0, 1, 2)
    rows = {r["label"]: r for r in json.loads(capsys.readouterr().out)["codex_seat_order"]}
    assert rows["private"]["skip_reason"] == _REFUSED

    cic.main(["headroom", "-n", "-m", _SLUG])
    out = capsys.readouterr().out
    assert f"private: ⛔ codex-priv (plus) skipped — {_REFUSED}" in out

    assert cic.main(["order", "-n", "-m", _SLUG]) == cic.EX_OK
    out = capsys.readouterr().out
    assert f"codex-priv (plus) skipped — {_REFUSED}" in _line(out, "private")

    # Another model's caller is not refused: no skip line at all.
    assert cli.main(["quota"]) == 0
    assert "skipped —" not in capsys.readouterr().out


def test_quota_footer_lists_no_skip_line_when_every_seat_is_eligible(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    for label in ("private", "de", "default"):
        _plan(three_seats.seats[label], "plus")
    assert cli.main(["quota"]) == 0
    assert "skipped" not in capsys.readouterr().out
