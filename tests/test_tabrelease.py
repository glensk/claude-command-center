"""Tests for the dead-tab release rule and its missing-since log (tp#1990)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from command_center import tabrelease
from command_center.models import TabbedRow
from command_center.tabrelease import MissingLog, gone_rows, update_missing

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
