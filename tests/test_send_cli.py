"""``ccc send -s ID -j``: refusals, revalidation, correlation and the stdin-only contract."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

import pytest
from bridgestub import (
    SID,
    TAB,
    World,
    ask,
    enqueue,
    question,
    run_cli,
    task_notice,
    turn_end,
    user,
)

from command_center.adapters.claude_agents import AgentEntry
from command_center.store import Store


def _send(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> tuple[int, dict[str, Any] | None, str]:
    return run_cli(monkeypatch, capsys, world, ["send", "-s", SID, "-j"], stdin=text)


def _deliveries(world: World) -> list[Any]:
    with Store(world.db) as store:
        return store.deliveries_for(SID)


def _idle_world(tmp_path: Path, **kw: Any) -> World:
    world = World(tmp_path, **kw)
    world.write(user("earlier"), *turn_end())
    return world


def test_idle_send_expects_and_matches_a_user_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    size_before = world.transcript.stat().st_size
    world.on_text = lambda w, text: w.write(task_notice(), user(text))
    code, env, _err = _send(world, monkeypatch, capsys, "print hello\nand  more\t!")
    assert code == 0 and env is not None and env["ok"] is True
    data = env["data"]
    assert data["outcome"] == "accepted" and data["channel"] == "python-api"
    assert world.texts == ["print hello\nand  more\t!"]
    (d,) = _deliveries(world)
    assert d.delivery_id == data["delivery_id"]
    assert d.state == "accepted" and d.matched_kind == "user" and d.expected == "user"
    assert d.anchor_offset == size_before and d.matched_offset >= size_before
    assert d.transcript_inode == world.transcript.stat().st_ino


def test_busy_send_expects_an_enqueue_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, status="busy")
    world.write(user("long job"))
    world.on_text = lambda w, text: w.write(enqueue(text))
    code, env, _err = _send(world, monkeypatch, capsys, "and then this")
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    (d,) = _deliveries(world)
    assert d.expected == "queue-operation/enqueue"
    assert d.matched_kind == "queue-operation/enqueue"


def test_shell_status_is_busy_and_expects_an_enqueue_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Registry ``shell`` (a shell tool running) is mid-turn: queue, never refuse."""
    world = World(tmp_path, status="shell")
    world.write(user("run the suite"))
    world.on_text = lambda w, text: w.write(enqueue(text))
    code, env, _err = _send(world, monkeypatch, capsys, "after that, lint")
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    (d,) = _deliveries(world)
    assert d.expected == "queue-operation/enqueue" and d.status_at_send == "busy"
    assert d.matched_kind == "queue-operation/enqueue"


def test_background_job_without_a_worker_is_refused_as_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job only ``claude agents`` lists (no registry entry) is background, not 'not running'."""
    world = _idle_world(tmp_path, registered=False)
    world.agents = [AgentEntry(config_dir=str(world.config_dir), session_id=SID, kind="background")]
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["error"]["code"] == "background"
    assert not world.texts and _deliveries(world) == []


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        ("waiting", "interactive", "waiting"),
        ("blocked", "interactive", "blocked"),
        ("idle", "bg", "background"),
        ("shell", "bg", "background"),
        ("weird", "interactive", "unknown_status"),
    ],
)
def test_refused_states_type_nothing(  # pylint: disable=too-many-positional-arguments
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    kind: str,
    code: str,
) -> None:
    world = _idle_world(tmp_path, status=status, kind=kind)
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["ok"] is False
    assert env["error"]["code"] == code and env["data"] is None
    assert not world.texts and _deliveries(world) == []


@pytest.mark.parametrize("status", ["waiting", "idle"])
def test_pending_picker_refuses_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    world = World(tmp_path, status=status)
    world.write(user("go"), ask("toolu_1", [question("Colour?", ["red", "blue"])]))
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["error"]["code"] == "picker_pending"
    assert not world.texts


def test_not_running_session_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, registered=False)
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["error"]["code"] == "not_running"
    world = _idle_world(tmp_path / "b", alive=False)
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["error"]["code"] == "not_running"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"tab_tty": "/dev/ttys099"}, "stale_tab"),
        ({"job_pid": 9999}, "foreground_not_claude"),
        ({"tab_reachable": False}, "iterm_unreachable"),
    ],
)
def test_stale_or_foreign_tab_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    code: str,
) -> None:
    world = _idle_world(tmp_path, **change)
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["error"]["code"] == code
    assert not world.texts


def test_child_of_claude_in_the_foreground_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from bridgestub import PID

    world = _idle_world(tmp_path, job_pid=PID + 1)
    world.on_text = lambda w, text: w.write(user(text))
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 0 and env is not None and env["data"]["outcome"] == "accepted"


@pytest.mark.parametrize("stored", ["", "w9t9p9:DEAD-BEEF-0000"])
def test_missing_or_wrong_stored_tab_is_resolved_by_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stored: str,
) -> None:
    # S-IDENT: the ONE iTerm session on the registry pid's tty is the tab.
    world = _idle_world(tmp_path, other_panes=[("DEAD-BEEF-0000", "/dev/ttys077")])
    with Store(world.db) as store:
        store.update_fields(SID, iterm_session_id=stored)
    world.on_text = lambda w, text: w.write(user(text))
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert world.texts == ["hello"]
    (d,) = _deliveries(world)
    assert d.iterm_session_id.rsplit(":", maxsplit=1)[-1] == TAB.rsplit(":", maxsplit=1)[-1]


def test_no_tab_known_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, tab_listed=False)
    with Store(world.db) as store:
        store.update_fields(SID, iterm_session_id="")
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["error"]["code"] == "no_tab"
    assert not world.texts


def test_fresh_session_without_a_transcript_anchors_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # S-SEND: a fresh session writes its transcript only with the first prompt, at
    # <config>/projects/<cwd, non-alphanumerics → '-'>/<id>.jsonl.
    world = World(tmp_path)
    world.transcript.unlink()
    world.transcript.parent.rmdir()
    world.transcript_resolves_missing = True
    expected = world.config_dir / "projects" / "-repo" / f"{SID}.jsonl"
    assert expected == world.transcript

    def first_prompt(w: World, text: str) -> None:
        w.transcript.parent.mkdir(parents=True)
        w.write(user(text))

    world.on_text = first_prompt
    rc, env, _err = _send(world, monkeypatch, capsys, "hello fresh")
    assert rc == 0 and env is not None and env["data"]["outcome"] == "accepted"
    (d,) = _deliveries(world)
    assert d.transcript_path == str(expected) and d.anchor_offset == 0
    assert d.transcript_inode == world.transcript.stat().st_ino  # pinned on acceptance


def test_fresh_session_whose_transcript_never_appears_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.transcript.unlink()
    world.transcript_resolves_missing = True
    rc, env, _err = _send(world, monkeypatch, capsys, "hello fresh")
    assert rc == 1 and env is not None and env["error"]["code"] == "delivery_unknown"
    (d,) = _deliveries(world)
    assert d.transcript_inode == 0 and d.state == "unknown"


def test_project_dir_name_replaces_every_non_alphanumeric() -> None:
    from command_center.adapters import claude_bridge as cb

    assert cb.project_dir_name("/private/tmp/bridge-scratch") == "-private-tmp-bridge-scratch"
    assert cb.project_dir_name("/Users/a/my_repo.v2") == "-Users-a-my-repo-v2"
    path = cb.expected_transcript_path("/cfg", "/private/tmp/bridge-scratch", SID)
    assert path == Path("/cfg/projects/-private-tmp-bridge-scratch") / f"{SID}.jsonl"


def test_partial_paste_is_unknown_with_the_delivery_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    world.send_result = ("", "partial")
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["ok"] is False
    assert env["error"]["code"] == "delivery_unknown"
    assert env["data"]["outcome"] == "unknown" and env["data"]["delivery_id"]
    (d,) = _deliveries(world)
    assert d.state == "unknown" and d.reason == "partial_paste"
    assert world.mono >= 8.0  # waited the whole correlation window, never re-sent
    assert len(world.texts) == 1


def test_nothing_sent_is_failed_and_raises_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    world.send_result = ("", "none")
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None
    assert env["error"]["code"] == "delivery_failed" and env["data"]["outcome"] == "failed"
    (d,) = _deliveries(world)
    assert d.state == "failed" and d.outcome == "failed" and d.reason == "nothing_sent"
    with Store(world.db) as store:
        events = store.events_after(0)
    assert [e.kind for e in events] == ["delivery_failed"]


def test_no_matching_record_within_eight_seconds_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    world.on_text = lambda w, text: w.write(user("something else"), task_notice())
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    assert rc == 1 and env is not None and env["data"]["outcome"] == "unknown"
    (d,) = _deliveries(world)
    assert d.reason == "no_matching_record"


def test_applescript_one_line_form_still_correlates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    world.send_result = ("applescript", "sent")
    world.on_text = lambda w, text: w.write(user(" ".join(text.split("\n"))))
    rc, env, _err = _send(world, monkeypatch, capsys, "line one\nline two")
    assert rc == 0 and env is not None
    assert env["data"] == {
        "delivery_id": env["data"]["delivery_id"],
        "outcome": "accepted",
        "channel": "applescript",
    }


@pytest.mark.parametrize(
    "text",
    ["esc \x1b here", "paste end \x1b[201~ injected", "nul \x00", "cr \r", "c1 \x9b", "del \x7f"],
)
def test_control_characters_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    world = _idle_world(tmp_path)
    rc, env, _err = _send(world, monkeypatch, capsys, text)
    assert rc == 2 and env is not None and env["error"]["code"] == "control_characters"
    assert not world.texts and _deliveries(world) == []


def test_too_long_and_empty_messages_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    rc, env, _err = _send(world, monkeypatch, capsys, "x" * 4001)
    assert rc == 2 and env is not None and env["error"]["code"] == "message_too_long"
    rc, env, _err = _send(world, monkeypatch, capsys, " \n\t")
    assert rc == 2 and env is not None and env["error"]["code"] == "empty_message"
    assert not world.texts


def test_message_is_never_taken_from_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    rc, _env, _err = run_cli(
        monkeypatch, capsys, world, ["send", "-s", SID, "-j", "hello"], stdin=""
    )
    assert rc == 2 and not world.texts


def test_a_terminal_on_stdin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import sys

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    world = _idle_world(tmp_path)
    monkeypatch.setattr(sys, "stdin", _Tty("hello"))
    rc, env, _err = run_cli(monkeypatch, capsys, world, ["send", "-s", SID, "-j"])
    assert rc == 2 and env is not None and env["error"]["code"] == "stdin_required"


def test_per_tab_lock_refuses_a_concurrent_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    lock_dir = world.tmp / "ccc" / "locks"
    lock_dir.mkdir(parents=True)
    uuid = TAB.rsplit(":", maxsplit=1)[-1].upper()
    fd = os.open(lock_dir / f"tab-{uuid}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rc, env, _err = _send(world, monkeypatch, capsys, "hello")
    finally:
        os.close(fd)
    assert rc == 1 and env is not None and env["error"]["code"] == "tab_locked"
    assert not world.texts
    world.on_text = lambda w, text: w.write(user(text))
    rc, env, _err = _send(world, monkeypatch, capsys, "hello")  # released → goes through
    assert rc == 0


def test_lock_file_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    _send(world, monkeypatch, capsys, "hello")
    (lock,) = (world.tmp / "ccc" / "locks").iterdir()
    assert lock.stat().st_mode & 0o777 == 0o600
