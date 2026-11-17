"""``ccc sent`` / ``ccc input-ledger`` and the daemon's matcher step (tp#2170 B4).

The listing is read-only (``mode=ro``; the stored row stays ``pending`` while it is
matched in memory), the envelope is schema_version 1 with every D7 field, each
``incomplete_reasons`` value has its fixture, and arming refuses while an older ccc process
lives (stubbed ``ps_table`` / ``pid_start`` / code stamp).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from bridgestub import SID, World, run_cli, turn_end, user

from command_center import bridge_cli, bridge_inputs, cli, input_ledger
from command_center.bridge_target import BridgeDeps
from command_center.daemon import DaemonReport, _advance_inputs
from command_center.snapshot import PsRow
from command_center.store import Store

TEXT = "please run the release checklist now"
HOUR_MS = 3600 * 1000
OTHER = "00000000-0000-4000-8000-00000000b0ff"


def _world(tmp_path: Path) -> World:
    world = World(tmp_path)
    world.write(user("an older prompt"), *turn_end())
    return world


def _record(world: World, at: int, text: str = TEXT, **kw: Any) -> str:
    args: dict[str, Any] = {
        "session_id": SID,
        "kind": "await_paste",
        "origin": "automation",
        "text": text,
        "config_dir": str(world.config_dir),
        "cwd": "/repo",
        "transcript": world.transcript,
        "paste": False,
        "now_ms": at,
        "gaps": world.tmp / "input-ledger-gaps.jsonl",
    }
    args.update(kw)
    input_id = input_ledger.record(lambda: Store(world.db), **args)
    assert input_id is not None
    return input_id


def _arm(world: World, at: int, coverage: int = input_ledger.COVERAGE) -> None:
    with Store(world.db) as store:
        store.meta_set(input_ledger.META_COMPLETE_SINCE, str(at), at)
        store.meta_set(input_ledger.META_COVERAGE, str(coverage), at)


def _sent(
    world: World,
    mp: pytest.MonkeyPatch,
    cap: pytest.CaptureFixture[str],
    *extra: str,
) -> tuple[int, dict[str, Any] | None, str]:
    return run_cli(mp, cap, world, ["sent", "-s", SID, *extra, "-j"])


def _stored(world: World, input_id: str) -> str:
    with Store(world.db) as store:
        row = store.get_input(input_id)
    assert row is not None
    return row.state


# --------------------------------------------------------------------------- the envelope


def test_envelope_golden_every_d7_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    t0 = world.clock_ms - HOUR_MS
    _arm(world, t0 - HOUR_MS)
    input_id = _record(world, t0)
    anchor = world.transcript.stat().st_size
    line = user(TEXT)
    world.write(line, *turn_end())
    with Store(world.db) as store:
        assert input_ledger.advance_session(store, SID, t0 + 1000) == [(input_id, "matched")]
    since = t0 - 60_000
    code, env, _err = _sent(world, monkeypatch, capsys, "-a", str(since))
    assert code == 0 and env is not None
    assert env == {
        "schema_version": 1,
        "ok": True,
        "error": None,
        "data": {
            "session_id": SID,
            "since": bridge_inputs.iso_ms(since),
            "since_ms": since,
            "until": bridge_inputs.iso_ms(world.clock_ms),
            "until_ms": world.clock_ms,
            "as_of": bridge_inputs.iso_ms(world.clock_ms),
            "normalisation": "ccc-text-sha-v1",
            "coverage": 1,
            "complete_since": bridge_inputs.iso_ms(t0 - HOUR_MS),
            "complete_since_ms": t0 - HOUR_MS,
            "complete": True,
            "incomplete_reasons": [],
            "inputs": [
                {
                    "input_id": input_id,
                    "kind": "await_paste",
                    "origin": "automation",
                    "created_at": bridge_inputs.iso_ms(t0),
                    "created_at_ms": t0,
                    "norm": "ccc-text-sha-v1",
                    "content_sha": hashlib.sha256(TEXT.encode()).hexdigest(),
                    "alt_sha": None,
                    "state": "matched",
                    "stored_state": "matched",
                    "matched_in_memory": False,
                    "transcript_path": str(world.transcript),
                    "transcript_inode": world.transcript.stat().st_ino,
                    "anchor_offset": anchor,
                    "line_offset": anchor,
                    "line_uuid": line["uuid"],
                    "line_kind": "user",
                    "stray_len": 0,
                    "delivery_id": None,
                    "reason": None,
                }
            ],
        },
    }
    assert TEXT not in capsys.readouterr().out


def test_envelope_null_forms_when_not_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    code, env, _err = _sent(world, monkeypatch, capsys)
    assert code == 0 and env is not None
    data = env["data"]
    assert data["since"] == "1970-01-01T00:00:00Z" and data["since_ms"] == 0
    assert data["coverage"] is None
    assert data["complete_since"] is None and data["complete_since_ms"] is None
    assert data["complete"] is False and data["incomplete_reasons"] == ["not_armed"]
    assert data["inputs"] == []


def test_store_without_ledger_tables_reads_not_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
    world.db = old
    before = old.read_bytes()
    code, env, _err = _sent(world, monkeypatch, capsys)
    assert code == 0 and env is not None
    assert env["data"]["incomplete_reasons"] == ["not_armed"]
    assert old.read_bytes() == before
    assert {
        r[0]
        for r in sqlite3.connect(old).execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    } == {"sessions"}


# --------------------------------------------------------------------------- incomplete_reasons


def _reasons(
    world: World, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str], since: int
) -> list[str]:
    code, env, _err = _sent(world, mp, cap, "-a", str(since))
    assert code == 0 and env is not None, env
    return list(env["data"]["incomplete_reasons"])


def test_reason_coverage_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - 3 * HOUR_MS, coverage=input_ledger.COVERAGE - 1)
    assert _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS) == ["coverage_stale"]


def test_reason_before_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - HOUR_MS)
    since = world.clock_ms - 2 * HOUR_MS
    assert _reasons(world, monkeypatch, capsys, since) == ["before_watermark"]
    assert not _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS)


def test_reason_pending_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - 3 * HOUR_MS)
    _record(world, world.clock_ms - 1000)  # typed, its line not written yet
    assert _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS) == ["pending_match"]


def test_reason_pending_row_written_just_before_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A1: a row written just before ``since`` can match a line inside the window."""
    world = _world(tmp_path)
    since = world.clock_ms - HOUR_MS
    _arm(world, since - 3 * HOUR_MS)
    input_id = _record(world, since - 5000)
    code, env, _err = _sent(world, monkeypatch, capsys, "-a", str(since))
    assert code == 0 and env is not None
    assert env["data"]["incomplete_reasons"] == ["pending_match"]
    assert [i["input_id"] for i in env["data"]["inputs"]] == [input_id]


def test_reason_unbound_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - 3 * HOUR_MS)
    _record(world, world.clock_ms - 1000, session_id="", transcript=None, kind="limit_resume")
    assert _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS) == ["unbound_rows"]


def test_reason_ledger_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - 3 * HOUR_MS)
    gaps = world.tmp / "input-ledger-gaps.jsonl"
    input_ledger.append_gap(
        OTHER, "park_now", "OperationalError", at_ms=world.clock_ms - 10, path=gaps
    )
    assert not _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS)
    input_ledger.append_gap(
        SID, "park_now", "OperationalError", at_ms=world.clock_ms - 10, path=gaps
    )
    assert _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS) == ["ledger_write_failed"]


def test_reason_not_armed_with_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _record(world, world.clock_ms - 1000)
    assert _reasons(world, monkeypatch, capsys, world.clock_ms - HOUR_MS) == [
        "not_armed",
        "pending_match",
    ]


# --------------------------------------------------------------------------- read-only matching


def test_pending_row_matched_in_memory_stored_row_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - 3 * HOUR_MS)
    input_id = _record(world, world.clock_ms - 2000)
    line = user(TEXT)
    world.write(line, *turn_end())
    before = world.db.read_bytes()
    code, env, _err = _sent(world, monkeypatch, capsys, "-a", str(world.clock_ms - HOUR_MS))
    assert code == 0 and env is not None
    data = env["data"]
    assert data["complete"] is True
    (item,) = data["inputs"]
    assert item["state"] == "matched" and item["stored_state"] == "pending"
    assert item["matched_in_memory"] is True and item["line_uuid"] == line["uuid"]
    assert _stored(world, input_id) == "pending"
    assert world.db.read_bytes() == before


def test_run_sent_connection_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path)
    _record(world, world.clock_ms - 2000)
    world.write(user(TEXT))
    changes: list[int] = []
    real = bridge_inputs.connect_ro

    class Spy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def execute(self, *args: Any) -> sqlite3.Cursor:
            return self.conn.execute(*args)

        def close(self) -> None:
            changes.append(self.conn.total_changes)
            self.conn.close()

    monkeypatch.setattr(bridge_inputs, "connect_ro", lambda path: Spy(real(path)))
    bridge_inputs.run_sent(world.deps(), SID, 0, world.clock_ms)
    assert changes == [0]


def test_expired_pending_row_reads_unmatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _arm(world, world.clock_ms - 20 * HOUR_MS)
    input_id = _record(world, world.clock_ms - 7 * HOUR_MS)
    code, env, _err = _sent(world, monkeypatch, capsys, "-a", str(world.clock_ms - 8 * HOUR_MS))
    assert code == 0 and env is not None
    (item,) = env["data"]["inputs"]
    assert item["state"] == "unmatched" and item["reason"] == "match_window"
    assert env["data"]["complete"] is True
    assert _stored(world, input_id) == "pending"


# --------------------------------------------------------------------------- argument checks


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("2027-01-15T07:00:00Z", 1_799_996_400_000),
        ("2027-01-15T08:00:00+01:00", 1_799_996_400_000),
        ("2027-01-15T07:00:00.250Z", 1_799_996_400_250),
        ("1799996400000", 1_799_996_400_000),
    ],
)
def test_after_parses_iso_with_z_offset_and_epoch_ms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    after: str,
    expected: int,
) -> None:
    world = _world(tmp_path)
    code, env, _err = _sent(world, monkeypatch, capsys, "-a", after, "-u", "1800000000000")
    assert code == 0 and env is not None
    assert env["data"]["since_ms"] == expected
    assert env["data"]["until_ms"] == 1_800_000_000_000


@pytest.mark.parametrize(
    ("argv", "code_name"),
    [
        (["-a", "yesterday"], "invalid_time"),
        (["-a", "2027-01-15T08:00:00"], "invalid_time"),
        (["-u", "12:00"], "invalid_time"),
        (["-a", "1799913600000", "-u", "1799913600000"], "invalid_window"),
    ],
)
def test_bad_times_are_usage_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    code_name: str,
) -> None:
    world = _world(tmp_path)
    code, env, _err = _sent(world, monkeypatch, capsys, *argv)
    assert code == 2 and env is not None
    assert env["ok"] is False and env["error"]["code"] == code_name


@pytest.mark.parametrize("sid", ["00000000", SID[:-1], SID + "0", "not-a-session"])
def test_session_must_be_a_full_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sid: str
) -> None:
    world = _world(tmp_path)
    code, env, _err = run_cli(monkeypatch, capsys, world, ["sent", "-s", sid, "-j"])
    assert code == 2 and env is not None and env["error"]["code"] == "invalid_session"


def test_unknown_argument_is_an_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    code, env, _err = _sent(world, monkeypatch, capsys, "-x")
    assert code == 2 and env is not None and env["ok"] is False


def test_text_form_has_header_and_one_line_per_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _record(world, world.clock_ms - 2000)
    monkeypatch.setattr("command_center.bridge_target.default_deps", world.deps)
    assert cli.main(["sent", "-s", SID]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "watermark not armed" in out[0] and "not_armed" in out[0]
    assert len(out) == 2 and "await_paste" in out[1] and "pending" in out[1]
    assert hashlib.sha256(TEXT.encode()).hexdigest()[:12] in out[1]
    assert TEXT not in "\n".join(out)


# --------------------------------------------------------------------------- input-ledger


def _ledger(
    world: World, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, Any] | None, str]:
    return run_cli(mp, cap, world, ["input-ledger", *argv, "-j"])


def _lstart(epoch_s: float) -> str:
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(epoch_s))


def _stub_processes(
    monkeypatch: pytest.MonkeyPatch, world: World, table: dict[int, Any], starts: dict[int, float]
) -> None:
    stamp_s = 1_800_000_000

    def deps() -> BridgeDeps:
        return dataclasses.replace(
            World.deps(world),
            ps_table=lambda: table,
            pid_start=lambda pid: _lstart(starts[pid]) if pid in starts else "",
            code_stamp=lambda: stamp_s * 1_000_000_000,
        )

    monkeypatch.setattr(world, "deps", deps)


def test_arm_refuses_with_an_older_ccc_process_force_overrides_disarm_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    stamp_s = 1_800_000_000
    table = {
        1: PsRow(0, "", "S", "launchd"),
        501: PsRow(1, "ttys001", "S", "/usr/bin/python3 /opt/tool/bin/ccc park -g"),
        502: PsRow(1, "", "S", "/opt/tool/bin/ccc tab-symbol --watch"),
        503: PsRow(1, "", "S", "/opt/tool/bin/ccc daemon"),
        504: PsRow(1, "ttys002", "S", "claude --resume x"),
    }
    starts: dict[int, float] = {
        501: stamp_s - 600,
        502: stamp_s - 600,
        503: stamp_s + 5,
        504: stamp_s - 600,
    }
    _stub_processes(monkeypatch, world, table, starts)

    code, env, _err = _ledger(world, monkeypatch, capsys, "-A")
    assert code == 1 and env is not None
    assert env["error"]["code"] == "stale_processes" and env["data"] == {"stale_pids": [501]}
    assert "park" not in env["error"]["message"]
    code, env, _err = _ledger(world, monkeypatch, capsys, "-S")
    assert code == 0 and env is not None and env["data"]["armed"] is False

    code, env, _err = _ledger(world, monkeypatch, capsys, "-A", "-F")
    assert code == 0 and env is not None
    assert env["data"]["armed"] is True and env["data"]["forced"] is True
    assert env["data"]["complete_since_ms"] == world.clock_ms
    code, env, _err = _ledger(world, monkeypatch, capsys)
    assert code == 0 and env is not None
    assert env["data"]["armed"] is True and env["data"]["coverage"] == input_ledger.COVERAGE
    assert env["data"]["coverage_stale"] is False

    code, env, _err = _ledger(world, monkeypatch, capsys, "-D")
    assert code == 0 and env is not None and env["data"] == {"armed": False, "removed": True}
    code, env, _err = _ledger(world, monkeypatch, capsys, "-S")
    assert code == 0 and env is not None and env["data"]["complete_since"] is None


def test_arm_succeeds_when_every_ccc_process_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    table = {601: PsRow(1, "", "S", "/opt/tool/bin/claude-session-continue x now")}
    _stub_processes(monkeypatch, world, table, {601: 1_800_000_100})
    code, env, _err = _ledger(world, monkeypatch, capsys, "-A")
    assert code == 0 and env is not None
    assert env["data"]["forced"] is False and env["data"]["stale_pids"] == []
    with Store(world.db) as store:
        assert store.meta_get(input_ledger.META_COMPLETE_SINCE) == str(world.clock_ms)


def test_arm_counts_an_unreadable_start_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    table = {701: PsRow(1, "", "S", "python3 -m command_center.cli panel-server")}
    _stub_processes(monkeypatch, world, table, {})
    code, env, _err = _ledger(world, monkeypatch, capsys, "-A")
    assert code == 1 and env is not None and env["data"] == {"stale_pids": [701]}


def test_force_without_arm_is_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    code, env, _err = _ledger(world, monkeypatch, capsys, "-F")
    assert code == 2 and env is not None and env["ok"] is False


def test_status_counts_rows_and_gaps_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _record(world, world.clock_ms - 2000)
    failed = _record(world, world.clock_ms - 1000)
    input_ledger.mark_failed(lambda: Store(world.db), failed, "not_typed")
    input_ledger.append_gap(
        SID, "park_now", "OSError", at_ms=5, path=world.tmp / "input-ledger-gaps.jsonl"
    )
    before = world.db.read_bytes()
    code, env, _err = _ledger(world, monkeypatch, capsys, "-S")
    assert code == 0 and env is not None
    data = env["data"]
    assert data["counts"] == {"pending": 1, "matched": 0, "unmatched": 0, "failed": 1}
    assert data["gaps"] == 1 and data["coverage_build"] == input_ledger.COVERAGE
    assert world.db.read_bytes() == before


def test_allowlist_names_its_reasons() -> None:
    for _name, flags, reason in bridge_inputs.ARM_ALLOWLIST:
        assert flags and reason
    assert bridge_inputs.allowlisted(["tab-symbol", "--watch"]) is not None
    assert bridge_inputs.allowlisted(["tab-symbol", "-H"]) is None
    assert bridge_inputs.allowlisted(["resume-halted", "--watch"]) is not None
    assert bridge_inputs.allowlisted(["park", "-g"]) is None


# --------------------------------------------------------------------------- parsers


@pytest.mark.parametrize("command", ["sent", "input-ledger"])
def test_help_exits_zero(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "-h"])
    assert exc.value.code == 0
    assert command in capsys.readouterr().out


def _subparser(name: str) -> argparse.ArgumentParser:
    parser = cli.build_parser()
    for action in parser._actions:  # pylint: disable=protected-access
        if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access
            return action.choices[name]
    raise AssertionError(name)


@pytest.mark.parametrize("command", ["sent", "input-ledger"])
def test_every_option_has_a_unique_short_form(command: str) -> None:
    shorts: list[str] = []
    for action in _subparser(command)._actions:  # pylint: disable=protected-access
        if not action.option_strings:
            continue
        short = [o for o in action.option_strings if len(o) == 2 and o[1] != "-"]
        assert len(short) == 1, action.option_strings
        shorts.extend(short)
    assert len(shorts) == len(set(shorts))


def test_both_commands_answer_argv_errors_with_an_envelope() -> None:
    assert {"sent", "input-ledger"} <= bridge_cli.JSON_COMMANDS


# --------------------------------------------------------------------------- the daemon step


def test_daemon_step_matches_and_dry_run_only_reports(tmp_path: Path) -> None:
    world = _world(tmp_path)
    input_id = _record(world, world.clock_ms - 2000)
    world.write(user(TEXT))
    expired = _record(world, world.clock_ms - 7 * HOUR_MS, text="an unrelated text")
    with Store(world.db) as store:
        dry = DaemonReport()
        _advance_inputs(store, dry, True, at_ms=world.clock_ms)
        assert dry.inputs_matched == [input_id[:8]] and dry.inputs_unmatched == [expired[:8]]
        assert dry.is_empty()
        assert store.get_input(input_id).state == "pending"  # type: ignore[union-attr]
        report = DaemonReport()
        _advance_inputs(store, report, False, at_ms=world.clock_ms)
        assert report.inputs_matched == [input_id[:8]]
        assert report.inputs_unmatched == [expired[:8]]
        assert store.get_input(input_id).state == "matched"  # type: ignore[union-attr]
        assert store.pending_input_sessions() == []


def test_daemon_step_isolates_a_failing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    _record(world, world.clock_ms - 2000)
    _record(world, world.clock_ms - 2000, session_id=OTHER, transcript=None)
    real = input_ledger.advance_session

    def flaky(store: Store, session_id: str, now_ms: int) -> list[tuple[str, str]]:
        if session_id == SID:
            raise OSError("boom")
        return real(store, session_id, now_ms)

    monkeypatch.setattr(input_ledger, "advance_session", flaky)
    with Store(world.db) as store:
        report = DaemonReport()
        _advance_inputs(store, report, False, at_ms=world.clock_ms + 7 * HOUR_MS)
    assert len(report.inputs_unmatched) == 1
    assert "input ledger" in capsys.readouterr().err
