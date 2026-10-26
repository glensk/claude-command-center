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

from command_center import inflight, limitswitch, quota
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


# ------------------------------------------------------------------- B2 detector


@pytest.fixture(name="ps_fixture")
def _ps_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_adapter, "_children_map", _ps_table)


def test_shell_command_from_argv_unquotes_the_eval_payload() -> None:
    """The eval payload comes back exactly as typed, quotes included."""
    table = _ps_table()[4242]
    assert inflight.shell_command_from_argv(table[0][1]) == "uv run pytest -q tests/test_x.py"
    assert inflight.shell_command_from_argv(table[2][1]) == "echo 'it''s' && grep -n \"x\" y"
    assert inflight.shell_command_from_argv("claude -p x") is None
    assert inflight.shell_command_from_argv("/bin/zsh -c eval 'unterminated") is None


@pytest.mark.usefixtures("ps_fixture")
def test_inventory_classifies_every_item() -> None:
    """Subagent a1, restartable shell b1, keep-warm k1, unknown processes."""
    adapter = claude_adapter.ClaudeAdapter()
    inv = inflight.inventory(adapter, 4242, PARENT, active_subagents=1)
    by_id = {item.id: item for item in inv.items}
    assert by_id["a1"].kind == inflight.KIND_SUBAGENT
    assert by_id["a1"].agent_type == "opus-builder"
    assert by_id["a1"].description == "tp#1 build part one"
    assert by_id["a1"].transcript.endswith("agent-a1.jsonl")
    assert by_id["b1"].kind == inflight.KIND_SHELL
    assert by_id["b1"].command == "uv run pytest -q tests/test_x.py"
    assert by_id["b1"].cwd == "/repo"
    assert by_id["b1"].description == "tp#1 run the x tests"
    assert by_id["k1"].kind == inflight.KIND_KEEPWARM
    unknown = [item for item in inv.items if item.kind == inflight.KIND_UNKNOWN]
    # pid 5003 (an untracked shell) and pid 5004 (a child claude); 5001 is b1 and 5002 k1.
    assert sorted(item.id for item in unknown) == ["pid:5003", "pid:5004"]
    assert not inv.clear
    assert "a1" not in by_id["b1"].to_dict().values()


def test_inventory_clear_when_nothing_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A readable transcript, no children, counter zero: clear."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    inv = inflight.inventory(claude_adapter.ClaudeAdapter(), 4242, transcript)
    assert inv.clear and not inv.items and not inv.reasons


def test_inventory_keepwarm_only_is_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A keep-warm timer alone never blocks; it is still listed (to be stopped)."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    lines = PARENT.read_text(encoding="utf-8").splitlines()
    keep = [line for line in lines if "toolu_K1" in line]
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("\n".join(keep) + "\n", encoding="utf-8")
    inv = inflight.inventory(claude_adapter.ClaudeAdapter(), 0, transcript)
    assert inv.clear
    assert [(item.kind, item.id) for item in inv.items] == [(inflight.KIND_KEEPWARM, "k1")]


def test_inventory_pending_counter_without_a_launch_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store counter alone is not clear and shows as one unknown item."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    inv = inflight.inventory(claude_adapter.ClaudeAdapter(), 0, transcript, active_subagents=2)
    assert not inv.clear
    assert [item.kind for item in inv.items] == [inflight.KIND_UNKNOWN]


def test_inventory_torn_last_line_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written LAST line is the live writer, not corruption."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(PARENT.read_text(encoding="utf-8") + '{"type": "assi', "utf-8")
    inv = inflight.inventory(claude_adapter.ClaudeAdapter(), 0, transcript)
    assert {item.id for item in inv.items} == {"a1", "b1", "k1"}


def test_inventory_unreadable_transcript_is_not_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing, malformed mid-file, or no transcript at all: unknown, never clear."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    adapter = claude_adapter.ClaudeAdapter()
    assert not inflight.inventory(adapter, 0, tmp_path / "missing.jsonl").clear
    assert not inflight.inventory(adapter, 0, None).clear
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n{}\n", encoding="utf-8")
    inv = inflight.inventory(adapter, 0, bad)
    assert not inv.clear
    assert inv.reasons


def test_background_work_is_the_cli_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cli._background_work`` IS the neutral detector (one implementation)."""
    from command_center import cli

    monkeypatch.setattr(claude_adapter, "_children_map", _ps_table)
    adapter = claude_adapter.ClaudeAdapter()
    assert cli._background_work is inflight.background_work  # noqa: SLF001
    reasons = inflight.background_work(adapter, 4242, PARENT)
    assert reasons[0] == "a child claude process (subagent) is running"
    assert "a background Bash task shell is running" in reasons
    assert reasons[-1] == "background task(s) not yet reported back: a1, b1"


def test_drain_returns_when_clear_and_times_out_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drain polls the detector until clear, or gives up after the bound."""
    calls = {"n": 0}

    def fake(*_args: object, **_kwargs: object) -> inflight.Inventory:
        calls["n"] += 1
        reasons = [] if calls["n"] >= 3 else ["busy"]
        return inflight.Inventory(items=[], reasons=reasons)

    monkeypatch.setattr(inflight, "inventory", fake)
    monkeypatch.setattr(inflight.time, "sleep", lambda _s: None)
    adapter = claude_adapter.ClaudeAdapter()
    ok, inv = inflight.drain(adapter, 0, tmp_path / "t.jsonl", timeout=60, poll=0)
    assert ok and inv.clear and calls["n"] == 3

    monkeypatch.setattr(
        inflight, "inventory", lambda *_a, **_k: inflight.Inventory(items=[], reasons=["busy"])
    )
    ok, inv = inflight.drain(adapter, 0, tmp_path / "t.jsonl", timeout=0, poll=0)
    assert not ok and inv.reasons == ["busy"]
