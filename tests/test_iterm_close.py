"""One-shot iTerm2 connections close cleanly (tp#1875).

A fake iTerm2 websocket server on 127.0.0.1 and the REAL ``iterm2.Connection`` with its
real reader task, connected the way the package connects (``close_timeout=0``): closing
the socket under the reader printed ``ConnectionClosedError: sent 1000 (OK); no close
frame received`` on every channel probe. No real iTerm is reached.
"""

# pylint: disable=protected-access  # the package's reader loop is private
from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import iterm2
import pytest
import websockets.legacy.client
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from command_center import iterm_conn, terminal


class _FakeIterm:
    """A websocket server that answers nothing and records how each client left."""

    def __init__(self) -> None:
        self.closes: list[int | None] = []
        self.done = threading.Event()
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = int(self._server.socket.getsockname()[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self, ws: ServerConnection) -> None:
        try:
            for _message in ws:
                pass
        except ConnectionClosed:
            pass
        self.closes.append(ws.close_code)
        self.done.set()

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture(name="fake_iterm")
def fake_iterm_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeIterm]:
    """``iterm2.Connection.async_create`` connects to the fake server, as the package does."""
    server = _FakeIterm()

    async def async_create() -> Any:
        connection = iterm2.Connection()
        connection.websocket = await websockets.legacy.client.connect(
            f"ws://127.0.0.1:{server.port}", ping_interval=None, close_timeout=0
        )
        loop = asyncio.get_running_loop()
        reader = asyncio.ensure_future(connection._async_dispatch_forever(connection, loop))
        setattr(connection, iterm_conn.DISPATCH_ATTR, reader)
        return connection

    monkeypatch.setattr(iterm2.Connection, "async_create", staticmethod(async_create))
    yield server
    server.stop()


def test_the_package_still_names_its_reader_task() -> None:
    """``close_connection`` reaches the reader by the package's private name; pin it."""
    assert hasattr(iterm2.Connection(), iterm_conn.DISPATCH_ATTR)


@pytest.mark.real_channel_probe
def test_channel_probe_closes_without_a_traceback(
    fake_iterm: _FakeIterm, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: True)
    monkeypatch.setattr(terminal, "_iterm_reachable_by_apple_event", lambda timeout=5: True)
    health = terminal.delivery_channel_health()
    assert health["python_api"] is True and health["python_api_error"] == ""
    assert fake_iterm.done.wait(5)
    assert fake_iterm.closes == [1000]  # the server saw the client's normal close
    err = capfd.readouterr().err
    assert "Traceback" not in err and "ConnectionClosed" not in err and "never retrieved" not in err


def test_open_connection_closes_on_an_exception_too(
    fake_iterm: _FakeIterm, capfd: pytest.CaptureFixture[str]
) -> None:
    opened: list[Any] = []

    async def _go() -> None:
        async with iterm_conn.open_connection(iterm2, 5) as conn:
            opened.append(conn)
            raise RuntimeError("the work failed")

    with pytest.raises(RuntimeError, match="the work failed"):
        asyncio.run(_go())
    assert fake_iterm.done.wait(5)
    assert fake_iterm.closes == [1000]
    # The handshake completed: iTerm's close frame arrived inside CLOSE_TIMEOUT_SEC.
    assert opened[0].websocket.close_rcvd is not None
    assert "Traceback" not in capfd.readouterr().err


def test_close_connection_retrieves_a_dead_reader(capfd: pytest.CaptureFixture[str]) -> None:
    """A reader that already died (iTerm dropped the socket) is not re-raised or re-logged."""
    closed: list[float] = []

    class _Socket:
        close_timeout = 0.0

        async def close(self) -> None:
            closed.append(self.close_timeout)
            raise OSError("socket already gone")  # swallowed: the work is done

    async def _go() -> None:
        async def _died() -> None:
            raise ConnectionError("iTerm went away")

        conn = type("Conn", (), {})()
        reader = asyncio.ensure_future(_died())
        await asyncio.wait({reader})
        setattr(conn, iterm_conn.DISPATCH_ATTR, reader)
        conn.websocket = _Socket()
        await iterm_conn.close_connection(conn)

    asyncio.run(_go())
    assert closed == [iterm_conn.CLOSE_TIMEOUT_SEC]
    assert "never retrieved" not in capfd.readouterr().err


def test_close_connection_tolerates_a_bare_fake() -> None:
    """Test fakes (``object()``, a ``websocket``-only stub) close as far as they can."""

    async def _go() -> None:
        await iterm_conn.close_connection(object())
        await iterm_conn.close_connection(type("Conn", (), {"websocket": None})())

    asyncio.run(_go())
