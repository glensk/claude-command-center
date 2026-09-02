#!/usr/bin/env python3
"""Merge ccc's hook + statusline wiring into ``$CLAUDE_HOME/settings.json``.

Two installers share this module:

* :func:`install_hooks` owns a fixed set of Claude Code hook entries (``<ccc> hook
  <event>``). It is **idempotent** (a ccc-owned entry is recognised by its command
  invoking the ``ccc`` binary with ``hook <known-event>``; a rerun replaces ccc's own
  entries in place and never duplicates or touches foreign hooks) and reversible
  (``uninstall`` strips only ccc-owned entries).
* :func:`install_statusline` sets ccc's status-line command, or — when a foreign
  statusLine already exists — generates a **chain script** that runs the original first
  and appends ccc's rows.

Both write **symlink-safely** (``settings.json`` is often a stow-managed symlink): the
real target behind the symlink is rewritten atomically (temp + ``os.replace``), never
the link itself. Every write is preceded by a timestamped backup, and ``--dry-run``
prints a unified diff without touching anything.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import copy
import difflib
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from . import config
from .hookspec import ALL_HOOK_ARGS, HOOK_SPEC

# HOOK_SPEC / ALL_HOOK_ARGS live in the import-cheap :mod:`command_center.hookspec` (the
# CLI reads the event names on every spawn and must not import this module for them), and
# are re-exported here because ``install.HOOK_SPEC`` / ``install.ALL_HOOK_ARGS`` are the
# addresses the installer, doctor and the tests use (both are read below).

# The valid hook events (the second token after ``ccc hook``), used to recognise
# ccc-owned entries on a rerun / uninstall / doctor scan — derived from HOOK_SPEC so
# there is one source of truth.
_HOOK_EVENTS: frozenset[str] = frozenset(ALL_HOOK_ARGS)

STATUSLINE_CHAIN_NAME = "statusline-chain.sh"


# --------------------------------------------------------------------------- #
# settings.json I/O (shared, symlink-safe)
# --------------------------------------------------------------------------- #
def settings_path() -> Path:
    """Path to Claude Code's ``settings.json`` (honours ``CLAUDE_HOME``)."""
    return config.claude_home() / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    """Parse ``settings.json`` into a dict (empty dict when missing / invalid)."""
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ccc_binary() -> str:
    """Absolute ``ccc`` invocation for generated commands (which → argv[0] → ``ccc``)."""
    return shutil.which("ccc") or (sys.argv[0] if sys.argv and sys.argv[0] else "ccc")


def _render(settings: dict) -> str:
    """Serialise settings to the on-disk JSON form (2-space indent, trailing newline)."""
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def _utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def backup_settings(path: Path) -> Path | None:
    """Copy the current ``settings.json`` to a timestamped sibling backup.

    Reads THROUGH any symlink (the real content) but writes the backup next to the
    logical path (``$CLAUDE_HOME``), so backups never land inside a dotfiles source
    tree. Returns the backup path, or ``None`` when there is nothing to back up.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    backup = path.with_name(f"{path.name}.ccc-backup-{_utc_stamp()}")
    try:
        backup.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return backup


def _write_through(path: Path, text: str) -> None:
    """Atomically write *text* to *path*, following (never replacing) a symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.resolve()  # real file behind any symlink chain
    tmp = target.with_name(f".{target.name}.ccc-tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)  # atomic; the symlink at `path` still points here
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _print_diff(path: Path, old_text: str, new_text: str) -> None:
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed)",
    )
    sys.stdout.writelines(diff)


# --------------------------------------------------------------------------- #
# hook wiring
# --------------------------------------------------------------------------- #
def _ccc_hook_arg(command: str) -> str | None:
    """Return the ccc hook-arg for a ccc-owned hook command, else ``None``.

    A command is ccc-owned iff it invokes the ``ccc`` binary (by that basename) with
    ``hook <known-event>`` — robust to the ccc path changing between installs.
    """
    parts = command.split()
    for i in range(1, len(parts) - 1):
        if parts[i] == "hook" and parts[i + 1] in _HOOK_EVENTS:
            binref = parts[i - 1]
            if binref == "ccc" or Path(binref).name == "ccc":
                return parts[i + 1]
    return None


def _is_ccc_hook_command(command: str) -> bool:
    return _ccc_hook_arg(command) is not None


def hook_commands(settings: dict, event: str | None = None) -> list[str]:
    """Every hook command in *settings* — ccc's and foreign — in wiring order.

    With *event* set, only that settings-event key's commands (doctor's Stop-order guard
    needs them in order). Tolerates any malformed shape a hand-edited settings.json can
    hold; the single walk both installer and doctor read.
    """
    commands: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return commands
    for key, groups in hooks.items():
        if not isinstance(groups, list) or (event is not None and key != event):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks", []) or []:
                if isinstance(entry, dict):
                    commands.append(str(entry.get("command", "")))
    return commands


def installed_hook_events(settings: dict) -> set[str]:
    """The set of ccc hook-args currently wired in *settings* (for doctor)."""
    found: set[str] = set()
    for command in hook_commands(settings):
        arg = _ccc_hook_arg(command)
        if arg:
            found.add(arg)
    return found


def _strip_ccc_hooks(settings: dict) -> None:
    """Remove every ccc-owned hook entry, dropping now-empty groups / events."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups: list = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            entries = group["hooks"]
            kept = [
                e
                for e in entries
                if not (isinstance(e, dict) and _is_ccc_hook_command(str(e.get("command", ""))))
            ]
            if len(kept) == len(entries):
                new_groups.append(group)  # untouched foreign group
            elif kept:
                new_groups.append({**group, "hooks": kept})  # foreign hooks survive
            # else: group was ccc-only → drop it
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]


def _insert_ccc_hooks(settings: dict, ccc: str) -> None:
    """Append ccc's own hook groups (one per :data:`HOOK_SPEC` entry)."""
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):  # defensive: a malformed foreign value
        hooks = settings["hooks"] = {}
    for event, matcher, arg in HOOK_SPEC:
        entry = {"type": "command", "command": f"{ccc} hook {arg}"}
        group = {"matcher": matcher, "hooks": [entry]} if matcher else {"hooks": [entry]}
        hooks.setdefault(event, []).append(group)


def build_hooks_settings(settings: dict, ccc: str, *, uninstall: bool) -> dict:
    """Return a copy of *settings* with ccc's hooks re-installed (or stripped)."""
    result = copy.deepcopy(settings)
    _strip_ccc_hooks(result)
    if not uninstall:
        _insert_ccc_hooks(result, ccc)
    return result


def install_hooks(*, dry_run: bool = False, uninstall: bool = False) -> int:
    """Merge (or remove) ccc's hook wiring in ``settings.json``. Returns an exit code."""
    path = settings_path()
    settings = load_settings(path)
    ccc = ccc_binary()
    new_settings = build_hooks_settings(settings, ccc, uninstall=uninstall)
    return _apply(path, settings, new_settings, dry_run=dry_run, label="hooks")


def _apply(path: Path, old: dict, new: dict, *, dry_run: bool, label: str) -> int:
    """Diff old→new settings; back up + write unless dry-run or unchanged."""
    old_text = _render(old)
    new_text = _render(new)
    if old_text == new_text:
        print(f"ccc {label}: already up to date (no change)")
        return 0
    _print_diff(path, old_text, new_text)
    if dry_run:
        print(f"\n[dry-run] no changes written ({label})")
        return 0
    backup = backup_settings(path)
    _write_through(path, new_text)
    if backup:
        print(f"\nbacked up previous settings → {backup}")
    print(f"wrote ccc {label} → {path}")
    return 0


# --------------------------------------------------------------------------- #
# statusline wiring
# --------------------------------------------------------------------------- #
def chain_script_path() -> Path:
    """Path of the generated statusline chain script under the command-center home."""
    return config.app_home() / STATUSLINE_CHAIN_NAME


def _ccc_statusline_command(ccc: str) -> str:
    return f"{ccc} statusline --capture-usage"


def _fresh_statusline(ccc: str) -> dict:
    return {"type": "command", "command": _ccc_statusline_command(ccc)}


def statusline_state(settings: dict) -> str:
    """Classify the current statusLine: ``none`` | ``direct`` | ``chain`` | ``foreign``."""
    sl = settings.get("statusLine")
    if not isinstance(sl, dict):
        return "none"
    cmd = str(sl.get("command", ""))
    if not cmd:
        return "none"
    if STATUSLINE_CHAIN_NAME in cmd or str(chain_script_path()) in cmd:
        return "chain"
    if " statusline --capture-usage" in cmd:
        return "direct"
    return "foreign"


def _render_chain_script(ccc: str, original: dict) -> str:
    """Shell that runs *original*'s command (2s timeout) then appends ccc's rows.

    The original statusLine object is recorded verbatim in a ``ccc-original-statusline``
    comment so ``--uninstall`` can restore it. Falls back to ccc-only output when the
    original fails / times out. shellcheck-clean.
    """
    original_cmd = str(original.get("command", "")) if isinstance(original, dict) else ""
    original_json = json.dumps(original if isinstance(original, dict) else {}, ensure_ascii=False)
    return f"""#!/usr/bin/env bash
# Generated by `ccc install-statusline --chain`. Do NOT edit by hand — rerun the
# command to regenerate. Chains a previously-configured Claude Code statusLine with
# ccc's own rows: the original command runs first (2s timeout), then ccc's rows are
# appended. `ccc install-statusline --uninstall` restores the original recorded below.
#
# ccc-statusline-chain: v1
# ccc-original-statusline: {original_json}
set -u

CCC_BIN={shlex.quote(ccc)}
ORIGINAL_CMD={shlex.quote(original_cmd)}

input="$(cat)"

run_original() {{
  if command -v timeout >/dev/null 2>&1; then
    printf '%s' "$input" | timeout 2 bash -c "$ORIGINAL_CMD"
  elif command -v gtimeout >/dev/null 2>&1; then
    printf '%s' "$input" | gtimeout 2 bash -c "$ORIGINAL_CMD"
  else
    printf '%s' "$input" | bash -c "$ORIGINAL_CMD"
  fi
}}

if [ -n "$ORIGINAL_CMD" ]; then
  run_original || true
fi

printf '%s' "$input" | "$CCC_BIN" statusline --capture-usage 2>/dev/null || true
"""


def _read_recorded_original(path: Path) -> dict | None:
    """Parse the ``ccc-original-statusline`` JSON recorded in a chain script."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r"#\s*ccc-original-statusline:\s*(.*)$", line)
        if match:
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                return None
            return data if isinstance(data, dict) else None
    return None


def _write_script(path: Path, content: str) -> None:
    """Atomically write an executable helper script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ccc-tmp-{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _print_foreign_refusal(current: dict, chain_path: Path) -> None:
    cmd = current.get("command", "") if isinstance(current, dict) else current
    print("ccc install-statusline: a statusLine is already configured:")
    print(f"    {cmd}")
    print("Refusing to overwrite it. Options:")
    print("  - ccc install-statusline --chain      keep it and append ccc's rows below it")
    print(f"      (generates {chain_path} and points statusLine at it)")
    print("  - remove/rename it in settings.json yourself, then rerun ccc install-statusline")
    print("  - ccc install-statusline --uninstall  (later) restores your original")


def install_statusline(
    *, chain: bool = False, dry_run: bool = False, uninstall: bool = False
) -> int:
    """Wire (or unwire) ccc's status line. Returns an exit code (1 = refused)."""
    path = settings_path()
    settings = load_settings(path)
    ccc = ccc_binary()
    chain_path = chain_script_path()
    state = statusline_state(settings)
    result = copy.deepcopy(settings)
    script_action: tuple[str, str] | tuple[str] | None = None

    if uninstall:
        if state == "direct":
            result.pop("statusLine", None)
        elif state == "chain":
            original = _read_recorded_original(chain_path)
            if original is not None:
                result["statusLine"] = original
            else:
                result.pop("statusLine", None)
                print(
                    "warning: could not read the recorded original; removed the chained "
                    "statusLine (restore it by hand from a settings.json.ccc-backup-* file)",
                    file=sys.stderr,
                )
            script_action = ("delete",)
        else:
            print("ccc statusline: nothing installed by ccc (no change)")
            return 0
    elif state in ("none", "direct"):
        result["statusLine"] = _fresh_statusline(ccc)
    elif state == "chain":  # already ours — regenerate the script, keep the pointer
        original = _read_recorded_original(chain_path) or {}
        script_action = ("write", _render_chain_script(ccc, original))
        result["statusLine"] = {"type": "command", "command": f"bash {chain_path}"}
    else:  # foreign statusLine present
        if not chain:
            _print_foreign_refusal(settings.get("statusLine", {}), chain_path)
            return 1
        original = (
            settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else {}
        )
        script_action = ("write", _render_chain_script(ccc, original or {}))
        result["statusLine"] = {"type": "command", "command": f"bash {chain_path}"}

    return _apply_statusline(path, settings, result, chain_path, script_action, dry_run=dry_run)


def _apply_statusline(
    path: Path,
    old: dict,
    new: dict,
    chain_path: Path,
    script_action: tuple[str, str] | tuple[str] | None,
    *,
    dry_run: bool,
) -> int:
    old_text = _render(old)
    new_text = _render(new)
    settings_changed = old_text != new_text
    if not settings_changed and script_action is None:
        print("ccc statusline: already up to date (no change)")
        return 0
    if settings_changed:
        _print_diff(path, old_text, new_text)
    if script_action is not None:
        verb = "write" if script_action[0] == "write" else "delete"
        print(f"\n[statusline chain script] {verb}: {chain_path}")
    if dry_run:
        print("\n[dry-run] no changes written (statusline)")
        return 0
    backup = backup_settings(path) if settings_changed else None
    # Write the script BEFORE repointing settings (settings never point at a missing
    # script); for delete, drop the settings pointer first, then remove the script.
    if script_action is not None and script_action[0] == "write":
        _write_script(chain_path, script_action[1])  # type: ignore[misc]
    if settings_changed:
        _write_through(path, new_text)
    if script_action is not None and script_action[0] == "delete":
        try:
            chain_path.unlink()
        except OSError:
            pass
    if backup:
        print(f"backed up previous settings → {backup}")
    print(f"statusline updated → {path}")
    return 0
