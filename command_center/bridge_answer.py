#!/usr/bin/env python3
"""``ccc answer -s ID -j`` — answer a pending AskUserQuestion picker with raw keys.

stdin: ``{"decision_id": "<sha256>", "answers": [{"question_index": 0, "option_indices":
[1]} | {"question_index": 1, "other_text": "…"}, …]}`` — one entry per question; unknown
fields are a usage error (exit 2).

Only with exactly ONE pending AskUserQuestion whose recomputed ``decision_id`` (the
``ccc inspect`` one) matches — else ``decision_changed``. The same target revalidation as
``ccc send`` runs inside the per-tab lock; the raw status must be ``waiting``. Keys go
through :func:`terminal.send_keys_via` (iTerm2 Python API only, no focus change); the
answer is verified through the pending call's ``tool_use_id`` and the top-level
``toolUseResult.answers`` (keyed by full question text; multi-select labels joined by
``", "``) within :data:`VERIFY_SEC` — every expected label must match.

The picker protocol lives in ONE table-driven function, :func:`picker_keys`, confirmed
by the attended S-PICKER spike (2026-10-09, every shape answered without a focus
change); adjust :data:`PICKER` / :data:`MULTI_SELECT_SUPPORTED` there, nowhere else.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import re
from typing import TYPE_CHECKING, Any

from .adapters import claude_bridge as cb
from .bridge_json import BridgeError, check_fields, control_chars, usage
from .bridge_send import inspect_transcript, transcript_of
from .bridge_target import require_live, revalidate, tab_for, tab_lock

if TYPE_CHECKING:
    from .bridge_target import BridgeDeps

#: Seconds to wait for the call's tool_result after the last key.
VERIFY_SEC = 10.0
POLL_SEC = 0.2
#: Raw statuses in which a picker can be answered.
ANSWER_STATUSES = ("waiting",)
#: Longest free-text ("Type something") answer.
OTHER_TEXT_MAX = 500
#: S-PICKER found a deterministic multi-select submit (the Submit row); False would
#: refuse every multi-select question with ``unsupported_shape``.
MULTI_SELECT_SUPPORTED = True

#: The picker protocol (S-PICKER, attended, 2026-10-09). Each question's list is options
#: 1..N, then N+1 "Type something", then — multi-select only — a "Submit" row, then
#: "Chat about this" (never reached: every count below is exact).
#:
#: * ONE single-select question: ``down`` × index, ``enter`` → submitted at once (no
#:   review screen).
#: * ONE multi-select question: digit N toggles option N without moving the focus;
#:   ``down`` × (N + 1) reaches Submit, ``enter`` → "Review your answers", ``enter``
#:   (= "1. Submit answers") → submitted.
#: * SEVERAL questions: a single-select ``enter`` / a multi-select Submit advances to the
#:   next question's tab; after the last one the review screen, ``enter`` submits.
#: * ``other_text`` (single-select only): ``down`` × N to "Type something", the text,
#:   ``enter`` — NOT verified by the attended run (moderate confidence).
#:
#: A short ``settle`` wait follows every question that another screen follows (the next
#: question or the review screen), so the TUI re-renders before the next key.
PICKER: dict[str, Any] = {
    "move": "down",
    "pick": "enter",
    "submit_steps_after_options": 1,
    "settle": "wait:0.6",
    "review_submit": "enter",
    "single_single_select_has_review": False,
    "max_digit_option": 9,
}

_DECISION_ID_RE = re.compile(r"[0-9a-f]{64}")


def parse_payload(raw: Any) -> tuple[str, list[dict[str, Any]]]:
    """Validate the stdin object's SHAPE (usage errors); semantics come later."""
    if not isinstance(raw, dict):
        raise usage("invalid_json", "stdin must be a JSON object")
    check_fields(raw, {"decision_id", "answers"}, "input")
    did = raw.get("decision_id")
    if not isinstance(did, str) or not _DECISION_ID_RE.fullmatch(did):
        raise usage("invalid_decision_id", "decision_id must be a 64-hex sha256")
    answers = raw.get("answers")
    if not isinstance(answers, list) or not answers:
        raise usage("invalid_answers", "answers must be a non-empty list")
    for i, ans in enumerate(answers):
        if not isinstance(ans, dict):
            raise usage("invalid_answers", f"answers[{i}] is not an object")
        if "option_indices" in ans and "other_text" in ans:
            raise usage("invalid_answers", f"answers[{i}]: option_indices OR other_text")
        allowed = {"question_index", "other_text" if "other_text" in ans else "option_indices"}
        check_fields(ans, allowed, f"answers[{i}]")
        qi = ans.get("question_index")
        if not isinstance(qi, int) or isinstance(qi, bool):
            raise usage("invalid_answers", f"answers[{i}].question_index must be an integer")
        if "other_text" in ans:
            if not isinstance(ans["other_text"], str):
                raise usage("invalid_answers", f"answers[{i}].other_text must be a string")
        else:
            opts = ans.get("option_indices")
            if (
                not isinstance(opts, list)
                or not opts
                or any(not isinstance(o, int) or isinstance(o, bool) for o in opts)
            ):
                raise usage(
                    "invalid_answers", f"answers[{i}].option_indices must be a non-empty int list"
                )
    return did, answers


def _by_question(questions: list[cb.Question], answers: list[dict[str, Any]]) -> list[dict]:
    """*answers* ordered by question, each question answered exactly once (refusals)."""
    seen: dict[int, dict[str, Any]] = {}
    for ans in answers:
        qi = int(ans["question_index"])
        if not 0 <= qi < len(questions):
            raise BridgeError("invalid_answer", f"question_index {qi} out of range")
        if qi in seen:
            raise BridgeError("invalid_answer", f"question {qi} answered twice")
        seen[qi] = ans
    missing = [i for i in range(len(questions)) if i not in seen]
    if missing:
        raise BridgeError("invalid_answer", f"no answer for question(s) {missing}")
    return [seen[i] for i in range(len(questions))]


def _question_keys(q: cb.Question, ans: dict[str, Any]) -> list[str]:
    """The keys that answer ONE question (a refusal on a bad shape), without the settle."""
    p = PICKER
    n = len(q.options)
    if "other_text" in ans:
        text = str(ans["other_text"])
        if q.multi_select:
            raise BridgeError("unsupported_shape", "free text on a multi-select question")
        if not text.strip() or len(text) > OTHER_TEXT_MAX or control_chars(text, ""):
            raise BridgeError("invalid_answer", "other_text is empty, too long or has controls")
        # Unverified by the attended S-PICKER run (see PICKER).
        return [p["move"]] * n + [f"text:{text}", p["pick"]]
    idx = [int(o) for o in ans["option_indices"]]
    if len(set(idx)) != len(idx) or any(not 0 <= o < n for o in idx):
        raise BridgeError("invalid_answer", "option_indices out of range or repeated")
    if not q.multi_select:
        if len(idx) != 1:
            raise BridgeError("invalid_answer", "a single-select question takes one option")
        return [p["move"]] * idx[0] + [p["pick"]]
    if not MULTI_SELECT_SUPPORTED:
        raise BridgeError("unsupported_shape", "multi-select answers are not supported")
    if any(o + 1 > p["max_digit_option"] for o in idx):
        raise BridgeError("unsupported_shape", "option beyond digit 9")
    steps = n + p["submit_steps_after_options"]
    return [str(o + 1) for o in idx] + [p["move"]] * steps + [p["pick"]]


def picker_keys(questions: list[cb.Question], answers: list[dict[str, Any]]) -> list[str]:
    """The key tokens that answer *questions* with *answers* (refusals on bad shapes)."""
    p = PICKER
    keys: list[str] = []
    for q, ans in zip(questions, _by_question(questions, answers), strict=True):
        keys += _question_keys(q, ans)
        keys.append(p["settle"])
    single = len(questions) == 1 and not questions[0].multi_select
    if not single or p["single_single_select_has_review"]:
        keys.append(p["review_submit"])
    elif keys and keys[-1] == p["settle"]:
        keys.pop()  # nothing follows the pick: it submits on its own
    return keys


def expected_answers(
    pending: cb.PendingAsk, questions: list[cb.Question], answers: list[dict[str, Any]]
) -> dict[str, str | list[str]]:
    """What ``toolUseResult.answers`` must hold: raw question text → raw label(s)/text."""
    texts = cb.raw_question_texts(pending)
    out: dict[str, str | list[str]] = {}
    for qtext, q, ans in zip(texts, questions, _by_question(questions, answers), strict=True):
        if "other_text" in ans:
            out[qtext] = str(ans["other_text"])
        elif q.multi_select:
            out[qtext] = [q.raw_labels[int(o)] for o in ans["option_indices"]]
        else:
            out[qtext] = q.raw_labels[int(ans["option_indices"][0])]
    return out


def _split_multi(answer: str, labels: list[str]) -> list[str]:
    """Multi-select answer → labels (a label may itself contain ``", "``)."""
    parts = answer.split(", ")
    out: list[str] = []
    i = 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            candidate = ", ".join(parts[i:j])
            if candidate in labels:
                out.append(candidate)
                i = j
                break
        else:
            out.append(parts[i])
            i += 1
    return out


def answers_match(
    want: dict[str, str | list[str]], got: dict[str, str], labels: dict[str, list[str]]
) -> list[str]:
    """The question texts whose recorded answer differs from *want* (``[]`` = all match)."""
    bad: list[str] = []
    for qtext, expected in want.items():
        have = got.get(qtext)
        if have is None:
            bad.append(qtext)
        elif isinstance(expected, list):
            if sorted(_split_multi(have, labels.get(qtext, []))) != sorted(expected):
                bad.append(qtext)
        elif cb.normalise(have) != cb.normalise(expected):
            bad.append(qtext)
    return bad


def _verify(
    deps: BridgeDeps, anchor: cb.Anchor, tool_use_id: str, deadline: float
) -> cb.AskResult | None:
    while True:
        try:
            located = cb.read_appended(anchor.path, anchor.inode, anchor.size)
        except (cb.TranscriptReplaced, OSError):
            return None
        for loc in located:
            result = cb.ask_result(loc.record, tool_use_id)
            if result is not None:
                return result
        if deps.monotonic() >= deadline:
            return None
        deps.sleep(POLL_SEC)


def run_answer(deps: BridgeDeps, session_id: str, raw: Any) -> dict[str, Any]:
    """Answer the pending picker; the ``data`` object (raises :class:`BridgeError`)."""
    did, answers = parse_payload(raw)
    live = require_live(deps, session_id)
    tab = tab_for(deps, live)
    with tab_lock(deps, tab):
        target = revalidate(deps, session_id, tab)
        if target.status not in ANSWER_STATUSES:
            raise BridgeError("not_waiting", f"session {session_id} is {target.status}")
        path = transcript_of(deps, session_id, target.live.cwd, target.live.config_dir)
        ins = inspect_transcript(path)
        if len(ins.pending) != 1 or ins.source != "ask_user_question" or not ins.anchor:
            raise BridgeError("decision_changed", "no single pending question picker")
        if cb.decision_id(ins.anchor, ins.questions) != did:
            raise BridgeError("decision_changed", "the pending decision is not the one answered")
        keys = picker_keys(ins.questions, answers)
        want = expected_answers(ins.pending[0], ins.questions, answers)
        labels = dict(
            zip(
                cb.raw_question_texts(ins.pending[0]),
                [q.raw_labels for q in ins.questions],
                strict=True,
            )
        )
        anchor = cb.Anchor.take(path)
        deadline_start = deps.monotonic()
        sent = deps.send_keys(tab, keys)
        data: dict[str, Any] = {
            "decision_id": did,
            "tool_use_id": ins.anchor,
            "outcome": "failed",
            "channel": "python-api" if sent != "none" else None,
            "keys": sum(1 for k in keys if not k.startswith("wait:")),
        }
        if sent == "none":
            raise BridgeError("answer_failed", "no key reached the tab", data=data)
        result = _verify(deps, anchor, ins.anchor, deadline_start + VERIFY_SEC)
        if result is None:
            data["outcome"] = "unknown"
            raise BridgeError(
                "answer_unknown", f"no answer recorded within {VERIFY_SEC:g} s", data=data
            )
        if result.is_error:
            raise BridgeError("answer_failed", "the picker was cancelled", data=data)
        bad = answers_match(want, result.answers, labels)
        if bad:
            data["mismatched_questions"] = [i for i, t in enumerate(want) if t in bad]
            raise BridgeError("answer_mismatch", "the recorded answer differs", data=data)
        data["outcome"] = "accepted"
        return data
