"""``ccc sessions -j``: the §6 envelope over both accounts, with S-IDENT tab validation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from command_center import bridge_cli, cli, config
from command_center.adapters import claude_agents
from command_center.adapters.claude_agents import AgentEntry
from command_center.models import LiveSession
from command_center.store import Store
from command_center.tab_titles import ItermPane

PRIV = "/home/u/.claude"
WORK = "/home/u/.claude-work"


@dataclass
class PsRow:
    ppid: int
    tty: str
    stat: str = "S+"
    command: str = "claude"


class FakeAdapter:
    def __init__(self, lives: list[LiveSession]) -> None:
        self.lives = lives

    def discover(self) -> list[LiveSession]:
        return self.lives


def _live(sid: str, pid: int, config_dir: str = PRIV, **kw: Any) -> LiveSession:
    base: dict[str, Any] = {
        "pid": pid,
        "session_id": sid,
        "cwd": "/r/voice",
        "alive": True,
        "config_dir": config_dir,
        "raw_status": "idle",
        "status_updated_at": 1_791_000_000_000,
    }
    base.update(kw)
    return LiveSession(**base)


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "s.db")
    yield s
    s.close()


def _collect(  # pylint: disable=too-many-arguments
    store: Store,
    lives: list[LiveSession],
    ps: dict[int, PsRow],
    panes: list[ItermPane] | None,
    agents: list[AgentEntry] | None = None,
) -> list[dict[str, Any]]:
    labels = {PRIV: "private", WORK: "work"}
    return bridge_cli.collect_sessions(
        adapter=FakeAdapter(lives),  # type: ignore[arg-type]
        store=store,
        cfg=config.Config(session_names=True),
        panes_reader=lambda: panes,
        ps_reader=lambda: ps,
        account_label=lambda cd: labels.get(cd, cd),
        agents_reader=lambda: list(agents or []),
    )


def test_both_accounts_kinds_status_and_names(store: Store) -> None:
    store.ensure("a1", cwd="/r/voice")
    store.set_aim("a1", "parser")
    store.update_fields("a1", short_aim="the parser", iterm_session_id="w0t0p0:UA")
    store.ensure("b2", cwd="/r/voice")
    lives = [
        _live("a1", 11),
        _live("b2", 22, WORK, kind="bg", raw_status="busy"),
        _live("c3", 33, raw_status="compacting"),  # unknown word, no ccc row: no name
    ]
    ps = {11: PsRow(1, "ttys001"), 22: PsRow(1, "??"), 33: PsRow(1, "ttys009")}
    panes = [ItermPane("UA", "/dev/ttys001", "x")]
    out = {e["session_id"]: e for e in _collect(store, lives, ps, panes)}
    assert out["a1"]["account"] == "private" and out["a1"]["kind"] == "interactive"
    assert out["a1"]["name"] == "voice parser" and out["a1"]["name_origin"] == "fallback"
    assert out["a1"]["aim_short"] == "the parser"
    assert out["a1"]["status_at"] == "2026-10-03T04:00:00Z"
    assert out["a1"]["tab"] == {"iterm_session_id": "w0t0p0:UA", "validated": True}
    assert out["b2"]["account"] == "work" and out["b2"]["kind"] == "background"
    assert out["b2"]["status"] == "busy" and out["b2"]["tab"]["validated"] is False
    assert out["c3"]["status"] == "unknown" and out["c3"]["name"] is None
    assert set(out["a1"]) == {
        "session_id",
        "name",
        "name_origin",
        "account",
        "account_conflict",
        "pid",
        "kind",
        "status",
        "status_at",
        "aim_short",
        "tab",
    }


def test_sdk_cli_and_dead_entries_are_excluded(store: Store) -> None:
    lives = [
        _live("h1", 1, entrypoint="sdk-cli"),
        _live("d1", 2, alive=False),
        _live("dm", 4, kind="daemon"),  # Claude Code's bg supervisor, not a session
        _live("dw", 5, kind="daemon-worker"),
        _live("ok", 3),
    ]
    assert [e["session_id"] for e in _collect(store, lives, {}, [])] == ["ok"]


# --------------------------------------------------------------------------- D4 background
def test_background_job_from_claude_agents_is_listed(store: Store) -> None:
    """A ``claude --bg`` job with no worker: no registry entry, only the CLI roster has it."""
    job = AgentEntry(
        config_dir=PRIV,
        session_id="e38e3586-23f9",
        kind="background",
        cwd="/r/voice",
        name="claude agents explanation",
        state="blocked",
        started_at=1_791_000_000_000,
    )
    out = {e["session_id"]: e for e in _collect(store, [_live("a1", 11)], {}, [], [job])}
    entry = out["e38e3586-23f9"]
    assert entry["kind"] == "background" and entry["status"] == "blocked"
    assert entry["pid"] is None and entry["account"] == "private"
    assert entry["tab"] == {"iterm_session_id": None, "validated": False}
    assert entry["status_at"] == "2026-10-03T04:00:00Z"
    # a canonical name like the others (a ccc row is created for it), built from the
    # job's own name since it has no AIM
    assert entry["name"] == "voice claude" and entry["name_origin"] == "fallback"
    row = store.get("e38e3586-23f9")
    assert row is not None and row.config_dir == PRIV


@pytest.mark.parametrize(
    ("state", "status"),
    [("working", "busy"), ("blocked", "blocked"), ("failed", "blocked"), ("done", "idle")],
)
def test_background_state_maps_to_status(store: Store, state: str, status: str) -> None:
    job = AgentEntry(config_dir=WORK, session_id="bg1", kind="background", state=state)
    entries = _collect(store, [], {}, [], [job])
    assert [(e["status"], e["account"]) for e in entries] == [(status, "work")]


def test_background_registry_entry_gets_no_tab_and_its_state(store: Store) -> None:
    """A bg job whose worker runs is in the registry too; still no tab, status from state."""
    store.ensure("b2", cwd="/r/voice")
    store.update_fields("b2", iterm_session_id="w0t0p0:UA")
    job = AgentEntry(
        config_dir=PRIV, session_id="b2", kind="background", pid=22, status="idle", state="working"
    )
    lives = [_live("b2", 22, kind="bg", raw_status="idle")]
    panes = [ItermPane("UA", "/dev/ttys001", "x")]
    entries = _collect(store, lives, {22: PsRow(1, "ttys001")}, panes, [job])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "background" and entry["status"] == "busy" and entry["pid"] == 22
    assert entry["tab"] == {"iterm_session_id": None, "validated": False}


# --------------------------------------------------------------------------- status mapping
@pytest.mark.parametrize(
    ("raw", "status"),
    [("busy", "busy"), ("shell", "busy"), ("idle", "idle"), ("waiting", "waiting")],
)
def test_registry_status_maps_to_the_contract(store: Store, raw: str, status: str) -> None:
    entries = _collect(store, [_live("a1", 11, raw_status=raw)], {}, [])
    assert [e["status"] for e in entries] == [status]


def test_the_cli_view_wins_over_the_registry(store: Store) -> None:
    """``claude agents`` is the CLI's own view: it decides when both report a status."""
    agent = AgentEntry(config_dir=PRIV, session_id="a1", kind="interactive", pid=11, status="busy")
    stale = AgentEntry(config_dir=WORK, session_id="a2", kind="interactive", status="busy")
    lives = [_live("a1", 11, raw_status="idle"), _live("a2", 12, raw_status="idle")]
    out = {e["session_id"]: e for e in _collect(store, lives, {}, [], [agent, stale])}
    assert out["a1"]["status"] == "busy"
    assert out["a2"]["status"] == "idle"  # another account's roster entry is not this one


def test_status_tables_cover_claude_code_2_1_295() -> None:
    assert {claude_agents.contract_status(s) for s in ("busy", "shell")} == {"busy"}
    assert claude_agents.contract_status("idle") == "idle"
    assert claude_agents.contract_status("waiting") == "waiting"
    assert claude_agents.contract_status("compacting") == "unknown"
    assert claude_agents.contract_status(None) == "unknown"
    assert claude_agents.background_status("stopped") == "idle"
    assert claude_agents.background_status("", "shell") == "busy"  # no state: registry word
    assert claude_agents.background_status("brand-new") == "unknown"


# --------------------------------------------------------------------------- roster reader
def test_list_agents_asks_every_account_and_survives_failures() -> None:
    priv = json.dumps(
        [
            {"id": "e38e", "cwd": "/u", "kind": "background", "startedAt": 5,
             "sessionId": "bg-1", "name": "explain", "state": "blocked"},
            {"pid": 7, "kind": "interactive", "startedAt": 6, "sessionId": "it-1",
             "status": "busy", "waitingFor": "input needed"},
            {"kind": "interactive", "pid": 8},  # no sessionId: skipped
        ]
    )  # fmt: skip
    outputs = {PRIV: priv, WORK: "not json", "/broken": ""}
    got = claude_agents.list_agents([PRIV, WORK, "/broken"], runner=outputs.__getitem__)
    assert [(a.config_dir, a.session_id, a.kind) for a in got] == [
        (PRIV, "bg-1", "background"),
        (PRIV, "it-1", "interactive"),
    ]
    bg, it = got
    assert bg.pid == 0 and bg.state == "blocked" and bg.name == "explain" and bg.background
    assert it.pid == 7 and it.status == "busy" and not it.background
    assert claude_agents.list_agents([]) == []


def test_validated_false_when_the_stored_tab_has_another_tty(store: Store) -> None:
    store.ensure("a1", cwd="/r")
    store.update_fields("a1", iterm_session_id="w0t0p0:UA")
    ps = {11: PsRow(1, "ttys001")}
    panes = [ItermPane("UA", "/dev/ttys005", "x"), ItermPane("UB", "/dev/ttys006", "y")]
    entries = _collect(store, [_live("a1", 11)], ps, panes)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tab"] == {"iterm_session_id": "w0t0p0:UA", "validated": False}


def test_tty_fallback_resolves_a_missing_or_wrong_tab(store: Store) -> None:
    store.ensure("a1", cwd="/r")  # no stored tab id at all
    store.ensure("b2", cwd="/r")
    store.update_fields("b2", iterm_session_id="w0t0p0:GONE")  # stale id
    ps = {11: PsRow(1, "ttys001"), 22: PsRow(1, "/dev/ttys002")}
    panes = [ItermPane("UA", "/dev/ttys001", "x"), ItermPane("UB", "/dev/ttys002", "y")]
    out = {
        e["session_id"]: e["tab"]
        for e in _collect(store, [_live("a1", 11), _live("b2", 22)], ps, panes)
    }
    assert out["a1"] == {"iterm_session_id": "UA", "validated": True}
    assert out["b2"] == {"iterm_session_id": "UB", "validated": True}


def test_iterm_unreadable_reports_unvalidated(store: Store) -> None:
    store.ensure("a1", cwd="/r")
    store.update_fields("a1", iterm_session_id="w0t0p0:UA")
    entries = _collect(store, [_live("a1", 11)], {11: PsRow(1, "ttys001")}, None)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tab"] == {"iterm_session_id": "w0t0p0:UA", "validated": False}


def test_account_conflict_is_flagged(store: Store) -> None:
    entries = _collect(store, [_live("a1", 11, "", conflict=True)], {}, [])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["account_conflict"] is True and entry["account"] is None


def test_tab_lookup_prefers_the_live_pid_row(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import store as store_mod

    monkeypatch.setattr(store_mod, "pid_alive", lambda pid: pid == 42)
    store.ensure("dead", cwd="/r")
    store.update_fields("dead", iterm_session_id="w0t0p0:UA", last_seen_pid=7, last_response_at=999)
    store.ensure("live", cwd="/r")
    store.update_fields("live", iterm_session_id="w0t0p0:UA", last_seen_pid=42, last_response_at=1)
    got = store.session_for_tab_uuid("UA")
    assert got is not None and got.session_id == "live"


# --------------------------------------------------------------------------- CLI surface
@pytest.fixture(name="cli_env")
def cli_env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[LiveSession]:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    lives = [_live("a1", 11)]
    from command_center import tab_titles, terminal
    from command_center.adapters import claude as claude_adapter

    monkeypatch.setattr(claude_adapter.ClaudeAdapter, "discover", lambda self: lives)
    monkeypatch.setattr(tab_titles, "read_panes", lambda: [])
    monkeypatch.setattr(terminal, "ps_table", lambda: {})
    return lives


@pytest.mark.usefixtures("cli_env")
def test_envelope_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sessions", "-j"]) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["schema_version"] == 1 and out["ok"] is True and out["error"] is None
    assert [s["session_id"] for s in out["data"]["sessions"]] == ["a1"]


@pytest.mark.usefixtures("cli_env")
def test_unknown_argument_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["sessions", "-j", "--bogus"])
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["data"] is None and out["error"]["code"] == "usage"


@pytest.mark.usefixtures("cli_env")
def test_internal_error_exits_3(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(bridge_cli, "collect_sessions", boom)
    assert cli.main(["sessions", "-j"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"]["code"] == "internal"
