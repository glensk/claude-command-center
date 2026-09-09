#!/usr/bin/env python3
"""Move a rate-limit-halted session to an account that still has quota — automatically.

When a Claude account hits its 5-hour session limit (or its weekly one) mid-turn, the
session stalls: its last main-chain assistant record is an ``isApiErrorMessage`` reading
"You've hit your session limit · resets 6:40pm". ``ccc switch-account <label> -N`` already
relaunches such a session under the other account IN THE SAME TAB with the conversation
intact (that is what the ``/cwork-to-cpriv`` slash command does); this module decides when
to do that by itself, so the halt costs seconds of idling instead of "until someone
notices the stalled tab".

**Why this is not a Stop hook.** Claude Code fires NO ``Stop`` for a turn that dies on a
rate limit — measured 2026-09-09: session ``544daa0f`` halted at 15:30:48 and its stop-hook
status log shows the previous chain at 15:26:36, the next only at 15:48. The trigger is
Claude Code's ``StopFailure`` event (``error == "rate_limit"``), with the daemon pass as a
backstop for sessions whose hook never fired.

**Two things the trigger cannot tell us**, both handled here:

* A transient server-side 429 ("Server is temporarily limiting requests (not your usage
  limit)") also arrives as ``error: "rate_limit"``. Moving seats would not help — both
  accounts hit the same overloaded server — so the halt must carry the *usage-limit* shape.
* ``is_halted`` stays true after the relaunch (it deliberately ignores trailing user
  records) until the first assistant record lands on the new seat. "Still halted" therefore
  cannot say WHICH account ran out; a late retry would attribute the old account's limit to
  the new one and bounce straight back. So the halt is an identified :class:`HaltObservation`
  — record uuid + timestamp + the account and process that hit it — claimed once
  (``Store.claim_limit_switch``) and never handled twice.

Layering follows :mod:`.resume`: :func:`observe` + :func:`decide` are pure and testable,
:func:`run` performs the effects. Every decision, including every refusal, is appended to
``app_home()/events.log`` as a ``limit-switch`` event, so "why did this session not move?"
is a grep.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import accounts, config, quota
from .adapters.claude import ClaudeAdapter, _assistant_text
from .models import now_ms
from .session_continue import parse_limit_message
from .store import Store

# Claude Code's own text for a 429 that is NOT the user's allowance: the server is shedding
# load. Both accounts share that server, so a switch cannot help and must not fire.
_OVERLOAD_MARKERS = (
    "not your usage limit",
    "temporarily limiting requests",
)

# A claim older than this is stale: its worker died between claiming and reporting an
# outcome, and the halt may be recovered by whoever observes it next.
CLAIM_TTL_MS = 10 * 60 * 1000

# Reason-specific backoffs for a REFUSAL (Codex O7/O12). A refusal never consumes the daily
# success budget — it only says "not now": background work has to finish, a blocked target
# has to reset, a failed relaunch needs the tab to settle.
BACKOFF_BUSY_MS = 60 * 1000  # background work / a turn in flight — check again soon
BACKOFF_NO_TARGET_MS = 15 * 60 * 1000  # every authorized seat is blocked too
BACKOFF_FAILED_MS = 10 * 60 * 1000  # the relauncher itself failed

# How long a limit-switch claim keeps `resume.py` off the same session: the reset watcher
# must not reap the tab a relaunch is about to reuse (Codex O2).
RESUME_HOLD_MS = CLAIM_TTL_MS


@dataclass(frozen=True)
class HaltObservation:  # pylint: disable=too-many-instance-attributes  # it IS the evidence
    """One identified rate-limit halt: what happened, to which account, and when it lifts."""

    session_id: str
    cwd: str
    halt_id: str  # the transcript record's uuid — the handle-once token
    halt_at_ms: int  # the record's own timestamp (never "now": see reset_at_ms)
    text: str
    source_config_dir: str
    pid: int = 0
    pid_start: str = ""
    reset_at_ms: int = 0  # absolute, parsed ONCE against halt_at_ms
    usage_limit: bool = True  # False = a transient server 429, not the account's allowance


@dataclass(frozen=True)
class Decision:
    """What :func:`decide` concluded. ``target`` is empty unless ``switch`` is True."""

    switch: bool
    reason: str
    target_label: str = ""
    target_config_dir: str = ""
    backoff_ms: int = 0


def _record_ms(record: dict) -> int:
    """Epoch-ms of a transcript record's own ``timestamp`` (0 when absent/unparseable)."""
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1000)


def _record_text(record: dict) -> str:
    """The record's assistant text — the adapter's own extractor, never a second parser."""
    return _assistant_text(record)


def observe(
    adapter: ClaudeAdapter,
    session_id: str,
    cwd: str,
    config_dir: str,
    *,
    transcript_path: Path | None = None,
    pid: int = 0,
    pid_start: str = "",
) -> HaltObservation | None:
    """Read the session's halt into an identified observation, or ``None`` if it is not halted.

    The reset time is parsed ONCE, against the halt record's OWN timestamp:
    :func:`parse_limit_message` resolves relative ("resets in 2h 7m") and clock ("resets
    6:40pm") forms against the ``now`` it is handed, so re-parsing the same message five
    minutes later moves the deadline five minutes later (Codex O8). A form it cannot parse
    (a dated "resets Sep 16 at 2pm") yields 0 — the caller falls back to the quota
    snapshot's own window rather than inventing a deadline.
    """
    record = adapter.halt_record(cwd, session_id, config_dir, transcript_path)
    if record is None:
        return None
    text = _record_text(record)
    halt_at = _record_ms(record) or now_ms()
    target = parse_limit_message(text, datetime.fromtimestamp(halt_at / 1000))
    return HaltObservation(
        session_id=session_id,
        cwd=cwd,
        halt_id=str(record.get("uuid") or "") or f"{session_id}:{halt_at}",
        halt_at_ms=halt_at,
        text=text,
        source_config_dir=config_dir,
        pid=pid,
        pid_start=pid_start,
        reset_at_ms=int(target.timestamp() * 1000) if target else 0,
        usage_limit=not any(marker in text.lower() for marker in _OVERLOAD_MARKERS),
    )


def _authorized_targets(source_label: str, cfg: config.Config) -> list[tuple[str, str]]:
    """Configured ``(label, config_dir)`` pairs this source may automatically move to.

    Authorization first, quota second: an account is a candidate only when the
    ``auto_switch_targets`` allow-list permits the transition AND the account dir is logged
    in as the identity ``claude_account_emails`` pins it to. Quota cannot grant permission
    to bill a seat (Codex O9).
    """
    emails = config.claude_account_email_map()
    out: list[tuple[str, str]] = []
    for label, path in config.claude_config_dirs().items():
        target = str(path)
        if accounts.same_config_dir(target, accounts.account_config_dir(source_label) or ""):
            continue
        if not config.auto_switch_allowed(source_label, label, cfg):
            continue
        expected = emails.get(label)
        if expected and accounts.account_email(target) != expected:
            continue
        out.append((label, target))
    return out


def _quota_rank(label: str, model: str, now: int) -> tuple[bool, float]:
    """``(available, urgency)`` for one account, from ONE model-scoped quota reading.

    ``quota.provider_state`` evaluates the windows that apply to the requested MODEL, while
    ``routing.score_accounts`` ranks the Fable weekly window whatever the session runs and
    falls back to the default account — mixing them would rank a seat by a window the
    resumed session will never touch (Codex O10). So both the filter and the order come from
    the same model-scoped verdict: ``urgency`` (headroom per hour until reset) prefers the
    allowance that expires soonest, and an UNKNOWN seat sorts last but stays eligible for
    one bounded trial rather than being deleted on a measurement gap.
    """
    state = quota.provider_state(f"claude:{label}", model=model, now=now)
    if state is None:
        return True, -1.0  # no evidence at all: usable, ranked last
    if state.state == quota.BLOCKED:
        return False, 0.0
    return True, float(state.urgency if state.urgency is not None else -1.0)


def decide(  # pylint: disable=too-many-return-statements
    obs: HaltObservation,
    cfg: config.Config,
    *,
    model: str = "",
    now: int | None = None,
    successes_today: int = 0,
) -> Decision:
    """Pure policy: should this halt move seats, and to which account?

    Refusals are as important as approvals — each carries the backoff that says when the
    same halt may be reconsidered, so a session blocked by background work is retried in a
    minute while one whose every seat is capped waits a quarter of an hour.
    """
    now = int(time.time()) if now is None else now
    if not cfg.auto_switch_on_limit:
        return Decision(False, "auto_switch_on_limit is off")
    if not obs.usage_limit:
        return Decision(False, "transient server 429, not a usage limit — both seats share it")
    if len(config.claude_config_dirs()) < 2:
        return Decision(False, "only one Claude account is configured")
    source_label = accounts.account_label(obs.source_config_dir)
    if not source_label:
        return Decision(False, "the halted session's account could not be resolved")
    if successes_today >= max(0, int(cfg.auto_switch_max_per_day)):
        return Decision(False, f"daily cap reached ({cfg.auto_switch_max_per_day} switches)")
    candidates = _authorized_targets(source_label, cfg)
    if not candidates:
        return Decision(False, f"no authorized target account for {source_label!r}")
    ranked: list[tuple[float, str, str]] = []
    for label, target_dir in candidates:
        available, urgency = _quota_rank(label, model, now)
        if not available:
            continue
        ranked.append((urgency, label, target_dir))
    if not ranked:
        return Decision(
            False,
            "every authorized account is rate-limited too",
            backoff_ms=BACKOFF_NO_TARGET_MS,
        )
    ranked.sort(key=lambda row: row[0], reverse=True)
    _, label, target_dir = ranked[0]
    return Decision(
        True, f"{source_label} is rate-limited; {label} still has quota", label, target_dir
    )


def record_source_block(obs: HaltObservation, *, model: str = "") -> None:
    """Mark the halted account blocked until its own reset — BEFORE any target is chosen.

    Ordering matters: a halt with no available target still proves this seat is out, and
    recording it is what stops the REVERSE switch from bouncing a session straight back into
    an exhausted account (Codex O8). ``observed_at`` is the halt record's timestamp, not
    "now", so a delayed hook cannot overwrite newer evidence — ``quota.record_block`` orders
    writes by that field.
    """
    label = accounts.account_label(obs.source_config_dir)
    if not label:
        return
    observed_at = int(obs.halt_at_ms / 1000) or int(time.time())
    blocked_until = int(obs.reset_at_ms / 1000)
    if not blocked_until:
        state = quota.provider_state(f"claude:{label}", model=model, now=observed_at)
        blocked_until = int(state.resets_at) if state and state.resets_at else 0
    if not blocked_until:  # unparseable and no snapshot: a bounded horizon, never forever
        blocked_until = observed_at + int(BACKOFF_NO_TARGET_MS / 1000)
    quota.record_block(
        f"claude:{label}",
        blocked_until=blocked_until,
        reason="rate-limit halt observed in a session transcript",
        scope="rate_limit",
        observed_at=observed_at,
        source="limitswitch",
    )


def log_event(session_id: str, detail: str) -> None:
    """Append one ``limit-switch`` line to ``app_home()/events.log`` (never raises)."""
    try:
        path = config.app_home() / "events.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{session_id}\tlimit-switch\t{detail}\n")
    except OSError:
        pass


def hold_active(session, now: int | None = None) -> bool:
    """True while *session*'s failover claim must keep other recovery off this session.

    :mod:`.resume` calls this in BOTH its planner and its executor: filtering candidates is
    not enough, because the planner also acts on queue entries recorded earlier and
    ``_reap_fresh`` kills the live process and CLOSES ITS TAB — the very tab a relaunch is
    about to reuse (Codex O2/O16).
    """
    now = now_ms() if now is None else now
    state = getattr(session, "limit_switch_state", "")
    if not state or state in ("failed", "refused", "abandoned", "started"):
        return False
    return int(getattr(session, "limit_switch_at", 0) or 0) > now - RESUME_HOLD_MS


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def run(
    session_id: str,
    cwd: str = "",
    *,
    transcript_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Observe, claim, decide and (unless *dry_run*) dispatch. Returns ``(switched, detail)``.

    Called by the ``StopFailure`` hook's detached worker and by the daemon backstop; both
    race on the same halt on purpose — the claim decides which one acts.

    EVERY outcome is logged here, including the early ones (session gone, not halted, a
    claim lost). The worker is detached and silent, so an unlogged early return is
    indistinguishable from "the hook never fired" when reading back later — which is exactly
    the question this log exists to answer.
    """
    switched, detail = _run(session_id, cwd, transcript_path=transcript_path, dry_run=dry_run)
    if not dry_run:
        log_event(session_id, ("switch: " if switched else "no switch: ") + detail)
    return switched, detail


def _run(  # pylint: disable=too-many-locals,too-many-return-statements,too-many-branches
    session_id: str,
    cwd: str = "",
    *,
    transcript_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """The decision itself — see :func:`run`, which owns the logging."""
    cfg = config.load_config()
    adapter = ClaudeAdapter()
    live = next((e for e in adapter.discover() if e.session_id == session_id and e.alive), None)
    if live is None:
        return False, "session is not live"
    if getattr(live, "conflict", False):
        return False, "session is live under two accounts (D9 conflict) — refusing"
    cwd = cwd or live.cwd
    source = live.config_dir
    if not source:
        return False, "the registry does not record which account this session bills"
    obs = observe(
        adapter,
        session_id,
        cwd,
        source,
        transcript_path=transcript_path,
        pid=live.pid,
        pid_start="",
    )
    if obs is None:
        return False, "not halted (no rate-limit record at the end of the transcript)"
    model = getattr(live, "model", "") or ""
    if obs.usage_limit and not dry_run:
        record_source_block(obs, model=model)  # before target selection, always
    with Store() as store:
        store.ensure(session_id, cwd=cwd)
        row = store.get(session_id)
        successes = 0
        if row is not None and getattr(row, "limit_switch_day", "") == _today():
            successes = int(getattr(row, "limit_switch_successes", 0) or 0)
        retry_at = int(getattr(row, "limit_switch_retry_not_before", 0) or 0) if row else 0
        same_halt = bool(row) and getattr(row, "limit_switch_halt_id", "") == obs.halt_id
        if same_halt and retry_at > now_ms():
            return (
                False,
                f"backing off until {time.strftime('%H:%M:%S', time.localtime(retry_at / 1000))}",
            )
        decision = decide(obs, cfg, model=model, now=int(time.time()), successes_today=successes)
        if dry_run:
            return decision.switch, f"[dry-run] {decision.reason}"
        if not decision.switch:
            if decision.backoff_ms and same_halt:
                store.set_limit_switch_state(
                    session_id,
                    "refused",
                    reason=decision.reason,
                    retry_not_before=now_ms() + decision.backoff_ms,
                )
            return False, decision.reason
        won = store.claim_limit_switch(
            session_id,
            halt_id=obs.halt_id,
            source=source,
            pid=obs.pid,
            pid_start=obs.pid_start,
            now=now_ms(),
            ttl_ms=CLAIM_TTL_MS,
        )
        if not won:
            return False, "another worker already owns this halt"
        store.set_limit_switch_state(
            session_id, "claimed", reason=decision.reason, target=decision.target_config_dir
        )
    ok, detail = _dispatch(session_id, decision, obs)
    with Store() as store:
        if ok:
            store.set_limit_switch_state(
                session_id,
                "dispatched",
                reason=detail,
                count_success=True,
                day=_today(),
                now=now_ms(),
            )
        else:
            store.set_limit_switch_state(
                session_id,
                "refused",
                reason=detail,
                retry_not_before=now_ms() + BACKOFF_BUSY_MS,
            )
    return ok, detail


def _dispatch(session_id: str, decision: Decision, obs: HaltObservation) -> tuple[bool, str]:
    """Run the ONE switch path (``switch-account -N``) in automatic mode.

    Automatic mode differs from the human one in exactly three ways, all fail-closed: trust
    is READ-ONLY (never established), ``--force`` is impossible, and a manual arm or pending
    close refuses the switch instead of being overwritten. The relauncher additionally
    re-checks the claim, background work and this same halt immediately before it signals.
    """
    import argparse  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    from . import cli  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    cfg = config.load_config()
    args = argparse.Namespace(
        label=decision.target_label,
        session=session_id,
        undo=False,
        quiet=True,
        force=False,
        now=True,
        cancel_prompt=False,
        prompt=cfg.auto_switch_prompt or "",
        no_continue=not (cfg.auto_switch_prompt or ""),
        auto=True,
        auto_halt_id=obs.halt_id,
    )
    # `cmd_switch_account` reports WHY it refuses on stderr, and this worker is detached
    # with its streams sent to /dev/null — so capture them and keep the reason. Without it
    # the log says only "refused", which is precisely the question the log exists to answer.
    import contextlib  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    import io  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_switch_account(args)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False, f"switch-account raised: {exc}"
    source_label = accounts.account_label(obs.source_config_dir) or "?"
    if code != 0:
        said = " ".join(captured.getvalue().split())[:300] or "no reason given"
        return False, f"{source_label} -> {decision.target_label} refused: {said}"
    return True, f"{source_label} -> {decision.target_label}: {decision.reason}"
