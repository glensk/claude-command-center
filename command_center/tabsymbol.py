#!/usr/bin/env python3
"""Per-iTerm-tab unique colored symbol, shared by the shell and the TUI.

Goal: when several Claude Code sessions run in the *same folder*, their command
center rows look identical. To tell them apart, every iTerm tab is given a
distinct colored emoji ("badge"). The badge is shown in two places that must
agree without coordinating:

* the iTerm tab **title** — prepended by the zsh ``chpwd`` hook
  (``_repo_tab_color_hook``) which calls ``ccc tab-symbol`` once per tab, and
* the **command center row** — read by the TUI for each session via its stored
  ``iterm_session_id``.

The badge is keyed to the iTerm tab (``$ITERM_SESSION_ID``), not the Claude
session, so it is assigned at folder-entry time (before ``claude`` even runs) and
survives every ``cd`` within the tab. Assignment is filesystem-backed (one small
file per tab, mirroring the sibling ``~/.cache/iterm-tab-rgb/`` cache used by the
tab-color system) so no daemon or DB coordination is needed: the shell writes,
the TUI reads.

The ``chpwd`` hook only fires on ``cd``, never while a CLI holds the foreground,
so a badge assigned *mid-session* would show in the TUI row but never reach the
tab title. :func:`seed_title` (called from the ``SessionStart`` hook) and
:func:`sync_live` (called from the daemon every pass and by ``ccc tab-symbol
--sync``) close that gap: they assign a badge if missing and push ``"<badge>
<leaf>"`` to the running tab's title via AppleScript, **preserving** any leading
``set-iterm-wait-marker.sh`` "🔴 " marker so a waiting tab is not reset.

Colored emoji are used (not ANSI-styled glyphs) so the *exact same character*
renders identically in the terminal table and the iTerm tab title — no separate
color channel to keep in sync.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import contextlib
import fcntl
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Session
    from .store import Store

# The "waiting for input" marker that ``set-iterm-wait-marker.sh`` prepends to a
# tab title (overridable there via ``$CLAUDE_WAIT_MARKER``). The title-sync below
# preserves it, so seeding a badge never strips a session's "waiting" indicator.
_DEFAULT_WAIT_MARKER = "🔴 "

# Assignment order = visual priority. Tabs claim the first free badge in this
# order, so it is front-loaded for maximum distinctness: the first 6 cover all 6
# shapes (circle / square / diamond / triangle / heart / star) before any shape
# repeats, the first 8 are 8 well-separated, high-contrast colors before any hue
# repeats, with only ONE red and ONE warm-yellow up front (the look-alike glyphs
# — extra reds 🔴🔻🟥, warm 🟨🟧🧡, dark ⚫ — are pushed to the tail). No two
# adjacent badges share a shape or a color family. Width-2 emoji throughout
# (uniform cell width); only widely-supported, no-variation-selector glyphs.
#
# Each entry is (emoji, shape, color-family); ``color`` groups look-alike hues
# ("warm" = yellow/gold/orange) so the ordering rules are testable.
BADGES: tuple[tuple[str, str, str], ...] = (
    ("🔺", "triangle", "red"),
    ("🟢", "circle", "green"),
    ("🟪", "square", "purple"),
    ("⭐", "star", "warm"),
    ("🔷", "diamond", "blue"),
    ("🤎", "heart", "brown"),
    ("💠", "diamond", "cyan"),
    ("⚪", "circle", "white"),
    ("💙", "heart", "blue"),
    ("🟩", "square", "green"),
    ("🟣", "circle", "purple"),
    ("🟨", "square", "warm"),
    ("🔵", "circle", "blue"),
    ("🟫", "square", "brown"),
    ("💜", "heart", "purple"),
    ("🟧", "square", "warm"),
    ("🔻", "triangle", "red"),
    ("🤍", "heart", "white"),
    ("🟤", "circle", "brown"),
    ("🟦", "square", "blue"),
    ("🔴", "circle", "red"),
    ("🧡", "heart", "warm"),
    ("⚫", "circle", "black"),
    ("💚", "heart", "green"),
)

PALETTE: tuple[str, ...] = tuple(emoji for emoji, _shape, _color in BADGES)

# Visible width of a badge cell ("<emoji> "): emoji renders as 2 cells + 1 space.
# The no-badge fallback pads to the same width so folder names stay column-aligned.
_CELL_PAD = "   "


def cache_dir() -> Path:
    """Directory holding one ``<slug>`` file per tab (env-overridable for tests)."""
    env = os.environ.get("CCC_TAB_SYMBOL_DIR")
    return Path(env) if env else Path.home() / ".cache" / "iterm-tab-symbol"


def slug(iterm_session_id: str) -> str:
    """Filesystem-safe key for a tab, matching the zsh ``${ITERM_SESSION_ID//:/_}``."""
    return iterm_session_id.replace(":", "_")


def read(iterm_session_id: str | None) -> str | None:
    """Return the badge already assigned to *iterm_session_id*, or ``None``."""
    if not iterm_session_id:
        return None
    path = cache_dir() / slug(iterm_session_id)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def cell(iterm_session_id: str | None, *, show: bool = True) -> str:
    """Render a fixed-width ``"<emoji> "`` badge cell (blank-padded otherwise).

    Pass ``show=False`` for a session whose process is gone (parked / finished): its
    badge no longer maps to any live iTerm tab — the tab was closed, and its
    ``$ITERM_SESSION_ID`` may since have been recycled by an unrelated shell — so
    showing the emoji would point at a tab that isn't there. The cell still pads to
    the same width so the folder column stays aligned.
    """
    badge = read(iterm_session_id) if show else None
    return f"{badge} " if badge else _CELL_PAD


def _repo_key(cwd_or_repo: str, root: str | None = None) -> str:
    """Normalize a cwd (or bare repo id) to a stable key so cwd and repo-name agree.

    A path-like input (absolute, ``~``-relative, or containing a slash) is reduced to
    its :func:`command_center.colors.short_folder` — the ``category/repo`` label the tab
    title and TUI row already show — so the shell hook (``ccc tab-symbol --print <cwd>``)
    and the TUI/ls row, both fed the same cwd under the same config, resolve to the same
    key. A bare token (e.g. a repo name) is used verbatim.

    *root* is the already-resolved :func:`command_center.repos.repo_root`. Pass it when
    rendering a whole listing: omitting it makes ``short_folder`` resolve the tree root
    per call, i.e. one config read per ROW (629 of them on a big table). ``None`` keeps
    the on-demand resolution, so a one-shot caller needs no change.
    """
    text = (cwd_or_repo or "").strip()
    if not text:
        return ""
    if text.startswith(("/", "~")) or "/" in text:
        from . import colors  # lazy: keep the shell-hook (``ccc tab-symbol``) import light

        return colors.short_folder(os.path.expanduser(text), root)
    return text


def symbol_for_repo(cwd_or_repo: str, root: str | None = None) -> str:
    """A deterministic badge for a repo/cwd — a stable hash into :data:`PALETTE`.

    The same input maps to the same emoji forever, with **no** shared cache, so a
    plain-terminal shell hook and the TUI/ls row agree without coordinating. The live
    iTerm-tab cache (:func:`assign` / :func:`read`) overrides this wherever present, so
    the author's real per-tab assignments still win on his machine; this is the generic
    fallback that makes every session — and every plain terminal — show a symbol.

    *root* is the pre-resolved repo-tree root (see :func:`_repo_key`); the badge itself
    is identical either way, it only saves the per-call root resolution.

    Returns ``""`` for an empty key (no cwd to key on).
    """
    key = _repo_key(cwd_or_repo, root)
    if not key:
        return ""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()  # noqa: S324 (non-crypto: stable slot)
    return PALETTE[int(digest, 16) % len(PALETTE)]


def cell_for(
    iterm_session_id: str | None, cwd: str, *, live: bool = True, root: str | None = None
) -> str:
    """Fixed-width ``"<emoji> "`` badge cell for a row: live tab cache, else deterministic.

    Resolution mirrors the tab title: a *live* session's claimed iTerm-tab badge wins (so
    same-folder sessions stay distinguishable exactly as their tabs are), and every other
    row falls back to :func:`symbol_for_repo` for *cwd* — a stable per-repo symbol that
    needs no live tab. So a parked/finished row, a demo row, or a plain-terminal session
    all still show their repo's symbol (the cell only blanks when there is no cwd at all).

    This is the per-ROW entry point, so *root* matters here: the TUI and ``ccc ls``
    resolve :func:`command_center.repos.repo_root` once per listing and pass it down
    rather than paying a config read (and, before the memo, a TOML parse) per row.
    """
    badge = (read(iterm_session_id) if live else None) or symbol_for_repo(cwd, root)
    return f"{badge} " if badge else _CELL_PAD


_SHAPE = {emoji: shape for emoji, shape, _color in BADGES}
_COLOR = {emoji: color for emoji, _shape, color in BADGES}


def _folder_path(directory: Path, own: str) -> Path:
    """Sidecar recording a tab's folder, so badges stay distinct *within* a folder."""
    return directory / f"{own}.dir"


def _scan(directory: Path, own: str) -> tuple[set[str], dict[str, str], list[Path]]:
    """Inspect other tabs: globally-used badges, each badge's folder, files oldest-first."""
    used: set[str] = set()
    folder_of: dict[str, str] = {}
    files: list[Path] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return used, folder_of, files
    for entry in entries:
        if entry.name == own or entry.name == ".lock" or entry.suffix == ".dir":
            continue
        if not entry.is_file():
            continue
        try:
            value = entry.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not value:
            continue
        used.add(value)
        files.append(entry)
        try:
            folder_of[value] = (
                _folder_path(directory, entry.name).read_text(encoding="utf-8").strip()
            )
        except OSError:
            folder_of[value] = ""
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
    return used, folder_of, files


def _shape_color_counts(badges: Iterable[str]) -> tuple[dict[str, int], dict[str, int]]:
    """Tally how many of *badges* wear each shape and each color family."""
    shape_count: dict[str, int] = {}
    color_count: dict[str, int] = {}
    for badge in badges:
        shape = _SHAPE.get(badge, "")
        color = _COLOR.get(badge, "")
        shape_count[shape] = shape_count.get(shape, 0) + 1
        color_count[color] = color_count.get(color, 0) + 1
    return shape_count, color_count


def _pick(used: set[str], folder_of: dict[str, str], folder: str) -> str | None:
    """Most-distinct free badge, derived from *all* currently-open badges.

    Lexicographic preference (lowest count wins), so a new tab gets — if possible —
    a shape and a color that no open tab is already wearing:

    1. shape unused *in this folder*  — the same-folder guarantee badges exist for
    2. color unused in this folder
    3. shape unused *globally* (across every open tab, any folder)
    4. color unused globally
    5. palette order (front-loaded for distinctness) as the final tiebreak

    Folder-distinctness stays primary so two sessions sharing one folder are never
    pushed together to free up a globally-rare glyph; among badges equally good for
    the folder, the globally-rarest shape/color wins — so distinct folders also drift
    apart instead of both marching down the palette head.
    """
    free = [(e, s, c) for e, s, c in BADGES if e not in used]
    if not free:
        return None
    siblings = [badge for badge, fld in folder_of.items() if fld == folder]
    folder_shape, folder_color = _shape_color_counts(siblings)
    global_shape, global_color = _shape_color_counts(folder_of)  # every other open tab
    return min(
        free,
        key=lambda b: (
            folder_shape.get(b[1], 0),
            folder_color.get(b[2], 0),
            global_shape.get(b[1], 0),
            global_color.get(b[2], 0),
            PALETTE.index(b[0]),
        ),
    )[0]


def assign(iterm_session_id: str | None, folder: str = "") -> str | None:
    """Return *iterm_session_id*'s badge, claiming the most-distinct free one if needed.

    Idempotent: a tab keeps its badge across ``cd``s. The chosen badge maximizes
    shape- then color-distinctness *among other tabs in the same folder* first (the
    whole point — same-folder sessions look as different as possible), then among
    **all** open tabs globally, so a new tab also prefers a shape and color no other
    tab is wearing; ties fall back to palette order. Claiming is serialized with a
    directory lock so two tabs
    opening at once never grab the same emoji. When the palette is globally
    exhausted the oldest other tab's badge is reclaimed (its file removed, so that
    tab re-claims on its next ``cd``). *folder* groups tabs (typically the cwd).
    """
    if not iterm_session_id:
        return None
    directory = cache_dir()
    own = slug(iterm_session_id)
    own_path = directory / own
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    lock_path = directory / ".lock"
    with contextlib.ExitStack() as stack:
        try:
            lock = lock_path.open("w", encoding="utf-8")
        except OSError:
            return read(iterm_session_id)
        stack.callback(lock.close)
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            stack.callback(lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_UN))
        used, folder_of, files = _scan(directory, own)
        existing = read(iterm_session_id)
        if existing in PALETTE:
            # Keep the badge, but refresh the folder sidecar (the tab may have cd'd).
            with contextlib.suppress(OSError):
                _folder_path(directory, own).write_text(folder, encoding="utf-8")
            return existing
        chosen = _pick(used, folder_of, folder)
        if chosen is None and files:
            # Palette exhausted: steal the least-recently-touched tab's badge.
            reclaimed = files[0]
            chosen = reclaimed.read_text(encoding="utf-8").strip()
            with contextlib.suppress(OSError):
                reclaimed.unlink()
                _folder_path(directory, reclaimed.name).unlink()
        if chosen is None:
            chosen = PALETTE[0]
        with contextlib.suppress(OSError):
            own_path.write_text(chosen, encoding="utf-8")
            _folder_path(directory, own).write_text(folder, encoding="utf-8")
        return chosen


def _wait_marker(marker: str | None) -> str:
    """Resolve the wait marker to preserve (caller arg → env → default)."""
    if marker is not None:
        return marker
    return os.environ.get("CLAUDE_WAIT_MARKER", _DEFAULT_WAIT_MARKER)


_TAB_AIM_W = 40  # chars of the AIM appended to a tab title


def title_core(badge: str, cwd: str, aim: str | None = None) -> str:
    """The non-marker part of a tab title — ``"<badge> <leaf>"``, matching the zsh hook.

    With *aim* (``aim_in_tab_title``) the session's AIM follows: ``"<badge> <leaf> 🎯 <aim>"``.
    """
    from . import colors  # lazy: keep the shell-hook (``ccc tab-symbol``) import light

    _category, leaf = colors.folder_split(cwd)
    if not aim:
        return f"{badge} {leaf}"
    line = aim.splitlines()[0].strip()
    if len(line) > _TAB_AIM_W:
        line = line[: _TAB_AIM_W - 1] + "…"
    return f"{badge} {leaf} 🎯 {line}"


def push_title(session: Session, *, marker: str | None = None) -> None:
    """Re-title ONE session's tab now (after its AIM changed). No-op without a tab/badge."""
    iid = session.iterm_session_id
    if session.done or not iid:
        return
    badge = assign(iid, folder=session.cwd)
    if not badge:
        return
    from . import config, terminal  # lazy: AppleScript layer, not needed on the read path

    aim = session.aim if config.load_config().aim_in_tab_title else None
    terminal.set_session_titles_preserving(
        {iid: title_core(badge, session.cwd, aim)}, marker=_wait_marker(marker)
    )


def seed_title(iterm_session_id: str | None, cwd: str, *, marker: str | None = None) -> str | None:
    """Claim this tab's badge (if unassigned) and seed its iTerm title with it.

    Marker-preserving, so a tab already flagged "waiting" keeps its marker. Called at
    session start so a freshly-launched session's tab shows its badge immediately —
    without waiting for the next ``cd`` (the zsh hook) or the next daemon pass.
    Returns the badge, or ``None`` when there is nothing to key on. Fail-safe: the
    title push is detached and swallows its own errors.
    """
    badge = assign(iterm_session_id, folder=cwd)
    if not badge or not iterm_session_id:
        return None
    from . import terminal  # lazy: AppleScript layer, not needed on the read path

    terminal.set_session_titles_preserving(
        {iterm_session_id: title_core(badge, cwd)}, marker=_wait_marker(marker)
    )
    return badge


def sync_live(store: Store, *, marker: str | None = None) -> list[str]:
    """Ensure every non-done tracked session has a badge AND its iTerm tab shows it.

    For each session carrying an ``iterm_session_id``: claim a badge if missing (so
    *every* session gets a symbol) and push ``"<badge> <leaf>"`` to its tab title,
    preserving any leading wait marker. This is the single convergence point the
    daemon runs every pass, ``ccc tab-symbol --sync`` runs on demand, and the TUI
    runs on each refresh — it heals tabs whose badge was assigned (or reshuffled by
    palette recycling) after the title was last set, which the ``cd``-driven zsh hook
    can never reach while a CLI holds the foreground. With no daemon loaded the TUI
    refresh is what keeps open tabs in sync with their rows. Returns the session ids
    that were badged.
    """
    from . import config  # lazy

    with_aim = config.load_config().aim_in_tab_title
    cores: dict[str, str] = {}
    badged: list[str] = []
    for session in store.list_sessions():
        iid = session.iterm_session_id
        if session.done or not iid:
            continue
        badge = assign(iid, folder=session.cwd)
        if not badge:
            continue
        cores[iid] = title_core(badge, session.cwd, session.aim if with_aim else None)
        badged.append(session.session_id)
    if cores:
        from . import terminal  # lazy: AppleScript layer, not needed on the read path

        terminal.set_session_titles_preserving(cores, marker=_wait_marker(marker))
    return badged


def badge_for_uuid(uuid: str) -> str | None:
    """The badge cached for the tab whose session UUID is *uuid* (any ``wNtNpN`` prefix).

    The cache key is the full ``$ITERM_SESSION_ID`` (``w0t3p0:UUID``) but the Python API
    only knows the UUID, and the ``wNtNpN`` part goes stale when a tab moves — so match
    on the UUID suffix and prefer the most recently written file.
    """
    if not uuid:
        return None
    paths = [p for p in cache_dir().glob(f"*_{uuid}") if p.is_file()]
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def healed_title(title: str, badge: str | None) -> str | None:
    """*title* with *badge* in front, or None when it already starts with it.

    A different palette badge already leading the title (the tab's badge was recycled
    since the rename) is replaced rather than stacked.
    """
    if not badge or title.startswith(badge):
        return None
    rest = title
    for other in PALETTE:
        if rest.startswith(f"{other} "):
            rest = rest[len(other) + 1 :]
            break
    return f"{badge} {rest}"


def _rename_with_badge(override: str, uuids: list[str]) -> str | None:
    """``ItermLink.retitle_overridden_tabs`` callback: badge of the tab's first badged session."""
    badge = next((b for b in map(badge_for_uuid, uuids) if b), None)
    return healed_title(override, badge)


WATCH_INTERVAL_SEC = 2.0
_WATCH_RETRY_SEC = 30.0
_WATCH_OP_TIMEOUT_SEC = 15.0


def heal_renamed_tabs_once() -> int:
    """One pass of :func:`watch`: badge every renamed tab now; tabs retitled (0 if offline)."""
    import asyncio  # pylint: disable=import-outside-toplevel

    from . import iterm_api  # pylint: disable=import-outside-toplevel

    api_link = iterm_api.CookieItermLink()

    async def _once() -> int:
        if not await asyncio.wait_for(api_link.reconnect(), _WATCH_OP_TIMEOUT_SEC):
            return 0
        return await asyncio.wait_for(
            api_link.retitle_overridden_tabs(_rename_with_badge), _WATCH_OP_TIMEOUT_SEC
        )

    try:
        return asyncio.run(_once())
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 0


def watch(interval: float = WATCH_INTERVAL_SEC, *, iterations: int | None = None) -> int:
    """Keep the badge in front of every tab title the user renamed (``ccc tab-symbol -w``).

    ccc puts ``"<badge> <leaf>"`` into the session *name*; a tab renamed via iTerm's
    "Edit Tab Title" displays its title override instead, so the badge vanished. This
    loop holds one iTerm2 Python-API connection and every *interval* seconds prefixes
    the tab's badge onto any override lacking it. iTerm not running / no cookie → retry
    every 30 s. *iterations* bounds the loop (tests); None runs until killed.
    """
    import asyncio  # pylint: disable=import-outside-toplevel

    from . import iterm_api  # pylint: disable=import-outside-toplevel

    link = iterm_api.CookieItermLink()

    async def _loop() -> None:
        done = 0
        while iterations is None or done < iterations:
            done += 1
            if not link.ready:
                try:
                    ok = await asyncio.wait_for(link.reconnect(), _WATCH_OP_TIMEOUT_SEC)
                except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                    ok = False
                if not ok:
                    await asyncio.sleep(_WATCH_RETRY_SEC)
                    continue
            try:
                await asyncio.wait_for(
                    link.retitle_overridden_tabs(_rename_with_badge), _WATCH_OP_TIMEOUT_SEC
                )
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                link.drop()
            await asyncio.sleep(interval)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_loop())
    return 0
