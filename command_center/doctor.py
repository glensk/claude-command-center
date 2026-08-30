#!/usr/bin/env python3
"""``ccc doctor`` — a read-only health check of the ccc install and its environment.

Prints a sectioned ✅ / ❌ / − report (− = not applicable / feature disabled) and exits
0 when nothing is broken, 1 when any ❌ is present. It mutates nothing and works with no
config at all (a fresh machine) — it states what is missing without crashing.

The report is built by the pure :func:`build_report` (easy to test); :func:`run` renders
it and returns the exit code.
"""

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
from dataclasses import dataclass, field
from pathlib import Path

from . import config, install

OK, FAIL, NA = "ok", "fail", "na"
_SYMBOL = {OK: "✅", FAIL: "❌", NA: "−"}


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
    wired = install.installed_hook_events(settings)
    expected = set(install.ALL_HOOK_ARGS)
    if wired >= expected:
        section.checks.append(Check(OK, "hooks wired", f"all {len(expected)} ccc events"))
    elif wired:
        missing = ", ".join(sorted(expected - wired))
        section.checks.append(
            Check(FAIL, "hooks wired", f"partial — missing: {missing} (ccc install-hooks)")
        )
    else:
        section.checks.append(Check(FAIL, "hooks wired", "none — run ccc install-hooks"))

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
    section.checks.append(_stop_order_check(settings))
    return section


def _stop_hook_commands(settings: dict) -> list[str]:
    """The Stop event's hook commands, flattened in wired order (empty if none)."""
    hooks = settings.get("hooks")
    stop = hooks.get("Stop") if isinstance(hooks, dict) else None
    if not isinstance(stop, list):
        return []
    commands: list[str] = []
    for group in stop:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks", []) or []:
            if isinstance(entry, dict):
                commands.append(str(entry.get("command", "")))
    return commands


def _stop_order_check(settings: dict) -> Check:
    """WARN when ccc's ``release-locks`` Stop hook is not the LAST Stop entry.

    close-after-done + lock-release must run AFTER foreign Stop hooks (e.g. the user's
    auto-commit) so this turn's work is committed before the pane/tab closes and the locks
    drop. Install enforces this; a later foreign append can break it — this guards that.
    Recognised via the same matcher install.py uses for ccc-owned entries.
    """
    commands = _stop_hook_commands(settings)
    positions = [
        i
        for i, cmd in enumerate(commands)
        if install._ccc_hook_arg(cmd) == "release-locks"  # pylint: disable=protected-access
    ]
    if not positions:
        return Check(NA, "Stop-hook order", "ccc release-locks not wired")
    if positions[-1] == len(commands) - 1:
        return Check(OK, "Stop-hook order", "release-locks runs last (after foreign Stop hooks)")
    return Check(
        FAIL,
        "Stop-hook order",
        "release-locks not last — close-after-done & lock-release must run after foreign "
        "Stop hooks like auto-commit (ccc install-hooks)",
    )


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


#: Score-ladder backend → the CLI whose presence enables it (custom has no CLI dep).
_SCORE_BACKEND_TOOL = {
    "copilot": "opencode",
    "gemini": "gemini",
    "codex": "codex",
    "claude": "claude",
}


def _score_ladder_checks(cfg: config.Config) -> list[Check]:
    """Per-rung availability of the configured AIM-score fallback ladder (``score_backends``)."""
    if not cfg.score_backends:
        return [Check(NA, "score ladder", "no backends configured (offline lexical score only)")]
    checks: list[Check] = []
    for name in cfg.score_backends:
        label = f"score ladder → {name}"
        if name == "custom":
            if cfg.score_custom_command.strip():
                checks.append(Check(OK, label, "score_custom_command configured"))
            else:
                checks.append(Check(FAIL, label, "no score_custom_command set"))
            continue
        tool = _SCORE_BACKEND_TOOL.get(name)
        if tool is None:
            checks.append(Check(FAIL, label, "unknown backend"))
        elif shutil.which(tool):
            checks.append(Check(OK, label, f"{tool} on PATH"))
        else:
            checks.append(Check(FAIL, label, f"{tool} not found"))
    return checks


def _readable(path: Path) -> bool:
    """True when *path* exists and this process can actually open it for reading."""
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _section_features(  # pylint: disable=too-many-branches,too-many-statements
    cfg: config.Config,
) -> Section:
    section = Section("Features & dependencies")
    section.checks.extend(_score_ladder_checks(cfg))

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

    if cfg.short_aim and cfg.short_aim_backend in ("codex", "auto"):
        if shutil.which("codex"):
            section.checks.append(Check(OK, "short_aim → codex", "codex CLI on PATH"))
        else:
            section.checks.append(
                Check(
                    FAIL,
                    "short_aim → codex",
                    f"codex not found (backend={cfg.short_aim_backend})",
                )
            )
    elif cfg.short_aim:
        section.checks.append(Check(NA, "short_aim → codex", "using the claude backend"))
    else:
        section.checks.append(Check(NA, "short_aim → codex", "disabled"))

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
            _section_daemon(),
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
