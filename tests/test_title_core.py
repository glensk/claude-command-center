"""Tab-title precedence (badge → name → AIM) and the hand-set-title freeze."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from command_center import config, tab_titles, tabsymbol, terminal
from command_center.models import Session

LEAF = tabsymbol.title_core("🟧", "/Users/x/repo").split(" ", 1)[1]


def test_without_a_name_the_folder_leaf_shows() -> None:
    assert tabsymbol.title_core("🟧", "/Users/x/repo") == f"🟧 {LEAF}"
    assert tabsymbol.title_core("🟧", "/Users/x/repo", name="  ") == f"🟧 {LEAF}"


def test_name_replaces_the_leaf_and_precedes_the_aim() -> None:
    assert tabsymbol.title_core("🟧", "/Users/x/repo", name="voice bridge") == "🟧 voice bridge"
    assert (
        tabsymbol.title_core("🟧", "/Users/x/repo", "ship X", name="voice bridge")
        == "🟧 voice bridge 🎯 ship X"
    )
    # left-to-right precedence: a narrow tab truncates the AIM first
    title = tabsymbol.title_core("🟧", "/Users/x/repo", "a" * 80, name="voice bridge")
    assert title.index("🟧") < title.index("voice bridge") < title.index("🎯")


@pytest.fixture(name="writes")
def writes_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(session_names=True, aim_in_tab_title=True)
    )
    seen: list[Any] = []

    def plain(cores: dict[str, str], marker: str = "") -> None:
        del marker
        seen.append(("plain", cores))

    def cas(entries: dict[str, tuple[str, str]], marker: str = "") -> None:
        del marker
        seen.append(("cas", entries))

    monkeypatch.setattr(terminal, "set_session_titles_preserving", plain)
    monkeypatch.setattr(tab_titles, "set_titles_cas", cas)
    return seen


def test_push_title_uses_name_then_aim(writes: list[Any]) -> None:
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="ship X",
        canonical_name="voice bridge",
        iterm_session_id="w0t0p0:U",
    )
    tabsymbol.push_title(session)
    [(kind, cores)] = writes
    assert kind == "plain" and cores["w0t0p0:U"].endswith(" voice bridge 🎯 ship X")


def test_names_off_keeps_the_leaf(writes: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_config", lambda: config.Config(session_names=False))
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        canonical_name="voice bridge",
        iterm_session_id="w0t0p0:U",
    )
    tabsymbol.push_title(session)
    assert writes[0][1]["w0t0p0:U"].endswith(f" {LEAF}")


def test_manual_tab_is_never_overwritten(writes: list[Any]) -> None:
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        canonical_name="sauna talk",
        canonical_name_origin="manual-tab",
        iterm_session_id="w0t0p0:U",
        title_written="🟧 old",
    )
    tabsymbol.push_title(session)
    tabsymbol.seed_title("w0t0p0:U", "/Users/x/repo", session=session)

    class _Store:
        def list_sessions(self) -> list[Session]:
            return [session]

    tabsymbol.sync_live(_Store())  # type: ignore[arg-type]
    assert writes == []


def test_a_later_write_is_compare_and_swap(writes: list[Any]) -> None:
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        canonical_name="voice bridge",
        iterm_session_id="w0t0p0:U",
        title_written="🟧 old core",
    )
    tabsymbol.push_title(session)
    [(kind, entries)] = writes
    assert kind == "cas" and entries["w0t0p0:U"][0] == "🟧 old core"


def test_cas_script_only_replaces_the_expected_body() -> None:
    script = tab_titles.cas_script_checks({"w0t0p0:U-1": ('🟧 "old"', "🟧 new")}, "🔴 ")
    assert 'if sid is "U-1" then' in script
    assert 'body is "🟧 \\"old\\""' in script and "considering case" in script
    assert 'set name of s to (pre & "🟧 new")' in script


def test_reader_parses_records() -> None:
    raw = "U1\x1f/dev/ttys001\x1f🔴 🟧 voice\x1eu2\x1f/dev/ttys002\x1fx\x1e\n"
    panes = tab_titles.parse_panes(raw)
    assert panes == [
        tab_titles.ItermPane("U1", "/dev/ttys001", "🔴 🟧 voice"),
        tab_titles.ItermPane("U2", "/dev/ttys002", "x"),
    ]


def test_one_owner_per_tab_prefers_the_live_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import store as store_mod

    monkeypatch.setattr(store_mod, "pid_alive", lambda pid: pid == 42)
    dead_recent = Session(
        session_id="dead", iterm_session_id="w0t0p0:U", last_seen_pid=7, last_response_at=99
    )
    live_old = Session(
        session_id="live", iterm_session_id="w1t2p0:u", last_seen_pid=42, last_response_at=1
    )
    assert [s.session_id for s in tabsymbol.tab_owners([dead_recent, live_old])] == ["live"]
