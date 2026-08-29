#!/usr/bin/env python3
"""Distinct per-tab background colours for OPEN sessions that resolve to the same one.

The id chip in the TUI row (and the session id in Claude Code's status line) is painted
with the tab's colour — the per-tab ``iterm-tab-rgb`` cache, the repo colour as fallback
(see :func:`command_center.colors.tab_rgb`). Two sessions in the *same repo* therefore
resolve to the *same* background, which is exactly the case the colour is supposed to
tell apart. :func:`dedupe_live` reassigns an unused palette colour to all but one of a
colliding group.

Reassignment is filesystem-backed, like :mod:`command_center.tabsymbol`: it writes the
tab's ``<rgb_dir>/<slug>`` colour file plus a ``<slug>.manual`` marker. Those are exactly
the two files the status-line wrapper already honours — with the marker present it follows
the cached colour and repaints the real tab to it on its next render — so the tab, the
status line and the ccc row converge with no terminal API involvement here.

Only OPEN sessions are deduped (the caller filters): a parked/finished row has no tab to
recolour, and its ``$ITERM_SESSION_ID`` may since have been recycled.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import subprocess
from collections.abc import Iterable
from typing import TYPE_CHECKING

from . import colors

if TYPE_CHECKING:
    from .models import Session

# Reassignment palette: visually well-separated hues, so a recoloured tab is obviously
# distinct from the one that kept the shared colour. Order is the assignment order (the
# first colour not already in use wins).
PALETTE: tuple[tuple[int, int, int], ...] = (
    (0, 199, 190),  # teal
    (255, 214, 10),  # yellow
    (191, 90, 242),  # purple
    (255, 159, 10),  # orange
    (100, 210, 255),  # sky
    (48, 209, 88),  # green
    (255, 105, 180),  # pink
    (94, 92, 230),  # indigo
    (255, 55, 95),  # raspberry
    (218, 165, 32),  # gold
    (0, 255, 255),  # cyan
    (127, 255, 0),  # chartreuse
    (250, 128, 114),  # salmon
    (64, 224, 208),  # turquoise
    (172, 142, 104),  # tan
    (192, 192, 192),  # silver
)


def slug(iterm_session_id: str) -> str:
    """Filesystem-safe key for a tab, matching the colour cache's ``${ITERM_SESSION_ID//:/_}``."""
    return iterm_session_id.replace(":", "_")


def _assign(tab_slug: str, rgb: tuple[int, int, int]) -> bool:
    """Write the tab's cached colour + its ``.manual`` marker. False on any IO failure."""
    directory = colors.tab_rgb_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / tab_slug).write_text(f"{rgb[0]};{rgb[1]};{rgb[2]}\n", encoding="utf-8")
        (directory / f"{tab_slug}.manual").touch()
    except OSError:
        return False
    return True


def _repaint_enabled() -> bool:
    """Real terminal repaints are suppressed in sandboxed runs (tests, ``ccc demo``).

    The ``CCC_TAB_RGB_DIR`` override marks exactly those runs: the colour cache is
    redirected away from the real one, so escapes must not reach a real tab either.
    """
    return "CCC_TAB_RGB_DIR" not in os.environ


def _pane_tty(pid: int | None) -> str | None:
    """The tty DEVICE of *pid*'s pane (``/dev/ttysNNN``), or ``None``.

    Writing the tab-colour escapes to the pane's device recolours the real tab at
    once — the session's own status line may not re-render for a long time on an
    idle session, and its subprocess has no controlling terminal anyway.
    """
    if not pid:
        return None
    try:
        out = subprocess.run(  # noqa: S603  (fixed argv, no shell)
            ["/bin/ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"/dev/{out}" if out and out != "??" else None


def _repaint(tty_device: str, rgb: tuple[int, int, int]) -> None:
    """Write the iTerm tab-colour OSC escapes to *tty_device* (best-effort)."""
    red, green, blue = rgb
    seq = (
        f"\033]6;1;bg;red;brightness;{red}\a"
        f"\033]6;1;bg;green;brightness;{green}\a"
        f"\033]6;1;bg;blue;brightness;{blue}\a"
    )
    try:
        with open(tty_device, "w", encoding="ascii") as handle:
            handle.write(seq)
    except OSError:
        pass


def _resolved_tabs(
    sessions: Iterable[Session],
) -> list[tuple[str, tuple[int, int, int], int | None]]:
    """``(slug, rgb, pid)`` per distinct TAB, oldest session first; unresolvable ones dropped.

    Keyed by tab, not by session: two sessions sharing one ``$ITERM_SESSION_ID`` (a nested
    ``claude``, a resume that re-used the tab) are ONE tab wearing ONE colour — counting
    them twice would make them collide with themselves and churn the cache every pass.
    """
    order = sorted(sessions, key=lambda s: (s.created_at, s.session_id))
    tabs: list[tuple[str, tuple[int, int, int], int | None]] = []
    seen: set[str] = set()
    for session in order:
        iid = session.iterm_session_id
        if not iid or slug(iid) in seen:
            continue  # no tab to key the colour cache on, or the same tab again
        rgb = colors.tab_rgb(iid) or colors.folder_rgb(session.cwd)
        if rgb is None:
            continue  # unmapped folder and no cached colour — nothing to collide with
        seen.add(slug(iid))
        tabs.append((slug(iid), rgb, session.last_seen_pid))
    return tabs


def dedupe_live(sessions: Iterable[Session]) -> list[str]:
    """Give every colliding OPEN tab but one a distinct colour; return the slugs rewritten.

    A group is the set of tabs resolving to one rgb. Within it the oldest (``created_at``,
    then ``session_id``) keeps the colour — so the assignment is stable across passes — and
    each other member takes the first palette colour no tab in this pass holds. The
    remainder of a group is left alone when the palette is exhausted.

    Idempotent: a recoloured tab resolves to its own cached colour on the next pass, so the
    group no longer collides and nothing is written.

    A rewritten tab is also repainted at once through its pane's tty device (resolved
    from the session's pid): an idle session's status line may not re-render for a long
    time, and it would otherwise keep wearing the stale colour until it does.
    """
    tabs = _resolved_tabs(sessions)
    groups: dict[tuple[int, int, int], list[tuple[str, int | None]]] = {}
    for tab_slug, rgb, pid in tabs:
        groups.setdefault(rgb, []).append((tab_slug, pid))
    used = set(groups)
    recoloured: list[str] = []
    for group in groups.values():
        for tab_slug, pid in group[1:]:  # group[0] is the oldest tab — it keeps the colour
            free = next((rgb for rgb in PALETTE if rgb not in used), None)
            if free is None:
                break  # palette exhausted: the rest keep the shared colour
            if _assign(tab_slug, free):
                used.add(free)
                recoloured.append(tab_slug)
                if _repaint_enabled():
                    tty_device = _pane_tty(pid)
                    if tty_device:
                        _repaint(tty_device, free)
    return recoloured
