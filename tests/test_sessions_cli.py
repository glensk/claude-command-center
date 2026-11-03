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
    def __init__(
        self, lives: list[LiveSession], transcripts: dict[str, Path] | None = None
    ) -> None:
        self.lives = lives
        self.transcripts = transcripts or {}

    def discover(self) -> list[LiveSession]:
        return self.lives

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        del cwd, config_dir
        return self.transcripts.get(session_id)


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
    *,
    subagents_reader: Any = None,
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
        subagents_reader=subagents_reader,
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
        "launch_profile",
        "pid",
        "kind",
        "status",
        "status_at",
        "aim_short",
        "display_name",
        "staff_agent",
        "tab",
    }
    assert out["a1"]["display_name"] == "parser" and out["a1"]["staff_agent"] is None
    assert out["b2"]["display_name"] == out["b2"]["name"]  # no AIM: the name
    assert out["c3"]["display_name"] is None


def test_display_name_is_the_staff_agent_name_or_the_aim(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tp#1366: a staff session is named after its agent (tab_name), any other by its AIM."""
    staff = tmp_path / "staff" / "zoho-solver"
    staff.mkdir(parents=True)
    (staff / "agent.yaml").write_text('name: zoho-solver\ntab_name: "zoho-agent"\n')
    roles = tmp_path / "tp" / "session-role"
    roles.mkdir(parents=True)
    (roles / "z1").write_text("zoho-solver\n")
    monkeypatch.setenv("TP_STAFF_ROOT", str(tmp_path / "staff"))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp"))
    store.ensure("z1", cwd="/r/voice")
    store.set_aim("z1", "answer tickets: tp#1236")
    store.ensure("d1", cwd="/r/voice")
    store.set_aim("d1", "fix decide: tp#1363, #1364")
    out = {
        e["session_id"]: e for e in _collect(store, [_live("z1", 11), _live("d1", 22)], {}, None)
    }
    assert (out["z1"]["display_name"], out["z1"]["staff_agent"]) == ("zoho-agent", "zoho-solver")
    assert (out["d1"]["display_name"], out["d1"]["staff_agent"]) == (
        "fix decide: tp#1363, #1364",
        None,
    )


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


# --------------------------------------------------------------------------- subagents key
_AGENT = {
    "id": "ag1",
    "description": "build B4",
    "agent_type": "builder",
    "state": "running",
    "started_at": "2026-11-03T02:51:34Z",
    "tp_id": 42,
}
_B2 = Path(__file__).parent / "fixtures" / "session_agents" / "proj" / "sess-0002.jsonl"


class _Reader:
    """A ``subagents_reader`` stub that records its calls and answers *known*."""

    def __init__(self, known: dict[str, list[dict[str, Any]]]) -> None:
        self.known = known
        self.calls: list[tuple[Store, list[tuple[str, Path | None]]]] = []

    def __call__(
        self, store: Store, targets: list[tuple[str, Path | None]]
    ) -> dict[str, list[dict[str, Any]]]:
        self.calls.append((store, list(targets)))
        return self.known


def test_subagents_key_present_with_entries(store: Store) -> None:
    reader = _Reader({"a1": [_AGENT]})
    lives = [_live("a1", 11), _live("b2", 22, kind="bg")]
    out = {e["session_id"]: e for e in _collect(store, lives, {}, [], subagents_reader=reader)}
    assert out["a1"]["agents"] == [_AGENT]
    assert "agents" not in out["b2"]  # the reader omitted it
    assert len(reader.calls) == 1 and reader.calls[0][0] is store
    assert [sid for sid, _ in reader.calls[0][1]] == ["a1", "b2"]  # background included


def test_subagents_key_present_and_empty(store: Store) -> None:
    entries = _collect(store, [_live("a1", 11)], {}, [], subagents_reader=_Reader({"a1": []}))
    assert entries[0]["agents"] == []


def test_subagents_key_absent_when_the_reader_omits_the_sid(store: Store) -> None:
    entries = _collect(store, [_live("a1", 11)], {}, [], subagents_reader=_Reader({}))
    assert "agents" not in entries[0]


def test_subagents_key_absent_when_the_reader_raises(store: Store) -> None:
    def boom(_store: Store, _targets: list[tuple[str, Path | None]]) -> dict[str, Any]:
        raise RuntimeError("cache unreadable")

    entries = _collect(store, [_live("a1", 11), _live("b2", 22)], {}, [], subagents_reader=boom)
    assert [e["session_id"] for e in entries] == ["a1", "b2"]
    assert all("agents" not in e for e in entries)


def test_subagents_key_absent_without_a_reader(store: Store) -> None:
    """The plain-text default (``subagents_reader=None``) never adds the key."""
    entries = _collect(store, [_live("a1", 11)], {}, [])
    assert "agents" not in entries[0]


def test_subagents_key_leaves_every_other_key_unchanged(store: Store) -> None:
    store.ensure("a1", cwd="/r/voice")
    store.set_aim("a1", "parser")
    lives = [_live("a1", 11)]
    plain = _collect(store, lives, {}, [])
    with_key = _collect(store, lives, {}, [], subagents_reader=_Reader({"a1": [_AGENT]}))
    assert with_key[0].pop("agents") == [_AGENT]
    assert with_key == plain


def test_subagents_key_on_an_account_conflict_row(store: Store) -> None:
    lives = [_live("a1", 11, "", conflict=True)]
    entries = _collect(store, lives, {}, [], subagents_reader=_Reader({"a1": [_AGENT]}))
    assert entries[0]["account_conflict"] is True and entries[0]["agents"] == [_AGENT]


def test_subagents_key_with_the_real_inventory_and_an_own_store() -> None:
    """No injected store: the reader runs before ``collect_sessions`` closes its own."""
    from command_center import session_agents

    entries = bridge_cli.collect_sessions(
        adapter=FakeAdapter([_live("a1", 11)], {"a1": _B2}),  # type: ignore[arg-type]
        cfg=config.Config(session_names=True),
        panes_reader=lambda: [],
        ps_reader=lambda: {},
        account_label=lambda cd: cd,
        agents_reader=lambda: [],
        subagents_reader=session_agents.inventory,
    )
    assert [a["id"] for a in entries[0]["agents"]] == ["ag-run", "ag-nometa", "ag-nots"]
    reopened = Store()
    try:
        assert str(_B2) in reopened.get_subagent_scans([str(_B2)])
    finally:
        reopened.close()


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
