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
    raw = _systemd_unspec(line.removeprefix("Environment=").rstrip("\n"))
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
        "/tmp/it's",
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


def _systemd_unspec(raw: str) -> str:
    """Resolve ``%`` the way systemd does: ``%%`` is a literal, any other ``%x`` a specifier."""
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "%":
            assert raw[i + 1 : i + 2] == "%", f"unescaped % would be read as a specifier: {raw}"
            i += 2
            out.append("%")
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _systemd_split_exec(line: str) -> list[str]:
    """Split one ``ExecStart=`` line into argv like systemd (specifiers, then shell-like words)."""
    raw = _systemd_unspec(line.removeprefix("ExecStart="))
    words: list[str] = []
    word: list[str] | None = None
    quote, i = "", 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            nxt = raw[i + 1]
            if nxt == "x":
                ch, i = chr(int(raw[i + 2 : i + 4], 16)), i + 4
            else:
                ch, i = nxt, i + 2
            word = (word or []) + [ch]
            continue
        i += 1
        if quote:
            if ch == quote:
                quote = ""
            else:
                word = (word or []) + [ch]
        elif ch in "\"'":
            quote, word = ch, word or []
        elif ch.isspace():
            if word is not None:
                words.append("".join(word))
            word = None
        else:
            word = (word or []) + [ch]
    assert not quote, f"unterminated quote: {line}"
    if word is not None:
        words.append("".join(word))
    return words


_ODD_PATHS = [
    "/opt/ccc",
    "/home/u/My Drive/bin/ccc",
    '/tmp/we"ird/ccc',
    "/tmp/it's/ccc",
    "/tmp/back\\slash/ccc",
    "/tmp/100%done/ccc",
    "/tmp/$HOME/ccc",
]


def _line(text: str, key: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith(key))


@pytest.mark.parametrize("ccc_path", _ODD_PATHS)
def test_exec_start_keeps_ccc_path_one_word(ccc_path: str) -> None:
    # tp#429: ExecStart= interpolated ccc_path raw, so a space split argv and a % was
    # read as a specifier.
    daemon = systemdunit.service_content(ccc_path, Path("/logs"))
    assert _systemd_split_exec(_line(daemon, "ExecStart=")) == [ccc_path, "daemon"]
    sync = systemdunit.future_sync_service_content(ccc_path, "/logs/f.log")
    assert _systemd_split_exec(_line(sync, "ExecStart=")) == [ccc_path, "sync-future"]


@pytest.mark.parametrize("directory", ["/logs", "/home/u/My Drive/100%/logs", "/tmp/we\"i'rd"])
def test_append_and_path_units_escape_percent(directory: str) -> None:
    # tp#429: these settings take the path literally after specifier expansion, so % is
    # the only character that needs escaping (and must be doubled).
    daemon = systemdunit.service_content("/opt/ccc", Path(directory))
    for key, name in (
        ("StandardOutput=append:", "daemon.log"),
        ("StandardError=append:", "daemon.err"),
    ):
        assert _systemd_unspec(_line(daemon, key).removeprefix(key)) == f"{directory}/{name}"
    sync = systemdunit.future_sync_service_content("/opt/ccc", f"{directory}/f.log")
    for key in ("StandardOutput=append:", "StandardError=append:"):
        assert _systemd_unspec(_line(sync, key).removeprefix(key)) == f"{directory}/f.log"
    path_unit = systemdunit.future_sync_path_content(directory)
    for key in ("PathModified=", "PathChanged="):
        assert _systemd_unspec(_line(path_unit, key).removeprefix(key)) == directory


@pytest.mark.parametrize("bad", ["/tmp/new\nline", "/tmp/trailing\\", "/tmp/sp ", " /tmp/sp"])
def test_unrepresentable_literal_paths_raise(bad: str) -> None:
    # A newline would inject a new unit directive; a trailing backslash continues the line.
    with pytest.raises(ValueError):
        systemdunit.future_sync_path_content(bad)
    with pytest.raises(ValueError):
        systemdunit.future_sync_service_content("/opt/ccc", bad)


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


# app home only ever appears as a middle component (…/daemon.log), so only a
# control character makes it unrepresentable.
@pytest.mark.parametrize("bad", ["new\nline", "tab\there"])
def test_install_unrepresentable_app_home_returns_1(
    bad: str,
    _tmp_home: Path,
    _fake_systemctl: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(config, "app_home", lambda: _tmp_home / bad)
    assert systemdunit.install() == 1
    assert "cannot install systemd user units" in capsys.readouterr().out
    unit_dir = _tmp_home / ".config" / "systemd" / "user"
    assert not any(unit_dir.iterdir())  # nothing half-written
    assert _fake_systemctl == []


def test_install_unrepresentable_future_dir_returns_1(
    _tmp_home: Path, _fake_systemctl: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.load_config()
    bad_dir = str(_tmp_home / "vault " / "01-llm-tasks")  # parent ends in a space
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: config.Config(**{**vars(cfg), "future_files": True, "future_dir": bad_dir}),
    )
    assert systemdunit.install() == 1
    unit_dir = _tmp_home / ".config" / "systemd" / "user"
    assert not any(unit_dir.iterdir())
    assert _fake_systemctl == []
