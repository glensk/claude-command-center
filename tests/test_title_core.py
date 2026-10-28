"""Tab-title precedence (staff name; badge → AIM words → name) and the hand-set-title freeze."""

from __future__ import annotations

import os
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


def test_push_title_shows_the_aim_words(writes: list[Any]) -> None:
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="ship X",
        canonical_name="voice bridge",
        iterm_session_id="w0t0p0:U",
    )
    tabsymbol.push_title(session)
    [(kind, cores)] = writes
    assert kind == "plain" and cores["w0t0p0:U"].endswith(" ship X")
    assert "voice bridge" not in cores["w0t0p0:U"] and "🎯" not in cores["w0t0p0:U"]


def test_hand_set_name_drops_the_aim_tail(writes: list[Any]) -> None:
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="answer the open tickets",
        canonical_name="zoho ticket solver",
        canonical_name_origin="manual",
        iterm_session_id="w0t0p0:U",
    )
    tabsymbol.push_title(session)
    [(kind, cores)] = writes
    assert kind == "plain" and cores["w0t0p0:U"].endswith(" zoho ticket solver")
    assert "🎯" not in cores["w0t0p0:U"]


@pytest.mark.parametrize("origin", ["llm", "fallback", ""])
def test_generated_or_unnamed_shows_the_aim_words(origin: str) -> None:
    cfg = config.Config(session_names=True, aim_in_tab_title=True)
    name = "voice bridge" if origin else None
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="ship X",
        canonical_name=name,
        canonical_name_origin=origin,
    )
    assert tabsymbol._session_core(session, "🟧", cfg) == "🟧 ship X"


@pytest.mark.parametrize("origin", ["manual", "llm", ""])
def test_aim_in_tab_title_off_never_shows_the_aim(origin: str) -> None:
    cfg = config.Config(session_names=True, aim_in_tab_title=False)
    name = "voice bridge" if origin else None
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="ship X",
        canonical_name=name,
        canonical_name_origin=origin,
    )
    assert tabsymbol._session_core(session, "🟧", cfg) == f"🟧 {name or LEAF}"


def test_manual_name_with_names_off_shows_the_aim_words() -> None:
    """The name is not shown with ``session_names`` off, so the AIM words show."""
    cfg = config.Config(session_names=False, aim_in_tab_title=True)
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="ship X",
        canonical_name="zoho ticket solver",
        canonical_name_origin="manual",
    )
    assert tabsymbol._session_core(session, "🟧", cfg) == "🟧 ship X"


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


@pytest.mark.parametrize(
    ("aim", "title"),
    [
        ("fix decide: tp#1363, #1364", "🟧 fix decide"),
        ("tp#1363, #1364: fix decide", "🟧 fix decide"),
        ("review the hook of tp#5: tp#9", "🟧 review the hook of tp#5"),
        ("tp#1363", f"🟧 {LEAF}"),
    ],
)
def test_the_tab_shows_the_aim_words_without_the_item_run(aim: str, title: str) -> None:
    """tp#1366: `<few words>: tp#N` titles the tab `<badge> <few words>`."""
    cfg = config.Config(session_names=True, aim_in_tab_title=True)
    session = Session(session_id="s", cwd="/Users/x/repo", aim=aim)
    assert tabsymbol._session_core(session, "🟧", cfg) == title


def test_a_staff_session_title_is_its_agent_symbol_and_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tp#1366: no badge, no AIM — the agent.yaml symbol and tab_name and nothing else."""
    staff = tmp_path / "staff" / "zoho-solver"
    staff.mkdir(parents=True)
    (staff / "agent.yaml").write_text('name: zoho-solver\ntab_name: "zoho-agent"\nsymbol: "🎫"\n')
    roles = tmp_path / "tp" / "session-role"
    roles.mkdir(parents=True)
    (roles / "s").write_text("zoho-solver\n")
    monkeypatch.setenv("TP_STAFF_ROOT", str(tmp_path / "staff"))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp"))
    cfg = config.Config(session_names=True, aim_in_tab_title=True)
    session = Session(
        session_id="s",
        cwd="/Users/x/repo",
        aim="answer tickets: tp#1236, #1177",
        canonical_name="zoho overseer",
        canonical_name_origin="llm",
    )
    assert tabsymbol._session_core(session, "🎫", cfg) == "🎫 zoho-agent"
    assert tabsymbol._session_core(session, "🎫", config.Config()) == "🎫 zoho-agent"
    (staff / "agent.yaml").write_text("name: zoho-solver\n")  # no tab_name: the agent id
    os.utime(staff / "agent.yaml", ns=(1, 1))  # a new mtime: the manifest cache re-reads
    assert tabsymbol._session_core(session, "🎫", cfg) == "zoho-solver"


def test_a_staff_tab_is_written_whole_so_no_wait_marker_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tp#1366: a staff tab is forced to its tab_name (dropping `🔴 `); others keep the marker."""
    staff = tmp_path / "staff" / "zoho-solver"
    staff.mkdir(parents=True)
    (staff / "agent.yaml").write_text('name: zoho-solver\ntab_name: "zoho-agent"\n')
    roles = tmp_path / "tp" / "session-role"
    roles.mkdir(parents=True)
    (roles / "staff-sid").write_text("zoho-solver\n")
    monkeypatch.setenv("TP_STAFF_ROOT", str(tmp_path / "staff"))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp"))
    forced: list[dict[str, str]] = []
    preserved: list[dict[str, str]] = []
    monkeypatch.setattr(terminal, "set_session_titles", forced.append)
    monkeypatch.setattr(
        terminal, "set_session_titles_preserving", lambda cores, marker="": preserved.append(cores)
    )
    plain: dict[str, str] = {}
    cas: dict[str, tuple[str, str]] = {}
    writes: list[tuple[str, str]] = []
    bare: dict[str, str] = {}
    for sid, iid in (("staff-sid", "w0t0p0:AAA"), ("other", "w0t1p0:BBB")):
        session = Session(session_id=sid, cwd="/Users/x/repo", iterm_session_id=iid)
        core = tabsymbol._session_core(session, "🟧", config.Config())
        tabsymbol._plan_write(session, core, plain, cas, writes, bare=bare)
    tabsymbol._write_titles(plain, cas, "🔴 ", bare)
    assert forced == [{"w0t0p0:AAA": "zoho-agent"}]
    assert list(preserved[0]) == ["w0t1p0:BBB"] and len(preserved) == 1
