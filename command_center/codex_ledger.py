#!/usr/bin/env python3
"""The Codex run ledger: one JSON line per PHYSICAL attempt the runner made.

Every Codex call in ccc goes through :func:`command_center.codex_in_claude.run_with_fallback`
(``run``, ``delegate``, :func:`command_center.llm.run_codex`), and until 2026-09-21 that
single entrance recorded nothing durable about what it launched: the seat-attempts file
keeps ONE timestamp per seat (the round-robin's input), and with ``codex_usage`` on the run
is ``--ephemeral``, so not even a rollout file survives. The question "did this Mac spend
the shared seat at 10:33?" was answerable only from file mtimes and a poller log.

This module is that record. ``<app_home>/codex-runs.jsonl`` gets one line per attempt that
physically reached ``codex`` — a skipped seat (``skipped:*``) is not spend and is not
written — carrying the seat, the oracle id, the caller's ``-p`` purpose, the outcome, the
wall time, the model/effort, the prompt size, the token usage codex reported and where the
call came from (cwd, the Claude session). Two consumers read it: ``ccc quota`` (each Codex
row's ``last_run`` and the table's ``last run … ago`` note) and ``ai logs`` (ai.py),
which merges these rows under the seat names it already prints (``codex-work``,
``codex-de``) so the ledger is the answer to "was it us" from now on.

Append-only, best-effort, never raises into the runner: a ledger that cannot be written
is a missing measurement, not a failed Codex call. Timestamps are local ISO seconds with
the UTC offset — the same spelling ai.py's own ``calls-*.jsonl`` uses, so the two sort
together as strings.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from .codex_in_claude import RunResult

LEDGER_NAME = "codex-runs.jsonl"

#: Attempts written per line at most: ``error_message`` is the runner's prose and is
#: capped so one stack trace cannot turn the ledger into a log file.
_ERROR_CHARS = 240


def ledger_path() -> Path:
    """``<app_home>/codex-runs.jsonl`` — published on ``ccc quota -j`` as ``codex_runs_log``."""
    return config.app_home() / LEDGER_NAME


def _iso(epoch: float) -> str:
    """Local ISO seconds with offset (``2026-09-21T10:33:39+02:00``)."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def attempt_records(
    result: RunResult,
    *,
    purpose: str,
    model: str,
    effort: str,
    prompt_chars: int,
    write: bool,
    workdir: str,
) -> list[dict[str, Any]]:
    """The ledger lines for *result*: one per PHYSICAL attempt, oldest first.

    ``ok`` is the attempt's own verdict (a hop's refused seat is ``False`` even when the
    call as a whole succeeded on the next seat). ``error`` is the outcome label of a
    non-ok attempt; the runner's prose (``error_message``) is attached to the LAST
    attempt only, which is the one it describes.
    """
    from .codex_in_claude import _seat_pid  # pylint: disable=import-outside-toplevel

    physical = [a for a in result.attempts if not a.outcome.startswith("skipped:")]
    rows: list[dict[str, Any]] = []
    for index, attempt in enumerate(physical):
        ok = attempt.outcome == "ok"
        row: dict[str, Any] = {
            "ts": _iso(attempt.ended_at),
            "seat": attempt.seat,
            "id": _seat_pid(attempt.seat) if attempt.seat else "",
            "purpose": purpose,
            "outcome": attempt.outcome,
            "ok": ok,
            "ms": int(attempt.elapsed_s * 1000),
            "model": model,
            "effort": effort,
            "prompt_chars": prompt_chars,
            "write": write,
            "cwd": workdir,
            "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        }
        if attempt.tokens_in or attempt.tokens_out:
            row["tokens_in"] = attempt.tokens_in
            row["tokens_out"] = attempt.tokens_out
        if not ok:
            row["error"] = attempt.outcome
            if index == len(physical) - 1 and result.error_message:
                row["error_message"] = " ".join(result.error_message.split())[:_ERROR_CHARS]
        rows.append(row)
    return rows


def record_result(result: RunResult, **context: Any) -> int:
    """Append *result*'s physical attempts to the ledger; the number of lines written.

    Best-effort: any failure (an unwritable home, a full disk) returns 0 and the Codex
    call it describes is unaffected. Serialized with the same advisory ``flock`` the
    usage caches use, so two runners finishing together cannot interleave a line.
    """
    try:
        rows = attempt_records(result, **context)
        if not rows:
            return 0
        from . import usage  # pylint: disable=import-outside-toplevel  # cycle-safe here

        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text)
        return len(rows)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 0


def read_runs(path: Path | None = None) -> list[dict[str, Any]]:
    """Every ledger line as a dict, oldest first; malformed lines are skipped."""
    path = ledger_path() if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("ts"):
            rows.append(row)
    return rows


def _epoch(stamp: str) -> int:
    """The epoch second of a ledger ``ts``, 0 when unparsable."""
    try:
        return int(datetime.fromisoformat(stamp).timestamp())
    except ValueError:
        return 0


def last_runs(rows: list[dict[str, Any]], now: int | None = None) -> dict[str, dict[str, Any]]:
    """Oracle id → the seat's newest attempt plus its 24 h count — the ``last_run`` field.

    ``{"ts", "age_s", "purpose", "outcome", "ok", "ms", "runs_24h"}``; a seat with no
    line is absent. ``runs_24h`` counts physical attempts, refusals included: a refusal
    is a round trip the seat was asked to bill even when it declined.
    """
    now = int(time.time()) if now is None else now
    recent = Counter(
        str(row.get("id") or "") for row in rows if now - _epoch(str(row["ts"])) <= 86_400
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:  # oldest first, so the last write per id wins
        pid = str(row.get("id") or "")
        if not pid:
            continue
        out[pid] = {
            "ts": row["ts"],
            "age_s": max(0, now - _epoch(str(row["ts"]))),
            "purpose": str(row.get("purpose") or ""),
            "outcome": str(row.get("outcome") or ""),
            "ok": bool(row.get("ok")),
            "ms": int(row.get("ms") or 0),
            "runs_24h": recent.get(pid, 0),
        }
    return out
