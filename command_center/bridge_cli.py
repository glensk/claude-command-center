#!/usr/bin/env python3
"""Machine-facing ``ccc … -j`` commands of the voice bridge (PLAN_claude-bridge.md §6).

``sessions``, ``name``, ``inspect``, ``send``, ``answer``, ``delivery`` and ``events`` —
internal-style commands (no TUI key) that the voice assistant and other agents drive with
``-j``. The envelope, exit codes and input checks live in :mod:`command_center.bridge_json`:

* stdout carries exactly ONE envelope ``{"schema_version": 1, "ok": bool, "data": {…} |
  null, "error": {"code": str, "message": str} | null}``; logs go to stderr only;
* exit codes 0 ok · 1 refused/failed (``error.code`` says why) · 2 usage · 3 internal;
* times are ISO-8601 UTC; unknown input (arguments, fields) is a usage error (2) — with
  ``-j`` an argv error is an envelope too (:func:`dispatch`).

``ccc sessions -j`` lists the interactive and background Claude Code sessions of EVERY
configured account with their memorable name, status and iTerm tab — background jobs
from the CLI's own ``claude agents --json`` roster too (a job whose worker is not running
has no registry entry), with a null, unvalidated tab. The status is the CLI's view mapped
to §6 (:mod:`command_center.adapters.claude_agents`: registry ``shell`` → busy). The tab is
``validated`` only when the registry pid's controlling tty is the tab's tty (spike
S-IDENT); a stored tab id that is missing or wrong is resolved by that tty
(:func:`bridge_target.resolve_tab`). Headless ``claude -p`` runs (registry
``entrypoint`` ``sdk-cli``) are not sessions and never listed.

``ccc name -s ID "x y"`` renames a session (a name another active session holds is
refused, exit 1) and prints the ``/rename <name>`` line to type into the running Claude
Code — ccc never renames a running process itself.

``inspect`` / ``send`` / ``answer`` / ``delivery`` / ``events`` read a session's
transcript, type into its tab and follow deliveries up (``bridge_inspect`` …
``bridge_events``). Message text travels on stdin only. Without ``-j`` the same data is
printed as indented JSON and a refusal as one ``❌`` line on stderr; the exit codes are
the same either way.
"""

# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, NoReturn

from . import bridge_target
from .bridge_json import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    MESSAGE_MAX,
    BridgeError,
    emit,
    envelope,
    iso,
    log,
    run_json,
    usage,
)
from .bridge_target import resolve_tab

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter
    from .adapters.claude_agents import AgentEntry
    from .config import Config
    from .models import LiveSession, Session
    from .store import Store
    from .tab_titles import ItermPane

# Subcommands whose argv errors are answered with an envelope (see :func:`dispatch`).
JSON_COMMANDS: frozenset[str] = frozenset(
    {"sessions", "name", "inspect", "send", "answer", "delivery", "events"}
)

_AIM_SHORT_MAX = 80
#: Upper bound read from stdin (characters) — far above every accepted input.
_STDIN_CAP = MESSAGE_MAX * 4 + 1


def _usage_exit(message: str) -> NoReturn:
    emit(envelope(False, None, usage("usage", message)))
    raise SystemExit(EXIT_USAGE)


def _wants_json(argv: Iterable[str]) -> bool:
    return any(a in ("-j", "--json") for a in argv)


def dispatch(parser: argparse.ArgumentParser, argv: list[str]) -> int:
    """Parse + run a :data:`JSON_COMMANDS` subcommand; argv errors become envelopes (exit 2).

    Unknown arguments are refused (never ignored). Without ``-j`` argparse's own
    stderr message is kept.
    """
    if not _wants_json(argv):
        args = parser.parse_args(argv)
        return int(args.func(args))
    sub = _find_subparser(parser, argv[0])
    if sub is not None:
        sub.error = _usage_exit  # type: ignore[method-assign]
    parser.error = _usage_exit  # type: ignore[method-assign]
    args, extra = parser.parse_known_args(argv)
    if extra:
        _usage_exit(f"unrecognized arguments: {' '.join(extra)}")
    return int(args.func(args))


def _find_subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser | None:
    for action in parser._actions:  # pylint: disable=protected-access
        if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access
            choice = action.choices.get(name)
            if isinstance(choice, argparse.ArgumentParser):
                return choice
    return None


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def _aim_short(session: Session | None) -> str | None:
    if session is None:
        return None
    if session.short_aim:
        return session.short_aim
    aim = (session.aim or "").strip()
    if not aim:
        return None
    line = aim.splitlines()[0].strip()
    return line if len(line) <= _AIM_SHORT_MAX else line[: _AIM_SHORT_MAX - 1] + "…"


def session_entry(
    live: LiveSession,
    row: Session | None,
    tab: tuple[str | None, bool],
    account_label: Callable[[str], str],
    status: str | None = None,
) -> dict[str, Any]:
    """One ``sessions[]`` item of the §6 contract (*status* defaults to the registry's)."""
    from .adapters.claude_agents import contract_status

    return {
        "session_id": live.session_id,
        "name": (row.canonical_name or None) if row else None,
        "name_origin": (row.canonical_name_origin or None) if row and row.canonical_name else None,
        "account": account_label(live.config_dir) if live.config_dir else None,
        "account_conflict": bool(live.conflict),
        "pid": live.pid or None,
        "kind": "interactive" if live.kind == "interactive" else "background",
        "status": status if status is not None else contract_status(live.raw_status),
        "status_at": iso(live.status_updated_at or live.updated_at),
        "aim_short": _aim_short(row),
        "tab": {"iterm_session_id": tab[0], "validated": tab[1]},
    }


def _agent_for(live: LiveSession, by_sid: dict[str, AgentEntry]) -> AgentEntry | None:
    """The ``claude agents`` entry of *live* (same account unless the id is in conflict)."""
    from . import accounts

    agent = by_sid.get(live.session_id)
    if agent is None or not live.config_dir:
        return agent
    return agent if accounts.same_config_dir(agent.config_dir, live.config_dir) else None


def live_status(live: LiveSession, agent: AgentEntry | None) -> str:
    """The §6 status: the CLI's own view (``claude agents``) first, else the registry's.

    A background session's status comes from its ``state`` (``working`` → busy,
    ``blocked`` / ``failed`` → blocked, ``done`` / ``stopped`` → idle).
    """
    from .adapters.claude_agents import background_status, contract_status

    if live.kind != "interactive" or (agent is not None and agent.background):
        state = agent.state if agent is not None else ""
        raw = (agent.status if agent is not None else "") or live.raw_status
        return background_status(state, raw)
    if agent is not None and agent.status:
        return contract_status(agent.status)
    return contract_status(live.raw_status)


def _background_live(agent: AgentEntry) -> LiveSession:
    """A registry-shaped row for a background job only ``claude agents`` lists (no worker)."""
    from .models import LiveSession as _Live

    return _Live(
        pid=agent.pid,
        session_id=agent.session_id,
        cwd=agent.cwd,
        kind="bg",
        raw_status=agent.status,
        name=agent.name or None,
        started_at=agent.started_at,
        updated_at=agent.started_at,
        alive=True,
        config_dir=agent.config_dir,
    )


def _merge_lives(lives: list[LiveSession], agents: list[AgentEntry]) -> list[LiveSession]:
    """*lives* plus every background session only the ``claude agents`` roster knows (D4)."""
    seen = {live.session_id for live in lives}
    out = list(lives)
    for agent in agents:
        if agent.background and agent.session_id not in seen:
            out.append(_background_live(agent))
            seen.add(agent.session_id)
    return out


def _named_row(
    db: Store,
    live: LiveSession,
    background: bool,
    cfg: Config,
    hint: str,
) -> Session | None:
    """The ccc row of *live*, given its deterministic name first when it has none.

    A background session gets a row of its own when ccc has none (no hook ever ran for a
    job whose worker is not running), so it carries a canonical name like the others.
    """
    from . import session_names

    row = db.get(live.session_id)
    if row is None and background and cfg.session_names:
        db.ensure(live.session_id, cwd=live.cwd)
        if live.config_dir:
            db.update_fields(live.session_id, config_dir=live.config_dir)
        row = db.get(live.session_id)
    if row is not None and cfg.session_names and not row.canonical_name and not row.done:
        session_names.ensure_name(db, live.session_id, use_llm=False, cfg=cfg, hint=hint)
        row = db.get(live.session_id)
    return row


def collect_sessions(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    adapter: ClaudeAdapter | None = None,
    store: Store | None = None,
    cfg: Config | None = None,
    panes_reader: Callable[[], list[ItermPane] | None] | None = None,
    ps_reader: Callable[[], dict[int, Any]] | None = None,
    account_label: Callable[[str], str] | None = None,
    agents_reader: Callable[[], list[AgentEntry]] | None = None,
) -> list[dict[str, Any]]:
    """Every Claude Code session of every account, shaped for ``ccc sessions -j``.

    Live registry sessions (interactive and ``bg``) plus every background session the
    CLI's ``claude agents --json`` lists (D4: a job whose worker is not running has no
    registry entry). The status prefers the CLI's view. Background sessions never get a
    tab (``iterm_session_id`` null, not validated). A session without a name gets its
    deterministic one first (no LLM inside the 3 s budget — the daemon and ``set-aim``
    hand out the LLM names). Every collaborator is injectable so tests never touch iTerm,
    ``ps``, ``claude`` or the real registries.
    """
    from . import accounts, config, tab_titles, terminal
    from .adapters import claude_agents
    from .adapters.claude import ClaudeAdapter as _Adapter
    from .store import Store as _Store

    cfg = cfg if cfg is not None else config.load_config()
    adapter = adapter if adapter is not None else _Adapter()
    registry = [
        live
        for live in adapter.discover()
        if live.alive
        and not str(live.entrypoint).startswith("sdk")
        and live.kind in claude_agents.SESSION_KINDS
    ]
    agents = (agents_reader or claude_agents.list_account_agents)()
    by_sid: dict[str, AgentEntry] = {}
    for roster in agents:
        by_sid.setdefault(roster.session_id, roster)
    lives = _merge_lives(registry, agents)
    order = {label: i for i, label in enumerate(config.claude_config_dirs())}
    label_of = account_label or accounts.account_label
    ps = (ps_reader or terminal.ps_table)() if registry else {}
    panes = (panes_reader or tab_titles.read_panes)() if registry else None
    out: list[dict[str, Any]] = []
    own_store = store is None
    db = store if store is not None else _Store()
    try:
        for live in lives:
            agent = _agent_for(live, by_sid)
            background = live.kind != "interactive" or (agent is not None and agent.background)
            hint = (agent.name if agent is not None else "") if background else ""
            row = _named_row(db, live, background, cfg, hint)
            if background:
                tab: tuple[str | None, bool] = (None, False)
            else:
                tab = resolve_tab(row.iterm_session_id if row else None, live.pid, ps, panes)
            entry = session_entry(live, row, tab, label_of, live_status(live, agent))
            if background:
                entry["kind"] = "background"
            out.append(entry)
    finally:
        if own_store:
            db.close()
    out.sort(
        key=lambda e: (
            order.get(e["account"] or "", len(order)),
            (e["name"] or "").casefold(),
            e["session_id"],
        )
    )
    return out


def cmd_sessions(args: argparse.Namespace) -> int:
    """``ccc sessions [-j]`` — every live Claude Code session, both accounts."""
    if getattr(args, "json", False):
        return run_json(lambda: {"sessions": collect_sessions()})
    try:
        sessions = collect_sessions()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    if not sessions:
        print("(no live Claude Code sessions)")
        return EXIT_OK
    for entry in sessions:
        tab = "✅ tab" if entry["tab"]["validated"] else "❌ tab"
        print(
            f"{entry['session_id'][:8]}  {(entry['name'] or '-'):<26} "
            f"{(entry['account'] or '?'):<8} {entry['kind']:<11} {entry['status']:<8} {tab}"
        )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# name
# --------------------------------------------------------------------------- #
def _resolve_id(store: Store, given: str | None) -> str:
    from .store import AmbiguousJobId, resolve_job_id

    if not given:
        # Inside a Claude Code session its id is in the env (the same two variables
        # `cli.resolve_session_id` reads first).
        given = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not given:
            raise BridgeError("usage", "pass -s/--session <id>", EXIT_USAGE)
    try:
        sid = resolve_job_id(store, given)
    except AmbiguousJobId as exc:
        raise BridgeError("ambiguous_session", str(exc)) from exc
    if not sid:
        raise BridgeError("not_found", f"no session {given}")
    return sid


def name_command(args: argparse.Namespace, store: Store | None = None) -> dict[str, Any]:
    """The ``ccc name`` work; returns the envelope's ``data``. Raises :class:`BridgeError`."""
    from . import session_names
    from .store import Store as _Store

    own = store is None
    db = store if store is not None else _Store()
    try:
        sid = _resolve_id(db, args.session)
        if args.auto:
            from . import config

            before = db.get(sid)
            previous = before.canonical_name if before is not None else None
            name = session_names.ensure_name(db, sid, use_llm=True, cfg=config.load_config())
            # an LLM name replaced the provisional fallback: the running process shows the
            # old one until the user types /rename
            changed = bool(previous) and name != previous
        elif args.name is not None:
            try:
                name = session_names.rename(db, sid, " ".join(args.name))
            except session_names.NameTaken as exc:
                raise BridgeError("name_taken", str(exc)) from exc
            except ValueError as exc:
                raise BridgeError("invalid_name", str(exc), EXIT_USAGE) from exc
            changed = True
        else:
            session = db.get(sid)
            name = session.canonical_name if session else None
            changed = False
        session = db.get(sid)
    finally:
        if own:
            db.close()
    return {
        "session_id": sid,
        "name": name,
        "name_origin": (session.canonical_name_origin or None) if session and name else None,
        "changed": changed,
        "rename_command": f"/rename {name}" if name and changed else None,
    }


def cmd_name(args: argparse.Namespace) -> int:
    """``ccc name [-s ID] ["x y"] [-A] [-j]`` — show, set or auto-generate a session's name."""
    if getattr(args, "json", False):
        return run_json(lambda: name_command(args))
    try:
        data = name_command(args)
    except BridgeError as exc:
        print(f"❌ {exc.message}", file=sys.stderr)
        return exc.exit_code
    if not data["name"]:
        print(f"{data['session_id'][:8]}: no name yet")
        return EXIT_OK
    if data["changed"]:
        print(f"✅ {data['session_id'][:8]} is now named {data['name']!r}")
        print("To show it in the running Claude Code session, type there:")
        print(data["rename_command"])
    else:
        print(data["name"])
    return EXIT_OK


# --------------------------------------------------------------------------- #
# inspect / send / answer / delivery / events
# --------------------------------------------------------------------------- #
def _read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        raise usage("stdin_required", "the input must come on stdin (it is never an argument)")
    return sys.stdin.read(_STDIN_CAP)


def _run(
    args: argparse.Namespace, body: Callable[[bridge_target.BridgeDeps], dict[str, Any]]
) -> int:
    """Run *body*; print the envelope (``-j``) or a readable form; return the exit code."""

    def call() -> dict[str, Any]:
        return body(bridge_target.default_deps())

    if getattr(args, "json", False):
        return run_json(call)
    try:
        data = call()
    except BridgeError as err:
        if err.data:
            print(json.dumps(err.data, indent=2, ensure_ascii=False))
        log(f"❌ {err.code}: {err.message}")
        return err.exit_code
    except Exception as exc:  # pylint: disable=broad-exception-caught  # exit 3, never a trace
        log(f"❌ internal: {type(exc).__name__}: {exc}")
        return EXIT_INTERNAL
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    from .bridge_inspect import run_inspect

    return _run(args, lambda deps: run_inspect(deps, args.session, use_llm=not args.no_llm))


def cmd_send(args: argparse.Namespace) -> int:
    from .bridge_send import run_send

    def body(deps: bridge_target.BridgeDeps) -> dict[str, Any]:
        return run_send(deps, args.session, _read_stdin())

    return _run(args, body)


def cmd_answer(args: argparse.Namespace) -> int:
    from .bridge_answer import run_answer

    def body(deps: bridge_target.BridgeDeps) -> dict[str, Any]:
        text = _read_stdin()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise usage("invalid_json", f"stdin is not JSON: {exc.msg}") from exc
        return run_answer(deps, args.session, raw)

    return _run(args, body)


def cmd_delivery(args: argparse.Namespace) -> int:
    from .bridge_events import run_delivery

    return _run(args, lambda deps: run_delivery(deps, args.id))


def cmd_events(args: argparse.Namespace) -> int:
    from .bridge_events import run_events

    return _run(args, lambda deps: run_events(deps, args.after, args.limit))


# --------------------------------------------------------------------------- #
# parser wiring
# --------------------------------------------------------------------------- #
def add_parsers(sub: Any) -> None:
    """Register every voice-bridge subcommand (:data:`JSON_COMMANDS`) on *sub*."""
    p_sessions = sub.add_parser(
        "sessions",
        help="every live Claude Code session of every account (name, status, tab)",
        description="List live Claude Code sessions of all accounts. Example: ccc sessions -j",
    )
    p_sessions.add_argument(
        "-j", "--json", action="store_true", help="print the versioned JSON envelope"
    )
    p_sessions.set_defaults(func=cmd_sessions)

    p_name = sub.add_parser(
        "name",
        help="show / set a session's memorable name (prints /rename for the running session)",
        description=(
            'Show or set a session\'s name. Examples: ccc name -s 1a2b "voice bridge"; '
            "ccc name -s 1a2b -j"
        ),
    )
    p_name.add_argument("name", nargs="*", default=None, help="the new name (1-2 words)")
    p_name.add_argument("-s", "--session", help="session id or unique prefix (default: this one)")
    p_name.add_argument(
        "-A",
        "--auto",
        action="store_true",
        help="internal: generate the name once if the session has none (LLM, 10 s)",
    )
    p_name.add_argument(
        "-j", "--json", action="store_true", help="print the versioned JSON envelope"
    )
    p_name.set_defaults(func=_name_entry)

    _add_transcript_parsers(sub)


def _name_entry(args: argparse.Namespace) -> int:
    # argparse yields [] for an omitted nargs="*" positional; None means "show".
    if not args.name:
        args.name = None
    return cmd_name(args)


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-j", "--json", action="store_true", help="print the JSON envelope (schema_version 1)"
    )


def _add_transcript_parsers(sub: Any) -> None:
    """Register ``inspect``, ``send``, ``answer``, ``delivery`` and ``events``."""
    p = sub.add_parser(
        "inspect",
        help="what a session last said + the decision it needs (voice bridge)",
        description=(
            "Reads the session's transcript: state, last reply (tail, <= 1500 chars), last "
            "prompt (head, <= 500) and a pending decision (AskUserQuestion picker, or "
            "'You [decision]:' to-do lines while idle) with a spoken-briefing summary."
        ),
    )
    p.add_argument("-s", "--session", required=True, help="Claude session id")
    p.add_argument("-N", "--no-llm", action="store_true", help="skip the voice-brief LLM summary")
    _json_flag(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser(
        "send",
        help="type a message (stdin) into a live session's tab and correlate it",
        description=(
            "Reads the message from stdin (<= 4000 chars, no control characters except "
            "newline/tab), revalidates the tab, types it via the iTerm2 API and matches "
            "the transcript record. Example: echo 'run the tests' | ccc send -s ID -j"
        ),
    )
    p.add_argument("-s", "--session", required=True, help="Claude session id")
    _json_flag(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser(
        "answer",
        help="answer a pending AskUserQuestion picker (JSON on stdin)",
        description=(
            'stdin: {"decision_id": "<from ccc inspect>", "answers": [{"question_index": 0, '
            '"option_indices": [1]}, {"question_index": 1, "other_text": "..."}]}'
        ),
    )
    p.add_argument("-s", "--session", required=True, help="Claude session id")
    _json_flag(p)
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("delivery", help="state of one `ccc send` delivery")
    p.add_argument("-i", "--id", required=True, help="delivery id (from ccc send)")
    _json_flag(p)
    p.set_defaults(func=cmd_delivery)

    p = sub.add_parser(
        "events", help="problem events after a cursor (stop_failure, needs_input, …)"
    )
    p.add_argument("-a", "--after", type=int, default=0, help="return events after CURSOR (0)")
    p.add_argument("-l", "--limit", type=int, default=200, help="max events (200)")
    _json_flag(p)
    p.set_defaults(func=cmd_events)
