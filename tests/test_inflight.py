"""In-flight work signals — the on-disk evidence a session handoff drains on.

The fixtures under ``tests/fixtures/inflight/`` pin the shapes Claude Code writes
(recorded 2026-10-26, Claude Code 2.1.310):

* **Subagent running vs finished** — the parent transcript carries the ``Agent``
  ``tool_use`` (its ``id`` is the subagent meta's ``toolUseId``) and a launch record whose
  ``toolUseResult`` is ``{"status": "async_launched", "agentId": …}``; a finished agent is
  one whose ``<task-notification>`` naming that id has reached the parent transcript.
  ``<proj>/<sid>/subagents/agent-<id>.meta.json`` holds ``agentType`` / ``description`` /
  ``toolUseId``; ``agent-<id>.jsonl``'s FIRST record is the original prompt. The
  subagent's own last record says nothing reliable about "finished" (a tool call or a
  text reply both occur mid-run), so the parent's notification is the signal, and the
  store's SubagentStart/SubagentStop counter covers the window before the launch record
  is written.
* **Background shell** — a Bash-tool shell's argv sources
  ``shell-snapshots/snapshot-…`` and embeds the command as ``eval '<command>'``
  (single quotes escaped ``'\\''``), followed by ``< /dev/null && pwd -P >| …``. The
  keep-warm timer is the same shape with ``cc-keepwarm.sh`` in the command.
* **Capped seat** — ``limitswitch.observe`` returns a halt with ``usage_limit=True`` for a
  "hit your … limit" API error at the end of the main chain (a server-side overload has
  ``usage_limit=False``), and ``quota.provider_state`` reports ``BLOCKED`` once a block is
  recorded for the account.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from command_center import limitswitch, quota
from command_center.adapters import claude as claude_adapter
from command_center.adapters.claude import background_work_split, is_keepwarm_command

FIXTURES = Path(__file__).parent / "fixtures" / "inflight"
PARENT = FIXTURES / "proj" / "sess-0001.jsonl"
SUBAGENTS = FIXTURES / "proj" / "sess-0001" / "subagents"


def _ps_table() -> dict[int, list[tuple[int, str]]]:
    """The fixture ``ps -Ao pid=,ppid=,command=`` as ``_children_map`` builds it."""
    children: dict[int, list[tuple[int, str]]] = {}
    for line in (FIXTURES / "ps.txt").read_text(encoding="utf-8").splitlines():
        pid, ppid, command = line.split(None, 2)
        children.setdefault(int(ppid), []).append((int(pid), command))
    return children


# --------------------------------------------------------------------------- B1 spike


def test_parent_transcript_running_vs_finished_subagents() -> None:
    """a1 is launched and never reported back; a2 has its notification; k1 is a timer."""
    pending, timers = background_work_split(PARENT) or ([], [])
    assert pending == ["a1", "b1"]
    assert timers == ["k1"]


def test_subagent_meta_and_first_record_are_the_capsule_sources() -> None:
    """The meta names the launching tool_use; the first jsonl record is the prompt."""
    meta = json.loads((SUBAGENTS / "agent-a1.meta.json").read_text(encoding="utf-8"))
    assert meta["agentType"] == "opus-builder"
    assert meta["description"] == "tp#1 build part one"
    assert meta["toolUseId"] == "toolu_A1"
    first = json.loads((SUBAGENTS / "agent-a1.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["isSidechain"] is True
    assert first["message"]["content"] == "Build part one of the thing."


def test_background_shell_argv_carries_the_command_and_the_keepwarm_marker() -> None:
    """The Bash-tool shell signature and the keep-warm marker are visible in argv."""
    shells = [cmd for _pid, cmd in _ps_table()[4242] if "shell-snapshots/snapshot-" in cmd]
    assert len(shells) == 3
    assert [is_keepwarm_command(cmd) for cmd in shells] == [False, True, False]
    assert "eval 'uv run pytest -q tests/test_x.py' < /dev/null" in shells[0]


def test_capped_seat_signal_is_a_usage_limit_halt() -> None:
    """A usage-limit halt is ``usage_limit``; a server overload is a halt but not that."""
    adapter = claude_adapter.ClaudeAdapter()
    capped = limitswitch.observe(
        adapter, "sess-0001", "/repo", "", transcript_path=FIXTURES / "halt.jsonl"
    )
    assert capped is not None and capped.usage_limit
    overload = limitswitch.observe(
        adapter, "sess-0001", "/repo", "", transcript_path=FIXTURES / "overload.jsonl"
    )
    assert overload is not None and not overload.usage_limit
    assert limitswitch.observe(adapter, "sess-0001", "/repo", "", transcript_path=PARENT) is None


def test_capped_seat_signal_from_the_quota_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded block makes the account's quota state BLOCKED until it lifts."""
    monkeypatch.setattr(
        quota.config, "claude_config_dirs", lambda: {"private": Path("/x"), "work": Path("/y")}
    )
    now = int(time.time())
    before = quota.provider_state("claude:work", now=now)
    assert before is None or before.state != quota.BLOCKED
    quota.record_block("claude:work", blocked_until=now + 3600, reason="test", observed_at=now)
    state = quota.provider_state("claude:work", now=now)
    assert state is not None and state.state == quota.BLOCKED
