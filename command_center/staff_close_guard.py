#!/usr/bin/env python3
"""The staff close guard: refuse a Stop after ``staff lease -r`` until a close is armed.

An llm-staff agent (the overseer) ends its episode with ``staff lease -r`` and then the
``ccc-mark-done-and-close`` Skill, which runs ``ccc mark-done --close``. When the second
half is forgotten, the idle ``claude`` process keeps counting as a live agent and blocks
every later launch. llm-staff makes the release itself write a pending-close marker,
``$STAFF_STATE/released/<agent>/<session>.json`` (``{agent, owner, generation, nonce,
released_at_iso}``), BEFORE it frees the lease row. This module is the ccc half: the
``release-locks`` Stop hook calls :func:`run` after it claimed the after-turn action.

Per guarded agent (``CC_STAFF_CLOSE_GUARD_AGENTS``, comma list, default ``mac-automator``):

* no marker → one ``lstat()`` and nothing else (no subprocess, no store work);
* marker + a claimed :class:`~command_center.models.CloseClaim`, or a session the store
  marks done → pass (the close is armed or the human said done);
* otherwise the read-only probe ``staff lease -s -j -a <agent> -o <session>`` (2 s): the
  lease held by this session again → the marker is stale, delete it, pass; anything else
  (free, lapsed, held by another owner, timeout, missing ``staff``, bad JSON) → BLOCK —
  re-hold is not proven;
* every block prints ``{"decision": "block", "reason": …}`` to stdout (exit 0) and bumps a
  per-nonce consecutive-block counter in the store (``Store.staff_guard_block``, one
  ``BEGIN IMMEDIATE``, printed before the commit); after :data:`ESCALATE_AFTER` blocks the
  next Stop passes once and logs ``escalated`` — the launch trigger's ``blocked:`` alert is
  then the backstop, so a broken ``ccc mark-done`` cannot burn turns forever.

Every decision taken while a marker exists lands in ``events.log`` as
``staff-close-guard`` with ``agent=… nonce=… decision=…`` — no command or transcript text.
``CC_NO_STAFF_CLOSE_GUARD=1`` turns the guard off; :func:`guard` (the hook's entry point)
swallows every exception (fail open: no block).
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
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import staffrole
from .models import CloseClaim, SwitchClaim

if TYPE_CHECKING:
    from .store import Store

#: Set (to anything but ``0``) to switch the guard off entirely.
KILL_SWITCH_ENV = "CC_NO_STAFF_CLOSE_GUARD"
#: Comma list of the llm-staff agents whose released lease must be followed by a close.
AGENTS_ENV = "CC_STAFF_CLOSE_GUARD_AGENTS"
DEFAULT_AGENTS: tuple[str, ...] = ("mac-automator",)
#: llm-staff's private state directory — the same override and default it uses itself.
STAFF_STATE_ENV = "STAFF_STATE"
DEFAULT_STAFF_STATE = "~/.local/state/llm-staff"
#: Consecutive blocks on one marker nonce before the guard lets one Stop through.
ESCALATE_AFTER = 5
#: Upper bound of the ``staff lease -s -j`` probe (seconds).
PROBE_TIMEOUT_S = 2.0
MARKER_MAX_BYTES = 64 * 1024
LOG_EVENT = "staff-close-guard"

# llm-staff validates agent names and owners with this exact pattern on write and read.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

BLOCK_REASON = (
    "You released the `{agent}` lease (`staff lease -r`), but no close is armed for this "
    "session. Call Skill ccc-mark-done-and-close now; if the episode continues, re-take the "
    "lease first (`staff lease -a {agent} -o {sid}`). "
    "(ccc staff close guard, block #{count}.)"
)

LogEvent = Callable[[str, str, str], None]


class MarkerError(ValueError):
    """A marker file exists but cannot be trusted (symlink, foreign owner, bad content)."""


@dataclass(frozen=True)
class Marker:
    """A validated released-lease marker of *agent* for session *owner*."""

    agent: str
    owner: str
    nonce: str
    path: Path


def valid_id(value: object) -> bool:
    """True when *value* is an llm-staff agent/owner/nonce id (no path syntax possible)."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def disabled() -> bool:
    """True when the kill switch :data:`KILL_SWITCH_ENV` is set."""
    return os.environ.get(KILL_SWITCH_ENV, "").strip() not in ("", "0")


def guarded_agents() -> list[str]:
    """The allowlisted agents, in order, invalid names dropped (unset → the default)."""
    raw = os.environ.get(AGENTS_ENV)
    names = DEFAULT_AGENTS if raw is None else tuple(part.strip() for part in raw.split(","))
    valid = (name for name in names if valid_id(name))
    # Canonical ids (tp#1383): a legacy name in the env and its renamed agent are ONE agent.
    return list(dict.fromkeys(staffrole.canonical_id(name) or name for name in valid))


def staff_state_dir() -> Path:
    """``$STAFF_STATE`` or llm-staff's default state directory."""
    return Path(os.path.expanduser(os.environ.get(STAFF_STATE_ENV) or DEFAULT_STAFF_STATE))


def marker_path(agent: str, sid: str) -> Path:
    """Where llm-staff writes *agent*'s released-lease marker for session *sid*."""
    return staff_state_dir() / "released" / agent / f"{sid}.json"


def _check_dir(path: Path) -> None:
    """The marker's agent directory must be a real directory owned by this user."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise MarkerError("marker directory is not a directory")
    if st.st_uid != os.getuid():
        raise MarkerError("marker directory has a foreign owner")


def _read_bounded(path: Path) -> bytes:
    """The bytes of *path* — no symlink, regular file, owned by this user, size-capped."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MarkerError(f"cannot open marker: {type(exc).__name__}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MarkerError("marker is not a regular file")
        if st.st_uid != os.getuid():
            raise MarkerError("marker has a foreign owner")
        chunks: list[bytes] = []
        size = 0
        while size <= MARKER_MAX_BYTES:
            chunk = os.read(fd, MARKER_MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MARKER_MAX_BYTES:
        raise MarkerError("marker too large")
    return b"".join(chunks)


def read_marker(path: Path, agent: str, sid: str) -> Marker:
    """Read and validate the marker at *path*; raises :class:`MarkerError` when untrusted."""
    _check_dir(path.parent)
    raw = _read_bounded(path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError alike
        raise MarkerError("marker is not JSON") from exc
    if not isinstance(data, dict):
        raise MarkerError("marker is not a JSON object")
    if data.get("agent") != agent or data.get("owner") != sid:
        raise MarkerError("marker names another agent or owner")
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or not valid_id(nonce):
        raise MarkerError("marker nonce is invalid")
    return Marker(agent=agent, owner=sid, nonce=nonce, path=path)


def lease_reheld(agent: str, sid: str) -> bool:
    """True only when ``staff lease -s -j`` PROVES *agent*'s lease is held by *sid* again.

    Every failure — ``staff`` not found, a timeout (:data:`PROBE_TIMEOUT_S`), a non-zero
    exit, output that is not one JSON object — answers ``False``: re-hold not proven.
    """
    from extdeps import resolve  # lazy: only a marker hit pays for the probe

    from .checks import run_structured
    from .external_deps import EXTERNAL_DEPS

    exe = resolve(EXTERNAL_DEPS["staff"])
    if not exe:
        return False
    result = run_structured(
        [exe, "lease", "-s", "-j", "-a", agent, "-o", sid],
        timeout=PROBE_TIMEOUT_S,
        max_bytes=MARKER_MAX_BYTES,
    )
    if result.timed_out or result.truncated or result.exit != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("held_by_owner") is True


def _remove_stale_marker(marker: Marker) -> None:
    """Delete *marker*'s file — only while it still carries the same nonce."""
    try:
        current = read_marker(marker.path, marker.agent, marker.owner)
    except (MarkerError, OSError):
        return
    if current.nonce != marker.nonce:
        return  # a newer release wrote a fresh marker meanwhile: not ours to drop
    try:
        os.unlink(marker.path)
    except OSError:
        pass


def _emit_block(agent: str, sid: str, count: int) -> None:
    """Print the Stop-hook block decision (exit stays 0) and flush it before the commit."""
    reason = BLOCK_REASON.format(agent=agent, sid=sid, count=count, limit=ESCALATE_AFTER)
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    sys.stdout.flush()


def _log(log_event: LogEvent, sid: str, agent: str, nonce: str, decision: str) -> None:
    log_event(sid, LOG_EVENT, f"agent={agent} nonce={nonce} decision={decision}")


def decide(
    store: Store,
    sid: str,
    agent: str,
    claim: SwitchClaim | CloseClaim | None,
    log_event: LogEvent,
) -> str:
    """The guard's verdict for one agent whose marker EXISTS; prints the block itself.

    Returns ``"block"``, ``"escalated"``, ``"pass-close"``, ``"pass-done"``,
    ``"pass-reheld"`` or ``"invalid-marker"`` (an untrusted marker never blocks).
    """
    path = marker_path(agent, sid)
    try:
        marker = read_marker(path, agent, sid)
    except (MarkerError, OSError):
        _log(log_event, sid, agent, "-", "invalid-marker")
        return "invalid-marker"
    nonce = marker.nonce
    verdict = ""
    if isinstance(claim, CloseClaim):
        verdict = "pass-close"
    else:
        session = store.get(sid)
        if session is not None and session.done:
            verdict = "pass-done"
        elif lease_reheld(agent, sid):
            _remove_stale_marker(marker)
            verdict = "pass-reheld"
    if verdict:
        store.staff_guard_reset(sid, agent, nonce)
    else:
        verdict, _count = store.staff_guard_block(
            sid,
            agent,
            nonce,
            limit=ESCALATE_AFTER,
            emit=lambda count: _emit_block(agent, sid, count),
        )
    _log(log_event, sid, agent, nonce, verdict)
    return verdict


def run(
    store: Store,
    sid: str,
    claim: SwitchClaim | CloseClaim | None,
    log_event: LogEvent,
) -> str:
    """Run the guard for one Stop of *sid*; returns the deciding verdict (``""`` = none).

    Called by ``release-locks`` AFTER the close/switch claim was handled, so it can never
    keep ``close-now`` from firing. At most ONE block is printed per Stop: the first
    guarded agent that blocks ends the loop. Raises on unexpected errors — :func:`guard`
    swallows them (no block).
    """
    if disabled() or not valid_id(sid):
        return ""
    verdict = ""
    for agent in guarded_agents():
        try:
            os.lstat(marker_path(agent, sid))
        except FileNotFoundError:
            continue
        verdict = decide(store, sid, agent, claim, log_event)
        if verdict == "block":
            break
    return verdict


def guard(
    store: Store,
    sid: str,
    claim: SwitchClaim | CloseClaim | None,
    log_event: LogEvent,
) -> None:
    """:func:`run` for the ``release-locks`` hook — never raises: any error means no block.

    A failure AFTER a block was printed (the counter's commit) leaves that block standing
    and the count rolled back: the counter under-counts, a shown block is never retracted.
    """
    try:
        run(store, sid, claim, log_event)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass
