#!/usr/bin/env python3
"""The running background subagents of one session, read cold from its transcript (tp#1598).

``running_agents`` is the producer behind the optional per-session ``agents`` array of
``ccc sessions -j``. "Running" is exactly inflight.py's definition: the pending ids of
``adapters.claude.background_launch_scan`` (the one scan ``background_work_split`` also
wraps) whose launch record is an agent launch. Metadata only, never transcript text.
Stdlib-only at import time.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store import Store

MAX_AGENTS = 20  # decide keeps the first 20 entries
MAX_ROW_BYTES = 256 * 1024  # a larger final list is not persisted
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


def _meta_path(transcript: Path, agent_id: str) -> Path:
    """Where the agent's ``meta.json`` lives."""
    return transcript.parent / transcript.stem / "subagents" / f"agent-{agent_id}.meta.json"


def _read_meta(transcript: Path, agent_id: str) -> dict[str, Any]:
    """The agent's ``meta.json`` (``{}`` when absent or unreadable)."""
    try:
        meta = json.loads(_meta_path(transcript, agent_id).read_text(encoding="utf-8"))
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


def inventory(
    store: Store,
    targets: list[tuple[str, Path | None]],
    *,
    deadline_s: float = 0.15,
    now_ms: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Running agents per session, from the ``subagent_scan`` cache or a cold rescan.

    *targets* is ``(session_id, transcript path | None)``. Only the sessions whose inventory
    is known are returned; a missing sid means "omit the key" (no path, unreadable or
    malformed transcript, a cold rescan skipped by the deadline, or any error). Never raises.

    Hit: ``(path, size, mtime_ns)`` equal, so no transcript read; agents whose meta file was
    not seen yet are re-checked. Miss: a cold ``running_agents`` rescan, started only while
    *deadline_s* of wall time has not passed. Only changed rows are written, then pruned.
    """
    try:
        return _inventory(store, targets, deadline_s, now_ms)
    except Exception:  # pylint: disable=broad-exception-caught
        return {}


def _inventory(
    store: Store,
    targets: list[tuple[str, Path | None]],
    deadline_s: float,
    now_ms: int | None,
) -> dict[str, list[dict[str, Any]]]:
    started = time.monotonic()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    paths = {sid: Path(path) for sid, path in targets if path is not None}
    cached = store.get_subagent_scans(str(p) for p in paths.values())
    result: dict[str, list[dict[str, Any]]] = {}
    writes: list[tuple[str, str, int, int, str, str, int]] = []
    for sid, path in paths.items():
        try:
            stat = path.stat()
            row = cached.get(str(path))
            hit = row is not None and (row["size"], row["mtime_ns"]) == (
                stat.st_size,
                stat.st_mtime_ns,
            )
            if hit:
                entry = _hit(path, sid, row, now)
                if entry is not None:
                    result[sid] = entry[0]
                    if entry[1] is not None:
                        writes.append(entry[1])
                    continue
            if time.monotonic() - started >= deadline_s:
                continue
            cold = running_agents(path)
            if cold is None:
                continue
            result[sid] = cold
            seen = [a["id"] for a in cold if _meta_path(path, a["id"]).is_file()]
            row_out = _row(path, sid, stat, cold, seen=seen, now=now)
            if row_out is not None:
                writes.append(row_out)
        except Exception:  # pylint: disable=broad-exception-caught
            result.pop(sid, None)
    store.put_subagent_scans(writes)
    store.prune_subagent_scans({str(p) for p in paths.values()}, now)
    return result


def _hit(
    path: Path, sid: str, row: Any, now: int
) -> tuple[list[dict[str, Any]], tuple[str, str, int, int, str, str, int] | None] | None:
    """Agents of a cache hit, plus the rewritten row when a meta file newly appeared.

    ``None`` when the stored row cannot be decoded (treated as a miss).
    """
    try:
        agents = json.loads(row["agents"])
        seen = json.loads(row["meta_seen"])
    except ValueError:
        return None
    if not isinstance(agents, list) or not isinstance(seen, list):
        return None
    changed = False
    for agent in agents:
        agent_id = agent.get("id")
        if agent_id in seen or not _meta_path(path, agent_id).is_file():
            continue
        meta = _read_meta(path, agent_id)
        agent["description"] = _text(meta.get("description"), agent.get("description"))
        agent["agent_type"] = _text(meta.get("agentType"), agent.get("agent_type"))
        seen.append(agent_id)
        changed = True
    if not changed:
        return agents, None
    stat = path.stat()
    return agents, _row(path, sid, stat, agents, seen=seen, now=now, identity=row)


def _row(
    path: Path,
    sid: str,
    stat: os.stat_result,
    agents: list[dict[str, Any]],
    *,
    seen: list[str],
    now: int,
    identity: Any = None,
) -> tuple[str, str, int, int, str, str, int] | None:
    """A ``subagent_scan`` row, or ``None`` when the agents JSON is over 256 KiB."""
    blob = json.dumps(agents, separators=(",", ":"))
    if len(blob.encode("utf-8")) > MAX_ROW_BYTES:
        return None
    size = identity["size"] if identity is not None else stat.st_size
    mtime_ns = identity["mtime_ns"] if identity is not None else stat.st_mtime_ns
    return (str(path), sid, int(mtime_ns), int(size), blob, json.dumps(seen), now)
