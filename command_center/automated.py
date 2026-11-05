#!/usr/bin/env python3
"""The session-start choke point for automated callers (``AI_AUTOMATED``).

An automated launcher (a LaunchAgent, a dispatcher) never starts a Claude session on its
own: it registers a draft, and the user's click starts it. The launcher says so explicitly
by exporting ``AI_AUTOMATED`` in its environment:

* ``AI_AUTOMATED=1`` (also ``true``/``yes``/``enforce``) — **enforce**: every start path
  refuses (``open-job``/``start-job``/``resume``/``resume-job``/``park`` exit
  :data:`EXIT_SKIPPED`; the in-process seams in :mod:`.terminal` launch nothing),
  ``new-job`` still registers but strips fire arming and stamps the job
  ``registered_by = automated``, and each refusal is one ``automated-start`` line in
  ``events.log``.
* ``AI_AUTOMATED=observe`` — **observe**: nothing changes; each would-be refusal is logged
  as ``automated-observe`` so a rollout can see what enforce would do.
* unset / anything else — off.

The classifier keys ONLY on that explicit value. A start with no controlling terminal and
no ``CLAUDECODE`` is NOT automated: the Obsidian start button, the phone ``launch`` toggle
and the daemon have no tty and are the user's own clicks or arming.

A stamped job is never auto-started by the daemon's reset fire or the job file's
``launch`` toggle; an explicit start (TUI, ``ccc open-job``/``start-job``, the Obsidian
button) by the user does start it.
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
import time
from typing import Any

from . import config

ENV = "AI_AUTOMATED"
# Optional caller name for the event log; launchd's own label is the fallback.
LAUNCHER_ENV = "AI_AUTOMATED_LAUNCHER"
MODE_OFF = ""
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
_ENFORCE_VALUES = frozenset({"1", "true", "yes", "enforce"})
# The ``registered_by`` stamp of a job an automated caller registered.
REGISTERED_BY_AUTOMATED = "automated"
# EX_TEMPFAIL: "skipped, not failed" — a caller maps it to its own drafted outcome.
EXIT_SKIPPED = 75
SKIP_MESSAGE = "skipped: automated session start; registered as a draft"
EVENT_REFUSED = "automated-start"
EVENT_OBSERVED = "automated-observe"
# A seat call (Codex run, Claude probe) an automated caller made (observe) or was refused
# (enforce) — the ``seat_violation`` tripwire of tp#1697 Design §2.
EVENT_SEAT_VIOLATION = "seat-violation"
EVENT_SEAT_REFUSED = "seat-refused"
SEAT_SKIP_MESSAGE = "skipped: automated caller (AI_AUTOMATED); no seat call"
WAITING_NOTE = "waiting for your start"


def mode(environ: dict[str, str] | None = None) -> str:
    """The caller's ``AI_AUTOMATED`` mode: :data:`MODE_ENFORCE`, :data:`MODE_OBSERVE` or off."""
    env = os.environ if environ is None else environ
    value = (env.get(ENV) or "").strip().lower()
    if value in _ENFORCE_VALUES:
        return MODE_ENFORCE
    if value == MODE_OBSERVE:
        return MODE_OBSERVE
    return MODE_OFF


def enforced() -> bool:
    """True when this process runs under ``AI_AUTOMATED`` enforce."""
    return mode() == MODE_ENFORCE


def is_automated() -> bool:
    """True under ``AI_AUTOMATED`` enforce OR observe (a seat probe is skipped in both)."""
    return mode() != MODE_OFF


def launcher_name() -> str:
    """Who is calling, for the event log: ``AI_AUTOMATED_LAUNCHER``, launchd's label, or ``?``."""
    return (
        os.environ.get(LAUNCHER_ENV, "").strip()
        or os.environ.get("XPC_SERVICE_NAME", "").strip()
        or "?"
    )


def is_stamped(session: Any) -> bool:
    """True for a job an automated caller registered (never auto-started)."""
    return getattr(session, "registered_by", "") == REGISTERED_BY_AUTOMATED


def _job_facts(session_id: str) -> tuple[str, str]:
    """``(job key, cwd)`` of *session_id* from the store; best effort, never raises."""
    try:
        from .store import Store  # pylint: disable=import-outside-toplevel

        with Store() as store:
            session = store.get(session_id) if session_id else None
    except Exception:  # pylint: disable=broad-exception-caught
        return session_id, ""
    if session is None:
        return session_id, ""
    return (session.idempotency_key or session_id), (session.cwd or "")


def log_event(event: str, session_id: str, surface: str, *, key: str = "", cwd: str = "") -> None:
    """Append one ``{launcher, job key, cwd, ts}`` line to ``app_home()/events.log``.

    Never raises: logging must not turn a refusal into a crash.
    """
    if not key and not cwd:
        key, cwd = _job_facts(session_id)
    detail = {
        "launcher": launcher_name(),
        "job_key": key or session_id,
        "cwd": cwd,
        "ts": int(time.time()),
        "surface": surface,
        "mode": mode(),
    }
    try:
        path = config.app_home() / "events.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{session_id}\t{event}\t{json.dumps(detail)}\n")
    except OSError:
        pass


def refuse_start(session_id: str, surface: str, *, key: str = "", cwd: str = "") -> bool:
    """The start check every session-start path runs: True means do NOT start.

    Enforce: log the refusal and return True. Observe: log what enforce would do and
    return False. Off: return False without touching anything.
    """
    current = mode()
    if current == MODE_OFF:
        return False
    if current == MODE_OBSERVE:
        log_event(EVENT_OBSERVED, session_id, surface, key=key, cwd=cwd)
        return False
    log_event(EVENT_REFUSED, session_id, surface, key=key, cwd=cwd)
    return True
