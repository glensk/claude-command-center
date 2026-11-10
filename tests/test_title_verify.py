"""tp#1988: a tab title is recorded only once it is really on the tab; badges never thrash.

* ``title_written`` is stamped from the osascript read-back, never from the dispatch —
  a compare-and-swap that did not fire left the DB claiming a title the tab never showed.
* 🔴 is the wait marker, so it is no badge: a 🔴-badged tab lost its badge to the
  marker's "off" strip (the badge-less ``random`` title of the report).
* A row whose process is gone claims no new badge: dozens of them outnumbered the
  palette and rotated every live tab's badge on each pass.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from command_center import config, tab_titles, tabsymbol, terminal
from command_center.store import Store


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    monkeypatch.setattr(config, "load_config", lambda: config.Config(aim_in_tab_title=True))
    s = Store(tmp_path / "verify.db")
    yield s
    s.close()


def _row(store: Store, sid: str, **fields: Any) -> None:
    store.ensure(sid, cwd="/r/repo")
    store.update_fields(sid, **fields)


def _fake_iterm(monkeypatch: pytest.MonkeyPatch, shown: dict[str, str]) -> list[Any]:
    """Fake writers: *shown* is what each tab (UUID) reads back after the write."""
    calls: list[Any] = []

    def _plain(cores: dict[str, str], marker: str = "") -> dict[str, str]:
        calls.append(("plain", cores, marker))
        return {
            tab_titles.uuid_of(i): shown.get(tab_titles.uuid_of(i), c) for i, c in cores.items()
        }

    def _cas(entries: dict[str, tuple[str, str]], marker: str = "") -> dict[str, str]:
        calls.append(("cas", entries, marker))
        return {tab_titles.uuid_of(i): shown[tab_titles.uuid_of(i)] for i in entries}

    monkeypatch.setattr(terminal, "set_session_titles_preserving", _plain)
    monkeypatch.setattr(tab_titles, "set_titles_cas", _cas)
    return calls


def test_a_cas_that_did_not_fire_is_not_recorded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(store, "s1", aim="new aim", iterm_session_id="w0t0p0:U1", title_written="🟫 old aim")
    calls = _fake_iterm(monkeypatch, {"U1": "random"})  # the tab drifted: CAS skips
    tabsymbol.sync_live(store)
    assert calls and calls[0][0] == "cas"
    got = store.get("s1")
    assert got is not None and got.title_written == "🟫 old aim" and got.title_generation == 0


def test_no_read_back_records_nothing(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _row(store, "s1", aim="ship it", iterm_session_id="w0t0p0:U1")
    monkeypatch.setattr(terminal, "set_session_titles_preserving", lambda c, marker="": None)
    tabsymbol.sync_live(store)
    got = store.get("s1")
    assert got is not None and got.title_written is None


def test_an_explicit_aim_overrides_drift_and_records_the_confirmed_title(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(store, "s1", aim="decide history improvements", iterm_session_id="w0t0p0:U1")
    store.update_fields("s1", title_written="🟫 random")
    calls = _fake_iterm(monkeypatch, {})  # plain write lands as asked
    monkeypatch.setattr("command_center.store.Store", lambda *a, **k: store)
    monkeypatch.setattr(store, "close", lambda: None)
    session = store.get("s1")
    assert session is not None
    tabsymbol.push_title(session, marker="🔴 ")
    assert len(calls) == 1
    kind, cores, _marker = calls[0]
    core = cores["w0t0p0:U1"]
    assert kind == "plain" and core.endswith(" decide history improvements")
    got = store.get("s1")
    assert got is not None and got.title_written == core


def test_a_marked_read_back_still_confirms_the_core() -> None:
    assert tabsymbol._shows("🔴 🟫 x", "🟫 x", "🔴 ")  # pylint: disable=protected-access
    assert not tabsymbol._shows("x", "🟫 x", "🔴 ")  # pylint: disable=protected-access
    assert not tabsymbol._shows(None, "🟫 x", "🔴 ")  # pylint: disable=protected-access


def test_the_wait_marker_is_never_a_badge() -> None:
    assert "🔴" not in tabsymbol.PALETTE


def test_a_dead_row_claims_no_new_badge(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_iterm(monkeypatch, {})
    _row(store, "dead", aim="gone", iterm_session_id="w0t1p0:DEAD", last_seen_pid=2**22 + 7)
    _row(store, "live", aim="here", iterm_session_id="w0t2p0:LIVE", last_seen_pid=os.getpid())
    badged = tabsymbol.sync_live(store)
    assert badged == ["live"]
    assert tabsymbol.read("w0t1p0:DEAD") is None and tabsymbol.read("w0t2p0:LIVE")


def test_read_back_parser() -> None:
    raw = "abc\x1f🟫 x\x1eDEF\x1f\x1e\n"
    assert terminal.parse_title_readback(raw) == {"ABC": "🟫 x", "DEF": ""}


@pytest.mark.real_title_writer
def test_title_script_addresses_only_targets_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts: list[str] = []

    class _Proc:
        returncode = 0

        def __init__(self, cmd: list[str], **_kw: object) -> None:
            scripts.append(cmd[-1])

        def communicate(self, timeout: float) -> tuple[str, str]:
            assert timeout == terminal.TITLE_WRITE_TIMEOUT_SEC
            return "ABC-1\x1f🟧 repo\x1e", ""

    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(terminal.subprocess, "Popen", _Proc)
    got = terminal.set_session_titles_preserving({"w0t1p0:ABC-1": "🟧 repo"}, marker="🔴 ")
    assert got == {"ABC-1": "🟧 repo"}
    script = scripts[0]
    assert 'set targets to {"ABC-1"}' in script
    assert "set ids to id of every session of every tab of every window" in script
    assert "set out to out & sid & fs & (name of s) & rs" in script
