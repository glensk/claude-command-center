#!/usr/bin/env python3
"""The context-relay hooks' state files that ``continue-session`` reads and writes (tp#1536).

ccc never imports the relay hooks; it reads their FILE format only, so a missing, stale
or corrupt file always falls back to the behaviour without it:

* **Relaunch record** (written by the relay hooks when a subagent relayed with a valid
  capsule): ``<state dir>/relaunch/<safe session>/<safe agent>.json`` with ``version`` 1,
  ``capsule_path`` and ``call`` (the exact Agent tool input that relaunches the agent,
  rendered once by the hooks — ccc carries it verbatim, never re-renders it) and
  ``consumed`` (null until the overseer relaunched or rejected it). :func:`relaunch_for`
  returns the unconsumed record's call plus the capsule text.
* **Retired marker** (written HERE, at the ``accepted`` event of a handoff and again,
  idempotently, by ``finalize``): ``<state dir>/retired/<safe session>.json`` = ``{transfer,
  successor, at}`` (file 0600, dir 0700, temp + rename). The hooks treat a session with a
  marker as retired (no Agent launches, no keep-warm demand on Stop).

``<state dir>`` = ``CC_RELAY_STATE_DIR`` or ``~/.local/state/cc-relay``. The marker is
written only when that dir already exists (the relay hooks are installed), so a ccc install
without them writes nothing there (inert). Ids are sanitized like the hooks do
(``[^A-Za-z0-9._-]`` -> ``_``, empty -> ``x``).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_ENV = "CC_RELAY_STATE_DIR"
DEFAULT_STATE_DIR = "~/.local/state/cc-relay"
RECORD_VERSION = 1
CAPSULE_READ_CAP = 64 * 1024  # a capsule is ≤ 8 KB; never slurp an arbitrary file
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe(value: object) -> str:
    """*value* as the hooks put it in a file name."""
    return _SAFE_RE.sub("_", str(value or "x")) or "x"


def state_dir() -> Path:
    """``CC_RELAY_STATE_DIR`` or ``~/.local/state/cc-relay`` (not created)."""
    return Path(os.environ.get(STATE_ENV) or os.path.expanduser(DEFAULT_STATE_DIR))


# ------------------------------------------------------------------ relaunch record


@dataclass(frozen=True)
class Relaunch:
    """An unconsumed relaunch record of one agent: its call and capsule."""

    call: dict[str, Any]
    capsule_path: str
    capsule: str  # "" when the capsule file is missing or unreadable


def record_path(session: str, agent_id: str) -> Path:
    """Where the hooks keep the relaunch record of *agent_id* launched by *session*."""
    return state_dir() / "relaunch" / safe(session) / f"{safe(agent_id)}.json"


def _read_capsule(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(CAPSULE_READ_CAP)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def relaunch_for(session: str, agent_id: str) -> Relaunch | None:
    """The unconsumed relaunch record of (*session*, *agent_id*), or ``None`` (absent,
    unreadable, another version, consumed, or without a ``call`` object)."""
    try:
        body = json.loads(record_path(session, agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("version") != RECORD_VERSION:
        return None
    call = body.get("call")
    if body.get("consumed") or not isinstance(call, dict):
        return None
    capsule_path = body.get("capsule_path")
    capsule_path = capsule_path if isinstance(capsule_path, str) else ""
    return Relaunch(call=call, capsule_path=capsule_path, capsule=_read_capsule(capsule_path))


# ------------------------------------------------------------------ retired marker


def retired_path(session: str) -> Path:
    """The retired marker of *session*."""
    return state_dir() / "retired" / f"{safe(session)}.json"


def read_retired(session: str) -> dict[str, Any] | None:
    """The retired marker of *session*, or ``None`` (absent / unreadable)."""
    try:
        body = json.loads(retired_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def write_retired(
    session: str, transfer: str, successor: str, now: float | None = None
) -> Path | None:
    """Mark *session* retired in favour of *successor* (transfer *transfer*).

    Idempotent: an existing marker for the same transfer and successor is kept (its
    ``at`` stays the accept time). ``None`` when the relay state dir does not exist
    (nothing written); ``OSError`` propagates to the caller."""
    base = state_dir()
    if not base.is_dir():
        return None
    current = read_retired(session)
    path = retired_path(session)
    if current and current.get("transfer") == transfer and current.get("successor") == successor:
        return path
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    body = {"transfer": transfer, "successor": successor, "at": time.time() if now is None else now}
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(body) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path
