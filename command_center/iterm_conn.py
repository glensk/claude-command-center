"""One-shot iTerm2 Python-API connections that close cleanly (tp#1875).

Every short-lived Python-API call in :mod:`command_center.terminal` and
:mod:`command_center.snapshot` connects through :func:`open_connection`. The module imports
nothing of ccc's own, so both can use it without an import cycle; the resident link lives
in :mod:`command_center.iterm_api`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

#: How long :func:`close_connection` waits for iTerm's close frame. The ``iterm2`` package
#: opens its socket with ``close_timeout=0``, which ends every close as "no close frame
#: received"; iTerm answers a local close in milliseconds.
CLOSE_TIMEOUT_SEC = 1.0
#: The package's private name for the reader task ``Connection.async_create`` starts.
DISPATCH_ATTR = "_Connection__dispatch_forever_future"


async def close_connection(connection: Any) -> None:
    """Close a one-shot ``iterm2`` connection with a full close handshake, raising nothing.

    ``Connection.async_create`` leaves a reader task parked in ``websocket.recv()``;
    closing the socket under it makes ``recv`` raise ``ConnectionClosedError``, which the
    package prints as a traceback. So the reader is cancelled and awaited first (the
    package swallows its ``CancelledError``; ``asyncio.wait`` never re-raises it into the
    caller), a reader that already died has its exception retrieved (no "never retrieved"
    log), and only then is the socket closed, with :data:`CLOSE_TIMEOUT_SEC` for iTerm's
    close frame. A connection without these attributes (a test fake) is closed as far as
    it has them.
    """
    reader = getattr(connection, DISPATCH_ATTR, None)
    if isinstance(reader, asyncio.Future):
        if not reader.done():
            reader.cancel()
            await asyncio.wait({reader})
        if not reader.cancelled():
            reader.exception()
    websocket = getattr(connection, "websocket", None)
    if websocket is None:
        return
    with contextlib.suppress(Exception):
        if hasattr(websocket, "close_timeout"):
            websocket.close_timeout = CLOSE_TIMEOUT_SEC
        await websocket.close()


@contextlib.asynccontextmanager
async def open_connection(iterm2: Any, timeout: float) -> AsyncIterator[Any]:
    """A fresh ``iterm2`` connection (connect bounded by *timeout*), closed on exit.

    The connection is closed by :func:`close_connection` on success, exception and
    cancellation alike, so no call leaves a socket to ``asyncio.run``'s teardown.
    """
    connection = await asyncio.wait_for(iterm2.Connection.async_create(), timeout=timeout)
    try:
        yield connection
    finally:
        await close_connection(connection)
