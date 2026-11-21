"""``ccc new-job --thread-from``, ``thread-accept``, ``thread-handback`` (tp#2372 F2a).

A relay successor registered as a future job joins its predecessor's thread at its first
SessionStart; the lineage is fill-only on both sides and never changes the source's
status; a staff (``accept``) relay is current only after a bound ``thread-accept`` and is
cancelled only by its op token; a fork never joins.
"""

from __future__ import annotations

# pylint: disable=redefined-outer-name  # pytest fixtures
import argparse
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from command_center import cli, hooks
from command_center.models import Status
from command_center.store import Store
from command_center.thread_join_store import ACCEPTED, CANCELLED, RESERVED


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME (the default store path is this test's own DB)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_AIM", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _source(repo: Path, status: str = Status.WORKING.value, thread_id: str = "") -> str:
    """A live source session row (its own thread root unless *thread_id*)."""
    sid = str(uuid.uuid4())
    with Store() as store:
        store.ensure(sid, cwd=str(repo))
        store.update_fields(sid, status=status, thread_id=thread_id or None)
    return sid


def _new_job(repo: Path, *argv: str) -> argparse.Namespace:
    base = ["new-job", "-a", "relay successor", "-c", str(repo), "-J"]
    return cli.build_parser().parse_args([*base, *argv])


def _register(repo: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, object]:
    capsys.readouterr()  # a previous SessionStart's context
    assert cli.cmd_new_job(_new_job(repo, *argv)) == 0
    doc: dict[str, object] = json.loads(capsys.readouterr().out)
    return doc


def _start(sid: str, source: str = "startup") -> None:
    """The job's SessionStart, after its launch cleared the draft flag."""
    with Store() as store:
        store.clear_draft(sid)
    hooks.handle_session_start({"session_id": sid, "cwd": "/repo", "source": source})


def _row(sid: str) -> sqlite3.Row:
    with Store() as store:
        row = store.conn.execute(
            "SELECT status, thread_id, handed_off_to, handed_off_from, handed_off_at, "
            "thread_from, thread_join FROM sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
    assert row is not None
    return row


def _state(sid: str) -> str:
    with Store() as store:
        join = store.thread_join_for(sid)
    assert join is not None
    return join.state


def _verb(*argv: str) -> int:
    args = cli.build_parser().parse_args(list(argv))
    rc: int = args.func(args)
    return rc


# --------------------------------------------------------------------------- #
# the join (start mode, the park relay)
# --------------------------------------------------------------------------- #
def test_first_session_start_joins_the_thread_with_target_side_lineage(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home, thread_id="root-thread")
    doc = _register(home, capsys, "-F", src)
    new = str(doc["session_id"])
    assert doc["thread_from"] == src and doc["thread_join"] == "start" and doc["thread_op"]
    assert (_row(new)["thread_from"], _row(new)["thread_join"]) == (src, "start")
    assert _state(new) == RESERVED
    _start(new)
    capsys.readouterr()
    target, source = _row(new), _row(src)
    assert target["thread_id"] == "root-thread"
    assert target["handed_off_from"] == src and target["handed_off_at"]
    assert source["handed_off_to"] == new
    assert _state(new) == ACCEPTED


def test_a_live_source_keeps_its_status(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _source(home, status=Status.WORKING.value)
    new = str(_register(home, capsys, "-F", src)["session_id"])
    _start(new)
    assert _row(src)["status"] == Status.WORKING.value
    assert _row(src)["handed_off_at"] is None  # the source side writes handed_off_to only


def test_a_second_relay_from_the_same_source_keeps_the_first_successor(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    first = str(_register(home, capsys, "-F", src)["session_id"])
    _start(first)
    second = str(_register(home, capsys, "-F", src)["session_id"])
    _start(second)
    assert _row(src)["handed_off_to"] == first
    assert _row(second)["handed_off_from"] == src
    assert _row(second)["thread_id"] == _row(first)["thread_id"] == src


def test_a_rerun_session_start_writes_nothing_new(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    new = str(_register(home, capsys, "-F", src)["session_id"])
    _start(new)
    before = tuple(_row(new))
    hooks.handle_session_start({"session_id": new, "cwd": "/repo", "source": "resume"})
    assert tuple(_row(new)) == before and _state(new) == ACCEPTED


# --------------------------------------------------------------------------- #
# the fork rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source", ["fork", "clear", "compact"])
def test_a_fork_clear_or_compact_start_never_joins(
    home: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    src = _source(home)
    new = str(_register(home, capsys, "-F", src)["session_id"])
    _start(new, source=source)
    row = _row(new)
    assert row["thread_id"] is None and row["handed_off_from"] is None
    assert _row(src)["handed_off_to"] is None and _state(new) == RESERVED


@pytest.mark.parametrize("source", ["fork", "resume"])
def test_a_fork_row_with_a_copied_parent_lineage_records_nothing(home: Path, source: str) -> None:
    parent = _source(home, thread_id="root-thread")
    fork = str(uuid.uuid4())
    with Store() as store:  # the forked row carries its parent's lineage, no reservation
        store.ensure(fork, cwd=str(home))
        store.update_fields(fork, handed_off_from=parent)
    hooks.handle_session_start({"session_id": fork, "cwd": "/repo", "source": source})
    assert _row(fork)["thread_id"] is None


# --------------------------------------------------------------------------- #
# refusals, before any write (exit 2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["-F", "not-a-session"],
        ["-F", "ABCDEF12-0000-0000-0000-000000000000"],
        ["-F", str(uuid.UUID(int=7))],  # well-formed, unknown
        ["-G", "accept"],
        ["-F", "<src>", "-G", "accept"],
        ["-F", "<src>", "-G", "accept", "-r", "staff/a/b"],
        ["-F", "<src>", "-r", "staff/a/b"],
        ["-F", "<src>", "-g", "3"],
        ["-F", "<draft>"],
    ],
)
def test_a_bad_thread_source_refuses_before_any_write(home: Path, argv: list[str]) -> None:
    src = _source(home)
    with Store() as store:
        store.create_draft("d-1", str(home), "an unstarted job")
        draft = str(uuid.uuid4())
        store.create_draft(draft, str(home), "another unstarted job")
        before = store.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    resolved = [src if a == "<src>" else draft if a == "<draft>" else a for a in argv]
    assert cli.cmd_new_job(_new_job(home, "-K", "key-1", *resolved)) == 2
    with Store() as store:
        assert store.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before
        assert store.conn.execute("SELECT COUNT(*) FROM thread_joins").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# idempotency (-K)
# --------------------------------------------------------------------------- #
def test_an_idempotent_rerun_returns_the_same_job_and_op(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    argv = ("-K", "relay-1", "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "4")
    first = _register(home, capsys, *argv)
    again = _register(home, capsys, *argv)
    assert again["created"] is False
    assert (again["session_id"], again["thread_op"]) == (first["session_id"], first["thread_op"])
    with Store() as store:
        assert store.conn.execute("SELECT COUNT(*) FROM thread_joins").fetchone()[0] == 1


@pytest.mark.parametrize(
    "argv",
    [
        [],  # the key named a thread relay, the re-run names none
        ["-F", "<other>"],
        ["-F", "<src>", "-G", "accept", "-r", "staff/a/x", "-g", "4"],
    ],
)
def test_the_key_check_includes_the_thread_fields(
    home: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    src, other = _source(home), _source(home)
    _register(home, capsys, "-K", "relay-1", "-F", src)
    resolved = [src if a == "<src>" else other if a == "<other>" else a for a in argv]
    assert cli.cmd_new_job(_new_job(home, "-K", "relay-1", *resolved)) == 2
    assert "thread_" in capsys.readouterr().err


def test_the_key_check_includes_the_staff_binding(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    _register(home, capsys, "-K", "k", "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "4")
    argv = ("-K", "k", "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "5")
    assert cli.cmd_new_job(_new_job(home, *argv)) == 2
    assert "lease_generation" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the staff relay: thread-accept / thread-handback
# --------------------------------------------------------------------------- #
def _staff(home: Path, capsys: pytest.CaptureFixture[str]) -> tuple[str, str, str]:
    src = _source(home)
    doc = _register(home, capsys, "-F", src, "-G", "accept", "-r", "staff/a/x", "-g", "4")
    return src, str(doc["session_id"]), str(doc["thread_op"])


def test_accept_mode_records_lineage_but_waits_for_thread_accept(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, new, op = _staff(home, capsys)
    _start(new)
    assert _row(new)["thread_id"] == src and _row(src)["handed_off_to"] == new
    assert _row(src)["status"] == Status.WORKING.value and _state(new) == RESERVED
    capsys.readouterr()
    accept = ("thread-accept", "-S", new, "-f", src, "-r", "staff/a/x")
    assert _verb(*accept, "-g", "5") == 2  # a different lease generation
    assert _verb("thread-accept", "-S", new, "-f", new, "-r", "staff/a/x", "-g", "4") == 2
    assert _verb(*accept, "-g", "4", "-O", "0" * 32) == 2  # another op
    assert _state(new) == RESERVED
    assert _verb(*accept, "-g", "4", "-J") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["state"] == ACCEPTED and doc["changed"] is True and doc["op"] == op
    assert _verb(*accept, "-g", "4", "-J") == 0  # idempotent
    assert json.loads(capsys.readouterr().out)["changed"] is False
    assert _verb("thread-handback", "-S", new, "-O", op) == 1  # accepted is never undone
    assert _state(new) == ACCEPTED


def test_thread_accept_joins_a_successor_whose_session_start_was_missed(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, new, _ = _staff(home, capsys)
    with Store() as store:
        store.clear_draft(new)
    assert _verb("thread-accept", "-S", new, "-f", src, "-r", "staff/a/x", "-g", "4") == 0
    assert _row(new)["thread_id"] == src and _row(new)["handed_off_from"] == src


def test_thread_accept_refuses_an_unstarted_successor(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, new, _ = _staff(home, capsys)
    assert _verb("thread-accept", "-S", new, "-f", src, "-r", "staff/a/x", "-g", "4") == 1
    assert _state(new) == RESERVED


def test_handback_cancels_only_a_pending_reservation_by_its_token(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, new, op = _staff(home, capsys)
    _start(new)
    assert _verb("thread-handback", "-S", new, "-O", "f" * 32) == 2  # not this op
    assert _verb("thread-handback", "-S", src, "-O", op) == 2  # not this session's op
    assert _verb("thread-handback", "-S", new, "-O", op, "-n") == 0
    assert _state(new) == RESERVED
    assert _verb("thread-handback", "-S", new, "-O", op) == 0
    assert _state(new) == CANCELLED
    assert _row(src)["handed_off_to"] is None  # the source stays current, no stale marker
    assert _verb("thread-handback", "-S", new, "-O", op) == 0  # already cancelled
    accept = ("thread-accept", "-S", new, "-f", src, "-r", "staff/a/x", "-g", "4")
    assert _verb(*accept) == 1


def test_a_cancelled_reservation_never_joins_late(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, new, op = _staff(home, capsys)
    assert _verb("thread-handback", "-S", new, "-O", op) == 0
    _start(new)
    assert _row(new)["thread_id"] is None and _row(src)["handed_off_to"] is None


def test_the_verbs_refuse_a_start_mode_relay(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _source(home)
    doc = _register(home, capsys, "-F", src)
    new, op = str(doc["session_id"]), str(doc["thread_op"])
    assert _verb("thread-accept", "-S", new, "-f", src, "-r", "x", "-g", "0") == 2
    assert _verb("thread-handback", "-S", new, "-O", op) == 2


def test_a_staff_source_keeps_taking_prompts_a_park_source_does_not(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    staff_src, staff_new, _ = _staff(home, capsys)
    _start(staff_new)
    park_src = _source(home, status=Status.PARKED.value)
    park_new = str(_register(home, capsys, "-F", park_src)["session_id"])
    _start(park_new)
    assert hooks._handed_off_target(staff_src) == ""  # pylint: disable=protected-access
    assert hooks._handed_off_target(park_src) == park_new  # pylint: disable=protected-access


# --------------------------------------------------------------------------- #
# inert by default
# --------------------------------------------------------------------------- #
def test_an_ordinary_job_is_unaffected(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = _register(home, capsys)
    assert "thread_from" not in doc and "thread_op" not in doc
    new = str(doc["session_id"])
    _start(new)
    assert _row(new)["thread_from"] is None and _row(new)["thread_id"] is None
    with Store() as store:
        assert store.conn.execute("SELECT COUNT(*) FROM thread_joins").fetchone()[0] == 0


def test_the_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with Store(db):
        pass
    with Store(db) as store:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(sessions)")}
        assert {"thread_from", "thread_join"} <= cols
        store.conn.execute("SELECT op_id FROM thread_joins").fetchall()
