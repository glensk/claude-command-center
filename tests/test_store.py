"""Unit tests for the SQLite store."""

from __future__ import annotations

import ast
import dataclasses
import sqlite3
from pathlib import Path

import pytest

from command_center.models import (
    AIM_SET_BY_LAUNCH,
    AIM_SET_BY_RELAY,
    AIM_SET_BY_TOOL,
    AIM_SET_BY_USER,
    DEFAULT_LLM,
    CloseClaim,
    LiveSession,
    MirrorVouch,
    TranscriptScan,
)
from command_center.store import AmbiguousJobId, Store, resolve_job_id


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def test_ensure_and_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.ensure("s1", cwd="/repo")
    assert session.session_id == "s1"
    assert session.cwd == "/repo"
    assert session.created_at > 0
    assert (session.llm_overseer, session.llm_exec) == (DEFAULT_LLM, DEFAULT_LLM)

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
    s = store.create_draft("jobllm", "/repo", "Do it", llm_overseer="opus-4.8", llm_exec="sonnet-5")
    assert s.llm_overseer == "opus-4.8" and s.llm_exec == "sonnet-5"
    # Default when omitted is opus-5 on both.
    d = store.create_draft("jobdef", "/repo", "Do it")
    assert d.llm_overseer == "opus-5" and d.llm_exec == "opus-5"
    # A retired or bogus value falls back to the active default.
    b = store.create_draft("jobbad", "/repo", "Do it", llm_overseer="gpt-9", llm_exec="")
    assert b.llm_overseer == "opus-5" and b.llm_exec == "opus-5"
    retired = store.create_draft(
        "jobretired", "/repo", "Do it", llm_overseer="fable-5", llm_exec="fable-5"
    )
    assert retired.llm_overseer == "opus-5" and retired.llm_exec == "opus-5"


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


def test_session_for_tab_uuid_live_row_beats_done_row(tmp_path: Path) -> None:
    """A reused tab: the done session that responded LAST still loses to the live one.

    Regression: f+j from a tab whose previous session exited (out of credits) and a
    new one was launched in the same tab landed on the dead row — first SQL match
    (insertion order) won, and the dead row was the older one.
    """
    store = _store(tmp_path)
    store.ensure("dead", cwd="/Users/x/repo")
    store.update_fields(
        "dead", iterm_session_id="w1t13p0:TAB-1", done=True, last_response_at=900, updated_at=950
    )
    store.ensure("fresh", cwd="/Users/x/repo")  # relaunched in the same tab, no reply yet
    store.update_fields("fresh", iterm_session_id="w1t13p0:TAB-1", last_response_at=0)
    chosen = store.session_for_tab_uuid("TAB-1")
    assert chosen is not None and chosen.session_id == "fresh"
    assert store.session_for_tab_uuid("w9t9p9:tab-1") is not None  # tail + case-insensitive
    assert store.session_for_tab_uuid("TAB-NOPE") is None
    assert store.session_for_tab_uuid("") is None


def test_session_for_tab_uuid_most_recent_live_wins(tmp_path: Path) -> None:
    """Two live rows on one tab → the one that answered most recently."""
    store = _store(tmp_path)
    store.ensure("old", cwd="/Users/x/repo")
    store.update_fields("old", iterm_session_id="w0t1p0:TAB-2", last_response_at=100)
    store.ensure("new", cwd="/Users/x/repo")
    store.update_fields("new", iterm_session_id="w0t1p0:TAB-2", last_response_at=200)
    store.ensure("gone", cwd="/Users/x/repo")  # archived: only wins when nothing else owns the tab
    store.update_fields(
        "gone", iterm_session_id="w0t1p0:TAB-2", last_response_at=999, archived=True
    )
    chosen = store.session_for_tab_uuid("TAB-2")
    assert chosen is not None and chosen.session_id == "new"
    store.ensure("only", cwd="/Users/x/repo")
    store.update_fields("only", iterm_session_id="w0t1p0:TAB-3", archived=True)
    chosen = store.session_for_tab_uuid("TAB-3")
    assert chosen is not None and chosen.session_id == "only"


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
    legacy_schema = (
        store_mod._SCHEMA.replace("    close_requested_at INTEGER NOT NULL DEFAULT 0,\n", "")
        .replace("    close_token       TEXT    NOT NULL DEFAULT '',\n", "")
        .replace("    close_bound       TEXT    NOT NULL DEFAULT '',\n", "")
    )
    assert "close_token" not in legacy_schema
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
        assert (got.close_token, got.close_bound) == ("", "")  # tp#232 columns, '' default
        # The whole arm → claim → restore → claim cycle works on the migrated row.
        token = store.arm_close("old", 5_000_000)
        kind, claim = store.claim_after_turn("old", 5_000_000, 600_000)
        assert kind == "close" and isinstance(claim, CloseClaim) and claim.token == token
        assert store.restore_close(
            "old", armed_at=5_000_000, token=token, bound="1:x", now=5_000_001, ttl_ms=600_000
        )
        assert store.claim_after_turn("old", 5_000_002, 600_000) == (
            "close",
            CloseClaim(armed_at=5_000_000, token=token, bound="1:x"),
        )


def test_active_subagents_counter_floors_at_zero_and_resets(tmp_path: Path) -> None:
    # The SubagentStart/SubagentStop counter `switch-account` vetoes on: it must survive
    # unpaired events (a stop whose start predates the column would otherwise drive it
    # negative and hide a real subagent) and be resettable after a crash.
    store = _store(tmp_path)
    store.ensure("s1", cwd="/repo")
    assert store.active_subagents("s1") == 0
    assert store.bump_subagents("s1", 1) == 1
    assert store.bump_subagents("s1", 1) == 2
    assert store.bump_subagents("s1", -1) == 1
    assert store.bump_subagents("s1", -1) == 0
    assert store.bump_subagents("s1", -1) == 0  # floored, never negative
    assert store.active_subagents("s1") == 0
    store.bump_subagents("s1", 3)
    store.reset_subagents("s1")
    assert store.active_subagents("s1") == 0
    # An untracked session has no counter and vetoes nothing.
    assert store.bump_subagents("nosuch", 1) == 0
    assert store.active_subagents("nosuch") == 0


def test_active_subagents_column_migrates_onto_existing_db(tmp_path: Path) -> None:
    # Guards the _SESSION_COLUMNS whitelist + the ALTER-in-place migration for
    # active_subagents: a pre-migration DB gains it (default 0) and the counter works on
    # the row that predates it.
    import sqlite3

    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA.replace(
        "    active_subagents  INTEGER NOT NULL DEFAULT 0,\n", ""
    )
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old', '/repo/old')")
    conn.commit()
    conn.close()
    with Store(db) as store:  # opening runs _ensure_columns → ALTER adds active_subagents
        row = store.get("old")
        assert row is not None
        assert row.active_subagents == 0  # NOT NULL default after the migration
        assert store.bump_subagents("old", 1) == 1
        got = store.get("old")
        assert got is not None and got.active_subagents == 1


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


def _flaky_wal(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[str]:
    """Make the first *failures* ``PRAGMA journal_mode=WAL`` calls raise SQLITE_BUSY.

    ``sqlite3.Connection`` is a C type, so the raise is injected through a subclass
    handed to ``sqlite3.connect`` as its ``factory``, not by patching a method.
    """
    from command_center import store as store_mod

    tries: list[str] = []
    real_connect = sqlite3.connect

    class _Flaky(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL":
                tries.append(sql)
                if len(tries) <= failures:
                    raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    monkeypatch.setattr(
        store_mod.sqlite3,
        "connect",
        lambda *args, **kwargs: real_connect(*args, factory=_Flaky, **kwargs),
    )
    return tries


def test_store_init_retries_the_wal_pragma_a_peer_is_holding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer mid-mode-switch must be waited out, not raised at, out of the constructor.

    ``PRAGMA journal_mode`` is the one statement SQLite does not run the busy handler
    for — it returns SQLITE_BUSY immediately — so two ccc processes opening the same
    fresh DB in the same instant used to kill one of them (a hook dying while the TUI
    started).
    """
    tries = _flaky_wal(monkeypatch, failures=2)
    with Store(tmp_path / "fresh.db") as store:
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert len(tries) == 3  # two refusals waited out, the third took


def test_store_init_still_raises_when_the_wal_retries_run_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backoff is bounded — a genuinely stuck DB fails loudly, never hangs a hook."""
    from command_center import store as store_mod

    monkeypatch.setattr(store_mod, "_WAL_RETRY_SLEEP", 0.0)
    tries = _flaky_wal(monkeypatch, failures=99)
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "stuck.db")
    assert len(tries) == store_mod._WAL_RETRIES  # pylint: disable=protected-access


def test_store_init_skips_the_wal_pragma_when_already_in_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every open after the first takes no lock at all — the mode is read, not rewritten."""
    db = tmp_path / "state.db"
    Store(db).close()  # first open switches the mode
    tries = _flaky_wal(monkeypatch, failures=99)  # any WAL write would now raise
    with Store(db) as store:
        assert store.get_transcript_scan("nope") is None
    assert tries == []


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


_TTL = 10 * 60 * 1000
_BOUND = "4321:Tue Sep  2 23:00:00 2026"


def _claim_close(store: Store, now: int) -> CloseClaim:
    kind, claim = store.claim_after_turn("s1", now, _TTL)
    assert kind == "close" and isinstance(claim, CloseClaim)
    return claim


def test_close_arm_is_claimed_once_with_its_token(tmp_path: Path) -> None:
    """A fresh arm is claimed exactly once; the claim carries the stamp + token, no binding."""
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    claim = _claim_close(store, 1_000_000)
    assert claim == CloseClaim(armed_at=1_000_000, token=token, bound="")
    assert store.claim_after_turn("s1", 1_000_000, _TTL) == ("", None)  # no re-fire
    store.close()


def _close_state(store: Store) -> tuple[int, str, str, int, str]:
    row = store.conn.execute(
        "SELECT close_requested_at, close_token, close_bound, close_claimed_at, close_holder "
        "FROM sessions WHERE session_id = 's1'"
    ).fetchone()
    return int(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4])


@pytest.mark.parametrize("held", ["in_flight", "stale_pending", "switch"])
def test_claim_after_turn_leaves_a_handed_off_rows_close_arm_alone(
    tmp_path: Path, held: str
) -> None:
    """tp#1595 B4: on a handed-off row the clearing branches never touch the close arm."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", handed_off_to="s2")
    token = store.arm_close("s1", 1_000_000)
    store.conn.execute("UPDATE sessions SET close_bound = 'handoff:{}' WHERE session_id = 's1'")
    store.conn.commit()
    if held == "in_flight":
        assert store.claim_close("s1", token, _BOUND)
    if held == "switch":
        store.update_fields("s1", switch_requested_at=5_000_000, switch_config_dir="/acct")
    before = _close_state(store)
    kind, _claim = store.claim_after_turn("s1", 5_000_000, _TTL)
    assert kind == ("switch" if held == "switch" else "")
    assert _close_state(store) == before
    got = store.get("s1")
    assert got is not None and got.switch_requested_at == 0
    store.close()


@pytest.mark.parametrize("held", ["in_flight", "stale_pending", "switch"])
def test_claim_after_turn_still_clears_a_plain_rows_close_arm(tmp_path: Path, held: str) -> None:
    """Without handed_off_to the clearing branches reset the whole close arm, as before."""
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    store.conn.execute("UPDATE sessions SET close_bound = 'x' WHERE session_id = 's1'")
    store.conn.commit()
    if held == "in_flight":
        assert store.claim_close("s1", token, _BOUND)
    if held == "switch":
        store.update_fields("s1", switch_requested_at=5_000_000, switch_config_dir="/acct")
    store.claim_after_turn("s1", 5_000_000, _TTL)
    assert _close_state(store) == (0, "", "", 0, "")
    store.close()


def test_claim_after_turn_still_claims_a_fresh_handoff_token(tmp_path: Path) -> None:
    """A pending handoff token inside the TTL is claimed (B4 restores, never retires, it)."""
    store = _store(tmp_path)
    store.ensure("s1")
    store.update_fields("s1", handed_off_to="s2")
    token = store.arm_close("s1", 1_000_000)
    claim = _claim_close(store, 1_000_000)
    assert claim.token == token
    assert _close_state(store)[:2] == (0, token)
    store.close()


def test_restored_close_arm_is_claimed_again_bound_to_its_process(tmp_path: Path) -> None:
    """A transient refusal hands the arm back: the next Stop claims it with its binding."""
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    _claim_close(store, 1_000_000)
    assert store.restore_close(
        "s1", armed_at=1_000_000, token=token, bound=_BOUND, now=1_060_000, ttl_ms=_TTL
    )
    again = _claim_close(store, 1_120_000)
    assert again == CloseClaim(armed_at=1_000_000, token=token, bound=_BOUND)  # TTL kept
    store.close()


@pytest.mark.parametrize("interloper", ["undo", "rearm", "switch_now"])
def test_close_restore_is_defeated_once_the_arm_changed(tmp_path: Path, interloper: str) -> None:
    """Token ABA: an undo, a new arm or a `switch -N` between claim and restore wins."""
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    _claim_close(store, 1_000_000)
    if interloper == "undo":
        store.disarm_close("s1")
    elif interloper == "rearm":
        store.arm_close("s1", 1_030_000)
    else:
        assert (
            store.arbitrate_now_switch("s1", "/cfg", now=1_030_000, ttl_ms=_TTL, override=True)
            == ""
        )
        store.disarm_close("s1")  # …and the relaunched session's SessionStart
    assert not store.restore_close(
        "s1", armed_at=1_000_000, token=token, bound=_BOUND, now=1_060_000, ttl_ms=_TTL
    )
    row = store.get("s1")
    assert row is not None and row.close_bound == ""
    assert row.close_requested_at == (1_030_000 if interloper == "rearm" else 0)
    store.close()


def test_close_restore_never_outlives_the_original_ttl(tmp_path: Path) -> None:
    """The retry loop is bounded: past armed_at + TTL the restore fails, the claim expires."""
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    _claim_close(store, 1_000_000)
    assert store.restore_close(
        "s1", armed_at=1_000_000, token=token, bound=_BOUND, now=1_000_000 + _TTL - 1, ttl_ms=_TTL
    )
    assert store.claim_after_turn("s1", 1_000_000 + _TTL, _TTL) == ("", None)  # expired
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (0, "")
    assert not store.restore_close(
        "s1", armed_at=1_000_000, token=token, bound=_BOUND, now=1_000_000 + _TTL, ttl_ms=_TTL
    )
    store.close()


def test_retire_close_with_a_stale_token_is_a_noop(tmp_path: Path) -> None:
    """Retiring an old arm's token must not clear a newer arm."""
    store = _store(tmp_path)
    store.ensure("s1")
    old = store.arm_close("s1", 1_000_000)
    new = store.arm_close("s1", 1_010_000)
    store.retire_close("s1", old)
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (1_010_000, new)
    store.retire_close("s1", new)
    row = store.get("s1")
    assert row is not None and row.close_token == "" and row.close_requested_at == 1_010_000
    store.close()


def test_ensure_close_armed_keeps_one_token_across_concurrent_callers(tmp_path: Path) -> None:
    """Concurrent finalizers converge on ONE token; a pending or in-flight arm is kept."""
    import threading  # pylint: disable=import-outside-toplevel

    store = _store(tmp_path)
    store.ensure("s1")
    assert store.ensure_close_armed("missing", 1_000_000) == ""
    barrier = threading.Barrier(4)
    tokens: list[str] = []

    def arm(now: int) -> None:
        with Store(tmp_path / "state.db") as own:
            barrier.wait()
            tokens.append(own.ensure_close_armed("s1", now))

    workers = [threading.Thread(target=arm, args=(1_000_000 + i,)) for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    row = store.get("s1")
    assert row is not None and row.close_token and set(tokens) == {row.close_token}
    token = row.close_token
    assert store.claim_close("s1", token) == row.close_requested_at  # now in flight
    assert store.ensure_close_armed("s1", 2_000_000) == token  # in flight is kept too
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (0, token)
    store.close()


def test_token_scoped_close_with_a_stale_token_is_a_noop(tmp_path: Path) -> None:
    """claim / restore / retire act only on the row's CURRENT token; one claimer wins."""
    store = _store(tmp_path)
    store.ensure("s1")
    old = store.arm_close("s1", 1_000_000)
    token = store.arm_close("s1", 1_010_000)
    assert store.claim_close("s1", old) == 0 and store.claim_close("s1", "") == 0
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (1_010_000, token)
    assert store.claim_close("s1", token) == 1_010_000
    assert store.claim_close("s1", token) == 0  # the duplicate closer loses
    assert not store.restore_close("s1", old) and not store.retire_close("s1", old)
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (0, token)
    assert store.restore_close("s1", token, armed_at=1_010_000)  # back pending, same stamp
    assert not store.restore_close("s1", token)  # already pending: no double restore
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (1_010_000, token)
    assert store.claim_close("s1", token) == 1_010_000
    assert store.restore_close("s1", token)  # no stamp given: re-armed at the current time
    row = store.get("s1")
    assert row is not None and row.close_requested_at > 1_010_000
    assert store.claim_close("s1", token) > 0
    assert store.retire_close("s1", token)
    assert not store.retire_close("s1", token)
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (0, "")
    assert store.ensure_close_armed("s1", 3_000_000) not in ("", token)  # debt gone: new arm
    store.close()


_HOLDER = "777:Mon Nov  2 21:00:00 2026"
_OTHER = "888:Mon Nov  2 21:30:00 2026"
_DEBT = 'handoff:{"iterm": "w0t1p0:X", "pane": "", "pid": 4321, "start": "S", "tty": "t9"}'


def _holder_cols(store: Store, sid: str = "s1") -> tuple[int, str]:
    row = store.get(sid)
    assert row is not None
    return row.close_claimed_at, row.close_holder


def _in_flight(store: Store, holder: str = _HOLDER, bound: str = "") -> tuple[str, int]:
    """s1 armed, optionally bound, then claimed by *holder* — ``(token, claimed_at)``."""
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    if bound:
        store.update_fields("s1", close_bound=bound)
    assert store.claim_close("s1", token, holder) == 1_000_000
    claimed_at, got = _holder_cols(store)
    assert claimed_at > 0 and got == holder
    return token, claimed_at


def test_claim_close_stamps_holder_and_claim_time(tmp_path: Path) -> None:
    """The claim records who and since when; a holderless claim records the time only."""
    from command_center.models import now_ms  # pylint: disable=import-outside-toplevel

    store = _store(tmp_path)
    before = now_ms()
    token, claimed_at = _in_flight(store)
    assert before <= claimed_at <= now_ms()
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (0, token)
    assert store.restore_close("s1", token)
    assert store.claim_close("s1", token) > 0  # legacy caller: no holder
    claimed_at, holder = _holder_cols(store)
    assert claimed_at > 0 and holder == ""
    store.close()


@pytest.mark.parametrize(
    "transition", ["restore", "retire", "disarm", "arm", "ensure_after_retire", "switch"]
)
def test_every_clearing_transition_clears_holder_and_claim_time(
    tmp_path: Path, transition: str
) -> None:
    store = _store(tmp_path)
    token, _at = _in_flight(store)
    if transition == "restore":
        assert store.restore_close("s1", token, holder=_HOLDER)
    elif transition == "retire":
        assert store.retire_close("s1", token, holder=_HOLDER)
    elif transition == "disarm":
        store.disarm_close("s1")
    elif transition == "arm":
        assert store.arm_close("s1", 2_000_000) != token
    elif transition == "ensure_after_retire":
        assert store.retire_close("s1", token)
        assert store.ensure_close_armed("s1", 2_000_000) not in ("", token)
    else:
        assert (
            store.arbitrate_now_switch("s1", "/cfg", now=2_000_000, ttl_ms=_TTL, override=True)
            == ""
        )
    assert _holder_cols(store) == (0, "")
    store.close()


def test_stop_chain_claim_stamps_time_holderless_and_its_clears_reset(tmp_path: Path) -> None:
    """claim_after_turn's close branch: claimed now, no holder; its clearing branches clear."""
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    claim = _claim_close(store, 1_000_500)
    assert claim.token == token
    assert _holder_cols(store) == (1_000_500, "")
    assert store.hold_close("s1", token, _HOLDER)
    assert _holder_cols(store) == (1_000_500, _HOLDER)
    # An expired stamp (no claim) clears the whole arm, holder columns included.
    store.conn.execute(
        "UPDATE sessions SET close_requested_at = 5, close_claimed_at = 9, "
        "close_holder = ? WHERE session_id = 's1'",
        (_OTHER,),
    )
    store.conn.commit()
    assert store.claim_after_turn("s1", 1_000_000 + _TTL, _TTL) == ("", None)
    assert _holder_cols(store) == (0, "")
    store.close()


def test_hold_close_fills_only_an_empty_holder_on_the_same_in_flight_token(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.ensure("s1")
    token = store.arm_close("s1", 1_000_000)
    assert not store.hold_close("s1", token, _HOLDER)  # pending: nobody to hold for
    _claim_close(store, 1_000_000)
    assert not store.hold_close("s1", "stale", _HOLDER)  # another token
    assert not store.hold_close("s1", token, "")  # an empty identity registers nothing
    assert store.hold_close("s1", token, _HOLDER)
    assert not store.hold_close("s1", token, _OTHER)  # already held: never overwritten
    assert _holder_cols(store)[1] == _HOLDER
    store.close()


def test_mark_in_flight_seen_stamps_only_a_legacy_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token, _at = _in_flight(store)
    assert not store.mark_in_flight_seen("s1", token, 5_000_000)  # already has a claim time
    store.conn.execute("UPDATE sessions SET close_claimed_at = 0 WHERE session_id = 's1'")
    store.conn.commit()
    assert not store.mark_in_flight_seen("s1", "stale", 5_000_000)
    assert store.mark_in_flight_seen("s1", token, 5_000_000)
    assert _holder_cols(store) == (5_000_000, _HOLDER)
    assert not store.mark_in_flight_seen("s1", token, 6_000_000)  # first seen stays first
    assert store.restore_close("s1", token)
    assert not store.mark_in_flight_seen("s1", token, 6_000_000)  # pending: not in flight
    store.close()


@pytest.mark.parametrize("bound", [_DEBT, _BOUND])
def test_reclaim_stale_close_goes_pending_and_keeps_the_bound(tmp_path: Path, bound: str) -> None:
    """A successful reclaim: pending at *now*, holder cleared, close_bound byte-for-byte."""
    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=bound)
    assert store.reclaim_stale_close(
        "s1", token, holder=_HOLDER, claimed_at=claimed_at, now=9_000_000
    )
    row = store.get("s1")
    assert row is not None
    assert (row.close_requested_at, row.close_token, row.close_bound) == (9_000_000, token, bound)
    assert (row.close_claimed_at, row.close_holder) == (0, "")
    store.close()


@pytest.mark.parametrize("change", ["token", "holder", "claimed_at", "pending"])
def test_reclaim_stale_close_with_changed_values_is_a_noop(tmp_path: Path, change: str) -> None:
    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=_DEBT)
    kwargs = {"holder": _HOLDER, "claimed_at": claimed_at, "now": 9_000_000}
    sid_token = token
    if change == "token":
        sid_token = "stale"
    elif change == "holder":
        kwargs["holder"] = _OTHER
    elif change == "claimed_at":
        kwargs["claimed_at"] = claimed_at - 1
    else:
        assert store.restore_close("s1", token, bound=_DEBT)
    before = store.get("s1")
    assert not store.reclaim_stale_close("s1", sid_token, **kwargs)  # type: ignore[arg-type]
    after = store.get("s1")
    assert before is not None and after is not None
    assert (after.close_requested_at, after.close_token, after.close_bound) == (
        before.close_requested_at,
        before.close_token,
        before.close_bound,
    )
    assert (after.close_claimed_at, after.close_holder) == (
        before.close_claimed_at,
        before.close_holder,
    )
    store.close()


def test_bind_close_debt_writes_the_debt_and_keeps_the_token_in_flight(tmp_path: Path) -> None:
    """The closer's pre-signal write: bound replaced, stamp 0, claim time and holder kept."""
    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=_BOUND)
    assert store.bind_close_debt("s1", token, _HOLDER, _DEBT)
    row = store.get("s1")
    assert row is not None
    assert (row.close_requested_at, row.close_token, row.close_bound) == (0, token, _DEBT)
    assert (row.close_claimed_at, row.close_holder) == (claimed_at, _HOLDER)
    store.close()


@pytest.mark.parametrize("change", ["token", "holder", "empty_holder", "pending", "reclaimed"])
def test_bind_close_debt_with_changed_values_is_a_noop(tmp_path: Path, change: str) -> None:
    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=_BOUND)
    sid_token, holder = token, _HOLDER
    if change == "token":
        sid_token = "stale"
    elif change == "holder":
        holder = _OTHER
    elif change == "empty_holder":
        assert store.restore_close("s1", token, bound=_BOUND)
        assert store.claim_close("s1", token) > 0  # holderless in flight
        holder = ""
    elif change == "pending":
        assert store.restore_close("s1", token, bound=_BOUND)
    else:  # reclaimed by the sweep, then re-claimed by a second closer
        assert store.reclaim_stale_close(
            "s1", token, holder=_HOLDER, claimed_at=claimed_at, now=9_000_000
        )
        assert store.claim_close("s1", token, _OTHER) > 0
    before = store.get("s1")
    assert not store.bind_close_debt("s1", sid_token, holder, _DEBT)
    after = store.get("s1")
    assert before is not None and after is not None
    assert dataclasses.replace(after, updated_at=0) == dataclasses.replace(before, updated_at=0)
    assert after.close_bound == _BOUND
    store.close()


def test_two_concurrent_reclaims_of_one_token_exactly_one_wins(tmp_path: Path) -> None:
    import threading  # pylint: disable=import-outside-toplevel

    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=_DEBT)
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def reclaim(now: int) -> None:
        with Store(tmp_path / "state.db") as own:
            barrier.wait()
            results.append(
                own.reclaim_stale_close("s1", token, holder=_HOLDER, claimed_at=claimed_at, now=now)
            )

    workers = [threading.Thread(target=reclaim, args=(9_000_000 + i,)) for i in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(results) == [False, True]
    row = store.get("s1")
    assert row is not None and row.close_requested_at in (9_000_000, 9_000_001)
    assert row.close_bound == _DEBT
    store.close()


@pytest.mark.parametrize(
    "order",
    ["restore_then_reclaim", "reclaim_then_restore", "retire_then_reclaim", "reclaim_then_retire"],
)
def test_reclaim_interleaved_with_the_holders_own_settle_ends_consistent(
    tmp_path: Path, order: str
) -> None:
    """Whichever lands first wins; the other is a no-op and the row is in ONE clean state."""
    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=_DEBT)
    other = Store(tmp_path / "state.db")  # the closer's own connection

    def reclaim() -> bool:
        return store.reclaim_stale_close(
            "s1", token, holder=_HOLDER, claimed_at=claimed_at, now=9_000_000
        )

    if order == "restore_then_reclaim":
        assert other.restore_close("s1", token, armed_at=1_000_000, bound=_DEBT, holder=_HOLDER)
        assert not reclaim()
        expect = (1_000_000, token)
    elif order == "reclaim_then_restore":
        assert reclaim()
        assert not other.restore_close("s1", token, armed_at=1_000_000, bound="", holder=_HOLDER)
        expect = (9_000_000, token)
    elif order == "retire_then_reclaim":
        assert other.retire_close("s1", token, holder=_HOLDER)
        assert not reclaim()
        expect = (0, "")
    else:
        assert reclaim()
        assert not other.retire_close("s1", token, holder=_HOLDER)  # holder is gone
        expect = (9_000_000, token)
    other.close()
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == expect
    assert row.close_bound == (_DEBT if expect[1] else "")
    assert (row.close_claimed_at, row.close_holder) == (0, "")
    store.close()


def test_holder_checked_restore_and_retire_spare_another_holders_claim(tmp_path: Path) -> None:
    """Reclaimed, then claimed by closer B: closer A's late restore and retire are no-ops."""
    store = _store(tmp_path)
    token, claimed_at = _in_flight(store, bound=_DEBT)
    assert store.reclaim_stale_close(
        "s1", token, holder=_HOLDER, claimed_at=claimed_at, now=9_000_000
    )
    assert store.claim_close("s1", token, _OTHER) == 9_000_000
    assert not store.restore_close("s1", token, bound="", holder=_HOLDER)
    assert not store.retire_close("s1", token, holder=_HOLDER)
    row = store.get("s1")
    assert row is not None
    assert (row.close_requested_at, row.close_token, row.close_bound) == (0, token, _DEBT)
    assert row.close_holder == _OTHER
    assert store.retire_close("s1", token, holder=_OTHER)  # B's own settle still works
    store.close()


def test_close_holder_columns_migrate_onto_an_older_db(tmp_path: Path) -> None:
    """A DB from before tp#1595 opens, gains both columns at their defaults and works."""
    from command_center import store as store_mod  # pylint: disable=import-outside-toplevel

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA.replace(  # pylint: disable=protected-access
        "    close_claimed_at  INTEGER NOT NULL DEFAULT 0,\n", ""
    ).replace("    close_holder      TEXT    NOT NULL DEFAULT '',\n", "")
    assert "close_holder" not in legacy_schema and "close_claimed_at" not in legacy_schema
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute(
        "INSERT INTO sessions (session_id, cwd, close_token, close_requested_at) "
        "VALUES ('old', '/repo', 'tok', 0)"
    )
    conn.commit()
    conn.close()
    with Store(db) as store:
        assert _holder_cols(store, "old") == (0, "")  # a legacy in-flight token
        assert store.mark_in_flight_seen("old", "tok", 5_000_000)
        assert store.reclaim_stale_close("old", "tok", holder="", claimed_at=5_000_000, now=7)
        assert store.claim_close("old", "tok", _HOLDER) == 7
        assert _holder_cols(store, "old")[1] == _HOLDER


def test_close_unarmed_or_expired_is_never_claimed(tmp_path: Path) -> None:
    """Unarmed → nothing; a stamp past the TTL is cleared (token too), never claimed."""
    store = _store(tmp_path)
    store.ensure("s1")
    assert store.claim_after_turn("s1", 1_000_000, _TTL) == ("", None)
    store.arm_close("s1", 1_000_000)
    assert store.claim_after_turn("s1", 1_000_000 + _TTL + 1, _TTL) == ("", None)
    row = store.get("s1")
    assert row is not None and (row.close_requested_at, row.close_token) == (0, "")
    store.close()


# ---------------------------------------------------------------------------
# mirror vouches + health (tp#123)
# ---------------------------------------------------------------------------
def test_mirror_vouches_upsert_and_drop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_mirror_vouches([])  # no-op, no commit needed
    store.put_mirror_vouches(
        [
            MirrorVouch("/v/a.md", "s1", "raw-a", "out-a", "policy", 10),
            MirrorVouch("/v/b.md", "s2", "raw-b", "out-b", "policy", 20),
        ]
    )
    assert set(store.mirror_vouches()) == {"/v/a.md", "/v/b.md"}
    store.put_mirror_vouches([MirrorVouch("/v/a.md", "s1", "raw-a2", "out-a2", "policy2", 30)])
    row = store.mirror_vouches()["/v/a.md"]
    assert (row.raw_sha, row.out_sha, row.policy, row.vouched_at) == (
        "raw-a2",
        "out-a2",
        "policy2",
        30,
    )
    store.drop_mirror_vouches([])  # no-op
    store.drop_mirror_vouches(["/v/b.md"])
    assert set(store.mirror_vouches()) == {"/v/a.md"}
    store.drop_mirror_vouches()
    assert store.mirror_vouches() == {}


def test_deleting_a_session_drops_its_vouches(tmp_path: Path) -> None:
    from command_center.models import MirrorVouch

    store = _store(tmp_path)
    store.ensure("s1", cwd="/repo")
    store.ensure("s2", cwd="/repo")
    store.put_mirror_vouches(
        [
            MirrorVouch("/v/a.md", "s1", "r", "o", "p", 1),
            MirrorVouch("/v/b.md", "s2", "r", "o", "p", 1),
        ]
    )
    assert store.delete_many(["s1"]) == 1
    assert set(store.mirror_vouches()) == {"/v/b.md"}


def test_mirror_health_is_a_single_overwritten_row(tmp_path: Path) -> None:
    from command_center.models import MirrorHealth

    store = _store(tmp_path)
    assert store.mirror_health() is None
    store.put_mirror_health(
        MirrorHealth(at=5, vouched=1, scrubbed=2, withheld=3, deferred=4, reason="x")
    )
    store.put_mirror_health(MirrorHealth(at=6, vouched=0, scrubbed=0, withheld=0, deferred=0))
    assert store.mirror_health() == MirrorHealth(
        at=6, vouched=0, scrubbed=0, withheld=0, deferred=0
    )
    assert store.conn.execute("SELECT COUNT(*) FROM mirror_health").fetchone()[0] == 1


def test_mirror_vouch_src_fp_round_trips_and_legacy_reads_empty(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.put_mirror_vouches(
        [
            MirrorVouch("/v/a.md", "s1", "r", "o", "p", 1, "fp-a"),
            MirrorVouch("/v/b.md", "s2", "r", "o", "p", 1),
        ]
    )
    rows = store.mirror_vouches()
    assert rows["/v/a.md"].src_fp == "fp-a" and rows["/v/b.md"].src_fp == ""
    store.conn.execute("UPDATE mirror_vouch SET src_fp = NULL")
    store.conn.commit()
    assert store.mirror_vouches()["/v/a.md"].src_fp == ""
    store.close()


def test_subagent_started_at_tracks_the_latest_start(tmp_path: Path) -> None:
    # Only a SubagentStart (+1) stamps the time; a stop leaves it, an untracked row is None.
    store = _store(tmp_path)
    store.ensure("s1", cwd="/repo")
    assert store.subagent_started_at("s1") is None
    store.bump_subagents("s1", 1)
    first = store.subagent_started_at("s1")
    assert first is not None and first > 0
    store.bump_subagents("s1", -1)
    assert store.subagent_started_at("s1") == first
    assert store.subagent_started_at("nosuch") is None


def test_claim_draft_stamps_the_launch_pid(tmp_path: Path) -> None:
    """tp#1512: the claim writes the exec-in-place claude pid in the same UPDATE."""
    store = _store(tmp_path)
    store.create_draft("j1", "/repo", "aim")
    assert store.claim_draft("j1", pid=4242) is True
    row = store.get("j1")
    assert row is not None and not row.draft and row.last_seen_pid == 4242
    assert store.claim_draft("j1", pid=5151) is False  # a losing claimant stamps nothing
    assert store.get("j1").last_seen_pid == 4242  # type: ignore[union-attr]
    store.create_draft("j2", "/repo", "aim")
    assert store.claim_draft("j2") is True  # no pid: the column is left alone
    assert store.get("j2").last_seen_pid is None  # type: ignore[union-attr]
    store.close()


# ---- B5 (tp#1566): aim_history.set_by + latest_user_aim -----------------------------


def _stamps(store: Store, sid: str) -> list[tuple[str, str | None]]:
    return [(rev.aim, rev.set_by) for rev in store.list_aim_history(sid)]


def test_b5_set_aim_stamps_the_new_row_and_defaults_to_user(tmp_path: Path) -> None:
    """``set_aim`` stamps only the row it appends; no stamp is ``user`` (tp#1991), an
    explicit ``None`` leaves it NULL."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "first", set_by=AIM_SET_BY_LAUNCH)
        store.set_aim("s1", "second", set_by=AIM_SET_BY_USER)
        store.set_aim("s1", "third", set_by=AIM_SET_BY_TOOL)
        store.set_aim("s1", "fourth")
        store.set_aim("s1", "fifth", set_by=None)
        assert _stamps(store, "s1") == [
            ("first", "launch"),
            ("second", "user"),
            ("third", "tool"),
            ("fourth", "user"),
            ("fifth", None),
        ]


def test_b5_seeded_pre_history_original_stays_null(tmp_path: Path) -> None:
    """The seeded original of a pre-history AIM is unstamped; the new row carries the stamp."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.update_fields("s1", aim="legacy aim")  # predates history: no row
        store.set_aim("s1", "new aim", set_by=AIM_SET_BY_USER)
        assert _stamps(store, "s1") == [("legacy aim", None), ("new aim", "user")]


def test_b5_create_draft_stamps_user_by_default(tmp_path: Path) -> None:
    """Job creation is the user's act: ``create_draft`` stamps ``user`` unless told otherwise."""
    with _store(tmp_path) as store:
        store.create_draft("j1", "/repo", "ship the job")
        store.create_draft("j2", "/repo", "carried aim", aim_set_by=None)
        assert _stamps(store, "j1") == [("ship the job", "user")]
        assert _stamps(store, "j2") == [("carried aim", None)]


def test_tp1991_create_draft_stamps_an_automated_registration_tool(tmp_path: Path) -> None:
    """An automated registration stamps ``tool``; an explicit ``aim_set_by`` still wins."""
    with _store(tmp_path) as store:
        store.create_draft("j1", "/repo", "agent job", registered_by="automated")
        store.create_draft(
            "j2", "/repo", "told so", registered_by="automated", aim_set_by=AIM_SET_BY_USER
        )
        store.create_draft("j3", "/repo", "user job", registered_by="")
        assert _stamps(store, "j1") == [("agent job", "tool")]
        assert _stamps(store, "j2") == [("told so", "user")]
        assert _stamps(store, "j3") == [("user job", "user")]


def test_b5_set_first_aim_stamps_the_row_it_rewrites(tmp_path: Path) -> None:
    """``set_first_aim`` re-stamps revision (1) in place (default ``user``), nothing else."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "launched aim", set_by=AIM_SET_BY_LAUNCH)
        store.set_aim("s1", "tool aim", set_by=AIM_SET_BY_TOOL)
        assert store.set_first_aim("s1", "restated first aim") is True
        assert _stamps(store, "s1") == [("restated first aim", "user"), ("tool aim", "tool")]
        assert store.set_first_aim("s1", "again", set_by=AIM_SET_BY_TOOL) is True
        assert _stamps(store, "s1")[0] == ("again", "tool")


def test_b5_migration_adds_set_by_and_leaves_legacy_rows_null(tmp_path: Path) -> None:
    """An old DB without ``aim_history.set_by`` gains the column; its rows read NULL."""
    from command_center import store as store_mod

    db = tmp_path / "legacy.db"
    legacy_schema = store_mod._SCHEMA.replace(
        "    created_at  INTEGER NOT NULL DEFAULT 0,\n    set_by      TEXT\n",
        "    created_at  INTEGER NOT NULL DEFAULT 0\n",
    )
    assert legacy_schema != store_mod._SCHEMA  # the replace really dropped the column
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)
    conn.execute("INSERT INTO sessions (session_id, cwd, aim) VALUES ('old', '/r', 'old aim')")
    conn.execute(
        "INSERT INTO aim_history (session_id, aim, score, created_at) "
        "VALUES ('old', 'old aim', 40, 1)"
    )
    conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(aim_history)")}
    conn.close()
    assert "set_by" not in columns
    with Store(db) as store:  # opening runs _ensure_columns → ALTER adds set_by
        assert _stamps(store, "old") == [("old aim", None)]
        store.set_aim("old", "new aim", set_by=AIM_SET_BY_USER)
        assert _stamps(store, "old") == [("old aim", None), ("new aim", "user")]


def test_b5_latest_user_aim_picks_the_newest_user_row(tmp_path: Path) -> None:
    """The newest ``user`` row wins over later ``tool``/``launch``/unstamped rows."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "A", set_by=AIM_SET_BY_USER)
        store.set_aim("s1", "B", set_by=AIM_SET_BY_USER)
        store.set_aim("s1", "B: tp#1", set_by=AIM_SET_BY_TOOL)
        store.set_aim("s1", "handoff accepted by session 12345678", set_by=None)
        assert store.latest_user_aim("s1") == ("B", "user")


def test_b5_latest_user_aim_falls_back_to_the_stripped_current_aim(tmp_path: Path) -> None:
    """Legacy history (no ``user`` row): the current AIM without its tp id run, stamp None."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "A", set_by=None)
        store.set_aim("s1", "B: tp#5, #6", set_by=None)
        assert store.latest_user_aim("s1") == ("B", None)
        # An AIM with no history rows at all (predates tracking) works the same way.
        store.ensure("s2", cwd="/repo")
        store.update_fields("s2", aim="plain words")
        assert store.latest_user_aim("s2") == ("plain words", None)
        # A multi-line AIM is kept whole (aimtext only reads the first line).
        store.ensure("s3", cwd="/repo")
        store.update_fields("s3", aim="line one\nline two")
        assert store.latest_user_aim("s3") == ("line one\nline two", None)


def test_b5_latest_user_aim_falls_back_to_the_first_row(tmp_path: Path) -> None:
    """A run-only (or missing) current AIM falls back to the first row; nothing → None."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "A", set_by=AIM_SET_BY_LAUNCH)
        store.set_aim("s1", "tp#5", set_by=None)
        assert store.latest_user_aim("s1") == ("A", "launch")
        store.update_fields("s1", aim=None)
        assert store.latest_user_aim("s1") == ("A", "launch")
        store.ensure("empty", cwd="/repo")
        assert store.latest_user_aim("empty") is None
        assert store.latest_user_aim("unknown") is None


def test_tp1991_latest_user_aim_step_two_returns_the_matching_rows_stamp(
    tmp_path: Path,
) -> None:
    """No ``user`` row: the current AIM carries the stamp of the row holding it."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "launched goal", set_by=AIM_SET_BY_LAUNCH)
        assert store.latest_user_aim("s1") == ("launched goal", "launch")
        store.set_aim("s1", "tool goal: tp#4", set_by=AIM_SET_BY_TOOL)
        assert store.latest_user_aim("s1") == ("tool goal", "tool")  # words match


def test_tp1991_stamp_of_matches_exact_then_words(tmp_path: Path) -> None:
    """``stamp_of``: the newest exact match, else a words match, else ``None``."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "goal", set_by=AIM_SET_BY_LAUNCH)
        store.set_aim("s1", "other", set_by=AIM_SET_BY_USER)
        store.set_aim("s1", "goal", set_by=AIM_SET_BY_TOOL)
        store.set_aim("s1", "words: tp#3", set_by=AIM_SET_BY_USER)
        store.set_aim("s1", "unknown author", set_by=None)
        assert store.stamp_of("s1", "goal") == "tool"  # the newest of two exact rows
        assert store.stamp_of("s1", "words") == "user"  # words match
        assert store.stamp_of("s1", "unknown author") is None
        assert store.stamp_of("s1", "never written") is None
        assert store.stamp_of("nobody", "goal") is None


def test_tp1991_stamp_of_looks_through_a_strip_rewrite(tmp_path: Path) -> None:
    """A ``tool`` row that only stripped the id run off the row before it is never the
    answer: the earlier row's stamp is (NULL stays ``None``)."""
    with _store(tmp_path) as store:
        conn = store.conn
        for sid, first in (("s1", AIM_SET_BY_LAUNCH), ("s2", None)):
            store.ensure(sid, cwd="/repo")
            for at, (aim, by) in enumerate((("fix it: tp#7", first), ("fix it", "tool"))):
                conn.execute(
                    "INSERT INTO aim_history (session_id, aim, score, created_at, set_by) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (sid, aim, at + 1, by),
                )
        conn.commit()
        assert store.stamp_of("s1", "fix it") == "launch"
        assert store.stamp_of("s2", "fix it") is None
        # A tool row that is NOT such a rewrite keeps its stamp.
        store.ensure("s3", cwd="/repo")
        store.set_aim("s3", "fix it: tp#7", set_by=AIM_SET_BY_USER)
        store.set_aim("s3", "a new goal", set_by=AIM_SET_BY_TOOL)
        assert store.stamp_of("s3", "a new goal") == "tool"


def test_tp1991_carry_first_aim_stamps_an_unstamped_anchor_relay(tmp_path: Path) -> None:
    """``carry_first_aim`` keeps a stamped anchor's stamp; a NULL anchor becomes ``relay``."""
    with _store(tmp_path) as store:
        for sid, by in (("root-null", None), ("root-launch", AIM_SET_BY_LAUNCH)):
            store.ensure(sid, cwd="/repo")
            store.set_aim(sid, f"anchor of {sid}", set_by=by)
        for target, source in (("t1", "root-null"), ("t2", "root-launch")):
            store.ensure(target, cwd="/repo")
            store.set_aim(target, "successor goal", set_by=AIM_SET_BY_RELAY)
            assert store.carry_first_aim(target, source)
        assert _stamps(store, "t1")[0] == ("anchor of root-null", "relay")
        assert _stamps(store, "t2")[0] == ("anchor of root-launch", "launch")


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_tp1991_every_production_aim_writer_passes_set_by() -> None:
    """Every ``set_aim`` / ``_record_aim_history`` / ``_insert_aim_history`` call in the
    package names its stamp: no writer leaves ``aim_history.set_by`` NULL by default."""
    package = Path(__file__).resolve().parent.parent / "command_center"
    writers = {"set_aim", "_record_aim_history", "_insert_aim_history"}
    missing: list[str] = []
    seen = 0
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in writers:
                seen += 1
                if not any(kw.arg == "set_by" for kw in node.keywords):
                    missing.append(f"{path.name}:{node.lineno}")
    assert seen >= 10  # the scan really found the writers
    assert not missing


# ---- B9 (tp#1566): Store.rewrite_aim_text — the one-time strip's write path --------------


def test_b9_rewrite_aim_text_keeps_everything_but_the_text(tmp_path: Path) -> None:
    """Only ``sessions.aim`` changes plus one ``tool`` history row; nothing is reset."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "fix decide", set_by=AIM_SET_BY_USER)
        # The old tracker rewrite (unstamped).
        store.set_aim("s1", "fix decide: tp#7, #8", set_by=None)
        # Auto sub-goals: set_aim would clear these, rewrite_aim_text must not.
        store.set_subgoals("s1", ["derived step", "second step"], source="auto")
        store.update_fields(
            "s1",
            aim_met=True,
            context_offset=4242,
            aim_score=77,
            aim_prev="fix decide",
            short_aim="decide fix",
        )
        before = store.get("s1")
        assert before is not None
        subgoals_before = [(g.text, g.source) for g in store.list_subgoals("s1")]
        assert [source for _, source in subgoals_before] == ["auto", "auto"]

        assert store.rewrite_aim_text("s1", "fix decide") is True

        after = store.get("s1")
        assert after is not None
        assert after.aim == "fix decide"
        assert (
            after.aim_met,
            after.context_offset,
            after.aim_score,
            after.aim_prev,
            after.short_aim,
        ) == (True, 4242, 77, "fix decide", "decide fix")
        assert [(g.text, g.source) for g in store.list_subgoals("s1")] == subgoals_before
        history = store.list_aim_history("s1")
        assert [(r.aim, r.set_by) for r in history] == [
            ("fix decide", "user"),
            ("fix decide: tp#7, #8", None),
            ("fix decide", "tool"),
        ]
        assert history[-1].short_aim == "decide fix"
        # Re-running is a no-op (same text): no new row.
        assert store.rewrite_aim_text("s1", "fix decide") is False
        assert len(store.list_aim_history("s1")) == 3


def test_b9_rewrite_aim_text_seeds_a_pre_history_original(tmp_path: Path) -> None:
    """An AIM that predates history keeps its original as an unstamped first row."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.update_fields("s1", aim="old words: tp#3")
        assert store.rewrite_aim_text("s1", "old words") is True
        assert [(r.aim, r.set_by) for r in store.list_aim_history("s1")] == [
            ("old words: tp#3", None),
            ("old words", "tool"),
        ]
        # No AIM, empty text or an unknown session: refused, nothing written.
        store.ensure("bare", cwd="/repo")
        assert store.rewrite_aim_text("bare", "x") is False
        assert store.rewrite_aim_text("s1", "  ") is False
        assert store.rewrite_aim_text("unknown", "x") is False
        assert store.list_aim_history("bare") == []


def test_tp1991_rewrite_aim_text_inherits_the_rewritten_rows_stamp(tmp_path: Path) -> None:
    """``strip-aim-runs`` keeps the author: the rewrite carries the stamp of the row it
    rewrites, ``tool`` (the fallback) only when that row has none."""
    with _store(tmp_path) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "the user's goal: tp#7", set_by=AIM_SET_BY_USER)
        assert store.rewrite_aim_text("s1", "the user's goal", set_by=AIM_SET_BY_TOOL)
        assert _stamps(store, "s1") == [
            ("the user's goal: tp#7", "user"),
            ("the user's goal", "user"),
        ]


_TAB = "w0t1p0:44444444-0000-4000-8000-000000000004"
_TAB_PID = 4_000_002


def _tabbed(store: Store, sid: str, iid: str | None = _TAB, pid: int | None = _TAB_PID) -> None:
    store.ensure(sid, cwd="/repo")
    store.conn.execute(
        "UPDATE sessions SET iterm_session_id = ?, last_seen_pid = ?, status = 'parked', "
        "title_written = 'old title', title_generation = 3, title_written_at = 123 "
        "WHERE session_id = ?",
        (iid, pid, sid),
    )
    store.conn.commit()


def test_list_tabbed_sessions_selects_non_draft_rows_with_a_tab_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _tabbed(store, "open")
    _tabbed(store, "done")
    store.update_fields("done", done=True)
    _tabbed(store, "arch")
    store.update_fields("arch", archived=True)
    _tabbed(store, "nopid", pid=None)
    _tabbed(store, "blank", iid="  ")
    _tabbed(store, "null", iid=None)
    store.create_draft("draft", "/repo", "a future job")
    store.update_fields("draft", iterm_session_id=_TAB)
    rows = {r.session_id: r for r in store.list_tabbed_sessions()}
    assert set(rows) == {"open", "done", "arch", "nopid"}
    assert rows["open"].iterm_session_id == _TAB and rows["open"].last_seen_pid == _TAB_PID
    assert rows["open"].status == "parked" and not rows["open"].draft
    assert rows["done"].done and rows["arch"].archived
    assert rows["nopid"].last_seen_pid is None


def _tab_cols(store: Store, sid: str) -> tuple:
    return tuple(
        store.conn.execute(
            "SELECT iterm_session_id, title_written, title_written_at, title_generation, "
            "updated_at, status FROM sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
    )


def test_release_tab_clears_tab_and_title_state_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("command_center.store.pid_alive", lambda _pid: False)
    store = _store(tmp_path)
    _tabbed(store, "s1")
    before = _tab_cols(store, "s1")
    assert store.release_tab("s1", _TAB, _TAB_PID) is True
    after = _tab_cols(store, "s1")
    assert after[:3] == (None, None, 0)
    assert after[3:] == before[3:]  # title_generation, updated_at, status untouched
    assert store.release_tab("s1", _TAB, _TAB_PID) is False  # already released
    assert not store.list_tabbed_sessions()


def test_release_tab_refuses_on_changed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = {"now": False}
    monkeypatch.setattr("command_center.store.pid_alive", lambda _pid: alive["now"])
    store = _store(tmp_path)
    _tabbed(store, "s1")
    before = _tab_cols(store, "s1")
    assert store.release_tab("s1", "w0t2p0:55555555-0000-4000-8000-000000000005", _TAB_PID) is False
    assert store.release_tab("s1", _TAB, _TAB_PID + 1) is False  # pid changed
    assert store.release_tab("s1", _TAB, 0) is False  # never a non-positive pid
    assert store.release_tab("missing", _TAB, _TAB_PID) is False
    alive["now"] = True  # the pid came alive between the read and the write
    assert store.release_tab("s1", _TAB, _TAB_PID) is False
    assert _tab_cols(store, "s1") == before


def test_release_tab_refuses_a_row_without_a_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("command_center.store.pid_alive", lambda _pid: False)
    store = _store(tmp_path)
    _tabbed(store, "nopid", pid=None)
    assert store.release_tab("nopid", _TAB, _TAB_PID) is False
    assert _tab_cols(store, "nopid")[0] == _TAB
