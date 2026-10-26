"""Tests for the resume-halted orchestrator's pure planner (and candidate finder).

The planner ``resume.plan`` is a pure function over (observed state, queue, now,
config, reset-signal) → (next queue, actions). These tests exercise the tricky
behaviour with no spawning: the reset gate, the global stagger, per-repo serial
dispatch, the transcript-progress finish signal, fresh-pid reap ordering, the
bounded requeue/fail ladder, manual-resume adoption, and done-pruning.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from command_center.config import Config
from command_center.models import LiveSession, now_ms
from command_center.resume import (
    _BACKOFF_BASE_MS,
    _BACKOFF_CAP_MS,
    Action,
    Entry,
    Observation,
    QueueState,
    _invalidate_reset,
    _is_drained,
    _signal_path,
    _state_path,
    apply_actions,
    candidates,
    has_work,
    load_state,
    plan,
    reconcile_failed_entries,
    repo_of,
    save_state,
    tick,
    will_auto_resume,
)
from command_center.store import Store

NOW = 1_000_000_000_000  # arbitrary epoch ms
# The reset gate is keyed per Claude account. These planner tests are single-account, so
# every entry carries the default key (""); the multi-account gating is covered by
# test_multi_account.py and test_two_accounts_gate_independently below.
ACCT = ""


def _cfg(**kw: object) -> Config:
    cfg = Config()
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _obs(
    *,
    alive: bool = False,
    raw: str = "",
    halted: bool = False,
    size: int = 0,
    cwd: str = "/r1",
    repo: str = "/r1",
    account: str = ACCT,
    progressed: bool = False,
    reset_hint: int = 0,
) -> Observation:
    return Observation(
        alive=alive,
        raw_status=raw,
        halted=halted,
        transcript_size=size,
        cwd=cwd,
        repo=repo,
        account=account,
        progressed=progressed,
        reset_hint_ms=reset_hint,
    )


def _kinds(actions: list) -> list[str]:
    return [a.kind for a in actions]


def _launch_ids(actions: list) -> list[str]:
    return [a.session_id for a in actions if a.kind == "launch_resume"]


# --------------------------------------------------------------------------- #
# reset gate
# --------------------------------------------------------------------------- #
def test_enqueues_and_waits_for_reset() -> None:
    observed = {"a": _obs(halted=True)}
    state, actions = plan(observed, {"a"}, QueueState(), NOW, _cfg(), reset_signals=set())
    assert state.entries["a"].state == "queued"
    assert "ensure_reset_wait" in _kinds(actions)
    assert "launch_resume" not in _kinds(actions)
    assert not state.reset_confirmed_at  # gate still closed


def test_legacy_entry_account_is_backfilled_from_observation() -> None:
    """A queue entry persisted BEFORE the per-account gate is re-stamped, not stranded.

    Such an entry carries the default key ("") whatever seat it ran on; left alone it
    would wait forever on the wrong account's gate.
    """
    entries = {"a": Entry("a", repo="/r1", cwd="/r1", account=ACCT)}  # legacy: no account
    observed = {"a": _obs(halted=True, account="work")}
    state, actions = plan(
        observed, {"a"}, QueueState(entries=entries), NOW, _cfg(), reset_signals=set()
    )
    assert state.entries["a"].account == "work"  # re-stamped from the live observation
    assert [a.account for a in actions if a.kind == "ensure_reset_wait"] == ["work"]


def test_reset_signal_confirms_then_dispatches() -> None:
    # The entry was queued on an EARLIER tick (a same-tick signal would be pre-halt
    # evidence and is invalidated instead — see the stale-evidence tests below).
    entries = {"a": Entry("a", repo="/r1", cwd="/r1")}
    observed = {"a": _obs(halted=True)}
    state, actions = plan(
        observed, {"a"}, QueueState(entries=entries), NOW, _cfg(), reset_signals={ACCT}
    )
    assert "confirm_reset" in _kinds(actions)
    assert state.reset_confirmed_at[ACCT] == NOW
    # confirmation + a free repo + open stagger gate → one resume dispatched same tick
    assert _launch_ids(actions) == ["a"]
    assert state.entries["a"].state == "launching"


# --------------------------------------------------------------------------- #
# stagger + per-repo serial
# --------------------------------------------------------------------------- #
def test_global_stagger_one_launch_per_tick() -> None:
    observed = {
        "a": _obs(halted=True, cwd="/r1", repo="/r1"),
        "b": _obs(halted=True, cwd="/r2", repo="/r2"),
    }
    entries = {
        "a": Entry("a", repo="/r1", cwd="/r1"),
        "b": Entry("b", repo="/r2", cwd="/r2"),
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=0, entries=entries)
    state, actions = plan(observed, {"a", "b"}, base, NOW, _cfg(), reset_signals=set())
    assert len(_launch_ids(actions)) == 1  # only one resume per tick, regardless of repo count
    launched = _launch_ids(actions)[0]
    other = "b" if launched == "a" else "a"
    assert state.entries[launched].state == "launching"
    assert state.entries[other].state == "queued"


def test_stagger_gate_closed_blocks_launch() -> None:
    observed = {"a": _obs(halted=True)}
    base = QueueState(
        reset_confirmed_at={ACCT: NOW - 1},
        last_launch_at=NOW - 1000,  # 1s ago < 120s
        entries={"a": Entry("a", repo="/r1", cwd="/r1")},
    )
    state, actions = plan(
        observed, {"a"}, base, NOW, _cfg(resume_stagger_sec=120), reset_signals=set()
    )
    assert "launch_resume" not in _kinds(actions)
    assert state.entries["a"].state == "queued"


def test_per_repo_serial_holds_sibling_launches_other_repo() -> None:
    # /r1 already has a running resume; its queued sibling waits, /r2's head goes.
    entries = {
        "A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100),
        "B": Entry("B", repo="/r1", cwd="/r1"),
        "C": Entry("C", repo="/r2", cwd="/r2"),
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=0, entries=entries)
    observed = {
        "A": _obs(
            alive=True, raw="busy", size=100, cwd="/r1", repo="/r1"
        ),  # not grown → stays running
        "B": _obs(halted=True, cwd="/r1", repo="/r1"),
        "C": _obs(halted=True, cwd="/r2", repo="/r2"),
    }
    state, actions = plan(observed, {"B", "C"}, base, NOW, _cfg(), reset_signals=set())
    assert _launch_ids(actions) == ["C"]  # /r1 is busy; only /r2 dispatches
    assert state.entries["A"].state == "running"
    assert state.entries["B"].state == "queued"


def test_alive_halted_head_is_reaped_before_relaunch() -> None:
    observed = {"a": _obs(alive=True, raw="busy", halted=True)}  # stuck live HALTED REPL
    base = QueueState(
        reset_confirmed_at={ACCT: NOW - 1},
        last_launch_at=0,
        entries={"a": Entry("a", repo="/r1", cwd="/r1")},
    )
    _state, actions = plan(observed, {"a"}, base, NOW, _cfg(), reset_signals=set())
    kinds = _kinds(actions)
    assert "reap" in kinds and "launch_resume" in kinds
    assert kinds.index("reap") < kinds.index("launch_resume")  # kill the REPL first
    assert all(a.session_id == "a" for a in actions if a.kind in ("reap", "launch_resume"))


# --------------------------------------------------------------------------- #
# finish signal (transcript progress + idle), done pruning
# --------------------------------------------------------------------------- #
def test_finish_on_progress_then_idle_frees_repo() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="idle", size=200)}  # grew past baseline, now idle
    state, actions = plan(observed, set(), base, NOW, _cfg(), reset_signals=set())
    assert "A" not in state.entries  # done entries are pruned
    assert any(a.kind == "notify" and "finished" in a.detail for a in actions)
    assert _is_drained(state)


def test_parked_after_progress_counts_as_finished() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=False, size=200)}  # progressed then the process exited
    state, _actions = plan(observed, set(), base, NOW, _cfg(), reset_signals=set())
    assert "A" not in state.entries  # done (had progress) → freed


# --------------------------------------------------------------------------- #
# re-halt, timeouts, the bounded requeue/fail ladder
# --------------------------------------------------------------------------- #
def test_rehalt_requeues_and_clears_reset() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", halted=True, size=150)}  # 429'd again, barren
    state, actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    entry = state.entries["A"]
    assert entry.state == "queued"
    assert entry.attempts == 1
    assert entry.retry_not_before == NOW + _BACKOFF_BASE_MS * 2  # fallback: 15min·2^1
    assert not state.reset_confirmed_at  # that account's limit is back → re-gate it
    assert "ensure_reset_wait" in _kinds(actions)
    assert "launch_resume" not in _kinds(actions)


def test_barren_rehalt_backs_off_instead_of_failing() -> None:
    """A rate-limit re-halt can NEVER become terminal `failed` (Codex O1).

    Pre-redesign this scenario (attempts at the cap) permanently stranded the session
    — the 5 live tombstones of 2026-07/08. Now it backs off and stays retryable.
    """
    entries = {
        "A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100, attempts=2)
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", halted=True, size=150)}
    state, actions = plan(
        observed, {"A"}, base, NOW, _cfg(resume_max_attempts=3), reset_signals=set()
    )
    entry = state.entries["A"]
    assert entry.state == "queued"  # never "failed" for a rate-limit re-halt
    assert entry.attempts == 3
    assert entry.retry_not_before == NOW + min(_BACKOFF_CAP_MS, _BACKOFF_BASE_MS * 2**3)
    assert not any(a.kind == "notify" and "failed" in a.detail for a in actions)


def test_productive_rehalt_resets_attempts() -> None:
    """Hours of real work then the NEXT window's limit = a fresh halt, not a failure."""
    entries = {
        "A": Entry(
            "A",
            repo="/r1",
            cwd="/r1",
            state="running",
            baseline_offset=100,
            attempts=2,
            retry_not_before=NOW - 5,
            fail_reason="re-halted on the limit",
        )
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", halted=True, size=900_000, progressed=True)}
    state, actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    entry = state.entries["A"]
    assert entry.state == "queued"
    assert entry.attempts == 0  # full reset — the resume worked
    assert entry.retry_not_before == 0
    assert entry.fail_reason == ""
    assert ACCT not in state.reset_confirmed_at  # the account still re-gates
    assert "launch_resume" not in _kinds(actions)


def test_barren_rehalt_uses_transcripts_reset_hint() -> None:
    """The halting error's OWN reset time (e.g. a weekly Opus cap) wins over fallback."""
    hint = NOW + 7_200_000  # the error says "resets in 2h"
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="running", baseline_offset=100)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(halted=True, size=150, reset_hint=hint)}
    state, _actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    assert state.entries["A"].retry_not_before == hint
    # A hint in the past is useless → the escalating fallback applies instead.
    entries2 = {"B": Entry("B", repo="/r2", cwd="/r2", state="running", baseline_offset=100)}
    base2 = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries2)
    observed2 = {"B": _obs(halted=True, size=150, cwd="/r2", repo="/r2", reset_hint=NOW - 1)}
    state2, _actions2 = plan(observed2, {"B"}, base2, NOW, _cfg(), reset_signals=set())
    assert state2.entries["B"].retry_not_before == NOW + _BACKOFF_BASE_MS * 2


def test_backoff_entry_not_dispatched_until_due() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", retry_not_before=NOW + 60_000)}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=0, entries=entries)
    observed = {"A": _obs(halted=True)}
    _state, actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    assert "launch_resume" not in _kinds(actions)  # its own limit has not reset yet
    _state2, actions2 = plan(observed, {"A"}, base, NOW + 61_000, _cfg(), reset_signals=set())
    assert _launch_ids(actions2) == ["A"]  # due → dispatched


def test_launch_timeout_when_dead_requeues() -> None:
    entries = {
        "A": Entry("A", repo="/r", cwd="/r", state="launching", baseline_offset=100, launched_at=0)
    }
    # Recent last_launch_at closes the stagger gate, so the requeue isn't re-dispatched
    # this same tick — isolating the timeout→requeue transition.
    base = QueueState(
        reset_confirmed_at={ACCT: NOW - 1}, last_launch_at=NOW - 1000, entries=entries
    )
    observed = {"A": _obs(alive=False, size=100)}  # never came up, no progress
    cfg = _cfg(resume_launch_timeout_sec=900)  # NOW - 0 >> 900s
    state, actions = plan(observed, set(), base, NOW, cfg, reset_signals=set())
    assert state.entries["A"].state == "queued"
    assert state.entries["A"].attempts == 1
    assert "launch_resume" not in _kinds(actions)


def test_live_but_slow_turn_is_not_failed() -> None:
    # Guards the bug: a launched resume that is ALIVE but hasn't produced output yet
    # (long tool call / slow probe) must never be reaped/failed by the launch timeout.
    entries = {
        "A": Entry("A", repo="/r", cwd="/r", state="launching", baseline_offset=100, launched_at=0)
    }
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", size=100)}  # alive, no growth yet
    cfg = _cfg(resume_launch_timeout_sec=900)
    state, actions = plan(observed, set(), base, NOW, cfg, reset_signals=set())
    assert state.entries["A"].state == "launching"  # left alone
    assert not any(a.kind == "notify" and "failed" in a.detail for a in actions)


# --------------------------------------------------------------------------- #
# manual-resume adoption (O9), done re-queue, drained helper
# --------------------------------------------------------------------------- #
def test_manual_resume_is_adopted_not_relaunched() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="queued")}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(alive=True, raw="busy", size=50)}  # user resumed it out-of-band
    state, actions = plan(observed, set(), base, NOW, _cfg(), reset_signals=set())
    assert state.entries["A"].state == "running"
    assert "launch_resume" not in _kinds(actions)  # no second claude --resume on a live id


def test_previously_done_session_requeues_when_halted_again() -> None:
    entries = {"A": Entry("A", repo="/r1", cwd="/r1", state="done")}
    base = QueueState(reset_confirmed_at={ACCT: NOW - 1}, entries=entries)
    observed = {"A": _obs(halted=True)}  # hit the limit again in a later window
    state, actions = plan(observed, {"A"}, base, NOW, _cfg(), reset_signals=set())
    # Revived, not stuck done — but NOT dispatched: the re-halt is a fresh halt, so the
    # earlier window's confirmation is stale evidence and the gate re-arms.
    assert state.entries["A"].state == "queued"
    assert ACCT not in state.reset_confirmed_at
    assert "launch_resume" not in _kinds(actions)
    assert "ensure_reset_wait" in _kinds(actions)


# --------------------------------------------------------------------------- #
# stale reset evidence from a previous cycle (the 2026-08-21 premature resume)
# --------------------------------------------------------------------------- #
def test_fresh_halt_invalidates_stale_confirmation() -> None:
    """A confirmation persisted by an EARLIER resume cycle must not release a new halt.

    Live incident 2026-08-21: ``reset_confirmed_at`` survived the drained queue, so
    the next session-limit halt was "resumed" ~2 minutes later — 2h17m before the
    actual reset — burning an attempt against the still-active limit.
    """
    base = QueueState(reset_confirmed_at={ACCT: NOW - 86_400_000})  # yesterday's cycle
    observed = {"a": _obs(halted=True)}
    state, actions = plan(observed, {"a"}, base, NOW, _cfg(), reset_signals=set())
    assert "launch_resume" not in _kinds(actions)
    assert ACCT not in state.reset_confirmed_at  # stale gate torn down
    assert "invalidate_reset" in _kinds(actions)
    assert "ensure_reset_wait" in _kinds(actions)  # re-verify via a fresh detector
    assert state.entries["a"].state == "queued"


def test_fresh_halt_ignores_stale_signal_file() -> None:
    """A leftover signal file from a previous window must not confirm a new halt."""
    observed = {"a": _obs(halted=True)}
    state, actions = plan(observed, {"a"}, QueueState(), NOW, _cfg(), reset_signals={ACCT})
    assert "confirm_reset" not in _kinds(actions)
    assert "launch_resume" not in _kinds(actions)
    assert not state.reset_confirmed_at
    assert "invalidate_reset" in _kinds(actions)
    assert state.entries["a"].state == "queued"


def test_confirmation_after_enqueue_still_dispatches() -> None:
    """The normal two-tick flow keeps working: enqueue, then a LATER tick confirms."""
    observed = {"a": _obs(halted=True)}
    state1, actions1 = plan(observed, {"a"}, QueueState(), NOW, _cfg(), reset_signals=set())
    assert "launch_resume" not in _kinds(actions1)
    state2, actions2 = plan(observed, {"a"}, state1, NOW + 60_000, _cfg(), reset_signals={ACCT})
    assert "confirm_reset" in _kinds(actions2)
    assert _launch_ids(actions2) == ["a"]
    assert state2.entries["a"].state == "launching"


def test_invalidate_reset_unlinks_signal_and_forgets_dead_detector(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    signal = _signal_path(ACCT)
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.write_text("stale", encoding="utf-8")
    state = QueueState(reset_wait_pid={ACCT: 99_999_999})  # long-dead detector pid
    _invalidate_reset(state, ACCT)
    assert not signal.exists()
    assert ACCT not in state.reset_wait_pid


def test_invalidate_reset_leaves_live_detector_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    signal = _signal_path(ACCT)
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.write_text("pending", encoding="utf-8")
    state = QueueState(reset_wait_pid={ACCT: os.getpid()})  # "detector" is alive
    _invalidate_reset(state, ACCT)
    assert signal.exists()  # a live detector's pending work is not clobbered
    assert state.reset_wait_pid == {ACCT: os.getpid()}


def test_resume_log_records_launch(tmp_path: Path, monkeypatch) -> None:
    """Every dispatched restart lands in resume.log with session id + path + account."""
    import command_center.resume as resume_mod

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(resume_mod, "_launch_resume", lambda *a, **k: True)
    store = Store(tmp_path / "s.db")
    store.ensure("abc-123")
    actions = [Action("launch_resume", "abc-123", cwd="/repo/x", account=ACCT)]
    apply_actions(
        actions,
        QueueState(),
        store,
        _StubAdapter(set(), str(tmp_path)),  # type: ignore[arg-type]
        _cfg(),
    )
    text = (tmp_path / "command-center" / "resume.log").read_text(encoding="utf-8")
    assert "launch" in text and "abc-123" in text and "cwd=/repo/x" in text


def test_is_drained() -> None:
    assert _is_drained(QueueState())
    assert _is_drained(QueueState(entries={"x": Entry("x", "/r", "/r", state="failed")}))
    assert not _is_drained(QueueState(entries={"x": Entry("x", "/r", "/r", state="queued")}))
    assert not _is_drained(QueueState(entries={"x": Entry("x", "/r", "/r", state="running")}))
    # A queued entry in a future backoff does NOT keep the watcher alive — it exits
    # and the daemon respawns it once the retry is due (has_work).
    backoff = QueueState(
        entries={"x": Entry("x", "/r", "/r", state="queued", retry_not_before=NOW + 10_000)}
    )
    assert _is_drained(backoff, NOW)
    assert not _is_drained(backoff, NOW + 10_000)


# --------------------------------------------------------------------------- #
# failed-entry maintenance: prune finished tombstones, revive legacy rate-limit
# --------------------------------------------------------------------------- #
def test_reconcile_failed_entries_prunes_and_revives(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    store.ensure("done_s")
    store.update_fields("done_s", done=True)
    store.ensure("open_rate")
    store.ensure("open_timeout")
    state = QueueState(
        entries={
            "done_s": Entry(
                "done_s",
                "/r",
                "/r",
                state="failed",
                fail_reason="re-halted on the limit",
                attempts=3,
            ),
            "ghost": Entry("ghost", "/r", "/r", state="failed", fail_reason="whatever"),
            "open_rate": Entry(
                "open_rate",
                "/r",
                "/r",
                state="failed",
                fail_reason="re-halted on the limit",
                attempts=3,
                retry_not_before=5,
            ),
            "open_timeout": Entry(
                "open_timeout",
                "/r",
                "/r",
                state="failed",
                fail_reason="no resume progress before timeout",
            ),
        }
    )
    rows = reconcile_failed_entries(store, state)
    assert "done_s" not in state.entries  # finished session → tombstone pruned
    assert "ghost" not in state.entries  # session gone from the store → pruned
    revived = state.entries["open_rate"]
    assert revived.state == "queued"  # legacy rate-limit failure → recoverable again
    assert revived.retry_not_before == 0
    assert state.entries["open_timeout"].state == "failed"  # infra fault stays terminal
    assert {r[0] for r in rows} == {"pruned-failed", "revived-legacy-failed"}


def test_has_work_gates_daemon_spawn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert not has_work()  # empty world
    save_state(QueueState(entries={"q": Entry("q", "/r", "/r", state="queued")}))
    assert has_work()  # a due queued entry
    save_state(
        QueueState(
            entries={
                "q": Entry("q", "/r", "/r", state="queued", retry_not_before=now_ms() + 3_600_000)
            }
        )
    )
    assert not has_work()  # backoff not due → no idle watcher respawn
    with Store() as store:
        store.ensure("f")
    save_state(
        QueueState(
            entries={
                "f": Entry(
                    "f", "/r", "/r", state="failed", fail_reason="no resume progress before timeout"
                )
            }
        )
    )
    assert not has_work()  # terminal failure of a still-open session: suppressed, no churn
    with Store() as store:
        store.update_fields("f", done=True)
    assert has_work()  # ...but once the session finishes, the tombstone is prunable
    save_state(
        QueueState(
            entries={
                "g": Entry("g", "/r", "/r", state="failed", fail_reason="re-halted on the limit")
            }
        )
    )
    assert has_work()  # legacy rate-limit failure → revivable


def test_will_auto_resume_honest_about_failed_entries(tmp_path: Path) -> None:
    """Terminal-failed → bare || (no false promise); backoff/legacy keep the ▶."""
    store = Store(tmp_path / "s.db")
    store.ensure("h")
    store.update_fields("h", cwd=str(tmp_path))
    session = store.get("h")
    assert session is not None
    adapter = _StubAdapter(halted_ids={"h"}, cwd=str(tmp_path))
    on = _cfg(resume_halted=True)
    terminal = QueueState(
        entries={"h": Entry("h", "/r", "/r", state="failed", fail_reason="no resume progress")}
    )
    legacy = QueueState(
        entries={"h": Entry("h", "/r", "/r", state="failed", fail_reason="re-halted on the limit")}
    )
    backoff = QueueState(
        entries={"h": Entry("h", "/r", "/r", state="queued", retry_not_before=NOW)}
    )
    assert not will_auto_resume(session, adapter, on, terminal)  # type: ignore[arg-type]
    assert will_auto_resume(session, adapter, on, legacy)  # type: ignore[arg-type]
    assert will_auto_resume(session, adapter, on, backoff)  # type: ignore[arg-type]


def test_dry_run_touches_neither_queue_nor_log(tmp_path: Path, monkeypatch) -> None:
    """`ccc resume-halted --dry-run` must leave resume_queue.json AND resume.log alone."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    with Store() as store:
        store.ensure("dead")
        store.update_fields("dead", done=True)
    save_state(
        QueueState(
            entries={
                "dead": Entry(
                    "dead",
                    "/r",
                    "/r",
                    state="failed",
                    fail_reason="re-halted on the limit",
                    attempts=3,
                )
            }
        )
    )
    queue_before = _state_path().read_bytes()
    assert tick(_cfg(resume_halted=True), dry_run=True)
    assert _state_path().read_bytes() == queue_before
    assert not (tmp_path / "command-center" / "resume.log").exists()


def test_tick_prunes_legacy_failed_done_sessions(tmp_path: Path, monkeypatch) -> None:
    """The PRODUCTION prune path: one real tick clears a finished tombstone + audits it."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    with Store() as store:
        store.ensure("dead")
        store.update_fields("dead", done=True)
    save_state(
        QueueState(
            entries={
                "dead": Entry(
                    "dead",
                    "/r",
                    "/r",
                    state="failed",
                    fail_reason="re-halted on the limit",
                    attempts=3,
                )
            }
        )
    )
    assert tick(_cfg(resume_halted=True), dry_run=False)  # drained after the prune
    assert "dead" not in load_state().entries
    log_text = (tmp_path / "command-center" / "resume.log").read_text(encoding="utf-8")
    assert "pruned-failed" in log_text and "dead" in log_text


# --------------------------------------------------------------------------- #
# state persistence round-trip + candidate finder
# --------------------------------------------------------------------------- #
def test_state_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    state = QueueState(
        reset_confirmed_at={"private": NOW, "work": NOW - 3},
        last_launch_at=NOW - 5,
        reset_wait_pid={"work": 4242},
        entries={
            "a": Entry(
                "a", "/r1", "/r1", state="running", attempts=1, baseline_offset=9, account="work"
            )
        },
    )
    save_state(state)
    loaded = load_state()
    assert loaded.reset_confirmed_at == {"private": NOW, "work": NOW - 3}
    assert loaded.reset_wait_pid == {"work": 4242}
    assert loaded.entries["a"].state == "running"
    assert loaded.entries["a"].baseline_offset == 9
    assert loaded.entries["a"].account == "work"


def test_state_load_migrates_legacy_scalar_gate(tmp_path: Path, monkeypatch) -> None:
    """A pre-multi-account queue file (scalar gate) upgrades in place instead of crashing."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    legacy = {
        "reset_confirmed_at": NOW,  # was a bare int: "the one gate"
        "last_launch_at": NOW - 5,
        "reset_wait_pid": 4242,
        "entries": {"a": {"session_id": "a", "repo": "/r1", "cwd": "/r1", "state": "queued"}},
    }
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = load_state()
    assert loaded.reset_confirmed_at == {ACCT: NOW}  # adopted as the default account's
    assert loaded.reset_wait_pid == {ACCT: 4242}
    assert loaded.entries["a"].account == ACCT  # unstamped entry → default account


def test_repo_of_falls_back_to_cwd_for_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-git-repo"
    plain.mkdir()
    assert repo_of(str(plain)) == str(plain)


class _StubAdapter:
    """Minimal adapter: only what candidates() touches."""

    def __init__(self, halted_ids: set[str], cwd: str) -> None:
        self._halted = halted_ids
        self._cwd = cwd

    def discover(self) -> list[LiveSession]:
        return []

    def is_halted(self, cwd: str, session_id: str) -> bool:
        return session_id in self._halted

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        return Path(self._cwd) / f"{session_id}.jsonl"  # treated as present


def test_candidates_filters(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    cwd = str(tmp_path)
    for sid in ("halted", "done", "draft", "fine"):
        store.ensure(sid)
        store.update_fields(sid, cwd=cwd)
    store.update_fields("done", done=True)
    store.update_fields("draft", draft=True)
    adapter = _StubAdapter(halted_ids={"halted", "done", "draft"}, cwd=cwd)
    found = {c.session_id for c in candidates(store, adapter)}  # type: ignore[arg-type]
    assert found == {"halted"}  # done/draft excluded; "fine" isn't halted


# --------------------------------------------------------------------------- #
# the ||▶ icon gate — it must promise exactly what the watcher would do
# --------------------------------------------------------------------------- #
def test_will_auto_resume_follows_the_config_gate(tmp_path: Path) -> None:
    """The green ▶ appears only when resume_halted is ON — the shipped default is OFF."""
    store = Store(tmp_path / "s.db")
    store.ensure("h")
    store.update_fields("h", cwd=str(tmp_path))
    session = store.get("h")
    assert session is not None
    adapter = _StubAdapter(halted_ids={"h"}, cwd=str(tmp_path))

    off = _cfg(resume_halted=False)  # ccc's shipped default (INERT_DEFAULT_KEYS)
    on = _cfg(resume_halted=True)
    assert not will_auto_resume(session, adapter, off)  # type: ignore[arg-type]
    assert will_auto_resume(session, adapter, on)  # type: ignore[arg-type]


def test_will_auto_resume_refuses_done_and_draft(tmp_path: Path) -> None:
    """Icon eligibility tracks candidates(): a done/draft session is never revived."""
    store = Store(tmp_path / "s.db")
    for sid in ("done", "draft"):
        store.ensure(sid)
        store.update_fields(sid, cwd=str(tmp_path))
    store.update_fields("done", done=True)
    store.update_fields("draft", draft=True)
    adapter = _StubAdapter(halted_ids={"done", "draft"}, cwd=str(tmp_path))
    on = _cfg(resume_halted=True)
    for sid in ("done", "draft"):
        session = store.get(sid)
        assert session is not None
        assert not will_auto_resume(session, adapter, on)  # type: ignore[arg-type]


def test_is_resumable_refuses_a_handed_off_session(tmp_path: Path) -> None:
    """tp#1300: the daemon never revives a session whose work moved to a successor."""
    from command_center.models import Session  # pylint: disable=import-outside-toplevel
    from command_center.resume import is_resumable  # pylint: disable=import-outside-toplevel

    class _Adapter:
        def transcript_path(self, *_a: object) -> Path:
            return tmp_path

    base = {"session_id": "old", "cwd": str(tmp_path), "config_dir": str(tmp_path)}
    handed = Session(**base, handed_off_to="new")  # type: ignore[arg-type]
    assert not is_resumable(handed, _Adapter())  # type: ignore[arg-type]
    assert is_resumable(Session(**base), _Adapter())  # type: ignore[arg-type]
