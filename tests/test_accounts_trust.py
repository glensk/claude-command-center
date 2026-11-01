"""`accounts.ensure_trusted` — pre-trusting the launch cwd per account (2026-09-01)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from command_center import accounts


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def test_default_account_trust_lives_next_to_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude Code's own lookup with CLAUDE_CONFIG_DIR unset: $HOME/.claude.json, the
    # sibling of ~/.claude — the one file is_trusted reads back (tp#1512).
    home = Path.home() / ".claude"
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    cfg = home.parent / ".claude.json"
    assert tmp_path in cfg.parents  # the suite's $HOME pin
    _write(cfg, {"theme": "dark", "projects": {"/repo/x": {"allowedTools": []}}})
    assert accounts.ensure_trusted(str(home), "/repo/x") is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["projects"]["/repo/x"] == {"allowedTools": [], "hasTrustDialogAccepted": True}
    assert accounts.ensure_trusted(str(home), "/repo/x") is False  # idempotent


def test_other_account_trust_lives_inside_its_config_dir(tmp_path: Path) -> None:
    work = tmp_path / ".claude-work"
    _write(work / ".claude.json", {"projects": {}})
    assert accounts.ensure_trusted(str(work), "/repo/y") is True
    data = json.loads((work / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"]["/repo/y"] == {"hasTrustDialogAccepted": True}


def test_missing_config_is_left_alone(tmp_path: Path) -> None:
    assert accounts.ensure_trusted(str(tmp_path / "never-ran"), "/repo/z") is False
    assert not (tmp_path / "never-ran" / ".claude.json").exists()


def test_apply_to_environ_trusts_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / ".claude-work"
    _write(work / ".claude.json", {"projects": {}})
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    accounts.apply_to_environ(str(work))
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(work) or os.environ["CLAUDE_CONFIG_DIR"].endswith(
        ".claude-work"
    )
    data = json.loads((work / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"][str(repo.resolve())] == {"hasTrustDialogAccepted": True}


def test_empty_config_dir_trusts_the_default_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unstamped row (``config_dir == ""``) is the default account, not the cwd's parent."""
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    cfg = Path.home() / ".claude.json"  # $HOME's, whatever CLAUDE_HOME says (tp#1512)
    _write(cfg, {"projects": {}})
    repo = tmp_path / "sub" / "repo"
    repo.mkdir(parents=True)
    stray = repo.parent / ".claude.json"  # where ``_resolve("")`` used to point
    _write(stray, {"projects": {}})
    monkeypatch.chdir(repo)
    assert accounts.ensure_trusted("") is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["projects"][str(repo.resolve())] == {"hasTrustDialogAccepted": True}
    assert json.loads(stray.read_text(encoding="utf-8")) == {"projects": {}}


def test_is_trusted_reads_the_default_account_for_an_empty_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "" is the default account for BOTH halves: is_trusted used to resolve "" to the
    # process cwd and read <cwd>/.claude.json, so ensure_trusted("") + is_trusted("")
    # disagreed wherever the cwd was not $HOME.
    home = Path.home()
    monkeypatch.setenv("CLAUDE_HOME", str(home / ".claude"))
    _write(home / ".claude.json", {"projects": {}})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert accounts.ensure_trusted("", str(tmp_path)) is True
    assert accounts.is_trusted("", str(tmp_path)) is True


# ------------------------------------------- tp#1512: the registration grant


def _projects() -> dict:
    data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    return data.get("projects", {})


def test_registration_grant_refuses_root_home_and_tmp_top() -> None:
    lines: list[str] = []
    for folder in ("/", str(Path.home()), "/tmp", "/private/tmp", "/var/folders"):
        assert accounts.grant_registration_trust("", folder, emit=lines.append) is False
    assert len(lines) == 5 and all(line.startswith("⚠ not trusting ") for line in lines)
    assert "the filesystem root" in lines[0] and "the home directory" in lines[1]
    assert "the top of /tmp" in lines[2] and "the top of /tmp" in lines[3]
    assert _projects() == {}  # nothing written


def test_registration_grant_refuses_the_per_user_var_folders_temp_root() -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()  # /private/var/folders/xx/yy/T on macOS
    if "folders" not in temp_root.parts:
        pytest.skip("no /var/folders temp root on this platform")
    lines: list[str] = []
    assert accounts.grant_registration_trust("", temp_root, emit=lines.append) is False
    assert "/var/folders temp root" in lines[0]


def test_registration_grant_refuses_a_world_writable_dir(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    lines: list[str] = []
    assert accounts.grant_registration_trust("", shared, emit=lines.append) is False
    assert lines == [f"⚠ not trusting {shared.resolve()} for private: world-writable"]
    missing = tmp_path / "missing"
    assert accounts.grant_registration_trust("", missing, emit=lines.append) is False
    assert "not an existing directory" in lines[-1]
    assert _projects() == {}


def test_registration_grant_allows_a_mktemp_subdir(tmp_path: Path) -> None:
    job = Path(tempfile.mkdtemp(dir=tmp_path))
    lines: list[str] = []
    assert accounts.grant_registration_trust("", job, emit=lines.append) is True
    assert lines == [f"✅ trusted {job.resolve()} for private"]
    assert accounts.is_trusted("", job)


def test_registration_grant_without_a_claude_json_warns(tmp_path: Path) -> None:
    (Path.home() / ".claude.json").unlink()
    lines: list[str] = []
    assert accounts.grant_registration_trust("", tmp_path, emit=lines.append) is False
    assert "has no .claude.json; log in once with it" in lines[0]


def test_is_trusted_ancestors_follows_claude_codes_walk(tmp_path: Path) -> None:
    """A trusted parent counts, bounded by the git root; ``/`` and ``$HOME`` exact only."""
    outer = tmp_path / "outer"
    repo = outer / "repo"
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    accounts.ensure_trusted("", outer)
    assert not accounts.is_trusted("", sub, ancestors=True)  # above the git root
    accounts.ensure_trusted("", repo)
    assert accounts.is_trusted("", sub, ancestors=True)  # the repo root counts
    assert not accounts.is_trusted("", sub)  # exact-key lookup unchanged
    plain = tmp_path / "plain" / "deep"
    plain.mkdir(parents=True)
    accounts.ensure_trusted("", tmp_path / "plain")
    assert accounts.is_trusted("", plain, ancestors=True)  # no repo: walks up


def test_is_trusted_never_inherits_root_or_home(tmp_path: Path) -> None:
    accounts.ensure_trusted("", "/")
    accounts.ensure_trusted("", Path.home())
    inside_home = Path.home() / "work"
    inside_home.mkdir()
    assert not accounts.is_trusted("", inside_home, ancestors=True)
    assert not accounts.is_trusted("", tmp_path, ancestors=True)
    assert accounts.is_trusted("", "/", ancestors=True)  # exact key still counts
