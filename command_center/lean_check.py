"""``ccc continue-session lean-check`` — may this session relay by code? (tp#1681 L19c).

Read-only. A relay hook asks it at a seam whether the successor can start from the facts
alone (``continue-session start --facts-only --await-idle``), without a model-written
handoff turn. The verdict is ``ready`` only when the session has:

* an **AIM** (the done-condition the successor inherits);
* a **recent next action or checkpoint**: a ``next_step`` while the session answered
  within the recency window, or a ``collect -S`` capture written within it;
* **facts providers**: at least one valid, required ``handoff_facts`` entry (the held tp
  items come from there);
* a **capsule per continuing agent**: every running subagent has an unconsumed relay
  relaunch record whose capsule file is readable (``relay_state.relaunch_for``); a
  subagent the store counts but the transcript does not name yet can have none.

Nothing is written: no store counter reset (unlike ``continue-session state``), no
provider run, no capture. The JSON shape (``VERSION``) is a contract with the hook::

    {"version": 1, "session_id": "…", "ready": false,
     "missing": ["next_action", "capsule:a1b2"],
     "aim": true, "next_action": "", "providers": ["tp"],
     "agents": [{"id": "a1b2", "capsule": ""}]}

Exit 0 when ready, 1 when not (or the session cannot be resolved).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter
    from .continue_session import Source
    from .store import Store

VERSION = 1
RECENT_DEFAULT_S = 3600  # a next step older than this is not "recent"


def _next_action(store: Store, sid: str, recent_s: int, now: float) -> str:
    """``"next_step"`` / ``"capture"`` when one is recent enough, else ``""``."""
    from . import handoff_facts

    session = store.get(sid)
    if session is not None and (session.next_step or "").strip():
        answered = (session.last_response_at or 0) / 1000
        if answered and now - answered <= recent_s:
            return "next_step"
    try:
        mtime = handoff_facts.capture_path(sid).stat().st_mtime
    except (OSError, handoff_facts.FactsError):
        return ""
    if now - mtime <= recent_s and handoff_facts.read_capture(sid) is not None:
        return "capture"
    return ""


def _providers() -> tuple[list[str], bool]:
    """``(names, ok)``: ok iff the entries are all valid and one of them is required."""
    from . import config, handoff_facts

    specs, bad = handoff_facts.parse_specs(config.load_config().handoff_facts)
    ok = not bad and any(not spec.optional for spec in specs)
    return [spec.name for spec in specs], ok


def _agents(adapter: ClaudeAdapter, store: Store, src: Source) -> list[dict[str, str]]:
    """Every continuing (running) subagent and its capsule path ("" = none)."""
    from . import continue_session, inflight, relay_state

    inv = continue_session.peek_inventory(adapter, store, src)
    out: list[dict[str, str]] = []
    for item in inv.items:
        if item.kind == inflight.KIND_SUBAGENT:
            relaunch = relay_state.relaunch_for(src.session_id, item.id)
            capsule = relaunch.capsule_path if relaunch is not None and relaunch.capsule else ""
            out.append({"id": item.id, "capsule": capsule})
        elif item.id == "subagents:counter":  # started, not in the transcript yet
            out.append({"id": item.id, "capsule": ""})
    return out


def check(
    adapter: ClaudeAdapter,
    store: Store,
    src: Source,
    *,
    recent_s: int = RECENT_DEFAULT_S,
    now: float | None = None,
) -> dict[str, Any]:
    """The lean-check verdict of *src* (read-only; the module docstring has the shape)."""
    now = time.time() if now is None else now
    session = store.get(src.session_id)
    aim = bool(session is not None and (session.aim or "").strip())
    next_action = _next_action(store, src.session_id, recent_s, now)
    names, providers_ok = _providers()
    agents = _agents(adapter, store, src)
    missing = [] if aim else ["aim"]
    if not next_action:
        missing.append("next_action")
    if not providers_ok:
        missing.append("facts_providers")
    missing += [f"capsule:{a['id']}" for a in agents if not a["capsule"]]
    return {
        "version": VERSION,
        "session_id": src.session_id,
        "ready": not missing,
        "missing": missing,
        "aim": aim,
        "next_action": next_action,
        "providers": names,
        "agents": agents,
    }


def cmd_lean_check(args: Any) -> int:
    """``continue-session lean-check``: print the verdict; exit 0 ready, 1 not."""
    from . import continue_session

    adapter, store, src = continue_session._open_source(args)
    with store:
        if src is None:
            data: dict[str, Any] = {
                "version": VERSION,
                "session_id": getattr(args, "session", None) or "",
                "ready": False,
                "missing": ["session"],
            }
        else:
            data = check(adapter, store, src, recent_s=args.recent)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    elif data["ready"]:
        print(f"ready: {data['session_id']}")
    else:
        print(f"not ready: missing {', '.join(data['missing'])}")
    return 0 if data["ready"] else 1


def add_parser(verbs: Any) -> None:
    """Register ``lean-check`` on the ``continue-session`` verb subparsers."""
    import argparse

    parser = verbs.add_parser(
        "lean-check",
        help="read-only: may this session relay by code (facts only, no handoff turn)?",
        description=(
            "Ready only with an AIM, a recent next action or checkpoint, a required "
            "facts provider and a capsule per running subagent. Writes nothing. "
            "Exit 0 ready, 1 not ready."
        ),
        epilog=(
            "example:\n"
            "  ccc continue-session lean-check -s <sid> -j\n"
            '  {"version": 1, "session_id": "<sid>", "ready": false, '
            '"missing": ["capsule:a1b2"], "aim": true,\n'
            '   "next_action": "next_step", "providers": ["tp"], '
            '"agents": [{"id": "a1b2", "capsule": ""}]}'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--session", help="the session id (default: this session)")
    parser.add_argument("-j", "--json", action="store_true", help="one-line JSON output")
    parser.add_argument(
        "-r",
        "--recent",
        type=int,
        default=RECENT_DEFAULT_S,
        metavar="SECONDS",
        help=f"how old a next step or capture may be (default {RECENT_DEFAULT_S})",
    )
