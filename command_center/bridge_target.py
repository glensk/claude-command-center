#!/usr/bin/env python3
"""Who a voice-bridge mutation is aimed at, and the checks right before the first byte.

``ccc send`` / ``ccc answer`` type into a live Claude Code tab. Before ANY byte goes out
(:func:`revalidate`, called inside the per-tab mutation lock):

1. the session is in the live registry, alive, interactive (``bg`` / ``fleet`` sessions
   — and background jobs only ``claude agents --json`` lists — are listed and read, never
   messaged: code ``background``), and not live under two accounts at once;
2. the tab (the stored iTerm session id, or — when that one is missing or wrong — the
   ONE iTerm session on the registry pid's tty, spike S-IDENT, :func:`resolve_tab`) has
   the registry pid's tty, and its foreground job (``jobPid``) is that claude process or
   one of its children — so the keystrokes cannot land in a shell, an editor or another
   session;
3. the raw registry status is read fresh and mapped to the §6 set (``shell`` — a shell
   tool running — is ``busy``; :func:`adapters.claude_agents.contract_status`); the
   caller decides what it allows.

Every collaborator that touches the machine (registry, ``ps``, iTerm, the clock, the
sender) is a field of :class:`BridgeDeps`, so tests drive the whole path with fakes.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import fcntl
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bridge_json import BridgeError

if TYPE_CHECKING:
    from .adapters.claude_agents import AgentEntry
    from .models import LiveSession
    from .store import Store
    from .tab_titles import ItermPane

#: How long ``ccc send`` / ``ccc answer`` wait for another mutation of the same tab.
LOCK_WAIT_SEC = 3.0


def _default_discover() -> list[LiveSession]:
    from .adapters.claude import ClaudeAdapter

    return list(ClaudeAdapter().discover())


def _default_agents() -> list[AgentEntry]:
    from .adapters import claude_agents

    return claude_agents.list_account_agents()


def _default_transcript(cwd: str, session_id: str, config_dir: str) -> Path | None:
    from .adapters.claude import ClaudeAdapter

    return ClaudeAdapter().transcript_path(cwd, session_id, config_dir or None)


def _default_tab_vars(iterm_session_id: str) -> dict[str, str] | None:
    from . import terminal

    return terminal.iterm_session_vars(iterm_session_id)


def _default_ps() -> dict[int, Any]:
    from . import terminal

    return terminal.ps_table()


def _default_panes() -> list[ItermPane] | None:
    from . import tab_titles

    return tab_titles.read_panes()


def _default_send_text(iterm_session_id: str, text: str) -> tuple[str, str]:
    from . import terminal

    return terminal.send_text_detailed(iterm_session_id, text)


def _default_send_keys(iterm_session_id: str, keys: Sequence[str]) -> str:
    from . import terminal

    return terminal.send_keys_via(iterm_session_id, keys)


def _default_store() -> Store:
    from .store import Store

    return Store()


def _default_llm(prompt: str, note: str, timeout: float) -> str | None:
    from . import llm
    from .config import load_config

    command = load_config().llm_custom_command.strip()
    if not command:
        return None
    return llm.run_custom(
        prompt, command, timeout=max(1, int(timeout)), purpose="voice-brief", note=note
    )


def _default_lock_dir() -> Path:
    from . import config

    return config.app_home() / "locks"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class BridgeDeps:  # pylint: disable=too-many-instance-attributes  # one seam per side effect
    """Every side effect of the bridge commands (replaced wholesale in tests)."""

    discover: Callable[[], list[LiveSession]] = _default_discover
    agents: Callable[[], list[AgentEntry]] = _default_agents
    transcript: Callable[[str, str, str], Path | None] = _default_transcript
    tab_vars: Callable[[str], dict[str, str] | None] = _default_tab_vars
    ps_table: Callable[[], dict[int, Any]] = _default_ps
    panes: Callable[[], list[ItermPane] | None] = _default_panes
    send_text: Callable[[str, str], tuple[str, str]] = _default_send_text
    send_keys: Callable[[str, Sequence[str]], str] = _default_send_keys
    store: Callable[[], Store] = _default_store
    llm: Callable[[str, str, float], str | None] = _default_llm
    lock_dir: Callable[[], Path] = _default_lock_dir
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now_ms: Callable[[], int] = _now_ms
    extra: dict[str, Any] = field(default_factory=dict)


#: The factory every CLI entry point calls; tests monkeypatch it.
def default_deps() -> BridgeDeps:
    return BridgeDeps()


def find_live(deps: BridgeDeps, session_id: str) -> LiveSession | None:
    """The registry entry of *session_id* (``None`` when it is not registered)."""
    return next((s for s in deps.discover() if s.session_id == session_id), None)


def require_live(deps: BridgeDeps, session_id: str) -> LiveSession:
    """The live, interactive, unambiguous registry entry of *session_id* — or a refusal."""
    live = find_live(deps, session_id)
    if live is None or not live.alive:
        # A background job whose worker is not running has no registry entry: only the
        # CLI's roster knows it — still a background session, never messaged (D4).
        if any(a.background and a.session_id == session_id for a in deps.agents()):
            raise BridgeError(
                "background", f"session {session_id} is a background session (read-only)"
            )
        raise BridgeError("not_running", f"session {session_id} is not running")
    if live.conflict:
        raise BridgeError(
            "account_conflict", f"session {session_id} is live under two accounts at once"
        )
    if live.kind != "interactive":
        raise BridgeError(
            "background", f"session {session_id} is a {live.kind} session (read-only)"
        )
    return live


def resolve_tab(
    stored: str | None, pid: int, ps: dict[int, Any], panes: list[ItermPane] | None
) -> tuple[str | None, bool]:
    """``(iterm_session_id, validated)`` for a session whose process is *pid* (S-IDENT).

    Validated = the pid's controlling tty equals the tab's tty. The stored id wins when it
    validates; otherwise the ONE iTerm session on the pid's tty is the tab (a stored id
    that is missing, closed or reused by another session is resolved this way). With no
    match, the stored id is reported unvalidated.
    """
    from . import snapshot, tab_titles, terminal

    if panes is None or pid <= 0:
        return (stored or None), False
    pid_tty = terminal.pid_tty(pid, ps)
    if not pid_tty:
        return (stored or None), False
    want = tab_titles.uuid_of(stored)
    for pane in panes:
        if want and pane.uuid == want and snapshot.normalize_tty(pane.tty) == pid_tty:
            return stored, True
    on_tty = [p for p in panes if snapshot.normalize_tty(p.tty) == pid_tty]
    if len(on_tty) == 1:
        return on_tty[0].uuid, True
    return (stored or None), False


def tab_for(deps: BridgeDeps, live: LiveSession) -> str:
    """The iTerm session id to type into for *live* (refusal when none can be found).

    The stored id when its tab is on the session's tty; else the one tab on that tty
    (:func:`resolve_tab`). When iTerm cannot be listed the stored id is returned as is —
    :func:`revalidate` checks it right before the first byte either way.
    """
    with deps.store() as store:
        row = store.get(live.session_id)
    stored = (row.iterm_session_id or "").strip() if row is not None else ""
    tab, _validated = resolve_tab(stored or None, live.pid, deps.ps_table(), deps.panes())
    if not tab:
        raise BridgeError("no_tab", f"no iTerm tab is known for session {live.session_id}")
    return tab


@dataclass(frozen=True)
class Target:
    """A validated live session + its tab, as seen right before typing."""

    live: LiveSession
    iterm_session_id: str
    tty: str
    status: str  # the §6 status (busy | idle | waiting | unknown)
    raw_status: str = ""  # the registry's own word (busy | shell | idle | waiting | …)


def revalidate(deps: BridgeDeps, session_id: str, iterm_session_id: str) -> Target:
    """Fresh registry + ``ps`` + iTerm reads; a refusal unless the tab is THIS session's."""
    from . import snapshot, terminal

    live = require_live(deps, session_id)
    table = deps.ps_table()
    want = terminal.pid_tty(live.pid, table) if table else ""
    if not want:
        raise BridgeError("stale_tab", f"session {session_id}: no tty for pid {live.pid}")
    tab = deps.tab_vars(iterm_session_id)
    if tab is None:
        raise BridgeError("iterm_unreachable", "iTerm2 is not reachable over the Python API")
    have = snapshot.normalize_tty(tab.get("tty"))
    if not have or have != want:
        raise BridgeError(
            "stale_tab",
            f"the stored tab belongs to {have or 'no tty'}, session {session_id} runs on {want}",
        )
    job = str(tab.get("jobPid") or "")
    if not job.isdigit() or not terminal.pid_descends_from(int(job), live.pid, table):
        raise BridgeError(
            "foreground_not_claude",
            f"the tab's foreground process is not session {session_id}'s claude",
        )
    from .adapters.claude_agents import contract_status

    raw = str(live.raw_status or "")
    return Target(live, iterm_session_id, have, contract_status(raw), raw or "unknown")


@contextmanager
def tab_lock(deps: BridgeDeps, iterm_session_id: str) -> Iterator[None]:
    """The per-tab mutation lock (an ``flock`` file under ccc's state dir).

    One send/answer per tab at a time across processes; waits up to
    :data:`LOCK_WAIT_SEC`, then refuses with ``tab_locked``.
    """
    uuid = iterm_session_id.split(":")[-1].strip().upper() or "unknown"
    safe = "".join(c for c in uuid if c.isalnum() or c in "-_") or "unknown"
    directory = deps.lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"tab-{safe}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = deps.monotonic() + LOCK_WAIT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if deps.monotonic() >= deadline:
                    raise BridgeError(
                        "tab_locked", "another send/answer to this tab is in progress"
                    ) from exc
                deps.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
