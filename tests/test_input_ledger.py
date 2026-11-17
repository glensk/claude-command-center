"""The input ledger core (tp#2170 B2): ``inputs`` / ``bridge_meta`` storage, the
``ccc-text-sha-v1`` shas, write-ahead recording, the gap file and the pure matcher."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import stat
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from bridgestub import dequeue, enqueue, queued, turn_end, user

from command_center import bridge_store, input_ledger
from command_center.adapters import claude_bridge as cb
from command_center.bridge_store import Delivery, Input
from command_center.store import Store

SID = "00000000-0000-4000-8000-00000000c001"
OTHER = "00000000-0000-4000-8000-00000000c002"
TEXT = "please run the release checklist now"
HOUR_MS = 3600 * 1000
T0 = 1_800_000_000_000


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


def _factory(path: Path) -> Callable[[], Store]:
    return lambda: Store(path)


def _write(path: Path, *records: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def _record(db: Path, transcript: Path | None, text: str = TEXT, at: int = T0, **kw: Any) -> str:
    args: dict[str, Any] = {
        "session_id": SID,
        "kind": "await_paste",
        "origin": "automation",
        "text": text,
        "config_dir": "",
        "cwd": "/repo",
        "transcript": transcript,
        "paste": True,
        "now_ms": at,
        "gaps": db.parent / "gaps.jsonl",
    }
    args.update(kw)
    input_id = input_ledger.record(_factory(db), **args)
    assert input_id is not None
    return input_id


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    Store(path).close()
    return path


@pytest.fixture(name="transcript")
def transcript_fixture(tmp_path: Path) -> Path:
    path = tmp_path / f"{SID}.jsonl"
    _write(path, user("an older prompt"), *turn_end())
    return path


def _rows(db: Path, session_id: str = SID) -> list[Input]:
    with Store(db) as store:
        return store.inputs_for(session_id, 0, 2**62)


def _match(db: Path, transcript: Path) -> dict[str, input_ledger.Match]:
    return input_ledger.match_session(_rows(db), cb.read_appended(transcript, 0, 0))


# ----------------------------------------------------------------------------- schema


def test_fresh_store_has_the_ledger_tables(db: Path) -> None:
    names = _tables(db)
    assert {"inputs", "bridge_meta", "idx_inputs_session_created", "idx_inputs_state"} <= names
    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(inputs)")]
        idx = [r[2] for r in conn.execute("PRAGMA index_info(idx_inputs_session_created)")]
    assert cols == [f.name for f in dataclasses.fields(Input)]
    assert "text" not in cols  # never the text
    assert idx == ["session_id", "created_at"]


def test_a_pre_ledger_db_gains_the_tables_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with Store(path) as store:
        store.ensure("s1", cwd="/repo")
        store.insert_delivery(Delivery(delivery_id="d1", session_id="s1"))
    with sqlite3.connect(path) as conn:  # what a pre-ledger build left behind
        conn.execute("DROP TABLE inputs")
        conn.execute("DROP TABLE bridge_meta")
    assert "inputs" not in _tables(path)
    with Store(path) as store:
        assert store.get("s1") is not None
        assert store.get_delivery("d1") is not None
        store.insert_input(Input(input_id="i1", session_id="s1", created_at=5))
        assert [r.input_id for r in store.inputs_for("s1", 0, 10)] == ["i1"]


def test_columns_a_newer_build_added_are_ignored(db: Path) -> None:
    with Store(db) as store:
        store.insert_input(Input(input_id="i1", session_id=SID, created_at=1))
    with sqlite3.connect(db) as conn:  # a NEWER ccc writes columns this build lacks
        conn.execute("ALTER TABLE inputs ADD COLUMN future_col TEXT DEFAULT 'z'")
        conn.execute(
            "INSERT INTO inputs (input_id, session_id, created_at, future_col) "
            "VALUES ('i2', ?, 2, 'new')",
            (SID,),
        )
    with Store(db) as store:
        assert [r.input_id for r in store.inputs_for(SID, 0, 10)] == ["i1", "i2"]
        assert store.update_input("i2", "pending", state="failed", reason="x")
        assert store.get_input("i2") == Input(
            input_id="i2", session_id=SID, created_at=2, state="failed", reason="x"
        )


def test_a_later_added_input_column_is_altered_in(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(bridge_store.ADDED_INPUT_COLUMNS, "extra", "TEXT NOT NULL DEFAULT ''")
    with Store(db) as store:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(inputs)")}
    assert "extra" in cols


def test_update_input_is_a_cas_and_rejects_unknown_fields(db: Path) -> None:
    with Store(db) as store:
        store.insert_input(Input(input_id="i1", session_id=SID))
        assert not store.update_input("i1", "matched", state="unmatched")
        assert store.update_input("i1", "pending", state="unmatched")
        assert not store.update_input("i1", "pending", state="matched")
        assert not store.update_input("nope", "pending", state="failed")
        with pytest.raises(ValueError):
            store.update_input("i1", "unmatched", text="never")


def test_bridge_meta_set_get_delete(db: Path) -> None:
    with Store(db) as store:
        assert store.meta_get(input_ledger.META_COMPLETE_SINCE) is None
        store.meta_set(input_ledger.META_COMPLETE_SINCE, "100", now_ms=1)
        store.meta_set(input_ledger.META_COMPLETE_SINCE, "200", now_ms=2)
        assert store.meta_get(input_ledger.META_COMPLETE_SINCE) == "200"
        assert store.meta_delete(input_ledger.META_COMPLETE_SINCE)
        assert not store.meta_delete(input_ledger.META_COMPLETE_SINCE)
        assert store.meta_get(input_ledger.META_COMPLETE_SINCE) is None


def test_queries_window_session_and_unbound_rows(db: Path) -> None:
    with Store(db) as store:
        for input_id, sid, at, state, cfg in (
            ("a", SID, 10, "matched", ""),
            ("b", SID, 20, "pending", ""),
            ("c", OTHER, 15, "pending", ""),
            ("u1", "", 12, "pending", "/cfg/one"),
            ("u2", "", 14, "unmatched", "/cfg/two"),
            ("u3", "", 16, "failed", "/cfg/one"),
            ("u4", "", 40, "pending", "/cfg/one"),
        ):
            store.insert_input(
                Input(input_id=input_id, session_id=sid, created_at=at, state=state, config_dir=cfg)
            )
        assert [r.input_id for r in store.inputs_for(SID, 10, 20)] == ["a"]  # until exclusive
        assert [r.input_id for r in store.inputs_for(SID, 0, 100)] == ["a", "b"]
        # any config dir counts (one transcript can be shared across accounts)
        assert [r.input_id for r in store.unbound_inputs(0, 30)] == ["u1", "u2"]
        assert sorted(store.pending_input_sessions()) == sorted(["", SID, OTHER])


# ----------------------------------------------------------------------------- shas


@pytest.mark.parametrize(
    "variant",
    [
        "first line\r\nsecond line",
        "first line\rsecond line",
        "first line\t\t\nsecond line \t",
        "\n\n  first line\nsecond line\n\n",
    ],
)
def test_content_sha_normalises_line_ends_and_whitespace(variant: str) -> None:
    assert input_ledger.content_sha(variant) == _sha("first line\nsecond line")


def test_content_sha_is_nfc() -> None:
    composed = "café über"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    assert input_ledger.content_sha(decomposed) == _sha(composed)


def test_alt_sha_only_when_the_one_line_form_differs() -> None:
    assert input_ledger.shas("one line only", paste=True) == (_sha("one line only"), "")
    assert input_ledger.shas("line one\n", paste=True)[1] == ""  # normalises the same
    content, alt = input_ledger.shas("line one\nline two", paste=True)
    assert content == _sha("line one\nline two")
    assert alt == _sha("line one line two")
    assert input_ledger.shas("line one\nline two", paste=False) == (content, "")


# ----------------------------------------------------------------------------- record


def test_record_writes_hashes_and_anchor_never_the_text(db: Path, transcript: Path) -> None:
    size, inode = transcript.stat().st_size, transcript.stat().st_ino
    input_id = _record(db, transcript, "multi\nline text", delivery_id="d1")
    row = _rows(db)[0]
    assert row.input_id == input_id
    assert (row.kind, row.origin, row.state, row.norm) == (
        "await_paste",
        "automation",
        "pending",
        "ccc-text-sha-v1",
    )
    assert (row.content_sha, row.alt_sha) == (_sha("multi\nline text"), _sha("multi line text"))
    assert (row.transcript_path, row.transcript_inode, row.anchor_offset) == (
        str(transcript),
        inode,
        size,
    )
    assert (row.created_at, row.updated_at, row.delivery_id) == (T0, T0, "d1")
    dump = "\n".join(str(r) for r in sqlite3.connect(db).execute("SELECT * FROM inputs"))
    assert "multi" not in dump and "line text" not in dump


def test_record_before_the_typer_runs(db: Path, transcript: Path) -> None:
    seen: list[list[str]] = []

    def typer(_text: str) -> None:  # what a ledgered path calls AFTER record()
        seen.append([r.state for r in _rows(db)])

    input_id = _record(db, transcript)
    typer(TEXT)
    assert input_id and seen == [["pending"]]


def test_record_fresh_session_anchors_on_the_expected_path(db: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    _record(db, transcript=None, config_dir=str(cfg), cwd="/work/repo", paste=False)
    row = _rows(db)[0]
    assert row.transcript_path == str(cfg / "projects" / "-work-repo" / f"{SID}.jsonl")
    assert (row.transcript_inode, row.anchor_offset, row.alt_sha) == (0, 0, "")


def _symlinked_cwd(tmp_path: Path) -> tuple[Path, Path]:
    """``(link, real)``: a job folder reached through a symlinked parent (macOS /tmp)."""
    real = tmp_path / "private-tmp"
    (real / "job").mkdir(parents=True)
    link = tmp_path / "tmp"
    os.symlink(real, link)
    return link / "job", (real / "job").resolve()


def test_start_job_row_on_a_symlinked_cwd_anchors_on_the_resolved_path(
    db: Path, tmp_path: Path
) -> None:
    link, real = _symlinked_cwd(tmp_path)
    cfg = tmp_path / "cfg"
    spec = input_ledger.LedgerSpec("start_job", "caller", SID, str(cfg), str(link), fresh=True)
    assert input_ledger.record_exec(_factory(db), spec, TEXT, now_ms=T0)
    (row,) = _rows(db)
    expected = cfg / "projects" / cb.project_dir_name(str(real)) / f"{SID}.jsonl"
    assert row.cwd == str(real)
    assert row.transcript_path == str(expected)
    # Claude Code writes the transcript there, its lines carrying the resolved cwd.
    expected.parent.mkdir(parents=True)
    _write(expected, user(TEXT, cwd=str(real)), *turn_end())
    assert set(_match(db, expected)) == {row.input_id}


def test_resume_row_on_a_symlinked_cwd_falls_back_to_the_resolved_path(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link, real = _symlinked_cwd(tmp_path)
    cfg = tmp_path / "cfg"
    monkeypatch.setattr(input_ledger, "_session_transcript", lambda _spec: None)
    spec = input_ledger.LedgerSpec("resume_prompt", "caller", SID, str(cfg), str(link))
    assert input_ledger.record_exec(_factory(db), spec, TEXT, now_ms=T0)
    (row,) = _rows(db)
    assert row.cwd == str(real)
    assert row.transcript_path == str(
        cfg / "projects" / cb.project_dir_name(str(real)) / f"{SID}.jsonl"
    )


def test_paste_row_on_a_symlinked_cwd_stores_the_resolved_cwd(
    db: Path, tmp_path: Path, transcript: Path
) -> None:
    link, real = _symlinked_cwd(tmp_path)
    _record(db, transcript, cwd=str(link))
    (row,) = _rows(db)
    assert (row.cwd, row.transcript_path) == (str(real), str(transcript))


def test_expected_transcript_path_resolves_a_symlinked_cwd(tmp_path: Path) -> None:
    link, real = _symlinked_cwd(tmp_path)
    path = cb.expected_transcript_path(str(tmp_path), str(link), SID)
    assert path == tmp_path / "projects" / cb.project_dir_name(str(real)) / f"{SID}.jsonl"
    assert cb.resolve_cwd("") == ""


def test_record_unbound_row(db: Path) -> None:
    _record(db, transcript=None, session_id="")
    (row,) = _rows(db, "")
    assert (row.session_id, row.transcript_path, row.anchor_offset) == ("", "", 0)


def test_locked_db_writes_a_0600_gap_line_and_returns_none(db: Path, tmp_path: Path) -> None:
    store = Store(db)
    store.conn.execute("PRAGMA busy_timeout=0")
    blocker = sqlite3.connect(db)
    blocker.execute("BEGIN IMMEDIATE")
    gaps = tmp_path / "home" / "gaps.jsonl"
    try:
        got = input_ledger.record(
            lambda: store,
            session_id=SID,
            kind="start_job",
            origin="caller",
            text=TEXT,
            config_dir="",
            cwd="/repo",
            transcript=None,
            paste=False,
            now_ms=T0,
            gaps=gaps,
        )
    finally:
        blocker.rollback()
        blocker.close()
    assert got is None
    assert stat.S_IMODE(gaps.stat().st_mode) == 0o600
    (line,) = gaps.read_text().splitlines()
    assert json.loads(line) == {
        "at_ms": T0,
        "session_id": SID,
        "kind": "start_job",
        "error_class": "OperationalError",
    }
    assert TEXT not in gaps.read_text()
    assert input_ledger.read_gaps(gaps, session_id=SID, since_ms=T0, until_ms=T0 + 1) == [
        input_ledger.Gap(T0, SID, "start_job", "OperationalError")
    ]


def test_read_gaps_filters_window_and_session(tmp_path: Path) -> None:
    gaps = tmp_path / "gaps.jsonl"
    input_ledger.append_gap(SID, "send", "E", at_ms=10, path=gaps)
    input_ledger.append_gap(OTHER, "send", "E", at_ms=11, path=gaps)
    input_ledger.append_gap("", "limit_resume", "E", at_ms=12, path=gaps)
    input_ledger.append_gap(SID, "send", "E", at_ms=30, path=gaps)
    with open(gaps, "a", encoding="utf-8") as handle:
        handle.write("not json\n")
    got = input_ledger.read_gaps(gaps, session_id=SID, since_ms=0, until_ms=30)
    assert [(g.at_ms, g.session_id) for g in got] == [(10, SID), (12, "")]
    assert len(input_ledger.read_gaps(gaps)) == 4
    assert not input_ledger.read_gaps(tmp_path / "missing.jsonl")


def test_mark_failed_and_mark_matched_are_cas(db: Path, transcript: Path) -> None:
    first = _record(db, transcript)
    second = _record(db, transcript, at=T0 + 1)
    assert input_ledger.mark_failed(_factory(db), first, "nothing_typed", now_ms=T0 + 5)
    assert not input_ledger.mark_failed(_factory(db), first, "again")
    assert not input_ledger.mark_failed(_factory(db), None, "no row")
    match = input_ledger.Match(7, "u-1", "user")
    assert input_ledger.mark_matched(_factory(db), second, match, now_ms=T0 + 6)
    by_id = {r.input_id: r for r in _rows(db)}
    assert (by_id[first].state, by_id[first].reason) == ("failed", "nothing_typed")
    assert (by_id[second].state, by_id[second].line_uuid, by_id[second].matched_at) == (
        "matched",
        "u-1",
        T0 + 6,
    )


# ----------------------------------------------------------------------------- matching


def test_two_identical_texts_match_fifo(db: Path, transcript: Path) -> None:
    one = _record(db, transcript, at=T0)
    two = _record(db, transcript, at=T0 + 1)
    first, second = user(TEXT), user(TEXT)
    _write(transcript, first, *turn_end(), second)
    got = _match(db, transcript)
    assert got[one].line_uuid == first["uuid"]
    assert got[two].line_uuid == second["uuid"]
    assert got[one].line_offset < got[two].line_offset


def test_a_hand_typed_prompt_in_between_is_ignored(db: Path, transcript: Path) -> None:
    input_id = _record(db, transcript)
    mine = user(TEXT)
    _write(transcript, user("something typed by hand"), *turn_end(), mine)
    assert _match(db, transcript)[input_id].line_uuid == mine["uuid"]


def test_records_before_the_anchor_are_not_claimed(db: Path, transcript: Path) -> None:
    _write(transcript, user(TEXT))
    input_id = _record(db, transcript)
    assert input_id not in _match(db, transcript)


def test_enqueue_is_followed_to_its_consumer(db: Path, transcript: Path) -> None:
    input_id = _record(db, transcript)
    _write(transcript, enqueue(TEXT))
    assert input_id not in _match(db, transcript)  # no uuid yet: still pending
    consumer = user(TEXT)
    _write(transcript, *turn_end(), dequeue(), consumer)
    got = _match(db, transcript)[input_id]
    assert (got.line_uuid, got.line_kind) == (consumer["uuid"], "user")


def test_enqueue_absorbed_mid_turn_matches_the_queued_command(db: Path, transcript: Path) -> None:
    input_id = _record(db, transcript)
    absorbed = queued(TEXT)
    _write(transcript, enqueue(TEXT), absorbed)
    got = _match(db, transcript)[input_id]
    assert (got.line_uuid, got.line_kind) == (absorbed["uuid"], "attachment/queued_command")


def test_a_pasted_content_wrapped_record_matches(db: Path, transcript: Path) -> None:
    text = "line one of a long paste\nline two of it"
    input_id = _record(db, transcript, text)
    rec = user(f'<pasted_content id="p1">\n{text}\n</pasted_content id="p1">')
    _write(transcript, rec)
    assert _match(db, transcript)[input_id].line_uuid == rec["uuid"]


def test_the_one_line_applescript_form_matches(db: Path, transcript: Path) -> None:
    input_id = _record(db, transcript, "line one\nline two")
    rec = user("line one line two")
    _write(transcript, rec)
    assert _match(db, transcript)[input_id].line_uuid == rec["uuid"]


def test_a_stray_prefix_matches_with_its_length(db: Path, transcript: Path) -> None:
    input_id = _record(db, transcript)
    rec = user("y" + TEXT)
    _write(transcript, rec)
    got = _match(db, transcript)[input_id]
    assert (got.line_uuid, got.stray_len) == (rec["uuid"], 1)


def test_advance_persists_matches_and_keeps_them_claimed(db: Path, transcript: Path) -> None:
    one = _record(db, transcript, at=T0)
    first = user(TEXT)
    _write(transcript, first)
    with Store(db) as store:
        assert input_ledger.advance_session(store, SID, T0 + 10) == [(one, "matched")]
        assert not input_ledger.advance_session(store, SID, T0 + 11)  # nothing pending
    two = _record(db, transcript, at=T0 + 20)
    second = user(TEXT)
    _write(transcript, *turn_end(), second)
    with Store(db) as store:
        assert input_ledger.advance_session(store, SID, T0 + 30) == [(two, "matched")]
    by_id = {r.input_id: r for r in _rows(db)}
    assert (by_id[one].line_uuid, by_id[one].line_kind, by_id[one].matched_at) == (
        first["uuid"],
        "user",
        T0 + 10,
    )
    assert by_id[two].line_uuid == second["uuid"]


def test_unmatched_after_the_window(db: Path, transcript: Path) -> None:
    input_id = _record(db, transcript)
    with Store(db) as store:
        assert not input_ledger.advance_session(store, SID, T0 + 6 * HOUR_MS - 1)
        assert input_ledger.advance_session(store, SID, T0 + 6 * HOUR_MS) == [
            (input_id, "unmatched")
        ]
    (row,) = _rows(db)
    assert (row.state, row.reason) == ("unmatched", "match_window")


def test_unbound_and_missing_transcript_rows_only_expire(db: Path, tmp_path: Path) -> None:
    unbound = _record(db, transcript=None, session_id="")
    gone = _record(db, transcript=tmp_path / "never-written.jsonl")
    with Store(db) as store:
        assert not input_ledger.advance_session(store, "", T0 + 1)
        assert not input_ledger.advance_session(store, SID, T0 + 1)
        assert input_ledger.advance_session(store, "", T0 + 6 * HOUR_MS) == [(unbound, "unmatched")]
        assert input_ledger.advance_session(store, SID, T0 + 6 * HOUR_MS) == [(gone, "unmatched")]


def test_a_fresh_anchor_matches_once_the_transcript_appears(db: Path, tmp_path: Path) -> None:
    path = tmp_path / "fresh.jsonl"
    input_id = _record(db, transcript=path, paste=False)
    first = user(TEXT)
    _write(path, first)
    with Store(db) as store:
        assert input_ledger.advance_session(store, SID, T0 + 1) == [(input_id, "matched")]


def test_a_replaced_transcript_does_not_match(db: Path, transcript: Path) -> None:
    _record(db, transcript)
    anchor = transcript.stat().st_size
    transcript.rename(transcript.with_suffix(".old"))  # the old inode stays in use
    while not transcript.exists() or transcript.stat().st_size <= anchor:
        _write(transcript, user("filler past the old anchor"))
    _write(transcript, user(TEXT))  # would match if the inode were not checked
    with Store(db) as store:
        assert not input_ledger.advance_session(store, SID, T0 + 1)
