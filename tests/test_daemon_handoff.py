"""The daemon's continue-session handoff sweep (tp#1582 B6), driven unwired.

Every closer spawn is stubbed (``spawn.spawn_ccc``) and ``os.kill`` refuses: the sweep
itself never signals anything; state lives in the temp ``CCC_HOME`` of ``conftest``.
"""

from __future__ import annotations

from typing import Any

import pytest

from command_center import cli, continue_session, daemon, handoff_sweep, spawn
from command_center.daemon import DaemonReport, _run_handoff_finalize
from command_center.models import LiveSession, now_ms
from command_center.store import Store
from command_center.transfer_store import ACCEPTED, OPENED, Transfer

SRC = "aaaaaaaa-1111-2222-3333-444444444444"
DST = "bbbbbbbb-1111-2222-3333-444444444444"
SRC2 = "cccccccc-1111-2222-3333-444444444444"
DST2 = "dddddddd-1111-2222-3333-444444444444"
WINDOW = handoff_sweep.PACING_MS


class _Spawns:
    """The stubbed ``spawn_ccc``: records each closer, returns ``ok``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.ok = True

    def __call__(self, args: list[str], *, internal: bool = True) -> bool:
        del internal
        self.calls.append(args)
        return self.ok


@pytest.fixture(name="spawns")
def _spawns(monkeypatch: pytest.MonkeyPatch) -> _Spawns:
    stub = _Spawns()
    monkeypatch.setattr(spawn, "spawn_ccc", stub)

    def no_kill(*_a: object) -> None:
        raise AssertionError("the handoff sweep must never signal a process")

    monkeypatch.setattr(daemon.os, "kill", no_kill)
    return stub


def _closer(tid: str, sid: str = SRC) -> list[str]:
    return ["close-now", "--handoff-transfer", tid, "--session", sid]


def _live(sid: str = SRC, status: str = "idle") -> dict[str, LiveSession]:
    return {sid: LiveSession(4242, sid, "/repo", raw_status=status, alive=True)}


def _transfer(state: str, *, src: str = SRC, dst: str = DST, capped: bool = False) -> str:
    """A transfer of *src* → *dst* driven to *state* (``accepted`` or ``finalized``)."""
    tid = "t" + src[:6] + dst[:6]
    with Store() as store:
        store.ensure(src, "/repo")
        store.ensure(dst, "/repo")
        store.create_transfer(
            Transfer(transfer_id=tid, source_session=src, target_session=dst, capped=capped), 1
        )
        assert store.transition_transfer(tid, OPENED, 2)
        assert store.transition_transfer(tid, ACCEPTED, 3)
        if state == "finalized":
            transfer = store.get_transfer(tid)
            assert continue_session.finalize(store, transfer, arm_close=not capped)
    return tid


def _state(tid: str) -> str:
    with Store() as store:
        transfer = store.get_transfer(tid)
    assert transfer is not None
    return transfer.state


def _row(sid: str = SRC) -> Any:
    with Store() as store:
        row = store.get(sid)
    assert row is not None
    return row


def _sweep(
    snapshot: dict[str, LiveSession] | None = None, *, dry_run: bool = False
) -> DaemonReport:
    report = DaemonReport()
    with Store() as store:
        _run_handoff_finalize(store, _live() if snapshot is None else snapshot, report, dry_run)
    return report


def _set_row(sid: str, **fields: Any) -> None:
    sets = ", ".join(f"{name} = ?" for name in fields)
    with Store() as store:
        store.conn.execute(
            f"UPDATE sessions SET {sets} WHERE session_id = ?",  # noqa: S608 - test names
            (*fields.values(), sid),
        )
        store.conn.commit()


# ------------------------------------------------------------------ the query


def test_the_candidate_query_is_index_driven() -> None:
    with Store() as store:
        seen: list[str] = []
        real = store.conn

        class Spy:  # pylint: disable=too-few-public-methods
            def execute(self, sql: str, *args: Any) -> Any:
                seen.append(sql)
                return real.execute(sql, *args)

        store.conn = Spy()  # type: ignore[assignment]
        try:
            store.handoff_finalize_candidates()
        finally:
            store.conn = real
        plan = [str(row[3]) for row in real.execute("EXPLAIN QUERY PLAN " + seen[0])]
    scans = [step for step in plan if step.startswith("SCAN")]
    assert scans and all("USING INDEX" in step for step in scans), plan
    assert any("idx_transfers_active_source" in step for step in plan)
    assert any("idx_sessions_close_token" in step for step in plan)


def test_the_query_selects_only_open_handoff_work() -> None:
    accepted = _transfer("accepted")
    finalized = _transfer("finalized", src=SRC2, dst=DST2)
    capped = _transfer("finalized", src="eeeeeeee-1", dst="ffffffff-1", capped=True)
    done = _transfer("finalized", src="99999999-1", dst="88888888-1")
    with Store() as store:
        token = store.get("99999999-1").close_token  # type: ignore[union-attr]
        assert store.retire_close("99999999-1", token)  # its close completed
        got = {c.transfer_id: c for c in store.handoff_finalize_candidates()}
    assert set(got) == {accepted, finalized}
    assert capped not in got and done not in got
    assert got[accepted].token_state == "none" and got[accepted].state == ACCEPTED
    assert got[finalized].token_state == "pending" and not got[finalized].debt


def test_a_finalized_row_handed_to_another_successor_is_not_a_candidate() -> None:
    tid = _transfer("finalized")
    _set_row(SRC, handed_off_to=DST2)  # a later handoff re-pointed the lineage
    with Store() as store:
        assert tid not in {c.transfer_id for c in store.handoff_finalize_candidates()}


# ------------------------------------------------------------------ accepted (stranded)


def test_a_crash_after_the_accepted_cas_is_finalized_and_closed(spawns: _Spawns) -> None:
    tid = _transfer("accepted")
    report = _sweep()
    assert _state(tid) == "finalized"
    row = _row()
    assert row.handed_off_to == DST and row.close_token and row.close_requested_at > 0
    assert spawns.calls == [_closer(tid)]
    assert (report.handoff_finalized, report.handoff_closers) == ([tid], [SRC])
    assert not report.handoff_errors and not report.is_empty()
    assert not _sweep().handoff_closers  # paced: the next pass spawns no second closer
    assert spawns.calls == [_closer(tid)]


def test_a_stranded_accept_with_the_source_gone_is_finalized_only(spawns: _Spawns) -> None:
    tid = _transfer("accepted")
    report = _sweep({})
    assert _state(tid) == "finalized" and report.handoff_finalized == [tid]
    assert not spawns.calls and not report.handoff_closers
    assert not _sweep({}).handoff_closers  # a gone source: never a closer, pass after pass


def test_a_capped_transfer_is_finalized_but_never_closed(spawns: _Spawns) -> None:
    tid = _transfer("accepted", capped=True)
    report = _sweep()
    assert _state(tid) == "finalized" and report.handoff_finalized == [tid]
    row = _row()
    assert row.handed_off_to == DST and not row.close_token and not row.close_requested_at
    assert not spawns.calls
    assert _sweep().is_empty()  # finalized + capped: no candidate any more


def test_a_simultaneous_start_finalizing_first_spawns_nothing(
    spawns: _Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query saw ``accepted``; meanwhile the source's own start finalized and its Stop
    chain claimed the close token. The sweep converges on that state: no second closer."""
    tid = _transfer("accepted")
    with Store() as store:
        stale = store.handoff_finalize_candidates()
        assert continue_session.finalize(store, store.get_transfer(tid))  # start's finalize
        assert store.claim_close(SRC, store.get(SRC).close_token)  # type: ignore[union-attr]
    monkeypatch.setattr(Store, "handoff_finalize_candidates", lambda _self: stale)
    report = _sweep()
    assert _state(tid) == "finalized" and not spawns.calls
    assert not report.handoff_closers and not report.handoff_errors


# ------------------------------------------------------------------ finalized with close debt


def test_finalized_before_the_spawn_gets_its_closer_paced(
    spawns: _Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _transfer("finalized")
    clock = [now_ms()]
    monkeypatch.setattr(handoff_sweep, "now_ms", lambda: clock[0])
    token = _row().close_token
    report = _sweep()
    assert spawns.calls == [_closer(tid)] and report.handoff_closers == [SRC]
    assert not report.handoff_finalized  # it was finalized already
    assert _row().close_token == token  # the existing token, never a new one
    clock[0] += WINDOW - 1
    assert not _sweep().handoff_closers  # one spawn per source per window
    clock[0] += 2
    assert _sweep().handoff_closers == [SRC]
    assert spawns.calls == [_closer(tid)] * 2


def test_an_idle_source_is_closed_and_a_busy_one_is_left_alone(spawns: _Spawns) -> None:
    tid = _transfer("finalized")
    assert not _sweep(_live(status="busy")).handoff_closers
    assert not spawns.calls  # busy: the closer would refuse; no spawn, no pacing stamp
    assert _sweep(_live(status="idle")).handoff_closers == [SRC]
    assert spawns.calls == [_closer(tid)]


def test_an_expired_arm_is_still_closed(spawns: _Spawns) -> None:
    """The arm is older than the Stop hook's TTL (an idle source has no next Stop); the
    token-scoped closer has no TTL, so the sweep still spawns it."""
    tid = _transfer("finalized")
    _set_row(SRC, close_requested_at=now_ms() - 60 * 60 * 1000)
    assert _sweep().handoff_closers == [SRC]
    assert spawns.calls == [_closer(tid)]


def test_a_gone_source_without_debt_is_never_closed(spawns: _Spawns) -> None:
    _transfer("finalized")
    assert _sweep({}).is_empty()
    assert _sweep({SRC: LiveSession(1, SRC, "/repo", alive=False)}).is_empty()
    assert not spawns.calls


def test_a_kept_debt_is_retried_regardless_of_liveness(spawns: _Spawns) -> None:
    """Post-SIGTERM debt (``handoff:`` binding): the process is gone, the pane is not —
    the closer's retry_terminal closes it without signalling again."""
    tid = _transfer("finalized")
    _set_row(SRC, close_bound='handoff:{"pid": 4242, "start": "s", "tty": "ttys001"}')
    report = _sweep({})
    assert report.handoff_closers == [SRC] and spawns.calls == [_closer(tid)]


def test_a_token_in_flight_spawns_nothing(spawns: _Spawns) -> None:
    _transfer("finalized")
    with Store() as store:
        assert store.claim_close(SRC, store.get(SRC).close_token)  # type: ignore[union-attr]
    _set_row(SRC, close_bound='handoff:{"pid": 1, "start": "s"}')
    assert _sweep().is_empty() and _sweep({}).is_empty()
    assert not spawns.calls


def test_a_completed_close_is_never_reopened(spawns: _Spawns) -> None:
    """The closer retired its token; the user resumed the source tab: it is live and idle,
    yet the sweep never selects it again (no age-based close)."""
    _transfer("finalized")
    with Store() as store:
        assert store.retire_close(SRC, store.get(SRC).close_token)  # type: ignore[union-attr]
    assert _sweep().is_empty()
    assert not spawns.calls and not _row().close_token


def test_a_token_retired_after_the_query_is_not_re_armed(
    spawns: _Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transfer("finalized")
    with Store() as store:
        stale = store.handoff_finalize_candidates()
        assert store.retire_close(SRC, store.get(SRC).close_token)  # type: ignore[union-attr]
    monkeypatch.setattr(Store, "handoff_finalize_candidates", lambda _self: stale)
    assert _sweep().is_empty()
    assert not spawns.calls and not _row().close_token  # no fresh token was minted


def test_a_spawn_failure_is_reported_and_retried_next_pass(spawns: _Spawns) -> None:
    tid = _transfer("finalized")
    spawns.ok = False
    report = _sweep()
    assert report.handoff_errors == [f"{tid}: closer spawn failed"]
    assert not report.handoff_closers
    spawns.ok = True
    assert _sweep().handoff_closers == [SRC]  # no pacing stamp for a failed spawn
    assert spawns.calls == [_closer(tid)] * 2


def test_a_stranded_accept_whose_closer_spawn_fails_is_reported(spawns: _Spawns) -> None:
    tid = _transfer("accepted")
    spawns.ok = False
    report = _sweep()
    assert report.handoff_finalized == [tid]
    assert report.handoff_errors == [f"{tid}: closer spawn failed"]
    spawns.ok = True
    assert _sweep().handoff_closers == [SRC]  # now finalized with a pending token


# ------------------------------------------------------------------ isolation + dry run


def test_one_failing_candidate_never_stops_the_others(
    spawns: _Spawns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = _transfer("accepted")
    ok = _transfer("finalized", src=SRC2, dst=DST2)

    def boom(*_a: object, **_k: object) -> dict[str, Any]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(continue_session, "finalize_handoff_source", boom)
    report = _sweep({**_live(), **_live(SRC2)})
    assert report.handoff_errors == [f"{broken}: RuntimeError: database is locked"]
    assert report.handoff_closers == [SRC2] and spawns.calls == [_closer(ok, SRC2)]
    assert f"handoff {broken} failed" in capsys.readouterr().err
    assert _state(broken) == "accepted"


def test_a_failing_query_never_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_self: Store) -> list[Any]:
        raise RuntimeError("no such table")

    monkeypatch.setattr(Store, "handoff_finalize_candidates", boom)
    report = _sweep()
    assert report.handoff_errors == ["query: RuntimeError: no such table"]
    assert "handoff sweep failed" in capsys.readouterr().err


def test_dry_run_reports_and_touches_nothing(spawns: _Spawns) -> None:
    accepted = _transfer("accepted")
    finalized = _transfer("finalized", src=SRC2, dst=DST2)
    before = _row(SRC2)
    report = _sweep({**_live(), **_live(SRC2)}, dry_run=True)
    assert report.handoff_finalized == [accepted]
    assert sorted(report.handoff_closers) == sorted([SRC, SRC2])
    assert not spawns.calls
    assert _state(accepted) == "accepted" and _state(finalized) == "finalized"
    assert not _row().handed_off_to and not _row().close_token
    after = _row(SRC2)
    assert (after.close_token, after.close_requested_at) == (
        before.close_token,
        before.close_requested_at,
    )
    assert not handoff_sweep.pacing_path().exists()


def test_daemon_cli_prints_the_handoff_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = DaemonReport(
        handoff_finalized=["t1"], handoff_closers=["s1", "s2"], handoff_errors=["t9: boom"]
    )
    monkeypatch.setattr(daemon, "run_once", lambda **_k: report)
    assert cli.main(["daemon", "-v"]) == 0
    out = capsys.readouterr()
    assert "handoff_finalized=1 handoff_closers=2" in out.out
    assert "  handoff-finalize: t1" in out.out and "  handoff-closer: s2" in out.out
    assert "handoff sweep: t9: boom" in out.err


def test_the_daemon_entry_point_never_raises_into_the_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(handoff_sweep, "run", boom)
    report = _sweep()
    assert report.handoff_errors == ["sweep: OSError: disk full"]
    assert "handoff sweep failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("do_reap", "dry_run"), [(True, False), (False, False), (True, True), (False, True)]
)
def test_run_once_runs_the_handoff_sweep_once_per_pass(
    monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, do_reap: bool, dry_run: bool
) -> None:
    """B7: every pass sweeps once, whatever ``do_reap``, against its snapshot, honouring dry run."""
    del spawns
    calls: list[tuple[dict[str, LiveSession], DaemonReport, bool]] = []

    def record(
        _store: Store, snapshot: dict[str, LiveSession], report: DaemonReport, dry: bool
    ) -> None:
        calls.append((snapshot, report, dry))

    monkeypatch.setattr(daemon, "_run_handoff_finalize", record)
    report = daemon.run_once(
        dry_run=dry_run, do_reap=do_reap, do_summary=False, do_progress=False, do_alerts=False
    )
    assert len(calls) == 1
    snapshot, passed, dry = calls[0]
    assert isinstance(snapshot, dict) and passed is report and dry is dry_run
