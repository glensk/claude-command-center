#!/usr/bin/env python3
"""Discover the category/repo tree on disk (for the future-job repo picker).

A *future job* is created against a repo's working directory. When the user opens
the picker on a category header (or off-grid), we need the list of repos that
actually exist under ``<repo_root>/<category>/`` — this module is that lookup, kept
dependency-light so it is trivial to unit-test against a temp tree.

The tree root (``repo_root``) is resolved from the ``repo_root`` config key, then the
``$GIT_BASE`` environment variable, then ``""`` (no tree). Its immediate children are
the categories (e.g. ``home``, ``infra``, ``llms``, ``sdsc``), and each category's
immediate children are the repos. With an empty root there is no category tree and
every session falls into the catch-all ``others`` bucket.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import shlex
import subprocess
from pathlib import Path

from . import config

# Hidden / tooling dirs that live alongside real categories & repos but aren't either.
_SKIP = {".git", ".vscode", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__", ".venv"}

# A path guaranteed not to be an ancestor of any real cwd, used when no ``repo_root`` is
# configured so category detection degrades cleanly to the ``others`` bucket.
_NO_ROOT = Path("/") / ".ccc-no-repo-root"


def repo_root(cfg: config.Config | None = None) -> str:
    """Resolved root of the category/repo tree, or ``""`` when there is no tree.

    Resolution: the ``repo_root`` config key → ``$GIT_BASE`` env → ``""``. Pass an
    already-loaded *cfg* to avoid a config read on hot paths.
    """
    root = getattr(cfg or config.load_config(), "repo_root", "") or ""
    if root:
        return os.path.expanduser(root)
    env = os.environ.get("GIT_BASE")
    if env:
        return os.path.expanduser(env)
    return ""


def git_base() -> Path:
    """The category/repo tree root as a :class:`Path` (see :func:`repo_root`).

    An empty ``repo_root`` yields a sentinel path that is not an ancestor of any real
    cwd, so on-disk discovery returns nothing and layout mapping degrades to the
    absolute-path fallback (the ``others`` bucket).
    """
    root = repo_root()
    return Path(root) if root else _NO_ROOT


def rel_under_root(path: str, root: str) -> str | None:
    """POSIX-relative path of *path* under *root*, or ``None`` when outside/at-root/empty.

    Pure string logic (no filesystem access): *root* and *path* are compared by prefix
    so callers never need the paths to exist on disk.
    """
    if not root or not path:
        return None
    base = os.path.expanduser(root).rstrip("/")
    target = path.rstrip("/")
    if not base or target == base:
        return None
    prefix = base + "/"
    if not target.startswith(prefix):
        return None
    return target[len(prefix) :]


def category_of(path: str, root: str) -> tuple[str, str] | None:
    """``(category, leaf)`` for *path* under *root*, or ``None`` when outside the tree.

    Layout is ``<root>/<category>/<repo…>``: the first segment beneath *root* is the
    category, the remainder is the leaf (``<root>/sdsc/runai-cscs`` →
    ``("sdsc", "runai-cscs")``; a sub-folder keeps its tail →
    ``("sdsc", "runai-cscs/tickets")``; a bare category dir → ``("sdsc", "sdsc")``).
    An empty *root* (no configured tree) always returns ``None`` — the shared helper
    behind core's category grouping, colors' folder split, and the repo picker.
    """
    rel = rel_under_root(path, root)
    if rel is None:
        return None
    head, sep, tail = rel.partition("/")
    if not head:
        return None
    return (head, tail if (sep and tail) else head)


def _subdirs(path: Path) -> list[str]:
    """Sorted names of real sub-directories of *path* (skipping tooling/hidden dirs)."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    names = [p.name for p in entries if p.is_dir() and p.name not in _SKIP]
    return sorted(names)


def categories() -> list[str]:
    """The category folders on disk (``home``, ``infra``, …); empty with no tree."""
    return _subdirs(git_base())


def repos_in(category: str) -> list[str]:
    """Repo folder names directly under ``<repo_root>/<category>/`` (sorted)."""
    return _subdirs(git_base() / category)


def repo_path(category: str, repo: str) -> Path:
    """Absolute working directory for ``<category>/<repo>``."""
    return git_base() / category / repo


def parse_repo_path(arg_string: str) -> Path | None:
    """The repo working directory implied by ``<category> <name> …`` create args.

    The create command takes ``<category> <repo_name>`` as its first two positional
    arguments, so the new repo lands at ``<repo_root>/<category>/<name>``. Returns
    ``None`` when fewer than two positionals are present.
    """
    positional = [token for token in shlex.split(arg_string) if not token.startswith("-")]
    if len(positional) >= 2:
        return git_base() / positional[0] / positional[1]
    return None


def create_repo(arg_string: str, cfg: config.Config | None = None) -> tuple[bool, str]:
    """Scaffold a repo via the ``create_repo_command`` template; return ``(ok, output)``.

    The template's ``{category}`` and ``{name}`` placeholders are filled from the first
    two positional tokens of *arg_string* and the whole command is run via the shell.
    An empty template disables the feature (``ok=False`` with an explanatory message).
    Synchronous and blocking (it may hit the network), so callers that must stay
    responsive should run it off the UI thread. ``output`` is the combined
    stdout+stderr (trimmed) for a toast.
    """
    cfg = cfg or config.load_config()
    template = cfg.create_repo_command.strip()
    if not template:
        return (False, "creating repos is disabled — set create_repo_command in the config")
    positional = [token for token in shlex.split(arg_string) if not token.startswith("-")]
    if len(positional) < 2:
        return (False, "need '<category> <name>' to create a repo")
    command = template.format(category=positional[0], name=positional[1])
    try:
        proc = subprocess.run(  # noqa: S602
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return (False, str(exc))
    return (proc.returncode == 0, (proc.stdout + proc.stderr).strip())
