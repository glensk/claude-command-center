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
import dataclasses
import fcntl
import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
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


def title_core(badge: str, cwd: str, aim: str | None = None, *, name: str | None = None) -> str:
    """The non-marker part of a tab title — ``"<badge> <leaf>"``, matching the zsh hook.

    Precedence is left to right, so a narrow tab truncates the least important part
    first: the badge, then the session's NAME (``session_names``; the folder leaf when it
    has none), then — with *aim* (``aim_in_tab_title``) — the AIM:
    ``"<badge> <name> 🎯 <aim>"``.
    """
    from . import colors  # lazy: keep the shell-hook (``ccc tab-symbol``) import light

    head = name.strip() if name and name.strip() else colors.folder_split(cwd)[1]
    if not aim:
        return f"{badge} {head}"
    line = aim.splitlines()[0].strip()
    if len(line) > _TAB_AIM_W:
        line = line[: _TAB_AIM_W - 1] + "…"
    return f"{badge} {head} 🎯 {line}"


def _session_core(session: Session, badge: str, cfg: Config) -> str:
    """*session*'s title core under *cfg* (its name when ``session_names``, its AIM if wanted)."""
    from . import session_names  # lazy

    aim = session.aim if cfg.aim_in_tab_title else None
    return title_core(badge, session.cwd, aim, name=session_names.title_name(session, cfg))


def _write_titles(plain: dict[str, str], cas: dict[str, tuple[str, str]], marker: str) -> None:
    """Dispatch one batch of title writes: unconditional *plain* cores, compare-and-swap *cas*."""
    from . import tab_titles, terminal  # lazy: AppleScript layer, not needed on the read path

    if plain:
        terminal.set_session_titles_preserving(plain, marker=marker)
    if cas:
        tab_titles.set_titles_cas(cas, marker=marker)


def _record_writes(store: object, writes: list[tuple[str, str]]) -> None:
    """Stamp ``title_written``/``title_generation`` for each ``(session_id, core)`` written.

    Tolerates stand-in stores without a connection (unit tests of the title sync).
    """
    if not writes or getattr(store, "conn", None) is None:
        return
    from . import session_names  # lazy

    for session_id, core in writes:
        session_names.record_title_write(store, session_id, core)  # type: ignore[arg-type]


def _plan_write(
    session: Session,
    core: str,
    plain: dict[str, str],
    cas: dict[str, tuple[str, str]],
    writes: list[tuple[str, str]],
) -> None:
    """File *session*'s *core* as an unconditional first write or a compare-and-swap.

    Once ccc has written a title (``title_written``) every later write is conditional on
    the tab still showing it — a title the user typed meanwhile is never replaced.
    """
    iid = session.iterm_session_id or ""
    if session.title_written:
        cas[iid] = (session.title_written, core)
    else:
        plain[iid] = core
    if core != session.title_written:
        writes.append((session.session_id, core))


_AIM_SEPARATOR = "🎯"
_TITLE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TITLE_WS_RE = re.compile(r"\s+")


def clean_title(title: str, marker: str | None = None) -> str:
    """The name a user meant by a tab *title*: wait marker, badge, AIM tail, controls stripped."""
    text = _TITLE_CONTROL_RE.sub("", title or "")
    marker = _DEFAULT_WAIT_MARKER if marker is None else marker
    for _ in range(2):  # marker before the badge, and a stray second marker
        if marker and text.startswith(marker):
            text = text[len(marker) :]
        stripped = text.lstrip()
        for badge in PALETTE:
            if stripped.startswith(badge):
                stripped = stripped[len(badge) :]
                break
        text = stripped
    if _AIM_SEPARATOR.strip() in text:
        text = text.split(_AIM_SEPARATOR.strip(), 1)[0]
    return _TITLE_WS_RE.sub(" ", text).strip()


def ccc_title_shapes(session: Session, marker: str | None = None) -> set[str]:
    """Every cleaned title body ccc itself could have written for *session*.

    :func:`clean_title` drops the badge and the AIM tail, so these are the folder leaf
    (no name yet), the session's name and whatever ccc last wrote. A live title equal to
    one of them is ccc's own — possibly a stale write that never landed — never a
    hand-set one.
    """
    shapes = {
        clean_title(title_core(PALETTE[0], session.cwd), marker),
        clean_title(session.title_written or "", marker),
    }
    if session.canonical_name:
        shapes.add(clean_title(session.canonical_name, marker))
    shapes.discard("")
    return shapes


def manual_title_candidate(
    session: Session, live_title: str | None, now_ms: int, marker: str | None = None
) -> str | None:
    """The hand-set name *live_title* carries for *session*, or ``None`` (pure).

    ``None`` when: ccc never wrote this tab's title (no baseline to differ from), the
    last write is younger than ``session_names.MANUAL_GRACE_MS`` (it may still be
    landing), the cleaned title is empty or too long, it is one of ccc's own shapes, or
    it already is the name.
    """
    from . import session_names  # lazy

    if live_title is None or not session.title_written:
        return None
    if now_ms - session.title_written_at < session_names.MANUAL_GRACE_MS:
        return None
    cleaned = clean_title(live_title, marker)
    if not cleaned or len(cleaned) > session_names.MAX_MANUAL_CHARS:
        return None
    if session_names.already_named(session, cleaned):
        return None
    if cleaned in ccc_title_shapes(session, marker):
        return None
    return cleaned


def push_title(session: Session, *, marker: str | None = None) -> None:
    """Re-title ONE session's tab now (after its AIM changed). No-op without a tab/badge.

    Never touches a tab whose title the user set by hand (``session_names.titles_frozen``).
    """
    iid = session.iterm_session_id
    if session.done or not iid:
        return
    from . import config, session_names  # lazy

    if session_names.titles_frozen(session):
        return
    badge = assign(iid, folder=session.cwd)
    if not badge:
        return
    core = _session_core(session, badge, config.load_config())
    plain: dict[str, str] = {}
    cas: dict[str, tuple[str, str]] = {}
    writes: list[tuple[str, str]] = []
    _plan_write(session, core, plain, cas, writes)
    _write_titles(plain, cas, _wait_marker(marker))
    if writes:
        from .store import Store  # lazy: the hook / CLI callers hold no store here

        try:
            with Store() as store:
                _record_writes(store, writes)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass  # bookkeeping only: the next sync re-records


def seed_title(
    iterm_session_id: str | None,
    cwd: str,
    *,
    marker: str | None = None,
    session: Session | None = None,
    store: Store | None = None,
) -> str | None:
    """Claim this tab's badge (if unassigned) and seed its iTerm title with it.

    Marker-preserving, so a tab already flagged "waiting" keeps its marker. Called at
    session start so a freshly-launched session's tab shows its badge immediately —
    without waiting for the next ``cd`` (the zsh hook) or the next daemon pass.
    With *session* the title carries its name (``session_names``), a hand-set title is
    left alone, and the write is recorded in *store*. Returns the badge, or ``None``
    when there is nothing to key on. Fail-safe: the title push is detached and swallows
    its own errors.
    """
    badge = assign(iterm_session_id, folder=cwd)
    if not badge or not iterm_session_id:
        return None
    if session is None:
        _write_titles({iterm_session_id: title_core(badge, cwd)}, {}, _wait_marker(marker))
        return badge
    from . import config, session_names  # lazy

    if session_names.titles_frozen(session):
        return badge
    cfg = config.load_config()
    name = session_names.title_name(session, cfg)
    core = title_core(badge, cwd or session.cwd, name=name)
    plain: dict[str, str] = {}
    cas: dict[str, tuple[str, str]] = {}
    writes: list[tuple[str, str]] = []
    seeded = (
        session
        if session.iterm_session_id == iterm_session_id
        else dataclasses.replace(session, iterm_session_id=iterm_session_id)
    )
    _plan_write(seeded, core, plain, cas, writes)
    _write_titles(plain, cas, _wait_marker(marker))
    if store is not None:
        _record_writes(store, writes)
    return badge


def tab_owners(sessions: Iterable[Session]) -> list[Session]:
    """ONE session per iTerm tab — the one the tab is really showing.

    A tab outlives its sessions, so several rows may carry the same tab id; the owner is
    the row whose ``last_seen_pid`` is still running, then the most recently active.
    Writing a title per row instead made the tab flip between the rows' titles.
    """
    from .store import pid_alive  # lazy

    by_tab: dict[str, list[Session]] = {}
    for session in sessions:
        uuid = (session.iterm_session_id or "").split(":")[-1].strip().upper()
        if uuid:
            by_tab.setdefault(uuid, []).append(session)
    owners = []
    for rows in by_tab.values():
        rows.sort(
            key=lambda s: (not pid_alive(s.last_seen_pid), -s.last_response_at, -s.updated_at)
        )
        owners.append(rows[0])
    return owners


def sync_live(store: Store, *, marker: str | None = None) -> list[str]:
    """Ensure every non-done tracked session has a badge AND its iTerm tab shows it.

    For each tab carrying a session's ``iterm_session_id`` (its owner, see
    :func:`tab_owners`): claim a badge if missing (so *every* session gets a symbol) and
    push ``"<badge> <name or leaf>"`` to its tab title, preserving any leading wait
    marker. This is the single convergence point the daemon runs every pass, ``ccc
    tab-symbol --sync`` runs on demand, and the TUI runs on each refresh — it heals tabs
    whose badge was assigned (or reshuffled by palette recycling) after the title was
    last set, which the ``cd``-driven zsh hook can never reach while a CLI holds the
    foreground. With no daemon loaded the TUI refresh is what keeps open tabs in sync
    with their rows.

    With ``session_names`` on it is also where a title the user typed by hand is noticed
    (one ``osascript`` read of the live titles, only when some tab has a ccc baseline):
    it becomes the session's name and that tab is never written again. Every write after
    the first is compare-and-swap. Returns the session ids that were badged.
    """
    import time  # pylint: disable=import-outside-toplevel

    from . import config, session_names  # lazy

    cfg = config.load_config()
    owners = tab_owners(s for s in store.list_sessions() if not s.done and s.iterm_session_id)
    wait_marker = _wait_marker(marker)
    live: dict[str, str] | None = None
    if getattr(cfg, "session_names", False) and any(s.title_written for s in owners):
        from . import tab_titles  # lazy: osascript read

        panes = tab_titles.read_panes()
        live = {p.uuid: p.name for p in panes} if panes is not None else None
    now = int(time.time() * 1000)
    plain: dict[str, str] = {}
    cas: dict[str, tuple[str, str]] = {}
    writes: list[tuple[str, str]] = []
    badged: list[str] = []
    for session in owners:
        iid = session.iterm_session_id or ""
        if live is not None and getattr(store, "conn", None) is not None:
            uuid = iid.split(":")[-1].strip().upper()
            hand = manual_title_candidate(session, live.get(uuid), now, wait_marker)
            if hand and not session_names.already_named(session, hand):
                session_names.adopt_manual_title(store, session.session_id, hand)
                continue
        if session_names.titles_frozen(session):
            continue
        badge = assign(iid, folder=session.cwd)
        if not badge:
            continue
        _plan_write(session, _session_core(session, badge, cfg), plain, cas, writes)
        badged.append(session.session_id)
    _write_titles(plain, cas, wait_marker)
    _record_writes(store, writes)
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


def adopt_overrides(pairs: list[tuple[str, list[str]]], store: Store | None = None) -> int:
    """Make each tab title override the user typed the name of the session in that tab (D3).

    *pairs* are ``(override, session_uuids)`` as the watcher saw them (current session
    first). ccc never writes an override itself (only the badge in front of one), so no
    grace is needed: the cleaned text (badge, wait marker and AIM tail stripped) becomes
    the owner's name with origin ``manual-tab``. Returns the number of names adopted;
    never raises.
    """
    if not pairs:
        return 0
    try:
        from . import config, session_names  # lazy

        if not config.load_config().session_names:
            return 0
        from .store import Store as _Store  # lazy

        adopted = 0
        with contextlib.ExitStack() as stack:
            db = store if store is not None else stack.enter_context(_Store())
            for override, uuids in pairs:
                cleaned = clean_title(override)
                if not cleaned or not uuids or len(cleaned) > session_names.MAX_MANUAL_CHARS:
                    continue
                session = db.session_for_tab_uuid(uuids[0])
                if session is None or session.done or session_names.already_named(session, cleaned):
                    continue
                if session_names.adopt_manual_title(db, session.session_id, cleaned):
                    adopted += 1
        return adopted
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 0


class _OverrideWatch:
    """The watcher's rename callback that also queues changed overrides for adoption."""

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}
        self.pending: list[tuple[str, list[str]]] = []

    def __call__(self, override: str, uuids: list[str]) -> str | None:
        key = uuids[0] if uuids else ""
        if key and self.seen.get(key) != override:
            self.seen[key] = override
            self.pending.append((override, list(uuids)))
        return _rename_with_badge(override, uuids)

    def flush(self) -> int:
        pending, self.pending = self.pending, []
        return adopt_overrides(pending)


WATCH_INTERVAL_SEC = 2.0
_WATCH_RETRY_SEC = 30.0
_WATCH_OP_TIMEOUT_SEC = 15.0


def heal_renamed_tabs_once() -> int:
    """One pass of :func:`watch`: badge every renamed tab now; tabs retitled (0 if offline)."""
    import asyncio  # pylint: disable=import-outside-toplevel

    from . import iterm_api  # pylint: disable=import-outside-toplevel

    api_link = iterm_api.CookieItermLink()
    callback = _OverrideWatch()

    async def _once() -> int:
        if not await asyncio.wait_for(api_link.reconnect(), _WATCH_OP_TIMEOUT_SEC):
            return 0
        return await asyncio.wait_for(
            api_link.retitle_overridden_tabs(callback), _WATCH_OP_TIMEOUT_SEC
        )

    try:
        return asyncio.run(_once())
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return 0
    finally:
        callback.flush()


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
    callback = _OverrideWatch()

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
                    link.retitle_overridden_tabs(callback), _WATCH_OP_TIMEOUT_SEC
                )
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                link.drop()
            callback.flush()
            await asyncio.sleep(interval)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_loop())
    return 0
