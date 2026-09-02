"""Tests for the two-sided FUTURE-job ↔ Obsidian file reconciler (:mod:`futuresync`).

Hermetic: a tmp ``CLAUDE_HOME`` (store + flock + sidecar), a tmp ``$GIT_BASE`` and a tmp
vault. No Obsidian, no real ``ccc`` spawns (``CCC_INTERNAL`` + scoring/short-aim off).
"""

from __future__ import annotations

import fcntl
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from command_center import config, futuresync
from command_center.future_files import display_hash, job_filename, parse_job_file, serialize
from command_center.models import Session
from command_center.store import Store


@dataclass
class Env:
    store: Store
    cfg: config.Config
    git_base: Path
    vault: Path
    future_dir: Path
    pad: Path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress detached score-aim/short-aim spawns
    git_base = tmp_path / "git"
    (git_base / "home" / "ccc").mkdir(parents=True)
    (git_base / "sdsc" / "zoho").mkdir(parents=True)
    monkeypatch.setenv("GIT_BASE", str(git_base))
    vault = tmp_path / "vault"
    future_dir = vault / "01-llm-tasks" / "future"
    pad = vault / "01-llm-tasks" / "new-prompt.md"
    future_dir.mkdir(parents=True)
    cfg = config.Config(
        vault_root=str(vault),
        future_dir=str(future_dir),
        future_pad=str(pad),
        future_delete_grace_sec=600,
        aim_score_on_set=False,
        short_aim=False,
    )
    store = Store(tmp_path / "claude" / "command-center" / "state.db")
    yield Env(store, cfg, git_base, vault, future_dir, pad)
    store.close()


def _session(env: Env, session_id: str) -> Session:
    session = env.store.get(session_id)
    assert session is not None
    return session


def _abs(env: Env, session_id: str) -> Path:
    session = _session(env, session_id)
    assert session.future_file
    return futuresync._abs_path(env.cfg, session.future_file)


def _new_draft(env: Env, aim: str, *, repo: str = "home/ccc", prompt: str | None = None) -> str:
    session_id = str(uuid.uuid4())
    env.store.create_draft(session_id, str(env.git_base / repo), aim, prompt=prompt)
    return session_id


# ---------------------------------------------------------------------------
# bootstrap + echo suppression
# ---------------------------------------------------------------------------
def test_bootstrap_export_and_echo_suppression(env: Env) -> None:
    sid = _new_draft(env, "Fix the widget bug")
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.exported == [sid]

    path = _abs(env, sid)
    assert path.exists()
    assert path.name == job_filename(sid, "Fix the widget bug")
    assert path.parent == env.future_dir / "home" / "ccc"
    session = _session(env, sid)
    assert session.future_sync_hash and session.future_synced_at > 0
    assert 'status: "registered"' in path.read_text(encoding="utf-8")

    # A second pass with no change must write NOTHING (kills the launchd retrigger loop).
    mtime = path.stat().st_mtime_ns
    report2 = futuresync.run_sync(env.store, env.cfg)
    assert report2.total() == 0
    assert path.stat().st_mtime_ns == mtime


def test_bootstrap_cwd_outside_git_base_lands_under_other(env: Env, tmp_path: Path) -> None:
    external = tmp_path / "external-repo"
    external.mkdir()
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(external), "Do external work")
    futuresync.run_sync(env.store, env.cfg)
    path = _abs(env, sid)
    assert path.parent == env.future_dir / "other" / "external-repo"
    assert path.exists()


# ---------------------------------------------------------------------------
# import (file wins)
# ---------------------------------------------------------------------------
def test_file_edit_imports_via_set_aim_and_rewrites_canonical(env: Env) -> None:
    sid = _new_draft(env, "Fix the widget bug")
    futuresync.run_sync(env.store, env.cfg)
    path = _abs(env, sid)

    path.write_text(
        path.read_text(encoding="utf-8").replace("Fix the widget bug", "Fix the login bug"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert report.imported == [sid]
    assert _session(env, sid).aim == "Fix the login bug"
    # set_aim chokepoint recorded history: original + the imported edit.
    history = env.store.list_aim_history(sid)
    assert [r.aim for r in history] == ["Fix the widget bug", "Fix the login bug"]
    # Canonical rewrite keeps status registered; the new hash makes the next pass a no-op.
    assert 'status: "registered"' in path.read_text(encoding="utf-8")
    assert futuresync.run_sync(env.store, env.cfg).total() == 0


def test_concurrent_db_and_file_edit_file_wins_and_conflict_recorded(env: Env) -> None:
    sid = _new_draft(env, "Original aim")
    futuresync.run_sync(env.store, env.cfg)
    path = _abs(env, sid)

    # DB also changed (agent set the AIM), and the last sync is ancient → a real conflict.
    env.store.set_aim(sid, "DB-side aim")
    env.store.update_fields(sid, future_synced_at=1)
    path.write_text(
        serialize(session_id=sid, aim="File wins aim", status="registered", repo="home/ccc"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert _session(env, sid).aim == "File wins aim"  # file wins
    assert any("conflict" in d for d in report.details)
    assert futuresync._CN_START in _abs(env, sid).read_text(encoding="utf-8")
    # The conflict note is a fixed point: the next no-op pass preserves it, writing nothing.
    mtime = _abs(env, sid).stat().st_mtime_ns
    assert futuresync.run_sync(env.store, env.cfg).total() == 0
    assert _abs(env, sid).stat().st_mtime_ns == mtime


def test_depends_on_file_edit_imports_and_exports(env: Env) -> None:
    parent = _new_draft(env, "Parent job")
    child = _new_draft(env, "Child job")
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export of both

    child_path = _abs(env, child)
    text = child_path.read_text(encoding="utf-8")
    assert "depends_on:" not in text  # none yet → key absent (byte-stable)
    # Add the dependency in the file (after the created: line) → import picks it up.
    text = text.replace('status: "registered"', f'status: "registered"\ndepends_on: "{parent}"')
    child_path.write_text(text, encoding="utf-8")
    report = futuresync.run_sync(env.store, env.cfg)

    assert child in report.imported
    assert _session(env, child).depends_on == parent
    # Canonical rewrite keeps depends_on; the next pass is a no-op (echo suppression).
    assert f'depends_on: "{parent}"' in child_path.read_text(encoding="utf-8")
    assert futuresync.run_sync(env.store, env.cfg).total() == 0


def test_depends_on_cycle_import_rejected_with_callout(env: Env) -> None:
    parent = _new_draft(env, "Parent job")
    child = _new_draft(env, "Child job")
    env.store.update_fields(parent, depends_on=child)  # parent already depends on child
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export

    # Editing the child to depend on the parent would close the cycle → rejected.
    child_path = _abs(env, child)
    text = child_path.read_text(encoding="utf-8").replace(
        'status: "registered"', f'status: "registered"\ndepends_on: "{parent}"'
    )
    child_path.write_text(text, encoding="utf-8")
    futuresync.run_sync(env.store, env.cfg)

    assert _session(env, child).depends_on is None  # field NOT imported
    on_disk = child_path.read_text(encoding="utf-8")
    assert "<!-- ccc-sync-error -->" in on_disk
    assert "cycle" in on_disk


def test_llm_models_export_and_import(env: Env) -> None:
    sid = _new_draft(env, "Ship the thing")
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export
    path = _abs(env, sid)

    # Export: the bootstrapped file carries the fable-5 defaults.
    text = path.read_text(encoding="utf-8")
    assert 'llm_overseer: "fable-5"' in text and 'llm_exec: "fable-5"' in text

    # Import (file wins): editing llm_exec in the file lands in the DB.
    path.write_text(text.replace('llm_exec: "fable-5"', 'llm_exec: "sonnet-5"'), encoding="utf-8")
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.imported == [sid]
    assert _session(env, sid).llm_exec == "sonnet-5"
    assert _session(env, sid).llm_overseer == "fable-5"
    # Canonical rewrite makes the next pass a no-op (echo suppression).
    assert futuresync.run_sync(env.store, env.cfg).total() == 0


def test_db_model_edit_exports_to_file_frontmatter(env: Env) -> None:
    """A DB-side llm_overseer/llm_exec change (as the TUI details pane makes) reaches the file."""
    sid = _new_draft(env, "Ship the thing")
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export (fable-5 defaults)
    path = _abs(env, sid)
    assert 'llm_overseer: "fable-5"' in path.read_text(encoding="utf-8")

    # Mirror the details-pane save: store.update_fields bumps updated_at → next sync re-exports.
    env.store.update_fields(sid, llm_overseer="opus-4.8", llm_exec="sonnet-5")
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.exported == [sid]
    text = path.read_text(encoding="utf-8")
    assert 'llm_overseer: "opus-4.8"' in text
    assert 'llm_exec: "sonnet-5"' in text
    # Echo suppression: the canonical rewrite makes the next pass a no-op.
    assert futuresync.run_sync(env.store, env.cfg).total() == 0


def test_repo_change_moves_file_and_updates_cwd(env: Env) -> None:
    sid = _new_draft(env, "Ship the thing")
    futuresync.run_sync(env.store, env.cfg)
    old = _abs(env, sid)

    old.write_text(
        serialize(session_id=sid, aim="Ship the thing", status="registered", repo="sdsc/zoho"),
        encoding="utf-8",
    )
    futuresync.run_sync(env.store, env.cfg)

    assert _session(env, sid).cwd == str(env.git_base / "sdsc" / "zoho")
    new = _abs(env, sid)
    assert new.parent == env.future_dir / "sdsc" / "zoho"
    assert new.name == old.name  # slug frozen; only the directory moved
    assert new.exists() and not old.exists()


# ---------------------------------------------------------------------------
# scan exclusions
# ---------------------------------------------------------------------------
def test_archive_dashboard_and_dotdirs_are_ignored(env: Env) -> None:
    ready = serialize(
        session_id=str(uuid.uuid4()), aim="Should not register", status="ready", repo="home/ccc"
    )
    (env.future_dir / "_archive").mkdir()
    (env.future_dir / "_archive" / "old.md").write_text(ready, encoding="utf-8")
    (env.future_dir / "_dashboard.md").write_text("# dashboard\n", encoding="utf-8")
    (env.future_dir / ".hidden.md").write_text(ready, encoding="utf-8")
    hidden_dir = env.future_dir / "home" / "ccc" / ".templates"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "tpl.md").write_text(ready, encoding="utf-8")

    report = futuresync.run_sync(env.store, env.cfg)
    assert report.registered == []
    assert [s for s in env.store.list_sessions() if s.draft] == []


# ---------------------------------------------------------------------------
# registration of ready files
# ---------------------------------------------------------------------------
def test_unknown_ready_file_registers_and_is_renamed_canonical(env: Env) -> None:
    sid = str(uuid.uuid4())
    manual = env.future_dir / "home" / "ccc" / "manual.md"
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(
        serialize(session_id=sid, aim="Do the manual thing", status="ready", repo="home/ccc"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert report.registered == [sid]
    session = _session(env, sid)
    assert session.draft
    canonical = env.future_dir / "home" / "ccc" / job_filename(sid, "Do the manual thing")
    assert canonical.exists() and not manual.exists()
    assert 'status: "registered"' in canonical.read_text(encoding="utf-8")


def test_invalid_ready_file_writes_error_once_then_no_op(env: Env) -> None:
    sid = str(uuid.uuid4())
    bad = env.future_dir / "home" / "ccc" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        serialize(session_id=sid, aim="x", status="ready", repo="home/does-not-exist"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.errors and env.store.get(sid) is None  # no DB row for an invalid job
    text = bad.read_text(encoding="utf-8")
    assert "> [!error]" in text and 'status: "error"' in text

    mtime = bad.stat().st_mtime_ns
    futuresync.run_sync(env.store, env.cfg)
    assert bad.stat().st_mtime_ns == mtime  # idempotent — no retrigger loop
    assert env.store.get(sid) is None


# ---------------------------------------------------------------------------
# duplicates
# ---------------------------------------------------------------------------
def test_duplicate_uuid_copy_gets_fresh_uuid_and_registers(env: Env) -> None:
    sid = _new_draft(env, "Ship the thing")
    futuresync.run_sync(env.store, env.cfg)
    canonical = _abs(env, sid)

    # A user copies the job file (same UUID) and flips it to ready.
    copy = canonical.parent / "copy.md"
    copy.write_text(
        canonical.read_text(encoding="utf-8").replace('status: "registered"', 'status: "ready"'),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    drafts = {s.session_id for s in env.store.list_sessions() if s.draft}
    assert sid in drafts and len(drafts) == 2  # a NEW job, not a mutation of the original
    assert report.registered and report.registered[0] != sid
    assert not copy.exists()  # renamed to its fresh-hash canonical name
    assert canonical.exists()  # the original is untouched


# ---------------------------------------------------------------------------
# pad
# ---------------------------------------------------------------------------
def test_pad_ready_registers_a_job_and_resets_the_pad(env: Env) -> None:
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(
        serialize(session_id="", aim="Pad captured task", status="ready", repo="home/ccc"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert len(report.registered) == 1
    draft = next(s for s in env.store.list_sessions() if s.draft)
    assert draft.aim == "Pad captured task"
    assert draft.future_file
    assert futuresync._abs_path(env.cfg, draft.future_file).exists()

    padjob = parse_job_file(env.pad.read_text(encoding="utf-8"))
    assert padjob.status == "draft" and padjob.session_id == "" and padjob.aim == ""


def test_pad_draft_with_launch_ticked_registers_and_launches(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phone flow: ticking launch on the pad alone (status still draft) submits the job."""
    calls: list[str] = []

    def fake_spawn(session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", fake_spawn)
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(
        serialize(
            session_id="", aim="Pad tap-launch task", status="draft", repo="home/ccc"
        ).replace("launch: false", "launch: true"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert len(report.registered) == 1
    assert report.launched == report.registered == calls
    draft = next(s for s in env.store.list_sessions() if s.draft)
    assert draft.aim == "Pad tap-launch task"
    padjob = parse_job_file(env.pad.read_text(encoding="utf-8"))
    assert padjob.status == "draft" and padjob.aim == ""  # pad reset after consuming


def test_pad_error_with_launch_ticked_retries_after_fix(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error pad keeps retrying while launch is ticked: fixing the field is enough."""
    calls: list[str] = []

    def fake_spawn(session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", fake_spawn)
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(
        serialize(session_id="", aim="Fixed pad task", status="error", repo="home/ccc").replace(
            "launch: false", "launch: true"
        ),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert len(report.registered) == 1
    assert report.launched == report.registered == calls
    padjob = parse_job_file(env.pad.read_text(encoding="utf-8"))
    assert padjob.status == "draft" and padjob.aim == ""  # pad reset after consuming


def test_pad_error_with_launch_still_invalid_no_churn(env: Env) -> None:
    """A still-invalid error pad rewrites an identical block — no retrigger loop."""
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(
        serialize(
            session_id="", aim="Broken pad task", status="error", repo="home/does-not-exist"
        ).replace("launch: false", "launch: true"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.registered == [] and report.errors

    mtime = env.pad.stat().st_mtime_ns
    futuresync.run_sync(env.store, env.cfg)
    assert env.pad.stat().st_mtime_ns == mtime  # idempotent — no mtime churn
    assert [s for s in env.store.list_sessions() if s.draft] == []


def test_pad_empty_repo_defaults_to_home(env: Env) -> None:
    """AIM-only pad capture: no repo picked ⇒ the job runs in $HOME."""
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(
        serialize(session_id="", aim="Homeless task", status="ready", repo=""),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert len(report.registered) == 1
    draft = next(s for s in env.store.list_sessions() if s.draft)
    assert draft.cwd == str(Path.home())
    padjob = parse_job_file(env.pad.read_text(encoding="utf-8"))
    assert padjob.status == "draft" and padjob.aim == ""  # pad reset after consuming


def test_registration_keeps_the_files_llm_choices(env: Env) -> None:
    """The pad's/file's llm_overseer + llm_exec land in the DB row (not DEFAULT_LLM)."""
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(
        serialize(
            session_id="",
            aim="Cheap pad task",
            status="ready",
            repo="home/ccc",
            llm_overseer="haiku-4.5",
            llm_exec="haiku-4.5",
        ),
        encoding="utf-8",
    )
    futuresync.run_sync(env.store, env.cfg)

    draft = next(s for s in env.store.list_sessions() if s.draft)
    assert draft.llm_overseer == "haiku-4.5"
    assert draft.llm_exec == "haiku-4.5"


# ---------------------------------------------------------------------------
# deletion grace + rename detection
# ---------------------------------------------------------------------------
def test_missing_file_grace_then_archive(env: Env) -> None:
    sid = _new_draft(env, "Fix the bug")
    futuresync.run_sync(env.store, env.cfg)
    _abs(env, sid).unlink()

    # Within grace: the clock starts, the draft is NOT archived.
    futuresync.run_sync(env.store, env.cfg)
    session = _session(env, sid)
    assert session.future_missing_since > 0 and not session.archived
    assert sid in {s.session_id for s in env.store.list_sessions()}

    # Past grace: archived (row kept, still a draft, just hidden from the lists).
    env.store.update_fields(sid, future_missing_since=1)  # ancient → well past grace
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.archived == [sid]
    archived = _session(env, sid)
    assert archived.archived and archived.draft  # not deleted, not un-drafted
    assert sid not in {s.session_id for s in env.store.list_sessions()}  # off the FUTURE list


def test_rename_detection_updates_future_file_instead_of_archiving(env: Env) -> None:
    sid = _new_draft(env, "Fix the bug")
    futuresync.run_sync(env.store, env.cfg)
    old = _abs(env, sid)
    moved = old.parent / "renamed-by-user.md"
    old.rename(moved)

    futuresync.run_sync(env.store, env.cfg)
    session = _session(env, sid)
    assert not session.archived and session.future_missing_since == 0
    assert session.future_file
    assert futuresync._abs_path(env.cfg, session.future_file) == moved


# ---------------------------------------------------------------------------
# flock singleton
# ---------------------------------------------------------------------------
def test_second_concurrent_run_returns_quietly(env: Env) -> None:
    _new_draft(env, "Fix the bug")
    lock_path = config.app_home() / "future_sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = futuresync.run_sync(env.store, env.cfg)  # another holds the lock
        assert report.total() == 0  # no-op: it did not run
    # Lock released → the same call now does the bootstrap export.
    assert futuresync.run_sync(env.store, env.cfg).total() == 1


# ---------------------------------------------------------------------------
# legacy-name migration (<hash>-<slug>.md → <slug>-<hash>.md)
# ---------------------------------------------------------------------------
def test_legacy_name_migration_renames_and_repoints(env: Env) -> None:
    sid = _new_draft(env, "Fix the widget bug")
    futuresync.run_sync(env.store, env.cfg)  # export → new-format <slug>-<hash>.md
    path = _abs(env, sid)
    assert path.name == job_filename(sid, "Fix the widget bug")

    # Simulate the ~19 live legacy files: an old <hash>-<slug>.md on disk whose slug is a
    # FROZEN one that differs from the current AIM's slug — migration must reuse it verbatim.
    legacy = path.parent / f"{display_hash(sid)}-old-frozen-slug.md"
    path.rename(legacy)
    env.store.update_fields(sid, future_file=futuresync._vault_relpath(env.cfg, legacy))

    report = futuresync.run_sync(env.store, env.cfg)
    migrated = _abs(env, sid)
    assert migrated.name == f"old-frozen-slug-{display_hash(sid)}.md"  # slug frozen, hash moved
    assert migrated.exists() and not legacy.exists()
    assert any("migrated" in d for d in report.details)
    assert parse_job_file(migrated.read_text(encoding="utf-8")).session_id == sid

    # A second pass is a no-op: already new format → no rename, no export.
    mtime = migrated.stat().st_mtime_ns
    report2 = futuresync.run_sync(env.store, env.cfg)
    assert not any("migrated" in d for d in report2.details)
    assert report2.total() == 0
    assert migrated.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------
# done-draft sweep (draft=1 AND done=1 AND archived=0 → archive file + row)
# ---------------------------------------------------------------------------
def test_done_draft_sweep_archives_file_and_row(env: Env) -> None:
    sid = _new_draft(env, "Wrap it up")
    futuresync.run_sync(env.store, env.cfg)  # export the live mirror file
    live = _abs(env, sid)
    assert live.exists()

    # Legacy state (the a094…/db00…/cd75… rows): done but never archived.
    env.store.update_fields(sid, done=True)

    report = futuresync.run_sync(env.store, env.cfg)
    assert sid in report.archived
    session = _session(env, sid)  # get() still returns archived rows
    assert session.archived is True

    archived = list((env.future_dir / "_archive").glob("*.md"))
    assert len(archived) == 1
    assert 'status: "archived"' in archived[0].read_text(encoding="utf-8")
    assert not live.exists()  # moved out of the live scan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_sync_future_help_exits_zero() -> None:
    from command_center.cli import build_parser

    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["sync-future", "--help"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# phone-friendly launch toggle (launch: true frontmatter flip)
# ---------------------------------------------------------------------------
def test_launch_toggle_spawns_start_job_and_resets(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_spawn(sid: str) -> bool:
        calls.append(sid)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", fake_spawn)
    sid = _new_draft(env, "Launch me from the phone")
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export
    path = _abs(env, sid)
    text = path.read_text(encoding="utf-8")
    assert "launch: false" in text  # canonical carries the (off) toggle

    path.write_text(text.replace("launch: false", "launch: true"), encoding="utf-8")
    report = futuresync.run_sync(env.store, env.cfg)

    assert report.launched == [sid]
    assert calls == [sid]
    # Flag consumed: file reset to launch: false BEFORE the spawn, so...
    assert "launch: false" in path.read_text(encoding="utf-8")
    # ...the next pass can never retrigger.
    report2 = futuresync.run_sync(env.store, env.cfg)
    assert report2.launched == [] and calls == [sid]


def test_launch_toggle_failed_spawn_still_consumes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(futuresync, "_spawn_launch", lambda sid: False)
    sid = _new_draft(env, "Spawn will fail")
    futuresync.run_sync(env.store, env.cfg)
    path = _abs(env, sid)
    path.write_text(
        path.read_text(encoding="utf-8").replace("launch: false", "launch: true"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.launched == [sid]
    assert any("FAILED" in d for d in report.details)
    assert "launch: false" in path.read_text(encoding="utf-8")


def test_consume_launch_ignores_non_draft(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_spawn(sid: str) -> bool:
        calls.append(sid)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", fake_spawn)
    sid = _new_draft(env, "Already promoted out of draft-hood")
    env.store.update_fields(sid, draft=False)
    report = futuresync.SyncReport()
    futuresync._consume_launch(env.store, env.cfg, sid, report)
    assert calls == [] and report.launched == []
    assert any("ignored" in d for d in report.details)


def test_consume_launch_blocked_by_dependency(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch flip on a job whose dependency is unsatisfied: no spawn + sync-error callout."""
    calls: list[str] = []

    def fake_spawn(sid: str) -> bool:
        calls.append(sid)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", fake_spawn)

    parent = _new_draft(env, "Parent job that is not done yet")
    child = _new_draft(env, "Child waits on parent")
    env.store.update_fields(child, depends_on=parent)
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export (both files)

    path = _abs(env, child)
    path.write_text(
        path.read_text(encoding="utf-8").replace("launch: false", "launch: true"),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)

    assert calls == []  # blocked → never spawned
    assert child not in report.launched
    assert "<!-- ccc-sync-error -->" in path.read_text(encoding="utf-8")
    assert "blocked: depends on" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# FUTURE-draft credential tripwire (a launched prompt is echoed into the mirrors)
# ---------------------------------------------------------------------------
#: A value the fake checker treats as a live credential (never a real secret shape).
DUMMY_VALUE = "DUMMY-LIVE-VALUE-0123456789"

_CHECKER_STUB = """#!/bin/sh
case "$1" in
  -h|--help)
    echo "usage: fake-broker {{ scrub | check }} < document"
    exit 0
    ;;
esac
printf '%s\\n' "$1" >> "{calls}"
document=$(cat)
{verdict}
"""
_VERDICT_CLEAN_OR_LEAK = """case "$document" in
  *{value}*)
    echo "LEAK-VERDICT v1: 1 hit(s)"
    echo "LIVE CREDENTIAL IN OUTPUT: test.key" >&2
    exit 1
    ;;
esac
echo "CLEAN-VERDICT v1"
exit 0"""
_VERDICT_DEGRADED = """echo "secret-broker: unavailable — cannot vouch (exit 3)" >&2
exit 3"""


@dataclass
class Checker:
    """A fake secret-broker client: its path, and the log of the verbs it was asked for."""

    path: Path
    calls_file: Path

    @property
    def calls(self) -> list[str]:
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text(encoding="utf-8").split()


def _checker(tmp_path: Path, *, degraded: bool = False) -> Checker:
    """Write an executable stub speaking the v1 verdict contract (never a real broker)."""
    calls = tmp_path / "checker-calls.log"
    script = tmp_path / ("checker-degraded.sh" if degraded else "checker.sh")
    verdict = _VERDICT_DEGRADED if degraded else _VERDICT_CLEAN_OR_LEAK.format(value=DUMMY_VALUE)
    script.write_text(_CHECKER_STUB.format(calls=calls, verdict=verdict), encoding="utf-8")
    script.chmod(0o755)
    return Checker(script, calls)


def _arm_tripwire(env: Env, checker: Checker) -> None:
    """Turn a mirror switch on (what arms the tripwire) and point it at *checker*."""
    env.cfg.mirror_running = True
    env.cfg.mirror_scrub_cmd = f"{checker.path} scrub"


def _tick_launch(env: Env, session_id: str) -> Path:
    """Flip the exported file's launch toggle on, the way a phone edit does."""
    path = _abs(env, session_id)
    path.write_text(
        path.read_text(encoding="utf-8").replace("launch: false", "launch: true"),
        encoding="utf-8",
    )
    return path


def _error_callout(text: str) -> str:
    """The managed error block of a job file ("" when there is none)."""
    start = text.find("<!-- ccc-sync-error -->")
    end = text.find("<!-- /ccc-sync-error -->")
    return "" if start < 0 or end < 0 else text[start : end + len("<!-- /ccc-sync-error -->")]


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the launch spawn with a recorder; returns the list of spawned ids."""
    calls: list[str] = []

    def fake_spawn(sid: str) -> bool:
        calls.append(sid)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", fake_spawn)
    return calls


def test_launch_with_credential_in_prompt_is_refused(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live credential in the prompt: no spawn, a status: error callout naming the label."""
    spawned = _no_spawn(monkeypatch)
    checker = _checker(tmp_path)
    _arm_tripwire(env, checker)
    sid = _new_draft(env, "Rotate the deploy key", prompt=f"use token {DUMMY_VALUE} to deploy")
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export
    path = _tick_launch(env, sid)

    report = futuresync.run_sync(env.store, env.cfg)

    assert spawned == []
    assert report.launched == []
    assert checker.calls == ["check"]
    text = path.read_text(encoding="utf-8")
    assert 'status: "error"' in text
    callout = _error_callout(text)
    assert "live credential in prompt/AIM (test.key)" in callout
    # The label travels, the VALUE never does (it stays only where the user typed it).
    assert DUMMY_VALUE not in callout
    assert any("launch refused (credential test.key)" in d for d in report.details)


def test_launch_with_degraded_checker_is_refused(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checker that cannot vouch is not a clean verdict: fail closed, no spawn."""
    spawned = _no_spawn(monkeypatch)
    checker = _checker(tmp_path, degraded=True)
    _arm_tripwire(env, checker)
    sid = _new_draft(env, "Harmless job", prompt="nothing secret here")
    futuresync.run_sync(env.store, env.cfg)
    path = _tick_launch(env, sid)

    report = futuresync.run_sync(env.store, env.cfg)

    assert spawned == []
    assert report.launched == []
    callout = _error_callout(path.read_text(encoding="utf-8"))
    assert "credential scrubber degraded" in callout
    assert any("launch refused (scrubber degraded)" in d for d in report.details)


def test_launch_with_clean_prompt_still_spawns(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean verdict changes nothing: the toggle launches exactly as before."""
    spawned = _no_spawn(monkeypatch)
    checker = _checker(tmp_path)
    _arm_tripwire(env, checker)
    sid = _new_draft(env, "Launch me from the phone", prompt="just do the work")
    futuresync.run_sync(env.store, env.cfg)
    path = _tick_launch(env, sid)

    report = futuresync.run_sync(env.store, env.cfg)

    assert spawned == [sid]
    assert report.launched == [sid]
    assert checker.calls == ["check"]
    assert "<!-- ccc-sync-error -->" not in path.read_text(encoding="utf-8")


def test_launch_tripwire_disarmed_without_mirrors(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mirror is on → the transcript never leaves the machine → no broker needed."""
    spawned = _no_spawn(monkeypatch)
    checker = _checker(tmp_path)
    env.cfg.mirror_scrub_cmd = f"{checker.path} scrub"  # mirrors stay off
    sid = _new_draft(env, "Rotate the deploy key", prompt=f"use token {DUMMY_VALUE} to deploy")
    futuresync.run_sync(env.store, env.cfg)
    _tick_launch(env, sid)

    report = futuresync.run_sync(env.store, env.cfg)

    assert spawned == [sid]
    assert report.launched == [sid]
    assert checker.calls == []  # never even resolved


def test_launch_tripwire_disarmed_by_allow_unscrubbed(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mirror_allow_unscrubbed`` is the one deliberate passthrough — here too."""
    spawned = _no_spawn(monkeypatch)
    checker = _checker(tmp_path)
    _arm_tripwire(env, checker)
    env.cfg.mirror_allow_unscrubbed = True
    sid = _new_draft(env, "Rotate the deploy key", prompt=f"use token {DUMMY_VALUE} to deploy")
    futuresync.run_sync(env.store, env.cfg)
    _tick_launch(env, sid)

    report = futuresync.run_sync(env.store, env.cfg)

    assert spawned == [sid]
    assert report.launched == [sid]
    assert checker.calls == []


def test_refused_launch_does_not_retrigger_next_pass(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The toggle was consumed before the refusal, so a refused launch never loops."""
    spawned = _no_spawn(monkeypatch)
    checker = _checker(tmp_path)
    _arm_tripwire(env, checker)
    sid = _new_draft(env, "Rotate the deploy key", prompt=f"use token {DUMMY_VALUE} to deploy")
    futuresync.run_sync(env.store, env.cfg)
    path = _tick_launch(env, sid)
    futuresync.run_sync(env.store, env.cfg)
    assert "launch: false" in path.read_text(encoding="utf-8")

    report2 = futuresync.run_sync(env.store, env.cfg)

    assert spawned == []
    assert report2.launched == []
    assert checker.calls == ["check"]  # one refusal, not one per pass


# ---------------------------------------------------------------------------
# per-job account (config_dir) round-trip in multi-account mode
# ---------------------------------------------------------------------------
@pytest.fixture
def accounts_two(env: Env, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Pin two accounts (private = the env's CLAUDE_HOME, work) over the single-account default."""
    private = config.claude_home()
    work = env.git_base.parent / "work"
    work.mkdir(exist_ok=True)
    dirs = {"private": Path(private), "work": work}
    monkeypatch.setattr(config, "claude_config_dirs", lambda: dict(dirs))
    return dirs


def test_file_account_edit_imports_to_config_dir(env: Env, accounts_two: dict[str, Path]) -> None:
    from command_center import accounts

    sid = _new_draft(env, "Ship the thing")  # create_draft stamps the default account
    futuresync.run_sync(env.store, env.cfg)  # bootstrap export
    path = _abs(env, sid)
    # Multi-account export always carries a concrete label — the default account's here.
    assert 'account: "private"' in path.read_text(encoding="utf-8")

    # Edit the file to account: "work" → import updates config_dir to the work dir.
    path.write_text(
        path.read_text(encoding="utf-8").replace('account: "private"', 'account: "work"'),
        encoding="utf-8",
    )
    report = futuresync.run_sync(env.store, env.cfg)
    assert report.imported == [sid]
    assert accounts.same_config_dir(_session(env, sid).config_dir, str(accounts_two["work"]))
    # Canonical rewrite keeps account: "work"; the next pass is a no-op (echo suppression).
    assert 'account: "work"' in path.read_text(encoding="utf-8")
    assert futuresync.run_sync(env.store, env.cfg).total() == 0


def test_file_account_edit_back_to_default(env: Env, accounts_two: dict[str, Path]) -> None:
    from command_center import accounts

    sid = _new_draft(env, "Ship the thing")
    futuresync.run_sync(env.store, env.cfg)
    path = _abs(env, sid)
    path.write_text(
        path.read_text(encoding="utf-8").replace('account: "private"', 'account: "work"'),
        encoding="utf-8",
    )
    futuresync.run_sync(env.store, env.cfg)
    assert accounts.same_config_dir(_session(env, sid).config_dir, str(accounts_two["work"]))

    # Edit back to the default label → config_dir returns to claude_home().
    path.write_text(
        path.read_text(encoding="utf-8").replace('account: "work"', 'account: "private"'),
        encoding="utf-8",
    )
    futuresync.run_sync(env.store, env.cfg)
    assert accounts.same_config_dir(_session(env, sid).config_dir, str(config.claude_home()))


def test_default_account_draft_exports_default_label(
    env: Env, accounts_two: dict[str, Path]
) -> None:
    # A DB-side default-account draft always exports a concrete label (never a blank select).
    sid = _new_draft(env, "Ship the thing")
    futuresync.run_sync(env.store, env.cfg)
    assert 'account: "private"' in _abs(env, sid).read_text(encoding="utf-8")
