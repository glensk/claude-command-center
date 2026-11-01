#!/usr/bin/env python3
"""The capped-seat path of the handoff commands (tp#1300).

A seat out of quota cannot run a model turn, so ``/cwork-to-cpriv`` & co. cannot have the
model write its handoff there. The ``UserPromptSubmit`` intercept
(:mod:`command_center.switch_intercept`) recognises the four handoff names
(:data:`HANDOFF_COMMANDS`) and calls :func:`intercept`:

* the seat is NOT capped → ``False``: the prompt reaches the model, which runs the
  ``.md`` procedure (inventory, drain, template, ``continue-session start``);
* capped and the in-flight detector is not clear → the prompt is blocked with a refusal
  that names the ``*-cache-write-hit`` command (no model can checkpoint agents);
* capped and clear → a detached ``ccc continue-session start --capped -A LABEL -s SID``
  and a one-line status.

``start --capped`` renders the recorded facts first, builds a BOUNDED digest (≤ 8 KB: the
facts, the last to-do list, the last main-chain texts) and asks ``llm_custom_command``
(``CCC_LLM_PURPOSE=handoff-summary`` — ccc's one LLM route; ``ccc llm-routing`` lists the
purpose) for ONLY a JSON supplement ``{current_state, decisions, first_steps}`` (D11). ccc
renders it as the three model sections and runs the same compose / scrub / snapshot path.
A router that is unset, fails, times out or answers anything else falls back to the
facts-only relay (``start --facts-only``, D10: the three sections written by code), with a
line in ``events.log``; nothing refuses. ``CCC_HANDOFF_ASSUME_CAPPED=1`` forces the capped
verdict (canaries never manufacture a quota exhaustion).

A BLOCKED quota reading alone is not a cap (tp#1327): a seat whose snapshot reads a full
window but still runs turns (extra usage) is not capped. :func:`is_capped` counts it capped
only while no turn on that seat succeeded since the reading began.
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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter
    from .continue_session import Source
    from .quota import ProviderQuota

# Handoff command → the account label it hands off TO (the account named last).
HANDOFF_COMMANDS = {
    "/cwork-to-cpriv": "private",
    "/cpriv-to-cwork": "work",
    "/cwork-continue-in-new-session": "work",
    "/cpriv-continue-in-new-session": "private",
}
# Target label → the same-session switch that still works on a capped seat.
CACHE_WRITE_HIT = {
    "private": "/cwork-to-cpriv-cache-write-hit",
    "work": "/cpriv-to-cwork-cache-write-hit",
}
ASSUME_CAPPED_ENV = "CCC_HANDOFF_ASSUME_CAPPED"
SUMMARY_PURPOSE = "handoff-summary"
SUMMARY_TIMEOUT_SEC = 180
DIGEST_MAX_BYTES = 8 * 1024  # the whole digest the router sees (D11)
SUPPLEMENT_KEYS = ("current_state", "decisions", "first_steps")
SUPPLEMENT_MAX_BYTES = 3 * 1024  # each router supplement field (D11)
STATE_MAX_BYTES = 4 * 1024  # the facts-only "Current state" copies (D10)
EMERGENCY_BANNER = "EMERGENCY RELAY — no model-written part"
ROUTER_BANNER = "CAPPED RELAY — written by the configured router from a digest, not by the session"
FIXED_FIRST_STEPS = """\
1. Run the `ccc continue-session accept` command named in the handoff preamble.
2. Re-claim the tp items listed under "tp ownership" before touching them.
3. Read "Subagent continuations" and "Background restarts": restart only what is declared
   restartable, decide on the rest (see "Decide before restarting").
4. Read "Open items and questions", then continue from the AIM above."""
_TODO_MARK = {"completed": "x", "in_progress": "~"}
_SUPPLEMENT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SUPPLEMENT_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}(?:\s|$)")
_MESSAGE_MAX_CHARS = 4_000  # one transcript message inside it
_TAIL_BYTES = 4 * 1024 * 1024


def handoff_command(prompt: str) -> str | None:
    """The target label when the WHOLE first token of *prompt* is a handoff command."""
    head = prompt.strip().split(maxsplit=1)
    return HANDOFF_COMMANDS.get(head[0]) if head else None


def cache_write_hit_for(label: str, source_label: str) -> str:
    """The same-session switch command to suggest: towards *label*, else away from source."""
    if label != source_label and label in CACHE_WRITE_HIT:
        return CACHE_WRITE_HIT[label]
    other = next((lab for lab in CACHE_WRITE_HIT if lab != source_label), label)
    return CACHE_WRITE_HIT.get(other, "/cwork-to-cpriv-cache-write-hit")


def _record_ms(record: dict[str, Any]) -> int:
    """Epoch-ms of a transcript record's own ``timestamp`` (0 when absent/unparseable)."""
    from .limitswitch import _record_ms as record_ms  # one timestamp parser

    return record_ms(record)


def _turn_outcomes(adapter: ClaudeAdapter, path: Path | None) -> tuple[int, int]:
    """``(newest successful turn, newest usage-limit halt)`` in one transcript, epoch-ms.

    A successful turn is a main-chain assistant record the API produced: no
    ``isApiErrorMessage`` flag and not Claude Code's ``<synthetic>`` filler (written on an
    interrupt, never a model call). A halt is an API error that reads "hit your … limit".
    """
    from .adapters.claude import _RATE_LIMIT_RE, _assistant_text

    if path is None:
        return 0, 0
    ok = halt = 0
    for record in adapter._tail_records(path):  # pylint: disable=protected-access
        if record.get("isSidechain") or record.get("type") != "assistant":
            continue
        stamp = _record_ms(record)
        message = record.get("message")
        if record.get("isApiErrorMessage"):
            if _RATE_LIMIT_RE.search(_assistant_text(record)):
                halt = max(halt, stamp)
        elif not (isinstance(message, dict) and message.get("model") == "<synthetic>"):
            ok = max(ok, stamp)
    return ok, halt


def seat_turns(adapter: ClaudeAdapter, src: Source) -> tuple[int, int]:
    """``(newest successful turn, newest usage-limit halt)`` on *src*'s seat, epoch-ms.

    Read from the source transcript and every live session on the same account — the
    transcripts ccc already reads; ``projects/`` is shared between the config dirs, so
    the account comes from each live session's own config dir, never from the path.
    """
    from . import accounts

    paths: dict[str, Path | None] = {src.session_id: src.transcript}
    for live in adapter.discover():
        if not live.alive or live.session_id in paths or not live.config_dir:
            continue
        if accounts.account_label(live.config_dir) != src.label:
            continue
        paths[live.session_id] = adapter.transcript_path(live.cwd, live.session_id, live.config_dir)
    ok = halt = 0
    for path in paths.values():
        one_ok, one_halt = _turn_outcomes(adapter, path)
        ok, halt = max(ok, one_ok), max(halt, one_halt)
    return ok, halt


def _blocked_since(label: str, state: ProviderQuota) -> int:
    """Epoch seconds from which the BLOCKED reading is known to hold (0 = unknown).

    A window verdict dates from the first capture that read that window full
    (:func:`usage.full_since`); ``captured_at`` would not do, the status line bumps it
    every few seconds. A cooldown entry dates from the refusal it observed.
    """
    from . import usage

    if state.source == "windows":
        return usage.full_since(label, state.blocked_by)
    return int(state.captured_at or 0)


def is_capped(adapter: ClaudeAdapter, src: Source) -> tuple[bool, str]:
    """``(capped, why)`` for the source seat (tp#1327).

    Capped = forced (:data:`ASSUME_CAPPED_ENV`), this session's last turn hit the usage
    limit, or the quota snapshot reads BLOCKED and no turn on that seat succeeded since
    the reading began (:func:`_blocked_since`) — a halt newer than that success counts
    again. A BLOCKED reading whose start is unknown stays capped.
    """
    if os.environ.get(ASSUME_CAPPED_ENV) == "1":
        return True, f"{ASSUME_CAPPED_ENV}=1"
    from . import limitswitch, quota

    obs = limitswitch.observe(
        adapter, src.session_id, src.cwd, src.config_dir, transcript_path=src.transcript
    )
    if obs is not None and obs.usage_limit:
        return True, "this session's last turn hit the account's usage limit"
    if not src.label:
        return False, ""
    state = quota.provider_state(f"claude:{src.label}")
    if state is None or state.state != quota.BLOCKED:
        return False, ""
    blocked = f"the {src.label} seat is blocked ({state.reason or 'quota'})"
    since = _blocked_since(src.label, state)
    ok_ms, halt_ms = seat_turns(adapter, src)
    if since and ok_ms > since * 1000 and ok_ms > halt_ms:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ok_ms / 1000))
        return False, f"{blocked}, but a turn succeeded on it at {when}"
    return True, blocked


def log_event(session_id: str, detail: str) -> None:
    """One ``handoff`` line in ``app_home()/events.log`` (never raises)."""
    from . import config

    try:
        path = config.app_home() / "events.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{session_id}\thandoff\t{detail}\n")
    except OSError:
        pass


def notify_failure(message: str) -> None:
    """Best-effort desktop notification for a capped handoff that did not start."""
    try:
        from . import config, notify

        notify.notify("ccc continue-session", message, config.load_config().notify)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass


def _block(reason: str) -> None:
    import sys

    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.stdout.write("\n")


def intercept(sid: str, prompt: str) -> bool:  # pylint: disable=too-many-return-statements
    """The UserPromptSubmit side: ``False`` unless the seat is capped (see module doc)."""
    label = handoff_command(prompt)
    if label is None or not sid:
        return False
    from . import continue_session, spawn
    from .store import Store

    adapter = continue_session._adapter()  # pylint: disable=protected-access
    with Store() as store:
        src = continue_session.resolve_source(adapter, store, sid)
        if src is None:
            return False
        capped, why = is_capped(adapter, src)
        if not capped:
            return False  # the model runs the .md procedure
        fallback = cache_write_hit_for(label, src.label)
        if label == src.label:
            _block(
                f"handoff refused: this {src.label} seat is capped ({why}) and the new "
                f"session would run on it too — use {fallback} to move to the other seat"
            )
            return True
        inv = continue_session.inventory(adapter, store, src)
    if not inv.clear:
        _block(
            f"handoff refused: this seat is capped ({why}) and work is still in flight "
            f"({'; '.join(inv.reasons)}) — no model can checkpoint it. Use {fallback} "
            "(same session, full cache write) or wait for the work to finish."
        )
        return True
    os.environ["CLAUDE_CODE_SESSION_ID"] = sid  # the detached start runs AS the source
    args = ["continue-session", "start", "--capped", "-A", label, "-s", sid]
    if not spawn.spawn_ccc(args, internal=False):
        _block(f"handoff could not start (ccc not on PATH) — use {fallback}")
        return True
    log_event(sid, f"capped handoff to {label} started ({why})")
    _block(
        f"capped seat ({why}): the handoff summary is being written by the configured "
        f"router; a NEW session opens on {label} when it is ready "
        "(`ccc continue-session status`). If nothing opens: " + fallback
    )
    return True


def _message_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def recent_messages(transcript: Path | None, limit: int = 40) -> list[tuple[str, str]]:
    """The last *limit* main-chain ``(role, text)`` pairs (tool traffic left out)."""
    if transcript is None:
        return []
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in raw.split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        role = record.get("type")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(record).strip()
        if text and not text.startswith(("<task-notification>", "<command-", "<local-command")):
            out.append((str(role), text[:_MESSAGE_MAX_CHARS]))
    return out[-limit:]


def last_todos(transcript: Path | None) -> list[str]:
    """The newest ``TodoWrite`` list of the main chain as ``[x] / [~] / [ ] subject`` lines."""
    if transcript is None:
        return []
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    found: list[str] = []
    for line in raw.split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or record.get("isSidechain"):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("name") == "TodoWrite"):
                continue
            todos = (block.get("input") or {}).get("todos")
            if isinstance(todos, list):
                found = [
                    f"[{_TODO_MARK.get(str(t.get('status')), ' ')}] {t.get('content') or ''}"
                    for t in todos
                    if isinstance(t, dict)
                ]
    return found


def _cut_bytes(text: str, limit: int) -> str:
    """*text* cut to at most *limit* UTF-8 bytes (a split character is dropped)."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _fact_lines(snap_doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for fact in snap_doc.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        fields = json.dumps(fact.get("fields"), ensure_ascii=False, sort_keys=True)
        out.append(f"- {fact.get('kind')} {fact.get('key')}: {fields[:300]}")
    return out


def digest(snap_doc: dict[str, Any], transcript: Path | None) -> str:
    """The bounded input the router summarizes (≤ :data:`DIGEST_MAX_BYTES` bytes)."""
    facts = _cut_bytes("\n".join(_fact_lines(snap_doc)), DIGEST_MAX_BYTES // 2)
    parts = [f"## Recorded facts\n{facts}"]
    todos = last_todos(transcript)
    if todos:
        parts.append("## Last to-do list\n" + "\n".join(todos))
    parts.append("## Recent messages")
    text = "\n\n".join(parts)
    budget = DIGEST_MAX_BYTES - len(text.encode("utf-8")) - 4
    tail: list[str] = []
    for role, msg in reversed(recent_messages(transcript)):
        entry = f"[{role}] {msg}"
        size = len(entry.encode("utf-8")) + 2
        if size > budget:
            break
        tail.append(entry)
        budget -= size
    return _cut_bytes("\n\n".join([text, *reversed(tail)]), DIGEST_MAX_BYTES)


def summary_prompt(digest_text: str) -> str:
    """The router prompt: a JSON model supplement, nothing else (D11)."""
    return (
        "You help a NEW coding-agent session take over the work of an old session whose "
        "account is out of quota. From the digest below write ONLY a JSON object with exactly "
        'the string keys "current_state", "decisions" and "first_steps" (what is done and half '
        "done; decisions and constraints that apply; the exact next actions). Each value is at "
        f"most {SUPPLEMENT_MAX_BYTES} bytes of plain text: no markdown headings (no line "
        "starting with #), no control characters, no other keys, nothing before or after the "
        "JSON. Be concrete (ids, paths, commands); never include credential values.\n\n"
        f"# Session digest\n\n{digest_text}\n"
    )


def parse_supplement(  # pylint: disable=too-many-return-statements
    text: str | None,
) -> tuple[dict[str, str] | None, str]:
    """``(supplement, "")`` for a valid router answer, else ``(None, why)`` (D11)."""
    if not text or not text.strip():
        return None, "the router returned nothing"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "the router answer is not JSON"
    if not isinstance(data, dict):
        return None, "the router answer is not a JSON object"
    if set(data) != set(SUPPLEMENT_KEYS):
        return None, "the router answer has missing or unknown keys"
    out: dict[str, str] = {}
    for key in SUPPLEMENT_KEYS:
        value = data[key]
        if not isinstance(value, str):
            return None, f"{key} is not a string"
        if len(value.encode("utf-8")) > SUPPLEMENT_MAX_BYTES:
            return None, f"{key} is over {SUPPLEMENT_MAX_BYTES} bytes"
        if _SUPPLEMENT_CONTROL_RE.search(value):
            return None, f"{key} holds control characters"
        if _SUPPLEMENT_HEADING_RE.search(value):
            return None, f"{key} holds a markdown heading"
        out[key] = value
    return out, ""


def _transcript_line(transcript: Path | None) -> str:
    if transcript is None:
        return "Predecessor transcript: (none found)"
    return f"Predecessor transcript (evidence only): {transcript}"


def _facts_only_state(snap_doc: dict[str, Any], transcript: Path | None) -> str:
    """D10: banner + last to-do list + the last 3 assistant texts (≤ 4 KB), each escaped."""
    from .handoff_doc import escape_headings

    todos = [t for t in snap_doc.get("facts") or [] if t.get("kind") == "todo_list"]
    todo_lines = [str(todos[-1]["fields"].get("text") or "")] if todos else last_todos(transcript)
    parts = []
    if any(t.strip() for t in todo_lines):
        parts.append("Last to-do list:\n" + "\n".join(todo_lines))
    texts = [m for role, m in recent_messages(transcript) if role == "assistant"][-3:]
    for text in texts:
        parts.append(f"Assistant (verbatim):\n{text}")
    body = _cut_bytes(escape_headings("\n\n".join(parts) or "none recorded"), STATE_MAX_BYTES)
    return f"{EMERGENCY_BANNER}\n\n{body}\n\n{_transcript_line(transcript)}"


def _tp_decisions(snap_doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for fact in snap_doc.get("facts") or []:
        if fact.get("kind") != "tp_item":
            continue
        fields = fact.get("fields") or {}
        raw = fields.get("decisions") or fields.get("decision") or []
        for item in [raw] if isinstance(raw, str) else raw:
            if str(item).strip():
                out.append(f"- tp#{fields.get('tp_id')}: {item}")
    return out


def _facts_only(snap_doc: dict[str, Any], transcript: Path | None) -> str:
    from .handoff_doc import escape_headings

    decisions = _cut_bytes(
        escape_headings("\n".join(_tp_decisions(snap_doc)) or "none recorded"), SUPPLEMENT_MAX_BYTES
    )
    return (
        f"## Current state\n\n{_facts_only_state(snap_doc, transcript)}\n\n"
        f"## Decisions and constraints\n\n{decisions}\n\n"
        "Reasoning behind them: read the predecessor transcript.\n\n"
        f"## First steps\n\n{FIXED_FIRST_STEPS}\n"
    )


def _from_supplement(sup: dict[str, str], transcript: Path | None) -> str:
    from .handoff_doc import escape_headings

    def body(key: str) -> str:
        return escape_headings(sup[key].strip()) or "none"

    return (
        f"## Current state\n\n{ROUTER_BANNER}\n\n{body('current_state')}\n\n"
        f"{_transcript_line(transcript)}\n\n"
        f"## Decisions and constraints\n\n{body('decisions')}\n\n"
        f"## First steps\n\n{body('first_steps')}\n"
    )


def model_part(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sid: str,
    snap_doc: dict[str, Any],
    transcript: Path | None,
    scrubber: Any,
    *,
    use_router: bool,
) -> str:
    """The three-section model part written by code (D10) or by the router (D11).

    With *use_router* the router sees a scrubbed digest (≤ 8 KB) and may answer ONLY a JSON
    supplement; unset / failed / invalid / over-cap falls back to the facts-only text (a
    logged event, never a refusal). Every inserted text passes ``escape_headings`` (D16).
    """
    if use_router:
        answer = summarize(summary_prompt(scrubber.text(digest(snap_doc, transcript))))
        sup, why = parse_supplement(answer)
        if answer is None:
            why = "the router (llm_custom_command) is unset or failed"
        if sup is not None:
            return _from_supplement(sup, transcript)
        log_event(sid, f"capped handoff: {why}; relaying facts-only instead")
    return _facts_only(snap_doc, transcript)


def summarize(prompt: str) -> str | None:
    """The router's handoff text (``llm_custom_command``, purpose handoff-summary) or None."""
    from . import config, llm

    command = config.load_config().llm_custom_command
    if not command.strip():
        return None
    return llm.run_custom(prompt, command, SUMMARY_TIMEOUT_SEC, purpose=SUMMARY_PURPOSE)
