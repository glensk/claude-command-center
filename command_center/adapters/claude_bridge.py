"""Claude Code transcript reader for the voice bridge (``ccc inspect/send/answer/delivery``).

The second module of the adapter package that knows Claude Code's undocumented JSONL
schema (``adapters/claude.py`` is the first): everything the bridge needs from a
transcript is derived here, so a schema change breaks at most this package.

Facts it relies on (Claude Code 2.1.29x, verified by the S-INSPECT fixtures in
``tests/fixtures/inspect/``):

- one record per content block; the blocks of one API message share ``message.id``;
- a turn ends with an assistant ``stop_reason`` of ``end_turn`` (or ``stop_sequence`` /
  ``max_tokens``) followed by ``system/stop_hook_summary`` + ``system/turn_duration``;
- a prompt typed at an idle prompt is a ``user`` record; one typed while busy is first a
  ``queue-operation/enqueue`` record (``content``, no uuid), later either ``remove`` +
  ``attachment/queued_command`` (absorbed mid-turn) or ``dequeue`` + a plain ``user``
  record; background-task notices are enqueues too (``<task-notification>`` prefix);
- a pending ``AskUserQuestion`` is not always the last record (queue-operations and
  attachments may follow it); its answer is the call's ``tool_result`` with top-level
  ``toolUseResult.answers`` keyed by the full question text (multi-select: labels joined
  by ``", "`` in tick order). The tool_result TEXT has two templates — never parse it.

``inspect_records`` is the port of the Phase-0 reference parser; its output for the four
fixtures is pinned by ``tests/test_inspect_decision.py``.
"""

# pylint: disable=too-many-lines  # one schema reader for the whole bridge, by design

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAST_REPLY_MAX = 1500
LAST_PROMPT_MAX = 500
SUMMARY_MAX = 800

INTERRUPT_MARKERS = ("[Request interrupted by user]", "[Request interrupted by user for tool use]")
END_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})
END_SYSTEM_SUBTYPES = frozenset({"stop_hook_summary", "turn_duration"})
TASK_NOTIFICATION_PREFIX = "<task-notification>"
ASK_TOOL = "AskUserQuestion"

TODO_HEADING_RE = re.compile(r"^\s*#{1,6}\s*To-do list\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^\s*#{1,6}\s")
TODO_DECISION_RE = re.compile(r"^\s*\d+\.\s+You \[decision\]:\s*(.+)$")
OPTION_MARKER_RE = re.compile(r"(?:(?<=\s)|^)([a-z])\)\s+")
RECOMMENDED_RE = re.compile(r"\s*\(recommended\)", re.IGNORECASE)
CONSEQUENCE_SPLIT_RE = re.compile(r"\s+(?:—|–|--?)\s+")
RECOMMEND_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[.?!])\s+)"
    r"(?:I(?:'d| would)? recommend|My (?:suggestion|recommendation)(?: is)?:?)\s+"
    r"(?P<rec>.+?)(?:\s+because\s+(?P<why>.+?))?[.!]?\s*$",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


class TranscriptUnknown(ValueError):
    """The transcript does not parse as the schema this module knows."""


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class Located:
    """One parsed record and its byte span in the file (``start`` inclusive, ``end`` excl.)."""

    start: int
    end: int
    record: dict[str, Any]


def _parse_lines(data: bytes, base: int, *, strict: bool) -> list[Located]:
    """Every complete JSON-object line of *data* (file offset *base*).

    A last line without its newline is a record still being written and is skipped. A
    malformed COMPLETE line raises :class:`TranscriptUnknown` when *strict*, else is
    skipped (the correlation scanners only look for the shapes they know).
    """
    out: list[Located] = []
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl == -1:
            break  # torn tail: not a complete record yet
        line = data[pos:nl]
        start, pos = base + pos, nl + 1
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if strict:
                raise TranscriptUnknown(f"byte {start}: not JSON") from exc
            continue
        if not isinstance(record, dict):
            if strict:
                raise TranscriptUnknown(f"byte {start}: not an object")
            continue
        out.append(Located(start, base + nl + 1, record))
    return out


def load_records(path: Path) -> list[dict[str, Any]]:
    """Every record of *path*; raises :class:`TranscriptUnknown` on a malformed line.

    A torn last line (no trailing newline: Claude Code is mid-write) is ignored, so a
    live transcript never reads as malformed. ``OSError`` propagates.
    """
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        # A complete file always ends with a newline; a final fragment that already
        # parses is a whole record whose newline has not landed yet — keep it.
        tail_start = data.rfind(b"\n") + 1
        try:
            json.loads(data[tail_start:])
            data += b"\n"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return [loc.record for loc in _parse_lines(data, 0, strict=True)]


_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def project_dir_name(cwd: str) -> str:
    """Claude Code's ``projects/`` folder for *cwd*: every non-alphanumeric char → ``-``.

    ``/private/tmp/bridge-scratch`` → ``-private-tmp-bridge-scratch`` (spike S-SEND).
    """
    return _NON_ALNUM_RE.sub("-", cwd)


def expected_transcript_path(config_dir: str, cwd: str, session_id: str) -> Path:
    """Where Claude Code WILL write *session_id*'s transcript under account *config_dir*.

    A fresh session has no transcript until its first prompt (spike S-SEND); this is the
    path the file appears at. An empty *config_dir* means the default account.
    """
    if config_dir:
        home = Path(config_dir).expanduser()
    else:
        from ..config import claude_home  # pylint: disable=import-outside-toplevel

        home = claude_home()
    return home / "projects" / project_dir_name(cwd) / f"{session_id}.jsonl"


@dataclass(frozen=True)
class Anchor:
    """A transcript's identity + size before an action; scans read only what follows.

    ``inode == 0`` is the anchor of a transcript that does not exist yet (a fresh
    session before its first prompt): any file that appears there is accepted.
    """

    path: Path
    inode: int
    size: int

    @classmethod
    def take(cls, path: Path) -> Anchor:
        st = path.stat()
        return cls(path, st.st_ino, st.st_size)

    @classmethod
    def take_or_fresh(cls, path: Path) -> Anchor:
        """:meth:`take`, or ``(inode 0, size 0)`` when *path* does not exist yet."""
        try:
            return cls.take(path)
        except FileNotFoundError:
            return cls(path, 0, 0)


class TranscriptReplaced(RuntimeError):
    """The transcript's inode changed since the anchor was taken."""


def read_appended(path: Path, inode: int, offset: int) -> list[Located]:
    """Complete records written at or after byte *offset* (inode must still match).

    With ``inode == 0`` (a fresh-session anchor) every inode is valid and a file that has
    not appeared yet reads as no records.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        if inode == 0:
            return []
        raise
    if inode and st.st_ino != inode:
        raise TranscriptReplaced(str(path))
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    return _parse_lines(data, offset, strict=False)


# --------------------------------------------------------------------------- text


def normalise(text: str) -> str:
    """NFC, CRLF/CR → LF, trailing spaces per line dropped, outer whitespace stripped."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def text_sha(text: str) -> str:
    """sha256 hex of :func:`normalise` (*text*) — the delivery correlation key."""
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def _payload_text(content: Any) -> str | None:
    """A message content (str or block list) as plain text; None if it holds no text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts) if parts else None
    return None


#: Correlation kinds of a delivered prompt.
KIND_USER = "user"
KIND_ENQUEUE = "queue-operation/enqueue"
KIND_QUEUED = "attachment/queued_command"


def prompt_record(record: dict[str, Any]) -> tuple[str, str] | None:
    """``(kind, text)`` for the record shapes a delivered prompt can take, else None.

    ``user`` (typed at an idle prompt), ``queue-operation/enqueue`` (typed while busy),
    ``attachment/queued_command`` (the queue draining it). Background-task notices
    (``<task-notification>``) and peer / task-notification queued commands are never a
    delivered prompt.
    """
    rtype = record.get("type")
    if record.get("isSidechain"):
        return None
    text: str | None = None
    kind = ""
    if rtype == "user" and not record.get("isMeta"):
        msg = record.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return None
        text, kind = _payload_text(content), KIND_USER
    elif rtype == "queue-operation" and record.get("operation") == "enqueue":
        text, kind = str(record.get("content") or ""), KIND_ENQUEUE
    elif rtype == "attachment":
        att = record.get("attachment")
        if (
            isinstance(att, dict)
            and att.get("type") == "queued_command"
            and att.get("commandMode") in (None, "prompt")
        ):
            origin = att.get("origin")
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                return None
            text, kind = _payload_text(att.get("prompt")), KIND_QUEUED
    if text is None or text.lstrip().startswith(TASK_NOTIFICATION_PREFIX):
        return None
    return kind, text


def is_turn_end_marker(record: dict[str, Any]) -> bool:
    """A ``system`` record Claude Code writes when a turn ended."""
    return record.get("type") == "system" and record.get("subtype") in END_SYSTEM_SUBTYPES


def is_end_stop(record: dict[str, Any]) -> bool:
    """An assistant record whose ``stop_reason`` ends the turn."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return False
    message = record.get("message")
    return isinstance(message, dict) and message.get("stop_reason") in END_STOP_REASONS


def ask_tool_uses(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``AskUserQuestion`` ``tool_use`` blocks of a main-chain assistant record."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return []
    return [
        b
        for b in _blocks(record)
        if b.get("type") == "tool_use" and b.get("name") == ASK_TOOL and b.get("id")
    ]


def tool_result_ids(record: dict[str, Any]) -> set[str]:
    """The ``tool_use_id`` of every ``tool_result`` block in a user record."""
    if record.get("type") != "user":
        return set()
    return {
        str(b.get("tool_use_id"))
        for b in _blocks(record)
        if b.get("type") == "tool_result" and b.get("tool_use_id")
    }


@dataclass(frozen=True)
class AskResult:
    """The answer to one AskUserQuestion call."""

    is_error: bool
    answers: dict[str, str]


def ask_result(record: dict[str, Any], tool_use_id: str) -> AskResult | None:
    """The answer in *record* to the call *tool_use_id*, else None.

    Read from the top-level ``toolUseResult.answers`` (never from the tool_result text).
    A cancelled picker yields ``is_error=True`` and no answers.
    """
    if record.get("type") != "user" or record.get("isSidechain"):
        return None
    block = next(
        (
            b
            for b in _blocks(record)
            if b.get("type") == "tool_result" and str(b.get("tool_use_id")) == tool_use_id
        ),
        None,
    )
    if block is None:
        return None
    result = record.get("toolUseResult")
    answers = result.get("answers") if isinstance(result, dict) else None
    clean = {str(k): str(v) for k, v in answers.items()} if isinstance(answers, dict) else {}
    return AskResult(bool(block.get("is_error")) or not clean, clean)


def is_interrupt(record: dict[str, Any]) -> bool:
    """A user record carrying Claude Code's interrupt marker."""
    if record.get("type") != "user":
        return False
    content = _content(record)
    texts = (
        [content]
        if isinstance(content, str)
        else [str(b.get("text") or "") for b in _blocks(record) if b.get("type") == "text"]
    )
    return any(t.strip() in INTERRUPT_MARKERS for t in texts)


# --------------------------------------------------------------------------- inspect scan


def _content(record: dict[str, Any]) -> Any:
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    content = _content(record)
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _is_main(record: dict[str, Any]) -> bool:
    return not record.get("isSidechain") and not record.get("isMeta")


def _typed_prompt(record: dict[str, Any]) -> str | None:
    """Text of a prompt typed at the idle prompt, or None."""
    if record.get("type") != "user" or not _is_main(record):
        return None
    if record.get("isCompactSummary"):
        return None
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = _content(record)
    if isinstance(content, str):
        text = content
    else:
        blocks = _blocks(record)
        if not blocks or any(b.get("type") == "tool_result" for b in blocks):
            return None
        text = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
    text = text.strip()
    if not text or text.startswith(TASK_NOTIFICATION_PREFIX) or text in INTERRUPT_MARKERS:
        return None
    return text


def _queued_prompt(record: dict[str, Any]) -> str | None:
    """Text of a prompt queued while busy and absorbed into the turn, or None."""
    if record.get("type") != "attachment" or not _is_main(record):
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    if attachment.get("commandMode") != "prompt":
        return None
    origin = attachment.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    prompt = attachment.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(
            str(b.get("text") or "")
            for b in prompt
            if isinstance(b, dict) and b.get("type") == "text"
        )
    text = str(prompt or "").strip()
    return text or None


def _answer(record: dict[str, Any]) -> str | None:
    """An AskUserQuestion answer rendered as ``"<question>"="<answer>"`` pairs, or None."""
    if record.get("type") != "user" or not _is_main(record):
        return None
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    questions, answers = result.get("questions"), result.get("answers")
    if not isinstance(questions, list) or not isinstance(answers, dict):
        return None
    pairs = [
        f'"{q.get("question")}"="{answers[q.get("question")]}"'
        for q in questions
        if isinstance(q, dict) and q.get("question") in answers
    ]
    return ", ".join(pairs) or None


@dataclass
class PendingAsk:
    """An AskUserQuestion call without a tool_result."""

    tool_use_id: str
    questions: list[dict[str, Any]]


@dataclass
class Scan:
    """What one pass over the transcript found."""

    last_prompt: str | None = None
    reply_parts: list[str] = field(default_factory=list)
    reply_anchor: str | None = None  # uuid of the record holding the latest reply text
    todo_anchor: str | None = None  # uuid of the record holding the to-do list
    turn_open: bool = False
    pending: list[PendingAsk] = field(default_factory=list)


def _scan_user(s: Scan, record: dict[str, Any], answered: set[str]) -> None:
    """A user record: tool results keep the turn open, an interrupt closes it."""
    for block in _blocks(record):
        if block.get("type") == "tool_result":
            answered.add(str(block.get("tool_use_id")))
            s.turn_open = True
    if is_interrupt(record):
        s.turn_open = False
        s.pending = []


def _scan_assistant(s: Scan, record: dict[str, Any], answered: set[str]) -> None:
    """An assistant record (one content block): reply text, tool calls, end of turn."""
    s.pending = [p for p in s.pending if p.tool_use_id not in answered]
    uuid = str(record.get("uuid"))
    for block in _blocks(record):
        if block.get("type") == "text" and str(block.get("text") or "").strip():
            text = str(block["text"]).strip()
            s.reply_parts.append(text)
            s.reply_anchor = uuid
            if any(TODO_HEADING_RE.match(line) for line in text.splitlines()):
                s.todo_anchor = uuid
        elif block.get("type") == "tool_use":
            if block.get("name") == ASK_TOOL:
                questions = (block.get("input") or {}).get("questions") or []
                s.pending.append(PendingAsk(str(block.get("id")), list(questions)))
            s.turn_open = True
    message = record.get("message")
    if isinstance(message, dict) and message.get("stop_reason") in END_STOP_REASONS:
        s.turn_open = False


def scan(records: list[dict[str, Any]]) -> Scan:
    """One ordered pass: human inputs, reply text, turn open/closed, pending asks."""
    s = Scan()
    answered: set[str] = set()
    for record in records:
        if record.get("isSidechain"):
            continue
        human = _typed_prompt(record) or _queued_prompt(record) or _answer(record)
        if human is not None:
            s.last_prompt = human
            s.reply_parts = []
            s.reply_anchor = s.todo_anchor = None
            s.turn_open = True
            s.pending = []
        rtype = record.get("type")
        if rtype == "user":
            _scan_user(s, record, answered)
        elif rtype == "assistant" and not record.get("isMeta"):
            _scan_assistant(s, record, answered)
        elif is_turn_end_marker(record):
            s.turn_open = False
    last_asst_ids = _last_assistant_tool_ids(records)
    s.pending = [
        p for p in s.pending if p.tool_use_id not in answered and p.tool_use_id in last_asst_ids
    ]
    return s


def _last_assistant_tool_ids(records: list[dict[str, Any]]) -> set[str]:
    """tool_use ids of the last assistant API message (one message = several records)."""
    last_id: str | None = None
    ids: set[str] = set()
    for record in records:
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        message = record.get("message")
        msg_id = message.get("id") if isinstance(message, dict) else None
        if msg_id != last_id:
            last_id, ids = msg_id, set()
        ids.update(str(b.get("id")) for b in _blocks(record) if b.get("type") == "tool_use")
    return ids


# --------------------------------------------------------------------------- decisions


@dataclass
class Question:
    """One decision question with its options and (optional) recommendation.

    ``raw_labels`` are the labels exactly as the tool call carries them (a
    ``(Recommended)`` marker included) — the strings ``toolUseResult.answers`` holds.
    """

    text: str
    multi_select: bool
    options: list[dict[str, str | None]]
    recommendation: str | None = None
    reason: str | None = None
    raw_labels: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"text": self.text, "multi_select": self.multi_select, "options": self.options}


def _strip_recommended(label: str) -> tuple[str, bool]:
    stripped, n = RECOMMENDED_RE.subn("", label)
    return stripped.strip(), n > 0


def ask_questions(pending: PendingAsk) -> list[Question]:
    """Questions of a pending AskUserQuestion call."""
    out: list[Question] = []
    for q in pending.questions:
        if not isinstance(q, dict):
            continue
        options: list[dict[str, str | None]] = []
        raw: list[str] = []
        rec = reason = None
        for opt in q.get("options") or []:
            if not isinstance(opt, dict):
                continue
            raw.append(str(opt.get("label", "")))
            label, recommended = _strip_recommended(str(opt.get("label", "")))
            consequence = str(opt.get("description") or "").strip() or None
            options.append({"label": label, "consequence": consequence})
            if recommended and rec is None:
                rec, reason = label, consequence
        out.append(
            Question(
                text=str(q.get("question", "")).strip(),
                multi_select=bool(q.get("multiSelect")),
                options=options,
                recommendation=rec,
                reason=reason,
                raw_labels=raw,
            )
        )
    return out


def raw_question_texts(pending: PendingAsk) -> list[str]:
    """The questions' text exactly as the call carries it (the ``answers`` keys)."""
    return [str(q.get("question", "")) for q in pending.questions if isinstance(q, dict)]


def _top_level_split(text: str, sep: str) -> list[str]:
    """Split *text* on *sep* outside (), [] and {}."""
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


def _label(text: str) -> str:
    text = text.strip().rstrip(".?!,;:").strip()
    return text[:1].upper() + text[1:] if text else text


def _explicit_options(body: str) -> tuple[str, list[tuple[str, str | None, bool]]] | None:
    """``(question, [(label, consequence, recommended)])`` from ``a) … b) …`` markers."""
    markers = list(OPTION_MARKER_RE.finditer(body))
    if len(markers) < 2 or markers[0].group(1) != "a":
        return None
    letters = [m.group(1) for m in markers]
    if letters != [chr(ord("a") + i) for i in range(len(letters))]:
        return None
    question = body[: markers[0].start()].strip().rstrip(":").strip()
    options: list[tuple[str, str | None, bool]] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
        seg = body[m.end() : end].strip().rstrip(",;").strip()
        seg = re.sub(r"\s+or$", "", seg)
        seg, recommended = _strip_recommended(seg)
        head, *tail = CONSEQUENCE_SPLIT_RE.split(seg, maxsplit=1)
        consequence = tail[0].strip().rstrip(".") if tail else None
        options.append((_label(head), consequence or None, recommended))
    return question, options


def _or_options(question: str) -> list[tuple[str, str | None, bool]]:
    """Options from a top-level ``X or Y`` split of the question sentence (else [])."""
    core = question.strip().rstrip("?").strip()
    parts = _top_level_split(core, " or ")
    if len(parts) < 2:
        return []
    lead = _top_level_split(parts[0], ": ")
    parts[0] = lead[-1]
    out: list[tuple[str, str | None, bool]] = []
    for part in parts:
        label, recommended = _strip_recommended(part.strip().strip(","))
        out.append((_label(label), None, recommended))
    return out


def todo_questions(reply: str) -> list[Question]:
    """Questions from ``You [decision]:`` lines in the last ``## To-do list`` section."""
    lines = reply.splitlines()
    starts = [i for i, line in enumerate(lines) if TODO_HEADING_RE.match(line)]
    if not starts:
        return []
    section: list[str] = []
    for line in lines[starts[-1] + 1 :]:
        if HEADING_RE.match(line):
            break
        section.append(line)
    out: list[Question] = []
    for line in section:
        m = TODO_DECISION_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        rec: str | None = None
        reason: str | None = None
        rm = RECOMMEND_SENTENCE_RE.search(body)
        if rm:
            rec = rm.group("rec").strip()
            reason = rm.group("why").strip() if rm.group("why") else None
            body = body[: rm.start()].strip()
        explicit = _explicit_options(body)
        if explicit is not None:
            question, opts = explicit
        else:
            question, opts = body, _or_options(body)
        marked = [(label, cons) for label, cons, recommended in opts if recommended]
        if marked:
            rec, reason = marked[0]
        out.append(
            Question(
                text=question,
                multi_select=False,
                options=[{"label": label, "consequence": cons} for label, cons, _r in opts],
                recommendation=rec,
                reason=reason,
                raw_labels=[label for label, _c, _r in opts],
            )
        )
    return out


def decision_id(anchor: str, questions: list[Question]) -> str:
    """sha256 over compact JSON ``[anchor, [[text, multi_select, [labels…]]…]]``."""
    payload = [
        anchor,
        [[q.text, q.multi_select, [o["label"] for o in q.options]] for q in questions],
    ]
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _combine(questions: list[Question], attr: str) -> str | None:
    values = [(i, getattr(q, attr)) for i, q in enumerate(questions, 1) if getattr(q, attr)]
    if not values:
        return None
    if len(questions) == 1:
        return str(values[0][1])
    return "; ".join(f"{i}) {v}" for i, v in values)


def recent_summary(reply: str) -> str:
    """Fallback summary: first 3 sentences of *reply* (headings dropped), ≤ 800 chars."""
    text = " ".join(
        line.strip() for line in reply.splitlines() if line.strip() and not HEADING_RE.match(line)
    )
    summary = " ".join(SENTENCE_SPLIT_RE.split(text)[:3]).strip()
    return cap_head(summary, SUMMARY_MAX)


def cap_head(text: str, limit: int) -> str:
    """*text* cut to *limit* chars, head kept (``…`` marks the cut)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cap_tail(text: str, limit: int) -> str:
    """*text* cut to *limit* chars, tail kept (``…`` marks the cut)."""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


@dataclass
class Inspection:
    """Everything ``ccc inspect`` derives from one transcript."""

    transcript_state: str  # idle | busy | waiting
    last_reply: str  # full text of the current turn's reply (uncapped)
    last_prompt: str  # full last human input (uncapped)
    pending: list[PendingAsk]
    questions: list[Question]
    source: str | None
    anchor: str | None

    def decision(self, state: str, summary: str) -> dict[str, Any] | None:
        """The §6 ``decision`` object (``None`` when there is none in *state*)."""
        if self.source == "todo_line" and state != "idle":
            return None
        if not self.questions or not self.source or not self.anchor:
            return None
        return {
            "decision_id": decision_id(self.anchor, self.questions),
            "source": self.source,
            "questions": [q.public() for q in self.questions],
            "recommendation": _combine(self.questions, "recommendation"),
            "recommendation_reason": _combine(self.questions, "reason"),
            "context": {"aim": None, "ticket": None, "recent_summary": summary},
        }


def inspect(records: list[dict[str, Any]]) -> Inspection:
    """Parse *records* into an :class:`Inspection` (state from the transcript alone)."""
    s = scan(records)
    reply = "\n\n".join(s.reply_parts)
    if s.pending:
        state = "waiting"
    elif s.turn_open:
        state = "busy"
    else:
        state = "idle"
    questions: list[Question] = []
    source = anchor = None
    if len(s.pending) == 1:
        questions = ask_questions(s.pending[0])
        source, anchor = "ask_user_question", s.pending[0].tool_use_id
    elif state == "idle" and s.todo_anchor:
        questions, source, anchor = todo_questions(reply), "todo_line", s.todo_anchor
    return Inspection(
        transcript_state=state,
        last_reply=reply,
        last_prompt=s.last_prompt or "",
        pending=s.pending,
        questions=questions,
        source=source,
        anchor=anchor,
    )


def inspect_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The reference ``data`` object of ``ccc inspect -j`` (transcript state, fallback
    summary, no AIM) — what the S-INSPECT fixtures pin."""
    ins = inspect(records)
    return {
        "state": ins.transcript_state,
        "last_reply": cap_tail(ins.last_reply, LAST_REPLY_MAX),
        "last_prompt": cap_head(ins.last_prompt, LAST_PROMPT_MAX),
        "decision": ins.decision(ins.transcript_state, recent_summary(ins.last_reply)),
    }


def iter_records(located: Iterable[Located]) -> list[dict[str, Any]]:
    """The bare records of *located*."""
    return [loc.record for loc in located]
