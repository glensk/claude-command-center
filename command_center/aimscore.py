#!/usr/bin/env python3
"""AIM specificity scoring — how *verifiable* a session's done-condition is.

Two tiers, both returning a 0–100 score (higher = more specific/testable):

* :func:`score_aim_lexical` — pure, instant, offline. A conservative heuristic
  used synchronously the moment an AIM is set, so the UI never shows a blank
  score. Deliberately **biased low** so the common path is "provisional red →
  the LLM clears it" (reads as *checked and fine*) rather than the alarming
  reverse.
* :func:`score_aim_llm` — one cheap LLM call that refines the score out-of-band
  (daemon / detached ``ccc score-aim``). Routed through the pluggable
  :func:`command_center.llm.run_ladder` fallback ladder (copilot / gemini / codex /
  claude / custom, per ``config.score_backends``), so the call can move off Anthropic
  tokens when another backend is available. Never raises.

Kept dependency-light (only the leaf ``models``; no store/autoprogress imports) so
it can be imported from ``store`` without an import cycle; ``llm`` is imported lazily.
"""

# Lazy `.llm` import avoids an import cycle (store -> aimscore) and keeps cost off
# the pure/instant path.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import re
from typing import TYPE_CHECKING

from .models import first_json_object

if TYPE_CHECKING:
    from .config import Config

# Abstract verbs/nouns with no observable end-state. Canonical vocabulary, shared:
# the lexical score penalizes any of these, and autoprogress.lint_subgoal rejects a
# sub-goal that *leads* with one. (Single source of truth — imported there.)
VAGUE_WORDS = frozenset(
    {
        "improve",
        "restructure",
        "refactor",
        "update",
        "handle",
        "support",
        "optimize",
        "enhance",
        "polish",
        "tweak",
        "clean",
        "cleanup",
        "better",
        "fix",
        "do",
        "make",
        "work",
        "stuff",
        "things",
        "various",
        "general",
        "misc",
        "maintain",
        "design",
        "investigate",
        "explore",
        "review",
        "consider",
        "ensure",
        "manage",
        "address",
    }
)

# Tokens that signal an objectively checkable outcome — pull the score up.
_TESTABLE = frozenset(
    {
        "test",
        "tests",
        "pass",
        "passes",
        "passing",
        "green",
        "merged",
        "deployed",
        "deploy",
        "build",
        "builds",
        "compiles",
        "exit",
        "error",
        "errors",
        "coverage",
        "release",
        "ship",
        "shipped",
        "working",
        "returns",
        "renders",
        "responds",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_NUMBER_RE = re.compile(r"\b\d+\b")
# A path/filename-ish token: has a slash, or a dotted extension like foo.py / x.md.
_PATHISH_RE = re.compile(r"[\w./-]+/[\w./-]+|\b[\w-]+\.[a-z]{1,4}\b")


def score_aim_lexical(aim: str | None) -> int:
    """Instant, offline specificity estimate (0–100, conservative-low).

    Starts below the typical vagueness threshold and only climbs when the AIM
    carries concrete, checkable signal (a test, a file/number, a metric word).
    """
    if not aim or not aim.strip():
        return 0
    text = aim.lower()
    words = _WORD_RE.findall(text)
    if not words:
        return 0

    score = 35  # conservative base — below the default threshold of 50
    word_set = set(words)

    if word_set & _TESTABLE:
        score += 35
    if _NUMBER_RE.search(text) or "#" in aim or _PATHISH_RE.search(text):
        score += 20
    if len(words) >= 6:
        score += 10
    if len(words) <= 2:
        score -= 20
    score -= 15 * len(word_set & VAGUE_WORDS)  # each distinct vague term hurts

    return max(0, min(100, score))


# The published rubric the INDEPENDENT checker scores against — and the same one the
# in-session sharpener is told to target. Transparent (not a black box) and reproducible:
# four criteria summing to 100, scored by a separate process so it never grades its own work.
AIM_RUBRIC = """\
Rate how CONCRETE and VERIFIABLE an AI coding session's done-condition (its "AIM") is — \
whether an INDEPENDENT grader could objectively decide the session is done. Award points per \
criterion and sum to a 0-100 score:
- end_state (0-30): names a specific artifact / file / output / state that exists when done
  (e.g. "PR #42 merged", "dist/app builds"). 0 if only an abstract verb, no observable result.
- objective_check (0-30): done is decidable automatically — a passing test, a command exiting 0,
  or a measurable metric/threshold. 0 if it needs human judgement.
- bounded (0-20): finite and singular, not open-ended ("improve X" / "handle edge cases" = open).
- no_vague (0-20): no vague head verb (improve/refactor/handle/support/optimize/clean/…); deduct
  per vague term."""

_AIM_SCORE_PROMPT = (
    AIM_RUBRIC
    + """

AIM: {aim}

Reply with STRICT minified JSON and nothing else:
{{"score":<int 0-100>,"criteria":{{"end_state":<int>,"objective_check":<int>,\
"bounded":<int>,"no_vague":<int>}},"reason":"<= 12 words why",\
"missing":"<= 16 words: the single most useful thing to add to score higher"}}"""
)

_CRITERIA = ("end_state", "objective_check", "bounded", "no_vague")


def score_aim_detailed(aim: str | None, cfg: Config, *, note: str = "") -> dict | None:
    """Independent rubric check of *aim*. ``None`` on failure (never raises).

    Returns ``{"score": int, "criteria": {...}, "reason": str, "missing": str, "backend": str}``
    — the per-criterion breakdown makes the score reproducible, ``missing`` is the actionable
    hint the sharpener optimizes against, and ``backend`` names the ladder rung that served the
    call. Runs the rubric (:data:`AIM_RUBRIC`) through :func:`command_center.llm.run_ladder`
    (``cfg.score_backends``); the claude rung uses ``cfg.score_model`` or ``cfg.llm_model``.

    *note* is the session's first AIM — exported (with the ``aim-score`` purpose) into the
    rung subprocesses' env as ``CCC_LLM_NOTE`` / ``CCC_LLM_PURPOSE``, log/route metadata
    for a custom router; it never affects the score.
    """
    if not aim or not aim.strip():
        return None
    from . import llm  # lazy: keep import cost off the fast/pure paths

    served = llm.run_ladder(_AIM_SCORE_PROMPT.format(aim=aim), cfg, purpose="aim-score", note=note)
    if served is None:
        return None
    backend, raw = served
    obj = first_json_object(raw)
    score = obj.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return None
    raw_crit = obj.get("criteria")
    crit = raw_crit if isinstance(raw_crit, dict) else {}
    return {
        "score": max(0, min(100, int(score))),
        "criteria": {k: int(crit.get(k, 0) or 0) for k in _CRITERIA},
        "reason": str(obj.get("reason") or "").strip(),
        "missing": str(obj.get("missing") or "").strip(),
        "backend": backend,
    }


def score_aim_llm(aim: str | None, cfg: Config, *, note: str = "") -> tuple[int, str] | None:
    """Refine the AIM score via the pluggable score-backend ladder. ``None`` on failure.

    Returns ``(score 0-100, reason)``; the reason folds in the rubric's ``missing`` hint so the
    stored ``aim_score_reason`` says how to improve. Never raises — degrades to ``None`` so the
    caller keeps the provisional lexical score. *note* (the session's first AIM) is forwarded
    to :func:`score_aim_detailed` as router-log metadata only.
    """
    detail = score_aim_detailed(aim, cfg, note=note)
    if detail is None:
        return None
    reason, missing = detail["reason"], detail["missing"]
    if missing:
        reason = f"{reason}; fix: {missing}" if reason else f"fix: {missing}"
    return (detail["score"], reason)
