#!/usr/bin/env python3
"""Derive a very short, scannable label from a session's AIM (the ``ccc`` /aim column).

The full AIM is preserved verbatim in the store; this generates a ≤ ~10-word identifier
("implement X", "Maria: ws reconnect") so a human can tell parked sessions apart at a
glance in a narrow column. It is generated out-of-band — never on the hot path — and
regenerated on every AIM change (see :func:`command_center.cli.cmd_set_aim`).

Backend is pluggable (``short_aim_backend``): the default ``auto`` picks ``codex`` when the
OpenAI Codex CLI is on ``PATH`` (so the cost lands on Codex/ChatGPT quota, NOT Claude tokens)
and otherwise falls back to ``claude`` (a cheap ``claude -p`` call); explicit ``codex`` /
``claude`` force one. Either way it NEVER raises — a failure returns ``None`` and callers keep
showing the full AIM.

Kept dependency-light (only the leaf ``llm``, imported lazily) so importing it is cheap.
"""

# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import re
import shutil

# Stored labels are capped here (safety net against a runaway model); the column crops
# further. Kept short on purpose — this is an at-a-glance identifier, not a summary (the
# full AIM lives one keypress away). Sized for the ≤10-word target the prompt asks for.
_MAX_CHARS = 72

_PROMPT = """\
You label AI coding sessions for a narrow dashboard column. Given one session's \
done-condition (its "AIM"), output ONE very short label so a human can instantly tell this \
session apart from others.

Rules:
- At most 10 words and {max_chars} characters. Imperative, lowercase, no trailing period, no \
quotes, no markdown, no code fences. Shorter is better — only use the full 10 words when the \
goal genuinely needs them.
- If the AIM centres on a specific PERSON, start with their name then a colon \
(e.g. "maria: ws reconnect backoff").
- If it is a feature/bugfix, say only WHAT is built or fixed ("implement X", "add Y", \
"fix Z") — drop ceremony (tests passing, PR merged, builds green): name the thing, not the proof.
- Keep the wording of the AIM itself; do not invent new terminology.{original_hint}

AIM: {aim}

Reply with ONLY the label, nothing else."""

# Strip a leading bullet/quote and a trailing period; collapse internal whitespace.
_LEAD_RE = re.compile(r"^[\s\-*>•·\"'`]+")
_TRAIL_RE = re.compile(r"[\s.\"'`]+$")
_WS_RE = re.compile(r"\s+")
# A line that is PURELY a markdown fence marker: ``` with an optional language tag.
_FENCE_RE = re.compile(r"^```[\w-]*\s*$")


def _strip_fence(raw: str) -> str:
    """Unwrap a reply enclosed in a markdown code fence (``` or ```lang … ```).

    Without this, first-non-empty-line selection turns a ```json-fenced reply into the
    literal label "json". Only a line that is PURELY a fence marker counts — a label
    that merely starts with backticks (inline code) is content and passes through
    untouched. A missing closing fence still unwraps (a truncated fenced reply must
    not yield its language tag either). Pure function.
    """
    lines = raw.strip().splitlines()
    if not lines or not _FENCE_RE.match(lines[0]):
        return raw
    body = lines[1:]
    if body and body[-1].strip() == "```":
        body = body[:-1]
    return "\n".join(body)


def _sanitize(raw: str | None) -> str | None:
    """Reduce a (possibly chatty) model reply to one clean short label, or ``None``.

    Strips an enclosing code fence, then takes the first non-empty line, strips
    surrounding quotes/bullets/backticks and a trailing period, collapses whitespace,
    and hard-caps the length so a verbose model can never blow out the column.
    """
    if not raw:
        return None
    line = next((ln for ln in _strip_fence(raw).splitlines() if ln.strip()), "")
    line = _WS_RE.sub(" ", _TRAIL_RE.sub("", _LEAD_RE.sub("", line))).strip()
    if not line:
        return None
    if len(line) > _MAX_CHARS:
        line = line[:_MAX_CHARS].rstrip(" .,-")
    return line or None


def resolve_backend(backend: str) -> str:
    """Resolve the short-AIM backend, expanding ``"auto"`` at use time.

    ``"auto"`` (the default) picks ``"codex"`` when the OpenAI Codex CLI is on ``PATH``
    (keeps the cost off Claude tokens) and otherwise ``"claude"`` — so a box without codex
    still gets short labels. Explicit ``"codex"``/``"claude"`` pass through unchanged; any
    other value is returned as-is (the dispatch below treats non-``"claude"`` as codex,
    the historical default — no behaviour change for unknown values).

    This is the single resolution point every call site funnels through (all go via
    :func:`generate`).
    """
    if backend == "auto":
        return "codex" if shutil.which("codex") else "claude"
    return backend


def _original_hint(aim: str, original: str | None) -> str:
    """A prompt clause nudging the model toward the session's first/original AIM.

    Observation: the right short label is often close to how the goal was first
    stated, before the AIM accreted detail. Empty when there is no distinct original.
    """
    if not original or original.strip() == aim.strip():
        return ""
    return (
        "\n- This goal was ORIGINALLY stated as below; the short label is usually close to "
        f'that original intent: "{original.strip()}"'
    )


def generate(
    aim: str | None,
    *,
    original: str | None = None,
    backend: str = "auto",
    model: str = "",
) -> str | None:
    """Generate a short label for *aim*. ``None`` on empty input or any backend failure.

    *original* (the session's first-ever AIM, if different) is offered to the model as a
    hint. *backend* selects the generator: ``"auto"`` (codex if on ``PATH``, else claude —
    the default), ``"codex"`` (the OpenAI Codex CLI — keeps the cost off Claude) or
    ``"claude"`` (a cheap ``claude -p`` call). *model* is the concrete model id; empty means
    the backend's own default.
    """
    if not aim or not aim.strip():
        return None
    from . import llm  # lazy: keep import cost off pure/fast paths

    prompt = _PROMPT.format(
        aim=aim.strip(), max_chars=_MAX_CHARS, original_hint=_original_hint(aim, original)
    )
    backend = resolve_backend(backend)
    if backend == "claude":
        # note = the session's first AIM (router-log metadata only); the codex backend
        # has no label support so it is left untouched.
        raw = llm.run_model(
            prompt, model, purpose="short-aim", note=llm.concise_note(original or aim)
        )
    else:
        raw = llm.run_codex(prompt, model)
    return _sanitize(raw)
