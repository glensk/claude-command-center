"""`ccc doctor` — read-only sectioned health check.

Covers a fresh machine (nothing wired), per-feature dependency checks keyed off the
config flags, and the exit-code / rendering contract. All external probes (``which``,
launchd, session-continue resolution) are monkeypatched — nothing real is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import config, doctor, install


def _which_factory(present: set[str]):
    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return fake_which


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    # Keep the daemon section deterministic across platforms.
    from command_center import launchd

    monkeypatch.setattr(launchd, "is_loaded", lambda: False)
    monkeypatch.setattr(launchd, "is_installed", lambda: False)


def _statuses(section: doctor.Section) -> dict[str, str]:
    return {c.label: c.status for c in section.checks}


def _feat(cfg: config.Config, label: str) -> str:
    """Status of a single feature check, by label."""
    return _statuses(doctor._section_features(cfg))[label]


# ------------------------------ fresh machine ------------------------------ #
def test_fresh_machine_reports_missing_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    report = doctor.build_report(config.Config())  # all-inert defaults
    text = doctor.render(report)
    assert "hooks wired" in text and "statusline wired" in text
    # Nothing is wired on a fresh machine → those are ❌ → exit 1.
    assert report.exit_code == 1
    assert "❌" in text


def test_render_never_raises_with_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"claude"}))
    # No config.toml, no settings.json — just defaults; must produce a report.
    out = doctor.render(doctor.build_report())
    assert "Core" in out and "Features & dependencies" in out


# ------------------------------ wiring reflects settings ------------------------------ #
def test_wiring_ok_after_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")
    install.install_hooks()
    install.install_statusline()
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"claude", "osascript"}))
    section = next(
        s for s in doctor.build_report(config.Config()).sections if s.title.startswith("Wiring")
    )
    statuses = _statuses(section)
    assert statuses["hooks wired"] == doctor.OK
    assert statuses["statusline wired"] == doctor.OK


# ------------------------------ per-feature dependency checks ------------------------------ #
def test_copilot_usage_flag_drives_gh_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"osascript"}))  # no gh
    on = _statuses(doctor._section_features(config.Config(copilot_usage=True)))
    assert on["copilot_usage → gh"] == doctor.FAIL
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"gh", "osascript"}))
    ok = _statuses(doctor._section_features(config.Config(copilot_usage=True)))
    assert ok["copilot_usage → gh"] == doctor.OK
    # Disabled → not-applicable, never a failure.
    off = _statuses(doctor._section_features(config.Config(copilot_usage=False)))
    assert off["copilot_usage → gh"] == doctor.NA


def test_codex_usage_flag_drives_the_auth_json_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One check per configured CODEX_HOME: the live fetch needs a readable auth.json."""
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # Disabled → not-applicable, never a failure (and no per-home rows at all).
    off = _statuses(doctor._section_features(config.Config(codex_usage=False)))
    assert off["codex_usage → auth.json"] == doctor.NA

    on = _statuses(doctor._section_features(config.Config(codex_usage=True)))
    assert on["codex_usage → auth.json (default)"] == doctor.FAIL  # no auth.json yet

    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    ok = _statuses(doctor._section_features(config.Config(codex_usage=True)))
    assert ok["codex_usage → auth.json (default)"] == doctor.OK


def test_short_aim_codex_backend_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"osascript"}))  # no codex
    cfg = config.Config(short_aim=True, short_aim_backend="codex")
    assert _feat(cfg, "short_aim → codex") == doctor.FAIL
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"codex", "osascript"}))
    assert _feat(cfg, "short_aim → codex") == doctor.OK
    # claude backend → the codex dep does not apply.
    claude_cfg = config.Config(short_aim=True, short_aim_backend="claude")
    assert _feat(claude_cfg, "short_aim → codex") == doctor.NA


def test_resume_halted_checks_session_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import resume

    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"osascript"}))
    monkeypatch.setattr(resume, "_resolve_continue_script", lambda cfg: "")
    cfg = config.Config(resume_halted=True)
    assert _feat(cfg, "resume_halted → session-continue") == doctor.FAIL
    monkeypatch.setattr(
        resume, "_resolve_continue_script", lambda cfg: "/x/claude-session-continue"
    )
    assert _feat(cfg, "resume_halted → session-continue") == doctor.OK


def test_vault_features_check_vault_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"osascript"}))
    missing = config.Config(future_files=True, vault_root=str(tmp_path / "nope"))
    assert _feat(missing, "vault features → vault_root") == doctor.FAIL
    present = config.Config(mirror_running=True, vault_root=str(tmp_path))
    assert _feat(present, "vault features → vault_root") == doctor.OK


def test_launcher_dependency_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))  # neither present
    iterm = _statuses(doctor._section_features(config.Config(launcher="iterm")))
    assert iterm["launcher=iterm → osascript"] == doctor.FAIL
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"tmux"}))
    tmux = _statuses(doctor._section_features(config.Config(launcher="tmux")))
    assert tmux["launcher=tmux → tmux"] == doctor.OK


def test_score_ladder_rungs_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    # opencode + claude present; gemini absent; custom has no command → per-rung ✅/❌.
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"opencode", "claude"}))
    cfg = config.Config(score_backends=["copilot", "gemini", "claude", "custom"])
    st = _statuses(doctor._section_features(cfg))
    assert st["score ladder → copilot"] == doctor.OK  # opencode on PATH
    assert st["score ladder → gemini"] == doctor.FAIL  # gemini not found
    assert st["score ladder → claude"] == doctor.OK
    assert st["score ladder → custom"] == doctor.FAIL  # no score_custom_command set


def test_score_ladder_custom_command_makes_it_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    cfg = config.Config(score_backends=["custom"], score_custom_command="my-router --score")
    assert _feat(cfg, "score ladder → custom") == doctor.OK


def test_score_ladder_empty_is_na(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    assert _feat(config.Config(score_backends=[]), "score ladder") == doctor.NA


# ------------------------------ daemon section: platform-aware ------------------------------ #
def test_daemon_section_reports_launchd_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import launchd

    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(launchd, "is_loaded", lambda: True)
    monkeypatch.setattr(launchd, "is_installed", lambda: True)
    statuses = _statuses(doctor._section_daemon())
    assert "launchd agent loaded" in statuses
    assert statuses["launchd agent loaded"] == doctor.OK


def test_daemon_section_reports_systemd_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import systemdunit

    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(systemdunit, "is_active", lambda cfg=None: True)
    monkeypatch.setattr(systemdunit, "is_installed", lambda cfg=None: True)
    statuses = _statuses(doctor._section_daemon())
    assert "systemd --user timer active" in statuses
    assert statuses["systemd --user timer active"] == doctor.OK


def test_daemon_section_systemd_installed_but_inactive_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from command_center import systemdunit

    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(systemdunit, "is_active", lambda cfg=None: False)
    monkeypatch.setattr(systemdunit, "is_installed", lambda cfg=None: True)
    statuses = _statuses(doctor._section_daemon())
    assert statuses["systemd --user timer"] == doctor.FAIL


def test_daemon_section_na_on_other_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "freebsd")
    statuses = _statuses(doctor._section_daemon())
    assert statuses["daemon service"] == doctor.NA


# ------------------------------ Stop-hook order guard ------------------------------ #
def _stop_settings(*commands: str) -> dict:
    """A settings dict whose Stop event lists *commands*, one per group, in order."""
    return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c}]} for c in commands]}}


def test_stop_order_ok_when_release_locks_last() -> None:
    settings = _stop_settings("/my/commit.sh", "/opt/ccc hook stop", "/opt/ccc hook release-locks")
    assert doctor._stop_order_check(settings).status == doctor.OK


def test_stop_order_warns_when_foreign_hook_after_release_locks() -> None:
    settings = _stop_settings(
        "/opt/ccc hook stop", "/opt/ccc hook release-locks", "/late/foreign.sh"
    )
    assert doctor._stop_order_check(settings).status == doctor.FAIL


def test_stop_order_na_when_release_locks_not_wired() -> None:
    assert doctor._stop_order_check({}).status == doctor.NA
    assert doctor._stop_order_check(_stop_settings("/only/foreign.sh")).status == doctor.NA


def test_exit_code_zero_when_no_failures() -> None:
    healthy = doctor.Report(
        [doctor.Section("x", [doctor.Check(doctor.OK, "a"), doctor.Check(doctor.NA, "b")])]
    )
    assert healthy.exit_code == 0
    broken = doctor.Report([doctor.Section("x", [doctor.Check(doctor.FAIL, "a")])])
    assert broken.exit_code == 1
