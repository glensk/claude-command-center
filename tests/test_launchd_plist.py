"""Tests for the generated WatchPaths future-sync launchd agent plist.

Unlike the periodic ``ccc daemon`` agent, the future-sync agent is WatchPaths-triggered.
It is now generated from config by :func:`command_center.launchd.future_sync_plist` (label,
watch path, log path and binary all resolved at generation time) rather than shipped as a
static hand-authored file. These tests pin the generated plist's shape and the keys the
launchd wiring depends on.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from command_center import config, launchd


def _cfg(tmp_path: Path) -> config.Config:
    vault = tmp_path / "vault"
    return config.Config(
        launchd_label="com.test.ccc",
        vault_root=str(vault),
        future_dir=str(vault / "01-llm-tasks" / "future"),
    )


@pytest.fixture
def plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    xml = launchd.future_sync_plist(_cfg(tmp_path))
    return plistlib.loads(xml.encode("utf-8"))


def test_plist_parses(plist: dict) -> None:
    assert isinstance(plist, dict)


def test_label_derives_from_config(plist: dict) -> None:
    # The future-sync label is the configured launchd_label plus a "-future-sync" suffix.
    assert plist["Label"] == "com.test.ccc-future-sync"
    assert launchd.future_sync_label(_cfg(Path("/tmp"))) == "com.test.ccc-future-sync"


def test_program_arguments_run_sync_future(plist: dict) -> None:
    args = plist["ProgramArguments"]
    assert args[1] == "sync-future"
    assert args[0].endswith("ccc")


def test_watch_paths_covers_future_task_root(tmp_path: Path, plist: dict) -> None:
    # Watches the parent of future_dir — the vault's task-files root — so any
    # future/running/done edit triggers a sync.
    assert plist["WatchPaths"] == [str(tmp_path / "vault" / "01-llm-tasks")]


def test_throttle_interval_set(plist: dict) -> None:
    assert plist["ThrottleInterval"] == 10


def test_override_env_values_are_xml_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    # tp#409: a CCC_HOME/CLAUDE_HOME holding & or < must still yield a parseable plist.
    monkeypatch.setenv("CCC_HOME", "/x/a&b")
    monkeypatch.setenv("CLAUDE_HOME", "/y/<c>")
    for xml in (
        launchd.plist_content("/bin/ccc", 60, Path("/tmp/logs"), "com.test.ccc"),
        launchd.future_sync_plist_content("/bin/ccc", "com.test.fs", "/w", "/l.log"),
        launchd.quota_probe_plist_content("/bin/ccc", "com.test.qp", "/l.log"),
    ):
        env = plistlib.loads(xml.encode("utf-8"))["EnvironmentVariables"]
        assert env["CCC_HOME"] == "/x/a&b"
        assert env["CLAUDE_HOME"] == "/y/<c>"


def test_run_at_load_true(plist: dict) -> None:
    assert plist["RunAtLoad"] is True


def test_environment_guards_present(plist: dict) -> None:
    env = plist["EnvironmentVariables"]
    assert env["CCC_INTERNAL"] == "1"
    assert env["AI_NO_AUTOCOMMIT"] == "1"
    assert "PATH" in env and "HOME" in env


def test_log_paths_under_command_center_home(tmp_path: Path, plist: dict) -> None:
    app_home = str(tmp_path / "home" / "command-center")
    assert plist["StandardOutPath"].startswith(app_home)
    assert plist["StandardErrorPath"].startswith(app_home)
    assert plist["StandardOutPath"].endswith("future-sync.log")


def test_daemon_plist_label_derives_from_config(tmp_path: Path) -> None:
    # The periodic daemon agent's label is likewise config-driven.
    xml = launchd.plist_content("/x/ccc", 300, tmp_path, agent_label="com.test.ccc")
    data = plistlib.loads(xml.encode("utf-8"))
    assert data["Label"] == "com.test.ccc"
    assert data["ProgramArguments"] == ["/x/ccc", "daemon"]


# ------------------------- resident panel server agent (tp#70 S5a) ------------------------- #
@pytest.fixture
def panel_plist(tmp_path: Path) -> dict:
    xml = launchd.panel_server_plist_content(
        "/opt/it's ccc/bin/ccc",
        launchd.panel_server_label(_cfg(tmp_path)),
        tmp_path / "app home",
        {"CCC_HOME": "/x/<&>"},
    )
    return plistlib.loads(xml.encode("utf-8"))


def test_panel_server_label_derives_from_config(tmp_path: Path, panel_plist: dict) -> None:
    assert launchd.panel_server_label(_cfg(tmp_path)) == "com.test.ccc-panel-server"
    assert panel_plist["Label"] == "com.test.ccc-panel-server"
    assert launchd.panel_server_plist_path(_cfg(tmp_path)).name == "com.test.ccc-panel-server.plist"


def test_panel_server_plist_shape(tmp_path: Path, panel_plist: dict) -> None:
    assert panel_plist["ProgramArguments"] == ["/opt/it's ccc/bin/ccc", "panel-server"]
    assert panel_plist["KeepAlive"] == {"SuccessfulExit": False}
    assert panel_plist["RunAtLoad"] is True
    assert panel_plist["LimitLoadToSessionType"] == "Aqua"
    assert panel_plist["ProcessType"] == "Interactive"
    env = panel_plist["EnvironmentVariables"]
    assert env["PATH"] and env["HOME"] and env["CCC_HOME"] == "/x/<&>"  # escaped, round-trips
    assert panel_plist["StandardOutPath"] == str(tmp_path / "app home" / "panel-server.log")
    assert panel_plist["StandardErrorPath"] == str(tmp_path / "app home" / "panel-server.err")


class _Launchctl:
    """Records every ``launchctl`` call; ``loaded`` drives ``print``'s exit code."""

    def __init__(self, loaded: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = loaded

    def __call__(self, *args: str):  # noqa: ANN204
        import subprocess

        self.calls.append(args)
        rc = 0
        if args[0] == "print":
            rc = 0 if self.loaded else 113
        elif args[0] == "bootstrap":
            self.loaded = True
        elif args[0] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(["launchctl", *args], rc, "", "")


@pytest.fixture
def fake_launchctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Launchctl:
    fake = _Launchctl()
    monkeypatch.setattr(launchd, "_launchctl", fake)
    monkeypatch.setattr(launchd.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: tmp_path / "userhome"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.setattr(launchd.config, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(launchd, "_ccc_path", lambda: "/opt/ccc")
    return fake


def test_panel_server_install_writes_plist_poker_and_bootstraps(
    tmp_path: Path, fake_launchctl: _Launchctl
) -> None:
    assert launchd.panel_server_install() == 0
    plist = launchd.panel_server_plist_path()
    assert plist.exists()
    poker = tmp_path / "claude" / "command-center" / "panel-poke.sh"
    assert poker.exists() and "COLD_CCC=/opt/ccc" in poker.read_text()
    verbs = [call[0] for call in fake_launchctl.calls]
    assert "bootstrap" in verbs
    assert launchd.panel_server_loaded()


def test_panel_server_stop_start_uninstall_and_purge(
    tmp_path: Path, fake_launchctl: _Launchctl
) -> None:
    launchd.panel_server_install()
    assert launchd.panel_server_stop() == 0 and not fake_launchctl.loaded
    assert launchd.panel_server_start() == 0 and fake_launchctl.loaded
    poker = tmp_path / "claude" / "command-center" / "panel-poke.sh"
    assert launchd.panel_server_uninstall() == 0
    assert not launchd.panel_server_plist_path().exists()
    assert poker.exists()  # kept: Karabiner may still point at it
    launchd.panel_server_install()
    assert launchd.panel_server_uninstall(purge=True) == 0
    assert not poker.exists()


def test_panel_server_start_without_install_fails(fake_launchctl: _Launchctl) -> None:
    assert launchd.panel_server_start() == 1
    assert not any(call[0] == "bootstrap" for call in fake_launchctl.calls)


def test_daemon_install_never_touches_the_panel_server(
    tmp_path: Path, fake_launchctl: _Launchctl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in contract: `ccc daemon --install` does not install the panel server."""
    import subprocess

    monkeypatch.setattr(
        launchd.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    monkeypatch.setattr(launchd, "quota_probe_plist", lambda cfg=None: "<plist/>")
    launchd.install()
    assert not launchd.panel_server_plist_path().exists()
    assert not (tmp_path / "claude" / "command-center" / "panel-poke.sh").exists()
