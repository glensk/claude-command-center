"""The ``ccc await`` probes and the bounded runner under them.

``checks.run_structured`` is exercised against real (tiny, local) processes; the
probes against a replaying fake runner, so no test ever calls zoho-api.py or
slack_api.py.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import pytest

from command_center import await_probes as ap
from command_center.checks import StructuredResult, run_structured, sanitize_error

NOW = 1_800_000_000


class FakeRunner:
    """Replays one :class:`StructuredResult` and records the call."""

    def __init__(self, result: StructuredResult) -> None:
        self.result = result
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> StructuredResult:
        self.calls.append((argv, kwargs))
        return self.result


def _ok(payload: Any, exit_code: int = 0) -> StructuredResult:
    return StructuredResult(exit=exit_code, stdout=json.dumps(payload))


# --------------------------------------------------------------------------- runner
def test_run_structured_captures_exit_and_output() -> None:
    result = run_structured([sys.executable, "-c", "print('hi'); raise SystemExit(3)"])
    assert (result.exit, result.stdout, result.timed_out) == (3, "hi\n", False)


def test_run_structured_caps_output() -> None:
    result = run_structured([sys.executable, "-c", "print('x' * 100000)"], max_bytes=100)
    assert len(result.stdout) == 100
    assert result.truncated


def test_run_structured_kills_the_group_on_timeout() -> None:
    started = time.monotonic()
    result = run_structured("sleep 30 & sleep 30", shell=True, timeout=0.5)
    assert result.timed_out and result.exit is None
    assert time.monotonic() - started < 10


def test_run_structured_reports_spawn_errors() -> None:
    result = run_structured(["/nonexistent/probe-binary"])
    assert result.exit is None and "FileNotFoundError" in result.spawn_error


def test_run_structured_refuses_mismatched_shell_mode() -> None:
    assert run_structured("echo hi").spawn_error
    assert run_structured(["echo", "hi"], shell=True).spawn_error


def test_run_structured_sanitizes_stderr() -> None:
    code = "import sys; sys.stderr.write('bad xoxp-1-abc\\x1b[31m\\n' + 'A' * 40); sys.exit(1)"
    result = run_structured([sys.executable, "-c", code])
    assert "xoxp" not in result.stderr and "\x1b" not in result.stderr
    assert "[redacted]" in result.stderr


def test_sanitize_error_bounds_and_redacts() -> None:
    assert len(sanitize_error("x " * 1000)) <= 400
    assert "secret" not in sanitize_error("Authorization: Bearer secret")


# --------------------------------------------------------------------------- backoff
@pytest.mark.parametrize(
    ("fails", "expected"),
    [(0, 120), (1, 240), (2, 480), (5, 1800), (40, 1800)],
)
def test_backoff(fails: int, expected: int) -> None:
    assert ap.next_check_after(NOW, 120, fails) == NOW + expected


# --------------------------------------------------------------------------- zoho
ZSPEC = {"exe": "/bin/zoho-api.py", "ticket": "209"}
FIRED = {
    "schema_version": 1,
    "ticket": "209",
    "newest_inbound": {
        "id": "1003",
        "time": "2026-09-02T08:12:00.500Z",
        "from": "req@example.org",
        "summary": "all good",
    },
    "watermark": "1788336720500:1003",
    "fired": True,
}


def test_zoho_fired() -> None:
    runner = FakeRunner(_ok(FIRED))
    res = ap.probe_zoho(ZSPEC, "1:a", runner=runner)
    assert runner.calls[0][0] == ["/bin/zoho-api.py", "-i", "209", "1:a"]
    assert res.outcome == "fired" and res.watermark == "1788336720500:1003"
    assert res.event is not None
    assert res.event.event_id == "zoho:209:1003"
    assert res.event.sender == "req@example.org"
    assert res.event.remote_epoch == 1788336720


def test_zoho_not_fired_keeps_or_advances_watermark() -> None:
    res = ap.probe_zoho(ZSPEC, "1:a", runner=FakeRunner(_ok({**FIRED, "fired": False})))
    assert res.outcome == "not_fired" and res.watermark == "1788336720500:1003"


def test_zoho_baseline_argv_has_no_watermark() -> None:
    runner = FakeRunner(_ok({**FIRED, "fired": False}))
    ap.probe_zoho(ZSPEC, "", runner=runner)
    assert runner.calls[0][0] == ["/bin/zoho-api.py", "-i", "209"]


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (StructuredResult(exit=5), "transient"),
        (StructuredResult(exit=1), "transient"),
        (StructuredResult(exit=None, timed_out=True), "transient"),
        (StructuredResult(exit=2), "permanent"),
        (StructuredResult(exit=3), "permanent"),
        (StructuredResult(exit=4, stderr="auth"), "permanent"),
        (StructuredResult(exit=0, stdout="not json"), "permanent"),
        (StructuredResult(exit=0, stdout='{"schema_version": 2, "fired": false}'), "permanent"),
        (StructuredResult(exit=0, stdout='{"schema_version": 1, "fired": true}'), "permanent"),
        (StructuredResult(exit=None, spawn_error="ENOENT"), "permanent"),
    ],
)
def test_zoho_failure_classes(result: StructuredResult, outcome: str) -> None:
    res = ap.probe_zoho(ZSPEC, "1:a", runner=FakeRunner(result))
    assert res.outcome == outcome
    assert res.watermark == "1:a"  # a failure never moves the watermark


def test_zoho_bad_spec_is_permanent() -> None:
    assert ap.probe_zoho({}, "", runner=FakeRunner(_ok(FIRED))).outcome == "permanent"


# --------------------------------------------------------------------------- slack
SSPEC = {"exe": "/bin/slack_api.py", "user_id": "U123ABC", "channel": "D1"}


def _msgs(*rows: tuple[str, str | None, str]) -> dict[str, Any]:
    return {
        "channel": "D1",
        "messages": [
            {"ts": ts, "user": user, "text": text, "bot_id": None} for ts, user, text in rows
        ],
        "has_more": False,
    }


def test_slack_fired_on_their_message_only() -> None:
    runner = FakeRunner(
        _ok(
            _msgs(
                ("100.1", "UME", "mine"), ("101.5", "U123ABC", "hi"), ("102.0", "U123ABC", "there")
            )
        )
    )
    res = ap.probe_slack(SSPEC, "100.0", runner=runner)
    assert runner.calls[0][0] == [
        "/bin/slack_api.py",
        "--dm",
        "U123ABC",
        "--oldest",
        "100.0",
        "--json",
    ]
    assert res.outcome == "fired" and res.watermark == "102.0"
    assert res.event is not None
    assert res.event.snippet == "hi / there"
    assert res.event.event_id == "slack:D1:102.0"
    assert res.event.remote_epoch == 102


def test_slack_own_and_bot_messages_do_not_fire_but_advance() -> None:
    payload = _msgs(("100.5", "UME", "mine"))
    payload["messages"].append({"ts": "101.0", "user": "U123ABC", "bot_id": "B1", "text": "x"})
    res = ap.probe_slack(SSPEC, "100.0", runner=FakeRunner(_ok(payload)))
    assert res.outcome == "not_fired" and res.watermark == "101.0"


def test_slack_messages_at_or_before_the_watermark_never_fire() -> None:
    res = ap.probe_slack(SSPEC, "101.5", runner=FakeRunner(_ok(_msgs(("101.5", "U123ABC", "old")))))
    assert res.outcome == "not_fired" and res.watermark == "101.5"


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (StructuredResult(exit=1, stderr="ERROR: invalid_auth"), "permanent"),
        (StructuredResult(exit=1, stderr="ERROR: not_authed"), "permanent"),
        (StructuredResult(exit=1, stderr="ERROR: SLACK_USER_TOKEN not set"), "permanent"),
        (StructuredResult(exit=1, stderr="ERROR: ratelimited (429)"), "transient"),
        (StructuredResult(exit=1, stderr="ERROR: HTTP 503"), "transient"),
        (StructuredResult(exit=1, stderr="URLError: nodename nor servname"), "transient"),
        (StructuredResult(exit=None, timed_out=True), "transient"),
        (StructuredResult(exit=0, stdout="{]"), "permanent"),
        (StructuredResult(exit=0, stdout='{"channel": "D1"}'), "permanent"),
        (StructuredResult(exit=None, spawn_error="ENOENT"), "permanent"),
    ],
)
def test_slack_failure_classes(result: StructuredResult, outcome: str) -> None:
    res = ap.probe_slack(SSPEC, "100.0", runner=FakeRunner(result))
    assert res.outcome == outcome and res.watermark == "100.0"


def test_slack_needs_a_member_id() -> None:
    bad = {**SSPEC, "user_id": "someone@example.org"}
    assert ap.probe_slack(bad, "1", runner=FakeRunner(_ok(_msgs()))).outcome == "permanent"


def test_slack_baseline() -> None:
    assert ap.slack_baseline(json.dumps(_msgs(("5.0", "U1", "a"), ("7.25", "U2", "b")))) == (
        "D1",
        "7.25",
    )
    assert ap.slack_baseline(json.dumps(_msgs())) == ("D1", "0")
    assert ap.slack_baseline("nope") is None


# --------------------------------------------------------------------------- cmd
def test_cmd_fired_with_stdout_as_event() -> None:
    runner = FakeRunner(StructuredResult(exit=0, stdout="checks green\n"))
    res = ap.probe_cmd({"cmd": "gh pr checks 1", "cwd": "/repo"}, "", now=NOW, runner=runner)
    argv, kwargs = runner.calls[0]
    assert argv == "gh pr checks 1" and kwargs["shell"] is True and kwargs["cwd"] == "/repo"
    assert res.outcome == "fired" and res.event is not None
    assert res.event.snippet == "checks green" and res.event.remote_epoch == NOW


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (StructuredResult(exit=1), "not_fired"),
        (StructuredResult(exit=None, timed_out=True), "transient"),
        (StructuredResult(exit=None, spawn_error="ENOENT"), "permanent"),
    ],
)
def test_cmd_classes(result: StructuredResult, outcome: str) -> None:
    assert ap.probe_cmd({"cmd": "x"}, "", now=NOW, runner=FakeRunner(result)).outcome == outcome


def test_cmd_real_runner_end_to_end() -> None:
    res = ap.probe_cmd({"cmd": "printf done"}, "", now=NOW)
    assert res.outcome == "fired" and res.event is not None and res.event.snippet == "done"
    assert ap.probe_cmd({"cmd": "exit 1"}, "", now=NOW).outcome == "not_fired"


def test_unknown_kind_is_permanent() -> None:
    assert ap.probe("fax", {}, "", now=NOW).outcome == "permanent"
