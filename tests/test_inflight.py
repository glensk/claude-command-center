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


# ------------------------------------------------ stale subagent counter (tp#1491)

NOW_MS = 1_800_000_000_000


def _empty_transcript(tmp_path: Path) -> Path:
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    return transcript


def test_counter_vetoes_only_while_the_latest_start_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter 1, no transcript agent: veto at 5 s, clear and stale at 300 s."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    adapter = claude_adapter.ClaudeAdapter()
    transcript = _empty_transcript(tmp_path)

    fresh = inflight.inventory(
        adapter,
        0,
        transcript,
        active_subagents=1,
        subagent_started_at=NOW_MS - 5_000,
        now_ms=NOW_MS,
    )
    assert not fresh.clear and fresh.stale_counter == 0
    assert [item.id for item in fresh.items] == ["subagents:counter"]
    assert "1 in-process subagent(s) still running" in fresh.reasons[0]

    old = inflight.inventory(
        adapter,
        0,
        transcript,
        active_subagents=1,
        subagent_started_at=NOW_MS - 300_000,
        now_ms=NOW_MS,
    )
    assert old.clear and not old.items and old.stale_counter == 1


def test_counter_without_a_timestamp_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing start time is treated as fresh: today's behaviour."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    inv = inflight.inventory(
        claude_adapter.ClaudeAdapter(), 0, _empty_transcript(tmp_path), active_subagents=1
    )
    assert not inv.clear and inv.stale_counter == 0


def test_stale_excess_does_not_hide_a_running_transcript_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter 2, one agent running in the transcript, old start: only that agent vetoes."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    inv = inflight.inventory(
        claude_adapter.ClaudeAdapter(),
        0,
        PARENT,
        active_subagents=2,
        subagent_started_at=NOW_MS - 300_000,
        now_ms=NOW_MS,
    )
    assert not inv.clear
    assert inv.stale_counter == 1
    assert "subagents:counter" not in {item.id for item in inv.items}
    assert not any("in-process subagent" in reason for reason in inv.reasons)
    assert any("a1" in reason for reason in inv.reasons)


def test_unknown_transcript_is_still_refused_with_a_stale_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale counter or not, an unlocatable transcript means unknown, hence refuse."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    inv = inflight.inventory(
        claude_adapter.ClaudeAdapter(),
        0,
        None,
        active_subagents=1,
        subagent_started_at=NOW_MS - 300_000,
        now_ms=NOW_MS,
    )
    assert not inv.clear and inv.stale_counter == 1


def test_drain_reports_a_stale_counter_once_and_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drain ignores the stale excess and hands it to the reset callback."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    monkeypatch.setattr(inflight.time, "time", lambda: NOW_MS / 1000)
    seen: list[int] = []
    ok, inv = inflight.drain(
        claude_adapter.ClaudeAdapter(),
        0,
        _empty_transcript(tmp_path),
        timeout=0,
        poll=0,
        active_subagents=lambda: 1,
        subagent_started_at=lambda: NOW_MS - 300_000,
        on_stale_counter=seen.append,
    )
    assert ok and inv.clear and seen == [1]


@pytest.mark.parametrize(
    ("command", "agent"),
    [
        ("staff wait -a dev-1", "dev-1"),
        ("staff watch -adev-1", "dev-1"),
        ("STAFF_HOME=/s /x/staff.py wait --agent=dev-1 2>&1 | tail -5", "dev-1"),
        ("cd /x && staff wait -a dev-2 -a dev-1", "dev-1"),  # the last one wins
        ("staff wait", None),  # $STAFF_AGENT only: no agent named
        ("staff wait -a dev-1 && make", None),
        ("echo staff wait -a dev-1", None),
        ("staff wait -a `x`", None),
        ("staff wait -a 'dev-1", None),  # unparsable
        ("staff status -a dev-1", None),
    ],
)
def test_doorbell_agent_is_anchored_and_explicit(command: str, agent: str | None) -> None:
    """tp#1790: the doorbell parser behind ``ccc close -A``'s disposable shell."""
    assert inflight.doorbell_agent(command) == agent
    assert inflight.staff_doorbell("dev-1")(command) is (agent == "dev-1")
    assert inflight.staff_doorbell("")(command) is False


def test_doorbell_counts_without_the_close_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No shared default changes: only an explicit *disposable_shell* exempts a doorbell."""
    command = "staff wait -a dev-1"
    shell = (
        f"/bin/zsh -c source /x/shell-snapshots/snapshot-zsh-1.sh && eval '{command}' < /dev/null"
    )
    monkeypatch.setattr(claude_adapter, "_children_map", lambda: {4242: [(5000, shell)]})
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    adapter = claude_adapter.ClaudeAdapter()
    assert not inflight.inventory(adapter, 4242, transcript).clear
    assert inflight.background_work(adapter, 4242, transcript)
    exempt = inflight.staff_doorbell("dev-1")
    assert inflight.inventory(adapter, 4242, transcript, disposable_shell=exempt).clear
    other = inflight.staff_doorbell("dev-2")
    assert not inflight.inventory(adapter, 4242, transcript, disposable_shell=other).clear


# ---------------------------------------- the 4 drain aborts of tp#2172 M7 (C6)
#
# Reproduced from the transcripts (2026-11-07 … 11-10). In none of them did the keep-warm
# timer veto: its shell carries ``cc-keepwarm.sh`` in argv and is skipped, its only child
# is the exec'd ``keep-warm-guard.py``. The vetoing shell was work outside the main
# transcript (root cause 1) or the session's own timed-out foreground Bash (root cause 2).
# Argv shape: ``ps`` of the 5ad15521 abort (Claude Code, 2026-11-10); paths anonymised.

_C6_SID = "5ad15521-12e9-43d5-9d8d-06bee3a83c47"
_C6_PREFIX = (
    "/bin/zsh -c source /home/user/.claude/shell-snapshots/snapshot-zsh-1794324079121-dw3w3m.sh"
    f" 2>/dev/null || true && export CODEX_COMPANION_SESSION_ID='{_C6_SID}'\\012"
    "export CLAUDE_PLUGIN_DATA='/home/user/.claude/plugins/data/codex-openai-codex'\\012"
    ": && { shopt -u extglob || setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL; } >/dev/null 2>&1"
    " || true && "
)
_C6_KEEPWARM = f"~/.claude/hooks/cc-keepwarm.sh 3000 /home/user/.claude/projects/p/{_C6_SID}.jsonl"
_C6_GUARD = (
    "/opt/homebrew/Frameworks/Python.framework/Versions/3.15/Resources/Python.app/Contents"
    f"/MacOS/Python /home/user/.claude/hooks/keep-warm-guard.py -T 3000 /p/{_C6_SID}.jsonl"
)

_C6_FOLLOWER = (
    'tail -f /private/tmp/claude-501/p/s/tasks/b7ukcu3cm.output | grep -m1 -A40 "=== pytest"'
)


def _c6_shell(command: str) -> str:
    """A Bash-tool shell's argv as ``ps -Ao command=`` printed it in the aborts."""
    return f"{_C6_PREFIX}eval '{command}' < /dev/null && pwd -P >| /tmp/claude-4f1a-cwd"


def _c6_bash(use_id: str, command: str, description: str, *, background: bool) -> list[str]:
    """The Bash ``tool_use`` record and its launch result (``backgroundTaskId`` = *use_id*)."""
    params: dict[str, object] = {"command": command, "description": description}
    if background:
        params["run_in_background"] = True
    use = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"u-{use_id}", "name": "Bash", "input": params}],
        },
    }
    launched: dict[str, object] = {"stdout": "", "stderr": "", "backgroundTaskId": use_id}
    if not background:
        launched["timedOutAfterMs"] = 120000  # moved to the background by the tool timeout
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"u-{use_id}", "content": "…"}],
        },
        "toolUseResult": launched,
    }
    return [json.dumps(use), json.dumps(result)]


def _c6_transcript(tmp_path: Path, *records: list[str]) -> Path:
    transcript = tmp_path / f"{_C6_SID}.jsonl"
    lines = [line for pair in records for line in pair]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


@pytest.mark.parametrize(
    "orphans",
    [
        # 2200569a 11-09: a finished builder's `tail -f` follower of its pytest output,
        # moved to the background at 180 s; it timed out at 01:50:14, the retry passed.
        ["timeout 560 tail -f /private/tmp/claude-501/p/s/tasks/brqq0nsgb.output"],
        # 5ad15521 11-10: a finished subagent's Touch ID helper rebuild (real work: the
        # successor reran it after the source TaskStopped it).
        ["/home/user/dotfiles/tools/vault-touchid/build.sh"],
        # cacad9b3 11-10: a finished reviewer's two followers, which never exit on their own.
        [
            _C6_FOLLOWER,
            _C6_FOLLOWER,
        ],
    ],
    ids=["2200569a-tail-timeout", "5ad15521-build", "cacad9b3-tail-grep"],
)
def test_c6_drain_abort_vetoer_is_a_subagent_shell_never_the_keepwarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphans: list[str]
) -> None:
    """Root cause 1: a subagent's foreground Bash, auto-backgrounded, outlives the subagent.

    Its shell is a child of the session's claude process but has no launch record in the
    MAIN transcript, so it shows as ``untracked shell`` and vetoes (unknown is refused).
    The keep-warm timer beside it is matched to its own shell and never vetoes: with the
    orphans gone the same tree is clear, which is why each retry passed.
    """
    transcript = _c6_transcript(
        tmp_path, _c6_bash("bkdcstgmr", _C6_KEEPWARM, "Arm keep-warm timer", background=True)
    )
    timer_tree: dict[int, list[tuple[int, str]]] = {
        4242: [(98000, _c6_shell(_C6_KEEPWARM))],
        98000: [(98001, _C6_GUARD)],
    }
    orphan_pids = [98628 + index for index in range(len(orphans))]
    full_tree = {
        **timer_tree,
        4242: timer_tree[4242]
        + [(pid, _c6_shell(cmd)) for pid, cmd in zip(orphan_pids, orphans, strict=True)],
    }
    adapter = claude_adapter.ClaudeAdapter()

    monkeypatch.setattr(claude_adapter, "_children_map", lambda: full_tree)
    inv = inflight.inventory(adapter, 4242, transcript)
    assert inv.reasons == ["a background Bash task shell is running"]
    by_id = {item.id: item for item in inv.items}
    assert by_id["bkdcstgmr"].kind == inflight.KIND_KEEPWARM
    assert by_id["bkdcstgmr"].pid == 98000
    untracked = [item for item in inv.items if item.kind == inflight.KIND_UNKNOWN]
    assert [(item.id, item.description, item.command) for item in untracked] == [
        (f"pid:{pid}", "untracked shell", cmd)
        for pid, cmd in zip(orphan_pids, orphans, strict=True)
    ]

    monkeypatch.setattr(claude_adapter, "_children_map", lambda: timer_tree)
    clear = inflight.inventory(adapter, 4242, transcript)
    assert clear.clear and not clear.reasons
    assert [item.kind for item in clear.items] == [inflight.KIND_KEEPWARM]


def test_c6_drain_first_vetoer_is_the_sessions_own_timed_out_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root cause 2 (61d91d4c 11-07): the session's own foreground Bash hit its 120 s timeout.

    The tool moved it to the background (``backgroundTaskId`` + ``timedOutAfterMs``, no
    ``run_in_background``): a runaway recursive glob. Both signals name it — the process
    probe and the transcript (pending ``br5llz9dy``); the keep-warm timer beside it does not.
    A successful ``TaskStop`` plus the shell's exit clears the session.
    """
    glob_cmd = "python3 /tmp/find-auto-arm.py"
    launches = [
        _c6_bash("bj62aasel", _C6_KEEPWARM, "Arm keep-warm timer", background=True),
        _c6_bash("br5llz9dy", glob_cmd, "Locate the auto-arm memo", background=False),
    ]
    transcript = _c6_transcript(tmp_path, *launches)
    tree = {
        4242: [(5100, _c6_shell(_C6_KEEPWARM)), (5200, _c6_shell(glob_cmd))],
        5100: [(5101, _C6_GUARD)],
    }
    monkeypatch.setattr(claude_adapter, "_children_map", lambda: tree)
    adapter = claude_adapter.ClaudeAdapter()
    inv = inflight.inventory(adapter, 4242, transcript)
    assert inv.reasons == [
        "a background Bash task shell is running",
        "background task(s) not yet reported back: br5llz9dy",
    ]
    kinds = {item.id: (item.kind, item.pid) for item in inv.items}
    assert kinds == {
        "br5llz9dy": (inflight.KIND_SHELL, 5200),
        "bj62aasel": (inflight.KIND_KEEPWARM, 5100),
    }

    stop = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "u-stop",
                            "name": "TaskStop",
                            "input": {"task_id": "br5llz9dy"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "u-stop",
                            "content": "Successfully stopped task: br5llz9dy",
                        }
                    ]
                },
            }
        ),
    ]
    stopped = _c6_transcript(tmp_path, *launches, stop)
    monkeypatch.setattr(claude_adapter, "_children_map", lambda: {**tree, 4242: tree[4242][:1]})
    assert inflight.inventory(adapter, 4242, stopped).clear


# ---------------------------------------- tp#2187: the drain names the blocking shell


def test_drain_and_start_refusal_name_an_untracked_shells_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing and the ``start`` refusal show an untracked shell's command.

    Not just ``untracked shell``: one line, cut to :data:`inflight.BRIEF_WIDTH`, every
    assignment value masked (no env values on screen).
    """
    from command_center import continue_session

    orphan = "SECRET_TOKEN=s3cr3t timeout 560 tail -f /private/tmp/claude-501/p/s/tasks/" + (
        "x" * 120
    )
    transcript = _c6_transcript(
        tmp_path,
        _c6_bash("bkdcstgmr", _C6_KEEPWARM, "Arm keep-warm timer", background=True),
    )
    tree = {4242: [(98000, _c6_shell(_C6_KEEPWARM)), (98628, _c6_shell(orphan))]}
    monkeypatch.setattr(claude_adapter, "_children_map", lambda: tree)
    inv = inflight.inventory(claude_adapter.ClaudeAdapter(), 4242, transcript)

    listing = continue_session._render_inflight(inv.to_dict())  # pylint: disable=protected-access
    line = next(row for row in listing.splitlines() if "pid:98628" in row)
    assert "untracked shell: SECRET_TOKEN=… timeout 560 tail -f /private/tmp/claude-501/" in line
    assert "s3cr3t" not in listing
    brief = line.split("untracked shell: ", 1)[1]
    assert len(brief) == inflight.BRIEF_WIDTH and brief.endswith("…")
    assert inv.unknown_processes() == [f"pid:98628 untracked shell: {brief}"]


def test_current_quote_style_matches_its_transcript_item_and_the_doorbell_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current Claude Code writes a quote inside ``eval '…'`` as ``'"'"'`` (live ``ps``).

    tp#2187: the matcher reads it like the older ``'\\''`` style, so the shell matches its
    transcript item and the close-only doorbell exemption applies to it.
    """
    grep_cmd = "grep -n 'drain' /home/user/notes.md"
    doorbell = "staff wait -a 'dev-1'"
    argv_grep = _c6_shell("grep -n '\"'\"'drain'\"'\"' /home/user/notes.md")
    argv_bell = _c6_shell("staff wait -a '\"'\"'dev-1'\"'\"'")
    assert inflight.shell_command_from_argv(argv_grep) == grep_cmd
    assert inflight.shell_command_from_argv(argv_bell) == doorbell

    transcript = _c6_transcript(
        tmp_path,
        _c6_bash("bgrep0001", grep_cmd, "Find drain notes", background=True),
        _c6_bash("bbell0001", doorbell, "Doorbell", background=True),
    )
    tree = {4242: [(5100, argv_grep), (5200, argv_bell)]}
    monkeypatch.setattr(claude_adapter, "_children_map", lambda: tree)
    adapter = claude_adapter.ClaudeAdapter()
    exempt = inflight.staff_doorbell("dev-1")
    inv = inflight.inventory(adapter, 4242, transcript, disposable_shell=exempt)
    kinds = {item.id: (item.kind, item.pid) for item in inv.items}
    assert kinds == {
        "bgrep0001": (inflight.KIND_SHELL, 5100),
        "bbell0001": (inflight.KIND_KEEPWARM, 5200),
    }

    monkeypatch.setattr(claude_adapter, "_children_map", lambda: {4242: tree[4242][1:]})
    bell_only = _c6_transcript(
        tmp_path, _c6_bash("bbell0001", doorbell, "Doorbell", background=True)
    )
    assert inflight.inventory(adapter, 4242, bell_only, disposable_shell=exempt).clear
    assert not inflight.inventory(adapter, 4242, bell_only).clear


# --------------------------------------- tp#2172 C8: ps failure, keep-alive notifications


@pytest.mark.parametrize("failure", ["timeout", "oserror", "nonzero"])
def test_c8_a_failed_ps_is_unknown_never_idle(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-18 23:21Z: ``ps`` failed under load, the walk saw no children and the drain
    passed with the session's own pylint shell alive. A failed read now vetoes."""
    import subprocess  # pylint: disable=import-outside-toplevel

    def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, float(kw["timeout"]))  # type: ignore[arg-type]
        if failure == "oserror":
            raise OSError("no ps")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(claude_adapter, "_PROC_CACHE", {"ts": 0.0, "map": {}, "failed_at": None})
    monkeypatch.setattr(claude_adapter.subprocess, "run", _run)
    adapter = claude_adapter.ClaudeAdapter()
    assert claude_adapter._children_map() == {}  # pylint: disable=protected-access
    assert adapter.process_scan_failed()
    inv = inflight.inventory(adapter, 4242, _empty_transcript(tmp_path))
    assert not inv.clear
    assert any("process table could not be read" in reason for reason in inv.reasons)
    # No live pid (nothing to walk): the process table is not consulted at all.
    assert inflight.inventory(adapter, 0, _empty_transcript(tmp_path)).clear


def test_c8_a_good_read_after_a_failed_one_in_the_window_still_vetoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass whose first read failed is not cleared by a later good read."""
    monkeypatch.setattr(
        claude_adapter, "_PROC_CACHE", {"ts": 0.0, "map": {}, "failed_at": time.monotonic()}
    )
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    inv = inflight.inventory(claude_adapter.ClaudeAdapter(), 4242, _empty_transcript(tmp_path))
    assert not inv.clear
    monkeypatch.setattr(
        claude_adapter,
        "_PROC_CACHE",
        {"ts": 0.0, "map": {}, "failed_at": time.monotonic() - 60.0},
    )
    assert inflight.inventory(
        claude_adapter.ClaudeAdapter(), 4242, _empty_transcript(tmp_path)
    ).clear


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _agent_launch(agent: str, epoch: float) -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": _iso(epoch),
            "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": agent},
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_" + agent}]},
        }
    )


def _agent_notification(agent: str, epoch: float, *, keepalive: bool) -> str:
    if keepalive:
        summary = f'Agent "{agent}" is waiting on its own background work'
        note = (
            "This agent stopped with background work of its own still running. It may "
            "resume when that work finishes, and the same task-id notifies again."
        )
    else:
        summary = f'Agent "{agent}" finished'
        note = "fires each time this agent stops with no live background children"
    content = (
        f"<task-notification>\n<task-id>{agent}</task-id>\n<status>completed</status>\n"
        f"<summary>{summary}</summary>\n<note>{note}</note>\n</task-notification>"
    )
    return json.dumps(
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": _iso(epoch),
            "content": content,
        }
    )


def test_c8_keepalive_notification_keeps_the_agent_and_never_calls_the_counter_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-18 23:20:43Z: agent a8d6673e notified "waiting on its own background work";
    at 23:27:39Z the drain called the store counter (1, SubagentStart 177 s old) stale and
    cleared it while the agent ran. The keep-alive notification no longer ends the agent;
    the later "finished" one does."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    adapter = claude_adapter.ClaudeAdapter()
    now = time.time()
    agent = "a8d6673efdbaa0117"
    transcript = tmp_path / "s.jsonl"
    lines = [
        _agent_launch(agent, now - 1500),
        _agent_notification(agent, now - 400, keepalive=True),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    now_ms = int(now * 1000)
    kept = inflight.inventory(
        adapter,
        0,
        transcript,
        active_subagents=1,
        subagent_started_at=now_ms - 177_000,
        now_ms=now_ms,
    )
    assert not kept.clear and kept.stale_counter == 0
    assert any(agent in reason for reason in kept.reasons)

    lines.append(_agent_notification(agent, now - 5, keepalive=False))
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = inflight.inventory(
        adapter,
        0,
        transcript,
        active_subagents=1,
        subagent_started_at=now_ms - 177_000,
        now_ms=now_ms,
    )
    assert done.clear and done.stale_counter == 1


def test_c8_keepalive_without_a_sign_of_life_ends_after_the_stale_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old keep-alive notification counts as ended unless the subagent jsonl moved."""
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    now = time.time()
    old = now - claude_adapter.KEEPALIVE_STALE_S - 600
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        _agent_launch("ag1", old - 60)
        + "\n"
        + _agent_notification("ag1", old, keepalive=True)
        + "\n",
        encoding="utf-8",
    )
    assert background_work_split(transcript) == ([], [])
    jsonl = tmp_path / "s" / "subagents" / "agent-ag1.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("{}\n", encoding="utf-8")  # written just now: alive
    assert background_work_split(transcript) == (["ag1"], [])
