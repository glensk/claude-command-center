#!/usr/bin/env python3
"""Warm iTerm2 Python-API link for the TUI's resident f+j jump.

One long-lived websocket connection replaces the per-jump osascript walk
(~620 ms over 16 sessions): after connect, focus-refresh + session-by-id
lookups are sub-millisecond and activation is a few RPCs. Everything here
degrades to None/False — callers fall back to the AppleScript helpers in
:mod:`command_center.terminal`. The ``iterm2`` package and iTerm's
"Enable Python API" setting are required for the fast path only.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


from typing import Any


class ItermLink:
    """Lazily-connected, self-healing wrapper over the iTerm2 async API.

    ``ensure`` doubles as the reconnect: callers invoke it per operation, so a
    websocket dropped by an iTerm restart is transparently re-established. Every
    method traps any exception, marks the link unready, and returns the degrade
    value, so a dead socket can never propagate to the UI loop.
    """

    def __init__(self) -> None:
        self._connection: Any = None
        self._app: Any = None

    @property
    def ready(self) -> bool:
        """True once a connection + app handle are live."""
        return self._app is not None

    def _drop(self) -> None:
        """Mark the link unready so the next ``ensure`` reconnects from scratch."""
        self._app = None
        self._connection = None

    async def ensure(self) -> bool:
        """Connect (or confirm we still are). Also the lazy reconnect — see class doc."""
        if self.ready:
            return True
        try:
            import iterm2  # pylint: disable=import-outside-toplevel
        except ImportError:
            return False
        try:
            self._connection = await iterm2.Connection.async_create()
            self._app = await iterm2.async_get_app(self._connection, create_if_needed=True)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._drop()
            return False
        return self.ready

    async def current_session_uuid(self) -> str | None:
        """UUID of iTerm's currently-focused session, or None (unready / no focus)."""
        if not await self.ensure():
            return None
        try:
            await self._app.async_refresh_focus()
            window = self._app.current_terminal_window
            if window is None:
                return None
            tab = window.current_tab
            if tab is None:
                return None
            session = tab.current_session
            if session is None:
                return None
            return session.session_id
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._drop()
            return None

    async def focus_session(self, iterm_session_id: str) -> bool:
        """Bring the tab/window for *iterm_session_id* forward (and iTerm frontmost).

        *iterm_session_id* is the ``$ITERM_SESSION_ID`` value (``w0t0p0:UUID``); the
        API session id is the UUID after the colon. Returns False if the session can't
        be located or anything goes wrong (caller falls back to AppleScript).
        """
        uuid = iterm_session_id.split(":")[-1].strip()
        if not uuid:
            return False
        if not await self.ensure():
            return False
        try:
            import iterm2  # pylint: disable=import-outside-toplevel

            session = self._app.get_session_by_id(uuid)
            if session is None:
                # Layout may have drifted since the cached app was fetched — refetch once.
                self._app = await iterm2.async_get_app(self._connection)
                session = self._app.get_session_by_id(uuid)
            if session is None:
                return False
            await session.async_activate(select_tab=True, order_window_front=True)
            # Bring iTerm frontmost when the user fired f+j from another app.
            await self._app.async_activate(raise_all_windows=False, ignoring_other_apps=True)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._drop()
            return False
        return True
