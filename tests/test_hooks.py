"""Tests for the Claude Code hook handlers (isolated via a temp CLAUDE_HOME)."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import hooks
from command_center.models import Status
from command_center.store import Store


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_AIM", raising=False)
    return tmp_path


def test_session_start_asks_when_no_aim(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "additionalContext" in out
    assert "done" in out.lower()
    assert Store().get("s1") is not None  # row was created


def test_session_start_picks_up_env_aim(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_SESSION_AIM", "no water hammer")
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None
    assert got.aim == "no water hammer"
    # The hook routes through set_aim, so the AIM is scored on arrival (never left
    # at the -1 sentinel that bypassed the vague-AIM machinery).
    assert got.aim_score >= 0


def test_session_start_grounds_when_aim_set(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="ship it", next_step="write tests")
    capsys.readouterr()  # drain
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "ship it" in out
    assert "write tests" in out


def test_user_prompt_nudges_to_adapt_stale_subgoals(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "ship X: pytest -q green and PR #5 merged")  # AIM rev 1
    store.set_subgoals("s1", ["write test_x", "open PR #5"], source="agent")  # adaptive @ rev 1
    store.set_aim("s1", "ship X and deploy: prod smoke test passes")  # AIM rev 2 -> stale
    store.update_fields("s1", aim_score=80)  # force concrete so the sharpen branch is skipped
    capsys.readouterr()  # drain
    hooks.handle_user_prompt({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "additionalContext" in out
    assert "subgoals --session" in out and "--merge" in out  # nudged to re-align, preserving ticks


def test_user_prompt_nudges_on_unresolved_drift(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "concrete aim: pytest -q green and PR merged")
    store.update_fields("s1", aim_score=80)  # concrete -> skip the sharpen branch
    store.set_drift("s1", "high", "coverage dropped: tests removed")
    capsys.readouterr()  # drain
    hooks.handle_user_prompt({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "DRIFT" in out and "ack-drift" in out  # the self-correct nudge fired


def test_user_prompt_nudges_to_tick_partial_checklist(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "concrete aim: pytest -q green and bug fixed")
    store.set_subgoals("s1", ["add tiebreaker", "tests pass", "docs updated"], source="auto")
    store.update_fields("s1", aim_score=80)  # concrete -> skip sharpen branch
    subs = store.list_subgoals("s1")
    store.set_subgoal_checked(subs[1].id, True)  # 1/3 -> partial, the trigger
    capsys.readouterr()  # drain
    hooks.handle_user_prompt({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "additionalContext" in out
    assert "ccc check --session" in out
    assert "add tiebreaker" in out and "docs updated" in out  # unticked items listed
    assert "tests pass" not in out  # the already-ticked item is not listed


def test_user_prompt_no_tick_nudge_when_complete(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.set_aim("s1", "concrete aim: pytest -q green")
    store.set_subgoals("s1", ["a", "b"], source="auto")
    store.update_fields("s1", aim_score=80)
    for sub in store.list_subgoals("s1"):
        store.set_subgoal_checked(sub.id, True)  # 2/2 complete
    capsys.readouterr()  # drain
    hooks.handle_user_prompt({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "ccc check --session" not in out  # nothing to nudge at 100%


def test_stop_flags_summary(home: Path) -> None:
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None
    assert got.needs_summary is True
    assert got.last_response_at > 0


def test_session_end_parks(home: Path) -> None:
    hooks.handle_session_end({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None
    assert got.status == Status.PARKED.value


def test_dispatch_never_raises_on_garbage(home: Path) -> None:
    # Unknown event and missing id must both be no-ops returning 0.
    assert hooks.dispatch("does-not-exist") == 0
    assert hooks.handle_stop({}) == 0


def test_dispatch_skips_headless_sdk(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A headless `claude -p` run (entrypoint sdk-cli) inherits the launching
    # session's CLAUDE_SESSION_AIM; dispatch must be a no-op so it never leaks a
    # row stamped with that AIM (the snezana-style duplicates).
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    monkeypatch.setenv("CLAUDE_SESSION_AIM", "inherited aim")
    monkeypatch.setattr(hooks, "_read_payload", lambda: {"session_id": "headless1", "cwd": "/repo"})
    assert hooks.dispatch("session-start") == 0
    assert capsys.readouterr().out == ""  # no additionalContext emitted
    assert Store().get("headless1") is None  # no row created

    # The real interactive entrypoint still runs handlers.
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(hooks, "_read_payload", lambda: {"session_id": "real1", "cwd": "/repo"})
    assert hooks.dispatch("session-start") == 0
    assert Store().get("real1") is not None


def test_iterm_session_captured(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t1p0:ABC-123")
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None
    assert got.iterm_session_id == "w0t1p0:ABC-123"


def test_iterm_session_self_heals_on_any_hook_event(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that missed its SessionStart stamp (transient store error, or first created
    by another handler) is re-stamped by the NEXT hook event — set-once used to leave it
    permanently un-locatable (f+j could neither highlight nor focus the session)."""
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None and got.iterm_session_id is None
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t8p0:HEAL-456")
    hooks.handle_post_tool_use({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None
    assert got.iterm_session_id == "w0t8p0:HEAL-456"


def test_iterm_session_not_cleared_when_env_absent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless/daemon-context hook run (no $ITERM_SESSION_ID) must never CLEAR a
    previously stamped tab id — absent env means "unknown here", not "no tab"."""
    monkeypatch.setenv("ITERM_SESSION_ID", "w0t1p0:KEEP-789")
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    hooks.handle_post_tool_use({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None
    assert got.iterm_session_id == "w0t1p0:KEEP-789"


def test_post_tool_use_captures_payload_todos(home: Path) -> None:
    from command_center.models import loads_todos  # pylint: disable=import-outside-toplevel

    payload = {
        "session_id": "s1",
        "cwd": "/repo",
        "tool_input": {
            "todos": [
                {"content": "first", "status": "completed", "activeForm": "doing first"},
                {"content": "second", "status": "in_progress", "activeForm": "doing second"},
            ]
        },
    }
    hooks.handle_post_tool_use(payload)
    got = Store().get("s1")
    assert got is not None
    assert loads_todos(got.todos) == [("completed", "first"), ("in_progress", "second")]
    assert got.todos_updated_at > 0


def test_post_tool_use_falls_back_to_disk(home: Path) -> None:
    import json  # pylint: disable=import-outside-toplevel

    from command_center.models import loads_todos  # pylint: disable=import-outside-toplevel

    task_dir = home / "tasks" / "s2"
    task_dir.mkdir(parents=True)
    (task_dir / "1.json").write_text(
        json.dumps({"subject": "disk task", "status": "pending"}), encoding="utf-8"
    )
    # No full ``tool_input.todos`` (e.g. a single-task TaskUpdate) -> read the disk list.
    hooks.handle_post_tool_use({"session_id": "s2", "cwd": "/repo", "tool_input": {"task_id": "1"}})
    got = Store().get("s2")
    assert got is not None
    assert loads_todos(got.todos) == [("pending", "disk task")]


def _set_cfg(**kw: object) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    cfg = config.load_config()
    for key, value in kw.items():
        setattr(cfg, key, value)
    config.save_config(cfg)


def _recorder(sink: list[list[str]]):
    """A stand-in for spawn.spawn_ccc that records calls instead of forking."""

    def record(args: list[str]) -> bool:
        sink.append(args)
        return True

    return record


def test_stop_spawns_grader_when_enabled(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    _set_cfg(grade_on_turn=True, grade_debounce_sec=30)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})  # fresh => last_progress_at 0
    assert calls == [["autoprogress", "--session", "s1"]]


def test_stop_debounce_skips_recent(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.models import now_ms  # pylint: disable=import-outside-toplevel

    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    _set_cfg(grade_on_turn=True, grade_debounce_sec=600)
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", last_progress_at=now_ms())  # graded just now
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert calls == []  # within debounce window


def test_stop_grader_disabled(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    _set_cfg(grade_on_turn=False)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert calls == []


def test_stop_grader_skipped_when_ccc_internal(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cfg(grade_on_turn=True)
    monkeypatch.setenv("CCC_INTERNAL", "1")  # recursion guard: we're inside our own claude -p
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert calls == []


def _ready(session_id: str, aim: str, score: int) -> None:
    store = Store()
    store.ensure(session_id, cwd="/repo")
    store.set_aim(session_id, aim)
    store.update_fields(session_id, aim_score=score)


def test_stop_spawns_assessor_for_concrete_aim(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    _set_cfg(grade_on_turn=True, assess_aim_on_turn=True, aim_score_threshold=50)
    _ready("s1", "all tests in tests/ pass and PR #42 merged", 80)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert ["assess-aim", "--session", "s1"] in calls
    assert ["autoprogress", "--session", "s1"] in calls


def test_stop_no_assessor_when_aim_vague(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    _set_cfg(grade_on_turn=True, assess_aim_on_turn=True, aim_score_threshold=50)
    _ready("s1", "improve things", 20)  # below threshold => not eligible
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert ["assess-aim", "--session", "s1"] not in calls


def test_stop_assessor_disabled(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    _set_cfg(grade_on_turn=False, assess_aim_on_turn=False)
    _ready("s1", "all tests pass and PR #42 merged", 80)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert calls == []  # both grader and assessor off


def test_stop_assessor_skipped_when_ccc_internal(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cfg(grade_on_turn=False, assess_aim_on_turn=True)
    monkeypatch.setenv("CCC_INTERNAL", "1")  # recursion guard
    _ready("s1", "all tests pass and PR #42 merged", 80)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_stop({"session_id": "s1", "cwd": "/repo"})
    assert calls == []


def test_session_start_sharpens_low_score_aim(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="improve things", aim_score=20)  # < default threshold 50
    capsys.readouterr()
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "score-aim --dry-run" in out  # points the agent at the independent checker
    assert "20/100" in out  # tells the agent the current score


def test_session_start_no_sharpen_when_specific(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="all tests in tests/ pass", aim_score=85)
    capsys.readouterr()
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    assert "score-aim --dry-run" not in capsys.readouterr().out


def test_user_prompt_sharpen_mutually_exclusive(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_cfg(sharpen_every_n_turns=1)
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="improve things", aim_score=20)
    capsys.readouterr()
    hooks.handle_user_prompt({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "score-aim --dry-run" in out  # vague-aim sharpen nudge fired
    assert "no done-condition" not in out  # not the no-aim nag


def test_nag_throttle(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    cfg = config.load_config()
    cfg.nag_every_n_turns = 2
    config.save_config(cfg)

    payload = {"session_id": "s1", "cwd": "/repo"}
    nagged: list[bool] = []
    for _ in range(4):
        hooks.handle_user_prompt(payload)
        nagged.append("additionalContext" in capsys.readouterr().out)
    # Counts 1..4; nag when (count-1) % 2 == 0 -> turns 1 and 3.
    assert nagged == [True, False, True, False]


def test_sharpen_nudge_is_auto_apply_with_revert(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The vague-AIM nudge is context-grounded, auto-applies, and keeps the goal intact."""
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="improve things", aim_score=20, aim_score_reason="no check")
    capsys.readouterr()
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    out = capsys.readouterr().out
    assert "set-aim" in out  # the agent applies the rewrite itself
    assert "revert" in out.lower()  # and tells the user how to revert
    assert "no check" in out  # surfaces the independent checker's reason
    assert "intact" in out  # intent-preservation guardrail (sharpen specificity only)
    assert "files you've edited" in out  # grounded in the session's own context


def test_user_prompt_clears_aim_transition(home: Path) -> None:
    """A new turn clears the `aim_prev` marker so the status-line transition is turn-scoped."""
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", aim="new aim", aim_prev="old aim", aim_score=85)
    hooks.handle_user_prompt({"session_id": "s1", "cwd": "/repo"})
    refreshed = store.get("s1")
    assert refreshed is not None and refreshed.aim_prev is None


def test_release_locks_claims_armed_close_and_spawns_once(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An armed close request is claimed once → one detached close-now spawn with the fresh env
    iterm id; a second invocation claims nothing and spawns nothing."""
    from command_center.models import now_ms

    monkeypatch.setenv("ITERM_SESSION_ID", "w0t1p0:LIVE-UUID")
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", close_requested_at=now_ms())  # mark-done --close armed it
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))

    hooks.handle_release_locks({"session_id": "s1", "cwd": "/repo"})
    assert calls == [["close-now", "--session", "s1", "--iterm", "w0t1p0:LIVE-UUID"]]
    assert store.get("s1").close_requested_at == 0  # type: ignore[union-attr]  # claimed & cleared

    # The one-shot request cannot re-fire on a later Stop.
    hooks.handle_release_locks({"session_id": "s1", "cwd": "/repo"})
    assert len(calls) == 1


def test_release_locks_unarmed_never_spawns(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with no armed close request never spawns the closer."""
    store = Store()
    store.ensure("s1", cwd="/repo")  # close_requested_at defaults to 0 (unarmed)
    calls: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", _recorder(calls))
    hooks.handle_release_locks({"session_id": "s1", "cwd": "/repo"})
    assert calls == []


# --------------------------------------------------------------------------- #
# SubagentStart/SubagentStop — the in-process subagent counter switch-account reads
# --------------------------------------------------------------------------- #
def test_subagent_start_stop_counts_in_flight_subagents_and_floors_at_zero(home: Path) -> None:
    """Each start increments, each stop decrements, and a stop without a start stays at 0.

    The counter is the only evidence of an IN-PROCESS Agent-tool subagent (no child
    process, and its transcript record can lag), so it must never drift: two parallel
    subagents read 2, and an extra SubagentStop (one whose start predates the column)
    must not push it negative and mask a real one.
    """
    payload = {"session_id": "s1", "cwd": "/repo"}
    store = Store()
    hooks.handle_subagent_start(payload)
    assert store.active_subagents("s1") == 1
    hooks.handle_subagent_start(payload)
    assert store.active_subagents("s1") == 2
    hooks.handle_subagent_stop(payload)
    assert store.active_subagents("s1") == 1
    hooks.handle_subagent_stop(payload)
    assert store.active_subagents("s1") == 0
    hooks.handle_subagent_stop(payload)  # unpaired stop
    assert store.active_subagents("s1") == 0


def test_subagent_start_records_parent_activity_and_logs_the_event(home: Path) -> None:
    """The parent's last_response_at advances and the event log names the start."""
    hooks.handle_subagent_start({"session_id": "s1", "cwd": "/repo"})
    got = Store().get("s1")
    assert got is not None and got.last_response_at > 0
    log = (home / "command-center" / "events.log").read_text(encoding="utf-8")
    assert "subagent-start" in log


def test_session_start_resets_a_stranded_subagent_count(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that died mid-subagent never fired its stop — the new start clears it."""
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.bump_subagents("s1", 3)
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    assert store.active_subagents("s1") == 0


def test_session_end_resets_the_subagent_count(home: Path) -> None:
    """The process is going: no subagent of it survives, so the counter cannot linger."""
    store = Store()
    store.ensure("s1", cwd="/repo")
    store.bump_subagents("s1", 2)
    hooks.handle_session_end({"session_id": "s1", "cwd": "/repo"})
    assert store.active_subagents("s1") == 0
