#!/usr/bin/env python3
"""``ccc init`` — the first-run wizard that ties the installers together.

Interactive (TTY) flow: report the detected environment, ask for a vault path, walk a
consent checklist grouped over :data:`config.INERT_DEFAULT_KEYS`, write a minimal
``config.toml`` (only keys that differ from the built-in defaults, plus the vault anchor
keys the user set), then offer to run each installer (``install-hooks`` /
``install-statusline`` / ``install-commands`` / ``obsidian-setup`` / ``daemon --install``)
and finish with ``ccc doctor``.

Non-interactive: ``-y/--yes`` writes the recommended profile (LLM checkers ON, vault
features ON only when a vault is supplied, copilot/resume/reap OFF) and runs the
installers; ``-m/--minimal`` writes nothing on and runs no installers. With neither ``-y``
nor ``-m`` and no TTY the wizard exits 3 (the repo's interactive-only convention).

The consent groups are the SINGLE source mapping every inert key to a wizard question;
:data:`UNMAPPED_INERT` names inert keys deliberately not surfaced. A drift test asserts the
union equals :data:`config.INERT_DEFAULT_KEYS`, so a new inert key cannot be added without
being mapped (or explicitly excluded) here.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, doctor, install, install_commands, launchd, obsidian, resume, shell_install

# --------------------------------------------------------------------------- #
# consent groups — the single mapping of inert keys → wizard questions
# --------------------------------------------------------------------------- #
#: Group A — the cheap LLM progress checkers, offered as ONE recommended-ON toggle.
GROUP_A_CHECKERS: tuple[str, ...] = (
    "aim_score_on_set",
    "grade_on_turn",
    "assess_aim_on_turn",
    "drift_check",
    "summarize",
    "autoprogress",
    "short_aim",
)
#: Group B — vault export features, offered only when a vault path is supplied.
GROUP_B_VAULT: tuple[str, ...] = (
    "future_files",
    "mirror_running",
    "mirror_done",
    "mirror_sessions",
)
#: Group C — offered individually, each behind its own dependency / caveat.
GROUP_C: tuple[str, ...] = ("copilot_usage", "resume_halted", "reap")
#: Inert keys intentionally NOT surfaced as a wizard question (secondary cost knobs).
UNMAPPED_INERT: tuple[str, ...] = ("verify_subgoals_llm", "claude_usage")

#: Anchor keys always written when the user supplies a vault (even if == default).
ANCHOR_KEYS: tuple[str, ...] = ("vault_root",)

#: Score-ladder backend → the CLI whose presence enables it, in ladder order. Detection
#: writes ``score_backends`` as the available subset (copilot, gemini, codex, then claude
#: last), so the score call prefers a non-Anthropic backend when one is installed.
SCORE_BACKEND_TOOLS: tuple[tuple[str, str], ...] = (
    ("copilot", "opencode"),
    ("gemini", "gemini"),
    ("codex", "codex"),
    ("claude", "claude"),
)


def detect_score_backends() -> list[str]:
    """The AIM-score ladder for THIS machine: the available backends in preference order.

    Presence-check only (``shutil.which`` — never an LLM/API call). Empty when none of the
    CLIs are on PATH (the AIM scorer then degrades to the offline lexical estimate).
    """
    return [name for name, tool in SCORE_BACKEND_TOOLS if shutil.which(tool)]


# --------------------------------------------------------------------------- #
# environment detection (presence checks only — never an LLM/API call)
# --------------------------------------------------------------------------- #
@dataclass
class Env:  # pylint: disable=too-many-instance-attributes
    """The presence of each tool ccc's optional features lean on (all detection-only)."""

    claude: str | None
    codex: str | None
    gh: str | None
    osascript: str | None
    iterm2: bool
    tmux: str | None
    resume_script: str
    vault_guess: str | None


def _iterm2_present() -> bool:
    if sys.platform != "darwin":
        return False
    return any(
        (base / "iTerm.app").exists()
        for base in (Path("/Applications"), Path.home() / "Applications")
    )


def detect_env(cfg: config.Config) -> Env:
    """Presence-check the tools ccc's optional features lean on."""
    vault = Path(cfg.vault_root).expanduser()
    return Env(
        claude=shutil.which("claude"),
        codex=shutil.which("codex"),
        gh=shutil.which("gh"),
        osascript=shutil.which("osascript"),
        iterm2=_iterm2_present(),
        tmux=shutil.which("tmux"),
        resume_script=resume._resolve_continue_script(cfg),  # pylint: disable=protected-access
        vault_guess=str(vault) if vault.is_dir() else None,
    )


def _print_env(env: Env) -> None:
    def mark(present: object) -> str:
        return "✅" if present else "−"

    print("environment:")
    print(f"  {mark(env.claude)} claude CLI        {env.claude or 'not found'}")
    print(f"  {mark(env.codex)} codex CLI         {env.codex or 'not found'}")
    print(f"  {mark(env.gh)} gh CLI            {env.gh or 'not found'}")
    print(f"  {mark(env.osascript)} osascript        {env.osascript or 'not found'}")
    print(f"  {mark(env.iterm2)} iTerm2           {'installed' if env.iterm2 else 'not detected'}")
    print(f"  {mark(env.tmux)} tmux             {env.tmux or 'not found'}")
    print(f"  {mark(env.vault_guess)} vault (config)   {env.vault_guess or 'none'}")


# --------------------------------------------------------------------------- #
# profile → config overrides (pure, testable)
# --------------------------------------------------------------------------- #
@dataclass
class Profile:
    """The choices a wizard run resolves to (interactive or ``-y``/``-m``)."""

    vault_root: str | None = None  # None → no vault features
    checkers: bool = False
    vault_features: bool = False
    copilot: bool = False
    resume: bool = False
    reap: bool = False
    score_backends: list[str] | None = None  # detected AIM-score ladder (None → leave default)


def vault_dir_overrides(vault_root: str) -> dict[str, str]:
    """The task-dir layout under *vault_root* (mirrors the ``~/obsidian`` defaults)."""
    base = f"{vault_root.rstrip('/')}/01-llm-tasks"
    return {
        "future_dir": f"{base}/future",
        "delete_dir": f"{base}/delete",
        "future_pad": f"{base}/new-prompt.md",
        "running_dir": f"{base}/running",
        "done_dir": f"{base}/done",
        "sessions_dir": f"{base}/sessions",
    }


def config_values(profile: Profile) -> dict[str, object]:
    """The full intended config values a profile implies (before default-filtering)."""
    values: dict[str, object] = {}
    if profile.checkers:
        for key in GROUP_A_CHECKERS:
            values[key] = True
        if profile.score_backends:
            # Write the detected AIM-score ladder so the score call prefers a non-Anthropic
            # backend when one is installed (minimal_config_text drops it if it == the default).
            values["score_backends"] = list(profile.score_backends)
    if profile.vault_root:
        values["vault_root"] = profile.vault_root
        values.update(vault_dir_overrides(profile.vault_root))
        if profile.vault_features:
            for key in GROUP_B_VAULT:
                values[key] = True
    if profile.copilot:
        values["copilot_usage"] = True
    if profile.resume:
        values["resume_halted"] = True
    if profile.reap:
        values["reap"] = True
    return values


def _format_toml_line(key: str, value: object) -> str:
    if isinstance(value, bool):
        return f"{key} = {str(value).lower()}"
    if isinstance(value, list):
        items = ", ".join(f'"{item}"' for item in value)
        return f"{key} = [{items}]"
    return f'{key} = "{value}"'


def minimal_config_text(profile: Profile) -> str:
    """Serialize ONLY the keys that differ from DEFAULTS (plus anchor keys the user set)."""
    values = config_values(profile)
    always = {k for k in ANCHOR_KEYS if k in values}
    lines = [
        _format_toml_line(key, value)
        for key, value in values.items()
        if key in always or value != config.DEFAULTS.get(key)
    ]
    return ("\n".join(lines) + "\n") if lines else ""


# --------------------------------------------------------------------------- #
# config write (backup on --force)
# --------------------------------------------------------------------------- #
def _utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def write_config(profile: Profile, *, force: bool) -> tuple[bool, Path | None]:
    """Write the minimal config. Returns ``(written, backup_path)``.

    Refuses (``written=False``, no backup) when a config already exists and *force* is
    False. With *force* an existing config is backed up to a timestamped sibling first.
    """
    path = config.config_path()
    backup: Path | None = None
    if path.exists() and not force:
        return False, None
    if path.exists() and force:
        try:
            backup = path.with_name(f"{path.name}.ccc-backup-{_utc_stamp()}")
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            backup = None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(minimal_config_text(profile), encoding="utf-8")
    return True, backup


# --------------------------------------------------------------------------- #
# interactive helpers
# --------------------------------------------------------------------------- #
def _ask_yes_no(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _interactive_profile(env: Env) -> tuple[Profile, bool]:
    """Walk the consent checklist. Returns ``(profile, codex_opt_in)``."""
    try:
        vault_in = input(
            f"\nVault path (Enter to skip all vault features) [{env.vault_guess or ''}]: "
        ).strip()
    except EOFError:
        vault_in = ""
    vault_root = vault_in or env.vault_guess or None

    checkers = _ask_yes_no(
        "\nEnable the LLM progress checkers (AIM score, per-turn grading, AIM-met, drift,\n"
        "  summaries, auto sub-goals, short labels)? A few cheap haiku calls per turn/idle pass",
        default=True,
    )
    vault_features = False
    if vault_root:
        vault_features = _ask_yes_no(
            "Enable the vault export features (future-job + running/done/session mirrors)?",
            default=True,
        )
    copilot = False
    if env.gh:
        copilot = _ask_yes_no("Show the GitHub Copilot usage card (uses `gh`)?", default=False)
    resume_opt = False
    if env.resume_script:
        resume_opt = _ask_yes_no(
            "Auto-resume rate-limit-halted sessions once the limit resets?", default=False
        )
    reap = _ask_yes_no(
        "Let the daemon auto-CLOSE idle sessions (SIGTERM; transcript kept)? Off by default",
        default=False,
    )
    codex_opt = False
    if env.codex:
        codex_opt = _ask_yes_no(
            "Also install the Codex delegate command + skill (optional)?", default=False
        )
    return (
        Profile(
            vault_root=vault_root,
            checkers=checkers,
            vault_features=vault_features,
            copilot=copilot,
            resume=resume_opt,
            reap=reap,
            score_backends=detect_score_backends() if checkers else None,
        ),
        codex_opt,
    )


# --------------------------------------------------------------------------- #
# installer phase
# --------------------------------------------------------------------------- #
def _run_installers(profile: Profile, *, codex: bool, ask: bool) -> None:
    """Run (or offer to run) each installer, reusing the existing implementations."""

    def offer(label: str, default: bool = True) -> bool:
        return _ask_yes_no(f"Run {label}?", default=default) if ask else True

    if offer("install-hooks"):
        install.install_hooks()
    if offer("install-statusline"):
        state = install.statusline_state(install.load_settings())
        install.install_statusline(chain=state == "foreign")
    if offer("install-commands"):
        install_commands.run(codex=codex)
    if offer("install-shell (AIM-at-startup wrapper + tab badges)"):
        shell_install.install()
    if profile.vault_root and offer("obsidian-setup"):
        obsidian.run_setup(root=profile.vault_root)
    if sys.platform == "darwin" and offer("daemon --install (launchd agent)"):
        launchd.install()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(args) -> int:  # pylint: disable=too-many-branches
    """Drive the wizard from parsed CLI args. Returns an exit code."""
    cfg = config.load_config()
    env = detect_env(cfg)
    _print_env(env)

    yes = bool(getattr(args, "yes", False))
    minimal = bool(getattr(args, "minimal", False))
    force = bool(getattr(args, "force", False))
    vault_arg = getattr(args, "vault_root", None)
    interactive = not yes and not minimal

    if interactive and not sys.stdin.isatty():
        print(
            "\nccc init needs an interactive terminal (or pass -y for the recommended profile,\n"
            "or -m for a minimal no-features setup).",
            file=sys.stderr,
        )
        return 3

    codex_opt = False
    if minimal:
        profile = Profile(vault_root=vault_arg or None)
    elif yes:
        vault = vault_arg or env.vault_guess
        profile = Profile(
            vault_root=vault,
            checkers=True,
            vault_features=bool(vault),
            copilot=False,
            resume=False,
            reap=False,
            score_backends=detect_score_backends(),
        )
    else:
        profile, codex_opt = _interactive_profile(env)

    written, backup = write_config(profile, force=force)
    if not written:
        print(
            f"\nconfig already exists: {config.config_path()}\n"
            "  rerun with -f/--force to overwrite it (a timestamped backup is kept).",
            file=sys.stderr,
        )
        return 1
    if backup:
        print(f"\nbacked up previous config → {backup}")
    print(f"wrote config → {config.config_path()}")

    if not minimal:
        print("\ninstallers:")
        _run_installers(profile, codex=codex_opt, ask=interactive)

    _print_linux_hotkey_pointer()

    print("\n" + "=" * 60)
    doctor.run(config.load_config())
    return 0


def _print_linux_hotkey_pointer() -> None:
    """On Linux, point at the suggested keyd/xremap peek-chord samples (macOS uses Karabiner)."""
    if not sys.platform.startswith("linux"):
        return
    from importlib.resources import files  # noqa: PLC0415 (local: only Linux init needs it)

    samples = files("command_center") / "assets" / "hotkeys-linux"
    print(
        f"\nhotkeys (Linux): sample keyd/xremap peek-chord configs live at {samples}\n"
        "  (suggested-only; peek's floating panel & jump are macOS-only — see docs/linux.md)."
    )
