"""SessionStart auto-accept of the inbound handoff transfer (tp#1681 L18).

The successor's ``SessionStart`` hook finds its single inbound ``prepared``/``opened``
transfer and runs the existing ``accept`` before the first request: same caller and
account checks, same hooks, one bounded attempt, a note of at most 200 bytes. Anything
else (no transfer, several, an unreadable row, a refusal) leaves the transfer as it is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import test_continue_session as tcs

# pylint: disable=unused-import  # fixtures re-used by name from test_continue_session
from test_continue_session import _fixture_session as fixture_session  # noqa: F401
from test_continue_session import _harness as _harness_fixture  # noqa: F401

# pylint: enable=unused-import
from command_center import accounts, cli, config, continue_session, hooks
from command_center.store import Store

NOTE_CAP = 200


def _start_hook(sid: str, capsys: pytest.CaptureFixture[str], source: str = "startup") -> str:
    """Run the SessionStart hook; its ONE JSON document's ``additionalContext``."""
    capsys.readouterr()
    assert hooks.handle_session_start({"session_id": sid, "cwd": "/repo", "source": source}) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out  # exactly one JSON document, nothing leaked from accept
    return str(json.loads(lines[0])["hookSpecificOutput"]["additionalContext"])


def _note(context: str) -> str:
    """The auto-accept note: the one paragraph that names a handoff, else ``""`` (an
    account-switch warning may precede it)."""
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


def test_session_start_accept_accepts_the_single_inbound_transfer(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    note = _note(_start_hook(target, capsys))
    assert note == continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid)
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


def test_session_start_accept_none_is_silent(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del harness
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    _hooks(monkeypatch)
    assert _note(_start_hook("no-transfer-here", capsys)) == ""


def test_session_start_accept_wrong_account_leaves_it_opened(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    monkeypatch.setattr(accounts, "env_config_dir", lambda: str(harness.work))
    _hooks(monkeypatch)
    note = _note(_start_hook(target, capsys))
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


def test_session_start_accept_multiple_rows_accept_none(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _insert_row("tsecond000001", "other-source", target, str(harness.private))
    _hooks(monkeypatch)
    note = _note(_start_hook(target, capsys))
    assert "none accepted (auto)" in note
    assert note.endswith(f"run: ccc continue-session status -s {target}")
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_session_start_accept_corrupt_row_accepts_none(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _insert_row("tcorrupt00001", "other-source", target, "")  # no target account recorded
    _hooks(monkeypatch)
    note = _note(_start_hook(target, capsys))
    assert "unreadable" in note and "none accepted (auto)" in note
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_session_start_accept_retry_after_a_failing_hook(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One bounded attempt (no in-hook retry wait); the transfer stays opened, and the
    recovery command — or the next start — accepts it."""
    tid, target = _successor(harness, monkeypatch)
    hook, counter = tcs._counting_hook(tmp_path, "flaky.sh", 1)  # pylint: disable=W0212
    _hooks(monkeypatch, [hook])
    clock = harness.clock
    note = _note(_start_hook(target, capsys))
    assert note.startswith(f"handoff {tid} NOT accepted (auto)")
    assert note.endswith(f"run: ccc continue-session accept {tid}")
    assert len(note.encode()) <= NOTE_CAP
    assert tcs._runs(counter) == 1 and harness.clock == clock  # pylint: disable=W0212
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access
    assert _note(_start_hook(target, capsys, "resume")) == (
        continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid)
    )
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


def test_session_start_accept_already_accepted_is_silent(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manual accept first: the start afterwards runs no hook and prints no note."""
    tid, target = _successor(harness, monkeypatch)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [tcs._ok_hook(tmp_path, log)])  # pylint: disable=protected-access
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", target)
    assert cli.main(["continue-session", "accept", tid]) == 0
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert log.read_text(encoding="utf-8").count("ran") == 1
    assert _note(_start_hook(target, capsys)) == ""
    assert log.read_text(encoding="utf-8").count("ran") == 1


def test_session_start_accept_resume_after_auto_accept_is_a_no_op(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid, target = _successor(harness, monkeypatch)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [tcs._ok_hook(tmp_path, log)])  # pylint: disable=protected-access
    assert _note(_start_hook(target, capsys)) != ""
    assert _note(_start_hook(target, capsys, "resume")) == ""
    assert _note(_start_hook(target, capsys, "startup")) == ""
    assert log.read_text(encoding="utf-8").count("ran") == 1
    assert tcs._state(tid) == "finalized"  # pylint: disable=protected-access


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_session_start_accept_skips_compact_and_clear(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch)
    assert _note(_start_hook(target, capsys, source)) == ""
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_session_start_accept_kill_switch_and_internal(
    harness: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid, target = _successor(harness, monkeypatch)
    _hooks(monkeypatch, auto=False)
    assert _note(_start_hook(target, capsys)) == ""
    _hooks(monkeypatch)
    monkeypatch.setenv("CCC_INTERNAL", "1")
    assert _note(_start_hook(target, capsys)) == ""
    assert tcs._state(tid) == "opened"  # pylint: disable=protected-access


def test_session_start_accept_note_lists_hook_next_steps_within_the_cap(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid, target = _successor(harness, monkeypatch)
    steps = json.dumps({"ok": True, "next_steps": ["tp#1 now yours", "x" * 400]})
    hook = tcs._script(tmp_path, "steps.sh", f"echo '{steps}'\n")  # pylint: disable=W0212
    _hooks(monkeypatch, [hook])
    note = _note(_start_hook(target, capsys))
    assert note.startswith(continue_session.AUTO_ACCEPT_NOTE.format(transfer=tid))
    assert "next: tp#1 now yours" in note
    assert len(note.encode()) <= NOTE_CAP


def test_session_start_accept_preamble_names_the_note() -> None:
    transfer = type(
        "T", (), {"source_session": "s", "transfer_id": "t1", "handoff_path": "/h.md"}
    )()
    flat = " ".join(continue_session.preamble(transfer, "private", "").split())
    head = continue_session.AUTO_ACCEPT_HEAD.format(transfer="t1")
    assert f"unless a SessionStart note reads `{head}`" in flat
    assert continue_session.AUTO_ACCEPT_NOTE.format(transfer="t1").startswith(head)
    assert "ccc continue-session accept t1" in flat


def test_session_start_accept_defaults_on() -> None:
    assert config.DEFAULTS["handoff_auto_accept"] is True
    assert config.Config().handoff_auto_accept is True
