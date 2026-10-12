"""Tests for a failed Codex run's CAUSE, its KIND/exit code, and the per-attempt run log.

The failure guarded here: on 2026-10-12 a debate round died after four minutes with a
reply file reading ``ERROR: Codex codex_failed: Codex exited 1:`` — nothing after the
colon. ``codex exec --json`` had reported ``turn.failed: Selected model is at capacity``
on STDOUT and nothing on stderr, the runner quoted only the stderr tail, and the run was
``--ephemeral``, so no file anywhere still held the cause. What is pinned:

* :func:`failure_cause` is never empty and prefers the server's ``turn.failed`` message;
* :func:`classify_task_failure` gives that failure a kind with its own exit code;
* every physical attempt leaves a run-log directory (``run.json`` + streams) named in the
  ``-j`` envelope, the ledger and the text-mode stderr, with secrets redacted;
* retention bounds the directory; ``runlog`` lists/shows it.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import pytest
from conftest import SeatFixture

# The runner suite's offline model catalog (autouse) and argv namespace.
from test_codex_runner import _known_model  # noqa: F401  # pylint: disable=unused-import
from test_codex_runner import _run_ns as _runner_ns

from command_center import codex_in_claude as cic
from command_center import codex_ledger, codex_runlog

_FIXTURES = Path(__file__).parent / "fixtures" / "codex_json"
_MODEL = "gpt-5.6-sol"
_CAPACITY = "Selected model is at capacity. Please try a different model."


def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    """The runner suite's `run` namespace, labelled like a codex-review debate round."""
    base: dict[str, object] = {"purpose": "debate", "note": "plan r1"}
    base.update(kw)
    return _runner_ns(seats, **base)


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _events(name: str) -> list[dict]:
    return cic.parse_json_events((_FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8"))


# ── cause extraction ──────────────────────────────────────────────────────────────
def test_cause_of_the_2026_10_12_failure_is_the_turn_failed_message() -> None:
    """The real shape: the cause is ONLY in the turn.failed event, stderr is empty."""
    assert cic.failure_cause(1, _events("model_capacity"), "") == _CAPACITY


def test_cause_prefers_turn_failed_over_retry_error_events() -> None:
    events: list[dict] = [
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "turn.failed", "error": {"message": "stream disconnected"}},
        {"type": "error", "message": "Reconnecting... 2/5"},
    ]
    assert cic.failure_cause(1, events, "") == "stream disconnected"


def test_cause_falls_back_to_stderr_tail_without_the_stdin_banner() -> None:
    stderr = "Reading additional input from stdin...\nline a\n\nline b\nline c\nline d\n"
    assert cic.failure_cause(1, [], stderr) == "line b | line c | line d"


def test_cause_falls_back_to_loose_stdout_then_the_exit_status() -> None:
    assert cic.failure_cause(1, [], "", "{}\nsomething odd\n") == "something odd"
    bare = cic.failure_cause(1, [{"type": "turn.started"}], "")
    assert bare and "exit 1" in bare and "turn.started" in bare
    assert "SIGKILL" in cic.failure_cause(-9, [], "")


def test_cause_is_one_scrubbed_bounded_line() -> None:
    token = "sk-proj-" + "A" * 40
    stderr = f"auth header Bearer {'b' * 30}\nkey {token}\n" + "x" * 2000
    cause = cic.failure_cause(1, [], stderr)
    assert "\n" not in cause
    assert len(cause) <= codex_runlog.CAUSE_CHARS
    assert token not in cause and "b" * 30 not in cause
    assert codex_runlog.REDACTED in cause


# ── classification ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("rc", "events", "stderr", "kind"),
    [
        (1, "model_capacity", "", "model_capacity"),
        (1, "server_overloaded", "", "model_capacity"),
        (
            1,
            [
                {
                    "type": "turn.failed",
                    "error": {"message": "The 'x' model is not supported when using Codex"},
                }
            ],
            "",
            "model_unavailable",
        ),
        (2, [], "error: unexpected argument '--frob' found\n\nUsage: codex exec", "codex_usage"),
        (1, [], "Error loading config.toml: bad key", "codex_usage"),
        (-11, [], "", "codex_crashed"),
        (101, [], "thread 'main' panicked at src/x.rs:1:1", "codex_crashed"),
        (1, [], "Traceback: ValueError: the task itself failed", "codex_failed"),
    ],
)
def test_classify_task_failure(rc: int, events: object, stderr: str, kind: str) -> None:
    stream = _events(events) if isinstance(events, str) else events
    assert cic.classify_task_failure(rc, stream, stderr)[0] == kind  # type: ignore[arg-type]


def test_capacity_is_still_not_a_seat_refusal() -> None:
    """Capacity is the model's state, not the seat's: no hop, no cooldown."""
    assert cic.classify_codex_failure(1, _events("model_capacity"), "").kind == ""


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("model_capacity", cic.EX_MODEL_CAPACITY),
        ("model_unavailable", cic.EX_INVALID_MODEL),
        ("codex_usage", cic.EX_CODEX_USAGE),
        ("codex_crashed", cic.EX_CODEX_CRASH),
        ("codex_failed", cic.EX_CODEX_FAIL),
        ("seat_refused_midrun", cic.EX_CODEX_FAIL),
        ("timeout", cic.EX_TIMEOUT),
        ("network", cic.EX_NETWORK),
        ("all_seats_unavailable", cic.EX_QUOTA),
        ("something_new", cic.EX_CODEX_FAIL),
    ],
)
def test_exit_code_per_kind(kind: str, code: int) -> None:
    assert cic._run_exit_code(cic.RunResult(error_kind=kind)) == code  # noqa: SLF001


def test_new_exit_codes_are_distinct_from_the_old_ones() -> None:
    old = {
        cic.EX_OK,
        cic.EX_USAGE,
        cic.EX_INVALID_MODEL,
        cic.EX_NO_CODEX,
        cic.EX_TIMEOUT,
        cic.EX_CODEX_FAIL,
        cic.EX_BAD_PATCH,
        cic.EX_QUOTA,
        cic.EX_NETWORK,
    }
    new = {cic.EX_MODEL_CAPACITY, cic.EX_CODEX_USAGE, cic.EX_CODEX_CRASH}
    assert len(new) == 3 and not new & old


# ── end to end through the fake codex ─────────────────────────────────────────────
def test_capacity_failure_envelope_names_cause_kind_and_run_log(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.scenarios(private="model_capacity", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_MODEL_CAPACITY
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["private"]  # no hop: not the seat's fault
    error = envelope["error"]
    assert error["kind"] == "model_capacity"
    assert error["cause"] == _CAPACITY
    assert _CAPACITY in error["message"] and "\n" not in error["message"]
    run_dir = Path(error["run_dir"])
    assert run_dir == Path(envelope["run_dir"]) == Path(envelope["attempts"][0]["run_dir"])
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["error_kind"] == "model_capacity"
    assert record["cause"] == _CAPACITY
    assert record["returncode"] == 1
    assert record["seat"] == "private" and record["model"] == _MODEL
    assert record["note"] == "plan r1" and record["purpose"] == "debate"
    assert record["attempt_id"] == envelope["attempts"][0]["attempt_id"]
    assert record["thread_id"] == "01a06cfb-0000-7000-8000-00000000cafe"
    assert record["last_event_type"] == "turn.failed"
    assert record["argv"][-1] == "-" and record["prompt_chars"] == len("reply OK")
    assert (run_dir / "prompt.txt").read_text(encoding="utf-8") == "reply OK"
    assert "turn.failed" in (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert (run_dir / "stderr.log").read_text(encoding="utf-8") == ""
    assert run_dir.name.split("-")[1:3] == ["debate", "plan"]
    assert oct(run_dir.stat().st_mode & 0o777) == "0o700"
    assert oct((run_dir / "run.json").stat().st_mode & 0o777) == "0o600"
    rows = codex_ledger.read_runs()
    assert rows[-1]["run_dir"] == str(run_dir)
    assert _CAPACITY in rows[-1]["error_message"]


def test_success_keeps_a_run_log_too(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.scenarios(private={"scenario": "ok", "reply": "the answer"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    run_dir = Path(envelope["run_dir"])
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "ok" and record["returncode"] == 0
    assert record["error_kind"] == "" and record["cause"] == ""
    assert record["tokens_in"] == 10
    assert (run_dir / "reply.txt").read_text(encoding="utf-8") == "the answer"


def test_each_hop_gets_its_own_run_log(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    dirs = [a["run_dir"] for a in envelope["attempts"]]
    assert len(set(dirs)) == 2
    first = json.loads((Path(dirs[0]) / "run.json").read_text(encoding="utf-8"))
    assert first["outcome"] == "refused:quota" and first["cause"]


def test_secrets_are_redacted_in_every_file(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "sk-proj-" + "Z" * 40
    three_seats.scenarios(
        private={
            "scenario": "scripted",
            "stdout": [json.dumps({"type": "turn.failed", "error": {"message": f"bad {token}"}})],
            "stderr": [f"key={token}"],
            "rc": 1,
        }
    )
    assert cic.cmd_run(_run_ns(three_seats, prompt=f"use {token}")) == cic.EX_CODEX_FAIL
    envelope = _envelope(capsys)
    assert token not in json.dumps(envelope["error"])
    run_dir = Path(envelope["run_dir"])
    for child in run_dir.iterdir():
        assert token not in child.read_text(encoding="utf-8"), child.name
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(events[0])["error"]["message"] == f"bad {codex_runlog.REDACTED}"


def test_text_mode_prints_cause_and_run_log_on_stderr(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    three_seats.scenarios(private="model_capacity")
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_MODEL_CAPACITY
    err = capsys.readouterr().err
    assert "ERROR: Codex exited 1 — the model is at capacity" in err
    assert _CAPACITY in err
    assert "run log: " in err


def test_runlog_can_be_switched_off(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(codex_runlog.RUNLOG_SWITCH_ENV, "0")
    three_seats.scenarios(private="model_capacity")
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_MODEL_CAPACITY
    envelope = _envelope(capsys)
    assert envelope["run_dir"] is None and envelope["error"]["cause"] == _CAPACITY
    assert not codex_runlog.runlog_root().exists()


# ── retention + the runlog command ────────────────────────────────────────────────
def _fake_run(root: Path, name: str, *, status: str = "ok", age_s: float = 0.0) -> Path:
    path = root / name
    path.mkdir(parents=True)
    record = {"attempt_id": name[-8:], "status": status, "purpose": "debate", "cause": "boom"}
    (path / "run.json").write_text(json.dumps(record), encoding="utf-8")
    if age_s:
        stamp = time.time() - age_s
        os.utime(path / "run.json", (stamp, stamp))
    return path


def test_prune_keeps_newest_n_drops_old_and_caps_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rl"
    for index in range(6):
        _fake_run(root, f"2026101{index}T000000-debate-s-aaaaaaa{index}")
    _fake_run(root, "2026100100T000000-debate-s-old00000", age_s=30 * 86400)
    monkeypatch.setenv(codex_runlog.KEEP_ENV, "4")
    removed = codex_runlog.prune(root)
    names = sorted(p.name for p in root.iterdir())
    assert len(names) == 4 and all("old" not in n for n in names)
    assert len(removed) == 3
    big = root / names[-1] / "events.jsonl"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setenv(codex_runlog.MAX_MB_ENV, "1")
    codex_runlog.prune(root)
    assert sorted(p.name for p in root.iterdir()) == []  # newest alone exceeds 1 MB


def test_runlog_lists_failures_and_shows_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "rl"
    monkeypatch.setenv(codex_runlog.RUNLOG_DIR_ENV, str(root))
    _fake_run(root, "20261012T150710-debate-plan-r1-86d11868-05a7cfc4", status="failed")
    _fake_run(root, "20261012T151140-debate-plan-r1-86d11868-d3007be6")
    (root / "20261012T150710-debate-plan-r1-86d11868-05a7cfc4" / "stderr.log").write_text(
        "the tail line\n", encoding="utf-8"
    )
    ns = argparse.Namespace(
        path=False, prune=False, show=None, limit=20, failed=True, json=True, tail=20
    )
    assert cic.cmd_runlog(ns) == cic.EX_OK
    rows = json.loads(capsys.readouterr().out)
    assert [r["attempt_id"] for r in rows] == ["05a7cfc4"]
    ns.failed, ns.json = False, False
    assert cic.cmd_runlog(ns) == cic.EX_OK
    out = capsys.readouterr().out
    assert out.index("d3007be6") < out.index("05a7cfc4")  # newest first
    assert "cause: boom" in out and "\x1b]8;;" in out  # OSC 8 links
    ns.show = "last-failed"
    assert cic.cmd_runlog(ns) == cic.EX_OK
    assert "the tail line" in capsys.readouterr().out
    ns.show = "05a7"  # attempt-id prefix
    assert cic.cmd_runlog(ns) == cic.EX_OK
    ns.show = "nope"
    assert cic.cmd_runlog(ns) == cic.EX_USAGE


def test_runlog_parser_has_unique_short_options() -> None:
    parser = cic.build_parser()
    args = parser.parse_args(["runlog", "-f", "-n", "5", "-s", "last", "-T", "3", "-j"])
    assert (args.failed, args.limit, args.show, args.tail, args.json) == (
        True,
        5,
        "last",
        3,
        True,
    )
    assert parser.parse_args(["runlog", "-p"]).path
    assert parser.parse_args(["runlog", "-P"]).prune


def test_find_rollout_by_thread_id(tmp_path: Path) -> None:
    now = time.time()
    day = time.localtime(now)
    folder = tmp_path / "sessions" / time.strftime("%Y/%m/%d", day)
    folder.mkdir(parents=True)
    hit = folder / "rollout-2026-10-12T15-07-10-01a0-thread.jsonl"
    hit.write_text("{}\n", encoding="utf-8")
    assert codex_runlog.find_rollout(tmp_path, "01a0-thread", now) == str(hit)
    assert codex_runlog.find_rollout(tmp_path, "other", now) == ""
    assert codex_runlog.find_rollout(tmp_path, "", now) == ""
