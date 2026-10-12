"""``ccc obsidian-setup`` — folders, generified dashboards, and job-button shellcommands.

The autouse ``_isolate_vault_dirs`` conftest fixture already points every loaded config's
vault dirs at ``tmp_path/vault``; these tests create that vault and drive :func:`run_setup`
against it, asserting the dashboards render from the config paths (not hardcoded), carry the
``ccc_generated`` marker, and that the shellcommands merge is idempotent + foreign-safe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import config, install, obsidian


@pytest.fixture(autouse=True)
def _pin_ccc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir(parents=True, exist_ok=True)
    return v


# ------------------------------ refusal ------------------------------ #
def test_refuses_when_vault_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No vault dir created → the loaded config's vault_root does not exist.
    assert obsidian.run_setup() == 1
    assert "does not exist" in capsys.readouterr().err


# ------------------------------ folders + pad ------------------------------ #
def test_default_run_creates_folders_and_pad(vault: Path) -> None:
    assert obsidian.run_setup() == 0
    cfg = config.load_config()
    for path in obsidian.task_dirs(cfg):
        assert path.is_dir()
    assert Path(cfg.future_pad).expanduser().is_file()


# ------------------------------ dashboards ------------------------------ #
def test_dashboards_render_from_config_paths(vault: Path) -> None:
    obsidian.run_setup()
    future_md = vault / "01-llm" / "ccc-tasks" / "future.md"
    assert future_md.is_file()
    text = future_md.read_text(encoding="utf-8")
    # Folder query + binary come from config, not hardcoded/personal values.
    assert 'const FOLDER = "01-llm/ccc-tasks/future"' in text
    assert '"/opt/ccc"' in text
    assert "{{CCC_BIN}}" not in text and "{{FUTURE_FOLDER}}" not in text
    assert obsidian.has_marker(text)
    # All four dashboards land where dashboard_targets says.
    for _tmpl, dest in obsidian.dashboard_targets(config.load_config()):
        assert dest.is_file()


def test_dashboard_folder_token_follows_custom_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = tmp_path / "myvault"
    (v).mkdir()

    def _custom() -> config.Config:
        cfg = config.Config()
        cfg.vault_root = str(v)
        cfg.future_dir = str(v / "tasks" / "fut")
        cfg.delete_dir = str(v / "tasks" / "del")
        cfg.future_pad = str(v / "tasks" / "pad.md")
        cfg.running_dir = str(v / "tasks" / "run")
        cfg.done_dir = str(v / "tasks" / "done")
        cfg.sessions_dir = str(v / "tasks" / "sess")
        return cfg

    monkeypatch.setattr(config, "load_config", _custom)
    assert obsidian.run_setup() == 0
    running = v / "tasks" / "running.md"
    assert running.is_file()
    assert 'const FOLDER = "tasks/run"' in running.read_text(encoding="utf-8")


def test_marker_guarded_overwrite(vault: Path) -> None:
    dest = vault / "01-llm" / "ccc-tasks" / "future.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("# my own note, no marker\n", encoding="utf-8")
    obsidian.run_setup()
    # A file without the marker is left untouched.
    assert dest.read_text(encoding="utf-8") == "# my own note, no marker\n"


def test_rerun_dashboard_is_idempotent(vault: Path) -> None:
    obsidian.run_setup()
    dest = vault / "01-llm" / "ccc-tasks" / "future.md"
    before = dest.stat().st_mtime_ns
    obsidian.run_setup()
    assert dest.stat().st_mtime_ns == before
    assert not list((vault / "01-llm" / "ccc-tasks").glob("future.md.ccc-backup-*"))


# ------------------------------ shellcommands ------------------------------ #
def _shellcmd_data(vault: Path) -> dict:
    path = vault / ".obsidian" / "plugins" / "obsidian-shellcommands" / "data.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_shellcommands_merge_preserves_foreign_and_adds_ours(vault: Path) -> None:
    plugin_dir = vault / ".obsidian" / "plugins" / "obsidian-shellcommands"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "data.json").write_text(
        json.dumps({"settings_version": "0.23.0", "shell_commands": [{"id": "foreign-thing"}]}),
        encoding="utf-8",
    )
    obsidian.run_setup()
    data = _shellcmd_data(vault)
    ids = {e["id"] for e in data["shell_commands"]}
    assert "foreign-thing" in ids
    assert "ccc-start-job-from-file" in ids
    entry = next(e for e in data["shell_commands"] if e["id"] == "ccc-start-job-from-file")
    assert entry["platform_specific_commands"]["default"].startswith("/opt/ccc open-job --file")
    assert data["settings_version"] == "0.23.0"


def test_shellcommands_merge_is_idempotent(vault: Path) -> None:
    plugin_dir = vault / ".obsidian" / "plugins" / "obsidian-shellcommands"
    plugin_dir.mkdir(parents=True, exist_ok=True)  # empty dir → first run creates data.json fresh
    obsidian.run_setup()
    data_path = plugin_dir / "data.json"
    first = data_path.read_text(encoding="utf-8")
    obsidian.run_setup()
    # No duplicate entries, no backup churn on the second pass.
    data = _shellcmd_data(vault)
    ours = [e for e in data["shell_commands"] if e["id"] == "ccc-start-job-from-file"]
    assert len(ours) == 1
    assert data_path.read_text(encoding="utf-8") == first
    assert not list(plugin_dir.glob("data.json.ccc-backup-*"))


def test_shellcommands_absent_plugin_prints_instructions(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert obsidian.run_setup() == 0
    out = capsys.readouterr().out
    assert "obsidian-shellcommands is not installed" in out
    assert not (vault / ".obsidian" / "plugins" / "obsidian-shellcommands").exists()


# ------------------------------ dry-run + uninstall ------------------------------ #
def test_dry_run_writes_nothing(vault: Path) -> None:
    assert obsidian.run_setup(dry_run=True) == 0
    assert not (vault / "01-llm" / "ccc-tasks" / "future.md").exists()
    assert not (vault / "01-llm" / "ccc-tasks" / "future").exists()


def test_uninstall_removes_only_marked(vault: Path) -> None:
    obsidian.run_setup()
    # An unmarked user file at a dashboard path is not removed.
    parked = vault / "01-llm" / "ccc-tasks" / "parked.md"
    parked.write_text("mine, no marker\n", encoding="utf-8")
    assert obsidian.run_setup(uninstall=True) == 0
    assert not (vault / "01-llm" / "ccc-tasks" / "future.md").exists()
    assert parked.exists()


# ------------------------------ pure helpers ------------------------------ #
def test_build_shellcommand_entries_shape() -> None:
    entries = obsidian.build_shellcommand_entries("/opt/ccc")
    assert [e["id"] for e in entries] == [
        "ccc-start-job-from-file",
        "ccc-done-job-from-file",
        "ccc-delete-job-from-file",
        "ccc-restore-job-from-file",
    ]
    subs = ("open-job", "done-job", "delete-job", "restore-job")
    for entry, sub in zip(entries, subs, strict=True):
        cmd = entry["platform_specific_commands"]["default"]
        assert cmd == f"/opt/ccc {sub} --file {{{{file_path:absolute}}}}"


def test_merge_shellcommands_uninstall_strips_ours_only() -> None:
    data = {"shell_commands": [{"id": "foreign"}, *obsidian.build_shellcommand_entries("/x/ccc")]}
    stripped = obsidian.merge_shellcommands(data, "/x/ccc", uninstall=True)
    ids = {e["id"] for e in stripped["shell_commands"]}
    assert ids == {"foreign"}
