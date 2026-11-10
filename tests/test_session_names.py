"""Hand-set session names (`ccc name`) and the tab-title bookkeeping; ccc generates none."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from command_center import config, llm_pending, llmrouting, session_names, tabsymbol
from command_center.bridge_cli import name_command
from command_center.store import Store


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "names.db")
    yield s
    s.close()


def _row(store: Store, sid: str, cwd: str = "/r/voice", **fields: Any) -> None:
    store.ensure(sid, cwd=cwd)
    if fields:
        store.update_fields(sid, **fields)


# --------------------------------------------------------------------------- no namer
def test_no_session_name_knob_or_purpose_is_left() -> None:
    """tp#1808: no config key, no LLM purpose, no pending queue for session names."""
    assert "session_names" not in config.DEFAULTS
    assert "session-name" not in llmrouting.PURPOSES
    assert "session-name" not in llm_pending.PURPOSES


def test_an_old_session_names_key_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old config.toml that still says `session_names = …` loads and blocks no writer."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("session_names = false\nshort_aim = true\n", encoding="utf-8")
    cfg = config.load_config()
    assert cfg.loaded_from_disk and cfg.short_aim is True
    assert not hasattr(cfg, "session_names")
    assert "session_names" not in config.unknown_config_keys()


# --------------------------------------------------------------------------- ccc name
def _name_args(sid: str, *words: str) -> argparse.Namespace:
    return argparse.Namespace(session=sid, name=list(words) or None, json=True)


def test_ccc_name_renames_and_prints_rename(store: Store) -> None:
    _row(store, "s1", title_written="🔺 voice")
    data = name_command(_name_args("s1", "Voice", "Bridge"), store=store)
    assert data["name"] == "Voice Bridge" and data["rename_command"] == "/rename Voice Bridge"
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == "manual"
    assert got.title_written is None  # the next title write lands unconditionally


def test_ccc_name_collision_is_rejected_casefolded(store: Store) -> None:
    from command_center.bridge_json import BridgeError

    _row(store, "s1", canonical_name="voice bridge")
    _row(store, "s2")
    with pytest.raises(BridgeError) as err:
        name_command(_name_args("s2", "VOICE", "bridge"), store=store)
    assert err.value.code == "name_taken" and err.value.exit_code == 1
    got = store.get("s2")
    assert got is not None and got.canonical_name is None


def test_done_sessions_do_not_hold_names(store: Store) -> None:
    _row(store, "old", canonical_name="voice parser", done=True)
    _row(store, "new")
    assert session_names.rename(store, "new", "voice parser") == "voice parser"


def test_rename_refuses_empty_long_and_unknown(store: Store) -> None:
    _row(store, "s1")
    with pytest.raises(ValueError):
        session_names.rename(store, "s1", " \x07 ")
    with pytest.raises(ValueError):
        session_names.rename(store, "s1", "x" * (session_names.MAX_MANUAL_CHARS + 1))
    with pytest.raises(ValueError):
        session_names.rename(store, "nope", "fine")


def test_ccc_name_cli_envelope_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import cli

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    with Store() as db:
        _row(db, "s1-full-id", canonical_name="taken")
        _row(db, "s2-full-id")
    assert cli.main(["name", "-s", "s2", "taken", "-j"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"]["code"] == "name_taken"
    assert cli.main(["name", "-s", "s2", "free", "-j"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["name"] == "free" and out["data"]["rename_command"] == "/rename free"
    assert cli.main(["name", "-s", "nope", "-j"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    with pytest.raises(SystemExit) as exc:  # no automatic namer any more (tp#1808)
        cli.main(["name", "-s", "s2", "-A", "-j"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_reconcile_never_clobbers_the_canonical_name(store: Store) -> None:
    from command_center.models import LiveSession

    _row(store, "s1", canonical_name="voice bridge", canonical_name_origin="manual")
    store.upsert_from_live(LiveSession(pid=1, session_id="s1", cwd="/r", name="other"))
    got = store.get("s1")
    assert got is not None and got.canonical_name == "voice bridge"
    assert got.observed_runtime_name == "other" and got.runtime_name_applied_at == 0
    store.upsert_from_live(LiveSession(pid=1, session_id="s1", cwd="/r", name="voice bridge"))
    got = store.get("s1")
    assert got is not None and got.runtime_name_applied_at > 0


# --------------------------------------------------------------------------- ccc name --reset
def test_reset_drops_a_hand_set_name_and_unfreezes_the_tab(store: Store) -> None:
    _row(
        store,
        "s1",
        canonical_name="obsidian",
        canonical_name_origin="manual-tab",
        title_written="🟧 voice",
    )
    before = store.get("s1")
    assert before is not None and session_names.titles_frozen(before)
    assert session_names.reset_name(store, "s1") is None
    got = store.get("s1")
    assert got is not None and got.canonical_name is None and got.canonical_name_origin == ""
    assert not session_names.titles_frozen(got) and got.title_written is None
    # a legacy automatic name is left as it is
    _row(store, "s2", canonical_name="voice parser", canonical_name_origin="fallback")
    assert session_names.reset_name(store, "s2") == "voice parser"
    with pytest.raises(ValueError):
        session_names.reset_name(store, "nope")


def test_ccc_name_reset_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import cli

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    with Store() as db:
        _row(db, "s1-full-id", canonical_name="sauna talk", canonical_name_origin="manual")
    assert cli.main(["name", "-s", "s1", "-R", "-j"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["name"] is None and out["data"]["changed"] is True
    assert cli.main(["name", "-s", "s1", "--reset"]) == 0
    assert "nothing to reset" in capsys.readouterr().out
    assert cli.main(["name", "-s", "s1", "-R", "x", "-j"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


# --------------------------------------------------------------------------- tab titles
def _sync_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    from command_center import tab_titles, terminal

    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    monkeypatch.setattr(config, "load_config", lambda: config.Config(aim_in_tab_title=False))
    writes: list[Any] = []

    def _plain(cores: dict[str, str], marker: str = "") -> dict[str, str]:
        del marker
        writes.append(("plain", cores))
        return {tab_titles.uuid_of(iid): core for iid, core in cores.items()}  # it landed

    def _cas(entries: dict[str, tuple[str, str]], marker: str = "") -> dict[str, str]:
        del marker
        writes.append(("cas", entries))
        return {tab_titles.uuid_of(iid): core for iid, (_exp, core) in entries.items()}

    monkeypatch.setattr(terminal, "set_session_titles_preserving", _plain)
    monkeypatch.setattr(tab_titles, "set_titles_cas", _cas)

    def _no_read() -> None:
        raise AssertionError("the title sync reads no live titles")

    monkeypatch.setattr(tab_titles, "read_panes", _no_read)
    return writes


def test_sync_shows_no_name_then_compare_and_swap(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(store, "s1", cwd="/r/voice", canonical_name="voice bridge", iterm_session_id="w0t0p0:U1")
    writes = _sync_env(monkeypatch, tmp_path)
    tabsymbol.sync_live(store)
    kind, cores = writes[0]
    core = cores["w0t0p0:U1"]
    badge = core.split(" ", 1)[0]
    assert kind == "plain" and core == tabsymbol.title_core(badge, "/r/voice")
    assert "bridge" not in core
    got = store.get("s1")
    assert got is not None and got.title_written == core and got.title_generation == 1
    store.update_fields("s1", cwd="/r/other")
    writes.clear()
    tabsymbol.sync_live(store)  # second write: conditional on the tab still showing `core`
    assert writes and writes[0][0] == "cas" and writes[0][1]["w0t0p0:U1"][0] == core


def test_a_frozen_tab_is_never_written(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(
        store,
        "s1",
        cwd="/r/voice",
        canonical_name="sauna talk",
        canonical_name_origin="manual-tab",
        iterm_session_id="w0t0p0:U1",
    )
    writes = _sync_env(monkeypatch, tmp_path)
    tabsymbol.sync_live(store)
    got = store.get("s1")
    assert got is not None
    tabsymbol.push_title(got)
    tabsymbol.seed_title("w0t0p0:U1", "/r/voice", session=got, store=store)
    assert not writes
