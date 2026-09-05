"""`ccc install-hooks` / `ccc install-statusline` — merge into settings.json safely.

Covers the merge algorithm (fresh file, foreign hooks preserved, rerun idempotence,
uninstall), dry-run writing nothing, symlink-target writes, and the statusline install
/ chain-script generation / uninstall-restore path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import install


@pytest.fixture(autouse=True)
def _claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    # Pin the ccc binary the generated commands reference, so tests are host-agnostic.
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")
    return tmp_path


def _load(home: Path) -> dict:
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


def _stop_commands(settings: dict) -> list[str]:
    return [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]


# ------------------------------ hooks: fresh install ------------------------------ #
def test_install_hooks_fresh_creates_all_events(_claude_home: Path) -> None:
    assert install.install_hooks() == 0
    settings = _load(_claude_home)
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "SessionEnd",
        "PreCompact",
        "SubagentStart",  # the in-process subagent counter switch-account reads
        "SubagentStop",
    ):
        assert event in settings["hooks"]
    # PreToolUse + PostToolUse carry their matcher objects.
    pre = settings["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    post_matchers = {g["matcher"] for g in settings["hooks"]["PostToolUse"]}
    assert post_matchers == {"Edit|Write|MultiEdit|NotebookEdit", "TodoWrite|TaskCreate|TaskUpdate"}
    start_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert start_cmd == "/opt/ccc hook session-start"


def test_install_hooks_release_locks_is_last_stop(_claude_home: Path) -> None:
    install.install_hooks()
    stops = _stop_commands(_load(_claude_home))
    assert stops[-1] == "/opt/ccc hook release-locks"
    assert "/opt/ccc hook stop" in stops


# ------------------------------ hooks: foreign preserved ------------------------------ #
def test_install_hooks_preserves_foreign_entries(_claude_home: Path) -> None:
    (_claude_home / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "/my/commit.sh"}]}],
                    "Notification": [{"hooks": [{"type": "command", "command": "/my/notify.sh"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    install.install_hooks()
    settings = _load(_claude_home)
    assert settings["model"] == "opus"  # untouched top-level key
    assert settings["hooks"]["Notification"][0]["hooks"][0]["command"] == "/my/notify.sh"
    stops = _stop_commands(settings)
    assert stops[0] == "/my/commit.sh"  # foreign Stop hook kept, and first
    assert stops[-1] == "/opt/ccc hook release-locks"  # ccc release-locks appended last


def test_install_hooks_is_idempotent(_claude_home: Path) -> None:
    install.install_hooks()
    first = (_claude_home / "settings.json").read_text(encoding="utf-8")
    install.install_hooks()
    install.install_hooks()
    assert (_claude_home / "settings.json").read_text(encoding="utf-8") == first


def test_install_hooks_rerun_after_ccc_path_change_replaces_not_duplicates(
    _claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install.install_hooks()
    monkeypatch.setattr(install, "ccc_binary", lambda: "/new/ccc")  # ccc moved
    install.install_hooks()
    settings = _load(_claude_home)
    stops = _stop_commands(settings)
    # Exactly the two ccc Stop entries, now with the new path — no stale /opt/ccc left.
    assert stops == ["/new/ccc hook stop", "/new/ccc hook release-locks"]


# ------------------------------ hooks: uninstall ------------------------------ #
def test_uninstall_removes_only_ccc_entries(_claude_home: Path) -> None:
    (_claude_home / "settings.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/my/commit.sh"}]}]}}
        ),
        encoding="utf-8",
    )
    install.install_hooks()
    install.install_hooks(uninstall=True)
    settings = _load(_claude_home)
    assert _stop_commands(settings) == ["/my/commit.sh"]
    # Events that were entirely ccc-owned are removed cleanly.
    assert "SessionStart" not in settings.get("hooks", {})


# ------------------------------ dry-run writes nothing ------------------------------ #
def test_dry_run_writes_nothing(_claude_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert not (_claude_home / "settings.json").exists()
    install.install_hooks(dry_run=True)
    out = capsys.readouterr().out
    assert "session-start" in out and "@@" in out  # a unified diff was printed
    assert not (_claude_home / "settings.json").exists()  # but nothing written


# ------------------------------ symlink-safe write ------------------------------ #
def test_write_through_symlink_keeps_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
    link = home / "settings.json"
    link.symlink_to(real)

    install.install_hooks()

    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert link.resolve() == real
    data = json.loads(real.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert "SessionStart" in data["hooks"]


def test_backup_written_before_overwrite(_claude_home: Path) -> None:
    (_claude_home / "settings.json").write_text('{"model": "opus"}\n', encoding="utf-8")
    install.install_hooks()
    backups = list(_claude_home.glob("settings.json.ccc-backup-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"model": "opus"}


# ------------------------------ statusline ------------------------------ #
def test_statusline_fresh_installs_direct(_claude_home: Path) -> None:
    assert install.install_statusline() == 0
    settings = _load(_claude_home)
    assert settings["statusLine"] == {
        "type": "command",
        "command": "/opt/ccc statusline --capture-usage",
    }
    assert install.statusline_state(settings) == "direct"


def test_statusline_foreign_refuses_without_chain(
    _claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (_claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "bash /my/sl.sh"}}),
        encoding="utf-8",
    )
    rc = install.install_statusline()
    assert rc == 1
    assert "already configured" in capsys.readouterr().out
    # Unchanged: still the foreign statusLine.
    assert _load(_claude_home)["statusLine"]["command"] == "bash /my/sl.sh"


def test_statusline_chain_generates_script_and_restores(_claude_home: Path) -> None:
    original = {"type": "command", "command": "bash /my/sl.sh", "refreshInterval": 3}
    (_claude_home / "settings.json").write_text(
        json.dumps({"statusLine": original}), encoding="utf-8"
    )
    assert install.install_statusline(chain=True) == 0
    settings = _load(_claude_home)
    chain = install.chain_script_path()
    assert chain.exists()
    assert settings["statusLine"]["command"] == f"bash {chain}"
    script = chain.read_text(encoding="utf-8")
    assert "run_original" in script and "statusline --capture-usage" in script
    assert "ccc-original-statusline:" in script  # records the original for uninstall

    # Uninstall restores the exact original object and removes the script.
    assert install.install_statusline(uninstall=True) == 0
    assert _load(_claude_home)["statusLine"] == original
    assert not chain.exists()


def test_statusline_chain_rerun_does_not_wrap_itself(_claude_home: Path) -> None:
    (_claude_home / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "bash /my/sl.sh"}}),
        encoding="utf-8",
    )
    install.install_statusline(chain=True)
    install.install_statusline(chain=True)  # rerun
    script = install.chain_script_path().read_text(encoding="utf-8")
    # The recorded original is still the FOREIGN command, not our own chain script.
    assert "/my/sl.sh" in script
    assert script.count("ccc-original-statusline:") == 1
    original = install._read_recorded_original(install.chain_script_path())
    assert original == {"type": "command", "command": "bash /my/sl.sh"}
