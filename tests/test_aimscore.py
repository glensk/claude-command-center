"""Unit tests for the lexical AIM scorer + the LLM-refine parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import aimscore, config

_THRESHOLD = 50  # default aim_score_threshold
_CFG = config.Config()  # default ladder = ["claude"] → the claude rung calls llm.run_model


def test_lexical_scorer_agrees_with_labels() -> None:
    """Calibration: the lexical scorer's vague/specific split tracks hand labels.

    A heuristic, so we assert a high agreement rate (regression guard) rather than
    perfection, plus the two motivating aims must read as vague.
    """
    labels = json.loads((Path(__file__).parent / "fixtures" / "aim_labels.json").read_text())
    agree = sum(
        1 for row in labels if (aimscore.score_aim_lexical(row["aim"]) < _THRESHOLD) == row["vague"]
    )
    assert agree / len(labels) >= 0.8
    assert aimscore.score_aim_lexical("improve the progress bar") < _THRESHOLD
    assert aimscore.score_aim_lexical("restructure and rename ccc") < _THRESHOLD


def test_lexical_empty_is_zero() -> None:
    assert aimscore.score_aim_lexical(None) == 0
    assert aimscore.score_aim_lexical("   ") == 0


def test_lexical_vague_below_threshold() -> None:
    # The aims that motivated this work must score vague (< 50).
    assert aimscore.score_aim_lexical("improve progress bar") < 50
    assert aimscore.score_aim_lexical("restructure and rename ccc") < 50
    assert aimscore.score_aim_lexical("make it better") < 50


def test_lexical_concrete_above_threshold() -> None:
    assert aimscore.score_aim_lexical("all tests in tests/ pass and PR #42 merged") >= 50
    assert aimscore.score_aim_lexical("ccc daemon exits 0 and test_store passes") >= 50


def test_lexical_clamped_0_100() -> None:
    score = aimscore.score_aim_lexical("tests pass coverage 100 deployed merged build green #1")
    assert 0 <= score <= 100


def test_score_aim_llm_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import llm

    monkeypatch.setattr(
        llm, "run_model", lambda *_a, **_k: '{"score": 82, "reason": "names a passing test"}'
    )
    result = aimscore.score_aim_llm("tests pass", _CFG)
    assert result == (82, "names a passing test")


def test_score_aim_llm_none_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import llm

    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: "not json at all")
    assert aimscore.score_aim_llm("tests pass", _CFG) is None


def test_score_aim_llm_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import llm

    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: '{"score": 250, "reason": "x"}')
    result = aimscore.score_aim_llm("tests pass", _CFG)
    assert result is not None and result[0] == 100


def test_score_aim_detailed_returns_breakdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The independent checker returns a per-criterion breakdown + an actionable `missing` hint."""
    from command_center import llm

    monkeypatch.setattr(
        llm,
        "run_model",
        lambda *_a, **_k: (
            '{"score":35,"criteria":{"end_state":15,"objective_check":0,"bounded":20,'
            '"no_vague":0},"reason":"names a file but no pass/fail check",'
            '"missing":"add a command that exits 0 to decide done"}'
        ),
    )
    detail = aimscore.score_aim_detailed("edit foo.py", _CFG)
    assert detail is not None
    assert detail["score"] == 35
    assert detail["criteria"] == {
        "end_state": 15,
        "objective_check": 0,
        "bounded": 20,
        "no_vague": 0,
    }
    assert detail["missing"] == "add a command that exits 0 to decide done"
    assert detail["backend"] == "router"


def test_score_aim_llm_folds_missing_into_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored reason carries the `missing` fix hint so the UI says how to improve."""
    from command_center import llm

    monkeypatch.setattr(
        llm,
        "run_model",
        lambda *_a, **_k: (
            '{"score":40,"reason":"leads with a vague verb","missing":"name a passing test"}'
        ),
    )
    result = aimscore.score_aim_llm("improve things", _CFG)
    assert result is not None
    score, reason = result
    assert score == 40
    assert "leads with a vague verb" in reason
    assert "fix: name a passing test" in reason


def test_score_aim_detailed_none_when_router_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Router failure → `None`, so the caller keeps the provisional lexical score."""
    from command_center import llm

    monkeypatch.setattr(llm, "run_model", lambda *_a, **_k: None)
    assert aimscore.score_aim_detailed("tests pass", _CFG) is None
    assert aimscore.score_aim_llm("tests pass", _CFG) is None


def test_score_aim_passes_aim_score_purpose_and_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scorer labels its routed call ``aim-score`` and forwards the first-AIM note.

    Both are router metadata (exported as ``CCC_LLM_PURPOSE`` / ``CCC_LLM_NOTE`` to the
    rung subprocesses) — they never affect the score itself.
    """
    from command_center import llm

    seen: dict[str, object] = {}

    def _capture(_prompt: str, _model: str, *, purpose: str = "", note: str = "") -> str:
        seen["purpose"] = purpose
        seen["note"] = note
        return '{"score": 70, "reason": "ok"}'

    monkeypatch.setattr(llm, "run_model", _capture)
    result = aimscore.score_aim_llm("tests pass", _CFG, note="ship the parser")
    assert result is not None and result[0] == 70
    assert seen["purpose"] == "aim-score"
    assert seen["note"] == "ship the parser"


def test_rubric_is_published_and_shared() -> None:
    """The rubric the checker scores against is a shared, importable constant (transparent)."""
    assert "end_state" in aimscore.AIM_RUBRIC
    assert "objective_check" in aimscore.AIM_RUBRIC
    assert aimscore.AIM_RUBRIC in aimscore._AIM_SCORE_PROMPT  # checker scores against it
