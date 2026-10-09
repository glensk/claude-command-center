#!/usr/bin/env python3
"""``ccc inspect -s ID -j`` — what a session last said and the decision it needs.

data: ``{state, live, last_reply (≤ 1500, tail kept), last_prompt (≤ 500, head kept),
decision | null}``. The transcript parse is :mod:`command_center.adapters.claude_bridge`
(the S-INSPECT reference parser); the live registry status overrides the transcript's
own state when the session is running. ``decision`` is the pending AskUserQuestion
(source ``ask_user_question``) or, only while idle, the ``You [decision]:`` lines of the
last reply's ``## To-do list`` (source ``todo_line``); its ``context`` carries the
session's AIM, a ticket id found in the AIM, and ``recent_summary``.

``recent_summary`` comes from the ccc LLM purpose ``voice-brief`` (through
``llm_custom_command`` only, :data:`BRIEF_TIMEOUT_SEC` budget), cached per transcript
(inode + size) in ``voice-brief-cache.json`` under ccc's state dir; with no router, a
failed call or ``--no-llm`` it falls back to the first three sentences of the last reply.
A transcript that does not parse is ``ok: false, code: transcript_unknown``.
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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters import claude_bridge as cb
from .adapters.claude_agents import contract_status
from .bridge_json import BridgeError
from .bridge_send import inspect_transcript
from .bridge_target import find_live

if TYPE_CHECKING:
    from .bridge_target import BridgeDeps

BRIEF_TIMEOUT_SEC = 10.0
BRIEF_CACHE_MAX = 200
LIVE_STATES = frozenset({"busy", "idle", "waiting", "blocked"})
TICKET_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{0,15}#\d+\b")
_BRIEF_REPLY_MAX = 4000

_BRIEF_PROMPT = """You brief a person, by voice, on an AI coding session that needs a decision.
Session goal: {aim}

The session's latest reply (may be cut):
{reply}

The decision it needs:
{questions}

In at most three short spoken sentences say what the session is building, why, and \
what it is waiting for. Plain text only: no markdown, no lists, no quotes."""


def _cache_path(deps: BridgeDeps) -> Path:
    return deps.lock_dir().parent / "voice-brief-cache.json"


def _cache_read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_write(path: Path, data: dict[str, Any]) -> None:
    if len(data) > BRIEF_CACHE_MAX:
        for key in sorted(data, key=lambda k: data[k].get("at", 0))[: len(data) - BRIEF_CACHE_MAX]:
            data.pop(key, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def _clean_summary(text: str) -> str:
    flat = " ".join(text.replace("*", "").replace("#", "").split())
    return cb.cap_head(flat, cb.SUMMARY_MAX)


def recent_summary(  # pylint: disable=too-many-positional-arguments
    deps: BridgeDeps,
    session_id: str,
    transcript: Path,
    ins: cb.Inspection,
    aim: str,
    use_llm: bool,
) -> str:
    """The ``voice-brief`` summary (cached per transcript size), else the fallback."""
    fallback = cb.recent_summary(ins.last_reply)
    if not use_llm:
        return fallback
    try:
        st = transcript.stat()
    except OSError:
        return fallback
    key = f"{st.st_ino}:{st.st_size}"
    cache_path = _cache_path(deps)
    cache = _cache_read(cache_path)
    hit = cache.get(session_id)
    if isinstance(hit, dict) and hit.get("key") == key and isinstance(hit.get("summary"), str):
        return str(hit["summary"])
    questions = "\n".join(
        f"- {q.text} Options: " + "; ".join(str(o["label"]) for o in q.options)
        for q in ins.questions
    )
    prompt = _BRIEF_PROMPT.format(
        aim=aim or "(not set)",
        reply=cb.cap_tail(ins.last_reply, _BRIEF_REPLY_MAX),
        questions=questions or "(none)",
    )
    try:
        out = deps.llm(prompt, aim[:160], BRIEF_TIMEOUT_SEC)
    except Exception:  # pylint: disable=broad-exception-caught  # the fallback always works
        out = None
    summary = _clean_summary(out or "")
    if not summary:
        return fallback
    cache[session_id] = {"key": key, "summary": summary, "at": deps.now_ms()}
    _cache_write(cache_path, cache)
    return summary


def run_inspect(deps: BridgeDeps, session_id: str, *, use_llm: bool = True) -> dict[str, Any]:
    """The ``ccc inspect`` data object (raises :class:`BridgeError`)."""
    live = None
    try:
        live = find_live(deps, session_id)
    except Exception:  # pylint: disable=broad-exception-caught  # registry is optional here
        live = None
    with deps.store() as store:
        row = store.get(session_id)
    if live is None and row is None:
        raise BridgeError("unknown_session", f"session {session_id} is not known to ccc")
    cwd = (live.cwd if live else "") or (row.cwd if row else "")
    config_dir = (live.config_dir if live else "") or (row.config_dir if row else "")
    path = deps.transcript(cwd, session_id, config_dir)
    if path is None or not path.is_file():
        raise BridgeError("transcript_missing", f"no transcript found for session {session_id}")
    ins = inspect_transcript(path)
    alive = bool(live and live.alive)
    # the registry's word mapped to §6 (``shell`` — a shell tool running — is ``busy``)
    raw = contract_status(live.raw_status) if live is not None and alive else ""
    state = raw if raw in LIVE_STATES else ins.transcript_state
    aim = (row.aim or "").strip() if row is not None and row.aim else ""
    decision = ins.decision(state, "")
    if decision is not None:
        decision["context"] = {
            "aim": aim or None,
            "ticket": (m.group(0) if (m := TICKET_RE.search(aim)) else None),
            "recent_summary": recent_summary(deps, session_id, path, ins, aim, use_llm),
        }
    return {
        "state": state,
        "live": alive,
        "last_reply": cb.cap_tail(ins.last_reply, cb.LAST_REPLY_MAX),
        "last_prompt": cb.cap_head(ins.last_prompt, cb.LAST_PROMPT_MAX),
        "decision": decision,
    }
