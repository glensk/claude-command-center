"""``claude agents --json`` reader and Claude Code's status vocabulary (voice bridge, §6).

The registry (``<config>/sessions/<pid>.json``, read by ``adapters/claude.py``) only holds
sessions with a live process. Background sessions (``claude --bg``) are daemon jobs: one
whose worker is not running has NO registry entry, so the CLI's own roster is the one
place that lists every one of them. Facts it relies on (Claude Code 2.1.295, read from
the CLI's ``printAgentsJson``):

- the output is a JSON list; every entry has ``kind`` (``interactive`` | ``background``),
  ``cwd``, ``startedAt`` (epoch ms), mostly ``sessionId`` and ``name``;
- interactive entries carry ``pid`` and ``status`` — the CLI folds the registry status
  to ``idle`` / ``waiting`` / ``busy`` (``shell``, a running shell tool, becomes ``busy``);
- background entries carry ``state`` (``working`` | ``blocked`` | ``done`` | ``failed``
  | ``stopped``; without ``--all`` only ``working`` / ``blocked`` jobs without a live
  worker are listed) and ``pid`` / ``status`` only while their worker is in the registry;
- registry ``kind`` is ``interactive`` | ``bg`` | ``daemon`` | ``daemon-worker``; only
  the first two are sessions, and registry ``status`` is ``busy`` | ``shell`` | ``idle``
  | ``waiting``.

Every account is asked separately (``CLAUDE_CONFIG_DIR`` pinned per account, in
parallel); an account whose call fails, times out or prints something else contributes
nothing — the registry still lists its live sessions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import BUSY_RAW_STATUSES

_LOG = logging.getLogger(__name__)

#: Seconds one ``claude agents --json`` call may take (it answers in ~0.8 s).
AGENTS_TIMEOUT_SEC = 2.5

#: The §6 ``status`` set of ``ccc sessions -j``.
CONTRACT_STATUSES = frozenset({"busy", "idle", "waiting", "blocked", "unknown"})
#: Registry ``status`` / interactive ``claude agents`` ``status`` → §6 status.
REGISTRY_STATUS: dict[str, str] = {
    **dict.fromkeys(BUSY_RAW_STATUSES, "busy"),
    "idle": "idle",
    "waiting": "waiting",
    "blocked": "blocked",  # not written by 2.1.295; §6 refuses it, so keep it if it comes
}
#: Background ``claude agents`` ``state`` → §6 status.
BACKGROUND_STATE: dict[str, str] = {
    "working": "busy",
    "blocked": "blocked",  # needs input / approval, or a login / billing / rate limit
    "failed": "blocked",  # gave up or errored: needs a person before it continues
    "done": "idle",
    "stopped": "idle",
}
#: Registry kinds that are sessions (``daemon`` / ``daemon-worker`` are the bg supervisor).
SESSION_KINDS = frozenset({"interactive", "bg", "fleet"})


def contract_status(raw: str | None) -> str:
    """The §6 status of a registry / interactive-agents *raw* status (``unknown`` if new)."""
    return REGISTRY_STATUS.get(str(raw or "").strip().lower(), "unknown")


def background_status(state: str | None, raw: str | None = None) -> str:
    """The §6 status of a background session: its ``state``, else its registry status."""
    got = BACKGROUND_STATE.get(str(state or "").strip().lower())
    return got if got is not None else contract_status(raw)


@dataclass(frozen=True)
class AgentEntry:  # pylint: disable=too-many-instance-attributes  # one field per CLI key
    """One ``claude agents --json`` entry, stamped with the account it came from."""

    config_dir: str
    session_id: str
    kind: str  # interactive | background
    pid: int = 0
    cwd: str = ""
    name: str = ""
    status: str = ""  # interactive (and a background job with a live worker)
    state: str = ""  # background only
    started_at: int = 0

    @property
    def background(self) -> bool:
        return self.kind == "background"


def parse_agents(text: str, config_dir: str) -> list[AgentEntry]:
    """The entries of one ``claude agents --json`` output (``[]`` when it is not a list)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[AgentEntry] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("sessionId"):
            continue
        out.append(_entry(item, config_dir))
    return out


def _entry(item: dict[str, Any], config_dir: str) -> AgentEntry:
    def _int(key: str) -> int:
        value = item.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _str(key: str) -> str:
        value = item.get(key)
        return value if isinstance(value, str) else ""

    return AgentEntry(
        config_dir=config_dir,
        session_id=_str("sessionId"),
        kind="background" if _str("kind") == "background" else "interactive",
        pid=_int("pid"),
        cwd=_str("cwd"),
        name=_str("name"),
        status=_str("status"),
        state=_str("state"),
        started_at=_int("startedAt"),
    )


def claude_binary() -> str | None:
    """The ``claude`` executable (``$PATH``, else the native installer's ``~/.local/bin``)."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if os.access(local, os.X_OK) else None


def _run_one(config_dir: str, binary: str, timeout: float) -> str:
    from .. import accounts  # pylint: disable=import-outside-toplevel

    try:
        proc = subprocess.run(  # noqa: S603  # fixed argv, no shell
            [binary, "agents", "--json"],
            env=accounts.launch_env(config_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.warning("claude agents --json failed for %s: %s", config_dir, exc)
        return ""
    if proc.returncode != 0:
        _LOG.warning("claude agents --json exited %s for %s", proc.returncode, config_dir)
        return ""
    return proc.stdout


def list_agents(
    config_dirs: Iterable[str],
    *,
    timeout: float = AGENTS_TIMEOUT_SEC,
    runner: Callable[[str], str] | None = None,
) -> list[AgentEntry]:
    """Every ``claude agents --json`` entry of every account in *config_dirs* (in parallel).

    *runner* maps a config dir to the command's stdout (tests); by default the real
    ``claude`` runs with that account's environment. Failures yield no entries.
    """
    dirs = [str(d) for d in config_dirs if str(d)]
    if not dirs:
        return []
    if runner is None:
        binary = claude_binary()
        if binary is None:
            _LOG.warning("claude agents --json skipped: no claude executable found")
            return []

        def runner(config_dir: str) -> str:
            return _run_one(config_dir, binary, timeout)

    with ThreadPoolExecutor(max_workers=len(dirs)) as pool:
        outputs = list(pool.map(runner, dirs))
    return [entry for d, text in zip(dirs, outputs, strict=True) for entry in parse_agents(text, d)]


def list_account_agents() -> list[AgentEntry]:
    """:func:`list_agents` over every configured Claude account (``claude_accounts``)."""
    from .. import config  # pylint: disable=import-outside-toplevel

    return list_agents(str(d) for d in config.claude_config_dirs().values())
