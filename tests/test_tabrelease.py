"""Tests for the dead-tab release rule and its missing-since log (tp#1990)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from command_center import iterm_api, tab_titles, tabrelease, tabsymbol
from command_center.models import LiveSession, TabbedRow
from command_center.store import Store
from command_center.tab_titles import ItermPane
from command_center.tabrelease import (
    MissingLog,
    ReleaseResult,
    badge_uuid,
    gone_rows,
    protected_uuids,
    release_gone,
    stale_badge_files,
    sweep_badges,
    update_missing,
)

_GONE = "11111111-0000-4000-8000-000000000001"
_LIVE = "22222222-0000-4000-8000-000000000002"
_OTHER = "33333333-0000-4000-8000-000000000003"
_GRACE = tabrelease.TAB_GONE_GRACE_MS
_T0 = 1_800_000_000_000
_DEAD_PID = 4_000_001


def _dead(_pid: int) -> bool:
    return False


def _alive(_pid: int) -> bool:
    return True


def _row(sid: str, uuid: str = _GONE, pid: int | None = _DEAD_PID, **over: object) -> TabbedRow:
    fields: dict = {
        "session_id": sid,
        "iterm_session_id": f"w0t1p0:{uuid}",
        "last_seen_pid": pid,
        "status": "parked",
    }
    fields.update(over)
    return TabbedRow(**fields)


def _released(
    rows: list[TabbedRow],
    missing: dict[str, int],
    now: int,
    *,
    live_ids: frozenset[str] = frozenset(),
    alive: Callable[[int], bool] = _dead,
) -> set[str]:
    got = gone_rows(rows, missing, set(live_ids), now, _GRACE, alive=alive)
    return {r.session_id for r in got}


def test_dead_pid_and_missing_past_grace_is_released() -> None:
    missing = {_GONE: _T0}
    assert _released([_row("s1")], missing, _T0 + _GRACE) == {"s1"}


def test_missing_inside_grace_is_kept() -> None:
    missing = {_GONE: _T0}
    assert not _released([_row("s1")], missing, _T0 + _GRACE - 1)
    assert not _released([_row("s1")], {}, _T0 + 10 * _GRACE)  # never observed missing


def test_seen_again_drops_entry_and_later_absence_restarts_clock() -> None:
    refs = [_GONE]
    log = update_missing({}, refs, [_LIVE], _T0)
    assert log == {_GONE: _T0}
    log = update_missing(log, refs, [_LIVE, _GONE], _T0 + _GRACE // 2)
    assert not log  # the pane came back: its entry is gone
    later = _T0 + 2 * _GRACE
    log = update_missing(log, refs, [_LIVE], later)
    assert log == {_GONE: later}  # clock restarted
    assert not _released([_row("s1")], log, later + _GRACE - 1)
    assert _released([_row("s1")], log, later + _GRACE) == {"s1"}


def test_partial_snapshot_omitting_a_pane_once_releases_nothing() -> None:
    refs = [_GONE, _LIVE]
    log = update_missing({}, refs, [_GONE, _LIVE], _T0)
    assert not log
    log = update_missing(log, refs, [_LIVE], _T0 + 1)  # partial restore snapshot
    log = update_missing(log, refs, [_GONE, _LIVE], _T0 + 2)  # back on the next read
    assert not _released([_row("s1")], log, _T0 + 10 * _GRACE)


def test_update_missing_keeps_first_stamp_and_prunes_unreferenced() -> None:
    log = update_missing({_GONE: _T0, _OTHER: _T0}, [_GONE], [_LIVE], _T0 + 5)
    assert log == {_GONE: _T0}  # first stamp kept, _OTHER no longer referenced
    lower = update_missing({}, [f"w1t2p0:{_GONE.lower()}"], [], _T0)
    assert lower == {_GONE: _T0}  # keys are the upper-cased UUID tail


@pytest.mark.parametrize("pid", [None, 0, -5])
def test_unknown_pid_is_kept(pid: int | None) -> None:
    assert not _released([_row("s1", pid=pid)], {_GONE: _T0}, _T0 + _GRACE)


def test_alive_pid_is_kept() -> None:
    assert not _released([_row("s1")], {_GONE: _T0}, _T0 + _GRACE, alive=_alive)


def test_alive_in_live_registry_is_kept() -> None:
    assert not _released([_row("s1")], {_GONE: _T0}, _T0 + _GRACE, live_ids=frozenset({"s1"}))


def test_draft_is_kept() -> None:
    assert not _released([_row("s1", draft=True)], {_GONE: _T0}, _T0 + _GRACE)


def test_done_and_archived_rows_are_released() -> None:
    rows = [_row("done", done=True, status="done"), _row("arch", archived=True)]
    assert _released(rows, {_GONE: _T0}, _T0 + _GRACE) == {"done", "arch"}


def test_three_rows_on_one_missing_uuid_all_released() -> None:
    rows = [_row("a"), _row("b"), _row("c"), _row("live", uuid=_LIVE)]
    assert _released(rows, {_GONE: _T0}, _T0 + _GRACE) == {"a", "b", "c"}


def test_default_probe_uses_store_pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake(pid: int | None) -> bool:
        calls.append(int(pid or 0))
        return False

    monkeypatch.setattr("command_center.store.pid_alive", fake)
    got = gone_rows([_row("s1")], {_GONE: _T0}, set(), _T0 + _GRACE)
    assert [r.session_id for r in got] == ["s1"] and calls == [_DEAD_PID]


def test_missing_log_roundtrip(tmp_path: Path) -> None:
    log = MissingLog(tmp_path / "home" / "tab-missing.json")
    assert not log.load()
    got = log.observe([_GONE, _LIVE], [_LIVE], _T0)
    assert got == {_GONE: _T0}
    assert json.loads(log.path.read_text(encoding="utf-8")) == {_GONE: _T0}
    assert MissingLog(log.path).observe([_GONE], [], _T0 + 99) == {_GONE: _T0}
    assert not list(log.path.parent.glob("*.tmp"))


@pytest.mark.parametrize("junk", ["{not json", "[1, 2]", '{"X": "soon"}', '{"X": true}', ""])
def test_corrupt_missing_log_is_empty_and_rewritten(tmp_path: Path, junk: str) -> None:
    path = tmp_path / "tab-missing.json"
    path.write_text(junk, encoding="utf-8")
    log = MissingLog(path)
    assert not log.load()
    assert not log.observe([_GONE], [_GONE], _T0)
    assert json.loads(path.read_text(encoding="utf-8")) == {}  # rewritten, well-formed


def test_observe_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "tab-missing.json"
    assert MissingLog(path).observe([_GONE], [], _T0, write=False) == {_GONE: _T0}
    assert not path.exists()


def test_missing_log_defaults_to_the_app_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCC_HOME", str(tmp_path / "ccc"))
    assert MissingLog().path == tmp_path / "ccc" / "command-center" / "tab-missing.json"


# ---- B2: badge cache sweep -----------------------------------------------------------


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}".upper()


def _badge(directory: Path, uuid: str, badge: str, *, folder: str = "/repo") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"w0t{int(uuid[:8], 16)}p0_{uuid}"
    path.write_text(badge, encoding="utf-8")
    path.with_name(path.name + ".dir").write_text(folder, encoding="utf-8")
    return path


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


def test_badge_uuid_parses_only_slug_shaped_names() -> None:
    assert badge_uuid(f"w0t1p0_{_GONE}") == _GONE.upper()
    assert badge_uuid(f"w0t1p0_{_GONE.lower()}") == _GONE.upper()
    for odd in (".lock", f"w0t1p0_{_GONE}.dir", "TAB-1", "w0t1p0_not-a-uuid", _GONE, "notes.txt"):
        assert badge_uuid(odd) is None


def test_sweep_removes_gone_file_past_grace_with_its_dir() -> None:
    directory = tabsymbol.cache_dir()
    gone = _badge(directory, _GONE.upper(), tabsymbol.PALETTE[0])
    removed = sweep_badges(directory, {_GONE.upper(): _T0}, set(), _T0 + _GRACE)
    assert removed == [gone]
    assert not gone.exists() and not gone.with_name(gone.name + ".dir").exists()


def test_sweep_keeps_present_inside_grace_and_protected_files() -> None:
    directory = tabsymbol.cache_dir()
    present = _badge(directory, _LIVE.upper(), tabsymbol.PALETTE[0])  # a shell tab, no row
    fresh = _badge(directory, _OTHER.upper(), tabsymbol.PALETTE[1])
    guarded = _badge(directory, _GONE.upper(), tabsymbol.PALETTE[2])
    missing = {_OTHER.upper(): _T0 + 1, _GONE.upper(): _T0}
    before = _names(directory)
    assert not sweep_badges(directory, missing, {_GONE.upper()}, _T0 + _GRACE)
    assert _names(directory) - {".lock"} == before  # the lock file is the only addition
    assert present.exists() and fresh.exists() and guarded.exists()


@pytest.mark.parametrize(
    ("pid", "alive", "live_ids"),
    [
        (None, _dead, set()),  # unknown pid
        (0, _dead, set()),  # unknown pid
        (_DEAD_PID, _alive, set()),  # pid alive
        (_DEAD_PID, _dead, {"s1"}),  # alive in the live registry
    ],
)
def test_gone_file_whose_row_is_alive_or_unknown_is_kept(
    pid: int | None, alive: Callable[[int], bool], live_ids: set[str]
) -> None:
    directory = tabsymbol.cache_dir()
    gone = _badge(directory, _GONE.upper(), tabsymbol.PALETTE[0])
    rows = [_row("dead", pid=_DEAD_PID, uuid=_GONE), _row("s1", pid=pid)]
    protected = protected_uuids(rows, live_ids, alive=alive)
    assert protected == {_GONE.upper()}
    assert not sweep_badges(directory, {_GONE.upper(): _T0}, protected, _T0 + _GRACE)
    assert gone.exists()


def test_a_dead_pid_row_does_not_protect_its_badge() -> None:
    assert not protected_uuids([_row("s1")], set(), alive=_dead)


def test_sweep_never_touches_lock_and_odd_names() -> None:
    directory = tabsymbol.cache_dir()
    directory.mkdir(parents=True)
    odd = ["TAB-1", "notes.txt", f"{_GONE}", f"w0t1p0_{_GONE}.dir", "w0t1p0_not-a-uuid"]
    for name in odd:
        (directory / name).write_text("x", encoding="utf-8")
    (directory / ".lock").write_text("", encoding="utf-8")
    missing = {_GONE.upper(): _T0}
    assert not stale_badge_files(directory, missing, set(), _T0 + _GRACE)
    assert not sweep_badges(directory, missing, set(), _T0 + _GRACE)
    assert _names(directory) == set(odd) | {".lock"}


def test_sweep_frees_slots_so_assign_does_not_steal_a_live_badge() -> None:
    directory = tabsymbol.cache_dir()
    palette = tabsymbol.PALETTE
    live = [_badge(directory, _uuid(i + 1), palette[i]) for i in range(len(palette) - 4)]
    gone = [_badge(directory, _uuid(100 + i), palette[len(live) + i]) for i in range(4)]
    os.utime(live[0], (1.0, 1.0))  # the oldest file: what a full palette would steal
    missing = {badge_uuid(p.name) or "": _T0 for p in gone}
    removed = sweep_badges(directory, missing, set(), _T0 + _GRACE)
    assert sorted(removed) == sorted(gone)
    freed = {palette[len(live) + i] for i in range(4)}
    got = tabsymbol.assign(f"w9t9p0:{_uuid(999)}", "/elsewhere")
    assert got in freed
    assert all(p.exists() for p in live)
    assert [
        tabsymbol.read(f"w0t{int(_uuid(i + 1)[:8], 16)}p0:{_uuid(i + 1)}") for i in range(len(live))
    ] == list(palette[: len(live)])
    assert not sweep_badges(directory, missing, set(), _T0 + _GRACE)  # second sweep: nothing


def test_sweep_of_a_missing_cache_dir_is_a_noop(tmp_path: Path) -> None:
    assert not sweep_badges(tmp_path / "nope", {_GONE.upper(): _T0}, set(), _T0 + _GRACE)
    assert not (tmp_path / "nope").exists()


# ---- B3: the effectful pass ----------------------------------------------------------
_PRESENT = "55555555-0000-4000-8000-000000000005"


def _store_row(store: Store, sid: str, uuid: str, pid: int | None) -> None:
    store.ensure(sid, cwd="/repo")
    store.conn.execute(
        "UPDATE sessions SET iterm_session_id = ?, last_seen_pid = ?, status = 'parked', "
        "title_written = 'old title', title_written_at = 123 WHERE session_id = ?",
        (f"w0t1p0:{uuid}", pid, sid),
    )
    store.conn.commit()


def _iid(store: Store, sid: str) -> str | None:
    got = store.get(sid)
    assert got is not None
    return got.iterm_session_id


class _Iterm:
    """Fake iTerm: running state and the pane read, counting reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, panes: list[str] | None) -> None:
        self.running = True
        self.panes = panes
        self.reads = 0
        monkeypatch.setattr(iterm_api, "iterm_running", lambda: self.running)
        monkeypatch.setattr(tab_titles, "read_panes", self._read)

    def _read(self, *_a: object, **_k: object) -> list[ItermPane] | None:
        self.reads += 1
        if self.panes is None:
            return None
        return [ItermPane(uuid=u, tty="/dev/ttys001", name="x") for u in self.panes]


@pytest.fixture(name="world")
def _world(tmp_path: Path) -> Iterator[tuple[Store, MissingLog, Path]]:
    """A store with a dead-pid row on a gone tab (badged) and one on a present tab."""
    store = Store(tmp_path / "state.db")
    _store_row(store, "dead", _GONE, _DEAD_PID)
    _store_row(store, "here", _PRESENT, _DEAD_PID)
    directory = tabsymbol.cache_dir()
    _badge(directory, _GONE.upper(), tabsymbol.PALETTE[0])
    _badge(directory, _PRESENT.upper(), tabsymbol.PALETTE[1])
    yield store, MissingLog(tmp_path / "tab-missing.json"), directory
    store.close()


def _run(
    store: Store,
    log: MissingLog,
    now: int,
    *,
    dry_run: bool = False,
    live: dict[str, LiveSession] | None = None,
) -> ReleaseResult:
    return release_gone(store, live or {}, dry_run=dry_run, now=now, alive=_dead, log=log)


def test_release_skips_without_iterm_and_never_reads(
    world: tuple[Store, MissingLog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, log, directory = world
    fake = _Iterm(monkeypatch, [_PRESENT])
    fake.running = False
    result = _run(store, log, _T0)
    assert result.skipped == tabrelease.SKIP_NOT_RUNNING and fake.reads == 0
    assert not log.path.exists() and _iid(store, "dead") and len(_names(directory) - {".lock"}) == 4


@pytest.mark.parametrize("panes", [None, []])
def test_release_skips_on_an_unreliable_read_and_writes_nothing(
    world: tuple[Store, MissingLog, Path],
    monkeypatch: pytest.MonkeyPatch,
    panes: list[str] | None,
) -> None:
    store, log, directory = world
    log.save({_GONE.upper(): _T0})  # a clock already past the grace
    before = log.path.read_text(encoding="utf-8")
    _Iterm(monkeypatch, panes)
    result = _run(store, log, _T0 + _GRACE)
    assert result.skipped and not result.released and not result.removed
    assert log.path.read_text(encoding="utf-8") == before
    assert _iid(store, "dead") == f"w0t1p0:{_GONE}" and len(_names(directory) - {".lock"}) == 4


def test_release_starts_the_clock_then_releases_past_the_grace(
    world: tuple[Store, MissingLog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, log, directory = world
    _Iterm(monkeypatch, [_PRESENT])
    first = _run(store, log, _T0)
    assert not first.released and not first.removed
    assert log.load() == {_GONE.upper(): _T0}  # the first pass only starts the clock
    assert {v.row.session_id: v.reason for v in first.verdicts} == {
        "dead": tabrelease.KEEP_GRACE,
        "here": tabrelease.KEEP_PRESENT,
    }
    second = _run(store, log, _T0 + _GRACE)
    assert [r.session_id for r in second.released] == ["dead"]
    assert [badge_uuid(p.name) for p in second.removed] == [_GONE.upper()]
    got = store.get("dead")
    assert got is not None and got.iterm_session_id is None
    assert got.status == "parked" and got.title_written is None  # the row stays, resumable
    assert _iid(store, "here") == f"w0t1p0:{_PRESENT}"
    assert {badge_uuid(n) for n in _names(directory)} >= {_PRESENT.upper()}
    third = _run(store, log, _T0 + 2 * _GRACE)
    assert not third.released and not third.removed  # a second run releases nothing
    assert not log.load()  # nothing references the gone UUID any more: pruned


def test_release_dry_run_reports_and_writes_nothing(
    world: tuple[Store, MissingLog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, log, directory = world
    log.save({_GONE.upper(): _T0})
    before = log.path.read_text(encoding="utf-8")
    _Iterm(monkeypatch, [_PRESENT])
    result = _run(store, log, _T0 + _GRACE, dry_run=True)
    assert [r.session_id for r in result.released] == ["dead"] and len(result.removed) == 1
    assert _iid(store, "dead") == f"w0t1p0:{_GONE}" and len(_names(directory) - {".lock"}) == 4
    assert log.path.read_text(encoding="utf-8") == before


def test_release_dry_run_without_a_log_never_creates_one(
    world: tuple[Store, MissingLog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, log, _directory = world
    _Iterm(monkeypatch, [_PRESENT])
    _run(store, log, _T0, dry_run=True)
    assert not log.path.exists()
    release_gone(store, (), dry_run=True, record=True, now=_T0, alive=_dead, log=log)
    assert log.load() == {_GONE.upper(): _T0}  # `-g` records the clock, still no release


def test_release_keeps_a_live_registry_row(
    world: tuple[Store, MissingLog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, log, directory = world
    log.save({_GONE.upper(): _T0})
    _Iterm(monkeypatch, [_PRESENT])
    live = {"dead": LiveSession(pid=_DEAD_PID, session_id="dead", cwd="/repo", alive=True)}
    result = _run(store, log, _T0 + _GRACE, live=live)
    assert not result.released and not result.removed
    assert [v.reason for v in result.verdicts if v.row.session_id == "dead"] == [
        tabrelease.KEEP_LIVE
    ]
    assert len(_names(directory) - {".lock"}) == 4


def test_release_cas_refusal_keeps_the_binding_and_the_badge(
    world: tuple[Store, MissingLog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, log, directory = world
    log.save({_GONE.upper(): _T0})
    _Iterm(monkeypatch, [_PRESENT])
    monkeypatch.setattr("command_center.store.pid_alive", lambda _pid: True)  # came alive
    result = _run(store, log, _T0 + _GRACE)
    assert not result.released and [r.session_id for r in result.refused] == ["dead"]
    assert _iid(store, "dead") == f"w0t1p0:{_GONE}"
    assert not result.removed and len(_names(directory) - {".lock"}) == 4
