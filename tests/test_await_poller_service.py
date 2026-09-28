"""The ``ccc await`` poller agent: launchd + systemd twins, service/doctor wiring, and
the daemon's backstop pass. ``launchctl`` / ``systemctl`` are always faked."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from command_center import await_eval, config, daemon, doctor, launchd, service, systemdunit


def _cfg(tmp_path: Path) -> config.Config:
    vault = tmp_path / "vault"
    return config.Config(
        launchd_label="com.test.ccc",
        vault_root=str(vault),
        future_dir=str(vault / "01-llm-tasks" / "future"),
    )


# --------------------------------------------------------------------------- launchd
def test_poller_plist_shape(tmp_path: Path) -> None:
    plist = plistlib.loads(
        launchd.await_poller_plist_content("/opt/ccc", "com.test.ccc.await", "/l.log").encode()
    )
    assert plist["Label"] == "com.test.ccc.await"
    assert plist["ProgramArguments"] == ["/opt/ccc", "await", "-r"]
    assert plist["StartInterval"] == 60
    assert plist["RunAtLoad"] is False
    assert plist["EnvironmentVariables"]["CCC_INTERNAL"] == "1"
    assert plist["EnvironmentVariables"]["AI_NO_AUTOCOMMIT"] == "1"
    assert launchd.await_poller_label(_cfg(tmp_path)) == "com.test.ccc.await"


class _LaunchctlRun:
    """Fake ``subprocess.run`` for launchd: ``load`` of *failing* paths fails."""

    def __init__(self, failing: str = "") -> None:
        self.failing = failing
        self.calls: list[list[str]] = []
        self.loaded: set[str] = set()

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:2] == ["launchctl", "load"]:
            if self.failing and cmd[2].endswith(self.failing):
                return subprocess.CompletedProcess(cmd, 1, "", "Load failed: 5")
            self.loaded.add(Path(cmd[2]).stem)
        if cmd[:2] == ["launchctl", "list"]:
            return subprocess.CompletedProcess(cmd, 0 if cmd[2] in self.loaded else 113, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture(name="mac")
def mac_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = _LaunchctlRun()
    monkeypatch.setattr(launchd.subprocess, "run", fake)
    monkeypatch.setattr(launchd.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(launchd.Path, "home", classmethod(lambda cls: tmp_path / "userhome"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.setattr(launchd.config, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(launchd, "_ccc_path", lambda: "/opt/ccc")
    monkeypatch.setattr(launchd, "quota_probe_plist", lambda cfg=None: "<plist/>")
    return {"fake": fake, "agents": tmp_path / "userhome" / "Library" / "LaunchAgents"}


def test_launchd_install_adds_the_poller(mac: dict[str, Any]) -> None:
    assert launchd.install() == 0
    assert (mac["agents"] / "com.test.ccc.await.plist").exists()
    assert launchd.await_poller_installed() and launchd.await_poller_loaded()


def test_launchd_poller_load_failure_is_reported_independently(
    mac: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    mac["fake"].failing = "com.test.ccc.await.plist"
    assert launchd.install() == 1
    out = capsys.readouterr().out
    assert "com.test.ccc.await.plist" in out and "backstop" in out
    assert launchd.is_loaded()  # the daemon itself did install
    assert launchd.await_poller_installed() and not launchd.await_poller_loaded()


@pytest.mark.usefixtures("mac")
def test_launchd_uninstall_removes_the_poller() -> None:
    launchd.install()
    assert launchd.uninstall() == 0
    assert not launchd.await_poller_installed()


def test_service_status_reports_both_agents(
    mac: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    mac["fake"].loaded = {"com.test.ccc"}
    assert service.status() == 1  # the poller is not loaded
    out = capsys.readouterr().out
    assert "com.test.ccc.await" in out and launchd.NOT_RUNNING_BADGE in out
    mac["fake"].loaded.add("com.test.ccc.await")
    assert service.status() == 0
    assert service.poller_active() and not service.poller_installed()


# --------------------------------------------------------------------------- systemd
class _Systemctl:
    def __init__(self, fail_poller: bool = False) -> None:
        self.fail_poller = fail_poller
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        rc = 1 if self.fail_poller and cmd[-1].endswith(".await.timer") else 0
        return subprocess.CompletedProcess(cmd, rc, "active", "boom" if rc else "")


@pytest.fixture(name="linux")
def linux_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    fake = _Systemctl()
    monkeypatch.setattr(systemdunit.subprocess, "run", fake)
    monkeypatch.setattr(systemdunit.shutil, "which", lambda name: f"/usr/bin/{name}")
    return {"fake": fake, "units": tmp_path / ".config" / "systemd" / "user"}


def test_poller_service_is_a_guarded_oneshot() -> None:
    unit = systemdunit.await_poller_service_content("/opt/ccc", "/l.log")
    assert "Type=oneshot" in unit
    assert "ExecStart=/opt/ccc await -r" in unit
    assert "Environment=CCC_INTERNAL=1" in unit


def test_systemd_install_adds_poller_units(linux: dict[str, Any]) -> None:
    assert systemdunit.install() == 0
    lbl = systemdunit.await_poller_label()
    assert (linux["units"] / f"{lbl}.service").exists()
    timer = (linux["units"] / f"{lbl}.timer").read_text()
    assert "OnUnitActiveSec=60" in timer
    assert ["systemctl", "--user", "enable", "--now", f"{lbl}.timer"] in linux["fake"].calls
    assert systemdunit.poller_installed() and systemdunit.poller_active()


def test_systemd_poller_failure_is_reported_independently(
    linux: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    linux["fake"].fail_poller = True
    assert systemdunit.install() == 1
    assert "await poller timer failed" in capsys.readouterr().out
    assert systemdunit.is_installed()  # the daemon units are in place


def test_systemd_uninstall_removes_poller_units(linux: dict[str, Any]) -> None:
    systemdunit.install()
    assert systemdunit.uninstall() == 0
    assert not systemdunit.poller_installed()
    lbl = systemdunit.await_poller_label()
    assert ["systemctl", "--user", "disable", "--now", f"{lbl}.timer"] in linux["fake"].calls


def test_systemd_status_includes_the_poller(
    linux: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    systemdunit.status()
    lbl = systemdunit.await_poller_label()
    assert ["systemctl", "--user", "status", f"{lbl}.timer"] in linux["fake"].calls
    capsys.readouterr()


# --------------------------------------------------------------------------- doctor
@pytest.mark.parametrize(
    ("active", "installed", "verdict", "needle"),
    [
        (True, True, doctor.OK, "every 60 s"),
        (False, True, doctor.FAIL, "not loaded"),
        (False, False, doctor.FAIL, "backstop"),
    ],
)
def test_doctor_poller_row(
    monkeypatch: pytest.MonkeyPatch, active: bool, installed: bool, verdict: str, needle: str
) -> None:
    monkeypatch.setattr(service, "poller_active", lambda cfg=None: active)
    monkeypatch.setattr(service, "poller_installed", lambda cfg=None: installed)
    check = doctor._await_poller_check()  # noqa: SLF001  # pylint: disable=protected-access
    assert check.status == verdict and needle in check.detail


# --------------------------------------------------------------------------- daemon backstop
def test_daemon_backstop_runs_the_pass_and_records_fired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_pass(_store: Any, **kwargs: Any) -> await_eval.PassReport:
        seen.append(kwargs)
        return await_eval.PassReport(fired=[7])

    monkeypatch.setattr(await_eval, "run_pass", fake_pass)
    monkeypatch.setattr(await_eval, "config_notifier", lambda: lambda _t, _m: None)
    report = daemon.DaemonReport()
    daemon._run_await_backstop(object(), report, dry_run=False)  # type: ignore[arg-type]  # noqa: SLF001  # pylint: disable=protected-access
    assert report.awaited == [7] and not report.is_empty()
    daemon._run_await_backstop(object(), report, dry_run=True)  # type: ignore[arg-type]  # noqa: SLF001  # pylint: disable=protected-access
    assert seen[1]["dry_run"] is True and seen[1]["notifier"] is None


def test_daemon_backstop_contains_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(await_eval, "run_pass", boom)
    report = daemon.DaemonReport()
    daemon._run_await_backstop(object(), report, dry_run=True)  # type: ignore[arg-type]  # noqa: SLF001  # pylint: disable=protected-access
    assert not report.awaited
    assert "await pass failed" in capsys.readouterr().err


def test_run_once_calls_the_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        daemon, "_run_await_backstop", lambda _s, _r, dry_run: calls.append(dry_run)
    )
    daemon.run_once(
        dry_run=True, do_reap=False, do_summary=False, do_progress=False, do_alerts=False
    )
    assert calls == [True]
