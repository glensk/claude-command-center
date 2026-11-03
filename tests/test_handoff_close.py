"""``ccc close-now --handoff-transfer`` — the token-scoped handoff closer (tp#1582 B3).

Every SIGTERM, ``os.kill``, tmux/iTerm close and ``spawn_ccc`` is stubbed; the registry,
``ps`` and the transcript are fakes and the store lives in a temporary ``CLAUDE_HOME``.
No live tab or process is touched.
"""

from __future__ import annotations

import argparse
import json
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from command_center import cli, handoff_close, terminal
from command_center.models import LiveSession, now_ms
from command_center.snapshot import PsRow
from command_center.store import Store
from command_center.transfer_store import Transfer

SRC = "a1b2c3d4-0000-4000-8000-000000000001"
TGT = "e5f6a7b8-0000-4000-8000-000000000002"
TID = "t0123456789ab"
PID = 4321
START = "Mon Nov  2 20:00:00 2026"
ITERM = "w0t1p0:STORED-UUID"
TTY = "ttys009"
SHELL = 4000  # the tab's login shell: holds the tty until the tab closes
OWN = "777:Mon Nov  2 21:00:00 2026"  # this closer's own "pid:start" (its token holder)


def _record(kind: str, uuid: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"type": kind, "uuid": uuid, "isSidechain": False, "sessionId": SRC}
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
    """The faked registry, ps table, transcript and terminal of one source session."""

    transcript: Path
    statuses: list[str] = field(default_factory=lambda: ["idle"])
    entries: int = 1
    table: dict[int, Any] = field(default_factory=dict)
    starts: list[str] = field(default_factory=list)
    killed: list[tuple[int, int]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    close_result: str = "tab"
    exits_on_kill: bool = True
    notes: list[str] = field(default_factory=list)
    on_sleep: list[Any] = field(default_factory=list)

    def status(self) -> str:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def append(self, record: dict[str, Any]) -> None:
        with self.transcript.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def _barrier(enabled: bool) -> None:
    from command_center import config

    cfg = config.load_config()
    cfg.stop_barrier_enabled = enabled
    cfg.stop_barrier_wait_sec = 1
    cfg.stop_barrier_settle_sec = 0
    config.save_config(cfg)


def _seed(*, state: str = "finalized", capped: bool = False) -> str:
    """A finalized transfer SRC → TGT with lineage set and a pending close token."""
    now = now_ms()
    with Store() as store:
        store.ensure(SRC, cwd="/repo")
        store.ensure(TGT, cwd="/repo")
        store.update_fields(SRC, iterm_session_id=ITERM)
        store.create_transfer(
            Transfer(transfer_id=TID, source_session=SRC, target_session=TGT, capped=capped),
            now,
        )
        store.conn.execute("UPDATE transfers SET state = ? WHERE transfer_id = ?", (state, TID))
        store.conn.commit()
        store.set_handoff_lineage(SRC, TGT, now)
        return store.ensure_close_armed(SRC, now)


def _row() -> Any:
    with Store() as store:
        return store.get(SRC)


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, barrier: bool = True) -> World:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _barrier(barrier)
    transcript = tmp_path / "src.jsonl"
    world = World(transcript=transcript)
    world.append(_prompt("u1", "do the thing"))
    world.append(_reply("a1", "done"))
    world.table = {
        1: PsRow(0, "", "S", "launchd"),
        SHELL: PsRow(1, TTY, "S", "-zsh"),
        PID: PsRow(SHELL, TTY, "S+", "claude --resume x"),
    }

    def _discover_raw(_self: Any, session_id: str = "") -> list[LiveSession]:
        status = world.status()
        return [
            LiveSession(pid=PID, session_id=session_id, cwd="/repo", raw_status=status, alive=True)
            for _ in range(world.entries)
        ]

    def _kill(pid: int, sig: int) -> None:
        world.killed.append((pid, sig))
        if world.exits_on_kill:
            world.table.pop(pid, None)

    def _close(target: str) -> str:
        world.closed.append(target)
        if world.close_result:
            world.table.pop(SHELL, None)
        return world.close_result

    def _pid_start(_pid: int) -> str:
        return world.starts.pop(0) if world.starts else START

    def _sleep(_sec: float) -> None:
        if world.on_sleep:
            world.on_sleep.pop(0)()

    def _no_spawn(argv: list[str]) -> bool:
        raise AssertionError(f"spawn_ccc must not run: {argv}")

    monkeypatch.setattr(cli.ClaudeAdapter, "discover_raw", _discover_raw)
    monkeypatch.setattr(cli.ClaudeAdapter, "transcript_path", lambda *_a, **_k: transcript)
    monkeypatch.setattr(cli.os, "kill", _kill)
    monkeypatch.setattr(cli, "_DRAIN_POLL_SEC", 0.0)
    monkeypatch.setattr(cli, "_notify_close_now", world.notes.append)
    monkeypatch.setattr(terminal, "ps_table", lambda: world.table)
    monkeypatch.setattr(terminal, "pid_start", _pid_start)
    monkeypatch.setattr(terminal, "stop_hook_tokens", lambda _cwd: set())
    monkeypatch.setattr(terminal, "tmux_pane_for_session", lambda _sid: None)
    monkeypatch.setattr(terminal, "close_iterm_session", _close)
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _no_spawn)
    monkeypatch.setattr(handoff_close, "STABLE_WINDOW_SEC", 0.0)
    monkeypatch.setattr(handoff_close, "EXIT_WAIT_SEC", 0.0)
    monkeypatch.setattr(handoff_close, "_sleep", _sleep)
    monkeypatch.setattr(handoff_close, "own_identity", lambda: OWN)
    return world


def _args(**extra: Any) -> argparse.Namespace:
    base = {"session": SRC, "handoff_transfer": TID, "iterm": "", "pid": 0}
    base.update(extra)
    return argparse.Namespace(**base)


def _events(tmp_path: Path) -> str:
    path = tmp_path / "command-center" / "events.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _assert_refused_and_restored(world: World, token: str, tmp_path: Path, text: str) -> None:
    assert not world.killed and not world.closed
    row = _row()
    assert row.close_token == token and row.close_requested_at > 0  # pending again
    assert text in _events(tmp_path)
    assert not world.notes  # a routine refusal never notifies


def test_the_fixture_transcript_reads_idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the idle fixture has no open turn, a later prompt opens one."""
    from command_center.bridge_send import inspect_transcript

    world = _install(tmp_path, monkeypatch)
    assert inspect_transcript(world.transcript).turn_active is False
    world.append(_prompt("u2", "one more thing"))
    assert inspect_transcript(world.transcript).turn_active is True


def test_idle_source_is_reaped_and_its_tab_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proven path, through the real parser: SIGTERM once, close the tab, retire."""
    world = _install(tmp_path, monkeypatch)
    _seed()
    assert cli.main(["close-now", "-H", TID, "-s", SRC]) == 0
    assert world.killed == [(PID, signal.SIGTERM)]
    assert world.closed == [ITERM]
    row = _row()
    assert row.close_token == "" and row.close_requested_at == 0 and row.close_bound == ""
    assert "reaped, closed" in _events(tmp_path)


def test_keep_warm_child_only_still_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A keep-warm timer under the source is disposable: never a hook, never a veto."""
    world = _install(tmp_path, monkeypatch)
    world.table[5000] = PsRow(
        PID,
        TTY,
        "S",
        "/bin/zsh -c source /home/me/.claude/shell-snapshots/snapshot-zsh-1.sh && "
        "eval '/home/me/.claude/hooks/cc-keepwarm.sh 3300'",
    )
    world.table[5001] = PsRow(5000, TTY, "S", "sleep 3300")
    _seed()
    assert handoff_close.run(_args()) == 0
    assert world.killed == [(PID, signal.SIGTERM)]


@pytest.mark.parametrize("status", ["busy", "waiting", "brand-new-status"])
def test_busy_or_unknown_registry_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    world = _install(tmp_path, monkeypatch)
    world.statuses = [status]
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "source busy")


@pytest.mark.parametrize(("entries", "text"), [(2, "conflict"), (0, "no live source process")])
def test_conflicting_or_missing_entries_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: int, text: str
) -> None:
    world = _install(tmp_path, monkeypatch)
    world.entries = entries
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, text)


def test_open_turn_in_the_transcript_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch)
    world.append(_prompt("u2", "still working"))
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "turn active")


def test_unreadable_transcript_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _install(tmp_path, monkeypatch)
    world.transcript.unlink()
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "transcript unreadable")


def test_prompt_starting_during_the_window_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt lands between two idle proofs: the turn is open, nothing is signalled."""
    world = _install(tmp_path, monkeypatch)
    world.on_sleep.append(lambda: world.append(_prompt("u2", "a new prompt")))
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "turn active")


def test_transcript_moving_during_the_window_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still idle, but the transcript identity changed (a closed turn was appended)."""
    world = _install(tmp_path, monkeypatch)
    world.on_sleep.append(lambda: world.append(_reply("a2", "late reply")))
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "changed during the stability window")


def test_registry_turning_busy_during_the_window_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch)
    world.statuses = ["idle", "idle", "busy"]  # bind, first proof, then the window's proof
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "source busy")


def test_replaced_pid_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same pid number, different start time: a newcomer — never signalled."""
    world = _install(tmp_path, monkeypatch)
    world.starts = [START, START, "Tue Nov  3 07:00:00 2026"]
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "replaced")


def test_a_claimed_token_makes_a_second_closer_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another closer (or the Stop chain) holds the token in flight: this one touches nothing."""
    world = _install(tmp_path, monkeypatch)
    token = _seed()
    with Store() as store:
        assert store.claim_close(SRC, token)
    assert handoff_close.run(_args()) == 1
    assert not world.killed and not world.closed
    row = _row()
    assert row.close_token == token and row.close_requested_at == 0  # left to its holder
    assert "held by another closer" in _events(tmp_path)


def test_duplicate_closers_one_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _install(tmp_path, monkeypatch)
    _seed()
    assert handoff_close.run(_args()) == 0
    assert handoff_close.run(_args()) == 1
    assert world.killed == [(PID, signal.SIGTERM)]
    assert world.closed == [ITERM]
    assert "no close armed" in _events(tmp_path)


def test_the_claim_records_the_closers_own_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In flight, the row names THIS closer and the claim time; the retire clears both."""
    world = _install(tmp_path, monkeypatch)
    seen: list[tuple[int, int, str]] = []
    world.on_sleep.append(
        lambda: seen.append(
            (_row().close_requested_at, _row().close_claimed_at, _row().close_holder)
        )
    )
    _seed()
    assert handoff_close.run(_args()) == 0
    assert len(seen) == 1
    stamp, claimed_at, holder = seen[0]
    assert stamp == 0 and claimed_at > 0 and holder == OWN
    row = _row()
    assert (row.close_token, row.close_claimed_at, row.close_holder) == ("", 0, "")


def test_an_unreadable_self_probe_refuses_without_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holder "" must only mean "nobody registered": no own identity → no claim at all."""
    world = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(handoff_close, "own_identity", lambda: "")
    token = _seed()
    before = _row()
    assert handoff_close.run(_args()) == 1
    assert not world.killed and not world.closed
    row = _row()
    assert (row.close_token, row.close_requested_at) == (token, before.close_requested_at)
    assert (row.close_claimed_at, row.close_holder, row.close_bound) == (0, "", "")
    assert "not claimed" in _events(tmp_path)


def test_own_identity_is_this_process_pid_and_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real probe: OK → "pid:start"; ps unreadable → ""."""
    import os  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(terminal, "ps_table", lambda: {os.getpid(): PsRow(1, "", "S", "ccc")})
    monkeypatch.setattr(terminal, "pid_start", lambda _pid: START)
    assert handoff_close.own_identity() == f"{os.getpid()}:{START}"
    monkeypatch.setattr(terminal, "ps_table", dict)
    assert handoff_close.own_identity() == ""


def test_a_token_reclaimed_and_reclaimed_mid_run_fails_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclaim + a second closer's claim during the window: closer A never signals."""
    world = _install(tmp_path, monkeypatch)
    other = "999:Mon Nov  2 22:00:00 2026"
    token = _seed()

    def steal() -> None:
        row = _row()
        with Store() as store:
            assert store.reclaim_stale_close(
                SRC, token, holder=OWN, claimed_at=row.close_claimed_at, now=now_ms()
            )
            assert store.claim_close(SRC, token, other)

    world.on_sleep.append(steal)
    assert handoff_close.run(_args()) == 1
    assert not world.killed and not world.closed
    row = _row()
    assert (row.close_token, row.close_requested_at, row.close_holder) == (token, 0, other)
    assert "changed hands" in _events(tmp_path)


def test_a_late_first_closer_never_settles_the_second_closers_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclaimed, closer B claims: closer A's _retire and _restore are both no-ops."""
    _install(tmp_path, monkeypatch)
    other = "999:Mon Nov  2 22:00:00 2026"
    token = _seed()
    with Store() as store:
        armed_at = store.claim_close(SRC, token, OWN)
        claimed_at = _row().close_claimed_at
        assert store.reclaim_stale_close(
            SRC, token, holder=OWN, claimed_at=claimed_at, now=now_ms()
        )
        assert store.claim_close(SRC, token, other)
    claim_a = handoff_close.Claim(SRC, TID, token, armed_at, "", OWN)
    assert not handoff_close._retire(claim_a)  # pylint: disable=protected-access
    assert not handoff_close._restore(claim_a, "")  # pylint: disable=protected-access
    row = _row()
    assert (row.close_token, row.close_requested_at, row.close_holder) == (token, 0, other)


@pytest.mark.parametrize(
    ("kw", "text"),
    [
        ({"state": "accepted"}, "not finalized"),
        ({"capped": True}, "capped"),
    ],
)
def test_lineage_refusals_never_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any], text: str
) -> None:
    world = _install(tmp_path, monkeypatch)
    token = _seed(**kw)
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, text)


def test_wrong_source_or_target_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _install(tmp_path, monkeypatch)
    token = _seed()
    with Store() as store:
        store.update_fields(SRC, handed_off_to="someone-else")
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "not handed off")
    assert handoff_close.run(_args(session=TGT)) == 1
    assert not world.killed


def test_lineage_lost_before_the_signal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revalidation right before the SIGTERM sees the lineage move."""
    world = _install(tmp_path, monkeypatch)
    token = _seed()

    def _drop_lineage() -> None:
        with Store() as store:
            store.update_fields(SRC, handed_off_to="")

    world.on_sleep.append(_drop_lineage)
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "not handed off")


def test_terminal_close_failure_keeps_the_debt_then_retry_closes_pane_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM landed but iTerm could not close the tab: the debt is kept with the recorded
    pane/tty, and a retry closes it WITHOUT a second signal."""
    world = _install(tmp_path, monkeypatch)
    world.close_result = ""  # the tab is still there (its shell holds the tty)
    token = _seed()
    assert handoff_close.run(_args()) == 1
    assert world.killed == [(PID, signal.SIGTERM)]
    assert world.closed == [ITERM]
    assert world.notes and "close debt kept" in world.notes[0]
    row = _row()
    assert row.close_token == token and row.close_requested_at > 0
    debt = handoff_close.Debt.decode(row.close_bound)
    assert debt == handoff_close.Debt(PID, START, "", ITERM, f"/dev/{TTY}")

    world.close_result = "tab"
    assert handoff_close.run(_args()) == 0
    assert world.killed == [(PID, signal.SIGTERM)]  # never re-signalled
    assert world.closed == [ITERM, ITERM]
    row = _row()
    assert row.close_token == "" and row.close_bound == ""


def test_retry_retires_when_the_pane_is_positively_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process is gone and nothing holds the recorded tty: the tab is gone too."""
    world = _install(tmp_path, monkeypatch)
    world.close_result = ""
    _seed()
    assert handoff_close.run(_args()) == 1
    world.table.pop(SHELL)  # the user closed the tab by hand
    assert handoff_close.run(_args()) == 0
    assert world.killed == [(PID, signal.SIGTERM)]
    assert _row().close_token == ""


def test_retry_never_closes_a_tab_a_claude_runs_in_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch)
    world.close_result = ""
    _seed()
    assert handoff_close.run(_args()) == 1
    world.table[6000] = PsRow(SHELL, TTY, "S+", "claude --resume y")
    assert handoff_close.run(_args()) == 1
    assert world.closed == [ITERM]  # the retry closed nothing
    assert _row().close_token == ""  # debt dropped, never closed
    assert len(world.killed) == 1


def test_retry_refuses_while_the_signalled_process_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch)
    world.exits_on_kill = False
    world.close_result = ""
    token = _seed()
    assert handoff_close.run(_args()) == 1
    assert handoff_close.run(_args()) == 1
    assert len(world.killed) == 1  # the retry never signals
    row = _row()
    assert row.close_token == token and handoff_close.Debt.decode(row.close_bound) is not None
    assert "not re-signalled" in _events(tmp_path)


def test_idle_guard_applies_with_the_stop_barrier_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch, barrier=False)
    world.statuses = ["busy"]
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "source busy")


def test_barrier_off_skips_only_the_drain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _install(tmp_path, monkeypatch, barrier=False)

    def _no_drain(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the drain must not run with the barrier off")

    monkeypatch.setattr(cli, "_wait_for_stop_drain", _no_drain)
    _seed()
    assert handoff_close.run(_args()) == 0
    assert world.killed == [(PID, signal.SIGTERM)]


def test_running_stop_hook_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli,
        "_wait_for_stop_drain",
        lambda *_a, **_k: (terminal.DRAIN_RUNNING, ["/bin/sh /home/me/.claude/stop-commit.sh"]),
    )
    token = _seed()
    assert handoff_close.run(_args()) == 1
    _assert_refused_and_restored(world, token, tmp_path, "Stop hooks still running")


# --- debt before the signal (tp#1595 B2) -------------------------------------------------

_EXPECTED_DEBT = handoff_close.Debt(PID, START, "", ITERM, f"/dev/{TTY}")


class _Killed(BaseException):
    """A SIGKILL of the closer: no ``except Exception`` runs, nothing settles the token."""


def _no_sigterm(pid: int) -> bool:
    raise AssertionError(f"a retry must never signal (pid {pid})")


def _reclaim(token: str) -> None:
    """The daemon sweep's reclaim of the dead closer's token (holder and claim time as read)."""
    row = _row()
    with Store() as store:
        assert store.reclaim_stale_close(
            SRC, token, holder=OWN, claimed_at=row.close_claimed_at, now=now_ms()
        )


def _assert_in_flight_with_debt(token: str) -> None:
    row = _row()
    assert (row.close_token, row.close_requested_at, row.close_holder) == (token, 0, OWN)
    assert handoff_close.Debt.decode(row.close_bound) == _EXPECTED_DEBT


def test_the_debt_is_on_the_row_before_the_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch)
    token = _seed()
    seen: list[Any] = []
    real_sigterm = cli._sigterm  # pylint: disable=protected-access

    def _sigterm(pid: int) -> bool:
        seen.append(_row())
        return real_sigterm(pid)

    monkeypatch.setattr(cli, "_sigterm", _sigterm)
    assert handoff_close.run(_args()) == 0
    assert len(seen) == 1
    row = seen[0]
    assert (row.close_token, row.close_requested_at, row.close_holder) == (token, 0, OWN)
    assert handoff_close.Debt.decode(row.close_bound) == _EXPECTED_DEBT
    assert world.killed == [(PID, signal.SIGTERM)]
    assert _row().close_token == ""  # proven close: retired as before


@pytest.mark.parametrize("race", ["reclaimed", "holder"])
def test_a_failed_debt_write_refuses_without_a_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    """The token moves between the revalidation and the debt write: nothing is signalled."""
    world = _install(tmp_path, monkeypatch)
    other = "999:Mon Nov  2 22:00:00 2026"
    token = _seed()
    real_revalidate = handoff_close._revalidate  # pylint: disable=protected-access

    def _revalidate_then_lose(*a: Any) -> None:
        real_revalidate(*a)
        with Store() as store:
            if race == "reclaimed":
                row = _row()
                assert store.reclaim_stale_close(
                    SRC, token, holder=OWN, claimed_at=row.close_claimed_at, now=now_ms()
                )
                assert store.claim_close(SRC, token, other)
            else:
                store.conn.execute(
                    "UPDATE sessions SET close_holder = ? WHERE session_id = ?", (other, SRC)
                )
                store.conn.commit()

    monkeypatch.setattr(handoff_close, "_revalidate", _revalidate_then_lose)
    monkeypatch.setattr(cli, "_sigterm", _no_sigterm)
    assert handoff_close.run(_args()) == 1
    assert not world.killed and not world.closed
    row = _row()
    assert (row.close_token, row.close_requested_at, row.close_holder) == (token, 0, other)
    assert row.close_bound == ""  # no debt written, the other holder's claim untouched
    assert "changed hands before the signal" in _events(tmp_path)


def test_a_closer_killed_after_the_signal_leaves_a_debt_the_retry_closes_unsignalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _install(tmp_path, monkeypatch)
    token = _seed()

    def _killed_while_waiting(_pid: int, _start: str) -> bool:
        raise _Killed

    monkeypatch.setattr(handoff_close, "_wait_exit", _killed_while_waiting)
    with pytest.raises(_Killed):
        handoff_close.run(_args())
    assert world.killed == [(PID, signal.SIGTERM)] and not world.closed
    _assert_in_flight_with_debt(token)

    _reclaim(token)
    monkeypatch.setattr(cli, "_sigterm", _no_sigterm)
    retried: list[handoff_close.Debt] = []
    real_retry = handoff_close.retry_terminal

    def _retry(claim: handoff_close.Claim, debt: handoff_close.Debt) -> int:
        retried.append(debt)
        return real_retry(claim, debt)

    monkeypatch.setattr(handoff_close, "retry_terminal", _retry)
    assert handoff_close.run(_args()) == 0
    assert retried == [_EXPECTED_DEBT]
    assert world.killed == [(PID, signal.SIGTERM)]  # never re-signalled
    assert world.closed == [ITERM]
    row = _row()
    assert (row.close_token, row.close_bound) == ("", "")


def test_a_closer_killed_before_the_signal_leaves_a_debt_the_retry_never_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The residual window (debt written, kill() not reached): fail safe, the tab stays."""
    world = _install(tmp_path, monkeypatch)
    token = _seed()

    def _killed_before_kill(_pid: int) -> bool:
        raise _Killed

    monkeypatch.setattr(cli, "_sigterm", _killed_before_kill)
    with pytest.raises(_Killed):
        handoff_close.run(_args())
    assert not world.killed and not world.closed
    _assert_in_flight_with_debt(token)

    _reclaim(token)
    monkeypatch.setattr(cli, "_sigterm", _no_sigterm)
    assert handoff_close.run(_args()) == 1
    assert not world.killed and not world.closed
    row = _row()
    assert row.close_token == token and row.close_requested_at > 0  # pending, debt kept
    assert handoff_close.Debt.decode(row.close_bound) == _EXPECTED_DEBT
    assert "still running after SIGTERM" in _events(tmp_path)
    assert "not re-signalled" in _events(tmp_path)


def test_an_error_after_the_signal_keeps_the_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception past the debt write never restores the old binding: retry is pane-only."""
    world = _install(tmp_path, monkeypatch)
    token = _seed()

    def _close_raises(_pane: str, _iterm: str) -> bool:
        raise RuntimeError("osascript exploded")

    monkeypatch.setattr(cli, "_close_terminal", _close_raises)
    assert handoff_close.run(_args()) == 1
    assert world.killed == [(PID, signal.SIGTERM)]
    row = _row()
    assert row.close_token == token and row.close_requested_at > 0
    assert handoff_close.Debt.decode(row.close_bound) == _EXPECTED_DEBT
    assert "internal error" in _events(tmp_path)
