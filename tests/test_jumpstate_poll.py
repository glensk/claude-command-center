"""The TUI's idle jump poll is stat-only: nothing pending means no file is read."""

from __future__ import annotations

import pytest

from command_center import jumpstate


def test_idle_poll_reads_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CCC_HOME", str(tmp_path))

    def boom(*_a: object, **_k: object) -> str:
        raise AssertionError("an idle poll must not read a file")

    monkeypatch.setattr(jumpstate, "_read", boom)
    assert jumpstate.any_pending() is False


@pytest.mark.parametrize(
    "setter",
    [jumpstate.request_restart, jumpstate.request_toggle, lambda: jumpstate.request_select("s1")],
)
def test_any_pending_sees_each_signal(monkeypatch: pytest.MonkeyPatch, tmp_path, setter) -> None:
    monkeypatch.setenv("CCC_HOME", str(tmp_path))
    setter()
    assert jumpstate.any_pending() is True
    jumpstate.clear_restart()
    jumpstate.clear_toggle()
    jumpstate.clear_request()
    assert jumpstate.any_pending() is False
