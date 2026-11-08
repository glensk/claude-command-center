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
  group ``fired`` for the next pass. The paste goes through the paste guard
  (:func:`bridge_target.guarded_paste`, tp#1867, the :data:`Paster` seam): tab → per-tab
  lock → revalidate → typing guard → input-box guard → the CAS ``fired → delivering``
  (the *claim*, so it happens inside the lock after the guards) → paste → ``delivered``.
  A HELD paste (``input_not_empty`` — the box holds a draft —, ``user_active``,
  ``input_not_ready``, ``tab_locked``) types nothing and consumes no attempt: the group
  stays ``fired`` (``PassReport.waiting`` + ``PassReport.held``, ``"<group>:<code>"``),
  ``held_since`` is stamped on its first held pass, and after :data:`HELD_NOTICE_SEC`
  held ONE notice goes out through the notifier directly (never ``notified_at``, whose
  dedup would swallow the later delivered notice). A paste whose Return did not go out
  (sender state ``partial``) is never re-pasted: the attempt counts and the group blocks
  at once ("prompt pasted but not submitted in <short>"). A stale tab, a foreign
  foreground process, an unreachable iTerm or a sender that typed nothing count an
  attempt like a failed typer; a session that left the registry, or one with no known
  tab, waits. Typing is ``terminal.send_text_detailed``: the iTerm2 Python API, falling
  back to AppleScript; the channel that delivered lands in ``PassReport.typed_via`` (the
  poller log's ``typed_via=['7:applescript']``).
  AT-LEAST-ONCE: a crash between the keystrokes and the mark can repeat the text
  (the stale-delivery reclaim re-sends it).
* **Closed session** — CAS ``fired → delivering``, then a new tab runs
  ``ccc fire-await <group> <token>``, which claims ``delivering → delivered`` by the
  token and execs ``claude --resume``. AT-MOST-ONCE: the claim precedes the exec.
  A launcher that returns False (or an exec that fails) moves the group back to
  ``fired`` under the same token, at most :data:`MAX_ATTEMPTS` times, then ``blocked``.

* **Fresh group** (``ccc await -F``) — never typed into a tab, live or not: always the
  new-tab path, whose ``fire-await`` execs ``claude "<preamble + prompt>"`` WITHOUT
  ``--resume`` (a new session, so a live old session is no double-resume). Preflight
  skips the two checks that only protect a resume (the old transcript, a pending
  attached prompt); a failed registry read does not hold it back either.

* **Reserved group** — a group whose session is being handed off
  (``await_groups.transfer_id`` names a ``prepared``/``opened`` transfer) is DEFERRED: it
  stays ``fired`` and is delivered after ``continue-session accept`` rebound it to the
  successor, or after a failed transfer released it back to the source. The
  ``fired → delivering`` CAS re-checks the reservation, so a pass racing the reservation
  cannot claim it either.

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
from .await_prompt import delivery_prompt
from .await_store import AwaitGroup
from .bridge_target import PasteResult, default_deps, guarded_paste, held_notice
from .models import short_id

if TYPE_CHECKING:
    from .await_eval import Notifier, PassReport
    from .models import LiveSession, Session
    from .store import Store

MAX_ATTEMPTS = 3
STALE_DELIVERY_SEC = 15 * 60
#: A live delivery held this long by the paste guard sends ONE notice (tp#1867 D4).
HELD_NOTICE_SEC = 10 * 60

#: Pastes into a live session's tab behind the paste guard: ``(live, text, claim)`` →
#: :class:`bridge_target.PasteResult`. *claim* is the ``fired → delivering`` CAS, run
#: inside the tab lock after the guards; ``False`` there means nothing was typed.
Paster = Callable[["LiveSession", str, Callable[[], bool]], PasteResult]
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
    if (
        not group.fresh
        and account_transcript(group.config_dir, group.cwd, group.session_id) is None
    ):
        return "no transcript under the session's own account"
    if not group.cwd or not os.path.isdir(group.cwd):
        return "the session's working directory is missing"
    if not accounts.is_trusted(group.config_dir, group.cwd):
        return "the working directory is not trusted for the session's account"
    if not group.fresh and session.fire_at > 0 and (session.prompt or "").strip():
        return "wait"
    return ""


def live_verdict(group: AwaitGroup, live: LiveSession | None) -> str:
    """``closed`` / ``type`` (idle live tab) / ``wait`` (live but not typeable now).

    A fresh group is always ``closed``: it starts a NEW session in a new tab, so the old
    session's liveness does not matter."""
    if group.fresh or live is None or not live.alive:
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


def _default_paster(live: LiveSession, text: str, claim: Callable[[], bool]) -> PasteResult:
    return guarded_paste(default_deps(), live, text, claim=claim)


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
    paster: Paster
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
        if ctx.store.block_group(
            group.id, reason, ctx.now, from_states=("fired",), token=group.delivery_token
        ):
            ctx.report.blocked_groups.append(group.id)
            ctx.notify(
                group.id,
                f"await group {group.id} fired but is blocked: {reason} "
                f"(`ccc await -R {group.id}` retries)",
            )
        return
    assert session is not None  # preflight refused a missing row
    verdict = live_verdict(group, live)
    if verdict == "wait":
        ctx.report.waiting.append(group.id)
        return
    if verdict == "type":
        assert live is not None  # live_verdict types only into a live entry
        _paste_live(ctx, group, live)
        return
    token = group.delivery_token
    if not ctx.store.mark_delivering(group.id, token, ctx.now):
        return  # another deliverer took it
    kind = _winner_kind(ctx.store, group)
    if ctx.launcher(group.id, token):
        ctx.report.launched.append(group.id)
        what = "a new session starting" if group.fresh else "session resuming"
        ctx.notify(group.id, f"await group {group.id} fired ({kind}); {what} in a new tab")
    else:
        ctx.fail(group, token, "no terminal tab could be opened")


#: Paste-guard refusals after which the session is not (or no longer) typeable: the next
#: pass routes it again through :func:`live_verdict`; no attempt is consumed.
_WAIT_CODES = frozenset({"no_tab", "not_running", "background", "account_conflict"})


def _paste_live(  # pylint: disable=too-many-return-statements  # one per D3 outcome
    ctx: _Ctx, group: AwaitGroup, live: LiveSession
) -> None:
    """Verdict ``type``: paste the prompt behind the paste guard (tp#1867 D3/D4, P1)."""
    token = group.delivery_token
    claimed: list[bool] = []

    def claim() -> bool:
        if not ctx.store.mark_delivering(group.id, token, ctx.now):
            return False
        claimed.append(True)
        return True

    try:
        result = ctx.paster(live, delivery_prompt(group), claim)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # The guard returns its refusals; an exception is a seam that broke. Claimed: the
        # paste may or may not have gone out — a failed typer. Not claimed: nothing typed.
        if claimed:
            ctx.fail(group, token, "typing into the live tab failed")
        else:
            ctx.report.waiting.append(group.id)
        return
    short = short_id(group.session_id).strip()
    if result.held:
        _hold(ctx, group, result, short)
        return
    code = result.refusal
    if code == "claim_lost":
        return  # another deliverer took it
    if code in _WAIT_CODES:
        ctx.report.waiting.append(group.id)
        return
    if code:
        # stale_tab / foreground_not_claude / iterm_unreachable (or an unknown code):
        # nothing typed, but the tab is wrong — count an attempt like a failed typer.
        if ctx.store.mark_delivering(group.id, token, ctx.now):
            ctx.fail(group, token, f"typing into the live tab failed ({code})")
        return
    if result.state == "partial":
        # The paste went out, its Return did not: ccc's own text sits in the box. Never
        # re-paste (it would hold forever, or deliver twice once Return is pressed).
        reason = f"prompt pasted but not submitted in {short}"
        state = ctx.store.revert_delivery(group.id, token, ctx.now, max_attempts=1, reason=reason)
        if state == "blocked":
            ctx.report.blocked_groups.append(group.id)
            ctx.notify(
                group.id, f"await group {group.id} blocked: {reason} — press Return in that tab"
            )
        return
    if result.state == "sent" and ctx.store.mark_delivered(group.id, token, ctx.now):
        kind = _winner_kind(ctx.store, group)
        ctx.report.delivered.append(group.id)
        channel = f" via {result.via}" if result.via else ""
        if channel:
            ctx.report.typed_via.append(f"{group.id}:{result.via}")
        ctx.notify(
            group.id,
            f"await group {group.id} fired ({kind}); event typed into the live session{channel}",
        )
        return
    ctx.fail(group, token, "typing into the live tab failed")  # state none: nothing typed


def _hold(ctx: _Ctx, group: AwaitGroup, result: PasteResult, short: str) -> None:
    """A held paste: the group stays ``fired``, no attempt; one notice after 10 min held."""
    ctx.report.waiting.append(group.id)
    ctx.report.held.append(f"{group.id}:{result.refusal}")
    since = ctx.store.mark_group_held(group.id, ctx.now)
    if (
        since
        and ctx.now - since >= HELD_NOTICE_SEC
        and ctx.store.claim_held_notice(group.id, ctx.now)
    ):
        ctx.notifier(
            "ccc await",
            f"await group {group.id} held: {held_notice(result.refusal, result.data, short)}",
        )


def deliver_pending(  # pylint: disable=too-many-arguments
    store: Store,
    *,
    now: int,
    report: PassReport,
    notifier: Notifier,
    discover: Discover | None = None,
    paster: Paster | None = None,
    launcher: Launcher | None = None,
) -> None:
    """Reclaim stale deliveries, then try every ``fired`` group once."""
    ctx = _Ctx(
        store=store,
        now=now,
        report=report,
        notifier=notifier,
        paster=paster or _default_paster,
        launcher=launcher or _default_launcher,
    )
    for stale in store.stale_deliveries(now, STALE_DELIVERY_SEC):
        ctx.fail(stale, stale.delivery_token, "delivery did not complete")
    fired = []
    for group in store.await_groups_in("fired"):
        if store.is_reserved(group.id):  # a pending handoff: it goes to whoever ends up owning it
            report.waiting.append(group.id)
        else:
            fired.append(group)
    if not fired:
        return
    live_map: dict[str, LiveSession] = {}
    if any(not g.fresh for g in fired):
        try:
            live_map = {ls.session_id: ls for ls in (discover or _default_discover)()}
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            # fail closed: a live session misread as closed would resume twice. A fresh
            # group starts a new session, so it never needs the registry.
            report.waiting.extend(g.id for g in fired if not g.fresh)
            fired = [g for g in fired if g.fresh]
    for group in fired:
        _deliver_one(ctx, group, live_map.get(group.session_id))


def _winner_kind(store: Store, group: AwaitGroup) -> str:
    if group.winner_source_id is None:
        return "?"
    src = store.get_await_source(group.winner_source_id)
    return src.kind if src else "?"
