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

The transfer itself (``start`` / ``accept`` / ``status`` / ``abort``) runs the state machine of
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
import dataclasses
import hashlib
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


def resolve_source(
    adapter: ClaudeAdapter, store: Store, explicit: str | None, *, outside: bool = False
) -> Source | None:
    """The source session: ``-s`` > this session's env > the live session in cwd.

    *outside* (``start -X``, tp#1722): only ``-s`` names the source, never the env, and the
    caller is treated as another shell — account, cwd, pid and transcript come from the
    registry/store and no process ancestry is ignored.
    """
    from . import accounts, terminal
    from .models import current_model

    if outside and not explicit:
        return None
    env_sid = "" if outside else _env_session()
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
    # tp#2032: a superseded observed model (opus-5) is never carried into the successor.
    model = current_model((stored.model if stored else "") or (getattr(live, "model", "") or ""))
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


def peek_inventory(adapter: ClaudeAdapter, store: Store, src: Source) -> Inventory:
    """The in-flight inventory of *src*, read-only (a stale counter is left as is)."""
    from . import inflight

    return inflight.inventory(
        adapter,
        src.pid,
        src.transcript,
        src.ignore,
        active_subagents=store.active_subagents(src.session_id),
        subagent_started_at=store.subagent_started_at(src.session_id),
    )


def inventory(adapter: ClaudeAdapter, store: Store, src: Source) -> Inventory:
    """The in-flight inventory of *src* (store counter included; a stale one is reset)."""
    inv = peek_inventory(adapter, store, src)
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
    from .inflight import item_detail

    head = "in flight: clear" if inv["clear"] else "in flight: NOT clear"
    lines = [head]
    lines += [f"  reason: {reason}" for reason in inv["reasons"]]
    for item in inv["items"]:
        detail = item_detail(item)
        lines.append(f"  {item['kind']:<9} {item['id']:<18} {detail}")
    return "\n".join(lines)


def _open_source(args: argparse.Namespace) -> tuple[Any, Any, Source | None]:
    from .store import Store

    adapter = _adapter()
    store = Store()
    outside = bool(getattr(args, "outside", False))
    return (
        adapter,
        store,
        resolve_source(adapter, store, getattr(args, "session", None), outside=outside),
    )


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


def _drain_source(
    adapter: ClaudeAdapter, store: Store, src: Source, timeout: float
) -> tuple[bool, Inventory]:
    """The ONE in-flight wait of ``drain`` and ``start -D``: ``(clear, last inventory)``.

    Keep-warm timers never count as in flight (the detector's rule); nothing is signalled.
    """
    from . import inflight

    return inflight.drain(
        adapter,
        src.pid,
        src.transcript,
        timeout=timeout,
        poll=DRAIN_POLL_SEC,
        ignore=src.ignore,
        active_subagents=lambda: store.active_subagents(src.session_id),
        subagent_started_at=lambda: store.subagent_started_at(src.session_id),
        on_stale_counter=lambda n: _clear_stale_counter(store, src.session_id, n),
    )


def cmd_drain(args: argparse.Namespace) -> int:
    """``continue-session drain``: block until nothing is in flight (exit 1 on timeout)."""
    adapter, store, src = _open_source(args)
    with store:
        if src is None:
            print("error: could not resolve a session (pass -s/--session <id>)", file=sys.stderr)
            return 1
        ok, inv = _drain_source(adapter, store, src, float(args.timeout))
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
START_DRAIN_MAX_SEC = 60  # `start -D`: the longest in-flight wait inside the one call
START_CALL_BUDGET_SEC = 540  # the whole `start` call stays under the 600 s Bash tool cap
START_CALL_MARGIN_SEC = 30  # launch, compose and finalize besides the timed parts
ACCEPT_WAIT_FLOOR_SEC = 120  # the clamp never cuts the accept wait below this


class StartRefused(Exception):
    """A fail-closed preflight refusal; the message is printed, nothing was written."""


#: tp#1681 L3c: once the exit of a model-written part over MODEL_MAX_CHARS. Never returned
#: since tp#2172 D1 (the part is cut, the full text kept in a sidecar); kept for the hook's
#: older texts that still name it.
EXIT_MODEL_TOO_LONG = 2


def effective_wait(wait: int, drain: int, provider_timeouts: list[float]) -> int:
    """The accept wait clamped so one ``start`` call stays under the Bash tool cap.

    ``min(wait, max(floor, budget − drain − Σ provider timeout_s − margin))``: the clamp
    only ever lowers ``-w``, and never below ``ACCEPT_WAIT_FLOOR_SEC`` (an explicitly
    smaller ``-w`` is kept).
    """
    room = START_CALL_BUDGET_SEC - drain - sum(provider_timeouts) - START_CALL_MARGIN_SEC
    return min(wait, max(ACCEPT_WAIT_FLOOR_SEC, int(room)))


def _provider_timeouts() -> list[float]:
    """The ``timeout_s`` of every configured ``handoff_facts`` provider (they run in order)."""
    from . import config, handoff_facts

    specs, _bad = handoff_facts.parse_specs(config.load_config().handoff_facts)
    return [spec.timeout_s for spec in specs]


def _drain_seconds(value: str) -> int:
    """argparse type of ``start -D``: whole seconds in ``0..START_DRAIN_MAX_SEC``."""
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a whole number of seconds: {value!r}") from exc
    if not 0 <= seconds <= START_DRAIN_MAX_SEC:
        raise argparse.ArgumentTypeError(f"must be 0..{START_DRAIN_MAX_SEC} seconds, not {seconds}")
    return seconds


def _launch(target_session: str) -> str:
    """Open the successor's tab (``ccc start-job <id>``); the launcher rung or ``""``."""
    from . import terminal

    return terminal.start_job_launch(target_session)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _monotonic() -> float:
    """The clock of ``accept``'s retry window (a seam next to ``_sleep``)."""
    return time.monotonic()


def _job_model(observed: str, explicit: str | None) -> str:
    """Q1: an explicit ``-O``/``-E`` wins; else the observed model when launchable; else default.

    *observed* (and *explicit*) may be a ccc label (``fable-5.2``) or a raw ``claude
    --model`` id (``claude-fable-5-2``, the live-entry fallback): a known id resolves to
    its label (tp#2330), a retired one to its replacement, and an id ccc has no row for
    (a newer model) passes through unchanged (tp#2335). Only an empty or non-token value
    (``<synthetic>``) takes the default; an explicit one of those is refused.
    """
    from .models import (
        NEW_JOB_DEFAULT_LLM,
        NEW_JOB_LLM_CHOICES,
        RETIRED_LLM_REPLACEMENTS,
        is_launchable_llm,
        model_label,
    )

    if explicit:
        label = model_label(explicit)
        if not is_launchable_llm(label):
            raise StartRefused(
                f"unknown model {explicit!r} (choices: {', '.join(NEW_JOB_LLM_CHOICES)}, "
                "or a model id)"
            )
        return label
    label = model_label(observed)
    label = RETIRED_LLM_REPLACEMENTS.get(label, label)
    return label if is_launchable_llm(label) else NEW_JOB_DEFAULT_LLM


def _notice_unknown_model(observed: str, overseer: str, executor: str) -> None:
    """One stderr line when the source's model is no launchable id (tp#2330, tp#2335).

    Any plain model id is carried over as-is; only a non-token observation (a transcript
    placeholder such as ``<synthetic>``) falls back to the default, and the relay says so
    instead of switching models silently.
    """
    from .models import RETIRED_LLM_REPLACEMENTS, is_launchable_llm, model_label

    label = model_label(observed)
    if not observed or is_launchable_llm(RETIRED_LLM_REPLACEMENTS.get(label, label)):
        return
    used = overseer if overseer == executor else f"{overseer} / {executor}"
    print(
        f"notice: source model {observed!r} is not a model id; "
        f"the successor runs on {used} (pass -O/-E to choose)",
        file=sys.stderr,
    )


def handoff_path(transfer_id: str) -> Path:
    """Where the vouched handoff of *transfer_id* is stored (``<app_home>/handoffs/``)."""
    from . import config

    return config.app_home() / "handoffs" / f"{transfer_id}.md"


MODEL_FULL_SUFFIX = ".model-full.md"  # tp#2172 D1: the uncut model part
FACTS_REST_SUFFIX = ".facts-rest.md"  # tp#2172 D2: the fact records moved out for size


def sidecar_path(transfer: Any, suffix: str) -> Path:
    """A handoff sidecar of *transfer*: ``<transfer><suffix>`` next to its handoff file."""
    return Path(transfer.handoff_path).with_name(f"{transfer.transfer_id}{suffix}")


def _sidecar_lines(model_full: str, facts_rest: str) -> str:
    """The preamble's lines naming the sidecars (paths; ``""`` = none)."""
    out = ""
    if model_full:
        out += (
            f"The model part below was cut for size; its full text is at {model_full}: read "
            "it only if a section below ends with `continued`.\n"
        )
    if facts_rest:
        out += f"Fact records moved out of the handoff for size are listed at {facts_rest}.\n"
    return out


# The preamble's first words: the successor's UserPromptSubmit hook accepts only a prompt
# that holds them (``hooks.HANDOFF_PROMPT_HEAD``, pinned equal by a test; tp#2326 D1).
HANDOFF_PROMPT_HEAD = "[handoff] You take over the work of session "


def preamble(
    transfer: Any, source_label: str, transcript: str, *, model_full: str = "", facts_rest: str = ""
) -> str:
    """D4: the short instruction block the successor reads before the handoff text.

    tp#1681 L3c: cut facts are fetched by KEY only (``facts <src> -k <key>``, the key each
    ``… cut`` line names), never as the whole snapshot; nothing here asks to re-claim work
    (ownership moves on ``accept`` through ``handoff_hooks``). tp#1681 L18: the successor's
    UserPromptSubmit hook accepts on this very prompt (tp#2326 D1: the text must hold
    :data:`HANDOFF_PROMPT_HEAD` and name ``transfer <id>``); its note starts with
    ``AUTO_ACCEPT_HEAD``. tp#2326 D2: the source's launch profile is named when set.
    tp#2172 D1/D2: *model_full* / *facts_rest* name the sidecars of a cut model part and of
    fact records moved out for size.
    """
    accepted_note = AUTO_ACCEPT_HEAD.format(transfer=transfer.transfer_id)
    profile = str(getattr(transfer, "launch_profile", "") or "")
    route = f", launch profile {profile}" if profile else ""
    return (
        f"{HANDOFF_PROMPT_HEAD}{transfer.source_session} "
        f"(account {source_label or '?'}{route}), transfer {transfer.transfer_id}. "
        f"The handoff below is also stored at {transfer.handoff_path}"
        + (f"; the old session's transcript is {transcript} (evidence only)" if transcript else "")
        + ".\n"
        + _sidecar_lines(model_full, facts_rest)
        + f"FIRST, unless a ccc hook note on this prompt reads `{accepted_note}` (the hook "
        "already accepted it), run (pass the Bash tool timeout 600000: under load accept "
        "retries a slow handoff hook itself for up to ~8 min): ccc continue-session accept "
        f"{transfer.transfer_id}\n"
        "Only after it succeeds: relaunch every agent under 'Subagent continuations' as a NEW "
        "Agent from its capsule (old agent ids cannot be resumed across sessions), restart "
        "ONLY the shells under 'Background restarts' marked `restart:` (declared restartable; "
        "the ones under 'Decide before restarting' are evidence, judge each before re-running "
        "anything), then follow 'First steps'. A fact cut for size: run only "
        f"`ccc continue-session facts {transfer.source_session} -k <key>` with the key its "
        "cut line names, once per key you need; never read the whole snapshot. accept "
        "already retries a "
        "refusing or timed-out hook; if it still fails and the error says the transfer is "
        "still opened, run accept once more; stop and report on any other failure: the old "
        "session stays authoritative until it succeeds.\n"
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


def _preflight(  # pylint: disable=too-many-arguments
    adapter: ClaudeAdapter,
    store: Store,
    src: Source,
    label: str,
    *,
    dry_run: bool = False,
    outside: bool = False,
) -> str:
    """Fail-closed checks before anything is written; returns the target config dir.

    *outside* (``start -X``) skips only the inside-the-source check: the provable-idle
    wait (``_await_idle``) stands in for it, every other check still applies.
    """
    from . import accounts

    if not src.inside and not outside:
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
            + "; ".join(inv.reasons + inv.unknown_processes())
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
    overseer = _job_model(src.model, getattr(args, "overseer", None))
    executor = _job_model(
        src.model, getattr(args, "executor", None) or getattr(args, "overseer", None)
    )
    if not getattr(args, "overseer", None):  # an explicit -O sets both: nothing inherited
        _notice_unknown_model(src.model, overseer, executor)
    return Transfer(
        transfer_id="",
        source_session=src.session_id,
        source_config_dir=src.config_dir,
        target_config_dir=target_dir,
        target_label=label,
        cwd=src.cwd,
        source_aim=(session.aim if session else "") or "",
        source_aim_rev=store.count_aim_history(src.session_id),
        llm_overseer=overseer,
        llm_exec=executor,
        no_codex=bool(session and session.no_codex),
        launch_profile=profile,
        prompt_sha256=digest,
        capped=bool(getattr(args, "capped", False)),
        facts_snapshot=facts_snapshot,
    )


def _carried_aim(store: Store, transfer: Any) -> tuple[str, str] | None:
    """The source's latest user AIM (``Store.latest_user_aim``) with a non-NULL stamp.

    Its stamp is carried so a chain A→B→C still finds it in B; an unknown author is
    stamped ``relay`` (a handoff copied it, tp#1991), so no successor row is NULL.
    """
    from .models import AIM_SET_BY_RELAY

    carried = store.latest_user_aim(transfer.source_session)
    if carried is None:
        return None
    text, set_by = carried
    return text, set_by or AIM_SET_BY_RELAY


def _successor_aim(store: Store, transfer: Any) -> tuple[str, str]:
    """The AIM a successor starts with and its ``aim_history.set_by`` stamp (tp#1566).

    The latest AIM the user set in the source (:func:`_carried_aim`); else the source's
    recorded AIM at ``start`` (the stamp of the source row holding it, else ``relay``);
    else a generic pointer at the source, stamped ``tool`` (ccc wrote it, tp#1991).
    """
    from .models import AIM_SET_BY_RELAY, AIM_SET_BY_TOOL

    carried = _carried_aim(store, transfer)
    if carried is not None:
        return carried
    if transfer.source_aim:
        stamp = store.stamp_of(transfer.source_session, transfer.source_aim)
        return transfer.source_aim, stamp or AIM_SET_BY_RELAY
    pointer = f"continue the work handed over by session {transfer.source_session[:8]}"
    return pointer, AIM_SET_BY_TOOL


def _reconcile_successor_aim(store: Store, transfer: Any) -> bool:
    """Give the successor the source's latest user AIM again; True if it changed (tp#1566).

    ``accept`` calls this once, after ``carry_first_aim``: it covers the user changing the
    source's AIM between ``start`` and ``accept``. Idempotent — a target that already holds
    that AIM is left alone (no new history row). Stamped like :func:`_carried_aim`.
    """
    carried = _carried_aim(store, transfer)
    target = store.get(transfer.target_session)
    if carried is None or target is None:
        return False
    text, set_by = carried
    if (target.aim or "") == text:
        return False
    return store.set_aim(transfer.target_session, text, set_by=set_by)


def _create_successor(store: Store, transfer: Any, src: Source, text: str) -> None:
    """The successor's draft job (idempotent): AIM, cwd, account, models, no_codex, prompt.

    The preamble names each sidecar of *transfer* that exists on disk (tp#2172 D1/D2)."""
    if store.get(transfer.target_session) is not None:
        return
    named = {
        key: str(path) if path.is_file() else ""
        for key, path in (
            ("model_full", sidecar_path(transfer, MODEL_FULL_SUFFIX)),
            ("facts_rest", sidecar_path(transfer, FACTS_REST_SUFFIX)),
        )
    }
    unchecked = [g.text for g in store.list_subgoals(src.session_id) if not g.checked]
    aim, aim_set_by = _successor_aim(store, transfer)
    store.create_draft(
        transfer.target_session,
        transfer.cwd,
        aim,
        aim_set_by=aim_set_by,
        prompt=preamble(transfer, src.label, str(src.transcript or ""), **named) + text,
        no_codex=transfer.no_codex,
        llm_overseer=transfer.llm_overseer,
        llm_exec=transfer.llm_exec,
        config_dir=transfer.target_config_dir,
        launch_profile=transfer.launch_profile,
        lean_start=True,  # tp#1681 L7w: the successor starts with quiet SessionStart hooks
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


@dataclass(frozen=True)
class Sidecars:
    """The scrubbed texts ``start`` keeps beside a handoff (tp#2172 D1/D2): the uncut model
    part (with its body size) and the fact records moved out for size; ``""`` = none."""

    model_full: str = ""
    model_chars: int = 0
    facts_rest: str = ""


NO_SIDECARS = Sidecars()


def _model_full_note(chars: int, path: Path | str) -> str:
    from . import handoff_doc

    return (
        f"note: model part {chars} chars, cut to {handoff_doc.MODEL_MAX_CHARS}; full text in {path}"
    )


def _write_sidecars(transfer: Any, sidecars: Sidecars) -> None:
    """Write *transfer*'s sidecars next to its handoff (0600, already scrubbed); a failed
    write only warns: the handoff itself is whole and every moved fact stays in the
    snapshot (``facts -k``)."""
    for text, suffix in (
        (sidecars.model_full, MODEL_FULL_SUFFIX),
        (sidecars.facts_rest, FACTS_REST_SUFFIX),
    ):
        if not text:
            continue
        path = sidecar_path(transfer, suffix)
        try:
            _write_handoff(path, text)
        except OSError as exc:
            print(f"warning: could not write {path}: {exc}", file=sys.stderr)
            continue
        if suffix == MODEL_FULL_SUFFIX:
            print(_model_full_note(sidecars.model_chars, path), file=sys.stderr)
        else:
            print(f"note: fact records moved out for size: {path}", file=sys.stderr)


def _mark_retired(transfer: Any) -> None:
    """Write the source's relay retired marker (tp#1536); a failure only warns — the
    handoff itself never depends on the relay hooks."""
    from . import relay_state

    try:
        relay_state.write_retired(
            transfer.source_session, transfer.transfer_id, transfer.target_session
        )
    except OSError as exc:
        print(f"warning: could not write the relay retired marker: {exc}", file=sys.stderr)


def finalize(store: Store, transfer: Any, *, arm_close: bool = True) -> bool:
    """Retire the source once the successor ACCEPTED: lineage, own close, CAS.

    The source's AIM is never touched: it stays exactly as the user set it (no new
    ``aim_history`` row either). The finished handoff shows as status ``handed_off`` plus
    ``handed_off_to`` (the ``⇢ <id8>`` marker), written by ``set_handoff_lineage``.
    The session writes come first and are idempotent, the ``accepted → finalized`` CAS
    last — so a crash in between is healed by re-running ``accept`` in the successor
    (:func:`finalize_handoff_source` finds the still-``accepted`` transfer and finalizes
    again; a still-running source ``start`` does the same). ``False`` when the transfer is not
    ``accepted`` (any more).
    """
    from .models import now_ms
    from .transfer_store import ACCEPTED, FINALIZED

    current = store.get_transfer(transfer.transfer_id)
    if current is None or current.state not in (ACCEPTED, FINALIZED):
        return False
    if current.state == FINALIZED:
        return True
    _mark_retired(current)  # idempotent; accept normally wrote it already
    now = now_ms()
    session = store.get(current.source_session)
    store.set_handoff_lineage(current.source_session, current.target_session, now)
    if arm_close and session is not None:
        # ONE close token per source: a pending or in-flight close (another finalizer, a
        # `mark-done --close`) is kept, never overwritten; its own Stop chain closes the tab.
        store.ensure_close_armed(current.source_session, now)
    return store.transition_transfer(current.transfer_id, FINALIZED, now) or (
        (store.get_transfer(current.transfer_id) or current).state == FINALIZED
    )


class FinalizeFailed(Exception):
    """:func:`finalize_handoff_source` could not finalize (the transfer is not accepted)."""


def _source_alive(adapter: ClaudeAdapter, session_id: str) -> bool:
    """Does *session_id* have a live registry entry? An unreadable registry counts as
    alive: the closer then decides, and it refuses on any doubt (nothing is signalled)."""
    try:
        return any(e.session_id == session_id and e.alive for e in adapter.discover())
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return True


def _spawn_closer(transfer_id: str, session_id: str) -> bool:
    """Start the detached token-scoped handoff closer (``close-now --handoff-transfer``)."""
    from . import spawn

    return spawn.spawn_ccc(
        ["close-now", "--handoff-transfer", transfer_id, "--session", session_id]
    )


def finalize_handoff_source(store: Store, adapter: ClaudeAdapter, transfer: Any) -> dict[str, Any]:
    """Retire an ACCEPTED transfer's source from the successor side (tp#1582).

    ``accept`` is the primary finalizer: a source whose own ``start`` timed out sits idle
    and has no next Stop to run :func:`finalize` or claim a close. Non-capped:
    :func:`finalize` (close token ensured, AIM, lineage, the ``finalized`` CAS last) and,
    when the source process is alive and its close token is PENDING, the detached B3 closer
    (it proves stable idle before any signal and refuses a busy source, handing the token
    back for the source's own Stop chain). A token already in flight (a closer or the
    source's Stop chain holds it) spawns nothing; a gone source is only finalized. Capped:
    finalize the metadata, no token, no spawn — its tab stays open. Idempotent: a duplicate
    ``accept`` on an ``accepted`` or ``finalized`` transfer heals a crash in between.

    Returns the ``"source"`` record of ``accept -j``: ``session``, ``state``, ``capped``,
    ``alive`` and ``closer`` (``spawned`` | ``in_flight`` | ``spawn_failed`` | ``none``).
    :class:`FinalizeFailed` when the transfer is not ``accepted``/``finalized``.
    """
    current = store.get_transfer(transfer.transfer_id) or transfer
    sid = current.source_session
    capped = bool(current.capped)
    if not finalize(store, current, arm_close=not capped):
        raise FinalizeFailed(f"transfer {current.transfer_id} is {current.state}, not accepted")
    record: dict[str, Any] = {
        "session": sid,
        "state": "finalized",
        "capped": capped,
        "alive": False,
        "closer": "none",
    }
    if capped:
        return record
    record["alive"] = _source_alive(adapter, sid)
    row = store.get(sid)
    if not record["alive"] or row is None or not row.close_token:
        return record
    if not row.close_requested_at:  # claimed: a closer or the source's own Stop holds it
        record["closer"] = "in_flight"
        return record
    spawned = _spawn_closer(current.transfer_id, sid)
    record["closer"] = "spawned" if spawned else "spawn_failed"
    return record


def _source_line(record: dict[str, Any], transfer_id: str) -> str:
    """The one line ``accept`` prints about the source (stderr for a warning)."""
    sid8 = str(record.get("session") or "")[:8]
    if record.get("capped"):
        return f"source {sid8} — finalized (capped: its tab stays open)"
    if not record.get("alive"):
        return f"source {sid8} gone — finalized"
    closer = record.get("closer")
    if closer in ("spawned", "in_flight"):
        return f"source {sid8} — finalized, closing its tab when idle"
    if closer == "spawn_failed":
        return (
            f"warning: source {sid8} — finalized, but its closer did not start; run "
            f"`ccc continue-session accept {transfer_id}` again here to retry"
        )
    return f"source {sid8} — finalized (no close pending)"


def _finalize_after_accept(store: Store, transfer: Any) -> dict[str, Any]:
    """:func:`finalize_handoff_source` for ``accept`` — a failure only warns (``accept``
    already succeeded): the record then carries ``state`` = ``error`` and the reason."""
    try:
        return finalize_handoff_source(store, _adapter(), transfer)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(
            f"warning: accepted, but the source {transfer.source_session[:8]} was not "
            f"finalized ({exc}); run `ccc continue-session accept {transfer.transfer_id}` "
            "again here to retry — never resume the source",
            file=sys.stderr,
        )
        return {"session": transfer.source_session, "state": "error", "error": str(exc)}


def _say_source(record: dict[str, Any], transfer_id: str, as_json: bool) -> None:
    """Print :func:`_source_line` (text mode; a warning goes to stderr in both modes)."""
    if record.get("state") == "error":
        return  # _finalize_after_accept already warned
    line = _source_line(record, transfer_id)
    if line.startswith("warning:"):
        print(line, file=sys.stderr)
    elif not as_json:
        print(line)


# ------------------------------------------------------- successor liveness (tp#2172 D4)

LAUNCH_GRACE_S = 120  # a successor still a draft this long after its (re)launch never started
START_GRACE_ENV = "CCC_RELAY_START_GRACE_S"  # R1: 0 = an unregistered successor aborts at once
START_GRACE_DEFAULT_S = 90  # registry entry missing this long after the draft cleared: dead
NO_ACCEPT_ENV = "CCC_RELAY_NO_ACCEPT_S"
NO_ACCEPT_DEFAULT_S = 240  # registered, transcript still, no accept: above tp#1805's 180 s hooks
GONE_POLLS = 2  # a registered successor missing from the registry on this many polls in a row
AUTO_RELAUNCHES = 1  # D5 (Q3): one automatic relaunch per `start` call
STARTING = "starting"
LIVE = "live"
DEAD_PREFIX = "dead:"
NO_FIRST_TURN = "no-first-turn"  # tp#2326 D3: registered, alive, its first prompt never sent
NO_FIRST_TURN_CAUSES = (
    "a trust dialog, a stalled start hook, or a launch route that did not submit the prompt"
)


def _now_ms() -> int:
    """Wall-clock epoch-ms of the liveness rules (a seam next to ``_monotonic``)."""
    from .models import now_ms

    return now_ms()


def _knob(name: str, default: int, minimum: int) -> int:
    """A whole-seconds environment knob; unset → *default*, invalid → *default* + a note."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if value < minimum:
        print(
            f"note: {name}={raw!r} is not a whole number of seconds >= {minimum}; using {default}",
            file=sys.stderr,
        )
        return default
    return value


@dataclass
class SuccessorWatch:  # pylint: disable=too-many-instance-attributes  # knobs + one wait's memory
    """The liveness knobs of one ``start`` call and what ONE wait remembers (tp#2172 D4).

    ``seen_registered`` / ``missing_polls`` turn a vanished registry entry into
    ``dead:process-gone`` only after it was seen alive in THIS wait and then missed on
    ``GONE_POLLS`` polls in a row; ``transcript_size`` / ``grown_at`` record the successor
    transcript's growth for ``dead:no-accept``; ``launched_ms`` is when this call
    (re)launched a still-draft successor (the never-started grace restarts there).
    """

    start_grace_s: int = START_GRACE_DEFAULT_S
    no_accept_s: int = NO_ACCEPT_DEFAULT_S
    launched_ms: int = 0
    seen_registered: bool = False
    missing_polls: int = 0
    transcript_size: int = -1
    grown_at: int = 0

    @classmethod
    def from_env(cls) -> SuccessorWatch:
        """The knobs, read ONCE per call (an invalid value falls back with a stderr note)."""
        return cls(
            start_grace_s=_knob(START_GRACE_ENV, START_GRACE_DEFAULT_S, 0),
            no_accept_s=_knob(NO_ACCEPT_ENV, NO_ACCEPT_DEFAULT_S, 1),
        )

    def fresh(self) -> SuccessorWatch:
        """Same knobs, no memory: the watch of a relaunched successor."""
        return SuccessorWatch(start_grace_s=self.start_grace_s, no_accept_s=self.no_accept_s)


def _transcript_growth(
    adapter: ClaudeAdapter, transfer: Any, now: int, watch: SuccessorWatch
) -> int:
    """Epoch-ms of the successor transcript's last growth (its mtime, or a size change this
    wait observed); 0 while it has no transcript."""
    try:
        path = adapter.transcript_path(
            transfer.cwd, transfer.target_session, transfer.target_config_dir or None
        )
        stat = path.stat() if path is not None else None
    except OSError:
        stat = None
    if stat is None:
        return watch.grown_at
    if 0 <= watch.transcript_size != stat.st_size:
        watch.grown_at = now
    watch.transcript_size = stat.st_size
    return max(watch.grown_at, int(stat.st_mtime * 1000))


def first_turn_seen(store: Store, adapter: ClaudeAdapter, transfer: Any) -> bool:
    """Did *transfer*'s successor ever get its first prompt? (tp#2326 D3)

    Any one signal, cheapest first: the successor row's ``prompt_count > 0`` (the
    ``UserPromptSubmit`` hook counts it); a ``matched`` ``start_job`` row in the target's
    input ledger; or a human prompt in its transcript (:func:`has_human_prompt`). Reads
    only, never ``advance_session``. A signal that raises counts as seen: a false
    ``False`` costs the relaunch and closes a tab that may hold a turn.
    """
    from .adapters.claude import has_human_prompt

    target = transfer.target_session
    try:
        row = store.get(target)
        if row is not None and row.prompt_count > 0:
            return True
        if any(
            r.kind == "start_job" and r.state == "matched"
            for r in store.inputs_for(target, 0, 2**62)
        ):
            return True
        path = adapter.transcript_path(transfer.cwd, target, transfer.target_config_dir or None)
        return path is not None and has_human_prompt(path)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return True


def successor_state(
    store: Store,
    adapter: ClaudeAdapter,
    transfer: Any,
    now: int,
    watch: SuccessorWatch | None = None,
) -> str:
    """``starting`` | ``live`` | ``dead:<why>`` for *transfer*'s successor (tp#2172 D4).

    * ``dead:never-started`` — still a draft ``LAUNCH_GRACE_S`` after ``opened_at`` (or this
      call's relaunch of it);
    * ``starting`` / ``dead:never-registered`` — no longer a draft but no live entry in
      Claude's registry, less / at least ``start_grace_s`` after ``target_launched_at``
      (claude registers some seconds after its draft clears);
    * ``dead:process-gone`` — an entry seen alive in this wait is gone or its pid dead on
      ``GONE_POLLS`` polls in a row (no grace: a closed tab ends the process);
    * ``dead:no-accept`` — registered and alive, but no transcript growth and no accept for
      ``no_accept_s`` since ``target_launched_at`` or the last growth, after its first
      prompt (:func:`first_turn_seen`);
    * ``dead:no-first-turn`` — the same, but the first prompt never arrived (tp#2326 D3:
      a trust dialog, a stalled start hook, or a launch route that did not submit the
      prompt; the successor's tab shows which);
    * ``live`` — anything else. An unreadable registry gives no verdict on that poll.

    Liveness is the adapter's registry read only, never a ``ps`` of some pid. *watch*
    carries the per-wait memory (a fresh one per call when omitted).
    """
    watch = watch if watch is not None else SuccessorWatch.from_env()
    target = transfer.target_session
    row = store.get(target)
    if row is None or row.draft:
        since = max(transfer.opened_at or transfer.created_at, watch.launched_ms)
        return DEAD_PREFIX + "never-started" if now - since >= LAUNCH_GRACE_S * 1000 else STARTING
    launched = transfer.target_launched_at or transfer.opened_at or transfer.created_at
    try:
        alive = any(e.session_id == target and e.alive for e in adapter.discover())
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return LIVE if watch.seen_registered else STARTING
    if not alive:
        if watch.seen_registered:
            watch.missing_polls += 1
            return DEAD_PREFIX + "process-gone" if watch.missing_polls >= GONE_POLLS else LIVE
        if now - launched >= watch.start_grace_s * 1000:
            return DEAD_PREFIX + "never-registered"
        return STARTING
    watch.seen_registered, watch.missing_polls = True, 0
    last = max(launched, _transcript_growth(adapter, transfer, now, watch))
    stuck = now - last >= watch.no_accept_s * 1000
    return _stuck_verdict(store, adapter, transfer) if stuck else LIVE


def _stuck_verdict(store: Store, adapter: ClaudeAdapter, transfer: Any) -> str:
    """A registered, alive successor stuck past the window: ``dead:no-accept`` after its
    first prompt, else ``dead:no-first-turn`` (tp#2326 D3; checked on this branch only,
    never per live poll)."""
    seen = first_turn_seen(store, adapter, transfer)
    return DEAD_PREFIX + ("no-accept" if seen else NO_FIRST_TURN)


def _wait_accepted_or_dead(
    store: Store, adapter: ClaudeAdapter, transfer_id: str, deadline: float, watch: SuccessorWatch
) -> tuple[Any, str]:
    """Poll *transfer_id* until it leaves ``prepared``/``opened``, its successor is dead,
    or *deadline* (``_monotonic``) passes: ``(transfer, "")`` or ``(transfer, "dead:<why>")``.

    Every poll first refreshes the transfer's ``start_pid``/``start_heartbeat_at`` (D5:
    the daemon sweep leaves a transfer with a fresh heartbeat to this call)."""
    from .transfer_store import OPENED, PREPARED

    pid = os.getpid()
    while True:
        store.heartbeat_transfer(transfer_id, pid, _now_ms())
        current = store.get_transfer(transfer_id)
        if current is None or current.state not in (PREPARED, OPENED):
            return current, ""
        verdict = successor_state(store, adapter, current, _now_ms(), watch)
        if verdict.startswith(DEAD_PREFIX):
            return current, verdict
        if _monotonic() >= deadline:
            return current, ""
        _sleep(ACCEPT_POLL_SEC)


class SuccessorDied(StartRefused):
    """The successor died and the one automatic relaunch did not save it (exit 1)."""


def _dry_run(
    transfer: Any, src: Source, text: str, as_json: bool, sidecars: Sidecars = NO_SIDECARS
) -> int:
    from . import launch_argv
    from .models import llm_model_id

    named = {"model_full": "", "facts_rest": ""}
    if sidecars.model_full:
        named["model_full"] = str(sidecar_path(transfer, MODEL_FULL_SUFFIX))
        print(
            _model_full_note(sidecars.model_chars, named["model_full"]) + " (dry run: not written)",
            file=sys.stderr,
        )
    if sidecars.facts_rest:
        named["facts_rest"] = str(sidecar_path(transfer, FACTS_REST_SUFFIX))
    prompt = preamble(transfer, src.label, str(src.transcript or ""), **named) + text
    # The successor launches through `ccc start-job`, which feeds this prompt on claude's
    # STDIN, never in argv (tp#1681 L23/L24): the dry run shows the same split.
    argv = launch_argv.claude_argv(
        session_id="<new session id>",
        model=llm_model_id(transfer.llm_overseer),
        name="",
    )
    if as_json:
        doc = {"dry_run": True, "snapshot": transfer.snapshot(), "argv": argv, "stdin": prompt}
        print(json.dumps(doc))
    else:
        print(f"[dry-run] would hand {src.session_id} to a NEW session on {transfer.target_label}")
        print("argv: " + " ".join(argv) + "  (prompt on stdin)")
        print("--- prompt (stdin) ---")
        print(prompt)
    return 0


def _account_change_note(src: Source, transfer: Any) -> str:
    from . import accounts

    if accounts.same_config_dir(src.config_dir, transfer.target_config_dir):
        return f"note: the account does not change ({transfer.target_label})"
    return f"note: the account changes {src.label or '?'} → {transfer.target_label}"


#: How the D6 note names a snapshot field this call could not apply (the flag that set it);
#: the account (``target_config_dir`` / ``target_label``) is named on its own.
_IGNORED_FIELD_NAMES = {
    "prompt_sha256": "handoff text",
    "llm_overseer": "overseer -O",
    "llm_exec": "executor -E",
}
_ACCOUNT_FIELDS = ("target_config_dir", "target_label")


@dataclass
class _Chosen:
    """The transfer ``start`` drives; ``ignored`` non-empty = an ACTIVE transfer with a
    live or starting successor is driven as stored (its handoff file is never rewritten)
    and these fields of this call are ignored (tp#2172 D6)."""

    transfer: Any
    ignored: list[str] = field(default_factory=list)


def _ignored_fields(active: Any, candidate: Any) -> list[str]:
    """The D6 note's names of the fields where *candidate* differs from *active*."""
    names: list[str] = []
    for name in active.differs(candidate):
        if name in _ACCOUNT_FIELDS:
            account = (
                f"account -A {candidate.target_label} (the successor stays on account "
                f"{active.target_label})"
            )
            if account not in names:
                names.append(account)
            continue
        names.append(_IGNORED_FIELD_NAMES.get(name, name))
    return names


def _settle_active(
    store: Store, adapter: ClaudeAdapter, active: Any, candidate: Any
) -> _Chosen | None:
    """D6 on an active transfer that this call meets: a :class:`_Chosen` to drive, or
    ``None`` once a differing transfer with a dead successor is aborted (``superseded``)."""
    from .transfer_store import OPENED, PREPARED

    ignored = _ignored_fields(active, candidate)
    if not ignored:
        return _Chosen(active)  # the identical re-run: drive it as before
    verdict = LIVE
    if active.state in (PREPARED, OPENED):
        verdict = successor_state(store, adapter, active, _now_ms())
    tid = active.transfer_id
    if verdict.startswith(DEAD_PREFIX):  # dead or never started: no successor will accept it
        code, message = abort_transfer(store, tid, "superseded", _now_ms())
        print(
            f"note: transfer {tid}'s successor is {verdict}; superseded by this call: {message}"
            if code == 0
            else f"warning: {message}",  # accepted meanwhile: the re-read drives it
            file=sys.stderr,
        )
        return None
    print(
        f"note: transfer {tid}'s successor {active.target_session} is {verdict} "
        f"({active.state}); it keeps the handoff it was given; ignored from this call: "
        + ", ".join(ignored),
        file=sys.stderr,
    )
    return _Chosen(active, ignored)


def _reuse_or_create(store: Store, adapter: ClaudeAdapter, candidate: Any, now: int) -> _Chosen:
    """The transfer to drive (tp#2172 D6): an identical active one (a retry); a differing
    active one whose successor is live or starting, as stored; else a fresh one (a
    differing active transfer with a dead successor is aborted first). A concurrent
    create (``TransferConflict``) is re-read once and judged by the same rules."""
    from .transfer_store import TransferConflict, new_transfer_id

    for _attempt in range(2):
        active = store.active_transfer(candidate.source_session)
        if active is not None:
            chosen = _settle_active(store, adapter, active, candidate)
            if chosen is not None:
                return chosen
        candidate.transfer_id = new_transfer_id()
        candidate.target_session = str(uuid.uuid4())
        candidate.handoff_path = str(handoff_path(candidate.transfer_id))
        try:
            return _Chosen(store.create_transfer(candidate, now))
        except TransferConflict:
            continue  # another start created one meanwhile: judge that one
    raise StartRefused("another transfer of this session started concurrently; re-run start")


def _driven_text(chosen: _Chosen, text: str) -> str:
    """The handoff text to drive *chosen* with: the STORED one when this call's fields are
    ignored (D6; this call's *text* only when the stored file is unreadable)."""
    if not chosen.ignored:
        return text
    try:
        return Path(chosen.transfer.handoff_path).read_text(encoding="utf-8") or text
    except OSError:
        return text


def _open_target(  # pylint: disable=too-many-arguments
    store: Store,
    transfer: Any,
    src: Source,
    text: str,
    watch: SuccessorWatch,
    *,
    rewrite: bool = True,
    sidecars: Sidecars = NO_SIDECARS,
) -> int | None:
    """Write the handoff, create and (re)launch a still-draft successor: ``None`` = go on
    waiting, else the exit code of a refused or failed launch (already reported).

    ``rewrite=False`` (D6: a differing re-run meets a live or starting successor) leaves
    the stored handoff file untouched: the successor already read it. A rewrite also writes
    this call's *sidecars* next to it (tp#2172 D1/D2), before the successor's preamble names
    them."""
    from .transfer_store import FAILED, OPENED, PREPARED

    if transfer.state not in (PREPARED, OPENED):
        return None
    if rewrite:
        _write_handoff(Path(transfer.handoff_path), text)
        _write_sidecars(transfer, sidecars)
    try:  # re-resolved from its name before every (re)launch, never from a snapshot
        _checked_profile(transfer.launch_profile)
    except StartRefused as exc:
        if transfer.state == PREPARED:
            store.transition_transfer(
                transfer.transfer_id, FAILED, _now_ms(), failure=f"launch: {exc}"[:200]
            )
            if store.get(transfer.target_session) is not None:
                store.update_fields(transfer.target_session, archived=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _create_successor(store, transfer, src, text)
    target_row = store.get(transfer.target_session)
    if target_row is None or not target_row.draft:
        return None
    launcher = _launch(transfer.target_session)  # never started: (re)launch it
    if not launcher:
        if transfer.state == PREPARED:
            store.transition_transfer(
                transfer.transfer_id, FAILED, _now_ms(), failure="launch: no terminal tab"
            )
            store.update_fields(transfer.target_session, archived=True)
        print(
            "error: could not open a terminal tab for the successor — the source "
            "session is unchanged",
            file=sys.stderr,
        )
        return 1
    watch.launched_ms = _now_ms()
    store.note_transfer(transfer.transfer_id, watch.launched_ms, launcher=launcher)
    store.transition_transfer(transfer.transfer_id, OPENED, watch.launched_ms)
    print(
        f"opened: successor {transfer.target_session} on {transfer.target_label} "
        f"({launcher}); waiting for it to accept transfer {transfer.transfer_id}"
    )
    return None


def _abort_dead(store: Store, transfer: Any, verdict: str) -> Any | None:
    """Abort *transfer* for its dead successor (D3's core); ``None`` once it is failed, or
    the re-read transfer when it was accepted meanwhile (then it is finalized, not
    relaunched). A ``no-first-turn`` abort that changed the row also stamps the orphan
    close (tp#2326 D6): the daemon closes that successor's tab once it is idle."""
    from .transfer_store import ACCEPTED, FINALIZED

    why = verdict.removeprefix(DEAD_PREFIX)
    now = _now_ms()
    code, message = abort_transfer(store, transfer.transfer_id, f"successor dead: {why}", now)
    print(f"note: {message}" if code == 0 else f"warning: {message}", file=sys.stderr)
    if code == 0 and why == NO_FIRST_TURN and message.startswith(ABORT_PREFIX):
        store.record_orphaned(transfer.transfer_id, now)
    current = store.get_transfer(transfer.transfer_id)
    if current is not None and current.state in (ACCEPTED, FINALIZED):
        return current
    return None


def _relaunch_candidate(dead: Any) -> Any:
    """The relaunch's transfer: the dead one's snapshot (same text, digest, accounts,
    models, facts snapshot), every per-transfer field reset (``_reuse_or_create`` gives
    it a new id, target and handoff path)."""
    from .transfer_store import PREPARED

    return dataclasses.replace(
        dead,
        transfer_id="",
        target_session="",
        handoff_path="",
        launcher="",
        state=PREPARED,
        failure="",
        created_at=0,
        opened_at=0,
        accepted_at=0,
        finalized_at=0,
        updated_at=0,
        target_launched_at=0,
        start_pid=0,
        start_heartbeat_at=0,
    )


def _no_first_turn_message(target: str) -> str:
    """The ``start`` error of a ``dead:no-first-turn`` successor (tp#2326 D4)."""
    return (
        f"successor {target} never received its first prompt ({NO_FIRST_TURN}: a trust "
        "dialog, a stalled start hook, or a launch route that did not submit the handoff; "
        "its tab shows which); nothing moved and THIS session stays authoritative — keep "
        "working here. ccc closes that tab once it is idle; fix the cause and run start again"
    )


def _drive(  # pylint: disable=too-many-locals,too-many-arguments
    store: Store,
    transfer: Any,
    src: Source,
    text: str,
    args: Any,
    *,
    rewrite: bool = True,
    sidecars: Sidecars = NO_SIDECARS,
) -> int:
    """Launch (or re-launch) the successor, wait for ``accepted``, finalize.

    A dead successor (``successor_state``) aborts its transfer and gets ONE automatic
    relaunch (tp#2172 D5): a fresh transfer with the same snapshot and text, launched and
    waited for inside the SAME ``-w`` budget; a second death raises :class:`SuccessorDied`.
    A ``no-first-turn`` death never relaunches (tp#2326 D4): it raises at once.
    ``rewrite=False`` keeps *transfer*'s stored handoff file as it is (D6); a relaunch
    writes its own new file. *sidecars* (tp#2172 D1/D2) are written next to every handoff
    file this call writes: they exist only once the transfer id does.
    """
    from .transfer_store import ACCEPTED, FAILED, FINALIZED

    watch = SuccessorWatch.from_env()
    deadline = _monotonic() + max(0.0, float(args.wait))
    adapter = _adapter()
    code = _open_target(store, transfer, src, text, watch, rewrite=rewrite, sidecars=sidecars)
    if code is not None:
        return code
    current, dead = _wait_accepted_or_dead(store, adapter, transfer.transfer_id, deadline, watch)
    deaths: list[str] = []
    while dead:
        why = dead.removeprefix(DEAD_PREFIX)
        healed = _abort_dead(store, current, dead)
        if healed is not None:  # accepted between the verdict and the abort: finalize it
            current = healed
            break
        deaths.append(why)
        first = current.target_session
        if len(deaths) > AUTO_RELAUNCHES:
            raise SuccessorDied(f"successor died twice: {'; '.join(deaths)}; nothing moved")
        if why == NO_FIRST_TURN:  # tp#2326 D4: deterministic so far, a relaunch would not help
            raise SuccessorDied(_no_first_turn_message(first))
        try:
            chosen = _reuse_or_create(store, adapter, _relaunch_candidate(current), _now_ms())
        except StartRefused as exc:
            raise SuccessorDied(f"successor {first} died ({why}); no relaunch: {exc}") from exc
        transfer, text = chosen.transfer, _driven_text(chosen, text)
        print(f"relaunched: successor {transfer.target_session} (first {first} died: {why})")
        watch = watch.fresh()
        code = _open_target(
            store, transfer, src, text, watch, rewrite=not chosen.ignored, sidecars=sidecars
        )
        if code is not None:
            return code
        current, dead = _wait_accepted_or_dead(
            store, adapter, transfer.transfer_id, deadline, watch
        )
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
    failed_required: list[tuple[str, str]] = field(default_factory=list)  # (name, error)
    sidecars: Sidecars = NO_SIDECARS  # tp#2172 D1/D2: written by _drive once the id exists


def _failed_required(snap_doc: dict[str, Any] | None) -> list[tuple[str, str]]:
    """``(provider, error)`` of every REQUIRED facts provider that failed (tp#1520).

    Read from the snapshot's warning facts (``fields.required``), which ARE frozen, so the
    fresh and the reuse paths of ``start`` both see them (``providers[]`` is not frozen).
    """
    out: list[tuple[str, str]] = []
    for fact in (snap_doc or {}).get("facts", []):
        fields = fact.get("fields") if isinstance(fact, dict) else None
        if fact.get("kind") == "warning" and isinstance(fields, dict) and fields.get("required"):
            out.append((str(fields.get("provider") or ""), str(fields.get("error") or "")))
    return out


def _reused(
    text: str, digest: str, sid: str, snapshot: str, sidecars: Sidecars = NO_SIDECARS
) -> _Composed:
    """A reused composed text, with the failed required providers of its stored snapshot."""
    failed = _failed_required(_stored_snapshot(sid, snapshot))
    return _Composed(text, digest, snapshot, failed, sidecars)


def _say_failed_required(args: argparse.Namespace, sid: str, composed: _Composed) -> None:
    """D4 stays (never block), but loud: stderr; a detached ``--capped`` run logs + notifies."""
    for name, error in composed.failed_required:
        line = (
            f"⚠ required facts provider {name} failed ({error}): the handoff marks its "
            "section UNKNOWN"
        )
        print(line, file=sys.stderr)
        if getattr(args, "capped", False):
            from . import handoff_capped

            handoff_capped.log_event(sid, line)
            handoff_capped.notify_failure(line)


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
    model: Any,
    snap_doc: dict[str, Any],
    snapshot: str,
    sid: str,
    args: Any,
    generated: bool = False,
    uncut: tuple[Any, Sidecars] | None = None,
) -> _Composed:
    """Compose the model part with the snapshot (D1, D9) and keep a cut original.

    *generated* (facts-only / capped) lets compose cut the model bodies to fit (tp#1835).
    *uncut* (tp#2172 D1): ``(digest model, sidecars)`` of a ``start -f`` part cut to
    ``MODEL_MAX_CHARS``: the digest comes from the UNCUT part, so an identical re-run still
    matches. Fact records moved out for size (D2) join the sidecars, scrubbed.
    """
    from . import handoff_doc, handoff_facts

    model_path = handoff_facts.facts_dir(sid) / f"{snapshot[:12]}.model.md"
    doc, composed = handoff_doc.compose_checked(
        model, snap_doc, ref=sid, model_path=str(model_path), generated=generated
    )
    if not doc.ok:
        raise StartRefused("handoff rejected — " + "; ".join(doc.errors))
    if composed.model_cut and not args.dry_run:  # D9: the cut part stays retrievable
        try:
            handoff_facts.write_private(model_path, handoff_facts.Scrubber().text(model.raw))
        except handoff_facts.FactsError as exc:
            raise StartRefused(f"facts collection refused — {exc}") from exc
    digest_model, sidecars = uncut if uncut is not None else (model, NO_SIDECARS)
    if composed.rest:
        rest = _scrubbed("the facts-rest sidecar", composed.rest)
        sidecars = dataclasses.replace(sidecars, facts_rest=rest)
    return _Composed(
        doc.text,
        handoff_doc.model_digest(digest_model, snapshot),
        snapshot,
        _failed_required(snap_doc),
        sidecars,
    )


def _scrubbed(what: str, text: str) -> str:
    """*text* through the handoff's fail-closed scrub (tp#2172 D1/D2); a refusal refuses
    ``start`` before anything is written."""
    from . import handoff_doc

    scrubbed, refusal = handoff_doc.scrub_text(text)
    if refusal:
        raise StartRefused(f"handoff rejected — {what}: {refusal}")
    return scrubbed


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
            return _reused(stored, active.prompt_sha256, src.session_id, active.facts_snapshot)
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
    return _finish(model, snap_doc, snapshot, src.session_id, args, generated=True)


def _compose(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: ClaudeAdapter, store: Store, src: Source, raw: bytes, active: Any, args: Any
) -> _Composed:
    """D2: facts frozen into a snapshot + the model file, composed once per transfer.

    An active PREPARED / OPENED transfer that already has a snapshot is REUSED (stored
    snapshot and stored composed text, no recollection): its digest is recomputed from the
    given model file and the stored snapshot id, so a changed ledger cannot fork a retry.
    ``--facts-only`` / ``--capped`` generate the model part (:func:`_compose_generated`);
    a transfer from before snapshots keeps the whole-document path. A model part over
    ``MODEL_MAX_CHARS`` is cut, never refused (tp#2172 D1): the digest is taken from the
    UNCUT part and the scrubbed full text rides along for :func:`_drive` to write.
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
    kept, full = handoff_doc.fit_model_part(raw)
    model = handoff_doc.parse_model_file(kept if full else raw)
    digest_model, sidecars = _uncut(raw, full) if full and model.ok else (model, NO_SIDECARS)
    sid = src.session_id
    retry = active is not None and active.state in (PREPARED, OPENED)
    if retry and model.ok:  # the identical re-run of a stored transfer is never re-composed
        digest = handoff_doc.model_digest(digest_model, active.facts_snapshot)
        try:
            stored = Path(active.handoff_path).read_text(encoding="utf-8")
        except OSError:
            stored = ""
        if stored and digest == active.prompt_sha256:
            return _reused(stored, digest, sid, active.facts_snapshot, sidecars)
    if not model.ok:
        raise StartRefused("handoff rejected — " + "; ".join(model.errors))
    if retry:
        snap_doc = _stored_snapshot(sid, active.facts_snapshot)
        if snap_doc is None:
            raise StartRefused(f"the stored facts snapshot {active.facts_snapshot[:12]} is gone")
        snapshot = active.facts_snapshot
    else:
        snap_doc, snapshot, _scrubber = _snapshot(adapter, store, src, args)
    return _finish(model, snap_doc, snapshot, sid, args, uncut=(digest_model, sidecars))


def _uncut(raw: bytes, full: str) -> tuple[Any, Sidecars]:
    """``(digest model, sidecars)`` of a ``start -f`` part cut to ``MODEL_MAX_CHARS``
    (tp#2172 D1): the UNCUT part's digest model (its parse, or the sha256 of its *full*
    text when only the whole file fails a check the cut one passes, e.g. the 10 KB cap)
    and the scrubbed full text."""
    from . import handoff_doc

    whole = handoff_doc.parse_model_file(raw)
    if not whole.ok:
        whole = handoff_doc.ModelPart(sha256=hashlib.sha256(full.encode("utf-8")).hexdigest())
    text = _scrubbed("the full model part", full)
    return whole, Sidecars(model_full=text, model_chars=handoff_doc.model_chars(full))


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
    # ``shell`` passes here only provisionally: it is idle only with no open turn
    # (tp#1662), which the transcript check below enforces.
    if live is None or contract_status(live.raw_status, False) != "idle":
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


def _clamp_wait(args: argparse.Namespace) -> None:
    """Lower ``args.wait`` to what the 600 s Bash cap leaves; say the effective value."""
    wait = effective_wait(int(args.wait), int(args.drain or 0), _provider_timeouts())
    args.wait = wait
    if not args.dry_run:
        print(f"accept wait: {wait}s (effective -w)", file=sys.stderr)


def _start_drained(adapter: ClaudeAdapter, store: Store, src: Source, args: Any) -> bool:
    """``start -D N``: one bounded in-flight wait; prints the inventory when still busy."""
    ok, inv = _drain_source(adapter, store, src, float(args.drain))
    if not ok:
        print(_render_inflight(inv.to_dict()))
    return ok


def _start_input(args: argparse.Namespace) -> tuple[bytes, str]:
    """``(model file bytes, refusal)``: the handoff file, or why the flags give none."""
    if args.facts_only and (args.file or args.capped):
        return b"", "--facts-only takes no -f/--file and no --capped"
    if args.facts_only or args.capped:
        return b"", ""
    if not args.file:
        return b"", "give -f/--file HANDOFF (or --capped)"
    try:
        return Path(args.file).read_bytes(), ""
    except OSError as exc:
        return b"", f"cannot read the handoff file: {exc}"


def cmd_start(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements,too-many-branches
    """``continue-session start``: hand this session's work to a NEW session."""
    from .models import now_ms
    from .transfer_store import ACCEPTED

    outside = bool(getattr(args, "outside", False))
    if outside and not args.session:
        print(
            "error: -X/--outside requires -s/--session <id> (the source is never taken "
            "from the environment)",
            file=sys.stderr,
        )
        return 2
    adapter, store, src = _open_source(args)
    with store:
        if src is None:
            print("error: could not resolve a session (pass -s/--session <id>)", file=sys.stderr)
            return 1
        if outside and src.pid <= 0:  # refuse at once: a closed source never turns idle
            return _fail(
                args, src.session_id, f"{src.session_id} is not a live session in Claude's registry"
            )
        if (args.await_idle or outside) and not _await_idle(adapter, src):
            print(
                f"error: session {src.session_id} is not provably idle — no transfer started",
                file=sys.stderr,
            )
            return 1
        _clamp_wait(args)
        active = store.active_transfer(src.session_id)
        if active is not None and active.state == ACCEPTED:  # a re-run after acceptance
            return _drive(store, active, src, "", args)
        session = store.get(src.session_id)
        if session is not None and session.handed_off_to:
            return _fail(args, src.session_id, f"{src.session_id} was already handed off")
        raw, problem = _start_input(args)
        if problem:
            return _fail(args, src.session_id, problem)
        if args.drain is not None and not _start_drained(adapter, store, src, args):
            return _fail(
                args,
                src.session_id,
                f"work is still in flight after {args.drain}s — no transfer started; "
                "the source session stays authoritative",
            )
        try:
            composed = _compose(adapter, store, src, raw, active, args)
        except StartRefused as exc:
            return _fail(args, src.session_id, str(exc))
        _say_failed_required(args, src.session_id, composed)
        try:
            target_dir = _preflight(
                adapter, store, src, args.account, dry_run=args.dry_run, outside=outside
            )
            candidate = _candidate(
                store, src, target_dir, args.account, args, composed.digest, composed.snapshot
            )
            print(_account_change_note(src, candidate), file=sys.stderr)
            if args.dry_run:
                candidate.transfer_id = "<transfer>"
                candidate.handoff_path = str(handoff_path("<transfer>"))
                return _dry_run(candidate, src, composed.text, args.json, composed.sidecars)
            chosen = _reuse_or_create(store, adapter, candidate, now_ms())
        except StartRefused as exc:
            return _fail(args, src.session_id, str(exc))
        transfer = chosen.transfer
        try:
            code = _drive(
                store,
                transfer,
                src,
                _driven_text(chosen, composed.text),
                args,
                rewrite=not chosen.ignored,
                sidecars=composed.sidecars,
            )
        except SuccessorDied as exc:  # both successors died: two failed rows, nothing moved
            return _fail(args, src.session_id, str(exc))
        if code not in (0, EXIT_WAITING):
            return _fail(args, src.session_id, f"transfer {transfer.transfer_id} did not open")
        return code


# ------------------------------------------------------------------ accept / status


HOOK_TIMEOUT_SEC = 180  # one hook run's default budget (`accept_hook_timeout_sec`, tp#1805)
ACCEPT_HOOK_RETRY_INTERVAL_SEC = 10  # the first wait between two accept attempts (tp#1521)
ACCEPT_HOOK_MAX_ATTEMPTS = 4  # each wait doubles: 10, 20, 40 s (tp#1805)
ACCEPT_HOOK_ATTEMPT_BUDGET_SEC = 20  # the least a retry must have left to be worth starting
PRIO_DARWIN_PROCESS = 4  # <sys/resource.h>; ``os`` exports no name for it
TASKPOLICY = "/usr/sbin/taskpolicy"
HOOK_OUTPUT_CAP = 64 * 1024
_PLACEHOLDERS = ("{old}", "{new}", "{label}", "{transfer}")


class HookFailed(Exception):
    """One ``handoff_hooks`` entry did not succeed; the message says why (no output dump).

    ``retryable`` is true for a non-zero exit (a refusal that may be transient, e.g. the
    source between two ``start`` runs) and for a timeout (a loaded machine, tp#1805; hooks
    are idempotent); a missing executable or a malformed answer is final.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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


def run_hook(template: object, transfer: Any, timeout: float | None = None) -> dict[str, Any]:
    """Run one hook argv-only (no shell), bounded by *timeout* (default ``HOOK_TIMEOUT_SEC``);
    its parsed JSON object, or HookFailed."""
    import subprocess

    limit = HOOK_TIMEOUT_SEC if timeout is None else timeout
    argv = _render_hook(template, transfer)
    name = os.path.basename(argv[0])
    try:
        returncode, stdout, stderr = run_bounded(argv, limit)
    except subprocess.TimeoutExpired as exc:
        raise HookFailed(f"{name}: timed out after {limit:g}s", retryable=True) from exc
    except OSError as exc:
        raise HookFailed(f"{name}: could not run ({exc.strerror or exc})") from exc
    if len(stdout) > HOOK_OUTPUT_CAP:
        raise HookFailed(f"{name}: stdout exceeds {HOOK_OUTPUT_CAP} bytes")
    detail = stderr.decode("utf-8", "replace").strip().splitlines()
    if returncode != 0:
        tail = f": {detail[-1][:300]}" if detail else ""
        raise HookFailed(f"{name}: exit {returncode}{tail}", retryable=True)
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


def _accept_prepare(store: Store, transfer: Any) -> int | None:
    """The accept-prepare transaction, run BEFORE any ``handoff_hooks`` entry: sweep the
    source's late await groups into the reservation, then refuse on an await collision
    (the successor already holds a group; never merged) or, retryably, while a reserved
    group is mid-delivery. ``None`` = go on; otherwise the exit code (nothing changed)."""
    import sqlite3

    from .await_delivery import STALE_DELIVERY_SEC
    from .await_store import AwaitBusy, AwaitConflict
    from .models import now_ms
    from .transfer_store import ACCEPTED, FINALIZED

    tid = transfer.transfer_id
    try:
        swept = store.prepare_accept(tid, now_ms(), stale_sec=STALE_DELIVERY_SEC)
    except AwaitConflict as exc:
        held = store.active_await(transfer.target_session)
        disarm = f"`ccc await -d {held.id}`" if held is not None else "`ccc await -d GROUP`"
        why = f"{exc}. Disarm this session's group ({disarm}) and run accept again"
    except AwaitBusy as exc:
        why = (
            f"{exc}; wait until that delivery finishes (or is reclaimed after "
            f"{STALE_DELIVERY_SEC // 60} min) and run accept again"
        )
    except sqlite3.Error as exc:
        why = f"could not prepare the await move ({exc}); run accept again"
    else:
        if swept is not None:
            return None
        current = store.get_transfer(tid)
        if current is not None and current.state in (ACCEPTED, FINALIZED):
            print(f"already accepted: transfer {tid} ({current.state})")
            return 0
        print(f"error: transfer {tid} changed state", file=sys.stderr)
        return 1
    store.note_transfer(tid, now_ms(), failure=f"await: {why}")
    print(f"error: {why}; nothing changed (no hook ran).", file=sys.stderr)
    return 1


def _accept_and_rebind(store: Store, transfer: Any) -> list[str] | None:
    """The ``accepted`` CAS + the await rebind (ONE transaction): the moved groups as next
    steps, or ``None`` after printing why (nothing changed; accept stays retryable)."""
    import sqlite3

    from .await_store import AwaitConflict
    from .models import now_ms
    from .transfer_store import ACCEPTED, FINALIZED

    try:
        moved = store.accept_transfer(transfer.transfer_id, now_ms())
    except (AwaitConflict, sqlite3.Error) as exc:
        store.note_transfer(transfer.transfer_id, now_ms(), failure=f"await rebind: {exc}")
        print(
            f"error: could not move the await group to this session — {exc}. The transfer "
            f"stays {transfer.state} and ownership is unchanged; fix the cause and run accept "
            "again.",
            file=sys.stderr,
        )
        return None
    if moved is None:  # lost the CAS: fine only when a racing accept already won
        current = store.get_transfer(transfer.transfer_id)
        if current is None or current.state not in (ACCEPTED, FINALIZED):
            print(f"error: transfer {transfer.transfer_id} changed state", file=sys.stderr)
            return None
        return []
    return [
        f"await group {group.id} ({group.state}) now wakes this session (`ccc await -l` lists it)"
        for group in moved
    ]


def leave_darwin_background() -> None:
    """Move this process out of darwin background before the hooks run (tp#1805).

    A Claude session may run its Bash tool in darwin background (E-cores, throttled I/O),
    and accept and its hooks inherit it: under load ``tp handoff`` then took ~6x its CPU
    time. accept blocks the successor's interactive session, so it leaves background for
    ITSELF (its hooks inherit normal QoS); nothing else changes and ``nice`` stays.
    Two calls, as neither clears both kinds: ``setpriority`` clears a background the
    process set on itself, ``taskpolicy -B -p`` one another process set. No-op off macOS;
    never raises.
    """
    import contextlib
    import subprocess

    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError, AttributeError):
        os.setpriority(PRIO_DARWIN_PROCESS, 0, 0)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - a fixed system binary, no shell
            [TASKPOLICY, "-B", "-p", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )


def _run_hooks(
    hooks: list[Any], transfer: Any, timeout: float, deadline: float
) -> list[str]:  # raises HookFailed
    """Every hook from the first, each run bounded by *timeout* and what remains."""
    steps: list[str] = []
    for template in hooks:
        limit = max(1.0, min(timeout, deadline - _monotonic()))
        steps += run_hook(template, transfer, limit).get("next_steps", [])
    return steps


def _run_hooks_with_retry(  # pylint: disable=too-many-return-statements,too-many-locals
    store: Store, transfer: Any, hooks: list[Any], cfg: Any
) -> tuple[Any, list[str]] | int:
    """Accept-prepare + the hook list, retried on a non-zero exit or a timeout within ONE
    monotonic window (``accept_hook_retry_sec``, tp#1521, tp#1805): ``(transfer, next
    steps)`` or an exit code.

    Each hook run is bounded by ``accept_hook_timeout_sec``; the first attempt also by
    ``max(window, timeout)``, a retry by the window. A retry starts only while attempts
    remain (``ACCEPT_HOOK_MAX_ATTEMPTS``) and it still fits (elapsed + the wait + one
    attempt's minimum budget ≤ the window); the wait doubles from
    ``ACCEPT_HOOK_RETRY_INTERVAL_SEC``. Each retry re-reads the transfer (``failed``
    stops, ``accepted`` is done), re-runs accept-prepare (an await group mid-delivery stops
    it, nothing rebound) and then the WHOLE hook list from the first entry.
    """
    from .models import now_ms
    from .transfer_store import ACCEPTED, FINALIZED, OPENED, PREPARED

    tid = transfer.transfer_id
    window = float(getattr(cfg, "accept_hook_retry_sec", 0) or 0)
    timeout = float(getattr(cfg, "accept_hook_timeout_sec", 0) or HOOK_TIMEOUT_SEC)
    if hooks:
        leave_darwin_background()
    started = _monotonic()
    attempt = 0
    while True:
        attempt += 1
        if attempt > 1:
            current = store.get_transfer(tid)
            if current is not None and current.state in (ACCEPTED, FINALIZED):
                print(f"already accepted: transfer {tid} ({current.state})")
                return 0
            if current is None or current.state not in (PREPARED, OPENED):
                now_state = current.state if current is not None else "gone"
                why = (current.failure if current is not None else "") or "no longer acceptable"
                print(f"error: transfer {tid} is {now_state}: {why}", file=sys.stderr)
                return 1
            transfer = current
        refused = _accept_prepare(store, transfer)  # BEFORE any hook (re-checked at the CAS)
        if refused is not None:
            return refused
        deadline = started + (max(window, timeout) if attempt == 1 else window)
        try:
            steps = _run_hooks(hooks, transfer, timeout, deadline)
        except HookFailed as exc:
            elapsed = _monotonic() - started
            wait = ACCEPT_HOOK_RETRY_INTERVAL_SEC * 2 ** (attempt - 1)
            fits = (
                attempt < ACCEPT_HOOK_MAX_ATTEMPTS
                and elapsed + wait + ACCEPT_HOOK_ATTEMPT_BUDGET_SEC <= window
            )
            if exc.retryable and fits:
                store.note_transfer(tid, now_ms(), failure=f"hook (retrying {attempt}): {exc}")
                print(
                    f"waiting: handoff hook failed ({exc}); retrying in {wait}s "
                    f"(attempt {attempt} of at most {ACCEPT_HOOK_MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                _sleep(wait)
                continue
            store.note_transfer(tid, now_ms(), failure=f"hook: {exc}")
            plural = "attempt" if attempt == 1 else "attempts"
            print(
                f"error: handoff hook failed after {attempt} {plural} in {elapsed:.0f}s — "
                f"{exc}. The transfer is still {transfer.state} and ownership is unchanged; "
                "accept already retried what fit its window (accept_hook_retry_sec / "
                "accept_hook_timeout_sec), so fix the cause and run accept again.",
                file=sys.stderr,
            )
            return 1
        return transfer, steps


def _accept_again(store: Store, transfer: Any, as_json: bool) -> int:
    """A duplicate ``accept`` (``accepted``/``finalized``): no hook runs again; it heals a
    crash after the accepted CAS (finalize + closer, both idempotent and token-scoped) and
    re-runs the idempotent steps a crash before it skipped: the lineage anchor
    (``carry_first_aim``) and the thread id (tp#1989). Never ``_reconcile_successor_aim``:
    it would overwrite an AIM the successor changed since (tp#1991)."""
    before = transfer.state
    store.carry_first_aim(transfer.target_session, transfer.source_session)
    store.record_thread(transfer.target_session, transfer.source_session)
    source = _finalize_after_accept(store, transfer)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "transfer": transfer.transfer_id,
                    "already": before,
                    "next_steps": [],
                    "source": source,
                }
            )
        )
    else:
        print(f"already accepted: transfer {transfer.transfer_id} ({before})")
    _say_source(source, transfer.transfer_id, as_json)
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    """``continue-session accept``: the successor takes the work over (hooks, then CAS
    plus the await rebind in one transaction)."""
    from . import config
    from .store import Store

    with Store() as store:
        return accept_as(
            store, args.transfer, _env_session(), bool(args.json), config.load_config()
        )


def accept_as(  # pylint: disable=too-many-return-statements
    store: Store, transfer_id: str, caller: str, as_json: bool, cfg: Any
) -> int:
    """The ``accept`` core with the caller's session id passed in: the CLI passes its
    session env, the SessionStart hook (which may lack it) its payload id (tp#1681 L18)."""
    import sqlite3

    from . import accounts
    from .models import now_ms
    from .transfer_store import ACCEPTED, FAILED, FINALIZED, OPENED, PREPARED

    transfer = store.get_transfer(transfer_id)
    if transfer is None:
        print(f"error: no such transfer {transfer_id}", file=sys.stderr)
        return 1
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
        return _accept_again(store, transfer, as_json)
    if transfer.state not in (PREPARED, OPENED):
        if transfer.state == FAILED:
            # tp#2172 D13: the daemon sweep closes this orphan successor's tab.
            try:
                store.record_accept_refusal(transfer.transfer_id, caller, now_ms())
            except sqlite3.Error as exc:
                print(f"warning: the refusal was not recorded ({exc})", file=sys.stderr)
        print(
            f"error: transfer {transfer.transfer_id} is {transfer.state}: "
            f"{transfer.failure or 'no longer acceptable'}",
            file=sys.stderr,
        )
        return 1
    hooks = _run_hooks_with_retry(store, transfer, cfg.handoff_hooks, cfg)
    if isinstance(hooks, int):
        return hooks
    transfer, steps = hooks
    moved = _accept_and_rebind(store, transfer)
    if moved is None:
        return 1
    steps += moved
    # The lineage's first AIM becomes this session's `/aim (1)` anchor (tp#1553).
    store.carry_first_aim(transfer.target_session, transfer.source_session)
    # …it joins the source's thread: one id across every relay (tp#1989)…
    store.record_thread(transfer.target_session, transfer.source_session)
    # …and its current AIM is the latest one the user set in the source (tp#1566).
    _reconcile_successor_aim(store, transfer)
    _mark_retired(transfer)
    store.note_transfer(transfer.transfer_id, now_ms(), failure="")
    # accept is the primary finalizer (tp#1582): an idle source never runs it itself.
    source = _finalize_after_accept(store, transfer)
    _print_accepted(transfer, steps, source, as_json)
    return 0


AUTO_ACCEPT_HEAD = "handoff {transfer} accepted"  # what the preamble tells the model to look for
AUTO_ACCEPT_NOTE = AUTO_ACCEPT_HEAD + " (auto)"
AUTO_ACCEPT_BUDGET_SEC = 45  # under Claude Code's 60 s default UserPromptSubmit hook timeout
AUTO_NOTE_MAX_BYTES = 200


def _fit(head: str, middle: str, tail: str) -> str:
    """``head + middle + tail`` within :data:`AUTO_NOTE_MAX_BYTES`, cutting only *middle*."""
    room = AUTO_NOTE_MAX_BYTES - len((head + tail).encode())
    if len(middle.encode()) > room:
        middle = middle.encode()[: max(0, room - 3)].decode("utf-8", "ignore") + "…"
    return head + middle + tail


def auto_accept(store: Store, session_id: str, inbound: list[Any], corrupt: int, cfg: Any) -> str:
    """The first-prompt auto-accept (tp#1681 L18, tp#2326 D1: run by the successor's
    ``UserPromptSubmit`` hook on its first submitted handoff prompt): ONE attempt of
    :func:`accept_as` on the single inbound transfer, its output captured; the ≤ 200-byte
    note for the context.

    Several inbound transfers or an unreadable row accept none and name the status
    command. A refusal or a failing hook leaves the transfer as it was and names the
    manual ``accept``. Hooks get ``min(accept_hook_timeout_sec, 45 s)`` in all, no retry.
    """
    import contextlib
    import io

    if corrupt or len(inbound) != 1:
        unreadable = f", {corrupt} unreadable" if corrupt else ""
        return _fit(
            f"handoff: {len(inbound) + corrupt} inbound transfers{unreadable}, ",
            "none accepted (auto)",
            f"; run: ccc continue-session status -s {session_id}",
        )
    tid = inbound[0].transfer_id
    budget = min(int(cfg.accept_hook_timeout_sec or HOOK_TIMEOUT_SEC), AUTO_ACCEPT_BUDGET_SEC)
    bounded = dataclasses.replace(cfg, accept_hook_timeout_sec=budget, accept_hook_retry_sec=0)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = accept_as(store, tid, session_id, True, bounded)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            code = 1
            print(f"error: {exc}", file=sys.stderr)
    if code == 0:
        try:
            steps = [str(s) for s in json.loads(out.getvalue()).get("next_steps", [])]
        except (ValueError, AttributeError):
            steps = []  # a racing accept already won: its text line, no steps
        middle = "; next: " + "; ".join(steps) if steps else ""
        return _fit(AUTO_ACCEPT_NOTE.format(transfer=tid), middle, "")
    errors = [ln for ln in err.getvalue().splitlines() if ln.startswith("error:")]
    reason = errors[-1].removeprefix("error:").strip() if errors else "accept failed"
    return _fit(
        f"handoff {tid} NOT accepted (auto): ",
        reason,
        f"; run: ccc continue-session accept {tid}",
    )


def _print_accepted(transfer: Any, steps: list[str], source: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "transfer": transfer.transfer_id,
                    "next_steps": steps,
                    "source": source,
                }
            )
        )
    else:
        print(
            f"accepted: transfer {transfer.transfer_id} — you now own the work of "
            f"{transfer.source_session}. Relaunch the subagents and restart the shells the "
            "handoff lists, then follow its First steps."
        )
    _say_source(source, transfer.transfer_id, as_json)
    if not as_json:
        for step in steps:
            print(f"next: {step}")


def recovery_step(transfer: Any, target_is_draft: bool, first_turn: bool | None = None) -> str:
    """The exact next step for *transfer* in its current state.

    *first_turn* (tp#2326 D7) is :func:`first_turn_seen` for an ``opened`` transfer with
    a started successor (``None`` otherwise): ``False`` names the unsent first prompt."""
    tid = transfer.transfer_id
    abort = f"; to give it up: `ccc continue-session abort {tid}` (nothing has moved yet)"
    if transfer.state == "opened" and not target_is_draft and first_turn is False:
        return (
            "the successor's first prompt is not submitted yet: if its tab shows the handoff "
            "text unsent, press Enter there; if the tab does not react, "
            f"`ccc continue-session abort {tid}` (nothing has moved) and fix the launch route"
        )
    if transfer.state == "failed" and (transfer.failure or "").endswith(NO_FIRST_TURN):
        return (
            "nothing moved; the source stays authoritative; its successor tab is closed by "
            f"the daemon when idle; fix the cause ({NO_FIRST_TURN_CAUSES}) and start a new "
            "transfer"
        )
    return {
        "prepared": "re-run the same `ccc continue-session start` in the source session" + abort,
        "opened": (
            "re-run the same `ccc continue-session start` in the source session (the "
            "successor never started; it relaunches it)" + abort
            if target_is_draft
            else f"in the successor run `ccc continue-session accept {tid}`; if its tab is "
            f"gone: `ccc resume {transfer.target_session}` and accept there" + abort
        ),
        "accepted": (
            "nothing in the source — accept finalizes it and closes its tab in the background "
            "once it is idle; never resume or prompt the source. If it stays accepted: in the "
            f"successor run `ccc continue-session accept {tid}` again (it finalizes, never "
            "re-running the hooks)"
        ),
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
        adapter = None
        for transfer in transfers:
            target = store.get(transfer.target_session)
            draft = bool(target is not None and target.draft)
            first_turn: bool | None = None
            if transfer.state == "opened" and target is not None and not draft:
                adapter = adapter or _adapter()
                first_turn = first_turn_seen(store, adapter, transfer)
            step = recovery_step(transfer, draft, first_turn)
            rows.append({**transfer.to_dict(), "first_turn": first_turn, "recovery": step})
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


ABORT_DEFAULT_REASON = "by hand"
ABORT_PREFIX = "aborted: "  # the ``failure`` text of an aborted transfer starts with this


@dataclass
class _Abort:  # pylint: disable=too-many-instance-attributes  # one report row
    """What one ``abort`` did (or why it did nothing)."""

    code: int
    message: str
    transfer: str
    state: str = ""  # the state after the call ("" = no such transfer)
    was: str = ""  # the state before it
    changed: bool = False
    released: list[int] = field(default_factory=list)  # await groups handed back to the source
    archived: bool = False  # the draft successor row was archived
    next_step: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.code == 0,
            "transfer": self.transfer,
            "state": self.state,
            "was": self.was,
            "changed": self.changed,
            "released_awaits": self.released,
            "archived_successor": self.archived,
            "message": self.message,
            "next": self.next_step,
        }


def _abort(store: Store, transfer_id: str, reason: str, now: int) -> _Abort:
    """The ``abort`` core: CAS ``prepared|opened → failed`` with ``aborted: <reason>``.

    ``accepted`` is refused (ownership moved; there is no ``accepted → failed`` edge and
    the successor already holds the work): its accept finalizes it. ``finalized``/``failed``
    are reported unchanged (idempotent). A lost CAS (the state moved under us) is re-read
    once and judged again, so a concurrent accept is refused, never overwritten.
    """
    from .transfer_store import ACCEPTED, FAILED, OPENED, PREPARED

    failure = (ABORT_PREFIX + (" ".join(reason.split()) or ABORT_DEFAULT_REASON))[:200]
    for _attempt in range(2):
        transfer = store.get_transfer(transfer_id)
        if transfer is None:
            return _Abort(1, f"no such transfer {transfer_id}", transfer_id)
        tid, target = transfer.transfer_id, transfer.target_session
        if transfer.state == ACCEPTED:
            return _Abort(
                1,
                f"transfer {tid} is accepted: ownership already moved to {target}, so it "
                f"cannot be aborted; run `ccc continue-session accept {tid}` in the successor "
                "to finalize",
                tid,
                state=ACCEPTED,
                was=ACCEPTED,
                next_step=f"in the successor {target} run `ccc continue-session accept {tid}`",
            )
        if transfer.state not in (PREPARED, OPENED):
            note = f" ({transfer.failure})" if transfer.state == FAILED and transfer.failure else ""
            return _Abort(
                0,
                f"transfer {tid} already {transfer.state}{note}; nothing changed",
                tid,
                state=transfer.state,
                was=transfer.state,
            )
        reserved = [group.id for group in store.reserved_awaits(tid)]
        if not store.transition_transfer(tid, FAILED, now, failure=failure):
            continue  # the state moved between the read and the CAS: judge it again
        row = store.get(target)
        archived = row is not None and bool(row.draft)
        if archived:  # never launched, or launched and never started: as `_drive`'s launch failure
            store.update_fields(target, archived=True)
        what = [f"transfer {tid} {transfer.state} → failed ({failure})"]
        if reserved:
            what.append(f"released {len(reserved)} await group(s) to the source")
        if archived:
            what.append(f"archived the never-started successor {target}")
        elif row is not None:
            what.append(
                f"the successor {target} was started and stays open: its accept now "
                "refuses; close its tab"
            )
        step = (
            "nothing moved; to hand off again re-run `ccc continue-session start` in the "
            f"source session {transfer.source_session} (a new transfer)"
        )
        return _Abort(
            0,
            "aborted: " + "; ".join(what),
            tid,
            state=FAILED,
            was=transfer.state,
            changed=True,
            released=reserved,
            archived=archived,
            next_step=step,
        )
    return _Abort(1, f"transfer {transfer_id} kept changing state; try again", transfer_id)


def abort_transfer(store: Store, transfer_id: str, reason: str, now: int) -> tuple[int, str]:
    """Fail an active (``prepared``/``opened``) transfer: ``(exit code, message)``.

    The reusable core of ``continue-session abort`` (and of the dead-successor relaunch
    and the daemon sweep): ``failure = "aborted: <reason>"``, the reserved await groups
    released (the ``failed`` CAS's transaction), a draft successor archived."""
    done = _abort(store, transfer_id, reason, now)
    return done.code, done.message


def cmd_abort(args: argparse.Namespace) -> int:
    """``continue-session abort``: give up an active transfer whose successor is stuck."""
    from .models import now_ms
    from .store import Store

    with Store() as store:
        done = _abort(store, args.transfer, args.reason or "", now_ms())
    if args.json:
        print(json.dumps(done.to_dict()))
        return done.code
    stream = sys.stdout if done.code == 0 else sys.stderr
    print(done.message if done.code == 0 else f"error: {done.message}", file=stream)
    if done.next_step:
        print(f"next: {done.next_step}", file=stream)
    return done.code


_VERBS = {
    "state": cmd_state,
    "template": cmd_template,
    "drain": cmd_drain,
    "start": cmd_start,
    "accept": cmd_accept,
    "status": cmd_status,
    "abort": cmd_abort,
}


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch ``ccc continue-session <verb>``."""
    from . import handoff_facts, lean_check

    verb = getattr(args, "cs_command", None)
    if verb == "lean-check":
        return lean_check.cmd_lean_check(args)
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
            "  ccc continue-session drain -t 540\n"
            "  ccc continue-session lean-check -s <sid> -j   # read-only code-relay verdict"
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
            "example (from inside the session; pass the Bash tool timeout 600000):\n"
            "  ccc continue-session start -A private -D 30 -f /path/handoff.md  # new-session-ok\n"
            "example (from any other shell; the source must be live and idle):\n"
            "  ccc continue-session start -X -s <sid> -A private -f /path/handoff.md"
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
    p_start.add_argument(
        "-X",
        "--outside",
        action="store_true",
        help="run from another shell, not the source session: requires -s, implies "
        "--await-idle, the source must be live (tp#1722)",
    )
    p_start.add_argument("-s", "--session", help="the source session id (default: this session)")
    p_start.add_argument("-O", "--overseer", help="model of the new session (default: observed)")
    p_start.add_argument("-E", "--executor", help="executor model for delegated work")
    p_start.add_argument(
        "-w",
        "--wait",
        type=int,
        default=ACCEPT_WAIT_DEFAULT_SEC,
        help=f"seconds to wait for the successor's accept (default {ACCEPT_WAIT_DEFAULT_SEC}); "
        f"lowered to {START_CALL_BUDGET_SEC} − drain − Σ handoff_facts timeout_s − "
        f"{START_CALL_MARGIN_SEC}, never below {ACCEPT_WAIT_FLOOR_SEC}",
    )
    p_start.add_argument(
        "-D",
        "--drain",
        type=_drain_seconds,
        default=None,
        metavar="SECONDS",
        help=f"first wait up to SECONDS (0..{START_DRAIN_MAX_SEC}) until nothing is in flight; "
        "still busy → exit 1 with the inventory, no transfer (default: no wait)",
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

    p_abort = verbs.add_parser(
        "abort",
        help="give up a prepared/opened transfer (stuck or dead successor); nothing moves",
        description=(
            "Fail an active transfer: prepared|opened → failed with 'aborted: <reason>'. "
            "The reserved await groups go back to the source and a never-started successor "
            "is archived; a started successor tab stays open (its accept then refuses). An "
            "accepted transfer is refused (ownership already moved: accept it in the "
            "successor); a finalized or failed one is reported unchanged (exit 0)."
        ),
        epilog="example:\n  ccc continue-session abort t0123456789ab -r 'successor hung'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_abort.add_argument("transfer", help="the transfer id (see `continue-session status`)")
    p_abort.add_argument(
        "-r",
        "--reason",
        default=ABORT_DEFAULT_REASON,
        help=f"recorded as 'aborted: REASON' (default {ABORT_DEFAULT_REASON!r})",
    )
    p_abort.add_argument("-j", "--json", action="store_true", help="JSON output")

    from . import handoff_facts, lean_check

    handoff_facts.add_parsers(verbs)
    lean_check.add_parser(verbs)
