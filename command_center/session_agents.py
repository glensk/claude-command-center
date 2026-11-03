"""The running background subagents of one session, read cold from its transcript (tp#1598).

``running_agents`` is the producer behind the optional per-session ``agents`` array of
``ccc sessions -j``. "Running" is exactly inflight.py's definition: the pending ids of
``adapters.claude.background_launch_scan`` (the one scan ``background_work_split`` also
wraps) whose launch record is an agent launch. Metadata only, never transcript text.
Stdlib-only at import time.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_AGENTS = 20  # decide keeps the first 20 entries
_TP_ITEM_RE = re.compile(r"(?m)^\[tp-item:\s*(\d+)\]")


def running_agents(transcript: Path) -> list[dict[str, Any]] | None:
    """Running background agents of *transcript*, in launch order, at most 20.

    ``None`` exactly when ``background_work_split`` is ``None`` (unknown inventory);
    ``[]`` when the scan ran and no agent runs. Entry keys, in order: ``id``,
    ``description``, ``agent_type``, ``state`` (always ``"running"``), ``started_at``
    (UTC ``YYYY-MM-DDTHH:MM:SSZ`` or ``None``), ``tp_id`` (int or ``None``).
    """
    from .adapters.claude import background_launch_scan

    scan = background_launch_scan(transcript)
    if scan is None:
        return None
    pending = set(scan.pending)
    agents: list[dict[str, Any]] = []
    for agent_id, launch in scan.launches.items():  # insertion order = launch order
        if agent_id not in pending or not _is_agent_launch(launch.record):
            continue
        meta = _read_meta(transcript, agent_id)
        params = launch.tool_input
        agents.append(
            {
                "id": agent_id,
                "description": _text(meta.get("description"), params.get("description")),
                "agent_type": _text(meta.get("agentType"), params.get("subagent_type")),
                "state": "running",
                "started_at": _started_at(launch.record.get("timestamp")),
                "tp_id": _tp_id(params.get("prompt")),
            }
        )
        if len(agents) >= MAX_AGENTS:
            break
    return agents


def _is_agent_launch(record: dict[str, object]) -> bool:
    """True for an ``async_launched`` agent record (not a ``backgroundTaskId`` Bash task)."""
    result = record.get("toolUseResult")
    return (
        isinstance(result, dict)
        and result.get("status") == "async_launched"
        and not result.get("backgroundTaskId")
        and bool(result.get("agentId"))
    )


def _read_meta(transcript: Path, agent_id: str) -> dict[str, Any]:
    """The agent's ``meta.json`` (``{}`` when absent or unreadable)."""
    path = transcript.parent / transcript.stem / "subagents" / f"agent-{agent_id}.meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _text(*candidates: object) -> str:
    """The first non-empty string among *candidates*, else ``""``."""
    for value in candidates:
        if isinstance(value, str) and value:
            return value
    return ""


def _started_at(stamp: object) -> str | None:
    """A transcript ISO timestamp as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); ``None`` if unparseable."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tp_id(prompt: object) -> int | None:
    """The int of the first line-start ``[tp-item: N]`` marker in *prompt*, else ``None``."""
    if not isinstance(prompt, str):
        return None
    match = _TP_ITEM_RE.search(prompt)
    return int(match.group(1)) if match else None
