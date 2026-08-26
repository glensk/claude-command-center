"""Generate / install the systemd USER units that run ``ccc daemon`` periodically.

Linux only — the systemd counterpart of :mod:`command_center.launchd`. ``install()``
writes ``~/.config/systemd/user/<label>.service`` + ``<label>.timer`` (a ``oneshot``
service triggered by an ``OnUnitActiveSec`` timer, the equivalent of launchd's
``StartInterval``) and, when vault features are on, a ``<label>-future-sync.path`` +
``.service`` pair — the systemd ``.path`` unit is the equivalent of launchd's
``WatchPaths``. All are enabled/started via ``systemctl --user``.

The content generators are pure (unit-testable on any platform); only the
``systemctl`` invocations touch the system, and tests mock those.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import config


def _unit_dir() -> Path:
    """The systemd USER unit directory (``~/.config/systemd/user``)."""
    return Path.home() / ".config" / "systemd" / "user"


def label(cfg: config.Config | None = None) -> str:
    """The systemd unit base name for the periodic ``ccc daemon`` job (``launchd_label``)."""
    return (cfg or config.load_config()).launchd_label


def future_sync_label(cfg: config.Config | None = None) -> str:
    """Base name for the future-sync path/service units (``<launchd_label>-future-sync``)."""
    return f"{label(cfg)}-future-sync"


def _ccc_path() -> str:
    return shutil.which("ccc") or str(Path.home() / ".local" / "bin" / "ccc")


def _service_path(cfg: config.Config | None = None) -> Path:
    return _unit_dir() / f"{label(cfg)}.service"


def _timer_path(cfg: config.Config | None = None) -> Path:
    return _unit_dir() / f"{label(cfg)}.timer"


def _fs_path_unit(cfg: config.Config | None = None) -> Path:
    return _unit_dir() / f"{future_sync_label(cfg)}.path"


def _fs_service_unit(cfg: config.Config | None = None) -> Path:
    return _unit_dir() / f"{future_sync_label(cfg)}.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl --user <args>`` (never raises; returns the completed process)."""
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def service_content(ccc_path: str, log_dir: Path) -> str:
    """Return the ``.service`` unit for one ``ccc daemon`` pass (``Type=oneshot``)."""
    # Same reason as launchd._override_env_xml: the daemon must housekeep the SAME tree
    # the status-line producer writes to, or orphaned usage temps are never reclaimed.
    env = "".join(
        f"Environment={name}={value}\n"
        for name in ("CCC_HOME", "CLAUDE_HOME")
        if (value := os.environ.get(name))
    )
    return f"""[Unit]
Description=ccc command center daemon (one pass)
After=default.target

[Service]
Type=oneshot
{env}ExecStart={ccc_path} daemon
StandardOutput=append:{log_dir / "daemon.log"}
StandardError=append:{log_dir / "daemon.err"}
"""


def timer_content(interval_sec: int) -> str:
    """Return the ``.timer`` unit firing the daemon service every *interval_sec* seconds."""
    return f"""[Unit]
Description=ccc command center daemon timer (every {interval_sec}s)

[Timer]
OnBootSec={interval_sec}
OnUnitActiveSec={interval_sec}
Persistent=true

[Install]
WantedBy=timers.target
"""


def future_sync_path_content(watch_path: str) -> str:
    """Return the ``.path`` unit watching *watch_path* (launchd ``WatchPaths`` equivalent).

    Triggers the sibling ``<label>-future-sync.service`` when the watched directory
    changes. systemd path units watch the named directory (not recursively) — the
    daemon's periodic pass covers deeper edits; see docs/linux.md.
    """
    return f"""[Unit]
Description=ccc future-sync watcher

[Path]
PathModified={watch_path}
PathChanged={watch_path}
Unit={future_sync_label()}.service

[Install]
WantedBy=default.target
"""


def future_sync_service_content(ccc_path: str, log_path: str) -> str:
    """Return the ``.service`` unit the future-sync path unit triggers (``ccc sync-future``).

    Guarded exactly like the launchd agent so it never auto-commits or recurses into
    ccc's own hooks (``CCC_INTERNAL`` / ``AI_NO_AUTOCOMMIT``).
    """
    return f"""[Unit]
Description=ccc future-sync (triggered by the path unit)

[Service]
Type=oneshot
Environment=CCC_INTERNAL=1
Environment=AI_NO_AUTOCOMMIT=1
ExecStart={ccc_path} sync-future
StandardOutput=append:{log_path}
StandardError=append:{log_path}
"""


def _vault_features_on(cfg: config.Config) -> bool:
    return cfg.future_files or cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions


def is_installed(cfg: config.Config | None = None) -> bool:
    """True if the daemon timer + service unit files are present."""
    return _service_path(cfg).exists() and _timer_path(cfg).exists()


def is_active(cfg: config.Config | None = None) -> bool:
    """True if the daemon ``.timer`` reports ``active`` (scheduled) to ``systemctl --user``.

    Returns False off Linux / when ``systemctl`` is absent.
    """
    if shutil.which("systemctl") is None:
        return False
    result = _systemctl("is-active", f"{label(cfg)}.timer")
    return result.stdout.strip() == "active"


def install() -> int:
    """Write + enable the daemon timer/service (and the future-sync path unit if vault-on)."""
    cfg = config.load_config()
    app = config.app_home()
    app.mkdir(parents=True, exist_ok=True)
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    ccc = _ccc_path()

    _service_path(cfg).write_text(service_content(ccc, app), encoding="utf-8")
    _timer_path(cfg).write_text(timer_content(cfg.daemon_interval_sec), encoding="utf-8")

    vault_on = _vault_features_on(cfg)
    if vault_on:
        watch_path = Path(cfg.future_dir).expanduser().parent
        log_path = app / "future-sync.log"
        _fs_path_unit(cfg).write_text(future_sync_path_content(str(watch_path)), encoding="utf-8")
        _fs_service_unit(cfg).write_text(
            future_sync_service_content(ccc, str(log_path)), encoding="utf-8"
        )

    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", f"{label(cfg)}.timer")
    if result.returncode != 0:
        print(f"wrote units but `systemctl --user enable --now` failed:\n{result.stderr.strip()}")
        return 1
    if vault_on:
        _systemctl("enable", "--now", f"{future_sync_label(cfg)}.path")
    print(f"installed and started systemd user units: {_timer_path(cfg)}")
    print(f"  runs `ccc daemon` every {cfg.daemon_interval_sec}s; logs in {app}")
    return 0


def uninstall() -> int:
    """Disable + remove every ccc systemd user unit (daemon timer/service + future-sync)."""
    cfg = config.load_config()
    _systemctl("disable", "--now", f"{label(cfg)}.timer")
    _systemctl("disable", "--now", f"{future_sync_label(cfg)}.path")
    removed = False
    for path in (
        _timer_path(cfg),
        _service_path(cfg),
        _fs_path_unit(cfg),
        _fs_service_unit(cfg),
    ):
        if path.exists():
            path.unlink()
            removed = True
    _systemctl("daemon-reload")
    print("removed ccc systemd user units" if removed else "systemd user units not installed")
    return 0


def status() -> int:
    """Print the daemon timer's ``systemctl --user status`` (informational)."""
    cfg = config.load_config()
    if shutil.which("systemctl") is None:
        print("systemctl not found (systemd user services unavailable)")
        return 1
    result = _systemctl("status", f"{label(cfg)}.timer")
    print(result.stdout.strip() or result.stderr.strip())
    return 0
