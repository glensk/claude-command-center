"""tp#1835: the composed handoff always fits the 24 KB cap (facts tiers 9-11, the guard)."""

from __future__ import annotations

from pathlib import Path

import bigsnap
from scrubstub import stub_scrubber

from command_center import handoff_doc
from command_center.handoff_facts import rec

BUDGET = handoff_doc.MAX_BYTES - handoff_doc.SCRUB_MARGIN
FACTS_CAP = BUDGET - handoff_doc.FACTS_FLOOR_RESERVE
REF = bigsnap.SID


def _size(lines: dict[str, list[str]]) -> int:
    return sum(len(ln.encode("utf-8")) + 1 for body in lines.values() for ln in body)


def _text(lines: dict[str, list[str]]) -> str:
    return "\n".join(ln for body in lines.values() for ln in body)


def _state(tp_id: int) -> str:
    return "waiting_queue" if tp_id % 2 else "work"


def _assert_never_cut(text: str) -> None:
    assert "AIM (done-condition): AIM-BIG: every held item relayed" in text
    for tp_id in bigsnap.HELD:
        assert f"tp#{tp_id} [{_state(tp_id)}" in text, tp_id
    for n, key in enumerate(bigsnap.QUESTION_KEYS):
        assert f"QUESTION-{n}" in text and f"-k {key}" in text
    assert "YOU-LINE: decide the schema" in text and "-k you_line:1" in text
    for n, key in enumerate(bigsnap.WARNING_KEYS):
        assert f"WARNING-{n}" in text and f"-k {key}" in text
    assert text.count("- restart: `tool watch") == 2
    assert bigsnap.SPEC_PATHS[0] in text


# ------------------------------------------------------------------------------- B1


def test_facts_at_the_floor_fit_the_cap() -> None:
    doc = bigsnap.big_snapshot()
    assert _size(handoff_doc._facts_lines(doc, 8, REF)) > BUDGET  # the failing shape
    lines = handoff_doc._facts_lines(doc, handoff_doc.MAX_LEVEL, REF, FACTS_CAP)
    assert _size(lines) <= FACTS_CAP
    text = _text(lines)
    _assert_never_cut(text)
    keys = {f["key"] for f in doc["facts"]}
    assert "-k core:agent:a-relaunched" in text and "core:agent:a-relaunched" in keys
    bare = f"— full: ccc continue-session facts {REF}"
    assert any(ln.endswith(bare) for ln in lines["tp ownership"])  # held fields + plans
    assert f"… 24 worktrees {bare}" in lines["Repositories, files, worktrees"]
    assert f"… 12 plans {bare}" in lines["tp ownership"]


def test_the_guard_never_cuts_protected_lines() -> None:
    doc = bigsnap.big_snapshot()
    doc["facts"] += [
        rec("note", f"note:{n}", "Repositories, files, worktrees", 4, text="n" * 400)
        for n in range(30)
    ]
    doc["facts"] += [
        rec("note", f"note:o{n}", "Open items and questions", 4, text="o" * 400) for n in range(30)
    ]
    unguarded = handoff_doc._facts_lines(doc, handoff_doc.MAX_LEVEL, REF)
    cap = 4000
    assert _size(unguarded) > cap
    lines = handoff_doc._facts_lines(doc, handoff_doc.MAX_LEVEL, REF, cap)
    assert _size(lines) < _size(unguarded)
    text = _text(lines)
    _assert_never_cut(text)
    assert f"… cut — full: ccc continue-session facts {REF}" in text
    assert "n" * 400 not in text  # the unprotected section went first


def test_released_items_stay_one_line() -> None:
    doc = bigsnap.big_snapshot()
    for level in (9, 10):
        body = handoff_doc._facts_lines(doc, level, REF)["tp ownership"]
        for tp_id in bigsnap.HELD:
            (line,) = [ln for ln in body if ln.startswith(f"- tp#{tp_id} [")]
            assert ("record only" in line) == (tp_id in bigsnap.RELEASED)
    floor = _text(handoff_doc._facts_lines(doc, handoff_doc.MAX_LEVEL, REF))
    for tp_id in bigsnap.RELEASED:
        assert f"tp#{tp_id} [{_state(tp_id)}, record]" in floor


def test_tier_9_drops_blocked_json() -> None:
    doc = bigsnap.big_snapshot()
    assert '{"background"' in "\n".join(handoff_doc._facts_lines(doc, 8, REF)["tp ownership"])
    for level in range(9, handoff_doc.MAX_LEVEL + 1):
        body = "\n".join(handoff_doc._facts_lines(doc, level, REF)["tp ownership"])
        assert '{"background"' not in body and '"claim"' not in body
    line = handoff_doc._facts_lines(doc, 9, REF)["tp ownership"][0]
    assert "; plan /repo/area/PLAN_item-100.md 3/9" in line
    assert "; blocked: queue — waiting for the repo lock" in line
    assert "; 2 assumptions" in line


def test_never_cut_overflow_degrades_to_id_and_state() -> None:
    doc = bigsnap.big_snapshot(held=200, title_bytes=200)
    lines = handoff_doc._facts_lines(doc, handoff_doc.MAX_LEVEL, REF, FACTS_CAP)
    assert _size(lines) <= FACTS_CAP
    text = _text(lines)
    for tp_id in range(100, 300):
        assert f"tp#{tp_id} [{_state(tp_id)}" in text, tp_id
    assert "t" * 200 not in text


def test_legacy_notes_still_drop_at_tier_8(tmp_path: Path) -> None:
    doc = bigsnap.big_snapshot(held=16)
    doc["facts"] = [f for f in doc["facts"] if f["kind"] in ("session", "tp_item")]
    legacy = (
        "# Handoff\n\n## Current state\n\nstate\n\n## Decisions and constraints\n\nnone\n\n"
        "## First steps\n\naccept\n\n## Repositories, files, worktrees\n\n" + "w" * 9000 + "\n"
    )
    model = handoff_doc.parse_model_file(legacy)
    assert model.ok and model.legacy
    assert len(handoff_doc.compose(model, doc, level=7).text.encode()) > BUDGET
    stub = stub_scrubber(tmp_path)
    result, composed = handoff_doc.compose_checked(
        model, doc, ref=REF, model_path="/x/m.model.md", scrub_policy=stub.scrub_cmd
    )
    assert result.ok, result.errors
    assert composed.level == handoff_doc.NOTES_DROP_LEVEL and composed.model_cut
    assert "### Model notes" in result.text and "w" * 9000 not in result.text
    assert '{"background"' in result.text  # no tier-9 compaction was applied
