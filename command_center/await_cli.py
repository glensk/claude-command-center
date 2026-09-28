#!/usr/bin/env python3
"""``ccc await`` — park a session on an external event and resume it when it fires.

Arming (the default form) takes each source's REMOTE baseline first (a Zoho ticket's
newest inbound thread, a Slack DM's newest ts), ensures the session's cwd is trusted
for its account — a deliberate, arm-time act; automation later only CHECKS trust — and
then writes the group and all its sources in ONE transaction (with ``-C`` also the
close-after-turn arm). Any failure before that transaction writes nothing.

The management verbs ``-l`` (list), ``-d`` (disarm), ``-R`` (retry a blocked group) and
``-r`` (one evaluation pass — what the poller runs every 60 s) are mutually exclusive
with arming and with each other. ``await`` is on cli's hot fast path: this module
imports nothing heavy at load time, and ``-r`` returns after one indexed query when no
group is active.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=import-outside-toplevel  # the hot path imports on demand
import argparse
import dataclasses
import functools
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

MAX_UNTIL_DAYS = 180
_RELATIVE = re.compile(r"^(\d+)([mhdw])$")
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}

EXIT_USAGE = 2
EXIT_MISSING_DEP = 3


class AwaitError(Exception):
    """A refusal with its exit code (1 = runtime, 2 = usage, 3 = missing dependency)."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- parser
def add_parser(sub: Any, func: Any) -> argparse.ArgumentParser:
    """Build the ``await`` subparser (used by cli's full AND hot-path parser)."""
    p = sub.add_parser(
        "await",
        help="resume a parked session when an external event fires (Zoho reply, Slack DM, cmd)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Arm an await group on a session: the first source to fire wins, and ccc\n"
            "resumes THAT session (same transcript, same account) with the message\n"
            "template, {event} replaced by the event as bounded JSON. No model runs\n"
            "while waiting. Nothing fires before --until -> the group expires (24 h\n"
            "grace for events dated before the deadline) with a notification.\n\n"
            "examples:\n"
            "  ccc await -z 123 -u 2026-10-05 -m 'The requester replied: {event}. Continue.' -C\n"
            "  ccc await -S U012AB3CD -z 123 -u 3d -m 'Answer arrived: {event}'\n"
            "  ccc await -x 'gh pr checks 12 --required' -i 300 -u 1d -m 'CI done: {event}'\n"
            "  ccc await -l            # this session's groups (-A: every session)\n"
            "  ccc await -d 7          # disarm group 7 (-d all: this session's)\n"
            "  ccc await -R 7          # retry a blocked group\n"
            "  ccc await -r            # one evaluation pass now (the poller's job)\n"
        ),
    )
    p.add_argument("-s", "--session", help="target session id (default: the calling session)")
    p.add_argument(
        "-z",
        "--zoho",
        action="append",
        default=[],
        metavar="TICKET",
        help="fire on a new inbound reply on this Zoho Desk ticket (zoho-api.py -i)",
    )
    p.add_argument(
        "-S",
        "--slack-dm",
        action="append",
        default=[],
        metavar="USER",
        help="fire on a Slack DM from USER (member id, @handle or email; slack_api.py)",
    )
    p.add_argument(
        "-x",
        "--cmd",
        action="append",
        default=[],
        metavar="CMD",
        help="fire when this shell predicate exits 0 (its stdout is the event)",
    )
    p.add_argument(
        "-i",
        "--interval",
        type=int,
        default=120,
        metavar="SEC",
        help="seconds between probes of each source (>= 60, default 120)",
    )
    p.add_argument(
        "-u",
        "--until",
        metavar="DATE",
        help="deadline: YYYY-MM-DD (end of that day), YYYY-MM-DDTHH:MM, or 30m/12h/3d/2w",
    )
    p.add_argument(
        "-m",
        "--message",
        metavar="TEMPLATE",
        help="the prompt the session resumes with; must contain {event}",
    )
    p.add_argument(
        "-C",
        "--close",
        action="store_true",
        help="close this session's tab after the current turn (only on the calling session)",
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="arm: take the baselines, write nothing; -r: probe and deliver nothing",
    )
    p.add_argument("-j", "--json", action="store_true", help="machine-readable output")
    verbs = p.add_mutually_exclusive_group()
    verbs.add_argument("-l", "--list", action="store_true", help="list await groups")
    verbs.add_argument(
        "-d", "--disarm", metavar="GROUP", help="disarm a group id, or 'all' (this session's)"
    )
    verbs.add_argument("-R", "--retry", type=int, metavar="GROUP", help="retry a blocked group")
    verbs.add_argument("-r", "--run", action="store_true", help="one evaluation pass now")
    p.add_argument("-A", "--all", action="store_true", help="-l / -d all: every session")
    p.set_defaults(func=func)
    return p


# --------------------------------------------------------------------------- helpers
def parse_until(value: str, now: float) -> int:
    """``--until`` to an epoch; AwaitError(2) when unparseable, past or too far out."""
    text = (value or "").strip()
    rel = _RELATIVE.match(text)
    if rel:
        epoch = int(now) + int(rel.group(1)) * _UNITS[rel.group(2)]
    else:
        try:
            if len(text) == 10:
                day = datetime.strptime(text, "%Y-%m-%d")
                epoch = int((day + timedelta(days=1) - timedelta(seconds=1)).timestamp())
            else:
                epoch = int(datetime.fromisoformat(text).timestamp())
        except ValueError as exc:
            raise AwaitError(f"cannot read --until {text!r}", EXIT_USAGE) from exc
    if epoch <= now:
        raise AwaitError(f"--until {text!r} is not in the future", EXIT_USAGE)
    if epoch > now + MAX_UNTIL_DAYS * 86400:
        raise AwaitError(f"--until is more than {MAX_UNTIL_DAYS} days out", EXIT_USAGE)
    return epoch


def _fmt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "-"


def _emit(args: argparse.Namespace, payload: Any, text: str) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif text:
        print(text)


def _caller_session_id() -> str:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


def _headless() -> bool:
    """A ``claude -p`` / SDK run has no tab (the same rule as ``cli``'s close arm)."""
    return bool(os.environ.get("CCC_INTERNAL")) or os.environ.get(
        "CLAUDE_CODE_ENTRYPOINT", ""
    ).startswith("sdk")


def _target_session_id(args: argparse.Namespace) -> str:
    """Explicit ``-s`` > the calling session's env id > the live session in this cwd."""
    sid = args.session or _caller_session_id()
    if not sid:
        from .adapters.claude import ClaudeAdapter

        here = os.getcwd()
        live = [s for s in ClaudeAdapter().discover() if s.cwd == here]
        live.sort(key=lambda s: (s.raw_status != "busy", -s.updated_at))
        sid = live[0].session_id if live else ""
    if not sid:
        raise AwaitError("no session: pass -s/--session or run inside a Claude session", EXIT_USAGE)
    return sid


def _baseline_error(what: str, result: Any) -> AwaitError:
    from .checks import sanitize_error

    detail = result.spawn_error or ("timeout" if result.timed_out else f"exit {result.exit}")
    return AwaitError(f"{what}: baseline failed ({detail}): {sanitize_error(result.stderr)}")


# --------------------------------------------------------------------------- sources
def _zoho_source(ticket: str, interval: int, runner: Any) -> Any:
    from . import await_probes, external_deps
    from .await_store import SourceSpec

    ticket = ticket.strip().lstrip("#")
    if not ticket.isdigit():
        raise AwaitError(f"-z {ticket!r}: not a ticket number", EXIT_USAGE)
    exe = external_deps.await_exe("zoho-api.py", needed_for="ccc await -z")
    result = runner(await_probes.zoho_argv(exe, ticket, ""), timeout=30.0, max_bytes=512 * 1024)
    try:
        data = json.loads(result.stdout) if result.exit == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise _baseline_error(f"zoho ticket {ticket}", result)
    return SourceSpec(
        kind="zoho-reply",
        spec={"schema_version": 1, "exe": exe, "ticket": ticket},
        watermark=str(data.get("watermark") or ""),
        interval_sec=interval,
    )


def _slack_source(user: str, interval: int, runner: Any) -> Any:
    from . import await_probes, external_deps
    from .await_store import SourceSpec

    exe = external_deps.await_exe("slack_api.py", needed_for="ccc await -S")
    user_id = user.strip()
    if not await_probes.is_slack_user_id(user_id):
        who = runner([exe, "--whois", user_id, "--json"], timeout=30.0, max_bytes=512 * 1024)
        try:
            record = json.loads(who.stdout) if who.exit == 0 else None
        except ValueError:
            record = None
        found = str(record.get("id") or "") if isinstance(record, dict) else ""
        if not await_probes.is_slack_user_id(found):
            raise _baseline_error(f"slack user {user_id!r}", who)
        user_id = found
    result = runner(await_probes.slack_argv(exe, user_id, ""), timeout=30.0, max_bytes=512 * 1024)
    base = await_probes.slack_baseline(result.stdout) if result.exit == 0 else None
    if base is None:
        raise _baseline_error(f"slack DM with {user_id}", result)
    channel, newest = base
    return SourceSpec(
        kind="slack-dm",
        spec={"schema_version": 1, "exe": exe, "user_id": user_id, "channel": channel},
        watermark=newest,
        interval_sec=interval,
    )


def _cmd_source(command: str, cwd: str, interval: int) -> Any:
    from .await_store import SourceSpec

    if not command.strip():
        raise AwaitError("-x needs a command", EXIT_USAGE)
    return SourceSpec(
        kind="cmd",
        spec={"schema_version": 1, "cmd": command, "cwd": cwd},
        watermark="",
        interval_sec=interval,
    )


# --------------------------------------------------------------------------- verbs
def _validate_arm(args: argparse.Namespace, now: float) -> int:
    """Every check that needs no store and no network; returns the ``until`` epoch."""
    from .await_prompt import template_error
    from .await_store import MIN_INTERVAL_SEC

    if not (args.zoho or args.slack_dm or args.cmd):
        raise AwaitError("arm needs at least one source: -z, -S or -x", EXIT_USAGE)
    if not args.message or not args.until:
        raise AwaitError("arming needs -u/--until and -m/--message", EXIT_USAGE)
    problem = template_error(args.message)
    if problem:
        raise AwaitError(problem, EXIT_USAGE)
    if args.interval < MIN_INTERVAL_SEC:
        raise AwaitError(f"-i/--interval must be >= {MIN_INTERVAL_SEC}", EXIT_USAGE)
    return parse_until(args.until, now)


def _check_close(session: Any, sid: str) -> None:
    """``-C`` only on the calling, interactive session with no switch pending."""
    if _caller_session_id() != sid:
        raise AwaitError("-C closes only the CALLING session's own tab", EXIT_USAGE)
    if _headless():
        raise AwaitError("-C: a headless/SDK session has no tab to close", EXIT_USAGE)
    if session.switch_requested_at:
        raise AwaitError(
            "-C: an account switch is pending for this session; let it finish first",
            EXIT_USAGE,
        )


def _snapshot(session: Any, sid: str) -> tuple[str, str]:
    """The ``(config_dir, cwd)`` the delivery will resume under."""
    from . import accounts

    config_dir = session.config_dir or (
        accounts.env_config_dir() if _caller_session_id() == sid else ""
    )
    if not config_dir and accounts.is_multi_account():
        raise AwaitError("the session's account is unknown; resume it once first")
    cwd = session.cwd
    if not cwd or not os.path.isdir(cwd):
        raise AwaitError(f"the session's working directory {cwd!r} is missing")
    return config_dir, cwd


def _baselines(args: argparse.Namespace, cwd: str, runner: Any) -> list[Any]:
    """Every source's remote baseline; the first failure aborts the whole arm."""
    sources = [_zoho_source(t, args.interval, runner) for t in args.zoho]
    sources += [_slack_source(u, args.interval, runner) for u in args.slack_dm]
    sources += [_cmd_source(c, cwd, args.interval) for c in args.cmd]
    return sources


def _arm(args: argparse.Namespace, runner: Any) -> int:
    from . import accounts
    from .await_store import AwaitConflict
    from .models import now_ms
    from .store import Store

    now = time.time()
    until = _validate_arm(args, now)
    sid = _target_session_id(args)
    with Store() as store:
        session = store.get(sid)
        if session is None:
            raise AwaitError(f"unknown session {sid}", EXIT_USAGE)
        if args.close:
            _check_close(session, sid)
        config_dir, cwd = _snapshot(session, sid)
        sources = _baselines(args, cwd, runner)
        preview = {
            "session_id": sid,
            "until": until,
            "config_dir": config_dir,
            "cwd": cwd,
            "close": bool(args.close),
            "sources": [dataclasses.asdict(s) for s in sources],
        }
        if args.dry_run:
            _emit(
                args,
                {"dry_run": True, **preview},
                f"dry run: would arm {len(sources)} source(s) on {sid[:8]} until {_fmt(until)}",
            )
            return 0
        accounts.ensure_trusted(config_dir, cwd)  # the deliberate, arm-time trust grant
        if not accounts.is_trusted(config_dir, cwd):
            raise AwaitError(f"could not trust {cwd} for the session's account (no .claude.json?)")
        common: dict[str, Any] = {
            "config_dir": config_dir,
            "cwd": cwd,
            "no_codex": bool(session.no_codex),
            "prompt_template": args.message,
            "until_epoch": until,
            "sources": sources,
            "now": int(now),
        }
        try:
            if args.close:
                group_id, _token = store.arm_await_and_close(sid, close_now_ms=now_ms(), **common)
            else:
                group_id = store.arm_await(sid, **common)
        except AwaitConflict as exc:
            raise AwaitError(f"{exc} (ccc await -l; -d GROUP to disarm it)") from exc
    kinds = ", ".join(s.kind for s in sources)
    _emit(
        args,
        {"group_id": group_id, **preview},
        f"armed await group {group_id} on {sid[:8]}: {kinds}; until {_fmt(until)}"
        + ("; this tab closes after the turn" if args.close else ""),
    )
    return 0


def _group_json(group: Any, sources: list[Any]) -> dict[str, Any]:
    return {
        "id": group.id,
        "session_id": group.session_id,
        "state": group.state,
        "until": group.until_epoch,
        "grace_until": group.grace_until_epoch,
        "blocked_reason": group.blocked_reason,
        "event_id": group.event_id,
        "delivery_attempts": group.delivery_attempts,
        "sources": [
            {
                "id": s.id,
                "kind": s.kind,
                "state": s.state,
                "next_check_at": s.next_check_at,
                "fail_count": s.fail_count,
                "last_error": s.last_error,
            }
            for s in sources
        ],
    }


def _list(args: argparse.Namespace) -> int:
    from .store import Store

    sid = None if args.all else _target_session_id(args)
    with Store() as store:
        rows = store.list_awaits(sid, include_inactive=True)[:50]
    if args.json:
        print(json.dumps([_group_json(g, s) for g, s in rows], sort_keys=True))
        return 0
    if not rows:
        print("no await groups")
        return 0
    for group, sources in rows:
        extra = f"  ({group.blocked_reason})" if group.blocked_reason else ""
        print(
            f"group {group.id}  {group.state:<10}  session {group.session_id[:8]}  "
            f"until {_fmt(group.until_epoch)}{extra}"
        )
        for src in sources:
            err = f"  last error: {src.last_error}" if src.last_error else ""
            print(
                f"    source {src.id}  {src.kind:<10}  {src.state:<8}  "
                f"next {_fmt(src.next_check_at)}  fails {src.fail_count}{err}"
            )
    return 0


def _disarm(args: argparse.Namespace) -> int:
    from .store import Store

    now = int(time.time())
    with Store() as store:
        if args.disarm == "all":
            sid = None if args.all else _target_session_id(args)
            ids = [g.id for g, _s in store.list_awaits(sid)]
        else:
            try:
                ids = [int(args.disarm)]
            except ValueError as exc:
                raise AwaitError("-d takes a group id or 'all'", EXIT_USAGE) from exc
        done = [gid for gid in ids if store.disarm_group(gid, now, reason="disarmed by user")]
    missing = sorted(set(ids) - set(done))
    _emit(
        args,
        {"disarmed": done, "not_active": missing},
        (f"disarmed group(s) {', '.join(map(str, done))}" if done else "nothing to disarm")
        + (f"; not active: {', '.join(map(str, missing))}" if missing else ""),
    )
    return 0 if done or args.disarm == "all" else 1


def _retry(args: argparse.Namespace) -> int:
    from .store import Store

    with Store() as store:
        state = store.retry_group(args.retry, int(time.time()))
    if not state:
        raise AwaitError(f"group {args.retry} is not blocked (or has nothing left to wait for)")
    _emit(args, {"group_id": args.retry, "state": state}, f"group {args.retry} → {state}")
    return 0


def _run(args: argparse.Namespace, runner: Any) -> int:
    from .store import Store

    with Store() as store:
        if not store.has_active_awaits():
            _emit(args, {"idle": True}, "")
            return 0
        from . import await_eval

        report = await_eval.run_pass(
            store,
            dry_run=args.dry_run,
            runner=runner,
            notifier=None if args.dry_run else await_eval.config_notifier(),
        )
    payload = dataclasses.asdict(report)
    text = (
        ""
        if report.is_empty()
        else " ".join(f"{k}={v}" for k, v in payload.items() if v and k != "idle")
    )
    _emit(args, payload, text)
    return 0


def cmd_await(args: argparse.Namespace, *, runner: Any = None) -> int:
    """Dispatch ``ccc await`` (see the module docstring)."""
    from extdeps import MissingExternalDependency

    if runner is None:
        from .checks import run_structured

        runner = run_structured
    verb = args.run or args.list or bool(args.disarm) or args.retry is not None
    arming = bool(args.zoho or args.slack_dm or args.cmd or args.message or args.until)
    if verb and (arming or args.close):
        print("error: -l, -d, -R and -r cannot be combined with arming", file=sys.stderr)
        return EXIT_USAGE
    action: Callable[[], int]
    if args.run:
        action = functools.partial(_run, args, runner)
    elif args.list:
        action = functools.partial(_list, args)
    elif args.disarm:
        action = functools.partial(_disarm, args)
    elif args.retry is not None:
        action = functools.partial(_retry, args)
    else:
        action = functools.partial(_arm, args, runner)
    try:
        return action()
    except MissingExternalDependency as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MISSING_DEP
    except AwaitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
