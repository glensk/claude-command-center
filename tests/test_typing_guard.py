"""The typing guard of ``ccc send`` / ``ccc answer`` (``user_active``), all with fakes."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from bridgestub import SID, TAB, World, answer, ask, question, run_cli, turn_end, user

from command_center import bridge_target, terminal
from command_center.adapters import claude_bridge as cb
from command_center.bridge_json import BridgeError
from command_center.store import Store

TOOL_ID = "toolu_guard"
COLOUR = question("Which colour?", ["red", "green"])
TYPING: dict[str, Any] = {"frontmost": True, "key_idle": 4.0, "focused": TAB}


def _idle_world(tmp_path: Path, **kw: Any) -> World:
    world = World(tmp_path, **kw)
    world.write(user("earlier"), *turn_end())
    world.on_text = lambda w, text: w.write(user(text))
    return world


def _send(world: World, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    code, env, _err = run_cli(mp, cap, world, ["send", "-s", SID, "-j"], stdin="hello")
    return code, env


def _deliveries(world: World) -> list[Any]:
    with Store(world.db) as store:
        return list(store.deliveries_for(SID))


def test_send_is_refused_while_the_user_types_in_the_focused_target_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, **TYPING)
    code, env = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None
    assert env["ok"] is False and env["data"] is None
    assert env["error"]["code"] == "user_active"
    assert not world.texts and not _deliveries(world)  # nothing typed, no delivery row


@pytest.mark.parametrize(
    "probes",
    [
        {"frontmost": False, "key_idle": 1.0, "focused": TAB},  # another app is in front
        {"frontmost": True, "key_idle": 30.0, "focused": TAB},  # quiet for the whole window
        {"frontmost": True, "key_idle": 1.0, "focused": "w0t2p0:OTHER-TAB"},  # other tab
        {"frontmost": True, "key_idle": 1.0, "focused": ""},  # no key terminal window
        {"frontmost": None, "key_idle": 99.0, "focused": None},  # unknown front, but quiet
    ],
)
def test_send_goes_through_when_the_user_is_not_typing_into_the_tab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    probes: dict[str, Any],
) -> None:
    world = _idle_world(tmp_path, **probes)
    code, env = _send(world, monkeypatch, capsys)
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"
    assert world.texts == ["hello"]


def test_unknown_probes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, frontmost=None, key_idle=None, focused=None)
    code, env = _send(world, monkeypatch, capsys)
    assert code == 1 and env is not None and env["error"]["code"] == "user_active"
    assert not world.texts


def test_guard_window_zero_disables_every_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, guard_sec=0, **TYPING)
    code, _env = _send(world, monkeypatch, capsys)
    assert code == 0 and world.texts == ["hello"] and world.focus_reads == 0


def test_focus_is_only_read_when_iterm_is_front_and_a_key_is_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, frontmost=True, key_idle=500.0, focused=TAB)
    assert _send(world, monkeypatch, capsys)[0] == 0
    assert world.focus_reads == 0  # the iTerm focus query is the last, rarest probe


def test_guard_runs_inside_the_per_tab_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _idle_world(tmp_path, frontmost=True, key_idle=1.0)
    held: list[bool] = []

    def focused_tab() -> str:
        lock = next((tmp_path / "ccc" / "locks").glob("tab-*.lock"))
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(fd)
        return "w0t9p0:SOMEWHERE-ELSE"

    world.focused_tab = focused_tab  # type: ignore[method-assign]
    assert _send(world, monkeypatch, capsys)[0] == 0
    assert held == [True]


def test_answer_is_refused_while_the_user_types_and_sends_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, status="waiting", **TYPING)
    world.write(user("decide"), ask(TOOL_ID, [COLOUR]))
    ins = cb.inspect(cb.load_records(world.transcript))
    assert ins.anchor is not None
    payload = {
        "decision_id": cb.decision_id(ins.anchor, ins.questions),
        "answers": [{"question_index": 0, "option_indices": [1]}],
    }
    code, env, _err = run_cli(
        monkeypatch, capsys, world, ["answer", "-s", SID, "-j"], stdin=json.dumps(payload)
    )
    assert code == 1 and env is not None and env["error"]["code"] == "user_active"
    assert not world.keys

    world.frontmost = False  # the user switched apps: the same answer goes through
    world.on_keys = lambda w, _k: w.write(answer(TOOL_ID, [COLOUR], {"Which colour?": "green"}))
    code, env, _err = run_cli(
        monkeypatch, capsys, world, ["answer", "-s", SID, "-j"], stdin=json.dumps(payload)
    )
    assert code == 0 and env is not None and env["data"]["outcome"] == "accepted"


def test_guard_message_never_names_a_person() -> None:
    deps = bridge_target.BridgeDeps(
        iterm_frontmost=lambda: True,
        keystroke_idle=lambda: 2.0,
        focused_tab=lambda: TAB,
        typing_guard_sec=lambda: 30.0,
    )
    with pytest.raises(BridgeError) as exc:
        bridge_target.guard_user_active(deps, TAB)
    assert exc.value.code == "user_active" and exc.value.exit_code == 1
    assert "2 s ago" in exc.value.message and "guard 30 s" in exc.value.message


# ------------------------------------------------------------------ the real probes


class _Recorder:
    """A fake ``iterm2`` session/tab/window whose every activation call fails the test."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.current_session = self
        self.current_tab = self

    def __getattr__(self, name: str) -> Any:
        if "activate" in name or "select" in name:
            raise AssertionError(f"the focus probe called {name}")
        raise AttributeError(name)


def test_iterm_focused_session_reads_without_activating(monkeypatch: pytest.MonkeyPatch) -> None:
    window = _Recorder("FOCUSED-UUID")

    async def _create() -> object:
        return object()

    async def _get_app(_conn: object) -> object:
        return types.SimpleNamespace(current_terminal_window=window)

    fake = types.ModuleType("iterm2")
    fake.app = types.SimpleNamespace(invalidate_app=lambda: None)  # type: ignore[attr-defined]
    fake.Connection = types.SimpleNamespace(async_create=_create)  # type: ignore[attr-defined]
    fake.async_get_app = _get_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "iterm2", fake)
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: True)
    assert terminal.iterm_focused_session() == "FOCUSED-UUID"

    async def _no_window(_conn: object) -> object:
        return types.SimpleNamespace(current_terminal_window=None)

    fake.async_get_app = _no_window  # type: ignore[attr-defined]
    assert terminal.iterm_focused_session() == ""


def test_iterm_focused_session_is_unknown_without_api_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: False)
    monkeypatch.setattr(terminal, "_iterm_reachable_by_apple_event", lambda: False)
    assert terminal.iterm_focused_session() is None


def test_iterm_frontmost_state_is_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs: list[str] = []

    def run(_argv: list[str], **_kw: Any) -> Any:
        return types.SimpleNamespace(stdout=outputs.pop(0))

    monkeypatch.setattr(terminal.shutil, "which", lambda _n: "/usr/bin/lsappinfo")
    monkeypatch.setattr(terminal.subprocess, "run", run)
    outputs[:] = ["ASN:0x0-0x1:", '"CFBundleIdentifier"="com.googlecode.iterm2"']
    assert terminal.iterm_frontmost_state() is True
    outputs[:] = ["ASN:0x0-0x2:", '"CFBundleIdentifier"="com.apple.Safari"']
    assert terminal.iterm_frontmost_state() is False
    outputs[:] = [""]
    assert terminal.iterm_frontmost_state() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreGraphics is macOS-only")
def test_seconds_since_keystroke_is_a_duration() -> None:
    value = terminal.seconds_since_keystroke()
    assert value is None or value >= 0


def test_seconds_since_keystroke_is_unknown_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal.sys, "platform", "linux")
    assert terminal.seconds_since_keystroke() is None
