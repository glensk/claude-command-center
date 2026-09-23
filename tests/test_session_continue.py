"""`claude-session-continue` — the ported reset-wait / resume engine.

Exercises the CLI surface ccc's auto-resume relies on (positional id + ``now``,
``-w/--wait-only`` + ``--signal-file``, the claude-missing exit code) and the pure
time / limit-message parsers, and the probe's run-ledger row (one per probe,
purpose ``probe-claude-session``). Never invokes a real ``claude``.
"""

from __future__ import annotations

import json
import subprocess
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
