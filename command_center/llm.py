#!/usr/bin/env python3
"""Best-effort routed LLM calls for ccc's derived text and judgements.

Every call goes through ``llm_custom_command`` (normally
``ai prompt -R judge -p <purpose>``). It NEVER raises, and it never chooses a provider:
an unset or failed router returns ``None`` so the caller follows its existing graceful
failure path.

Recursion guard: the subprocess runs with ``CCC_INTERNAL=1``, which marks it
ccc-internal (detached ccc spawns are suppressed, ``switch-account`` refuses), and
``AI_NO_AUTOCOMMIT=1`` (so the auto-commit Stop hook doesn't fire). What keeps such a
helper ``claude -p`` run from creating a session row is the hook-side guard
``hooks._is_headless()`` (``CLAUDE_CODE_ENTRYPOINT`` starting with ``sdk``), not this
marker.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import logging
import os
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)

# Bounded wall-clock for the router. Deliberately ABOVE ai.py's inner
# DEFAULT_PROMPT_TIMEOUT (150s): ai.py must time out first so it can log + classify
# the failure in `ai logs`; an outer kill at the same instant would SIGKILL it
# mid-log and the row would vanish.
_ROUTER_TIMEOUT_SEC = 180

_PROMPT = """You are summarizing a parked AI coding session for a dashboard.
Session goal (done when): {aim}

Recent transcript (oldest first, most recent last):
{tail}

Reply with STRICT minified JSON and nothing else:
{{"summary":"<= one sentence on the current state","next_step":"1-3 short lines, \
each starting with '- ', the concrete next actions toward the goal"}}"""


def concise_note(text: str | None, limit: int = 160) -> str:
    """Collapse *text* to one <=limit-char line for the ``CCC_LLM_NOTE`` label.

    The note rides into every headless LLM subprocess's environment so an external
    router can log which session/goal a call served; it must be a single short line,
    bounded length). Returns ``""`` for falsy input.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _block_text(content: object) -> str:
    """Flatten a Claude message ``content`` (str, or list of typed blocks) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            parts.append(f"[tool:{block.get('name', '')}]")
        # thinking / tool_result blocks are intentionally skipped (noise / bulk)
    return " ".join(p for p in parts if p)


def _lines_to_turns(lines: list[str]) -> list[str]:
    """Parse transcript JSONL *lines* to ``[role] text`` snippets (user/assistant only)."""
    out: list[str] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role", rec["type"])
        snippet = _block_text(message.get("content")).strip()
        if snippet:
            out.append(f"[{role}] {snippet[:400]}")
    return out


def _read_transcript_tail(path: Path, max_chars: int = 6000) -> str:
    """Extract a compact, human-readable tail of a session transcript JSONL.

    Claude Code transcript records carry text under ``message.content``: a plain
    string for user turns, a list of typed blocks for assistant turns.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(_lines_to_turns(text.splitlines()[-200:]))[-max_chars:]


def read_transcript_delta(path: Path, offset: int, max_chars: int = 8000) -> tuple[str, int]:
    """Return ``(delta_text, new_offset)`` for transcript content added since *offset*.

    Reads from byte *offset* to end-of-file (transcripts are append-only JSONL),
    keeps only user / final-assistant text (no tool_use/tool_result/thinking),
    and advances the offset to the current file size so the next pass reads only
    what is genuinely new. A truncated/rotated file (size < offset) resets to 0.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ("", offset)
    start = 0 if size < offset else offset
    try:
        with path.open("rb") as handle:
            mid_line = False
            if start > 0:
                handle.seek(start - 1)
                # If the byte before `start` isn't a newline we landed mid-line.
                mid_line = handle.read(1) != b"\n"
            else:
                handle.seek(0)
            raw = handle.read()
    except OSError:
        return ("", offset)
    text = raw.decode("utf-8", errors="replace")
    # Drop a leading partial line only if we actually seeked into the middle of one.
    if mid_line and "\n" in text:
        text = text.split("\n", 1)[1]
    delta = "\n".join(_lines_to_turns(text.splitlines()))[-max_chars:]
    return (delta, size)


def summarize(
    aim: str | None, transcript_path: Path | None, model: str, *, note: str = ""
) -> tuple[str | None, str | None]:
    """Return ``(summary, next_step)`` for a session, or ``(None, None)`` on failure.

    *note* is the session's first/original AIM (already made concise) — exported as
    ``CCC_LLM_NOTE`` so an external router can log which session the call served.
    """
    tail = _read_transcript_tail(transcript_path) if transcript_path else ""
    if not tail:
        return (None, None)
    raw = _dispatch(
        _PROMPT.format(aim=aim or "(none set)", tail=tail),
        model,
        purpose="summary-nextstep",
        note=note,
    )
    return _parse(raw) if raw else (None, None)


def run_model(prompt: str, model: str, *, purpose: str = "", note: str = "") -> str | None:
    """Public, never-raising headless LLM call. Returns stdout or ``None``.

    Shared by :mod:`command_center.autoprogress` so it inherits the same recursion
    guard (``CCC_INTERNAL=1``), no-autocommit guard, and timeout as summaries.

    *purpose* is the per-action label (``aim-score`` / ``aim-met`` / ``subgoal-drift`` /
    ``subgoal-derive`` / ``subgoal-grade`` / ``summary-nextstep`` / ``short-aim``) and
    *note* is the session's first AIM — both are exported to the subprocess env as
    ``CCC_LLM_PURPOSE`` / ``CCC_LLM_NOTE`` so a configured ``llm_custom_command``
    router can log or route each call per action. They are metadata only: they never
    change what is generated.
    """
    return _dispatch(prompt, model, purpose=purpose, note=note)


def _dispatch(prompt: str, model: str, purpose: str = "", note: str = "") -> str | None:
    """Route one headless call through ``llm_custom_command`` only.

    ``model`` remains in this internal compatibility seam for callers compiled against
    older ccc versions, but is deliberately ignored: provider/model selection belongs
    to the external purpose ladder. An unset command, non-zero exit, timeout, or empty
    answer is a visible warning and a failed call; ccc never falls back to a provider.
    """
    del model
    from .config import load_config  # lazy: keep module import light

    command = load_config().llm_custom_command.strip()
    label = purpose or "unspecified"
    if not command:
        _LOG.warning("LLM router unavailable for purpose %s: llm_custom_command is empty", label)
        return None
    out = run_custom(prompt, command, purpose=purpose, note=note)
    if out is None:
        _LOG.warning("LLM router failed for purpose %s: %s", label, command)
    return out


def _guarded_env(purpose: str = "", note: str = "") -> dict[str, str]:
    """Env for a headless helper subprocess (the same guards the claude/codex runners set).

    ``CCC_INTERNAL=1`` marks the subprocess ccc-internal — detached ccc spawns are
    suppressed and ``switch-account`` refuses inside it, so nothing recurses; the guard
    that keeps its hooks from creating a session row is ``hooks._is_headless()``
    (``CLAUDE_CODE_ENTRYPOINT`` starting with ``sdk``). ``AI_NO_AUTOCOMMIT=1`` so the
    auto-commit Stop hook does not fire. A non-empty
    *purpose* / *note* is exported as ``CCC_LLM_PURPOSE`` / ``CCC_LLM_NOTE`` so an
    external command can log or route each call per action.
    """
    env = dict(os.environ)
    env["CCC_INTERNAL"] = "1"
    env["AI_NO_AUTOCOMMIT"] = "1"
    if purpose:
        env["CCC_LLM_PURPOSE"] = purpose
    if note:
        env["CCC_LLM_NOTE"] = note
    return env


def run_custom(
    prompt: str,
    command: str,
    timeout: int = _ROUTER_TIMEOUT_SEC,
    *,
    purpose: str = "",
    note: str = "",
) -> str | None:
    """User escape-hatch backend: run *command* via the shell, *prompt* on stdin.

    The command must print the model's raw text response on stdout; a non-zero exit (or an
    empty *command*) fails the call. This lets a user route the call through their own
    multi-provider router without ccc depending on any private tool.
    *purpose* / *note* ride into the command's env as ``CCC_LLM_PURPOSE`` /
    ``CCC_LLM_NOTE`` — per-action metadata the router can log or route on. Never
    raises. ``shell=True`` is intentional — the command is the user's own configured hook.
    """
    if not command.strip():
        return None
    try:
        result = subprocess.run(
            command,
            shell=True,  # noqa: S602  # deliberate: the command is the user's own escape hatch
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_guarded_env(purpose, note),
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _parse(raw: str) -> tuple[str | None, str | None]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return (None, None)
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return (None, None)
    summary = data.get("summary")
    next_step = data.get("next_step")
    return (
        summary if isinstance(summary, str) and summary.strip() else None,
        next_step if isinstance(next_step, str) and next_step.strip() else None,
    )
