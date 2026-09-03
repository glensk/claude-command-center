"""Claude Code adapter.

This is the *only* module that reads Claude Code's internal, undocumented
on-disk layout, so a future Claude Code change can break at most this file:

* live registry: ``$CLAUDE_HOME/sessions/<pid>.json``
  (fields: pid, sessionId, cwd, kind, status, name?, agent?, *At timestamps)
* transcripts:   ``$CLAUDE_HOME/projects/<cwd-with-slashes-as-dashes>/<id>.jsonl``

``probe()`` lets callers detect a layout change and degrade gracefully.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .. import config
from ..models import (
    CODEX_WORKFLOW_NAME,
    EFFORT_LEVELS,
    LiveSession,
    SessionEvent,
    TranscriptScan,
    now_ms,
)

# A genuine invocation of the Codex workflow records the slash command as a
# ``<command-name>/codex-implement-task-and-claude-review`` tag in the transcript.
# Matching the BARE name is far too loose: the workflow's name also appears in every
# session's injected ``skill_listing`` attachment (the available-skills list) and in
# this repo's AGENTS.md prose — so a bare-substring scan badges EVERY session as a
# Codex-workflow session. The ``<command-name>/`` prefix is present only on an actual
# invocation (verified: absent from skill_listing records and doc prose).
_CODEX_WORKFLOW_MARKER = f"<command-name>/{CODEX_WORKFLOW_NAME}"

# Short-lived cache of the process tree (ppid -> [(pid, command)]) so all the
# per-session has_subagent() calls in one TUI refresh share a single `ps` scan.
_PROC_CACHE: dict[str, object] = {"ts": 0.0, "map": {}}

# Claude Code's rate-limit halt text, e.g. "You've hit your weekly limit · resets …"
# or "You've hit your session limit · resets …" (5-hour window). Matched only inside
# an isApiErrorMessage assistant record (see is_halted) so user prompts that merely
# quote the phrase never trigger it.
_RATE_LIMIT_RE = re.compile(r"hit your \w[\w-]* limit", re.IGNORECASE)
# Tail size scanned for the halt: the error is always the final transcript line, so a
# small read suffices regardless of how large the full transcript is.
_HALT_TAIL_BYTES = 65_536

# A Bash-tool shell signature: Claude Code runs every Bash tool call as a zsh that
# first ``source``s a per-session shell snapshot under ``~/.claude/shell-snapshots/``.
# A *foreground* call keeps the session "busy"; one that survives into an idle session
# is a background task (``run_in_background``). This path substring identifies such a
# shell and excludes the session's persistent helpers (MCP servers, ``caffeinate`` …),
# which are not snapshot shells. See ``has_background_task`` + ``models.derive_status``.
_BG_SHELL_SIGNATURE = "shell-snapshots/snapshot-"

# A ``claude`` process launched by ``ccc start-job`` (or the user) carries an explicit
# ``--effort <level>`` / ``--effort=<level>`` argument. This parses the level out of the
# live process's command line (see ``session_effort``); the value is validated against
# ``models.EFFORT_LEVELS`` by the caller.
_EFFORT_ARG_RE = re.compile(r"--effort(?:=|\s+)(\S+)")

# Seconds a "no transcript found" resolution is trusted before the glob fallback
# re-runs. A found path is cached indefinitely (transcripts never move) but
# revalidated with a cheap exists() on every hit; the exact-munged-path probe
# still runs on every negative hit, so a brand-new transcript in its canonical
# location is picked up instantly — only cross-account glob DISCOVERY is delayed.
_TRANSCRIPT_NEG_TTL = 60.0


def _same_dir(a: Path, b: Path) -> bool:
    """True if paths *a* and *b* point at the same directory (resolve-tolerant)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _resolve_registry_group(entries: list[LiveSession]) -> LiveSession:
    """Collapse every registry entry for ONE session id into the single truthful one.

    A crashed session leaves its ``sessions/<pid>.json`` behind, so the same id can sit
    in several registries (or twice in one) with only ONE running process. Two rules:

    * **A live entry always wins.** A stale dead entry must never shadow the running
      process — that would report ``alive=False``, park a working session and make
      ``focus-job`` refuse. Ties (and all-dead groups) break on ``updated_at``.
    * **A D9 conflict needs two LIVE entries in different accounts.** "Live in two
      accounts" is about running processes, not leftover files: flagging a dead entry
      would blank ``config_dir`` forever (the stale file never "exits"), permanently
      refusing resume for an id that merely once ran under the other account — exactly
      what resuming a session under the second account's ``claude --resume`` produces.
    """
    alive = [e for e in entries if e.alive]
    chosen = max(alive or entries, key=lambda e: e.updated_at)
    homes = {e.config_dir for e in alive}
    if len(homes) > 1:
        from .. import accounts

        first, second = sorted(homes)[:2]
        accounts.warn_conflict(chosen.session_id, first, second)
        chosen.conflict = True
        chosen.config_dir = ""
    return chosen


def _assistant_text(record: dict) -> str:
    """Concatenate the text blocks of an assistant transcript record (else "")."""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return " ".join(parts)


# Wrapper blocks Claude Code wraps around a user turn that are not part of what the
# human typed: slash-command stubs, the stdout it echoes back, the hook / session
# context appended as <system-reminder>, and the <task-notification> blocks the harness
# injects as a *user* record every time a background agent/task finishes. All are
# stripped before a prompt is shown; a record whose text is *only* such a wrapper (a
# lone task-notification is the common case) then collapses to "" and is skipped, so
# `ccc peek` lists real human prompts only — not background-task completion notices.
_PROMPT_WRAPPER_RE = re.compile(
    r"<(command-[a-z]+|local-command-[a-z]+|system-reminder|bash-[a-z]+|task-notification)>"
    r".*?</\1>",
    re.DOTALL,
)
# In-process caches for the two per-transcript facts, keyed by the file's
# ``(mtime_ns, size)`` identity — the same pin the persisted ``transcript_scan`` row uses
# (see ``store.transcript_scans``), so a cache hit means "this exact file state".
# session_id -> (mtime_ns, size, marker found, bytes scanned)
_CODEX_WORKFLOW_CACHE: dict[str, tuple[int, int, bool, int]] = {}
# transcript path -> (mtime_ns, size, observed model)
_OBSERVED_MODEL_CACHE: dict[str, tuple[int, int, str | None]] = {}


def _prompt_payload_text(content: object) -> str | None:
    """The human-readable text of a prompt payload, or ``None`` if it carries none.

    A prompt is stored either as a plain string or — as soon as the human pasted an
    image alongside it — as a block list, in which case only the ``text`` blocks are
    prose (image blocks are skipped). Both ``user`` records and ``queued_command``
    attachments use these two shapes, so both read them through here.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    return " ".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _user_prompt_text(record: dict) -> str | None:
    """The human-typed text of a transcript record, or ``None`` if it is not one.

    A genuine prompt is a main-chain ``user`` record whose content is a string or a
    block list with real ``text`` — not a ``tool_result``-only list, not meta /
    sidechain bookkeeping. Wrapper blocks (slash-command stubs, echoed command
    stdout, appended ``<system-reminder>`` hook context) are stripped.
    """
    if not isinstance(record, dict) or record.get("type") != "user":
        return None
    if record.get("isMeta") or record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, list):
        blocks = [block for block in content if isinstance(block, dict)]
        if blocks and all(block.get("type") == "tool_result" for block in blocks):
            return None  # a tool result fed back to the model, not a prompt
    text = _prompt_payload_text(content)
    if text is None:
        return None
    cleaned = _PROMPT_WRAPPER_RE.sub("", text).strip()
    return cleaned or None


def _queued_prompt_text(record: dict) -> str | None:
    """The human-typed text of a *queued* prompt record, or ``None`` if it is not one.

    A prompt typed while Claude Code is still working is **queued**, and the queue
    drains it into the conversation as an ``attachment`` record of type
    ``queued_command`` — never as a ``user`` record. So :func:`_user_prompt_text`
    alone silently drops every such turn (it was the whole reason a real ask went
    missing from the ``ccc peek`` prompts tab). The record sits at the position where
    the queue delivered it, so reading it in file order keeps the conversation order.

    Two of the three flavours are not human prose and stay excluded: ``commandMode ==
    "task-notification"`` is the harness's background-agent completion notice, and
    ``origin.kind == "peer"`` is a ``<cross-session-message>`` injected by another
    Claude session. A missing ``origin`` is human (older Claude Code wrote none).
    """
    if not isinstance(record, dict) or record.get("type") != "attachment":
        return None
    if record.get("isMeta") or record.get("isSidechain"):
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    if attachment.get("commandMode") != "prompt":
        return None
    origin = attachment.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    text = _prompt_payload_text(attachment.get("prompt"))
    if text is None:
        return None
    cleaned = _PROMPT_WRAPPER_RE.sub("", text).strip()
    return cleaned or None


def _prompt_text(record: dict) -> str | None:
    """The human prompt of *record* — typed at an idle prompt **or** queued while busy.

    The single filter behind every prompt listing (``ccc peek``'s prompts tab, the
    ``(N)`` indexing of its session tab, and the vault session mirrors), so the two
    on-disk shapes a human turn can take never diverge between them.
    """
    return _user_prompt_text(record) or _queued_prompt_text(record)


def _tool_result_text(content: object) -> str:
    """Flatten a ``tool_result`` block's ``content`` payload to plain text (else "").

    The payload is a string, or a list of blocks whose ``text`` fields carry the
    output (image/other block types are skipped).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _model_in_line(line: bytes) -> str | None:
    """The real ``message.model`` of one raw transcript line, or ``None``.

    The prefilter (the raw bytes ``"assistant"`` AND ``"model"`` both present) is what makes
    the tail read cheap: transcripts are written as COMPACT JSON, so a record that carries a
    model always contains those exact byte sequences and a line that does not can be
    rejected without a ``json.loads``. ``"<synthetic>"`` is the harness's own placeholder
    model and never counts.
    """
    if b'"assistant"' not in line or b'"model"' not in line:
        return None
    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None  # a partial line (live transcript mid-write) or plain junk
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    value = message.get("model")
    return value if isinstance(value, str) and value and value != "<synthetic>" else None


def last_model_in_file(path: Path, chunk_size: int = 65536) -> str | None:
    """The raw ``message.model`` of the LAST real assistant record in transcript *path*.

    Reads the file BACKWARDS in *chunk_size* blocks and returns on the first hit, because
    the answer is essentially always in the last 64 KiB (198 of 200 sampled transcripts):
    the previous forward walk parsed every line of every stored transcript on each pass —
    376k lines across 677 sessions, ~27 s of a cold ``ccc ls``. Each block carries its
    leading partial line into the next (earlier) block, so no record is split by the
    chunking; lines are then examined newest-first (see :func:`_model_in_line`).

    A trailing partial line (a live transcript caught mid-write) simply fails to parse and
    is skipped, as are blank lines. ``None`` when the file is empty or holds no real
    assistant record.

    A read failure RAISES :class:`OSError` — it is not a fact about the transcript, and
    swallowing it as ``None`` would let a transient EIO/EACCES be persisted as "this
    session ran on no model" (see ``scan_transcript`` § Property P). Every caller contains
    it and simply retries on the next pass.
    """
    with path.open("rb") as handle:
        pos = handle.seek(0, os.SEEK_END)
        carry = b""  # partial first line of the block already scanned
        while pos > 0:
            start = max(0, pos - chunk_size)
            handle.seek(start)
            block = handle.read(pos - start) + carry
            pos = start
            lines = block.split(b"\n")
            if pos > 0:  # lines[0] continues into the block before this one
                carry = lines[0]
                lines = lines[1:]
            for line in reversed(lines):
                model = _model_in_line(line)
                if model is not None:
                    return model
    return None


def codex_marker_in_file(path: Path, start: int = 0, chunk_size: int = 1 << 20) -> tuple[bool, int]:
    """Scan bytes ``[start, size)`` of *path* for the Codex-workflow marker.

    Returns ``(found, size)``: the size is the offset the scan covered, so the next pass
    over an append-only transcript can resume there instead of re-reading the whole file
    (a full-file scan of every session cost ~7 s of a cold ``ccc ls``). Consecutive chunks
    overlap by ``len(marker) - 1`` bytes so a marker straddling a boundary is still found,
    and a resume rewinds by the same ``len(marker) - 1`` bytes: a live transcript can be
    caught mid-write, so the pass that recorded *start* may have covered the first half of
    a marker whose second half landed later. The rewound window is one byte too short to
    hold a whole marker, so it can never re-find one that ended at or before *start*.
    A *start* past the end means the file was truncated or rewritten under us, so the scan
    restarts from 0.

    A read failure RAISES :class:`OSError` rather than reporting ``(False, start)``: "I
    could not read it" is not the fact "no marker", and persisting it would freeze a
    wrong answer into the scan row (see ``scan_transcript`` § Property P). Every caller
    contains it and retries on the next pass.
    """
    marker = _CODEX_WORKFLOW_MARKER.encode()
    overlap = len(marker) - 1
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        # Cross-pass split (see the docstring): re-read the last ``overlap`` bytes before
        # *start* — bounded, and never enough to re-find an already-covered marker.
        pos = max(0, start - overlap) if 0 <= start <= size else 0
        handle.seek(pos)
        carry = b""  # tail of the previous chunk, in case the marker straddles
        while pos < size:
            block = handle.read(chunk_size)
            if not block:
                break
            pos += len(block)
            window = carry + block
            if marker in window:
                return True, size
            # Carry the last marker-length-1 bytes of the STREAM (not of this block):
            # correct even when chunk_size is smaller than the marker itself.
            carry = window[-overlap:] if overlap else b""
    return False, size


def first_record_is_queue_op(path: Path) -> bool | None:
    """Whether *path*'s FIRST non-blank record is a ``queue-operation`` — tri-state.

    That first record is the on-disk signature of a headless ``claude -p`` run (the
    prompt is enqueued), whereas an interactive session's transcript opens with session
    meta (``last-prompt`` / ``summary`` / a user message).

    ``True``/``False`` are returned ONLY after the first non-blank line really parsed as
    JSON. ``None`` means "nothing decided yet" — an empty file, an unreadable one, or a
    first record that is not valid JSON (a transcript caught mid-write) — because this
    answer gets PERSISTED (``TranscriptScan.headless``): storing a not-yet-known as
    ``False`` would freeze a headless row into looking interactive forever, so the
    undetermined case must stay distinguishable and be re-probed next pass.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                return isinstance(record, dict) and record.get("type") == "queue-operation"
    except (OSError, json.JSONDecodeError):
        return None
    return None  # empty file (or blank lines only): nothing to decide on yet


def events_in_file(path: Path) -> list[SessionEvent]:
    """The normalized conversation events of transcript *path*, oldest first.

    The single schema-aware walk behind the full-session rendering (vault session
    mirrors + the peek panel's session tab — see :mod:`command_center.sessionmd`):

    * ``prompt`` events use the exact :func:`_prompt_text` filter — both the ``user``
      records and the ``queued_command`` attachments a busy-typed prompt lands in — so
      they align 1:1 with :meth:`ClaudeAdapter.all_user_prompts` (same ``(N)``
      indexing as the prompts tab).
    * ``text`` events are main-chain assistant text blocks (thinking is skipped).
    * ``tool`` events are main-chain ``tool_use`` blocks; the later ``tool_result``
      record is paired back onto the event by ``tool_use_id`` (result stays ``None``
      if the transcript ends before the result arrives).

    Meta / sidechain records are skipped throughout. Read errors yield the events
    collected so far (missing file → ``[]``).
    """
    events: list[SessionEvent] = []
    pending: dict[str, SessionEvent] = {}  # tool_use_id → its (unpaired) tool event
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("isSidechain"):
                    continue
                _collect_events(record, events, pending)
    except OSError:
        pass
    return events


def _collect_events(
    record: dict, events: list[SessionEvent], pending: dict[str, SessionEvent]
) -> None:
    """Append *record*'s events to *events* (helper of :func:`events_in_file`)."""
    rtype = record.get("type")
    if rtype == "attachment":
        # A prompt queued while Claude was busy — the only place it is recorded.
        queued = _queued_prompt_text(record)
        if queued is not None:
            events.append(SessionEvent(kind="prompt", text=queued))
        return
    if rtype == "user":
        prompt = _user_prompt_text(record)
        if prompt is not None:
            events.append(SessionEvent(kind="prompt", text=prompt))
        # The same user record may (also) carry tool_result blocks — pair them.
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                event = pending.pop(str(block.get("tool_use_id", "")), None)
                if event is not None:
                    event.tool_result = _tool_result_text(block.get("content"))
        return
    if rtype != "assistant" or record.get("isMeta"):
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            events.append(SessionEvent(kind="text", text=content))
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", ""))
            if text.strip():
                events.append(SessionEvent(kind="text", text=text))
        elif btype == "tool_use":
            raw_input = block.get("input")
            event = SessionEvent(
                kind="tool",
                tool_name=str(block.get("name", "")),
                tool_input=raw_input if isinstance(raw_input, dict) else {},
            )
            events.append(event)
            block_id = str(block.get("id", ""))
            if block_id:
                pending[block_id] = event
        # thinking / redacted_thinking blocks are deliberately not emitted


def _is_claude_program(command: str) -> bool:
    """True if *command*'s executable (argv[0]) is the ``claude`` CLI itself.

    Matches the program NAME, never a substring of the whole command line: a
    helper whose *path* contains ``.claude`` — the statusline command
    (``bash ~/.claude/statusline-command.sh``) or any hook script under
    ``~/.claude/`` — is NOT a subagent, yet the old ``"claude" in command``
    test counted it, painting an idle session with the green ▶ every refresh.
    Genuine Claude Code subagents run as ``claude -p …`` whose argv[0] basename
    is ``claude`` (whether bare or an absolute path), which this still catches.
    """
    head = command.split(maxsplit=1)
    if not head:
        return False
    return os.path.basename(head[0]) == "claude"


_TRANSCRIPT_TAIL_BYTES = 32 * 1024 * 1024
_TASK_ID_RE = re.compile(r"<task-id>([A-Za-z0-9_-]+)</task-id>")


def transcript_tail_is_user_prompt(transcript: Path) -> bool:
    """True when the transcript's LAST record is a plain user prompt (no turn started on it).

    Claude Code appends the user's prompt — a slash command included, as its
    ``<command-name>`` record — the moment it is submitted and marks the session busy,
    BEFORE any command expansion or model request. So a registry ``busy`` whose
    transcript ends on such a record is "busy because of this very prompt", not a turn
    in flight: nothing has been generated or run yet. A user record carrying a
    ``tool_result`` block is a turn in progress and does NOT count. ``False`` on any
    read/parse problem (unknown ⇒ not idle).
    """
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            tail = handle.read().decode("utf-8", errors="replace")
        lines = [line for line in tail.splitlines() if line.strip()]
        record = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def pending_background_work(transcript: Path) -> list[str] | None:
    """Ids of background Bash tasks / agents the session started but was never told finished.

    Claude Code does not expose in-process background work anywhere else on disk, but
    the transcript records both ends of it in STRUCTURED fields: a launch is a record
    whose ``toolUseResult`` carries ``backgroundTaskId`` (a ``run_in_background`` Bash
    task) or ``status == "async_launched"`` with an ``agentId`` (a background agent);
    it ends with a ``TaskStop`` tool call that did not error, or with an ``attachment``
    record of ``commandMode == "task-notification"`` whose prompt names the id in
    ``<task-id>…</task-id>``. Launched minus ended = still in flight (order kept).

    ``None`` means UNKNOWN — the file could not be read, or a record other than the
    (possibly still being written) last line is not JSON — and callers must treat it
    as "refuse", never as "clear". Only the last 32 MB are scanned, which comfortably
    covers any session's background history.
    """
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return None
    lines = raw.split(b"\n")
    if size > _TRANSCRIPT_TAIL_BYTES:
        lines = lines[1:]  # the seek landed mid-record: drop the partial first line
    launched: list[str] = []
    ended: set[str] = set()
    stops: dict[str, str] = {}  # TaskStop tool_use id -> task id it targets
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                continue  # a partial last line is the live process mid-write, not corruption
            return None
        if not isinstance(record, dict):
            return None
        result = record.get("toolUseResult")
        if isinstance(result, dict):
            task_id = result.get("backgroundTaskId")
            if (
                not (isinstance(task_id, str) and task_id)
                and result.get("status") == "async_launched"
            ):
                task_id = result.get("agentId")
            if isinstance(task_id, str) and task_id and task_id not in launched:
                launched.append(task_id)
        attachment = record.get("attachment")
        if isinstance(attachment, dict) and attachment.get("commandMode") == "task-notification":
            ended.update(_TASK_ID_RE.findall(str(attachment.get("prompt") or "")))
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "TaskStop":
                params = block.get("input")
                target = params.get("task_id") if isinstance(params, dict) else None
                if isinstance(target, str) and isinstance(block.get("id"), str):
                    stops[block["id"]] = target
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in stops:
                if not block.get("is_error"):
                    ended.add(stops[block["tool_use_id"]])
    return [task_id for task_id in launched if task_id not in ended]


def _children_map() -> dict[int, list[tuple[int, str]]]:
    now = time.monotonic()
    if isinstance(_PROC_CACHE["ts"], float) and now - _PROC_CACHE["ts"] < 3.0:
        return _PROC_CACHE["map"]  # type: ignore[return-value]
    children: dict[int, list[tuple[int, str]]] = {}
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        out = ""
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            child_pid, parent_pid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(parent_pid, []).append((child_pid, parts[2]))
    _PROC_CACHE["ts"] = now
    _PROC_CACHE["map"] = children
    return children


def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _command_for_pid(pid: int) -> str | None:
    """The command line of *pid* from the cached process scan (``None`` if not found).

    Reuses the same short-lived ``ps`` snapshot (:func:`_children_map`) that
    ``has_subagent`` / ``has_background_task`` walk, so this costs no extra syscall on a
    refresh that already scanned the tree.
    """
    for entries in _children_map().values():
        for child_pid, command in entries:
            if child_pid == pid:
                return command
    return None


class ClaudeAdapter:  # pylint: disable=too-many-public-methods
    """Reads Claude Code session state from one or more account config dirs.

    Multi-account (D0/D1/D14): ``discover()`` scans EVERY configured account's
    ``sessions/`` registry and stamps each :class:`LiveSession` with its account's
    absolute ``config_dir``; ``transcript_path`` resolves against the session's own
    account first, then falls back to every other account (so a shared-store symlink
    is an optimisation, never a correctness precondition). The single-home
    constructor (``ClaudeAdapter(claude_home=…)``) still works — a lone home becomes
    ``{"private": home}`` — so every existing call site is unaffected.
    """

    name = "claude"

    def __init__(self, claude_home: Path | None = None) -> None:
        if claude_home is not None:
            self.homes: dict[str, Path] = {"private": Path(claude_home)}
        else:
            self.homes = config.claude_config_dirs()
        # The default account's home backs the back-compat single-home attributes
        # (``self.home`` / ``sessions_dir`` / ``projects_dir``): the passed home, or
        # the account whose dir is ``claude_home()``, else the first configured one.
        default = config.claude_home()
        self.home = next(
            (h for h in self.homes.values() if h == default),
            next(iter(self.homes.values()), default),
        )
        if claude_home is not None:
            self.home = Path(claude_home)
        self.sessions_dir = self.home / "sessions"
        self.projects_dir = self.home / "projects"
        # Per-session-id transcript resolution cache (see ``transcript_path``): a
        # found Path is trusted (revalidated with exists()), a None is trusted for
        # ``_TRANSCRIPT_NEG_TTL`` seconds. Instance-level so a fresh adapter starts clean.
        self._transcript_cache: dict[str, tuple[Path | None, float]] = {}

    def discover(self) -> list[LiveSession]:
        """Every live session across ALL configured account registries (D0/D9).

        Each entry is stamped with its account's absolute ``config_dir``. An id that is
        RUNNING under two accounts at once is a CONFLICT (D9): warn once, leave
        ``config_dir`` blank and set ``conflict`` so reconcile never mis-attributes it and
        resume/focus refuse until one side exits — never a race won by ``updated_at``.
        """
        groups: dict[str, list[LiveSession]] = {}
        for home in self.homes.values():
            sessions_dir = home / "sessions"
            if not sessions_dir.is_dir():
                continue
            home_str = str(home)
            for path in sessions_dir.glob("*.json"):
                live = self._parse_registry_entry(path, home_str)
                if live is not None:
                    groups.setdefault(live.session_id, []).append(live)
        return [_resolve_registry_group(entries) for entries in groups.values()]

    def _parse_registry_entry(self, path: Path, home_str: str) -> LiveSession | None:
        """Parse one ``sessions/<pid>.json`` into a stamped :class:`LiveSession`."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        session_id = data.get("sessionId")
        if not session_id:
            return None
        # Skip headless / SDK sessions: every `claude -p` (the daemon's own
        # summary & auto-progress calls, plus any other tool's headless runs)
        # registers here with entrypoint "sdk-cli", reusing kind "interactive".
        # They are not user sessions — left unfiltered they flood the store with
        # short-lived "parked" rows (typically at cwd "/", the launchd cwd) that
        # reconcile() then persists forever. entrypoint is the robust signal
        # (cwd-independent); kind is not (Claude reuses "interactive").
        entrypoint = str(data.get("entrypoint", "cli"))
        if entrypoint.startswith("sdk"):
            return None
        pid = int(data.get("pid", 0) or 0)
        return LiveSession(
            pid=pid,
            session_id=session_id,
            cwd=data.get("cwd", ""),
            kind=data.get("kind", "interactive"),
            entrypoint=entrypoint,
            raw_status=data.get("status", "idle"),
            name=data.get("name"),
            agent=data.get("agent", "claude"),
            started_at=int(data.get("startedAt", 0) or 0),
            updated_at=int(data.get("updatedAt", 0) or 0),
            status_updated_at=int(data.get("statusUpdatedAt", 0) or 0),
            alive=_pid_alive(pid),
            config_dir=home_str,
        )

    def _ordered_projects_dirs(self, config_dir: str | None) -> list[Path]:
        """Account ``projects/`` dirs to search, the owning account first (D14)."""
        homes = list(self.homes.values())
        if config_dir:
            try:
                owner = Path(config_dir).expanduser().resolve()
            except OSError:
                owner = Path(config_dir).expanduser()
            homes.sort(key=lambda h: 0 if _same_dir(h, owner) else 1)
        return [home / "projects" for home in homes]

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        """Resolve the JSONL transcript, owning account first, others as fallback (D14).

        Tries each account's ``projects/`` dir in turn — exact munged path, then the
        ``*/<id>.jsonl`` glob — starting with the session's own account (*config_dir*)
        when known. This makes D0's shared-store symlink an optimisation rather than a
        correctness precondition: a transcript resolves even if the trees were never
        shared. ``None`` when no account holds it.

        Resolution is memoized per session id (ids are unique UUIDs, so the found
        transcript is THE session's transcript regardless of cwd spelling or account
        ordering — the cross-account glob is the TUI-refresh hot path). A positive hit
        is revalidated with a cheap ``exists()`` (a deleted transcript triggers full
        re-resolution); a negative result is trusted for ``_TRANSCRIPT_NEG_TTL`` seconds
        with the exact-munged-path probe still live, so a new transcript in its canonical
        location lands instantly — only cross-account glob DISCOVERY is delayed. The
        plain-dict cache is safe under the GIL for the TUI's worker-thread use: a race
        only causes redundant re-resolution, never a wrong answer.
        """
        encoded = cwd.replace("/", "-")
        cached = self._transcript_cache.get(session_id)
        if cached is not None:
            path, stamp = cached
            if path is not None:
                if path.exists():
                    return path
                self._transcript_cache.pop(session_id, None)  # deleted → full re-resolve below
            elif time.monotonic() - stamp < _TRANSCRIPT_NEG_TTL:
                # cheap exact-path probes only; skip the expensive glob during the TTL
                for projects_dir in self._ordered_projects_dirs(config_dir):
                    candidate = projects_dir / encoded / f"{session_id}.jsonl"
                    if candidate.exists():
                        self._transcript_cache[session_id] = (candidate, time.monotonic())
                        return candidate
                return None
        resolved = None
        for projects_dir in self._ordered_projects_dirs(config_dir):
            candidate = projects_dir / encoded / f"{session_id}.jsonl"
            if candidate.exists():
                resolved = candidate
                break
            hits = list(projects_dir.glob(f"*/{session_id}.jsonl"))
            if hits:
                resolved = hits[0]
                break
        self._transcript_cache[session_id] = (resolved, time.monotonic())
        return resolved

    def all_user_prompts_in_file(self, path: Path) -> list[str]:
        """Every human-typed prompt in transcript *path*, oldest first (cleaned).

        Walks the whole file — a prompt can sit far behind a long agentic turn, so
        the tail-only read used for halt detection is not enough here. Applies the
        same :func:`_prompt_text` filter as the last-prompt case (meta / sidechain /
        tool-result records are skipped, wrappers stripped), so a prompt typed while
        Claude was busy — queued, and stored as a ``queued_command`` attachment rather
        than a ``user`` record — is listed too. Cheap in practice: run on demand for
        ``ccc peek``, not per refresh.
        """
        prompts: list[str] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = _prompt_text(record)
                    if text is not None:
                        prompts.append(text)
        except OSError:
            return prompts
        return prompts

    def last_prompt_in_file(self, path: Path) -> str | None:
        """The last human-typed prompt in transcript *path* (cleaned), or ``None``."""
        prompts = self.all_user_prompts_in_file(path)
        return prompts[-1] if prompts else None

    def all_user_prompts(self, cwd: str, session_id: str) -> list[str]:
        """Every prompt the human typed in *session_id*, oldest first (for ``ccc peek``).

        ``[]`` when the transcript is missing or holds no human turn yet.
        """
        path = self.transcript_path(cwd, session_id)
        return self.all_user_prompts_in_file(path) if path is not None else []

    def session_events(self, cwd: str, session_id: str) -> list[SessionEvent]:
        """The session's normalized conversation events (see :func:`events_in_file`).

        ``[]`` when the transcript is missing. Callers cache by transcript mtime
        (see ``sessionmd``) — this always re-reads.
        """
        path = self.transcript_path(cwd, session_id)
        return events_in_file(path) if path is not None else []

    def uses_codex_workflow(self, cwd: str, session_id: str) -> bool:
        """True if this transcript invoked the Codex implementation workflow.

        The launch ``job_type`` covers future jobs started through ccc; this catches manual
        use of the slash command or same-named skill by scanning the transcript for the
        workflow marker (:func:`codex_marker_in_file`). The in-process cache is keyed by the
        file's ``(mtime_ns, size)`` identity: an unchanged file answers from the cache, a
        GROWN one is scanned only over the bytes appended since the last pass (transcripts
        are append-only), and any other change (a rewrite in place) forces a full rescan.
        A marker once seen is sticky — it can never un-happen.

        This is the per-process view; :meth:`scan_transcript` is the same answer persisted
        across processes (``store.transcript_scans``).
        """
        path = self.transcript_path(cwd, session_id)
        if path is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        start = 0
        cached = _CODEX_WORKFLOW_CACHE.get(session_id)
        if cached is not None:
            c_mtime, c_size, c_found, c_scanned = cached
            if c_found:
                return True
            if c_mtime == st.st_mtime_ns and c_size == st.st_size:
                return False
            if st.st_size > c_size:  # grew → only the new bytes can hold a new marker
                start = c_scanned
        found, scanned_to = codex_marker_in_file(path, start)
        _CODEX_WORKFLOW_CACHE[session_id] = (st.st_mtime_ns, st.st_size, found, scanned_to)
        return found

    def observed_model(self, cwd: str, session_id: str) -> str | None:
        """The model the session actually ran on — its last real ``message.model``.

        Returns the ``message.model`` value of the LAST assistant record in the
        transcript, skipping any whose model is missing or the harness-injected literal
        ``"<synthetic>"``. This is the OBSERVED model, unlike the job-config
        ``llm_overseer``/``llm_exec`` fields (pure DB defaults for a session that was
        never launched as a ccc job). ``None`` when the transcript is missing or holds
        no real model entry.

        The read is a backwards tail scan (:func:`last_model_in_file`), cached in-process
        per transcript path by the file's ``(mtime_ns, size)`` identity — the same pin
        :meth:`scan_transcript` persists across processes.
        """
        path = self.transcript_path(cwd, session_id)
        if path is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path)
        cached = _OBSERVED_MODEL_CACHE.get(key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        model = last_model_in_file(path)
        _OBSERVED_MODEL_CACHE[key] = (st.st_mtime_ns, st.st_size, model)
        return model

    def scan_transcript(
        self, cwd: str, session_id: str, prior: TranscriptScan | None
    ) -> TranscriptScan | None:
        """The session's transcript facts, reusing *prior* when the file is untouched.

        The cross-process counterpart of :meth:`observed_model` + :meth:`uses_codex_workflow`
        (the persisted rows come from ``store.transcript_scans``): one ``stat()`` decides
        whether the file has to be opened at all. When *prior* pins the same
        ``(path, mtime_ns, size)`` the very SAME object is returned, so a caller can test
        ``new is prior`` to know there is nothing to re-persist — that identity is what makes
        a pass over hundreds of frozen (done/parked) transcripts cost one stat each instead
        of a full re-parse.

        Otherwise the model is re-read from the tail and the marker scan resumes at
        ``prior.codex_scanned_to`` — the file is append-only, so only the bytes added since
        the last pass can hold a new marker; a file that shrank below that offset (truncated
        or rewritten) is scanned from 0 again. A marker already found is sticky and is never
        looked for again. ``None`` when no transcript resolves under any account, or it
        cannot be stat'ed.

        ``headless`` (the first record being a queue-operation) is the one fact that can be
        *missing* from an otherwise-current row: rows written before the column existed
        carry ``None``. So an identity match with ``headless is None`` still probes the
        first record once — and only then, when something was actually learned, returns a
        NEW object so the caller persists it. An undetermined probe returns *prior*
        unchanged, which is what keeps a broken/empty transcript from re-writing its row on
        every pass. On a fresh row the fact is carried forward from *prior* ONLY on a
        same-path GROW (an append-only file's first record cannot change); truncation, a
        same-size rewrite or a path change all re-probe.

        **Property P (why no writer needs to win a race).** Every persisted row satisfies
        ``codex_scanned_to <= size``: ``codex_marker_in_file`` reports the size IT saw at
        its own open, which can exceed the ``stat()`` taken a moment earlier on a live
        transcript, so the offset is clamped to the pinned size (bytes covered beyond it
        are simply re-read next pass — bounded). On ANY identity mismatch the next pass
        re-derives every fact from the CURRENT file: the model from the tail, the marker
        from the persisted offset when the file grew else from 0, ``headless`` per the grow
        rule above. So whichever of two concurrent scanners wrote last, the next pass scans
        a superset of the bytes that could hold an unseen marker — a stale write costs one
        bounded re-read, never a wrong sticky fact (``codex=True`` is sticky and an
        append-only file re-finds its marker anyway). This is precisely why
        ``Store.put_transcript_scans`` carries no ordering guard: an
        ``excluded.mtime_ns >= mtime_ns`` rule would refuse *forever* to persist a
        transcript restored with an OLDER mtime, and rescan it on every pass instead.

        A read failure (``OSError`` out of ``last_model_in_file`` /
        ``codex_marker_in_file``) PROPAGATES: it is not a fact about the transcript, so
        nothing is persisted and the caller retries next pass. Only this method's own
        ``stat()`` failure returns ``None`` ("no transcript").
        """
        path = self.transcript_path(cwd, session_id)
        if path is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        same: TranscriptScan | None = None
        if prior is not None and prior.path == str(path):
            if prior.mtime_ns == st.st_mtime_ns and prior.size == st.st_size:
                if prior.headless is not None:
                    return prior  # identity == "nothing changed" (see the docstring)
                # Current row, but from before ``headless`` existed: fill it in once.
                probe = first_record_is_queue_op(path)
                if probe is None:
                    return prior  # nothing learned → no write this pass
                return dataclasses.replace(prior, headless=probe)
            same = prior
        if same is not None and same.codex:
            found, scanned_to = True, st.st_size
        else:
            # Resume only when the file GREW (append-only): any other change — a rewrite in
            # place, same size or smaller — rescans from 0, matching uses_codex_workflow.
            start = same.codex_scanned_to if same is not None and st.st_size > same.size else 0
            found, scanned_to = codex_marker_in_file(path, start)
        # Property P: pin the offset to the size THIS row records, never the (possibly
        # larger) size the marker scan saw at its own open.
        scanned_to = min(scanned_to, st.st_size)
        # The first record of an append-only file cannot change, so a same-path GROW
        # carries the known fact forward; anything else (truncation, a same-size rewrite,
        # a new path) re-reads it.
        headless = (
            same.headless
            if (same is not None and same.headless is not None and st.st_size > same.size)
            else first_record_is_queue_op(path)
        )
        return TranscriptScan(
            session_id=session_id,
            path=str(path),
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
            model=last_model_in_file(path),
            codex=found,
            codex_scanned_to=scanned_to,
            scanned_at=now_ms(),
            headless=headless,
        )

    def last_user_prompt(self, cwd: str, session_id: str) -> str | None:
        """The most recent prompt the human typed in *session_id*, cleaned for display.

        ``None`` when the transcript is missing or holds no human turn yet.
        """
        path = self.transcript_path(cwd, session_id)
        return self.last_prompt_in_file(path) if path is not None else None

    def is_oneshot_headless(self, cwd: str, session_id: str) -> bool:
        """True if the transcript opens with a queued one-shot prompt.

        That is the on-disk signature of a headless ``claude -p`` run (e.g.
        ``ai.py``'s commit-message generation): the prompt is enqueued as the very
        first transcript record (``type == "queue-operation"``), whereas an
        interactive session's transcript opens with session meta (``last-prompt`` /
        ``summary`` / a user message). The live ``discover()`` filter already blocks
        new headless rows by entrypoint; this lets ``prune`` retro-detect ones that
        leaked in before the hook learned to skip the ``sdk`` entrypoint, since their
        ``entrypoint`` is no longer on disk once parked.

        The probe itself is :func:`first_record_is_queue_op` (shared with the persisted
        ``TranscriptScan.headless`` fact); its tri-state ``None`` ("not determined yet")
        collapses to ``False`` here — this per-call answer is never stored, and a
        missing/unreadable transcript must not be pruned as headless junk.
        """
        path = self.transcript_path(cwd, session_id)
        if path is None:
            return False
        return first_record_is_queue_op(path) is True

    def claude_version(self, cwd: str, session_id: str) -> str | None:
        """The Claude Code version that last wrote to the session's transcript.

        Every transcript record carries a ``"version"`` field (e.g. ``"2.1.193"``);
        we return the most recent one. Tail-only read, so it stays cheap to poll each
        refresh. ``None`` when the transcript is missing or has no versioned record.
        """
        path = self.transcript_path(cwd, session_id)
        if path is None:
            return None
        for record in reversed(self._tail_records(path)):  # _tail_records is most-recent-last
            version = record.get("version")
            if isinstance(version, str) and version:
                return version
        return None

    def _tail_records(self, path: Path, max_bytes: int = _HALT_TAIL_BYTES) -> list[dict]:
        """Parse the JSONL records in the last *max_bytes* of *path* (most recent last).

        Reads only the tail so a multi-MB transcript stays cheap to poll each refresh.
        When the file exceeds *max_bytes* the first (possibly mid-record) line is dropped.
        """
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                truncated = size > max_bytes
                if truncated:
                    handle.seek(-max_bytes, os.SEEK_END)
                chunk = handle.read()
        except OSError:
            return []
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        if truncated and lines:
            lines = lines[1:]
        records: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def is_halted(self, cwd: str, session_id: str) -> bool:
        """True if the session's last main-chain *assistant* turn is a rate-limit halt.

        The halt's on-disk signature is an ``assistant`` record with
        ``isApiErrorMessage == true`` whose text reads "You've hit your … limit ·"
        (5-hour or weekly window). We look at the last **assistant** record only —
        NOT the last user/assistant record. A trailing *user* record after the error
        (a queued "continue", a background ``<task-notification>``, a slash command,
        or the auto-retry re-attempting the turn) does **not** mean the limit lifted:
        the session is still rate-limited and the next attempt simply 429s again. The
        halt is over only once a *successful* assistant turn follows (one without the
        ``isApiErrorMessage`` flag). Keying on the last user/assistant record instead
        made the status flip-flop HALTED↔WORKING during a wait, because every such
        trailing user record momentarily cleared the halt while the session sat idle
        (still alive, still reporting "busy"), falling through to WORKING (green ▶).
        Sub-agent (``isSidechain``) chatter is ignored — only the main thread counts.
        Gating on ``isApiErrorMessage`` is what keeps the many user prompts and
        task-notifications that merely *quote* the phrase from false-triggering.
        """
        path = self.transcript_path(cwd, session_id)
        if path is None:
            return False
        last_assistant: dict | None = None
        for record in self._tail_records(path):
            if record.get("isSidechain"):
                continue
            if record.get("type") == "assistant":
                last_assistant = record
        if last_assistant is None or not last_assistant.get("isApiErrorMessage"):
            return False
        return _RATE_LIMIT_RE.search(_assistant_text(last_assistant)) is not None

    def successful_response_since(self, cwd: str, session_id: str, byte_offset: int) -> bool:
        """True iff a successful MAIN-CHAIN assistant record landed after *byte_offset*.

        "Successful" = a ``type == "assistant"`` record with ``isApiErrorMessage``
        falsy — ANY such record proves the resumed session produced real model output
        (NOT turn completion: one turn can hold many assistant records). Sidechain
        records and ALL API-error records (rate-limit, overloaded, auth, …) never
        count, so a resume whose only yield is the injected prompt + an error turn
        stays "barren". Seeks to *byte_offset* (the transcript size at resume
        dispatch): the byte before it tells whether we sit on a record boundary; a
        partial first line is skipped, JSON-decode errors on a possibly-partial tail
        are ignored, and a file shorter than the offset (truncation/rotation) is a
        conservative False. Called once per observed re-halt, never per poll.
        """
        path = self.transcript_path(cwd, session_id)
        if path is None or byte_offset < 0:
            return False
        try:
            with path.open("rb") as handle:
                if byte_offset > 0:
                    handle.seek(byte_offset - 1)
                    at_boundary = handle.read(1) == b"\n"
                else:
                    at_boundary = True
                data = handle.read()
        except OSError:
            return False
        if not data:
            return False
        lines = data.decode("utf-8", errors="replace").splitlines()
        if not at_boundary and lines:
            lines = lines[1:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("isSidechain"):
                continue
            if record.get("type") == "assistant" and not record.get("isApiErrorMessage"):
                return True
        return False

    def halt_reset_at_ms(self, cwd: str, session_id: str) -> int:
        """Epoch ms the halting limit says it resets — 0 when absent/unparseable.

        Reads the same last main-chain assistant error record :meth:`is_halted` keys
        on and parses its reset phrasing ("resets 7:20pm", "resets in 2h 7m", ISO,
        epoch) via :func:`session_continue.parse_limit_message`. The session's OWN
        error names the true gate — e.g. a weekly Opus cap that the haiku
        ``--wait-only`` probe cannot see — so auto-resume can back off until then
        instead of relaunching into it every window.
        """
        from datetime import datetime

        from ..session_continue import parse_limit_message

        path = self.transcript_path(cwd, session_id)
        if path is None:
            return 0
        last_assistant: dict | None = None
        for record in self._tail_records(path):
            if record.get("isSidechain"):
                continue
            if record.get("type") == "assistant":
                last_assistant = record
        if last_assistant is None or not last_assistant.get("isApiErrorMessage"):
            return 0
        target = parse_limit_message(_assistant_text(last_assistant), datetime.now())
        if target is None:
            return 0
        try:
            return int(target.timestamp() * 1000)
        except (OverflowError, OSError, ValueError):
            return 0

    def last_activity_ms(self, live: LiveSession) -> int:
        transcript = self.transcript_path(live.cwd, live.session_id)
        if transcript is not None:
            try:
                return int(transcript.stat().st_mtime * 1000)
            except OSError:
                pass
        return live.updated_at or live.status_updated_at or live.started_at

    def has_subagent(self, pid: int, ignore: frozenset[int] = frozenset()) -> bool:
        """Best-effort: True if *pid* has a descendant ``claude`` process (a subagent).

        Claude Code does not expose subagents on disk, so this looks for a live
        child ``claude`` process (e.g. a ``claude -p`` subagent) in the process
        tree. It will not detect in-process Task agents. Pids in *ignore* (and their
        subtrees) are skipped — a caller running INSIDE the session passes its own
        ancestry so the shell it was typed into never counts as background work.
        """
        return self._descendant_matches(pid, _is_claude_program, ignore)

    def has_background_task(self, pid: int, ignore: frozenset[int] = frozenset()) -> bool:
        """Best-effort: True if *pid* has a live descendant Bash-tool shell.

        See ``_BG_SHELL_SIGNATURE``. Walks the same cached process tree as
        :meth:`has_subagent`. Combined with an *idle* raw status (see
        ``models.derive_status``) a match means the session spawned a background task
        that is still running. Deterministic and stateless — recomputed from the live
        process tree each refresh, so it clears the instant the task exits. *ignore*
        as in :meth:`has_subagent` (a ``!``-prefixed command's own Bash-tool shell
        carries the signature too, and must not veto itself).
        """
        return self._descendant_matches(pid, lambda cmd: _BG_SHELL_SIGNATURE in cmd, ignore)

    @staticmethod
    def _descendant_matches(
        pid: int, matches: Callable[[str], bool], ignore: frozenset[int]
    ) -> bool:
        """True when any descendant of *pid* (skipping *ignore* subtrees) satisfies *matches*."""
        if pid <= 0:
            return False
        children = _children_map()
        seen: set[int] = set(ignore)
        stack = [pid]
        while stack:
            parent = stack.pop()
            for child_pid, command in children.get(parent, []):
                if child_pid in seen:
                    continue
                seen.add(child_pid)
                if matches(command):
                    return True
                stack.append(child_pid)
        return False

    def session_effort(self, pid: int) -> str | None:
        """The ``--effort <level>`` the LIVE ``claude`` process *pid* was launched with.

        Reads *pid*'s command line from the same cached ``ps`` scan
        ``has_subagent``/``has_background_task`` use (:func:`_command_for_pid`) and parses a
        ``--effort <level>`` / ``--effort=<level>`` argument, validated against
        :data:`command_center.models.EFFORT_LEVELS`. ``None`` when the process is gone, the
        flag is absent, or its value is not a known level. Like :meth:`observed_model` this
        is NOT part of the ``Adapter`` protocol — callers getattr-probe it defensively.
        """
        if pid <= 0:
            return None
        command = _command_for_pid(pid)
        if not command:
            return None
        match = _EFFORT_ARG_RE.search(command)
        if match is None:
            return None
        level = match.group(1)
        return level if level in EFFORT_LEVELS else None

    def todos(self, session_id: str, config_dir: str | None = None) -> list[tuple[str, str]]:
        """Claude Code's in-session TodoWrite list as ``[(status, subject)]``.

        Stored at ``<account>/tasks/<session-id>/<n>.json``. *config_dir* selects the
        account's ``tasks/`` dir (the default account's when ``None``); an account that
        never wrote a task list (e.g. ``~/.claude-work/tasks`` may not exist yet) simply
        yields ``[]``. Empty unless the agent actually used the TodoWrite tool this turn.
        """
        base = Path(config_dir).expanduser() if config_dir else self.home
        task_dir = base / "tasks" / session_id
        if not task_dir.is_dir():
            return []
        files = list(task_dir.glob("*.json"))
        files.sort(key=lambda p: int(p.stem) if p.stem.isdigit() else 1_000_000)
        todos: list[tuple[str, str]] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            subject = data.get("subject") or data.get("content") or ""
            if subject:
                todos.append((str(data.get("status", "pending")), str(subject)))
        return todos

    def probe(self) -> bool:
        if not self.sessions_dir.is_dir():
            return False
        for path in self.sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if "sessionId" in data and "pid" in data:
                return True
        return False
