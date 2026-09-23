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
import io
import json
import time
from datetime import datetime

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import codex_ledger, quota
from command_center.cli import _quota_last_run_note, cmd_record_run
from command_center.cli import main as ccc_main

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
    assert cic.cmd_run(_run_ns(three_seats, note="  #255\tticket\n")) == cic.EX_OK
    rows = _ledger_rows(three_seats)
    assert [(r["seat"], r["id"], r["outcome"], r["ok"]) for r in rows] == [
        ("private", "codex:private", "refused:quota", False),
        ("de", "codex:de", "ok", True),
    ]
    for row in rows:
        assert row["purpose"] == "checker"
        assert row["provider"] == "codex"
        # `run -N`: the same sanitized note on EVERY attempt — a hop keeps its context.
        assert row["note"] == "#255 ticket"
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
    assert snap["llm_runs_log"] == snap["codex_runs_log"], "two keys, ONE file (no rename)"
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


# ── Claude rows (`ccc record-run`) ───────────────────────────────────────────────────
def _claude_row(**kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 1,
        "provider": "claude",
        "seat": "work",
        "purpose": "checker",
        "note": "#255",
        "requested_model": "opus",
        "model": "claude-opus-5",
        "outcome": "ok",
        "ok": True,
        "ms": 6100,
        "llm_ms": 5400,
        "prompt_chars": 34000,
        "tokens_in": 12,
        "tokens_out": 40,
        "tokens_cache_read": 30000,
        "cwd": "/x/sdsc-automations",
    }
    row.update(kw)
    return row


def _record(monkeypatch: pytest.MonkeyPatch, payload: object, **ns: object) -> tuple[int, str]:
    """Run `ccc record-run` with *payload* on stdin; `(exit, stderr)`."""
    err = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("sys.stderr", err)
    base: dict[str, object] = {"file": None, "source": "-", "quiet": True}
    base.update(ns)
    return cmd_record_run(argparse.Namespace(**base)), err.getvalue()


def test_record_run_appends_a_validated_claude_row(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One object or an array; `id` defaults to the ccc oracle id, `note` is sanitized."""
    code, err = _record(monkeypatch, _claude_row(note=" #255\n(ticket) "))
    assert (code, err) == (0, "")
    code, _ = _record(
        monkeypatch,
        [_claude_row(seat="private", outcome="usage-limit", ok=False), _claude_row()],
    )
    assert code == 0
    rows = _ledger_rows(three_seats)
    assert [(r["provider"], r["seat"], r["id"], r["outcome"]) for r in rows] == [
        ("claude", "work", "claude:work", "ok"),
        ("claude", "private", "claude:private", "usage-limit"),
        ("claude", "work", "claude:work", "ok"),
    ]
    first = rows[0]
    assert first["note"] == "#255 (ticket)"
    assert first["requested_model"] == "opus" and first["model"] == "claude-opus-5"
    assert first["tokens_cache_read"] == 30000 and first["llm_ms"] == 5400
    assert "schema_version" not in first, "the version is the input contract, not a column"
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"provider": "claude"}, "missing 'seat'"),
        (_claude_row(provider="mistral"), "provider 'mistral'"),
        (_claude_row(ms="6100"), "'ms' must be int"),
        (_claude_row(ok=1), "'ok' must be bool"),
        (_claude_row(ticket="#255"), "unknown key(s) ticket"),
        (_claude_row(schema_version=2), "schema_version 2"),
        (_claude_row(ts="yesterday"), "not ISO-8601"),
        ([], "empty array"),
        ("not json", "not a JSON object"),
    ],
)
def test_record_run_refuses_malformed_input_and_writes_nothing(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, payload: object, reason: str
) -> None:
    """Exit 2 names the offending key; the WHOLE batch is validated before any append."""
    code, err = _record(monkeypatch, [payload, _claude_row()] if payload != [] else payload)
    assert code == 2, err
    assert reason in err
    assert not _ledger_rows(three_seats), "a malformed batch records nothing"


def test_record_run_takes_the_positional_dash_and_a_file(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`ccc record-run -` (what sdsc-automations runs) and `ccc record-run PATH` both
    parse through the real argparse — the first live run failed on exactly this."""
    from command_center.cli import main as ccc_main

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_claude_row())))
    assert ccc_main(["record-run", "-q", "-"]) == 0
    payload = tmp_path / "rows.json"
    payload.write_text(json.dumps([_claude_row(seat="private")]), encoding="utf-8")
    assert ccc_main(["record-run", "-q", str(payload)]) == 0
    assert [r["seat"] for r in _ledger_rows(three_seats)] == ["work", "private"]


def test_record_run_exits_1_when_the_append_fails(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer never lies: an unwritable ledger is a non-zero exit, not a silent 0."""
    blocker = three_seats.ccc_home / "command-center" / codex_ledger.LEDGER_NAME
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.mkdir()
    code, err = _record(monkeypatch, _claude_row())
    assert code == 1 and "cannot append" in err


def test_claude_rows_never_stamp_a_codex_seat(three_seats: SeatFixture) -> None:
    """`last_runs` (→ `ccc quota` last_run) is Codex-only; a row without `provider` is a
    Codex row written before the field existed and still counts."""
    now = int(datetime.fromisoformat("2026-09-21T11:30:00+02:00").timestamp())
    legacy = json.loads(_line("2026-09-21T11:00:00+02:00", "codex:de"))
    assert "provider" not in legacy
    claude = codex_ledger.validate_row(_claude_row(ts="2026-09-21T11:20:00+02:00"))
    runs = codex_ledger.last_runs([legacy, claude], now)
    assert list(runs) == ["codex:de"], "the newer Claude row is the other family's spend"
    assert runs["codex:de"]["runs_24h"] == 1
    assert codex_ledger.provider_of(legacy) == "codex"
    assert codex_ledger.provider_of(claude) == "claude"
    # The snapshot path: a Claude row on disk leaves every Codex row's last_run alone.
    codex_ledger.append_rows([claude])
    snap = quota.snapshot(now=now)
    assert all("last_run" not in p for p in snap["providers"] if p["id"].startswith("codex"))


def test_sanitize_note_collapses_and_caps() -> None:
    assert codex_ledger.sanitize_note(None) == ""
    assert codex_ledger.sanitize_note("  #255\t\x00ticket \n ") == "#255 ticket"
    assert len(codex_ledger.sanitize_note("x" * 500)) == codex_ledger.NOTE_CHARS


# ── tp#392: every routed provider family, `caller`, `attempt_id` ─────────────────────
@pytest.mark.parametrize(
    "provider", ["agy", "opencode", "copilot", "gemini", "openai", "anthropic", "claude", "codex"]
)
def test_record_run_takes_every_routed_provider_family(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    code, err = _record(monkeypatch, _claude_row(provider=provider, seat="free"))
    assert (code, err) == (0, "")
    (row,) = _ledger_rows(three_seats)
    assert row["provider"] == provider and row["id"] == f"{provider}:free"


def test_every_row_gets_a_unique_attempt_id_unless_it_brings_one(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`attempt_id` is per PHYSICAL attempt (unlike `id`, the seat's oracle id); a writer
    that must reference its line later — ai.py — supplies its own and it is kept."""
    code, _ = _record(
        monkeypatch,
        [_claude_row(), _claude_row(), _claude_row(attempt_id="ai-7f3e", caller=" ai.py:\tpush ")],
    )
    assert code == 0
    first, second, third = _ledger_rows(three_seats)
    assert first["id"] == second["id"] == "claude:work"
    assert first["attempt_id"] != second["attempt_id"]
    assert len(first["attempt_id"]) == 32
    assert third["attempt_id"] == "ai-7f3e"
    assert third["caller"] == "ai.py: push", "sanitized like `note`"
    assert "caller" not in first


def test_record_run_type_checks_the_new_keys(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, err = _record(monkeypatch, _claude_row(attempt_id=7))
    assert code == 2 and "'attempt_id' must be str" in err
    code, err = _record(monkeypatch, _claude_row(caller=["ai.py"]))
    assert code == 2 and "'caller' must be str" in err
    assert not _ledger_rows(three_seats)


def test_relabel_reply_history_dry_run_and_atomic_apply(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    path = codex_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    before = _line("2026-09-21T19:11:13+02:00", "codex:de")
    genuine = _line("2026-09-22T10:00:00+02:00", "codex:de", note="#255")
    legacy = _line("2026-09-22T10:01:00+02:00", "codex:de")
    malformed = "not json"
    original = "\n".join((before, genuine, legacy, malformed)) + "\n"
    path.write_text(original, encoding="utf-8")

    assert ccc_main(["ledger-relabel"]) == 0
    dry = capsys.readouterr().out
    assert "would relabel 1 row(s)" in dry and "line 3" in dry
    assert path.read_text(encoding="utf-8") == original
    assert not path.with_name(path.name + ".bak").exists()

    assert ccc_main(["ledger-relabel", "--apply"]) == 0
    applied = capsys.readouterr().out
    assert "relabelled 1 row(s)" in applied
    assert path.with_name(path.name + ".bak").read_text(encoding="utf-8") == original
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == before and rows[1] == genuine and rows[3] == malformed
    changed = json.loads(rows[2])
    assert changed["purpose"] == "reply-2nd-opinion"
    assert changed["relabelled_from"] == "checker"

    assert ccc_main(["ledger-relabel", "--apply"]) == 0
    assert "relabelled 0 row(s)" in capsys.readouterr().out


def test_relabelled_from_is_an_optional_record_run_field() -> None:
    row = codex_ledger.validate_row(
        _claude_row(purpose="reply-2nd-opinion", relabelled_from="checker")
    )
    assert row["relabelled_from"] == "checker"


def test_non_codex_families_never_stamp_a_codex_seat() -> None:
    now = int(datetime.fromisoformat("2026-09-23T11:30:00+02:00").timestamp())
    rows = [
        codex_ledger.validate_row(
            _claude_row(provider=p, seat="default", ts="2026-09-23T11:00:00+02:00")
        )
        for p in ("agy", "opencode", "copilot")
    ]
    assert not codex_ledger.last_runs(rows, now)
