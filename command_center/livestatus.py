#!/usr/bin/env python3
"""A session's live status from Claude Code's on-disk state — stdlib only (tp#1806 B2).

``ccc statusline`` asks "what is this session doing right now?" on every render. The rules
it needs — the ``sessions/<pid>.json`` registry parse (sdk filter, pid probe), the
collapse of several entries for one id, the transcript search, the rate-limit halt check
on the transcript tail and the status precedence ladder — live here, importable without
:mod:`command_center.adapters.claude` or :mod:`command_center.models` (dataclasses and the
rest of the package). Both delegate to this module, so every caller runs the same code.

Values are plain: a registry entry is a ``dict`` of :class:`models.LiveSession` field
names, a status is the :class:`models.Status` value string. The adapter keeps its own
per-instance transcript memo around :func:`find_transcript`.

Imports: the standard library only.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=duplicate-code  # the adapter's other transcript scans share these idioms
import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

#: Registry ``status`` values that mean mid-turn. Claude Code 2.1.295 writes ``busy`` |
#: ``shell`` | ``idle`` | ``waiting``; ``shell`` = a shell tool is running (the CLI's own
#: ``claude agents`` view folds it into ``busy``).
BUSY_RAW_STATUSES = frozenset({"busy", "shell"})

# Claude Code's rate-limit halt text, e.g. "You've hit your weekly limit · resets …"
# or "You've hit your session limit · resets …" (5-hour window). Matched only inside
# an isApiErrorMessage assistant record (see halt_record) so user prompts that merely
# quote the phrase never trigger it.
RATE_LIMIT_RE = re.compile(r"hit your \w[\w-]* limit", re.IGNORECASE)
# Tail size scanned for the halt: the error is always the final transcript line, so a
# small read suffices regardless of how large the full transcript is.
HALT_TAIL_BYTES = 65_536


def effective_status(raw: str | None, turn_active: bool | None) -> str:
    """The registry *raw* status with a turn-less ``shell`` read as ``idle`` (tp#1662).

    Claude Code 2.1.317 leaves the registry at ``shell`` after a turn ENDS while a
    ``run_in_background`` Bash (e.g. the keep-warm timer) still lives, so ``shell`` alone
    cannot tell a running shell tool from an idle session holding a background shell.
    The transcript can: ``turn_active is False`` (no open turn) makes it ``idle``. A
    ``True`` or unknown (``None``) *turn_active* keeps ``shell`` busy — fail closed.
    Every other status is returned unchanged.
    """
    text = raw or ""
    if text.strip().lower() == "shell" and turn_active is False:
        return "idle"
    return text


def pid_alive(pid: int) -> bool:
    """True if a process with *pid* exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def parse_registry_entry(
    path: Path, home_str: str, alive: Callable[[int], bool] = pid_alive
) -> dict[str, Any] | None:
    """Parse one ``sessions/<pid>.json`` into the fields of a stamped ``LiveSession``.

    ``None`` for an unreadable file, an entry without a session id, or a headless/SDK
    session. *alive* probes the pid (the adapter passes its own, patchable, probe). A
    non-numeric ``pid`` raises ``ValueError`` like it always did.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session_id = data.get("sessionId")
    if not session_id:
        return None
    # Skip headless / SDK sessions: every `claude -p` (the daemon's own
    # summary & auto-progress calls, plus any other tool's headless runs)
    # registers here with entrypoint "sdk-cli", reusing kind "interactive".
    # They are not user sessions — left unfiltered they flood the store with
    # short-lived "parked" rows (typically at cwd "/", the launchd cwd) that
    # reconcile() then persists forever. entrypoint is the robust signal
    # (cwd-independent); kind is not (Claude reuses "interactive").
    entrypoint = str(data.get("entrypoint", "cli"))
    if entrypoint.startswith("sdk"):
        return None
    pid = int(data.get("pid", 0) or 0)
    return {
        "pid": pid,
        "session_id": session_id,
        "cwd": data.get("cwd", ""),
        "kind": data.get("kind", "interactive"),
        "entrypoint": entrypoint,
        "raw_status": data.get("status", "idle"),
        "name": data.get("name"),
        "agent": data.get("agent", "claude"),
        "started_at": int(data.get("startedAt", 0) or 0),
        "updated_at": int(data.get("updatedAt", 0) or 0),
        "status_updated_at": int(data.get("statusUpdatedAt", 0) or 0),
        "alive": alive(pid),
        "config_dir": home_str,
    }


def resolve_registry_group(
    entries: Sequence[tuple[bool, int, str]],
) -> tuple[int, tuple[str, str] | None]:
    """Pick the truthful entry among ``(alive, updated_at, config_dir)`` of ONE session id.

    Returns ``(index of the chosen entry, conflict)``; *conflict* is the first two
    account dirs (sorted) when the id is LIVE under more than one account, else ``None``.

    A crashed session leaves its ``sessions/<pid>.json`` behind, so the same id can sit
    in several registries (or twice in one) with only ONE running process. Two rules:

    * **A live entry always wins.** A stale dead entry must never shadow the running
      process. Ties (and all-dead groups) break on ``updated_at`` (first one wins).
    * **A D9 conflict needs two LIVE entries in different accounts.** A dead entry never
      counts: the stale file never "exits", so flagging it would refuse resume forever.
    """
    alive = [i for i, entry in enumerate(entries) if entry[0]]
    chosen = max(alive or range(len(entries)), key=lambda i: entries[i][1])
    homes = {entries[i][2] for i in alive}
    if len(homes) > 1:
        first, second = sorted(homes)[:2]
        return chosen, (first, second)
    return chosen, None


def find_transcript(
    projects_dirs: Iterable[Path], cwd: str, session_id: str, *, glob: bool = True
) -> Path | None:
    """The JSONL transcript of *session_id*: first hit over *projects_dirs*, in order.

    Per dir: the exact munged path (``<cwd with / as ->/<id>.jsonl``), then — when *glob*
    — any ``*/<id>.jsonl``. No memo: the adapter wraps its own around this.
    """
    encoded = cwd.replace("/", "-")
    for projects_dir in projects_dirs:
        candidate = projects_dir / encoded / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
        if glob:
            hits = list(projects_dir.glob(f"*/{session_id}.jsonl"))
            if hits:
                return hits[0]
    return None


def tail_records(path: Path, max_bytes: int = HALT_TAIL_BYTES) -> list[dict]:
    """Parse the JSONL records in the last *max_bytes* of *path* (most recent last).

    Reads only the tail so a multi-MB transcript stays cheap to poll each refresh.
    When the file exceeds *max_bytes* the first (possibly mid-record) line is dropped.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            truncated = size > max_bytes
            if truncated:
                handle.seek(-max_bytes, os.SEEK_END)
            chunk = handle.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        lines = lines[1:]
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def assistant_text(record: dict) -> str:
    """Concatenate the text blocks of an assistant transcript record (else "")."""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return " ".join(parts)


def halt_record(records: Iterable[dict]) -> dict | None:
    """The last main-chain assistant record of *records* when it is a rate-limit halt.

    Only the last **assistant** record counts (a trailing user record after the error —
    a queued "continue", a task notification, the auto-retry — does not lift the halt);
    sub-agent (``isSidechain``) records are ignored; ``isApiErrorMessage`` gates the
    text match so prompts that merely quote the phrase never trigger it.
    """
    last_assistant: dict | None = None
    for record in records:
        if record.get("isSidechain"):
            continue
        if record.get("type") == "assistant":
            last_assistant = record
    if last_assistant is None or not last_assistant.get("isApiErrorMessage"):
        return None
    if RATE_LIMIT_RE.search(assistant_text(last_assistant)) is None:
        return None
    return last_assistant


def derive_status_value(  # pylint: disable=too-many-return-statements,too-many-arguments  # ordered precedence ladder, one return per rule
    live: tuple[str, bool] | None,
    stored: tuple[bool, str] | None,
    halted: bool = False,
    background: bool = False,
    codex_waiting: bool = False,
    *,
    turn_active: bool | None = None,
) -> str:
    """The ``models.Status`` value for a session — the ladder behind ``derive_status``.

    *live* is ``(raw_status, alive)`` of the registry entry (``None`` = not registered),
    *stored* is ``(done, handed_off_to)`` of the stored row (``None`` = no row). See
    :func:`command_center.models.derive_status` for what each signal means.
    """
    raw = effective_status(live[0], turn_active).lower() if live is not None else ""
    is_alive = live is not None and live[1]
    busy = is_alive and raw in BUSY_RAW_STATUSES
    if stored is not None and stored[1]:
        # Terminal like done (tp#1300): the work moved to the successor. Only a turn that is
        # literally still running (the finalize message being written) reads WORKING. Once
        # the reconcile pass closed it as done (handoff done), done wins like any done row.
        done_or_handed = "done" if stored[0] else "handed_off"
        return "working" if busy else done_or_handed
    if stored is not None and stored[0]:
        if busy:
            return "working"  # done, but still mid-turn — ✓ waits for the turn to end
        return "done"
    if not is_alive:
        return "parked"
    if halted:
        return "halted"
    if raw in BUSY_RAW_STATUSES:
        return "working"
    if raw.startswith("wait"):
        return "waiting_input"
    if background:
        return "snoozed"  # idle, but a background task is still running
    if raw == "idle" and codex_waiting:
        return "waiting_codex"
    return "idle"
