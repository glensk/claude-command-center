"""The Codex run ledger (`codex-runs.jsonl`) — one line per physical attempt.

Why it exists (2026-09-21): a 2 % reading on the shared team seat could not be attributed
from this machine — the runner's ephemeral runs left no rollout, the attempts file keeps
one stamp per seat, and `ai logs` never sees Codex (ai.py excludes that seat). The
ledger is the durable answer: written by `run_with_fallback` after every round, read by
`ccc quota` (each Codex row's `last_run` + the table's `last run … ago` note) and by
`ai logs` (ai.py) through the `codex_runs_log` pointer on `-j`.

The seats come from the `three_seats` fixture and `codex` is `tests/fakes/fake_codex.py`,
exactly as in `test_codex_runner.py`.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import codex_ledger, quota
from command_center.cli import _quota_last_run_note

_MODEL = "gpt-5.6-sol"


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline — `valid_slug` must not shell out to codex."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [{"slug": _MODEL, "visibility": "list", "default_reasoning_level": "medium"}],
    )


def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": _MODEL,
        "effort": "low",
        "timeout": 60,
        "idle_timeout": 0,
        "purpose": "checker",
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


def _ledger_rows(seats: SeatFixture) -> list[dict]:
    path = seats.ccc_home / "command-center" / codex_ledger.LEDGER_NAME
    assert path == codex_ledger.ledger_path(), "the ledger lives in ccc's app home"
    return codex_ledger.read_runs(path)


# ── written by the runner ────────────────────────────────────────────────────────────
def test_a_hop_writes_one_line_per_physical_attempt(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """private refuses → de serves: two lines, each with ITS seat, outcome and time."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1234")
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de answered"})
    before = time.time()
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    rows = _ledger_rows(three_seats)
    assert [(r["seat"], r["id"], r["outcome"], r["ok"]) for r in rows] == [
        ("private", "codex:private", "refused:quota", False),
        ("de", "codex:de", "ok", True),
    ]
    for row in rows:
        assert row["purpose"] == "checker"
        assert row["model"] == _MODEL and row["effort"] == "low"
        assert row["prompt_chars"] == len("reply OK")
        assert row["write"] is False
        assert row["cwd"] == str(three_seats.workdir)
        assert row["session"] == "sess-1234"
        assert row["ms"] >= 0
        stamp = datetime.fromisoformat(row["ts"])
        assert stamp.tzinfo is not None, "local ISO with offset, like ai.py's calls-*.jsonl"
        assert before - 1 <= stamp.timestamp() <= time.time() + 1
    refused, served = rows
    assert refused["error"] == "refused:quota"
    assert "error_message" not in refused, "the runner's prose belongs to the LAST attempt"
    assert "error" not in served
    # The fake's `turn.completed` carries `usage.input_tokens: 10` on the ok stream.
    assert served["tokens_in"] == 10 and served["tokens_out"] == 0
    assert "tokens_in" not in refused, "a refusal reports no usage"


def test_a_skipped_seat_is_not_spend_and_not_written(three_seats: SeatFixture) -> None:
    """A held seat is skipped without a process — and without a ledger line."""
    quota.record_block("codex:private", scope="hold", blocked_until=int(time.time()) + 3600)
    three_seats.scenarios(de={"scenario": "ok", "reply": "de answered"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    rows = _ledger_rows(three_seats)
    assert [r["seat"] for r in rows] == ["de"]
    assert three_seats.call_homes() == ["de"]


def test_an_unwritable_ledger_never_fails_the_call(three_seats: SeatFixture) -> None:
    """Best-effort: the Codex reply is delivered even when the ledger cannot be written."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "answered"})
    blocker = three_seats.ccc_home / "command-center" / codex_ledger.LEDGER_NAME
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.mkdir()  # a DIRECTORY where the file should be: every open() fails
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    assert not codex_ledger.read_runs(blocker)


# ── read back ────────────────────────────────────────────────────────────────────────
def _line(ts: str, pid: str, **kw: object) -> str:
    row: dict[str, object] = {
        "ts": ts,
        "seat": pid.partition(":")[2] or "default",
        "id": pid,
        "purpose": "checker",
        "outcome": "ok",
        "ok": True,
        "ms": 6100,
    }
    row.update(kw)
    return json.dumps(row)


def test_read_runs_skips_junk_and_last_runs_summarises_per_seat(tmp_path) -> None:
    now = int(datetime.fromisoformat("2026-09-21T11:30:00+02:00").timestamp())
    ledger = tmp_path / "codex-runs.jsonl"
    ledger.write_text(
        "\n".join(
            [
                _line("2026-09-19T09:00:00+02:00", "codex"),  # older than 24 h
                "not json",
                json.dumps({"no": "ts"}),
                _line("2026-09-21T10:33:11+02:00", "codex:de"),
                _line(
                    "2026-09-21T10:33:39+02:00",
                    "codex",
                    outcome="refused:quota",
                    ok=False,
                    ms=900,
                ),
                _line("2026-09-21T11:00:00+02:00", "codex", purpose="debate", ms=42000),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rows = codex_ledger.read_runs(ledger)
    assert len(rows) == 4
    last = codex_ledger.last_runs(rows, now)
    assert set(last) == {"codex", "codex:de"}
    assert last["codex"]["purpose"] == "debate"
    assert last["codex"]["ok"] is True and last["codex"]["ms"] == 42000
    assert last["codex"]["age_s"] == 30 * 60
    assert last["codex"]["runs_24h"] == 2, "the refusal counts; the 2-day-old run does not"
    assert last["codex:de"]["runs_24h"] == 1
    assert last["codex:de"]["age_s"] == 56 * 60 + 49
    assert not codex_ledger.read_runs(tmp_path / "absent.jsonl")


# ── surfaced by ccc quota ────────────────────────────────────────────────────────────
def test_snapshot_carries_last_run_per_codex_seat_and_the_ledger_path(
    three_seats: SeatFixture,
) -> None:
    three_seats.scenarios(private={"scenario": "ok", "reply": "answered"})
    assert cic.cmd_run(_run_ns(three_seats, purpose="vet")) == cic.EX_OK
    snap = quota.snapshot(now=int(time.time()))
    assert snap["codex_runs_log"] == str(codex_ledger.ledger_path())
    rows = {prov["id"]: prov for prov in snap["providers"]}
    served = rows["codex:private"]["last_run"]
    assert served["purpose"] == "vet" and served["ok"] is True and served["runs_24h"] == 1
    assert served["age_s"] >= 0
    assert "last_run" not in rows["codex:de"], "a seat never launched carries no field"
    assert "last_run" not in rows["copilot"]
    # The serialized row round-trips (what `ccc quota -p` reads back).
    assert quota._rehydrate(rows["codex:private"]).last_run == served  # noqa: SLF001
    assert not quota._rehydrate(rows["codex:de"]).last_run  # noqa: SLF001


def test_table_note_reads_as_prose() -> None:
    assert _quota_last_run_note({}) == ""
    assert _quota_last_run_note({"last_run": {}}) == ""
    ok = {"age_s": 58 * 60, "purpose": "checker", "outcome": "ok", "ok": True, "ms": 6100}
    assert _quota_last_run_note({"last_run": {**ok, "runs_24h": 1}}) == (
        "last run 58m ago · checker · 6s"
    )
    assert _quota_last_run_note({"last_run": {**ok, "runs_24h": 3}}) == (
        "last run 58m ago · checker · 6s (3 in 24h)"
    )
    refused = {**ok, "outcome": "refused:quota", "ok": False, "ms": 900, "runs_24h": 1}
    assert _quota_last_run_note({"last_run": refused}) == (
        "last run 58m ago · checker · 1s · refused:quota"
    )
