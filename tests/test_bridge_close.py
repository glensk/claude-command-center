"""``ccc close -s ID -j`` — closing an IDLE session from outside it (tp#1481 B2).

Fakes only: the registry (``discover`` / ``discover_raw``), ``ps`` (the table, the process
tree and the start stamps), the transcript, ``osascript``, ``tmux`` and ``os.kill`` are
stubbed; the store lives in a temporary ``CLAUDE_HOME``. No real session, tab or pane is
ever signalled or closed.
"""

from __future__ import annotations

import json
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from bridgestub import ask, question

from command_center import (
    bridge_cli,
    bridge_close,
    cli,
    config,
    handoff_close,
    staffrole,
    terminal,
)
from command_center.adapters import claude as claude_adapter
from command_center.adapters.claude_agents import AgentEntry
from command_center.bridge_json import BridgeError
from command_center.bridge_target import BridgeDeps
from command_center.models import LiveSession, Status, now_ms
from command_center.snapshot import PsRow
from command_center.store import Store
from command_center.tab_titles import ItermPane

SID = "c1d2e3f4-0000-4000-8000-000000000001"
PID = 4321
START = "Tue Nov  3 09:00:00 2026"
TAB = "w0t1p0:TAB-UUID"
TTY = "ttys011"
SHELL = 4000
OWN = "777:Tue Nov  3 10:00:00 2026"
BG_SHELL = "/bin/zsh -c source /x/shell-snapshots/snapshot-zsh-1.sh && eval 'sleep 600' < /dev/null"


def _record(kind: str, uuid: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"type": kind, "uuid": uuid, "isSidechain": False, "sessionId": SID}
    base.update(extra)
    return base


def _prompt(uuid: str, text: str) -> dict[str, Any]:
    return _record(
        "user",
        uuid,
        message={"role": "user", "content": text},
        origin={"kind": "human"},
        promptSource="typed",
    )


def _reply(uuid: str, text: str) -> dict[str, Any]:
    return _record(
        "assistant",
        uuid,
        message={
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    )


@dataclass
class World:  # pylint: disable=too-many-instance-attributes
    """The faked registry, ps, transcript and terminals of one session."""

    transcript: Path
    statuses: list[str] = field(default_factory=lambda: ["idle"])
    kind: str = "interactive"
    entries: int = 1
    alive: bool = True
    table: dict[int, Any] = field(default_factory=dict)
    starts: list[str] = field(default_factory=list)
    killed: list[tuple[int, int]] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    tmux_pane: str = ""
    tmux_kills: list[str] = field(default_factory=list)
    osascript_out: str | None = "tab\n"
    background_roster: bool = False
    on_sleep: list[Any] = field(default_factory=list)
    on_kill: list[Any] = field(default_factory=list)

    def status(self) -> str:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def append(self, record: dict[str, Any]) -> None:
        with self.transcript.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def live(self) -> list[LiveSession]:
        if not self.alive:
            return []
        return [
            LiveSession(
                pid=PID,
                session_id=SID,
                cwd="/repo",
                kind=self.kind,
                raw_status=self.status(),
                alive=True,
                conflict=self.entries > 1,
            )
            for _ in range(self.entries)
        ]


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[World, BridgeDeps]:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = config.load_config()
    cfg.stop_barrier_enabled = True
    cfg.stop_barrier_wait_sec = 1
    cfg.stop_barrier_settle_sec = 0
    config.save_config(cfg)
    world = World(transcript=tmp_path / "s.jsonl")
    world.append(_prompt("u1", "do the thing"))
    world.append(_reply("a1", "done"))
    world.table = {
        1: PsRow(0, "", "S", "launchd"),
        SHELL: PsRow(1, TTY, "S", "-zsh"),
        PID: PsRow(SHELL, TTY, "S+", "claude --resume x"),
    }
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(SID, iterm_session_id=TAB)

    def _discover_raw(_self: Any, session_id: str = "") -> list[LiveSession]:
        return [e for e in world.live() if not session_id or e.session_id == session_id]

    def _children() -> dict[int, list[tuple[int, str]]]:
        out: dict[int, list[tuple[int, str]]] = {}
        for pid, row in world.table.items():
            out.setdefault(row.ppid, []).append((pid, row.command))
        return out

    def _kill(pid: int, sig: int) -> None:
        world.killed.append((pid, sig))
        world.alive = False
        world.table.pop(pid, None)
        for hook in world.on_kill:
            hook()

    def _osascript(script: str, *_a: Any, **_k: Any) -> str | None:
        world.scripts.append(script)
        if world.osascript_out:
            world.table.pop(SHELL, None)
        return world.osascript_out

    def _run(argv: list[str], *_a: Any, **_k: Any) -> Any:
        assert argv[:2] == ["tmux", "kill-pane"], argv
        world.tmux_kills.append(argv[-1])
        world.table.pop(SHELL, None)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _sleep(_sec: float) -> None:
        if world.on_sleep:
            world.on_sleep.pop(0)()

    monkeypatch.setattr(cli.ClaudeAdapter, "discover_raw", _discover_raw)
    monkeypatch.setattr(cli.ClaudeAdapter, "transcript_path", lambda *_a, **_k: world.transcript)
    monkeypatch.setattr(claude_adapter, "_children_map", _children)
    monkeypatch.setattr(cli.os, "kill", _kill)
    monkeypatch.setattr(cli, "_DRAIN_POLL_SEC", 0.0)
    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(terminal, "ps_table", lambda: world.table)
    monkeypatch.setattr(
        terminal, "pid_start", lambda _p: world.starts.pop(0) if world.starts else START
    )
    monkeypatch.setattr(terminal, "stop_hook_tokens", lambda _cwd: set())
    monkeypatch.setattr(
        terminal,
        "tmux_pane_for_session",
        lambda _sid: ("main", world.tmux_pane) if world.tmux_pane else None,
    )
    monkeypatch.setattr(terminal, "_osascript", _osascript)
    monkeypatch.setattr(staffrole, "adopted_role", lambda _sid: None)
    monkeypatch.setattr(handoff_close, "STABLE_WINDOW_SEC", 0.0)
    monkeypatch.setattr(handoff_close, "EXIT_WAIT_SEC", 0.0)
    monkeypatch.setattr(handoff_close, "_sleep", _sleep)
    monkeypatch.setattr(handoff_close, "own_identity", lambda: OWN)

    def _agents() -> list[AgentEntry]:
        if not world.background_roster:
            return []
        return [AgentEntry(config_dir="/acct", session_id=SID, kind="background")]

    deps = BridgeDeps(
        discover=world.live,
        agents=_agents,
        ps_table=lambda: world.table,
        panes=lambda: [ItermPane(uuid="TAB-UUID", tty=f"/dev/{TTY}", name="x")],
        lock_dir=lambda: tmp_path / "locks",
    )
    return world, deps


def _row() -> Any:
    with Store() as store:
        return store.get(SID)


def _refused(deps: BridgeDeps, reason: str) -> None:
    with pytest.raises(BridgeError) as err:
        bridge_close.run_close(deps, SID)
    assert err.value.code == reason, err.value.message
    assert err.value.data == {"session_id": SID, "result": "refused", "reason": reason}


def _nothing_signalled(world: World) -> None:
    assert not world.killed and not world.scripts and not world.tmux_kills
    row = _row()
    assert not row.close_token  # an own arm is retired: never a close-after-turn


def test_idle_closes_tab_and_parks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    data = bridge_close.run_close(deps, SID)
    assert data == {
        "session_id": SID,
        "result": "closed",
        "terminal": "tab",
        "status": Status.PARKED.value,
        "close_debt": False,
    }
    assert world.killed == [(PID, signal.SIGTERM)]
    assert len(world.scripts) == 1
    row = _row()
    assert row.status == Status.PARKED.value and not row.done and not row.close_token
    # tp#1785: the signal leaves its session-close line.
    log = (config.app_home() / "events.log").read_text(encoding="utf-8")
    assert f"{SID}\tsession-close\tpid={PID} path=bridge-close caller=" in log


def test_idle_done_stays_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    with Store() as store:
        store.update_fields(SID, done=True, status=Status.DONE.value)
    data = bridge_close.run_close(deps, SID)
    assert data["result"] == "closed" and data["status"] == Status.DONE.value
    assert world.killed and _row().status == Status.DONE.value and _row().done


def test_done_but_busy_is_refused_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    with Store() as store:
        store.update_fields(SID, done=True, status=Status.DONE.value)
    world.statuses = ["busy"]
    _refused(deps, "busy")
    _nothing_signalled(world)


@pytest.mark.parametrize("raw", ["busy", "waiting"])
def test_not_idle_registry_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.statuses = [raw]
    _refused(deps, "busy")
    _nothing_signalled(world)


@pytest.mark.parametrize("keepwarm", [False, True])
def test_shell_with_no_open_turn_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keepwarm: bool
) -> None:
    """tp#1662: ``shell`` after the turn ended (a keep-warm timer alive) is idle."""
    world, deps = _install(tmp_path, monkeypatch)
    world.statuses = ["shell"]
    if keepwarm:
        world.table[5002] = PsRow(PID, TTY, "S", BG_SHELL.replace("sleep 600", "cc-keepwarm.sh"))
    data = bridge_close.run_close(deps, SID)
    assert data["result"] == "closed"
    assert world.killed == [(PID, signal.SIGTERM)]


def test_shell_with_real_background_shell_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.statuses = ["shell"]
    world.table[5001] = PsRow(PID, TTY, "S", BG_SHELL)
    _refused(deps, "background_shell")
    _nothing_signalled(world)


def test_shell_with_open_turn_is_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.statuses = ["shell"]
    world.append(_prompt("u2", "another one"))
    _refused(deps, "busy")
    _nothing_signalled(world)


def test_shell_without_transcript_is_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown turn state keeps ``shell`` busy (fail closed)."""
    world, deps = _install(tmp_path, monkeypatch)
    world.statuses = ["shell"]
    monkeypatch.setattr(cli.ClaudeAdapter, "transcript_path", lambda *_a, **_k: None)
    _refused(deps, "busy")
    _nothing_signalled(world)


def test_open_turn_is_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.append(_prompt("u2", "another one"))
    _refused(deps, "busy")
    _nothing_signalled(world)


def test_subagent_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.table[5000] = PsRow(PID, TTY, "S", "claude -p subagent")
    _refused(deps, "subagents")
    _nothing_signalled(world)


def test_background_shell_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.table[5001] = PsRow(PID, TTY, "S", BG_SHELL)
    _refused(deps, "background_shell")
    _nothing_signalled(world)


def test_picker_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.append(_prompt("u2", "pick"))
    world.append(ask("toolu_1", [question("Which?", ["a", "b"])]))
    world.statuses = ["waiting"]
    _refused(deps, "picker_pending")
    _nothing_signalled(world)


def test_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.alive = False
    _refused(deps, "not_running")
    assert not world.killed and not world.scripts


@pytest.mark.parametrize("roster", [False, True])
def test_background_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roster: bool) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    if roster:  # a background job only `claude agents` lists
        world.alive = False
        world.background_roster = True
    else:
        world.kind = "bg"
    _refused(deps, "background")
    assert not world.killed and not world.scripts


def test_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.entries = 2
    _refused(deps, "conflict")
    assert not world.killed and not world.scripts


def test_staff_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(staffrole, "adopted_role", lambda _sid: "dev-1")
    _refused(deps, "staff_bound")
    assert not world.killed and not world.scripts


def test_stop_chain_not_drained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.table[6000] = PsRow(PID, TTY, "S", "python3 /hooks/commit-hook.py")
    monkeypatch.setattr(terminal, "stop_hook_tokens", lambda _cwd: {"commit-hook.py"})
    monkeypatch.setattr(bridge_close, "_probe", lambda *_a, **_k: "")  # the hook is no bg shell
    _refused(deps, "stop_chain")
    _nothing_signalled(world)


def test_pid_replaced_between_check_and_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    # start stamps: bind, two stability observes, then the fresh pre-SIGTERM re-check differs
    world.starts = [START, START, START, "Tue Nov  3 11:11:11 2026"]
    _refused(deps, "identity_changed")
    _nothing_signalled(world)


def test_concurrent_armed_close_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An armed ``mark-done --close`` already claimed by the Stop chain: refused, signalled once."""
    world, deps = _install(tmp_path, monkeypatch)
    with Store() as store:
        token = store.arm_close(SID, now_ms())
        assert store.claim_close(SID, token, "")  # the Stop chain's close-now holds it
    _refused(deps, "close_in_flight")
    assert not world.killed
    cli._sigterm(PID)  # the in-flight closer's own signal
    assert world.killed == [(PID, signal.SIGTERM)]
    assert _row().close_token == token  # never retired by the refused caller


def test_lost_claim_race_is_close_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(Store, "claim_close", lambda *_a, **_k: 0)
    _refused(deps, "close_in_flight")
    _nothing_signalled(world)


def test_pending_user_arm_is_claimed_and_restored_on_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    with Store() as store:
        token = store.arm_close(SID, now_ms())
    world.table[5001] = PsRow(PID, TTY, "S", BG_SHELL)
    _refused(deps, "background_shell")
    row = _row()
    assert row.close_token == token and row.close_requested_at > 0  # still the user's arm
    assert not world.killed


def test_transcript_appended_in_window_is_stale_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(handoff_close, "STABLE_WINDOW_SEC", 60.0)
    clock = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(handoff_close.time, "monotonic", lambda: next(clock, 100.0))
    world.on_sleep.append(lambda: world.append(_record("system", "s1", subtype="x")))
    _refused(deps, "stale_evidence")
    _nothing_signalled(world)


def test_tmux_hosted_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.tmux_pane = "%7"
    data = bridge_close.run_close(deps, SID)
    assert data["result"] == "closed" and data["terminal"] == "tmux"
    assert world.tmux_kills == ["%7"] and not world.scripts


def test_repeated_call_is_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    assert bridge_close.run_close(deps, SID)["result"] == "closed"
    _refused(deps, "not_running")
    assert world.killed == [(PID, signal.SIGTERM)]  # signalled once only
    assert len(world.scripts) == 1


def test_unvalidated_tab_is_never_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stale-evidence guard: a stored tab id on another tty is never closed."""
    world, deps = _install(tmp_path, monkeypatch)
    deps.panes = lambda: [ItermPane(uuid="TAB-UUID", tty="/dev/ttys099", name="x")]
    data = bridge_close.run_close(deps, SID)
    assert data["terminal"] == "none" and not world.scripts
    assert world.killed == [(PID, signal.SIGTERM)]


def test_failed_terminal_close_keeps_debt_and_retry_never_resignals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.osascript_out = ""
    data = bridge_close.run_close(deps, SID)
    assert data["terminal"] == "none" and data["close_debt"] is True
    assert handoff_close.Debt.decode(_row().close_bound) is not None
    world.osascript_out = "session\n"
    again = bridge_close.run_close(deps, SID)
    assert again["terminal"] == "pane" and again["close_debt"] is False
    assert world.killed == [(PID, signal.SIGTERM)]  # never a second signal
    assert not _row().close_token


def test_applescript_has_no_activate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    bridge_close.run_close(deps, SID)
    assert world.scripts
    for script in world.scripts:
        assert "activate" not in script and "select" not in script and "close" in script


def test_cli_envelope_and_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    world.statuses = ["busy"]
    monkeypatch.setattr(bridge_cli.bridge_target, "default_deps", lambda: deps)
    assert cli.main(["close", "-s", SID, "-j"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False and env["error"]["code"] == "busy"
    assert env["data"]["reason"] == "busy"
    assert "close" in bridge_cli.JSON_COMMANDS
    with pytest.raises(SystemExit) as done:
        cli.main(["close", "-h"])
    assert done.value.code == 0
    assert "idle" in capsys.readouterr().out


def test_sessions_json_exposes_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    with Store() as store:
        store.update_fields(SID, done=True)
        row = store.get(SID)
    live = LiveSession(pid=PID, session_id=SID, cwd="/repo", raw_status="idle", alive=True)
    entry = bridge_cli.session_entry(live, row, (TAB, True), lambda _d: "a")
    assert entry["done"] is True
    assert bridge_cli.session_entry(live, None, (None, False), lambda _d: "a")["done"] is False


# --------------------------------------------------------------------------- tp#1790 -A
AGENT = "dev-1"


def _shell(command: str) -> str:
    """A Bash-tool shell argv running *command* (``'`` written as ``'\\''``)."""
    quoted = command.replace("'", "'\\''")
    return f"/bin/zsh -c source /x/shell-snapshots/snapshot-zsh-1.sh && eval '{quoted}' < /dev/null"


def _bg_launch(world: World, use_id: str, task_id: str, command: str) -> None:
    """A ``run_in_background`` Bash launch of *command* in the transcript (no notification)."""
    world.append(
        _record(
            "assistant",
            f"a-{use_id}",
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": use_id,
                        "name": "Bash",
                        "input": {"command": command, "run_in_background": True},
                    }
                ],
                "stop_reason": "tool_use",
            },
        )
    )
    world.append(
        _record(
            "user",
            f"u-{use_id}",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": use_id, "content": "started"}],
            },
            toolUseResult={"backgroundTaskId": task_id},
        )
    )
    world.append(_reply(f"r-{use_id}", "waiting for mail"))


def _staff_session(
    world: World, monkeypatch: pytest.MonkeyPatch, command: str, role: str = AGENT
) -> None:
    """An idle staff session bound to *role* holding one background shell running *command*."""
    monkeypatch.setattr(staffrole, "adopted_role", lambda _sid: role)
    world.statuses = ["shell"]
    _bg_launch(world, "toolu_bell", "bell1", command)
    world.table[5003] = PsRow(PID, TTY, "S", _shell(command))


def _staff_refused(deps: BridgeDeps, reason: str, agent: str = AGENT) -> None:
    with pytest.raises(BridgeError) as err:
        bridge_close.run_close(deps, SID, agent)
    assert err.value.code == reason, err.value.message
    assert err.value.data == {"session_id": SID, "result": "refused", "reason": reason}


def test_staff_close_without_flag_is_staff_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, f"staff wait -a {AGENT}")
    _refused(deps, "staff_bound")
    _nothing_signalled(world)


@pytest.mark.parametrize("role", ["dev-2", None])
def test_staff_close_other_agent_is_staff_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    """``-A`` naming another agent than the binding (or a session bound to none) refuses."""
    world, deps = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(staffrole, "adopted_role", lambda _sid: role)
    _staff_refused(deps, "staff_bound")
    _nothing_signalled(world)


@pytest.mark.parametrize(
    "command",
    [
        f"staff wait -a {AGENT}",
        f"staff watch -a {AGENT}",
        f"cd /x && STAFF_HOME=/s ~/bin/staff.py wait --agent={AGENT} -t 0 2>&1 | tail -5",
        f"staff wait --agent {AGENT}",
    ],
)
def test_staff_close_doorbell_only_passes_both_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, command)
    probes: list[str] = []
    real = bridge_close._probe

    def _counting(*args: Any, **kwargs: Any) -> str:
        probes.append(real(*args, **kwargs))
        return probes[-1]

    monkeypatch.setattr(bridge_close, "_probe", _counting)
    data = bridge_close.run_close(deps, SID, AGENT)
    assert data["result"] == "closed"
    assert probes == ["", ""]  # the idle gate's and _close_live's probe, both clear
    assert world.killed == [(PID, signal.SIGTERM)]


@pytest.mark.parametrize(
    "command",
    [
        "staff wait -a dev-2",  # another agent's doorbell
        "staff wait",  # no explicit agent ($STAFF_AGENT): not provably this agent's
        f"staff wait -a {AGENT}; make test",  # a chained command is work
        f"echo staff wait -a {AGENT}",  # not anchored on the command word
        f"staff wait -a {AGENT} $(sleep 600)",  # a command substitution
        "sleep 600",  # any other shell
    ],
)
def test_staff_close_other_shell_is_background_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, command)
    _staff_refused(deps, "background_shell")
    _nothing_signalled(world)


def test_staff_close_doorbell_plus_other_shell_is_background_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, f"staff wait -a {AGENT}")
    world.table[5001] = PsRow(PID, TTY, "S", BG_SHELL)
    _staff_refused(deps, "background_shell")
    _nothing_signalled(world)


def test_staff_close_second_probe_sees_new_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real shell appearing after the idle gate is caught by _close_live's probe."""
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, f"staff wait -a {AGENT}")
    real = bridge_close._idle_gate

    def _gate(*args: Any) -> None:
        real(*args)
        world.table[5001] = PsRow(PID, TTY, "S", BG_SHELL)

    monkeypatch.setattr(bridge_close, "_idle_gate", _gate)
    _staff_refused(deps, "background_shell")
    _nothing_signalled(world)


def test_staff_close_readopt_in_window_is_identity_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, f"staff wait -a {AGENT}")
    roles = iter([AGENT])  # run_close's check reads AGENT, the re-read under the lock dev-2
    monkeypatch.setattr(staffrole, "adopted_role", lambda _sid: next(roles, "dev-2"))
    _staff_refused(deps, "identity_changed")
    _nothing_signalled(world)


def test_staff_close_binding_reread_under_tab_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-read sits inside the tab lock, after the revalidation, before the signal."""
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, f"staff wait -a {AGENT}")
    order: list[str] = []
    real_lock = bridge_close.tab_lock
    real_check = bridge_close._check_binding
    real_revalidate = handoff_close._revalidate

    class _Lock:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __enter__(self) -> Any:
            order.append("lock")
            return self.inner.__enter__()

        def __exit__(self, *exc: Any) -> Any:
            order.append("unlock")
            return self.inner.__exit__(*exc)

    def _check(*args: Any) -> None:
        order.append("binding")
        real_check(*args)

    def _revalidate(*args: Any) -> None:
        order.append("revalidate")
        real_revalidate(*args)

    world.on_kill.append(lambda: order.append("kill"))
    monkeypatch.setattr(bridge_close, "tab_lock", lambda *a: _Lock(real_lock(*a)))
    monkeypatch.setattr(bridge_close, "_check_binding", _check)
    monkeypatch.setattr(handoff_close, "_revalidate", _revalidate)
    assert bridge_close.run_close(deps, SID, AGENT)["result"] == "closed"
    assert order == ["lock", "revalidate", "binding", "kill", "unlock"]


def test_staff_close_cli_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world, deps = _install(tmp_path, monkeypatch)
    _staff_session(world, monkeypatch, f"staff wait -a {AGENT}")
    monkeypatch.setattr(bridge_cli.bridge_target, "default_deps", lambda: deps)
    assert cli.main(["close", "-s", SID, "-A", "dev-2", "-j"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "staff_bound"
    assert not world.killed
    assert cli.main(["close", "-s", SID, "--staff-agent", AGENT, "-j"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True and env["data"]["result"] == "closed"
    with pytest.raises(SystemExit) as done:
        cli.main(["close", "-h"])
    assert done.value.code == 0
    assert "--staff-agent" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("tail", "status"),
    [
        ("ended", "idle"),  # no open turn: a doorbell-only session is idle
        ("open", "busy"),  # an open turn
        ("unknown", "busy"),  # no transcript: unknown stays busy (fail closed)
    ],
)
def test_staff_close_sessions_json_shell_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str, status: str
) -> None:
    world, _deps = _install(tmp_path, monkeypatch)
    if tail == "open":
        world.append(_prompt("u2", "another one"))
    live = LiveSession(pid=PID, session_id=SID, cwd="/repo", raw_status="shell", alive=True)
    adapter = cli.ClaudeAdapter()
    if tail == "unknown":
        monkeypatch.setattr(cli.ClaudeAdapter, "transcript_path", lambda *_a, **_k: None)
    turn = bridge_cli.shell_turn_active(adapter, live, "shell")
    assert bridge_cli.live_status(live, None, turn) == status
    entries = bridge_cli.collect_sessions(
        adapter=_OneLive(adapter, live),  # type: ignore[arg-type]
        panes_reader=lambda: [],
        ps_reader=lambda: world.table,
        account_label=lambda _d: "a",
        agents_reader=list,
    )
    assert [(e["status"], e["raw_status"]) for e in entries] == [(status, "shell")]


class _OneLive:
    """An adapter whose registry is *live* only (the real transcript lookup otherwise)."""

    def __init__(self, inner: Any, live: LiveSession) -> None:
        self.inner = inner
        self.live = live

    def discover(self) -> list[LiveSession]:
        return [self.live]

    def transcript_path(self, *args: Any, **kwargs: Any) -> Path | None:
        return self.inner.transcript_path(*args, **kwargs)
