#!/usr/bin/env python3
"""Full-session markdown rendering — the terminal-like conversation view.

The ONE canonical renderer behind both surfaces that show a session's full
conversation (what the human typed, what Claude replied, the tool calls
between): the vault **session mirror** files (:mod:`command_center.mirrors`,
``sessions_dir``) and the ``ccc peek`` panel's **session** tab. Both consume
:func:`session_segments`; the plain markdown (:func:`render_session`) is its
tag-free concatenation — the same pattern as ``peek.prompt_segments`` /
``format_prompts`` — so the file and the panel can never diverge.

Input is the normalized event stream from
:meth:`ClaudeAdapter.session_events` (``prompt`` / ``text`` / ``tool`` — the
adapter owns ALL transcript-schema knowledge; this module only formats).
Fidelity contract (user decision): prompts and assistant text verbatim, one
``⏺ Tool(input…)`` line per tool call with its result truncated to a few
``⎿``-prefixed lines — NO thinking blocks, NO full tool outputs.

Size contract: the rendered body is capped at :data:`SESSION_MAX_BYTES`
(UTF-8, trim note included) by dropping the OLDEST whole turns first, with a
``showing last N of M turns`` note; turn numbering keeps the original ``(N)``
indices (they align 1:1 with the prompts tab). If a single newest turn alone
exceeds the cap it is hard-truncated keeping its trailing bytes at a UTF-8-safe
boundary. Everything is deterministic — same events, same bytes — preserving
the mirrors' byte-stability contract.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SessionEvent

if TYPE_CHECKING:  # imported for annotations only — no runtime dependency
    from .adapters import ClaudeAdapter
    from .models import Session

# Styling tags consumed by the peek panel (the vault file uses the plain concat):
# the ``## (N) you`` / ``## claude`` header lines, ordinary body text, the newest
# prompt's body (rendered pronounced), and the dim ⏺/⎿ tool lines.
TAG_RULE = "rule"
TAG_TEXT = "text"
TAG_LAST = "last"
TAG_TOOL = "tool"

# Byte cap of the rendered body (UTF-8, including the trim note) — decision 3.
SESSION_MAX_BYTES = 512 * 1024
# One-line tool-call input summary cap (first line only).
_TOOL_INPUT_CHARS = 160
# Tool-result excerpt caps: at most this many lines / total characters.
_TOOL_RESULT_LINES = 3
_TOOL_RESULT_CHARS = 300

# Input keys tried (in order) for the ⏺ one-liner — the most telling field per tool
# (Bash → command, Edit/Write/Read → file_path, Grep/Glob → pattern, Agent/Task →
# description, …); the first non-empty string wins, else compact JSON.
_INPUT_SUMMARY_KEYS = (
    "command",
    "file_path",
    "pattern",
    "description",
    "prompt",
    "query",
    "url",
    "skill",
)

_EMPTY_BODY = "(no conversation in this session yet)"

# Render cache: cache key → (transcript mtime, segments). Same contract as
# ``mirrors._PROMPT_CACHE`` — a frozen DONE transcript is rendered once per
# process, a growing RUNNING one re-renders when its mtime moves. Byte-stable:
# the same mtime yields the same segments, hence the same bytes.
_RENDER_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}


# A line-leading run of 3+ backticks — what opens (or closes) a markdown code fence.
_FENCE_RE = re.compile(r"^(\s*)(`{3,})", re.MULTILINE)


def escape_fences(text: str) -> str:
    """Backslash-escape line-leading code-fence runs in embedded verbatim text.

    A pasted terminal snippet in a USER PROMPT often opens a fence it never closes
    at line start (its "closing" backticks sit mid-line, which is not a fence) — and
    an unclosed fence swallows EVERYTHING below it in the rendered file (headings,
    the ``full session`` wikilink, …). ``\\```` renders as the literal backticks, so
    the paste stays readable and the document structure downstream survives. Shared
    by the session render (``## (N) you`` bodies) and the running/done mirrors'
    ``## Prompts`` list.
    """
    return _FENCE_RE.sub(r"\1\\\2", text)


def _balance_fences(text: str) -> str:
    """Append a closing fence when an ASSISTANT text block leaves one dangling.

    Claude's own replies carry intentional, normally BALANCED code fences — those
    must keep rendering as code (unlike prompt pastes, which are escaped). An odd
    number of line-leading fence runs (a truncated reply) would swallow the rest of
    the file; close it with a matching-length fence instead.
    """
    runs = [match.group(2) for match in _FENCE_RE.finditer(text)]
    if len(runs) % 2 == 0:
        return text
    return text + "\n" + "`" * max(len(run) for run in runs)


def _one_line(text: str, cap: int) -> str:
    """First line of *text*, whitespace-collapsed, hard-capped at *cap* chars."""
    line = " ".join(text.strip().split("\n", 1)[0].split())
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


def _summarize_input(tool_input: dict) -> str:
    """The one-line input summary for a ⏺ tool-call line (may be empty)."""
    for key in _INPUT_SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value, _TOOL_INPUT_CHARS)
    if not tool_input:
        return ""
    try:
        return _one_line(json.dumps(tool_input, ensure_ascii=False), _TOOL_INPUT_CHARS)
    except (TypeError, ValueError):
        return ""


def _result_lines(result: str) -> list[str]:
    """The truncated ⎿ excerpt of a tool result (≤ lines/chars caps, ``…`` on trim)."""
    text = result.strip()
    if not text:
        return []
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    trimmed = len(lines) > _TOOL_RESULT_LINES
    lines = lines[:_TOOL_RESULT_LINES]
    out: list[str] = []
    budget = _TOOL_RESULT_CHARS
    for line in lines:
        if budget <= 0:
            trimmed = True
            break
        if len(line) > budget:
            line = line[: max(1, budget - 1)].rstrip() + "…"
            trimmed = True
        out.append(line)
        budget -= len(line)
    if trimmed and out and not out[-1].endswith("…"):
        out[-1] += " …"
    return out


def _tool_block(event: SessionEvent) -> str:
    """The ⏺/⎿ lines of one tool call (trailing blank line included)."""
    summary = _summarize_input(event.tool_input)
    head = f"⏺ {event.tool_name}({summary})" if summary else f"⏺ {event.tool_name}"
    lines = [head]
    results = _result_lines(event.tool_result or "")
    if results:
        lines.append(f"  ⎿ {results[0]}")
        lines.extend(f"    {line}" for line in results[1:])
    return "\n".join(lines) + "\n\n"


def _group_turns(events: list[SessionEvent]) -> list[tuple[int | None, list[SessionEvent]]]:
    """Split *events* into turns: ``(prompt_number, events)``, oldest first.

    A turn is one human prompt plus everything until the next. Events before the
    first prompt (a resumed session's tail, tool results…) form a number-less
    preamble turn. Prompt numbers are 1-based over the FULL event list, so the
    ``(N)`` headers match the prompts tab even after old turns are trimmed.
    """
    turns: list[tuple[int | None, list[SessionEvent]]] = []
    number = 0
    for event in events:
        if event.kind == "prompt":
            number += 1
            turns.append((number, [event]))
        elif turns:
            turns[-1][1].append(event)
        else:
            turns.append((None, [event]))  # preamble before the first prompt
    return turns


def _render_turn(
    number: int | None, events: list[SessionEvent], *, newest: bool
) -> list[tuple[str, str]]:
    """One turn as ``(text, tag)`` segments (headers rule-tagged, tools dim)."""
    segments: list[tuple[str, str]] = []
    body = events
    if number is not None and events and events[0].kind == "prompt":
        segments.append((f"## ({number}) you\n\n", TAG_RULE))
        prompt = escape_fences(events[0].text.strip())
        segments.append((prompt + "\n\n", TAG_LAST if newest else TAG_TEXT))
        body = events[1:]
    if body:
        segments.append(("## claude\n\n", TAG_RULE))
    for event in body:
        if event.kind == "text":
            segments.append((_balance_fences(event.text.strip()) + "\n\n", TAG_TEXT))
        elif event.kind == "tool":
            segments.append((_tool_block(event), TAG_TOOL))
    return segments


def _size(segments: list[tuple[str, str]]) -> int:
    return sum(len(text.encode("utf-8")) for text, _tag in segments)


def _truncate_tail(segments: list[tuple[str, str]], max_bytes: int) -> list[tuple[str, str]]:
    """Hard-truncate an oversized single turn, keeping its TRAILING bytes (O5).

    The per-line styling of the cut region is collapsed to plain text — acceptable
    for this pathological case (a lone turn larger than the whole-body cap).
    """
    note = "… [turn truncated — showing only its newest part]\n\n"
    body = "".join(text for text, _tag in segments)
    budget = max(1, max_bytes - len(note.encode("utf-8")))
    tail = body.encode("utf-8")[-budget:].decode("utf-8", errors="ignore")
    return [(note, TAG_TOOL), (tail, TAG_TEXT)]


def session_segments(events: list[SessionEvent]) -> list[tuple[str, str]]:
    """The full-session body as ``(text, tag)`` segments, oldest first (capped).

    The canonical output both surfaces render: the vault session mirror embeds the
    tag-free concatenation (:func:`render_session`), the peek panel styles the same
    segments. Oldest whole turns are dropped (with a ``showing last N of M turns``
    note, counted inside the cap) until the body fits :data:`SESSION_MAX_BYTES`;
    a single turn that alone exceeds the cap is tail-truncated.
    """
    turns = _group_turns(events)
    if not turns:
        return [(_EMPTY_BODY, TAG_TEXT)]
    rendered = [
        _render_turn(number, turn_events, newest=index == len(turns) - 1)
        for index, (number, turn_events) in enumerate(turns)
    ]
    sizes = [_size(segments) for segments in rendered]
    total = len(rendered)

    def note_text(keep: int) -> str:
        return f"_showing last {keep} of {total} turns_\n\n"

    def note_bytes(keep: int) -> int:
        return len(note_text(keep).encode("utf-8")) if keep < total else 0

    keep = total
    while keep > 1 and sum(sizes[-keep:]) + note_bytes(keep) > SESSION_MAX_BYTES:
        keep -= 1
    segments = [segment for turn in rendered[-keep:] for segment in turn]
    if keep < total:
        segments = [(note_text(keep), TAG_TOOL), *segments]
    if _size(segments) > SESSION_MAX_BYTES:  # a single oversized turn (keep == 1)
        prefix = segments[:1] if keep < total else []
        budget = SESSION_MAX_BYTES - _size(prefix)
        segments = prefix + _truncate_tail(segments[len(prefix) :], budget)
    return segments


def render_session(events: list[SessionEvent]) -> str:
    """The full-session markdown body — the tag-free concatenation of the segments."""
    return "".join(text for text, _tag in session_segments(events))


def segments_for_path(path: Path, cache_key: str | None = None) -> list[tuple[str, str]]:
    """Mtime-cached segments for transcript *path* (see :data:`_RENDER_CACHE`)."""
    # Lazy import: adapters.claude owns the JSONL schema; keep this module's import
    # graph one-directional (mirrors/peek → sessionmd → adapters).
    # pylint: disable-next=import-outside-toplevel
    from .adapters.claude import events_in_file  # noqa: PLC0415

    key = cache_key or str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return session_segments([])
    cached = _RENDER_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    segments = session_segments(events_in_file(path))
    _RENDER_CACHE[key] = (mtime, segments)
    return segments


def segments_for(adapter: ClaudeAdapter, session: Session) -> list[tuple[str, str]]:
    """Mtime-cached segments for a tracked *session* (missing transcript → empty body)."""
    path = adapter.transcript_path(session.cwd, session.session_id)
    if path is None:
        return session_segments([])
    return segments_for_path(path, cache_key=session.session_id)


def render_for(adapter: ClaudeAdapter, session: Session) -> str:
    """The session's markdown body (the mirrors' entry point) — cached via segments."""
    return "".join(text for text, _tag in segments_for(adapter, session))
