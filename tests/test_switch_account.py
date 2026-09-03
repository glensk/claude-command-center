"""Tests for ``ccc switch-account`` / ``ccc switch-now`` — the fail-closed account relaunch.

Five layers, all hermetic (no real osascript / tmux / ps / kill / notification / network):

* :mod:`command_center.store` — the atomic one-shot after-turn claim and its expectation.
* :mod:`command_center.hooks` — what the ``release-locks`` Stop hook spawns, and the
  SessionStart consumption of the expectation.
* ``cli.cmd_switch_account`` — every guard that must refuse to arm.
* ``cli.cmd_switch_now`` — every guard that must refuse to signal or type.
* the primitives the two commands stand on (:mod:`command_center.accounts`,
  :mod:`command_center.terminal`, the adapter's ``ignore`` ancestry filter).

``pending_background_work`` itself is covered in ``tests/test_adapter.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from command_center import accounts, cli, config, hooks, terminal
from command_center.adapters import claude as claude_adapter
from command_center.models import LiveSession, SwitchClaim, now_ms
from command_center.snapshot import PsRow
from command_center.store import Store

SID = "s1"
PID = 4321
ITERM = "w0t1p0:UUID"
TTY = "/dev/ttys009"
START = "Tue Sep  2 23:00:00 2026"


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def two_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two configured accounts — ``private`` is the default one — and no in-session env.

    ``private`` doubles as ``CLAUDE_HOME``, so ``accounts.env_config_dir()`` with
    ``CLAUDE_CONFIG_DIR`` unset resolves to it (the seat a bare shell bills), and the
    store/events log live under it too. Every env var the commands read as evidence is
    cleared so each test opts INTO the evidence it wants.
    """
    private, work = tmp_path / ".claude", tmp_path / ".claude-work"
    private.mkdir()
    work.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(private))
    for name in (
        "CCC_INTERNAL",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_PID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "TMUX_PANE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "claude_config_dirs", lambda: {"private": private, "work": work})
    return private, work


def _recorder(sink: list[list[str]]) -> Callable[[list[str]], bool]:
    """A ``spawn.spawn_ccc`` stand-in that records argv and reports success."""

    def record(argv: list[str]) -> bool:
        sink.append(argv)
        return True

    return record


def _transcript(config_dir: Path, cwd: str, sid: str = SID) -> Path:
    """Claude's EXACT transcript path for an account, cwd and session id."""
    return config_dir / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl"


def _jsonl(path: Path, records: list[object] | None = None) -> Path:
    """Write a minimal valid JSONL transcript at *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = records if records is not None else [{"type": "user"}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _live(cwd: str, config_dir: Path, *, pid: int = PID, status: str = "idle") -> LiveSession:
    """One ALIVE registry entry for :data:`SID`."""
    return LiveSession(
        pid=pid,
        session_id=SID,
        cwd=cwd,
        alive=True,
        config_dir=str(config_dir),
        raw_status=status,
    )


def _stub_account_probes(
    monkeypatch: pytest.MonkeyPatch, private: Path, work: Path
) -> list[tuple[str, str]]:
    """Pin identity/trust for both accounts; return the ``ensure_trusted`` call log."""
    trusted: list[tuple[str, str]] = []

    def ensure_trusted(target: str, workdir: str | Path | None = None) -> bool:
        trusted.append((str(target), str(workdir)))
        return True

    emails = {str(private.resolve()): "p@example.com", str(work.resolve()): "w@example.com"}
    monkeypatch.setattr(accounts, "ensure_trusted", ensure_trusted)
    monkeypatch.setattr(accounts, "is_trusted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        accounts, "account_email", lambda directory: emails.get(str(Path(directory).resolve()))
    )
    monkeypatch.setattr(config, "claude_account_email_map", lambda: {"work": "w@example.com"})
    return trusted


def _prepare_arm(
    monkeypatch: pytest.MonkeyPatch,
    pair: tuple[Path, Path],
    *,
    live: LiveSession | None = None,
    inside: bool = True,
) -> tuple[str, list[tuple[str, str]]]:
    """Stub every probe ``switch-account`` consults so a clean call would arm.

    ``inside=True`` supplies the in-session evidence (``CLAUDE_CODE_SESSION_ID`` +
    ``CLAUDE_PID``) that makes this shell the session's own; ``inside=False`` leaves
    them unset, which is the "-N from another tab" path where the registry entry —
    not the environment — is authoritative. Returns the session cwd and the
    ``ensure_trusted`` log.
    """
    private, work = pair
    entry = live if live is not None else _live(os.getcwd(), private)
    cwd = entry.cwd
    monkeypatch.setattr(cli.ClaudeAdapter, "discover", lambda _self: [entry])
    monkeypatch.setattr(cli, "_background_work", lambda *_args, **_kwargs: [])
    source = _jsonl(_transcript(private, cwd))
    _jsonl(_transcript(work, cwd))
    monkeypatch.setattr(
        cli.ClaudeAdapter, "transcript_path", lambda _self, *_args, **_kwargs: source
    )
    monkeypatch.setattr(terminal, "ps_table", lambda: {})
    monkeypatch.setattr(terminal, "pid_ancestry", lambda *_args: frozenset())
    monkeypatch.setattr(terminal, "tmux_pane_for_session", lambda _sid: None)
    _stub_account_probes(monkeypatch, private, work)
    trusted = _stub_account_probes(monkeypatch, private, work)
    if inside:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        monkeypatch.setenv("CLAUDE_PID", str(PID))
    return cwd, trusted


class _KillSpy:
    """Record delivered signals; answer the sig-0 liveness probe per *exits*."""

    def __init__(self, exits: bool = True) -> None:
        self.exits = exits
        self.signals: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        if sig == 0:
            if self.exits and self.signals:
                raise ProcessLookupError(pid)
            return
        self.signals.append((pid, sig))


def _now_args(target: Path, source: Path, cwd: Path, **over: object) -> argparse.Namespace:
    """The complete ``switch-now`` Namespace the hook would hand it."""
    values: dict[str, object] = {
        "session": SID,
        "iterm": ITERM,
        "tmux_pane": "",
        "pid": PID,
        "config_dir": str(target),
        "source_dir": str(source),
        "cwd": str(cwd),
        "force": False,
        "no_codex": False,
    }
    values.update(over)
    return argparse.Namespace(**values)


class _NowEnv:
    """The stubbed world ``cmd_switch_now`` runs against (one per test)."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.kill = _KillSpy()
        self.typed: dict[str, list[tuple[str, str]]] = {"iterm": [], "tmux": []}
        self.notes: list[str] = []


def _prepare_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pair: tuple[Path, Path]
) -> _NowEnv:
    """Stub every probe ``switch-now`` makes so the happy path completes.

    The two accounts SHARE one transcript store (``work/projects`` is a symlink to
    ``private/projects``), which is the ``samefile`` precondition the relauncher
    insists on. All waits are collapsed to ~50 ms and ``time.sleep`` is neutered.
    """
    private, work = pair
    cwd = tmp_path / "repo"
    cwd.mkdir(exist_ok=True)
    _jsonl(_transcript(private, str(cwd)))
    if not (work / "projects").exists():
        (work / "projects").symlink_to(private / "projects", target_is_directory=True)
    for name, value in (
        ("_SWITCH_POLL_SEC", 0.0),
        ("_SWITCH_EXIT_WAIT_SEC", 0.05),
        ("_SWITCH_HOOK_WAIT_SEC", 0.05),
        ("_SWITCH_READY_WAIT_SEC", 0.05),
    ):
        monkeypatch.setattr(cli, name, value)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(terminal, "ps_table", lambda: {})
    monkeypatch.setattr(terminal, "iterm_session_tty", lambda _sid: TTY)
    monkeypatch.setattr(terminal, "tmux_pane_info", lambda _pane: (100, TTY))
    monkeypatch.setattr(terminal, "pid_is_claude", lambda pid, _table: pid == PID)
    monkeypatch.setattr(terminal, "pid_tty", lambda _pid, _table: TTY)
    monkeypatch.setattr(terminal, "pid_start", lambda _pid: START)
    monkeypatch.setattr(terminal, "pid_descends_from", lambda *_args: True)
    monkeypatch.setattr(terminal, "live_hook_children", lambda *_args: [])
    monkeypatch.setattr(terminal, "tty_ready_for_input", lambda *_args: True)
    monkeypatch.setattr(cli, "_background_work", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        cli, "_strict_live_entries", lambda _sid: [LiveSession(PID, SID, str(cwd), alive=True)]
    )
    _stub_account_probes(monkeypatch, private, work)
    env = _NowEnv(_now_args(work, private, cwd))

    def _type_iterm(sid: str, text: str) -> bool:
        env.typed["iterm"].append((sid, text))
        return True

    def _type_tmux(pane: str, text: str) -> bool:
        env.typed["tmux"].append((pane, text))
        return True

    monkeypatch.setattr(terminal, "type_into_iterm_session", _type_iterm)
    monkeypatch.setattr(terminal, "tmux_send_keys", _type_tmux)
    monkeypatch.setattr(cli, "_notify_switch", env.notes.append)
    monkeypatch.setattr(cli.os, "kill", env.kill)
    return env


# --------------------------------------------------------------------------- #
# 1. store — the atomic after-turn claim
# --------------------------------------------------------------------------- #
def test_claim_after_turn_returns_the_switch_snapshot_exactly_once(tmp_path: Path) -> None:
    """A fresh arm yields one row-derived claim, keeps the expectation, then goes quiet."""
    with Store(tmp_path / "db") as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(
            SID,
            switch_requested_at=now_ms(),
            switch_config_dir="/target",
            switch_force=1,
            no_codex=True,
        )
        assert store.claim_after_turn(SID, now_ms(), 60_000) == (
            "switch",
            SwitchClaim(target="/target", force=True, no_codex=True, cwd="/repo"),
        )
        row = store.get(SID)
        assert row is not None
        # The timestamp is spent, but the target survives as the SessionStart expectation.
        assert (row.switch_requested_at, row.switch_config_dir, row.switch_force) == (
            0,
            "/target",
            0,
        )
        assert store.claim_after_turn(SID, now_ms(), 60_000) == ("", None)


def test_claim_after_turn_clears_an_expired_arm_without_claiming_it(tmp_path: Path) -> None:
    """A stamp older than the TTL is wiped, never fired into a later unrelated turn."""
    with Store(tmp_path / "db") as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(
            SID, switch_requested_at=899, switch_config_dir="/target", switch_force=1
        )
        assert store.claim_after_turn(SID, 1000, 100) == ("", None)
        row = store.get(SID)
        assert row is not None
        assert (row.switch_requested_at, row.switch_config_dir, row.switch_force) == (0, "", 0)


def test_claim_after_turn_close_beats_switch_and_drops_every_switch_column(
    tmp_path: Path,
) -> None:
    """A tab about to close has nowhere to relaunch — the switch is dropped whole."""
    with Store(tmp_path / "db") as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(
            SID,
            close_requested_at=now_ms(),
            switch_requested_at=now_ms(),
            switch_config_dir="/target",
            switch_force=1,
        )
        assert store.claim_after_turn(SID, now_ms(), 60_000) == ("close", None)
        row = store.get(SID)
        assert row is not None
        assert (row.switch_requested_at, row.switch_config_dir, row.switch_force) == (0, "", 0)


def test_claim_after_turn_has_one_winner_across_two_connections(tmp_path: Path) -> None:
    """Two Store connections on one DB cannot both claim the same arm."""
    db = tmp_path / "db"
    first, second = Store(db), Store(db)
    try:
        first.ensure(SID, cwd="/repo")
        first.update_fields(SID, switch_requested_at=now_ms(), switch_config_dir="/target")
        results = [
            first.claim_after_turn(SID, now_ms(), 60_000),
            second.claim_after_turn(SID, now_ms(), 60_000),
        ]
        assert [kind for kind, _claim in results].count("switch") == 1
        assert sum(bool(kind) for kind, _claim in results) == 1
    finally:
        first.close()
        second.close()


def test_pop_switch_expectation_is_one_shot(tmp_path: Path) -> None:
    """The expected account is handed out once and cleared, so it cannot linger."""
    with Store(tmp_path / "db") as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(SID, switch_config_dir="/target")
        assert store.pop_switch_expectation(SID) == "/target"
        assert store.pop_switch_expectation(SID) == ""


# --------------------------------------------------------------------------- #
# 2. hooks — release-locks spawns the relauncher, SessionStart consumes the arm
# --------------------------------------------------------------------------- #
def _arm_switch(work: Path, *, force: int = 0, no_codex: bool = False) -> None:
    """Arm a fresh switch-after-turn for :data:`SID` in the default store."""
    with Store() as store:
        store.ensure(SID, cwd="/stored")
        store.update_fields(
            SID,
            switch_requested_at=now_ms(),
            switch_config_dir=str(work),
            switch_force=force,
            no_codex=no_codex,
        )


def test_release_locks_spawns_one_switch_now_with_the_full_hook_evidence(
    two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The armed switch spawns exactly one relauncher carrying argv for every launch fact."""
    private, work = two_accounts
    monkeypatch.setenv("ITERM_SESSION_ID", ITERM)
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("CLAUDE_PID", str(PID))
    _arm_switch(work, force=1, no_codex=True)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    assert hooks.handle_release_locks({"session_id": SID, "cwd": "/payload"}) == 0
    assert calls == [
        [
            "switch-now",
            "--session",
            SID,
            "--iterm",
            ITERM,
            "--tmux-pane",
            "%7",
            "--config-dir",
            str(work),
            "--source-dir",
            str(private),
            "--cwd",
            "/payload",
            "--pid",
            str(PID),
            "--force",
            "--no-codex",
        ]
    ]
    assert hooks.handle_release_locks({"session_id": SID, "cwd": "/payload"}) == 0
    assert len(calls) == 1  # the claim is one-shot: a second Stop spawns nothing


def test_release_locks_omits_force_no_codex_and_a_nonnumeric_pid(
    two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unforced, codex-allowed arm and a junk ``$CLAUDE_PID`` add no flags."""
    _private, work = two_accounts
    monkeypatch.setenv("CLAUDE_PID", "not-a-pid")
    _arm_switch(work)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_release_locks({"session_id": SID, "cwd": "/payload"})
    assert "--pid" not in calls[0]
    assert "--force" not in calls[0]
    assert "--no-codex" not in calls[0]


def test_release_locks_spawns_only_close_now_when_both_are_armed(
    two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a close armed alongside, no switch-now is spawned and the switch state dies."""
    _private, work = two_accounts
    _arm_switch(work, force=1)
    with Store() as store:
        store.update_fields(SID, close_requested_at=now_ms())
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_release_locks({"session_id": SID, "cwd": "/payload"})
    assert calls == [["close-now", "--session", SID, "--iterm", ""]]
    with Store() as store:
        row = store.get(SID)
    assert row is not None
    assert (row.switch_requested_at, row.switch_config_dir, row.switch_force) == (0, "", 0)


@pytest.mark.parametrize("matching", [True, False])
def test_session_start_suppresses_the_heads_up_only_for_the_expected_account(
    two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, matching: bool
) -> None:
    """An intentional relaunch is silent; an unexpected account still gets the warning."""
    private, work = two_accounts
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    expected = work if matching else work.parent / ".claude-elsewhere"
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        # Stored account differs from the env account => ensure_current_session warns.
        store.update_fields(SID, config_dir=str(private), switch_config_dir=str(expected))
    emitted: list[tuple[str, str | None]] = []
    monkeypatch.setattr(hooks, "_emit_context", lambda event, text: emitted.append((event, text)))
    assert hooks.handle_session_start({"session_id": SID, "cwd": "/repo"}) == 0
    context = emitted[0][1] or ""
    assert ("Heads up" in context) is not matching
    with Store() as store:
        row = store.get(SID)
    assert row is not None and row.switch_config_dir == ""  # consumed either way


# --------------------------------------------------------------------------- #
# 3. cli — `ccc switch-account` arms only when every guard is satisfied
# --------------------------------------------------------------------------- #
def test_switch_account_arms_the_target_and_pre_trusts_the_cwd(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean in-session call stamps target + timestamp, no force, and trusts the cwd."""
    _private, work = two_accounts
    cwd, trusted = _prepare_arm(monkeypatch, two_accounts)
    assert cli.main(["switch-account", "work", "-s", SID]) == 0
    with Store() as store:
        row = store.get(SID)
    assert row is not None
    assert row.switch_requested_at > 0
    assert (row.switch_config_dir, row.switch_force) == (str(work), 0)
    assert trusted == [(str(work), cwd)]
    assert "armed:" in capsys.readouterr().out


def test_switch_account_force_records_the_flag(
    two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-f`` travels into the row so the relauncher inherits the decision."""
    _prepare_arm(monkeypatch, two_accounts)
    assert cli.main(["switch-account", "work", "-s", SID, "-f"]) == 0
    with Store() as store:
        row = store.get(SID)
    assert row is not None and row.switch_force == 1


def test_switch_account_undo_disarms_once_then_reports_nothing_armed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-u`` clears an arm and a second ``-u`` fails loudly instead of pretending."""
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(SID, switch_requested_at=now_ms(), switch_config_dir="/target")
    assert cli.main(["switch-account", "-u", "-s", SID]) == 0
    with Store() as store:
        row = store.get(SID)
    assert row is not None and (row.switch_requested_at, row.switch_config_dir) == (0, "")
    assert cli.main(["switch-account", "-u", "-s", SID]) == 1
    assert "nothing armed" in capsys.readouterr().err


def test_switch_account_refuses_a_single_account_setup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With one configured account there is nothing to switch to."""
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "only one Claude account" in capsys.readouterr().err


def test_switch_account_refuses_an_unknown_label(
    two_accounts: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfigured label names no config dir, so nothing can be armed."""
    assert cli.main(["switch-account", "nope", "-s", SID]) == 1
    assert "unknown account" in capsys.readouterr().err


def test_switch_account_refuses_the_account_it_already_bills(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Relaunching onto the current seat would change nothing but cost a turn."""
    _private, work = two_accounts
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    _prepare_arm(monkeypatch, two_accounts, live=_live(os.getcwd(), work))
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "already bills" in capsys.readouterr().err


def test_switch_account_refuses_a_headless_entrypoint_and_arms_nothing(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``claude -p`` run is no interactive tab to relaunch — refuse before any write."""
    _prepare_arm(monkeypatch, two_accounts)
    monkeypatch.setenv("CCC_INTERNAL", "1")
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "nothing armed" in capsys.readouterr().err
    with Store() as store:
        assert store.get(SID) is None


def test_switch_account_requires_the_transcript_under_the_targets_own_project_path(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transcript only the SOURCE account can see would resume into 'No conversation found'."""
    _private, work = two_accounts
    cwd, _trusted = _prepare_arm(monkeypatch, two_accounts)
    _transcript(work, cwd).unlink()
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "cannot see" in capsys.readouterr().err


def test_switch_account_refuses_when_the_shell_belongs_to_another_session(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An arm fires from its own Stop hooks, so it must be issued from inside that session."""
    _prepare_arm(monkeypatch, two_accounts)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "someone-else")
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "belongs to session" in capsys.readouterr().err


@pytest.mark.parametrize("registry", ["raises", "two-alive", "none-alive"])
def test_switch_account_fails_closed_on_an_unusable_registry(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registry: str,
) -> None:
    """Unreadable, ambiguous and empty registries all refuse rather than guess a process."""
    private, _work = two_accounts
    _prepare_arm(monkeypatch, two_accounts)
    cwd = os.getcwd()
    if registry == "raises":

        def discover(_self: object) -> list[LiveSession]:
            raise OSError("registry unreadable")

    elif registry == "two-alive":

        def discover(_self: object) -> list[LiveSession]:
            return [_live(cwd, private, pid=1), _live(cwd, private, pid=2)]

    else:

        def discover(_self: object) -> list[LiveSession]:
            return []

    monkeypatch.setattr(cli.ClaudeAdapter, "discover", discover)
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    expected = {
        "raises": "could not read",
        "two-alive": "live under two Claude accounts",
        "none-alive": "not a live session",
    }[registry]
    assert expected in capsys.readouterr().err


def test_switch_account_refuses_a_pid_that_disagrees_with_the_registry(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``$CLAUDE_PID`` other than the registry's would target the wrong process."""
    _prepare_arm(monkeypatch, two_accounts)
    monkeypatch.setenv("CLAUDE_PID", "9999")
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert f"registry lists pid {PID}" in capsys.readouterr().err


def test_switch_account_refuses_when_the_registry_account_differs_from_the_env(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A registry seat that contradicts ``$CLAUDE_CONFIG_DIR`` makes billing unknowable."""
    other = tmp_path / ".claude-third"
    other.mkdir()
    _prepare_arm(monkeypatch, two_accounts, live=_live(os.getcwd(), other))
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "the registry says" in capsys.readouterr().err


def test_switch_account_refuses_background_work_unless_forced(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In-flight background work would die unreported — it takes an explicit ``--force``."""
    _prepare_arm(monkeypatch, two_accounts)
    monkeypatch.setattr(cli, "_background_work", lambda *_args, **_kwargs: ["a subagent"])
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    err = capsys.readouterr().err
    assert "a subagent" in err and "--force" in err
    with Store() as store:
        assert store.get(SID) is None
    assert cli.main(["switch-account", "work", "-s", SID, "-f"]) == 0
    assert "warning (--force)" in capsys.readouterr().err


@pytest.mark.parametrize("drift", ["target-not-its-email", "both-dirs-one-identity"])
def test_switch_account_refuses_account_identity_drift(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    drift: str,
) -> None:
    """A hard-linked label logged in as someone else, or two dirs on one seat, cannot arm."""
    _prepare_arm(monkeypatch, two_accounts)
    if drift == "target-not-its-email":
        monkeypatch.setattr(accounts, "account_email", lambda _directory: "stranger@example.com")
    else:
        monkeypatch.setattr(accounts, "account_email", lambda _directory: "w@example.com")
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "logged in as" in capsys.readouterr().err


def test_switch_account_requires_confirmed_trust_for_the_target(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unconfirmed trust flag would park the relaunch on the trust dialog forever."""
    _prepare_arm(monkeypatch, two_accounts)
    monkeypatch.setattr(accounts, "is_trusted", lambda *_args, **_kwargs: False)
    assert cli.main(["switch-account", "work", "-s", SID]) == 1
    assert "trusted" in capsys.readouterr().err


def test_switch_account_refuses_a_cwd_with_control_characters(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cwd is typed into a shell verbatim, so a control character is a hard refusal."""
    private, _work = two_accounts
    _prepare_arm(monkeypatch, two_accounts, live=_live("/re\x01po", private), inside=False)
    assert cli.main(["switch-account", "work", "-s", SID, "-N"]) == 1
    assert "control characters" in capsys.readouterr().err


def test_switch_account_now_spawns_the_relauncher_without_arming_a_turn(
    two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-N`` needs no model turn: it spawns switch-now and leaves only the expectation."""
    private, work = two_accounts
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.ensure(SID, cwd=str(repo))
        store.update_fields(SID, iterm_session_id=ITERM)
    _prepare_arm(monkeypatch, two_accounts, live=_live(str(repo), private), inside=False)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    assert cli.main(["switch-account", "work", "-s", SID, "-N"]) == 0
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "switch-now"
    assert argv[argv.index("--pid") + 1] == str(PID)
    assert argv[argv.index("--source-dir") + 1] == str(private)
    assert argv[argv.index("--cwd") + 1] == str(repo)
    assert argv[argv.index("--config-dir") + 1] == str(work)
    with Store() as store:
        row = store.get(SID)
    assert row is not None
    assert (row.switch_config_dir, row.switch_requested_at) == (str(work), 0)


@pytest.mark.parametrize("halted", [False, True])
def test_switch_account_now_refuses_a_busy_session_unless_it_is_rate_limit_halted(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    halted: bool,
) -> None:
    """A live turn must not be cut off — but a rate-limit halt is exactly what ``-N`` is for."""
    private, _work = two_accounts
    repo = tmp_path / "repo"
    repo.mkdir()
    with Store() as store:
        store.ensure(SID, cwd=str(repo))
        store.update_fields(SID, iterm_session_id=ITERM)
    _prepare_arm(
        monkeypatch,
        two_accounts,
        live=_live(str(repo), private, status="busy"),
        inside=False,
    )
    monkeypatch.setattr(cli.ClaudeAdapter, "is_halted", lambda _self, _cwd, _sid: halted)
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder([]))
    assert cli.main(["switch-account", "work", "-s", SID, "-N"]) == (0 if halted else 1)
    if not halted:
        assert "mid-turn" in capsys.readouterr().err


def test_switch_account_now_refuses_without_any_terminal_evidence(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no iTerm tab id and no tmux pane there is nowhere to type the relaunch."""
    private, _work = two_accounts
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_arm(monkeypatch, two_accounts, live=_live(str(repo), private), inside=False)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    assert cli.main(["switch-account", "work", "-s", SID, "-N"]) == 1
    assert "no terminal is recorded" in capsys.readouterr().err
    assert calls == []


# --------------------------------------------------------------------------- #
# 4. cli — `ccc switch-now` types only when everything still holds
# --------------------------------------------------------------------------- #
def test_switch_now_types_the_exact_relaunch_into_the_iterm_tab(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path SIGTERMs the bound pid, types the pinned resume and logs it silently."""
    _private, work = two_accounts
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    assert cli.cmd_switch_now(env.args) == 0
    expected = (
        f"cd {shlex.quote(env.args.cwd)} && ( unset CLAUDE_SECURESTORAGE_CONFIG_DIR; "
        f"export CLAUDE_CONFIG_DIR={shlex.quote(str(work))}; claude --resume {SID} )"
    )
    assert env.kill.signals == [(PID, signal.SIGTERM)]
    assert env.typed == {"iterm": [(ITERM, expected)], "tmux": []}
    assert env.notes == []
    log = (config.app_home() / "events.log").read_text(encoding="utf-8")
    assert "relaunched under" in log and "'work'" in log


def test_switch_now_carries_the_no_codex_flag_into_the_typed_command(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-codex`` reaches the shell as the kill switch every integration honours."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    env.args.no_codex = True
    assert cli.cmd_switch_now(env.args) == 0
    assert "export CCC_NO_CODEX=1" in env.typed["iterm"][0][1]


@pytest.mark.parametrize("missing", ["source_dir", "cwd", "config_dir", "session"])
def test_switch_now_requires_every_immutable_launch_fact(
    tmp_path: Path,
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """Nothing is ever defaulted: a missing argv fact aborts before any signal."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    setattr(env.args, missing, "")
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    assert env.notes == []  # a malformed invocation is a bug, not a user-facing failure


def test_switch_now_refuses_a_cwd_with_control_characters(
    tmp_path: Path,
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cwd goes into a typed shell line, so control characters abort before the kill."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    env.args.cwd = f"{env.args.cwd}\x01"
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    assert "control characters" in capsys.readouterr().err


def test_switch_now_requires_terminal_evidence_and_notifies_when_absent(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an iTerm id or a pane there is no tab to own — notify and stop."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    env.args.iterm = ""
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    assert env.notes and "claude --resume" in env.notes[0]


def test_switch_now_refuses_when_the_tabs_tty_is_unknown(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tty is what binds a pid to this tab; without it nothing may be signalled."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(terminal, "iterm_session_tty", lambda _sid: "")
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == [] and env.notes


def test_switch_now_rejects_a_registry_pid_on_a_different_tty(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no usable ``--pid`` hint the registry pid is accepted only on THIS tab's tty."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(terminal, "pid_is_claude", lambda _pid, _table: False)
    monkeypatch.setattr(terminal, "pid_tty", lambda _pid, _table: "/dev/ttys777")
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == [] and env.notes


def test_switch_now_falls_back_to_the_single_registry_pid_on_this_tty(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hookless invocation may bind the one registry pid — but only on the tab's own tty."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    env.args.pid = 0
    assert cli.cmd_switch_now(env.args) == 0
    assert env.kill.signals == [(PID, signal.SIGTERM)]


def test_switch_now_refuses_when_another_live_process_owns_the_session(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second live incarnation (a manual resume, a D9 conflict) makes the kill ambiguous."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(
        cli,
        "_strict_live_entries",
        lambda _sid: [LiveSession(PID, SID, env.args.cwd, alive=True), LiveSession(9999, SID, "")],
    )
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    assert env.notes and "9999" in env.notes[0]


def test_switch_now_vetoes_background_work_unless_the_arm_was_forced(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Background work blocks the kill; a forced arm already made that call at arm time."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(cli, "_background_work", lambda *_args, **_kwargs: ["agent a1"])
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    env.args.force = True
    assert cli.cmd_switch_now(env.args) == 0
    assert env.kill.signals == [(PID, signal.SIGTERM)]


@pytest.mark.parametrize("drift", ["email", "trust", "transcript"])
def test_switch_now_revalidates_the_target_before_signalling(
    tmp_path: Path,
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    """Identity, trust and a shared transcript are re-checked — drift aborts before the kill."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    if drift == "email":
        monkeypatch.setattr(accounts, "account_email", lambda _directory: "stranger@example.com")
    elif drift == "trust":
        monkeypatch.setattr(accounts, "is_trusted", lambda *_args, **_kwargs: False)
    else:
        projects = Path(env.args.config_dir) / "projects"
        projects.unlink()  # replace the shared symlink with an independent copy
        _jsonl(_transcript(Path(env.args.config_dir), env.args.cwd))
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == [] and env.notes


def test_switch_now_waits_out_the_stop_hook_chain_before_the_kill(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook children that never finish (auto-commit, linters) time out instead of being cut off."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(terminal, "live_hook_children", lambda *_args: ["run-stop-hook.sh"])
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    assert env.notes and "Stop hooks still running" in env.notes[0]


def test_switch_now_refuses_a_pid_recycled_during_the_hook_wait(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different start time means the number was handed to a newcomer — never signal it."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    starts = iter([START, "Wed Sep  3 07:00:00 2026"])
    monkeypatch.setattr(terminal, "pid_start", lambda _pid: next(starts, "later"))
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == []
    assert env.notes and "recycled" in env.notes[0]


def test_switch_now_refuses_to_type_when_the_process_never_exits(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text typed while Claude still owns the tty lands in its composer, not the shell."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    survivor = _KillSpy(exits=False)
    monkeypatch.setattr(cli.os, "kill", survivor)
    assert cli.cmd_switch_now(env.args) == 1
    assert survivor.signals == [(PID, signal.SIGTERM)]
    assert env.typed == {"iterm": [], "tmux": []}
    assert env.notes and "still alive" in env.notes[0]


def test_switch_now_refuses_a_tty_that_never_returns_to_a_shell_prompt(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keystrokes are only safe once a POSIX shell owns the foreground process group."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(terminal, "tty_ready_for_input", lambda *_args: False)
    assert cli.cmd_switch_now(env.args) == 1
    assert env.kill.signals == [(PID, signal.SIGTERM)]
    assert env.typed == {"iterm": [], "tmux": []}
    assert env.notes and "POSIX shell prompt" in env.notes[0]


def test_switch_now_notifies_the_manual_command_when_typing_fails(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed delivery never opens a new tab — the user gets the command to run instead."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    monkeypatch.setattr(terminal, "type_into_iterm_session", lambda _sid, _text: False)
    assert cli.cmd_switch_now(env.args) == 1
    assert env.notes and "could not type" in env.notes[0]
    assert "claude --resume" in env.notes[0]


def test_switch_now_uses_tmux_send_keys_and_requires_pane_ancestry(
    tmp_path: Path, two_accounts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside tmux the relaunch goes through send-keys, and only into the pane that owns it."""
    env = _prepare_now(tmp_path, monkeypatch, two_accounts)
    env.args.tmux_pane = "%7"
    assert cli.cmd_switch_now(env.args) == 0
    assert env.typed["tmux"] and not env.typed["iterm"]
    assert env.typed["tmux"][0][0] == "%7"

    other = _prepare_now(tmp_path, monkeypatch, two_accounts)
    other.args.tmux_pane = "%7"
    monkeypatch.setattr(terminal, "pid_descends_from", lambda *_args: False)
    assert cli.cmd_switch_now(other.args) == 1
    assert other.kill.signals == []
    assert other.typed == {"iterm": [], "tmux": []}


# --------------------------------------------------------------------------- #
# 5. accounts — the relaunch line and the trust read-back
# --------------------------------------------------------------------------- #
def test_relaunch_command_pins_a_non_default_account(
    two_accounts: tuple[Path, Path],
) -> None:
    """A named account exports ``CLAUDE_CONFIG_DIR`` inside a subshell, after a ``cd``."""
    _private, work = two_accounts
    assert accounts.relaunch_command(accounts.LaunchTarget(str(work)), SID, "/repo") == (
        f"cd /repo && ( unset CLAUDE_SECURESTORAGE_CONFIG_DIR; "
        f"export CLAUDE_CONFIG_DIR={shlex.quote(str(work))}; claude --resume {SID} )"
    )


def test_relaunch_command_unsets_both_vars_for_the_default_account(
    two_accounts: tuple[Path, Path],
) -> None:
    """The default seat is the UNSET-var account, so both vars are cleared in one unset."""
    private, _work = two_accounts
    assert accounts.relaunch_command(accounts.LaunchTarget(str(private)), SID, "/repo") == (
        f"cd /repo && ( unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR; "
        f"claude --resume {SID} )"
    )


def test_relaunch_command_adds_the_no_codex_kill_switch(
    two_accounts: tuple[Path, Path],
) -> None:
    """A no-codex row carries ``CCC_NO_CODEX=1`` into the relaunched session."""
    _private, work = two_accounts
    command = accounts.relaunch_command(
        accounts.LaunchTarget(str(work), no_codex=True), SID, "/repo"
    )
    assert "export CCC_NO_CODEX=1" in command


def test_relaunch_command_omits_the_cd_without_a_cwd(two_accounts: tuple[Path, Path]) -> None:
    """An unknown cwd relaunches where the shell already is rather than guessing."""
    _private, work = two_accounts
    command = accounts.relaunch_command(accounts.LaunchTarget(str(work)), SID, "")
    assert not command.startswith("cd ") and "cd " not in command
    assert command.startswith("( unset ")


def test_is_trusted_reads_back_the_accepted_trust_flag(tmp_path: Path) -> None:
    """Only an explicit ``hasTrustDialogAccepted: true`` for the RESOLVED cwd counts."""
    work, repo = tmp_path / ".claude-work", tmp_path / "repo"
    work.mkdir()
    repo.mkdir()
    assert accounts.is_trusted(str(work), repo) is False  # no .claude.json at all
    path = work / ".claude.json"
    path.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    assert accounts.is_trusted(str(work), repo) is False
    path.write_text(
        json.dumps({"projects": {str(repo.resolve()): {"hasTrustDialogAccepted": False}}}),
        encoding="utf-8",
    )
    assert accounts.is_trusted(str(work), repo) is False
    path.write_text(
        json.dumps({"projects": {str(repo.resolve()): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    assert accounts.is_trusted(str(work), repo) is True


# --------------------------------------------------------------------------- #
# 6. terminal — the process/tty primitives the relauncher trusts
# --------------------------------------------------------------------------- #
def _ps_table() -> dict[int, Any]:
    """A tiny process tree: login shell → claude → a Stop hook and an MCP server."""
    return {
        100: PsRow(1, "ttys003", "Ss", "-zsh"),
        200: PsRow(100, "ttys003", "S+", "claude --resume s1"),
        300: PsRow(200, "ttys003", "S", "/bin/bash /Users/x/.claude/hooks/run-stop-hook.sh"),
        400: PsRow(200, "??", "S", "node mcp-server.js"),
    }


def test_pid_descends_from_walks_the_parent_chain() -> None:
    """Ancestry is transitive and reflexive, and a missing/reversed pair is never a match."""
    table = _ps_table()
    assert terminal.pid_descends_from(300, 100, table) is True
    assert terminal.pid_descends_from(400, 200, table) is True
    assert terminal.pid_descends_from(200, 200, table) is True
    assert terminal.pid_descends_from(100, 300, table) is False
    assert terminal.pid_descends_from(0, 100, table) is False


def test_pid_tty_normalizes_and_blanks_unknown_ttys() -> None:
    """``ps``'s bare name becomes ``/dev/<name>``; ``??`` and an absent pid are ``""``."""
    table = _ps_table()
    assert terminal.pid_tty(200, table) == "/dev/ttys003"
    assert terminal.pid_tty(400, table) == ""
    assert terminal.pid_tty(999, table) == ""


def test_pid_ancestry_returns_the_pid_and_every_parent() -> None:
    """The caller's own chain is what ``_background_work`` excludes from its veto."""
    assert terminal.pid_ancestry(300, _ps_table()) == frozenset({300, 200, 100, 1})


def test_pid_is_claude_matches_only_a_known_claude_command() -> None:
    """The bound pid must still be a claude — a shell or a gone pid never is."""
    table = _ps_table()
    assert terminal.pid_is_claude(200, table) is True
    assert terminal.pid_is_claude(100, table) is False
    assert terminal.pid_is_claude(0, table) is False


def test_live_hook_children_finds_hook_processes_but_not_mcp_servers() -> None:
    """A Stop turn is over when no hook-looking descendant remains; MCP servers never count."""
    found = terminal.live_hook_children(200, _ps_table())
    assert found == ["/bin/bash /Users/x/.claude/hooks/run-stop-hook.sh"]
    assert terminal.live_hook_children(300, _ps_table()) == []


@pytest.mark.parametrize(
    "rows, ready",
    [
        ({10: PsRow(1, "ttys003", "S+", "zsh")}, True),
        ({10: PsRow(1, "ttys003", "Ss+", "-zsh")}, True),
        ({10: PsRow(1, "ttys003", "S+", "/usr/local/bin/claude --resume s1")}, False),
        ({10: PsRow(1, "ttys003", "S+", "vim notes.md")}, False),
        ({10: PsRow(1, "ttys003", "Ss", "-zsh")}, False),
        ({}, False),
    ],
)
def test_tty_ready_for_input_requires_an_idle_posix_shell(
    rows: dict[int, Any], ready: bool
) -> None:
    """Ready = no claude on the tty and a POSIX shell holding the foreground group."""
    assert terminal.tty_ready_for_input("/dev/ttys003", rows) is ready


def test_type_into_iterm_session_escapes_the_text_into_the_applescript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uuid selects the session and embedded quotes are escaped for the literal."""
    scripts: list[str] = []

    def fake_osascript(script: str, timeout: float = 10) -> str:
        scripts.append(script)
        return "ok"

    monkeypatch.setattr(terminal, "_osascript", fake_osascript)
    assert terminal.type_into_iterm_session(ITERM, 'echo "hi"') is True
    assert "UUID" in scripts[0]
    assert 'echo \\"hi\\"' in scripts[0]
    assert terminal.type_into_iterm_session("", "text") is False


def test_type_into_iterm_session_falls_back_to_the_python_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AppleScript delivers nothing the Python-API rung decides; both failing is False."""
    monkeypatch.setattr(terminal, "_osascript", lambda _script, timeout=10: None)
    monkeypatch.setattr(terminal, "send_text_to_session", lambda _sid, _text: False)
    assert terminal.type_into_iterm_session(ITERM, "text") is False


def test_iterm_session_tty_strips_the_osascript_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AppleScript answer arrives with a trailing newline; the caller compares raw ttys."""
    monkeypatch.setattr(terminal, "_osascript", lambda _script, timeout=10: "/dev/ttys003\n")
    assert terminal.iterm_session_tty(ITERM) == "/dev/ttys003"
    assert terminal.iterm_session_tty("") == ""


def test_tmux_pane_info_parses_pid_and_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``display-message`` returns a tab-separated pair; a missing tmux answers None."""
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="123\t/dev/ttys004\n", stderr=""
        ),
    )
    assert terminal.tmux_pane_info("%3") == (123, "/dev/ttys004")
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: None)
    assert terminal.tmux_pane_info("%3") is None


def test_tmux_send_keys_sends_the_literal_text_then_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-l`` keeps the relaunch line literal; a separate Enter submits it."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    assert terminal.tmux_send_keys("%7", "claude --resume s1") is True
    assert calls == [
        ["/usr/bin/tmux", "send-keys", "-t", "%7", "-l", "claude --resume s1"],
        ["/usr/bin/tmux", "send-keys", "-t", "%7", "Enter"],
    ]


def test_pid_start_returns_the_stripped_ps_lstart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start time is compared verbatim across the hook wait, so it must be stable."""
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{START}\n", stderr=""
        ),
    )
    assert terminal.pid_start(PID) == START
    assert terminal.pid_start(0) == ""


# --------------------------------------------------------------------------- #
# 7. adapter — the `ignore` ancestry filter (a `!` command cannot veto itself)
# --------------------------------------------------------------------------- #
def test_has_background_task_skips_the_callers_own_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Bash-tool shell running ``! ccc switch-account`` must not veto its own switch."""
    monkeypatch.setattr(
        claude_adapter,
        "_children_map",
        lambda: {PID: [(9001, "bash --init-file /x/shell-snapshots/snapshot-abc")]},
    )
    adapter = claude_adapter.ClaudeAdapter()
    assert adapter.has_background_task(PID) is True
    assert adapter.has_background_task(PID, frozenset({9001})) is False


def test_has_subagent_skips_ignored_descendants(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ignored subtree is invisible to the subagent probe as well."""
    monkeypatch.setattr(
        claude_adapter, "_children_map", lambda: {PID: [(9002, "claude -p do the thing")]}
    )
    adapter = claude_adapter.ClaudeAdapter()
    assert adapter.has_subagent(PID) is True
    assert adapter.has_subagent(PID, frozenset({9002})) is False


# --------------------------------------------------------------------------- #
# 8. `-N` from a slash command's inline expansion: busy-by-this-prompt and -c
# --------------------------------------------------------------------------- #
def _write_records(path: Path, *records: dict[str, object]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        ({"type": "user", "message": {"content": "<command-name>/x-now</command-name>"}}, True),
        ({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}, True),
        (
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            False,
        ),
        ({"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}}, False),
    ],
)
def test_transcript_tail_is_user_prompt_reads_only_the_last_record(
    tmp_path: Path, last: dict[str, object], expected: bool
) -> None:
    """A bare user prompt as the last record ⇒ nothing generated yet; anything else ⇒ not idle."""
    from command_center.adapters.claude import transcript_tail_is_user_prompt

    path = tmp_path / "t.jsonl"
    _write_records(path, {"type": "assistant", "message": {"content": []}}, last)
    assert transcript_tail_is_user_prompt(path) is expected


def test_transcript_tail_is_user_prompt_is_false_on_empty_or_broken_files(tmp_path: Path) -> None:
    """Unknown is never idle: an empty, missing or unparsable tail answers False."""
    from command_center.adapters.claude import transcript_tail_is_user_prompt

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"type": "user"\n', encoding="utf-8")
    assert transcript_tail_is_user_prompt(empty) is False
    assert transcript_tail_is_user_prompt(broken) is False
    assert transcript_tail_is_user_prompt(tmp_path / "missing.jsonl") is False


@pytest.mark.parametrize("tail_is_prompt", [True, False])
def test_switch_account_now_tolerates_busy_raised_by_the_prompt_being_expanded(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tail_is_prompt: bool,
) -> None:
    """Inside the session, registry 'busy' with a bare user prompt as the transcript's last
    record is the prompt being expanded — not a turn in flight; a turn in progress still refuses."""
    private, _work = two_accounts
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)  # inside the session: the command's own cwd IS the session cwd
    monkeypatch.setenv("ITERM_SESSION_ID", ITERM)  # …and its tab id is in the env
    with Store() as store:
        store.ensure(SID, cwd=str(repo))
        store.update_fields(SID, iterm_session_id=ITERM)
    _prepare_arm(monkeypatch, two_accounts, live=_live(str(repo), private, status="busy"))
    last: dict[str, object] = (
        {"type": "user", "message": {"content": "<command-name>/cpriv-to-cwork-now</command-name>"}}
        if tail_is_prompt
        else {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
    )
    _write_records(
        _transcript(private, str(repo)), {"type": "user", "message": {"content": "q"}}, last
    )
    monkeypatch.setattr(cli.ClaudeAdapter, "is_halted", lambda _self, _cwd, _sid: False)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    rc = cli.main(["switch-account", "work", "-s", SID, "-N"])
    err = capsys.readouterr().err
    assert (rc, len(calls)) == ((0, 1) if tail_is_prompt else (1, 0)), err
    if not tail_is_prompt:
        assert "mid-turn" in err


def test_switch_account_now_cancel_prompt_exits_nonzero_after_spawning(
    two_accounts: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-N -c`` spawns the relauncher, then exits 1 on purpose so Claude Code cancels the
    prompt — the status goes to stderr, where the cancelled-prompt notice shows it."""
    private, _work = two_accounts
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ITERM_SESSION_ID", ITERM)
    with Store() as store:
        store.ensure(SID, cwd=str(repo))
        store.update_fields(SID, iterm_session_id=ITERM)
    _prepare_arm(monkeypatch, two_accounts, live=_live(str(repo), private))
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    rc = cli.main(["switch-account", "work", "-s", SID, "-N", "-c"])
    err = capsys.readouterr().err
    assert rc == 1, err
    assert len(calls) == 1 and calls[0][0] == "switch-now", err
    assert "relaunching now" in err and "cancelled on purpose" in err
