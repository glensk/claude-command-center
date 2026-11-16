"""`claude-session-continue` — the ported reset-wait / resume engine.

Exercises the CLI surface ccc's auto-resume relies on (positional id + ``now``,
``-w/--wait-only`` + ``--signal-file``, the claude-missing exit code) and the pure
time / limit-message parsers, and the probe's run-ledger row (one per probe,
purpose ``probe-claude-session``). Never invokes a real ``claude``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from command_center import accounts, codex_ledger
from command_center import session_continue as sc


# ------------------------------ CLI surface / arg parsing ------------------------------ #
def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        sc.parse_args(["--help"])
    assert exc.value.code == 0


def test_positional_id_and_time() -> None:
    args = sc.parse_args(["0199-uuid", "now"])
    assert args.session_id == "0199-uuid"
    assert args.time == "now"
    assert not args.wait_only


def test_wait_only_signal_file_contract() -> None:
    # The exact form resume.py spawns: `<script> auto --wait-only --signal-file <f>`.
    args = sc.parse_args(["auto", "--wait-only", "--signal-file", "/tmp/sig"])
    assert args.session_id == "auto"
    assert args.wait_only is True
    assert args.signal_file == "/tmp/sig"


def test_short_flags_present() -> None:
    args = sc.parse_args(["id", "-w", "-s", "/tmp/x", "-n", "-d"])
    assert args.wait_only and args.signal_file == "/tmp/x"
    assert args.no_prompt and args.dry_run


# ------------------------------ build_command ------------------------------ #
def test_build_command_resume_with_defaults() -> None:
    args = sc.parse_args(["myid"])
    assert sc.build_command(args, "myid") == [
        "claude",
        "--resume",
        "myid",
        "--dangerously-skip-permissions",
        "--",
        sc.DEFAULT_PROMPT,
    ]


def test_build_command_no_prompt_no_skip() -> None:
    args = sc.parse_args(["myid", "--no-prompt", "--no-skip-permissions"])
    assert sc.build_command(args, "myid") == ["claude", "--resume", "myid"]


def test_build_command_last_uses_continue() -> None:
    args = sc.parse_args(["last"])
    assert sc.build_command(args, "last")[:2] == ["claude", "--continue"]


# ------------------------------ run_wait_only ------------------------------ #
def test_wait_only_now_writes_signal(tmp_path: Path) -> None:
    sig = tmp_path / "reset.signal"
    args = sc.parse_args(["now", "--wait-only", "--signal-file", str(sig)])
    assert sc.run_wait_only(args) == 0
    assert sig.exists() and sig.read_text().strip()  # a timestamp was written


def test_wait_only_dry_run_writes_nothing(tmp_path: Path) -> None:
    sig = tmp_path / "reset.signal"
    args = sc.parse_args(["now", "--wait-only", "--signal-file", str(sig), "--dry-run"])
    assert sc.run_wait_only(args) == 0
    assert not sig.exists()


# ------------------------------ main guardrails (no real claude) ------------------------------ #
def test_main_without_claude_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    assert sc.main(["myid", "now"]) == 1


# ------------------------------ CLAUDE_HOME override ------------------------------ #
def test_claude_home_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "custom"))
    assert sc._claude_home() == str(tmp_path / "custom")
    path = sc.session_file_path("abc")
    assert str(tmp_path / "custom") in path and path.endswith("abc.jsonl")


# ------------------------------ pure parsers ------------------------------ #
def test_parse_clock_time_variants() -> None:
    assert sc.parse_clock_time("1:10am") == (1, 10)
    assert sc.parse_clock_time("13:45") == (13, 45)
    assert sc.parse_clock_time("1am") == (1, 0)
    assert sc.parse_clock_time("nonsense") is None


def test_compute_start_rolls_to_tomorrow() -> None:
    now = datetime(2026, 7, 8, 12, 0, 0)
    # A time already past today lands tomorrow (+1 min margin).
    target = sc.compute_start(now, 1, 10)
    assert target.day == 9 and (target.hour, target.minute) == (1, 11)


def test_parse_limit_message_epoch_and_relative() -> None:
    now = datetime(2026, 7, 8, 12, 0, 0)
    epoch = sc.parse_limit_message("Claude AI usage limit reached|1749600600", now)
    assert epoch is not None
    rel = sc.parse_limit_message("resets in 2h 7m", now)
    assert rel is not None and rel.hour == 14 and rel.minute == 8  # +2h07m +1m margin


# ------------------------------ the probe's ledger row ------------------------------ #
def _envelope(result: str, *, is_error: bool = False) -> str:
    return json.dumps(
        {
            "result": result,
            "is_error": is_error,
            "session_id": "probe-sess",
            "duration_api_ms": 321,
            "modelUsage": {"claude-haiku-4-5-20251001": {}},
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
    )


def _fake_probe(monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int = 0) -> list[Any]:
    seen: list[Any] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    monkeypatch.setattr(sc.subprocess, "run", run)
    return seen


def test_probe_asks_for_json_and_records_one_ok_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    seen = _fake_probe(monkeypatch, _envelope("Hello!"))
    assert sc.probe_limit() == (False, None)
    assert seen[0][seen[0].index("--output-format") + 1] == "json"
    (row,) = codex_ledger.read_runs()
    assert (row["provider"], row["seat"], row["purpose"]) == (
        "claude",
        "private",
        "probe-claude-session",
    )
    assert row["outcome"] == "ok" and row["ok"] is True
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert (row["tokens_in"], row["tokens_out"], row["session"]) == (4, 2, "probe-sess")


def test_limit_in_json_result_still_parses_and_is_a_quota_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_probe(monkeypatch, _envelope("Claude AI usage limit reached|1749600600", is_error=True))
    limited, target = sc.probe_limit()
    assert limited and target is not None
    (row,) = codex_ledger.read_runs()
    assert row["outcome"] == "error:quota" and row["ok"] is False
    assert "limit reached" in row["error_message"]


def test_plain_text_output_still_detects_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_probe(monkeypatch, "You've hit your session limit - resets 1:10am", rc=1)
    limited, target = sc.probe_limit()
    assert limited and target is not None
    assert codex_ledger.read_runs()[0]["outcome"] == "error:quota"


def test_timeout_is_a_row_too(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(sc.subprocess, "run", run)
    assert sc.probe_limit() == (True, None)
    (row,) = codex_ledger.read_runs()
    assert row["outcome"] == "error:timeout" and row["ok"] is False


def test_probe_row_seat_follows_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / ".claude-work"
    work.mkdir()
    monkeypatch.setattr(
        accounts.config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / ".claude", "work": work},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    _fake_probe(monkeypatch, _envelope("hi"))
    sc.probe_limit()
    assert codex_ledger.read_runs()[0]["seat"] == "work"


def test_a_broken_ledger_never_breaks_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_rows: list[dict[str, Any]], path: Path | None = None) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(codex_ledger, "append_rows", boom)
    _fake_probe(monkeypatch, _envelope("hi"))
    assert sc.probe_limit() == (False, None)


def test_probe_envelope_carries_usd_and_one_hour_cache() -> None:
    fields = sc._envelope_fields(  # noqa: SLF001
        {
            "total_cost_usd": 0.004,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 4,
                "cache_creation_input_tokens": 900,
                "cache_creation": {"ephemeral_1h_input_tokens": 600},
            },
        }
    )
    assert fields["usd"] == 0.004
    assert fields["tokens_cache_create_1h"] == 600


# ------------------------- tp#1697 B14: no automated seat probe ------------------------ #
def _no_claude(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Any subprocess is recorded; a `claude` one fails the test outright."""
    seen: list[list[str]] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        assert cmd[0] != "claude", "an automated caller must never run `claude -p`"
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(sc.subprocess, "run", run)
    return seen


@pytest.mark.parametrize("value", ["1", "observe"])
def test_automated_probe_runs_no_claude_and_uses_the_ccusage_estimate(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AI_AUTOMATED", value)
    seen = _no_claude(monkeypatch)
    estimate = datetime(2026, 11, 5, 14, 1)
    monkeypatch.setattr(sc, "ccusage_reset_target", lambda: estimate)
    assert sc.probe_limit() == (True, estimate)
    assert not seen
    assert not codex_ledger.read_runs()  # no probe, no probe row


def test_automated_wait_waits_for_the_estimate_once_without_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_AUTOMATED", "1")
    seen = _no_claude(monkeypatch)
    estimate = datetime(2026, 11, 5, 14, 1)
    monkeypatch.setattr(sc, "ccusage_reset_target", lambda: estimate)
    waited: list[datetime] = []
    monkeypatch.setattr(sc, "wait_until", waited.append)
    sc.auto_wait_loop()
    assert waited == [estimate]
    assert not seen


def test_automated_without_an_estimate_does_not_auto_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ccusage estimate ⇒ the detector exits non-zero and writes NO reset signal."""
    monkeypatch.setenv("AI_AUTOMATED", "1")
    seen = _no_claude(monkeypatch)
    monkeypatch.setattr(sc, "ccusage_reset_target", lambda: None)
    monkeypatch.setattr(sc, "wait_until", lambda _t: pytest.fail("must not wait"))
    monkeypatch.setattr(sc.shutil, "which", lambda _name: "/usr/bin/true")
    signal_file = tmp_path / "reset.signal"
    with pytest.raises(SystemExit) as exc:
        sc._main(["auto", "--wait-only", "--signal-file", str(signal_file)])
    assert exc.value.code not in (0, None)
    assert not signal_file.exists()
    assert not seen


def test_attended_probe_still_runs_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_AUTOMATED", raising=False)
    seen = _fake_probe(monkeypatch, _envelope("Hello!"))
    assert sc.probe_limit() == (False, None)
    assert seen and seen[0][0] == "claude"


# ------------------------------ input ledger (tp#2170 P10) ------------------------------ #
P10_SID = "33333333-4444-5555-6666-777777777777"


def _ledger_rows() -> list[dict[str, Any]]:
    """Every ``inputs`` row (the tp#2170 ledger) of the test store (``CCC_HOME`` is pinned)."""
    from command_center.store import Store

    with Store() as store:
        cur = store.conn.execute("SELECT * FROM inputs ORDER BY rowid")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _resume_now(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> tuple[int, list[int]]:
    """Run ``main(argv)`` up to the exec (stubbed); ``(exit, rows seen at the exec)``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "acct"))
    monkeypatch.setattr(sc.shutil, "which", lambda name: "/usr/bin/true")
    monkeypatch.setattr(sc, "build_command", lambda _args, sid: ["claude", "--resume", sid])
    seen: list[int] = []
    monkeypatch.setattr(sc, "_exec_attested", lambda _cmd: seen.append(len(_ledger_rows())))
    return sc.main(argv), seen


def test_resume_ledgers_the_prompt_before_the_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P10: one ``limit_resume`` / ``automation`` row of ``args.prompt`` before the exec;
    no column or log line carries the text."""
    from command_center.input_ledger import content_sha

    prompt = "carry on with the quokka plan"
    code, seen = _resume_now(monkeypatch, tmp_path, [P10_SID, "now", "-p", prompt])
    assert code == 1 and seen == [1]  # the stubbed exec returned
    (row,) = _ledger_rows()
    assert (row["kind"], row["origin"], row["state"]) == ("limit_resume", "automation", "pending")
    assert row["session_id"] == P10_SID and row["content_sha"] == content_sha(prompt)
    assert row["alt_sha"] == "" and row["config_dir"] == str(tmp_path / "acct")
    assert row["cwd"] == str(Path.cwd()) and str(row["transcript_path"]).endswith(".jsonl")
    out = capsys.readouterr()
    for blob in [*map(str, row.values()), out.err]:
        assert "quokka" not in blob


def test_resume_default_prompt_is_ledgered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from command_center.input_ledger import content_sha

    _resume_now(monkeypatch, tmp_path, [P10_SID, "now"])
    (row,) = _ledger_rows()
    assert row["content_sha"] == content_sha(sc.DEFAULT_PROMPT)


def test_resume_no_prompt_writes_no_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P10: ``-n/--no-prompt`` sends no prompt, so it writes no row."""
    code, seen = _resume_now(monkeypatch, tmp_path, [P10_SID, "now", "-n"])
    assert code == 1 and seen == [0] and not _ledger_rows()


def test_resume_dry_run_writes_no_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    code, seen = _resume_now(monkeypatch, tmp_path, [P10_SID, "now", "-d"])
    assert code == 0 and not seen and not _ledger_rows()


def test_resume_unresolved_last_writes_an_unbound_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D5: a ``last`` that resolves to nothing leaves the row unbound (``session_id ''``)."""
    monkeypatch.setattr(sc, "resolve_last_session", lambda: None)
    _resume_now(monkeypatch, tmp_path, ["last", "now"])
    (row,) = _ledger_rows()
    assert (row["kind"], row["session_id"], row["transcript_path"]) == ("limit_resume", "", "")


def test_resume_resolved_last_binds_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sc, "resolve_last_session", lambda: P10_SID)
    _resume_now(monkeypatch, tmp_path, ["last", "now"])
    (row,) = _ledger_rows()
    assert row["session_id"] == P10_SID


def test_resume_exec_failure_marks_the_row_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(_cmd: list[str]) -> None:
        raise FileNotFoundError("claude")

    _resume_now(monkeypatch, tmp_path, [P10_SID, "now"])  # stubs everything
    monkeypatch.setattr(sc, "_exec_attested", _boom)
    with pytest.raises(FileNotFoundError):
        sc.main([P10_SID, "now"])
    rows = _ledger_rows()
    assert [(r["state"], r["reason"]) for r in rows] == [
        ("pending", ""),
        ("failed", "exec_failed"),
    ]


def test_session_continue_stays_stdlib_only_at_import() -> None:
    """P10: the ledger is imported lazily — importing the module pulls in no store/ledger."""
    probe = (
        "import sys, command_center.session_continue; "
        "print(','.join(sorted(m for m in sys.modules if m.startswith('command_center'))))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = set(done.stdout.strip().split(","))
    assert "command_center.input_ledger" not in loaded
    assert "command_center.store" not in loaded
    assert loaded <= {
        "command_center",
        "command_center.session_continue",
        "command_center.brokenpipe",
    }
