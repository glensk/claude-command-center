"""The TUI re-converges iTerm tab titles onto the badge cache, gated on change.

Regression cover for "ccc row/status-line badge disagrees with the iTerm tab":
the row + status line read the badge from the cache (source of truth) while the
tab title is a pushed copy. ``CommandCenterApp._sync_tab_badges`` re-pushes on each
refresh so they stay in lock-step even with no daemon — but only when the badge↔tab
mapping actually moved, so it does not spawn AppleScript every 5 s for no reason.
"""

# pytest fixtures are passed as same-named arguments.
# pylint: disable=redefined-outer-name

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import tabsymbol
from command_center.core import Row
from command_center.models import Session, Status
from command_center.store import Store
from command_center.views import tui
from command_center.views.tui import CommandCenterApp

# The stubbed sync_live never dereferences the store, so a sentinel suffices.
_STORE: Store = object()  # type: ignore[assignment]


def _row(sid: str, iid: str | None, *, done: bool = False) -> Row:
    return Row(
        session=Session(sid, cwd=f"/Users/x/{sid}", iterm_session_id=iid, done=done),
        live=None,
        status=Status.DONE if done else Status.IDLE,
        checked=0,
        total=0,
    )


@pytest.fixture()
def app() -> CommandCenterApp:
    instance = CommandCenterApp()
    instance.store = object()  # type: ignore[assignment]  # truthy; sync_live is stubbed
    instance.cfg.sync_tab_titles = True
    return instance


def _count_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def _stub(_store: object, **_kw: object) -> list[str]:
        calls.append(1)
        return []

    monkeypatch.setattr(tabsymbol, "sync_live", _stub)
    return calls


def test_pushes_once_then_gates_until_mapping_moves(
    monkeypatch: pytest.MonkeyPatch, app: CommandCenterApp
) -> None:
    calls = _count_calls(monkeypatch)
    rows = [_row("a", "w0t0p0:AA"), _row("b", "w0t1p0:BB")]
    app._sync_tab_badges(rows, _STORE)
    assert len(calls) == 1  # first render -> one converge
    app._sync_tab_badges(rows, _STORE)
    assert len(calls) == 1  # identical mapping -> gated, no second AppleScript spawn
    tabsymbol.assign("w0t0p0:AA")  # a badge now exists for AA -> signature moves
    app._sync_tab_badges(rows, _STORE)
    assert len(calls) == 2  # mapping changed -> re-converge


def test_done_and_idless_rows_are_excluded_from_signature(
    monkeypatch: pytest.MonkeyPatch, app: CommandCenterApp
) -> None:
    calls = _count_calls(monkeypatch)
    # Mirrors sync_live's own filter: a done tab and a row with no iTerm id never
    # contribute to the signature, so they cannot trigger spurious re-pushes.
    rows = [_row("a", "w0t0p0:AA"), _row("c", "w0t2p0:CC", done=True), _row("d", None)]
    app._sync_tab_badges(rows, _STORE)
    assert len(calls) == 1
    app._sync_tab_badges(list(rows), _STORE)  # same live mapping (only AA counts) -> gated
    assert len(calls) == 1


def test_disabled_by_config(monkeypatch: pytest.MonkeyPatch, app: CommandCenterApp) -> None:
    calls = _count_calls(monkeypatch)
    app.cfg.sync_tab_titles = False
    app._sync_tab_badges([_row("a", "w0t0p0:AA")], _STORE)
    assert not calls


def test_refresh_worker_noop_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # The store guard lives in _refresh_worker (each run opens its OWN Store; see the
    # cross-thread InterfaceError note there). Unmounted (store None) it returns before
    # touching Store, the worker API, or the badge sync.
    calls = _count_calls(monkeypatch)
    instance = CommandCenterApp()
    instance.store = None
    instance.cfg.sync_tab_titles = True
    instance._refresh_worker()
    assert not calls


def _adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sid: str, extra: str) -> None:
    """tp#1288: `sid` adopted `slack-agent`, whose agent.yaml carries *extra*."""
    staff = tmp_path / "staff" / "slack-agent"
    staff.mkdir(parents=True, exist_ok=True)
    (staff / "agent.yaml").write_text(f"name: slack-agent\n{extra}", encoding="utf-8")
    roles = tmp_path / "tp-cache" / "session-role"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / sid).write_text("slack-agent\n", encoding="utf-8")
    monkeypatch.setenv("TP_STAFF_ROOT", str(tmp_path / "staff"))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp-cache"))


def test_adopting_a_role_moves_the_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app: CommandCenterApp
) -> None:
    calls = _count_calls(monkeypatch)
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp-cache"))
    rows = [_row("a", "w0t0p0:AA")]
    app._sync_tab_badges(rows, _STORE)
    assert len(calls) == 1
    _adopted(tmp_path, monkeypatch, "a", 'symbol: "💬"\n')  # the tab title must change
    app._sync_tab_badges(rows, _STORE)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ('symbol: "💬"\nglyph: "\\uf198"\n', "\uf198 "),  # Nerd glyph, padded to two cells
        ('symbol: "💬"\n', "💬"),
    ],
)
def test_row_badge_of_an_adopted_session_is_its_agent_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, expected: str
) -> None:
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))
    _adopted(tmp_path, monkeypatch, "s1", extra)
    session = Session("s1", cwd="/Users/x/s1", iterm_session_id="w0t1p0:AA")
    assert tui._id_badge(session, live=True) == expected  # pylint: disable=protected-access
    other = Session("s2", cwd="/Users/x/s2", iterm_session_id="w0t2p0:BB")
    assert tui._id_badge(other, live=False) in tabsymbol.PALETTE  # pylint: disable=protected-access
