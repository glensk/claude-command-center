#!/usr/bin/env python3
"""Codex seats whose login is dead: the fix command, and ONE push a day asking for it.

A seat whose ``codex login`` has died (``401 refresh_token_reused``, ``Please log out and
sign in again``, no ``auth.json`` at all) refuses every call until a person logs it in
again. The runner classifies that refusal as ``auth`` (shown everywhere as
``needs login``), records a cooldown entry with scope ``auth`` so later calls skip the
seat at once, and calls :func:`notify_needs_login` — which pushes ONE message per seat
per local day through the push hook, naming the exact ``CODEX_HOME=<home> codex login``
to run. ccc never logs a seat in itself: the login is an interactive browser flow.

The push hook is the Claude Code ``Notification``-style script at
``~/.claude/hooks/ntfy-push.sh`` (JSON ``{"message", "cwd"}`` on stdin); ``$CCC_PUSH_HOOK``
names another executable, and an empty value (or ``0`` / ``off``) disables the push. A
missing hook is a silent no-op — the runner's stderr line still says what to run.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config

# The cooldown scope a dead login is recorded under. Deliberately the pre-existing
# ``auth`` scope (``ccc quota -m -s auth`` writes it too), so the debate gate, the
# cooldown store and older ccc builds keep reading the same entries.
NEEDS_LOGIN_SCOPE = "auth"
NEEDS_LOGIN = "needs login"  # the one display spelling (order / headroom / usage)
ALERTS_NAME = "codex-login-alerts.json"
PUSH_HOOK_ENV = "CCC_PUSH_HOOK"
_DEFAULT_PUSH_HOOK = (".claude", "hooks", "ntfy-push.sh")  # under the home directory
_PUSH_TIMEOUT_S = 15
_DISABLED = ("", "0", "off", "none", "false")


def login_command(home: Path | str) -> str:
    """The exact command that logs seat *home* in again (shell-quoted)."""
    return f"CODEX_HOME={shlex.quote(str(home))} codex login"


def is_needs_login(state: str, scope: str) -> bool:
    """True for a BLOCKED quota row whose cooldown scope says the login is dead."""
    return state == "blocked" and scope == NEEDS_LOGIN_SCOPE


def push_hook() -> Path | None:
    """The executable push hook to call, or ``None`` (disabled, missing, not executable)."""
    raw = os.environ.get(PUSH_HOOK_ENV)
    if raw is not None:
        if raw.strip().lower() in _DISABLED:
            return None
        path = Path(raw.strip()).expanduser()
    else:
        path = Path.home().joinpath(*_DEFAULT_PUSH_HOOK)
    return path if path.is_file() and os.access(path, os.X_OK) else None


def _alerts_path() -> Path:
    """The per-seat "last pushed on" map (beside the cooldown store)."""
    return config.app_home() / ALERTS_NAME


def _local_day(now: int) -> str:
    """The local calendar day of *now* — the dedupe key ("once per seat per day")."""
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")  # noqa: DTZ006  # local day on purpose


def _read_alerts(path: Path) -> dict[str, str]:
    """The alert map, ``{}`` when absent, corrupt or foreign-shaped. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def claim_daily_alert(pid: str, now: int | None = None) -> bool:
    """Win today's push for seat *pid*; False when it already went out today.

    The read-check-write runs under one lock, so two runners that hit the same dead seat
    in the same second push once between them, not twice.
    """
    from . import usage  # pylint: disable=import-outside-toplevel  # cycle via the runner

    now_ts = int(time.time()) if now is None else int(now)
    today = _local_day(now_ts)
    path = _alerts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
        current = _read_alerts(path)
        if current.get(pid) == today:
            return False
        current[pid] = today
        usage._atomic_write_json(path, current)  # noqa: SLF001
    return True


def needs_login_message(label: str, home: Path | str, email: str = "", detail: str = "") -> str:
    """The one-line ask: which seat, why, and the exact command."""
    who = f"{label} ({email})" if email else label
    why = f" ({detail})" if detail else ""
    return f"Codex seat {who} needs login{why}. Run: {login_command(home)}"


def _push(message: str, home: Path | str) -> bool:
    """Hand *message* to the push hook. True when the hook ran and exited 0."""
    hook = push_hook()
    if hook is None:
        return False
    payload = json.dumps({"message": message, "cwd": str(home)})
    try:
        proc = subprocess.run(  # noqa: S603  # a configured local hook, fixed argv
            [str(hook)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=_PUSH_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def notify_needs_login(  # pylint: disable=too-many-arguments
    label: str,
    pid: str,
    home: Path | str,
    *,
    email: str = "",
    detail: str = "",
    now: int | None = None,
) -> bool:
    """Push "seat *label* needs login — run …" at most once per seat per day. Never raises.

    Returns True only when this call claimed today's slot AND the hook accepted the
    message. A seat with no ``pid`` (an unregistered ``$CODEX_HOME``) is never pushed:
    nothing is recorded about it either, so there is no row to point the reader at.
    """
    if not pid:
        return False
    try:
        if not claim_daily_alert(pid, now):
            return False
        return _push(needs_login_message(label, home, email, detail), home)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False  # a notification must never change a run's outcome
