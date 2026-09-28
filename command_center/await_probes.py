#!/usr/bin/env python3
"""The ``ccc await`` probes: one cheap, token-free check per source kind.

Every probe runs through :func:`command_center.checks.run_structured` (injectable as
*runner* so tests replay canned outputs) and maps what it saw onto ONE of four
outcomes — the classification table of the plan:

* **zoho-reply** — fired: exit 0 + ``fired: true``; not fired: exit 0 + ``fired: false``;
  transient: exit 5, a timeout, any other exit; permanent: exit 2/3/4, malformed JSON,
  a spawn error.
* **slack-dm** — fired: a message with ``user == U`` and ``ts > W``; not fired: no such
  message; transient: a timeout, rate-limit / 5xx / network text, any other exit;
  permanent: auth-error text, malformed JSON, a spawn error.
* **cmd** — fired: exit 0 (stdout = the event); not fired: a non-zero exit; transient:
  a timeout; permanent: a spawn error.

A transient failure backs the SOURCE off (:func:`next_check_after`); a permanent one
blocks only that source. Nothing here touches the store.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import dataclasses
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from .checks import StructuredResult, run_structured, sanitize_error

Outcome = Literal["fired", "not_fired", "transient", "permanent"]

PROBE_TIMEOUT_SEC = 30.0
PROBE_MAX_BYTES = 512 * 1024
MAX_BACKOFF_SEC = 30 * 60

#: ``zoho-api.py -i`` exit codes (its ``ProbeExit`` contract).
ZOHO_PERMANENT_EXITS = frozenset({2, 3, 4})
ZOHO_TRANSIENT_EXIT = 5

_SLACK_AUTH = re.compile(
    r"not_authed|invalid_auth|token_revoked|token_expired|account_inactive|"
    r"missing_scope|no (?:slack )?(?:user )?token|SLACK_[A-Z_]*TOKEN",
    re.IGNORECASE,
)
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")

Runner = Callable[..., StructuredResult]


@dataclasses.dataclass(frozen=True)
class Event:
    """What a fired source saw. Raw (unsanitized) — :mod:`await_prompt` bounds it."""

    source: str
    sender: str
    time: str
    snippet: str
    event_id: str
    remote_epoch: int


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """One probe's classified outcome. *watermark* is the source's next watermark."""

    outcome: Outcome
    watermark: str = ""
    event: Event | None = None
    error: str = ""


def next_check_after(now: int, interval: int, fail_count: int) -> int:
    """When to probe again: *interval* after a clean probe, doubled per transient failure."""
    if fail_count <= 0:
        return now + interval
    return now + min(interval * (2 ** min(fail_count, 16)), MAX_BACKOFF_SEC)


def _iso_epoch(value: str) -> int:
    """A Zoho ISO time (``2026-09-02T08:12:00.500Z``) to epoch seconds (0 if unparseable)."""
    try:
        parsed = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return 0
    return int(parsed.timestamp())


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _failure(result: StructuredResult) -> ProbeResult | None:
    """The outcome every kind shares: spawn error → permanent, timeout → transient."""
    if result.spawn_error:
        return ProbeResult("permanent", error=f"spawn: {result.spawn_error}")
    if result.timed_out:
        return ProbeResult("transient", error="timeout")
    return None


# --------------------------------------------------------------------------- zoho-reply
def zoho_argv(exe: str, ticket: str, watermark: str) -> list[str]:
    """``zoho-api.py -i TICKET [WATERMARK]`` (no watermark = the arm-time baseline)."""
    return [exe, "-i", ticket, *([watermark] if watermark else [])]


def probe_zoho(  # pylint: disable=too-many-return-statements  # one per table row
    spec: dict[str, Any], watermark: str, *, runner: Runner = run_structured
) -> ProbeResult:
    """Did the ticket's requester send a new inbound mail since *watermark*?"""
    exe, ticket = str(spec.get("exe") or ""), str(spec.get("ticket") or "")
    if not exe or not ticket:
        return ProbeResult("permanent", watermark=watermark, error="spec: exe/ticket missing")
    result = runner(
        zoho_argv(exe, ticket, watermark), timeout=PROBE_TIMEOUT_SEC, max_bytes=PROBE_MAX_BYTES
    )
    failed = _failure(result)
    if failed:
        return dataclasses.replace(failed, watermark=watermark)
    if result.exit in ZOHO_PERMANENT_EXITS:
        return ProbeResult(
            "permanent", watermark=watermark, error=f"exit {result.exit}: {result.stderr}"
        )
    if result.exit != 0:
        return ProbeResult(
            "transient", watermark=watermark, error=f"exit {result.exit}: {result.stderr}"
        )
    data = _json_object(result.stdout)
    if data is None or data.get("schema_version") != 1 or not isinstance(data.get("fired"), bool):
        return ProbeResult("permanent", watermark=watermark, error="malformed zoho-api JSON")
    new_mark = str(data.get("watermark") or watermark)
    newest = data.get("newest_inbound")
    if not data["fired"]:
        return ProbeResult("not_fired", watermark=new_mark)
    if not isinstance(newest, dict):
        return ProbeResult("permanent", watermark=watermark, error="fired without newest_inbound")
    when = str(newest.get("time") or "")
    return ProbeResult(
        "fired",
        watermark=new_mark,
        event=Event(
            source="zoho-reply",
            sender=str(newest.get("from") or ""),
            time=when,
            snippet=str(newest.get("summary") or ""),
            event_id=f"zoho:{ticket}:{newest.get('id') or ''}",
            remote_epoch=_iso_epoch(when),
        ),
    )


# --------------------------------------------------------------------------- slack-dm
def slack_argv(exe: str, user_id: str, watermark: str) -> list[str]:
    """``slack_api.py --dm U --oldest W --json`` (no ``--oldest`` for the baseline)."""
    return [exe, "--dm", user_id, *(["--oldest", watermark] if watermark else []), "--json"]


def is_slack_user_id(value: str) -> bool:
    """True for a Slack member id (``U…``/``W…``)."""
    return bool(_SLACK_USER_ID.match(value or ""))


def _slack_error(result: StructuredResult, watermark: str) -> ProbeResult:
    text = result.stderr or result.stdout[:400]
    kind: Outcome = "permanent" if _SLACK_AUTH.search(text) else "transient"
    return ProbeResult(
        kind, watermark=watermark, error=f"exit {result.exit}: {sanitize_error(text)}"
    )


def _ts(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def probe_slack(
    spec: dict[str, Any], watermark: str, *, runner: Runner = run_structured
) -> ProbeResult:
    """Did Slack user ``spec.user_id`` write in our DM since *watermark* (a Slack ts)?

    Only that user's own messages count (not ours, not a bot's). The watermark
    advances to the newest ts seen either way, so our own replies are consumed too.
    """
    exe, user_id = str(spec.get("exe") or ""), str(spec.get("user_id") or "")
    if not exe or not is_slack_user_id(user_id):
        return ProbeResult("permanent", watermark=watermark, error="spec: exe/user_id missing")
    result = runner(
        slack_argv(exe, user_id, watermark or "0"),
        timeout=PROBE_TIMEOUT_SEC,
        max_bytes=PROBE_MAX_BYTES,
    )
    failed = _failure(result)
    if failed:
        return dataclasses.replace(failed, watermark=watermark)
    if result.exit != 0:
        return _slack_error(result, watermark)
    data = _json_object(result.stdout)
    messages = data.get("messages") if data else None
    if not isinstance(messages, list):
        return ProbeResult("permanent", watermark=watermark, error="malformed slack_api JSON")
    floor = _ts(watermark or "0")
    rows = [m for m in messages if isinstance(m, dict) and _ts(m.get("ts")) > floor]
    newest_ts = max((str(m.get("ts")) for m in rows), key=_ts, default=watermark)
    theirs = sorted(
        (m for m in rows if m.get("user") == user_id and not m.get("bot_id")),
        key=lambda m: _ts(m.get("ts")),
    )
    if not theirs:
        return ProbeResult("not_fired", watermark=newest_ts or watermark)
    last = theirs[-1]
    snippet = " / ".join(str(m.get("text") or "") for m in theirs)
    ts = str(last.get("ts"))
    return ProbeResult(
        "fired",
        watermark=newest_ts,
        event=Event(
            source="slack-dm",
            sender=user_id,
            time=datetime.fromtimestamp(_ts(ts), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            snippet=snippet,
            event_id=f"slack:{spec.get('channel') or user_id}:{ts}",
            remote_epoch=int(_ts(ts)),
        ),
    )


def slack_baseline(stdout: str) -> tuple[str, str] | None:
    """``(channel, newest ts)`` from a baseline ``--dm U --json`` answer (``"0"`` if empty)."""
    data = _json_object(stdout)
    if not data or not isinstance(data.get("messages"), list):
        return None
    newest = max(
        (str(m.get("ts")) for m in data["messages"] if isinstance(m, dict) and m.get("ts")),
        key=_ts,
        default="0",
    )
    return str(data.get("channel") or ""), newest


# --------------------------------------------------------------------------- cmd
def probe_cmd(
    spec: dict[str, Any], watermark: str, *, now: int, runner: Runner = run_structured
) -> ProbeResult:
    """A user-authored shell predicate: exit 0 fires, its stdout is the event."""
    command, cwd = str(spec.get("cmd") or ""), str(spec.get("cwd") or "") or None
    if not command:
        return ProbeResult("permanent", watermark=watermark, error="spec: cmd missing")
    result = runner(
        command, cwd=cwd, shell=True, timeout=PROBE_TIMEOUT_SEC, max_bytes=PROBE_MAX_BYTES
    )
    failed = _failure(result)
    if failed:
        return dataclasses.replace(failed, watermark=watermark)
    if result.exit != 0:
        return ProbeResult("not_fired", watermark=watermark)
    digest = hashlib.sha256(result.stdout.encode("utf-8", errors="replace")).hexdigest()[:12]
    return ProbeResult(
        "fired",
        watermark=watermark,
        event=Event(
            source="cmd",
            sender="",
            time=datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            snippet=result.stdout.strip(),
            event_id=f"cmd:{now}:{digest}",
            remote_epoch=now,
        ),
    )


def probe(
    kind: str, spec: dict[str, Any], watermark: str, *, now: int, runner: Runner = run_structured
) -> ProbeResult:
    """Dispatch to the kind's probe; an unknown kind is permanent."""
    if kind == "zoho-reply":
        return probe_zoho(spec, watermark, runner=runner)
    if kind == "slack-dm":
        return probe_slack(spec, watermark, runner=runner)
    if kind == "cmd":
        return probe_cmd(spec, watermark, now=now, runner=runner)
    return ProbeResult("permanent", watermark=watermark, error=f"unknown source kind {kind!r}")
