"""Tests for the store-only session card (:mod:`command_center.sessioncard`, ``ccc show``).

Hermetic: a tmp ``CLAUDE_HOME`` (store + transcripts) and a tmp ``$GIT_BASE``. No vault,
no scrubber — the card is printed, never written. The mirror documents the module also
renders stay pinned by ``tests/test_mirrors.py`` (unchanged by the lift).
"""

# pylint: disable=redefined-outer-name  # pytest fixtures are injected by parameter name

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from futurestub import hermetic_ccc

from command_center import cli, mirrors, sessioncard
from command_center.adapters import ClaudeAdapter
from command_center.models import Status, now_ms
from command_center.store import Store

# The pinned ``ccc show -j`` key set (schema_version 1): a change here is a schema change.
CARD_KEYS = {
    "schema_version",
    "session_id",
    "id",
    "phase",
    "status",
    "archived",
    "repo",
    "cwd",
    "aim",
    "first_aim",
    "short_aim",
    "aim_history",
    "next_step",
    "summary",
    "blocked_on",
    "subgoals",
    "progress",
    "job_type",
    "llm_overseer",
    "llm_exec",
    "model",
    "effort",
    "importance",
    "deadline",
    "depends_on",
    "created",
    "last_response",
    "done_at",
    "transcript",
    "resume",
    "prompt",
    "start_date",
    "start_when",
    "conversation",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Store, Path]]:
    git_base, _vault = hermetic_ccc(tmp_path, monkeypatch)
    # The caller's session must come from the test, never from the shell running pytest.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    sessioncard.VIEW_CACHE.clear()
    store = Store(tmp_path / "claude" / "command-center" / "state.db")
    yield store, git_base
    store.close()
    sessioncard.VIEW_CACHE.clear()


def _running(store: Store, git_base: Path, aim: str) -> str:
    sid = str(uuid.uuid4())
    store.ensure(sid, cwd=str(git_base / "home" / "ccc"))
    store.set_aim(sid, aim)
    return sid


def _write_transcript(store: Store, cwd: str, sid: str, prompts: list[str]) -> Path:
    """A minimal Claude transcript (user records + one assistant reply) under CLAUDE_HOME."""
    proj = store.path.parents[1] / "projects" / cwd.replace("/", "-")
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    records: list[dict[str, object]] = [
        {"type": "user", "message": {"role": "user", "content": text}} for text in prompts
    ]
    records.append(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-1",
                "content": [{"type": "text", "text": "assistant reply here"}],
            },
        }
    )
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _card(store: Store, git_base: Path, sid: str, **kw: bool) -> dict[str, Any]:
    session = store.get(sid)
    assert session is not None
    return sessioncard.build_card(store, session, ClaudeAdapter(), git_base, **kw)


# ---------------------------------------------------------------------------
# the lift is pure: mirrors uses the very same objects
# ---------------------------------------------------------------------------
def test_mirrors_reuses_the_sessioncard_objects() -> None:
    assert mirrors._VIEW_CACHE is sessioncard.VIEW_CACHE  # pylint: disable=protected-access
    assert mirrors._serialize_session is sessioncard.serialize_session  # pylint: disable=protected-access
    assert mirrors.RUNNING == sessioncard.RUNNING and mirrors.DONE == sessioncard.DONE
    # The renderer's source is part of the card fingerprint: an edit re-renders every card.
    assert "sessioncard.py" in mirrors._CODE_MODULES  # pylint: disable=protected-access


def test_render_prompts_caps_are_parameters() -> None:
    prompts = [f"p{i}" for i in range(5)]
    assert sessioncard.render_prompts(prompts).startswith("1. p0")
    trimmed = sessioncard.render_prompts(prompts, max_items=2)
    assert trimmed.startswith("_showing last 2 of 5 prompts_") and "2. p4" in trimmed


# ---------------------------------------------------------------------------
# cards per phase
# ---------------------------------------------------------------------------
def test_running_card(env: tuple[Store, Path]) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Fix the widget bug")
    store.set_subgoals(sid, ["find bug", "write test"], source="user")
    store.set_subgoal_checked(store.list_subgoals(sid)[0].id, True)
    store.update_fields(
        sid, next_step="write the failing test", summary="looked at it", blocked_on="CI red"
    )
    card = _card(store, git_base, sid)
    assert card["phase"] == "running" and card["archived"] is False
    assert card["repo"] == "home/ccc"
    assert card["aim"] == card["first_aim"] == "Fix the widget bug"
    assert card["progress"] == {"checked": 1, "total": 2}
    assert card["subgoals"] == [
        {"text": "find bug", "checked": True},
        {"text": "write test", "checked": False},
    ]
    assert card["resume"] == f"c --resume {sid}"
    assert card["prompt"] is None and card["start_date"] is None and card["start_when"] is None
    assert card["conversation"] is None

    text = sessioncard.render_text(card)
    assert text.startswith(f"{card['id']}  running")
    for heading in ("AIM", "AIM history", "Next step", "Sub-goals", "Summary", "Blocked on"):
        assert f"\n## {heading}\n" in text
    assert "- [x] find bug\n- [ ] write test" in text
    assert "write the failing test" in text and "CI red" in text
    assert f"Resume: `c --resume {sid}`" in text
    assert "## Prompt\n" not in text and "## Conversation" not in text


def test_parked_card(env: tuple[Store, Path]) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Waiting for the reset")
    store.update_fields(sid, status=Status.PARKED.value)
    card = _card(store, git_base, sid)
    assert card["phase"] == "parked" and card["status"] == "parked"
    assert "  parked  ·  status parked" in sessioncard.render_text(card)


def test_done_card(env: tuple[Store, Path]) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Ship the release")
    store.set_aim(sid, "Ship the release and tag it")
    store.update_fields(sid, done=True, status=Status.DONE.value, done_at=now_ms(), archived=True)
    card = _card(store, git_base, sid)
    assert card["phase"] == "done" and card["archived"] is True
    assert card["done_at"]  # an ISO date
    assert card["first_aim"] == "Ship the release"
    assert card["aim"] == "Ship the release and tag it"
    assert [r["aim"] for r in card["aim_history"]] == [
        "Ship the release",
        "Ship the release and tag it",
    ]
    text = sessioncard.render_text(card)
    assert "  done (archived)" in text
    assert "· current) Ship the release and tag it" in text
    assert "done at" in text


def test_draft_card(env: tuple[Store, Path]) -> None:
    store, git_base = env
    other = _running(store, git_base, "The prerequisite")
    sid = str(uuid.uuid4())
    store.create_draft(
        sid,
        str(git_base / "sdsc" / "zoho"),
        "Migrate the reports",
        prompt="Please migrate the reports to the new API.",
        start_when="during the holidays",
        start_date="2027-01-04",
        depends_on=other,
    )
    card = _card(store, git_base, sid)
    assert card["phase"] == "draft" and card["repo"] == "sdsc/zoho"
    assert card["prompt"] == "Please migrate the reports to the new API."
    assert card["start_date"] == "2027-01-04"
    assert card["start_when"] == "during the holidays"
    assert card["depends_on"] == other
    assert card["resume"] == ""
    text = sessioncard.render_text(card)
    assert "## Prompt\n\nPlease migrate the reports to the new API." in text
    assert "start date   2027-01-04" in text and "when         during the holidays" in text
    assert f"depends on   {other}" in text
    assert "Resume:" not in text


def test_draft_prompt_defaults_to_the_aim(env: tuple[Store, Path]) -> None:
    store, git_base = env
    sid = str(uuid.uuid4())
    store.create_draft(sid, str(git_base / "home" / "ccc"), "Tidy the docs")
    card = _card(store, git_base, sid)
    assert card["prompt"] == "Tidy the docs"
    assert card["start_date"] == "" and card["start_when"] == ""


# ---------------------------------------------------------------------------
# -t: the conversation, -j: the pinned keys
# ---------------------------------------------------------------------------
def test_conversation_from_a_synthetic_transcript(env: tuple[Store, Path]) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Talk it through")
    cwd = str(git_base / "home" / "ccc")
    _write_transcript(store, cwd, sid, ["first prompt here", "second prompt here"])
    session = store.get(sid)
    assert session is not None
    card = _card(store, git_base, sid, conversation=True)
    convo = card["conversation"]
    assert isinstance(convo, str)
    assert "first prompt here" in convo and "assistant reply here" in convo
    # The SAME segments the SESSION mirror (and the peek session tab) render.
    body = "".join(t for t, _ in sessioncard.view_for(ClaudeAdapter(), session).segments)
    assert convo == body
    text = sessioncard.render_text(card)
    assert "\n## Conversation\n\n" in text and "second prompt here" in text


def test_json_keys_are_pinned(env: tuple[Store, Path]) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Pin the schema")
    card = _card(store, git_base, sid)
    assert set(card) == CARD_KEYS
    assert card["schema_version"] == 1
    json.dumps(card)  # JSON-ready as is


# ---------------------------------------------------------------------------
# the CLI verb
# ---------------------------------------------------------------------------
def test_cli_show_json_by_prefix(
    env: tuple[Store, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Show me by prefix")
    assert cli.main(["show", "-s", sid[:8], "-j"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["session_id"] == sid and set(doc) == CARD_KEYS


def test_cli_show_defaults_to_the_callers_session(
    env: tuple[Store, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, git_base = env
    sid = _running(store, git_base, "Who am I")
    cwd = str(git_base / "home" / "ccc")
    _write_transcript(store, cwd, sid, ["hello from the caller"])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    assert cli.main(["show", "-t"]) == 0
    out = capsys.readouterr().out
    assert "Who am I" in out and "## Conversation" in out and "hello from the caller" in out


@pytest.mark.usefixtures("env")  # the hermetic store
def test_cli_show_unknown_session(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["show", "-s", "deadbeef-0000"]) == 1
    assert "no such session" in capsys.readouterr().err


def test_cli_show_help_names_every_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["show", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("-s, --session", "-t, --transcript", "-j, --json"):
        assert flag in out
