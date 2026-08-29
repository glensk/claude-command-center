#!/usr/bin/env python3
"""Generate / install the launchd agent that runs ``ccc daemon`` periodically.

macOS only. ``install()`` writes ``~/Library/LaunchAgents/<label>.plist`` and
loads it; ``uninstall()`` unloads and removes it. The plist sets an explicit
PATH so the daemon can find ``ccc``, ``pgrep``, ``osascript`` and ``claude``
under launchd's minimal environment.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import shutil
import subprocess
from pathlib import Path

from . import config

# Uniform running/not-running badges, shared by the TUI help topic and Settings so
# every "is this process up?" indicator reads identically.
RUNNING_BADGE = "✅ (running)"
NOT_RUNNING_BADGE = "❌ (not running)"


def label(cfg: config.Config | None = None) -> str:
    """The launchd agent label for the periodic ``ccc daemon`` job (``launchd_label``)."""
    return (cfg or config.load_config()).launchd_label


def future_sync_label(cfg: config.Config | None = None) -> str:
    """Label for the WatchPaths future-sync agent (``<launchd_label>-future-sync``)."""
    return f"{label(cfg)}-future-sync"


def state_badge(running: bool) -> str:
    """The shared ``✅ (running)`` / ``❌ (not running)`` badge for a process state."""
    return RUNNING_BADGE if running else NOT_RUNNING_BADGE


def _plist_path(agent_label: str | None = None) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{agent_label or label()}.plist"


def _ccc_path() -> str:
    return shutil.which("ccc") or str(Path.home() / ".local" / "bin" / "ccc")


def _path_env() -> str:
    """The explicit PATH launchd agents run with (launchd's own env is minimal)."""
    return ":".join(
        [
            str(Path.home() / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )


def is_installed() -> bool:
    """True if the launchd agent plist is present (daemon auto-close enabled)."""
    return _plist_path().exists()


def is_loaded() -> bool:
    """True if the launchd agent is currently loaded (registered with ``launchctl``).

    Determined live via ``launchctl list <label>`` (exit 0 ⇒ loaded). The daemon is
    a periodic ``StartInterval`` job, so between passes it shows PID ``-`` in
    ``launchctl list`` — it is still loaded and scheduled, i.e. "running" in the
    sense that matters here. Returns False off macOS / when ``launchctl`` is absent.
    """
    if shutil.which("launchctl") is None:
        return False
    result = subprocess.run(
        ["launchctl", "list", label()], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def _override_env_xml() -> str:
    """Plist ``EnvironmentVariables`` entries for any CCC_HOME / CLAUDE_HOME override.

    Without these, a shell whose CCC_HOME/CLAUDE_HOME point elsewhere writes usage
    caches into one tree while the agent housekeeps another — so the daemon would sweep
    orphaned temps (:func:`usage.sweep_stale_temps`) in a directory the producer never
    touches. Empty when neither is set, which is the common single-tree case.
    """
    return "".join(
        f"\n        <key>{name}</key>\n        <string>{value}</string>"
        for name in ("CCC_HOME", "CLAUDE_HOME")
        if (value := os.environ.get(name))
    )


def plist_content(
    ccc_path: str, interval_sec: int, log_dir: Path, agent_label: str | None = None
) -> str:
    """Return the launchd plist XML for the periodic ``ccc daemon`` agent."""
    home = Path.home()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{agent_label or label()}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ccc_path}</string>
        <string>daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{_path_env()}</string>
        <key>HOME</key>
        <string>{home}</string>{_override_env_xml()}
    </dict>
    <key>StartInterval</key>
    <integer>{interval_sec}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir / "daemon.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / "daemon.err"}</string>
</dict>
</plist>
"""


def future_sync_plist_content(
    ccc_path: str, agent_label: str, watch_path: str, log_path: str
) -> str:
    """Return the launchd plist XML for the WatchPaths future-sync agent.

    This agent runs ``ccc sync-future`` whenever anything under *watch_path* changes,
    guarded so it never auto-commits or recurses into ccc's own hooks. Throttled to at
    most one run per 10s. Binary path, label, watch path and log path are resolved by
    :func:`future_sync_plist` at generation time.
    """
    home = Path.home()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{agent_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ccc_path}</string>
        <string>sync-future</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>{watch_path}</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CCC_INTERNAL</key>
        <string>1</string>
        <key>AI_NO_AUTOCOMMIT</key>
        <string>1</string>
        <key>PATH</key>
        <string>{_path_env()}</string>
        <key>HOME</key>
        <string>{home}</string>{_override_env_xml()}
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def future_sync_plist(cfg: config.Config | None = None) -> str:
    """Generate the future-sync WatchPaths agent plist from config.

    Label = ``<launchd_label>-future-sync``; watch path = the parent of ``future_dir``
    (the vault's task-files root, so any future/running/done edit triggers a sync);
    log = ``future-sync.log`` under the command-center home. The ``ccc`` binary is
    resolved on PATH at generation time.
    """
    cfg = cfg or config.load_config()
    watch_path = Path(cfg.future_dir).expanduser().parent
    log_path = config.app_home() / "future-sync.log"
    return future_sync_plist_content(
        _ccc_path(), future_sync_label(cfg), str(watch_path), str(log_path)
    )


def install() -> int:
    cfg = config.load_config()
    app = config.app_home()
    app.mkdir(parents=True, exist_ok=True)
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist_content(_ccc_path(), cfg.daemon_interval_sec, app), encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    result = subprocess.run(
        ["launchctl", "load", str(path)], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        print(f"installed and loaded launchd agent: {path}")
        print(f"  runs `ccc daemon` every {cfg.daemon_interval_sec}s; logs in {app}")
    else:
        print(f"wrote {path} but `launchctl load` failed:\n{result.stderr.strip()}")
        return 1
    return 0


def uninstall() -> int:
    path = _plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
        path.unlink()
        print(f"unloaded and removed {path}")
    else:
        print("launchd agent not installed")
    return 0
