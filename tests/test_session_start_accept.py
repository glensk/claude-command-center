"""First-prompt auto-accept of the inbound handoff transfer (tp#1681 L18, tp#2326 D1).

The successor's ``UserPromptSubmit`` hook, on its FIRST SUBMITTED HANDOFF PROMPT (the
preamble's head within the first ``HANDOFF_HEAD_WINDOW`` characters, naming
``transfer <id>``), finds its single inbound ``prepared``/``opened`` transfer and runs the
existing ``accept``: same caller and account checks, same hooks, one bounded attempt, a
note of at most 200 bytes, first in the emitted context. Anything else (no transfer,
several, an unreadable row, a refusal, a prompt that names another transfer) leaves the
transfer as it is. ``SessionStart`` accepts nothing any more.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import test_continue_session as tcs

# pylint: disable=unused-import  # fixtures re-used by name from test_continue_session
from test_continue_session import _fixture_session as fixture_session  # noqa: F401
from test_continue_session import _harness as _harness_fixture  # noqa: F401

# pylint: enable=unused-import
import command_center
from command_center import accounts, cli, config, continue_session, hooks
from command_center.store import Store

NOTE_CAP = 200
ORCHESTRATION = (
    "[orchestration] You are the overseer running as opus-5.5. Delegate implementation "
    "work to subagents via the Agent tool with model 'sonnet'; keep planning, review, "
    "verification and integration yourself. "
)


def _launch_prompt(target: str) -> str:
    """The successor's stored launch prompt: the preamble plus the handoff text."""
    with Store() as store:
        job = store.get(target)
    assert job is not None and job.prompt
    return job.prompt


def _emitted(capsys: pytest.CaptureFixture[str]) -> str:
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) <= 1, out  # at most one JSON document, nothing leaked from accept
    if not lines:
        return ""
    return str(json.loads(lines[0])["hookSpecificOutput"].get("additionalContext", ""))


def _prompt_hook(sid: str, capsys: pytest.CaptureFixture[str], prompt: str | None = None) -> str:
    """Run the UserPromptSubmit hook (default prompt: the launch prompt); its context."""
    capsys.readouterr()
    text = _launch_prompt(sid) if prompt is None else prompt
    payload = {"session_id": sid, "cwd": "/repo", "prompt": text}
    assert hooks.handle_user_prompt(payload) == 0
    return _emitted(capsys)


def _start_hook(sid: str, capsys: pytest.CaptureFixture[str], source: str = "startup") -> str:
    """Run the SessionStart hook; its ONE JSON document's ``additionalContext``."""
    capsys.readouterr()
    assert hooks.handle_session_start({"session_id": sid, "cwd": "/repo", "source": source}) == 0
    return _emitted(capsys)


def _note(context: str) -> str:
    """The auto-accept note: the one paragraph that names a handoff, else ``""``."""
    notes = [part for part in context.split("\n\n") if part.startswith("handoff")]
    assert len(notes) <= 1, context
    return notes[0] if notes else ""


def _successor(harness: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """An opened transfer and its successor session, which bills the target account."""
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    tid = tcs._opened_transfer(harness)  # pylint: disable=protected-access
    target = tcs._as_successor(monkeypatch, harness, tid)  # pylint: disable=protected-access
    # A hook process may lack the session env: the payload id is the caller.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return tid, target


def _hooks(monkeypatch: pytest.MonkeyPatch, *argvs: list[str], auto: bool = True) -> None:
    cfg = config.Config(handoff_hooks=[list(a) for a in argvs], handoff_auto_accept=auto)
    monkeypatch.setattr(config, "load_config", lambda: cfg)


def test_prompt_accept_accepts_the_single_inbound_transfer(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    note = _note(_prompt_hook(target, capsys))
    assert note == continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid)
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


def test_prompt_accept_none_is_silent(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del harness
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    _hooks(monkeypatch)
    prompt = hooks.HANDOFF_PROMPT_HEAD + "src-1 (account private), transfer t-none."
    assert _note(_prompt_hook("no-transfer-here", capsys, prompt)) == ""


def test_prompt_accept_wrong_account_leaves_it_opened(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    monkeypatch.setattr(accounts, "env_config_dir", lambda: str(harness.work))
    _hooks(monkeypatch)
    note = _note(_prompt_hook(target, capsys))
    assert note.startswith(f"handoff {tid} NOT accepted (auto)")
    assert note.endswith(f"run: ccc continue-session accept {tid}")
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def _insert_row(transfer_id: str, source: str, target: str, config_dir: str) -> None:
    with Store() as store:
        store.conn.execute(
            "INSERT INTO transfers (transfer_id, source_session, target_session, "
            "target_config_dir, state) VALUES (?, ?, ?, ?, 'opened')",
            (transfer_id, source, target, config_dir),
        )
        store.conn.commit()


def test_prompt_accept_multiple_rows_accept_none(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _insert_row("tsecond000001", "other-source", target, str(harness.private))
    _hooks(monkeypatch)
    note = _note(_prompt_hook(target, capsys))
    assert "none accepted (auto)" in note
    assert note.endswith(f"run: ccc continue-session status -s {target}")
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_prompt_accept_corrupt_row_accepts_none(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _insert_row("tcorrupt00001", "other-source", target, "")  # no target account recorded
    _hooks(monkeypatch)
    note = _note(_prompt_hook(target, capsys))
    assert "unreadable" in note and "none accepted (auto)" in note
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_prompt_accept_failing_hook_then_manual_accept(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One bounded attempt (no in-hook retry wait); the transfer stays opened, and the
    recovery command it names accepts it."""
    tid, target = _successor(harness, monkeypatch)
    hook, counter = tcs._counting_hook(tmp_path, "flaky.sh", 1)  # pylint: disable=W0212
    _hooks(monkeypatch, [hook])
    clock = harness.clock
    note = _note(_prompt_hook(target, capsys))
    assert note.startswith(f"handoff {tid} NOT accepted (auto)")
    assert note.endswith(f"run: ccc continue-session accept {tid}")
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._runs(counter) == 1 and harness.clock == clock  # pylint: disable=W0212
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", target)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


def test_prompt_accept_already_accepted_is_silent(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manual accept first: the handoff prompt afterwards runs no hook and has no note."""
    tid, target = _successor(harness, monkeypatch)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [tcs._ok_hook(tmp_path, log)])  # pylint: disable=protected-access
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", target)
    assert cli.main(["continue-session", "accept", tid]) == 0
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert log.read_text(encoding="utf-8").count("ran") == 1
    assert _note(_prompt_hook(target, capsys)) == ""
    assert log.read_text(encoding="utf-8").count("ran") == 1


def test_prompt_accept_second_identical_prompt_is_a_no_op(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid, target = _successor(harness, monkeypatch)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [tcs._ok_hook(tmp_path, log)])  # pylint: disable=protected-access
    assert _note(_prompt_hook(target, capsys)) != ""
    assert _note(_prompt_hook(target, capsys)) == ""
    assert _note(_start_hook(target, capsys, "resume")) == ""
    assert log.read_text(encoding="utf-8").count("ran") == 1
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


def test_prompt_accept_kill_switch_and_internal(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch, auto=False)
    assert _note(_prompt_hook(target, capsys)) == ""
    _hooks(monkeypatch)
    monkeypatch.setenv("CCC_INTERNAL", "1")
    assert _note(_prompt_hook(target, capsys)) == ""
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_prompt_accept_note_lists_hook_next_steps_within_the_cap(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid, target = _successor(harness, monkeypatch)
    steps = json.dumps({"ok": True, "next_steps": ["tp#1 now yours", "x" * 400]})
    hook = tcs._script(tmp_path, "steps.sh", f"echo '{steps}'\n")  # pylint: disable=W0212
    _hooks(monkeypatch, [hook])
    note = _note(_prompt_hook(target, capsys))
    assert note.startswith(continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid))
    assert "next: tp#1 now yours" in note
    assert len(note.encode()) <= NOTE_CAP


@pytest.mark.parametrize("source", ["startup", "resume", "compact", "clear"])
def test_session_start_with_an_inbound_transfer_accepts_nothing(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    """tp#2326 D1: SessionStart moves nothing for a handoff: no hook, no closer, opened."""
    tid, target = _successor(harness, monkeypatch)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [tcs._ok_hook(tmp_path, log)])  # pylint: disable=protected-access
    spawned = len(harness.spawned)
    assert _note(_start_hook(target, capsys, source)) == ""
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access
    assert not log.exists()
    assert len(harness.spawned) == spawned


def test_ordinary_prompt_queries_no_transfer_and_imports_no_continue_session(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    queried: list[str] = []

    def _spy(_self: Store, sid: str) -> tuple[list[Any], int]:
        queried.append(sid)
        return [], 0

    monkeypatch.setattr(Store, "inbound_transfers", _spy)
    monkeypatch.delitem(sys.modules, "command_center.continue_session")
    monkeypatch.delattr(command_center, "continue_session")
    _prompt_hook(target, capsys, "please run the tests")
    assert not queried
    assert "command_center.continue_session" not in sys.modules
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_prompt_naming_another_transfer_accepts_nothing_and_says_so(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    other = _launch_prompt(target).replace(f"transfer {tid}", "transfer tother0000001")
    other = other.replace(tid, "tother0000001")
    note = _note(_prompt_hook(target, capsys, other))
    assert note == (
        "handoff: this prompt names no inbound transfer; "
        f"run: ccc continue-session status -s {target}"
    )
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access
    # a transfer id that merely starts with the inbound one is another transfer
    longer = _launch_prompt(target).replace(f"transfer {tid}", f"transfer {tid}9")
    longer = longer.replace(f"accept {tid}", f"accept {tid}9")
    assert _note(_prompt_hook(target, capsys, longer)).startswith("handoff: this prompt names")
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_prompt_with_the_orchestration_prefix_still_accepts(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``start-job`` prepends ``[orchestration] …`` when executor != overseer."""
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    note = _note(_prompt_hook(target, capsys, ORCHESTRATION + _launch_prompt(target)))
    assert note == continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid)
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


def test_prompt_head_past_the_window_does_not_accept(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    pad = "x" * (hooks.HANDOFF_HEAD_WINDOW - len(hooks.HANDOFF_PROMPT_HEAD) + 1)
    assert _note(_prompt_hook(target, capsys, pad + _launch_prompt(target))) == ""
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access
    edge = "x" * (hooks.HANDOFF_HEAD_WINDOW - len(hooks.HANDOFF_PROMPT_HEAD))
    note = _note(_prompt_hook(target, capsys, edge + _launch_prompt(target)))
    assert note == continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid)


def test_prompt_accept_note_comes_first_and_nudges_see_the_reconciled_aim(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    with Store() as store:
        store.update_fields(target, aim="")  # the reconcile gives it back on accept
    context = _prompt_hook(target, capsys)
    first = context.split("\n\n", maxsplit=1)[0]
    assert first == continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid)
    assert "still has no done-condition" not in context  # no no-AIM nudge
    with Store() as store:
        successor = store.get(target)
    assert successor is not None and successor.aim == "tp#1 shipped"


def test_handed_off_source_refuses_the_handoff_prompt_first(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    with Store() as store:
        store.update_fields(target, handed_off_to="some-later-successor")
    capsys.readouterr()
    payload = {"session_id": target, "cwd": "/repo", "prompt": _launch_prompt(target)}
    assert hooks.handle_user_prompt(payload) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "handed off" in out["reason"]
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_manual_accept_as_finalizes_at_once(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    spawned = len(harness.spawned)
    with Store() as store:
        assert continue_session.accept_as(store, tid, target, False, config.load_config()) == 0
    capsys.readouterr()
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access
    closers = harness.spawned[spawned:]
    assert any("close-now" in args and "--handoff-transfer" in args for args in closers)


def test_prompt_accept_preamble_names_the_note() -> None:
    transfer = type(
        "T", (), {"source_session": "s", "transfer_id": "t1", "handoff_path": "/h.md"}
    )()
    flat = " ".join(continue_session.preamble(transfer, "private", "").split())
    head = continue_session.AUTO_ACCEPT_HEAD.format(transfer="t1")
    assert f"unless a ccc hook note on this prompt reads `{head}`" in flat
    assert "SessionStart" not in flat
    assert continue_session.AUTO_ACCEPT_NOTE.format(transfer="t1").startswith(head)
    assert "ccc continue-session accept t1" in flat


def test_prompt_accept_defaults_on() -> None:
    assert config.DEFAULTS["handoff_auto_accept"] is True
    assert config.Config().handoff_auto_accept is True
