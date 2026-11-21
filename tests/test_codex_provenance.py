"""Codex seat provenance (tp#2500 B1): the runner's reply digest and its verifier.

A debate seat's answer counts toward a converged unanimous decision only when the TOOL
wrote its evidence, never the debating agent. What is pinned:

* every physical attempt's ledger row carries ``source = "runner"``, and one that produced
  a reply carries ``reply_sha256`` — the sha256 of the bytes its run log stores as
  ``reply.txt``; ``run -j`` reports the serving attempt's digest next to ``attempt_ids``;
* ``codex-in-claude provenance -a ID`` re-hashes the stored reply against the runner row:
  exit 0 verified, 1 not found / hand-written / digest mismatch, 3 unknown;
* an edited reply file reads ``digest_ok: false``; a row without ``source = "runner"``
  never verifies, even when it names a real run log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest
from conftest import SeatFixture

# The runner suite's offline model catalog (autouse) and argv namespace.
from test_codex_runner import _known_model  # noqa: F401  # pylint: disable=unused-import
from test_codex_runner import _run_ns as _runner_ns

from command_center import codex_in_claude as cic
from command_center import codex_ledger, codex_runlog


def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    """A ``run -j`` namespace labelled like a codex-review debate round."""
    base: dict[str, object] = {"purpose": "debate", "note": "plan r1"}
    base.update(kw)
    return _runner_ns(seats, **base)


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _provenance(capsys: pytest.CaptureFixture[str], attempt_id: str) -> tuple[int, dict]:
    """``codex-in-claude provenance -a ID -j`` through the real parser."""
    code = cic.main(["provenance", "-a", attempt_id, "-j"])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _served(
    seats: SeatFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> dict:
    """One ok debate round on ``private``; its ``run -j`` envelope."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-prov")
    seats.scenarios(private={"scenario": "ok", "reply": "pick: A\n```answers\n[]\n```"})
    assert cic.cmd_run(_run_ns(seats)) == cic.EX_OK
    return _envelope(capsys)


# ── written by the runner ────────────────────────────────────────────────────────────
def test_a_run_row_carries_the_digest_and_the_subcommand_verifies_it(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _served(three_seats, capsys, monkeypatch)
    (attempt_id,) = envelope["attempt_ids"]
    reply_path = Path(envelope["run_dir"]) / codex_runlog.REPLY_NAME
    digest = hashlib.sha256(reply_path.read_bytes()).hexdigest()
    assert reply_path.read_text(encoding="utf-8") == envelope["reply"]
    assert envelope["reply_sha256"] == digest
    assert envelope["attempts"][0]["reply_sha256"] == digest
    (row,) = codex_ledger.read_runs()
    assert row["source"] == codex_ledger.SOURCE_RUNNER
    assert row["reply_sha256"] == digest and row["attempt_id"] == attempt_id
    record = codex_runlog.read_record(Path(envelope["run_dir"]))
    assert record is not None and record["reply_sha256"] == digest

    code, report = _provenance(capsys, attempt_id)
    assert code == codex_ledger.PROVENANCE_VERIFIED, report
    assert report == {
        "attempt_id": attempt_id,
        "model": row["model"],
        "seat": "private",
        "purpose": "debate",
        "session": "sess-prov",
        "ts": row["ts"],
        "reply_sha256": digest,
        "digest_ok": True,
        "reply_path": str(reply_path),
        "verified": True,
        "why": "",
    }


def test_a_refused_hop_is_a_runner_row_without_a_digest(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refused attempt wrote no reply: ``source`` yes, ``reply_sha256`` no — and it
    never verifies; the serving attempt does."""
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    refused, served = codex_ledger.read_runs()
    assert refused["source"] == served["source"] == "runner"
    assert "reply_sha256" not in refused
    assert envelope["attempts"][0]["reply_sha256"] is None
    assert envelope["reply_sha256"] == served["reply_sha256"]
    code, report = _provenance(capsys, refused["attempt_id"])
    assert code == codex_ledger.PROVENANCE_REFUSED
    assert "no reply_sha256" in report["why"] and report["verified"] is False
    assert _provenance(capsys, served["attempt_id"])[0] == codex_ledger.PROVENANCE_VERIFIED


def test_a_failed_run_reports_no_top_level_digest(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.scenarios(private="model_capacity")
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_MODEL_CAPACITY
    assert _envelope(capsys)["reply_sha256"] is None


# ── what never verifies ──────────────────────────────────────────────────────────────
def test_an_edited_reply_file_reads_digest_ok_false(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _served(three_seats, capsys, monkeypatch)
    reply_path = Path(envelope["run_dir"]) / codex_runlog.REPLY_NAME
    reply_path.write_text("pick: B\n```answers\n[]\n```", encoding="utf-8")
    code, report = _provenance(capsys, envelope["attempt_ids"][0])
    assert code == codex_ledger.PROVENANCE_REFUSED
    assert report["digest_ok"] is False and report["verified"] is False
    assert "digest mismatch" in report["why"]


def test_a_row_without_source_runner_never_verifies(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hand-appended row naming a REAL run log with the right digest still fails: only
    ``source = "runner"`` counts (``ccc record-run`` refuses that key)."""
    envelope = _served(three_seats, capsys, monkeypatch)
    (runner_row,) = codex_ledger.read_runs()
    forged = {k: v for k, v in runner_row.items() if k != "source"}
    forged["attempt_id"] = "forged-attempt"
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    code, report = codex_ledger.verify_provenance("forged-attempt", ledger)
    assert code == codex_ledger.PROVENANCE_REFUSED
    assert "hand-written" in report["why"] and report["digest_ok"] is False
    assert report["seat"] == "private", "the report still says what the row claims"
    # The same row with `source` but pointing at the real run log: run.json disagrees.
    forged["source"] = "runner"
    ledger.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    code, report = codex_ledger.verify_provenance("forged-attempt", ledger)
    assert code == codex_ledger.PROVENANCE_REFUSED
    assert "another attempt" in report["why"] and report["digest_ok"] is False
    assert envelope["run_dir"] == forged["run_dir"]


def test_a_record_run_row_reusing_the_attempt_id_does_not_shadow_the_runner_row(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _served(three_seats, capsys, monkeypatch)
    attempt_id = envelope["attempt_ids"][0]
    claude = codex_ledger.validate_row(
        {
            "provider": "claude",
            "seat": "work",
            "purpose": "debate",
            "outcome": "ok",
            "ok": True,
            "ms": 1,
            "attempt_id": attempt_id,
        }
    )
    codex_ledger.append_rows([claude])
    code, report = _provenance(capsys, attempt_id)
    assert code == codex_ledger.PROVENANCE_VERIFIED and report["seat"] == "private"


@pytest.mark.parametrize(
    ("ledger_text", "attempt", "code", "why"),
    [
        (None, "abc", codex_ledger.PROVENANCE_REFUSED, "no run ledger"),
        ("", "abc", codex_ledger.PROVENANCE_REFUSED, "not in the run ledger"),
        ("junk\n", " ", codex_ledger.PROVENANCE_REFUSED, "empty attempt id"),
        (
            '{"attempt_id":"abc","source":"runner","reply_sha256":"00"}\n' * 2,
            "abc",
            codex_ledger.PROVENANCE_UNKNOWN,
            "ambiguous",
        ),
        (
            '{"attempt_id":"abc","source":"runner","reply_sha256":"00"}\n',
            "abc",
            codex_ledger.PROVENANCE_UNKNOWN,
            "names no run log",
        ),
    ],
)
def test_verify_provenance_exit_codes(
    tmp_path: Path, ledger_text: str | None, attempt: str, code: int, why: str
) -> None:
    ledger = tmp_path / "codex-runs.jsonl"
    if ledger_text is not None:
        ledger.write_text(ledger_text, encoding="utf-8")
    got, report = codex_ledger.verify_provenance(attempt, ledger)
    assert got == code and why in report["why"]
    assert report["verified"] is False and report["digest_ok"] is False


def test_a_pruned_reply_is_unknown(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _served(three_seats, capsys, monkeypatch)
    (Path(envelope["run_dir"]) / codex_runlog.REPLY_NAME).unlink()
    code, report = _provenance(capsys, envelope["attempt_ids"][0])
    assert code == codex_ledger.PROVENANCE_UNKNOWN
    assert "stored reply unreadable" in report["why"]


def test_run_logs_off_keep_the_digest_but_never_verify(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(codex_runlog.RUNLOG_SWITCH_ENV, "0")
    envelope = _served(three_seats, capsys, monkeypatch)
    assert envelope["run_dir"] is None
    assert envelope["reply_sha256"] == codex_runlog.reply_digest(envelope["reply"])
    code, report = _provenance(capsys, envelope["attempt_ids"][0])
    assert code == codex_ledger.PROVENANCE_UNKNOWN and report["verified"] is False


def test_the_stored_reply_is_scrubbed_and_the_digest_covers_the_stored_bytes() -> None:
    reply = "token sk-proj-abcdefghijklmnopqrstuvwxyz0123 here"
    stored = codex_runlog.reply_bytes(reply)
    assert b"[redacted]" in stored
    assert codex_runlog.reply_digest(reply) == hashlib.sha256(stored).hexdigest()
    assert codex_runlog.reply_digest("") == ""


def test_text_mode_prints_the_verdict(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _served(three_seats, capsys, monkeypatch)
    assert cic.main(["provenance", "--attempt", envelope["attempt_ids"][0]]) == 0
    out = capsys.readouterr().out
    assert "verified: True" in out and "digest_ok: True" in out
