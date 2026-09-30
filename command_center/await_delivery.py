#!/usr/bin/env python3
"""Delivering a fired ``ccc await`` group: resume THAT session with the event.

The group is an outbox row keyed by its ``delivery_token``:

* **Preflight** (before any claim) — the session is not done/archived; its transcript
  exists in the SNAPSHOT account's own ``projects/`` dir (:func:`account_transcript`,
  no cross-account fallback: resuming under another seat would fork the conversation
  into that seat's tree); the cwd is already trusted for that account
  (``accounts.is_trusted`` — trust was ensured at ARM time, a deliberate user act;
  automation never grants it); and the session has no pending attached prompt
  (``fire_at > 0``) — delivery then WAITS, it never overwrites. A failed preflight
  BLOCKS the group with a reason and one notification; ``ccc await -R`` recovers it.
* **Live session** — typed into its tab only when it is alive, interactive, not a
  D9 conflict, under the snapshot account and ``idle``; any other live state leaves the
  group ``fired`` for the next pass. CAS ``fired → delivering``, type, ``delivered``.
  Typing is ``terminal.send_text_via``: the iTerm2 Python API, falling back to
  AppleScript; the channel that delivered lands in ``PassReport.typed_via`` (the
  poller log's ``typed_via=['7:applescript']``).
  AT-LEAST-ONCE: a crash between the keystrokes and the mark can repeat the text
  (the stale-delivery reclaim re-sends it).
* **Closed session** — CAS ``fired → delivering``, then a new tab runs
  ``ccc fire-await <group> <token>``, which claims ``delivering → delivered`` by the
  token and execs ``claude --resume``. AT-MOST-ONCE: the claim precedes the exec.
  A launcher that returns False (or an exec that fails) moves the group back to
  ``fired`` under the same token, at most :data:`MAX_ATTEMPTS` times, then ``blocked``.

A ``delivering`` row nobody finished within :data:`STALE_DELIVERY_SEC` (a crash
between the CAS and the send, a tab that never ran ``fire-await``) is handed back the
same way. When the live-session registry cannot be read, NOTHING is delivered that
pass: a live session misread as closed would be resumed a second time.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import accounts
from .await_prompt import compose_prompt
from .await_store import AwaitGroup

if TYPE_CHECKING:
    from .await_eval import Notifier, PassReport
    from .models import LiveSession, Session
    from .store import Store

MAX_ATTEMPTS = 3
STALE_DELIVERY_SEC = 15 * 60

#: Types into a live tab: truthy on success — the channel name (``python-api`` /
#: ``applescript``) when known, else ``True``.
Typer = Callable[[str, str], bool | str]
Launcher = Callable[[int, str], bool]
Discover = Callable[[], list["LiveSession"]]


def account_transcript(config_dir: str, cwd: str, session_id: str) -> Path | None:
    """The session's transcript in *config_dir*'s OWN ``projects/`` dir, or ``None``.

    Exact munged-cwd path first, then a ``*/<id>.jsonl`` glob inside that ONE dir —
    unlike ``ClaudeAdapter.transcript_path``, which falls back across every account.
    """
    home = Path(config_dir).expanduser() if config_dir else accounts.default_config_dir()
    projects = home / "projects"
    if cwd:
        exact = projects / cwd.replace("/", "-") / f"{session_id}.jsonl"
        if exact.is_file():
            return exact
    try:
        hits = sorted(projects.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return hits[0] if hits else None


def preflight(  # pylint: disable=too-many-return-statements  # one per check
    group: AwaitGroup, session: Session | None
) -> str:
    """Why *group* cannot be delivered right now (``""`` = go, ``"wait"`` = retry later)."""
    if session is None:
        return "the session row is gone"
    if session.done or session.archived:
        return "the session is done"
    if not group.config_dir and accounts.is_multi_account():
        return "the session's account is unknown"
    if account_transcript(group.config_dir, group.cwd, group.session_id) is None:
        return "no transcript under the session's own account"
    if not group.cwd or not os.path.isdir(group.cwd):
        return "the session's working directory is missing"
    if not accounts.is_trusted(group.config_dir, group.cwd):
        return "the working directory is not trusted for the session's account"
    if session.fire_at > 0 and (session.prompt or "").strip():
        return "wait"
    return ""


def live_verdict(group: AwaitGroup, live: LiveSession | None) -> str:
    """``closed`` / ``type`` (idle live tab) / ``wait`` (live but not typeable now)."""
    if live is None or not live.alive:
        return "closed"
    typeable = all(
        (
            live.kind == "interactive",
            live.entrypoint == "cli",
            not live.conflict,
            bool(live.config_dir),
            live.raw_status == "idle",
        )
    )
    if typeable and accounts.same_config_dir(live.config_dir, group.config_dir):
        return "type"
    return "wait"


def _default_discover() -> list[LiveSession]:
    from .adapters.claude import ClaudeAdapter  # pylint: disable=import-outside-toplevel

    return list(ClaudeAdapter().discover())


def _default_typer(iterm_session_id: str, text: str) -> str:
    from . import terminal  # pylint: disable=import-outside-toplevel

    return terminal.send_text_via(iterm_session_id, text)


def _default_launcher(group_id: int, token: str) -> bool:
    from . import terminal  # pylint: disable=import-outside-toplevel

    return terminal.fire_await_in_new_tab(group_id, token)


@dataclasses.dataclass
class _Ctx:
    """One delivery pass's collaborators."""

    store: Store
    now: int
    report: PassReport
    notifier: Notifier
    typer: Typer
    launcher: Launcher

    def notify(self, group_id: int, message: str) -> None:
        from .await_eval import notify_group  # pylint: disable=import-outside-toplevel

        notify_group(self.store, group_id, message, self.now, self.notifier)

    def fail(self, group: AwaitGroup, token: str, reason: str) -> None:
        """Hand a claimed delivery back; notify when that exhausted the retries."""
        state = self.store.revert_delivery(
            group.id, token, self.now, max_attempts=MAX_ATTEMPTS, reason=reason
        )
        if state == "blocked":
            self.report.blocked_groups.append(group.id)
            self.notify(
                group.id,
                f"await group {group.id} blocked after {MAX_ATTEMPTS} delivery attempts "
                f"(`ccc await -R {group.id}` retries)",
            )


def _deliver_one(ctx: _Ctx, group: AwaitGroup, live: LiveSession | None) -> None:
    session = ctx.store.get(group.session_id)
    reason = preflight(group, session)
    if reason == "wait":
        ctx.report.waiting.append(group.id)
        return
    if reason:
        if ctx.store.block_group(group.id, reason, ctx.now, from_states=("fired",)):
            ctx.report.blocked_groups.append(group.id)
            ctx.notify(
                group.id,
                f"await group {group.id} fired but is blocked: {reason} "
                f"(`ccc await -R {group.id}` retries)",
            )
        return
    assert session is not None  # preflight refused a missing row
    verdict = live_verdict(group, live)
    tab = session.iterm_session_id or ""
    if verdict == "wait" or (verdict == "type" and not tab):
        ctx.report.waiting.append(group.id)
        return
    token = group.delivery_token
    if not ctx.store.mark_delivering(group.id, token, ctx.now):
        return  # another deliverer took it
    kind = _winner_kind(ctx.store, group)
    if verdict == "type":
        prompt = compose_prompt(group.prompt_template, group.event_payload)
        via = ctx.typer(tab, prompt)
        if via and ctx.store.mark_delivered(group.id, token, ctx.now):
            ctx.report.delivered.append(group.id)
            channel = f" via {via}" if isinstance(via, str) else ""
            if channel:
                ctx.report.typed_via.append(f"{group.id}:{via}")
            ctx.notify(
                group.id,
                f"await group {group.id} fired ({kind}); event typed into the live "
                f"session{channel}",
            )
        else:
            ctx.fail(group, token, "typing into the live tab failed")
        return
    if ctx.launcher(group.id, token):
        ctx.report.launched.append(group.id)
        ctx.notify(
            group.id, f"await group {group.id} fired ({kind}); session resuming in a new tab"
        )
    else:
        ctx.fail(group, token, "no terminal tab could be opened")


def deliver_pending(  # pylint: disable=too-many-arguments
    store: Store,
    *,
    now: int,
    report: PassReport,
    notifier: Notifier,
    discover: Discover | None = None,
    typer: Typer | None = None,
    launcher: Launcher | None = None,
) -> None:
    """Reclaim stale deliveries, then try every ``fired`` group once."""
    ctx = _Ctx(
        store=store,
        now=now,
        report=report,
        notifier=notifier,
        typer=typer or _default_typer,
        launcher=launcher or _default_launcher,
    )
    for stale in store.stale_deliveries(now, STALE_DELIVERY_SEC):
        ctx.fail(stale, stale.delivery_token, "delivery did not complete")
    fired = store.await_groups_in("fired")
    if not fired:
        return
    try:
        live_map = {ls.session_id: ls for ls in (discover or _default_discover)()}
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        report.waiting.extend(g.id for g in fired)
        return  # fail closed: a live session misread as closed would resume twice
    for group in fired:
        _deliver_one(ctx, group, live_map.get(group.session_id))


def _winner_kind(store: Store, group: AwaitGroup) -> str:
    if group.winner_source_id is None:
        return "?"
    src = store.get_await_source(group.winner_source_id)
    return src.kind if src else "?"
