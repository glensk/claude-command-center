#!/usr/bin/env python3
"""``ccc doctor`` — a read-only health check of the ccc install and its environment.

Prints a sectioned ✅ / ❌ / − report (− = not applicable / feature disabled) and exits
0 when nothing is broken, 1 when any ❌ is present. It mutates nothing and works with no
config at all (a fresh machine) — it states what is missing without crashing.

The report is built by the pure :func:`build_report` (easy to test); :func:`run` renders
it and returns the exit code.
"""

# `_section_fast_path` reads `cli._HOT_SUBCOMMANDS` through a LAZY import, exactly as
# `cli.cmd_doctor` imports this module — two deliberate function-local edges, never an
# import-time cycle (pylint only sees the pair).
# pylint: disable=cyclic-import

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import config, install, scrub

# The command-text analysis behind "duplicate hook path" and the Spawn-fast-path section
# lives in :mod:`command_center.hookroutes` — the ``install-hooks`` preflight answers
# "does this foreign hook also spawn `ccc hook`?" from the same helpers, and neither of
# them may import the other. ``doctor._foreign_ccc_calls`` stays the address it was.
from .hookroutes import _foreign_ccc_calls, foreign_hook_routes
from .models import MirrorHealth

OK, FAIL, NA = "ok", "fail", "na"
_SYMBOL = {OK: "✅", FAIL: "❌", NA: "−"}

#: Claude Code's own hook timeout when an entry declares none (seconds). A Stop hook may
#: therefore run this long even where settings.json says nothing at all.
CLAUDE_HOOK_DEFAULT_TIMEOUT_SEC = 60
#: The managed-settings file an administrator can deploy — a hook scope ccc can neither
#: read nor enumerate. Module-level so tests can point them somewhere hermetic.
_MANAGED_SETTINGS_DARWIN = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
_MANAGED_SETTINGS_POSIX = Path("/etc/claude-code/managed-settings.json")


def _managed_settings_path() -> Path:
    """Where a managed-settings file would live on this platform."""
    return _MANAGED_SETTINGS_DARWIN if sys.platform == "darwin" else _MANAGED_SETTINGS_POSIX


@dataclass
class Check:
    status: str  # OK | FAIL | NA
    label: str
    detail: str = ""


@dataclass
class Section:
    title: str
    checks: list[Check] = field(default_factory=list)


@dataclass
class Report:
    sections: list[Section]

    @property
    def exit_code(self) -> int:
        """1 if any check failed, else 0."""
        return 1 if any(c.status == FAIL for s in self.sections for c in s.checks) else 0


def _claude_version() -> str:
    try:
        proc = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0] if text else ""


def _iterm2_present() -> bool:
    if sys.platform != "darwin":
        return False
    return any(
        (base / "iTerm.app").exists()
        for base in (Path("/Applications"), Path.home() / "Applications")
    )


def _section_core() -> Section:
    section = Section("Core")
    if shutil.which("claude"):
        version = _claude_version()
        section.checks.append(Check(OK, "claude CLI on PATH", version or "version unknown"))
    else:
        section.checks.append(Check(FAIL, "claude CLI on PATH", "not found — install Claude Code"))
    cfg_path = config.config_path()
    if cfg_path.exists():
        section.checks.append(Check(OK, "config.toml", str(cfg_path)))
    else:
        section.checks.append(Check(NA, "config.toml", "absent — using built-in defaults"))
    return section


def _section_wiring() -> Section:
    section = Section("Wiring (settings.json)")
    settings = install.load_settings()
    section.checks.append(_hooks_wired_check(settings))
    section.checks.append(_duplicate_hook_path_check(settings))

    state = install.statusline_state(settings)
    if state == "direct":
        section.checks.append(Check(OK, "statusline wired", "direct ccc command"))
    elif state == "chain":
        section.checks.append(Check(OK, "statusline wired", "chained after another statusline"))
    elif state == "foreign":
        section.checks.append(
            Check(
                FAIL,
                "statusline wired",
                "a non-ccc statusLine is set (ccc install-statusline --chain)",
            )
        )
    else:
        section.checks.append(Check(FAIL, "statusline wired", "none — run ccc install-statusline"))
    section.checks.append(_stop_hook_timeout_coverage_check(settings))
    return section


def _hooks_wired_check(settings: dict) -> Check:
    """Is EXACTLY ccc's wiring in place — every entry once, no extra ccc entry? (tp#222)

    Counted, not set-compared: two identical ``ccc hook stop`` entries are a double-run
    (Claude Code executes every entry wired on an event), and a set of hook-args cannot
    see the difference. Missing entries stay the louder finding — they mean ccc is half
    wired — so they are reported first.
    """
    wired = install.installed_hook_wiring(settings)
    if not wired:
        return Check(FAIL, "hooks wired", "none — run ccc install-hooks")
    expected: Counter[tuple[str, str | None, str]] = Counter(install.HOOK_SPEC)
    missing = expected - wired
    if missing:
        names = ", ".join(sorted({arg for _event, _matcher, arg in missing}))
        return Check(FAIL, "hooks wired", f"partial — missing: {names} (ccc install-hooks)")
    extra = wired - expected
    if extra:
        keys = sorted(extra, key=lambda k: (k[0], k[2], k[1] or ""))
        detail = ", ".join(
            f"{event}/{arg} ×{wired[(event, matcher, arg)]}" for event, matcher, arg in keys
        )
        return Check(
            FAIL, "hooks wired", f"duplicate/unexpected ccc entries: {detail} (ccc install-hooks)"
        )
    return Check(
        OK, "hooks wired", f"all {len(set(install.ALL_HOOK_ARGS))} ccc events, each exactly once"
    )


def _duplicate_hook_path_check(settings: dict) -> Check:
    """Is ccc's own wiring the ONLY path to ``ccc hook``? (tp#222)

    A hand-wired forwarder (``cc-hook.sh <event>`` whose body calls ``ccc hook "$1"``)
    is FOREIGN to the installer, which preserves foreign hooks by contract — so it sits
    next to ccc's entries and every event it is wired on runs ccc twice, with nothing
    saying so. ``install-hooks`` refuses to install next to one; this names it after the
    fact. A foreign command whose route cannot be resolved is − (informational).
    """
    label = "duplicate hook path"
    offenders, indeterminate = foreign_hook_routes(
        install.hook_commands(settings),
        install._is_ccc_hook_command,  # pylint: disable=protected-access
    )
    if offenders:
        return Check(
            FAIL,
            label,
            f"forwarder(s) to `ccc hook`: {', '.join(offenders)} — every event they carry "
            "runs ccc twice; remove them, ccc install-hooks owns the wiring",
        )
    if indeterminate:
        return Check(
            NA,
            label,
            f"{len(indeterminate)} foreign hook command(s) unresolved (dynamic shell construct)",
        )
    return Check(OK, label, "none — ccc's entries are the only path to `ccc hook`")


def _stop_coverage_blocker(
    settings: dict, details: list[tuple[str, str | None, str, int | None, str]]
) -> str:
    """Why the Stop-hook timeout coverage cannot be PROVEN COMPLETE — ``""`` when it can.

    Three ways to be blind: a Stop entry that is not a readable ``command``, an enabled
    plugin (its hooks live in the plugin's own ``hooks.json`` — measured: the
    ``openai-codex`` plugin registers a ``Stop`` hook), or a managed-settings file. The
    returned reason is a bare clause, composed by the caller — because being blind to ONE
    scope never makes a readable, over-long hook safe: an unreadable scope can only push the
    true maximum HIGHER, so a FAIL from the readable scopes still stands and is reported.
    """
    unreadable = [d for d in details if d[4] != "command" or not d[2].strip()]
    if unreadable:
        count = len(unreadable)
        return (
            f"{count} Stop {'entry' if count == 1 else 'entries'} ccc cannot read "
            "(not a command entry / empty command)"
        )
    plugins = settings.get("enabledPlugins")
    if isinstance(plugins, dict) and plugins:
        count = len(plugins)
        return (
            f"{count} enabled {'plugin' if count == 1 else 'plugins'} may add Stop hooks "
            "ccc cannot read"
        )
    managed = _managed_settings_path()
    try:
        present = managed.is_file()
    except OSError:
        present = False
    if present:
        return f"managed settings ({managed}) may add Stop hooks ccc cannot read"
    return ""


def _stop_hook_timeout_coverage_check(settings: dict) -> Check:
    """Can a foreign Stop hook still be running when the lock lease expires?

    Registration order proves nothing — Claude Code runs every hook of one event in
    PARALLEL — so the meaningful question is the WINDOW, not the position: ``release-locks``
    LEASES the session's file locks for ``stop_barrier_wait_sec``
    (:meth:`command_center.store.Store.protect_locks`), and a foreign Stop hook (an
    auto-commit, a linter sweep) still running when that lease expires can be writing a file
    a peer is already allowed to edit again. This compares the longest DECLARED foreign Stop
    timeout — an omitted one means Claude Code's own
    :data:`CLAUDE_HOOK_DEFAULT_TIMEOUT_SEC` — against that window, and says plainly that a
    declared timeout is an upper bound, never proof that the hook finished.

    A readable breach is reported FIRST, even when a scope is unreadable
    (:func:`_stop_coverage_blocker`): an invisible plugin scope cannot make a visible 300 s
    hook safe, it can only raise the true maximum further, so the blocker is appended to the
    FAIL instead of hiding it. Only when nothing readable breaches the window does a blocker
    turn the check NA — carrying what IS measurable, so the reader still learns the longest
    readable timeout. NA also when ``release-locks`` is not wired at all (no window to
    compare against).
    """
    label = "Stop-hook timeout coverage"
    details = install.hook_entry_details(settings, "Stop")
    is_ccc = install._ccc_hook_arg  # pylint: disable=protected-access
    if not any(is_ccc(command) == "release-locks" for _e, _m, command, _t, _k in details):
        return Check(NA, label, "ccc release-locks not wired")
    barrier = config.load_config().stop_barrier_wait_sec
    foreign = [
        (timeout if timeout is not None else CLAUDE_HOOK_DEFAULT_TIMEOUT_SEC, command)
        for _e, _m, command, timeout, _k in details
        if is_ccc(command) is None
    ]
    blocker = _stop_coverage_blocker(settings, details)
    longest, name = 0, ""
    if foreign:
        longest, command = max(foreign, key=lambda item: item[0])
        name = install.hook_command_name(command)
    if foreign and longest > barrier:
        detail = (
            f"{name} may run {longest}s > stop_barrier_wait_sec ({barrier}s) — the lock "
            "lease can expire while that hook is still writing the session's files "
            "(raise stop_barrier_wait_sec)"
        )
        return Check(FAIL, label, f"{detail} (and {blocker})" if blocker else detail)
    measured = (
        f"longest readable foreign Stop timeout {longest}s ({name}) <= "
        f"stop_barrier_wait_sec ({barrier}s)"
        if foreign
        else f"no readable foreign Stop hooks; the lock lease covers {barrier}s"
    )
    if blocker:
        return Check(NA, label, f"cannot prove coverage: {blocker}; {measured}")
    if not foreign:
        return Check(
            OK,
            label,
            f"no foreign Stop hooks; the lock lease covers {barrier}s "
            "(a declared timeout is an upper bound, not proof a hook finished)",
        )
    return Check(
        OK,
        label,
        f"{measured} — a declared timeout is an upper bound, NOT proof the hook finished",
    )


# --------------------------------------------------------------------------- #
# spawn fast path (which ccc subcommands the wiring spawns, and how they parse)
# --------------------------------------------------------------------------- #
def _spawned_ccc_subcommands(settings: dict) -> tuple[list[str], bool]:
    """Which ccc subcommands the wiring in *settings* spawns (first-seen order).

    ccc's OWN wiring is read from structured knowledge, never by parsing text: a
    ``direct`` / ``chain`` statusLine spawns ``statusline`` by construction, and a hook
    command the installer's recognizer owns spawns ``hook``. Only FOREIGN wiring — a
    third-party statusLine, a hook ccc does not own — is inspected as text.
    """
    names: dict[str, None] = {}
    indeterminate = False
    statusline = settings.get("statusLine")
    command = str(statusline.get("command", "")) if isinstance(statusline, dict) else ""
    if install.statusline_state(settings) in ("direct", "chain"):
        names["statusline"] = None
    elif command:
        found, unknown = _foreign_ccc_calls(command)
        names.update(dict.fromkeys(found))
        indeterminate = indeterminate or unknown
    for hook_command in install.hook_commands(settings):
        if install._ccc_hook_arg(hook_command):  # pylint: disable=protected-access
            names["hook"] = None
            continue
        found, unknown = _foreign_ccc_calls(hook_command)
        names.update(dict.fromkeys(found))
        indeterminate = indeterminate or unknown
    return list(names), indeterminate


def _section_fast_path() -> Section:
    """Does every ccc command the wiring spawns get the short parser? (tp#115)

    The status line and the hooks spawn ccc hundreds of times a minute, where building
    all ~80 subparsers is pure overhead; ``cli._HOT_SUBCOMMANDS`` is the set that skips
    it. This section names the subcommands the live wiring actually spawns and whether
    each one is on that fast path — so a new high-frequency external call that misses it
    is visible without reading any code. Purely informational: never ❌.
    """
    from . import cli  # pylint: disable=import-outside-toplevel

    section = Section("Spawn fast path")
    names, indeterminate = _spawned_ccc_subcommands(install.load_settings())
    for name in names:
        if name in cli._HOT_SUBCOMMANDS:  # pylint: disable=protected-access
            section.checks.append(Check(OK, f"ccc {name}", "short parser"))
        else:
            section.checks.append(
                Check(NA, f"ccc {name}", "full parser — fine unless spawned every few seconds")
            )
    if indeterminate:
        section.checks.append(
            Check(NA, "spawned ccc commands", "indeterminate (dynamic shell construct)")
        )
    if not names and not indeterminate:
        section.checks.append(Check(NA, "spawned ccc commands", "none found in statusLine/hooks"))
    return section


def _section_daemon() -> Section:
    section = Section("Daemon")
    if sys.platform == "darwin":
        from . import launchd  # pylint: disable=import-outside-toplevel

        if launchd.is_loaded():
            section.checks.append(Check(OK, "launchd agent loaded", launchd.label()))
        elif launchd.is_installed():
            section.checks.append(
                Check(FAIL, "launchd agent", "installed but not loaded (launchctl load)")
            )
        else:
            section.checks.append(
                Check(FAIL, "launchd agent", "not installed — ccc daemon --install")
            )
        return section
    if sys.platform.startswith("linux"):
        from . import systemdunit  # pylint: disable=import-outside-toplevel

        if systemdunit.is_active():
            section.checks.append(
                Check(OK, "systemd --user timer active", f"{systemdunit.label()}.timer")
            )
        elif systemdunit.is_installed():
            section.checks.append(
                Check(FAIL, "systemd --user timer", "installed but not active (systemctl --user)")
            )
        else:
            section.checks.append(
                Check(FAIL, "systemd --user timer", "not installed — ccc daemon --install")
            )
        return section
    section.checks.append(Check(NA, "daemon service", "no launchd/systemd on this platform"))
    return section


#: macOS's Automation (TCC) store; the Apple-events grant that gates every AppleScript to iTerm2.
_TCC_DB = Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"
_ITERM_BUNDLE_ID = "com.googlecode.iterm2"


def _tcc_apple_events_grant(client: str, db: Path | None = None) -> str:
    """macOS's Automation verdict for executable *client* (a real path) → iTerm2.

    ``"allowed"`` / ``"denied"`` / ``"unknown"`` (no row — never asked, so the first
    launch will prompt) / ``"unreadable"`` (db absent or protected, or a schema this
    reader does not know). Read-only (``mode=ro`` URI); never raises. The TCC schema
    is Apple-private, so anything unexpected degrades to ``"unreadable"`` rather than
    a wrong verdict.
    """
    import sqlite3  # pylint: disable=import-outside-toplevel

    path = db or _TCC_DB
    if not path.exists():
        return "unreadable"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute(
                "SELECT auth_value FROM access WHERE service = 'kTCCServiceAppleEvents' "
                "AND client = ? AND indirect_object_identifier = ?",
                (client, _ITERM_BUNDLE_ID),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return "unreadable"
    if not rows:
        return "unknown"
    values = {row[0] for row in rows}
    if values & {2, 3}:  # 2 = allowed, 3 = limited
        return "allowed"
    if 0 in values:
        return "denied"
    return "unknown"


def _iterm_api_server_enabled() -> bool | None:
    """iTerm2's "Enable Python API" preference; ``None`` when it cannot be read."""
    argv = ["defaults", "read", _ITERM_BUNDLE_ID, "EnableAPIServer"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() == "1" if proc.returncode == 0 else None


def _section_terminal() -> Section:
    """The iTerm2 launch path (tp#90): can a launchd-started ccc still reach a tab?

    Reports the Automation grant for **ccc's own interpreter** — the responsible
    executable of ccc's own LaunchAgents (``~/.local/bin/ccc`` resolves to it), and
    therefore what decides whether ``ccc sync-future``'s launches land in an iTerm2
    tab or fall through to tmux. A job launched by ANOTHER program (gitlab-ci-watch's
    poller) is judged on THAT program's executable, which this section cannot know:
    prove it dynamically (``ccc terminal-probe`` from its LaunchAgent). ❌ only for an
    explicit denial; a never-asked path or an unreadable store is − (informational).
    """
    import os  # pylint: disable=import-outside-toplevel

    from . import terminal  # pylint: disable=import-outside-toplevel

    section = Section("Terminal (iTerm2 launch path)")
    if sys.platform != "darwin":
        section.checks.append(Check(NA, "iTerm2 launcher", "not macOS — tmux launcher"))
        return section
    if not _iterm2_present():
        section.checks.append(
            Check(NA, "iTerm2 launcher", "iTerm2 not detected — launches fall through to tmux")
        )
        return section

    exe = os.path.realpath(sys.executable)
    grant = _tcc_apple_events_grant(exe)
    label = "Automation grant → iTerm2 (ccc's interpreter)"
    if grant == "allowed":
        section.checks.append(Check(OK, label, exe))
    elif grant == "denied":
        section.checks.append(
            Check(
                FAIL,
                label,
                f"DENIED for {exe} — every launchd launch of ccc's own agents falls through "
                "to tmux; re-allow under System Settings › Privacy & Security › Automation",
            )
        )
    elif grant == "unknown":
        section.checks.append(
            Check(
                NA,
                label,
                f"not yet asked for {exe} — the first launchd launch prompts (10 s), then "
                "falls through to tmux until Allow is clicked; the grant is per executable "
                "path, so a Homebrew/uv python upgrade asks again",
            )
        )
    else:
        section.checks.append(
            Check(
                NA,
                label,
                "TCC.db unreadable — check System Settings › Privacy & Security › Automation",
            )
        )

    api = _iterm_api_server_enabled()
    if api:
        section.checks.append(Check(OK, "iTerm2 Python API server", "enabled"))
    else:
        section.checks.append(
            Check(
                NA,
                "iTerm2 Python API server",
                "disabled or unknown — the Python-API rung is skipped (AppleScript + tmux remain)",
            )
        )
    if terminal.is_iterm_api_auth_tcc_free():
        section.checks.append(
            Check(OK, "Python-API rung TCC-free", "iTerm2's disable-automation-auth switch is set")
        )
    else:
        section.checks.append(
            Check(
                NA,
                "Python-API rung TCC-free",
                "no disable-automation-auth switch — the rung needs the same Automation grant "
                "(it fetches its cookie via AppleScript), so it is skipped once AppleScript failed",
            )
        )
    section.checks.append(
        Check(
            NA,
            "other launchd launchers",
            "a job launched by another program is judged on THAT executable — prove it with "
            "`ccc terminal-probe -j` from its own LaunchAgent (gitlab-ci-watch: "
            "tests/acceptance/launchd_tab_probe.sh)",
        )
    )
    return section


def _readable(path: Path) -> bool:
    """True when *path* exists and this process can actually open it for reading."""
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _last_mirror_health() -> MirrorHealth | None:
    """Counters of the last mirror pass, or ``None`` (no pass yet / unreadable store).

    Read-only and best-effort: ``ccc doctor`` must work on a machine with no store at
    all, so ANY failure to open or query it degrades to "no row" rather than raising.
    Only called when the mirror feature is on — a doctor run with mirrors off never
    touches the database.
    """
    try:
        from .store import Store  # pylint: disable=import-outside-toplevel

        with Store() as store:
            return store.mirror_health()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


def _mirror_scrubber_check(cfg: config.Config) -> Check:
    """The mirror credential scrubber: resolvable, and what the last pass did with it.

    The mirror roots embed transcripts verbatim, so a mirror switch without a working
    scrubber is a live-credential export waiting to happen — hence ❌ for the explicit
    ``mirror_allow_unscrubbed`` opt-out and ❌ for an unresolvable command, both of which
    are silent at runtime otherwise (the pass just withholds every write).
    """
    label = "mirrors → scrubber"
    if not (cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions):
        return Check(NA, label, "disabled")
    if cfg.mirror_allow_unscrubbed:
        return Check(
            FAIL,
            label,
            "mirror_allow_unscrubbed = true — mirrors are written WITHOUT a credential scrub",
        )
    resolution = scrub.resolve_scrubber(cfg.mirror_scrub_cmd)
    if not resolution.ok or resolution.scrubber is None:
        return Check(FAIL, label, resolution.reason)
    exe = resolution.scrubber.executable
    health = _last_mirror_health()
    if health is not None and health.withheld > 0:
        return Check(
            FAIL, label, f"{exe} — last pass withheld {health.withheld} write(s): {health.reason}"
        )
    detail = exe
    if health is not None:
        detail += (
            f" — last pass: vouched={health.vouched} scrubbed={health.scrubbed} "
            f"deferred={health.deferred}"
        )
    return Check(OK, label, detail)


def _section_features(  # pylint: disable=too-many-branches,too-many-statements
    cfg: config.Config,
) -> Section:
    section = Section("Features & dependencies")
    if cfg.llm_custom_command.strip():
        section.checks.append(Check(OK, "LLM router", "llm_custom_command configured"))
    else:
        section.checks.append(Check(NA, "LLM router", "llm_custom_command is empty"))

    if cfg.copilot_usage:
        if shutil.which("gh"):
            section.checks.append(Check(OK, "copilot_usage → gh", "GitHub CLI on PATH"))
        else:
            section.checks.append(
                Check(FAIL, "copilot_usage → gh", "gh not found (uv tool install gh?)")
            )
    else:
        section.checks.append(Check(NA, "copilot_usage → gh", "disabled"))

    # The live Codex fetch is authorized by the ChatGPT token `codex login` writes into
    # each CODEX_HOME's auth.json; without a readable one the endpoint can only 401, so
    # check every configured home by name (the label is what `ccc codex-usage -a` takes).
    if cfg.codex_usage:
        for label, home in config.codex_homes().items():
            auth = home / "auth.json"
            check_label = f"codex_usage → auth.json ({label})"
            if _readable(auth):
                section.checks.append(Check(OK, check_label, str(auth)))
            else:
                section.checks.append(
                    Check(
                        FAIL, check_label, f"{auth} not readable (CODEX_HOME={home} codex login?)"
                    )
                )
    else:
        section.checks.append(Check(NA, "codex_usage → auth.json", "disabled"))

    if cfg.resume_halted:
        from . import resume  # pylint: disable=import-outside-toplevel

        script = resume._resolve_continue_script(cfg)  # pylint: disable=protected-access
        if script:
            section.checks.append(Check(OK, "resume_halted → session-continue", script))
        else:
            section.checks.append(
                Check(
                    FAIL,
                    "resume_halted → session-continue",
                    "claude-session-continue not resolvable",
                )
            )
    else:
        section.checks.append(Check(NA, "resume_halted → session-continue", "disabled"))

    vault_on = cfg.future_files or cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions
    if vault_on:
        vault = Path(cfg.vault_root).expanduser()
        if vault.is_dir():
            section.checks.append(Check(OK, "vault features → vault_root", str(vault)))
        else:
            section.checks.append(
                Check(FAIL, "vault features → vault_root", f"{vault} does not exist")
            )
    else:
        section.checks.append(Check(NA, "vault features → vault_root", "disabled"))

    section.checks.append(_mirror_scrubber_check(cfg))

    if cfg.launcher == "iterm":
        if shutil.which("osascript"):
            section.checks.append(Check(OK, "launcher=iterm → osascript", "present"))
        else:
            section.checks.append(
                Check(FAIL, "launcher=iterm → osascript", "not found (tmux fallback engages)")
            )
    elif cfg.launcher == "tmux":
        if shutil.which("tmux"):
            section.checks.append(Check(OK, "launcher=tmux → tmux", "present"))
        else:
            section.checks.append(Check(FAIL, "launcher=tmux → tmux", "tmux not found"))
    else:
        section.checks.append(Check(NA, f"launcher={cfg.launcher}", "unknown launcher"))

    # Informational only (peek / jump degrade gracefully without iTerm2).
    if _iterm2_present():
        section.checks.append(Check(OK, "iTerm2 (peek/jump)", "installed"))
    else:
        section.checks.append(Check(NA, "iTerm2 (peek/jump)", "not detected — peek/jump degrade"))
    return section


def build_report(cfg: config.Config | None = None) -> Report:
    """Assemble the full doctor report (pure; no output)."""
    cfg = cfg or config.load_config()
    return Report(
        [
            _section_core(),
            _section_wiring(),
            _section_fast_path(),
            _section_daemon(),
            _section_terminal(),
            _section_features(cfg),
        ]
    )


def render(report: Report) -> str:
    """Render *report* as the sectioned ✅ / ❌ / − text block."""
    lines: list[str] = []
    for section in report.sections:
        lines.append(f"\n{section.title}")
        for check in section.checks:
            symbol = _SYMBOL.get(check.status, "?")
            suffix = f"  — {check.detail}" if check.detail else ""
            lines.append(f"  {symbol} {check.label}{suffix}")
    verdict = "all good" if report.exit_code == 0 else "issues found (see ❌ above)"
    lines.append(f"\n{'✅' if report.exit_code == 0 else '❌'} ccc doctor: {verdict}")
    return "\n".join(lines).lstrip("\n")


def run(cfg: config.Config | None = None) -> int:
    """Print the doctor report and return its exit code (0 = healthy, 1 = ❌ present)."""
    report = build_report(cfg)
    print(render(report))
    return report.exit_code
