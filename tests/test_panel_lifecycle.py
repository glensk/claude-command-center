"""Panel teardown: nothing a panel installs may outlive it (tp#70 S2, V9/V3/R3).

Both panels were written for a one-shot process, where process exit collects the local
key monitor, the notification observers and the AppKit timers. In the resident panel
server they would accumulate: a second peek panel would inherit the first's key monitor
(every keystroke handled twice) and its resign-key observer (an instant double dismissal),
and a stale park timeout timer would ``stopModalWithCode_`` a LATER modal session — i.e.
cancel the wrong panel. :meth:`peek.PeekPanel.close` and ``parkpanel.capture_prompt``'s
``finally:`` are the fix; these tests drive both against a stub AppKit so they run
headlessly (no GUI session, no real window server).
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from command_center import parkpanel, peek


class _FakeNSObject:
    """Enough of ``NSObject`` for ``_ParkPanelActions`` to subclass and be allocated."""

    @classmethod
    def alloc(cls) -> _FakeNSObject:
        return cls()

    def init(self) -> _FakeNSObject:
        return self


def _fake_appkit(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a MagicMock ``AppKit`` for the lazily-importing panel functions."""
    fake = MagicMock(name="AppKit")
    fake.NSObject = _FakeNSObject
    # Distinct objects per call, so "which monitor/observer/timer was removed" is testable.
    fake.NSEvent.addLocalMonitorForEventsMatchingMask_handler_.side_effect = lambda *a, **k: (
        MagicMock(name="monitor")
    )
    center = fake.NSNotificationCenter.defaultCenter.return_value
    center.addObserverForName_object_queue_usingBlock_.side_effect = lambda *a, **k: MagicMock(
        name="observer"
    )
    fake.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.side_effect = lambda *a, **k: (
        MagicMock(name="timer")
    )
    fake.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_.side_effect = (
        lambda *a, **k: MagicMock(name="timer")
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake)
    # The ✕ button's target class is cached per process — never let a stub-based one leak.
    monkeypatch.setattr(peek, "_CLOSE_TARGET_CLASS", [])
    return fake


# --------------------------------------------------------------------------- #
# peek: build_panel → PeekPanel.close()
# --------------------------------------------------------------------------- #
def _build(**kwargs: Any) -> peek.PeekPanel:
    return peek.build_panel("prompts body", "aim body", "subtitle", **kwargs)


def test_peek_panel_close_removes_monitor_observers_and_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_appkit(monkeypatch)
    panel = _build(timeout=3.0)

    assert panel.monitor is not None
    assert len(panel.observers) == 2  # become-key + resign-key (the click-away latch)
    assert panel.timer is not None
    monitor, observers, timer = panel.monitor, list(panel.observers), panel.timer

    panel.close()

    fake.NSEvent.removeMonitor_.assert_called_once_with(monitor)
    center = fake.NSNotificationCenter.defaultCenter.return_value
    assert [call.args[0] for call in center.removeObserver_.call_args_list] == observers
    timer.invalidate.assert_called_once_with()
    panel.window.orderOut_.assert_called_once_with(None)
    panel.window.close.assert_called_once_with()
    assert panel.monitor is None and not panel.observers and panel.timer is None


def test_peek_panel_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dismissal path that closes twice must not remove another panel's monitor."""
    fake = _fake_appkit(monkeypatch)
    panel = _build()

    panel.close()
    panel.close()

    assert fake.NSEvent.removeMonitor_.call_count == 1
    assert panel.window.close.call_count == 1


def test_peek_panel_without_timeout_has_no_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``timeout=0`` (the chord's default) schedules nothing to invalidate later."""
    fake = _fake_appkit(monkeypatch)
    panel = _build(timeout=0.0)
    assert panel.timer is None
    fake.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.assert_not_called()


def test_peek_dismissal_goes_through_the_loop_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The auto-dismiss timer stops the injected loop — never ``NSApp.stop_`` directly."""
    fake = _fake_appkit(monkeypatch)
    stops: list[str] = []

    class _Loop:
        def run(self, panel: peek.PeekPanel) -> None:
            del panel

        def stop(self) -> None:
            stops.append("stop")

    _build(timeout=1.0, loop=_Loop())
    # The block the timer was scheduled with is the dismissal path; fire it by hand.
    block = fake.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.call_args.args[2]
    block(MagicMock(name="timer"))
    assert stops == ["stop"]
    fake.NSApplication.sharedApplication.return_value.stop_.assert_not_called()


def test_peek_close_button_dismisses_through_the_loop_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top-right ✕ button's ``close:`` action stops the loop; close() unhooks it."""
    fake = _fake_appkit(monkeypatch)
    stops: list[str] = []

    class _Loop:
        def run(self, panel: peek.PeekPanel) -> None:
            del panel

        def stop(self) -> None:
            stops.append("stop")

    panel = _build(loop=_Loop())
    title, target, action = fake.NSButton.buttonWithTitle_target_action_.call_args.args
    assert (title, action) == ("✕", "close:")
    assert target is panel.close_target
    target.close_(None)
    assert stops == ["stop"]
    panel.close()
    assert panel.close_target is None and target.on_close is None
    target.close_(None)  # a late click after close is a no-op
    assert stops == ["stop"]


# --------------------------------------------------------------------------- #
# park: capture_prompt(on_shown=…) + finally: cleanup
# --------------------------------------------------------------------------- #
def _park_appkit(monkeypatch: pytest.MonkeyPatch, typed: str, response: int) -> Any:
    fake = _fake_appkit(monkeypatch)
    app = fake.NSApplication.sharedApplication.return_value
    app.runModalForWindow_.return_value = response
    fake.NSTextView.alloc.return_value.initWithFrame_.return_value.string.return_value = typed
    # The action class is built against AppKit and cached per process — give this test its
    # own cache so the stub class never leaks into another test (or into real use).
    monkeypatch.setattr(parkpanel, "_LAZY", {})
    return fake


def test_capture_prompt_invalidates_both_timers_and_drops_its_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _park_appkit(monkeypatch, typed="typed prompt", response=1)
    monkeypatch.setenv("CCC_PARK_PANEL_TIMEOUT", "5")  # schedules the smoke-timeout timer

    result = parkpanel.capture_prompt("header", "", poll=lambda: ("resolved", ""))

    assert result == "typed prompt"
    timers = [
        call.args[0]
        for call in fake.NSRunLoop.currentRunLoop.return_value.addTimer_forMode_.call_args_list
    ]
    assert len(timers) == 2  # the 100 ms resolution poll AND the smoke timeout
    for timer in timers:
        timer.invalidate.assert_called_once_with()
    window = (
        fake.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
    )
    window.orderOut_.assert_called_once_with(None)
    window.close.assert_called_once_with()

    # The action target outlives this call (a PyObjC instance); its references to the
    # closed window's views and to the caller's poll must be gone.
    target = fake.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_.call_args_list[
        0
    ].args[1]
    assert (target.poll, target.header_field, target.text_view) == (None, None, None)


def test_capture_prompt_cleans_up_even_when_the_modal_loop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash inside the modal session must not leave a live timer behind (R3)."""
    fake = _park_appkit(monkeypatch, typed="x", response=1)
    monkeypatch.setenv("CCC_PARK_PANEL_TIMEOUT", "5")
    app = fake.NSApplication.sharedApplication.return_value
    app.runModalForWindow_.side_effect = RuntimeError("modal blew up")

    with pytest.raises(RuntimeError):
        parkpanel.capture_prompt("header", "", poll=lambda: ("resolved", ""))

    timers = [
        call.args[0]
        for call in fake.NSRunLoop.currentRunLoop.return_value.addTimer_forMode_.call_args_list
    ]
    assert timers and all(t.invalidate.call_count == 1 for t in timers)


def test_capture_prompt_on_shown_fires_after_display_and_before_the_modal_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server's ack hook: the pixels exist, and the call has not blocked yet (D5/D11)."""
    fake = _park_appkit(monkeypatch, typed="typed prompt", response=1)
    monkeypatch.delenv("CCC_PARK_PANEL_TIMEOUT", raising=False)
    order: list[str] = []
    window = (
        fake.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
    )
    window.displayIfNeeded.side_effect = lambda: order.append("display")
    app = fake.NSApplication.sharedApplication.return_value

    def _modal(_window: Any) -> int:
        order.append("modal")
        return 1

    app.runModalForWindow_.side_effect = _modal

    parkpanel.capture_prompt("header", "", on_shown=lambda: order.append("shown"))

    assert order == ["display", "shown", "modal"]


def test_capture_prompt_without_on_shown_leaves_the_cold_path_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hook → no extra ``displayIfNeeded``: the cold panel behaves exactly as before."""
    fake = _park_appkit(monkeypatch, typed="typed prompt", response=1)
    monkeypatch.delenv("CCC_PARK_PANEL_TIMEOUT", raising=False)

    parkpanel.capture_prompt("header")

    window = (
        fake.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
    )
    window.displayIfNeeded.assert_not_called()


def test_capture_prompt_raising_on_shown_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V19: the server's ack write raising must not leak the window or either timer."""
    fake = _park_appkit(monkeypatch, typed="x", response=1)
    monkeypatch.setenv("CCC_PARK_PANEL_TIMEOUT", "5")
    app = fake.NSApplication.sharedApplication.return_value

    def _boom() -> None:
        raise OSError("ack write failed")

    with pytest.raises(OSError):
        parkpanel.capture_prompt("header", "", poll=lambda: ("resolved", ""), on_shown=_boom)

    app.runModalForWindow_.assert_not_called()
    timers = [
        call.args[0]
        for call in fake.NSRunLoop.currentRunLoop.return_value.addTimer_forMode_.call_args_list
    ]
    assert len(timers) == 2 and all(t.invalidate.call_count == 1 for t in timers)
    window = (
        fake.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
    )
    window.orderOut_.assert_called_once_with(None)
    window.close.assert_called_once_with()


def test_build_panel_failure_releases_what_it_already_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build that raises after the monitor + first observer exist removes both (D5)."""
    fake = _fake_appkit(monkeypatch)
    center = fake.NSNotificationCenter.defaultCenter.return_value
    installed: list[Any] = []

    def _observer(*_a: Any, **_k: Any) -> Any:
        if installed:
            raise RuntimeError("second observer failed")
        installed.append(MagicMock(name="observer"))
        return installed[0]

    center.addObserverForName_object_queue_usingBlock_.side_effect = _observer

    with pytest.raises(RuntimeError):
        _build(timeout=3.0)

    fake.NSEvent.removeMonitor_.assert_called_once()
    assert [call.args[0] for call in center.removeObserver_.call_args_list] == installed
    window = (
        fake.NSWindow.alloc.return_value.initWithContentRect_styleMask_backing_defer_.return_value
    )
    window.close.assert_called_once_with()
    fake.NSTimer.scheduledTimerWithTimeInterval_repeats_block_.assert_not_called()
