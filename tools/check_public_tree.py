#!/usr/bin/env python3
"""Forbidden-pattern checker for the public ``ccc`` tree.

Scans every tracked-candidate file (git-tracked + untracked-not-ignored) for a
fixed set of personal/private anchors that must not ship in a public repo. Prints
``file:line: pattern`` for every non-allowlisted hit and exits non-zero if any
remain; exits 0 when the tree is clean.

The candidate set is derived from ``git ls-files -c -o --exclude-standard`` when
run inside a git repo (so caches, .venv, and gitignored files are skipped
automatically); otherwise it walks the tree skipping ``.git`` and common cache
directories. Binary files and the allowlist file itself are always skipped.

Allowlist file format (one entry per line, ``#`` comments and blanks ignored):

    <relative-path>:<pattern>

Each entry whitelists exactly that pattern inside that file. Paths are relative
to ``--root`` and use forward slashes.

Usage:
  tools/check_public_tree.py [-r ROOT] [-a ALLOWLIST] [-q]

Options:
  -r, --root       Tree root to scan (default: this repo's root).
  -a, --allowlist  Allowlist file (default: tools/public_tree_allowlist.txt).
  -q, --quiet      Suppress the per-hit listing; only set the exit code.
  -h, --help       Show this help and exit.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Forbidden anchors (CASE-SENSITIVE). Assembled from fragments so THIS scanner's
# own source does not match itself — do not "simplify" back to literals.
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "/Users/" + "albert",
    "com." + "albert",
    "EPF" + "L",
    "pixel" + "9a",
    "42-" + "Git",
    "mydot" + "files",
    "." + "claude-code-sessions",
    "CSC" + "S",
    "albert." + "glensk",
    "Albe" + "rt",  # the first name alone, as it appeared in comments
    "epfl" + ".ch",  # the employer domain, as it appeared in test fixtures
)

# Fallback walk: directories never descended into when git enumeration is absent.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".direnv",
    "node_modules",
}

# The allowlist path itself is never scanned (it lists the very patterns).
_ALLOWLIST_DEFAULT = "tools/public_tree_allowlist.txt"


def _default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_candidates(root: Path) -> list[Path] | None:
    """Return git tracked + untracked-not-ignored files, or None if not a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-c", "-o", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    rels = [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    return [root / r for r in rels]


def _walk_candidates(root: Path) -> list[Path]:
    """Fallback enumeration when the root is not a git repo."""
    import os

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            found.append(Path(dirpath) / name)
    return found


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return True
    return b"\x00" in chunk


def _load_allowlist(path: Path) -> set[tuple[str, str]]:
    """Parse ``<relpath>:<pattern>`` entries into a set of (relpath, pattern)."""
    allowed: set[tuple[str, str]] = set()
    if not path.is_file():
        return allowed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rel, sep, pattern = line.partition(":")
        if not sep:
            continue
        allowed.add((rel.strip(), pattern.strip()))
    return allowed


def scan(root: Path, allowlist_path: Path) -> list[tuple[str, int, str]]:
    """Return non-allowlisted hits as (relpath, 1-based-lineno, pattern)."""
    allowed = _load_allowlist(allowlist_path)
    candidates = _git_candidates(root)
    if candidates is None:
        candidates = _walk_candidates(root)

    allowlist_rel = _rel(allowlist_path, root)
    hits: list[tuple[str, int, str]] = []

    for path in sorted(set(candidates)):
        if not path.is_file():
            continue
        rel = _rel(path, root)
        if rel == allowlist_rel:
            continue
        if _is_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in line and (rel, pattern) not in allowed:
                    hits.append((rel, lineno, pattern))
    return hits


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root_default = _default_root()
    parser = argparse.ArgumentParser(
        description="Scan the public tree for forbidden personal/private anchors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        default=root_default,
        help="Tree root to scan (default: this repo's root).",
    )
    parser.add_argument(
        "-a",
        "--allowlist",
        type=Path,
        default=root_default / _ALLOWLIST_DEFAULT,
        help=f"Allowlist file (default: {_ALLOWLIST_DEFAULT}).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the per-hit listing; only set the exit code.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    hits = scan(args.root, args.allowlist)
    if not hits:
        if not args.quiet:
            print("clean: no forbidden patterns found")
        return 0
    if not args.quiet:
        for rel, lineno, pattern in hits:
            print(f"{rel}:{lineno}: {pattern}")
        print(f"\n{len(hits)} forbidden-pattern hit(s) across the tree", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
