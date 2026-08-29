#!/usr/bin/env python3
"""Automatic per-session progress: derive sub-goals, then auto-check them off.

The progress bar is ``checked / total`` of a sub-goal checklist. Manually
maintaining that checklist is friction, so for any session that has an AIM
("done when: …") but no checklist, this module *derives* 3-6 concrete,
checkable sub-goals with a cheap model, then on each daemon pass reads only the
**new** transcript content (a byte offset is persisted per session) and asks the
cheap model which sub-goals are now satisfied — checking those off. The bar
fills with zero manual effort.

Research notes (informed the design; be skeptical — these are evals, not a law):

* **Checklist grading with a small model tracks human judgment.** RocketEval
  (ICLR'25) shows a lightweight judge grading a per-item checklist correlates
  ~0.96 with human preference; TheAgentCompany and "grading-notes / subgoal"
  LLM-judge setups report Cohen's κ 0.84-0.92 at the per-subgoal level. This is
  exactly our shape: derive a fixed checklist, grade each item independently.
  https://openreview.net/forum?id=zJjzNj6QUe ·
  https://arxiv.org/pdf/2412.14161 (TheAgentCompany)
* **Grade each subgoal independently as success / fail / ambiguous, and be
  conservative** — map "ambiguous" → leave unchecked, only flip on clear
  evidence. (LLM-as-judge survey; conservative CORRECT/INCORRECT rubric.)
  https://www.evidentlyai.com/llm-guide/llm-as-a-judge
* **Progress = milestone-completion ratio.** "Action-advancement" / milestone
  tracking in agent-eval surveys frames intermediate progress as the fraction of
  sub-goals reached — i.e. ``checked / total``, which is what we render.
  https://arxiv.org/html/2507.21504v1 (agent-eval survey)
* **Claude Code's own TodoWrite list is a free corroborating signal** but is
  empty unless the agent used the tool, so it can't be the primary mechanism;
  we rely on the transcript delta and treat TodoWrite as optional context.
  https://docs.claude.com/en/docs/agent-sdk/todo-tracking

Cost guards: cheap model only (``config.llm_model``), capped at
``config.max_autoprogress_per_run`` sessions per daemon pass, and we never
re-read the whole transcript — only the delta since the last persisted offset.

Like :mod:`command_center.llm` this module never raises into the daemon: failures
degrade to "no change this pass".
"""

# Lazy `.llm` import keeps import cost off the daemon's fast paths.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import re
from dataclasses import dataclass, field
from pathlib import Path

from . import aimscore, checks, config
from .models import first_json_object, now_ms
from .store import Store

_DERIVE_PROMPT = """You are setting up a progress checklist for an AI coding session.
The session is "done" when: {aim}
{context}
Break that done-condition into 3 to 6 sub-goals that together mean it is done.

RULES — every sub-goal MUST be OBJECTIVELY VERIFIABLE:
- name a concrete, observable outcome: a file/artifact exists, a test passes, a
  command exits 0, or a measurable value is reached.
- short imperative phrase (<= 8 words), clearly done or not done.
- DO NOT use vague verbs (design, update, improve, refactor, handle, support,
  clean up, investigate, review) — they cannot be graded.
- Grade only the SUBSTANCE of the done-condition. DO NOT add process/ceremony
  steps — opening or merging a pull request, pushing, committing, code review,
  deployment, release — UNLESS the done-condition explicitly asks for one.
Order by dependency (earliest first). You MAY tag each "essential" or "optional".

Reply with STRICT minified JSON and nothing else (either item form is accepted):
{{"subgoals":["add tests/test_x.py","test_x passes","x handles empty input"]}}
or {{"subgoals":[{{"text":"test_x passes","importance":"essential"}}]}}"""

_ASSESS_PROMPT = """You are grading the progress of an AI coding session.
The session is "done" when: {aim}

Sub-goals (0-indexed):
{numbered}

Below is ONLY the conversation added since the last check (user messages and the
assistant's final replies; tool calls omitted). Earlier sub-goals may already be
satisfied from before — judge ONLY from this new evidence whether any *additional*
sub-goal has now clearly been completed.

New conversation:
{delta}

Be conservative: include an index ONLY if the new evidence clearly shows that
sub-goal is done. If it is merely planned, in progress, or ambiguous, leave it out.

Reply with STRICT minified JSON and nothing else, listing the 0-indexed sub-goals
that are now clearly satisfied:
{{"satisfied":[0,2]}}"""


@dataclass
class AutoProgressResult:
    """What an auto-progress pass did for one session (also for --dry-run preview)."""

    session_id: str
    derived: list[str] = field(default_factory=list)  # newly proposed sub-goals
    checked: list[str] = field(default_factory=list)  # sub-goal texts newly checked off
    note: str = ""  # why nothing happened, when applicable

    def changed(self) -> bool:
        return bool(self.derived or self.checked)


def lint_subgoal(text: str) -> str | None:
    """Return a reason a sub-goal isn't objectively checkable, or ``None`` if it's fine."""
    words = re.findall(r"[a-z]+", text.lower())
    if not words:
        return "empty"
    if words[0] in aimscore.VAGUE_WORDS:  # leads with an abstract verb -> ungradeable
        return f"starts with vague verb {words[0]!r}"
    if len(words) < 2:
        return "too short to verify"
    return None


def _clean_item(raw_text: str) -> str:
    """Strip leading list/number noise and clamp length."""
    return str(raw_text).strip().lstrip("-*0123456789. )").strip()[:120]


def parse_subgoal_items(raw: str | None) -> list[tuple[str, int]]:
    """Extract ``[(text, weight), …]`` from a model reply (capped at 6).

    Each item may be a plain string (weight 1) or an object
    ``{"text": …, "importance": "essential"|"optional"}`` (essential → weight 2).
    """
    data = first_json_object(raw)
    items = data.get("subgoals") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[tuple[str, int]] = []
    for item in items:
        if isinstance(item, dict):
            text = _clean_item(str(item.get("text", "")))
            weight = 2 if str(item.get("importance", "")).strip().lower().startswith("ess") else 1
        else:
            text, weight = _clean_item(str(item)), 1
        if text and len(out) < 6:
            out.append((text, weight))
    return out


def parse_subgoals(raw: str | None) -> list[str]:
    """Extract a clean ``subgoals`` text list from a (possibly chatty) model reply."""
    return [text for text, _weight in parse_subgoal_items(raw)]


def _verify_items(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Drop sub-goals that fail the lexical verifiability lint, preserving weights.

    If *every* item fails (a sign the AIM itself is vague — the aim score path
    handles that), keep the originals: a vague checklist still beats none.
    """
    kept = [(text, weight) for text, weight in items if lint_subgoal(text) is None]
    return kept or items


# Process/ceremony phrases the cheap model loves to append (the derive prompt used to
# *suggest* "a PR is merged" / "open PR"). They are almost never part of the AIM's
# substance and — for a direct-push-to-main workflow — are permanently unsatisfiable,
# capping progress below 100%. "merge" is matched ONLY in a PR/branch context so a
# substantive goal like "merge logic selects latest reset" is never mistaken for one.
_CEREMONY_RE = re.compile(
    r"pull request"
    r"|\bopen(?:s|ed|ing)?\s+(?:a\s+|the\s+)?pr\b"
    r"|\bpr\s+(?:is\s+)?merged\b"
    r"|\bmerge[sd]?\s+(?:the\s+)?(?:pr|pull request|branch)\b"
    r"|\bmerge[sd]?\s+(?:in)?to\s+(?:main|master)\b"
    r"|\bcode review\b|\bget(?:s|ting)?\s+reviewed\b"
    r"|\bdeploy|\brelease[sd]?\b"
    r"|\bpush(?:es|ed)?\s+(?:to|the|changes)\b"
    r"|\bcommit(?:s|ted)?\b",
    re.IGNORECASE,
)


def _drop_ceremony(items: list[tuple[str, int]], aim: str | None) -> list[tuple[str, int]]:
    """Strip process/ceremony sub-goals unless the AIM itself asks for one.

    If the done-condition explicitly mentions a PR / push / deploy / etc., the user
    wants it — keep everything. Otherwise drop ceremony items; never return empty.
    """
    if _CEREMONY_RE.search(aim or ""):
        return items
    kept = [(text, weight) for text, weight in items if not _CEREMONY_RE.search(text)]
    return kept or items


def parse_satisfied(raw: str | None, count: int) -> list[int]:
    """Extract the in-range, de-duplicated 0-based ``satisfied`` indices from a reply."""
    data = first_json_object(raw)
    items = data.get("satisfied") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    seen: set[int] = set()
    for item in items:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count:
            seen.add(idx)
    return sorted(seen)


def _run_predicate_checks(
    store: Store, session_id: str, cwd: str, subgoals: list, result: AutoProgressResult
) -> list:
    """Tick any unchecked sub-goal whose ``check_cmd`` exits 0 — deterministic, no LLM.

    Runs for sub-goals of any source (a user-defined check command is authoritative).
    Returns the sub-goal list, refreshed when something was ticked.
    """
    ran = False
    for sub in subgoals:
        if not sub.checked and sub.check_cmd and checks.run_exit0(sub.check_cmd, cwd):
            store.set_subgoal_checked(sub.id, True)
            result.checked.append(sub.text)
            ran = True
    if ran:
        store.update_fields(session_id, last_progress_at=now_ms())
        return store.list_subgoals(session_id)
    return subgoals


def run_for_session(
    store: Store,
    session_id: str,
    transcript_path: Path | None,
    *,
    model: str,
    dry_run: bool = False,
    full_regrade: bool = False,
) -> AutoProgressResult:
    """Derive sub-goals (if missing) and auto-check satisfied ones for one session.

    Never raises. Respects manual work: it only manages a checklist it authored
    (``source == 'auto'``) and never unchecks a goal that is already checked.

    ``full_regrade`` grades pending sub-goals against the WHOLE transcript instead of
    just the unseen delta, and leaves ``context_offset`` untouched. The per-turn delta
    grader is conservative and can miss evidence that is split across several turns;
    a periodic full re-grade (the daemon runs one when a session goes idle) lets the
    bar catch up to multi-turn behavioural goals. It never derives a new checklist.
    """
    # pylint: disable=too-many-return-statements,too-many-branches  # flat guard clauses
    from . import llm

    result = AutoProgressResult(session_id)
    session = store.get(session_id)
    if session is None or not session.aim or session.done or session.draft:
        result.note = "no aim / done / draft / unknown"
        return result

    # The session's first/original AIM — logged (not routed on) by ai.py so a derive/grade
    # call row says which session it served. Computed once for both LLM call sites below.
    note = llm.concise_note(
        next((r.aim for r in store.list_aim_history(session_id)), "") or session.aim
    )

    subgoals = store.list_subgoals(session_id)

    # --- 1. derive, only when there is no checklist at all -------------------
    if not subgoals:
        if full_regrade:  # nothing authored yet → nothing to re-grade
            result.note = "no checklist to re-grade"
            return result
        context = _derive_context(session.summary)
        proposed: list[str] = []
        if not dry_run:
            raw = llm.run_model(
                _DERIVE_PROMPT.format(aim=session.aim, context=context),
                model,
                purpose="subgoal-derive",
                note=note,
            )
            # drop ungradeable items, then ceremony steps the AIM never asked for
            items = _drop_ceremony(_verify_items(parse_subgoal_items(raw)), session.aim)
            proposed = [text for text, _w in items]
            if proposed:
                store.set_subgoals(
                    session_id,
                    proposed,
                    source="auto",
                    weights=[w for _t, w in items],
                    model=model,
                )
        result.derived = proposed
        # A checklist is often derived from a *retrospective* AIM — e.g. the end-of-turn
        # AIM-sharpen names what the session just finished — so the satisfying work is
        # ALREADY in the transcript, sitting behind the offset. Grade the whole transcript
        # against the fresh checklist right now (full_regrade) so the bar reflects that work
        # immediately, instead of stranding it at 0/N until a needs_summary-gated daemon
        # re-grade that never fires again for an idle/finished session. Then baseline the
        # delta offset so later turns grade incrementally and never re-grade this history.
        if proposed and transcript_path is not None and not dry_run:
            result.checked = run_for_session(
                store, session_id, transcript_path, model=model, full_regrade=True
            ).checked
        if transcript_path is not None and not dry_run:
            _, new_offset = llm.read_transcript_delta(transcript_path, session.context_offset)
            store.update_fields(session_id, context_offset=new_offset)
        return result

    # --- 2. machine-check predicates first: deterministic, any source, no LLM
    if not dry_run:
        subgoals = _run_predicate_checks(store, session_id, session.cwd, subgoals, result)

    # --- 3. LLM grading only manages an auto checklist we authored ----------
    if any(sg.source != "auto" for sg in subgoals):
        result.note = "user-authored checklist; predicates only"
        return result

    # The LLM only grades sub-goals without a predicate — a check_cmd is authoritative.
    pending = [sg for sg in subgoals if not sg.checked and not sg.check_cmd]
    if not pending:
        result.note = "all sub-goals checked or predicate-gated"
        return result

    # --- 4. read only the new transcript content, grade, check off ----------
    if transcript_path is None:
        result.note = "no transcript"
        return result
    # full_regrade reads the whole transcript (offset 0) so multi-turn evidence is seen.
    offset = 0 if full_regrade else session.context_offset
    delta, new_offset = llm.read_transcript_delta(transcript_path, offset)
    if not delta.strip():
        result.note = "no new transcript content"
        return result
    if dry_run:
        result.note = f"would grade {len(pending)} pending sub-goal(s) against new context"
        return result

    numbered = "\n".join(f"{i}. {sg.text}" for i, sg in enumerate(subgoals))
    raw = llm.run_model(
        _ASSESS_PROMPT.format(aim=session.aim, numbered=numbered, delta=delta),
        model,
        purpose="subgoal-grade",
        note=note,
    )
    satisfied = set(parse_satisfied(raw, len(subgoals)))
    for idx, sub in enumerate(subgoals):
        # never uncheck; never override a predicate-gated sub-goal
        if idx in satisfied and not sub.checked and not sub.check_cmd:
            store.set_subgoal_checked(sub.id, True)
            result.checked.append(sub.text)
    # Stamp the debounce clock only on a real grading pass (not derive / no-delta),
    # so a no-op after-turn spawn never suppresses the next genuine grade. A full
    # re-grade leaves context_offset alone so the normal delta grader still advances it.
    if full_regrade:
        store.update_fields(session_id, last_progress_at=now_ms())
    else:
        store.update_fields(session_id, context_offset=new_offset, last_progress_at=now_ms())
    return result


def _derive_context(summary: str | None) -> str:
    return f"Current state: {summary}\n" if summary and summary.strip() else ""


def run_pass(store: Store, adapter: object, *, dry_run: bool = False) -> list[AutoProgressResult]:
    """Run auto-progress for every eligible session, capped per pass (cost guard).

    Eligible = not done, has an AIM, and is live or parked (we skip nothing on
    status beyond ``done`` — a parked session can still get its checklist filled).
    """
    cfg = config.load_config()
    candidates = [
        s
        for s in store.list_sessions()
        if not s.done and s.aim and s.aim.strip() and s.status != "done"
    ]
    # Process the freshest first; cap the number of LLM passes per run.
    candidates.sort(key=lambda s: s.last_response_at, reverse=True)
    results: list[AutoProgressResult] = []
    for session in candidates[: max(0, cfg.max_autoprogress_per_run)]:
        transcript = _transcript_path(adapter, session.cwd, session.session_id)
        results.append(
            run_for_session(
                store, session.session_id, transcript, model=cfg.llm_model, dry_run=dry_run
            )
        )
    return results


def _transcript_path(adapter: object, cwd: str, session_id: str) -> Path | None:
    getter = getattr(adapter, "transcript_path", None)
    if getter is None:
        return None
    try:
        result = getter(cwd, session_id)
    except OSError:
        return None
    return result if isinstance(result, Path) else None
