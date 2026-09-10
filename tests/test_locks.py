"""Cross-session file locks: store mechanics + the Pre/PostToolUse hooks + `ccc handoff`."""

from __future__ import annotations

import json
import re
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest

from command_center import cli, config, gitcommit, hooks
from command_center.models import now_ms
from command_center.store import Store

TTL = 30 * 60 * 1000  # ms


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """A store under a temp CLAUDE_HOME — hooks/CLI open their own Store() at the same path."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    s = Store()
    try:
        yield s
    finally:
        s.close()


# ---- store mechanics ----------------------------------------------------
def test_acquire_contended_then_released(store: Store) -> None:
    """The AIM's reproducing case: A holds F → B blocked → A releases → B acquires."""
    store.ensure("A")
    store.ensure("B")
    live = {"A", "B"}
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, live, TTL) is None  # A takes it
    assert store.acquire_file_lock("B", "/f.py", t, live, TTL) == "A"  # B blocked by live A
    assert store.acquire_file_lock("A", "/f.py", t + 1, live, TTL) is None  # A re-acquire refreshes
    assert store.release_file_lock("A", "/f.py") is True
    assert store.acquire_file_lock("B", "/f.py", t + 2, live, TTL) is None  # now B may take it


def test_dead_holder_is_reclaimed(store: Store) -> None:
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL) is None
    assert store.acquire_file_lock("B", "/f.py", t, {"B"}, TTL) is None  # A not live → reclaimed


def test_stale_lock_is_reclaimed(store: Store) -> None:
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL) is None
    # Past the TTL, A is reclaimable even though it is still live.
    assert store.acquire_file_lock("B", "/f.py", t + TTL + 1, {"A", "B"}, TTL) is None


def test_waiters_and_release_all(store: Store) -> None:
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    store.add_waiter("B", "/f.py", t)
    store.add_waiter("B", "/f.py", t)  # idempotent
    waiters = store.waiters_on_my_locks("A")
    assert [(w.session_id, w.file_path) for w in waiters] == [("B", "/f.py")]
    assert store.release_all_file_locks("A") == 1
    assert store.waiters_on_my_locks("A") == []  # A holds nothing now


def test_list_file_locks_filters_invalid(store: Store) -> None:
    store.ensure("A")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A"}, TTL)
    assert [lock.file_path for lock in store.list_file_locks({"A"}, TTL, t)] == ["/f.py"]
    assert store.list_file_locks(set(), TTL, t) == []  # holder not live
    assert store.list_file_locks({"A"}, TTL, t + TTL + 1) == []  # stale


# ---- the Stop-time protection lease -------------------------------------
def test_unprotected_lock_keeps_the_liveness_ttl_rule(store: Store) -> None:
    """Branch 4: with no lease (``protected_until == 0``) the old rule decides, unchanged."""
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    assert store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL) is None
    assert store.protection_deadline("/f.py") == 0  # a plain acquire leases nothing
    assert store.acquire_file_lock("B", "/f.py", t, {"A", "B"}, TTL) == "A"  # live + fresh → deny
    assert store.acquire_file_lock("B", "/f.py", t, {"B"}, TTL) is None  # dead holder → reclaim


def test_protected_lock_denies_a_dead_and_stale_holder(store: Store) -> None:
    """Branch 2: a lease outranks BOTH liveness and the TTL — the files may be mid-commit.

    A last-edit-then-long-turn is what puts a lease past the TTL: the row was refreshed at
    *t*, the turn ended ~TTL later, and the Stop hook leased it 180 s from THERE.
    """
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    stop = t + TTL - 1000  # the turn ends just before the row goes TTL-stale
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    assert store.protect_locks("A", stop + 180_000) == 1  # A's Stop hook leased it
    assert store.protection_deadline("/f.py") == stop + 180_000
    # A is neither live nor fresh, yet B is refused for as long as the lease runs.
    assert store.acquire_file_lock("B", "/f.py", t + TTL + 1, {"B"}, TTL) == "A"
    assert store.acquire_file_lock("B", "/f.py", stop + 179_999, {"A", "B"}, TTL) == "A"
    # …and the moment it lapses, the same call reclaims it.
    assert store.acquire_file_lock("B", "/f.py", stop + 180_000, {"B"}, TTL) is None


def test_protected_lock_refreshed_29_minutes_ago_still_denies(store: Store) -> None:
    """The tp#225 case: inside the 1800 s TTL, future lease → DENY however live the holder."""
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    stop = t + 29 * 60 * 1000  # the turn ended 29 min after the last edit — inside the TTL
    store.protect_locks("A", stop + 180_000)
    now = stop + 1000
    assert store.acquire_file_lock("B", "/f.py", now, {"A", "B"}, TTL) == "A"
    assert store.acquire_file_lock("B", "/f.py", now, {"B"}, TTL) == "A"  # dead holder: still deny


def test_expired_protection_is_reclaimed_from_a_live_fresh_holder(store: Store) -> None:
    """Branch 3: an EXPIRED lease reclaims regardless of liveness and TTL.

    Load-bearing: without it a leased row would fall back to the 1800 s ``file_lock_ttl_sec``
    instead of the ~180 s lease, so a finished turn would strand the file for half an hour.
    """
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    store.protect_locks("A", t + 180_000)
    assert store.acquire_file_lock("B", "/f.py", t + 180_000, {"A", "B"}, TTL) is None  # at the ms
    assert store.protection_deadline("/f.py") == 0  # B's acquire cleared the stamp


def test_holder_reacquire_clears_protection(store: Store) -> None:
    """Branch 1: the holder editing again is a NEW turn, so its own lease is void."""
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    store.protect_locks("A", t + 180_000)
    assert store.acquire_file_lock("A", "/f.py", t + 10, {"A", "B"}, TTL) is None
    assert store.protection_deadline("/f.py") == 0
    # …and the ordinary rule is back: B may reclaim it once A goes away.
    assert store.acquire_file_lock("B", "/f.py", t + 20, {"B"}, TTL) is None


def test_protect_locks_is_monotonic_and_counts_rows(store: Store) -> None:
    """A second Stop in the same turn may only EXTEND the window, never shorten it."""
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A"}, TTL)
    store.acquire_file_lock("A", "/g.py", t, {"A"}, TTL)
    store.acquire_file_lock("B", "/h.py", t, {"B"}, TTL)
    assert store.protect_locks("A", t + 180_000) == 2  # only A's two rows
    assert store.protect_locks("A", t + 60_000) == 2  # smaller deadline: counted, not applied
    assert store.protection_deadline("/f.py") == t + 180_000
    assert store.protect_locks("A", t + 300_000) == 2  # larger deadline: extends
    assert store.protection_deadline("/g.py") == t + 300_000
    assert store.protection_deadline("/h.py") == 0  # B's row untouched
    assert store.protection_deadline("/nope.py") == 0  # no row at all


def test_protect_locks_clears_the_sessions_own_waits(store: Store) -> None:
    """As ``release_all_file_locks`` did: our pending waits go, waiters on US stay."""
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A", "B"}, TTL)
    store.add_waiter("B", "/f.py", t)  # B queued on A's file
    store.acquire_file_lock("B", "/g.py", t, {"A", "B"}, TTL)
    store.add_waiter("A", "/g.py", t)  # A queued on B's file
    store.protect_locks("A", t + 180_000)
    assert [w.session_id for w in store.waiters_on_my_locks("B")] == []  # A's own wait dropped
    assert [w.session_id for w in store.waiters_on_my_locks("A")] == ["B"]  # B still queued on A


def test_release_unprotected_file_locks_keeps_the_leased_rows(store: Store) -> None:
    store.ensure("A")
    t = 1_000_000
    store.acquire_file_lock("A", "/leased.py", t, {"A"}, TTL)
    store.acquire_file_lock("A", "/plain.py", t, {"A"}, TTL)
    store.protect_locks("A", t + 180_000)
    store.acquire_file_lock("A", "/plain.py", t + 1, {"A"}, TTL)  # a later edit voids ITS lease
    assert store.release_unprotected_file_locks("A", t + 2) == 1  # only /plain.py went
    assert [lk.file_path for lk in store.list_file_locks({"A"}, TTL, t + 2)] == ["/leased.py"]
    assert store.release_unprotected_file_locks("A", t + 180_000) == 1  # lease over → it goes too
    assert store.list_file_locks({"A"}, TTL, t + 180_000) == []


def test_list_file_locks_surfaces_a_dead_stale_but_leased_row(store: Store) -> None:
    """Such a row still DENIES an edit, so `ccc locks` may never report "no lock"."""
    store.ensure("A")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A"}, TTL)
    stop = t + TTL - 1000  # the turn ended just before the row went TTL-stale
    store.protect_locks("A", stop + 180_000)
    later = t + TTL + 1  # holder dead AND past the TTL, but still leased
    locks = store.list_file_locks(set(), TTL, later)
    assert [(lk.file_path, lk.protected_until) for lk in locks] == [("/f.py", stop + 180_000)]
    assert store.list_file_locks(set(), TTL, stop + 180_001) == []  # lease over → off the list


def test_delete_erases_a_protected_lock(store: Store) -> None:
    """The documented ``ON DELETE CASCADE`` exception: an operator's delete outranks a lease."""
    store.ensure("A")
    store.ensure("B")
    t = 1_000_000
    store.acquire_file_lock("A", "/f.py", t, {"A"}, TTL)
    store.acquire_file_lock("B", "/g.py", t, {"B"}, TTL)
    store.protect_locks("A", t + 180_000)
    store.protect_locks("B", t + 180_000)
    store.delete("A")
    assert store.protection_deadline("/f.py") == 0  # row gone with the session
    assert store.acquire_file_lock("B", "/f.py", t + 1, {"B"}, TTL) is None  # freely acquirable
    assert store.delete_many(["B"]) == 1
    assert store.list_file_locks({"A", "B"}, TTL, t + 1) == []


def test_stop_barrier_wait_sec_is_clamped_at_load(store: Store) -> None:
    """A hand-edited out-of-range lease is clamped into 0..900 silently (hook hot path)."""
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop_barrier_wait_sec = 10000\n", encoding="utf-8")
    config.invalidate_config_cache()
    assert config.load_config().stop_barrier_wait_sec == 900
    path.write_text("stop_barrier_wait_sec = -5\n", encoding="utf-8")
    config.invalidate_config_cache()
    assert config.load_config().stop_barrier_wait_sec == 0  # 0 = lease disabled, not negative
    path.write_text("stop_barrier_wait_sec = 240\n", encoding="utf-8")
    config.invalidate_config_cache()
    assert config.load_config().stop_barrier_wait_sec == 240  # in range: untouched


# ---- PreToolUse hook ----------------------------------------------------
def test_pre_tool_use_denies_when_held(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("A")
    store.ensure("B")
    monkeypatch.setattr(hooks, "_live_ids", lambda: {"A", "B"})
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A", "B"}, TTL)
    rc = hooks.handle_pre_tool_use(
        {
            "session_id": "B",
            "cwd": "/repo",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/f.py"},
        }
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "/repo/f.py" in decision["permissionDecisionReason"]
    assert [w.session_id for w in store.waiters_on_my_locks("A")] == ["B"]  # B queued


def test_pre_tool_use_deny_names_the_bounded_wait_when_leased(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lease frees at a KNOWN time, so the denial says how long instead of "shortly"."""
    store.ensure("A")
    store.ensure("B")
    monkeypatch.setattr(hooks, "_live_ids", lambda: set())  # A's process is already gone
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A"}, TTL)
    store.protect_locks("A", now_ms() + 60_000)  # A's Stop hook leased it for 60 s
    rc = hooks.handle_pre_tool_use(
        {
            "session_id": "B",
            "cwd": "/repo",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/f.py"},
        }
    )
    assert rc == 0
    reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "/repo/f.py" in reason
    assert "committed" in reason  # WHY it is held: the Stop chain may still be committing
    seconds = re.search(r"about (\d+) s", reason)
    assert seconds is not None and 55 <= int(seconds.group(1)) <= 60
    assert [w.session_id for w in store.waiters_on_my_locks("A")] == ["B"]  # B still queued


def test_pre_tool_use_deny_stays_open_ended_for_a_live_holder(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mid-turn there is no deadline to name — that denial keeps its original wording."""
    store.ensure("A")
    store.ensure("B")
    monkeypatch.setattr(hooks, "_live_ids", lambda: {"A", "B"})
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A", "B"}, TTL)
    hooks.handle_pre_tool_use(
        {
            "session_id": "B",
            "cwd": "/repo",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/f.py"},
        }
    )
    reason = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "being edited by another live Claude Code session" in reason
    assert "about" not in reason


def test_pre_tool_use_allows_when_free(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("B")
    monkeypatch.setattr(hooks, "_live_ids", lambda: {"B"})
    rc = hooks.handle_pre_tool_use(
        {
            "session_id": "B",
            "cwd": "/repo",
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/g.py"},
        }
    )
    assert rc == 0
    assert capsys.readouterr().out == ""  # no decision → the edit proceeds
    assert [lk.session_id for lk in store.list_file_locks({"B"}, TTL, now_ms())] == [
        "B"
    ]  # B holds it


def test_pre_tool_use_fails_open_when_disabled(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hooks.config, "load_config", lambda: config.Config(file_lock_enabled=False))
    rc = hooks.handle_pre_tool_use(
        {"session_id": "B", "tool_name": "Edit", "tool_input": {"file_path": "/repo/f.py"}}
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert store.list_file_locks({"B"}, TTL, now_ms()) == []  # nothing acquired


# ---- PostToolUse hook (eager-handoff nudge) -----------------------------
def test_post_tool_edit_nudges_handoff(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    store.ensure("A")
    store.ensure("B")
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A", "B"}, TTL)
    store.add_waiter("B", "/repo/f.py", now_ms())
    rc = hooks.handle_post_tool_use(
        {"session_id": "A", "tool_name": "Edit", "tool_input": {"file_path": "/repo/f.py"}}
    )
    assert rc == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "ccc handoff /repo/f.py" in ctx


# ---- ccc handoff --------------------------------------------------------
def test_handoff_commits_then_releases(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("A")
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A"}, TTL)
    monkeypatch.setattr(gitcommit, "commit_and_push", lambda repo, paths, msg, **k: (True, "ok"))
    rc = cli.cmd_handoff(Namespace(file="/repo/f.py", message="", session="A"))
    assert rc == 0
    assert store.list_file_locks({"A"}, TTL, now_ms()) == []  # released after commit


def test_handoff_keeps_lock_when_commit_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.ensure("A")
    store.acquire_file_lock("A", "/repo/f.py", now_ms(), {"A"}, TTL)
    monkeypatch.setattr(gitcommit, "commit_and_push", lambda repo, paths, msg, **k: (False, "boom"))
    rc = cli.cmd_handoff(Namespace(file="/repo/f.py", message="m", session="A"))
    assert rc == 1
    assert [lk.session_id for lk in store.list_file_locks({"A"}, TTL, now_ms())] == ["A"]  # kept
