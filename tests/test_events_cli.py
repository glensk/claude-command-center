"""``ccc events --after CURSOR -j`` and the StopFailure hook's event rows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bridgestub import SID, World, ask, question, run_cli, user

from command_center import config, hooks, spawn
from command_center.store import Store

ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _cfg_on() -> config.Config:
    return config.Config(auto_switch_on_limit=True)


def test_every_stop_failure_kind_is_persisted_and_rate_limit_still_fails_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[list[str]] = []

    def _spawn(args: list[str], **_kw: object) -> bool:
        spawned.append(args)
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", _spawn)
    monkeypatch.setattr(config, "load_config", _cfg_on)
    for error in ("overloaded", "authentication_failed", "", "rate_limit"):
        hooks.handle_stop_failure({"session_id": SID, "cwd": "/r", "error": error})
    hooks.handle_stop_failure({"cwd": "/r", "error": "overloaded"})  # no session id → nothing
    with Store() as store:
        events = store.events_after(0)
    assert [e.kind for e in events] == ["stop_failure"] * 4
    assert [e.detail_obj()["error"] for e in events] == [
        "overloaded",
        "authentication_failed",
        "unknown",
        "rate_limit",
    ]
    assert all(e.session_id == SID for e in events)
    assert len(spawned) == 1 and spawned[0][:3] == ["switch-on-limit", "--session", SID]


def test_a_broken_store_never_breaks_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    import command_center.store as store_mod

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(store_mod, "Store", _boom)
    assert hooks.handle_stop_failure({"session_id": SID, "error": "overloaded"}) == 0


def test_events_after_a_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    code, env, _err = run_cli(monkeypatch, capsys, world, ["events", "-j"])
    assert code == 0 and env == {
        "schema_version": 1,
        "ok": True,
        "data": {"events": [], "next_cursor": 0},
        "error": None,
    }
    with Store(world.db) as store:
        c1 = store.add_event("stop_failure", SID, world.clock_ms, {"error": "overloaded"})
        c2 = store.add_event("stop_failure", "other", world.clock_ms + 1, {"error": "x"})
    assert c1 is not None and c2 is not None and c2 > c1
    code, env, _err = run_cli(monkeypatch, capsys, world, ["events", "-a", "0", "-j"])
    assert code == 0 and env is not None
    events = env["data"]["events"]
    assert [e["cursor"] for e in events] == [c1, c2]
    assert env["data"]["next_cursor"] == c2
    assert events[0] == {
        "cursor": c1,
        "kind": "stop_failure",
        "session_id": SID,
        "at": events[0]["at"],
        "detail": {"error": "overloaded"},
    }
    assert ISO.fullmatch(events[0]["at"])
    code, env, _err = run_cli(monkeypatch, capsys, world, ["events", "--after", str(c1), "-j"])
    assert env is not None and [e["cursor"] for e in env["data"]["events"]] == [c2]
    code, env, _err = run_cli(monkeypatch, capsys, world, ["events", "-a", str(c2), "-j"])
    assert env is not None and env["data"] == {"events": [], "next_cursor": c2}


def test_events_runs_the_transcript_scanner_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    run_cli(monkeypatch, capsys, world, ["send", "-s", SID, "-j"], stdin="go")
    world.write(ask("toolu_1", [question("Which?", ["a", "b"])]))
    code, env, _err = run_cli(monkeypatch, capsys, world, ["events", "-j"])
    assert code == 0 and env is not None
    (event,) = env["data"]["events"]
    assert event["kind"] == "needs_input" and event["session_id"] == SID
    assert event["detail"]["tool_use_id"] == "toolu_1"
    code, env, _err = run_cli(
        monkeypatch, capsys, world, ["events", "-a", str(event["cursor"]), "-j"]
    )
    assert env is not None and env["data"]["events"] == []  # reported once


def test_dedupe_key_records_an_event_once(tmp_path: Path) -> None:
    with Store(tmp_path / "s.db") as store:
        assert store.add_event("needs_input", SID, 1, {}, "k1") is not None
        assert store.add_event("needs_input", SID, 2, {}, "k1") is None
        assert len(store.events_after(0)) == 1


def test_negative_cursor_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    code, env, _err = run_cli(monkeypatch, capsys, world, ["events", "-a", "-5", "-j"])
    assert code == 2 and env is not None and env["error"]["code"] == "invalid_cursor"
