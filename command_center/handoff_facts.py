#!/usr/bin/env python3
"""Handoff FACTS collector — the code-filled half of a ``continue-session`` handoff (tp#1479).

A handoff must not depend on the outgoing model remembering what some tool already
records. This module collects those facts as TYPED RECORDS ``{kind, key, section,
priority, fields}`` (never markdown; the composer renders them and escapes every free
text) from two sources:

* **ccc itself** — the session row (AIM, sub-goals, lineage), the in-flight subagents
  (verbatim spec + final result, copied from the subagent transcripts), background shells
  classified ``declared-restartable`` / ``evidence-only`` (D15), CronCreate jobs not
  deleted, keep-warm timers (running and stopped), armed ``ccc await`` groups;
* **configured providers** — config key ``handoff_facts`` (default ``[]``, inert): a list
  of tables ``{argv, timeout_s, optional, name}`` run like ``handoff_hooks`` (argv-only,
  own process group, killed on timeout, stdout capped at 256 KB). Everything
  user-specific (tp items, commit ledger, schedules …) comes from there.

Two phases (D8): ``capture`` runs BEFORE the drain and writes ``capture.json`` so
``finalize`` (at ``start``, after the drain) still knows what was running; ``finalize``
refreshes the recorded transcripts, attaches final results, re-runs the providers and
freezes a SNAPSHOT ``<app_home>/handoffs/facts/<source_sid>/<snap12>.json`` whose id is
the sha256 of its canonical JSON (D2). Every file persisted here — snapshot, capture,
agent spec, agent result — goes through ``mirror_scrub_cmd`` first and is written 0600;
a withheld scrub raises :class:`FactsError` and nothing is written (fail closed).

A failed provider NEVER blocks the relay (D4): its records become one ``warning`` fact.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=too-many-lines,protected-access  # inflight's transcript readers are shared

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .handoff_doc import escape_headings

if TYPE_CHECKING:
    from .adapters.claude import ClaudeAdapter
    from .continue_session import Source
    from .store import Store

VERSION = 1

#: Kinds a PROVIDER may return (the external facts provider's closed set).
PROVIDER_KINDS = frozenset(
    {
        "tp_item",
        "tp_filed",
        "question",
        "you_line",
        "wait",
        "schedule",
        "schedule_unknown",
        "commit",
        "owned_file",
        "worktree",
        "plan",
        "headless_run",
        "todo_list",
        "debate_pause",
        "keepwarm",
        "relay_latch",
        "warning",
    }
)
#: Kinds ccc itself emits.
CORE_KINDS = frozenset(
    {
        "session",
        "subgoal",
        "lineage",
        "transcript",
        "agent",
        "shell",
        "keepwarm_timer",
        "cron",
        "await",
    }
)
PHASES = ("capture", "finalize")
PRIORITY_MIN, PRIORITY_MAX = 0, 4

PROVIDER_STDOUT_CAP = 256 * 1024
PROVIDER_TIMEOUT_DEFAULT_S = 10.0
PROVIDER_TIMEOUT_MAX_S = 120.0
RESULT_INLINE_CAP = 3 * 1024  # D7: the agent's last result text inline; the rest in a file

DECLARED = "declared-restartable"
EVIDENCE_ONLY = "evidence-only"
PLACEHOLDERS = ("{session}", "{cwd}", "{transcript}", "{config_dir}", "{phase}", "{tp_ids}")

_TOP_KEYS = frozenset({"version", "provider", "phase", "facts", "repos", "warnings", "elapsed_s"})
_FACT_KEYS = frozenset({"kind", "key", "section", "priority", "fields"})
_MARKDOWN_FIELD_RE = re.compile(r"^(markdown|md|body_md|text_md|rendered|.*_markdown|.*_md)$", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_RESULT_RE = re.compile(r"<result>(.*?)</result>", re.DOTALL)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)

Record = dict[str, Any]


class FactsError(Exception):
    """Collection must stop (a scrub withheld, a bad session id); the message says why."""


# ----------------------------------------------------------------------------- records


def rec(kind: str, key: str, section: str, priority: int, **fields: Any) -> Record:
    """One typed record."""
    return {"kind": kind, "key": key, "section": section, "priority": priority, "fields": fields}


def _warning(source: str, message: str, section: str = "Open items and questions") -> Record:
    return rec("warning", f"warning:{source}", section, 0, source=source, message=message)


# ------------------------------------------------------------------------------ scrub


class Scrubber:
    """``mirror_scrub_cmd`` resolved once; :meth:`text` raises :class:`FactsError` if withheld."""

    def __init__(self, policy: str | None = None) -> None:
        self._policy = policy
        self._resolved: Any = None

    def text(self, content: str) -> str:
        """*content* with credential shapes scrubbed, or :class:`FactsError` (fail closed)."""
        from . import config, scrub

        if self._resolved is None:
            policy = self._policy
            if policy is None:
                policy = config.load_config().mirror_scrub_cmd
            self._resolved = scrub.resolve_scrubber(policy)
        if self._resolved.scrubber is None:
            raise FactsError(f"credential scrub withheld: {self._resolved.reason}")
        result = scrub.scrub(self._resolved.scrubber, content)
        if not result.ok:
            raise FactsError(f"credential scrub withheld: {result.reason}")
        return result.text


def write_private(path: Path, text: str) -> None:
    """Atomically write *text* to *path* with mode 0600 (directories 0700)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def facts_dir(source_sid: str) -> Path:
    """``<app_home>/handoffs/facts/<source_sid>/`` (the sid is validated, never a path)."""
    from . import config

    if not _SID_RE.match(source_sid or ""):
        raise FactsError(f"not a valid session id: {source_sid!r}")
    return config.app_home() / "handoffs" / "facts" / source_sid


def _write_scrubbed(path: Path, text: str, scrubber: Scrubber) -> str:
    """Scrub *text*, write it 0600 to *path*; the scrubbed text (nothing written on failure)."""
    clean = scrubber.text(text)
    write_private(path, clean)
    return clean


# -------------------------------------------------------------------------- providers


@dataclass
class ProviderSpec:
    """One validated ``handoff_facts`` entry."""

    name: str
    argv: list[str]
    timeout_s: float = PROVIDER_TIMEOUT_DEFAULT_S
    optional: bool = False


@dataclass
class ProviderRun:
    """The outcome of one provider."""

    name: str
    ok: bool
    facts: list[Record] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str = ""


def parse_specs(entries: object) -> tuple[list[ProviderSpec], list[Record]]:
    """``(specs, warning facts)`` from the config value; a bad entry is a warning, not a crash."""
    specs: list[ProviderSpec] = []
    bad: list[Record] = []
    if not entries:
        return specs, bad
    if not isinstance(entries, list):
        return specs, [_warning("handoff_facts", "⚠ handoff_facts config is not a list")]
    for index, entry in enumerate(entries, 1):
        argv = entry.get("argv") if isinstance(entry, dict) else None
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
        ):
            bad.append(
                _warning(
                    f"handoff_facts[{index}]",
                    f"⚠ handoff_facts entry {index} has no argv (a non-empty list of strings)",
                )
            )
            continue
        raw_timeout = entry.get("timeout_s", PROVIDER_TIMEOUT_DEFAULT_S)
        timeout = (
            float(raw_timeout)
            if isinstance(raw_timeout, int | float) and not isinstance(raw_timeout, bool)
            else PROVIDER_TIMEOUT_DEFAULT_S
        )
        timeout = min(max(timeout, 1.0), PROVIDER_TIMEOUT_MAX_S)
        name = str(entry.get("name") or "").strip() or _default_name(argv)
        specs.append(ProviderSpec(name, argv, timeout, bool(entry.get("optional", False))))
    return specs, bad


def _default_name(argv: list[str]) -> str:
    base = os.path.basename(argv[0])
    flags = [argv[i + 1] for i, part in enumerate(argv[:-1]) if part in ("-P", "--part")]
    return f"{base}:{flags[0]}" if flags else base


def render_argv(
    spec: ProviderSpec, values: dict[str, str], tp_ids: list[int] | None = None
) -> list[str]:
    """The argv with placeholders replaced (plain ``str.replace``, no ``format``).

    ``{tp_ids}`` becomes ``1479,1480`` (the held ids the required part reported, so the
    optional part does not pay ``tp held`` twice). With no ids known the whole argument
    is dropped together with a preceding ``-x`` style flag, so the provider asks itself.
    """
    out: list[str] = []
    for part in spec.argv:
        if part == "{tp_ids}":
            if tp_ids:
                out.append(",".join(str(i) for i in tp_ids))
            elif out and out[-1].startswith("-"):
                out.pop()
            continue
        for key, value in values.items():
            part = part.replace(key, value)
        out.append(part)
    return out


def _run_provider(argv: list[str], timeout: float) -> bytes:
    """Run *argv* (no shell, own process group, bounded); stdout bytes or FactsError."""
    from . import continue_session

    try:
        returncode, stdout, _stderr = continue_session.run_bounded(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise FactsError(f"timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise FactsError(f"could not run ({exc.strerror or exc})") from exc
    if returncode != 0:  # stderr is deliberately not echoed (never logged raw)
        raise FactsError(f"exit {returncode}")
    if len(stdout) > PROVIDER_STDOUT_CAP:
        raise FactsError(f"stdout exceeds {PROVIDER_STDOUT_CAP} bytes")
    return stdout


def validate_provider_doc(doc: object) -> tuple[list[Record], list[str], list[str]]:
    """``(facts, repos, warnings)`` of a provider document, or :class:`FactsError`.

    Contract: ``{version: 1, provider, facts: [{kind, key, section, priority, fields}],
    repos: [abs paths], warnings: [str]}``. Unknown top-level or fact keys, an unknown
    ``kind``, a duplicate ``key``, a ``section`` that is not a handoff section, a bad
    priority, control characters, or a markdown-typed field make the provider a failure.
    """
    from .handoff_doc import REQUIRED_SECTIONS

    if not isinstance(doc, dict):
        raise FactsError("answer is not a JSON object")
    unknown = sorted(set(doc) - _TOP_KEYS)
    if unknown:
        raise FactsError(f"unknown top-level key(s): {', '.join(unknown)}")
    if doc.get("version") != VERSION:
        raise FactsError(f"unsupported contract version {doc.get('version')!r}")
    raw_facts = doc.get("facts")
    if not isinstance(raw_facts, list):
        raise FactsError('"facts" is not a list')
    facts: list[Record] = []
    seen: set[str] = set()
    for index, fact in enumerate(raw_facts):
        facts.append(_validate_fact(fact, index, seen, REQUIRED_SECTIONS))
    repos = doc.get("repos", [])
    warns = doc.get("warnings", [])
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise FactsError('"repos" is not a list of strings')
    if not isinstance(warns, list) or not all(isinstance(w, str) for w in warns):
        raise FactsError('"warnings" is not a list of strings')
    return facts, [str(r) for r in repos], [_CONTROL_RE.sub(" ", w) for w in warns]


def _validate_fact(fact: object, index: int, seen: set[str], sections: tuple[str, ...]) -> Record:
    where = f"fact {index}"
    if not isinstance(fact, dict) or set(fact) != _FACT_KEYS:
        raise FactsError(f"{where}: keys must be exactly {sorted(_FACT_KEYS)}")
    kind, key = fact["kind"], fact["key"]
    if kind not in PROVIDER_KINDS:
        raise FactsError(f"{where}: unknown kind {kind!r}")
    if not isinstance(key, str) or not key or key in seen:
        raise FactsError(f"{where}: key is empty, not a string or a duplicate ({key!r})")
    seen.add(key)
    if fact["section"] not in sections:
        raise FactsError(f"{where}: unknown section {fact['section']!r}")
    priority = fact["priority"]
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not PRIORITY_MIN <= priority <= PRIORITY_MAX
    ):
        raise FactsError(f"{where}: priority must be an int {PRIORITY_MIN}..{PRIORITY_MAX}")
    fields = fact["fields"]
    if not isinstance(fields, dict):
        raise FactsError(f"{where}: fields is not an object")
    _check_fields(fields, where)
    return {k: fact[k] for k in ("kind", "key", "section", "priority", "fields")}


def _check_fields(value: object, where: str) -> None:
    """No markdown-typed field names and no control characters, at any depth."""
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            raise FactsError(f"{where}: control characters in a field value")
    elif isinstance(value, dict):
        for name, inner in value.items():
            if not isinstance(name, str) or _MARKDOWN_FIELD_RE.match(name):
                raise FactsError(f"{where}: markdown field {name!r} is not allowed")
            _check_fields(inner, where)
    elif isinstance(value, list):
        for inner in value:
            _check_fields(inner, where)


def run_provider(
    spec: ProviderSpec, values: dict[str, str], tp_ids: list[int] | None = None
) -> ProviderRun:
    """Run one provider; every failure becomes a ``warning`` fact (D4), never an exception."""
    argv = render_argv(spec, values, tp_ids)
    started = time.monotonic()
    try:
        stdout = _run_provider(argv, spec.timeout_s)
        try:
            doc = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FactsError("stdout is not JSON") from exc
        facts, repos, warns = validate_provider_doc(doc)
    except FactsError as exc:
        hand = shlex.join(argv)
        text = f"⚠ facts provider {spec.name} failed: {exc} — run {hand} by hand"
        return ProviderRun(
            spec.name,
            False,
            [_warning(f"provider:{spec.name}", text)],
            [],
            [text],
            round(time.monotonic() - started, 2),
            str(exc),
        )
    return ProviderRun(spec.name, True, facts, repos, warns, round(time.monotonic() - started, 2))


def run_providers(
    specs: list[ProviderSpec], values: dict[str, str]
) -> tuple[list[ProviderRun], list[Record]]:
    """Run *specs* in order; held tp ids from earlier ``tp_item`` records feed ``{tp_ids}``.

    Returns the runs and the merged facts, keys made unique across providers.
    """
    runs: list[ProviderRun] = []
    merged: list[Record] = []
    tp_ids: list[int] = []
    taken: set[str] = set()
    for spec in specs:
        run = run_provider(spec, values, tp_ids)
        runs.append(run)
        for fact in run.facts:
            if fact["key"] in taken:
                suffix = 2
                while f"{fact['key']}#{suffix}" in taken:
                    suffix += 1
                fact = {**fact, "key": f"{fact['key']}#{suffix}"}
            taken.add(fact["key"])
            merged.append(fact)
            if fact["kind"] == "tp_item":
                tid = fact["fields"].get("tp_id")
                if isinstance(tid, int) and tid not in tp_ids:
                    tp_ids.append(tid)
    return runs, merged


# ---------------------------------------------------------- transcript-derived facts


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _record_text(record: dict[str, Any]) -> str:
    """All text a record carries (string content, text blocks, queue content, attachment)."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        parts += [str(b.get("text") or "") for b in content if isinstance(b, dict)]
    if isinstance(record.get("content"), str):
        parts.append(record["content"])
    attachment = record.get("attachment")
    if isinstance(attachment, dict) and isinstance(attachment.get("prompt"), str):
        parts.append(attachment["prompt"])
    return "\n".join(p for p in parts if p)


def _notifications(records: list[dict[str, Any]]) -> dict[str, str]:
    """``{task id: the finishing notification's result/summary text}`` (the last one wins)."""
    found: dict[str, str] = {}
    for record in records:
        text = _record_text(record)
        if not text.lstrip().startswith("<task-notification>"):
            continue
        ids = _TASK_ID_RE.findall(text)
        body = _RESULT_RE.search(text) or _SUMMARY_RE.search(text)
        for task_id in ids:
            found[task_id] = (body.group(1) if body else "").strip()
    return found


def _first_user_text(jsonl: Path) -> str:
    """The verbatim spec: the first record of a subagent transcript (``""`` if unreadable)."""
    try:
        with jsonl.open("r", encoding="utf-8") as handle:
            first = handle.readline()
        record = json.loads(first)
    except (OSError, ValueError):
        return ""
    if not isinstance(record, dict) or record.get("type") != "user":
        return ""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("text")
        )
    return ""


_STUB_RE = re.compile(r"delivered to you as a message from|SubagentHandback call", re.I)


def _handback_text(jsonl: Path) -> str:
    """The input of the agent's LAST ``SubagentHandback`` tool_use (its real report)."""
    from . import inflight

    last = ""
    for record in inflight._transcript_records(jsonl):
        for block in _blocks(record):
            if block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                params = block.get("input")
                text = params.get("message") if isinstance(params, dict) else None
                if isinstance(text, str) and text.strip():
                    last = text
    return last


def _last_assistant_text(jsonl: Path) -> str:
    from . import inflight

    last = ""
    for record in inflight._transcript_records(jsonl):
        if record.get("type") == "assistant":
            text = "\n".join(
                str(b.get("text") or "")
                for b in _blocks(record)
                if b.get("type") == "text" and b.get("text")
            )
            if text:
                last = text
    return last


def _bash_commands(records: list[dict[str, Any]]) -> dict[str, str]:
    """``{tool_use id: command}`` of every Bash call."""
    out: dict[str, str] = {}
    for record in records:
        for block in _blocks(record):
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                params = block.get("input")
                if isinstance(params, dict) and isinstance(block.get("id"), str):
                    out[block["id"]] = str(params.get("command") or "")
    return out


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def declarations(commands: dict[str, str]) -> dict[str, str]:
    """``{key: consumer}`` for every ``cc-bg-keep[.sh] <key> <consumer>`` call (mirrors the
    keep-warm guard's ``declarations``: a malformed command declares nothing)."""
    out: dict[str, str] = {}
    for command in commands.values():
        if "cc-bg-keep" not in command:
            continue
        toks = _tokens(command)
        for idx, tok in enumerate(toks):
            if not Path(tok).name.startswith("cc-bg-keep"):
                continue
            args = toks[idx + 1 : idx + 3]
            if len(args) == 2 and all(
                a and not a.startswith("-") and not set(a) <= set("();<>|&") for a in args
            ):
                out[args[0]] = args[1]
    return out


def classify_shell(command: str, keys: list[str], declared: dict[str, str]) -> tuple[str, str]:
    """``(classification, reason)`` for one pending shell (D15): never inferred.

    ``declared-restartable``: a ``cc-bg-keep`` consumer declaration under one of its
    *keys* (tool_use id, task id, description), or the allowlist — the keep-warm timer
    (``cc-keepwarm.sh``), ``codex-review.py -w`` and ``tp drive N …``. Everything else is
    ``evidence-only``: re-running a non-idempotent command can corrupt state.
    """
    for key in keys:
        if key and key in declared:
            return DECLARED, f"cc-bg-keep consumer: {declared[key]}"
    toks = [Path(t).name if "/" in t else t for t in _tokens(command)]
    if "cc-keepwarm.sh" in toks:
        return DECLARED, "allowlist: keep-warm timer"
    if "codex-review.py" in toks and "-w" in toks:
        return DECLARED, "allowlist: codex-review.py -w"
    for idx, tok in enumerate(toks[:-1]):
        if tok in ("tp", "tp.py") and toks[idx + 1] == "drive":
            return (
                DECLARED,
                "allowlist: tp drive (restart only after `tp active N -j` shows no lock holder)",
            )
    return EVIDENCE_ONLY, "no cc-bg-keep consumer declaration and not on the allowlist"


def _cron_facts(records: list[dict[str, Any]]) -> list[Record]:
    """CronCreate jobs of the transcript that were not CronDelete'd."""
    created: dict[str, Record] = {}
    uses: dict[str, dict[str, Any]] = {}
    deleted: set[str] = set()
    for record in records:
        for block in _blocks(record):
            if block.get("type") == "tool_use" and block.get("name") == "CronCreate":
                params = block.get("input")
                uses[str(block.get("id"))] = params if isinstance(params, dict) else {}
            elif block.get("type") == "tool_use" and block.get("name") == "CronDelete":
                params = block.get("input")
                if isinstance(params, dict) and params.get("id"):
                    deleted.add(str(params["id"]))
        result = record.get("toolUseResult")
        if (
            isinstance(result, dict)
            and isinstance(result.get("id"), str)
            and "humanSchedule" in result
        ):
            use_id = next(
                (
                    str(b.get("tool_use_id"))
                    for b in _blocks(record)
                    if b.get("type") == "tool_result"
                ),
                "",
            )
            params = uses.get(use_id, {})
            created[result["id"]] = rec(
                "cron",
                f"core:cron:{result['id']}",
                "Background restarts",
                1,
                cron_id=result["id"],
                schedule=str(result.get("humanSchedule") or params.get("cron") or ""),
                recurring=bool(result.get("recurring")),
                durable=bool(result.get("durable")),
                prompt=str(params.get("prompt") or ""),
                note="session-only: re-create with CronCreate in the successor"
                + ("" if result.get("recurring") else " (a one-shot may already have fired)"),
            )
    return [fact for cid, fact in created.items() if cid not in deleted]


def _stopped_timers(records: list[dict[str, Any]], pending_timers: set[str]) -> list[Record]:
    """Keep-warm timers stopped with ``TaskStop`` (never a restart) and the running ones."""
    from .adapters.claude import is_keepwarm_command

    commands = _bash_commands(records)
    timer_uses = {u for u, c in commands.items() if is_keepwarm_command(c)}
    task_for_use: dict[str, str] = {}
    stops: set[str] = set()
    for record in records:
        result = record.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("backgroundTaskId"), str):
            for block in _blocks(record):
                if block.get("type") == "tool_result":
                    task_for_use[str(block.get("tool_use_id"))] = result["backgroundTaskId"]
        for block in _blocks(record):
            if block.get("type") == "tool_use" and block.get("name") == "TaskStop":
                params = block.get("input")
                if isinstance(params, dict) and params.get("task_id"):
                    stops.add(str(params["task_id"]))
    out: list[Record] = []
    for use_id in sorted(timer_uses):
        task = task_for_use.get(use_id, "")
        if not task:
            continue
        state = "running" if task in pending_timers else ("stopped" if task in stops else "ended")
        if state == "ended":
            continue
        out.append(
            rec(
                "keepwarm_timer",
                f"core:keepwarm:{task}",
                "Background restarts",
                2,
                task_id=task,
                state=state,
                command=commands[use_id],
                note=(
                    "stopped on purpose: do NOT restart it"
                    if state == "stopped"
                    else "re-arm with the same -u in the successor"
                ),
            )
        )
    return out


# -------------------------------------------------------------------------- core facts


def _spec_for(  # pylint: disable=too-many-locals
    agent_id: str,
    transcript: Path | None,
    records: list[dict[str, Any]],
    recorded_jsonl: str,
    notes: dict[str, str],
) -> dict[str, Any]:
    """Everything the successor needs to relaunch *agent_id*; model is optional (tool_use only)."""
    from . import inflight

    launches, uses = inflight._launch_details(records)
    launch = launches.get(agent_id)
    params = uses.get(launch.tool_use_id, {}) if launch else {}
    meta: dict[str, Any] = {}
    jsonl_path = recorded_jsonl
    if transcript is not None:
        meta, found = inflight._subagent_meta(transcript, agent_id)
        jsonl_path = recorded_jsonl or found
    jsonl = Path(jsonl_path) if jsonl_path and Path(jsonl_path).exists() else None
    spec = (_first_user_text(jsonl) if jsonl else "") or str(params.get("prompt") or "")
    finished = agent_id in notes
    # the real report is the SubagentHandback input; the notification only carries a stub
    # for such agents ("delivered to you as a message …") which is never stored as a result
    result = _handback_text(jsonl) if jsonl is not None else ""
    if not result:
        note = notes.get(agent_id, "")
        result = "" if _STUB_RE.search(note) else note
    if not result and jsonl is not None:
        result = _last_assistant_text(jsonl)
    return {
        "agent_id": agent_id,
        "agent_type": str(meta.get("agentType") or params.get("subagent_type") or ""),
        "description": str(meta.get("description") or params.get("description") or ""),
        "cwd": launch.cwd if launch else "",
        "model": str(params.get("model") or ""),  # meta.json has none
        "isolation": str(params.get("isolation") or meta.get("isolation") or ""),
        "transcript": str(jsonl) if jsonl else "",
        "spec": spec,
        "result": result,
        "finished": finished,
    }


def agent_fact(
    info: dict[str, Any], source_sid: str, scrubber: Scrubber, *, persist: bool = True
) -> Record:
    """The ``agent`` record of *info*; spec and result copied to 0600 scrubbed files (D7)."""
    agent_id = info["agent_id"]
    base = facts_dir(source_sid) / "agents"
    spec_path = result_path = ""
    spec_sha = ""
    inline = ""
    truncated = False
    spec = str(info["spec"])
    if spec:
        path = base / f"{agent_id}.spec.md"
        clean = scrubber.text(spec) if not persist else _write_scrubbed(path, spec, scrubber)
        spec_path, spec_sha = str(path), hashlib.sha256(clean.encode("utf-8")).hexdigest()
    result = str(info["result"])
    if result:
        path = base / f"{agent_id}.result.md"
        clean = scrubber.text(result) if not persist else _write_scrubbed(path, result, scrubber)
        raw = clean.encode("utf-8")
        truncated = len(raw) > RESULT_INLINE_CAP
        inline = raw[:RESULT_INLINE_CAP].decode("utf-8", "ignore")
        result_path = str(path)
    return rec(
        "agent",
        f"core:agent:{agent_id}",
        "Subagent continuations",
        2 if info["finished"] else 0,  # running agents are the continuations: sort first
        agent_id=agent_id,
        agent_type=info["agent_type"],
        description=info["description"],
        cwd=info["cwd"],
        model=info["model"],
        isolation=info["isolation"],
        status="finished" if info["finished"] else "running",
        transcript=info["transcript"],
        spec_path=spec_path,
        spec_sha256=spec_sha,
        result_inline=inline,
        result_truncated=truncated,
        result_path=result_path,
    )


def shell_fact(item: dict[str, Any], keys: list[str], declared: dict[str, str]) -> Record:
    """The ``shell`` record of a pending Bash task, classified (D15)."""
    command = str(item.get("command") or "")
    classification, reason = classify_shell(command, keys, declared)
    return rec(
        "shell",
        f"core:shell:{item.get('id')}",
        "Background restarts",
        0,
        task_id=str(item.get("id") or ""),
        command=command,
        cwd=str(item.get("cwd") or ""),
        description=str(item.get("description") or ""),
        classification=classification,
        reason=reason,
        restartable=classification == DECLARED,
    )


def session_facts(store: Store, src: Source) -> list[Record]:
    """Session row, AIM / deadline / sub-goals, lineage and the transcript path."""
    session = store.get(src.session_id)
    out = [
        rec(
            "session",
            "core:session",
            "Goal and done-condition",
            0,
            session_id=src.session_id,
            cwd=src.cwd,
            account=src.label,
            config_dir=src.config_dir,
            model=src.model,
            effort=(session.effort if session else "") or "",
            no_codex=bool(session and session.no_codex),
            launch_profile=(session.launch_profile if session else "") or "",
            aim=(session.aim if session else "") or "",
            aim_rev=store.count_aim_history(src.session_id) if session else 0,
            deadline=(getattr(session, "deadline", None) or "") if session else "",
        )
    ]
    for index, goal in enumerate(store.list_subgoals(src.session_id) if session else [], 1):
        out.append(
            rec(
                "subgoal",
                f"core:subgoal:{index}",
                "Goal and done-condition",
                1,
                index=index,
                text=goal.text,
                checked=bool(goal.checked),
            )
        )
    if session and (session.handed_off_from or session.handed_off_to):
        out.append(
            rec(
                "lineage",
                "core:lineage",
                "Current state",
                2,
                handed_off_from=session.handed_off_from or "",
                handed_off_to=session.handed_off_to or "",
            )
        )
    out.append(
        rec(
            "transcript",
            "core:transcript",
            "Current state",
            0,
            path=str(src.transcript or ""),
        )
    )
    return out


def await_facts(store: Store, session_id: str) -> list[Record]:
    """The session's armed ``ccc await`` group (the successor re-binds it at accept, B7)."""
    group = store.active_await(session_id)
    if group is None:
        return []
    sources = [
        {"kind": s.kind, "label": s.label, "state": s.state}
        for s in store.await_sources_of(group.id)
    ]
    return [
        rec(
            "await",
            f"core:await:{group.id}",
            "Background restarts",
            0,
            group_id=group.id,
            state=group.state,
            purpose=group.purpose,
            until_epoch=group.until_epoch,
            items=group.items_list(),
            sources=sources,
        )
    ]


def capture_path(source_sid: str) -> Path:
    """Where the phase-``capture`` document of *source_sid* lives."""
    return facts_dir(source_sid) / "capture.json"


def read_capture(source_sid: str) -> dict[str, Any] | None:
    """The stored capture of *source_sid*, or ``None`` (absent / unreadable)."""
    try:
        data = json.loads(capture_path(source_sid).read_text(encoding="utf-8"))
    except (OSError, ValueError, FactsError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


def transcript_facts(
    src: Source,
    inflight_items: list[dict[str, Any]],
    phase: str,
    scrubber: Scrubber,
    *,
    persist: bool,
    capture: dict[str, Any] | None,
) -> tuple[list[Record], list[str]]:
    """Agents, shells, crons and keep-warm timers read from the transcript + inventory."""
    from . import inflight

    warnings: list[str] = []
    records = inflight._transcript_records(src.transcript) if src.transcript else []
    if src.transcript is None or not records:
        warnings.append("⚠ session transcript not readable — agents, shells and crons unknown")
    commands = _bash_commands(records)
    declared = declarations(commands)
    notes = _notifications(records)
    launches, uses = inflight._launch_details(records)
    out: list[Record] = []

    if phase == "finalize" and capture is not None:
        agent_rows = [
            (str(a["agent_id"]), str(a.get("transcript") or ""))
            for a in capture.get("agents", [])
            if isinstance(a, dict) and a.get("agent_id")
        ]
        shell_facts = [f for f in capture.get("shells", []) if isinstance(f, dict)]
    else:
        if phase == "finalize":
            warnings.append(
                "⚠ no capture.json — agents are those with a launch record in the transcript; "
                "run `ccc continue-session collect -S` BEFORE draining next time"
            )
            agent_rows = [(tid, "") for tid, launch in launches.items() if launch.is_agent]
        else:
            agent_rows = [
                (str(i["id"]), str(i.get("transcript") or ""))
                for i in inflight_items
                if i["kind"] == inflight.KIND_SUBAGENT
            ]
        shell_facts = []
        for item in inflight_items:
            if item["kind"] != inflight.KIND_SHELL:
                continue
            launch = launches.get(str(item["id"]))
            keys = [
                str(item["id"]),
                launch.tool_use_id if launch else "",
                str(item.get("description") or ""),
            ]
            shell_facts.append(shell_fact(item, keys, declared))
        # pid-scanned processes that match no parent Bash call are mostly the shells of a
        # running SUBAGENT; they cannot be attributed, so they are counted, never listed
        loose = [i for i in inflight_items if i["kind"] == inflight.KIND_UNKNOWN]
        if loose:
            warnings.append(
                f"{len(loose)} untracked process(es) (shells/children not started by this "
                "session's own Bash calls, e.g. a running subagent's) are not listed"
            )
    for agent_id, recorded in agent_rows:
        info = _spec_for(agent_id, src.transcript, records, recorded, notes)
        out.append(agent_fact(info, src.session_id, scrubber, persist=persist))
    out += shell_facts
    out += _cron_facts(records)
    timers = {str(i["id"]) for i in inflight_items if i["kind"] == inflight.KIND_KEEPWARM}
    out += _stopped_timers(records, timers)
    del uses
    return out, warnings


# ------------------------------------------------------------------------ collection


def provider_values(src: Source, phase: str) -> dict[str, str]:
    """The placeholder values for providers."""
    return {
        "{session}": src.session_id,
        "{cwd}": src.cwd,
        "{transcript}": str(src.transcript or ""),
        "{config_dir}": src.config_dir,
        "{phase}": phase,
    }


def collect(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    adapter: ClaudeAdapter,
    store: Store,
    src: Source,
    phase: str = "finalize",
    *,
    specs_config: object | None = None,
    scrubber: Scrubber | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """The facts document of *src* for *phase*.

    *persist* writes the agent spec/result files (scrubbed, 0600); the capture and the
    snapshot are written by :func:`save_capture` / :func:`freeze`. Raises
    :class:`FactsError` when a scrub is withheld.
    """
    from . import config, continue_session

    if phase not in PHASES:
        raise FactsError(f"unknown phase {phase!r}")
    scrub_with = scrubber or Scrubber()
    if specs_config is None:
        specs_config = config.load_config().handoff_facts
    specs, bad = parse_specs(specs_config)
    inv = continue_session.inventory(adapter, store, src)
    items = [item.to_dict() for item in inv.items]
    capture = read_capture(src.session_id) if phase == "finalize" else None
    facts: list[Record] = session_facts(store, src)
    t_facts, warns = transcript_facts(
        src, items, phase, scrub_with, persist=persist, capture=capture
    )
    facts += t_facts
    facts += await_facts(store, src.session_id)
    runs, provider_facts = run_providers(specs, provider_values(src, phase))
    facts += bad + provider_facts
    warnings = warns + [w for run in runs for w in run.warnings]
    repos = sorted({r for run in runs for r in run.repos})
    for text in warns:
        facts.append(_warning("collector", text))
    return {
        "version": VERSION,
        "session_id": src.session_id,
        "phase": phase,
        "collected_at_ms": int(time.time() * 1000),
        "facts": facts,
        "repos": repos,
        "warnings": warnings,
        "providers": [
            {"name": r.name, "ok": r.ok, "elapsed_s": r.elapsed_s, "error": r.error} for r in runs
        ],
    }


def canonical(doc: dict[str, Any]) -> str:
    """The canonical JSON of *doc* (sorted keys, no volatile timing fields)."""
    stable = {k: v for k, v in doc.items() if k not in ("collected_at_ms", "providers")}
    return json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def save_capture(doc: dict[str, Any], scrubber: Scrubber | None = None) -> Path:
    """Write *doc* (phase ``capture``) as ``capture.json``, scrubbed and 0600."""
    path = capture_path(doc["session_id"])
    scrub_with = scrubber or Scrubber()
    text = json.dumps(
        {
            "version": VERSION,
            "session_id": doc["session_id"],
            "captured_at_ms": doc["collected_at_ms"],
            "agents": [
                {k: f["fields"][k] for k in ("agent_id", "transcript", "description")}
                for f in doc["facts"]
                if f["kind"] == "agent"
            ],
            "shells": [f for f in doc["facts"] if f["kind"] == "shell"],
        },
        ensure_ascii=False,
        indent=1,
    )
    clean = scrub_with.text(text)
    json.loads(clean)  # a scrubber that broke the JSON must not leave a poisoned capture
    write_private(path, clean)
    return path


def freeze(
    doc: dict[str, Any], scrubber: Scrubber | None = None, *, write: bool = True
) -> tuple[str, Path]:
    """Freeze *doc* as ``<snap12>.json`` (D2); ``(snapshot id, path)``.

    The id is the sha256 of the SCRUBBED canonical JSON, so the same facts always map to
    the same path whatever transfer id comes later. Fail closed on a withheld scrub.
    ``write=False`` only computes the id (a dry run writes nothing).
    """
    scrub_with = scrubber or Scrubber()
    clean = scrub_with.text(canonical(doc))
    try:
        json.loads(clean)
    except ValueError as exc:
        raise FactsError("the scrubber broke the snapshot JSON — withheld") from exc
    snap = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    path = facts_dir(doc["session_id"]) / f"{snap[:12]}.json"
    if write and not path.exists():
        write_private(path, clean)
    return snap, path


# --------------------------------------------------------------------------- rendering


def _scalar(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def render_preview(doc: dict[str, Any]) -> str:
    """A compact human preview grouped by handoff section (NOT the composed handoff)."""
    from .handoff_doc import REQUIRED_SECTIONS

    lines = [f"facts of session {doc['session_id']} (phase {doc['phase']})"]
    for section in REQUIRED_SECTIONS:
        rows = sorted(
            (f for f in doc["facts"] if f["section"] == section),
            key=lambda f: (f["priority"], f["key"]),
        )
        if not rows:
            continue
        lines.append(f"\n[{section}]")
        for fact in rows:
            shown = ", ".join(
                f"{k}={_scalar(v)}"
                for k, v in fact["fields"].items()
                if v not in ("", None, [], {})
            )
            lines.append(f"- {fact['kind']} {fact['key']}: {shown}")
    for text in doc["warnings"]:
        lines.append(f"warning: {text}")
    return escape_headings("\n".join(lines)) + "\n"


# ------------------------------------------------------------------------------- CLI


def find_snapshot(ref: str, store: Store | None = None) -> tuple[str, Path] | None:
    """``(source sid, snapshot path)`` for a transfer id or a source session id."""
    sid = ref
    snap = ""
    if store is not None:
        transfer = store.get_transfer(ref)
        if transfer is not None:
            sid = transfer.source_session
            snap = str(getattr(transfer, "facts_snapshot", "") or "")
    try:
        directory = facts_dir(sid)
    except FactsError:
        return None
    if snap:
        hit = sorted(directory.glob(f"{snap[:12]}*.json"))
        return (sid, hit[0]) if hit else None
    candidates = [p for p in directory.glob("*.json") if p.name != "capture.json"]
    if not candidates:
        return None
    return sid, max(candidates, key=lambda p: p.stat().st_mtime_ns)


def _fail(message: str) -> int:
    import sys

    print(f"error: {message}", file=sys.stderr)
    return 1


def cmd_collect(args: Any) -> int:
    """``continue-session collect``: preview (``-m``/``-j``), ``-S`` capture, ``-F`` freeze."""
    import sys

    from . import continue_session

    adapter, store, src = continue_session._open_source(args)
    with store:
        if src is None:
            return _fail("could not resolve a session (pass -s/--session <id>)")
        phase = "capture" if args.save else args.phase
        scrubber = Scrubber()
        try:
            doc = collect(
                adapter, store, src, phase, scrubber=scrubber, persist=args.save or args.freeze
            )
            if args.save:
                print(f"capture: {save_capture(doc, scrubber)}", file=sys.stderr)
            if args.freeze:
                snap, path = freeze(doc, scrubber)
                print(f"snapshot: {snap[:12]} {path}", file=sys.stderr)
            if args.json:
                print(json.dumps(doc, ensure_ascii=False, indent=2))
            else:
                print(scrubber.text(render_preview(doc)), end="")
        except FactsError as exc:
            return _fail(str(exc))
    return 0


def cmd_facts(args: Any) -> int:
    """``continue-session facts``: print the frozen snapshot (or one key) of a handoff."""
    from .store import Store

    with Store() as store:
        found = find_snapshot(args.ref, store)
    if found is None:
        return _fail(f"no facts snapshot for {args.ref!r}")
    sid, path = found
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read {path}: {exc}")
    if args.key:
        key = args.key[2:] if args.key.startswith("#/") else args.key
        hit = next((f for f in doc.get("facts", []) if f.get("key") == key), None)
        if hit is None:
            return _fail(f"no fact {key!r} in {path.name}")
        doc = hit
    if args.json or args.key:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(f"snapshot {path.name} of session {sid}")
        print(render_preview({**doc, "session_id": sid}), end="")
    return 0


def add_parsers(verbs: Any) -> None:
    """Register ``collect`` and ``facts`` on the ``continue-session`` verb subparsers."""
    p_collect = verbs.add_parser(
        "collect",
        help="collect the code facts a handoff carries (preview; -S capture; -F freeze)",
        epilog=(
            "examples:\n"
            "  ccc continue-session collect -m            # preview, writes nothing\n"
            "  ccc continue-session collect -S            # capture BEFORE draining\n"
            "  ccc continue-session collect -j -p finalize"
        ),
        formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
    )
    p_collect.add_argument("-s", "--session", help="the session id (default: this session)")
    p_collect.add_argument("-j", "--json", action="store_true", help="the whole facts document")
    p_collect.add_argument("-m", "--markdown", action="store_true", help="text preview (default)")
    p_collect.add_argument(
        "-S", "--save", action="store_true", help="phase capture: write capture.json (before drain)"
    )
    p_collect.add_argument(
        "-p", "--phase", choices=PHASES, default="finalize", help="phase (default finalize)"
    )
    p_collect.add_argument(
        "-F", "--freeze", action="store_true", help="also freeze the snapshot and print its id"
    )
    p_facts = verbs.add_parser(
        "facts", help="print the frozen facts snapshot a handoff was composed from"
    )
    p_facts.add_argument("ref", help="a transfer id or the source session id")
    p_facts.add_argument("-j", "--json", action="store_true", help="JSON output")
    p_facts.add_argument("-k", "--key", help="one fact by key (a leading '#/' is accepted)")


VERBS: dict[str, Callable[[Any], int]] = {"collect": cmd_collect, "facts": cmd_facts}
