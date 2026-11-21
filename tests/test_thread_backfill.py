"""``ccc thread-backfill``: the raw-edge audit, quarantine and the empty-column fill (tp#2372 F1a).

Fixture stores only (``tmp_path``); placeholder ids. A clean component gets its root id on
every empty ``thread_id``; any ambiguous one is quarantined whole and never written.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from command_center import cli, thread, thread_backfill
from command_center.bridge_aims import connect_ro
from command_center.store import Store
from command_center.transfer_store import FINALIZED, PREPARED, Transfer

NOW = 1_800_000_000_000
MIN = 60_000


@pytest.fixture(name="db")
def _db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    with Store(path) as store:
        for sid in ("root", "s1", "s2", "s3", "lone"):
            store.ensure(sid, cwd="/repo")
    return path


def _edge(
    db: Path,
    source: str,
    target: str,
    *,
    state: str = FINALIZED,
    lineage: bool = True,
    tid: str = "",
) -> None:
    """A *source* → *target* transfer in *state* (accepted_at set when finalized)."""
    with Store(db) as store:
        store.ensure(target, cwd="/repo")
        store.create_transfer(
            Transfer(
                transfer_id=tid or f"t-{source}-{target}",
                source_session=source,
                target_session=target,
            ),
            NOW - MIN,
        )
        if state == FINALIZED:
            store.conn.execute(
                "UPDATE transfers SET state = ?, accepted_at = ? WHERE transfer_id = ?",
                (FINALIZED, NOW, tid or f"t-{source}-{target}"),
            )
            store.conn.commit()
        if lineage:
            store.update_fields(target, handed_off_from=source)


def _set_thread(db: Path, sid: str, value: str) -> None:
    with Store(db) as store:
        store.conn.execute("UPDATE sessions SET thread_id = ? WHERE session_id = ?", (value, sid))
        store.conn.commit()


def _threads(db: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(db)
    try:
        return {str(r[0]): r[1] for r in conn.execute("SELECT session_id, thread_id FROM sessions")}
    finally:
        conn.close()


def _audit(db: Path) -> thread_backfill.Audit:
    conn = connect_ro(db)
    try:
        return thread_backfill.audit(conn)
    finally:
        conn.close()


def _quarantine(db: Path) -> dict[str, list[str]]:
    """root → reasons of every quarantined component."""
    return {c.root: c.reasons for c in _audit(db).components if c.quarantined}


def _write(db: Path) -> int:
    result = _audit(db)
    _again, n = thread_backfill.apply(db, result.fingerprint)
    return n


def test_a_clean_chain_gets_its_root_everywhere(db: Path) -> None:
    _edge(db, "root", "s1")
    _edge(db, "s1", "s2")
    assert not _quarantine(db)
    assert _write(db) == 5  # root, s1, s2 + s3 and lone (their own roots)
    rows = _threads(db)
    assert [rows[s] for s in ("root", "s1", "s2")] == ["root", "root", "root"]
    assert rows["s3"] == "s3" and rows["lone"] == "lone"


def test_a_root_gets_its_own_id(db: Path) -> None:
    comp = next(c for c in _audit(db).components if c.root == "lone")
    assert comp.members == ["lone"] and not comp.quarantined
    assert comp.writes == [("lone", "lone")]


def test_a_rerun_writes_nothing(db: Path) -> None:
    _edge(db, "root", "s1")
    assert _write(db) == 5
    before = _threads(db)
    assert _write(db) == 0
    assert _threads(db) == before
    payload = thread_backfill.report(_audit(db), held=None, write=False)
    assert payload["written"] == 0 and payload["already"] == 5


def test_a_recorded_id_is_never_overwritten(db: Path) -> None:
    _edge(db, "root", "s1")
    _edge(db, "s1", "s2")
    _set_thread(db, "s1", "root")  # agrees with the root: kept, counted as already
    result = _audit(db)
    assert ("s1", "root") not in result.writes
    assert _write(db) == 4
    assert _threads(db)["s1"] == "root"


def test_a_conflicting_recorded_id_quarantines_the_component(db: Path) -> None:
    _edge(db, "root", "s1")
    _edge(db, "s1", "s2")
    _set_thread(db, "s2", "elsewhere")  # disagrees with the structural root
    assert _quarantine(db) == {"root": ["conflict"]}
    _write(db)
    rows = _threads(db)
    assert rows["root"] is None and rows["s1"] is None and rows["s2"] == "elsewhere"
    assert rows["lone"] == "lone"


def test_a_fork_source_quarantines(db: Path) -> None:
    _edge(db, "root", "s1")
    _edge(db, "root", "s2")
    assert _quarantine(db) == {"root": ["fork"]}
    assert _audit(db).figures["fork_sources"] == 1


def test_a_target_with_two_parents_quarantines(db: Path) -> None:
    _edge(db, "root", "s2")
    _edge(db, "s1", "s2", lineage=False)  # a second, accepted transfer into s2
    assert _quarantine(db) == {"root": ["multi_parent"]}


def test_a_single_unaccepted_edge_quarantines(db: Path) -> None:
    _edge(db, "root", "s1", state=PREPARED)  # start wrote handed_off_from; never accepted
    assert _quarantine(db) == {"root": ["unaccepted"]}
    assert _audit(db).figures["unaccepted_edges"] == 1


def test_a_bare_lineage_column_without_a_transfer_is_unaccepted(db: Path) -> None:
    with Store(db) as store:
        store.update_fields("s1", handed_off_from="root")
    assert _quarantine(db) == {"root": ["unaccepted"]}


def test_a_missing_ancestor_quarantines(db: Path) -> None:
    _edge(db, "gone", "s1")
    with Store(db) as store:
        store.conn.execute("DELETE FROM sessions WHERE session_id = 'gone'")
        store.conn.commit()
    comps = {c.root: c for c in _audit(db).components if c.quarantined}
    assert list(comps) == ["gone"]
    assert comps["gone"].reasons == ["missing_ancestor"]
    assert comps["gone"].missing == ["gone"]


def test_a_cycle_quarantines(db: Path) -> None:
    _edge(db, "s1", "s2")
    _edge(db, "s2", "s1")
    quarantined = _quarantine(db)
    assert list(quarantined.values()) == [["cycle"]]


def test_a_chain_past_max_hops_quarantines(db: Path) -> None:
    ids = ["root"] + [f"h{i:03d}" for i in range(thread.MAX_HOPS + 1)]
    for source, target in zip(ids, ids[1:], strict=False):
        _edge(db, source, target)
    reasons = _quarantine(db)["root"]
    assert reasons[0] == "max_hops"
    assert _write(db) == 4  # only s1, s2, s3, lone
    assert _threads(db)[ids[-1]] is None


def test_write_refuses_without_or_with_a_stale_fingerprint(db: Path) -> None:
    with pytest.raises(thread_backfill.Refused):
        thread_backfill.apply(db, "")
    stale = _audit(db).fingerprint
    _edge(db, "root", "s1")
    with pytest.raises(thread_backfill.Refused):
        thread_backfill.apply(db, stale)
    assert set(_threads(db).values()) == {None}


def _tp_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE items(tp_id INTEGER PRIMARY KEY, status TEXT);"
        "CREATE TABLE holdings(id INTEGER PRIMARY KEY, tp_id INTEGER, session TEXT, "
        "claim_at TEXT, ended TEXT, drive_at TEXT, plan_row TEXT, plan_path TEXT);"
        "INSERT INTO items VALUES (7, 'open'), (8, 'blocked'), (9, 'open');"
        "INSERT INTO holdings VALUES (1, 7, 's1', 'x', NULL, 'x', NULL, NULL);"
        "INSERT INTO holdings VALUES (2, 8, 's2', NULL, NULL, NULL, 'active', NULL);"
        "INSERT INTO holdings VALUES (3, 9, 's1', 'x', 'y', NULL, NULL, NULL);"  # ended
        "INSERT INTO holdings VALUES (4, 9, 'lone', 'x', NULL, NULL, NULL, NULL);"
    )
    conn.commit()
    conn.close()
    return path


def test_cli_json_lists_quarantine_with_tp_obligations(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edge(db, "root", "s1")
    _edge(db, "root", "s2")
    tp = _tp_db(tmp_path / "tp.sqlite3")
    out = tmp_path / "out.json"
    argv = ["thread-backfill", "-n", "-j", "-d", str(db), "-T", str(tp), "-o", str(out)]
    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True and payload["written"] == 2  # s3, lone
    [q] = payload["quarantined"]
    assert q["root"] == "root" and q["reason"] == "fork"
    assert q["sessions"] == ["root", "s1", "s2"]
    assert q["tp_claims"] == [{"session": "s1", "tp_id": 7}]
    kinds = {(o["session"], o["tp_id"]): (o["kinds"], o["item_status"]) for o in q["obligations"]}
    assert kinds == {("s1", 7): (["claim", "drive"], "open"), ("s2", 8): (["plan"], "blocked")}
    assert payload["summary"]["quarantined_holding_sessions"] == 2
    full = json.loads(out.read_text(encoding="utf-8"))
    assert {(w["session_id"], w["thread_id"]) for w in full["writes"]} == {
        ("s3", "s3"),
        ("lone", "lone"),
    }
    assert out.stat().st_mode & 0o777 == 0o600
    assert set(_threads(db).values()) == {None}  # the dry run wrote nothing


def test_cli_write_needs_the_fingerprint(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["thread-backfill", "-w", "-d", str(db)]) == thread_backfill.EXIT_REFUSED
    assert set(_threads(db).values()) == {None}
    capsys.readouterr()
    assert cli.main(["thread-backfill", "-j", "-d", str(db)]) == 0
    fp = json.loads(capsys.readouterr().out)["fingerprint"]
    assert cli.main(["thread-backfill", "-w", "-j", "-d", str(db), "-F", fp]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == 5
    assert _threads(db)["lone"] == "lone"


def test_cli_human_output_and_help(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["thread-backfill", "-d", str(db)]) == 0
    assert "would write 5" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.main(["thread-backfill", "-h"])
    assert exc.value.code == 0
