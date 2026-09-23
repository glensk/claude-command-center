"""`ccc doctor` — read-only sectioned health check.

Covers a fresh machine (nothing wired), per-feature dependency checks keyed off the
config flags, and the exit-code / rendering contract. All external probes (``which``,
launchd, session-continue resolution) are monkeypatched — nothing real is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import config, doctor, hookroutes, install


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
    # Never read this machine's real Automation store: the Terminal section must be −
    # ("unreadable") unless a test points it at a fake TCC.db of its own.
    monkeypatch.setattr(doctor, "_TCC_DB", tmp_path / "no-such-TCC.db")
    # A machine that really carries a managed-settings file must not flip the Stop-hook
    # coverage check to "cannot prove coverage" in these tests.
    monkeypatch.setattr(doctor, "_MANAGED_SETTINGS_DARWIN", tmp_path / "no-managed.json")
    monkeypatch.setattr(doctor, "_MANAGED_SETTINGS_POSIX", tmp_path / "no-managed.json")


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


def test_llm_router_check() -> None:
    assert _feat(config.Config(llm_custom_command=""), "LLM router") == doctor.NA
    assert _feat(config.Config(llm_custom_command="ai prompt"), "LLM router") == doctor.OK


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


# ------------------------- Stop-hook timeout coverage ------------------------- #
_CCC_STOP = "/opt/ccc hook stop"
_CCC_RELEASE = "/opt/ccc hook release-locks"


def _stop_settings(*commands: str) -> dict:
    """A settings dict whose Stop event lists *commands*, one per group, in order."""
    return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c}]} for c in commands]}}


def _stop_entries(*entries: dict) -> dict:
    """A settings dict whose Stop event lists *entries* verbatim (timeouts, odd types, …)."""
    return {"hooks": {"Stop": [{"hooks": [dict(entry)]} for entry in entries]}}


def _barrier_wait(seconds: int) -> None:
    cfg = config.load_config()
    cfg.stop_barrier_wait_sec = seconds
    config.save_config(cfg)


def test_stop_timeout_coverage_na_when_release_locks_not_wired() -> None:
    """Nothing to cover: without ccc's lease hook there is no window to compare against."""
    assert doctor._stop_hook_timeout_coverage_check({}).status == doctor.NA
    check = doctor._stop_hook_timeout_coverage_check(_stop_settings("/only/foreign.sh"))
    assert check.status == doctor.NA and "not wired" in check.detail


def test_stop_timeout_coverage_na_on_an_entry_it_cannot_read() -> None:
    """A non-``command`` (or empty) Stop entry may do anything — never claim coverage."""
    settings = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE},
        {"type": "prompt", "command": "/foreign/thing.sh", "timeout": 5},
    )
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.NA and "cannot prove coverage" in check.detail
    empty = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE}, {"type": "command", "command": ""}
    )
    assert doctor._stop_hook_timeout_coverage_check(empty).status == doctor.NA


def test_stop_timeout_coverage_na_when_a_plugin_may_add_stop_hooks() -> None:
    """An enabled plugin registers hooks in a file ccc never reads (measured: openai-codex)."""
    settings = _stop_settings(_CCC_STOP, _CCC_RELEASE, "/my/commit.sh")
    settings["enabledPlugins"] = {"codex@openai-codex": True}
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.NA
    assert "1 enabled plugin" in check.detail and "cannot read" in check.detail


def test_stop_timeout_coverage_na_when_managed_settings_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed-settings file is a scope ccc can neither read nor enumerate."""
    managed = tmp_path / "managed-settings.json"
    managed.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_MANAGED_SETTINGS_DARWIN", managed)
    monkeypatch.setattr(doctor, "_MANAGED_SETTINGS_POSIX", managed)
    check = doctor._stop_hook_timeout_coverage_check(_stop_settings(_CCC_RELEASE, "/my/commit.sh"))
    assert check.status == doctor.NA and "managed settings" in check.detail


def test_stop_timeout_coverage_fails_even_when_a_scope_is_unreadable() -> None:
    """A readable 300 s hook is a FAIL whatever else is invisible — NA would hide it.

    An unenumerable plugin scope can only push the true maximum HIGHER; it can never make a
    hook ccc CAN see safe, so the blocker is appended to the finding instead of replacing it.
    """
    settings = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE},
        {"type": "command", "command": "/bin/zsh /me/auto-commit-after-turn.sh", "timeout": 300},
    )
    settings["enabledPlugins"] = {"codex@openai-codex": True}
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.FAIL
    assert "auto-commit-after-turn.sh" in check.detail and "300s" in check.detail
    assert "and 1 enabled plugin may add Stop hooks ccc cannot read" in check.detail


def test_stop_timeout_coverage_na_still_reports_what_is_measurable() -> None:
    """An NA that measures nothing is a wasted check: name the longest READABLE timeout."""
    settings = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE},
        {"type": "command", "command": "/my/lint.sh", "timeout": 30},
    )
    settings["enabledPlugins"] = {"codex@openai-codex": True}
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.NA
    assert "1 enabled plugin" in check.detail
    assert "30s" in check.detail and "180s" in check.detail and "lint.sh" in check.detail


def test_stop_timeout_coverage_ok_below_the_barrier_and_says_it_is_only_an_upper_bound() -> None:
    """A declared timeout bounds the hook; it is NOT proof the hook finished — say so."""
    settings = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE},
        {"type": "command", "command": "/my/commit.sh", "timeout": 120},
        {"type": "command", "command": "/my/lint.sh", "timeout": 30},
    )
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.OK
    assert "120s" in check.detail and "commit.sh" in check.detail
    assert "upper bound" in check.detail


def test_stop_timeout_coverage_fails_when_a_foreign_hook_outlives_the_lease() -> None:
    """A 300 s auto-commit against a 180 s lease: the locks free themselves mid-commit."""
    settings = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE},
        {"type": "command", "command": "/bin/zsh /me/auto-commit-after-turn.sh", "timeout": 300},
    )
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.FAIL
    assert "auto-commit-after-turn.sh" in check.detail
    assert "300s" in check.detail and "180s" in check.detail
    assert "stop_barrier_wait_sec" in check.detail


def test_stop_timeout_coverage_normalizes_an_omitted_timeout_to_claude_codes_default() -> None:
    """No `timeout` key means Claude Code's own default (60 s), not "no limit" and not 0."""
    settings = _stop_entries(
        {"type": "command", "command": _CCC_RELEASE},
        {"type": "command", "command": "/my/commit.sh"},
    )
    assert doctor.CLAUDE_HOOK_DEFAULT_TIMEOUT_SEC == 60
    _barrier_wait(30)
    check = doctor._stop_hook_timeout_coverage_check(settings)
    assert check.status == doctor.FAIL and "60s" in check.detail and "30s" in check.detail
    _barrier_wait(180)
    assert doctor._stop_hook_timeout_coverage_check(settings).status == doctor.OK


def test_stop_timeout_coverage_ok_without_any_foreign_stop_hook() -> None:
    """ccc's own entries are not measured against the window they define."""
    check = doctor._stop_hook_timeout_coverage_check(_stop_settings(_CCC_STOP, _CCC_RELEASE))
    assert check.status == doctor.OK and "no foreign Stop hooks" in check.detail


def test_exit_code_zero_when_no_failures() -> None:
    healthy = doctor.Report(
        [doctor.Section("x", [doctor.Check(doctor.OK, "a"), doctor.Check(doctor.NA, "b")])]
    )
    assert healthy.exit_code == 0
    broken = doctor.Report([doctor.Section("x", [doctor.Check(doctor.FAIL, "a")])])
    assert broken.exit_code == 1


# ------------------------- Terminal section (tp#90) ------------------------- #
def _tcc_db(path: Path, rows: list[tuple[str, str, int, int, str]]) -> Path:
    """A fake TCC.db with only the columns the doctor reads."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE access (service TEXT, client TEXT, client_type INTEGER, "
        "auth_value INTEGER, indirect_object_identifier TEXT)"
    )
    conn.executemany("INSERT INTO access VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


_AE, _ITERM = "kTCCServiceAppleEvents", "com.googlecode.iterm2"


def test_tcc_grant_verdicts(tmp_path: Path) -> None:
    db = _tcc_db(
        tmp_path / "TCC.db",
        [
            (_AE, "/py/allowed", 1, 2, _ITERM),
            (_AE, "/py/denied", 1, 0, _ITERM),
            (_AE, "/py/terminal-only", 1, 2, "com.apple.Terminal"),
        ],
    )
    grant = doctor._tcc_apple_events_grant
    assert grant("/py/allowed", db) == "allowed"
    assert grant("/py/denied", db) == "denied"
    assert (
        grant("/py/terminal-only", db) == "unknown"
    )  # a grant for Terminal.app is not one for iTerm2
    assert grant("/py/never-asked", db) == "unknown"
    assert grant("/py/allowed", tmp_path / "missing.db") == "unreadable"
    junk = tmp_path / "junk.db"
    junk.write_text("not a database", encoding="utf-8")
    assert grant("/py/allowed", junk) == "unreadable"


def test_terminal_section_fails_only_on_an_explicit_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import sys

    from command_center import terminal

    exe = os.path.realpath(sys.executable)
    label = "Automation grant → iTerm2 (ccc's interpreter)"
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "_iterm2_present", lambda: True)
    monkeypatch.setattr(doctor, "_iterm_api_server_enabled", lambda: True)
    monkeypatch.setattr(terminal, "is_iterm_api_auth_tcc_free", lambda: False)

    monkeypatch.setattr(
        doctor, "_TCC_DB", _tcc_db(tmp_path / "denied.db", [(_AE, exe, 1, 0, _ITERM)])
    )
    section = doctor._section_terminal()
    assert _statuses(section)[label] == doctor.FAIL
    assert _statuses(section)["Python-API rung TCC-free"] == doctor.NA
    assert _statuses(section)["iTerm2 Python API server"] == doctor.OK

    monkeypatch.setattr(
        doctor, "_TCC_DB", _tcc_db(tmp_path / "allowed.db", [(_AE, exe, 1, 2, _ITERM)])
    )
    assert _statuses(doctor._section_terminal())[label] == doctor.OK

    monkeypatch.setattr(doctor, "_TCC_DB", _tcc_db(tmp_path / "empty.db", []))
    assert _statuses(doctor._section_terminal())[label] == doctor.NA  # never asked: informational

    monkeypatch.setattr(doctor, "_TCC_DB", tmp_path / "absent.db")
    assert _statuses(doctor._section_terminal())[label] == doctor.NA  # unreadable: informational

    monkeypatch.setattr(terminal, "is_iterm_api_auth_tcc_free", lambda: True)
    assert _statuses(doctor._section_terminal())["Python-API rung TCC-free"] == doctor.OK

    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert {c.status for c in doctor._section_terminal().checks} == {doctor.NA}


def test_report_includes_the_terminal_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"claude"}))
    assert "Terminal (iTerm2 launch path)" in doctor.render(doctor.build_report(config.Config()))


# ------------------------- Spawn fast path (tp#115) ------------------------- #
def _write_settings(home: Path, settings: dict) -> None:
    import json

    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _fast_path(home: Path, monkeypatch: pytest.MonkeyPatch, settings: dict) -> dict[str, str]:
    """The Spawn-fast-path statuses for *settings*, with *home* as $HOME (readable scripts)."""
    monkeypatch.setenv("HOME", str(home))
    _write_settings(home, settings)
    section = doctor._section_fast_path()
    assert doctor.FAIL not in {c.status for c in section.checks}  # informational only
    return _statuses(section)


def test_fast_path_reads_cccs_own_statusline_from_its_state_not_its_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chained ccc statusLine is `statusline` by construction — no text parsing.

    The chain script deliberately does NOT exist here: were the section reading it, the
    missing file would surface as an indeterminate row instead of a plain ✅.
    """
    chain = install.chain_script_path()
    statuses = _fast_path(
        tmp_path,
        monkeypatch,
        {"statusLine": {"type": "command", "command": f"bash {chain}"}},
    )
    assert statuses == {"ccc statusline": doctor.OK}


def test_fast_path_follows_one_indirection_into_a_foreign_statusline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign status line's script is the case only text can answer."""
    script = tmp_path / "my-statusline.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "# ccc install-statusline is not run here\n"
        '"$CCC_BIN" statusline --print-glyph\n'
        'ccc aim --session "$sid" --format bar\n'
        "ccc frobnicate\n"
        'ccc "$cmd"\n',
        encoding="utf-8",
    )
    statuses = _fast_path(
        tmp_path,
        monkeypatch,
        {"statusLine": {"type": "command", "command": f"bash {script}"}},
    )
    assert statuses["ccc statusline"] == doctor.OK
    assert statuses["ccc aim"] == doctor.OK
    assert statuses["ccc frobnicate"] == doctor.NA  # full parser — visible, not broken
    assert statuses["spawned ccc commands"] == doctor.NA  # `ccc "$cmd"` is indeterminate
    assert "ccc install-statusline" not in statuses  # a comment line is not a spawn


def test_fast_path_follows_a_foreign_hook_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_script = tmp_path / "cc-hook.sh"
    hook_script.write_text('#!/usr/bin/env bash\nccc hook "${1:-}" || true\n', encoding="utf-8")
    statuses = _fast_path(
        tmp_path,
        monkeypatch,
        {"hooks": {"Stop": [{"hooks": [{"command": f"bash {hook_script}"}]}]}},
    )
    assert statuses == {"ccc hook": doctor.OK}


def test_fast_path_says_so_when_nothing_spawns_ccc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = _fast_path(tmp_path, monkeypatch, {})
    assert statuses == {"spawned ccc commands": doctor.NA}
    detail = doctor._section_fast_path().checks[0].detail
    assert "none found" in detail


def test_report_includes_the_fast_path_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"claude"}))
    assert "Spawn fast path" in doctor.render(doctor.build_report(config.Config()))


# ------------------- hook wiring exactness / duplicate path (tp#222) ------------------- #
#: A hand-wired forwarder: foreign to the installer, yet it spawns `ccc hook` itself.
_FORWARDER_BODY = '#!/usr/bin/env bash\nccc hook "${1:-}" || true\n'
#: A wrapper that execs its own arguments — the hook behind `--` is invisible in its body.
_WRAPPER_BODY = '#!/bin/bash\n"$@"\n'


def _ccc_wiring() -> dict:
    """settings.json holding exactly ccc's own hook entries (`/opt/ccc hook <event>`)."""
    return install.build_hooks_settings({}, "/opt/ccc", uninstall=False)


def _with_hook(settings: dict, event: str, command: str) -> dict:
    """*settings* plus one extra hook *command* wired on *event*."""
    groups = settings.setdefault("hooks", {}).setdefault(event, [])
    groups.append({"hooks": [{"type": "command", "command": command}]})
    return settings


def _script(home: Path, name: str, body: str) -> Path:
    path = home / name
    path.write_text(body, encoding="utf-8")
    return path


def _wiring(home: Path, monkeypatch: pytest.MonkeyPatch, settings: dict) -> doctor.Section:
    """The Wiring section for *settings*, with *home* as $HOME (readable hook scripts)."""
    monkeypatch.setenv("HOME", str(home))
    _write_settings(home, settings)
    return doctor._section_wiring()


def _detail(section: doctor.Section, label: str) -> str:
    return next(c.detail for c in section.checks if c.label == label)


def test_duplicate_hook_path_flags_a_foreign_forwarder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident shape: a `cc-hook.sh <event>` entry next to ccc's own direct ones."""
    script = _script(tmp_path, "cc-hook.sh", _FORWARDER_BODY)
    section = _wiring(
        tmp_path,
        monkeypatch,
        _with_hook(_ccc_wiring(), "SessionStart", f"{script} session-start"),
    )
    assert _statuses(section)["duplicate hook path"] == doctor.FAIL
    detail = _detail(section, "duplicate hook path")
    assert "cc-hook.sh" in detail and "twice" in detail
    # ccc's own wiring is untouched and complete — only the extra path is the problem.
    assert _statuses(section)["hooks wired"] == doctor.OK


def test_duplicate_hook_path_sees_through_a_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run-stop-hook.sh "label" -- <forwarder> stop`: the wrapper's body says nothing."""
    forwarder = _script(tmp_path, "cc-hook.sh", _FORWARDER_BODY)
    wrapper = _script(tmp_path, "run-stop-hook.sh", _WRAPPER_BODY)
    section = _wiring(
        tmp_path,
        monkeypatch,
        _with_hook(_ccc_wiring(), "Stop", f'{wrapper} "cc-hook stop" -- {forwarder} stop'),
    )
    assert _statuses(section)["duplicate hook path"] == doctor.FAIL
    assert "cc-hook.sh" in _detail(section, "duplicate hook path")


def test_duplicate_hook_path_ok_for_cccs_own_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _wiring(tmp_path, monkeypatch, _ccc_wiring())
    assert _statuses(section)["duplicate hook path"] == doctor.OK
    assert "only path" in _detail(section, "duplicate hook path")


def test_duplicate_hook_path_ignores_an_unrelated_foreign_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain commit hook does not reach ccc — it must not colour the check at all."""
    section = _wiring(tmp_path, monkeypatch, _with_hook(_ccc_wiring(), "Stop", "/my/commit.sh"))
    assert _statuses(section)["duplicate hook path"] == doctor.OK


def test_duplicate_hook_path_is_na_for_a_dynamic_foreign_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command built at runtime could hide anything: informational, never ❌."""
    section = _wiring(tmp_path, monkeypatch, _with_hook(_ccc_wiring(), "Stop", 'bash -c "$X"'))
    assert _statuses(section)["duplicate hook path"] == doctor.NA
    assert "unresolved" in _detail(section, "duplicate hook path")


def test_hooks_wired_fails_on_a_duplicated_ccc_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical ccc entries run ccc twice — a set of hook-args cannot see that."""
    section = _wiring(
        tmp_path, monkeypatch, _with_hook(_ccc_wiring(), "Stop", "/opt/ccc hook stop")
    )
    assert _statuses(section)["hooks wired"] == doctor.FAIL
    assert "Stop/stop \u00d72" in _detail(section, "hooks wired")


def test_hooks_wired_ok_only_for_the_exact_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _wiring(tmp_path, monkeypatch, _ccc_wiring())
    assert _statuses(section)["hooks wired"] == doctor.OK
    assert "exactly once" in _detail(section, "hooks wired")


def test_foreign_hook_routes_classifies_offender_unknown_and_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared helper itself: (offenders, indeterminate) by display name."""
    monkeypatch.setenv("HOME", str(tmp_path))
    forwarder = _script(tmp_path, "cc-hook.sh", _FORWARDER_BODY)
    dynamic = _script(tmp_path, "dyn.sh", '#!/usr/bin/env bash\nccc "$1"\n')
    commands = [
        "/opt/ccc hook stop",  # ccc's own: never a duplicate path to itself
        f"{forwarder} stop",
        f"bash {dynamic}",
        "/my/commit.sh",
    ]
    offenders, indeterminate = hookroutes.foreign_hook_routes(
        commands, lambda cmd: cmd.startswith("/opt/ccc ")
    )
    assert offenders == ["cc-hook.sh"]
    assert indeterminate == ["dyn.sh"]
    assert hookroutes.foreign_hook_routes(["/my/commit.sh"], lambda _c: False) == ([], [])


# ------------------------------ mirror scrubber ------------------------------ #
def test_mirror_scrubber_check_states(tmp_path: Path) -> None:
    from scrubstub import stub_scrubber

    from command_center.models import MirrorHealth
    from command_center.store import Store

    label = "mirrors → scrubber"
    assert _feat(config.Config(), label) == doctor.NA  # mirrors off → never probed
    assert (
        _feat(config.Config(mirror_running=True, mirror_allow_unscrubbed=True), label)
        == doctor.FAIL
    )
    assert (
        _feat(config.Config(mirror_sessions=True, mirror_scrub_cmd="/nonexistent/x scrub"), label)
        == doctor.FAIL
    )
    stub = stub_scrubber(tmp_path)
    cfg = config.Config(mirror_done=True, mirror_scrub_cmd=stub.scrub_cmd)
    check = {c.label: c for c in doctor._section_features(cfg).checks}[label]
    assert check.status == doctor.OK and str(stub.path) in check.detail
    with Store() as store:  # CLAUDE_HOME is pinned to tmp_path by the autouse fixture
        store.put_mirror_health(
            MirrorHealth(
                at=1, vouched=0, scrubbed=0, withheld=2, deferred=0, reason="scrubber exit 3"
            )
        )
    check = {c.label: c for c in doctor._section_features(cfg).checks}[label]
    assert check.status == doctor.FAIL and "withheld 2" in check.detail
