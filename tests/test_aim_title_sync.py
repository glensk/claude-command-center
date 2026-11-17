"""tp#2211: an AIM change re-titles the session's tab AND its window title, always."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from command_center import cli, config, hooks, tab_titles, tabsymbol, terminal
from command_center.models import Session
from command_center.store import Store


@pytest.mark.parametrize(
    ("aim", "words"),
    [
        ("tp#2172", "tp#2172"),  # ids only: the AIM itself, never the folder leaf
        ("tp#1363, #1364", "tp#1363, #1364"),
        ("fix decide: tp#1363", "fix decide"),  # words win (tp#1366)
        ("", ""),
        (None, ""),
    ],
)
def test_an_ids_only_aim_still_titles_the_tab(aim: str | None, words: str) -> None:
    assert tabsymbol.aim_title_words(aim) == words


def test_an_ids_only_aim_reaches_the_session_core() -> None:
    session = Session(session_id="s", cwd="/Users/x/repo", aim="tp#2172")
    assert tabsymbol._session_core(session, "⭐", config.Config(aim_in_tab_title=True)) == (
        "⭐ tp#2172"
    )


@pytest.mark.parametrize(
    ("current", "aim", "title"),
    [
        ("🟧 me@host | ~", "tp#2172", "🟧 tp#2172"),
        ("🟦 me@host | ~/w", "ship the parser: tp#5", "🟦 ship the parser: tp#5"),
        ("me@host | ~", "ship it", "ship it"),  # no marker: the AIM alone
        ("| odd", "ship it", "ship it"),  # ASCII punctuation is no marker
        ("ab cd", "ship it", "ship it"),
        ("", "ship it", "ship it"),
        (None, "line one\nline two", "line one"),
        ("🟧 x", "", None),
        ("🟧 x", None, None),
    ],
)
def test_window_title_keeps_the_marker_and_shows_the_aim(
    current: str | None, aim: str | None, title: str | None
) -> None:
    assert tabsymbol.window_title(current, aim) == title


def test_window_title_clips_a_long_aim() -> None:
    title = tabsymbol.window_title("🟧 x", "a" * 200)
    assert title is not None and title.startswith("🟧 ") and title.endswith("…")
    assert len(title) == len("🟧 ") + 80


def test_pane_readback_parses_tty_and_window_title() -> None:
    raw = "/dev/ttys011\x1f🟧 me@host | ~\x1e"
    assert terminal.parse_pane_readback(raw) == ("/dev/ttys011", "🟧 me@host | ~")
    assert terminal.parse_pane_readback("") is None
    assert terminal.parse_pane_readback("/etc/passwd\x1fx\x1e") is None


def test_write_window_title_reaches_the_pane_device() -> None:
    master, slave = os.openpty()
    try:
        device = os.ttyname(slave)
        assert terminal.write_window_title(device, "🟧 ship\x07it")
        got = os.read(master, 1024)
    finally:
        os.close(master)
        os.close(slave)
    assert got == "\033]2;🟧 shipit\007".encode()


def test_write_window_title_refuses_a_non_tty_path(tmp_path: Path) -> None:
    target = tmp_path / "not-a-tty"
    assert not terminal.write_window_title(str(target), "x")
    assert not target.exists()


@pytest.fixture(name="calls")
def calls_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list[Any]]:
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    seen: dict[str, list[Any]] = {"tab": [], "read": [], "window": []}

    def plain(cores: dict[str, str], marker: str = "") -> None:
        del marker
        seen["tab"].append(cores)

    def read(iid: str) -> tuple[str, str]:
        seen["read"].append(iid)
        return ("/dev/ttys011", "🟧 me@host | ~")

    def write(tty: str, title: str) -> bool:
        seen["window"].append((tty, title))
        return True

    monkeypatch.setattr(terminal, "set_session_titles_preserving", plain)
    monkeypatch.setattr(tab_titles, "set_titles_cas", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal, "read_session_pane", read)
    monkeypatch.setattr(terminal, "write_window_title", write)
    return seen


def _session(aim: str = "tp#2172") -> Session:
    return Session(session_id="s", cwd="/Users/x", aim=aim, iterm_session_id="w1t16p1:UUID")


def test_push_title_sets_tab_and_window_title(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    cfg = config.Config(aim_in_tab_title=True, aim_in_window_title=True)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    tabsymbol.push_title(_session())
    [cores] = calls["tab"]
    assert cores["w1t16p1:UUID"].endswith(" tp#2172")
    assert calls["window"] == [("/dev/ttys011", "🟧 tp#2172")]


def test_window_title_is_inert_by_default(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: config.Config(aim_in_tab_title=True))
    tabsymbol.push_title(_session())
    assert calls["read"] == [] and calls["window"] == []
    assert len(calls["tab"]) == 1


def test_a_frozen_tab_gets_no_window_title_either(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    cfg = config.Config(aim_in_tab_title=True, aim_in_window_title=True)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    session = _session()
    session.canonical_name_origin = "manual-tab"
    from command_center import session_names

    monkeypatch.setattr(session_names, "titles_frozen", lambda _s: True)
    tabsymbol.push_title(session)
    assert calls == {"tab": [], "read": [], "window": []}


def test_set_aim_retitles_even_when_the_aim_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-setting the same AIM heals a drifted title (the tab -> AIM path relies on it)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    cfg = config.Config(
        aim_in_tab_title=True, aim_score_on_set=False, short_aim=False, future_files=False
    )
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: cfg)
    pushed: list[str | None] = []
    monkeypatch.setattr(tabsymbol, "push_title", lambda s, **_k: pushed.append(s.aim))
    args = argparse.Namespace(text="ship the parser", session="s1", first=False, by=None)
    assert cli.cmd_set_aim(args) == 0
    assert cli.cmd_set_aim(args) == 0
    assert pushed == ["ship the parser", "ship the parser"]


@pytest.fixture(name="tab")
def tab_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, str]]]:
    """A store with session ``s1`` in tab ``w1t1p0:UUID1``; iTerm reads back each write."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    (tmp_path / "command-center").mkdir(parents=True)
    with Store() as store:
        store.ensure("s1", cwd="/Users/x/repo")
        store.update_fields("s1", iterm_session_id="w1t1p0:UUID1")
    writes: list[dict[str, str]] = []

    def plain(cores: dict[str, str], marker: str = "") -> dict[str, str]:
        del marker
        writes.append(cores)
        return {tab_titles.uuid_of(iid): core for iid, core in cores.items()}

    monkeypatch.setattr(terminal, "set_session_titles_preserving", plain)
    monkeypatch.setattr(tab_titles, "set_titles_cas", lambda *_a, **_k: None)
    return {"writes": writes}


def _set_aim(monkeypatch: pytest.MonkeyPatch, text: str, *, aim_in_tab_title: bool = False) -> int:
    cfg = config.Config(
        aim_in_tab_title=aim_in_tab_title,
        aim_score_on_set=False,
        short_aim=False,
        future_files=False,
    )
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: cfg)
    args = argparse.Namespace(text=text, session="s1", first=False, by=None)
    return cli.cmd_set_aim(args)


def test_set_aim_confirms_a_tab_that_shows_the_aim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tab: dict[str, list[dict[str, str]]],
) -> None:
    assert _set_aim(monkeypatch, "ship it", aim_in_tab_title=True) == 0
    [cores] = tab["writes"]
    assert cores["w1t1p0:UUID1"].endswith(" ship it")
    assert f"{cli.AIM_TAB_TITLED}{cores['w1t1p0:UUID1']}" in capsys.readouterr().out.splitlines()


def test_set_aim_retitles_with_both_title_keys_off_but_confirms_no_aim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tab: dict[str, list[dict[str, str]]],
) -> None:
    """Not gated on the keys (like the ``/aim`` intercept); the core then shows no AIM."""
    assert _set_aim(monkeypatch, "ship it") == 0
    [cores] = tab["writes"]
    assert cores["w1t1p0:UUID1"].endswith("repo")  # the folder, not the AIM
    assert "ship it" not in cores["w1t1p0:UUID1"]
    assert cli.AIM_TAB_TITLED not in capsys.readouterr().out


@pytest.mark.parametrize("why", ["frozen", "done"])
def test_set_aim_confirms_nothing_for_a_frozen_or_done_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tab: dict[str, list[dict[str, str]]],
    why: str,
) -> None:
    """push_title skips the tab, so the caller (iterm-set-tabtitle.py) must title it."""
    if why == "frozen":
        from command_center import session_names

        monkeypatch.setattr(session_names, "titles_frozen", lambda _s: True)
    else:
        with Store() as store:
            store.update_fields("s1", done=True)
    assert _set_aim(monkeypatch, "ship it", aim_in_tab_title=True) == 0
    assert tab["writes"] == []
    assert cli.AIM_TAB_TITLED not in capsys.readouterr().out


def test_set_aim_confirms_nothing_when_iterm_does_not_read_back_the_title(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    (tmp_path / "command-center").mkdir(parents=True)
    with Store() as store:
        store.ensure("s1", cwd="/Users/x/repo")
        store.update_fields("s1", iterm_session_id="w1t1p0:UUID1")
    monkeypatch.setattr(terminal, "set_session_titles_preserving", lambda *_a, **_k: None)
    assert _set_aim(monkeypatch, "ship it", aim_in_tab_title=True) == 0
    assert cli.AIM_TAB_TITLED not in capsys.readouterr().out


def test_session_start_titles_the_tab_with_the_launch_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher's AIM is stored BEFORE the title is seeded, so the first title shows it."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_AIM", "tp#2172")
    monkeypatch.setenv("ITERM_SESSION_ID", "w1t16p1:UUID")
    cfg = config.Config(aim_in_tab_title=True, aim_in_window_title=True, aim_score_on_set=False)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    seeded: list[str | None] = []
    windows: list[str | None] = []

    def seed(_iid: str, _cwd: str, *, session: Session, **_k: Any) -> str:
        seeded.append(session.aim)
        return "⭐"

    def window(session: Session) -> bool:
        windows.append(session.aim)
        return True

    monkeypatch.setattr(tabsymbol, "seed_title", seed)
    monkeypatch.setattr(tabsymbol, "push_window_title", window)
    hooks.handle_session_start({"session_id": "s1", "cwd": "/repo"})
    assert seeded == ["tp#2172"]
    assert windows == ["tp#2172"]
    got = Store().get("s1")
    assert got is not None and got.aim == "tp#2172"
