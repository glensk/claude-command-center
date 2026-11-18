"""``ccc obsidian-setup`` — mirror folders, the capture pad and the generified dashboards.

The autouse ``_isolate_vault_dirs`` conftest fixture already points every loaded config's
vault dirs at ``tmp_path/vault``; these tests create that vault and drive :func:`run_setup`
against it, asserting the dashboards render from the config paths (not hardcoded) and carry
the ``ccc_generated`` marker. Future jobs live in ccc's store (tp#2266 B12): setup seeds no
``future/`` / ``delete/`` folders, no ``future.md`` / ``delete.md`` and no job-button
shellcommands; it seeds the capture pad only when the pad is served, and strips the
retired job-button entries from an existing shellcommands ``data.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import config, install, obsidian

_RETIRED_IDS = {
    "ccc-start-job-from-file",
    "ccc-done-job-from-file",
    "ccc-delete-job-from-file",
    "ccc-restore-job-from-file",
}


@pytest.fixture(autouse=True)
def _pin_ccc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")


@pytest.fixture(name="vault")
def _vault_fixture(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir(parents=True, exist_ok=True)
    return v


def _with(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Wrap the (already vault-isolated) ``load_config`` with config *overrides*."""
    loaded = config.load_config

    def _patched() -> config.Config:
        cfg = loaded()
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    monkeypatch.setattr(config, "load_config", _patched)


def _tasks(vault: Path) -> Path:
    return vault / "01-llm" / "ccc-tasks"


# ------------------------------ refusal ------------------------------ #
def test_refuses_when_vault_missing(capsys: pytest.CaptureFixture[str]) -> None:
    # No vault dir created → the loaded config's vault_root does not exist.
    assert obsidian.run_setup() == 1
    assert "does not exist" in capsys.readouterr().err


# ------------------------------ folders + pad ------------------------------ #
@pytest.mark.usefixtures("vault")
def test_default_run_creates_mirror_folders_and_no_job_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with(monkeypatch, future_files=False, future_pad_enabled=False)
    assert obsidian.run_setup() == 0
    cfg = config.load_config()
    for path in obsidian.task_dirs(cfg):
        assert path.is_dir()
    assert Path(cfg.future_dir).expanduser() not in obsidian.task_dirs(cfg)
    assert not Path(cfg.future_dir).expanduser().exists()
    assert not Path(cfg.delete_dir).expanduser().exists()
    # The pad is not served → not seeded.
    assert not Path(cfg.future_pad).expanduser().exists()


@pytest.mark.parametrize(
    ("future_files", "pad_enabled"), [(False, True), (True, False), (True, True)]
)
@pytest.mark.usefixtures("vault")
def test_pad_seeded_when_served(
    monkeypatch: pytest.MonkeyPatch, future_files: bool, pad_enabled: bool
) -> None:
    """Pad gate = ``future_files`` OR ``future_pad_enabled`` (``futuresync.pad_on``)."""
    _with(monkeypatch, future_files=future_files, future_pad_enabled=pad_enabled)
    assert obsidian.run_setup() == 0
    cfg = config.load_config()
    assert Path(cfg.future_pad).expanduser().is_file()
    # Still no job folders: only the pad.
    assert not Path(cfg.future_dir).expanduser().exists()
    assert not Path(cfg.delete_dir).expanduser().exists()


# ------------------------------ dashboards ------------------------------ #
def test_dashboards_render_from_config_paths(vault: Path) -> None:
    obsidian.run_setup()
    running_md = _tasks(vault) / "running.md"
    assert running_md.is_file()
    text = running_md.read_text(encoding="utf-8")
    # Folder query + binary come from config, not hardcoded/personal values.
    assert 'const FOLDER = "01-llm/ccc-tasks/running"' in text
    assert '"/opt/ccc"' in text
    assert "{{CCC_BIN}}" not in text and "{{RUNNING_FOLDER}}" not in text
    assert obsidian.has_marker(text)
    for _tmpl, dest in obsidian.dashboard_targets(config.load_config()):
        assert dest.is_file()


def test_job_dashboards_are_retired(vault: Path) -> None:
    """tp#2266 B12: no ``future.md`` / ``delete.md`` target, asset or seeded file."""
    templates = {tmpl for tmpl, _ in obsidian.dashboard_targets(config.load_config())}
    assert templates == {"running.md.tmpl", "parked.md.tmpl"}
    obsidian.run_setup()
    assert not (_tasks(vault) / "future.md").exists()
    assert not (_tasks(vault) / "delete" / "delete.md").exists()
    with pytest.raises(FileNotFoundError):
        obsidian.render_dashboard("future.md.tmpl", config.load_config(), vault, "/opt/ccc")


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
    dest = _tasks(vault) / "running.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("# my own note, no marker\n", encoding="utf-8")
    obsidian.run_setup()
    # A file without the marker is left untouched.
    assert dest.read_text(encoding="utf-8") == "# my own note, no marker\n"


def test_rerun_dashboard_is_idempotent(vault: Path) -> None:
    obsidian.run_setup()
    dest = _tasks(vault) / "running.md"
    before = dest.stat().st_mtime_ns
    obsidian.run_setup()
    assert dest.stat().st_mtime_ns == before
    assert not list(_tasks(vault).glob("running.md.ccc-backup-*"))


# ------------------------------ shellcommands ------------------------------ #
def _plugin_dir(vault: Path) -> Path:
    return vault / ".obsidian" / "plugins" / "obsidian-shellcommands"


def _seed_shellcmds(vault: Path, entries: list[dict]) -> Path:
    plugin_dir = _plugin_dir(vault)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    data_path = plugin_dir / "data.json"
    data_path.write_text(
        json.dumps({"settings_version": "0.23.0", "shell_commands": entries}), encoding="utf-8"
    )
    return data_path


@pytest.mark.parametrize("uninstall", [False, True])
def test_setup_strips_retired_job_buttons_keeps_foreign(vault: Path, uninstall: bool) -> None:
    entries = [{"id": "foreign-thing"}, *({"id": i, "alias": "old"} for i in sorted(_RETIRED_IDS))]
    data_path = _seed_shellcmds(vault, entries)
    assert obsidian.run_setup(uninstall=uninstall) == 0
    data = json.loads(data_path.read_text(encoding="utf-8"))
    assert [e["id"] for e in data["shell_commands"]] == ["foreign-thing"]
    assert data["settings_version"] == "0.23.0"
    assert len(list(_plugin_dir(vault).glob("data.json.ccc-backup-*"))) == 1


def test_setup_never_adds_shellcommands(vault: Path) -> None:
    """A clean data.json is left byte-identical; no ccc entry is ever merged in."""
    data_path = _seed_shellcmds(vault, [{"id": "foreign-thing"}])
    before = data_path.read_text(encoding="utf-8")
    obsidian.run_setup()
    obsidian.run_setup()
    assert data_path.read_text(encoding="utf-8") == before
    assert not list(_plugin_dir(vault).glob("data.json.ccc-backup-*"))


def test_setup_creates_no_shellcommands_config(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert obsidian.run_setup() == 0
    out = capsys.readouterr().out
    assert "shellcommands" not in out
    assert not _plugin_dir(vault).exists()


# ------------------------------ dry-run + uninstall ------------------------------ #
def test_dry_run_writes_nothing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _with(monkeypatch, future_pad_enabled=True)
    data_path = _seed_shellcmds(vault, [{"id": "ccc-start-job-from-file"}])
    before = data_path.read_text(encoding="utf-8")
    assert obsidian.run_setup(dry_run=True) == 0
    assert not (_tasks(vault) / "running.md").exists()
    assert not (_tasks(vault) / "running").exists()
    assert not Path(config.load_config().future_pad).expanduser().exists()
    assert data_path.read_text(encoding="utf-8") == before


def test_uninstall_removes_only_marked(vault: Path) -> None:
    obsidian.run_setup()
    # An unmarked user file at a dashboard path is not removed.
    parked = _tasks(vault) / "parked.md"
    parked.write_text("mine, no marker\n", encoding="utf-8")
    assert obsidian.run_setup(uninstall=True) == 0
    assert not (_tasks(vault) / "running.md").exists()
    assert parked.exists()


# ------------------------------ pure helpers ------------------------------ #
def test_retired_entry_ids() -> None:
    assert obsidian.retired_entry_ids() == _RETIRED_IDS


def test_strip_shellcommands_strips_ours_only_and_is_idempotent() -> None:
    data = {
        "settings_version": "0.23.0",
        "shell_commands": [{"id": "foreign"}, *({"id": i} for i in sorted(_RETIRED_IDS))],
    }
    stripped = obsidian.strip_shellcommands(data)
    assert stripped == {"settings_version": "0.23.0", "shell_commands": [{"id": "foreign"}]}
    assert obsidian.strip_shellcommands(stripped) == stripped
    assert len(data["shell_commands"]) == 5  # input not mutated
    assert obsidian.strip_shellcommands({"other": 1}) == {"other": 1}
