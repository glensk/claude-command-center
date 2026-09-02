"""Unit tests for the SQLite store."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from command_center.models import LiveSession, TranscriptScan
from command_center.store import AmbiguousJobId, Store, resolve_job_id


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def test_ensure_and_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.ensure("s1", cwd="/repo")
    assert session.session_id == "s1"
    assert session.cwd == "/repo"
    assert session.created_at > 0

    store.update_fields("s1", aim="done when green", deadline="2026-07-01")
    got = store.get("s1")
    assert got is not None
    assert got.aim == "done when green"
    assert got.deadline == "2026-07-01"
    assert got.updated_at >= got.created_at


def test_create_draft_stores_aim_prompt_and_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_draft(
        "job1",
        "/repo/sdsc/zoho",
        "Migrate Zendesk tickets to Zoho",
        prompt="do the migration",
        start_when="during holidays",
    )
    assert session.draft is True
    assert session.prompt == "do the migration"
    assert session.aim == "Migrate Zendesk tickets to Zoho"
    assert session.start_when == "during holidays"
    assert session.aim_score >= 0  # set_aim seeds an instant lexical score
    assert store.list_aim_history("job1"), "create_draft routes the AIM through set_aim"


def test_create_draft_blank_prompt_stays_null(tmp_path: Path) -> None:
    # A blank prompt is NOT copied from the AIM: NULL means "defaults to the AIM at
    # launch" (cmd_start_job falls back), and the mirrored file's empty # Prompt round-trips.
    store = _store(tmp_path)
    assert store.create_draft("job2", "/repo", "Ship the feature").prompt is None  # no prompt
    assert store.create_draft("job2b", "/repo", "Ship it", prompt="   ").prompt is None  # blank


def test_create_draft_stores_llm_models_and_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Explicit valid choices are stored verbatim.
    s = store.create_draft("jobllm", "/repo", "Do it", llm_overseer="fable-5", llm_exec="sonnet-5")
    assert s.llm_overseer == "fable-5" and s.llm_exec == "sonnet-5"
    # Default when omitted is fable-5 on both.
    d = store.create_draft("jobdef", "/repo", "Do it")
    assert d.llm_overseer == "fable-5" and d.llm_exec == "fable-5"
    # A bogus value falls back to the fable-5 default (validated against LLM_CHOICES).
    b = store.create_draft("jobbad", "/repo", "Do it", llm_overseer="gpt-9", llm_exec="")
    assert b.llm_overseer == "fable-5" and b.llm_exec == "fable-5"


def test_clear_draft_promotes_to_real_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft("job3", "/repo", "Do the thing")
    store.clear_draft("job3")
    got = store.get("job3")
    assert got is not None
    assert got.draft is False
    assert got.status == "idle"


def test_bool_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", done=True, keep=True)
    got = store.get("s1")
    assert got is not None
    assert got.done is True
    assert got.keep is True


def test_archived_excluded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("a")
    store.ensure("b")
    store.update_fields("b", archived=True)
    ids = {s.session_id for s in store.list_sessions()}
    assert ids == {"a"}
    assert len(store.list_sessions(include_archived=True)) == 2


_UUID_A = "ad2096c4-0000-4000-8000-000000000001"
_UUID_B = "ad2096c4-0000-4000-8000-000000000002"  # shares A's 8-char display prefix
_UUID_C = "be317d55-0000-4000-8000-000000000003"


def test_resolve_job_id_exact_wins_over_shared_prefix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft(_UUID_A, "/repo", "A")
    store.create_draft(_UUID_B, "/repo", "B")
    # A full id is returned even though B shares its whole 8-char prefix.
    assert resolve_job_id(store, _UUID_A) == _UUID_A
    assert resolve_job_id(store, _UUID_B.upper()) == _UUID_B  # case-insensitive exact


def test_resolve_job_id_unique_prefix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft(_UUID_A, "/repo", "A")
    store.create_draft(_UUID_C, "/repo", "C")
    assert resolve_job_id(store, "be317d55") == _UUID_C  # unique 8-char prefix
    assert resolve_job_id(store, "BE317") == _UUID_C  # case-insensitive prefix


def test_resolve_job_id_ambiguous_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft(_UUID_A, "/repo", "A")
    store.create_draft(_UUID_B, "/repo", "B")
    with pytest.raises(AmbiguousJobId) as excinfo:
        resolve_job_id(store, "ad2096")
    assert set(excinfo.value.matches) == {_UUID_A, _UUID_B}
    assert "ambiguous job id ad2096" in str(excinfo.value)


def test_resolve_job_id_no_match_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft(_UUID_A, "/repo", "A")
    assert resolve_job_id(store, "ffffffff") is None
    assert resolve_job_id(store, "") is None


def test_resolve_job_id_includes_archived_and_accepts_id_iterable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_draft(_UUID_A, "/repo", "A")
    store.update_fields(_UUID_A, archived=True)  # deleted/archived job still resolvable
    assert resolve_job_id(store, "ad2096c4") == _UUID_A
    # The resolver also accepts a plain iterable of ids (not just a Store).
    assert resolve_job_id([_UUID_A, _UUID_C], "be31") == _UUID_C


def test_subgoals_and_progress(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["find valve", "throttle", "test"])
    subs = store.list_subgoals("s1")
    assert [s.text for s in subs] == ["find valve", "throttle", "test"]
    assert store.progress("s1") == (0, 3)

    store.set_subgoal_checked(subs[0].id, True)
    assert store.progress("s1") == (1, 3)

    # Replacing the checklist clears the old rows.
    store.set_subgoals("s1", ["only one"])
    assert store.progress("s1") == (0, 1)


def test_check_all_subgoals_reconciles_to_full(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b", "c"], source="auto")
    subs = store.list_subgoals("s1")
    store.set_subgoal_checked(subs[1].id, True)  # one already ticked → 1/3
    assert store.progress("s1") == (1, 3)

    flipped = store.check_all_subgoals("s1")  # mark-done reconciles the rest
    assert flipped == 2  # only the two still-unchecked rows flip
    assert store.progress("s1") == (3, 3)
    assert store.check_all_subgoals("s1") == 0  # idempotent — nothing left to flip


def test_manual_progress_roundtrip_and_cleared_on_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress is None  # default: auto (sub-goal ratio)

    store.update_fields("s1", manual_progress=40)
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress == 40

    # Blank edit clears the override back to auto.
    store.update_fields("s1", manual_progress=None)
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress is None

    # Mark-done (check_all_subgoals) clears a set override so done never reads 40%.
    store.update_fields("s1", manual_progress=40)
    store.check_all_subgoals("s1")
    session = store.get("s1")
    assert session is not None
    assert session.manual_progress is None


def test_set_subgoals_reports_change_and_records_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.set_subgoals("s1", ["a", "b"]) is True  # first version
    assert store.set_subgoals("s1", ["a", "b"]) is False  # identical -> no-op, no new history
    assert store.set_subgoals("s1", ["a", "b", "c"]) is True  # real change
    hist = store.list_subgoal_history("s1")
    assert len(hist) == 2  # only the two real changes recorded
    assert [t for t, _ in hist[-1].items] == ["a", "b", "c"]
    assert hist[0].drift_severity == "none"  # first-ever has nothing to drift from


def test_set_subgoals_merge_preserves_ticks_and_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["alpha", "beta", "gamma"])
    subs = {s.text: s for s in store.list_subgoals("s1")}
    store.set_subgoal_checked(subs["beta"].id, True)
    # Regenerate with merge: beta survives (stays checked), delta is new (unchecked).
    store.set_subgoals(
        "s1", ["alpha", "beta", "delta"], source="agent", model="claude-haiku-4-5", merge=True
    )
    got = {s.text: s for s in store.list_subgoals("s1")}
    assert got["beta"].checked is True  # carried over
    assert got["delta"].checked is False  # new item
    assert got["alpha"].checked is False
    assert got["delta"].source == "agent"
    assert got["delta"].model == "claude-haiku-4-5"
    session = store.get("s1")
    assert session is not None and session.subgoals_adaptive is True  # agent lists adapt


def test_subgoals_stale_after_aim_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "ship X: pytest -q green and PR #5 merged")  # AIM rev 1
    store.set_subgoals("s1", ["write test_x", "open PR #5"], source="agent")  # adaptive @ rev 1
    assert store.subgoals_stale("s1") is False
    store.set_aim("s1", "ship X and deploy: prod smoke test passes")  # AIM rev 2 -> stale
    assert store.subgoals_stale("s1") is True
    store.set_subgoals("s1", ["write test_x", "deploy"], source="agent", merge=True)  # re-aligned
    assert store.subgoals_stale("s1") is False
    # A pinned (non-adaptive) checklist never goes stale.
    store.ensure("s2")
    store.set_aim("s2", "do thing one and thing two concretely")
    store.set_subgoals("s2", ["a"], source="user")  # pinned
    store.set_aim("s2", "do thing one, two and three concretely")
    assert store.subgoals_stale("s2") is False


def test_drift_setters_and_resolution(tmp_path: Path) -> None:
    from command_center.models import Session, drift_unresolved

    store = _store(tmp_path)
    store.ensure("s1")

    def unresolved() -> bool:
        session = store.get("s1")
        assert isinstance(session, Session)
        return drift_unresolved(session)

    store.set_drift("s1", "medium", "coverage dropped")
    assert unresolved() is True

    store.ack_drift("s1")
    assert unresolved() is False  # acknowledged -> resolved

    store.set_drift("s1", "high", "goalpost moved")  # re-flag
    assert unresolved() is True
    store.set_drift("s1", "none", None)  # a later clean check clears it
    assert unresolved() is False


def test_prunable_and_delete_many(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # junk: a contentless leftover (e.g. a headless `claude -p` row at "/").
    store.ensure("junk", cwd="/")
    # protected by signal: each of these trips one guard and must survive.
    store.ensure("has_aim")
    store.update_fields("has_aim", aim="done when green")
    store.ensure("has_prompt")
    store.update_fields("has_prompt", prompt_count=2)
    store.ensure("kept")
    store.update_fields("kept", keep=True)
    store.ensure("has_subgoal")
    store.set_subgoals("has_subgoal", ["step one"])
    # live (currently running) — protected even though it is otherwise contentless.
    store.ensure("live_empty", cwd="/")

    victims = store.prunable_sessions(protect_ids={"live_empty"})
    assert {s.session_id for s in victims} == {"junk"}

    assert store.delete_many(s.session_id for s in victims) == 1
    assert store.get("junk") is None
    assert {s.session_id for s in store.list_sessions()} == {
        "has_aim",
        "has_prompt",
        "kept",
        "has_subgoal",
        "live_empty",
    }
    assert store.delete_many([]) == 0  # no-op on empty input


def test_prunable_headless_overrides_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A headless `claude -p` leak: carries an env-inherited aim + auto next-step +
    # prompt_count, so the contentless guards would spare it — but headless_ids
    # prunes it anyway.
    store.ensure("headless", cwd="/repo")
    store.update_fields("headless", aim="inherited aim", next_step="auto step", prompt_count=1)
    # A real session that merely shares the same transcript shape must still be
    # protected when it is live, done, or kept.
    store.ensure("done_headless")
    store.update_fields("done_headless", aim="x", done=True)
    store.ensure("kept_headless")
    store.update_fields("kept_headless", aim="x", keep=True)
    store.ensure("live_headless")

    victims = store.prunable_sessions(
        protect_ids={"live_headless"},
        headless_ids={"headless", "done_headless", "kept_headless", "live_headless"},
    )
    assert {s.session_id for s in victims} == {"headless"}


def test_prunable_orphan_overrides_aim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A dead-launched job: a future job that start-job launched but that never had a turn,
    # so it carries an AIM inherited from the launch (which would spare it from the
    # contentless guards) yet has no transcript to resume. orphan_ids prunes it anyway.
    store.ensure("orphan", cwd="/repo")
    store.update_fields("orphan", aim="inherited aim")
    # done / kept / live rows are protected even when the caller names them as orphans.
    store.ensure("done_orphan")
    store.update_fields("done_orphan", aim="x", done=True)
    store.ensure("kept_orphan")
    store.update_fields("kept_orphan", aim="x", keep=True)
    store.ensure("live_orphan")

    victims = store.prunable_sessions(
        protect_ids={"live_orphan"},
        orphan_ids={"orphan", "done_orphan", "kept_orphan", "live_orphan"},
    )
    assert {s.session_id for s in victims} == {"orphan"}


def test_aim_score_columns_roundtrip(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist: an un-whitelisted column is silently dropped.
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields(
        "s1", aim_score=70, aim_score_reason="names a passing test", last_progress_at=123
    )
    got = store.get("s1")
    assert got is not None
    assert got.aim_score == 70
    assert got.aim_score_reason == "names a passing test"
    assert got.last_progress_at == 123


def test_version_column_roundtrip(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for `version`.
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.get("s1").version is None  # type: ignore[union-attr]  # NULL by default
    store.update_fields("s1", version="2.1.193")
    got = store.get("s1")
    assert got is not None
    assert got.version == "2.1.193"


def test_model_effort_columns_roundtrip(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for the OBSERVED
    # model/effort columns. Both default to "" (NOT NULL) and survive a round-trip.
    store = _store(tmp_path)
    store.ensure("s1")
    got = store.get("s1")
    assert got is not None
    assert got.model == "" and got.effort == ""  # NOT NULL defaults
    store.update_fields("s1", model="opus-4.8", effort="xhigh")
    got = store.get("s1")
    assert got is not None
    assert got.model == "opus-4.8"
    assert got.effort == "xhigh"


def test_set_aim_clears_auto_subgoals_and_resets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b"], source="auto")
    store.update_fields("s1", aim="old aim", context_offset=500, aim_score=80)

    changed = store.set_aim("s1", "all tests in tests/ pass and PR #42 merged")
    assert changed is True
    got = store.get("s1")
    assert got is not None
    assert got.aim == "all tests in tests/ pass and PR #42 merged"
    assert store.progress("s1") == (0, 0)  # auto checklist cleared
    assert got.context_offset == 0  # offset reset so a fresh checklist re-derives
    assert got.aim_score >= 50  # concrete aim => specific (lexical), reason cleared
    assert got.aim_score_reason is None


def test_set_aim_met_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim_met("s1", True, "tests pass and PR merged", 12345)
    got = store.get("s1")
    assert got is not None
    assert got.aim_met is True
    assert got.aim_met_reason == "tests pass and PR merged"
    assert got.aim_assessed_at == 12345
    # Latest-wins: a later turn can flip it back to False.
    store.set_aim_met("s1", False, "regressed", 22222)
    got = store.get("s1")
    assert got is not None and got.aim_met is False and got.aim_assessed_at == 22222


def test_set_aim_clears_met_verdict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "old concrete aim: pytest -q green")
    store.set_aim_met("s1", True, "was done", 999)
    # A new AIM invalidates the prior "is it done?" verdict.
    store.set_aim("s1", "a different concrete aim: ruff check clean")
    got = store.get("s1")
    assert got is not None
    assert got.aim_met is False
    assert got.aim_assessed_at == 0
    assert got.aim_met_reason is None


def test_set_aim_noop_when_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "ship it")
    store.set_subgoals("s1", ["x"], source="auto")
    changed = store.set_aim("s1", "ship it")  # same aim
    assert changed is False
    assert store.progress("s1") == (0, 1)  # checklist untouched


def test_set_aim_preserves_user_subgoals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="old")
    store.set_subgoals("s1", ["manual one", "manual two"], source="user")
    store.set_aim("s1", "a different aim")
    assert [s.text for s in store.list_subgoals("s1")] == ["manual one", "manual two"]


def test_set_aim_records_prev_on_change_only(tmp_path: Path) -> None:
    """The first AIM records no transition; a later change records old + a change time."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim")  # initial set: no prior AIM -> no transition
    first = store.get("s1")
    assert first is not None and first.aim_prev is None and first.aim_changed_at == 0
    store.set_aim("s1", "second aim")  # a real change -> remember where it came from
    second = store.get("s1")
    assert second is not None
    assert second.aim_prev == "first aim"
    assert second.aim_changed_at > 0


def test_aim_history_records_progression(tmp_path: Path) -> None:
    """Every AIM (re)definition is appended in order; the last is the current AIM."""
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.list_aim_history("s1") == []  # nothing yet
    store.set_aim("s1", "first vague aim")
    store.set_aim("s1", "second aim")
    store.set_aim("s1", "third, concrete aim: pytest -q green")
    history = store.list_aim_history("s1")
    assert [h.aim for h in history] == [
        "first vague aim",
        "second aim",
        "third, concrete aim: pytest -q green",
    ]
    assert all(h.score >= 0 for h in history)  # each revision carries its lexical score
    store.set_aim("s1", "third, concrete aim: pytest -q green")  # no-op -> no new row
    assert len(store.list_aim_history("s1")) == 3


def test_count_aim_history_tracks_running_index(tmp_path: Path) -> None:
    """``count_aim_history`` is the current AIM's 1-based running index (0 before any row)."""
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.count_aim_history("s1") == 0  # no rows yet
    store.set_aim("s1", "first aim")
    assert store.count_aim_history("s1") == 1
    store.set_aim("s1", "second aim")
    assert store.count_aim_history("s1") == 2
    store.set_aim("s1", "second aim")  # no-op -> index unchanged
    assert store.count_aim_history("s1") == 2


def test_aim_history_seeds_preexisting_original(tmp_path: Path) -> None:
    """A session whose AIM predates history-tracking still shows where it started."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", aim="legacy aim", aim_score=40)  # set without going through set_aim
    assert store.list_aim_history("s1") == []  # no history rows for the legacy AIM
    store.set_aim("s1", "sharpened aim")  # first tracked change seeds the original first
    assert [h.aim for h in store.list_aim_history("s1")] == ["legacy aim", "sharpened aim"]


def test_sessions_carry_their_first_aim_revision(tmp_path: Path) -> None:
    """Every Session read joins revision (1) + its short label — what the `/aim` column shows."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim: ccc ls shows the row")
    store.set_short_aim("s1", "first label")  # revision (1) IS the current one here
    store.set_aim("s1", "second aim: pytest -q green")
    store.set_short_aim("s1", "second label")  # lands on the CURRENT revision only

    session = store.get("s1")
    assert session is not None
    assert session.first_aim == "first aim: ccc ls shows the row"
    assert session.first_short_aim == "first label"  # revision (1) kept its own label
    assert session.aim == "second aim: pytest -q green"  # …and the current one is untouched
    assert session.short_aim == "second label"
    listed = next(s for s in store.list_sessions() if s.session_id == "s1")
    assert (listed.first_aim, listed.first_short_aim) == (session.first_aim, "first label")

    # An AIM that predates history tracking has no recorded revision (1) → both stay None.
    store.ensure("s2")
    store.update_fields("s2", aim="legacy aim")
    legacy = store.get("s2")
    assert legacy is not None
    assert (legacy.first_aim, legacy.first_short_aim) == (None, None)


def test_set_first_short_aim_labels_revision_one(tmp_path: Path) -> None:
    """The revision-(1) label is writable on its own (the `/aim` column's text after a rewrite)."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim: ccc ls shows the row")
    store.set_aim("s1", "second aim: pytest -q green")
    store.set_short_aim("s1", "second label")

    store.set_first_short_aim("s1", "first label")
    history = store.list_aim_history("s1")
    assert [h.short_aim for h in history] == ["first label", "second label"]
    session = store.get("s1")
    assert session is not None
    assert session.first_short_aim == "first label"
    assert session.short_aim == "second label"  # the current label is left alone

    store.set_first_short_aim("s1", "  ")  # blank clears back to None
    assert store.list_aim_history("s1")[0].short_aim is None


def test_set_first_aim_rewrites_revision_one_in_place(tmp_path: Path) -> None:
    """``/aim (1)`` is editable: revision 1 is rewritten, no revision added, current AIM kept."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first vague aim")
    store.set_aim("s1", "second, concrete aim: pytest -q green")
    store.set_short_aim("s1", "second")  # label on the CURRENT revision — must survive

    assert store.set_first_aim("s1", "first aim, restated: ccc ls shows the row") is True
    history = store.list_aim_history("s1")
    assert [h.aim for h in history] == [
        "first aim, restated: ccc ls shows the row",
        "second, concrete aim: pytest -q green",
    ]
    assert len(history) == 2  # rewritten in place — the running index never shifts
    assert history[0].score >= 0  # re-scored lexically for the new wording
    assert history[0].short_aim is None  # the old label described the old wording
    session = store.get("s1")
    assert session is not None
    assert session.aim == "second, concrete aim: pytest -q green"  # current AIM untouched
    # The CURRENT revision's short label is generated from the original AIM as a hint, so it
    # went stale too — dropped here (the `/aim` column falls back to the full AIM until the
    # generator backfills), which is what keeps the column from showing the pre-edit wording.
    assert session.short_aim is None
    assert history[1].short_aim is None

    # Idempotent / refuses to empty a history row.
    assert store.set_first_aim("s1", "first aim, restated: ccc ls shows the row") is False
    assert store.set_first_aim("s1", "   ") is False
    assert [h.aim for h in store.list_aim_history("s1")][0] == (
        "first aim, restated: ccc ls shows the row"
    )


def test_set_first_aim_mirrors_when_it_is_also_the_current_aim(tmp_path: Path) -> None:
    """With one revision (or a pre-history AIM) ``/aim (1)`` IS the current AIM — mirror it."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "only aim: tests pass")
    store.set_aim_met("s1", True, "looks done", 123)

    assert store.set_first_aim("s1", "only aim, restated: pytest -q is green") is True
    session = store.get("s1")
    assert session is not None
    assert session.aim == "only aim, restated: pytest -q is green"  # live AIM kept in sync
    assert session.aim_met is False  # a DONE verdict never outlives the wording it judged
    assert len(store.list_aim_history("s1")) == 1  # still a single revision

    # Pre-history session: the live AIM is the sole, unrecorded revision 1.
    store.ensure("s2")
    store.update_fields("s2", aim="legacy aim", aim_score=40)
    assert store.set_first_aim("s2", "legacy aim, restated: ruff check clean") is True
    legacy = store.get("s2")
    assert legacy is not None
    assert legacy.aim == "legacy aim, restated: ruff check clean"
    assert store.list_aim_history("s2") == []  # rewriting it must not invent a revision 2

    # Nothing to adapt when no AIM was ever set.
    store.ensure("s3")
    assert store.set_first_aim("s3", "some aim") is False


def test_set_short_aim_writes_session_and_latest_revision(tmp_path: Path) -> None:
    """The short label lands on the session AND mirrors onto the current AIM-history row."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim")
    store.set_aim("s1", "second aim")
    store.set_short_aim("s1", "implement second")
    got = store.get("s1")
    assert got is not None and got.short_aim == "implement second"
    history = store.list_aim_history("s1")
    assert history[-1].short_aim == "implement second"  # mirrored onto the current revision
    assert history[0].short_aim is None  # an earlier revision is untouched
    store.set_short_aim("s1", "  ")  # blank clears back to NULL
    assert (cleared := store.get("s1")) is not None and cleared.short_aim is None


def test_set_aim_clears_stale_short_aim(tmp_path: Path) -> None:
    """Changing the AIM drops the old short label so the column never shows a stale one."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_aim("s1", "first aim")
    store.set_short_aim("s1", "implement first")
    store.set_aim("s1", "a wholly different aim")
    got = store.get("s1")
    assert got is not None and got.short_aim is None


def test_set_subgoals_weight_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b", "c"], source="auto", weights=[2, 1, 3])
    assert [s.weight for s in store.list_subgoals("s1")] == [2, 1, 3]
    # Default weight is 1 when none supplied.
    store.set_subgoals("s1", ["only"])
    assert store.list_subgoals("s1")[0].weight == 1


def test_set_subgoal_check_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["build passes"], source="auto")
    sub = store.list_subgoals("s1")[0]
    assert sub.check_cmd is None
    store.set_subgoal_check(sub.id, "make build")
    assert store.list_subgoals("s1")[0].check_cmd == "make build"
    store.set_subgoal_check(sub.id, "")  # empty clears it
    assert store.list_subgoals("s1")[0].check_cmd is None


def test_progress_weighted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    store.set_subgoals("s1", ["a", "b", "c"], source="auto", weights=[3, 1, 1])
    subs = store.list_subgoals("s1")
    store.set_subgoal_checked(subs[0].id, True)  # the weight-3 item
    assert store.progress("s1") == (1, 3)  # unweighted count
    assert store.progress_weighted("s1") == (3, 5)  # 3 of 5 weight done

    # All-default weights => weighted == unweighted.
    store.set_subgoals("s1", ["x", "y"])
    assert store.progress_weighted("s1") == store.progress("s1")


def test_upsert_preserves_user_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure("s1", cwd="/old")
    store.update_fields("s1", aim="keep me", next_step="my step", next_step_source="user")

    live = LiveSession(
        pid=42, session_id="s1", cwd="/new", name="renamed", agent="claude", alive=True
    )
    store.upsert_from_live(live)

    got = store.get("s1")
    assert got is not None
    assert got.cwd == "/new"  # reconcile updates infra fields
    assert got.name == "renamed"
    assert got.last_seen_pid == 42
    assert got.aim == "keep me"  # but never user-authored fields
    assert got.next_step == "my step"


def test_create_draft_stores_start_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_draft(
        "job-sd",
        "/repo/home/mac",
        "Re-enable FileVault after the trip",
        start_when="return from Slovenia",
        start_date="2026-08-11",
    )
    assert session.start_date == "2026-08-11"
    assert session.start_when == "return from Slovenia"
    # Blank stays NULL (no fixed date → plain FUTURE bucket).
    blank = store.create_draft("job-nd", "/repo/home/mac", "Other", start_date="  ")
    assert blank.start_date is None


def test_create_draft_stores_depends_on(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = "3a8b7c12-1234-5678-9abc-def012345678"
    session = store.create_draft("job-dep", "/repo/home/mac", "Do it", depends_on=parent)
    assert session.depends_on == parent
    # Blank stays NULL (no dependency).
    blank = store.create_draft("job-nodep", "/repo/home/mac", "Other", depends_on="  ")
    assert blank.depends_on is None


def test_depends_on_column_migrates_onto_existing_db(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for depends_on:
    # a pre-migration DB (schema without the column) gains it, and update_fields persists it.
    import sqlite3

    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA.replace("    depends_on        TEXT,\n", "")
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:  # opening runs _ensure_columns → ALTER adds depends_on
        row = store.get("old")
        assert row is not None
        assert row.depends_on is None  # NULL default after the migration
        store.update_fields("old", depends_on="parent-uuid")
        got = store.get("old")
        assert got is not None and got.depends_on == "parent-uuid"


def test_row_to_session_drops_columns_this_build_does_not_know(tmp_path: Path) -> None:
    # The DB is shared and the code is an editable install: a NEWER ccc (another session's
    # hook, the daemon) can ALTER TABLE under a long-lived OLDER process (the TUI). Reading
    # such a row must drop the unknown column, not raise inside Session(**row) — that
    # TypeError killed the TUI's refresh worker and froze its last frame (2026-09-02).
    import sqlite3

    db = tmp_path / "state.db"
    with Store(db):
        pass  # creates the schema
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('s1', '/repo/s1')")
    conn.execute("ALTER TABLE sessions ADD COLUMN from_the_future TEXT NOT NULL DEFAULT 'x'")
    conn.commit()
    conn.close()
    with Store(db) as store:
        row = store.get("s1")
        assert row is not None
        assert row.cwd == "/repo/s1"
        assert not hasattr(row, "from_the_future")
        assert row.done is False  # bool coercion of the known columns still happens


def test_close_requested_at_column_migrates_and_roundtrips(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for the new
    # close_requested_at column: a pre-migration DB gains it (default 0) and it survives a
    # round-trip through Session.
    import sqlite3

    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA.replace(
        "    close_requested_at INTEGER NOT NULL DEFAULT 0,\n", ""
    )
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:  # opening runs _ensure_columns → ALTER adds close_requested_at
        row = store.get("old")
        assert row is not None
        assert row.close_requested_at == 0  # NOT NULL default after the migration
        store.update_fields("old", close_requested_at=1234567890)
        got = store.get("old")
        assert got is not None and got.close_requested_at == 1234567890


def _scan(session_id: str, **over: object) -> TranscriptScan:
    """A TranscriptScan for *session_id* with sane defaults, overridable per field."""
    fields: dict[str, object] = {
        "session_id": session_id,
        "path": f"/transcripts/{session_id}.jsonl",
        "mtime_ns": 1_700_000_000_000_000_000,
        "size": 4096,
        "model": "claude-fable-5",
        "codex": False,
        "codex_scanned_to": 4096,
        "scanned_at": 1_700_000_000_000,
        "headless": None,
    }
    fields.update(over)
    return TranscriptScan(**fields)  # type: ignore[arg-type]


def test_transcript_scans_roundtrip_and_replace(tmp_path: Path) -> None:
    # The scan rows are what let a later pass skip re-parsing a frozen transcript, so they
    # must survive the round trip exactly — including the INTEGER-stored `codex` flag,
    # which has to come back as a real bool.
    store = _store(tmp_path)
    assert store.transcript_scans() == {}
    assert store.get_transcript_scan("s1") is None

    store.put_transcript_scans([_scan("s1", codex=True), _scan("s2")])
    got = store.get_transcript_scan("s1")
    assert got is not None
    assert got == _scan("s1", codex=True)
    assert got.codex is True  # coerced back from INTEGER, not left as 1
    assert store.get_transcript_scan("s2") is not None
    assert store.get_transcript_scan("s2").codex is False  # type: ignore[union-attr]
    assert set(store.transcript_scans()) == {"s1", "s2"}

    # A later pass replaces the row for the same session id (INSERT OR REPLACE).
    store.put_transcript_scans([_scan("s1", size=9000, model="claude-opus-4-8", codex=True)])
    again = store.transcript_scans()["s1"]
    assert again.size == 9000 and again.model == "claude-opus-4-8"
    assert len(store.transcript_scans()) == 2  # replaced, not duplicated

    store.put_transcript_scans([])  # no-op on an empty batch
    assert len(store.transcript_scans()) == 2
    store.close()


def test_delete_removes_transcript_scan_rows(tmp_path: Path) -> None:
    # transcript_scan carries no foreign key (concurrent writers), so the deletes must
    # clear it explicitly — else a re-created session id would inherit stale facts.
    store = _store(tmp_path)
    for sid in ("s1", "s2", "s3"):
        store.ensure(sid, cwd="/repo")
    store.put_transcript_scans([_scan("s1"), _scan("s2"), _scan("s3")])

    store.delete("s1")
    assert store.get_transcript_scan("s1") is None
    assert set(store.transcript_scans()) == {"s2", "s3"}

    store.delete_many(["s2", "s3"])
    assert store.transcript_scans() == {}
    store.close()


def test_transcript_scan_table_is_created_on_an_older_db(tmp_path: Path) -> None:
    # A DB written by a ccc that predates the table gains it on open (CREATE IF NOT EXISTS
    # in _SCHEMA), exactly like the ALTER-in-place column migrations.
    import sqlite3

    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA[
        : store_mod._SCHEMA.index("CREATE TABLE IF NOT EXISTS transcript_scan")
    ]
    assert "transcript_scan" not in legacy_schema
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()

    with Store(db) as store:  # opening runs _SCHEMA → the table appears
        assert store.transcript_scans() == {}
        store.put_transcript_scans([_scan("old")])
        assert store.get_transcript_scan("old") is not None


def _legacy_scan_schema() -> str:
    """``_SCHEMA`` as it was before ``transcript_scan.headless`` existed."""
    from command_center import store as store_mod

    legacy = store_mod._SCHEMA.replace(
        "    scanned_at       INTEGER NOT NULL DEFAULT 0,\n    headless         INTEGER\n",
        "    scanned_at       INTEGER NOT NULL DEFAULT 0\n",
    )
    assert "headless" not in legacy
    return legacy


def _legacy_scan_db(db: Path) -> None:
    """Create *db* with the pre-``headless`` schema and one scan row already in it."""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.executescript(_legacy_scan_schema())
    conn.execute(
        "INSERT INTO transcript_scan (session_id, path, mtime_ns, size, model, codex, "
        "codex_scanned_to, scanned_at) VALUES ('old', '/t/old.jsonl', 1, 2, 'm', 0, 2, 3)"
    )
    conn.commit()
    conn.close()


def test_transcript_scan_headless_roundtrips_all_three_states(tmp_path: Path) -> None:
    # headless is TRI-STATE: True/False are facts, None is "not determined yet" (an
    # empty/unreadable first record) and MUST come back as None so the next scan
    # re-probes instead of inheriting a bogus False.
    store = _store(tmp_path)
    store.put_transcript_scans(
        [_scan("yes", headless=True), _scan("no", headless=False), _scan("dunno")]
    )
    got = store.transcript_scans()
    assert got["yes"].headless is True  # coerced back from INTEGER, not left as 1
    assert got["no"].headless is False
    assert got["dunno"].headless is None
    assert got["yes"] == _scan("yes", headless=True)

    # A later pass overwrites the value in both directions, None included.
    store.put_transcript_scans([_scan("yes", headless=None), _scan("dunno", headless=False)])
    again = store.transcript_scans()
    assert again["yes"].headless is None
    assert again["dunno"].headless is False
    store.close()


def test_transcript_scan_headless_column_migrates_onto_an_older_db(tmp_path: Path) -> None:
    # A DB written before the column existed gains it on open (the transcript_scan arm of
    # _ensure_columns); the pre-existing row reads back as None = undetermined, not False.
    db = tmp_path / "legacy.db"
    _legacy_scan_db(db)

    with Store(db) as store:  # opening runs _ensure_columns → ALTER adds headless
        old = store.get_transcript_scan("old")
        assert old is not None and old.headless is None
        store.put_transcript_scans([_scan("old", headless=True)])
        assert store.get_transcript_scan("old").headless is True  # type: ignore[union-attr]


def test_add_column_swallows_a_peer_that_won_the_migration_race(tmp_path: Path) -> None:
    # Every ccc process runs the migration on open, and the DDL runs in autocommit
    # (Python's legacy transaction control only opens an implicit transaction before DML),
    # so the swallow matters exactly when both PRAGMAs preceded either ALTER: the loser
    # must treat "duplicate column name" as "already migrated", not as an error.
    db = tmp_path / "legacy.db"
    _legacy_scan_db(db)

    with Store(db) as first:  # its __init__ already added the column
        assert first.get_transcript_scan("old") is not None
        with Store(db) as second:
            second._add_column("transcript_scan", "headless", "INTEGER")  # pylint: disable=protected-access
            rows = second.conn.execute("SELECT headless FROM transcript_scan").fetchall()
            assert [row["headless"] for row in rows] == [None]

    # Anything OTHER than a duplicate column still raises (never a blanket swallow).
    with Store(db) as store:
        with pytest.raises(sqlite3.OperationalError):
            store._add_column("no_such_table", "x", "INTEGER")  # pylint: disable=protected-access


def test_two_concurrent_store_inits_both_migrate_and_read_headless(tmp_path: Path) -> None:
    # The real race: two fresh ccc processes opening the same legacy DB at once. Both
    # constructors must succeed (one ALTER wins, the other is swallowed) and both stores
    # must be able to read the column.
    import threading

    db = tmp_path / "legacy.db"
    _legacy_scan_db(db)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    seen: list[object] = []

    def _open() -> None:
        try:
            barrier.wait(timeout=10)
            with Store(db) as store:
                row = store.get_transcript_scan("old")
                assert row is not None
                seen.append(row.headless)
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    threads = [threading.Thread(target=_open) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    assert seen == [None, None]


def test_put_transcript_scans_upsert_preserves_a_newer_builds_column(tmp_path: Path) -> None:
    # INSERT OR REPLACE deletes and re-inserts the row, resetting every column THIS build
    # does not know about (a newer ccc's — the DB is shared and the code is an editable
    # install). The explicit UPSERT touches only the columns this build owns.
    import sqlite3

    db = tmp_path / "state.db"
    store = Store(db)
    store.put_transcript_scans([_scan("s1")])
    store.conn.execute("ALTER TABLE transcript_scan ADD COLUMN future TEXT")
    store.conn.execute("UPDATE transcript_scan SET future = 'x' WHERE session_id = 's1'")
    store.conn.commit()

    store.put_transcript_scans([_scan("s1", size=9000, headless=True)])
    row = store.conn.execute(
        "SELECT size, headless, future FROM transcript_scan WHERE session_id = 's1'"
    ).fetchone()
    assert row["size"] == 9000 and row["headless"] == 1
    assert row["future"] == "x"  # untouched by a build that has never heard of it
    assert isinstance(store.conn, sqlite3.Connection)
    store.close()


def test_list_sessions_matches_get_field_for_field(tmp_path: Path) -> None:
    # list_sessions reads its rows POSITIONALLY off ONE precomputed column map (29 ms of
    # the 88 ms at 637 rows was `row["name"]` linear scans); get() derives its own map for
    # a single row. The two must stay indistinguishable, field for field.
    store = _store(tmp_path)
    store.ensure("s1", cwd="/repo/one")
    store.set_aim("s1", "ship it")
    store.update_fields("s1", done=True, importance=2, model="claude-fable-5", effort="high")
    store.ensure("s2", cwd="/repo/two")
    store.create_draft("s3", "/repo/three", "Do the thing")

    listed = store.list_sessions()
    assert [s.session_id for s in listed] == ["s1", "s2", "s3"]
    for session in listed:
        single = store.get(session.session_id)
        assert single is not None
        assert dataclasses.asdict(session) == dataclasses.asdict(single)
    store.close()


def test_claim_close_request_fires_at_most_once(tmp_path: Path) -> None:
    """A fresh armed request is claimed exactly once; the second claim returns False."""
    store = _store(tmp_path)
    store.ensure("s1")
    now = 1_000_000
    ttl = 10 * 60 * 1000
    store.update_fields("s1", close_requested_at=now)  # arm
    assert store.claim_close_request("s1", now, ttl) is True  # first caller wins
    assert store.get("s1").close_requested_at == 0  # type: ignore[union-attr]  # cleared
    assert store.claim_close_request("s1", now, ttl) is False  # already claimed → no re-fire


def test_claim_close_request_expired_is_cleared_not_claimed(tmp_path: Path) -> None:
    """A stamp older than the TTL is never claimed (False) but is cleared so it can't linger."""
    store = _store(tmp_path)
    store.ensure("s1")
    ttl = 10 * 60 * 1000
    armed_at = 1_000_000
    store.update_fields("s1", close_requested_at=armed_at)
    now = armed_at + ttl + 1  # one ms past the TTL → expired
    assert store.claim_close_request("s1", now, ttl) is False
    assert store.get("s1").close_requested_at == 0  # type: ignore[union-attr]  # still cleared


def test_claim_close_request_unarmed_is_false(tmp_path: Path) -> None:
    """An unarmed session (close_requested_at == 0) is never claimed."""
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.claim_close_request("s1", 1_000_000, 10 * 60 * 1000) is False
