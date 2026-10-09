"""Session names: generated once, unique across accounts, hand-set titles win (D2/D3)."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from command_center import config, llm, session_names, tabsymbol
from command_center.bridge_cli import name_command
from command_center.store import Store


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "names.db")
    yield s
    s.close()


def _cfg(command: str = "") -> config.Config:
    return config.Config(llm_custom_command=command)


def _row(
    store: Store, sid: str, cwd: str = "/r/voice", aim: str | None = None, **fields: Any
) -> None:
    store.ensure(sid, cwd=cwd)
    if aim:
        store.set_aim(sid, aim)
    if fields:
        store.update_fields(sid, **fields)


# --------------------------------------------------------------------------- namer
def test_normalize_generated_shapes_a_model_reply() -> None:
    assert session_names.normalize_generated('"Voice Bridge!"\nbecause…') == "voice bridge"
    assert session_names.normalize_generated("Zürich Café Löwen extra") == "zurich cafe"
    assert session_names.normalize_generated("```\nvoice bridge\n```") == "voice bridge"
    long = session_names.normalize_generated("supercalifragilistic expialidocious")
    assert long == "supercalifragilistic"
    assert len(session_names.normalize_generated("a" * 40) or "") == session_names.MAX_CHARS
    assert session_names.normalize_generated("") is None
    assert session_names.normalize_generated("!!!") is None


def test_llm_namer_uses_the_session_name_purpose_with_a_30s_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake(
        prompt: str, command: str, timeout: int = 0, *, purpose: str = "", note: str = ""
    ) -> str:
        del note
        seen.update(prompt=prompt, command=command, timeout=timeout, purpose=purpose)
        return "Voice Bridge"

    monkeypatch.setattr(llm, "run_custom", fake)
    assert (
        session_names.llm_name("build the voice bridge", "/r/my-stt-tts", "router")
        == "voice bridge"
    )
    assert (
        seen["purpose"] == "session-name" and seen["timeout"] == session_names.LLM_TIMEOUT_SEC == 30
    )
    assert seen["command"] == "router" and "my-stt-tts" in seen["prompt"]


def test_no_router_means_no_llm_call(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setattr(llm, "run_custom", lambda *_a, **_k: pytest.fail("LLM called"))
    _row(store, "s1", cwd="/r/my-stt-tts", aim="implement the voice bridge")
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("")) == "my-stt-tts voice"


def test_fallback_is_repo_folder_plus_first_aim_noun(tmp_path: Path) -> None:
    repo = tmp_path / "my_repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    assert (
        session_names.fallback_name(str(repo / "sub"), "Make sure the parser works")
        == "my-repo parser"
    )
    assert session_names.fallback_name(str(repo), None) == "my-repo"
    assert session_names.fallback_name("/x/averyveryverylongfoldername", "fix tokenizer") == (
        "averyveryveryl tokenizer"
    )
    assert session_names.fallback_name("", "#tp 855") == "session"


def test_fallback_is_at_most_two_words_and_skips_generic_nouns(tmp_path: Path) -> None:
    def name(folder: str, aim: str | None) -> str:
        repo = tmp_path / folder
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        return session_names.fallback_name(str(repo), aim)

    assert name("runai-quickstart", "tp off items") == "runai-quickstart"  # no good noun
    assert name("02-llm-staff", "lack agent agent standards") == "02-llm-staff agent"
    assert name("browser-login", "make shared crome run somewhere else") == "browser-login crome"
    assert name("sdsc-automations", "/home/u/x/PROMPT.md") == "sdsc-automations"  # a path
    assert name("oicd-azure-build", "oicd continue") == "oicd-azure-build"  # folder word
    assert name("books-download", "./welib_zlib_daily.py run") == "books-download welib"
    assert name("my-stt-tts", "#tp 855") == "my-stt-tts"
    assert name("Zürich_Tools", "Café Löwen") == "zurich-tools cafe"
    # the first noun that fits beside the WHOLE folder (24 chars), never a cut folder
    assert name("runai-quickstart", "authentication yubikey") == "runai-quickstart yubikey"
    for got in (name("runai-quickstart", "users shared items lack"), name("x", "a b c d")):
        assert len(got) <= session_names.MAX_CHARS and len(got.split()) <= 2
        assert got == got.lower() and got.isascii()


def test_fallback_clash_tries_the_next_noun_before_a_suffix(store: Store, tmp_path: Path) -> None:
    repo = tmp_path / "runai-quickstart"
    (repo / ".git").mkdir(parents=True)
    _row(store, "s1", cwd=str(repo), aim="ssh nas")
    _row(store, "s2", cwd=str(repo), aim="ssh gitlab")
    _row(store, "s3", cwd=str(repo), aim="ssh")
    names = [session_names.ensure_name(store, s, use_llm=False, cfg=_cfg()) for s in ("s1", "s2")]
    assert names == ["runai-quickstart ssh", "runai-quickstart gitlab"]
    # no other noun: the §7.1 clash rule (account label, then a session-id word)
    third = session_names.ensure_name(store, "s3", use_llm=False, cfg=_cfg())
    assert third is not None and third.startswith("runai-quickstart ssh ")


def test_router_failure_falls_back(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setattr(llm, "run_custom", lambda *_a, **_k: None)
    _row(store, "s1", cwd="/r/voice", aim="ship the parser")
    assert (
        session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("router")) == "voice parser"
    )
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == session_names.ORIGIN_FALLBACK


def test_generated_once_and_aim_change_never_renames(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    replies = iter(["voice bridge", "something else"])
    monkeypatch.setattr(llm, "run_custom", lambda *_a, **_k: next(replies))
    _row(store, "s1", aim="build the voice bridge")
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == "voice bridge"
    store.set_aim("s1", "a completely different goal now")
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == "voice bridge"
    got = store.get("s1")
    assert got is not None
    assert got.canonical_name == "voice bridge" and got.canonical_name_origin == "llm"
    assert got.name_source_aim == "build the voice bridge"


# --------------------------------------------------------------------------- provisional
def test_fallback_is_upgraded_once_by_the_llm_namer(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    """``ccc sessions -j`` names without an LLM; the namer replaces that ONCE, then never."""
    replies = iter(["voice bridge", "something else"])
    calls: list[str] = []

    def fake(prompt: str, *_a: Any, **_k: Any) -> str:
        calls.append(prompt)
        return next(replies)

    monkeypatch.setattr(llm, "run_custom", fake)
    _row(store, "s1", aim="build the parser")
    assert session_names.ensure_name(store, "s1", use_llm=False, cfg=_cfg("r")) == "voice parser"
    store.set_aim("s1", "build the voice bridge")  # the AIM moved on before the namer ran
    got = store.get("s1")
    assert got is not None and session_names.upgradable(got, _cfg("r"))
    assert not session_names.upgradable(got, _cfg(""))  # no router: nothing to upgrade with
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == "voice bridge"
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == "llm"
    assert got.name_source_aim == "build the parser"  # first AIM, snapshotted at the upgrade
    assert not session_names.upgradable(got, _cfg("r"))
    # never twice
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == "voice bridge"
    assert len(calls) == 1


def test_failed_upgrades_keep_the_fallback_and_are_bounded(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(llm, "run_custom", lambda p, *_a, **_k: calls.append(p))
    _row(store, "s1", aim="ship the parser", canonical_name="voice parser")
    store.update_fields("s1", canonical_name_origin="fallback")
    for _ in range(session_names.MAX_UPGRADE_TRIES + 2):
        assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == (
            "voice parser"
        )
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == "fallback"
    assert got.name_upgrade_tries == session_names.MAX_UPGRADE_TRIES
    assert len(calls) == session_names.MAX_UPGRADE_TRIES


@pytest.mark.parametrize("origin", ["llm", "manual", "manual-tab"])
def test_final_names_are_never_replaced_by_the_namer(
    monkeypatch: pytest.MonkeyPatch, store: Store, origin: str
) -> None:
    monkeypatch.setattr(llm, "run_custom", lambda *_a, **_k: pytest.fail("LLM called"))
    _row(store, "s1", aim="ship the parser", canonical_name="sauna talk")
    store.update_fields("s1", canonical_name_origin=origin)
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == "sauna talk"
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == origin


def test_a_peer_rename_during_the_llm_call_wins(
    monkeypatch: pytest.MonkeyPatch, store: Store, tmp_path: Path
) -> None:
    """The upgrade replaces only the fallback it started from (inside the transaction)."""

    def fake(*_a: Any, **_k: Any) -> str:
        with Store(tmp_path / "names.db") as peer:
            session_names.adopt_manual_title(peer, "s1", "sauna talk")
        return "voice bridge"

    monkeypatch.setattr(llm, "run_custom", fake)
    _row(store, "s1", aim="ship the parser", canonical_name="voice parser")
    store.update_fields("s1", canonical_name_origin="fallback")
    assert session_names.ensure_name(store, "s1", use_llm=True, cfg=_cfg("r")) == "sauna talk"
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == "manual-tab"


def test_ccc_name_auto_upgrades_and_prints_rename(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    monkeypatch.setattr(config, "load_config", lambda: _cfg("router"))
    monkeypatch.setattr(llm, "run_custom", lambda *_a, **_k: "voice bridge")
    _row(store, "s1", aim="build the voice bridge", canonical_name="voice parser")
    store.update_fields("s1", canonical_name_origin="fallback")
    data = name_command(_name_args("s1", auto=True), store=store)
    assert data["name"] == "voice bridge" and data["name_origin"] == "llm"
    assert data["changed"] is True and data["rename_command"] == "/rename voice bridge"


def test_daemon_backfill_picks_unnamed_and_upgradable_rows(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    from command_center import daemon, spawn
    from command_center.models import LiveSession

    spawned: list[list[str]] = []
    monkeypatch.setattr(spawn, "spawn_ccc", spawned.append)
    _row(store, "new", aim="a")
    _row(store, "prov", aim="b", canonical_name="voice b")
    store.update_fields("prov", canonical_name_origin="fallback")
    _row(store, "spent", aim="c", canonical_name="voice c", name_upgrade_tries=3)
    store.update_fields("spent", canonical_name_origin="fallback")
    _row(store, "final", aim="d", canonical_name="voice d")
    store.update_fields("final", canonical_name_origin="llm")
    live = {
        sid: LiveSession(pid=1, session_id=sid, cwd="/r", alive=True)
        for sid in ("new", "prov", "spent", "final")
    }
    cfg = config.Config(llm_custom_command="router", max_summaries_per_run=10)
    got = daemon._backfill_session_names(store, cfg, live, dry_run=False)  # noqa: SLF001
    assert sorted(got) == ["new", "prov"]
    assert sorted(cmd[-1] for cmd in spawned) == ["new", "prov"]
    no_router = config.Config(llm_custom_command="", max_summaries_per_run=10)
    dry = daemon._backfill_session_names(store, no_router, live, dry_run=True)  # noqa: SLF001
    assert dry == ["new"]


# --------------------------------------------------------------------------- uniqueness
def test_uniqueness_is_casefolded_and_adds_account_then_id(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    monkeypatch.setattr(session_names, "_account_label", lambda cd: "work" if cd else "private")
    _row(store, "aaaa1111", cwd="/r/voice", aim="parser", config_dir="")
    _row(store, "bbbb2222", cwd="/r/voice", aim="parser", config_dir="/w")
    _row(store, "cccc3333", cwd="/r/voice", aim="parser", config_dir="/w")
    store.update_fields("aaaa1111", canonical_name="Voice Parser")  # different case, same name
    assert (
        session_names.ensure_name(store, "bbbb2222", use_llm=False, cfg=_cfg())
        == "voice parser work"
    )
    assert (
        session_names.ensure_name(store, "cccc3333", use_llm=False, cfg=_cfg())
        == "voice parser cccc"
    )


def test_done_sessions_do_not_hold_names(store: Store) -> None:
    _row(store, "old", aim="parser", canonical_name="voice parser", done=True)
    _row(store, "new", aim="parser")
    assert session_names.ensure_name(store, "new", use_llm=False, cfg=_cfg()) == "voice parser"


def test_concurrent_writer_wins_and_is_kept(store: Store, tmp_path: Path) -> None:
    """The transaction re-reads the row: a name a peer stored meanwhile is not replaced."""
    _row(store, "s1", aim="parser")
    with Store(tmp_path / "names.db") as peer:
        peer.update_fields("s1", canonical_name="peer name", canonical_name_origin="llm")
    got = session_names._write_name(  # noqa: SLF001
        store, "s1", "mine", "fallback", source_aim=None, only_if_unnamed=True, refuse_clash=False
    )
    assert got == "peer name"


# --------------------------------------------------------------------------- ccc name
def _name_args(sid: str, *words: str, auto: bool = False) -> argparse.Namespace:
    return argparse.Namespace(session=sid, name=list(words) or None, auto=auto, json=True)


def test_ccc_name_renames_and_prints_rename(store: Store) -> None:
    _row(store, "s1", aim="parser")
    data = name_command(_name_args("s1", "Voice", "Bridge"), store=store)
    assert data["name"] == "Voice Bridge" and data["rename_command"] == "/rename Voice Bridge"
    got = store.get("s1")
    assert got is not None and got.canonical_name_origin == "manual"


def test_ccc_name_collision_is_rejected(store: Store) -> None:
    from command_center.bridge_json import BridgeError

    _row(store, "s1", canonical_name="voice bridge")
    _row(store, "s2")
    with pytest.raises(BridgeError) as err:
        name_command(_name_args("s2", "VOICE", "bridge"), store=store)
    assert err.value.code == "name_taken" and err.value.exit_code == 1
    got = store.get("s2")
    assert got is not None and got.canonical_name is None


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


def test_find_is_case_insensitive(store: Store) -> None:
    _row(store, "s1", canonical_name="Voice Bridge")
    assert [s.session_id for s in session_names.find(store, "voice BRIDGE")] == ["s1"]


# --------------------------------------------------------------------------- hand-set titles
def _written(store: Store, sid: str, core: str, age_ms: int) -> None:
    session_names.record_title_write(store, sid, core, int(time.time() * 1000) - age_ms)


def test_manual_title_needs_the_5s_grace(store: Store) -> None:
    _row(store, "s1", cwd="/r/voice", canonical_name="voice bridge", iterm_session_id="w0t0p0:U1")
    _written(store, "s1", "🔺 voice bridge", age_ms=1000)
    now = int(time.time() * 1000)
    session = store.get("s1")
    assert session is not None
    assert tabsymbol.manual_title_candidate(session, "🔺 sauna talk", now) is None  # in grace
    assert tabsymbol.manual_title_candidate(session, "🔺 sauna talk", now + 5000) == "sauna talk"


def test_manual_title_strips_marker_badge_aim_and_controls(store: Store) -> None:
    _row(store, "s1", cwd="/r/voice", canonical_name="voice bridge")
    _written(store, "s1", "🔺 voice bridge", age_ms=60_000)
    session = store.get("s1")
    assert session is not None
    now = int(time.time() * 1000)
    assert tabsymbol.manual_title_candidate(session, "🔴 🔺 my\x07 tab 🎯 the aim", now) == "my tab"
    # empty after stripping, ccc's own shapes and the current name are never "manual"
    assert tabsymbol.manual_title_candidate(session, "🔴 🔺  ", now) is None
    assert tabsymbol.manual_title_candidate(session, "🔺 voice bridge 🎯 x", now) is None
    leaf = tabsymbol.title_core("🟢", "/r/voice")  # the folder-leaf title (no name)
    assert tabsymbol.manual_title_candidate(session, leaf, now) is None
    # no ccc baseline yet → nothing can be called hand-set
    _row(store, "s2", cwd="/r/voice")
    fresh = store.get("s2")
    assert fresh is not None and tabsymbol.manual_title_candidate(fresh, "anything", now) is None


def _sync_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live: dict[str, str]) -> list[Any]:
    from command_center import tab_titles, terminal

    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "badges"))
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: config.Config(session_names=True, aim_in_tab_title=False),
    )
    writes: list[Any] = []
    monkeypatch.setattr(
        terminal, "set_session_titles_preserving", lambda c, marker="": writes.append(("plain", c))
    )
    monkeypatch.setattr(
        tab_titles, "set_titles_cas", lambda e, marker="": writes.append(("cas", e))
    )
    monkeypatch.setattr(
        tab_titles,
        "read_panes",
        lambda: [tab_titles.ItermPane(uuid=u, tty="", name=n) for u, n in live.items()],
    )
    return writes


def test_sync_adopts_a_hand_set_title_and_never_writes_that_tab_again(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(store, "s1", cwd="/r/voice", canonical_name="voice bridge", iterm_session_id="w0t0p0:U1")
    _written(store, "s1", "🔺 voice bridge", age_ms=60_000)
    writes = _sync_env(monkeypatch, tmp_path, {"U1": "🔴 🔺 sauna talk"})
    tabsymbol.sync_live(store)
    got = store.get("s1")
    assert got is not None
    assert got.canonical_name == "sauna talk" and got.canonical_name_origin == "manual-tab"
    assert not writes  # the tab was not written in the adopting pass
    writes.clear()
    tabsymbol.sync_live(store)  # nor in any later one
    assert not writes
    tabsymbol.push_title(got)
    tabsymbol.seed_title("w0t0p0:U1", "/r/voice", session=got, store=store)
    assert not writes


def test_sync_writes_name_then_compare_and_swap(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(store, "s1", cwd="/r/voice", canonical_name="voice bridge", iterm_session_id="w0t0p0:U1")
    writes = _sync_env(monkeypatch, tmp_path, {"U1": "🔺 voice"})
    tabsymbol.sync_live(store)
    kind, cores = writes[0]
    core = cores["w0t0p0:U1"]
    assert kind == "plain" and core.endswith(" voice bridge")
    got = store.get("s1")
    assert got is not None and got.title_written == core and got.title_generation == 1
    store.update_fields("s1", aim="new aim")
    store.update_fields("s1", canonical_name="voice bridge two")
    writes.clear()
    tabsymbol.sync_live(store)  # second write: conditional on the tab still showing `core`
    assert writes and writes[0][0] == "cas" and writes[0][1]["w0t0p0:U1"][0] == core


def test_watcher_override_becomes_the_name(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_config", lambda: config.Config(session_names=True))
    _row(store, "s1", cwd="/r/voice", canonical_name="voice bridge", iterm_session_id="w0t0p0:U1")
    assert tabsymbol.adopt_overrides([("🔺 sauna talk", ["U1"])], store=store) == 1
    got = store.get("s1")
    assert got is not None and got.canonical_name == "sauna talk"
    assert got.canonical_name_origin == "manual-tab"
    assert tabsymbol.adopt_overrides([("🔺 sauna talk", ["U1"])], store=store) == 0  # idempotent


def test_hand_set_title_wins_over_the_generator(store: Store) -> None:
    _row(store, "s1", aim="parser")
    session_names.adopt_manual_title(store, "s1", "sauna talk")
    assert session_names.ensure_name(store, "s1", use_llm=False, cfg=_cfg()) == "sauna talk"
    got = store.get("s1")
    assert got is not None and session_names.titles_frozen(got)


def test_hand_set_clash_gets_a_suffix_and_is_not_readopted(store: Store) -> None:
    _row(store, "s1", canonical_name="sauna talk")
    _row(store, "s2")
    assert session_names.adopt_manual_title(store, "s2", "sauna talk") == "sauna talk s2"
    got = store.get("s2")
    assert got is not None and session_names.already_named(got, "sauna talk")


def test_reconcile_never_clobbers_the_canonical_name(store: Store) -> None:
    from command_center.models import LiveSession

    _row(store, "s1", canonical_name="voice bridge", canonical_name_origin="llm")
    store.upsert_from_live(LiveSession(pid=1, session_id="s1", cwd="/r", name="other"))
    got = store.get("s1")
    assert got is not None and got.canonical_name == "voice bridge"
    assert got.observed_runtime_name == "other" and got.runtime_name_applied_at == 0
    store.upsert_from_live(LiveSession(pid=1, session_id="s1", cwd="/r", name="voice bridge"))
    got = store.get("s1")
    assert got is not None and got.runtime_name_applied_at > 0


@pytest.mark.parametrize(
    ("aim", "ok"),
    [
        ("#tp 855", False),
        ("tp#855", False),
        ("ticket 12 #wip", False),
        ("", False),
        ("build the voice bridge", True),
        ("fix tp#855 voice bridge", True),
    ],
)
def test_aim_without_substance_is_not_sent_to_the_llm(aim: str, ok: bool) -> None:
    assert session_names.aim_has_substance(aim) is ok
