"""Tests for the daemon's pure decision logic and the launchd plist."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from command_center import config, launchd
from command_center.daemon import (
    DaemonReport,
    _alert_reason,
    _backfill_versions,
    _label,
    _refresh_claude_usage,
    _refresh_codex_usage,
    _refresh_copilot_usage,
    run_once,
)
from command_center.launchd import plist_content
from command_center.models import LiveSession, Session, Status, now_ms
from command_center.store import Store

DAY_MS = 86_400_000


def _write_transcript(home: Path, cwd: str, session_id: str) -> None:
    """Give *session_id* a real (minimal) transcript under *home*'s projects dir.

    A session the daemon scores / short-aims genuinely has a transcript on disk; without
    one it is a *dead-launched orphan* (``core.orphan_launched_ids``) and the prune pass
    reaps it before scoring — so tests exercising the backfill passes must materialise it.
    """
    path = home / "projects" / cwd.replace("/", "-") / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "last-prompt"}) + "\n", encoding="utf-8")


class _VersionStub:
    """Minimal adapter returning a fixed Claude Code version for any session."""

    name = "claude"

    def __init__(self, version: str | None) -> None:
        self._version = version

    def discover(self) -> list[LiveSession]:
        return []

    def claude_version(self, cwd: str, session_id: str) -> str | None:
        return self._version


def test_backfill_versions_fills_only_missing_non_draft(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    store.ensure("parked")  # version None → gets filled
    store.ensure("kept")
    store.update_fields("kept", version="2.1.100")  # already set → left as-is
    store.ensure("draft")
    store.update_fields("draft", draft=True)  # future job, no transcript → skipped

    # Dry-run is a no-op.
    _backfill_versions(store, _VersionStub("2.1.193"), dry_run=True)  # type: ignore[arg-type]
    assert store.get("parked").version is None  # type: ignore[union-attr]

    _backfill_versions(store, _VersionStub("2.1.193"), dry_run=False)  # type: ignore[arg-type]
    assert store.get("parked").version == "2.1.193"  # type: ignore[union-attr]
    assert store.get("kept").version == "2.1.100"  # type: ignore[union-attr]  # not clobbered
    assert store.get("draft").version is None  # type: ignore[union-attr]  # drafts skipped
    store.close()


def test_label() -> None:
    assert _label(Session("s", name="My Session")) == "My Session"
    assert _label(Session("s", cwd="/Users/a/b/garten")) == "garten"
    assert _label(Session("abcdef1234", cwd="")) == "abcdef12"


def test_alert_reason_deadline() -> None:
    today = date(2026, 6, 23)
    cfg = config.Config(deadline_warn_days=2, stale_days=7)
    assert (_alert_reason(Session("s", deadline="2026-06-20"), cfg, today) or "").startswith(
        "overdue"
    )
    assert "due" in (_alert_reason(Session("s", deadline="2026-06-24"), cfg, today) or "")
    # Far-future deadline, never touched -> nothing to flag.
    assert _alert_reason(Session("s", deadline="2026-12-31"), cfg, today) is None


def test_alert_reason_stale() -> None:
    cfg = config.Config(stale_days=7)
    today = date.today()
    old = Session("s", last_response_at=now_ms() - 8 * DAY_MS)
    reason = _alert_reason(old, cfg, today)
    assert reason is not None and "parked" in reason
    recent = Session("s", last_response_at=now_ms() - 1 * DAY_MS)
    assert _alert_reason(recent, cfg, today) is None


def test_plist_content() -> None:
    plist = plist_content("/Users/x/.local/bin/ccc", 300, Path("/tmp/cc"))
    assert "<integer>300</integer>" in plist
    assert "<string>/Users/x/.local/bin/ccc</string>" in plist
    assert "RunAtLoad" in plist
    assert "/tmp/cc/daemon.log" in plist


def test_state_badge() -> None:
    assert launchd.state_badge(True) == "✅ (running)"
    assert launchd.state_badge(False) == "❌ (not running)"


def test_is_loaded_reads_launchctl_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loaded ⇒ `launchctl list <label>` exits 0; not loaded ⇒ non-zero. A periodic
    # job listed with PID `-` is still loaded (exit 0), so is_loaded() is True.
    monkeypatch.setattr(launchd.shutil, "which", lambda _name: "/bin/launchctl")

    class _Result:
        def __init__(self, code: int) -> None:
            self.returncode = code

    monkeypatch.setattr(launchd.subprocess, "run", lambda *a, **k: _Result(0))
    assert launchd.is_loaded() is True
    monkeypatch.setattr(launchd.subprocess, "run", lambda *a, **k: _Result(1))
    assert launchd.is_loaded() is False


def test_is_loaded_false_without_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off macOS (no launchctl on PATH) is_loaded() is False without shelling out.
    monkeypatch.setattr(launchd.shutil, "which", lambda _name: None)
    assert launchd.is_loaded() is False


def test_report_empty() -> None:
    assert DaemonReport().is_empty()
    assert not DaemonReport(reaped=["x"]).is_empty()
    assert not DaemonReport(pruned=["x"]).is_empty()
    # A pure-housekeeping pass stays "empty" so it is not reported as activity.
    assert DaemonReport(temps_swept=3).is_empty()


def test_run_once_sweeps_temps_and_dry_run_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan sweep runs on a real pass and is suppressed under --dry-run."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    calls: list[str] = []

    from command_center import usage as usage_mod

    def _fake_sweep(*_args: object, **_kwargs: object) -> int:
        calls.append("swept")
        return 7

    monkeypatch.setattr(usage_mod, "sweep_stale_temps", _fake_sweep)

    assert (
        run_once(
            dry_run=True, do_reap=False, do_summary=False, do_progress=False, do_alerts=False
        ).temps_swept
        == 0
    )
    assert calls == []
    assert (
        run_once(
            dry_run=False, do_reap=False, do_summary=False, do_progress=False, do_alerts=False
        ).temps_swept
        == 7
    )
    assert calls == ["swept"]


def test_daemon_prunes_headless_junk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A daemon pass self-heals: it deletes contentless leftover rows (headless junk a
    # pre-fix ccc process reconciled in) while sparing the live session.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "111.json").write_text(
        json.dumps(
            {"pid": os.getpid(), "sessionId": "live", "cwd": "/Users/x/repo", "entrypoint": "cli"}
        ),
        encoding="utf-8",
    )
    with Store() as store:
        store.ensure("live", cwd="/Users/x/repo")
        store.ensure("junk", cwd="/")  # contentless leftover, not in the live registry

    report = run_once(do_reap=False, do_summary=False, do_progress=False, do_alerts=False)
    assert "junk" in report.pruned
    assert "live" not in report.pruned

    with Store() as store:
        assert {s.session_id for s in store.list_sessions()} == {"live"}


def test_daemon_backfills_unscored_aim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An AIM written straight to the store (bypassing set_aim) stays at the -1
    # sentinel; a daemon pass self-heals by scoring it. The LLM refine is stubbed
    # so the test never spawns a real subprocess.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    import command_center.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "spawn_ccc", lambda args: True)
    with Store() as store:
        store.ensure("s1", cwd="/repo")
        store.update_fields("s1", aim="ship the usage panel with passing tests", aim_score=-1)
    _write_transcript(tmp_path, "/repo", "s1")  # a real (scorable) session, not a dead orphan

    report = run_once(do_reap=False, do_summary=False, do_progress=False, do_alerts=False)
    assert "s1" in report.scored

    with Store() as store:
        got = store.get("s1")
        assert got is not None and got.aim_score >= 0


def test_daemon_backfills_missing_short_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A session with an AIM but no short_aim gets a detached `ccc short-aim` spawned; one that
    # already has a label is left alone. The spawn is stubbed so no real codex subprocess runs.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    # short_aim now defaults OFF (fresh-install INERT contract); this test exercises the
    # backfill feature, so opt in explicitly via the on-disk config.
    (tmp_path / "command-center").mkdir(parents=True, exist_ok=True)
    (tmp_path / "command-center" / "config.toml").write_text("short_aim = true\n", encoding="utf-8")
    import command_center.spawn as spawn_mod

    spawned: list[list[str]] = []

    def fake_spawn(args: list[str]) -> bool:
        spawned.append(args)
        return True

    monkeypatch.setattr(spawn_mod, "spawn_ccc", fake_spawn)
    with Store() as store:
        store.ensure("needs", cwd="/repo")
        store.update_fields("needs", aim="the usage panel ships", aim_score=70)
        store.ensure("has", cwd="/repo")
        store.update_fields("has", aim="the login bug is fixed", aim_score=70)
        store.set_short_aim("has", "fix login bug")
    for sid in ("needs", "has"):  # real (labelled) sessions, not dead-launched orphans
        _write_transcript(tmp_path, "/repo", sid)

    report = run_once(do_reap=False, do_summary=False, do_progress=False, do_alerts=False)
    assert "needs" in report.short_aimed
    assert "has" not in report.short_aimed  # already labelled -> skipped (idempotent)
    assert ["short-aim", "--session", "needs"] in spawned
    assert ["short-aim", "--session", "has"] not in spawned

    # A labelled CURRENT aim whose revision (1) lost its label (`set-aim --first` drops the
    # stale one) is pending again: revision (1) is what the `/aim` column renders, and the
    # same generator run backfills it.
    with Store() as store:
        store.set_aim("has", "the login bug is fixed for good")  # revision (2)
        store.set_short_aim("has", "fix login bug")  # lands on revision (2) only
        store.set_first_short_aim("has", None)
    report = run_once(do_reap=False, do_summary=False, do_progress=False, do_alerts=False)
    assert "has" in report.short_aimed


def test_refresh_copilot_usage_tightens_throttle_while_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cache aged between the active (300s) and idle (900s) throttles is FRESH when idle
    # but STALE while a job works — so the adaptive throttle only fetches in the latter case.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center import usage

    cfg = config.load_config()
    cfg.copilot_usage = True
    cfg.copilot_usage_refresh_sec = 900
    cfg.copilot_usage_refresh_active_sec = 300

    snap = usage.CopilotUsage(0, 2026, 6, "AI Credits", "AI credits", 1.0, 0.01, 0.0)
    usage._write_copilot_usage(snap)
    aged = time.time() - 500  # 500s old: fresh under 900, stale under 300
    os.utime(usage._copilot_usage_path(), (aged, aged))

    fetches: list[int] = []

    def _fake_fetch(*_a: object, **_k: object) -> usage.CopilotUsage:
        fetches.append(1)
        return snap

    monkeypatch.setattr(usage, "fetch_copilot_usage", _fake_fetch)

    with Store() as store:
        store.ensure("s1", cwd="/repo")

        # Idle session -> idle throttle (900) -> 500s cache is fresh -> no fetch.
        store.update_fields("s1", status=Status.IDLE.value)
        _refresh_copilot_usage(store, cfg, DaemonReport(), dry_run=False)
        assert fetches == []

        # Working session -> active throttle (300) -> 500s cache is stale -> fetch fires.
        store.update_fields("s1", status=Status.WORKING.value)
        report = DaemonReport()
        _refresh_copilot_usage(store, cfg, report, dry_run=False)
        assert fetches == [1]
        assert report.copilot_refreshed is True


def test_refresh_claude_usage_fetches_each_stale_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per configured account, a stale snapshot triggers exactly one OAuth fetch."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center import usage

    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": config.claude_home(), "work": tmp_path / "work"},
    )
    cfg = config.load_config()
    cfg.claude_usage = True
    cfg.claude_usage_refresh_sec = 600
    cfg.claude_usage_refresh_active_sec = 200

    fetched: list[str] = []

    def _fake_fetch(account: str, now: int | None = None) -> usage.Usage:
        fetched.append(account)
        return usage.Usage(captured_at=0, five_hour=usage.Window(3.0, 1), seven_day=None)

    monkeypatch.setattr(usage, "fetch_claude_usage", _fake_fetch)
    # Both accounts start with no cache → both stale.
    monkeypatch.setattr(usage, "claude_usage_stale", lambda *_a, **_k: True)

    with Store() as store:
        store.ensure("s1", cwd="/repo")
        report = DaemonReport()
        _refresh_claude_usage(store, cfg, report, dry_run=False)
        assert sorted(fetched) == ["private", "work"]
        assert report.claude_refreshed is True

    # Kill-switch off → no fetch.
    fetched.clear()
    cfg.claude_usage = False
    with Store() as store:
        _refresh_claude_usage(store, cfg, DaemonReport(), dry_run=False)
    assert fetched == []


def test_refresh_codex_usage_fetches_each_stale_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per configured CODEX_HOME, a stale cache triggers exactly one live fetch."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    from command_center import usage

    homes = {"default": tmp_path / "codex", "private": tmp_path / "codex-private"}
    monkeypatch.setattr(config, "codex_homes", lambda: dict(homes))
    cfg = config.load_config()
    cfg.codex_usage = True
    cfg.codex_usage_refresh_sec = 600
    cfg.codex_usage_refresh_active_sec = 200

    fetched: list[Path] = []

    def _fake_fetch(home: Path, now: int | None = None) -> usage.Usage:
        fetched.append(home)
        return usage.Usage(captured_at=0, five_hour=usage.Window(3.0, 1), seven_day=None, live=True)

    monkeypatch.setattr(usage, "fetch_codex_usage", _fake_fetch)
    # Neither home has a cache yet → both stale.
    monkeypatch.setattr(usage, "codex_usage_stale", lambda *_a, **_k: True)

    with Store() as store:
        store.ensure("s1", cwd="/repo")
        report = DaemonReport()
        _refresh_codex_usage(store, cfg, report, dry_run=False)
        assert sorted(fetched) == sorted(homes.values())
        assert report.codex_refreshed is True
        # A routine refresh is not "something happened" — it must not wake the reporter.
        assert DaemonReport(codex_refreshed=True).is_empty() is True

    # Kill-switch off → no fetch. Same for a dry run.
    fetched.clear()
    cfg.codex_usage = False
    with Store() as store:
        _refresh_codex_usage(store, cfg, DaemonReport(), dry_run=False)
        cfg.codex_usage = True
        _refresh_codex_usage(store, cfg, DaemonReport(), dry_run=True)
    assert fetched == []
