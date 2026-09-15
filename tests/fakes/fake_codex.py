#!/usr/bin/env python3
"""A fake ``codex`` binary: scripted per CODEX_HOME, so seat fallback is testable.

The runner tests need a ``codex exec`` that refuses on one seat and succeeds on
another, deterministically and without a network. This script is that binary —
tests put it first on ``PATH`` (or monkeypatch ``codex_launch.resolve_codex``) and
drive it with two environment variables:

``$FAKE_CODEX_CONTROL``
    JSON ``{"<CODEX_HOME>": "<scenario>"}`` or ``{"<CODEX_HOME>": {"scenario":
    "...", "reply": "...", "resets_at": 123}}``. The key is the *resolved* home, so
    which seat the runner picked decides what happens. An unlisted home ⇒ ``ok``.

``$FAKE_CODEX_LOG``
    JSON-lines file; one ``{"home", "argv", "cwd", "pgid", "env"}`` per invocation.
    The prompt arrives on **stdin** (argv ends with ``-``) and is written verbatim to
    ``<log>.stdin.<n>``, so a test can assert it never reached argv.

Scenarios (see ``tests/test_codex_runner.py``):

``ok``                            success; writes the reply to the ``-o`` file
``ok_rollout``                    success AND a rollout with a windowed ``rate_limits``
                                  event at ``used_pct`` (both windows), so the seat is
                                  MEASURABLE afterwards — what a non-ephemeral run
                                  really leaves behind
``premium_only``                  success AND a rollout whose newest block is the
                                  WINDOWLESS ``premium`` shape a short exec logs: the
                                  run happened and measured nothing
``refuse_quota``                  the REAL 2026-09-04 refusal stream + rc 1, and a
                                  rollout file with an exhausted window so the
                                  cooldown deadline can be derived from it
``refuse_entitlement`` / ``refuse_auth``
                                  the synthetic structured refusals
``refuse_stderr_only``            no failure EVENT at all; 80 stderr lines whose last
                                  one carries the refusal (the fallback path)
``task_fail``                     a plain traceback; rc 1 and NOT the seat's fault
``midrun_write``                  writes a file, emits a file_change item, THEN refuses
``hang`` / ``hang_with_grandchild`` / ``stall_with_grandchild``
                                  never finish; the grandchild variants leave a
                                  ``sleep`` in the SAME process group, which the
                                  runner's group sweep must kill
``leader_exits_leaving_grandchild``
                                  rc 0 immediately, grandchild still running
``network_dead``                  the 2026-09-07 seven-hour hang: the two LOCAL start
                                  events, then "Reconnecting... waiting for network"
                                  error events on stdout plus ``ERROR
                                  codex_models_manager`` log lines on stderr, forever —
                                  loud output that is TROUBLE, never progress
``silent_after_start``            the two local start events, then total silence: codex
                                  never got a model response (the startup guard's case)
``trouble_then_ok``               three reconnect error events and THEN a real reply — a
                                  flap the progress watchdog must NOT kill
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "codex_json"


def _control() -> dict:
    """The scenario record for THIS invocation's ``$CODEX_HOME`` (default ``ok``)."""
    raw = os.environ.get("FAKE_CODEX_CONTROL", "")
    table: dict = {}
    if raw and Path(raw).exists():
        try:
            loaded = json.loads(Path(raw).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            table = loaded
    home = os.environ.get("CODEX_HOME", "")
    resolved = str(Path(home).resolve()) if home else ""
    entry = table.get(home) or table.get(resolved) or "ok"
    return {"scenario": entry} if isinstance(entry, str) else dict(entry)


def _log(argv: list[str], stdin_text: str) -> None:
    """Append this invocation to ``$FAKE_CODEX_LOG`` and dump its stdin beside it."""
    path = os.environ.get("FAKE_CODEX_LOG", "")
    if not path:
        return
    row = {
        "home": os.environ.get("CODEX_HOME", ""),
        "argv": argv,
        "cwd": os.getcwd(),
        "pgid": os.getpgid(0),
        "env": {
            key: os.environ.get(key, "")
            for key in ("CCC_NO_CODEX", "CCC_INTERNAL", "AI_NO_AUTOCOMMIT", "CODEX_HOME")
        },
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    index = len(Path(path).read_text(encoding="utf-8").splitlines())
    Path(f"{path}.stdin.{index}").write_text(stdin_text, encoding="utf-8")


def _workspace(argv: list[str]) -> Path:
    """The ``-C`` root codex was pointed at (the real binary chdirs there)."""
    if "-C" in argv:
        return Path(argv[argv.index("-C") + 1])
    return Path.cwd()


def _out_path(argv: list[str]) -> str:
    """The ``-o`` / ``--output-last-message`` target, or ``""``."""
    for flag in ("-o", "--output-last-message"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    return ""


def _replay(name: str) -> None:
    """Print one captured/synthetic event stream verbatim (comment lines included)."""
    sys.stdout.write((FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8"))
    sys.stdout.flush()


def _write_rollout(resets_at: int) -> None:
    """Leave an exhausted 5h window in this seat's rollouts (the refusal's deadline)."""
    home = Path(os.environ.get("CODEX_HOME", ""))
    if not home.name:
        return
    day = home / "sessions" / "2026" / "09" / "04"
    day.mkdir(parents=True, exist_ok=True)
    (day / "rollout-2026-09-04T10-00-00-fake.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 100.0,
                            "resets_at": resets_at,
                            "window_minutes": 300,
                        },
                        "secondary": None,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_windowed_rollout(used_pct: float, resets_at: int, week_resets_at: int) -> None:
    """Leave a normal, populated ``rate_limits`` event in this seat's rollouts."""
    home = Path(os.environ.get("CODEX_HOME", ""))
    if not home.name:
        return
    day = home / "sessions" / "2026" / "09" / "09"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-09T12-00-00-{int(time.time() * 1000)}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": int(time.time()),
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": used_pct,
                            "resets_at": resets_at,
                            "window_minutes": 300,
                        },
                        "secondary": {
                            "used_percent": used_pct,
                            "resets_at": week_resets_at,
                            "window_minutes": 10080,
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_premium_rollout() -> None:
    """Leave the WINDOWLESS ``premium`` block a short ``codex exec`` really writes.

    Both windows null: the run is recorded, but it measured nothing. This is why the
    fill policy needs a round-robin tiebreak — see tests/test_usage.py's
    ``_CODEX_PREMIUM_NULL`` for the shape captured in the wild.
    """
    home = Path(os.environ.get("CODEX_HOME", ""))
    if not home.name:
        return
    day = home / "sessions" / "2026" / "09" / "09"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-09T12-00-00-{int(time.time() * 1000)}-premium.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": int(time.time()),
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "premium",
                        "limit_name": None,
                        "primary": None,
                        "secondary": None,
                        "credits": {"has_credits": False, "unlimited": False, "balance": None},
                        "individual_limit": None,
                        "plan_type": None,
                        "rate_limit_reached_type": None,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _spawn_grandchild() -> None:
    """A descendant in the SAME process group, so only a group sweep reaches it."""
    sentinel = os.environ.get("FAKE_CODEX_SENTINEL", "")
    if not sentinel:
        return
    subprocess.Popen(  # noqa: S602  # pylint: disable=consider-using-with
        ["sh", "-c", f'sleep 8; touch "{sentinel}"'],
        start_new_session=False,
    )


def _ignore_first_sigterm() -> None:
    """Survive one SIGTERM for a second — proves the sweep escalates to SIGKILL."""

    def _handler(_signum: int, _frame: object) -> None:
        time.sleep(1)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    signal.signal(signal.SIGTERM, _handler)


def main() -> int:  # pylint: disable=too-many-branches,too-many-return-statements
    """Behave like ``codex exec`` for the configured scenario; return its exit code."""
    argv = sys.argv[1:]
    stdin_text = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    _log(argv, stdin_text)
    control = _control()
    scenario = str(control.get("scenario") or "ok")
    reply = str(control.get("reply") or f"OK {os.environ.get('CODEX_HOME', '')}")
    out_path = _out_path(argv)

    if scenario in ("ok", "ok_rollout", "premium_only"):
        if scenario == "ok_rollout":
            now = int(time.time())
            _write_windowed_rollout(
                float(control.get("used_pct") or 0.0),
                int(control.get("resets_at") or (now + 3600)),
                int(control.get("week_resets_at") or (now + 5 * 86400)),
            )
        elif scenario == "premium_only":
            _write_premium_rollout()
        print(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": control.get("thread_id") or "01a06cfb-5cd0-7cb0-836d-e053998a7c64",
                }
            )
        )
        print(json.dumps({"type": "turn.started"}))
        print(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": reply},
                }
            )
        )
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
        if out_path:
            Path(out_path).write_text(reply, encoding="utf-8")
        return 0

    if scenario == "refuse_quota":
        _write_rollout(int(control.get("resets_at") or (int(time.time()) + 7200)))
        _replay("quota_refusal")
        print("Reading additional input from stdin...", file=sys.stderr)
        return 1

    if scenario in ("refuse_entitlement", "refuse_auth"):
        _replay("entitlement" if scenario == "refuse_entitlement" else "auth")
        if scenario == "refuse_auth":
            print("codex: not logged in", file=sys.stderr)
        return 1

    if scenario == "server_overloaded":
        _replay("server_overloaded")
        return 1

    if scenario == "refuse_stderr_only":
        # No failure EVENT at all — only the stderr tail carries the refusal.
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000001"}
            )
        )
        for number in range(79):
            print(f"noise line {number}", file=sys.stderr)
        print("You've hit your usage limit.", file=sys.stderr)
        return 1

    if scenario == "task_fail":
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000002"}
            )
        )
        print(
            "Traceback (most recent call last):\n  ValueError: the task itself failed",
            file=sys.stderr,
        )
        return 1

    if scenario == "midrun_write":
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000003"}
            )
        )
        (_workspace(argv) / "touched.txt").write_text("codex was here\n", encoding="utf-8")
        print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "file_change"}}))
        message = "Your workspace is out of credits. Add credits to continue."
        print(json.dumps({"type": "turn.failed", "error": {"message": message}}))
        return 1

    if scenario == "leader_exits_leaving_grandchild":
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000004"}
            )
        )
        _spawn_grandchild()
        if out_path:
            Path(out_path).write_text(reply, encoding="utf-8")
        return 0

    if scenario == "network_dead":
        # Codex 0.152.1 on a dead connection: it announces the (locally emitted) thread
        # and turn, never gets a model response, and then reconnects FOREVER. Every line
        # below is trouble — an any-output watchdog stays quiet for hours on this stream.
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000005"}
            )
        )
        print(json.dumps({"type": "turn.started"}), flush=True)
        _spawn_grandchild()
        deadline = time.monotonic() + 40
        next_log = 0.0
        while time.monotonic() < deadline:
            print(
                json.dumps(
                    {
                        "type": "error",
                        "message": (
                            "Reconnecting... waiting for network "
                            "(Connection failed: error sending request)"
                        ),
                    }
                ),
                flush=True,
            )
            if time.monotonic() >= next_log:
                next_log = time.monotonic() + 0.5
                print(
                    "2026-09-08T06:50:08.463644Z ERROR codex_models_manager::manager: "
                    "failed to refresh available models: Connection failed: error sending request",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(0.2)
        return 0

    if scenario == "silent_after_start":
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000006"}
            )
        )
        print(json.dumps({"type": "turn.started"}), flush=True)
        time.sleep(40)
        return 0

    if scenario == "late_progress":
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000008"}
            )
        )
        print(json.dumps({"type": "turn.started"}), flush=True)
        time.sleep(6)
        # The whole answer lands in ONE burst and the process exits at once, so these
        # lines only reach the supervisor's buffers when its reader threads join --
        # after ``proc.wait`` returned, where no periodic tick can have seen them.
        print(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": reply},
                }
            )
        )
        print(json.dumps({"type": "turn.completed"}), flush=True)
        return 0

    if scenario == "trouble_then_ok":
        print(
            json.dumps(
                {"type": "thread.started", "thread_id": "01a06cfb-0000-7000-8000-000000000007"}
            )
        )
        print(json.dumps({"type": "turn.started"}), flush=True)
        for attempt in (2, 3, 4):
            print(
                json.dumps(
                    {
                        "type": "error",
                        "message": (
                            f"Reconnecting... {attempt}/5 (stream disconnected before "
                            "completion: Connection refused (os error 61))"
                        ),
                    }
                ),
                flush=True,
            )
            time.sleep(0.2)
        print(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": reply},
                }
            )
        )
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}), flush=True)
        if out_path:
            Path(out_path).write_text(reply, encoding="utf-8")
        return 0

    if scenario in ("hang", "hang_with_grandchild", "stall_with_grandchild"):
        if scenario != "hang":
            _spawn_grandchild()
        _ignore_first_sigterm()
        time.sleep(30)
        return 0

    print(f"fake codex: unknown scenario {scenario!r}", file=sys.stderr)
    return 99


if __name__ == "__main__":
    sys.exit(main())
