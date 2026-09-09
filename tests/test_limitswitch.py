"""Tests for the automatic rate-limit failover (:mod:`command_center.limitswitch`).

The decision layer is pure — ``observe`` reads one transcript, ``decide`` maps
(observation, config, quota) to a verdict — so the interesting behaviour is testable with
no processes and no terminals:

* a transient server 429 is NOT a usage limit and must never move a session;
* the halt carries an identity, so the same halt is claimed exactly once even when the
  hook and the daemon backstop observe it together;
* an account is a target only when the allow-list authorizes it AND it is not itself
  rate-limited;
* the reset deadline is parsed once, against the halt record's own timestamp;
* a refusal backs off without consuming the daily success budget;
* the claim keeps :mod:`command_center.resume` off the session — in its planner AND in its
  executor, because the executor's reap kills the process and closes its tab;
* automatic recovery only ever READS the target's trust flag.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from command_center import accounts, config, limitswitch
from command_center.adapters.claude import ClaudeAdapter
from command_center.models import Session, now_ms
from command_center.store import Store

SESSION = "11111111-2222-3333-4444-555555555555"
HALT_TEXT = "You've hit your session limit · resets 6:40pm (Europe/Berlin)"
OVERLOAD_TEXT = (
    "API Error: Server is temporarily limiting requests (not your usage limit) · please retry"
)


def _cfg(**kw: object) -> config.Config:
    cfg = config.Config()
    cfg.auto_switch_on_limit = True
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _write_transcript(home: Path, cwd: str, session_id: str, text: str, uuid: str) -> Path:
    """One transcript whose last main-chain assistant record is an API-error halt."""
    project = home / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": "2026-09-09T13:30:48.440Z",
            "isApiErrorMessage": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _accounts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """Two configured accounts ("private", "work"), both logged in and both trusting cwd."""
    private, work = tmp_path / "private", tmp_path / "work"
    for home in (private, work):
        (home / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "claude_config_dirs", lambda: {"private": private, "work": work})
    monkeypatch.setattr(config, "claude_account_email_map", dict)
    monkeypatch.setattr(accounts, "account_email", lambda _dir: "")
    labels = {str(private): "private", str(work): "work"}
    monkeypatch.setattr(accounts, "account_label", lambda d: labels.get(str(d), ""))
    monkeypatch.setattr(accounts, "account_config_dir", lambda label: str(tmp_path / label))
    return str(private), str(work)


# --------------------------------------------------------------------------- observe


def test_observe_reads_identity_and_reset_from_the_halt_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observation carries the record's uuid + timestamp, not "now"."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    path = _write_transcript(tmp_path / "home", "/r1", SESSION, HALT_TEXT, "halt-uuid-1")
    obs = limitswitch.observe(ClaudeAdapter(), SESSION, "/r1", "/cfg", transcript_path=path)
    assert obs is not None
    assert obs.halt_id == "halt-uuid-1"
    assert obs.usage_limit is True
    # 2026-09-09T13:30:48.440Z — the RECORD's stamp, so re-observing later cannot drift it.
    assert obs.halt_at_ms == 1788960648440
    assert obs.reset_at_ms > obs.halt_at_ms  # "resets 6:40pm" resolved against that stamp


def test_observe_flags_a_transient_server_429_as_not_a_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code reports a shed-load 429 as `rate_limit` too — it must not move seats."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    text = f"{OVERLOAD_TEXT} · You've hit your session limit"
    path = _write_transcript(tmp_path / "home", "/r1", SESSION, text, "halt-uuid-2")
    obs = limitswitch.observe(ClaudeAdapter(), SESSION, "/r1", "/cfg", transcript_path=path)
    assert obs is not None and obs.usage_limit is False


def test_observe_returns_none_when_the_last_assistant_turn_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    path = _write_transcript(tmp_path / "home", "/r1", SESSION, HALT_TEXT, "halt-uuid-3")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "done"}})
            + "\n"
        )
    assert (
        limitswitch.observe(ClaudeAdapter(), SESSION, "/r1", "/cfg", transcript_path=path) is None
    )


# ---------------------------------------------------------------------------- decide


def _obs(source: str, *, usage_limit: bool = True) -> limitswitch.HaltObservation:
    return limitswitch.HaltObservation(
        session_id=SESSION,
        cwd="/r1",
        halt_id="halt-uuid-1",
        halt_at_ms=1788960648440,
        text=HALT_TEXT,
        source_config_dir=source,
        reset_at_ms=1788960648440 + 3_600_000,
        usage_limit=usage_limit,
    )


def test_decide_picks_the_other_account_when_it_has_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, work = _accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(limitswitch, "_quota_rank", lambda label, model, now: (True, 5.0))
    decision = limitswitch.decide(_obs(private), _cfg())
    assert decision.switch and decision.target_label == "work"


def test_decide_refuses_when_every_authorized_account_is_blocked_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, _ = _accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(limitswitch, "_quota_rank", lambda label, model, now: (False, 0.0))
    decision = limitswitch.decide(_obs(private), _cfg())
    assert not decision.switch
    assert "rate-limited too" in decision.reason
    assert decision.backoff_ms == limitswitch.BACKOFF_NO_TARGET_MS


def test_decide_never_moves_on_a_transient_server_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, _ = _accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(limitswitch, "_quota_rank", lambda label, model, now: (True, 5.0))
    decision = limitswitch.decide(_obs(private, usage_limit=False), _cfg())
    assert not decision.switch and "transient" in decision.reason


def test_decide_honours_the_allow_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quota is not authorization: an unlisted transition is refused however free the seat."""
    private, _ = _accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(limitswitch, "_quota_rank", lambda label, model, now: (True, 5.0))
    decision = limitswitch.decide(_obs(private), _cfg(auto_switch_targets=["work>private"]))
    assert not decision.switch and "no authorized target" in decision.reason


def test_decide_stops_at_the_daily_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private, _ = _accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(limitswitch, "_quota_rank", lambda label, model, now: (True, 5.0))
    decision = limitswitch.decide(_obs(private), _cfg(auto_switch_max_per_day=2), successes_today=2)
    assert not decision.switch and "daily cap" in decision.reason


def test_decide_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private, _ = _accounts(monkeypatch, tmp_path)
    cfg = config.Config()  # shipped defaults: the feature is inert
    assert not limitswitch.decide(_obs(private), cfg).switch


# ----------------------------------------------------------------------------- claim


def test_one_halt_is_claimed_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook worker and the daemon backstop race on purpose; only one may act."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        first = store.claim_limit_switch(
            SESSION, halt_id="h1", source="/private", pid=1, pid_start="", now=now_ms(), ttl_ms=1000
        )
        second = store.claim_limit_switch(
            SESSION, halt_id="h1", source="/private", pid=1, pid_start="", now=now_ms(), ttl_ms=1000
        )
        assert first is True and second is False


def test_a_finished_halt_is_never_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_halted` stays true after the relaunch — the halt ID is what stops a re-fire."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.claim_limit_switch(
            SESSION, halt_id="h1", source="/private", pid=1, pid_start="", now=now_ms(), ttl_ms=1000
        )
        store.set_limit_switch_state(SESSION, "started")
        assert not store.claim_limit_switch(
            SESSION, halt_id="h1", source="/private", pid=1, pid_start="", now=now_ms(), ttl_ms=1000
        )
        # A NEW halt on the new seat is a different id and may be claimed.
        assert store.claim_limit_switch(
            SESSION, halt_id="h2", source="/work", pid=2, pid_start="", now=now_ms(), ttl_ms=1000
        )


def test_a_dead_workers_claim_is_taken_over_after_the_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    now = now_ms()
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.claim_limit_switch(
            SESSION,
            halt_id="h1",
            source="/private",
            pid=1,
            pid_start="",
            now=now - 10_000,
            ttl_ms=1000,
        )
        assert store.claim_limit_switch(
            SESSION, halt_id="h2", source="/private", pid=1, pid_start="", now=now, ttl_ms=1000
        )


def test_owns_tracks_the_exact_halt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-SIGTERM re-check: a claim taken over meanwhile must abort the kill."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.claim_limit_switch(
            SESSION, halt_id="h1", source="/private", pid=1, pid_start="", now=now_ms(), ttl_ms=1000
        )
        assert store.limit_switch_owns(SESSION, "h1", ("claimed", "dispatched"))
        assert not store.limit_switch_owns(SESSION, "h2", ("claimed", "dispatched"))
        store.set_limit_switch_state(SESSION, "started")
        assert not store.limit_switch_owns(SESSION, "h1", ("claimed", "dispatched"))


def test_refusals_do_not_consume_the_daily_success_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.set_limit_switch_state(SESSION, "refused", reason="busy", retry_not_before=123)
        row = store.get(SESSION)
        assert row is not None
        assert row.limit_switch_successes == 0
        assert row.limit_switch_retry_not_before == 123
        store.set_limit_switch_state(SESSION, "dispatched", count_success=True, day="2026-09-09")
        row = store.get(SESSION)
        assert row is not None and row.limit_switch_successes == 1


# ------------------------------------------------------------------ arm arbitration


def test_an_automatic_now_switch_stands_down_for_a_pending_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human's armed switch/close must not be silently retargeted by the failover."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    now = now_ms()
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.update_fields(
            SESSION,
            switch_requested_at=now,
            switch_config_dir="/armed",
            switch_prompt="p",
            switch_force=1,
        )
        refused = store.arbitrate_now_switch(
            SESSION, "/work", now=now, ttl_ms=600_000, override=False
        )
        assert "already armed" in refused
        row = store.get(SESSION)
        assert row is not None and row.switch_config_dir == "/armed"  # untouched
        # An explicit human -N supersedes the arm AND clears its force/prompt.
        assert (
            store.arbitrate_now_switch(SESSION, "/work", now=now, ttl_ms=600_000, override=True)
            == ""
        )
        row = store.get(SESSION)
        assert row is not None
        assert (row.switch_config_dir, row.switch_force, row.switch_prompt) == ("/work", 0, "")
        assert row.switch_requested_at == 0


def test_an_automatic_now_switch_stands_down_for_a_pending_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    now = now_ms()
    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.update_fields(SESSION, close_requested_at=now)
        assert "close-after-turn" in store.arbitrate_now_switch(
            SESSION, "/work", now=now, ttl_ms=600_000, override=False
        )


# ------------------------------------------------------------------- resume interlock


def test_hold_active_only_while_a_claim_is_live() -> None:
    now = now_ms()
    live = Session(session_id=SESSION, cwd="/r1")
    live.limit_switch_state = "dispatched"
    live.limit_switch_at = now
    assert limitswitch.hold_active(live, now)
    live.limit_switch_state = "started"  # finished: resume may act again
    assert not limitswitch.hold_active(live, now)
    live.limit_switch_state = "dispatched"
    live.limit_switch_at = now - limitswitch.RESUME_HOLD_MS - 1  # stale claim
    assert not limitswitch.hold_active(live, now)


def test_trust_policy_never_defaults_to_ensure() -> None:
    """Only the literal "ensure" may turn the automatic trust CHECK into a trust WRITE."""
    for raw in ("", "require", "REQUIRE", "yes", "true", "ensur", None, True):
        cfg = config.Config()
        cfg.auto_switch_trust = raw  # type: ignore[assignment]
        assert config.auto_switch_trust_policy(cfg) == "require"
    cfg = config.Config()
    cfg.auto_switch_trust = " Ensure "
    assert config.auto_switch_trust_policy(cfg) == "ensure"


# ------------------------------------------------- resume executor: nothing destructive


def test_resume_executor_refuses_before_reaping_when_trust_was_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trust revoked between planning and execution: no kill, no tab closed, no write.

    The planner schedules ``reap`` BEFORE ``launch_resume`` and the executor runs them in
    that order, so a trust check that only guarded the launch would kill the session and
    close its tab first, then refuse (Codex O16). Asserts the whole promise: no reap, no
    launch, both account files byte-identical, and the refusal recorded on the entry.
    """
    from command_center import resume

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    private = tmp_path / "private"
    (private / "projects").mkdir(parents=True)
    claude_json = private / ".claude.json"
    claude_json.write_text(json.dumps({"projects": {}}), encoding="utf-8")  # /r1 NOT trusted
    before = claude_json.read_bytes()

    reaped: list[str] = []
    launched: list[str] = []
    monkeypatch.setattr(resume, "_reap_fresh", lambda *a, **k: reaped.append("reap"))
    monkeypatch.setattr(resume, "_launch_resume", lambda *a, **k: launched.append("launch"))

    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.update_fields(SESSION, config_dir=str(private))
        state = resume.QueueState()
        state.entries[SESSION] = resume.Entry(
            session_id=SESSION, cwd="/r1", repo="/r1", account="", state="launching"
        )
        actions = [
            resume.Action("reap", SESSION),
            resume.Action("launch_resume", SESSION, cwd="/r1", account=""),
        ]
        resume.apply_actions(actions, state, store, ClaudeAdapter(), config.Config())

    assert reaped == [] and launched == []
    assert claude_json.read_bytes() == before  # read-only: automation never grants trust
    entry = state.entries[SESSION]
    assert entry.state == "failed" and "does not trust" in entry.fail_reason


def test_resume_executor_stands_down_while_a_failover_owns_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queue entry planned earlier must not reap the tab a failover is relaunching into."""
    from command_center import resume

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    reaped: list[str] = []
    monkeypatch.setattr(resume, "_reap_fresh", lambda *a, **k: reaped.append("reap"))
    monkeypatch.setattr(resume, "_launch_resume", lambda *a, **k: True)

    with Store() as store:
        store.ensure(SESSION, cwd="/r1")
        store.claim_limit_switch(
            SESSION,
            halt_id="h1",
            source="/private",
            pid=1,
            pid_start="",
            now=now_ms(),
            ttl_ms=limitswitch.CLAIM_TTL_MS,
        )
        state = resume.QueueState()
        resume.apply_actions(
            [resume.Action("reap", SESSION)], state, store, ClaudeAdapter(), config.Config()
        )
    assert reaped == []


# ------------------------------------------------------------------- hook registration


def test_stop_failure_is_a_registered_hook_event() -> None:
    """`ccc hook stop-failure` must exist in the spec, or argparse rejects it (Codex O11)."""
    from command_center import hookspec, install

    assert ("StopFailure", None, "stop-failure") in hookspec.HOOK_SPEC
    assert "stop-failure" in hookspec.HOOK_EVENTS
    assert "stop-failure" in install.ALL_HOOK_ARGS


def test_stop_failure_hook_only_acts_on_a_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other StopFailure error (auth, overload, bad request) must spawn nothing."""
    from command_center import hooks, spawn

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    spawned: list[list[str]] = []

    def _spawn(args: list[str], **_kw: object) -> bool:
        spawned.append(args)
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", _spawn)
    monkeypatch.setattr(config, "load_config", _cfg)

    base = {"session_id": SESSION, "cwd": "/r1", "transcript_path": "/t.jsonl"}
    for error in ("overloaded", "authentication_failed", "invalid_request", ""):
        hooks.handle_stop_failure({**base, "error": error})
    assert spawned == []

    hooks.handle_stop_failure({**base, "error": "rate_limit"})
    assert spawned and spawned[0][:3] == ["switch-on-limit", "--session", SESSION]


def test_stop_failure_hook_is_inert_while_the_feature_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import hooks, spawn

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    spawned: list[list[str]] = []

    def _spawn(args: list[str], **_kw: object) -> bool:
        spawned.append(args)
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", _spawn)
    monkeypatch.setattr(config, "load_config", config.Config)  # shipped defaults
    hooks.handle_stop_failure({"session_id": SESSION, "cwd": "/r1", "error": "rate_limit"})
    assert spawned == []


def test_the_failover_worker_is_not_spawned_as_an_internal_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CCC_INTERNAL would make `switch-account` refuse — the worker must not carry it."""
    from command_center import hooks, spawn

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    seen: dict[str, object] = {}

    def _spawn(args: list[str], **kw: object) -> bool:
        seen.update(args=args, **kw)
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", _spawn)
    monkeypatch.setattr(config, "load_config", _cfg)
    hooks.handle_stop_failure({"session_id": SESSION, "cwd": "/r1", "error": "rate_limit"})
    assert seen.get("internal") is False


def test_uncollapsed_registry_sees_one_id_live_under_two_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`discover()` collapses a D9 conflict into ONE row; the destructive checks need both.

    This is the evidence `switch-account` refuses on (Codex O4): with only the collapsed
    view, "is this id live under two accounts?" is unanswerable, and an in-session caller
    that takes its billing account from the environment sails past the blanked `config_dir`.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    private, work = tmp_path / "private", tmp_path / "work"
    for home, pid in ((private, os.getpid()), (work, os.getpid())):
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": SESSION,
                    "cwd": "/r1",
                    "kind": "interactive",
                    "entrypoint": "cli",
                    "status": "idle",
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(config, "claude_config_dirs", lambda: {"private": private, "work": work})
    adapter = ClaudeAdapter()
    assert len(adapter.discover_raw(SESSION)) == 2  # both registry files, uncollapsed
    collapsed = [e for e in adapter.discover() if e.session_id == SESSION]
    assert len(collapsed) == 1 and collapsed[0].conflict  # what the old guard could see
