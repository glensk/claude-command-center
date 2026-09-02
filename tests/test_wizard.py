"""``ccc init`` — the first-run wizard.

Covers the consent-group ↔ ``INERT_DEFAULT_KEYS`` drift guard, the non-TTY exit-3
contract, what ``-y`` / ``-m`` write to a temp CLAUDE_HOME, and the ``-f`` backup-on-force
path. The installers are stubbed so ``run`` never touches the real machine (no launchd,
no settings.json, no vault writes).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center import (
    config,
    doctor,
    install,
    install_commands,
    launchd,
    obsidian,
    shell_install,
    wizard,
)


@pytest.fixture(autouse=True)
def _claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    return tmp_path / "claude"


@pytest.fixture
def _stub_installers(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Neutralise every side-effecting installer the wizard may call."""
    calls: dict[str, int] = {}

    def rec(name: str):
        def _f(*_a, **_k) -> int:
            calls[name] = calls.get(name, 0) + 1
            return 0

        return _f

    monkeypatch.setattr(install, "install_hooks", rec("hooks"))
    monkeypatch.setattr(install, "install_statusline", rec("statusline"))
    monkeypatch.setattr(install_commands, "run", rec("commands"))
    monkeypatch.setattr(obsidian, "run_setup", rec("obsidian"))
    monkeypatch.setattr(launchd, "install", rec("daemon"))
    monkeypatch.setattr(shell_install, "install", rec("shell"))
    monkeypatch.setattr(doctor, "run", rec("doctor"))
    return calls


def _args(**kw) -> SimpleNamespace:
    base = {"yes": False, "minimal": False, "vault_root": None, "force": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _load(home: Path) -> dict:
    return tomllib.loads((home / "command-center" / "config.toml").read_text(encoding="utf-8"))


# ------------------------------ consent-group drift guard ------------------------------ #
def test_consent_groups_cover_every_inert_key() -> None:
    mapped = (
        set(wizard.GROUP_A_CHECKERS)
        | set(wizard.GROUP_B_VAULT)
        | set(wizard.GROUP_C)
        | set(wizard.UNMAPPED_INERT)
    )
    assert mapped == set(config.INERT_DEFAULT_KEYS), (
        "a new inert key must be mapped into an init consent group (or UNMAPPED_INERT)"
    )
    # No key double-counted across groups.
    total = (
        len(wizard.GROUP_A_CHECKERS)
        + len(wizard.GROUP_B_VAULT)
        + len(wizard.GROUP_C)
        + len(wizard.UNMAPPED_INERT)
    )
    assert total == len(config.INERT_DEFAULT_KEYS)


# ------------------------------ non-TTY exit 3 ------------------------------ #
def test_non_tty_without_yes_or_minimal_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert wizard.run(_args()) == 3


# ------------------------------ -y writes the recommended profile ------------------------------ #
def test_yes_writes_checkers_on_no_vault(
    _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    assert wizard.run(_args(yes=True)) == 0
    data = _load(_claude_home)
    for key in wizard.GROUP_A_CHECKERS:
        assert data[key] is True
    # No vault → no vault features, no vault anchor, group C stays off (absent = default).
    for key in (*wizard.GROUP_B_VAULT, "copilot_usage", "resume_halted", "reap", "vault_root"):
        assert key not in data
    # Installers all ran (obsidian-setup skipped without a vault).
    assert _stub_installers["hooks"] and _stub_installers["commands"]
    assert "obsidian" not in _stub_installers


def test_yes_with_vault_enables_vault_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _claude_home: Path,
    _stub_installers: dict[str, int],
) -> None:
    # A resolvable credential scrubber is what unlocks the mirror switches (never probe
    # the developer's real PATH here — the answer would differ per machine).
    monkeypatch.setattr(wizard, "scrubber_status", lambda: ("/opt/stub/scrubber", ""))
    vault = tmp_path / "v"
    vault.mkdir()
    assert wizard.run(_args(yes=True, vault_root=str(vault))) == 0
    data = _load(_claude_home)
    for key in wizard.GROUP_B_VAULT:
        assert data[key] is True
    assert data["vault_root"] == str(vault)
    assert data["future_dir"] == f"{vault}/01-llm-tasks/future"
    assert _stub_installers.get("obsidian")


def test_yes_with_vault_but_no_scrubber_leaves_mirrors_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _claude_home: Path,
    _stub_installers: dict[str, int],
) -> None:
    """No scrubber → the FUTURE files go on, the verbatim mirrors stay off (and it says so)."""
    monkeypatch.setattr(
        wizard,
        "scrubber_status",
        lambda: (None, "no scrubber configured (mirror_scrub_cmd is empty)"),
    )
    vault = tmp_path / "v"
    vault.mkdir()
    assert wizard.run(_args(yes=True, vault_root=str(vault))) == 0
    data = _load(_claude_home)
    assert data["future_files"] is True
    for key in wizard.GROUP_B_MIRRORS:
        assert key not in data
    out = capsys.readouterr().out
    assert "mirrors: off" in out
    assert "no scrubber configured" in out


# ------------------------------ -m minimal ------------------------------ #
def test_minimal_writes_no_feature_flags_and_runs_no_installers(
    _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    assert wizard.run(_args(minimal=True)) == 0
    data = _load(_claude_home)
    for key in (*wizard.GROUP_A_CHECKERS, *wizard.GROUP_B_VAULT, *wizard.GROUP_C):
        assert key not in data
    assert _stub_installers == {} or "doctor" in _stub_installers  # only doctor may run
    assert "hooks" not in _stub_installers


# ------------------------------ existing config: refuse / -f backup ------------------------ #
def test_refuses_existing_config_without_force(
    _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("reap = true\n", encoding="utf-8")
    assert wizard.run(_args(yes=True)) == 1
    # Untouched.
    assert path.read_text(encoding="utf-8") == "reap = true\n"


def test_force_backs_up_existing_config(
    _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("reap = true\n", encoding="utf-8")
    assert wizard.run(_args(yes=True, force=True)) == 0
    backups = list(path.parent.glob("config.toml.ccc-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "reap = true\n"
    # The new config is the -y profile (checkers on), not the old content.
    assert _load(_claude_home)["grade_on_turn"] is True


# ------------------------------ score-backend detection ------------------------------ #
def test_detect_score_backends_orders_available(monkeypatch: pytest.MonkeyPatch) -> None:
    present = {"opencode", "codex", "claude"}  # gemini absent
    monkeypatch.setattr(
        wizard.shutil, "which", lambda tool: f"/bin/{tool}" if tool in present else None
    )
    # copilot (opencode) first, gemini dropped, codex, claude last.
    assert wizard.detect_score_backends() == ["copilot", "codex", "claude"]


def test_detect_score_backends_empty_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard.shutil, "which", lambda _tool: None)
    assert wizard.detect_score_backends() == []


def test_yes_writes_detected_score_ladder(
    monkeypatch: pytest.MonkeyPatch, _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    monkeypatch.setattr(wizard, "detect_score_backends", lambda: ["copilot", "codex", "claude"])
    assert wizard.run(_args(yes=True)) == 0
    assert _load(_claude_home)["score_backends"] == ["copilot", "codex", "claude"]


def test_yes_omits_score_ladder_when_claude_only(
    monkeypatch: pytest.MonkeyPatch, _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    # Detection == the default ["claude"] → minimal_config_text drops it (equals the default).
    monkeypatch.setattr(wizard, "detect_score_backends", lambda: ["claude"])
    assert wizard.run(_args(yes=True)) == 0
    assert "score_backends" not in _load(_claude_home)


def test_minimal_omits_score_ladder(
    monkeypatch: pytest.MonkeyPatch, _claude_home: Path, _stub_installers: dict[str, int]
) -> None:
    # No checkers → no ladder written even if backends are detected.
    monkeypatch.setattr(wizard, "detect_score_backends", lambda: ["copilot", "claude"])
    assert wizard.run(_args(minimal=True)) == 0
    assert "score_backends" not in _load(_claude_home)


# ------------------------------ pure profile → config ------------------------------ #
def test_minimal_config_text_only_diffs_from_defaults() -> None:
    text = wizard.minimal_config_text(wizard.Profile(checkers=True))
    assert "grade_on_turn = true" in text
    assert "reap" not in text  # already default false → not emitted
    assert "verify_subgoals_llm" not in text  # never turned on by init


def test_config_values_splits_future_files_from_mirrors() -> None:
    """``vault_features`` writes only the FUTURE keys; the mirrors need their own consent."""
    base = wizard.Profile(vault_root="/tmp/v", vault_features=True)
    values = wizard.config_values(base)
    for key in wizard.GROUP_B_FUTURE:
        assert values[key] is True
    for key in wizard.GROUP_B_MIRRORS:
        assert key not in values

    with_mirrors = wizard.config_values(
        wizard.Profile(vault_root="/tmp/v", vault_features=True, mirrors=True)
    )
    for key in wizard.GROUP_B_VAULT:
        assert with_mirrors[key] is True


def test_score_backends_serialized_as_toml_list() -> None:
    profile = wizard.Profile(checkers=True, score_backends=["copilot", "gemini", "claude"])
    text = wizard.minimal_config_text(profile)
    assert 'score_backends = ["copilot", "gemini", "claude"]' in text
