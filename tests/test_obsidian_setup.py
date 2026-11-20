"""``ccc obsidian-setup`` — the capture pad and the retired-entry strip.

The autouse ``_isolate_vault_dirs`` conftest fixture already points every loaded config's
vault dirs at ``tmp_path/vault``; these tests create that vault and drive :func:`run_setup`
against it. Future jobs live in ccc's store (tp#2266 B12) and the mirror export is gone
(B10): setup seeds no ``future/`` / ``delete/`` / mirror folders, no dashboards and no
job-button shellcommands; it seeds the capture pad only when the pad is served, and strips
the retired job-button entries from an existing shellcommands ``data.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import config, obsidian

_RETIRED_IDS = {
    "ccc-start-job-from-file",
    "ccc-done-job-from-file",
    "ccc-delete-job-from-file",
    "ccc-restore-job-from-file",
}


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
def test_default_run_seeds_no_folder_and_no_dashboard(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with(monkeypatch, future_files=False, future_pad_enabled=False)
    assert obsidian.run_setup() == 0
    cfg = config.load_config()
    assert not Path(cfg.future_dir).expanduser().exists()
    assert not Path(cfg.delete_dir).expanduser().exists()
    # The pad is not served → not seeded; no mirror folder or dashboard either.
    assert not Path(cfg.future_pad).expanduser().exists()
    assert not (_tasks(vault) / "running").exists()
    assert not (_tasks(vault) / "running.md").exists()
    assert not (_tasks(vault) / "parked.md").exists()
    assert not hasattr(obsidian, "task_dirs")
    assert not hasattr(obsidian, "render_dashboard")


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


def test_uninstall_seeds_nothing_and_keeps_legacy_dashboards(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-u seeds no pad; a legacy dashboard is left for tools/retire_vault_files.py."""
    _with(monkeypatch, future_pad_enabled=True)
    legacy = _tasks(vault) / "running.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("---\nccc_generated: true\n---\n", encoding="utf-8")
    assert obsidian.run_setup(uninstall=True) == 0
    assert legacy.exists()
    assert not Path(config.load_config().future_pad).expanduser().exists()


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
