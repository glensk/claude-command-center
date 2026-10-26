"""Staff role symbols (tp#1288): agent.yaml `symbol:` replaces an adopted tab's badge."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import colors, staffrole, tabsymbol, terminal
from command_center.models import Session


@pytest.fixture(autouse=True)
def _staff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A staff root with email-agent (📧) and slack-agent (no symbol); an empty role cache."""
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))
    staff = tmp_path / "staff"
    _agent(staff, "email-agent", 'symbol: "📧"   # tab + decide + Obsidian\n')
    _agent(staff, "slack-agent", "")
    (tmp_path / "tp-cache" / "session-role").mkdir(parents=True)
    monkeypatch.setenv("TP_STAFF_ROOT", str(staff))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp-cache"))
    return staff


def _agent(staff: Path, name: str, extra: str) -> None:
    (staff / name).mkdir(parents=True, exist_ok=True)
    (staff / name / "agent.yaml").write_text(
        f"schema_version: 1\nname: {name}\nrelay:\n  symbol: nested-is-ignored\n{extra}",
        encoding="utf-8",
    )


def _adopt(tmp_path: Path, sid: str, agent: str) -> None:
    (tmp_path / "tp-cache" / "session-role" / sid).write_text(agent + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('symbol: "📧"\n', "📧"),
        ("symbol: '🧭'\n", "🧭"),
        ("symbol: 💬  # comment\n", "💬"),
        ('symbol: "⚙️"\n', None),  # VS16: wider than its two cells
        ('symbol: "👩‍💻"\n', None),  # ZWJ sequence
        ('symbol: "ab"\n', None),
        ('symbol: "x"\n', None),  # ASCII is no symbol
        ("", None),
    ],
)
def test_agent_symbol_parses_one_top_level_codepoint(
    _staff: Path, line: str, expected: str | None
) -> None:
    _agent(_staff, "probe-agent", line)
    assert staffrole.agent_symbol("probe-agent") == expected


def test_a_palette_badge_is_no_role_symbol(_staff: Path, tmp_path: Path) -> None:
    _agent(_staff, "probe-agent", f'symbol: "{tabsymbol.PALETTE[0]}"\n')
    _adopt(tmp_path, "s1", "probe-agent")
    assert staffrole.session_symbol("s1") == tabsymbol.PALETTE[0]
    assert tabsymbol.role_symbol("s1") is None  # would look like an ordinary tab


def test_agent_symbol_rejects_bad_names_and_missing_agents() -> None:
    assert staffrole.agent_symbol("../email-agent") is None
    assert staffrole.agent_symbol("ghost") is None
    assert staffrole.agent_symbol(None) is None


def test_session_symbol_needs_an_adopt_binding(tmp_path: Path) -> None:
    assert staffrole.session_symbol("s1") is None
    assert not staffrole.has_bindings()
    _adopt(tmp_path, "s1", "email-agent")
    _adopt(tmp_path, "s2", "slack-agent")
    assert staffrole.has_bindings()
    assert staffrole.session_symbol("s1") == "📧"
    assert staffrole.session_symbol("s2") is None  # adopted, but the agent has no symbol
    assert staffrole.agent_symbols() == frozenset({"📧"})


def test_role_marker_carries_the_agent_symbol(tmp_path: Path) -> None:
    _adopt(tmp_path, "s1", "email-agent")
    _adopt(tmp_path, "s2", "slack-agent")
    assert staffrole.role_marker("s1") == f"{staffrole.ROLE_MARKER}📧"
    assert staffrole.role_marker("s2") == staffrole.ROLE_MARKER
    assert staffrole.role_marker("s3") == ""


def test_cell_for_shows_the_agent_symbol_of_an_adopted_session(tmp_path: Path) -> None:
    _adopt(tmp_path, "s1", "email-agent")
    tabsymbol.assign("w0t1p0:AA", folder="/Users/x/ra")
    assert tabsymbol.cell_for("w0t1p0:AA", "/Users/x/ra", session_id="s1") == "📧 "
    assert tabsymbol.cell_for(None, "/Users/x/ra", live=False, session_id="s1") == "📧 "
    plain = tabsymbol.cell_for("w0t1p0:AA", "/Users/x/ra", session_id="s9")
    assert plain.strip() in tabsymbol.PALETTE


class _FakeStore:
    def __init__(self, sessions: list[Session]) -> None:
        self._sessions = sessions

    def list_sessions(self) -> list[Session]:
        return self._sessions


def test_sync_live_titles_an_adopted_tab_with_its_agent_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(
        terminal,
        "set_session_titles_preserving",
        lambda cores, marker="🔴 ": captured.update(cores=cores),
    )
    _adopt(tmp_path, "a", "email-agent")
    store = _FakeStore(
        [
            Session("a", cwd="/Users/x/ra", iterm_session_id="w0t1p0:AA"),
            Session("b", cwd="/Users/x/rb", iterm_session_id="w0t2p0:BB"),
        ]
    )
    assert set(tabsymbol.sync_live(store)) == {"a", "b"}  # type: ignore[arg-type]
    cores = captured["cores"]
    assert cores["w0t1p0:AA"] == f"📧 {colors.folder_split('/Users/x/ra')[1]}"
    assert tabsymbol.read("w0t1p0:AA") in tabsymbol.PALETTE  # the tab keeps its slot
    badge_b = tabsymbol.read("w0t2p0:BB")
    assert badge_b in tabsymbol.PALETTE
    assert cores["w0t2p0:BB"] == f"{badge_b} {colors.folder_split('/Users/x/rb')[1]}"


def test_seed_title_uses_the_agent_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(
        terminal,
        "set_session_titles_preserving",
        lambda cores, marker="🔴 ": captured.update(cores=cores),
    )
    _adopt(tmp_path, "a", "email-agent")
    session = Session("a", cwd="/Users/x/ra", iterm_session_id="w0t1p0:AA")
    badge = tabsymbol.seed_title("w0t1p0:AA", "/Users/x/ra", session=session)
    assert badge in tabsymbol.PALETTE
    assert captured["cores"]["w0t1p0:AA"].startswith("📧 ")


def test_title_parsers_treat_an_agent_symbol_as_a_badge() -> None:
    assert tabsymbol.clean_title("🔴 📧 email-agent 🎯 route mail") == "email-agent"
    assert tabsymbol.healed_title("📧 pitch", "🔷") == "🔷 pitch"  # role gone: badge back
    assert tabsymbol.healed_title("🔷 pitch", "📧") == "📧 pitch"  # adopted: no stacking
    assert tabsymbol.healed_title("📧 pitch", "📧") is None


def test_rename_callback_prefers_the_agent_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tabsymbol, "_role_symbol_for_uuid", lambda uuid: "📧" if uuid == "R" else None
    )
    monkeypatch.setattr(tabsymbol, "badge_for_uuid", lambda uuid: "🟢")
    rename = tabsymbol._rename_with_badge  # pylint: disable=protected-access
    assert rename("mail", ["R", "X"]) == "📧 mail"
    assert rename("mail", ["X"]) == "🟢 mail"


def test_role_lookup_by_tab_skips_the_store_without_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("store opened")

    monkeypatch.setattr("command_center.store.Store", _boom)
    tabsymbol._role_by_uuid.clear()  # pylint: disable=protected-access
    assert tabsymbol._role_symbol_for_uuid("UUID") is None  # pylint: disable=protected-access
