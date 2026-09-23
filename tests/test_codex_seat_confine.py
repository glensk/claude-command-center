"""`codex-in-claude run -S/--seat` and `-X/--caller` (tp#392, plan C2).

`-S` confines ONE call to ONE seat: the ladder resolver in ai.py walks the qualifying
Codex seats itself, so the runner must never hop on its behalf — a refusal or an
ineligible seat comes back as a typed failure and nothing persistent (pin, order) moves.
`-X` names the caller on every ledger line, and `run -j` returns the `attempt_id` of each
line it wrote so the caller can reference exactly those.

Seats come from the `three_seats` fixture (order private → de → default) and `codex` is
`tests/fakes/fake_codex.py`, exactly as in `test_codex_ledger.py`.
"""

from __future__ import annotations

import argparse
import json
import time

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import codex_ledger, quota

_MODEL = "gpt-5.6-sol"


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline — `valid_slug` must not shell out to codex."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [{"slug": _MODEL, "visibility": "list", "default_reasoning_level": "medium"}],
    )


def _run(seats: SeatFixture, capsys: pytest.CaptureFixture[str], **kw: object) -> tuple[int, dict]:
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": _MODEL,
        "effort": "low",
        "timeout": 60,
        "idle_timeout": 0,
        "purpose": "cheap",
        "max_attempts": 0,
        "persist": False,
        "ignore_quota": False,
        "ephemeral": False,
        "headroom": False,
        "min_remaining": None,
        "json": True,
    }
    base.update(kw)
    capsys.readouterr()
    code = cic.cmd_run(argparse.Namespace(**base))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _ledger() -> list[dict]:
    return codex_ledger.read_runs()


def test_parser_offers_seat_and_caller_short_options() -> None:
    args = cic.build_parser().parse_args(["run", "-S", "codex-de", "-X", "ai.py:abc", "hi"])
    assert args.seat == "codex-de" and args.caller == "ai.py:abc"


@pytest.mark.parametrize("name", ["de", "codex:de", "codex-de"])
def test_seat_runs_only_there_in_every_spelling(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """private leads the order, but `-S de` never touches it."""
    three_seats.scenarios(de={"scenario": "ok", "reply": "de answered"})
    code, out = _run(three_seats, capsys, seat=name)
    assert code == cic.EX_OK and out["ok"] and out["reply"] == "de answered"
    assert out["seat"]["label"] == "de"
    assert three_seats.call_homes() == ["de"]


def test_a_refusing_seat_fails_typed_and_never_hops(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.scenarios(private="refuse_quota")
    code, out = _run(three_seats, capsys, seat="private")
    assert code == cic.EX_QUOTA and not out["ok"]
    assert out["error"]["kind"] == "seat_refused"
    assert three_seats.call_homes() == ["private"], "no fallback to de/default"
    # The refusal is still a physical attempt: one ledger line, its id in the envelope.
    rows = _ledger()
    assert [r["attempt_id"] for r in rows] == out["attempt_ids"]


def test_an_ineligible_seat_is_unavailable_without_a_process(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    quota.record_block("codex:de", scope="hold", blocked_until=int(time.time()) + 3600)
    code, out = _run(three_seats, capsys, seat="codex-de")
    assert code == cic.EX_QUOTA
    assert out["error"]["kind"] == "seat_unavailable"
    assert three_seats.call_homes() == []
    assert out["attempt_ids"] == [] and not _ledger()


def test_an_unknown_seat_is_a_usage_error(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(three_seats, capsys, seat="nosuch")
    assert code == cic.EX_USAGE
    assert out["error"]["kind"] == "seat_unknown"
    assert three_seats.call_homes() == []


def test_seat_leaves_the_pin_and_order_alone(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    before_cfg = three_seats.cic_config.read_bytes() if three_seats.cic_config.exists() else b""
    toml = three_seats.ccc_home / "command-center" / "config.toml"
    before_toml = toml.read_bytes()
    _run(three_seats, capsys, seat="de")
    after_cfg = three_seats.cic_config.read_bytes() if three_seats.cic_config.exists() else b""
    assert after_cfg == before_cfg and toml.read_bytes() == before_toml
    # …and the next unconfined call walks the normal order again.
    three_seats.scenarios()
    _run(three_seats, capsys)
    assert three_seats.call_homes()[-1] == "private"


def test_caller_and_attempt_ids_reach_the_ledger(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unconfined hop: two lines, each with its own attempt_id, both carrying -X."""
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "x"})
    code, out = _run(three_seats, capsys, caller="ai.py:0123abcd")
    assert code == cic.EX_OK
    rows = _ledger()
    assert [r["caller"] for r in rows] == ["ai.py:0123abcd"] * 2
    ids = [r["attempt_id"] for r in rows]
    assert len(set(ids)) == 2 and all(len(i) == 32 for i in ids)
    assert out["attempt_ids"] == ids
    assert [a["attempt_id"] for a in out["attempts"]] == ids


def test_a_skip_has_no_attempt_id(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    quota.record_block("codex:private", scope="hold", blocked_until=int(time.time()) + 3600)
    _code, out = _run(three_seats, capsys)
    skipped = [a for a in out["attempts"] if a["outcome"].startswith("skipped:")]
    for attempt in skipped:
        assert attempt["attempt_id"] is None
    assert len(out["attempt_ids"]) == len(out["attempts"]) - len(skipped)
