#!/usr/bin/env python3
"""In-flight work detector — what a session still has running, by evidence.

ONE implementation for both consumers: ``ccc switch-account`` refuses to relaunch a
session while :func:`background_work` returns reasons, and ``ccc continue-session``
(the handoff, tp#1300) drains on the same verdict and lists every item it found so the
successor can relaunch agents from their continuation capsules and restart shells.

Evidence (pinned by ``tests/fixtures/inflight/``, see ``tests/test_inflight.py``):

* the transcript's structured launch / completion records
  (``adapters.claude.background_work_split``) — a background agent or Bash task is in
  flight from its launch record until its ``<task-notification>`` (or a successful
  ``TaskStop``) reaches the parent transcript;
* the process tree under the live ``claude`` pid — a child ``claude`` program (a
  subagent process) and Bash-tool shells, whose argv embeds the command as
  ``eval '<command>'``;
* the store's SubagentStart/SubagentStop counter, which covers an in-process agent
  before its launch record is written.

Unknown is never clear: an unreadable transcript, an unmatched shell or a child
``claude`` all keep :attr:`Inventory.clear` false. Keep-warm timers are listed (to be
stopped) but never block. Stdlib-only at import time: ``cli`` imports this module on
every invocation, hot paths included.
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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter

KIND_SUBAGENT = "subagent"
KIND_SHELL = "shell"
KIND_KEEPWARM = "keepwarm"
KIND_UNKNOWN = "unknown"

# The Bash tool's shell embeds the command as ``eval '<command>' < /dev/null && pwd -P …``
# with every single quote inside written as ``'\''``.
_EVAL_RE = re.compile(r"\beval '((?:[^']|'\\'')*)' < /dev/null")

# Only the tail is scanned, as in ``adapters.claude.background_work_split``.
_TAIL_BYTES = 32 * 1024 * 1024


@dataclass
class Item:  # pylint: disable=too-many-instance-attributes
    """One piece of in-flight work and what is known about it."""

    kind: str
    id: str
    description: str = ""
    command: str = ""
    cwd: str = ""
    agent_type: str = ""
    transcript: str = ""
    pid: int = 0

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape ``ccc continue-session state -j`` prints."""
        return asdict(self)


@dataclass
class Inventory:
    """Every in-flight item plus the veto reasons; ``clear`` iff there are no reasons."""

    items: list[Item] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def clear(self) -> bool:
        """True when nothing blocks a relaunch or a handoff."""
        return not self.reasons

    def to_dict(self) -> dict[str, Any]:
        """JSON shape: ``{"clear", "reasons", "items"}``."""
        return {
            "clear": self.clear,
            "reasons": list(self.reasons),
            "items": [item.to_dict() for item in self.items],
        }


def background_work(
    adapter: ClaudeAdapter,
    live_pid: int,
    transcript: Any,
    ignore: frozenset[int] = frozenset(),
    *,
    active_subagents: int = 0,
    keepwarm: list[str] | None = None,
) -> list[str]:
    """Why the session must NOT be relaunched yet: background work still in flight, by evidence.

    Process tree (a child claude = subagent, a background Bash-tool shell) plus the
    transcript's structured launch/completion records
    (``adapters.claude.pending_background_work``) plus *active_subagents*, the store's
    SubagentStart/SubagentStop counter (``Store.active_subagents``) — an IN-PROCESS
    Agent-tool subagent runs inside the session's own process, so it shows up in neither
    of the first two until its launch record reaches the transcript. An unreadable
    transcript is a reason too — unknown is refused, never assumed clear. Empty ⇒ clear to
    switch. *ignore* is the caller's own process ancestry when it runs inside the session
    (the ``!`` prefix's Bash-tool shell must not veto the very command it is running).
    Keep-warm timers (``adapters.claude.is_keepwarm_command``) never count — they are
    disposable and the new process re-arms its own; their transcript ids are appended to
    *keepwarm* when the caller passes a list.
    """
    from .adapters.claude import background_work_split

    reasons: list[str] = []
    if active_subagents > 0:
        reasons.append(
            f"{active_subagents} in-process subagent(s) still running "
            "(SubagentStart/SubagentStop counter)"
        )
    if live_pid > 0:
        if adapter.has_subagent(live_pid, ignore):
            reasons.append("a child claude process (subagent) is running")
        if adapter.has_background_task(live_pid, ignore, skip_keepwarm=True):
            reasons.append("a background Bash task shell is running")
    if transcript is None:
        reasons.append("the session transcript could not be located — background state unknown")
    else:
        split = background_work_split(transcript)
        if split is None:
            reasons.append(
                "the session transcript is unreadable/malformed — background state unknown"
            )
        else:
            pending, timers = split
            if pending:
                reasons.append("background task(s) not yet reported back: " + ", ".join(pending))
            if keepwarm is not None:
                keepwarm.extend(timers)
    return reasons


def shell_command_from_argv(argv: str) -> str | None:
    """The command a Bash-tool shell runs, unquoted from its ``eval '…'``; else ``None``."""
    match = _EVAL_RE.search(argv)
    if match is None:
        return None
    return match.group(1).replace("'\\''", "'")


@dataclass
class _Launch:
    """One background launch read from the transcript."""

    task_id: str
    is_agent: bool
    tool_use_id: str = ""
    cwd: str = ""


def _transcript_records(transcript: Path) -> list[dict[str, Any]]:
    """The tail's parseable dict records (details only; the verdict is the adapter's)."""
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _launch_details(
    records: list[dict[str, Any]],
) -> tuple[dict[str, _Launch], dict[str, dict[str, Any]]]:
    """``(task id → launch, tool_use id → tool_use input)`` from *records*."""
    launches: dict[str, _Launch] = {}
    uses: dict[str, dict[str, Any]] = {}
    for record in records:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        for block in blocks:
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                params = block.get("input")
                uses[block["id"]] = params if isinstance(params, dict) else {}
        result = record.get("toolUseResult")
        if not isinstance(result, dict):
            continue
        task_id = result.get("backgroundTaskId")
        is_agent = False
        if not (isinstance(task_id, str) and task_id) and result.get("status") == "async_launched":
            task_id, is_agent = result.get("agentId"), True
        if not (isinstance(task_id, str) and task_id) or task_id in launches:
            continue
        use_id = next(
            (
                str(b.get("tool_use_id"))
                for b in blocks
                if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)
            ),
            "",
        )
        launches[task_id] = _Launch(task_id, is_agent, use_id, str(record.get("cwd") or ""))
    return launches, uses


def _subagent_meta(transcript: Path, agent_id: str) -> tuple[dict[str, Any], str]:
    """``(meta.json dict, subagent jsonl path)`` for *agent_id* of *transcript*'s session."""
    base = transcript.parent / transcript.stem / "subagents"
    jsonl = base / f"agent-{agent_id}.jsonl"
    try:
        meta = json.loads((base / f"agent-{agent_id}.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return (meta if isinstance(meta, dict) else {}), (str(jsonl) if jsonl.exists() else "")


def _transcript_items(transcript: Path) -> list[Item]:
    """In-flight items from the transcript (the adapter's verdict, enriched with details)."""
    from .adapters.claude import background_work_split

    split = background_work_split(transcript)
    if split is None:
        return []
    pending, timers = split
    launches, uses = _launch_details(_transcript_records(transcript))
    items: list[Item] = []
    for task_id in [*pending, *timers]:
        launch = launches.get(task_id, _Launch(task_id, False))
        params = uses.get(launch.tool_use_id, {})
        description = str(params.get("description") or "")
        if task_id in timers:
            items.append(
                Item(KIND_KEEPWARM, task_id, description, str(params.get("command") or ""))
            )
        elif launch.is_agent:
            meta, jsonl = _subagent_meta(transcript, task_id)
            items.append(
                Item(
                    KIND_SUBAGENT,
                    task_id,
                    str(meta.get("description") or description),
                    cwd=launch.cwd,
                    agent_type=str(meta.get("agentType") or params.get("subagent_type") or ""),
                    transcript=jsonl,
                )
            )
        else:
            items.append(
                Item(
                    KIND_SHELL,
                    task_id,
                    description,
                    str(params.get("command") or ""),
                    cwd=launch.cwd,
                )
            )
    return items


def _process_items(live_pid: int, ignore: frozenset[int], known: list[Item]) -> list[Item]:
    """Child processes of *live_pid*: match shells to *known* items, the rest are unknown."""
    # pylint: disable=protected-access  # the adapter's cached ``ps`` scan is the one source
    from .adapters import claude as claude_adapter

    if live_pid <= 0:
        return []
    children = claude_adapter._children_map()
    unmatched = [item for item in known if item.kind in (KIND_SHELL, KIND_KEEPWARM)]
    unknown: list[Item] = []
    seen: set[int] = set(ignore)
    stack = [live_pid]
    while stack:
        parent = stack.pop()
        for child_pid, command in sorted(children.get(parent, [])):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            if claude_adapter._is_claude_program(command):
                unknown.append(
                    Item(
                        KIND_UNKNOWN,
                        f"pid:{child_pid}",
                        "child claude process",
                        command,
                        pid=child_pid,
                    )
                )
                continue
            if claude_adapter._BG_SHELL_SIGNATURE in command:
                shell = shell_command_from_argv(command)
                match = next((i for i in unmatched if shell and i.command == shell), None)
                if match is not None:
                    match.pid = child_pid
                    unmatched.remove(match)
                else:
                    unknown.append(
                        Item(
                            KIND_UNKNOWN,
                            f"pid:{child_pid}",
                            "untracked shell",
                            shell or command,
                            pid=child_pid,
                        )
                    )
                continue
            stack.append(child_pid)
    return unknown


def inventory(
    adapter: ClaudeAdapter,
    live_pid: int,
    transcript: Path | None,
    ignore: frozenset[int] = frozenset(),
    *,
    active_subagents: int = 0,
) -> Inventory:
    """Classify every in-flight item of a session; reasons are :func:`background_work`'s."""
    items = _transcript_items(transcript) if transcript is not None else []
    items += _process_items(live_pid, ignore, items)
    agents = sum(1 for item in items if item.kind == KIND_SUBAGENT)
    if active_subagents > agents:
        items.append(
            Item(
                KIND_UNKNOWN,
                "subagents:counter",
                f"{active_subagents - agents} subagent(s) counted by the store "
                "with no launch record yet",
            )
        )
    reasons = background_work(
        adapter, live_pid, transcript, ignore, active_subagents=active_subagents
    )
    return Inventory(items=items, reasons=reasons)


def drain(  # pylint: disable=too-many-arguments
    adapter: ClaudeAdapter,
    live_pid: int,
    transcript: Path | None,
    *,
    timeout: float,
    poll: float = 3.0,
    ignore: frozenset[int] = frozenset(),
    active_subagents: Callable[[], int] | None = None,
) -> tuple[bool, Inventory]:
    """Poll :func:`inventory` until clear (``True``) or *timeout* seconds pass (``False``).

    *poll* below 3 s gains nothing: the adapter caches the process table for 3 s.
    *active_subagents* is re-read on every poll (the store counter moves).
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        count = active_subagents() if active_subagents is not None else 0
        inv = inventory(adapter, live_pid, transcript, ignore, active_subagents=count)
        if inv.clear:
            return True, inv
        if time.monotonic() >= deadline:
            return False, inv
        time.sleep(max(0.0, poll))
