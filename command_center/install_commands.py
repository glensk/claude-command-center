#!/usr/bin/env python3
"""``ccc install-commands`` — copy ccc's slash commands into ``$CLAUDE_HOME/commands``.

Ships the seven ccc slash commands (aim, next-step, done, block, deadline,
aim-history, subgoal-history) as package data under ``assets/commands/`` and, with
``--codex``, the optional codex command + skill under ``assets/codex/`` — whose
``description:`` is stamped with the resolved Codex model (see ``codex_in_claude``) so the
slash-command help names the model. Every file is
written **atomically** (temp + ``os.replace``); a file that would be overwritten with
different content is first backed up to a timestamped sibling. Reruns are **idempotent**
(a byte-identical target is skipped). ``--uninstall`` removes only files whose content
still byte-matches what ccc would install — a user-edited command is left alone.

The plan is built by the pure :func:`build_plan` (easy to test); :func:`run` applies it.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from . import config

#: The seven core slash-command asset stems (installed always).
CORE_COMMANDS: tuple[str, ...] = (
    "aim",
    "next-step",
    "done",
    "block",
    "deadline",
    "aim-history",
    "subgoal-history",
)

#: Core skill directory names, installed ALWAYS (not behind --codex).
CORE_SKILLS: tuple[str, ...] = ("ccc-mark-done-and-close",)

#: Skill directory name for the optional codex asset.
_CODEX_SKILL = "codex-implement-task-and-claude-review"
_CODEX_COMMAND = "codex-implement-task-and-claude-review.md"


def _codex_stamped(text: str) -> str:
    """Stamp the resolved Codex model marker into a codex asset's ``description:``.

    The asset ships marker-free; the installed copy carries ``[codex <model> effort=<e>]``
    so Claude Code's slash-command help names the model Codex will actually run — and so a
    re-install can never silently revert the marker written by ``set-model``. Falls back to
    the raw asset if the frontmatter cannot be parsed (never blocks an install).
    """
    # Lazy: keeps the install path free of the engine's POSIX-only imports at import time.
    from .codex_in_claude import (  # pylint: disable=import-outside-toplevel
        inject_marker,
        marker_for,
    )

    try:
        return inject_marker(text, marker_for("delegate-review"))
    except (ValueError, OSError):
        return text


@dataclass(frozen=True)
class Item:
    """One file the installer manages: where it goes and the exact content it holds."""

    dest: Path
    content: str
    label: str  # short human label for logs (e.g. "commands/aim.md")


def _assets() -> object:
    """The ``command_center/assets`` Traversable (works from a non-editable wheel)."""
    return files("command_center") / "assets"


def _read_asset(*parts: str) -> str:
    node = _assets()
    for part in parts:
        node = node / part  # type: ignore[operator]
    return node.read_text(encoding="utf-8")  # type: ignore[attr-defined]


def build_plan(claude_home: Path, *, codex: bool) -> list[Item]:
    """Return the ordered list of files ``install-commands`` manages for this invocation."""
    commands_dir = claude_home / "commands"
    items: list[Item] = [
        Item(
            dest=commands_dir / f"{stem}.md",
            content=_read_asset("commands", f"{stem}.md"),
            label=f"commands/{stem}.md",
        )
        for stem in CORE_COMMANDS
    ]
    # Core skills ship by default (not behind --codex) so `ccc-mark-done-and-close` is
    # always available inside a session.
    items.extend(
        Item(
            dest=claude_home / "skills" / name / "SKILL.md",
            content=_read_asset("skills", name, "SKILL.md"),
            label=f"skills/{name}/SKILL.md",
        )
        for name in CORE_SKILLS
    )
    if codex:
        items.append(
            Item(
                dest=commands_dir / _CODEX_COMMAND,
                content=_codex_stamped(_read_asset("codex", "commands", _CODEX_COMMAND)),
                label=f"commands/{_CODEX_COMMAND}",
            )
        )
        items.append(
            Item(
                dest=claude_home / "skills" / _CODEX_SKILL / "SKILL.md",
                content=_codex_stamped(_read_asset("codex", "skills", _CODEX_SKILL, "SKILL.md")),
                label=f"skills/{_CODEX_SKILL}/SKILL.md",
            )
        )
    return items


def _utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp + ``os.replace``), creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ccc-tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _backup(path: Path) -> Path | None:
    """Copy *path* to a timestamped sibling before it is overwritten."""
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


def _current(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def run(  # pylint: disable=too-many-branches
    *, codex: bool = False, dry_run: bool = False, uninstall: bool = False
) -> int:
    """Install (or uninstall) ccc's slash commands. Returns an exit code (always 0)."""
    home = config.claude_home()
    items = build_plan(home, codex=codex)
    verb = "uninstall" if uninstall else "install"
    written = removed = skipped = backed_up = 0

    for item in items:
        existing = _current(item.dest)
        if uninstall:
            if existing is None:
                skipped += 1
                continue
            if existing != item.content:
                print(f"  keep    {item.label} (modified since install — not ours to remove)")
                skipped += 1
                continue
            print(f"  remove  {item.label}")
            if not dry_run:
                try:
                    item.dest.unlink()
                except OSError:
                    pass
            removed += 1
            continue

        if existing == item.content:
            skipped += 1
            continue
        if existing is None:
            print(f"  create  {item.label}")
        else:
            print(f"  update  {item.label} (previous content backed up)")
        if not dry_run:
            if existing is not None:
                backup = _backup(item.dest)
                if backup is not None:
                    backed_up += 1
            _atomic_write(item.dest, item.content)
        written += 1

    tag = "[dry-run] " if dry_run else ""
    if uninstall:
        print(f"{tag}ccc {verb}-commands: removed={removed} skipped={skipped}")
    else:
        print(
            f"{tag}ccc {verb}-commands: written={written} skipped(up-to-date)={skipped} "
            f"backups={backed_up}  → {home / 'commands'}"
        )
        for name in CORE_SKILLS:
            print(f"{tag}  + skill (skills/{name})")
        if codex:
            print(f"{tag}  + codex command & skill (skills/{_CODEX_SKILL})")
    return 0
