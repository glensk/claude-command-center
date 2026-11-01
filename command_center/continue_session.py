#!/usr/bin/env python3
"""``ccc continue-session`` — hand a session's work to a NEW session (tp#1300).

The handoff is the cheap alternative to ``switch-account`` (``/…-cache-write-hit``): the
source session writes a self-contained handoff (:mod:`command_center.handoff_doc`) and a
fresh Claude Code session — its own ``--session-id``, never ``--resume`` — continues from
it on the named account. Verbs:

* ``state`` (read-only) — what the source would hand over: AIM, unchecked sub-goals, cwd,
  account, observed model, ``no_codex``, the in-flight inventory
  (:mod:`command_center.inflight`) and the handoff lineage;
* ``template`` (read-only) — the fixed handoff template;
* ``drain`` (read-only) — block until the in-flight detector is clear, exit 1 on timeout.

The transfer itself (``start`` / ``accept`` / ``status``) runs the state machine of
:mod:`command_center.transfer_store`.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports,too-many-lines  # shim first; one verb file

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter
    from .inflight import Inventory
    from .store import Store
    from .transfer_store import Transfer

DRAIN_DEFAULT_SEC = 540  # one foreground Bash call, under the 600 s tool cap
DRAIN_POLL_SEC = 3.0  # the adapter caches the process table for 3 s
IDLE_WAIT_SEC = 120  # D12: how long ``start --await-idle`` waits for a provable idle
IDLE_POLL_SEC = 2
IDLE_STABLE_SEC = 10  # the transcript must stay unchanged this long


@dataclass
class Source:  # pylint: disable=too-many-instance-attributes
    """The session being handed off, as this process can observe it."""

    session_id: str
    cwd: str = ""
    config_dir: str = ""
    label: str = ""
    pid: int = 0
    inside: bool = False  # this process runs inside the session (its Bash tool)
    transcript: Path | None = None
    ignore: frozenset[int] = field(default_factory=frozenset)
    model: str = ""


def _adapter() -> ClaudeAdapter:
    from .adapters.claude import ClaudeAdapter

    return ClaudeAdapter()


def _env_session() -> str:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


def resolve_source(adapter: ClaudeAdapter, store: Store, explicit: str | None) -> Source | None:
    """The source session: ``-s`` > this session's env > the live session in cwd."""
    from . import accounts, terminal

    env_sid = _env_session()
    sid = explicit or env_sid
    if not sid:  # like `cli.resolve_session_id`: the live session in this cwd
        here = [e for e in adapter.discover() if e.cwd == os.getcwd() and e.alive]
        here.sort(key=lambda e: (e.raw_status != "busy", -e.updated_at))
        sid = here[0].session_id if here else ""
    if not sid:
        return None
    inside = bool(env_sid) and env_sid == sid
    live = next((e for e in adapter.discover() if e.session_id == sid and e.alive), None)
    stored = store.get(sid)
    config_dir = (
        accounts.env_config_dir()
        if inside
        else ((live.config_dir if live else "") or (stored.config_dir if stored else ""))
    )
    cwd = (live.cwd if live else "") or (stored.cwd if stored else "") or os.getcwd()
    model = (stored.model if stored else "") or (getattr(live, "model", "") or "")
    ignore = terminal.pid_ancestry(os.getpid(), terminal.ps_table()) if inside else frozenset()
    return Source(
        session_id=sid,
        cwd=cwd,
        config_dir=config_dir,
        label=accounts.account_label(config_dir) if config_dir else "",
        pid=live.pid if live else 0,
        inside=inside,
        transcript=adapter.transcript_path(cwd, sid, config_dir or None),
        ignore=ignore,
        model=model,
    )


def _clear_stale_counter(store: Store, session_id: str, stale: int) -> None:
    """Reset a stale SubagentStart/SubagentStop counter and say so (stderr: JSON stays clean)."""
    store.reset_subagents(session_id)
    print(
        f"note: stale subagent counter ({stale}) cleared — no launch record pending",
        file=sys.stderr,
    )


def inventory(adapter: ClaudeAdapter, store: Store, src: Source) -> Inventory:
    """The in-flight inventory of *src* (store counter included)."""
    from . import inflight

    inv = inflight.inventory(
        adapter,
        src.pid,
        src.transcript,
        src.ignore,
        active_subagents=store.active_subagents(src.session_id),
        subagent_started_at=store.subagent_started_at(src.session_id),
    )
    if inv.stale_counter:
        _clear_stale_counter(store, src.session_id, inv.stale_counter)
    return inv


def state(adapter: ClaudeAdapter, store: Store, src: Source) -> dict[str, Any]:
    """Everything ``state -j`` reports about *src* (read-only)."""
    session = store.get(src.session_id)
    subgoals = store.list_subgoals(src.session_id) if session else []
    active = store.active_transfer(src.session_id)
    return {
        "session_id": src.session_id,
        "cwd": src.cwd,
        "account": src.label,
        "config_dir": src.config_dir,
        "live_pid": src.pid,
        "transcript": str(src.transcript) if src.transcript else "",
        "model": src.model,
        "effort": (session.effort if session else "") or "",
        "no_codex": bool(session and session.no_codex),
        "launch_profile": (session.launch_profile if session else "") or "",
        "aim": (session.aim if session else "") or "",
        "aim_rev": store.count_aim_history(src.session_id) if session else 0,
        "subgoals": {
            "unchecked": [g.text for g in subgoals if not g.checked],
            "checked": [g.text for g in subgoals if g.checked],
        },
        "handed_off_from": (session.handed_off_from if session else None) or "",
        "handed_off_to": (session.handed_off_to if session else None) or "",
        "active_transfer": active.to_dict() if active else None,
        "inflight": inventory(adapter, store, src).to_dict(),
        "capped": _capped(adapter, src),
    }


def _capped(adapter: ClaudeAdapter, src: Source) -> dict[str, Any]:
    """The capped-seat verdict a typed handoff would act on (tp#1327)."""
    from . import handoff_capped

    capped, why = handoff_capped.is_capped(adapter, src)
    return {"capped": capped, "why": why}


def _render_state(data: dict[str, Any]) -> str:
    lines = [
        f"session:   {data['session_id']} ({data['account'] or '?'}, pid {data['live_pid']})",
        f"cwd:       {data['cwd']}",
        f"model:     {data['model'] or '?'}  no_codex: {data['no_codex']}"
        + (f"  profile: {data['launch_profile']}" if data.get("launch_profile") else ""),
        f"AIM:       {data['aim'] or '(none)'}",
    ]
    for text in data["subgoals"]["unchecked"]:
        lines.append(f"  [ ] {text}")
    for text in data["subgoals"]["checked"]:
        lines.append(f"  [x] {text}")
    if data["handed_off_from"]:
        lines.append(f"took over from: {data['handed_off_from']}")
    if data["handed_off_to"]:
        lines.append(f"handed off to:  {data['handed_off_to']}")
    if data["active_transfer"]:
        t = data["active_transfer"]
        lines.append(f"transfer:  {t['transfer_id']} {t['state']} → {t['target_session']}")
    capped = data.get("capped") or {}
    verdict = "CAPPED" if capped.get("capped") else "not capped"
    lines.append(f"seat:      {verdict}" + (f" — {capped['why']}" if capped.get("why") else ""))
    lines.append(_render_inflight(data["inflight"]))
    return "\n".join(lines)


def _render_inflight(inv: dict[str, Any]) -> str:
    head = "in flight: clear" if inv["clear"] else "in flight: NOT clear"
    lines = [head]
    lines += [f"  reason: {reason}" for reason in inv["reasons"]]
    for item in inv["items"]:
        detail = item["description"] or item["command"] or item["agent_type"]
        lines.append(f"  {item['kind']:<9} {item['id']:<18} {detail}")
    return "\n".join(lines)


def _open_source(args: argparse.Namespace) -> tuple[Any, Any, Source | None]:
    from .store import Store

    adapter = _adapter()
    store = Store()
    return adapter, store, resolve_source(adapter, store, getattr(args, "session", None))


def cmd_state(args: argparse.Namespace) -> int:
    """``continue-session state``: what a handoff of this session would carry."""
    adapter, store, src = _open_source(args)
    with store:
        if src is None:
            print("error: could not resolve a session (pass -s/--session <id>)", file=sys.stderr)
            return 1
        data = state(adapter, store, src)
    print(json.dumps(data, indent=2) if args.json else _render_state(data))
    return 0


def cmd_template(_args: argparse.Namespace) -> int:
    """``continue-session template``: print the fixed handoff template."""
    from . import handoff_doc

    sys.stdout.write(handoff_doc.template())
    return 0


def cmd_drain(args: argparse.Namespace) -> int:
    """``continue-session drain``: block until nothing is in flight (exit 1 on timeout)."""
    from . import inflight

    adapter, store, src = _open_source(args)
    with store:
        if src is None:
            print("error: could not resolve a session (pass -s/--session <id>)", file=sys.stderr)
            return 1
        ok, inv = inflight.drain(
            adapter,
            src.pid,
            src.transcript,
            timeout=float(args.timeout),
            poll=DRAIN_POLL_SEC,
            ignore=src.ignore,
            active_subagents=lambda: store.active_subagents(src.session_id),
            subagent_started_at=lambda: store.subagent_started_at(src.session_id),
            on_stale_counter=lambda n: _clear_stale_counter(store, src.session_id, n),
        )
    data = inv.to_dict()
    if args.json:
        print(json.dumps({"drained": ok, **data}, indent=2))
    else:
        print(_render_inflight(data))
        if not ok:
            print(
                f"not drained after {args.timeout}s — the transfer must not start; the "
                "source session stays authoritative",
                file=sys.stderr,
            )
    return 0 if ok else 1


# ------------------------------------------------------------------------- start


ACCEPT_WAIT_DEFAULT_SEC = 420  # `start` waits this long for `accepted` (under the tool cap)
ACCEPT_POLL_SEC = 2.0
EXIT_WAITING = 3  # `start` timed out waiting for `accepted`: both sessions stay open


class StartRefused(Exception):
    """A fail-closed preflight refusal; the message is printed, nothing was written."""


def _launch(target_session: str) -> str:
    """Open the successor's tab (``ccc start-job <id>``); the launcher rung or ``""``."""
    from . import terminal

    return terminal.start_job_launch(target_session)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _job_model(observed: str, explicit: str | None) -> str:
    """Q1: an explicit ``-O``/``-E`` wins; else the observed model when launchable; else default."""
    from .models import NEW_JOB_DEFAULT_LLM, NEW_JOB_LLM_CHOICES

    if explicit:
        if explicit not in NEW_JOB_LLM_CHOICES:
            raise StartRefused(
                f"unknown model {explicit!r} (choices: {', '.join(NEW_JOB_LLM_CHOICES)})"
            )
        return explicit
    return observed if observed in NEW_JOB_LLM_CHOICES else NEW_JOB_DEFAULT_LLM


def handoff_path(transfer_id: str) -> Path:
    """Where the vouched handoff of *transfer_id* is stored (``<app_home>/handoffs/``)."""
    from . import config

    return config.app_home() / "handoffs" / f"{transfer_id}.md"


def preamble(transfer: Any, source_label: str, transcript: str) -> str:
    """D4: the short instruction block the successor reads before the handoff text."""
    return (
        f"[handoff] You take over the work of session {transfer.source_session} "
        f"(account {source_label or '?'}), transfer {transfer.transfer_id}. The handoff "
        f"below is also stored at {transfer.handoff_path}"
        + (f"; the old session's transcript is {transcript} (evidence only)" if transcript else "")
        + ".\n"
        f"FIRST run: ccc continue-session accept {transfer.transfer_id}\n"
        "Only after it succeeds: relaunch every agent under 'Subagent continuations' as a NEW "
        "Agent from its capsule (old agent ids cannot be resumed across sessions), restart "
        "ONLY the shells under 'Background restarts' marked `restart:` (declared restartable; "
        "the ones under 'Decide before restarting' are evidence, judge each before re-running "
        "anything), then follow 'First steps'. Facts cut for size: "
        f"ccc continue-session facts {transfer.source_session} -j. If accept "
        "fails, stop and report why: the old session stays authoritative until it succeeds.\n"
        "\n"
    )


def _target_dir(label: str) -> str:
    from . import accounts, config

    dirs = config.claude_config_dirs()
    if label not in dirs:
        known = ", ".join(sorted(dirs)) or "none"
        raise StartRefused(f"unknown account {label!r} (configured: {known})")
    target = str(dirs[label])
    email = accounts.account_email(target)
    expected = config.claude_account_email_map().get(label)
    if not email:
        raise StartRefused(f"the {label!r} account ({target}) is not logged in — log in first")
    if expected and email != expected:
        raise StartRefused(
            f"the {label!r} account is logged in as {email}, not {expected} "
            "(claude_account_emails) — log in there first"
        )
    return target


def _preflight(
    adapter: ClaudeAdapter, store: Store, src: Source, label: str, *, dry_run: bool = False
) -> str:
    """Fail-closed checks before anything is written; returns the target config dir."""
    from . import accounts

    if not src.inside:
        raise StartRefused(
            f"continue-session start must run inside the source session {src.session_id} "
            "(its own Bash tool), not from another shell"
        )
    if src.pid <= 0:
        raise StartRefused(f"{src.session_id} is not a live session in Claude's registry")
    if not src.config_dir:
        raise StartRefused("could not tell which account this session bills")
    target = _target_dir(label)
    if dry_run:  # writes nothing: report what the real run would establish
        if not accounts.is_trusted(target, src.cwd):
            print(f"[dry-run] would trust {src.cwd} for the {label!r} account", file=sys.stderr)
    else:
        accounts.ensure_trusted(target, src.cwd)  # the successor must not park on the dialog
    if not dry_run and not accounts.is_trusted(target, src.cwd):
        raise StartRefused(f"could not confirm {src.cwd} is trusted for the {label!r} account")
    inv = inventory(adapter, store, src)
    if not inv.clear:
        raise StartRefused(
            "work is still in flight — drain first (ccc continue-session drain): "
            + "; ".join(inv.reasons)
        )
    return target


def _checked_profile(profile: str) -> str:
    """*profile* ("" = bare claude) proven to resolve NOW, or :class:`StartRefused`.

    Only the NAME travels (transfer row, successor draft); ``start-job`` resolves it again
    at launch. An unresolvable profile refuses with the source session left authoritative:
    a successor must never start a bare ``claude`` on another billing route.
    """
    from .config import LaunchProfileError, resolve_launch_profile

    if profile:
        try:
            resolve_launch_profile(profile)
        except LaunchProfileError as exc:
            raise StartRefused(f"{exc} — the source session stays authoritative") from exc
    return profile


def _candidate(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    src: Source,
    target_dir: str,
    label: str,
    args: Any,
    digest: str,
    facts_snapshot: str = "",
) -> Transfer:
    """The snapshot this ``start`` would write (target id and transfer id still unset)."""
    from .transfer_store import Transfer

    session = store.get(src.session_id)
    profile = _checked_profile((session.launch_profile if session else "") or "")
    return Transfer(
        transfer_id="",
        source_session=src.session_id,
        source_config_dir=src.config_dir,
        target_config_dir=target_dir,
        target_label=label,
        cwd=src.cwd,
        source_aim=(session.aim if session else "") or "",
        source_aim_rev=store.count_aim_history(src.session_id),
        llm_overseer=_job_model(src.model, getattr(args, "overseer", None)),
        llm_exec=_job_model(
            src.model, getattr(args, "executor", None) or getattr(args, "overseer", None)
        ),
        no_codex=bool(session and session.no_codex),
        launch_profile=profile,
        prompt_sha256=digest,
        capped=bool(getattr(args, "capped", False)),
        facts_snapshot=facts_snapshot,
    )


def _create_successor(store: Store, transfer: Any, src: Source, text: str) -> None:
    """The successor's draft job (idempotent): AIM, cwd, account, models, no_codex, prompt."""
    if store.get(transfer.target_session) is not None:
        return
    unchecked = [g.text for g in store.list_subgoals(src.session_id) if not g.checked]
    aim = transfer.source_aim or f"continue the work handed over by session {src.session_id[:8]}"
    store.create_draft(
        transfer.target_session,
        transfer.cwd,
        aim,
        prompt=preamble(transfer, src.label, str(src.transcript or "")) + text,
        no_codex=transfer.no_codex,
        llm_overseer=transfer.llm_overseer,
        llm_exec=transfer.llm_exec,
        config_dir=transfer.target_config_dir,
        launch_profile=transfer.launch_profile,
    )
    if unchecked:  # Q2: the open sub-goals travel; the checked ones are in the text
        store.set_subgoals(transfer.target_session, unchecked)
    store.update_fields(transfer.target_session, handed_off_from=src.session_id)


def _write_handoff(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def finalize(store: Store, transfer: Any, *, arm_close: bool = True) -> bool:
    """Retire the source once the successor ACCEPTED: AIM, lineage, own close, CAS.

    The session writes come first and are idempotent, the ``accepted → finalized`` CAS
    last — so a crash in between is healed by re-running ``start`` (it finds the
    still-``accepted`` transfer and finalizes again). The source's ORIGINAL AIM stays in
    its AIM history and in the transfer record. ``False`` when the transfer is not
    ``accepted`` (any more).
    """
    from .models import now_ms
    from .transfer_store import ACCEPTED, FINALIZED

    current = store.get_transfer(transfer.transfer_id)
    if current is None or current.state not in (ACCEPTED, FINALIZED):
        return False
    if current.state == FINALIZED:
        return True
    now = now_ms()
    target8 = current.target_session[:8]
    session = store.get(current.source_session)
    final_aim = f"handoff accepted by session {target8}"
    if session is not None and session.aim != final_aim:
        store.set_aim(current.source_session, final_aim)
    store.set_handoff_lineage(current.source_session, current.target_session, now)
    if arm_close and session is not None and not session.close_requested_at:
        store.arm_close(current.source_session, now)  # its own Stop chain closes the tab
    return store.transition_transfer(current.transfer_id, FINALIZED, now) or (
        (store.get_transfer(current.transfer_id) or current).state == FINALIZED
    )


def _wait_accepted(store: Store, transfer_id: str, timeout: float) -> Any:
    from .transfer_store import ACCEPTED, FAILED, FINALIZED

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        current = store.get_transfer(transfer_id)
        if current is None or current.state in (ACCEPTED, FINALIZED, FAILED):
            return current
        if time.monotonic() >= deadline:
            return current
        _sleep(ACCEPT_POLL_SEC)


def _dry_run(transfer: Any, src: Source, text: str, as_json: bool) -> int:
    from . import launch_argv
    from .models import LLM_MODEL_IDS

    prompt = preamble(transfer, src.label, str(src.transcript or "")) + text
    argv = launch_argv.claude_argv(
        session_id="<new session id>",
        model=LLM_MODEL_IDS.get(transfer.llm_overseer, transfer.llm_overseer),
        prompt=prompt,
        name="",
    )
    if as_json:
        print(json.dumps({"dry_run": True, "snapshot": transfer.snapshot(), "argv": argv}))
    else:
        print(f"[dry-run] would hand {src.session_id} to a NEW session on {transfer.target_label}")
        print("argv: " + " ".join(argv[:-1]) + " <prompt>")
        print("--- prompt ---")
        print(prompt)
    return 0


def _account_change_note(src: Source, transfer: Any) -> str:
    from . import accounts

    if accounts.same_config_dir(src.config_dir, transfer.target_config_dir):
        return f"note: the account does not change ({transfer.target_label})"
    return f"note: the account changes {src.label or '?'} → {transfer.target_label}"


def _reuse_or_create(store: Store, candidate: Any, now: int) -> Any:
    """The transfer to drive: an exact-match active one (retry), or a fresh one."""
    from .transfer_store import TransferConflict, new_transfer_id

    active = store.active_transfer(candidate.source_session)
    if active is not None:
        differing = active.differs(candidate)
        if differing:
            raise StartRefused(
                f"transfer {active.transfer_id} ({active.state}) is already active for this "
                f"session with different {', '.join(differing)} — see "
                f"`ccc continue-session status {active.transfer_id}`"
            )
        return active
    candidate.transfer_id = new_transfer_id()
    candidate.target_session = str(uuid.uuid4())
    candidate.handoff_path = str(handoff_path(candidate.transfer_id))
    try:
        return store.create_transfer(candidate, now)
    except TransferConflict as exc:
        raise StartRefused("another transfer of this session started concurrently") from exc


def _drive(store: Store, transfer: Any, src: Source, text: str, args: Any) -> int:
    """Launch (or re-launch) the successor, wait for ``accepted``, finalize."""
    from .models import now_ms
    from .transfer_store import ACCEPTED, FAILED, FINALIZED, OPENED, PREPARED

    if transfer.state in (PREPARED, OPENED):
        _write_handoff(Path(transfer.handoff_path), text)
        try:  # re-resolved from its name before every (re)launch, never from a snapshot
            _checked_profile(transfer.launch_profile)
        except StartRefused as exc:
            if transfer.state == PREPARED:
                store.transition_transfer(
                    transfer.transfer_id, FAILED, now_ms(), failure=f"launch: {exc}"[:200]
                )
                if store.get(transfer.target_session) is not None:
                    store.update_fields(transfer.target_session, archived=True)
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _create_successor(store, transfer, src, text)
        target_row = store.get(transfer.target_session)
        if target_row is not None and target_row.draft:  # never started: (re)launch it
            launcher = _launch(transfer.target_session)
            if not launcher:
                if transfer.state == PREPARED:
                    store.transition_transfer(
                        transfer.transfer_id, FAILED, now_ms(), failure="launch: no terminal tab"
                    )
                    store.update_fields(transfer.target_session, archived=True)
                print(
                    "error: could not open a terminal tab for the successor — the source "
                    "session is unchanged",
                    file=sys.stderr,
                )
                return 1
            store.note_transfer(transfer.transfer_id, now_ms(), launcher=launcher)
            store.transition_transfer(transfer.transfer_id, OPENED, now_ms())
            print(
                f"opened: successor {transfer.target_session} on {transfer.target_label} "
                f"({launcher}); waiting for it to accept transfer {transfer.transfer_id}"
            )
    current = _wait_accepted(store, transfer.transfer_id, float(args.wait))
    if current is None or current.state == FAILED:
        print(f"error: transfer {transfer.transfer_id} failed: {current and current.failure}")
        return 1
    if current.state not in (ACCEPTED, FINALIZED):
        print(
            f"waiting: transfer {transfer.transfer_id} is still {current.state} after "
            f"{args.wait}s — both sessions stay open. Re-run the same start command to keep "
            "waiting; `ccc continue-session status` shows the recovery step.",
            file=sys.stderr,
        )
        return EXIT_WAITING
    if not finalize(store, current, arm_close=not current.capped):
        print(f"error: could not finalize transfer {transfer.transfer_id}", file=sys.stderr)
        return 1
    print(
        f"finalized: session {current.target_session} accepted transfer "
        f"{transfer.transfer_id}; this session is handed off and closes after this turn. "
        "Make no further tool calls."
    )
    return 0


def _fail(args: argparse.Namespace, sid: str, message: str) -> int:
    """Print *message*; a detached ``--capped`` run also logs and notifies (exit 1)."""
    print(f"error: {message}", file=sys.stderr)
    if getattr(args, "capped", False):
        from . import handoff_capped

        fallback = handoff_capped.cache_write_hit_for(args.account, "")
        handoff_capped.log_event(sid, f"capped handoff failed: {message}")
        handoff_capped.notify_failure(f"handoff did not start: {message[:160]} — use {fallback}")
    return 1


@dataclass
class _Composed:
    """What ``start`` hands to the transfer: the vouched text, its digest, the snapshot id."""

    text: str
    digest: str
    snapshot: str = ""


def _stored_snapshot(source_sid: str, snapshot: str) -> dict[str, Any] | None:
    from . import handoff_facts

    try:
        hit = sorted(handoff_facts.facts_dir(source_sid).glob(f"{snapshot[:12]}*.json"))
        doc = json.loads(hit[0].read_text(encoding="utf-8")) if hit else None
    except (OSError, ValueError, handoff_facts.FactsError):
        return None
    return doc if isinstance(doc, dict) else None


def _snapshot(
    adapter: ClaudeAdapter, store: Store, src: Source, args: Any
) -> tuple[dict[str, Any], str, Any]:
    """Collect and freeze the facts: ``(snapshot document, snapshot id, scrubber)``."""
    from . import handoff_facts

    scrubber = handoff_facts.Scrubber()
    try:
        doc = handoff_facts.collect(
            adapter, store, src, "finalize", scrubber=scrubber, persist=not args.dry_run
        )
        snapshot, _path = handoff_facts.freeze(doc, scrubber, write=not args.dry_run)
    except handoff_facts.FactsError as exc:
        raise StartRefused(f"facts collection refused — {exc}") from exc
    return doc, snapshot, scrubber


def _finish(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    model: Any, snap_doc: dict[str, Any], snapshot: str, sid: str, args: Any
) -> _Composed:
    """Compose the model part with the snapshot (D1, D9) and keep a cut original."""
    from . import handoff_doc, handoff_facts

    model_path = handoff_facts.facts_dir(sid) / f"{snapshot[:12]}.model.md"
    doc, composed = handoff_doc.compose_checked(
        model, snap_doc, ref=sid, model_path=str(model_path)
    )
    if not doc.ok:
        raise StartRefused("handoff rejected — " + "; ".join(doc.errors))
    if composed.model_cut and not args.dry_run:  # D9: the cut part stays retrievable
        try:
            handoff_facts.write_private(model_path, handoff_facts.Scrubber().text(model.raw))
        except handoff_facts.FactsError as exc:
            raise StartRefused(f"facts collection refused — {exc}") from exc
    return _Composed(doc.text, handoff_doc.model_digest(model, snapshot), snapshot)


def _compose_generated(
    adapter: ClaudeAdapter, store: Store, src: Source, active: Any, args: Any
) -> _Composed:
    """D10 / D11: the model part written by code (``--facts-only``) or the router (``--capped``).

    A retry of an active PREPARED / OPENED transfer reuses its stored composed text, so a
    router that answers differently the second time cannot fork the transfer.
    """
    from . import handoff_capped, handoff_doc, handoff_facts
    from .transfer_store import OPENED, PREPARED

    if active is not None and active.state in (PREPARED, OPENED) and active.facts_snapshot:
        try:
            stored = Path(active.handoff_path).read_text(encoding="utf-8")
        except OSError:
            stored = ""
        if stored:
            return _Composed(stored, active.prompt_sha256, active.facts_snapshot)
    snap_doc, snapshot, scrubber = _snapshot(adapter, store, src, args)
    try:
        text = handoff_capped.model_part(
            src.session_id, snap_doc, src.transcript, scrubber, use_router=bool(args.capped)
        )
    except handoff_facts.FactsError as exc:
        raise StartRefused(f"facts collection refused — {exc}") from exc
    model = handoff_doc.validate_model_part(text)
    if not model.ok:  # code-written text is valid by construction; never ship a bad one
        raise StartRefused("handoff rejected — " + "; ".join(model.errors))
    return _finish(model, snap_doc, snapshot, src.session_id, args)


def _compose(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: ClaudeAdapter, store: Store, src: Source, raw: bytes, active: Any, args: Any
) -> _Composed:
    """D2: facts frozen into a snapshot + the model file, composed once per transfer.

    An active PREPARED / OPENED transfer that already has a snapshot is REUSED (stored
    snapshot and stored composed text, no recollection): its digest is recomputed from the
    given model file and the stored snapshot id, so a changed ledger cannot fork a retry.
    ``--facts-only`` / ``--capped`` generate the model part (:func:`_compose_generated`);
    a transfer from before snapshots keeps the whole-document path.
    """
    from . import handoff_doc
    from .transfer_store import OPENED, PREPARED

    if args.facts_only or args.capped:
        return _compose_generated(adapter, store, src, active, args)
    if active is not None and active.state in (PREPARED, OPENED) and not active.facts_snapshot:
        doc = handoff_doc.validate(raw)
        if not doc.ok:
            raise StartRefused("handoff rejected — " + "; ".join(doc.errors))
        return _Composed(doc.text, doc.sha256)
    model = handoff_doc.parse_model_file(raw)
    if not model.ok:
        raise StartRefused("handoff rejected — " + "; ".join(model.errors))
    sid = src.session_id
    if active is not None and active.state in (PREPARED, OPENED):
        digest = handoff_doc.model_digest(model, active.facts_snapshot)
        try:
            stored = Path(active.handoff_path).read_text(encoding="utf-8")
        except OSError:
            stored = ""
        if stored and digest == active.prompt_sha256:
            return _Composed(stored, digest, active.facts_snapshot)
        snap_doc = _stored_snapshot(sid, active.facts_snapshot)
        if snap_doc is None:
            raise StartRefused(f"the stored facts snapshot {active.facts_snapshot[:12]} is gone")
        snapshot = active.facts_snapshot
    else:
        snap_doc, snapshot, _scrubber = _snapshot(adapter, store, src, args)
    return _finish(model, snap_doc, snapshot, sid, args)


def _transcript_stamp(path: Path | None) -> tuple[int, int] | None:
    try:
        stat = path.stat() if path is not None else None
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size) if stat is not None else None


def _idle_now(adapter: ClaudeAdapter, src: Source) -> bool:
    """The registry reads the session idle AND its transcript holds no open turn."""
    from .adapters.claude_agents import contract_status
    from .bridge_send import inspect_transcript

    live = next((e for e in adapter.discover() if e.session_id == src.session_id and e.alive), None)
    if live is None or contract_status(live.raw_status) != "idle":
        return False
    if src.transcript is None or not src.transcript.is_file():
        return False
    return inspect_transcript(src.transcript).turn_active is False


def _await_idle(adapter: ClaudeAdapter, src: Source) -> bool:
    """D12: wait (≤ IDLE_WAIT_SEC, poll IDLE_POLL_SEC) for idle + a transcript still for 10 s."""
    need = -(-IDLE_STABLE_SEC // IDLE_POLL_SEC)
    stable = 0
    last: tuple[int, int] | None = None
    for _ in range(IDLE_WAIT_SEC // IDLE_POLL_SEC):
        stamp = _transcript_stamp(src.transcript)
        still = stamp is not None and stamp == last and _idle_now(adapter, src)
        stable = stable + 1 if still else 0
        last = stamp
        if stable >= need:
            return True
        _sleep(IDLE_POLL_SEC)
    return False


def cmd_start(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements
    """``continue-session start``: hand this session's work to a NEW session."""
    from .models import now_ms
    from .transfer_store import ACCEPTED

    adapter, store, src = _open_source(args)
    with store:
        if src is None:
            print("error: could not resolve a session (pass -s/--session <id>)", file=sys.stderr)
            return 1
        if args.await_idle and not _await_idle(adapter, src):
            print(
                f"error: session {src.session_id} is not provably idle — no transfer started",
                file=sys.stderr,
            )
            return 1
        active = store.active_transfer(src.session_id)
        if active is not None and active.state == ACCEPTED:  # a re-run after acceptance
            return _drive(store, active, src, "", args)
        session = store.get(src.session_id)
        if session is not None and session.handed_off_to:
            return _fail(args, src.session_id, f"{src.session_id} was already handed off")
        if args.facts_only and (args.file or args.capped):
            return _fail(args, src.session_id, "--facts-only takes no -f/--file and no --capped")
        raw = b""
        if not (args.facts_only or args.capped):
            if not args.file:
                return _fail(args, src.session_id, "give -f/--file HANDOFF (or --capped)")
            try:
                raw = Path(args.file).read_bytes()
            except OSError as exc:
                return _fail(args, src.session_id, f"cannot read the handoff file: {exc}")
        try:
            composed = _compose(adapter, store, src, raw, active, args)
        except StartRefused as exc:
            return _fail(args, src.session_id, str(exc))
        try:
            target_dir = _preflight(adapter, store, src, args.account, dry_run=args.dry_run)
            candidate = _candidate(
                store, src, target_dir, args.account, args, composed.digest, composed.snapshot
            )
            print(_account_change_note(src, candidate), file=sys.stderr)
            if args.dry_run:
                candidate.transfer_id = "<transfer>"
                candidate.handoff_path = str(handoff_path("<transfer>"))
                return _dry_run(candidate, src, composed.text, args.json)
            transfer = _reuse_or_create(store, candidate, now_ms())
        except StartRefused as exc:
            return _fail(args, src.session_id, str(exc))
        code = _drive(store, transfer, src, composed.text, args)
        if code not in (0, EXIT_WAITING):
            return _fail(args, src.session_id, f"transfer {transfer.transfer_id} did not open")
        return code


# ------------------------------------------------------------------ accept / status


HOOK_TIMEOUT_SEC = 60
HOOK_OUTPUT_CAP = 64 * 1024
_PLACEHOLDERS = ("{old}", "{new}", "{label}", "{transfer}")


class HookFailed(Exception):
    """One ``handoff_hooks`` entry did not succeed; the message says why (no output dump)."""


def _render_hook(template: object, transfer: Any) -> list[str]:
    """Substitute the placeholders into one argv template (plain replace, no format())."""
    if (
        not isinstance(template, list)
        or not template
        or not all(isinstance(part, str) for part in template)
    ):
        raise HookFailed(f"handoff_hooks entry is not a non-empty list of strings: {template!r}")
    values = {
        "{old}": transfer.source_session,
        "{new}": transfer.target_session,
        "{label}": transfer.target_label,
        "{transfer}": transfer.transfer_id,
    }
    argv = []
    for part in template:
        for key in _PLACEHOLDERS:
            part = part.replace(key, values[key])
        argv.append(part)
    return argv


def run_bounded(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """Run *argv* (no shell, own process group) bounded by *timeout*; ``(rc, stdout, stderr)``.

    A timeout kills the whole group and raises ``subprocess.TimeoutExpired``; a program that
    cannot start raises ``OSError``. Shared by ``handoff_hooks`` and the facts providers.
    """
    import signal
    import subprocess

    with subprocess.Popen(  # noqa: S603 - argv from the user's own config, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # its own group: a timeout kills grandchildren too
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            raise
        return proc.returncode, stdout, stderr


def run_hook(template: object, transfer: Any) -> dict[str, Any]:
    """Run one hook argv-only (no shell), bounded; its parsed JSON object, or HookFailed."""
    import subprocess

    argv = _render_hook(template, transfer)
    name = os.path.basename(argv[0])
    try:
        returncode, stdout, stderr = run_bounded(argv, HOOK_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise HookFailed(f"{name}: timed out after {HOOK_TIMEOUT_SEC}s") from exc
    except OSError as exc:
        raise HookFailed(f"{name}: could not run ({exc.strerror or exc})") from exc
    if len(stdout) > HOOK_OUTPUT_CAP:
        raise HookFailed(f"{name}: stdout exceeds {HOOK_OUTPUT_CAP} bytes")
    detail = stderr.decode("utf-8", "replace").strip().splitlines()
    if returncode != 0:
        tail = f": {detail[-1][:300]}" if detail else ""
        raise HookFailed(f"{name}: exit {returncode}{tail}")
    try:
        data = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HookFailed(f"{name}: stdout is not JSON") from exc
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise HookFailed(f'{name}: JSON is not an object with "ok": true')
    steps = data.get("next_steps", [])
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        raise HookFailed(f'{name}: "next_steps" is not a list of strings')
    if not isinstance(data.get("items", []), list):
        raise HookFailed(f'{name}: "items" is not a list')
    return data


def cmd_accept(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements
    """``continue-session accept``: the successor takes the work over (hooks, then CAS)."""
    from . import accounts, config
    from .models import now_ms
    from .store import Store
    from .transfer_store import ACCEPTED, FINALIZED, OPENED, PREPARED

    with Store() as store:
        transfer = store.get_transfer(args.transfer)
        if transfer is None:
            print(f"error: no such transfer {args.transfer}", file=sys.stderr)
            return 1
        caller = _env_session()
        if caller != transfer.target_session:
            print(
                f"error: accept must run inside the successor session "
                f"{transfer.target_session}, not {caller or 'an outside shell'}",
                file=sys.stderr,
            )
            return 1
        if not accounts.same_config_dir(accounts.env_config_dir(), transfer.target_config_dir):
            print(
                f"error: this session does not bill the {transfer.target_label!r} account the "
                "transfer was made for — refusing",
                file=sys.stderr,
            )
            return 1
        if transfer.state in (ACCEPTED, FINALIZED):
            print(f"already accepted: transfer {transfer.transfer_id} ({transfer.state})")
            return 0
        if transfer.state not in (PREPARED, OPENED):
            print(
                f"error: transfer {transfer.transfer_id} is {transfer.state}: "
                f"{transfer.failure or 'no longer acceptable'}",
                file=sys.stderr,
            )
            return 1
        steps: list[str] = []
        try:
            for template in config.load_config().handoff_hooks:
                steps += run_hook(template, transfer).get("next_steps", [])
        except HookFailed as exc:
            store.note_transfer(transfer.transfer_id, now_ms(), failure=f"hook: {exc}")
            print(
                f"error: handoff hook failed — {exc}. The transfer stays {transfer.state} and "
                "ownership is unchanged; fix the cause and run accept again.",
                file=sys.stderr,
            )
            return 1
        now = now_ms()
        store.transition_transfer(transfer.transfer_id, OPENED, now)  # a launcher race
        if not store.transition_transfer(transfer.transfer_id, ACCEPTED, now):
            current = store.get_transfer(transfer.transfer_id)
            if current is None or current.state not in (ACCEPTED, FINALIZED):
                print(
                    f"error: transfer {transfer.transfer_id} changed state",
                    file=sys.stderr,
                )
                return 1
        store.note_transfer(transfer.transfer_id, now, failure="")
    if args.json:
        print(json.dumps({"ok": True, "transfer": transfer.transfer_id, "next_steps": steps}))
    else:
        print(
            f"accepted: transfer {transfer.transfer_id} — you now own the work of "
            f"{transfer.source_session}. Relaunch the subagents and restart the shells the "
            "handoff lists, then follow its First steps."
        )
        for step in steps:
            print(f"next: {step}")
    return 0


def recovery_step(transfer: Any, target_is_draft: bool) -> str:
    """The exact next step for *transfer* in its current state."""
    tid = transfer.transfer_id
    return {
        "prepared": "re-run the same `ccc continue-session start` in the source session",
        "opened": (
            "re-run the same `ccc continue-session start` in the source session (the "
            "successor never started; it relaunches it)"
            if target_is_draft
            else f"in the successor run `ccc continue-session accept {tid}`; if its tab is "
            f"gone: `ccc resume {transfer.target_session}` and accept there"
        ),
        "accepted": "re-run the same `ccc continue-session start` in the source (it finalizes)",
        "finalized": "nothing — the handoff is complete",
        "failed": f"fix the cause ({transfer.failure or '?'}) and start a new transfer",
    }.get(transfer.state, "?")


def cmd_status(args: argparse.Namespace) -> int:
    """``continue-session status``: state, snapshot and the recovery step."""
    from .store import Store

    with Store() as store:
        if args.transfer:
            found = store.get_transfer(args.transfer)
            transfers = [found] if found is not None else []
        else:
            sid = args.session or _env_session()
            transfers = store.transfers_of(sid) if sid else []
        if not transfers:
            print("no transfer found", file=sys.stderr)
            return 1
        rows = []
        for transfer in transfers:
            target = store.get(transfer.target_session)
            step = recovery_step(transfer, bool(target is not None and target.draft))
            rows.append({**transfer.to_dict(), "recovery": step})
    if args.json:
        print(json.dumps(rows if not args.transfer else rows[0], indent=2))
        return 0
    for row in rows:
        print(
            f"{row['transfer_id']}  {row['state']:<9}  {row['source_session'][:8]} → "
            f"{row['target_session'][:8]} ({row['target_label']})"
        )
        if row["failure"]:
            print(f"  last failure: {row['failure']}")
        print(f"  handoff: {row['handoff_path']}")
        print(f"  next:    {row['recovery']}")
    return 0


_VERBS = {
    "state": cmd_state,
    "template": cmd_template,
    "drain": cmd_drain,
    "start": cmd_start,
    "accept": cmd_accept,
    "status": cmd_status,
}


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch ``ccc continue-session <verb>``."""
    from . import handoff_facts

    verb = getattr(args, "cs_command", None)
    handler = _VERBS.get(verb or "") or handoff_facts.VERBS.get(verb or "")
    if handler is None:
        args.cs_parser.print_help()
        return 2
    return handler(args)


def add_parser(sub: Any) -> None:
    """Register ``continue-session`` and its verbs on the ccc subparsers *sub*."""
    parser = sub.add_parser(
        "continue-session",
        help="hand this session's work to a NEW session (handoff; see -h)",
        description=(
            "Hand a session's work to a NEW Claude Code session (its own session id, never "
            "--resume) from a handoff document. The cheap alternative to the same-session "
            "switch (/cwork-to-cpriv-cache-write-hit), whose first turn re-writes the "
            "whole conversation into the other organization's prompt cache."
        ),
        epilog=(
            "examples:\n"
            "  ccc continue-session state -j\n"
            "  ccc continue-session template > handoff.md\n"
            "  ccc continue-session drain -t 540"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(func=dispatch, cs_parser=parser)
    verbs = parser.add_subparsers(dest="cs_command")

    p_state = verbs.add_parser("state", help="what a handoff of this session would carry")
    p_state.add_argument("-s", "--session", help="the session id (default: this session)")
    p_state.add_argument("-j", "--json", action="store_true", help="JSON output")

    verbs.add_parser("template", help="print the fixed handoff template")

    p_drain = verbs.add_parser(
        "drain", help="block until no subagent / background shell is in flight"
    )
    p_drain.add_argument("-s", "--session", help="the session id (default: this session)")
    p_drain.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=DRAIN_DEFAULT_SEC,
        help=f"give up after this many seconds (default {DRAIN_DEFAULT_SEC}); exit 1",
    )
    p_drain.add_argument("-j", "--json", action="store_true", help="JSON output")

    p_start = verbs.add_parser(
        "start",
        help="validate the handoff, open a NEW session on an account, finalize on accept",
        epilog=(
            "example (from inside the session, after drain):\n"
            "  ccc continue-session start -A private -f /path/handoff.md  # new-session-ok"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_start.add_argument(
        "-A", "--account", required=True, help="target account label (see claude_accounts)"
    )
    p_start.add_argument("-f", "--file", help="the filled handoff document")
    p_start.add_argument(
        "-C",
        "--capped",
        action="store_true",
        help="capped seat: the router writes the handoff (llm_custom_command, "
        "purpose handoff-summary); spawned by the UserPromptSubmit intercept",
    )
    p_start.add_argument(
        "-F",
        "--facts-only",
        action="store_true",
        help="no model turn: the three model sections are written by code from the facts "
        "(emergency relay; takes no -f and no --capped)",
    )
    p_start.add_argument(
        "-I",
        "--await-idle",
        action="store_true",
        help="first wait (up to 120 s) until the session is provably idle with a still "
        "transcript; otherwise exit without a transfer",
    )
    p_start.add_argument("-s", "--session", help="the source session id (default: this session)")
    p_start.add_argument("-O", "--overseer", help="model of the new session (default: observed)")
    p_start.add_argument("-E", "--executor", help="executor model for delegated work")
    p_start.add_argument(
        "-w",
        "--wait",
        type=int,
        default=ACCEPT_WAIT_DEFAULT_SEC,
        help=f"seconds to wait for the successor's accept (default {ACCEPT_WAIT_DEFAULT_SEC})",
    )
    p_start.add_argument(
        "-n", "--dry-run", action="store_true", help="print prompt and argv; launch nothing"
    )
    p_start.add_argument("-j", "--json", action="store_true", help="JSON output (dry run)")

    p_accept = verbs.add_parser(
        "accept", help="(in the successor) take the handed-off work over; runs handoff_hooks"
    )
    p_accept.add_argument("transfer", help="the transfer id from the handoff preamble")
    p_accept.add_argument("-j", "--json", action="store_true", help="JSON output")

    p_status = verbs.add_parser("status", help="a transfer's state and its recovery step")
    p_status.add_argument("transfer", nargs="?", help="transfer id (default: this session's)")
    p_status.add_argument("-s", "--session", help="list the transfers of this session id")
    p_status.add_argument("-j", "--json", action="store_true", help="JSON output")

    from . import handoff_facts

    handoff_facts.add_parsers(verbs)
