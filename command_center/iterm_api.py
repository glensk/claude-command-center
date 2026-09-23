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


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import asyncio
import contextlib
import os
import threading
from collections.abc import Callable, Coroutine, Iterator, Mapping
from typing import Any, TypeVar

_T = TypeVar("_T")

#: The two variables the ``iterm2`` package reads its websocket credentials from.
AUTH_ENV = ("ITERM2_COOKIE", "ITERM2_KEY")
ITERM_BUNDLE_ID = "com.googlecode.iterm2"
#: The bounded cookie request the resident panel server makes itself (tp#70 D4) — the
#: SAME Apple event ``iterm2.auth`` would send, but in a subprocess with a timeout.
COOKIE_SCRIPT = (
    'tell application "iTerm2" to request cookie and key for app named "ccc panel-server"'
)
COOKIE_TIMEOUT_SEC = 5.0

# Serialises every window in which the cookie sits in os.environ (see scoped_auth_env).
_AUTH_LOCK = threading.Lock()


def strip_auth_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of *env* (default ``os.environ``) without the iTerm2 cookie/key.

    Every child the panel server spawns gets this env, so a credential that is only
    ever meant for the websocket handshake can never reach a subprocess (tp#70 R17).
    """
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k not in AUTH_ENV}


def iterm_running() -> bool:
    """True when iTerm2 is running — checked WITHOUT an Apple event (NSWorkspace).

    Asking an absent app for a cookie would launch it (or hang on a TCC prompt for
    nothing); the running-application list costs neither.
    """
    try:
        import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False
    running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(  # pylint: disable=no-member
        ITERM_BUNDLE_ID
    )
    return bool(running) and len(running) > 0


def fetch_cookie(timeout: float = COOKIE_TIMEOUT_SEC) -> tuple[str, str] | None:
    """``(cookie, key)`` from iTerm2 via a BOUNDED ``osascript`` subprocess, or None.

    The ``iterm2`` package's own request runs in-process with no timeout (tp#90); this
    is the same Apple event with *timeout* on it. The output is never logged.
    """
    from . import terminal  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = terminal._osascript(COOKIE_SCRIPT, timeout=timeout)  # pylint: disable=protected-access
    parts = (out or "").split()
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


@contextlib.contextmanager
def scoped_auth_env(cookie: str, key: str) -> Iterator[None]:
    """Install the cookie/key in ``os.environ`` for ONE connect, then remove them.

    The ``iterm2`` package only reads its credentials from the environment, so they must
    be there while :meth:`iterm2.Connection.async_create` builds its handshake headers —
    and nowhere else, ever: the window is serialised by a process-wide lock, the variables
    are removed in ``finally`` on success, 401, timeout, cancellation and exception
    alike, and whatever was there before (normally nothing — the server scrubs both at
    start) is NOT restored. Inside the window ``iterm2.auth.authenticate`` is replaced by
    a refusal, so a 401 can never fall back to the package's unbounded in-process
    AppleScript request (tp#70 D4): the connect just fails and the caller degrades.
    """
    import iterm2.auth  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    def _refuse(*_args: object, **_kwargs: object) -> bool:
        raise iterm2.auth.AuthenticationException("in-process iTerm2 auth disabled")

    with _AUTH_LOCK:
        original = iterm2.auth.authenticate
        iterm2.auth.authenticate = _refuse
        os.environ[AUTH_ENV[0]] = cookie
        os.environ[AUTH_ENV[1]] = key
        try:
            yield
        finally:
            for name in AUTH_ENV:
                os.environ.pop(name, None)
            iterm2.auth.authenticate = original


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

    def drop(self) -> None:
        """Forget the connection (the next :meth:`ensure` reconnects)."""
        self._drop()

    async def _connect(self, iterm2: Any) -> tuple[Any, Any]:
        """Open a connection and fetch ITS app object.

        ``iterm2.app`` keeps a process-wide ``App`` singleton bound to the FIRST
        connection; ``async_get_app`` would hand a reconnect that stale object (whose
        RPCs go to the dead socket), so it is invalidated first (tp#70 V16).
        """
        iterm2.app.invalidate_app()
        connection = await iterm2.Connection.async_create()
        app = await iterm2.async_get_app(connection, create_if_needed=True)
        return connection, app

    async def ensure(self) -> bool:
        """Connect (or confirm we still are). Also the lazy reconnect — see class doc."""
        if self.ready:
            return True
        try:
            import iterm2  # pylint: disable=import-outside-toplevel
        except ImportError:
            return False
        try:
            self._connection, self._app = await self._connect(iterm2)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._drop()
            return False
        return self.ready

    async def session_variable(self, uuid: str, name: str) -> str | None:
        """Variable *name* (``path``, ``tty``, …) of session *uuid*, or None."""
        if not uuid or not await self.ensure():
            return None
        try:
            session = self._app.get_session_by_id(uuid)
            if session is None:
                import iterm2  # pylint: disable=import-outside-toplevel

                # A tab opened after the app was fetched — refetch once (as focus_session).
                self._app = await iterm2.async_get_app(self._connection)
                session = self._app.get_session_by_id(uuid)
            if session is None:
                return None
            value = await session.async_get_variable(name)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._drop()
            return None
        return str(value) if value else None

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


class CookieItermLink(ItermLink):
    """The panel server's link: the ``iterm2`` package never authenticates by itself.

    Before every (re)connect: iTerm must be running (NSWorkspace — no Apple event to an
    absent app), the cookie comes from the bounded :func:`fetch_cookie` subprocess, and
    it sits in the environment only for the connect (:func:`scoped_auth_env`). Any
    failure leaves the link unready; the server reports ``degraded`` and re-probes.
    The two probes are injectable for tests.
    """

    def __init__(
        self,
        *,
        running: Callable[[], bool] = iterm_running,
        cookie: Callable[[], tuple[str, str] | None] = fetch_cookie,
    ) -> None:
        super().__init__()
        self._running = running
        self._cookie = cookie

    async def ensure(self) -> bool:
        """Report readiness only — this link NEVER connects as a side effect of an op.

        Reconnects are the server's decision (:meth:`reconnect`, run by its probe while
        no panel is active), so a resolver thread can never open the cookie window
        concurrently with a child spawn on another thread (tp#70 D4/R17).
        """
        return self.ready

    async def reconnect(self) -> bool:
        """Drop any old connection and connect afresh (bounded by the caller)."""
        self._drop()
        return await super().ensure()

    async def _connect(self, iterm2: Any) -> tuple[Any, Any]:
        if not self._running():
            raise ConnectionError("iTerm2 is not running")
        creds = self._cookie()  # a subprocess — spawned BEFORE the cookie is in os.environ
        if creds is None:
            raise ConnectionError("no iTerm2 cookie")
        with scoped_auth_env(*creds):
            return await super()._connect(iterm2)


class LinkThread:
    """A dedicated asyncio loop thread for an :class:`ItermLink` (tp#70 D4/D12).

    :meth:`call` runs one coroutine on it and waits at most *timeout* seconds; on expiry
    the future is cancelled and ``None`` comes back, so a wedged websocket can never
    block the caller (the panel server's main thread) for longer than the bound.
    """

    def __init__(self, link: CookieItermLink | None = None) -> None:
        self.link = link or CookieItermLink()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="iterm-link", daemon=True
        )
        self._thread.start()

    def call(
        self, factory: Callable[[CookieItermLink], Coroutine[Any, Any, _T]], timeout: float
    ) -> _T | None:
        """``factory(link)`` on the link thread, bounded by *timeout*; None on any failure."""
        future = asyncio.run_coroutine_threadsafe(factory(self.link), self._loop)
        try:
            return future.result(timeout)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            future.cancel()
            return None

    def stop(self) -> None:
        """Stop the loop and join the thread (bounded)."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)
