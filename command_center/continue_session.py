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


def inventory(adapter: ClaudeAdapter, store: Store, src: Source) -> Inventory:
    """The in-flight inventory of *src* (store counter included)."""
    from . import inflight

    return inflight.inventory(
        adapter,
        src.pid,
        src.transcript,
        src.ignore,
        active_subagents=store.active_subagents(src.session_id),
    )


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
    }


def _render_state(data: dict[str, Any]) -> str:
    lines = [
        f"session:   {data['session_id']} ({data['account'] or '?'}, pid {data['live_pid']})",
        f"cwd:       {data['cwd']}",
        f"model:     {data['model'] or '?'}  no_codex: {data['no_codex']}",
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
        "every shell under 'Background restarts', then follow 'First steps'. If accept "
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


def _preflight(adapter: ClaudeAdapter, store: Store, src: Source, label: str) -> str:
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
    accounts.ensure_trusted(target, src.cwd)  # the successor must not park on the dialog
    if not accounts.is_trusted(target, src.cwd):
        raise StartRefused(f"could not confirm {src.cwd} is trusted for the {label!r} account")
    inv = inventory(adapter, store, src)
    if not inv.clear:
        raise StartRefused(
            "work is still in flight — drain first (ccc continue-session drain): "
            + "; ".join(inv.reasons)
        )
    return target


def _candidate(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store, src: Source, target_dir: str, label: str, args: Any, digest: str
) -> Transfer:
    """The snapshot this ``start`` would write (target id and transfer id still unset)."""
    from .transfer_store import Transfer

    session = store.get(src.session_id)
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
        prompt_sha256=digest,
        capped=bool(getattr(args, "capped", False)),
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


def cmd_start(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements
    """``continue-session start``: hand this session's work to a NEW session."""
    from . import handoff_doc
    from .models import now_ms
    from .transfer_store import ACCEPTED

    adapter, store, src = _open_source(args)
    with store:
        if src is None:
            print("error: could not resolve a session (pass -s/--session <id>)", file=sys.stderr)
            return 1
        active = store.active_transfer(src.session_id)
        if active is not None and active.state == ACCEPTED:  # a re-run after acceptance
            return _drive(store, active, src, "", args)
        session = store.get(src.session_id)
        if session is not None and session.handed_off_to:
            print(f"error: {src.session_id} was already handed off", file=sys.stderr)
            return 1
        try:
            raw = Path(args.file).read_bytes()
        except OSError as exc:
            print(f"error: cannot read the handoff file: {exc}", file=sys.stderr)
            return 1
        doc = handoff_doc.validate(raw)
        if not doc.ok:
            print("error: handoff rejected — " + "; ".join(doc.errors), file=sys.stderr)
            return 1
        try:
            target_dir = _preflight(adapter, store, src, args.account)
            candidate = _candidate(store, src, target_dir, args.account, args, doc.sha256)
            print(_account_change_note(src, candidate), file=sys.stderr)
            if args.dry_run:
                candidate.transfer_id = "<transfer>"
                candidate.handoff_path = str(handoff_path("<transfer>"))
                return _dry_run(candidate, src, doc.text, args.json)
            transfer = _reuse_or_create(store, candidate, now_ms())
        except StartRefused as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return _drive(store, transfer, src, doc.text, args)


_VERBS = {"state": cmd_state, "template": cmd_template, "drain": cmd_drain, "start": cmd_start}


def main(args: argparse.Namespace) -> int:
    """Dispatch ``ccc continue-session <verb>``."""
    verb = getattr(args, "cs_command", None)
    handler = _VERBS.get(verb or "")
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
    parser.set_defaults(func=main, cs_parser=parser)
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
    p_start.add_argument("-f", "--file", required=True, help="the filled handoff document")
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
