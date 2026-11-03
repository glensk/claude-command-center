"""``ccc lineage -s ID -j`` — a session's continue-session predecessors (tp#1481 B3a).

Fixture stores only: every test opens a ``Store`` under ``tmp_path`` (``CLAUDE_HOME`` is
pinned there too); the real ccc state is never read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center import bridge_cli, bridge_lineage, cli
from command_center.bridge_target import BridgeDeps
from command_center.store import Store
from command_center.transfer_store import ACCEPTED, FAILED, FINALIZED, Transfer

NOW = 1_800_000_000_000  # 2027-01-15T08:00:00Z
MIN = 60_000


def _iso(ms: int) -> str:
    return bridge_lineage.iso(ms) or ""


def _hop(
    store: Store,
    source: str,
    target: str,
    *,
    state: str = FINALIZED,
    at: int = NOW,
    lineage: bool = True,
    tid: str = "",
) -> None:
    """A transfer *source* → *target* in *state*, accepted at *at*; optionally the row lineage."""
    transfer_id = tid or f"t-{source}-{target}"
    store.create_transfer(
        Transfer(transfer_id=transfer_id, source_session=source, target_session=target),
        at - MIN,
    )
    accepted_at = at if state in (ACCEPTED, FINALIZED) else 0
    store.conn.execute(
        "UPDATE transfers SET state = ?, accepted_at = ? WHERE transfer_id = ?",
        (state, accepted_at, transfer_id),
    )
    store.conn.commit()
    if lineage:
        store.set_handoff_lineage(source, target, at)


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "ccc.db")
    for sid in ("a", "b", "c", "d", "x"):
        db.ensure(sid, cwd="/repo")
    return db


def test_chain_of_three_newest_first(store: Store) -> None:
    _hop(store, "a", "b", at=NOW)
    _hop(store, "b", "c", at=NOW + MIN)
    # the newest hop is known from the transfers table only (no row lineage yet)
    _hop(store, "c", "d", state=ACCEPTED, at=NOW + 2 * MIN, lineage=False)
    data = bridge_lineage.lineage(store, "d")
    assert data == {
        "session_id": "d",
        "chain": [
            {
                "session_id": "c",
                "at": _iso(NOW + 2 * MIN),
                "via": "continue-session",
                "state": ACCEPTED,
            },
            {
                "session_id": "b",
                "at": _iso(NOW + MIN),
                "via": "continue-session",
                "state": FINALIZED,
            },
            {"session_id": "a", "at": _iso(NOW), "via": "continue-session", "state": FINALIZED},
        ],
        "truncated": False,
    }
    assert data["chain"][0]["at"].endswith("Z")


def test_failed_transfer_is_skipped(store: Store) -> None:
    _hop(store, "c", "d", at=NOW, lineage=False)
    # a newer transfer into d that failed never was a hop
    _hop(store, "x", "d", state=FAILED, at=NOW + 5 * MIN, lineage=False)
    chain = bridge_lineage.lineage(store, "d")["chain"]
    assert [h["session_id"] for h in chain] == ["c"]
    assert chain[0]["state"] == FINALIZED


def test_only_failed_transfers_give_an_empty_chain(store: Store) -> None:
    _hop(store, "x", "d", state=FAILED, lineage=False)
    assert bridge_lineage.lineage(store, "d") == {
        "session_id": "d",
        "chain": [],
        "truncated": False,
    }


def test_cycle_is_cut(store: Store) -> None:
    _hop(store, "a", "b", at=NOW, lineage=False)
    _hop(store, "b", "c", at=NOW + MIN, lineage=False)
    _hop(store, "c", "a", at=NOW + 2 * MIN, lineage=False, tid="t-loop")
    data = bridge_lineage.lineage(store, "c")
    assert [h["session_id"] for h in data["chain"]] == ["b", "a"]
    assert data["truncated"] is True


def test_self_loop_is_cut(store: Store) -> None:
    _hop(store, "a", "a", lineage=False)
    assert bridge_lineage.lineage(store, "a") == {
        "session_id": "a",
        "chain": [],
        "truncated": True,
    }


def test_unknown_session_gives_an_empty_chain(store: Store) -> None:
    assert bridge_lineage.lineage(store, "nope-0000") == {
        "session_id": "nope-0000",
        "chain": [],
        "truncated": False,
    }


def test_handed_off_from_without_a_transfer(store: Store) -> None:
    store.set_handoff_lineage("a", "b", NOW)
    assert bridge_lineage.lineage(store, "b")["chain"] == [
        {"session_id": "a", "at": _iso(NOW), "via": "continue-session", "state": None}
    ]


def test_row_lineage_wins_over_another_transfer(store: Store) -> None:
    _hop(store, "a", "d", at=NOW)  # the recorded predecessor
    _hop(store, "x", "d", state=FINALIZED, at=NOW + MIN, lineage=False)
    chain = bridge_lineage.lineage(store, "d")["chain"]
    assert [h["session_id"] for h in chain] == ["a"]


def test_hop_cap(store: Store) -> None:
    ids = [f"s{i}" for i in range(6)]
    for i in range(5):
        _hop(store, ids[i], ids[i + 1], at=NOW + i * MIN, lineage=False)
    data = bridge_lineage.lineage(store, "s5", max_hops=3)
    assert [h["session_id"] for h in data["chain"]] == ["s4", "s3", "s2"]
    assert data["truncated"] is True
    assert bridge_lineage.MAX_HOPS == 50
    full = bridge_lineage.lineage(store, "s5")
    assert len(full["chain"]) == 5 and full["truncated"] is False


def test_read_only(store: Store) -> None:
    _hop(store, "a", "b")
    before = store.conn.total_changes
    bridge_lineage.lineage(store, "b")
    bridge_lineage.lineage(store, "unknown")
    assert store.conn.total_changes == before


def test_cli_envelope_and_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ccc.db"
    with Store(path) as db:
        _hop(db, "a", "b")
    deps = BridgeDeps(store=lambda: Store(path))
    monkeypatch.setattr(bridge_cli.bridge_target, "default_deps", lambda: deps)
    assert cli.main(["lineage", "-s", "b", "-j"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True and env["error"] is None
    assert env["data"]["chain"] == [
        {"session_id": "a", "at": _iso(NOW), "via": "continue-session", "state": FINALIZED}
    ]
    assert cli.main(["lineage", "-s", "zzz", "-j"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["chain"] == []
    with pytest.raises(SystemExit) as usage:  # -s is required: a usage envelope, exit 2
        cli.main(["lineage", "-j"])
    assert usage.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
    assert "lineage" in bridge_cli.JSON_COMMANDS
    with pytest.raises(SystemExit) as done:
        cli.main(["lineage", "-h"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert "-s" in out and "-j" in out and "predecessors" in out
