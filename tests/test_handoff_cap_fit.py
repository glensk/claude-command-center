"""tp#1835: the composed handoff always fits the 24 KB cap (facts tiers 9-11, the guard)."""

from __future__ import annotations

import json
from pathlib import Path

import bigsnap
import pytest
from scrubstub import DUMMY_VALUE, PLACEHOLDER, stub_scrubber

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
    doc = bigsnap.big_snapshot(held=40)  # tp#1681 L3c2: the held line made 25 items fit
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
    tier8 = "\n".join(handoff_doc._facts_lines(doc, 8, REF)["tp ownership"])
    assert tier8.startswith("- held: tp#100 [work]")  # tp#1681 L3c2: below tier 9
    assert "  tp#108 blocked: queue — waiting for the repo lock" in tier8
    for level in range(8, handoff_doc.MAX_LEVEL + 1):
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
    doc = bigsnap.big_snapshot(held=36)
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
    assert "- held: tp#100 [work]" in result.text  # no tier-9 compaction was applied


# ------------------------------------------------------------------------------- B2


def _facts_only_part(doc: dict, tmp_path: Path) -> handoff_doc.ModelPart:
    from command_center import handoff_capped  # pylint: disable=import-outside-toplevel

    transcript = tmp_path / "t.jsonl"
    texts = [f"ASSISTANT-{n}: " + "a" * 1200 for n in range(3)]
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}})
            for t in texts
        )
        + "\n",
        encoding="utf-8",
    )
    text = handoff_capped.model_part(REF, doc, transcript, None, use_router=False)
    model = handoff_doc.validate_model_part(text)
    assert model.ok, model.errors
    return model


def _checked(
    model: handoff_doc.ModelPart, doc: dict, tmp_path: Path, *, generated: bool = False
) -> tuple[handoff_doc.HandoffDoc, handoff_doc.Composed]:
    stub = stub_scrubber(tmp_path)
    return handoff_doc.compose_checked(
        model,
        doc,
        ref=REF,
        model_path="/x/m.model.md",
        scrub_policy=stub.scrub_cmd,
        generated=generated,
    )


def test_facts_only_always_fits(tmp_path: Path) -> None:
    from command_center import handoff_capped  # pylint: disable=import-outside-toplevel

    for held, title in ((25, 0), (200, 200)):
        doc = bigsnap.big_snapshot(held=held, title_bytes=title)
        model = _facts_only_part(doc, tmp_path)
        result, composed = _checked(model, doc, tmp_path, generated=True)
        assert result.ok, result.errors
        assert len(result.text.encode()) <= handoff_doc.MAX_BYTES
        state = handoff_doc.sections(result.text)["current state"]
        assert handoff_capped.EMERGENCY_BANNER in state
        assert "Predecessor transcript (evidence only):" in state
        assert "First steps" in result.text and handoff_capped.FIXED_FIRST_STEPS in result.text
        if held == 25:
            assert composed.level <= handoff_doc.COLLAPSE_LEVEL  # tier-9 titles survive
            assert "a long ticket title" in result.text
        if composed.model_cut:
            assert "full original: /x/m.model.md" in result.text


def test_a_cut_generated_part_keeps_banner_and_transcript(tmp_path: Path) -> None:
    doc = bigsnap.big_snapshot(held=200, title_bytes=200)
    doc["facts"] += [
        rec("note", f"note:{n}", "Repositories, files, worktrees", 4, text="n" * 400)
        for n in range(80)
    ]
    model = _facts_only_part(doc, tmp_path)
    result, composed = _checked(model, doc, tmp_path, generated=True)
    assert result.ok, result.errors
    assert composed.model_cut and composed.level == handoff_doc.MAX_LEVEL
    state = handoff_doc.sections(result.text)["current state"]
    body = state[state.index("EMERGENCY RELAY") :].splitlines()
    assert body[-1].startswith("Predecessor transcript (evidence only):")
    assert "… cut — full original: /x/m.model.md" in "\n".join(body)
    _assert_never_cut(result.text)


def test_written_model_part_refusal_names_the_room(tmp_path: Path) -> None:
    doc = bigsnap.big_snapshot(held=200, title_bytes=200)
    doc["facts"] += [
        rec("note", f"note:{n}", "Repositories, files, worktrees", 4, text="n" * 400)
        for n in range(80)
    ]
    body = "x" * 3300 + "\n"
    big = handoff_doc.validate_model_part(
        "".join(f"## {t}\n\n{body}\n" for t in handoff_doc.MODEL_SECTIONS)
    )
    assert big.ok
    result, _ = _checked(big, doc, tmp_path)
    assert not result.ok
    (msg,) = [e for e in result.errors if "at most" in e]
    room = int(msg.split("at most ")[1].split(" bytes")[0])
    assert 0 < room < 10 * 1024
    third, rest = divmod(room, 3)
    sizes = [third + rest, third, third]
    exact = handoff_doc.validate_model_part(
        "".join(
            f"## {t}\n\n{'y' * n}\n\n"
            for t, n in zip(handoff_doc.MODEL_SECTIONS, sizes, strict=True)
        )
    )
    assert sum(len(b.encode()) for b in exact.sections.values()) == room
    result, _ = _checked(exact, doc, tmp_path)
    assert result.ok, result.errors


def test_facts_alone_overflow_does_not_blame_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handoff_doc, "_guard_facts", lambda rendered, _cap, _ref: rendered)
    doc = bigsnap.big_snapshot()
    doc["facts"] += [
        rec("note", f"note:{n}", "Repositories, files, worktrees", 4, text="n" * 400)
        for n in range(80)
    ]
    for generated in (False, True):
        model = _facts_only_part(doc, tmp_path)
        result, _ = _checked(model, doc, tmp_path, generated=generated)
        assert not result.ok
        text = " ".join(result.errors)
        assert "facts alone" in text and "shorten" not in text


def test_scrub_growth_is_budgeted(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    stub.path.write_text(
        stub.path.read_text(encoding="utf-8").replace(
            f'sed "s/{DUMMY_VALUE}/{PLACEHOLDER}/g"',
            'sed "s/GROW/GROWGROWGROWGROWGROW/g"',
        ),
        encoding="utf-8",
    )
    doc = bigsnap.big_snapshot()
    doc["facts"][0]["fields"]["aim"] += " GROW" * 200  # +3.2 KB after the scrub
    model = _facts_only_part(doc, tmp_path)
    result, _ = handoff_doc.compose_checked(
        model,
        doc,
        ref=REF,
        model_path="/m",
        scrub_policy=stub.scrub_cmd,
        generated=True,
    )
    assert result.ok, result.errors
    assert len(result.text.encode()) <= handoff_doc.MAX_BYTES
