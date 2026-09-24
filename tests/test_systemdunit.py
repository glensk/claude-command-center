"""systemd --user daemon units (the Linux launchd equivalent).

Pure content generators are asserted directly; install/uninstall flows run with
``systemctl`` and the unit dir redirected to a temp HOME (no real systemctl call).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import config, systemdunit


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


@pytest.fixture
def _fake_systemctl(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ``systemctl --user …`` invocation instead of running it."""
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "active"
        stderr = ""

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(systemdunit.subprocess, "run", fake_run)
    monkeypatch.setattr(systemdunit.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


# --------------------------------------------------------------------------- #
# pure content generators
# --------------------------------------------------------------------------- #
def test_service_content_is_oneshot_daemon() -> None:
    text = systemdunit.service_content("/opt/ccc", Path("/logs"))
    assert "Type=oneshot" in text
    assert "ExecStart=/opt/ccc daemon" in text
    assert "/logs/daemon.log" in text and "/logs/daemon.err" in text


def _systemd_parse_environment(line: str) -> str:
    """Decode one ``Environment=`` line the way systemd does (specifiers, then unquote+unescape)."""
    raw = line.removeprefix("Environment=").rstrip("\n").replace("%%", "%")
    assert raw.startswith('"') and raw.endswith('"'), raw
    body, out, i = raw[1:-1], [], 0
    while i < len(body):
        ch = body[i]
        assert ch != '"', f"unescaped quote would end the word early: {raw}"
        if ch == "\\":
            nxt = body[i + 1]
            if nxt == "x":
                out.append(chr(int(body[i + 2 : i + 4], 16)))
                i += 4
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "value",
    [
        "/home/u/.ccc",
        "/home/u/My Drive/ccc",
        '/tmp/we"ird',
        "/tmp/back\\slash",
        "/tmp/100%done",
        "/tmp/tab\there",
        "/tmp/$HOME",
    ],
)
def test_service_content_escapes_override_env(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # tp#426: CCC_HOME/CLAUDE_HOME were interpolated raw, so a space, quote or %
    # split, mangled or broke the Environment= assignment.
    monkeypatch.setenv("CCC_HOME", value)
    monkeypatch.setenv("CLAUDE_HOME", value)
    text = systemdunit.service_content("/opt/ccc", Path("/logs"))
    lines = [ln for ln in text.splitlines() if ln.startswith("Environment=")]
    assert [_systemd_parse_environment(ln) for ln in lines] == [
        f"CCC_HOME={value}",
        f"CLAUDE_HOME={value}",
    ]


def test_service_content_omits_unset_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    assert "Environment=" not in systemdunit.service_content("/opt/ccc", Path("/logs"))


def test_timer_content_fires_every_interval() -> None:
    text = systemdunit.timer_content(300)
    assert "OnUnitActiveSec=300" in text
    assert "OnBootSec=300" in text
    assert "WantedBy=timers.target" in text


def test_future_sync_path_unit_watches_dir_and_targets_service() -> None:
    text = systemdunit.future_sync_path_content("/vault/01-llm-tasks")
    assert "PathModified=/vault/01-llm-tasks" in text
    assert "PathChanged=/vault/01-llm-tasks" in text
    assert f"Unit={systemdunit.future_sync_label()}.service" in text


def test_future_sync_service_is_guarded_oneshot() -> None:
    text = systemdunit.future_sync_service_content("/opt/ccc", "/logs/future-sync.log")
    assert "ExecStart=/opt/ccc sync-future" in text
    assert "Environment=CCC_INTERNAL=1" in text
    assert "Environment=AI_NO_AUTOCOMMIT=1" in text


def test_labels_derive_from_config() -> None:
    cfg = config.Config(launchd_label="com.example.ccc")
    assert systemdunit.label(cfg) == "com.example.ccc"
    assert systemdunit.future_sync_label(cfg) == "com.example.ccc-future-sync"


# --------------------------------------------------------------------------- #
# install / uninstall (systemctl mocked)
# --------------------------------------------------------------------------- #
def test_install_writes_units_and_enables_timer(
    _tmp_home: Path, _fake_systemctl: list[list[str]]
) -> None:
    assert systemdunit.install() == 0
    unit_dir = _tmp_home / ".config" / "systemd" / "user"
    lbl = systemdunit.label()
    assert (unit_dir / f"{lbl}.service").exists()
    assert (unit_dir / f"{lbl}.timer").exists()
    # daemon-reload + enable --now the timer were issued.
    assert ["systemctl", "--user", "daemon-reload"] in _fake_systemctl
    assert ["systemctl", "--user", "enable", "--now", f"{lbl}.timer"] in _fake_systemctl


def test_install_adds_path_unit_when_vault_features_on(
    _tmp_home: Path, _fake_systemctl: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.load_config()
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(**{**vars(cfg), "future_files": True})
    )
    assert systemdunit.install() == 0
    unit_dir = _tmp_home / ".config" / "systemd" / "user"
    fs = systemdunit.future_sync_label()
    assert (unit_dir / f"{fs}.path").exists()
    assert (unit_dir / f"{fs}.service").exists()
    assert ["systemctl", "--user", "enable", "--now", f"{fs}.path"] in _fake_systemctl


def test_install_no_path_unit_when_vault_off(
    _tmp_home: Path, _fake_systemctl: list[list[str]]
) -> None:
    systemdunit.install()  # default config: all vault features off
    unit_dir = _tmp_home / ".config" / "systemd" / "user"
    assert not (unit_dir / f"{systemdunit.future_sync_label()}.path").exists()


def test_uninstall_removes_units_and_disables(
    _tmp_home: Path, _fake_systemctl: list[list[str]]
) -> None:
    systemdunit.install()
    _fake_systemctl.clear()
    assert systemdunit.uninstall() == 0
    unit_dir = _tmp_home / ".config" / "systemd" / "user"
    lbl = systemdunit.label()
    assert not (unit_dir / f"{lbl}.timer").exists()
    assert not (unit_dir / f"{lbl}.service").exists()
    assert ["systemctl", "--user", "disable", "--now", f"{lbl}.timer"] in _fake_systemctl


def test_is_active_reads_systemctl(_fake_systemctl: list[list[str]]) -> None:
    assert systemdunit.is_active() is True  # fake systemctl returns stdout "active"


def test_is_active_false_without_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(systemdunit.shutil, "which", lambda _name: None)
    assert systemdunit.is_active() is False


def test_is_installed_reflects_files(_tmp_home: Path, _fake_systemctl: list[list[str]]) -> None:
    assert systemdunit.is_installed() is False
    systemdunit.install()
    assert systemdunit.is_installed() is True
