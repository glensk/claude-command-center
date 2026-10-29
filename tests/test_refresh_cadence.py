"""tp#1427: restricted reconcile, change-driven rebuilds and the diffed table.

Everything here is deterministic: a fake clock for the gate, injected change signals for the
TUI, and a stub adapter for the core build — no sleeps used for timing.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from command_center import refreshgate
from command_center.core import build_rows
from command_center.models import LiveSession, Status, now_ms
from command_center.store import Store

_DAY = 86_400_000
_BASE = "/repo-root"


@pytest.fixture(autouse=True)
def _repo_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_BASE", _BASE)


class _Adapter:  # pylint: disable=unused-argument  # a stub: signatures mirror the adapter
    name = "claude"

    def __init__(self, live: list[LiveSession] | None = None) -> None:
        self._live = live or []

    def discover(self) -> list[LiveSession]:
        return self._live

    def last_activity_ms(self, live: LiveSession) -> int:
        return 0

    def is_oneshot_headless(self, cwd: str, session_id: str) -> bool:
        return False

    def is_halted(self, cwd: str, session_id: str) -> bool:
        return False

    def claude_version(self, cwd: str, session_id: str) -> str | None:
        return None

    def probe(self) -> bool:
        return True


def _seed_mixed(store: Store) -> None:
    store.ensure("active", cwd=f"{_BASE}/sdsc/a")
    store.update_fields("active", status=Status.IDLE.value, aim="work")
    store.ensure("parked", cwd=f"{_BASE}/sdsc/b")
    store.update_fields("parked", status=Status.PARKED.value)
    store.ensure("done-recent", cwd=f"{_BASE}/home/c")
    store.update_fields("done-recent", done=True, status=Status.DONE.value, done_at=now_ms() - _DAY)
    store.ensure("done-old", cwd=f"{_BASE}/home/d")
    store.update_fields(
        "done-old", done=True, status=Status.DONE.value, done_at=now_ms() - 30 * _DAY
    )
    store.ensure("done-resumed", cwd=f"{_BASE}/home/e")  # old, but live again
    store.update_fields(
        "done-resumed", done=True, status=Status.DONE.value, done_at=now_ms() - 30 * _DAY
    )
    store.ensure("done-nostamp", cwd=f"{_BASE}/home/f")  # no finish stamp → never aged out
    store.update_fields("done-nostamp", done=True, status=Status.DONE.value)
    store.ensure("archived", cwd=f"{_BASE}/home/g")
    store.update_fields("archived", status=Status.PARKED.value, archived=True)
    draft = store.create_draft("draft-1", f"{_BASE}/llms/x", "later", "p")
    assert draft.draft


def _snapshot(rows: list[Any]) -> list[tuple[Any, ...]]:
    return [
        (r.session.session_id, r.status, r.checked, r.total, r.uses_codex_workflow, r.dep_depth)
        for r in rows
    ]


@pytest.mark.parametrize("include_done", [False, True])
@pytest.mark.parametrize("days", [0, 7])
def test_restricted_build_gives_the_same_rows_as_the_full_one(
    tmp_path: Path, include_done: bool, days: int
) -> None:
    store = Store(tmp_path / "s.db")
    _seed_mixed(store)
    live = [LiveSession(pid=1, session_id="done-resumed", cwd=f"{_BASE}/home/e", alive=True)]
    full = build_rows(
        store, _Adapter(live), include_done=include_done, done_max_age_days=days, restrict=False
    )
    cut = build_rows(
        store, _Adapter(live), include_done=include_done, done_max_age_days=days, restrict=True
    )
    assert _snapshot(cut) == _snapshot(full)
    ids = {r.session.session_id for r in cut}
    assert "done-resumed" in ids  # a resumed done session is still picked up
    assert "archived" not in ids
    assert any(r.is_draft for r in cut)  # drafts survive the restriction
    store.close()


def test_restricted_pass_loads_and_reconciles_only_the_relevant_rows(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    _seed_mixed(store)
    relevant = {
        s.session_id
        for s in store.list_relevant_sessions(
            [], include_done=True, done_cutoff_ms=now_ms() - 7 * _DAY
        )
    }
    assert "done-old" not in relevant and "done-resumed" not in relevant
    assert {"active", "parked", "done-recent", "done-nostamp"} <= relevant
    hidden = {
        s.session_id for s in store.list_relevant_sessions(["done-resumed"], include_done=False)
    }
    assert "done-resumed" in hidden and "done-recent" not in hidden
    # The parking loop must not touch rows outside the restriction: an old done row keeps
    # its stored status although it is not live.
    store.update_fields("done-old", status=Status.PARKED.value)  # stale stamp a full pass heals
    build_rows(store, _Adapter(), include_done=True, done_max_age_days=7, restrict=True)
    old = store.get("done-old")
    assert old is not None and old.status == Status.PARKED.value  # untouched
    build_rows(store, _Adapter(), include_done=True, done_max_age_days=7)  # full pass heals it
    healed = store.get("done-old")
    assert healed is not None and healed.status == Status.DONE.value
    store.close()


# ---------------------------------------------------------------- fix 4: the gate
class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _gate(clock: _Clock) -> refreshgate.RefreshGate:
    return refreshgate.RefreshGate(force_after_s=30.0, floor_s=1.0, clock=clock)


def test_gate_builds_first_then_waits_for_a_signal_or_the_forced_age() -> None:
    clock = _Clock()
    gate = _gate(clock)
    files: refreshgate.FilesSignature = (("a", 1, 1),)
    assert gate.due(files, 5) == "first"
    gate.begin(files)
    clock.now += 0.2  # a fast build
    gate.end(db_version=5)
    clock.now += 2.0
    assert gate.due(files, 5) is None  # nothing changed
    assert gate.due(files, 6) == "db"
    assert gate.due((("a", 2, 1),), 5) == "files"
    clock.now += 28.0  # 30 s since the build ended
    assert gate.due(files, 5) == "age"


def test_gate_never_starts_a_build_inside_max_floor_twice_the_last_duration() -> None:
    clock = _Clock()
    gate = _gate(clock)
    files: refreshgate.FilesSignature = ()
    gate.begin(files)
    clock.now += 3.0  # a slow build under load
    gate.end(db_version=1)
    assert gate.min_gap_s == 6.0
    clock.now += 5.9
    assert gate.due(files, 2) is None  # changed, but still inside 2 × 3 s
    clock.now += 0.2
    assert gate.due(files, 2) == "db"
    # a fast build falls back to the 1 s floor
    gate.begin(files)
    clock.now += 0.1
    gate.end(db_version=2)
    assert gate.min_gap_s == 1.0


def test_gate_a_commit_during_the_build_is_remembered_but_its_own_writes_are_not() -> None:
    clock = _Clock()
    gate = _gate(clock)
    files: refreshgate.FilesSignature = ()
    gate.begin(files)
    clock.now += 0.1
    gate.end(db_version=9)  # the build's own commits are inside the baseline
    clock.now += 2.0
    assert gate.due(files, 9) is None
    gate.begin(files)
    clock.now += 0.1
    gate.end(db_version=9, foreign_write=True)  # another process committed mid-build
    clock.now += 2.0
    assert gate.due(files, 9) == "dirty"
    gate.begin(files)  # the next build clears it
    gate.end(db_version=9)
    clock.now += 2.0
    assert gate.due(files, 9) is None


def test_data_version_moves_for_another_connection_only(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    mine = sqlite3.connect(db)
    mine.execute("CREATE TABLE t (x)")
    mine.commit()
    base = refreshgate.data_version(mine)
    mine.execute("INSERT INTO t VALUES (1)")
    mine.commit()
    assert refreshgate.data_version(mine) == base  # its own commit: unchanged
    other = sqlite3.connect(db)
    other.execute("INSERT INTO t VALUES (2)")
    other.commit()
    other.close()
    assert refreshgate.data_version(mine) != base  # a foreign commit moves it
    mine.close()


def test_files_signature_sees_registry_and_transcript_changes(tmp_path: Path) -> None:
    reg = tmp_path / "sessions"
    reg.mkdir()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("a")
    (reg / "1.json").write_text("{}")
    (reg / "notes.txt").write_text("ignored")
    before = refreshgate.files_signature([reg, tmp_path / "missing"], [transcript])
    assert len(before) == 2  # one registry entry + one transcript; the .txt is ignored
    transcript.write_text("ab")
    assert refreshgate.files_signature([reg], [transcript]) != before
    after_append = refreshgate.files_signature([reg], [transcript])
    (reg / "2.json").write_text("{}")
    assert refreshgate.files_signature([reg], [transcript]) != after_append
    transcript.unlink()
    assert refreshgate.files_signature([reg], [transcript])[-1][1:] == (-1, -1)


# ---------------------------------------------------------------- TUI wiring
async def _settle(pilot: Any) -> None:
    while any(not w.is_finished for w in pilot.app.workers):
        await pilot.pause()
    await pilot.pause()


def _tui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("tui-a", cwd=f"{_BASE}/sdsc/a")
    store.update_fields("tui-a", aim="one", next_step="- n")
    store.ensure("tui-b", cwd=f"{_BASE}/sdsc/b")
    store.update_fields("tui-b", aim="two")
    store.close()


def test_tick_rebuilds_only_on_change_and_repaints_usage_on_its_own_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tui_env(tmp_path, monkeypatch)
    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp

    builds: list[str] = []
    real = tui_mod.build_rows

    def counting(*args: Any, **kwargs: Any) -> Any:
        builds.append("build")
        assert kwargs.get("restrict") is True  # the TUI always asks for the restricted pass
        return real(*args, **kwargs)

    monkeypatch.setattr(tui_mod, "build_rows", counting)

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            clock = _Clock()
            app._gate = refreshgate.RefreshGate(force_after_s=30.0, floor_s=1.0, clock=clock)
            signal = {"db": 1, "files": (("f", 1, 1),)}
            monkeypatch.setattr(app, "_db_version", lambda: signal["db"])
            monkeypatch.setattr(app, "_files_signature", lambda: signal["files"])
            app.refresh_data()  # explicit action: builds at once and seeds the baseline
            await _settle(pilot)
            baseline = len(builds)
            clock.now += 5
            app._tick()
            await _settle(pilot)
            assert len(builds) == baseline  # nothing changed → no build

            signal["db"] = 2  # another process committed
            app._tick()
            await _settle(pilot)
            assert len(builds) == baseline + 1
            clock.now += 5
            app._tick()
            await _settle(pilot)
            assert len(builds) == baseline + 1  # the new baseline absorbed it

            signal["files"] = (("f", 2, 1),)  # a live transcript grew
            clock.now += 5
            app._tick()
            await _settle(pilot)
            assert len(builds) == baseline + 2

            clock.now += 31  # no signal, but 6 × usage_refresh_sec passed
            app._tick()
            await _settle(pilot)
            assert len(builds) == baseline + 3

            # Inside max(1 s, 2 × duration) of the last build end: a change must wait.
            signal["db"] = 3
            clock.now += 0.5
            app._tick()
            await _settle(pilot)
            assert len(builds) == baseline + 3

    asyncio.run(scenario())


def test_usage_cards_repaint_every_usage_refresh_sec_without_a_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tui_env(tmp_path, monkeypatch)
    from command_center.views import tui as tui_mod
    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.cfg.usage_refresh_sec = 5.0
            clock = _Clock()
            app._gate = refreshgate.RefreshGate(force_after_s=1e9, floor_s=1.0, clock=clock)
            monkeypatch.setattr(app, "_db_version", lambda: 1)
            monkeypatch.setattr(app, "_files_signature", lambda: ())
            app.refresh_data()
            await _settle(pilot)
            paints: list[int] = []
            monkeypatch.setattr(app, "_update_usage", lambda: paints.append(1))
            monkeypatch.setattr(tui_mod, "monotonic", lambda: app._last_usage_paint + 4.0)
            app._tick()
            assert not paints  # < usage_refresh_sec since the last paint
            monkeypatch.setattr(tui_mod, "monotonic", lambda: app._last_usage_paint + 5.0)
            app._tick()
            assert paints == [1]

    asyncio.run(scenario())


def test_the_files_signature_and_change_check_are_cheap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tui_env(tmp_path, monkeypatch)
    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            start = time.perf_counter()
            for _ in range(20):
                app._files_signature()
                app._db_version()
            per_check_ms = (time.perf_counter() - start) / 20 * 1000
            assert per_check_ms < 5.0

    asyncio.run(scenario())


# ---------------------------------------------------------------- fix 5: diffed table
def test_apply_rows_updates_only_changed_cells_and_keeps_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tui_env(tmp_path, monkeypatch)
    from textual.widgets import DataTable

    from command_center.views.tui import CommandCenterApp

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            table = app.query_one("#sessions", DataTable)
            rows = list(app._rows.values())
            assert len(rows) == 2
            app.update_detail = lambda: None  # type: ignore[method-assign]  # not under test
            table.move_cursor(row=2)
            cursor = table.cursor_coordinate
            clears: list[int] = []
            updates: list[tuple[str, Any]] = []
            real_clear = table.clear
            real_update = table.update_cell

            def spy_clear(*args: Any, **kwargs: Any) -> Any:
                clears.append(1)
                return real_clear(*args, **kwargs)

            def spy_update(row_key: Any, column_key: Any, value: Any, **kwargs: Any) -> None:
                updates.append((str(row_key), value))
                real_update(row_key, column_key, value, **kwargs)

            monkeypatch.setattr(table, "clear", spy_clear)
            monkeypatch.setattr(table, "update_cell", spy_update)

            app._apply_rows(rows)  # identical rows → zero updates, no clear
            assert not clears and not updates

            changed = rows[0]
            changed.session.next_step = "- a brand new next step"
            app._apply_rows(rows)
            assert not clears  # same keys / order → a diff, not a rebuild
            assert {key for key, _ in updates} == {changed.session.session_id}
            assert table.cursor_coordinate == cursor  # selection untouched
            texts = [v.plain for _, v in updates if isinstance(v, Text)]
            assert any("brand new next step" in t for t in texts)

            # A base-style-only change (same plain text) is still a change.
            updates.clear()
            app._drawn[0] = (app._drawn[0][0], tuple(Text("x") for _ in app._drawn[0][1]))
            app._drawn[0][1][0].stylize("bold")
            app._apply_rows(rows)
            assert updates and not clears

            # A different shape (one row fewer) falls back to the full rebuild.
            app._apply_rows(rows[:1])
            assert clears == [1]
            assert table.row_count == len(app._drawn)

    asyncio.run(scenario())


def test_diffed_table_renders_exactly_what_a_full_rebuild_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alignment guard: diff-updated cells equal the cells a clean rebuild produces."""
    _tui_env(tmp_path, monkeypatch)
    from textual.widgets import DataTable

    from command_center.views.tui import CommandCenterApp

    def cells(table: DataTable) -> list[list[str]]:
        # Session rows only: the rule separators are repainted post-layout in both paths.
        return [
            [c.plain if isinstance(c, Text) else str(c) for c in table.get_row_at(i)]
            for i, row in enumerate(table.ordered_rows)
            if not str(row.key.value).startswith("__sep__")
        ]

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            table = app.query_one("#sessions", DataTable)
            rows = list(app._rows.values())
            app.update_detail = lambda: None  # type: ignore[method-assign]
            rows[1].session.next_step = "- changed"
            rows[1].session.aim = "a different aim"
            app._apply_rows(rows)  # diff path
            diffed = cells(table)
            app._drawn = []  # force the full path
            app._apply_rows(rows)
            assert cells(table) == diffed
            widths = {len(r) for r in diffed}
            assert len(widths) == 1  # every row keeps the full column set (blank cells included)

    asyncio.run(scenario())
