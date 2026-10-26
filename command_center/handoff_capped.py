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

``start --capped`` builds a BOUNDED digest (AIM, sub-goals, the last main-chain texts,
the ``state`` output) and gets the handoff from ``llm_custom_command`` with
``CCC_LLM_PURPOSE=handoff-summary`` — ccc's one LLM route; the router picks the provider
(``ccc llm-routing`` lists the purpose). A router that is unset, fails or times out is a
logged failure (``events.log``), a desktop notification and a pointer to the
``*-cache-write-hit`` command. The summary passes the same validation as a model-written
handoff and the same transfer path. ``CCC_HANDOFF_ASSUME_CAPPED=1`` forces the capped
verdict (canaries never manufacture a quota exhaustion).
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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter
    from .continue_session import Source

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
DIGEST_MAX_CHARS = 40_000  # the whole digest the router sees
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


def is_capped(adapter: ClaudeAdapter, src: Source) -> tuple[bool, str]:
    """``(capped, why)`` for the source seat: forced, a usage-limit halt, or BLOCKED quota."""
    if os.environ.get(ASSUME_CAPPED_ENV) == "1":
        return True, f"{ASSUME_CAPPED_ENV}=1"
    from . import limitswitch, quota

    obs = limitswitch.observe(
        adapter, src.session_id, src.cwd, src.config_dir, transcript_path=src.transcript
    )
    if obs is not None and obs.usage_limit:
        return True, "this session's last turn hit the account's usage limit"
    if src.label:
        state = quota.provider_state(f"claude:{src.label}")
        if state is not None and state.state == quota.BLOCKED:
            return True, f"the {src.label} seat is blocked ({state.reason or 'quota'})"
    return False, ""


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


def digest(state: dict[str, Any], transcript: Path | None) -> str:
    """The bounded input the router summarizes (≤ :data:`DIGEST_MAX_CHARS`)."""
    head = json.dumps(state, indent=1, ensure_ascii=False)[: DIGEST_MAX_CHARS // 4]
    parts = [f"## Session state (ccc continue-session state -j)\n{head}", "## Recent messages"]
    budget = DIGEST_MAX_CHARS - len(parts[0]) - 200
    tail: list[str] = []
    for role, text in reversed(recent_messages(transcript)):
        entry = f"[{role}] {text}"
        if len(entry) > budget:
            break
        tail.append(entry)
        budget -= len(entry)
    parts += list(reversed(tail))
    return "\n\n".join(parts)


def summary_prompt(digest_text: str, template: str) -> str:
    """The router prompt: fill the template from the digest, output the document only."""
    return (
        "You write a handoff document for a NEW coding-agent session that continues the "
        "work of an old session whose account is out of quota. Fill EVERY section of the "
        "template below from the session digest; write 'none' where nothing applies; be "
        "concrete (ids, paths, commands, next actions); never include credential values. "
        "Output ONLY the filled markdown document, nothing before or after it.\n\n"
        f"# Template\n\n{template}\n\n# Session digest\n\n{digest_text}\n"
    )


def summarize(prompt: str) -> str | None:
    """The router's handoff text (``llm_custom_command``, purpose handoff-summary) or None."""
    from . import config, llm

    command = config.load_config().llm_custom_command
    if not command.strip():
        return None
    return llm.run_custom(prompt, command, SUMMARY_TIMEOUT_SEC, purpose=SUMMARY_PURPOSE)
