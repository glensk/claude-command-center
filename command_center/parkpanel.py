#!/usr/bin/env python3
"""Floating editable macOS panel to capture a parked prompt ("ccc park panel").

The write-side sibling of the read-only peek panel (:mod:`command_center.peek`):
one dark floating window with a header (repo · account · fire time), a multi-line
editable text view, and two buttons — **Park ⌘↵** and **Cancel ⎋**. Plain Return
types a newline (prompts are multi-line by nature); only ⌘↵ confirms. Unlike the
peek panel it deliberately does NOT close on click-away — half-written prompt text
must never be lost to a stray click.

AppKit/PyObjC is imported lazily inside :func:`capture_prompt` (macOS-only,
GUI-session-only), so importing this module is safe anywhere and non-GUI commands
never pay the AppKit cost. ``CCC_PARK_PANEL_TIMEOUT=<seconds>`` auto-cancels the
panel — a smoke-test hook, never set in normal use.
"""

# AppKit is imported lazily inside each function (GUI cost only when the panel is
# actually shown) and its attributes resolve dynamically through PyObjC.
# pylint: disable=import-outside-toplevel,no-member

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
from typing import Any

# PyObjC classes register globally once per process; keep the lazily-defined
# action target here so a second capture_prompt call never re-declares it.
_LAZY: dict[str, object] = {}


def warm_appkit() -> None:
    """Start importing AppKit on a background thread (it costs ~450 ms).

    Call at command entry, BEFORE any resolution work (osascript, store, usage):
    the framework load then overlaps that work instead of following it, which is
    most of the chord-to-panel latency. Importing a Cocoa framework off the main
    thread is safe — only UI calls are main-thread-bound — and Python's import
    lock makes the later in-function ``import AppKit`` a cheap ``sys.modules``
    hit. Idempotent; the daemon never calls it (no GUI there).
    """
    import threading  # pylint: disable=import-outside-toplevel

    if "warm" not in _LAZY:
        thread = threading.Thread(
            target=__import__, args=("AppKit",), daemon=True, name="appkit-warm"
        )
        thread.start()
        _LAZY["warm"] = thread


def _actions_class() -> object:
    import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,no-member

    if "actions" not in _LAZY:

        class _ParkPanelActions(AppKit.NSObject):
            """Button/timer target: modal-loop control + deferred-resolution poll."""

            def park_(self, _sender: object) -> None:  # noqa: N802 (ObjC selector)
                """⌘↵ / Park button: end the modal loop with 'park' (1)."""
                AppKit.NSApp.stopModalWithCode_(1)

            def cancel_(self, _sender: object) -> None:  # noqa: N802 (ObjC selector)
                """⎋ / Cancel button / smoke timeout: end the modal loop with 0."""
                AppKit.NSApp.stopModalWithCode_(0)

            def tick_(self, timer: Any) -> None:  # noqa: N802 (ObjC selector)
                """Poll the deferred target resolution; fill header/prefill once done.

                The panel opens instantly with a placeholder header while a worker
                thread resolves the target; this timer (modal-panel run-loop mode)
                applies the result on the main thread. The prefill only lands in an
                EMPTY editor — text the user already typed always wins.
                """
                poll = getattr(self, "poll", None)
                result = poll() if poll is not None else None
                if poll is not None and result is None:
                    return  # worker still running — keep polling
                if result is not None:
                    header_text, prefill = result
                    if header_text:
                        self.header_field.setStringValue_(header_text)
                    if prefill and not str(self.text_view.string()).strip():
                        self.text_view.setString_(prefill)
                timer.invalidate()

        _LAZY["actions"] = _ParkPanelActions
    return _LAZY["actions"]


def capture_prompt(  # noqa: PLR0915  pylint: disable=no-member,too-many-statements,too-many-locals
    header: str,
    initial: str = "",
    poll: Any = None,
) -> str | None:
    """Show the park panel and return the typed prompt, or ``None`` on cancel/empty.

    *header* is the one-line context under the title (repo · account · fire time);
    *initial* prefills the text view (e.g. the clipboard with ``-c``). *poll* makes
    the panel open INSTANTLY with *header* as a placeholder: a 100 ms timer calls
    it until it returns ``(resolved_header, prefill)`` — filled in on the main
    thread while the user is already typing (a prefill never overwrites typed
    text). Blocks in a modal loop until ⌘↵ (park), ⎋ (cancel), or the smoke-test
    timeout fires. AppKit attributes resolve dynamically via PyObjC, hence the
    pylint disables.
    """
    import AppKit  # noqa: PLC0415 (lazy: AppKit must not load for non-GUI commands)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    width, height = 760.0, 460.0
    screen = AppKit.NSScreen.mainScreen()
    frame = screen.frame() if screen is not None else AppKit.NSMakeRect(0, 0, 1440, 900)
    rect = AppKit.NSMakeRect(
        frame.origin.x + (frame.size.width - width) / 2.0,
        frame.origin.y + (frame.size.height - height) / 2.0,
        width,
        height,
    )
    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskFullSizeContentView
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, AppKit.NSBackingStoreBuffered, False
    )
    window.setTitle_("ccc park panel")
    window.setTitlebarAppearsTransparent_(True)
    window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    window.setMovableByWindowBackground_(True)
    window.setLevel_(AppKit.NSFloatingWindowLevel)
    window.setReleasedWhenClosed_(False)
    dark = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
    if dark is not None:
        window.setAppearance_(dark)
    for button_kind in (
        AppKit.NSWindowCloseButton,
        AppKit.NSWindowMiniaturizeButton,
        AppKit.NSWindowZoomButton,
    ):
        handle = window.standardWindowButton_(button_kind)
        if handle is not None:
            handle.setHidden_(True)
    window.setBackgroundColor_(
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.09, 0.11, 1.0)
    )

    content = window.contentView()
    pad = 22.0

    def _label(text: str, y: float, *, bold: bool = False, white: bool = False) -> Any:
        field = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(pad, y, width - 2.0 * pad, 22.0)
        )
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setTextColor_(
            AppKit.NSColor.whiteColor() if white else AppKit.NSColor.secondaryLabelColor()
        )
        field.setFont_(
            AppKit.NSFont.boldSystemFontOfSize_(15.0)
            if bold
            else AppKit.NSFont.systemFontOfSize_(12.0)
        )
        field.setStringValue_(text)
        content.addSubview_(field)
        return field

    _label("ccc park panel", height - 36.0, bold=True, white=True)
    _label("write the prompt   ·   ⌘↵  park   ·   ⎋  cancel (nothing saved)", height - 58.0)
    header_field = _label(header, height - 80.0)

    # ── The editable prompt body ──
    buttons_h = 46.0
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(
        AppKit.NSMakeRect(pad, buttons_h + 8.0, width - 2.0 * pad, height - 96.0 - buttons_h - 8.0)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(AppKit.NSNoBorder)
    scroll.setDrawsBackground_(True)
    body_bg = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.13, 0.16, 1.0)
    scroll.setBackgroundColor_(body_bg)
    text = AppKit.NSTextView.alloc().initWithFrame_(
        AppKit.NSMakeRect(0, 0, width - 2.0 * pad, height - 96.0 - buttons_h - 8.0)
    )
    text.setEditable_(True)
    text.setRichText_(False)
    text.setAllowsUndo_(True)
    text.setFont_(
        AppKit.NSFont.monospacedSystemFontOfSize_weight_(13.0, AppKit.NSFontWeightRegular)
    )
    text.setTextColor_(AppKit.NSColor.whiteColor())
    text.setBackgroundColor_(body_bg)
    text.setInsertionPointColor_(AppKit.NSColor.whiteColor())
    text.setTextContainerInset_(AppKit.NSMakeSize(10.0, 10.0))
    text.setAutoresizingMask_(AppKit.NSViewWidthSizable)
    text.setString_(initial or "")
    scroll.setDocumentView_(text)
    content.addSubview_(scroll)

    # ── Park (⌘↵) / Cancel (⎋) — plain Return stays a newline in the text view ──
    target = _actions_class().alloc().init()  # type: ignore[attr-defined]
    park_btn = AppKit.NSButton.buttonWithTitle_target_action_("Park  ⌘↵", target, "park:")
    park_btn.setKeyEquivalent_("\r")
    park_btn.setKeyEquivalentModifierMask_(AppKit.NSEventModifierFlagCommand)
    park_btn.setFrame_(AppKit.NSMakeRect(width - pad - 110.0, 10.0, 110.0, 28.0))
    content.addSubview_(park_btn)
    cancel_btn = AppKit.NSButton.buttonWithTitle_target_action_("Cancel  ⎋", target, "cancel:")
    cancel_btn.setKeyEquivalent_("\x1b")
    cancel_btn.setFrame_(AppKit.NSMakeRect(width - pad - 230.0, 10.0, 110.0, 28.0))
    content.addSubview_(cancel_btn)

    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)
    window.makeFirstResponder_(text)

    if poll is not None:  # deferred target resolution: fill header/prefill when ready
        target.poll = poll
        target.header_field = header_field
        target.text_view = text
        poll_timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, target, "tick:", None, True
        )
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(
            poll_timer, AppKit.NSModalPanelRunLoopMode
        )

    timeout = float(os.environ.get("CCC_PARK_PANEL_TIMEOUT", "0") or 0)
    if timeout > 0:  # smoke-test hook: auto-cancel so a headless check never hangs
        timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            timeout, target, "cancel:", None, False
        )
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSModalPanelRunLoopMode)

    response = app.runModalForWindow_(window)
    result = str(text.string()).strip()
    window.orderOut_(None)
    window.close()
    return result if response == 1 and result else None
