"""Tests for core.build_rows (done-age filtering)."""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from command_center import usage
from command_center.core import Row, build_rows
from command_center.models import (
    CODEX_WORKFLOW_NAME,
    LiveSession,
    Session,
    Status,
    TranscriptScan,
    now_ms,
)
from command_center.store import Store

_DAY = 86_400_000

# Generic repo-tree root for the category-grouping fixtures (no personal anchors).
_BASE = "/repo-root"


@pytest.fixture(autouse=True)
def _repo_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point category grouping at the generic ``_BASE`` tree via ``$GIT_BASE``."""
    monkeypatch.setenv("GIT_BASE", _BASE)


class _StubAdapter:
    name = "claude"

    def discover(self) -> list[LiveSession]:
        return []

    def last_activity_ms(self, live: LiveSession) -> int:
        return 0

    def is_oneshot_headless(self, cwd: str, session_id: str) -> bool:
        return False

    def is_halted(self, cwd: str, session_id: str) -> bool:
        return False

    def claude_version(self, cwd: str, session_id: str) -> str | None:
        return None

    def probe(self) -> bool:
        return True


class _BgAdapter(_StubAdapter):
    """Reports one idle live session that has a running background task."""

    def __init__(self, session_id: str, cwd: str) -> None:
        self._live = LiveSession(
            pid=4321, session_id=session_id, cwd=cwd, raw_status="idle", alive=True
        )

    def discover(self) -> list[LiveSession]:
        return [self._live]

    def has_background_task(self, pid: int) -> bool:
        return True


class _LiveAdapter(_StubAdapter):
    def __init__(
        self,
        session_id: str,
        cwd: str = "/repo",
        *,
        raw_status: str = "idle",
        uses_codex: bool = False,
        halted: bool = False,
        background: bool = False,
    ) -> None:
        self._live = LiveSession(
            pid=4321,
            session_id=session_id,
            cwd=cwd,
            raw_status=raw_status,
            alive=True,
        )
        self._uses_codex = uses_codex
        self._halted = halted
        self._background = background

    def discover(self) -> list[LiveSession]:
        return [self._live]

    def is_halted(self, cwd: str, session_id: str) -> bool:
        return self._halted

    def has_background_task(self, pid: int) -> bool:
        return self._background

    def uses_codex_workflow(self, cwd: str, session_id: str) -> bool:
        return self._uses_codex


def _codex_usage(pct: float, reset_delta: int = 3600) -> usage.Usage:
    now = int(time.time())
    return usage.Usage(now, usage.Window(pct, now + reset_delta), None)


def test_reconcile_marks_snoozed_when_background_task_live(tmp_path: Path) -> None:
    from command_center.core import reconcile

    store = Store(tmp_path / "s.db")
    store.ensure("bg")
    store.update_fields("bg", cwd="/x", status=Status.IDLE.value)
    reconcile(store, _BgAdapter("bg", "/x"))
    session = store.get("bg")
    assert session is not None
    assert session.status == Status.SNOOZED.value  # idle + live bg task → 💤
    store.close()


def test_build_rows_exposes_codex_workflow_from_job_type_and_adapter(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    store.ensure("draft-launched", cwd="/repo")
    store.update_fields("draft-launched", job_type="codex")
    store.ensure("manual", cwd="/repo")
    store.ensure("plain", cwd="/repo")

    class _WorkflowAdapter(_StubAdapter):
        def uses_codex_workflow(self, cwd: str, session_id: str) -> bool:
            return session_id == "manual"

    rows = {r.session.session_id: r for r in build_rows(store, _WorkflowAdapter())}
    assert rows["draft-launched"].uses_codex_workflow is True
    assert rows["manual"].uses_codex_workflow is True
    assert rows["plain"].uses_codex_workflow is False
    store.close()


def test_reconcile_marks_waiting_codex_when_idle_workflow_and_usage_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.core import reconcile

    monkeypatch.setattr(usage, "read_codex_usage", lambda: _codex_usage(100.0))
    store = Store(tmp_path / "s.db")
    store.ensure("codex", cwd="/repo")
    reconcile(store, _LiveAdapter("codex", uses_codex=True))
    session = store.get("codex")
    assert session is not None
    assert session.status == Status.WAITING_CODEX.value
    store.close()


def test_reconcile_waiting_codex_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.core import reconcile

    def status_for(
        sid: str,
        *,
        pct: float = 100.0,
        uses_codex: bool = True,
        raw_status: str = "idle",
        halted: bool = False,
        background: bool = False,
    ) -> Status:
        monkeypatch.setattr(usage, "read_codex_usage", lambda: _codex_usage(pct))
        store = Store(tmp_path / f"{sid}.db")
        store.ensure(sid, cwd="/repo")
        reconcile(
            store,
            _LiveAdapter(
                sid,
                uses_codex=uses_codex,
                raw_status=raw_status,
                halted=halted,
                background=background,
            ),
        )
        got = store.get(sid)
        store.close()
        assert got is not None
        return Status(got.status)

    assert status_for("healthy", pct=20.0) is Status.IDLE
    assert status_for("plain", uses_codex=False) is Status.IDLE
    assert status_for("working", raw_status="busy") is Status.WORKING
    assert status_for("waiting", raw_status="waiting") is Status.WAITING_INPUT
    assert status_for("halted", halted=True) is Status.HALTED
    assert status_for("bg", background=True) is Status.SNOOZED


def test_done_age_filter(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    store.ensure("recent")
    store.update_fields("recent", done=True, status=Status.DONE.value, done_at=now_ms() - _DAY)
    store.ensure("old")
    store.update_fields("old", done=True, status=Status.DONE.value, done_at=now_ms() - 10 * _DAY)
    store.ensure("active")
    store.update_fields("active", status=Status.IDLE.value)

    adapter = _StubAdapter()
    all_ids = {r.session.session_id for r in build_rows(store, adapter, done_max_age_days=0)}
    assert {"recent", "old", "active"} <= all_ids  # 0 = show every done session

    recent_ids = {r.session.session_id for r in build_rows(store, adapter, done_max_age_days=3)}
    assert "recent" in recent_ids
    assert "active" in recent_ids
    assert "old" not in recent_ids  # finished > 3 days ago is hidden
    store.close()


def test_done_open_session_stays_active_only_closed_is_finished(tmp_path: Path) -> None:
    """A done session that is still open stays in the active list (not FINISHED) and is
    never hidden by the finished filter; only a done session whose process is gone is
    treated as finished."""
    repo = f"{_BASE}/sdsc"
    store = Store(tmp_path / "s.db")
    store.ensure("done-open", cwd=f"{repo}/repo-x")
    store.update_fields("done-open", done=True, status=Status.DONE.value, done_at=now_ms())
    store.ensure("done-closed", cwd=f"{repo}/repo-y")
    store.update_fields("done-closed", done=True, status=Status.DONE.value, done_at=now_ms())

    class _OneLiveAdapter(_StubAdapter):
        def discover(self) -> list[LiveSession]:
            # done-open is still registered & alive; done-closed is not.
            return [LiveSession(pid=1, session_id="done-open", cwd=f"{repo}/repo-x", alive=True)]

    adapter = _OneLiveAdapter()
    # Finished hidden (TUI default): the open done session survives, the closed one is gone.
    hidden = {r.session.session_id: r for r in build_rows(store, adapter, include_done=False)}
    assert "done-open" in hidden
    assert hidden["done-open"].is_open is True
    assert hidden["done-open"].is_finished is False  # stays in place, shown with a ✓
    assert "done-closed" not in hidden  # only the closed one is filtered out

    # Finished shown: the closed one returns and is the only one classified finished.
    shown = {r.session.session_id: r for r in build_rows(store, adapter, include_done=True)}
    assert shown["done-closed"].is_finished is True
    assert shown["done-open"].is_finished is False
    store.close()


def test_reconcile_heals_done_session_stamped_parked(tmp_path: Path) -> None:
    """A done session that a later close stamped PARKED is healed back to DONE by
    reconcile, so it classifies as finished (sinks to the FINISHED section) instead
    of lingering in the active list as a parked row."""
    store = Store(tmp_path / "s.db")
    store.ensure("done-parked")
    store.update_fields("done-parked", done=True, status=Status.PARKED.value, done_at=now_ms())

    rows = {r.session.session_id: r for r in build_rows(store, _StubAdapter())}
    got = store.get("done-parked")
    assert got is not None and got.status == Status.DONE.value  # healed by reconcile
    assert rows["done-parked"].status is Status.DONE
    assert rows["done-parked"].is_finished is True  # FINISHED bucket, not active
    store.close()


def test_reconcile_stamps_closed_at_on_park_and_clears_on_reopen(tmp_path: Path) -> None:
    """closed_at records WHEN the process went away: stamped once on the live→gone
    transition, left untouched while the row stays parked, and cleared back to 0
    the moment the session is observed live again (resume/reopen)."""
    from command_center.core import reconcile  # pylint: disable=import-outside-toplevel

    store = Store(tmp_path / "s.db")
    store.ensure("s1")
    store.update_fields("s1", cwd="/x", status=Status.IDLE.value)
    before = now_ms()
    reconcile(store, _StubAdapter())  # process gone: idle → parked, stamp the close
    session = store.get("s1")
    assert session is not None
    assert session.status == Status.PARKED.value
    assert session.closed_at >= before
    stamp = session.closed_at
    reconcile(store, _StubAdapter())  # already parked: the stamp is not re-written
    session = store.get("s1")
    assert session is not None and session.closed_at == stamp
    reconcile(store, _LiveAdapter("s1", "/x"))  # reopened: the stamp is cleared
    session = store.get("s1")
    assert session is not None and session.closed_at == 0
    store.close()


def test_reconcile_stamps_claude_version(tmp_path: Path) -> None:
    """reconcile() records the live session's Claude Code version, but a read miss
    (None) never clobbers a previously-stored value."""
    from command_center.core import reconcile  # pylint: disable=import-outside-toplevel

    store = Store(tmp_path / "s.db")

    class _VersionAdapter(_StubAdapter):
        def __init__(self, version: str | None) -> None:
            self._version = version

        def discover(self) -> list[LiveSession]:
            return [LiveSession(pid=1, session_id="s1", cwd="/repo", alive=True)]

        def claude_version(self, cwd: str, session_id: str) -> str | None:
            return self._version

    reconcile(store, _VersionAdapter("2.1.193"))
    assert store.get("s1").version == "2.1.193"  # type: ignore[union-attr]

    # A later pass that fails to read the version must keep the stored one.
    reconcile(store, _VersionAdapter(None))
    assert store.get("s1").version == "2.1.193"  # type: ignore[union-attr]
    store.close()


class _ModelEffortAdapter(_StubAdapter):
    """One live idle session that reports an OBSERVED model + an optional --effort flag."""

    def __init__(
        self, sid: str, cwd: str = "/repo", *, model: str | None = None, effort: str | None = None
    ) -> None:
        self._live = LiveSession(pid=4321, session_id=sid, cwd=cwd, raw_status="idle", alive=True)
        self._model = model
        self._effort = effort

    def discover(self) -> list[LiveSession]:
        return [self._live]

    def observed_model(self, cwd: str, session_id: str) -> str | None:
        return self._model

    def session_effort(self, pid: int) -> str | None:
        return self._effort


def test_reconcile_persists_observed_model_and_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.core import reconcile

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))

    # (a) an explicit --effort flag is authoritative; the model is reverse-mapped to its
    # ccc choice label.
    store = Store(tmp_path / "a.db")
    store.ensure("a", cwd="/repo")
    reconcile(store, _ModelEffortAdapter("a", model="claude-fable-5", effort="xhigh"))
    got = store.get("a")
    assert got is not None and got.model == "fable-5" and got.effort == "xhigh"
    store.close()

    # (b) no flag + a settings.json effortLevel → fill the empty effort ONCE from that default.
    (home / "settings.json").write_text('{"effortLevel": "high"}', encoding="utf-8")
    store = Store(tmp_path / "b.db")
    store.ensure("b", cwd="/repo")
    reconcile(store, _ModelEffortAdapter("b", model="claude-opus-4-8", effort=None))
    got = store.get("b")
    assert got is not None and got.model == "opus-4.8" and got.effort == "high"
    store.close()

    # (c) no flag but effort already set → the settings default must NOT backfill a stored value.
    store = Store(tmp_path / "c.db")
    store.ensure("c", cwd="/repo")
    store.update_fields("c", effort="low")
    reconcile(store, _ModelEffortAdapter("c", model="claude-fable-5", effort=None))
    got = store.get("c")
    assert got is not None and got.effort == "low"  # preserved, never overwritten
    store.close()


def test_reconcile_persists_observed_model_for_parked_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked (non-live) session's model still updates from its transcript; effort is not
    touched, and an unchanged pass writes nothing (byte-stable)."""
    from command_center.core import reconcile

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    class _ParkedModelAdapter(_StubAdapter):
        # No live sessions (discover → []); reports an observed model for the parked row.
        def observed_model(self, cwd: str, session_id: str) -> str | None:
            return "claude-fable-5"

    store = Store(tmp_path / "s.db")
    store.ensure("p", cwd="/repo")
    store.update_fields("p", status=Status.PARKED.value)
    reconcile(store, _ParkedModelAdapter())
    got = store.get("p")
    assert got is not None and got.model == "fable-5"  # captured from the transcript
    assert got.effort == ""  # never captured for a non-live session

    # A second, unchanged pass must not write (model already stored, status already parked).
    calls: list[str] = []
    orig = store.update_fields

    def _spy(session_id: str, **fields: object) -> None:
        calls.append(session_id)
        orig(session_id, **fields)

    monkeypatch.setattr(store, "update_fields", _spy)
    reconcile(store, _ParkedModelAdapter())
    assert calls == []  # byte-stable: nothing changed → no store write
    store.close()


class _ScanAdapter(_StubAdapter):
    """Stub with the ``scan_transcript`` capability, recording what it was handed.

    Mimics the real contract: given a *prior* row (the file is unchanged) it returns that
    SAME object, so the caller can tell "nothing to persist" by identity.
    """

    def __init__(
        self, live: str | None = None, *, model: str = "claude-fable-5", codex: bool = False
    ) -> None:
        self._live = (
            LiveSession(pid=4321, session_id=live, cwd="/repo", raw_status="idle", alive=True)
            if live
            else None
        )
        self._model = model
        self._codex = codex
        self.calls: list[str] = []
        self.priors: list[TranscriptScan | None] = []

    def discover(self) -> list[LiveSession]:
        return [self._live] if self._live is not None else []

    def scan_transcript(
        self, cwd: str, session_id: str, prior: TranscriptScan | None
    ) -> TranscriptScan | None:
        self.calls.append(session_id)
        self.priors.append(prior)
        if prior is not None:
            return prior  # unchanged transcript → the caller's own object back
        return TranscriptScan(
            session_id=session_id,
            path=f"/transcripts/{session_id}.jsonl",
            mtime_ns=1_700_000_000_000_000_000,
            size=4096,
            model=self._model,
            codex=self._codex,
            codex_scanned_to=4096,
            scanned_at=1_700_000_000_000,
        )


def test_reconcile_persists_transcript_scans_and_reuses_them_next_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan rows are the cross-PROCESS cache: pass one scans every non-draft session
    and persists what it read; the next process hands those rows straight back to the
    adapter, which recognises the file as untouched and returns them unchanged — so
    nothing is re-read and nothing is re-written."""
    from command_center.core import reconcile  # pylint: disable=import-outside-toplevel

    db = tmp_path / "s.db"
    store = Store(db)
    store.ensure("parked", cwd="/repo")
    store.create_draft("job", "/repo", "Ship the thing")  # a draft has no transcript
    adapter = _ScanAdapter(live="live1", codex=True)

    scans = reconcile(store, adapter)
    assert adapter.calls == ["live1", "parked"]  # the draft is never scanned
    assert adapter.priors == [None, None]  # nothing persisted yet
    assert set(scans) == {"live1", "parked"}
    stored = store.transcript_scans()
    assert set(stored) == {"live1", "parked"}
    assert stored["parked"] == scans["parked"]
    for sid in ("live1", "parked"):
        session = store.get(sid)
        assert session is not None and session.model == "fable-5"  # model_label of the raw id
    store.close()

    # A FRESH process (new Store on the same file) starts from the persisted rows.
    store = Store(db)
    adapter = _ScanAdapter(live="live1")
    before = store.transcript_scans()
    batches: list[list[TranscriptScan]] = []
    orig = store.put_transcript_scans

    def _spy(scans_in: Iterable[TranscriptScan]) -> None:
        rows = list(scans_in)
        batches.append(rows)
        orig(rows)

    monkeypatch.setattr(store, "put_transcript_scans", _spy)
    reconcile(store, adapter)
    assert adapter.priors == [before["live1"], before["parked"]]
    assert batches == [[]]  # nothing dirty → the batch write is a no-op
    assert store.transcript_scans() == before  # byte-stable: same rows, same scanned_at
    store.close()


def test_build_rows_without_reconcile_uses_the_persisted_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reconcile_first=False`` is the quick first paint: stored rows plus persisted
    transcript facts, with no reconcile, no transcript read and no usage read."""

    def _no_usage() -> usage.Usage | None:
        raise AssertionError("the quick paint must not read the Codex usage snapshot")

    monkeypatch.setattr(usage, "read_codex_usage", _no_usage)
    store = Store(tmp_path / "s.db")
    store.ensure("codexrow", cwd="/repo")
    store.ensure("plain", cwd="/repo")
    store.put_transcript_scans(
        [
            TranscriptScan("codexrow", "/t/codexrow.jsonl", 1, 2, "claude-fable-5", True, 2, 5),
            TranscriptScan("plain", "/t/plain.jsonl", 1, 2, "claude-fable-5", False, 2, 5),
        ]
    )
    adapter = _ScanAdapter()

    rows = {r.session.session_id: r for r in build_rows(store, adapter, reconcile_first=False)}
    assert adapter.calls == []  # no transcript touched at all
    assert rows["codexrow"].uses_codex_workflow is True  # read off the persisted scan
    assert rows["plain"].uses_codex_workflow is False
    session = store.get("plain")
    assert session is not None and session.status == Status.IDLE.value  # never reconciled
    store.close()


class _CountingAdapter(_StubAdapter):
    """A stub whose registry reads are counted (``discover`` calls)."""

    def __init__(self, live: list[LiveSession] | None = None) -> None:
        self.discovers = 0
        self._live = live or []

    def discover(self) -> list[LiveSession]:
        self.discovers += 1
        return list(self._live)


def _count_list_sessions(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap ``Store.list_sessions`` with a call counter; returns the mutable counter."""
    calls = {"n": 0}
    orig = Store.list_sessions

    def _counted(self: Store, include_archived: bool = False) -> list[Session]:
        calls["n"] += 1
        return orig(self, include_archived)

    monkeypatch.setattr(Store, "list_sessions", _counted)
    return calls


def test_build_rows_reads_the_registry_and_the_sessions_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One registry read and one session read per build.

    ``build_rows`` used to reconcile (which read both) and then read BOTH again for the
    render — 88 ms of duplicated `list_sessions` per refresh at 639 rows, plus a second
    registry scan. It now renders the ReconcilePass it just made.
    """
    store = Store(tmp_path / "s.db")
    store.ensure("a", cwd="/repo")
    store.ensure("b", cwd="/repo")
    adapter = _CountingAdapter()
    calls = _count_list_sessions(monkeypatch)

    rows = build_rows(store, adapter)
    assert {r.session.session_id for r in rows} == {"a", "b"}
    assert calls["n"] == 1
    assert adapter.discovers == 1

    # And again for a second build (per-build, not a one-off memo).
    build_rows(store, adapter)
    assert calls["n"] == 2
    assert adapter.discovers == 2
    store.close()


def test_build_rows_renders_the_row_the_same_pass_just_parked(tmp_path: Path) -> None:
    """The refreshed touched row is what build_rows sees: an idle non-live session is
    PARKED with a close stamp in the SAME build that parked it (the pre-write snapshot
    would have shown it as still idle, one refresh behind)."""
    store = Store(tmp_path / "s.db")
    store.ensure("gone", cwd="/repo")
    store.update_fields("gone", status=Status.IDLE.value)

    rows = {r.session.session_id: r for r in build_rows(store, _StubAdapter())}
    assert rows["gone"].status is Status.PARKED
    assert rows["gone"].session.status == Status.PARKED.value
    assert rows["gone"].session.closed_at > 0
    store.close()


def test_build_rows_includes_a_live_session_the_store_had_never_seen(tmp_path: Path) -> None:
    """The session snapshot is taken AFTER the live loop, so a brand-new live session the
    pass itself inserted is rendered by that same build."""
    store = Store(tmp_path / "s.db")
    assert store.list_sessions() == []

    rows = {r.session.session_id: r for r in build_rows(store, _LiveAdapter("newbie", "/repo"))}
    assert set(rows) == {"newbie"}
    assert rows["newbie"].is_open is True
    store.close()


def test_build_rows_snapshot_is_the_passes_consistency_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row another process inserts AFTER this pass's snapshot shows up one build later.

    That window is the documented consistency boundary of ReconcilePass.sessions — the
    exact same class of window the old second ``list_sessions()`` had, for one read
    instead of two.
    """
    db = tmp_path / "s.db"
    store = Store(db)
    store.ensure("known", cwd="/repo")

    orig = Store.list_sessions
    armed = {"yes": True}

    def _racing(self: Store, include_archived: bool = False) -> list[Session]:
        rows = orig(self, include_archived)
        if armed["yes"]:  # a peer ccc process inserts a row right after our snapshot
            armed["yes"] = False
            with Store(db) as peer:
                peer.ensure("late", cwd="/repo")
        return rows

    monkeypatch.setattr(Store, "list_sessions", _racing)
    first = {r.session.session_id for r in build_rows(store, _StubAdapter())}
    assert first == {"known"}  # "late" landed after the snapshot → not in THIS build

    monkeypatch.setattr(Store, "list_sessions", orig)
    second = {r.session.session_id for r in build_rows(store, _StubAdapter())}
    assert second == {"known", "late"}  # the next pass sees it
    store.close()


def test_build_rows_accepts_a_codex_usage_snapshot_from_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TUI worker already read the Codex snapshot; handing it in must not read it
    again (and the default path still calls ``read_codex_usage()`` with no arguments)."""

    def _no_usage() -> usage.Usage | None:
        raise AssertionError("build_rows must not re-read a snapshot it was handed")

    monkeypatch.setattr(usage, "read_codex_usage", _no_usage)
    store = Store(tmp_path / "s.db")
    store.ensure("s1", cwd="/repo")
    store.update_fields("s1", job_type="codex")

    rows = {
        r.session.session_id: r
        for r in build_rows(store, _StubAdapter(), codex_usage=_codex_usage(100.0))
    }
    assert rows["s1"].uses_codex_workflow is True
    assert rows["s1"].codex_reset_label == "5h"  # the handed-in snapshot really was used
    store.close()


def test_headless_leak_ids_reads_the_persisted_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prune/daemon classification comes off the persisted ``headless`` fact.

    Probing every transcript cost 0.52 s per pass. The first pass fills the column (for
    ARCHIVED rows too — reconcile never sees those); the next one opens no file at all.
    """
    from command_center.adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel
    from command_center.adapters import (
        claude as claude_mod,  # pylint: disable=import-outside-toplevel
    )
    from command_center.core import headless_leak_ids  # pylint: disable=import-outside-toplevel

    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    (proj / "junk.jsonl").write_text('{"type": "queue-operation"}\n', encoding="utf-8")
    (proj / "real.jsonl").write_text('{"type": "last-prompt"}\n', encoding="utf-8")
    (proj / "shelved.jsonl").write_text('{"type": "queue-operation"}\n', encoding="utf-8")

    store = Store(tmp_path / "s.db")
    for sid in ("junk", "real", "shelved", "live"):
        store.ensure(sid, cwd="/repo")
    store.update_fields("shelved", archived=True)
    adapter = ClaudeAdapter(claude_home=tmp_path)

    assert headless_leak_ids(store, adapter, {"live"}) == {"junk", "shelved"}
    persisted = store.transcript_scans()
    assert persisted["junk"].headless is True
    assert persisted["real"].headless is False
    assert persisted["shelved"].headless is True  # archived rows get their row here
    assert "live" not in persisted  # live ids are skipped entirely

    # Second pass: the fact is known and the files are untouched → nothing is opened.
    def _boom(*_args: object, **_kwargs: object) -> bool | None:
        raise AssertionError("a persisted headless fact must not be re-probed")

    monkeypatch.setattr(claude_mod, "first_record_is_queue_op", _boom)
    assert headless_leak_ids(store, adapter, {"live"}) == {"junk", "shelved"}
    assert store.transcript_scans() == persisted  # byte-stable: no rewrite either
    store.close()


def test_headless_leak_ids_falls_back_without_the_scan_capability(tmp_path: Path) -> None:
    """A stub adapter with no ``scan_transcript`` keeps the legacy per-session probe."""
    from command_center.core import headless_leak_ids  # pylint: disable=import-outside-toplevel

    class _HeadlessStub(_StubAdapter):
        def is_oneshot_headless(self, cwd: str, session_id: str) -> bool:
            return session_id == "junk"

    store = Store(tmp_path / "s.db")
    for sid in ("junk", "real", "live"):
        store.ensure(sid, cwd="/repo")
    assert headless_leak_ids(store, _HeadlessStub(), {"live"}) == {"junk"}
    assert store.transcript_scans() == {}  # no scan rows without the capability
    store.close()


def test_transcript_facts_persists_nothing_on_a_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript read failure is not a fact: nothing is persisted and the next pass
    retries (the readers now RAISE OSError instead of reporting "no model")."""
    from command_center import core  # pylint: disable=import-outside-toplevel
    from command_center.adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel
    from command_center.adapters import (
        claude as claude_mod,  # pylint: disable=import-outside-toplevel
    )

    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    (proj / "sid.jsonl").write_text(
        '{"type": "assistant", "message": {"role": "assistant", "model": "claude-fable-5", '
        '"content": [{"type": "text", "text": "x"}]}}\n',
        encoding="utf-8",
    )
    store = Store(tmp_path / "s.db")
    session = store.ensure("sid", cwd="/repo")
    adapter = ClaudeAdapter(claude_home=tmp_path)

    real = claude_mod.last_model_in_file
    failures = {"left": 1}

    def _flaky(path: Path, chunk_size: int = 65536) -> str | None:
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("simulated EIO")
        return real(path, chunk_size)

    monkeypatch.setattr(claude_mod, "last_model_in_file", _flaky)
    scans: dict[str, TranscriptScan] = {}
    dirty: list[TranscriptScan] = []
    assert core._transcript_facts(adapter, session, scans, dirty) == ("", False)  # pylint: disable=protected-access
    assert dirty == [] and scans == {}  # nothing learned → nothing written

    assert core._transcript_facts(adapter, session, scans, dirty) == ("fable-5", False)  # pylint: disable=protected-access
    assert [row.model for row in dirty] == ["claude-fable-5"]


def test_transcript_facts_answers_from_the_prior_row_when_a_reread_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a row is known, a transient read failure — or a transcript that vanished —
    answers from that row (the one build_rows renders anyway), never "" / False, and still
    persists nothing: one pass must not disagree with its own rows."""
    from command_center import core  # pylint: disable=import-outside-toplevel
    from command_center.adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel
    from command_center.adapters import (
        claude as claude_mod,  # pylint: disable=import-outside-toplevel
    )

    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    path = proj / "sid.jsonl"
    path.write_text(
        '{"type": "user", "message": {"role": "user", "content": '
        f'"<command-name>/{CODEX_WORKFLOW_NAME}</command-name>"}}}}\n'
        '{"type": "assistant", "message": {"role": "assistant", "model": "claude-fable-5", '
        '"content": [{"type": "text", "text": "x"}]}}\n',
        encoding="utf-8",
    )
    store = Store(tmp_path / "s.db")
    session = store.ensure("sid", cwd="/repo")
    adapter = ClaudeAdapter(claude_home=tmp_path)
    scans: dict[str, TranscriptScan] = {}
    dirty: list[TranscriptScan] = []
    assert core._transcript_facts(adapter, session, scans, dirty) == ("fable-5", True)  # pylint: disable=protected-access
    prior = scans["sid"]
    dirty.clear()

    def _fail(path: Path, chunk_size: int = 65536) -> str | None:
        raise OSError("simulated EIO")

    monkeypatch.setattr(claude_mod, "last_model_in_file", _fail)
    with path.open("a", encoding="utf-8") as handle:  # identity changes → a real re-read
        handle.write('{"type": "user", "message": {"role": "user", "content": "more"}}\n')
    assert core._transcript_facts(adapter, session, scans, dirty) == ("fable-5", True)  # pylint: disable=protected-access
    assert dirty == [] and scans["sid"] is prior  # nothing learned → nothing written

    path.unlink()  # vanished: still the prior row, still nothing persisted
    assert core._transcript_facts(adapter, session, scans, dirty) == ("fable-5", True)  # pylint: disable=protected-access
    assert dirty == [] and scans["sid"] is prior
    store.close()


def test_category_rank() -> None:
    from command_center.core import _category_rank  # pylint: disable=import-outside-toplevel

    order = ("home", "infra", "llms", "sdsc")
    base = _BASE
    assert _category_rank(f"{base}/home/repo", order, base) == 0
    assert _category_rank(f"{base}/infra/repo", order, base) == 1
    assert _category_rank(f"{base}/sdsc/repo/sub", order, base) == 3
    assert _category_rank(f"{base}/unknowncat/repo", order, base) == 4  # unknown category → last
    assert _category_rank("/tmp/elsewhere", order, base) == 4  # outside the tree → last
    assert _category_rank("", order, base) == 4
    assert _category_rank(f"{base}/home/repo", order, "") == 4  # no configured tree → last


def _park_with_progress(store: Store, sid: str, cwd: str, done: int, total: int) -> None:
    store.ensure(sid, cwd=cwd)
    store.update_fields(sid, status=Status.PARKED.value)
    if total:
        store.set_subgoals(sid, [f"g{i}" for i in range(total)])
        for sub in store.list_subgoals(sid)[:done]:
            store.set_subgoal_checked(sub.id, True)


def test_parked_sort_by_folder_then_progress(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    base = _BASE
    _park_with_progress(store, "home-lo", f"{base}/home/p", 1, 4)  # home, 25%
    _park_with_progress(store, "home-hi", f"{base}/home/q", 3, 4)  # home, 75%
    _park_with_progress(store, "infra-mid", f"{base}/infra/r", 1, 2)  # infra, 50%
    _park_with_progress(store, "sdsc-full", f"{base}/sdsc/s", 2, 2)  # sdsc, 100%
    _park_with_progress(store, "outside", "/tmp/elsewhere", 5, 5)  # not under the tree → last

    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    # home first; within home most-progress first; infra and sdsc next; outside-tree last.
    assert order == ["home-hi", "home-lo", "infra-mid", "sdsc-full", "outside"]
    store.close()


def test_category_is_primary_aim_only_breaks_ties_within_it(tmp_path: Path) -> None:
    # Category is the PRIMARY key: a no-aim session in an earlier repo category
    # outranks an aim-defined session in a later one. AIM-first applies only WITHIN
    # a category, so a category never splits across an AIM / no-AIM divide.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("sdsc-aim", cwd=f"{base}/sdsc/s")  # last category, but HAS an aim
    store.update_fields("sdsc-aim", status=Status.PARKED.value, aim="ship it")
    store.ensure("infra-noaim", cwd=f"{base}/infra/r")  # first category, NO aim
    store.update_fields("infra-noaim", status=Status.PARKED.value)

    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    assert order == ["infra-noaim", "sdsc-aim"]  # earlier category wins over aim
    store.close()


def test_category_stays_contiguous_aim_first_within(tmp_path: Path) -> None:
    # Each category is one contiguous block (infra then sdsc), and within a block
    # the aim-defined session sorts first — so neither category appears twice.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("sdsc-aim", cwd=f"{base}/sdsc/s")
    store.update_fields("sdsc-aim", status=Status.PARKED.value, aim="a")
    store.ensure("infra-aim", cwd=f"{base}/infra/r")
    store.update_fields("infra-aim", status=Status.PARKED.value, aim="b")
    store.ensure("sdsc-noaim", cwd=f"{base}/sdsc/t")
    store.update_fields("sdsc-noaim", status=Status.PARKED.value)
    store.ensure("infra-noaim", cwd=f"{base}/infra/u")
    store.update_fields("infra-noaim", status=Status.PARKED.value)

    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    # infra block (aim-first), then sdsc block (aim-first) — categories never split.
    assert order == ["infra-aim", "infra-noaim", "sdsc-aim", "sdsc-noaim"]
    store.close()


def test_finished_sinks_to_bottom(tmp_path: Path) -> None:
    # A DONE session lands last even though it has an aim and a top-category folder.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("done-infra", cwd=f"{base}/infra/r")
    store.update_fields("done-infra", status=Status.DONE.value, done=True, aim="x")
    store.ensure("active-noaim", cwd=f"{base}/sdsc/s")
    store.update_fields("active-noaim", status=Status.PARKED.value)

    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    assert order == ["active-noaim", "done-infra"]  # FINISHED bucket is always last
    store.close()


def test_draft_jobs_bucket_between_active_and_finished(tmp_path: Path) -> None:
    # A future job (draft) sorts below active sessions and above FINISHED ones, and
    # is never flipped to PARKED by reconcile (it owns its own status until launched).
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("active", cwd=f"{base}/infra/r")
    store.update_fields("active", status=Status.PARKED.value)
    store.ensure("done", cwd=f"{base}/infra/r")
    store.update_fields("done", status=Status.DONE.value, done=True)
    store.create_draft("future", f"{base}/sdsc/zoho", "Migrate Zendesk tickets to Zoho")

    rows = {r.session.session_id: r for r in build_rows(store, _StubAdapter())}
    assert rows["future"].is_draft is True
    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    assert order == ["active", "future", "done"]  # active → FUTURE → FINISHED
    # reconcile must not have parked the draft (it has no live process).
    assert store.get("future").draft is True  # type: ignore[union-attr]
    store.close()


def test_build_rows_dedupes_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive guard: even if the store ever yielded the same id twice, build_rows
    must emit it once — no session id shown twice."""
    store = Store(tmp_path / "s.db")
    store.ensure("dup")
    store.update_fields("dup", status=Status.PARKED.value)
    dup = store.get("dup")
    monkeypatch.setattr(store, "list_sessions", lambda include_archived=False: [dup, dup])

    rows = build_rows(store, _StubAdapter())
    assert [r.session.session_id for r in rows] == ["dup"]
    store.close()


# ---------------------------------------------------------------------------
# dependency hoisting (deps.py + core._hoist_dependents)
# ---------------------------------------------------------------------------
def _rows_by_id(store: Store) -> dict[str, Row]:
    return {r.session.session_id: r for r in build_rows(store, _StubAdapter())}


def test_hoist_active_parent_draft_child(tmp_path: Path) -> None:
    # A draft child depending on an UNMET (parked) parent hoists directly under it.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("parent", cwd=f"{base}/infra/r")
    store.update_fields("parent", status=Status.PARKED.value, aim="build parent")
    store.create_draft("child", f"{base}/infra/r", "needs parent", depends_on="parent")

    rows = build_rows(store, _StubAdapter())
    order = [r.session.session_id for r in rows]
    assert order.index("child") == order.index("parent") + 1  # directly under the parent
    by_id = {r.session.session_id: r for r in rows}
    assert by_id["child"].dep_depth == 1
    assert by_id["child"].dep_state == "unmet"
    assert by_id["parent"].dep_depth == 0
    store.close()


def test_hoist_chain_nests_recursively(tmp_path: Path) -> None:
    # C (active) ← B (draft, depends C) ← A (draft, depends B): nested depths 0/1/2.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("c", cwd=f"{base}/infra/r")
    store.update_fields("c", status=Status.PARKED.value, aim="root job")
    store.create_draft("b", f"{base}/infra/r", "middle", depends_on="c")
    store.create_draft("a", f"{base}/infra/r", "leaf", depends_on="b")

    rows = build_rows(store, _StubAdapter())
    order = [r.session.session_id for r in rows]
    assert order == ["c", "b", "a"]
    by_id = {r.session.session_id: r for r in rows}
    assert (by_id["c"].dep_depth, by_id["b"].dep_depth, by_id["a"].dep_depth) == (0, 1, 2)
    store.close()


def test_hoist_children_keep_relative_order(tmp_path: Path) -> None:
    # Two draft children of one parent keep their sort order (newest-created first) under it.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("parent", cwd=f"{base}/infra/r")
    store.update_fields("parent", status=Status.PARKED.value, aim="parent")
    store.create_draft("older", f"{base}/infra/r", "older child", depends_on="parent")
    store.update_fields("older", created_at=1000)
    store.create_draft("newer", f"{base}/infra/r", "newer child", depends_on="parent")
    store.update_fields("newer", created_at=2000)

    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    assert order == ["parent", "newer", "older"]  # future bucket sorts newest-first
    store.close()


def test_two_cycle_degrades_to_permutation(tmp_path: Path) -> None:
    # a↔b mutual dependency: neither hoists (cycle), output is a permutation, both marked.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.create_draft("a", f"{base}/infra/r", "job a")
    store.create_draft("b", f"{base}/infra/r", "job b")
    store.update_fields("a", depends_on="b")
    store.update_fields("b", depends_on="a")

    rows = build_rows(store, _StubAdapter())
    order = [r.session.session_id for r in rows]
    assert sorted(order) == ["a", "b"]  # permutation: every row exactly once
    by_id = {r.session.session_id: r for r in rows}
    assert by_id["a"].dep_depth == 0 and by_id["b"].dep_depth == 0  # not hoisted
    assert by_id["a"].dep_state == "unmet" and by_id["b"].dep_state == "unmet"
    store.close()


def test_self_dependency_degrades(tmp_path: Path) -> None:
    # A job depending on itself: no hoist, still marked, present exactly once.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.create_draft("a", f"{base}/infra/r", "job a")
    store.update_fields("a", depends_on="a")

    rows = build_rows(store, _StubAdapter())
    assert [r.session.session_id for r in rows] == ["a"]
    assert rows[0].dep_depth == 0 and rows[0].dep_state == "unmet"
    store.close()


def test_parent_done_no_hoist_state_satisfied(tmp_path: Path) -> None:
    # A satisfied (done) parent: child is not hoisted and its marker state is satisfied.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("parent", cwd=f"{base}/infra/r")
    store.update_fields("parent", status=Status.DONE.value, done=True, aim="done")
    store.create_draft("child", f"{base}/infra/r", "needs parent", depends_on="parent")

    by_id = _rows_by_id(store)
    assert "child" in by_id
    child = by_id["child"]
    assert child.dep_depth == 0  # not hoisted (parent satisfied)
    assert child.dep_state == "satisfied"
    store.close()


def test_parent_cancelled_marker_no_hoist(tmp_path: Path) -> None:
    # An archived (cancelled) parent isn't visible; child shows the cancelled marker, no hoist.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("parent", cwd=f"{base}/infra/r")
    store.update_fields("parent", archived=True)
    store.create_draft("child", f"{base}/infra/r", "needs parent", depends_on="parent")

    by_id = _rows_by_id(store)
    child = by_id["child"]
    assert child.dep_depth == 0
    assert child.dep_state == "cancelled"
    store.close()


def test_parent_missing_marker_no_hoist(tmp_path: Path) -> None:
    # A dangling dependency (no such row) → missing marker, no hoist.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.create_draft("child", f"{base}/infra/r", "needs ghost", depends_on="ghost-uuid")

    by_id = _rows_by_id(store)
    child = by_id["child"]
    assert child.dep_depth == 0
    assert child.dep_state == "missing"
    store.close()


def test_done_child_never_hoists(tmp_path: Path) -> None:
    # A DONE child no longer waits on anything: even with an unmet parent it stays in its
    # own (FINISHED) position — dep_state is still computed for the detail/ls notes.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("parent", cwd=f"{base}/infra/r")
    store.update_fields("parent", status=Status.PARKED.value, aim="parent")
    store.ensure("child", cwd=f"{base}/infra/r")
    store.update_fields(
        "child", status=Status.DONE.value, done=True, aim="was waiting", depends_on="parent"
    )

    rows = build_rows(store, _StubAdapter())
    order = [r.session.session_id for r in rows]
    assert order == ["parent", "child"]  # child sank to FINISHED, not glued to the parent
    by_id = {r.session.session_id: r for r in rows}
    assert by_id["child"].dep_depth == 0
    assert by_id["child"].dep_state == "unmet"  # state still reported, marker/hoist gated off
    store.close()


def test_include_future_false_hides_hoisted_draft(tmp_path: Path) -> None:
    # With future jobs hidden, a hoisted draft child is excluded entirely.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("parent", cwd=f"{base}/infra/r")
    store.update_fields("parent", status=Status.PARKED.value, aim="parent")
    store.create_draft("child", f"{base}/infra/r", "needs parent", depends_on="parent")

    order = [r.session.session_id for r in build_rows(store, _StubAdapter(), include_future=False)]
    assert order == ["parent"]  # the draft child is hidden
    store.close()


def test_scheduled_drafts_sink_below_finished_soonest_first(tmp_path: Path) -> None:
    # A draft with a FIXED start_date leaves the FUTURE bucket and sinks to the very
    # bottom (below FINISHED), ordered soonest-date-first; undated drafts stay in FUTURE.
    store = Store(tmp_path / "s.db")
    base = _BASE
    store.ensure("active", cwd=f"{base}/infra/r")
    store.update_fields("active", status=Status.PARKED.value)
    store.ensure("done", cwd=f"{base}/infra/r")
    store.update_fields("done", status=Status.DONE.value, done=True)
    store.create_draft("future", f"{base}/sdsc/zoho", "Migrate Zendesk tickets to Zoho")
    store.create_draft("sched-late", f"{base}/home/a", "Revert mac", start_date="2036-09-01")
    store.create_draft("sched-soon", f"{base}/home/b", "FileVault", start_date="2036-08-11")

    order = [r.session.session_id for r in build_rows(store, _StubAdapter())]
    assert order == ["active", "future", "done", "sched-soon", "sched-late"]
    store.close()


def test_orphan_launched_ids_flags_only_never_turned_parked_rows(tmp_path: Path) -> None:
    """A dead-launched phantom (parked, no turn, no transcript) is flagged; real work isn't."""
    from command_center.core import orphan_launched_ids

    class _TxAdapter(_StubAdapter):
        """Adds the concrete-adapter ``transcript_path`` probe; reports a hit only for *have*."""

        def __init__(self, have: set[str]) -> None:
            self._have = have

        def transcript_path(
            self, cwd: str, session_id: str, config_dir: str | None = None
        ) -> Path | None:
            return Path(f"/x/{session_id}.jsonl") if session_id in self._have else None

    store = Store(tmp_path / "s.db")
    # dead-launched phantom: parked, prompt_count 0, inherited AIM, NO transcript → flagged.
    store.ensure("dead", cwd="/repo")
    store.update_fields("dead", aim="fix antennapod", status=Status.PARKED.value)
    # real parked session whose transcript was merely deleted (kept prompt_count) → spared.
    store.ensure("deleted_tx", cwd="/repo")
    store.update_fields("deleted_tx", aim="x", status=Status.PARKED.value, prompt_count=3)
    # real parked session WITH a transcript → spared.
    store.ensure("real", cwd="/repo")
    store.update_fields("real", status=Status.PARKED.value)
    # a FUTURE draft (no transcript by design) → spared.
    store.ensure("draft", cwd="/repo")
    store.update_fields("draft", aim="later", draft=True, status=Status.PARKED.value)
    # a freshly-created idle row still awaiting its first turn / scoring → spared (not parked).
    store.ensure("idle", cwd="/repo")
    store.update_fields("idle", aim="pending", status=Status.IDLE.value)
    # a live never-turned row → spared via live_ids.
    store.ensure("live", cwd="/repo")
    store.update_fields("live", status=Status.PARKED.value)

    got = orphan_launched_ids(store, _TxAdapter(have={"real"}), live_ids={"live"})
    assert got == {"dead"}
    store.close()
