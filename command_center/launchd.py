#!/usr/bin/env python3
"""Generate / install the launchd agents that run ``ccc daemon`` and ``ccc quota -P``.

macOS only. ``install()`` writes ``~/Library/LaunchAgents/<label>.plist`` for the
periodic daemon AND ``<prefix>.ccc-quota-probe.plist`` for the hourly OpenCode free-tier
probe (:mod:`command_center.quota_probe`), and loads both; ``uninstall()`` unloads and
removes both. The plist sets an explicit
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


#: How often the quota-probe agent runs ``ccc quota -P``: one free request an hour.
QUOTA_PROBE_INTERVAL_SEC = 3600


def quota_probe_label(cfg: config.Config | None = None) -> str:
    """Label for the hourly probe agent: ``<launchd_label minus its last dot-segment>`` +
    ``.ccc-quota-probe`` (``com.example.claude-command-center`` →
    ``com.example.ccc-quota-probe``, under the same reverse-DNS prefix as the install)."""
    base = label(cfg)
    return f"{base.rpartition('.')[0] or base}.ccc-quota-probe"


def quota_probe_plist_path(cfg: config.Config | None = None) -> Path:
    """Where the probe agent's plist lives; its presence is what "installed" means."""
    return _plist_path(quota_probe_label(cfg))


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


def quota_probe_plist_content(
    ccc_path: str, agent_label: str, log_path: str, ai_bin: str = ""
) -> str:
    """Return the launchd plist XML for the hourly ``ccc quota -P`` agent.

    ``RunAtLoad`` is false: loading the agent must not spend a request, the first probe
    comes one interval later. *ai_bin* (resolved at generation time) is written into the
    environment as ``AI_BIN`` because launchd's minimal PATH rarely reaches ai.py,
    and without ai.py the probe could only ask config.toml's model, never the registry's.
    """
    home = Path.home()
    ai_env = f"\n        <key>AI_BIN</key>\n        <string>{ai_bin}</string>" if ai_bin else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{agent_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ccc_path}</string>
        <string>quota</string>
        <string>-P</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CCC_INTERNAL</key>
        <string>1</string>
        <key>AI_NO_AUTOCOMMIT</key>
        <string>1</string>
        <key>PATH</key>
        <string>{_path_env()}</string>
        <key>HOME</key>
        <string>{home}</string>{ai_env}{_override_env_xml()}
    </dict>
    <key>StartInterval</key>
    <integer>{QUOTA_PROBE_INTERVAL_SEC}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def quota_probe_plist(cfg: config.Config | None = None) -> str:
    """Generate the quota-probe agent plist from config (label, log, binaries)."""
    cfg = cfg or config.load_config()
    from . import external_deps  # pylint: disable=import-outside-toplevel  # capability-scoped

    return quota_probe_plist_content(
        _ccc_path(),
        quota_probe_label(cfg),
        str(config.app_home() / "quota-probe.log"),
        external_deps.ai_exe() or "",
    )


def _write_and_load(path: Path, content: str) -> subprocess.CompletedProcess[str]:
    """Write *path* and (re)load it with ``launchctl``; the ``load`` result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    return subprocess.run(
        ["launchctl", "load", str(path)], capture_output=True, text=True, check=False
    )


def install() -> int:
    cfg = config.load_config()
    app = config.app_home()
    app.mkdir(parents=True, exist_ok=True)
    path = _plist_path()
    result = _write_and_load(path, plist_content(_ccc_path(), cfg.daemon_interval_sec, app))
    if result.returncode == 0:
        print(f"installed and loaded launchd agent: {path}")
        print(f"  runs `ccc daemon` every {cfg.daemon_interval_sec}s; logs in {app}")
    else:
        print(f"wrote {path} but `launchctl load` failed:\n{result.stderr.strip()}")
        return 1
    probe_path = quota_probe_plist_path(cfg)
    result = _write_and_load(probe_path, quota_probe_plist(cfg))
    if result.returncode != 0:
        print(f"wrote {probe_path} but `launchctl load` failed:\n{result.stderr.strip()}")
        return 1
    print(f"installed and loaded launchd agent: {probe_path}")
    print(f"  runs `ccc quota -P` every {QUOTA_PROBE_INTERVAL_SEC}s (not at load)")
    return 0


def uninstall() -> int:
    removed = False
    for path in (_plist_path(), quota_probe_plist_path()):
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
            path.unlink()
            print(f"unloaded and removed {path}")
            removed = True
    if not removed:
        print("launchd agent not installed")
    return 0
