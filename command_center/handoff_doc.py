#!/usr/bin/env python3
"""The handoff document of ``ccc continue-session`` (tp#1300): template and validation.

The source session fills :func:`template` (package data
``command_center/assets/handoff/template.md``, read through ``importlib.resources`` so a
non-editable install works) and ``continue-session start`` hands the result to a NEW
session. Every path that produces a handoff — the model-written one and the capped-seat
``--capped`` summary — goes through :func:`validate`, which fails closed:

* strict UTF-8, at most :data:`MAX_BYTES` (24 KB);
* no control characters except newline and tab;
* every section of :data:`REQUIRED_SECTIONS` present as a ``## `` heading with a
  non-empty body (HTML comments — the template's guidance — do not count);
* credential shapes scrubbed through :mod:`command_center.scrub` (``mirror_scrub_cmd``,
  fail-closed: no scrubber or a withheld scrub rejects the document);
* the sha256 of the vouched text is what the transfer snapshot records.

A rejected document leaves the source session and every ownership record unchanged —
nothing has been written yet when validation runs.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

MAX_BYTES = 24 * 1024

REQUIRED_SECTIONS = (
    "Goal and done-condition",
    "Current state",
    "Decisions and constraints",
    "tp ownership",
    "Repositories, files, worktrees",
    "Subagent continuations",
    "Background restarts",
    "Open items and questions",
    "First steps",
)

#: D1: the three sections the MODEL writes; the other six are filled by ccc from the facts.
MODEL_SECTIONS = ("Current state", "Decisions and constraints", "First steps")
CODE_SECTIONS = tuple(t for t in REQUIRED_SECTIONS if t not in MODEL_SECTIONS)
MODEL_MAX_BYTES = 10 * 1024  # D9: the model part (and a legacy document's model share)
SCRUB_MARGIN = 2 * 1024  # D9: composition budgets MAX_BYTES - this; the scrubber may lengthen
MAX_LEVEL = 8  # compose cut tiers (0 = nothing cut)
RESTART_LINES = 5  # commits kept per repo from cut tier 2 on

_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ALLOWED_CONTROLS = frozenset("\n\t")


def template() -> str:
    """The fixed handoff template (package data)."""
    return (files("command_center") / "assets" / "handoff" / "template.md").read_text(
        encoding="utf-8"
    )


@dataclass
class HandoffDoc:
    """The outcome of :func:`validate`: the vouched text and its digest, or the errors."""

    text: str = ""
    sha256: str = ""
    labels: tuple[str, ...] = ()  # scrubber labels (names only, never values)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the document passed every check."""
        return not self.errors


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def sections(text: str) -> dict[str, str]:
    """``{normalized heading: body}`` for every ``## `` section (comments stripped)."""
    out: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = _COMMENT_RE.sub("", text[match.end() : end]).strip()
        out.setdefault(_norm(match.group(1)), body)
    return out


def _control_errors(text: str) -> list[str]:
    bad = sorted(
        {
            f"U+{ord(ch):04X}"
            for ch in text
            if ch not in _ALLOWED_CONTROLS and unicodedata.category(ch) == "Cc"
        }
    )
    return [f"control character(s) not allowed: {', '.join(bad)}"] if bad else []


def check_structure(text: str) -> list[str]:
    """Structural errors of *text* (size, control characters, required sections)."""
    errors: list[str] = []
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        errors.append(f"handoff is {size} bytes; the cap is {MAX_BYTES}")
    errors += _control_errors(text)
    found = sections(text)
    for title in REQUIRED_SECTIONS:
        body = found.get(_norm(title))
        if body is None:
            errors.append(f"missing section: ## {title}")
        elif not body:
            errors.append(f"empty section: ## {title} (write 'none' if nothing applies)")
    return errors


def validate(raw: bytes | str, *, scrub_policy: str | None = None) -> HandoffDoc:
    """Validate a handoff document and scrub credential shapes; fail closed.

    *scrub_policy* is the scrubber command (``None`` = the configured
    ``mirror_scrub_cmd``). The structure is checked BEFORE scrubbing (no scrubber call for a
    document that is rejected anyway) and again AFTER it (the scrubber must not have broken
    a section).
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return HandoffDoc(errors=[f"handoff is not valid UTF-8 (byte {exc.start})"])
    else:
        text = raw
    errors = check_structure(text)
    if errors:
        return HandoffDoc(errors=errors)
    from . import config, scrub  # local: scrub resolution spawns a probe

    policy = config.load_config().mirror_scrub_cmd if scrub_policy is None else scrub_policy
    resolution = scrub.resolve_scrubber(policy)
    if resolution.scrubber is None:
        return HandoffDoc(errors=[f"credential scrub withheld: {resolution.reason}"])
    result = scrub.scrub(resolution.scrubber, text)
    if not result.ok:
        return HandoffDoc(errors=[f"credential scrub withheld: {result.reason}"])
    errors = check_structure(result.text)
    if errors:
        return HandoffDoc(errors=["after scrubbing: " + e for e in errors])
    digest = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
    return HandoffDoc(text=result.text, sha256=digest, labels=result.labels)


# ------------------------------------------------------------------ model part + compose


def escape_headings(text: str) -> str:
    """Backslash the first ``#`` of every line matching ``^ {0,3}#`` (D16).

    ``_HEADING_RE`` needs a column-0 ``##``, so inserted free text can never open a section
    (``sections()`` keeps the FIRST occurrence of a name, so a copied ``## First steps``
    would otherwise replace the real one). Fences do not protect: the split ignores them.
    """
    return re.sub(r"(?m)^( {0,3})#", r"\1\\#", text)


@dataclass
class ModelPart:
    """The model-written input of ``start``: the three sections (+ legacy code-section notes)."""

    sections: dict[str, str] = field(default_factory=dict)  # MODEL_SECTIONS title -> body
    notes: dict[str, str] = field(default_factory=dict)  # legacy: CODE_SECTIONS title -> body
    legacy: bool = False
    raw: str = ""
    sha256: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the input passed every check."""
        return not self.errors


def _decode(raw: bytes | str) -> tuple[str, list[str]]:
    if isinstance(raw, str):
        return raw, []
    try:
        return raw.decode("utf-8"), []
    except UnicodeDecodeError as exc:
        return "", [f"handoff is not valid UTF-8 (byte {exc.start})"]


def _model_bodies(found: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    out: dict[str, str] = {}
    errors: list[str] = []
    for title in MODEL_SECTIONS:
        body = found.get(_norm(title))
        if body is None:
            errors.append(f"missing section: ## {title}")
        elif not body:
            errors.append(f"empty section: ## {title} (write 'none' if nothing applies)")
        else:
            out[title] = body
    return out, errors


def validate_model_part(raw: bytes | str) -> ModelPart:
    """Strict check of a 3-section model part (D1, D9); never scrubs (compose does)."""
    text, errors = _decode(raw)
    if errors:
        return ModelPart(errors=errors)
    size = len(text.encode("utf-8"))
    if size > MODEL_MAX_BYTES:
        errors.append(
            f"model part is {size} bytes; the cap is {MODEL_MAX_BYTES} — shorten it "
            "(ccc fills the facts itself)"
        )
    errors += _control_errors(text)
    stripped = _COMMENT_RE.sub("", text)
    allowed = {_norm(t) for t in MODEL_SECTIONS}
    for match in _HEADING_RE.finditer(stripped):
        if _norm(match.group(1)) not in allowed:
            errors.append(
                f"unexpected section: ## {match.group(1).strip()} — write only "
                + ", ".join(f"## {t}" for t in MODEL_SECTIONS)
            )
    bodies, missing = _model_bodies(sections(stripped))
    errors += missing
    if errors:
        return ModelPart(errors=errors)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ModelPart(sections=bodies, raw=text, sha256=digest)


def parse_model_file(raw: bytes | str) -> ModelPart:
    """The ``start -f`` file: a 3-section model part, or a legacy full document (D2, D9).

    A file with a NON-EMPTY body under any code-filled section name is legacy: those bodies
    become ``### Model notes`` (cut to the shared model budget by :func:`compose`). An empty
    code heading (the template left as it was) is ignored.
    """
    text, errors = _decode(raw)
    if errors:
        return ModelPart(errors=errors)
    found = sections(text)
    notes = {t: found[_norm(t)] for t in CODE_SECTIONS if found.get(_norm(t))}
    if not notes:  # a plain model part (empty code headings from the template are dropped)
        code = {_norm(t) for t in CODE_SECTIONS}
        kept = [
            ln
            for ln in _COMMENT_RE.sub("", text).splitlines()
            if not ((m := _HEADING_RE.match(ln)) and _norm(m.group(1)) in code)
        ]
        return validate_model_part("\n".join(kept) if len(kept) != len(text.splitlines()) else text)
    control = _control_errors(text)
    bodies, missing = _model_bodies(found)
    if control or missing:
        return ModelPart(errors=control + missing)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ModelPart(sections=bodies, notes=notes, legacy=bool(notes), raw=text, sha256=digest)


def model_digest(model: ModelPart, snapshot_id: str) -> str:
    """D2: the transfer's ``prompt_sha256`` = sha256(model part sha + snapshot id)."""
    return hashlib.sha256(f"{model.sha256}\n{snapshot_id}".encode()).hexdigest()


def _cut_to(text: str, limit: int) -> str:
    """*text* cut at a line boundary to at most *limit* bytes."""
    if len(text.encode("utf-8")) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        size = len(line.encode("utf-8")) + 1
        if used + size > limit:
            break
        kept.append(line)
        used += size
    return "\n".join(kept)


def _fit_model(model: ModelPart, model_path: str) -> tuple[dict[str, str], dict[str, str], bool]:
    """The three bodies and the legacy notes inside the shared 10 KB (D9); ``cut`` flag."""
    bodies = dict(model.sections)
    total = sum(len(b.encode("utf-8")) for b in bodies.values())
    cut = False
    if total > MODEL_MAX_BYTES:  # only a legacy document can get here (validate rejects it)
        share = MODEL_MAX_BYTES // len(MODEL_SECTIONS)
        bodies = {
            t: _cut_to(b, share) + f"\n… cut — full original: {model_path}"
            if len(b.encode("utf-8")) > share
            else b
            for t, b in bodies.items()
        }
        total = sum(len(b.encode("utf-8")) for b in bodies.values())
        cut = True
    remaining = max(0, MODEL_MAX_BYTES - total)
    notes: dict[str, str] = {}
    for title in CODE_SECTIONS:
        body = model.notes.get(title)
        if not body:
            continue
        size = len(body.encode("utf-8"))
        if size <= remaining:
            notes[title] = body
            remaining -= size
            continue
        cut = True
        part = _cut_to(body, remaining) if remaining > 0 else ""
        notes[title] = (part + "\n" if part else "") + f"… cut — full original: {model_path}"
        remaining = 0
    return bodies, notes, cut


@dataclass
class Composed:
    """One composition: the text, the tier it needed and whether model text was cut."""

    text: str
    level: int
    model_cut: bool = False


def _one(value: Any) -> str:
    if value is None or value in ("", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(str(value).split())


def _kv(fields: dict[str, Any], skip: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """``(inline k=v text, multi-line blocks)`` of *fields* (empty values dropped)."""
    parts: list[str] = []
    blocks: list[str] = []
    for key, value in fields.items():
        if key in skip or value in (None, "", [], {}):
            continue
        if isinstance(value, str) and "\n" in value:
            blocks.append(f"  {key}:\n" + "\n".join(f"    {ln}" for ln in value.splitlines()))
        else:
            parts.append(f"{key}={_one(value)}")
    return ", ".join(parts), blocks


def _generic(fact: dict[str, Any], skip: tuple[str, ...] = ()) -> list[str]:
    inline, blocks = _kv(fact["fields"], skip)
    return [f"- {fact['kind']} {fact['key']}: {inline}".rstrip(": "), *blocks]


def _box_ids(boxes: Any) -> list[str]:
    out = []
    for box in boxes if isinstance(boxes, list) else []:
        out.append(str(box.get("id", box)) if isinstance(box, dict) else str(box))
    return out


def _tp_item(fact: dict[str, Any], level: int) -> list[str]:
    f = fact["fields"]
    head = f"- tp#{f.get('tp_id')} [{_one(f.get('state'))}] {_one(f.get('title'))}"
    if f.get("repo") or f.get("tp_priority"):
        head += f" ({_one(f.get('repo'))}, {_one(f.get('tp_priority'))})"
    out = [head]
    plan = ""
    if f.get("plan_path"):
        plan = f"plan {f['plan_path']} {f.get('boxes_done')}/{f.get('boxes_total')}"
        boxes = f.get("next_open_boxes") or []
        shown = _box_ids(boxes) if level >= 3 else [_one(b) for b in boxes]
        if shown:
            plan += "; next open: " + ", ".join(shown)
    if plan:
        out.append(f"  {plan}")
    # Recorded decisions are not repeated in this never-cut line: the facts-only relay
    # renders them under "Decisions and constraints" (capped there), and the snapshot keeps them.
    rest, blocks = _kv(
        f,
        (
            "tp_id",
            "title",
            "state",
            "repo",
            "tp_priority",
            "plan_path",
            "boxes_done",
            "boxes_total",
            "next_open_boxes",
            "decisions",
        ),
    )
    if rest:
        out.append(f"  {rest}")
    return out + blocks


def _agent(fact: dict[str, Any], level: int, ref: str) -> list[str]:
    f = fact["fields"]
    out = [
        f"- agent {f.get('agent_id')} ({_one(f.get('agent_type'))}, {_one(f.get('status'))}): "
        f"{_one(f.get('description'))}"
    ]
    finished = f.get("status") == "finished"
    if finished and level >= 3:  # history, already delivered: a pointer, not the result
        if f.get("result_path"):
            out.append(f"  result_path: {_one(f['result_path'])}")
        return out
    for key in ("cwd", "model", "isolation", "spec_path", "result_path"):
        if f.get(key):
            out.append(f"  {key}: {_one(f[key])}")
    inline = str(f.get("result_inline") or "")
    cut = bool(f.get("result_truncated"))
    if inline and level >= 4 and len(inline.encode("utf-8")) > 1024:
        inline, cut = _cut_to(inline, 1024), True
    if inline:
        out.append("  result:")
        out += [f"    {ln}" for ln in inline.splitlines()]
        if cut:
            out.append(f"    … cut — full: {f.get('result_path') or ref}")
    return out


def _shell_lines(shells: list[dict[str, Any]]) -> list[str]:
    declared = [s for s in shells if s["fields"].get("classification") == "declared-restartable"]
    other = [s for s in shells if s not in declared]
    out: list[str] = []
    for fact in declared:
        f = fact["fields"]
        out.append(
            f"- restart: `{_one(f.get('command'))}` (cwd {_one(f.get('cwd'))}) — "
            f"{_one(f.get('reason'))}"
        )
    if other:
        out.append("Decide before restarting (evidence only — judge each; do not blindly re-run):")
        for fact in other:
            f = fact["fields"]
            out.append(
                f"- `{_one(f.get('command'))}` (cwd {_one(f.get('cwd'))}); "
                f"{_one(f.get('description'))}; {_one(f.get('reason'))}"
            )
    return out


def _session_lines(fact: dict[str, Any]) -> list[str]:
    f = fact["fields"]
    out = [f"AIM (done-condition): {_one(f.get('aim')) or 'none set'}"]
    if f.get("deadline"):
        out.append(f"Deadline: {_one(f['deadline'])}")
    out.append(
        f"Source session {f.get('session_id')} on account {_one(f.get('account'))}, "
        f"cwd {_one(f.get('cwd'))}, model {_one(f.get('model'))}"
    )
    return out


def _render_plain(  # pylint: disable=too-many-return-statements
    fact: dict[str, Any], level: int, ref: str
) -> list[str]:
    kind, f = fact["kind"], fact["fields"]
    if kind == "session":
        return _session_lines(fact)
    if kind == "subgoal":
        return [f"- [{'x' if f.get('checked') else ' '}] sub-goal: {_one(f.get('text'))}"]
    if kind == "lineage":
        return [f"Lineage: from {_one(f.get('handed_off_from')) or '-'}"]
    if kind == "transcript":
        return [f"Predecessor transcript (evidence only): {_one(f.get('path'))}"]
    if kind == "tp_item":
        return _tp_item(fact, level)
    if kind == "tp_filed":
        return [f"- tp#{f.get('tp_id')} [{_one(f.get('state'))}] {_one(f.get('title'))}"]
    if kind == "question":
        return [f"- ? tp#{f.get('tp_id')} {_one(f.get('source'))}: {_one(f.get('detail'))}"]
    if kind == "agent":
        return _agent(fact, level, ref)
    if kind == "warning":
        return [f"⚠ {_one(f.get('message'))}"]
    if kind == "worktree" and level >= 5:
        return [f"- worktree {_one(f.get('path'))} ({_one(f.get('branch'))})"]
    return _generic(fact)


_COLLAPSE = {  # kind -> (cut tier, noun)
    "tp_filed": (1, "filed (not held) items"),
    "wait": (6, "waits/schedules"),
    "schedule": (6, "waits/schedules"),
    "schedule_unknown": (6, "waits/schedules"),
}


def _section_of(fact: dict[str, Any]) -> str:
    title = str(fact.get("section"))
    return title if title in REQUIRED_SECTIONS else "Open items and questions"


def _facts_lines(  # pylint: disable=too-many-locals,too-many-branches
    doc: dict[str, Any], level: int, ref: str
) -> dict[str, list[str]]:
    """Render the snapshot's facts per section at cut tier *level* (code facts only)."""
    facts = sorted(
        enumerate(doc.get("facts", [])), key=lambda p: (int(p[1].get("priority", 4)), p[0])
    )
    out: dict[str, list[str]] = {t: [] for t in REQUIRED_SECTIONS}
    shells: list[dict[str, Any]] = []
    cut: dict[tuple[str, str], list[dict[str, Any]]] = {}
    commits: dict[str, int] = {}
    owned: dict[str, int] = {}
    seen_msgs = {_one(f["fields"].get("message")) for _, f in facts if f.get("kind") == "warning"}
    for _, fact in facts:
        kind, section = fact["kind"], _section_of(fact)
        if kind == "shell":
            shells.append(fact)
            continue
        collapse = _COLLAPSE.get(kind)
        if collapse and level >= collapse[0]:
            cut.setdefault((section, collapse[1]), []).append(fact)
            continue
        if kind == "agent" and level >= 5 and fact["fields"].get("status") == "finished":
            cut.setdefault((section, "finished subagents"), []).append(fact)
            continue
        if kind == "commit" and level >= 2:
            repo = _one(fact["fields"].get("repo"))
            commits[repo] = commits.get(repo, 0) + 1
            if commits[repo] > RESTART_LINES:
                cut.setdefault((section, "older commits"), []).append(fact)
                continue
        if kind == "owned_file" and level >= 7:
            owned[section] = owned.get(section, 0) + 1
            if owned[section] > 10:
                cut.setdefault((section, "owned files"), []).append(fact)
                continue
        lines = _render_plain(fact, level, ref)
        if kind == "tp_filed" and not any(ln.startswith("Filed, not held") for ln in out[section]):
            out[section].append("Filed, not held (this session filed them; accept moves nothing):")
        out[section] += lines
    for text in doc.get("warnings", []):
        if _one(text) not in seen_msgs:
            out["Open items and questions"].append(f"⚠ {_one(text)}")
    if shells:
        out["Background restarts"] += _shell_lines(shells)
    for (section, noun), gone in cut.items():
        first = gone[0]["key"]
        out[section].append(
            f"… {len(gone)} more {noun} — full: ccc continue-session facts {ref} -k {first}"
        )
    return out


def compose(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    model: ModelPart,
    doc: dict[str, Any],
    *,
    level: int = 0,
    ref: str = "<source-session>",
    model_path: str = "<stored original>",
) -> Composed:
    """The handoff text from the model part and the facts snapshot *doc* (D1, D9, D16).

    Code facts come first in every section; *level* (0..:data:`MAX_LEVEL`) is the cut tier
    (``compose_checked`` raises it until the text fits *budget*, then once more if the
    scrubber lengthens it). Never cut: the AIM, held tp ids with state, open questions,
    background restarts, agent spec paths and warning lines. Every inserted free text goes
    through :func:`escape_headings`; the three model sections are copied verbatim.
    """
    bodies, notes, cut = _fit_model(model, model_path)
    lines = _facts_lines(doc, level, ref)
    parts = ["# Handoff", ""]
    for title in REQUIRED_SECTIONS:
        parts += [f"## {title}", ""]
        code = escape_headings("\n".join(lines[title])).strip()
        if code:
            parts += [code, ""]
        if title in bodies:
            parts += [bodies[title].strip(), ""]
        note = notes.get(title)
        if note:
            if level >= MAX_LEVEL:
                parts += ["### Model notes", "", f"… cut — full original: {model_path}", ""]
                cut = True
            else:
                parts += ["### Model notes", "", escape_headings(note), ""]
        if not code and title not in bodies and not (note and level < MAX_LEVEL):
            parts += ["none", ""]
    return Composed("\n".join(parts).rstrip("\n") + "\n", level, cut)


def compose_checked(  # pylint: disable=too-many-arguments
    model: ModelPart,
    doc: dict[str, Any],
    *,
    ref: str,
    model_path: str,
    scrub_policy: str | None = None,
) -> tuple[HandoffDoc, Composed]:
    """Compose at the smallest cut tier that fits, :func:`validate` it, and re-cut once.

    The scrubber may lengthen the text, so a document that validates too big gets ONE more
    tier (two validate passes at most); then the refusal names the sizes.
    """
    budget = MAX_BYTES - SCRUB_MARGIN
    level = 0
    composed = compose(model, doc, level=level, ref=ref, model_path=model_path)
    while len(composed.text.encode("utf-8")) > budget and level < MAX_LEVEL:
        level += 1
        composed = compose(model, doc, level=level, ref=ref, model_path=model_path)
    sizes = [len(composed.text.encode("utf-8"))]
    result = validate(composed.text, scrub_policy=scrub_policy)
    if not result.ok and any("the cap is" in e for e in result.errors) and level < MAX_LEVEL:
        composed = compose(model, doc, level=level + 1, ref=ref, model_path=model_path)
        sizes.append(len(composed.text.encode("utf-8")))
        result = validate(composed.text, scrub_policy=scrub_policy)
    if not result.ok and any("the cap is" in e for e in result.errors):
        result.errors.append(
            f"composed handoff sizes {sizes} bytes after the cuts; the cap is {MAX_BYTES} — "
            "shorten the model part"
        )
    return result, composed
