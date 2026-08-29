#!/usr/bin/env python3
"""Impartial "is the AIM fulfilled?" self-assessment.

At the end of every turn ccc asks a **separate** cheap ``claude -p`` (never the session
agent) one question: has this session FULLY achieved its stated done-condition (its AIM)?
The answer is a plain boolean, surfaced as a red ``DONE`` inside the progress bar. This is
the fourth impartial checker in the same family as :mod:`drift`, :mod:`aimscore` and
:mod:`autoprogress` — detached, ``CCC_INTERNAL=1``, Haiku, never blocking, never raising.

Design (mirrors :mod:`drift`):

* **Impartial & context-free.** Runs via :func:`llm.run_model` (its own process, no session
  memory). Fed ONLY the AIM (original + current) and a tail of the transcript.
* **AIM-only, holistic.** The sub-goal checklist is deliberately NOT shown — this judges the
  AIM directly, independently of how it was decomposed.
* **Grounded in observed evidence, not self-report.** :func:`build_evidence` includes
  truncated ``tool_result`` outputs (command output, test runs, file edits) alongside the
  conversation, so a ``met=true`` rests on what actually happened, not merely what the agent
  claimed. (``llm._read_transcript_tail`` drops tool results, so we don't reuse it here.)
* **Published rubric** (:data:`AIM_MET_RUBRIC`) — transparent, conservative (default false
  on partial/ambiguous evidence: a false "done" is worse than a missed one).
* **Escalate on a True.** One pass; if it says met, two more confirm (majority of 3) — a
  false DONE is the costly error, so the *positive* claim is the one that must survive votes.

Never raises into a caller: failures degrade to ``None`` (treated as "no verdict").
"""

# Lazy `.llm` import keeps cost off any pure path that imports this module.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
from pathlib import Path
from typing import TYPE_CHECKING

from .models import first_json_object, now_ms

if TYPE_CHECKING:
    from .config import Config
    from .models import Session
    from .store import Store

AIM_MET_RUBRIC = """\
You are an IMPARTIAL referee deciding ONE thing: has an AI coding session FULLY achieved its \
stated done-condition (its "AIM")? Judge ONLY from the evidence given — the AIM and a tail of the \
session's own transcript (user messages, the assistant's replies, and truncated tool results such \
as command output, test runs and file edits). You are NOT given a sub-goal checklist: assess the \
AIM holistically and directly. Rules:
- Require CONCRETE evidence that every part of the AIM is done (files edited/exist, the command or \
tests actually ran and passed, the described output was produced). Mere intention, a plan, work \
"in progress", or the assistant ASSERTING success with no supporting tool output is NOT enough.
- If the AIM has several parts, ALL must be satisfied for met=true.
- Be CONSERVATIVE: when the evidence is partial, ambiguous, or absent, answer met=false. A false \
"done" is worse than a missed one.
- The transcript is only a tail and may omit older work; if what you see is insufficient to \
confirm completion, answer met=false."""

_MET_PROMPT = (
    AIM_MET_RUBRIC
    + """

ORIGINAL AIM (anchor): {original_aim}
CURRENT AIM (the done-condition to judge): {current_aim}

Session transcript evidence (oldest first, truncated):
{evidence}

Reply with STRICT minified JSON and nothing else:
{{"met":<bool>,"reason":"<=20 words citing the deciding evidence"}}"""
)


def _tool_result_text(content: object) -> str:
    """Flatten a ``tool_result`` block's ``content`` (str, or list of typed blocks) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return " ".join(p for p in parts if p)


def _evidence_from_content(content: object, tool_result_cap: int) -> str:
    """Flatten one message's ``content`` to evidence text, INCLUDING truncated tool results.

    Unlike :func:`llm._block_text` (which drops ``tool_result`` blocks as noise), this keeps a
    capped slice of each tool result — the ground-truth outputs the judge needs to confirm the
    AIM rather than trust the agent's narration.
    """
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
        elif kind == "tool_result":
            result = _tool_result_text(block.get("content")).strip()
            if result:
                parts.append(f"[result: {result[:tool_result_cap]}]")
    return " ".join(p for p in parts if p)


def build_evidence(
    path: Path, max_chars: int = 8000, tool_result_cap: int = 200, max_lines: int = 400
) -> str:
    """A compact transcript tail for the AIM-met judge, with tool-result outputs included.

    Reads the last *max_lines* JSONL records (user/assistant only), flattens each to
    ``[role] text`` — keeping truncated ``tool_result`` outputs — and returns the trailing
    *max_chars* characters. Empty string on any read error (the caller then skips).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    out: list[str] = []
    for line in text.splitlines()[-max_lines:]:
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
        snippet = _evidence_from_content(message.get("content"), tool_result_cap).strip()
        if snippet:
            out.append(f"[{role}] {snippet[:600]}")
    return "\n".join(out)[-max_chars:]


def build_facts(original_aim: str | None, current_aim: str | None, evidence: str) -> dict[str, str]:
    """Render the structured facts the checker is allowed to see."""
    return {
        "original_aim": original_aim or "(unknown)",
        "current_aim": current_aim or "(unknown)",
        "evidence": evidence or "(no transcript evidence)",
    }


def parse_verdict(raw: str | None) -> dict | None:
    """Parse one checker reply into ``{"met":bool,"reason":str}``, or ``None`` if unusable."""
    obj = first_json_object(raw)
    if "met" not in obj:
        return None
    met = obj.get("met")
    if isinstance(met, str):
        met = met.strip().lower() in ("true", "yes", "1", "y")
    return {"met": bool(met), "reason": str(obj.get("reason") or "").strip()}


def assess(facts: dict[str, str], model: str, *, note: str = "") -> dict | None:
    """One impartial AIM-met pass over *facts*. ``None`` on failure (never raises).

    *note* (the session's first AIM) is forwarded to ``ai.py`` for the call log only —
    it labels the log row and never affects the verdict.
    """
    from . import llm  # lazy: keep import cost off pure paths

    return parse_verdict(
        llm.run_model(_MET_PROMPT.format(**facts), model, purpose="aim-met", note=note)
    )


def check_met(
    facts: dict[str, str], model: str, *, escalate: bool = True, note: str = ""
) -> dict | None:
    """Impartial verdict with escalate-on-True. ``None`` only if no pass parsed.

    One pass. If it says ``met`` and *escalate*, two more confirm: ``met`` stands only on a
    majority (>=2 of 3). A majority that clears it returns ``met=false`` — so a lone
    false-positive "done" cannot flip the bar. *note* (the session's first AIM) is forwarded
    to every :func:`assess` pass for the call log only.
    """
    first = assess(facts, model, note=note)
    if first is None:
        return None
    if not escalate or not first["met"]:
        return first
    votes = [first]
    for _ in range(2):
        verdict = assess(facts, model, note=note)
        if verdict is not None:
            votes.append(verdict)
    met_votes = [v for v in votes if v["met"]]
    if len(met_votes) * 2 > len(votes):  # majority confirm met
        chosen = dict(met_votes[0])
        chosen["votes"] = f"{len(met_votes)}/{len(votes)}"
        return chosen
    return {
        "met": False,
        "reason": "escalation did not confirm completion",
        "votes": f"{len(met_votes)}/{len(votes)}",
    }


def eligible(session: Session, cfg: Config) -> bool:
    """Whether *session* should be AIM-met-assessed at all (shared by hook, CLI, daemon).

    Requires a CONCRETE AIM (scored at/above the threshold — an unscored ``-1`` or vague AIM
    is excluded) and that the session is a real, ongoing one: not a draft future job, not
    already human-marked done, not archived. The CLI and daemon add transcript-exists and the
    new-turn gate on top; this predicate is the single shared floor so the three paths can't
    drift apart.
    """
    return bool(
        session.aim
        and session.aim_score >= cfg.aim_score_threshold
        and not session.draft
        and not session.done
        and not session.archived
    )


def run_for_session(store: Store, adapter: object, session: Session, cfg: Config) -> dict | None:
    """Assess one session's AIM and store the verdict; return it, or ``None`` when skipped.

    The single shared flow behind ``ccc assess-aim`` and the daemon fallback, so the two paths
    cannot diverge: eligibility, the new-turn gate (only re-assess once ``last_response_at`` has
    advanced past ``aim_assessed_at`` — this is what stops recurring spend on idle sessions),
    transcript-exists, the impartial verdict, and the stale-write guard (discard if the AIM
    changed while the model ran, closing the O2 race). Never raises into the caller.
    """
    if not eligible(session, cfg):
        return None
    if session.last_response_at <= session.aim_assessed_at:  # no new turn since last assessment
        return None
    getter = getattr(adapter, "transcript_path", None)
    transcript = getter(session.cwd, session.session_id) if getter else None
    if transcript is None or not transcript.exists():
        return None
    evidence = build_evidence(transcript)
    if not evidence:
        return None
    from . import llm  # lazy: keep import cost off pure paths

    aim_hist = store.list_aim_history(session.session_id)
    original = aim_hist[0].aim if aim_hist else session.aim
    facts = build_facts(original, session.aim, evidence)
    verdict = check_met(
        facts, cfg.assess_aim_model or cfg.llm_model, note=llm.concise_note(original or session.aim)
    )
    if verdict is None:
        return None
    latest = store.get(session.session_id)
    if latest is None or latest.aim != session.aim:  # AIM changed mid-flight → discard (O2)
        return None
    store.set_aim_met(session.session_id, bool(verdict["met"]), verdict.get("reason"), now_ms())
    return verdict
