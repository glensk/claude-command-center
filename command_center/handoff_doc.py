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
import re
import unicodedata
from dataclasses import dataclass, field
from importlib.resources import files

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
