"""Parked prompts (park.py + fire_at plumbing): scheduling, claims, dispatch, markers."""

from __future__ import annotations

import argparse
import io
import sqlite3
import time
from pathlib import Path

import pytest

from command_center import cli, config, daemon, park, usage
from command_center.store import Store

NOW = 1_700_000_000


def _snapshot(
    *,
    five_hour: usage.Window | None = None,
    seven_day: usage.Window | None = None,
    fable_week: usage.Window | None = None,
    oauth_fetched_at: int = NOW,
) -> usage.Usage:
    return usage.Usage(
        captured_at=NOW,
        five_hour=five_hour,
        seven_day=seven_day,
        fable_week=fable_week,
        oauth_fetched_at=oauth_fetched_at,
    )


# ---- fire_time --------------------------------------------------------------


def test_fire_time_is_resets_at_plus_buffer_regardless_of_utilization() -> None:
    snap = _snapshot(five_hour=usage.Window(used_percentage=12.0, resets_at=NOW + 3600))
    assert park.fire_time(snap, "five_hour", NOW, 90) == NOW + 3690


def test_fire_time_none_without_usable_reset() -> None:
    assert park.fire_time(None, "five_hour", NOW) is None  # no snapshot
    snap = _snapshot(five_hour=usage.Window(used_percentage=100.0, resets_at=NOW - 10))
    assert park.fire_time(snap, "five_hour", NOW) is None  # reset already passed
    assert park.fire_time(snap, "seven_day", NOW) is None  # window absent
    assert park.fire_time(snap, "bogus", NOW) is None  # unknown window name


def test_fire_time_selects_only_the_requested_window() -> None:
    snap = _snapshot(
        five_hour=usage.Window(used_percentage=100.0, resets_at=NOW + 1000),
        seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 90_000),
    )
    assert park.fire_time(snap, "seven_day", NOW, 0) == NOW + 90_000


# ---- postpone_until ----------------------------------------------------------


def test_postpone_only_on_fresh_exhaustion_of_the_own_window() -> None:
    snap = _snapshot(seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 5000))
    assert park.postpone_until(snap, "seven_day", NOW, buffer_sec=90) == NOW + 5090


def test_postpone_ignores_other_windows() -> None:
    # An exhausted Fable-weekly must never hold back a five_hour job (debate R2-O1).
    snap = _snapshot(
        five_hour=usage.Window(used_percentage=40.0, resets_at=NOW + 900),
        fable_week=usage.Window(used_percentage=100.0, resets_at=NOW + 500_000),
    )
    assert park.postpone_until(snap, "five_hour", NOW) is None


def test_postpone_requires_fresh_oauth_and_real_exhaustion() -> None:
    stale = _snapshot(
        seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 5000),
        oauth_fetched_at=NOW - 3600,
    )
    assert park.postpone_until(stale, "seven_day", NOW) is None  # stale ⇒ fire as recorded
    fresh_ok = _snapshot(seven_day=usage.Window(used_percentage=99.0, resets_at=NOW + 5000))
    assert park.postpone_until(fresh_ok, "seven_day", NOW) is None  # not exhausted
    assert park.postpone_until(fresh_ok, "", NOW) is None  # no window recorded
    assert park.postpone_until(None, "seven_day", NOW) is None


# ---- format_fire ---------------------------------------------------------------


def test_format_fire_future_now_and_overdue() -> None:
    hhmm = time.strftime("%H:%M", time.localtime(NOW + 2220))
    assert park.format_fire(NOW + 2220, NOW) == f"fires {hhmm} (in 37m)"
    assert park.format_fire(NOW + 30, NOW) == "fires now"
    assert park.format_fire(NOW - park.FIRE_GRACE_SEC, NOW) == "fires now"
    assert park.format_fire(NOW - 300, NOW) == "overdue 5m"
    assert park.format_fire(NOW - (2 * 86400 + 3 * 3600), NOW) == "overdue 2d 3h"


# ---- prompt guards --------------------------------------------------------------


def test_prompt_size_error_caps_argv_budget() -> None:
    assert park.prompt_size_error("fine") is None
    assert "argv budget" in (park.prompt_size_error("x" * (park.MAX_PROMPT_BYTES + 1)) or "")


def test_resolve_prompt_positional_wins_and_empty_stdin_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(prompt="do the thing", clipboard=False)
    assert park._resolve_prompt(args, "hdr") == (  # pylint: disable=protected-access
        "do the thing",
        None,
    )
    empty = io.StringIO("")
    monkeypatch.setattr("sys.stdin", empty)
    text, err = park._resolve_prompt(  # pylint: disable=protected-access
        argparse.Namespace(prompt=None, clipboard=False), "hdr"
    )
    assert text is None and err is not None


def test_pick_editor_falls_back_when_configured_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setenv("EDITOR", "code -w")  # the observed real-world failure
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vim" if name == "vim" else None)
    argv, note = park._pick_editor()  # pylint: disable=protected-access
    assert argv == ["vim"]
    assert note is not None and "code -w" in note
    # Nothing runnable at all → a final error, not a crash.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv, note = park._pick_editor()  # pylint: disable=protected-access
    assert argv is None and note is not None and note.startswith("error:")


def test_pick_editor_uses_configured_editor_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setenv("EDITOR", "code -w")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/code")
    argv, note = park._pick_editor()  # pylint: disable=protected-access
    assert argv == ["code", "-w"] and note is None


# ---- grab mode (the q+p chord panel) --------------------------------------------


def _grab_setup(monkeypatch: pytest.MonkeyPatch, panel_text: str | None) -> list[str]:
    from command_center import parkpanel, peek

    notified: list[str] = []
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: None)
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", lambda: "/tmp")
    monkeypatch.setattr(parkpanel, "capture_prompt", lambda header, initial="": panel_text)
    monkeypatch.setattr(
        "command_center.notify.notify", lambda title, msg, channels: notified.append(title)
    )
    snap = _snapshot(five_hour=usage.Window(used_percentage=100.0, resets_at=NOW + 1200))
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    monkeypatch.setattr(time, "time", lambda: NOW)
    return notified


def test_grab_registers_armed_job_for_frontmost_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    notified = _grab_setup(monkeypatch, "panel prompt\nsecond line")
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.cwd == "/tmp" and job.prompt == "panel prompt\nsecond line"
        assert job.fire_at == NOW + 1200 + park.DEFAULT_BUFFER_SEC
        assert job.fire_window == "five_hour" and job.aim == "panel prompt"
    assert notified == ["⏳ prompt parked"]


def test_grab_cancel_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _grab_setup(monkeypatch, None)
    assert cli.main(["park", "-g"]) == 130
    with Store() as store:
        assert not [s for s in store.list_sessions() if s.draft]


# ---- attach mode (q+p over a LIVE Claude session tab) -----------------------------


def _attach_setup(monkeypatch: pytest.MonkeyPatch, panel_text: str | None) -> list[str]:
    """A live tracked session in the frontmost tab + a panel returning *panel_text*."""
    from types import SimpleNamespace

    from command_center import peek
    from command_center.adapters import claude as claude_adapter

    notified = _grab_setup(monkeypatch, panel_text)
    with Store() as store:
        store.ensure("live-1", cwd="/tmp")
        store.update_fields("live-1", iterm_session_id="w0t0p0:UUID-LIVE", aim="the aim")
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-LIVE")
    monkeypatch.setattr(peek, "_session_for_uuid", lambda store, uuid: store.get("live-1"))

    class _FakeAdapter:
        def discover(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(session_id="live-1", alive=True, kind="interactive")]

    monkeypatch.setattr(claude_adapter, "ClaudeAdapter", _FakeAdapter)
    return notified


def test_grab_attaches_to_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    notified = _attach_setup(monkeypatch, "the real prompt")
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and not row.draft
        assert row.prompt == "the real prompt"
        assert row.fire_at == NOW + 1200 + park.DEFAULT_BUFFER_SEC
        assert row.fire_window == "five_hour"
        assert not [s for s in store.list_sessions() if s.draft]  # NO detached job
    assert notified == ["⏳ prompt parked"]


def test_grab_new_job_flag_skips_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    _attach_setup(monkeypatch, "detached prompt")
    assert cli.main(["park", "-g", "-j"]) == 0
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and row.fire_at == 0 and row.prompt is None
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.prompt == "detached prompt"


def test_grab_attach_now_delivers_into_the_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import terminal

    sent: list[tuple[str, str]] = []
    _attach_setup(monkeypatch, "say hallo")

    def _fake_send(uuid: str, text: str) -> bool:
        sent.append((uuid, text))
        return True

    monkeypatch.setattr(terminal, "send_text_to_session", _fake_send)
    assert cli.main(["park", "-g", "-N"]) == 0
    assert sent == [("w0t0p0:UUID-LIVE", "say hallo")]
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and row.fire_at == 0  # delivered, nothing armed


# ---- daemon delivery of attached prompts ---------------------------------------------


def _attached_row(store: Store, fire_at: int) -> None:
    store.ensure("att-1", cwd="/tmp")
    store.update_fields(
        "att-1",
        prompt="attached prompt",
        fire_at=fire_at,
        fire_window="five_hour",
        iterm_session_id="w0t0p0:UUID-ATT",
    )


def test_deliver_attached_injects_into_live_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from command_center import terminal

    _, notified = _daemon_setup(monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        terminal,
        "send_text_to_session",
        lambda uuid, text: sent.append((uuid, text)) or True,  # type: ignore[func-returns-value]
    )
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now - 10)
        live = {"att-1": SimpleNamespace(alive=True)}
        report = daemon.DaemonReport()
        daemon._deliver_attached_prompts(  # pylint: disable=protected-access
            store, config.load_config(), report, False, live
        )
        assert report.reset_fired == ["att-1"] and sent == [("w0t0p0:UUID-ATT", "attached prompt")]
        row = store.get("att-1")
        assert row is not None and row.fire_at == 0
    assert notified == ["⏳ parked prompt delivered"]


def test_deliver_attached_falls_back_to_resume_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import terminal

    _, notified = _daemon_setup(monkeypatch)
    resumed: list[str] = []
    monkeypatch.setattr(
        terminal,
        "fire_attached_in_new_tab",
        lambda sid: resumed.append(sid) or True,  # type: ignore[func-returns-value]
    )
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now - 10)
        report = daemon.DaemonReport()
        daemon._deliver_attached_prompts(  # pylint: disable=protected-access
            store, config.load_config(), report, False, {}
        )
        assert resumed == ["att-1"]
        row = store.get("att-1")  # lease kept: fire-attached consumes it via claim_fire
        assert row is not None and row.fire_at >= now + park.FIRE_RETRY_SEC - 5
    assert notified == ["⏳ parked prompt resuming in a new tab"]


def test_fire_attached_claims_and_execs_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    import os as os_mod

    execs: list[list[str]] = []
    monkeypatch.setattr(os_mod, "execvp", lambda prog, argv: execs.append(list(argv)))
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now + 900)
    assert cli.main(["fire-attached", "att-1"]) == 0
    assert execs == [["claude", "--resume", "att-1", "attached prompt"]]
    with Store() as store:
        row = store.get("att-1")
        assert row is not None and row.fire_at == 0
    # Second delivery attempt: the claim is gone → refuse, never resume twice.
    assert cli.main(["fire-attached", "att-1"]) == 1
    assert len(execs) == 1


def test_grab_second_park_prefills_the_armed_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import parkpanel

    _attach_setup(monkeypatch, "ignored")
    with Store() as store:  # a prompt is already armed on the session
        store.update_fields(
            "live-1", prompt="old armed prompt", fire_at=NOW + 600, fire_window="five_hour"
        )
    seen_initial: list[str] = []

    def _panel(header: str, initial: str = "") -> str:
        del header
        seen_initial.append(initial)
        return "edited prompt"

    monkeypatch.setattr(parkpanel, "capture_prompt", _panel)
    assert cli.main(["park", "-g"]) == 0
    assert seen_initial == ["old armed prompt"]  # the panel reopened for editing
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and row.prompt == "edited prompt"
        assert row.fire_at == NOW + 1200 + park.DEFAULT_BUFFER_SEC  # re-armed


def test_claim_fire_prints_prompt_once(capsys: pytest.CaptureFixture[str]) -> None:
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now + 600)
    assert cli.main(["claim-fire", "att-1"]) == 0
    assert capsys.readouterr().out.strip() == "attached prompt"
    with Store() as store:
        row = store.get("att-1")
        assert row is not None and row.fire_at == 0  # disarmed: the daemon won't deliver
    assert cli.main(["claim-fire", "att-1"]) == 1  # one-shot


def test_hooks_announce_armed_parked_prompt(capsys: pytest.CaptureFixture[str]) -> None:
    import json as json_mod

    from command_center import hooks

    now = int(time.time())
    with Store() as store:
        _attached_row(store, now + 600)
    assert hooks.handle_user_prompt({"session_id": "att-1", "cwd": "/tmp"}) == 0
    payload = json_mod.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "parked prompt" in context.lower() and "ccc claim-fire att-1" in context
    assert hooks.handle_session_start({"session_id": "att-1", "cwd": "/tmp"}) == 0
    payload = json_mod.loads(capsys.readouterr().out)
    assert "ccc claim-fire att-1" in payload["hookSpecificOutput"]["additionalContext"]
    with Store() as store:  # disarmed → the notice disappears
        store.update_fields("att-1", fire_at=0)
    hooks.handle_user_prompt({"session_id": "att-1", "cwd": "/tmp"})
    leftover = capsys.readouterr().out
    assert "claim-fire" not in leftover


def test_fire_attached_rearms_on_exec_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import os as os_mod

    def _boom(prog: str, argv: list[str]) -> None:
        raise OSError("no claude")

    monkeypatch.setattr(os_mod, "execvp", _boom)
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now - 5)
    assert cli.main(["fire-attached", "att-1"]) == 1
    with Store() as store:
        row = store.get("att-1")
        assert row is not None and row.fire_at > now  # re-armed: the daemon retries


# ---- store: columns, index, claim, summary ------------------------------------


def test_upgrade_adds_fire_columns_and_partial_index(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    # A pre-fire_at DB: the original-schema columns exist (archived was never an
    # ALTER-add), fire_at/fire_window and the partial index do not.
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, "
        "cwd TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT 'claude', "
        "archived INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    with Store(db) as store:
        cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(sessions)")}
        assert {"fire_at", "fire_window"} <= cols
        indexes = {
            row["name"]
            for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_sessions_fire_pending" in indexes
        assert "idx_sessions_armed_fire" not in indexes  # draft-scoped first cut is dropped


def test_create_draft_stamps_fire_fields_and_claim_is_one_shot() -> None:
    with Store() as store:
        store.create_draft("j1", "/tmp", "aim", fire_at=NOW + 100, fire_window="five_hour")
        job = store.get("j1")
        assert job is not None and job.fire_at == NOW + 100 and job.fire_window == "five_hour"
        assert store.claim_draft("j1") is True
        claimed = store.get("j1")
        assert claimed is not None and not claimed.draft and claimed.fire_at == 0
        assert store.claim_draft("j1") is False  # already consumed


def test_claim_draft_exactly_one_winner_across_connections() -> None:
    with Store() as store:
        store.create_draft("j2", "/tmp", "aim")
    first, second = Store(), Store()
    try:
        wins = [first.claim_draft("j2"), second.claim_draft("j2")]
    finally:
        first.close()
        second.close()
    assert wins.count(True) == 1


def test_armed_draft_summary_counts_only_armed_live_drafts() -> None:
    with Store() as store:
        assert store.armed_draft_summary() is None
        store.create_draft("a1", "/tmp", "one", fire_at=NOW + 500, fire_window="five_hour")
        store.create_draft("a2", "/tmp", "two", fire_at=NOW + 100, fire_window="five_hour")
        store.create_draft("a3", "/tmp", "unarmed")
        store.create_draft("a4", "/tmp", "gone", fire_at=NOW + 50, fire_window="five_hour")
        store.update_fields("a4", archived=True)
        assert store.armed_draft_summary() == (NOW + 100, 2)
        # An ATTACHED prompt (fire_at on a live non-draft row) counts too.
        store.ensure("a5", cwd="/tmp")
        store.update_fields("a5", prompt="attached", fire_at=NOW + 10, fire_window="five_hour")
        assert store.armed_draft_summary() == (NOW + 10, 3)


def test_claim_fire_is_one_shot() -> None:
    with Store() as store:
        store.ensure("f1", cwd="/tmp")
        store.update_fields("f1", prompt="p", fire_at=NOW + 10)
        assert store.claim_fire("f1") is True
        row = store.get("f1")
        assert row is not None and row.fire_at == 0
        assert store.claim_fire("f1") is False


# ---- new-job --at-reset ----------------------------------------------------------


def test_new_job_at_reset_arms_from_usage_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snapshot(five_hour=usage.Window(used_percentage=100.0, resets_at=NOW + 1800))
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    monkeypatch.setattr(time, "time", lambda: NOW)
    assert cli.main(["new-job", "-a", "armed job", "-c", "/tmp", "-R"]) == 0
    with Store() as store:
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.fire_at == NOW + 1800 + park.DEFAULT_BUFFER_SEC
        assert job.fire_window == "five_hour"


def test_new_job_at_reset_fails_loudly_without_usable_reset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    assert cli.main(["new-job", "-a", "cannot arm", "-c", "/tmp", "-R"]) == 1
    assert "usable five_hour reset" in capsys.readouterr().err
    with Store() as store:
        assert not [s for s in store.list_sessions() if s.draft]  # nothing half-registered


# ---- start-job --auto ---------------------------------------------------------------


def test_start_job_auto_disarms_on_tripped_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import date, timedelta

    ahead = (date.today() + timedelta(days=3)).isoformat()
    with Store() as store:
        store.create_draft(
            "g1", "/tmp", "guarded", start_date=ahead, fire_at=NOW - 300, fire_window="five_hour"
        )
    assert cli.main(["start-job", "g1", "--auto"]) == 1
    assert "auto-fire blocked" in capsys.readouterr().err
    with Store() as store:
        job = store.get("g1")
        assert job is not None and job.draft and job.fire_at == 0  # disarmed, draft kept


# ---- lifecycle: restore must not re-arm ----------------------------------------------


def test_restore_job_clears_stale_fire_at(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import futuresync

    monkeypatch.setattr(futuresync, "unarchive_file", lambda *a, **k: None)
    with Store() as store:
        store.create_draft("r1", "/tmp", "restored", fire_at=NOW - 999, fire_window="five_hour")
        store.update_fields("r1", archived=True)
    assert cli.main(["restore-job", "r1"]) == 0
    with Store() as store:
        job = store.get("r1")
        assert job is not None and job.draft and not job.archived and job.fire_at == 0


# ---- daemon dispatch -------------------------------------------------------------------


def _daemon_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, bool]], list[str]]:
    launched: list[tuple[str, bool]] = []
    notified: list[str] = []
    from command_center import terminal

    def _fake_launch(sid: str, force: bool = False, auto: bool = False) -> bool:
        del force
        launched.append((sid, auto))
        return True

    monkeypatch.setattr(terminal, "start_job_in_new_tab", _fake_launch)
    monkeypatch.setattr(daemon, "notify", lambda title, msg, channels: notified.append(title))
    monkeypatch.setattr(usage, "read_usage", lambda label: None)  # no postpone data
    return launched, notified


def test_fire_reset_jobs_dispatches_due_with_rearm_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched, notified = _daemon_setup(monkeypatch)
    now = int(time.time())
    with Store() as store:
        store.create_draft(
            "d1", "/tmp", "due", fire_at=now - park.FIRE_GRACE_SEC - 5, fire_window="five_hour"
        )
        store.create_draft("d2", "/tmp", "graced", fire_at=now - 5, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=False
        )
        assert report.reset_fired == ["d1"]
        assert launched == [("d1", True)]
        assert notified == ["⏳ parked prompt fired"]
        fired = store.get("d1")
        # Re-arm-forward lease BEFORE dispatch: a crash/tab-that-never-ran retries later.
        assert fired is not None and fired.fire_at >= now + park.FIRE_RETRY_SEC - 5
        graced = store.get("d2")  # within the grace window — the foreground waiter's slot
        assert graced is not None and graced.fire_at == now - 5


def test_fire_reset_jobs_postpones_on_fresh_own_window_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched, _ = _daemon_setup(monkeypatch)
    now = int(time.time())
    snap = usage.Usage(
        captured_at=now,
        five_hour=usage.Window(used_percentage=100.0, resets_at=now + 4000),
        seven_day=None,
        oauth_fetched_at=now,
    )
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    with Store() as store:
        store.create_draft("p1", "/tmp", "held", fire_at=now - 500, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=False
        )
        assert report.reset_postponed == ["p1"] and not launched
        held = store.get("p1")
        assert held is not None and held.fire_at == now + 4000 + park.DEFAULT_BUFFER_SEC


def test_fire_reset_jobs_dry_run_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    launched, _ = _daemon_setup(monkeypatch)
    now = int(time.time())
    with Store() as store:
        store.create_draft("d3", "/tmp", "dry", fire_at=now - 500, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=True
        )
        assert report.reset_fired == ["d3"] and not launched
        row = store.get("d3")
        assert row is not None and row.fire_at == now - 500
