"""The live (seat, model) eligibility verdict (tp#2672 B1).

A seat's eligibility comes from LIVE state, recomputed on every read and never written
to the cooldown store: the newest plan reading (``free`` blocks, fail-open otherwise)
and a recent ``refused:model`` attempt of the SAME model on that seat (24 h, with one
read-only canary after half the TTL). Several causes compose into one BLOCKED row.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import codex_launch, codex_ledger, config, quota, usage

_NOW = 1_800_000_000
_HOUR = 3600
_DAY = 86400
_MODEL = "gpt-5.6-sol"
_OTHER = "gpt-5.5"


@pytest.fixture(autouse=True)
def _known_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline: alias resolution must not read a real cache."""
    catalog = [
        {"slug": _MODEL, "visibility": "list", "default_reasoning_level": "medium"},
        {"slug": _OTHER, "visibility": "list", "default_reasoning_level": "medium"},
        {"slug": "gpt-5.4-mini", "visibility": "list", "default_reasoning_level": "medium"},
    ]
    monkeypatch.setattr(cic, "list_models", lambda **_: catalog)


# ── helpers ──────────────────────────────────────────────────────────────────────
def _row(
    pid: str = "codex:private",
    label: str = "private",
    *,
    state: str = quota.AVAILABLE,
    plan: str = "",
    plan_at: int = _NOW - 60,
    **kw: Any,
) -> quota.ProviderQuota:
    """One Codex row as the ordinary seat resolution would build it."""
    return quota.ProviderQuota(
        id=pid,
        kind="codex",
        state=state,
        source=kw.pop("source", "windows"),
        account=label,
        plan={"plan_type": plan, "captured_at": plan_at, "source": "windows"} if plan else {},
        **kw,
    )


def _attempt(
    outcome: str, ts: int, model: str = _MODEL, seat: str = "private", **extra: Any
) -> dict[str, Any]:
    """One ledger attempt line (the shape ``attempt_records`` writes)."""
    row: dict[str, Any] = {
        "ts": codex_ledger._iso(ts),  # noqa: SLF001
        "provider": "codex",
        "seat": seat,
        "id": "codex" if seat == "default" else f"codex:{seat}",
        "purpose": "test",
        "outcome": outcome,
        "ok": outcome == "ok",
        "ms": 10,
    }
    if model:
        row["model"] = model
    row.update(extra)
    return row


def _summary(*rows: dict[str, Any], now: int = _NOW) -> codex_ledger.SeatModelSummary:
    return codex_ledger.summarize_seat_models(list(rows), now)


def _judge(
    rows: list[quota.ProviderQuota],
    *attempts: dict[str, Any],
    model: str = _MODEL,
    now: int = _NOW,
) -> list[quota.ProviderQuota]:
    return quota.codex_seat_eligibility(rows, now, model, summary=_summary(*attempts, now=now))


def _write_ledger(*rows: dict[str, Any]) -> None:
    path = codex_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _free_body(now: int) -> dict[str, Any]:
    """A ``wham/usage`` body of a free plan: no 5h/7d window, one 30-day window."""
    return {
        "plan_type": "free",
        "rate_limit": {
            "primary_window": {
                "used_percent": 4,
                "limit_window_seconds": 30 * _DAY,
                "reset_at": now + 20 * _DAY,
            }
        },
    }


def _fetch_free(monkeypatch: pytest.MonkeyPatch, home: Path, now: int) -> None:
    """Record a windowless free-plan live reading for *home* (real fetch, fake body)."""
    monkeypatch.setattr(usage, "_codex_auth_tokens", lambda _home: ("tok", "acct"))
    monkeypatch.setattr(
        usage, "_get_wham_usage_body", lambda *_a, **_k: (json.dumps(_free_body(now)), 200)
    )
    snap = usage.fetch_codex_usage(home, now)
    assert snap is not None and snap.plan_type == "free" and snap.five_hour is None
    usage._codex_cache.clear()  # noqa: SLF001


def _live_plan(home: Path, plan: str, now: int) -> None:
    """A fresh windowed live reading of *plan* for *home*."""
    usage._write_codex_usage(  # noqa: SLF001
        home,
        usage.Usage(
            captured_at=now,
            five_hour=usage.Window(used_percentage=10.0, resets_at=now + 4 * _HOUR),
            seven_day=usage.Window(used_percentage=10.0, resets_at=now + 3 * _DAY),
            live=True,
            plan_type=plan,
        ),
        now,
    )
    usage._codex_cache.clear()  # noqa: SLF001


def _by_label(rows: list[quota.ProviderQuota]) -> dict[str, quota.ProviderQuota]:
    return {row.account: row for row in rows}


# ── rule 1: funding ──────────────────────────────────────────────────────────────
def test_free_live_meta_makes_seat_ineligible(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale windowed `plus` loses to a newer windowless `free`: the seat is BLOCKED."""
    stale = usage.Usage(
        captured_at=_NOW - 23 * _DAY, five_hour=None, seven_day=None, live=True, plan_type="plus"
    )
    meta = {"plan_type": "free", "captured_at": _NOW - 120, "other_windows": []}
    reading = quota._plan_reading(stale, None, meta)  # noqa: SLF001
    assert reading == {"plan_type": "free", "captured_at": _NOW - 120, "source": "live_meta"}
    row = _row(state=quota.UNKNOWN)
    row.plan = reading
    judged = quota.codex_seat_eligibility([row], _NOW)[0]
    assert judged.state == quota.BLOCKED
    assert judged.reason == "plan free (live 2m)"
    assert judged.blocked_by == "plan_type" and judged.block_scope == "funding"
    assert judged.resets_at == 0
    assert judged.blockers == [{"kind": "funding", "reason": "plan free (live 2m)", "resets_at": 0}]

    # The same through the real files and the shared row construction.
    now = int(time.time())
    _fetch_free(monkeypatch, three_seats.seats["private"], now)
    rows = _by_label(quota._codex_quotas(now, {}))  # noqa: SLF001
    assert rows["private"].state == quota.BLOCKED
    assert "plan free" in rows["private"].reason
    assert "private" not in [cand.label for cand in cic.codex_homes_in_order(now)]
    assert quota.read_cooldowns() == {}  # computed, never written to the store


def test_paid_reading_restores_seat(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer windowed `plus` reading makes the seat eligible again, no config change."""
    now = int(time.time())
    home = three_seats.seats["private"]
    _fetch_free(monkeypatch, home, now - 60)
    assert _by_label(quota._codex_quotas(now, {}))["private"].state == quota.BLOCKED  # noqa: SLF001
    _live_plan(home, "plus", now)
    row = _by_label(quota._codex_quotas(now, {}))["private"]  # noqa: SLF001
    assert row.state != quota.BLOCKED and not row.blockers
    assert row.plan["plan_type"] == "plus"
    assert "private" in [cand.label for cand in cic.codex_homes_in_order(now)]


def test_no_plan_reading_is_fail_open() -> None:
    """No plan reading at all (never fetched, fetch failing) stays eligible, no note."""
    judged = quota.codex_seat_eligibility([_row(state=quota.UNKNOWN)], _NOW)[0]
    assert judged.state == quota.UNKNOWN and not judged.blockers and judged.note == ""


def test_unknown_plan_type_is_fail_open_with_note() -> None:
    """An unrecognized plan value stays eligible and says so in the advisory note."""
    judged = quota.codex_seat_eligibility([_row(plan="galaxy", note="x")], _NOW)[0]
    assert judged.state == quota.AVAILABLE
    assert judged.note == "x · plan galaxy unknown"
    for funded in ("plus", "pro", "team", "business", "enterprise", "edu"):
        row = quota.codex_seat_eligibility([_row(plan=funded)], _NOW)[0]
        assert row.state == quota.AVAILABLE and row.note == ""


# ── rule 2: (seat, model) refusal ────────────────────────────────────────────────
def test_model_refusal_expires_and_clears() -> None:
    """2 h old blocks the pair; 25 h old does not; a later same-model ok clears it early."""
    fresh = _judge([_row()], _attempt("refused:model", _NOW - 2 * _HOUR))[0]
    assert fresh.state == quota.BLOCKED
    assert fresh.reason == f"refused:model {_MODEL} 2h ago"
    assert fresh.blocked_by == "model_refusal" and fresh.block_scope == "funding"
    assert fresh.resets_at == _NOW - 2 * _HOUR + 24 * _HOUR  # the refusal's expiry
    expired = _judge([_row()], _attempt("refused:model", _NOW - 25 * _HOUR))[0]
    assert expired.state == quota.AVAILABLE
    cleared = _judge(
        [_row()],
        _attempt("refused:model", _NOW - 2 * _HOUR),
        _attempt("ok", _NOW - _HOUR),
    )[0]
    assert cleared.state == quota.AVAILABLE


def test_other_model_success_does_not_clear_refusal() -> None:
    """An ok of ANOTHER model on the seat proves nothing about the refused one."""
    judged = _judge(
        [_row()],
        _attempt("refused:model", _NOW - 2 * _HOUR),
        _attempt("ok", _NOW - _HOUR, model=_OTHER),
    )[0]
    assert judged.state == quota.BLOCKED


def test_paid_reading_does_not_clear_refusal() -> None:
    """Funding is not model entitlement: a newer `plus` reading leaves the refusal."""
    judged = _judge([_row(plan="plus", plan_at=_NOW - 60)], _attempt("refused:model", _NOW - _HOUR))
    assert judged[0].state == quota.BLOCKED
    assert [b["kind"] for b in judged[0].blockers] == ["model_refusal"]


def test_refusal_for_other_model_does_not_skip() -> None:
    """A refusal is keyed by (seat, model): another model keeps the seat; no model skips."""
    refusal = _attempt("refused:model", _NOW - _HOUR)
    assert _judge([_row()], refusal, model=_OTHER)[0].state == quota.AVAILABLE
    assert _judge([_row()], refusal, model="")[0].state == quota.AVAILABLE
    # The alias of the refused slug is the same model.
    assert _judge([_row()], refusal, model="sol")[0].state == quota.BLOCKED
    assert _judge([_row()], refusal, model=" GPT-5.6-SOL ")[0].state == quota.BLOCKED


# ── the refusal canary ───────────────────────────────────────────────────────────
def test_refusal_canary_after_half_ttl(three_seats: SeatFixture) -> None:
    """13 h old → one canary ranked last; a second caller does not get it; a refusal re-arms."""
    del three_seats  # the temp CCC_HOME holds the canary claims
    refused_at = _NOW - 13 * _HOUR
    rows = _judge(
        [_row(), _row("codex:de", "de"), _row("codex", "default", state=quota.BLOCKED)],
        _attempt("refused:model", refused_at),
    )
    order = ["private", "de", "default"]
    ranks = quota.rank_codex_seats(rows, "", order, policy="order", now=_NOW, canary=True)
    assert [rank.row.account for rank in ranks] == ["de", "private"]
    canary = ranks[-1]
    assert canary.probe and canary.probe_kind == "refusal_canary"
    assert canary.model == _MODEL and canary.canary_at == refused_at + 12 * _HOUR
    assert canary.row.state == quota.AVAILABLE  # the pre-refusal row
    # Not offered without the canary flag (write runs, resumes) nor before half the TTL.
    plain = quota.rank_codex_seats(rows, "", order, policy="order", now=_NOW)
    assert [rank.row.account for rank in plain] == ["de"]
    early = _judge([_row()], _attempt("refused:model", _NOW - 11 * _HOUR))
    assert not quota.rank_codex_seats(early, "", order, policy="order", now=_NOW, canary=True)

    assert quota.claim_refusal_canary("codex:private", _MODEL, due_at=canary.canary_at, now=_NOW)
    assert not quota.claim_refusal_canary(
        "codex:private", _MODEL, due_at=canary.canary_at, now=_NOW + 1
    )
    again = quota.rank_codex_seats(rows, "", order, policy="order", now=_NOW + 1, canary=True)
    assert [rank.row.account for rank in again] == ["de"]  # claimed: gone for everyone

    # The canary was refused again: the TTL restarts from the new refusal.
    later = _NOW + 10 * _HOUR
    rows = _judge(
        [_row()],
        _attempt("refused:model", refused_at),
        _attempt("refused:model", _NOW),
        now=later,
    )
    assert rows[0].state == quota.BLOCKED and rows[0].resets_at == _NOW + 24 * _HOUR
    assert not quota.rank_codex_seats(rows, "", order, policy="order", now=later, canary=True)
    rearmed = _NOW + 12 * _HOUR
    rows = _judge(
        [_row()],
        _attempt("refused:model", refused_at),
        _attempt("refused:model", _NOW),
        now=rearmed,
    )
    ranks = quota.rank_codex_seats(rows, "", order, policy="order", now=rearmed, canary=True)
    assert [rank.probe_kind for rank in ranks] == ["refusal_canary"]
    assert quota.claim_refusal_canary("codex:private", _MODEL, due_at=ranks[0].canary_at)


def test_refusal_canary_key_is_per_seat_model(three_seats: SeatFixture) -> None:
    """Two refused models on one seat each get a canary; other traffic consumes neither."""
    del three_seats
    refused_at = _NOW - 13 * _HOUR
    attempts = (
        _attempt("refused:model", refused_at),
        _attempt("refused:model", refused_at, model=_OTHER),
        _attempt("ok", _NOW - _HOUR, model="gpt-5.4-mini"),  # a third model works fine
    )
    quota.record_seat_attempt("codex:private", _NOW - _HOUR)  # ordinary seat traffic
    attempts_before = quota.read_seat_attempts()
    due = {}
    for model in (_MODEL, _OTHER):
        rows = _judge([_row()], *attempts, model=model)
        ranks = quota.rank_codex_seats(rows, "", ["private"], policy="order", now=_NOW, canary=True)
        assert [rank.probe_kind for rank in ranks] == ["refusal_canary"]
        due[model] = ranks[0].canary_at
    assert quota.refusal_canary_key("codex:private", "GPT-5.6-SOL") == (
        f"model-refusal:codex:private:{_MODEL}"
    )
    assert quota.claim_refusal_canary("codex:private", _MODEL, due_at=due[_MODEL], now=_NOW)
    # The first model's claim does not consume the second model's canary.
    rows = _judge([_row()], *attempts, model=_OTHER)
    ranks = quota.rank_codex_seats(rows, "", ["private"], policy="order", now=_NOW, canary=True)
    assert [rank.model for rank in ranks] == [_OTHER]
    assert quota.claim_refusal_canary("codex:private", _OTHER, due_at=due[_OTHER], now=_NOW)
    assert set(quota.read_refusal_canaries()) == {
        f"model-refusal:codex:private:{_MODEL}",
        f"model-refusal:codex:private:{_OTHER}",
    }
    # The seat-wide probe / attempt map is untouched by either claim.
    assert quota.read_seat_attempts() == attempts_before
    assert quota.claim_probe("codex:private", now=_NOW + 2 * _DAY)  # still its own clock


def _hold_others(*labels: str) -> None:
    for label in labels:
        pid = "codex" if label == "default" else f"codex:{label}"
        quota.record_block(
            pid, blocked_until=int(time.time()) + _DAY, kind=quota.KIND_HOLD, reason="held"
        )


def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": _MODEL,
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
        "json": True,
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
        "model": _MODEL,
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


_CHATGPT_MODEL_REFUSAL = {
    "scenario": "scripted",
    "rc": 1,
    "stdout": [
        json.dumps(
            {
                "type": "error",
                "message": f"The '{_MODEL}' model is not supported when using Codex "
                "with a ChatGPT account.",
            }
        )
    ],
}


def test_refusal_canary_launches_for_read_only_run(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: one read-only call launches the canary; write runs and resumes never do."""
    now = int(time.time())
    _hold_others("de", "default")
    _write_ledger(_attempt("refused:model", now - 13 * _HOUR))

    seen: list[dict[str, Any]] = []
    real = cic.codex_homes_in_order

    def _spy(*args: Any, **kwargs: Any) -> list[cic.SeatCandidate]:
        seen.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(cic, "codex_homes_in_order", _spy)

    # A debate round selects with probe=False and still gets the canary.
    debate = cic.debate_seat_verdict(refresh=False, model=_MODEL)
    assert debate["seat"] == "private", debate
    assert debate["state"] in ("allowed", "unknown")
    assert any(call.get("canary") and call.get("probe") is False for call in seen)
    assert not quota.read_refusal_canaries()  # the verdict itself claims nothing

    # A write run is never offered it: nothing launches anywhere.
    seen.clear()
    three_seats.reset_log()
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True)) != cic.EX_OK
    assert three_seats.calls() == []
    assert seen and not any(call.get("canary") for call in seen)
    capsys.readouterr()

    # The read-only run (a pinned debate round: `run -S`) takes the canary; the seat
    # refuses the model again, and that refusal is logged as `refused:model`.
    three_seats.reset_log()
    three_seats.scenarios(private=_CHATGPT_MODEL_REFUSAL)
    assert cic.cmd_run(_run_ns(three_seats, seat="private")) == cic.EX_INVALID_MODEL
    assert three_seats.call_homes() == ["private"]
    claims = quota.read_refusal_canaries()
    assert list(claims) == [f"model-refusal:codex:private:{_MODEL}"]
    assert quota.read_seat_attempts().get("codex:private") is None  # its own namespace
    outcomes = [row["outcome"] for row in codex_ledger.read_runs()]
    assert outcomes == ["refused:model", "refused:model"]
    capsys.readouterr()

    # A second read-only caller does not get it: the refusal restarted the TTL.
    three_seats.reset_log()
    three_seats.scenarios(private={"scenario": "ok", "reply": "should not run"})
    cic.cmd_run(_run_ns(three_seats))
    assert three_seats.calls() == []
    verdict = cic.debate_seat_verdict(refresh=False, model=_MODEL)
    assert verdict["state"] not in ("allowed", "unknown"), verdict
    private = next(seat for seat in verdict["seats"] if seat["seat"] == "private")
    assert private["state"] == "blocked" and "refused:model" in private["reason"]
    capsys.readouterr()

    # A resume builds its one candidate directly — never through the canary ranking, and
    # it claims no canary (the resume's own refusal gate is tp#2672 B2).
    session = "019ff5b3-7bea-7c80-ad5e-21cc5b7c64bd"
    codex_launch.record_launch(
        session, str(three_seats.workdir), write=False, codex_home=three_seats.seats["private"]
    )
    seen.clear()
    cic.cmd_delegate(_delegate_ns(three_seats, resume=session))
    assert not any(call.get("canary") for call in seen)
    assert quota.read_refusal_canaries() == claims
    capsys.readouterr()


# ── composition ──────────────────────────────────────────────────────────────────
def _rota_row(underlying: quota.ProviderQuota, next_mine_at: int) -> quota.ProviderQuota:
    payload = {"holder": "bob", "next_mine_at": next_mine_at, "tz": "UTC"}
    return quota._rota_blocked(underlying, "rota: bob's week", payload, _NOW)  # noqa: SLF001


def test_free_plus_rota_keeps_both_causes() -> None:
    """A free seat on somebody else's rota week: one BLOCKED row, both causes, no reset."""
    row = _rota_row(_row(plan="free"), _NOW + 3 * _DAY)
    judged = quota.codex_seat_eligibility([row], _NOW)[0]
    assert judged.state == quota.BLOCKED
    assert [b["kind"] for b in judged.blockers] == ["funding", "rota"]
    assert judged.reason == "plan free (live 1m); rota: bob's week"
    assert judged.block_scope == "rota"  # the runner's typed skip and -Q still see the rota
    assert judged.resets_at == 0  # funding is untimed
    assert judged.rota["underlying"]["state"] == quota.AVAILABLE  # preserved


def test_refusal_plus_hold() -> None:
    """A hold plus a model refusal: both listed, hold first, the later reset when both timed."""
    hold = _row(
        state=quota.BLOCKED,
        source="hold",
        blocked_by="hold",
        block_scope="hold",
        reason="held",
        resets_at=_NOW + 30 * _HOUR,
    )
    judged = _judge([hold], _attempt("refused:model", _NOW - _HOUR))[0]
    assert [b["kind"] for b in judged.blockers] == ["hold", "model_refusal"]
    assert judged.reason == f"held; refused:model {_MODEL} 1h ago"
    assert judged.block_scope == "hold" and judged.blocked_by == "hold"
    assert judged.resets_at == _NOW + 30 * _HOUR
    open_hold = _row(state=quota.BLOCKED, source="hold", block_scope="hold", reason="held")
    assert _judge([open_hold], _attempt("refused:model", _NOW - _HOUR))[0].resets_at == 0


def test_refusal_plus_quota_takes_later_reset() -> None:
    """An exhausted window and a refusal: the LATER of the two clearing times."""
    exhausted = _row(
        state=quota.BLOCKED,
        blocked_by="seven_day",
        reason="7d at 100 %",
        resets_at=_NOW + 2 * _DAY,
    )
    judged = _judge([exhausted], _attempt("refused:model", _NOW - _HOUR))[0]
    assert [b["kind"] for b in judged.blockers] == ["model_refusal", "quota"]
    assert judged.resets_at == _NOW + 2 * _DAY
    soon = _row(state=quota.BLOCKED, blocked_by="five_hour", resets_at=_NOW + _HOUR)
    judged = _judge([soon], _attempt("refused:model", _NOW - _HOUR))[0]
    assert judged.resets_at == _NOW + 23 * _HOUR  # the refusal outlasts the window


def test_free_plus_quota_has_no_reset() -> None:
    """Free + an exhausted window: no reset promised; the retry projection says none."""
    exhausted = _row(
        state=quota.BLOCKED, plan="free", blocked_by="seven_day", resets_at=_NOW + 2 * _DAY
    )
    judged = quota.codex_seat_eligibility([exhausted], _NOW)[0]
    assert [b["kind"] for b in judged.blockers] == ["funding", "quota"]
    assert judged.resets_at == 0
    assert cic._debate_retry({"state": "blocked"}, judged, _NOW) == (True, None)  # noqa: SLF001


def test_free_plus_refusal_has_no_reset() -> None:
    """Free + a timed refusal: still no reset, no retry time."""
    judged = _judge([_row(plan="free")], _attempt("refused:model", _NOW - _HOUR))[0]
    assert [b["kind"] for b in judged.blockers] == ["funding", "model_refusal"]
    assert judged.blocked_by == "plan_type" and judged.resets_at == 0
    assert cic._debate_retry({"state": "blocked"}, judged, _NOW) == (True, None)  # noqa: SLF001
    # No canary either: the funding block holds the seat too.
    late = _judge([_row(plan="free")], _attempt("refused:model", _NOW - 13 * _HOUR))
    assert not quota.rank_codex_seats(late, "", [], policy="order", now=_NOW, canary=True)


def test_paid_recovery_with_other_blocker_stays_blocked() -> None:
    """A seat that is paid again but still held stays BLOCKED by the hold alone."""
    held = _row(
        state=quota.BLOCKED,
        plan="free",
        source="hold",
        block_scope="hold",
        reason="held",
        resets_at=_NOW + _DAY,
    )
    assert [b["kind"] for b in quota.codex_seat_eligibility([held], _NOW)[0].blockers] == [
        "hold",
        "funding",
    ]
    held.plan = {"plan_type": "plus", "captured_at": _NOW, "source": "windows"}
    judged = quota.codex_seat_eligibility([held], _NOW)[0]
    assert judged.state == quota.BLOCKED
    assert [b["kind"] for b in judged.blockers] == ["hold"]
    assert judged.reason == "held" and judged.resets_at == _NOW + _DAY


def test_judging_twice_is_idempotent() -> None:
    """A row judged again starts from its pre-verdict state: causes are never doubled."""
    once = _judge([_row(plan="free")], _attempt("refused:model", _NOW - _HOUR))
    twice = _judge(once, _attempt("refused:model", _NOW - _HOUR))
    assert twice[0].blockers == once[0].blockers
    assert quota.codex_seat_eligibility(once, _NOW)[0].blockers == [
        {"kind": "funding", "reason": "plan free (live 1m)", "resets_at": 0}
    ]


def test_blockers_serialize_and_rehydrate() -> None:
    """``blockers`` and ``plan`` reach the JSON row; the internal base verdict does not."""
    judged = quota.codex_seat_eligibility([_row(plan="free")], _NOW)[0]
    data = quota._provider_dict(judged)  # noqa: SLF001
    assert data["blockers"][0]["kind"] == "funding"
    assert data["plan"]["plan_type"] == "free"
    assert "pre_eligibility" not in data
    back = quota._rehydrate(data)  # noqa: SLF001
    assert back.blockers == judged.blockers and back.plan == judged.plan
    assert "blockers" not in quota._provider_dict(_row())  # noqa: SLF001


# ── the ledger summary ───────────────────────────────────────────────────────────
def test_seat_model_summary_skips_malformed_lines(tmp_path: Path) -> None:
    """Garbage lines, foreign shapes and markers never become a (seat, model) fact."""
    path = tmp_path / "codex-runs.jsonl"
    lines = [
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"no": "ts"}),
        json.dumps(_attempt("refused:model", _NOW - _HOUR, ts_bad=True) | {"ts": "garbage"}),
        json.dumps(_attempt("refused:model", _NOW - _HOUR) | {"purpose": "marker"}),
        json.dumps(_attempt("refused:model", _NOW - _HOUR, provider="claude")),
        json.dumps(_attempt("refused:model", _NOW - 2 * _HOUR)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = codex_ledger.seat_model_summary(path, now=_NOW)
    assert summary.models == {("codex:private", _MODEL): {"ok": 0, "refused": _NOW - 2 * _HOUR}}
    assert summary.last_runs["codex:private"]["outcome"] == "refused:model"
    assert not codex_ledger.seat_model_summary(tmp_path / "missing.jsonl").models


def test_seat_model_summary_unknown_model() -> None:
    """A row with no model names no pair, but still stamps the seat's last run."""
    summary = _summary(_attempt("refused:model", _NOW - _HOUR, model=""))
    assert not summary.models
    assert summary.pair("codex:private", _MODEL) == {"ok": 0, "refused": 0}
    assert summary.last_runs["codex:private"]["runs_24h"] == 1
    # A seat-only row (no id) is attributed through its label; models are case-folded.
    summary = _summary(_attempt("ok", _NOW - _HOUR, model="GPT-5.6-SOL", id=""))
    assert summary.pair("codex:private", _MODEL)["ok"] == _NOW - _HOUR


def test_seat_model_summary_single_pass(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quota snapshot reads the ledger ONCE for last_run stamps and the verdict."""
    del three_seats
    _write_ledger(_attempt("ok", int(time.time()) - 60))
    calls: list[int] = []
    real = codex_ledger.read_runs

    def _counting(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(codex_ledger, "read_runs", _counting)
    snap = quota.snapshot()
    assert len(calls) == 1
    private = next(p for p in snap["providers"] if p["id"] == "codex:private")
    assert private["last_run"]["outcome"] == "ok"
    # The legacy entry point still answers the same mapping from the same fold.
    rows = real()
    assert codex_ledger.last_runs(rows) == codex_ledger.summarize_seat_models(rows).last_runs


# ── config ───────────────────────────────────────────────────────────────────────
def test_refusal_ttl_config_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hours in (0, 720]; a bool, a string, 0, negative or > 720 is refused by name."""
    assert config.DEFAULTS["codex_model_refusal_ttl_h"] == 24
    assert config.codex_model_refusal_ttl_h(config.Config()) == 24.0
    assert config.codex_model_refusal_ttl_h(config.Config(codex_model_refusal_ttl_h=1.5)) == 1.5
    assert config.codex_model_refusal_ttl_h(config.Config(codex_model_refusal_ttl_h=720)) == 720
    for bad in (0, -1, 721, True, "24"):
        cfg = config.Config(codex_model_refusal_ttl_h=bad)  # type: ignore[arg-type]
        with pytest.raises(config.RefusalTtlError, match="codex_model_refusal_ttl_h"):
            config.codex_model_refusal_ttl_h(cfg)
    # A bad value falls back to the default instead of unblocking (or blocking forever).
    monkeypatch.setattr(config, "load_config", lambda: config.Config(codex_model_refusal_ttl_h=0))
    assert quota._model_refusal_ttl_sec() == 24 * _HOUR  # noqa: SLF001
    monkeypatch.setattr(config, "load_config", lambda: config.Config(codex_model_refusal_ttl_h=2))
    assert quota._model_refusal_ttl_sec() == 2 * _HOUR  # noqa: SLF001
    assert _judge([_row()], _attempt("refused:model", _NOW - 3 * _HOUR))[0].state == (
        quota.AVAILABLE
    )
