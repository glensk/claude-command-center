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

Since the same day the file is also the ledger of headless ``claude -p`` turns an EXTERNAL
caller wants next to its Codex ones — the sdsc-automations checker pair judges every
ticket command with Opus AND Codex, and ``ai logs`` showed only the Codex half. Such a
caller appends through ``ccc record-run`` (:func:`validate_row` + :func:`append_rows`),
one JSON object per physical attempt, with ``provider`` naming the family (``codex`` |
``claude``; a row without the key is a Codex row written before the field existed) and
``note`` carrying its context (``#255`` — the ticket). The FILE NAME stays
``codex-runs.jsonl`` on purpose: a long-lived older runner process may still append to
it, and a rename would split the history across two files.

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

#: The caller's context note (``#255``) is one short label, never a paragraph.
NOTE_CHARS = 120

#: Provider families a row may name; a row without ``provider`` is ``codex`` (written
#: before the field existed).
PROVIDERS = ("codex", "claude")

#: ``ccc record-run`` input contract: a row must carry these, with these types.
SCHEMA_VERSION = 1
_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "provider": str,
    "seat": str,
    "purpose": str,
    "outcome": str,
    "ok": bool,
    "ms": int,
}
_OPTIONAL: dict[str, type | tuple[type, ...]] = {
    "ts": str,
    "id": str,
    "note": str,
    "model": str,
    "requested_model": str,
    "effort": str,
    "prompt_chars": int,
    "llm_ms": int,
    "tokens_in": int,
    "tokens_out": int,
    "tokens_cache_read": int,
    "tokens_cache_create": int,
    "write": bool,
    "cwd": str,
    "session": str,
    "error": str,
    "error_message": str,
}


def sanitize_note(note: str | None) -> str:
    """*note* as the ledger stores it: whitespace and control characters collapsed to
    single spaces, capped at :data:`NOTE_CHARS`. ``""`` for nothing."""
    if not note:
        return ""
    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in str(note)).split())
    return cleaned[:NOTE_CHARS]


def provider_of(row: dict[str, Any]) -> str:
    """The row's provider family — ``codex`` when the key predates the field."""
    return str(row.get("provider") or "codex")


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
    note: str = "",
) -> list[dict[str, Any]]:
    """The ledger lines for *result*: one per PHYSICAL attempt, oldest first.

    ``ok`` is the attempt's own verdict (a hop's refused seat is ``False`` even when the
    call as a whole succeeded on the next seat). ``error`` is the outcome label of a
    non-ok attempt; the runner's prose (``error_message``) is attached to the LAST
    attempt only, which is the one it describes. *note* (the caller's ``-N``, sanitized)
    rides on EVERY attempt of the round — a hop does not change what it was for.
    """
    from .codex_in_claude import _seat_pid  # pylint: disable=import-outside-toplevel

    physical = [a for a in result.attempts if not a.outcome.startswith("skipped:")]
    clean_note = sanitize_note(note)
    rows: list[dict[str, Any]] = []
    for index, attempt in enumerate(physical):
        ok = attempt.outcome == "ok"
        row: dict[str, Any] = {
            "ts": _iso(attempt.ended_at),
            "provider": "codex",
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
        if clean_note:
            row["note"] = clean_note
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
        return _append_locked(rows)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 0


def _append_locked(rows: list[dict[str, Any]], path: Path | None = None) -> int:
    """Append *rows* under the ledger lock; the number written. Raises on I/O failure."""
    from . import usage  # pylint: disable=import-outside-toplevel  # cycle-safe here

    path = ledger_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
    return len(rows)


def validate_row(raw: Any, where: str = "row") -> dict[str, Any]:
    """*raw* (one ``ccc record-run`` object) as a ledger row, or :class:`ValueError`.

    Strict on purpose (the writer never lies about what it recorded): every
    :data:`_REQUIRED` key with its type, every known optional key with its type, an
    unknown key refused, ``provider`` one of :data:`PROVIDERS`, ``schema_version``
    :data:`SCHEMA_VERSION`. ``ts`` defaults to now, ``id`` to ``<provider>:<seat>``
    (the ccc oracle id — ``claude:work``), ``note`` is sanitized.
    """
    _check_keys(raw, where)
    provider = raw["provider"]
    if provider not in PROVIDERS:
        raise ValueError(f"{where}: provider {provider!r} not in {'/'.join(PROVIDERS)}")
    if not raw["seat"]:
        raise ValueError(f"{where}: empty seat")
    row: dict[str, Any] = {
        "ts": str(raw.get("ts") or _iso(time.time())),
        "provider": provider,
        "seat": raw["seat"],
        "id": str(raw.get("id") or f"{provider}:{raw['seat']}"),
        "purpose": raw["purpose"],
        "outcome": raw["outcome"],
        "ok": raw["ok"],
        "ms": raw["ms"],
    }
    if _epoch(row["ts"]) == 0:
        raise ValueError(f"{where}: ts {row['ts']!r} is not ISO-8601")
    for key in _OPTIONAL:
        if key in ("ts", "id"):
            continue
        value = raw.get(key)
        if value is None or value == "":
            continue
        row[key] = sanitize_note(value) if key == "note" else value
    if "error_message" in row:
        row["error_message"] = " ".join(str(row["error_message"]).split())[:_ERROR_CHARS]
    return row


def _check_keys(raw: Any, where: str) -> None:
    """The shape half of :func:`validate_row`: an object, a known version, no unknown
    key, every required key present, every present key of its declared type."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: not a JSON object")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"{where}: schema_version {version!r} (this ccc writes {SCHEMA_VERSION})")
    unknown = sorted(set(raw) - {"schema_version", *_REQUIRED, *_OPTIONAL})
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ValueError(f"{where}: missing {missing[0]!r}")
    for key, kind in {**_REQUIRED, **_OPTIONAL}.items():
        if key in raw and raw[key] is not None:
            _check_type(raw[key], kind, key, where)


def _check_type(value: Any, kind: type | tuple[type, ...], key: str, where: str) -> None:
    """``bool`` is an ``int`` in Python; the ledger keeps them apart."""
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        want = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        raise ValueError(f"{where}: {key!r} must be {want}, got {type(value).__name__}")


def append_rows(rows: list[dict[str, Any]], path: Path | None = None) -> int:
    """Append already-validated *rows*; the number written. Raises on I/O failure —
    this is the strict entrance behind ``ccc record-run``, whose exit code must say
    whether the record exists."""
    if not rows:
        return 0
    return _append_locked(rows, path)


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
    line is absent. Codex rows only: a Claude row (``provider: claude``) is the other
    family's spend and never stamps a Codex seat. ``runs_24h`` counts physical attempts,
    refusals included: a refusal is a round trip the seat was asked to bill even when it
    declined.
    """
    now = int(time.time()) if now is None else now
    rows = [row for row in rows if provider_of(row) == "codex"]  # Claude rows stamp nothing
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
