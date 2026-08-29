#!/usr/bin/env python3
"""``ccc peek`` — show this Claude session's prompts (and AIM history) for the focused tab.

Bound to a Karabiner chord (hold ``s``, tap ``p`` while iTerm2 is frontmost), this
answers "what have I asked in *this* tab?": it maps the focused iTerm session to the
Claude Code session running there and shows a small floating panel. A header mirrors
the Claude Code status line — the tab's coloured emoji badge, the session id on its
tab-colour background, and the working dir — above three tabs —

* **prompts** — every human prompt of the session, oldest ``(1)`` at the top down to
  the most recent at the bottom (the view opens scrolled to the bottom). Injected
  ``<task-notification>`` records (background-task completion notices) are filtered
  out by the adapter, so this lists only what the human typed,
* **session** — the full conversation, terminal-like: prompts, Claude's replies and
  dim ``⏺ Tool(input…)`` / ``⎿ result…`` lines (:mod:`command_center.sessionmd` —
  the SAME canonical render the vault session mirror embeds), and
* **aim** — the session's AIM (done-condition) history, oldest first, current last.

When the focused tab is the **ccc TUI itself**, the panel shows the TUI-selected
row's session instead (see :func:`_ccc_selected_session`) — this is how PARKED and
DONE sessions (which have no live tab) are peeked at.

The panel is titled "ccc peek panel" (so it can be referred to by name). ``←`` / ``→``
switch tabs; ``⌘C`` copies the visible tab (selection, else the whole body); ``/`` or
``⌘F`` starts an incremental search over the visible tab (matches highlighted,
``Return`` / ``⇧Return`` cycle them, ``⎋`` leaves search); Space, Return or Escape
close the panel when not searching. It also closes on click-away — the moment it stops
being the key window (the user clicked the terminal or another window).

The mapping reuses the ``iterm_session_id`` ccc already records per session (the
``$ITERM_SESSION_ID`` value, ``w0t1p0:UUID``); the focused tab's UUID comes from
iTerm via AppleScript — or from ``$ITERM_SESSION_ID`` directly when ``ccc peek`` is
run inside the tab's own shell. The AIM history comes from the store, so it is only
available for a tab ccc tracks (the cwd fallback for an untracked tab shows prompts
only).

The AppKit panel is imported lazily inside ``show_panel`` so importing this module
(and every other ``ccc`` command) stays free of the PyObjC / GUI cost, and so the
resolution logic stays importable and unit-testable on any platform.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# Lazy imports (colors, AppKit) keep this module — and so every `ccc` command that
# never peeks — free of their cost; the import sits inside the function on purpose.
# pylint: disable=import-outside-toplevel
import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import sessionmd
from .adapters import ClaudeAdapter
from .models import AimRevision, Session, synthesize_aim_revision
from .store import Store

# iTerm2's CFBundleIdentifier — the Karabiner condition and this module agree on it.
_ITERM_BUNDLE_ID = "com.googlecode.iterm2"
# macOS virtual key codes that dismiss the panel when NOT searching: space, return,
# keypad-enter, escape. In search mode these are re-purposed (space types a space,
# return jumps to the next match, escape leaves search) — see ``_on_key``.
_DISMISS_KEYCODES = {49, 36, 76, 53}
_ESCAPE_KEYCODE = 53  # ⎋ — leave search (or close the panel when not searching)
_RETURN_KEYCODES = frozenset({36, 76})  # Return / keypad-Enter — next match while searching
_DELETE_KEYCODE = 51  # ⌫ — delete the last search character
_SEARCH_KEYCODE = 3  # "f" — ⌘F starts search / jumps to the next match
# macOS virtual key codes for the left / right arrows (switch between the two tabs).
_PREV_TAB_KEYCODE = 123  # ←
_NEXT_TAB_KEYCODE = 124  # →
# macOS virtual key code for "c" — ⌘C copies the visible tab (selection, else all).
_COPY_KEYCODE = 8
# Width of the header rules / separators drawn in the tabs' bodies.
_RULE_WIDTH = 56
# Separator drawn between successive AIM revisions in the aim tab's body.
_BLOCK_SEP = "\n\n" + "─" * _RULE_WIDTH + "\n\n"
# Styling tags emitted by ``prompt_segments`` and consumed by the panel: the
# ``(N) ───`` header rule above each prompt, an ordinary prompt body, and the
# newest prompt's body (rendered pronounced — bold gold — in the panel).
_TAG_RULE = "rule"
_TAG_TEXT = "text"
_TAG_LAST = "last"


def _uuid(iterm_session_id: str | None) -> str | None:
    """The UUID tail of an ``$ITERM_SESSION_ID`` (``w0t1p0:UUID`` → ``UUID``)."""
    if not iterm_session_id:
        return None
    tail = iterm_session_id.split(":")[-1].strip().upper()
    return tail or None


def _osascript(script: str) -> str | None:
    """Run a one-line AppleScript and return its stdout (stripped), or ``None``."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return out or None


def frontmost_iterm_uuid() -> str | None:
    """UUID of the iTerm session the user is looking at, or ``None``.

    Prefers ``$ITERM_SESSION_ID`` (set when ``ccc peek`` runs inside the tab's own
    shell); otherwise asks iTerm for the current session of its current window — the
    path the Karabiner chord takes, where no such env exists.
    """
    env = _uuid(os.environ.get("ITERM_SESSION_ID"))
    if env:
        return env
    out = _osascript(
        'tell application "iTerm2" to tell current session of current window to return id'
    )
    return out.upper() if out else None


def frontmost_iterm_cwd() -> str | None:
    """Working directory of the focused iTerm session (used for the cwd fallback)."""
    return _osascript(
        'tell application "iTerm2" to tell current session of current window '
        'to return variable named "path"'
    )


def _focused_tty() -> str | None:
    """The tty of the focused iTerm session, or ``None``.

    ``None`` when ``ccc peek`` runs inside a session tab's own shell
    (``$ITERM_SESSION_ID`` set — that tab is a session tab, never the ccc TUI), so
    the AppleScript round-trip is skipped on that path.
    """
    if os.environ.get("ITERM_SESSION_ID"):
        return None
    from . import terminal  # lazy: keep module import cheap

    current = terminal.current_iterm_session()
    return current[1] if current else None


def _ccc_selected_session(store: Store) -> Session | None:
    """The TUI-selected session IFF the focused tab is the ccc TUI itself, else ``None``.

    This is how the peek panel reaches PARKED and DONE sessions (which have no live
    tab): focus the ccc TUI, put the cursor on a row, hit the peek chord. Detection
    reuses the exact ``ccc jump`` machinery — the TUI's controlling tty
    (:func:`jump.find_ccc_tty`, a cheap ``ps`` scan, checked FIRST so no AppleScript
    runs when no TUI is up) compared against the focused tab's tty, then the cursor
    row from :func:`jumpstate.get_selected`. Checked BEFORE the uuid → session map: a
    stale uuid mapping recorded on the ccc tab would otherwise shadow the selected row
    (the ccc tab is never a session tab, so this order can't shadow a real match).
    """
    from . import jump, jumpstate  # lazy: keep module import cheap

    ccc_tty = jump.find_ccc_tty()
    if ccc_tty is None or _focused_tty() != ccc_tty:
        return None
    sid = jumpstate.get_selected()
    return store.get(sid) if sid else None


def session_prompts(adapter: ClaudeAdapter, session: Session) -> list[str]:
    """Every human-typed prompt of a tracked *session*, oldest first (cleaned).

    The **single** source the ``ccc peek`` panel and the RUNNING/DONE vault mirrors
    (:mod:`command_center.mirrors`) both read, so the panel's prompt list and the
    mirror's ``## Prompts`` section can never diverge. A thin wrapper over
    :meth:`ClaudeAdapter.all_user_prompts` — the filtering/order/truncation live there
    (``<task-notification>`` and other non-human records are dropped; oldest first; no
    truncation).
    """
    return adapter.all_user_prompts(session.cwd, session.session_id)


def _session_for_uuid(store: Store, uuid: str) -> Session | None:
    """The tracked session whose tab UUID matches, most-recently-active first."""
    matches = [
        session
        for session in store.list_sessions(include_archived=True)
        if _uuid(session.iterm_session_id) == uuid
    ]
    if not matches:
        return None
    matches.sort(key=lambda s: (s.last_response_at, s.updated_at), reverse=True)
    return matches[0]


def _leaf(cwd: str) -> str:
    """Short repo label for the panel subtitle (``…/sdsc/runai-cscs`` → that path)."""
    if not cwd:
        return ""
    from . import colors  # lazy: keep module import cheap

    _category, leaf = colors.folder_split(cwd)
    return leaf or os.path.basename(cwd.rstrip("/"))


def _tab_badge(iterm_session_id: str | None) -> str:
    """The tab's coloured emoji badge (💙 / 🔵 / …) for the header, or ``""``.

    Reads the same per-tab cache the status line and TUI share (``ccc tab-symbol``),
    keyed by the session's stored ``$ITERM_SESSION_ID`` — so the panel shows the exact
    glyph the user sees in the iTerm tab title and the Claude Code status line.
    """
    from . import tabsymbol  # lazy: keep module import cheap

    return tabsymbol.read(iterm_session_id) or ""


def _id_rgb(iterm_session_id: str | None, cwd: str) -> tuple[int, int, int] | None:
    """Session-id background colour: the tab colour, else the repo (cwd) colour.

    Mirrors ``statusline-command.sh``'s resolution chain — the per-tab
    ``iterm-tab-rgb`` cache first, the repo tab colour as fallback — so the id is
    painted with the same background Claude Code shows.
    """
    from . import colors  # lazy: keep module import cheap

    return colors.tab_rgb(iterm_session_id) or colors.folder_rgb(cwd)


@dataclass
class PeekData:  # pylint: disable=too-many-instance-attributes
    """Everything ``ccc peek`` shows for the focused tab — prompts + AIM history.

    The identity fields (``session_id`` / ``cwd`` / ``badge`` / ``id_rgb``) drive the
    panel header, which mirrors the Claude Code status line: the tab's coloured emoji
    badge, the session id painted on its tab-colour background, and the working dir.
    """

    prompts: list[str] = field(default_factory=list)  # every human prompt, oldest first
    aim_revisions: list[AimRevision] = field(default_factory=list)  # AIM progression, oldest first
    # Full-conversation segments for the session tab (sessionmd.session_segments —
    # the SAME canonical render the vault session mirror embeds).
    session_segments: list[tuple[str, str]] = field(default_factory=list)
    label: str = ""  # repo leaf when resolved, else why nothing showed
    resolved: bool = False  # True once a tracked session / transcript was found
    session_id: str = ""  # the Claude session id (full uuid), for the header + resume hint
    cwd: str = ""  # the session's working directory (shown home-collapsed in the header)
    badge: str = ""  # the tab's coloured emoji (💙, 🔵, …), matching iTerm tab + status line
    id_rgb: tuple[int, int, int] | None = None  # session-id background colour (tab colour)


def _peek_for_session(adapter: ClaudeAdapter, store: Store, session: Session) -> PeekData:
    """The full :class:`PeekData` of a tracked *session* (prompts + aim + session tab)."""
    prompts = session_prompts(adapter, session)
    aim_revisions = store.list_aim_history(session.session_id)
    # Pre-history session (AIM set before tracking began): synthesize one
    # revision from the live session so the aim tab is not empty.
    if not aim_revisions and session.aim:
        aim_revisions = [synthesize_aim_revision(session)]
    return PeekData(
        prompts,
        aim_revisions,
        sessionmd.segments_for(adapter, session),
        _leaf(session.cwd),
        resolved=True,
        session_id=session.session_id,
        cwd=session.cwd,
        badge=_tab_badge(session.iterm_session_id),
        id_rgb=_id_rgb(session.iterm_session_id, session.cwd),
    )


def resolve_peek(
    adapter: ClaudeAdapter | None = None,
    store: Store | None = None,
    session_id: str | None = None,
) -> PeekData:
    """Resolve the focused iTerm tab to its prompts + AIM history + full session.

    When *session_id* is given, all focus detection is skipped and that specific
    tracked session is shown directly — this is how the TUI's ``sp`` chord peeks the
    highlighted row (``ccc peek --session <id>``), targeting the exact selected row
    without any tty/uuid guessing.

    Otherwise the resolution order is: (1) the focused tab IS the ccc TUI → the
    TUI-selected row (:func:`_ccc_selected_session` — how parked/done sessions are
    reached); (2) the tab-UUID → tracked-session map (which also yields the AIM
    history); (3) the newest transcript in the focused tab's project directory
    (prompts + session only — an untracked tab has no AIM history). When nothing
    resolves, ``resolved`` is ``False`` and ``label`` says why.
    """
    adapter = adapter or ClaudeAdapter()
    own_store = store is None
    store = store or Store()
    try:
        if session_id is not None:
            session = store.get(session_id)
            if session is None:
                return PeekData(label=f"no tracked session {session_id}")
            return _peek_for_session(adapter, store, session)

        session = _ccc_selected_session(store)
        if session is not None:
            return _peek_for_session(adapter, store, session)

        uuid = frontmost_iterm_uuid()
        if uuid is None:
            return PeekData(label="no focused iTerm session")
        session = _session_for_uuid(store, uuid)
        if session is not None:
            return _peek_for_session(adapter, store, session)

        # Fallback: the focused tab is not tracked by ccc (yet) — read the newest
        # transcript in its project directory directly (no AIM history for it). We can
        # still colour the id by the cwd's repo colour; the badge is unknown here (the
        # per-tab caches are keyed by the full $ITERM_SESSION_ID, not the bare uuid).
        cwd = frontmost_iterm_cwd()
        if cwd:
            project = adapter.projects_dir / cwd.replace("/", "-")
            if project.is_dir():
                transcripts = sorted(project.glob("*.jsonl"), key=_safe_mtime, reverse=True)
                for path in transcripts:
                    prompts = adapter.all_user_prompts_in_file(path)
                    if prompts:
                        return PeekData(
                            prompts,
                            [],
                            sessionmd.segments_for_path(path),
                            _leaf(cwd),
                            resolved=True,
                            session_id=path.stem,
                            cwd=cwd,
                            id_rgb=_id_rgb(None, cwd),
                        )
        return PeekData(label="no Claude session tracked for this tab")
    finally:
        if own_store:
            store.close()


def resolve_prompt(
    adapter: ClaudeAdapter | None = None, store: Store | None = None
) -> tuple[str | None, str]:
    """Back-compat shim: ``(last_prompt_or_None, label)`` for the focused iTerm session.

    ``ccc peek`` now shows every prompt; this returns just the most recent one for
    callers (and tests) that still want the single last prompt.
    """
    data = resolve_peek(adapter=adapter, store=store)
    return (data.prompts[-1] if data.prompts else None), data.label


def _prompt_rule(index: int) -> str:
    """The ``(N) ───…`` header line drawn above prompt *index* (1-based)."""
    prefix = f"({index}) "
    return prefix + "─" * max(4, _RULE_WIDTH - len(prefix))


def prompt_segments(prompts: list[str]) -> list[tuple[str, str]]:
    """The prompts tab body as ``(text, tag)`` segments, oldest first.

    Each prompt is headed by a ``(N) ───…`` rule (tag ``"rule"``) followed by one
    empty line, then the prompt body — the newest body is tagged ``"last"`` so the
    panel can render it pronounced (bold gold), everything else ``"text"``.
    ``format_prompts`` is the tag-free concatenation of these segments, so the plain
    text (``--print``, tests) and the styled panel can never diverge.
    """
    if not prompts:
        return [("(no prompts in this session yet)", _TAG_TEXT)]
    total = len(prompts)
    segments: list[tuple[str, str]] = []
    for index, text in enumerate(prompts, 1):
        if index > 1:
            segments.append(("\n\n", _TAG_TEXT))
        segments.append((_prompt_rule(index) + "\n\n", _TAG_RULE))
        segments.append((text, _TAG_LAST if index == total else _TAG_TEXT))
    return segments


def format_prompts(prompts: list[str]) -> str:
    """All prompts as one scrollable block — oldest ``(1)`` on top, newest at the bottom."""
    return "".join(text for text, _tag in prompt_segments(prompts))


def format_session(segments: list[tuple[str, str]]) -> str:
    """The session tab's plain text — the tag-free concatenation of its segments.

    Identical bytes to the vault session mirror's body (same canonical segments from
    :mod:`command_center.sessionmd`), so the panel and the file can never diverge.
    """
    return "".join(text for text, _tag in segments)


def format_aim(revisions: list[AimRevision]) -> str:
    """The AIM progression as one block — oldest ``(1)`` on top, current marked at the bottom."""
    if not revisions:
        return "(no AIM set for this session yet)"
    from datetime import datetime  # noqa: PLC0415 (lazy: keep module import cheap)

    total = len(revisions)
    blocks: list[str] = []
    for index, rev in enumerate(revisions, 1):
        when = (
            datetime.fromtimestamp(rev.created_at / 1000).strftime("%Y-%m-%d %H:%M")
            if rev.created_at
            else "—"
        )
        score = f"{rev.score}%" if rev.score >= 0 else "—"
        marker = "   ← current" if index == total else ""
        block = f"({index})  {when}  ·  {score}{marker}\n{rev.aim}"
        if rev.short_aim:
            block += f"\n  ↳ short: {rev.short_aim}"
        blocks.append(block)
    return _BLOCK_SEP.join(blocks)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def show_panel(  # noqa: PLR0913,PLR0915  pylint: disable=no-member,too-many-statements,too-many-locals,too-many-arguments,line-too-long
    prompts_text: str,
    aim_text: str,
    subtitle: str,
    *,
    session_id: str = "",
    cwd: str = "",
    badge: str = "",
    id_rgb: tuple[int, int, int] | None = None,
    prompts_segments: list[tuple[str, str]] | None = None,
    session_segments: list[tuple[str, str]] | None = None,
    timeout: float = 0.0,
) -> None:
    """Show *prompts_text* / *aim_text* in a titled two-tab floating macOS panel.

    A **title** line names the panel — "ccc peek panel" (also the window's OS title) —
    so it can be referred to unambiguously ("the ccc peek panel"). Below it a
    header mirrors the Claude Code status line: the tab's coloured *badge* emoji, the
    *session_id* painted on its tab-colour (*id_rgb*) background, and the working
    directory (*cwd*, home-collapsed). The panel has a **prompts** tab and an **aim**
    tab; ``←`` / ``→`` switch between them, ``⌘C`` copies the visible tab (selection,
    else the whole body), and — when not searching — Space / Return / Esc close it.
    Each tab is an independently scrollable text view, opened scrolled to the bottom
    (newest entry). When *prompts_segments* (from :func:`prompt_segments`) is given,
    the prompts tab renders styled: each ``(N) ───`` header rule in blue, the newest
    prompt in bold gold — the plain *prompts_text* is the fallback body otherwise.

    **Search**: ``/`` or ``⌘F`` starts an incremental find over the *visible* tab;
    every match is highlighted amber, the current one orange. ``Return`` (``⇧Return``)
    jumps to the next (previous) match, ``⌫`` edits the query, ``⎋`` leaves search
    (a second ``⎋`` then closes). Search survives ``←`` / ``→`` so a term can be
    chased across both tabs.

    **Click-away**: the panel closes as soon as it stops being the key window — i.e.
    the user clicked the terminal (or any other window) — so it never lingers in the
    foreground. Clicking inside the panel keeps it key, so its own use never closes it.

    PyObjC is imported here (not at module top) so non-GUI commands never load
    AppKit, and so the resolution logic stays importable on any platform. *timeout*
    > 0 auto-dismisses after that many seconds (used by ``--timeout``); 0 waits for a
    key. AppKit attributes are resolved dynamically by PyObjC, hence the disables.
    """
    import AppKit  # noqa: PLC0415 (lazy: AppKit must not load for non-GUI commands)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    width, height = 820.0, 600.0
    screen = AppKit.NSScreen.mainScreen()
    frame = screen.frame() if screen is not None else AppKit.NSMakeRect(0, 0, 1440, 900)
    origin_x = frame.origin.x + (frame.size.width - width) / 2.0
    origin_y = frame.origin.y + (frame.size.height - height) / 2.0
    rect = AppKit.NSMakeRect(origin_x, origin_y, width, height)

    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskFullSizeContentView
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, AppKit.NSBackingStoreBuffered, False
    )
    window.setTitlebarAppearsTransparent_(True)
    window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    window.setMovableByWindowBackground_(True)
    window.setLevel_(AppKit.NSFloatingWindowLevel)
    window.setReleasedWhenClosed_(False)
    # Dark appearance so the tab strip and the scrollers render against the dark panel.
    dark = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
    if dark is not None:
        window.setAppearance_(dark)
    for button in (
        AppKit.NSWindowCloseButton,
        AppKit.NSWindowMiniaturizeButton,
        AppKit.NSWindowZoomButton,
    ):
        handle = window.standardWindowButton_(button)
        if handle is not None:
            handle.setHidden_(True)
    window.setBackgroundColor_(
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.09, 0.11, 1.0)
    )

    content = window.contentView()
    pad = 22.0
    title_y = height - 36.0
    hint_y = height - 58.0
    header_y = height - 90.0
    search_y = height - 126.0
    search_h = 26.0
    box_w = (width - 2.0 * pad) * 0.6
    tabs_h = search_y - 12.0 - pad

    # ── Title: the panel's own name, so it can be referred to ("the ccc peek panel")
    # in conversation — set as the (hidden) window title too for the OS.
    window.setTitle_("ccc peek panel")
    title = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad, title_y, width - 2.0 * pad, 22.0)
    )
    title.setBezeled_(False)
    title.setDrawsBackground_(False)
    title.setEditable_(False)
    title.setSelectable_(False)
    title.setTextColor_(AppKit.NSColor.whiteColor())
    title.setFont_(AppKit.NSFont.boldSystemFontOfSize_(15.0))
    title.setStringValue_("ccc peek panel")
    content.addSubview_(title)

    hint = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad, hint_y, width - 2.0 * pad, 20.0)
    )
    hint.setStringValue_(
        f"{subtitle}   ·   ← / →  tab   ·   /  or  ⌘F  find   ·   ⌘C copy   ·   Space / ⎋  close"
    )
    hint.setBezeled_(False)
    hint.setDrawsBackground_(False)
    hint.setEditable_(False)
    hint.setSelectable_(False)
    hint.setTextColor_(AppKit.NSColor.secondaryLabelColor())
    hint.setFont_(AppKit.NSFont.systemFontOfSize_(12.0))
    content.addSubview_(hint)

    # ── Header: badge · session-id (on its tab-colour background) · working dir ──
    # Rebuilds the status-line identity so the peek panel is unambiguously "this tab".
    header = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad, header_y, width - 2.0 * pad, 26.0)
    )
    header.setBezeled_(False)
    header.setDrawsBackground_(False)
    header.setEditable_(False)
    header.setSelectable_(True)  # so the id / path can be selected and copied
    head_font = AppKit.NSFont.systemFontOfSize_(13.0)
    mono_head = AppKit.NSFont.userFixedPitchFontOfSize_(13.0) or head_font
    fg_attr = AppKit.NSForegroundColorAttributeName
    bg_attr = AppKit.NSBackgroundColorAttributeName
    font_attr = AppKit.NSFontAttributeName
    header_str = AppKit.NSMutableAttributedString.alloc().init()

    def _seg(text: str, attrs: dict) -> None:
        header_str.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        )

    if badge:
        _seg(f"{badge}  ", {font_attr: head_font, fg_attr: AppKit.NSColor.whiteColor()})
    if session_id:
        if id_rgb is not None:
            red, green, blue = id_rgb
            back = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                red / 255.0, green / 255.0, blue / 255.0, 1.0
            )
            lum = (red * 299 + green * 587 + blue * 114) / 1000.0
            fore = AppKit.NSColor.blackColor() if lum > 128 else AppKit.NSColor.whiteColor()
            _seg(f" {session_id} ", {font_attr: mono_head, fg_attr: fore, bg_attr: back})
        else:
            _seg(session_id, {font_attr: mono_head, fg_attr: AppKit.NSColor.whiteColor()})
    home = os.path.expanduser("~")
    pwd_disp = "~" + cwd[len(home) :] if cwd and cwd.startswith(home) else cwd
    if pwd_disp:
        _seg(
            f"   {pwd_disp}", {font_attr: mono_head, fg_attr: AppKit.NSColor.secondaryLabelColor()}
        )
    header.setAttributedStringValue_(header_str)
    content.addSubview_(header)

    # ── Search box (display-only: the local key monitor owns the query string) ──
    search_box = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad, search_y, box_w, search_h)
    )
    search_box.setBezeled_(True)
    search_box.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
    search_box.setEditable_(False)
    search_box.setSelectable_(False)
    search_box.setDrawsBackground_(True)
    search_box.setFont_(AppKit.NSFont.systemFontOfSize_(13.0))
    search_box.setTextColor_(AppKit.NSColor.whiteColor())
    search_box.cell().setPlaceholderString_("🔍  press  /  or  ⌘F  to search")
    content.addSubview_(search_box)

    count_label = AppKit.NSTextField.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad + box_w + 12.0, search_y, width - 2.0 * pad - box_w - 12.0, search_h)
    )
    count_label.setBezeled_(False)
    count_label.setDrawsBackground_(False)
    count_label.setEditable_(False)
    count_label.setSelectable_(False)
    count_label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
    count_label.setFont_(AppKit.NSFont.systemFontOfSize_(12.0))
    content.addSubview_(count_label)

    mono = AppKit.NSFont.userFixedPitchFontOfSize_(15.0) or AppKit.NSFont.systemFontOfSize_(15.0)
    tabs = AppKit.NSTabView.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad, pad, width - 2.0 * pad, tabs_h)
    )
    tabs.setFont_(AppKit.NSFont.systemFontOfSize_(13.0))

    # Segment styling (prompts + session tabs): header rules in a distinct blue, the
    # newest prompt bold gold, the session tab's ⏺/⎿ tool lines dim.
    rule_color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.38, 0.68, 0.90, 1.0)
    gold_color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(1.00, 0.80, 0.25, 1.0)
    bold_mono = AppKit.NSFontManager.sharedFontManager().convertFont_toHaveTrait_(
        mono, AppKit.NSBoldFontMask
    )
    _seg_attrs = {
        _TAG_RULE: {font_attr: mono, fg_attr: rule_color},
        _TAG_LAST: {font_attr: bold_mono, fg_attr: gold_color},
        _TAG_TEXT: {font_attr: mono, fg_attr: AppKit.NSColor.whiteColor()},
        sessionmd.TAG_TOOL: {font_attr: mono, fg_attr: AppKit.NSColor.secondaryLabelColor()},
    }

    def _add_tab(identifier: str, label: str, body: str, segments: list | None = None):
        item = AppKit.NSTabViewItem.alloc().initWithIdentifier_(identifier)
        item.setLabel_(label)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0.0, 0.0, width - 2.0 * pad, tabs_h - 40.0)
        )
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(False)
        # Legacy (non-overlay) scrollers stay permanently visible on the right rather
        # than fading out when idle, so the panel always advertises it is scrollable.
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleLegacy)
        scroll.setDrawsBackground_(False)
        text_view = AppKit.NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setDrawsBackground_(False)
        text_view.setTextColor_(AppKit.NSColor.whiteColor())
        text_view.setFont_(mono)
        text_view.setTextContainerInset_(AppKit.NSMakeSize(6.0, 6.0))
        text_view.setVerticallyResizable_(True)
        text_view.setHorizontallyResizable_(False)
        text_view.textContainer().setWidthTracksTextView_(True)
        if segments:
            styled = AppKit.NSMutableAttributedString.alloc().init()
            for seg_text, tag in segments:
                styled.appendAttributedString_(
                    AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        seg_text, _seg_attrs.get(tag, _seg_attrs[_TAG_TEXT])
                    )
                )
            text_view.textStorage().setAttributedString_(styled)
        else:
            text_view.setString_(body)
        scroll.setDocumentView_(text_view)
        item.setView_(scroll)
        tabs.addTabViewItem_(item)
        return text_view

    prompts_view = _add_tab("prompts", "prompts", prompts_text, segments=prompts_segments)
    session_view = _add_tab(
        "session",
        "session",
        format_session(session_segments) if session_segments else "(no session to show)",
        segments=session_segments,
    )
    aim_view = _add_tab("aim", "aim", aim_text)
    content.addSubview_(tabs)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    def _scroll_to_bottom(text_view) -> None:
        text_view.scrollRangeToVisible_(AppKit.NSMakeRange(text_view.textStorage().length(), 0))

    # Display each tab once so its layout is realised, then scroll it to the newest
    # entry at the bottom; leave the prompts tab selected (and bottom-focused).
    tabs.selectTabViewItemWithIdentifier_("aim")
    _scroll_to_bottom(aim_view)
    tabs.selectTabViewItemWithIdentifier_("session")
    _scroll_to_bottom(session_view)
    tabs.selectTabViewItemWithIdentifier_("prompts")
    _scroll_to_bottom(prompts_view)

    def _dismiss() -> None:
        app.stop_(None)
        # Wake the run loop so stop_ takes effect immediately (it only checks
        # between events, and a key event monitor that swallows its event would
        # otherwise leave the loop idle until the next keystroke).
        nudge = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(  # noqa: E501
            AppKit.NSEventTypeApplicationDefined,
            AppKit.NSMakePoint(0.0, 0.0),
            0,
            0.0,
            0,
            None,
            0,
            0,
            0,
        )
        app.postEvent_atStart_(nudge, True)

    def _copy_visible_tab() -> None:
        # Copy the focused tab's selection to the clipboard — or, when nothing is
        # selected, its whole body (so ⌘C right after the panel opens grabs all of
        # the visible prompts / AIM). An Accessory app has no Edit▸Copy menu item,
        # so the standard ⌘C key-equivalent never fires; do it by hand here.
        item = tabs.selectedTabViewItem()
        text_view = item.view().documentView() if item is not None else None
        if text_view is None:
            return
        body = text_view.string()
        rng = text_view.selectedRange()
        text = body.substringWithRange_(rng) if rng.length > 0 else body
        pasteboard = AppKit.NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)

    # ── In-panel incremental search ─────────────────────────────────────────────
    # The local key monitor owns the query so it works regardless of first responder
    # (the same reason dismissal is monitor-driven). Matches are painted directly onto
    # each tab's text storage, so they show whether or not the view is focused.
    hilite = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.90, 0.75, 0.20, 0.40)  # all
    current = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(1.00, 0.60, 0.10, 0.95)  # active
    # A SimpleNamespace (not a dict) so the heterogeneous fields keep their types for the
    # closures below: on/query/idx are scalar, ranges holds NSRange values.
    from types import SimpleNamespace  # noqa: PLC0415 (lazy: trivial stdlib, keep top clean)

    search = SimpleNamespace(on=False, query="", ranges=[], idx=0)

    def _current_view():
        item = tabs.selectedTabViewItem()
        return item.view().documentView() if item is not None else prompts_view

    def _clear_highlights() -> None:
        for view in (prompts_view, session_view, aim_view):
            storage = view.textStorage()
            storage.removeAttribute_range_(bg_attr, AppKit.NSMakeRange(0, storage.length()))

    def _find_ranges(view, query: str) -> list:
        ranges: list = []
        if not query:
            return ranges
        body = view.string()
        length = body.length()
        start = 0
        while start < length:
            found = body.rangeOfString_options_range_(
                query, AppKit.NSCaseInsensitiveSearch, AppKit.NSMakeRange(start, length - start)
            )
            if found.length == 0:
                break
            ranges.append(found)
            start = found.location + found.length
        return ranges

    def _render_search() -> None:
        search_box.setStringValue_(f"🔍  {search.query}" if (search.on or search.query) else "")
        count = len(search.ranges)
        if search.query and count == 0:
            count_label.setStringValue_("no match")
        elif count:
            count_label.setStringValue_(f"{search.idx + 1}/{count}")
        else:
            count_label.setStringValue_("")

    def _apply_search() -> None:
        view = _current_view()
        _clear_highlights()
        ranges = _find_ranges(view, search.query)
        search.ranges = ranges
        if search.idx >= len(ranges):
            search.idx = 0
        storage = view.textStorage()
        for index, span in enumerate(ranges):
            storage.addAttribute_value_range_(
                bg_attr, current if index == search.idx else hilite, span
            )
        if ranges:
            span = ranges[search.idx]
            view.setSelectedRange_(AppKit.NSMakeRange(span.location, 0))
            view.scrollRangeToVisible_(span)
        _render_search()

    def _enter_search() -> None:
        search.on = True
        _render_search()

    def _leave_search() -> None:
        search.on = False
        search.query = ""
        search.ranges = []
        search.idx = 0
        _clear_highlights()
        _render_search()

    def _jump(delta: int) -> None:
        if not search.ranges:
            return
        search.idx = (search.idx + delta) % len(search.ranges)
        _apply_search()

    def _on_key(event):  # pylint: disable=too-many-branches,too-many-return-statements
        code = event.keyCode()
        flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
        cmd = bool(flags & AppKit.NSEventModifierFlagCommand)
        shift = bool(flags & AppKit.NSEventModifierFlagShift)
        if code == _COPY_KEYCODE and cmd:
            _copy_visible_tab()
            return None  # swallow so the keystroke isn't beeped / re-handled
        if code == _SEARCH_KEYCODE and cmd:  # ⌘F: start search, or jump to next match
            if search.on and search.ranges:
                _jump(1)
            else:
                _enter_search()
            return None
        chars = event.charactersIgnoringModifiers() or ""
        if search.on:
            if code == _ESCAPE_KEYCODE:
                _leave_search()  # first ⎋ leaves search; a second then closes the panel
                return None
            if code in _RETURN_KEYCODES:
                _jump(-1 if shift else 1)  # ⇧Return steps backwards
                return None
            if code == _DELETE_KEYCODE:
                search.query = search.query[:-1]
                search.idx = 0
                _apply_search()
                return None
            if code in (_PREV_TAB_KEYCODE, _NEXT_TAB_KEYCODE):
                # Chase the term across tabs: switch, then re-highlight the new view.
                (
                    tabs.selectPreviousTabViewItem_
                    if code == _PREV_TAB_KEYCODE
                    else tabs.selectNextTabViewItem_
                )(None)
                search.idx = 0
                _apply_search()
                return None
            if chars and not cmd and chars.isprintable():
                search.query += chars
                search.idx = 0
                _apply_search()
                return None
            return None  # swallow every other key while searching (never dismiss)
        if chars == "/":  # start search
            _enter_search()
            return None
        if code in _DISMISS_KEYCODES:
            _dismiss()
            return None  # swallow the dismiss keystroke
        if code == _PREV_TAB_KEYCODE:
            tabs.selectPreviousTabViewItem_(None)
            return None  # swallow so the text view doesn't also move its cursor
        if code == _NEXT_TAB_KEYCODE:
            tabs.selectNextTabViewItem_(None)
            return None
        return event

    # A local monitor catches the keystroke regardless of first responder (the
    # selectable text view would otherwise consume keyDown itself).
    AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, _on_key)

    # Click-away dismissal: close the panel the moment it stops being the key window —
    # i.e. the user clicked the terminal (or any other window). A "has been key at
    # least once" latch guards against a transient resign during the initial
    # ordering/activation dance (which would otherwise close it before it ever showed).
    # Clicking *inside* the panel keeps it key, so it is never dismissed by its own use.
    key_seen = {"yet": False}
    notifications = AppKit.NSNotificationCenter.defaultCenter()

    def _on_become_key(_note) -> None:
        key_seen["yet"] = True

    def _on_resign_key(_note) -> None:
        if key_seen["yet"]:
            _dismiss()

    notifications.addObserverForName_object_queue_usingBlock_(
        AppKit.NSWindowDidBecomeKeyNotification, window, None, _on_become_key
    )
    notifications.addObserverForName_object_queue_usingBlock_(
        AppKit.NSWindowDidResignKeyNotification, window, None, _on_resign_key
    )

    if timeout and timeout > 0:
        AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            timeout, False, lambda _timer: _dismiss()
        )

    app.run()


def run(args: argparse.Namespace) -> int:
    """Resolve the focused tab's prompts + AIM history and show them (or print with --print).

    ``--session <id>`` bypasses focus detection and peeks that exact tracked session
    (the TUI's ``sp`` chord uses it for the highlighted row). Off macOS the floating
    AppKit panel does not exist, so ``ccc peek`` automatically behaves as ``--print``
    (dumps the resolved prompts to stdout) instead of failing on the AppKit import.
    """
    if not getattr(args, "print_only", False) and sys.platform == "darwin":
        # AppKit costs ~450 ms to import — start it loading on a thread NOW so it
        # overlaps the resolution below instead of following it (same trick as the
        # park panel; see parkpanel.warm_appkit).
        from .parkpanel import warm_appkit

        warm_appkit()
    data = resolve_peek(session_id=getattr(args, "session", None))
    prompts_body = (
        format_prompts(data.prompts) if data.resolved else f"No prompt to show — {data.label}."
    )
    if getattr(args, "print_only", False) or sys.platform != "darwin":
        print(prompts_body)
        return 0
    label = data.label if data.resolved else "ccc peek"
    show_panel(
        prompts_body,
        format_aim(data.aim_revisions),
        label,
        session_id=data.session_id,
        cwd=data.cwd,
        badge=data.badge,
        id_rgb=data.id_rgb,
        prompts_segments=prompt_segments(data.prompts) if data.resolved else None,
        session_segments=data.session_segments if data.resolved else None,
        timeout=float(getattr(args, "timeout", 0.0) or 0.0),
    )
    return 0
