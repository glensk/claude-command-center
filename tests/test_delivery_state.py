"""The delivery state machine (``ccc delivery -i ID -j``): FIFO, duplicates, terminal states."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bridgestub import (
    SID,
    World,
    answer,
    ask,
    dequeue,
    enqueue,
    question,
    queued,
    run_cli,
    task_notice,
    turn_end,
    user,
)

from command_center import bridge_delivery
from command_center.store import Store

HOUR_MS = 3600 * 1000


def _send(world: World, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str], text: str) -> str:
    _code, env, _err = run_cli(mp, cap, world, ["send", "-s", SID, "-j"], stdin=text)
    assert env is not None and env["data"] is not None, env
    return str(env["data"]["delivery_id"])


def _state(world: World, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str], did: str) -> str:
    code, env, _err = run_cli(mp, cap, world, ["delivery", "-i", did, "-j"])
    assert code == 0 and env is not None and env["ok"] is True, env
    return str(env["data"]["state"])


def _events(world: World) -> list[Any]:
    with Store(world.db) as store:
        return store.events_after(0)


@pytest.fixture(name="busy")
def busy_world(tmp_path: Path) -> World:
    world = World(tmp_path, status="busy")
    world.write(user("long running job"))
    world.on_text = lambda w, text: w.write(enqueue(text))
    return world


def test_idle_delivery_completes_at_the_first_turn_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.write(user("old"), *turn_end())
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "do it")
    assert _state(world, monkeypatch, capsys, did) == "accepted"
    world.write(*turn_end("did it"))
    assert _state(world, monkeypatch, capsys, did) == "completed"
    assert _events(world) == []  # successes are silent


def test_busy_delivery_waits_for_its_own_turn(
    busy: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    did = _send(busy, monkeypatch, capsys, "next task")
    busy.write(*turn_end("the long job is done"))  # the CURRENT turn, not ours
    assert _state(busy, monkeypatch, capsys, did) == "accepted"
    busy.write(dequeue(), user("next task"))
    assert _state(busy, monkeypatch, capsys, did) == "accepted"
    busy.write(*turn_end("next task done"))
    assert _state(busy, monkeypatch, capsys, did) == "completed"


def test_prompt_absorbed_mid_turn_completes_with_that_turn(
    busy: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    did = _send(busy, monkeypatch, capsys, "also check the logs")
    busy.write({"type": "queue-operation", "operation": "remove"}, queued("also check the logs"))
    busy.write(*turn_end())
    assert _state(busy, monkeypatch, capsys, did) == "completed"


def test_fifo_order_of_two_queued_prompts(
    busy: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _send(busy, monkeypatch, capsys, "first")
    second = _send(busy, monkeypatch, capsys, "second")
    busy.write(*turn_end(), dequeue(), user("first"), *turn_end())
    assert _state(busy, monkeypatch, capsys, first) == "completed"
    assert _state(busy, monkeypatch, capsys, second) == "accepted"
    busy.write(dequeue(), user("second"), *turn_end())
    assert _state(busy, monkeypatch, capsys, second) == "completed"


def test_duplicate_messages_claim_records_in_order(
    busy: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _send(busy, monkeypatch, capsys, "same text")
    second = _send(busy, monkeypatch, capsys, "same text")
    with Store(busy.db) as store:
        d1, d2 = store.deliveries_for(SID)
    assert {d1.delivery_id, d2.delivery_id} == {first, second}
    assert d1.matched_offset != d2.matched_offset  # each claimed its own enqueue
    busy.write(*turn_end(), dequeue(), user("same text"), *turn_end())
    assert _state(busy, monkeypatch, capsys, first) == "completed"
    assert _state(busy, monkeypatch, capsys, second) == "accepted"
    busy.write(dequeue(), user("same text"), *turn_end())
    assert _state(busy, monkeypatch, capsys, second) == "completed"


def test_interleaved_manual_prompt_does_not_complete_the_delivery(
    busy: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    did = _send(busy, monkeypatch, capsys, "voice prompt")
    busy.write(*turn_end(), user("typed by hand"), task_notice(), *turn_end())
    assert _state(busy, monkeypatch, capsys, did) == "accepted"
    busy.write(dequeue(), user("voice prompt"), *turn_end())
    assert _state(busy, monkeypatch, capsys, did) == "completed"


def test_needs_input_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "set it up")
    world.write(ask("toolu_q", [question("Which?", ["a", "b"])]))
    assert _state(world, monkeypatch, capsys, did) == "needs_input"
    assert _state(world, monkeypatch, capsys, did) == "needs_input"
    run_cli(monkeypatch, capsys, world, ["events", "-j"])
    events = _events(world)
    assert [e.kind for e in events] == ["needs_input"]
    assert events[0].detail_obj()["delivery_id"] == did


def test_answered_question_then_turn_end_is_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "set it up")
    q = [question("Which?", ["a", "b"])]
    world.write(ask("toolu_q", q), answer("toolu_q", q, {"Which?": "a"}), *turn_end())
    assert _state(world, monkeypatch, capsys, did) == "completed"


def test_process_death_fails_the_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "work")
    world.registered = False
    assert _state(world, monkeypatch, capsys, did) == "failed"
    (event,) = _events(world)
    assert event.kind == "delivery_failed"
    assert event.detail_obj() == {"delivery_id": did, "state": "failed", "reason": "process_exited"}


def test_completion_wins_over_a_later_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "work")
    world.write(*turn_end())
    world.registered = False
    assert _state(world, monkeypatch, capsys, did) == "completed"


def test_stop_failure_after_the_anchor_fails_the_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "work")
    with Store(world.db) as store:
        store.add_event("stop_failure", SID, world.clock_ms + 5, {"error": "overloaded"})
    assert _state(world, monkeypatch, capsys, did) == "failed"
    kinds = [e.kind for e in _events(world)]
    assert kinds == ["stop_failure", "delivery_failed"]


def test_no_stop_within_six_hours_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "work")
    world.clock_ms += 5 * HOUR_MS
    assert _state(world, monkeypatch, capsys, did) == "accepted"
    world.clock_ms += 2 * HOUR_MS
    assert _state(world, monkeypatch, capsys, did) == "timed_out"
    (event,) = _events(world)
    assert event.kind == "delivery_failed" and event.detail_obj()["state"] == "timed_out"


def test_unknown_delivery_is_promoted_by_later_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.send_result = ("", "partial")
    did = _send(world, monkeypatch, capsys, "late text")
    assert _state(world, monkeypatch, capsys, did) == "unknown"
    world.write(user("late text"))
    assert _state(world, monkeypatch, capsys, did) == "accepted"
    world.write(*turn_end())
    assert _state(world, monkeypatch, capsys, did) == "completed"


def test_earlier_unknown_delivery_claims_the_first_identical_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, status="busy")
    world.write(user("job"))
    world.send_result = ("", "partial")
    first = _send(world, monkeypatch, capsys, "again")  # unknown: no record yet
    world.send_result = ("python-api", "sent")
    world.on_text = lambda w, text: w.write(enqueue(text), enqueue(text))
    second = _send(world, monkeypatch, capsys, "again")
    with Store(world.db) as store:
        rows = {d.delivery_id: d for d in store.deliveries_for(SID)}
    assert rows[second].state == "accepted"
    assert _state(world, monkeypatch, capsys, first) == "accepted"
    assert rows[second].matched_offset > 0
    with Store(world.db) as store:
        rows = {d.delivery_id: d for d in store.deliveries_for(SID)}
    assert rows[first].matched_offset < rows[second].matched_offset  # FIFO


def test_stale_sending_row_becomes_unknown(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.write(user("x"))
    size = world.transcript.stat().st_size
    with Store(world.db) as store:
        store.insert_delivery(
            bridge_delivery.Delivery(
                delivery_id="d-stale",
                session_id=SID,
                transcript_path=str(world.transcript),
                transcript_inode=world.transcript.stat().st_ino,
                anchor_offset=size,
                content_sha="0" * 64,
                state="sending",
                created_at=world.clock_ms - 2 * bridge_delivery.SENDING_STALE_MS,
            )
        )
        changes = bridge_delivery.advance_all(store, world.deps())
        stale = store.get_delivery("d-stale")
        assert not changes and stale is not None and stale.state == "unknown"


def test_replaced_transcript_fails_the_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.on_text = lambda w, text: w.write(user(text))
    did = _send(world, monkeypatch, capsys, "work")
    world.transcript.unlink()
    world.transcript.write_text("", encoding="utf-8")  # new inode
    assert _state(world, monkeypatch, capsys, did) == "failed"


def test_unknown_delivery_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    code, env, _err = run_cli(monkeypatch, capsys, world, ["delivery", "-i", "nope", "-j"])
    assert code == 1 and env is not None and env["error"]["code"] == "unknown_delivery"
