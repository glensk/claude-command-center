"""``terminal.send_keys_via``: the byte map, the Python-API-only path, partial sends."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from command_center import terminal


class _FakeSession:
    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.fail_after = fail_after

    async def async_send_text(self, text: str, suppress_broadcast: bool = False) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("socket closed")
        self.sent.append((text, suppress_broadcast))


def _fake_iterm2(monkeypatch: pytest.MonkeyPatch, session: _FakeSession | None) -> dict[str, Any]:
    calls: dict[str, Any] = {"invalidated": 0, "lookups": []}

    class _App:
        def get_session_by_id(self, uuid: str) -> _FakeSession | None:
            calls["lookups"].append(uuid)
            return session

    class _Connection:
        @staticmethod
        async def async_create() -> object:
            return object()

    async def async_get_app(_conn: object) -> _App:
        return _App()

    def invalidate_app() -> None:
        calls["invalidated"] += 1

    module = types.ModuleType("iterm2")
    module.Connection = _Connection  # type: ignore[attr-defined]
    module.async_get_app = async_get_app  # type: ignore[attr-defined]
    module.app = types.SimpleNamespace(invalidate_app=invalidate_app)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "iterm2", module)
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: True)

    def _no_applescript(*_a: object, **_k: object) -> None:
        raise AssertionError("AppleScript must never be used to send keys")

    monkeypatch.setattr(terminal, "_osascript", _no_applescript)
    monkeypatch.setattr(terminal, "_send_text_applescript", _no_applescript)
    return calls


def test_byte_map() -> None:
    assert terminal.KEY_BYTES == {
        "up": "\x1b[A",
        "down": "\x1b[B",
        "right": "\x1b[C",
        "left": "\x1b[D",
        "enter": "\r",
        "space": " ",
        "tab": "\t",
        "btab": "\x1b[Z",
        "esc": "\x1b",
    }
    assert terminal.key_payloads(["DOWN", "3", "text:hi, you", "wait:0.5", "esc"]) == [
        ("down", "\x1b[B"),
        ("3", "3"),
        ("text:hi, you", "hi, you"),
        ("wait:0.5", ""),
        ("esc", "\x1b"),
    ]


@pytest.mark.parametrize("bad", ["home", "12", "text:", "text:a\x1b[201~", "wait:99", "wait:x"])
def test_bad_tokens_raise_before_connecting(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    calls = _fake_iterm2(monkeypatch, _FakeSession())
    with pytest.raises(ValueError):
        terminal.send_keys_via("w0t0p0:UUID", ["down", bad])
    assert calls["invalidated"] == 0


def test_keys_go_through_the_python_api_only(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    calls = _fake_iterm2(monkeypatch, session)
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", _sleep)
    result = terminal.send_keys_via("w0t0p0:UUID-1", ["down", "enter", "wait:0.5", "text:ok"])
    assert result == "sent"
    assert calls["invalidated"] == 1  # the App singleton is reset before connecting
    assert calls["lookups"] == ["UUID-1"]
    assert session.sent == [("\x1b[B", True), ("\r", True), ("ok", True)]
    assert 0.5 in sleeps and sleeps.count(terminal.KEY_DELAY) == 3


def test_partial_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _sleep)
    _fake_iterm2(monkeypatch, _FakeSession(fail_after=1))
    assert terminal.send_keys_via("UUID", ["down", "enter"]) == "partial"
    _fake_iterm2(monkeypatch, _FakeSession(fail_after=0))
    assert terminal.send_keys_via("UUID", ["down", "enter"]) == "none"
    _fake_iterm2(monkeypatch, None)  # the API does not know the session
    assert terminal.send_keys_via("UUID", ["down"]) == "none"
    assert terminal.send_keys_via("", ["down"]) == "none"


def test_unreachable_iterm_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    calls = _fake_iterm2(monkeypatch, session)
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: False)
    monkeypatch.setattr(terminal, "_iterm_reachable_by_apple_event", lambda timeout=5: False)
    assert terminal.send_keys_via("UUID", ["down"]) == "none"
    assert calls["invalidated"] == 0 and not session.sent


def test_send_text_detailed_reports_a_partial_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: True)
    monkeypatch.setattr(terminal, "_send_text_python_api", lambda uuid, text: "partial")
    monkeypatch.setattr(
        terminal,
        "_send_text_applescript",
        lambda uuid, text: pytest.fail("a half-sent paste must not fall back"),
    )
    assert terminal.send_text_detailed("UUID", "hi") == ("", "partial")
    assert terminal.send_text_via("UUID", "hi") == ""
    monkeypatch.setattr(terminal, "_send_text_python_api", lambda uuid, text: "sent")
    assert terminal.send_text_detailed("UUID", "hi") == ("python-api", "sent")
    monkeypatch.setattr(terminal, "_send_text_python_api", lambda uuid, text: "none")
    monkeypatch.setattr(terminal, "_send_text_applescript", lambda uuid, text: True)
    assert terminal.send_text_detailed("UUID", "hi") == ("applescript", "sent")
    assert terminal.send_text_via("UUID", "hi") == "applescript"


def test_iterm_session_vars_reads_tty_and_job_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    class _VarSession(_FakeSession):
        async def async_get_variable(self, name: str) -> object:
            return {"tty": "/dev/ttys004", "jobPid": 77}.get(name)

    calls = _fake_iterm2(monkeypatch, _VarSession())
    assert terminal.iterm_session_vars("w0t0p0:U") == {"tty": "/dev/ttys004", "jobPid": "77"}
    assert calls["invalidated"] == 1
    _fake_iterm2(monkeypatch, None)
    assert terminal.iterm_session_vars("w0t0p0:U") == {}
