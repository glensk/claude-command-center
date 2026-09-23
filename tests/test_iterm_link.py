"""iTerm link hardening for the resident panel server (tp#70 S3b, D4/V16/R17).

Driven against a fake ``iterm2`` package so it runs headlessly: the real package keeps a
process-wide ``App`` singleton bound to the first connection (V16) and, on a 401, falls
back to an in-process AppleScript cookie request with no timeout (tp#90). These tests pin
that a reconnect gets ITS OWN app object, that the cookie is in ``os.environ`` only for
the connect (success / 401 / timeout / exception), that the package can never
authenticate by itself, and that child processes never see the credentials.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import types
from typing import Any

import pytest

from command_center import iterm_api


class _AuthError(Exception):
    pass


def _fake_iterm2(monkeypatch: pytest.MonkeyPatch, connect: Any) -> types.SimpleNamespace:
    """Install a minimal fake ``iterm2`` whose App singleton mimics the real one."""
    state = types.SimpleNamespace(instance=None, connects=0, env_seen=[], auth_calls=0)

    auth = types.ModuleType("iterm2.auth")
    auth.AuthenticationException = _AuthError  # type: ignore[attr-defined]

    def _authenticate(*_a: object, **_k: object) -> bool:
        state.auth_calls += 1
        return True

    auth.authenticate = _authenticate  # type: ignore[attr-defined]

    app_mod = types.ModuleType("iterm2.app")

    def _invalidate() -> None:
        state.instance = None

    app_mod.invalidate_app = _invalidate  # type: ignore[attr-defined]

    class _Connection:
        @staticmethod
        async def async_create() -> Any:
            state.connects += 1
            state.env_seen.append((os.environ.get("ITERM2_COOKIE"), os.environ.get("ITERM2_KEY")))
            # The real package calls auth.authenticate() when no cookie works.
            if os.environ.get("ITERM2_COOKIE") == "stale":
                try:
                    auth.authenticate(True)
                except _AuthError as exc:
                    raise ConnectionRefusedError("401") from exc
            return await connect(state.connects)

    async def _get_app(connection: Any, create_if_needed: bool = True) -> Any:
        del create_if_needed
        if state.instance is None:
            state.instance = types.SimpleNamespace(connection=connection)
        return state.instance

    pkg = types.ModuleType("iterm2")
    pkg.auth = auth  # type: ignore[attr-defined]
    pkg.app = app_mod  # type: ignore[attr-defined]
    pkg.Connection = _Connection  # type: ignore[attr-defined]
    pkg.async_get_app = _get_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "iterm2", pkg)
    monkeypatch.setitem(sys.modules, "iterm2.auth", auth)
    monkeypatch.setitem(sys.modules, "iterm2.app", app_mod)
    for name in iterm_api.AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    return state


async def _conn(n: int) -> str:
    return f"conn-{n}"


def _cookie_link(cookie: tuple[str, str] | None = ("c00kie", "k3y")) -> Any:
    return iterm_api.CookieItermLink(running=lambda: True, cookie=lambda: cookie)


def test_second_connection_gets_its_own_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """V16: after a drop, the reconnect's app is bound to the NEW connection."""
    _fake_iterm2(monkeypatch, _conn)
    for link in (iterm_api.ItermLink(), _cookie_link()):
        assert asyncio.run(link.ensure()) is True
        first = link._app  # pylint: disable=protected-access
        link.drop()
        assert asyncio.run(link.ensure()) is True
        second = link._app  # pylint: disable=protected-access
        assert second is not first
        assert second.connection == link._connection  # pylint: disable=protected-access


def test_cookie_is_in_env_only_during_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_iterm2(monkeypatch, _conn)
    link = _cookie_link()
    assert asyncio.run(link.ensure()) is True
    assert state.env_seen == [("c00kie", "k3y")]
    assert not any(name in os.environ for name in iterm_api.AUTH_ENV)


def test_env_removed_after_401_and_package_never_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected cookie fails the connect — no in-process AppleScript fallback."""
    state = _fake_iterm2(monkeypatch, _conn)
    link = _cookie_link(("stale", "k"))
    assert asyncio.run(link.ensure()) is False
    assert state.auth_calls == 0  # the package's own authenticate was replaced
    assert not any(name in os.environ for name in iterm_api.AUTH_ENV)
    assert sys.modules["iterm2.auth"].authenticate.__name__ == "_authenticate"  # restored


def test_env_removed_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_n: int) -> str:
        raise OSError("socket gone")

    _fake_iterm2(monkeypatch, _boom)
    assert asyncio.run(_cookie_link().ensure()) is False
    assert not any(name in os.environ for name in iterm_api.AUTH_ENV)


def test_env_removed_after_timeout_via_link_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged connect is cancelled at the bound; the cookie does not linger."""

    async def _hang(_n: int) -> str:
        await asyncio.sleep(30)
        return "never"

    _fake_iterm2(monkeypatch, _hang)
    thread = iterm_api.LinkThread(_cookie_link())
    try:
        start = time.monotonic()
        assert thread.call(lambda link: link.ensure(), timeout=0.2) is None
        assert time.monotonic() - start < 2.0
        deadline = time.monotonic() + 2.0
        while any(n in os.environ for n in iterm_api.AUTH_ENV) and time.monotonic() < deadline:
            time.sleep(0.01)  # cancellation unwinds the context manager on the loop thread
        assert not any(name in os.environ for name in iterm_api.AUTH_ENV)
    finally:
        thread.stop()


def test_no_connect_when_iterm_absent_or_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_iterm2(monkeypatch, _conn)
    absent = iterm_api.CookieItermLink(running=lambda: False, cookie=lambda: ("c", "k"))
    assert asyncio.run(absent.ensure()) is False
    assert asyncio.run(_cookie_link(None).ensure()) is False
    assert state.connects == 0


def test_link_thread_returns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm2(monkeypatch, _conn)
    thread = iterm_api.LinkThread(_cookie_link())
    try:
        assert thread.call(lambda link: link.ensure(), timeout=2.0) is True
    finally:
        thread.stop()


def test_child_env_never_carries_the_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ITERM2_COOKIE", "secret")
    monkeypatch.setenv("ITERM2_KEY", "secret-key")
    env = iterm_api.strip_auth_env()
    assert "ITERM2_COOKIE" not in env and "ITERM2_KEY" not in env
    out = subprocess.run(
        ["/usr/bin/env"], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert "ITERM2_" not in out
    assert iterm_api.strip_auth_env({"A": "1", "ITERM2_KEY": "x"}) == {"A": "1"}


def test_fetch_cookie_parses_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import terminal

    seen: dict[str, Any] = {}

    def _fake(script: str, timeout: float = 10) -> str | None:
        seen.update(script=script, timeout=timeout)
        return "abc def\n"

    monkeypatch.setattr(terminal, "_osascript", _fake)
    assert iterm_api.fetch_cookie() == ("abc", "def")
    assert seen["timeout"] == iterm_api.COOKIE_TIMEOUT_SEC
    assert "request cookie and key" in seen["script"]
    monkeypatch.setattr(terminal, "_osascript", lambda *_a, **_k: None)
    assert iterm_api.fetch_cookie() is None
