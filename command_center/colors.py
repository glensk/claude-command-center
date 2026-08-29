#!/usr/bin/env python3
"""Folder → RGB colours that match the iTerm2 tab colours.

Reads the precomputed ``~/.cache/repo-tab-colors.zsh`` (folder basename → "R;G;B"),
which is the same source the status line and the ``cd`` tab-colour hook use, when
present. ``_home`` is the key for ``$HOME``. Category grouping and the repo/leaf
split are derived from the configured ``repo_root`` (see :mod:`command_center.repos`);
when a folder has no cached colour a deterministic per-category palette fills in.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

from . import repos

_CACHE_FILE = Path.home() / ".cache" / "repo-tab-colors.zsh"
_LINE = re.compile(r'^\s*(\S+)\s+"(\d{1,3});(\d{1,3});(\d{1,3})"')

# Catch-all category for sessions outside the configured tree (home, /tmp, …). Keeps
# them in one contiguous, single-header block instead of one stray header per folder.
OTHERS = "others"

# Deterministic fallback palette for category colours when neither an explicit
# ``category_colors`` override nor the tab-colour cache supplies one. Red is
# deliberately omitted (reserved for importance / errors elsewhere).
_CATEGORY_PALETTE: tuple[str, ...] = (
    "#4fb0c6",  # cyan
    "#5cb85c",  # green
    "#5599ff",  # blue
    "#c678dd",  # magenta
    "#e5c07b",  # amber
    "#56b6c2",  # teal
    "#98c379",  # lime
    "#61afef",  # sky
    "#d19a66",  # orange
    "#b294bb",  # violet
)


def _tab_rgb_dir() -> Path:
    """Directory of per-tab ``R;G;B`` files (env-overridable for tests)."""
    env = os.environ.get("CCC_TAB_RGB_DIR")
    return Path(env) if env else Path.home() / ".cache" / "iterm-tab-rgb"


def tab_rgb_dir() -> Path:
    """The per-tab colour cache directory — the one :func:`tab_rgb` reads.

    Public so a *writer* (:mod:`command_center.tabcolor`, which reassigns colliding
    tab colours) can never drift from the reader's location.
    """
    return _tab_rgb_dir()


def tab_rgb(iterm_session_id: str | None) -> tuple[int, int, int] | None:
    """RGB the status line uses as the *session-id background*, or ``None``.

    Reads ``~/.cache/iterm-tab-rgb/<slug>`` — the per-tab colour written by the ``cd``
    hook / ``iterm-set-tabtitle.py`` and keyed by ``$ITERM_SESSION_ID`` (``:`` → ``_``),
    exactly the primary source ``statusline-command.sh`` reads for the coloured id. So
    ``ccc peek`` can paint the id with the same background the user sees in Claude Code.
    Callers fall back to :func:`folder_rgb` (the repo colour) when this returns ``None``,
    mirroring the status line's own fallback chain.
    """
    if not iterm_session_id:
        return None
    slug = iterm_session_id.replace(":", "_")
    try:
        raw = (_tab_rgb_dir() / slug).read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw[0].replace(",", ";").split(";")
    if len(parts) != 3:
        return None
    try:
        rgb = tuple(int(p) for p in parts)
    except ValueError:
        return None
    return rgb if all(0 <= v <= 255 for v in rgb) else None  # type: ignore[return-value]


@lru_cache(maxsize=1)
def _rgb_map() -> dict[str, tuple[int, int, int]]:
    mapping: dict[str, tuple[int, int, int]] = {}
    try:
        text = _CACHE_FILE.read_text(encoding="utf-8")
    except OSError:
        return mapping
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            mapping[match.group(1)] = (
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
            )
    return mapping


def _folder_key(cwd: str) -> str:
    home = os.path.expanduser("~")
    if cwd.rstrip("/") == home:
        return "_home"
    return os.path.basename(cwd.rstrip("/"))


def folder_rgb(cwd: str) -> tuple[int, int, int] | None:
    """RGB matching the iTerm tab colour for *cwd*, or None if the folder is unmapped."""
    if not cwd:
        return None
    return _rgb_map().get(_folder_key(cwd))


def folder_hex(cwd: str) -> str | None:
    """The tab colour as ``#rrggbb`` (for Rich styles), or None if unmapped."""
    rgb = folder_rgb(cwd)
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}" if rgb else None


def short_folder(cwd: str, root: str | None = None) -> str:
    """Drop the ``<repo_root>/`` prefix → ``infra/network-apis``; else collapse ``$HOME``.

    *root* is the resolved :func:`command_center.repos.repo_root`; pass it on hot paths
    to avoid a config read (it is resolved on demand when omitted).
    """
    if not cwd:
        return "?"
    if root is None:
        root = repos.repo_root()
    rel = repos.rel_under_root(cwd, root)
    if rel is not None:
        return rel
    home = os.path.expanduser("~")
    return "~" + cwd[len(home) :] if cwd.startswith(home) else cwd


def folder_split(cwd: str, root: str | None = None) -> tuple[str, str]:
    """Split the short folder into ``(category, leaf)`` for the grouped TUI list.

    Under the configured tree the category is the first segment and the leaf the repo
    path beneath it (``sdsc/runai-cscs`` → ``("sdsc", "runai-cscs")``; a sub-folder
    keeps its tail → ``("sdsc", "runai-cscs/tickets")``). Everything *outside* the tree
    collapses into a single ``others`` category, with the leaf being the full
    home-relative path (``~/scratch`` → ``("others", "~/scratch")``,
    ``/tmp/x`` → ``("others", "/tmp/x")``) so the section shows where each session
    actually lives.
    """
    if root is None:
        root = repos.repo_root()
    hit = repos.category_of(cwd, root)
    if hit is not None:
        return hit
    return OTHERS, short_folder(cwd, root)


def name_hex(name: str) -> str | None:
    """Hex tab colour for a bare folder *name* (e.g. a category), or None."""
    rgb = _rgb_map().get(name)
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}" if rgb else None


def category_color(category: str, cfg: object | None = None) -> str | None:
    """A stable colour (name or ``#rrggbb``) for *category*, or ``None`` for ``others``.

    Resolution: an explicit ``category_colors`` config override → the tab-colour cache
    (``name_hex``) → a deterministic hashed slot of :data:`_CATEGORY_PALETTE`. Passing
    *cfg* supplies the overrides; without it only the cache/palette are consulted.
    """
    if not category or category == OTHERS:
        return None
    overrides = getattr(cfg, "category_colors", None) or {}
    if category in overrides:
        return str(overrides[category])
    cached = name_hex(category)
    if cached:
        return cached
    digest = hashlib.md5(category.encode("utf-8")).hexdigest()  # noqa: S324 (non-crypto)
    return _CATEGORY_PALETTE[int(digest, 16) % len(_CATEGORY_PALETTE)]


def folder_style(cwd: str, cfg: object | None = None, root: str | None = None) -> str:
    """Rich style for a folder cell: the tab colour, else the category palette, else grey.

    The tab-colour cache wins when present (so nothing changes where a cache exists);
    otherwise a deterministic per-category palette colours the folder, and only a
    catch-all ``others`` folder falls through to ``grey70``.
    """
    hit = folder_hex(cwd)
    if hit:
        return hit
    category, _leaf = folder_split(cwd, root)
    return category_color(category, cfg) or "grey70"
