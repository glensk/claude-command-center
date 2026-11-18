"""``ccc edit-job``, the ``future_files`` gate, ``sync-future --detach``, the DB-only capture
pad and the row-only lifecycle at ``future_file = NULL`` (tp#2266 B11)."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from futurestub import FutureEnv, future_env

from command_center import cli, daemon, futuresync, systemdunit
from command_center.future_files import serialize
from command_center.models import AIM_SET_BY_USER
from command_center.store import Store


@dataclass
class Env(FutureEnv):
    store: Store


@pytest.fixture(name="env")
def _env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    mirrors = tmp_path / "mirrors"
    fe = future_env(
        tmp_path,
        monkeypatch,
        future_files=False,
        future_pad_enabled=False,
        running_dir=str(mirrors / "running"),
        done_dir=str(mirrors / "done"),
        sessions_dir=str(mirrors / "sessions"),
    )
    store = Store(tmp_path / "claude" / "command-center" / "state.db")
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: fe.cfg)
    yield Env(**vars(fe), store=store)
    store.close()


def _draft(env: Env, aim: str = "Ship it", repo: str = "home/ccc") -> str:
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / repo), aim, prompt="do it")
    return sid


def _vault_md(env: Env) -> list[Path]:
    return sorted(p for p in env.vault.rglob("*.md") if p != env.pad) if env.vault.exists() else []


def _job_path(env: Env, sid: str) -> Path:
    row = env.store.get(sid)
    assert row is not None and row.future_file
    return futuresync._abs_path(env.cfg, row.future_file)  # pylint: disable=protected-access


def _edit(*argv: str) -> int:
    return cli.main(["edit-job", *argv])


# ---------------------------------------------------------------------------
# edit-job
# ---------------------------------------------------------------------------
def test_edit_job_each_flag(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    dep = _draft(env, "Parent")
    sid = _draft(env)
    rc = _edit(
        sid,
        "-p",
        "new prompt",
        "-c",
        str(env.git_base / "sdsc" / "zoho"),
        "-D",
        "2030-01-02",
        "-w",
        "holidays",
        "-d",
        dep,
        "-s",
        "2030-01-01",
        "-j",
        "codex",
        "-O",
        "sonnet-5",
        "-E",
        "sonnet-5",
    )
    assert rc == 0, capsys.readouterr().err
    row = env.store.get(sid)
    assert row is not None
    assert row.prompt == "new prompt"
    assert row.cwd == str(env.git_base / "sdsc" / "zoho")
    assert (row.deadline, row.start_when, row.start_date) == (
        "2030-01-02",
        "holidays",
        "2030-01-01",
    )
    assert row.depends_on == dep and row.job_type == "codex"
    assert (row.llm_overseer, row.llm_exec) == ("sonnet-5", "sonnet-5")
    assert _edit(sid, "-j", "claude", "-N") == 0
    assert env.store.get(sid).no_codex is True  # type: ignore[union-attr]
    assert _edit(sid, "-C", "-R", "-T", "-W", "-U", "-S") == 0
    row = env.store.get(sid)
    assert row is not None and row.no_codex is False and row.prompt is None
    assert (row.deadline, row.start_when, row.start_date, row.depends_on) == (None,) * 4
    assert _vault_md(env) == []  # DB-only: no job file written


def test_edit_job_aim_lands_in_history_set_by_user(env: Env) -> None:
    sid = _draft(env)
    assert _edit(sid, "-a", "A sharper AIM") == 0
    assert env.store.get(sid).aim == "A sharper AIM"  # type: ignore[union-attr]
    history = env.store.list_aim_history(sid)
    assert history and history[-1].aim == "A sharper AIM"
    assert history[-1].set_by == AIM_SET_BY_USER


def test_edit_job_cycle_refused(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    parent = _draft(env, "Parent")
    child = _draft(env, "Child")
    assert _edit(child, "-d", parent) == 0
    assert _edit(parent, "-d", child) == 1
    assert "cycle" in capsys.readouterr().err
    assert env.store.get(parent).depends_on is None  # type: ignore[union-attr]


def test_edit_job_non_draft_refused_exit_2(env: Env) -> None:
    sid = _draft(env)
    env.store.claim_draft(sid)
    assert _edit(sid, "-a", "nope") == 2
    archived = _draft(env, "Archived")
    env.store.update_fields(archived, archived=True)
    assert _edit(archived, "-a", "nope") == 2


def test_edit_job_json_shape(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    sid = _draft(env)
    capsys.readouterr()
    assert _edit(sid, "-w", "soon", "-J") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"version": 1, "session_id": sid, "changed": ["start_when"]}


# ---------------------------------------------------------------------------
# run_sync gate (the _bootstrap_export hazard)
# ---------------------------------------------------------------------------
def test_bootstrap_export_gated_off_writes_nothing(env: Env) -> None:
    sid = _draft(env)
    report = futuresync.run_sync(env.store, env.cfg)
    row = env.store.get(sid)
    assert report.total() == 0 and row is not None and row.future_file is None
    assert _vault_md(env) == []
    assert cli.main(["sync-future"]) == 0  # the CLI entry point is gated too
    assert _vault_md(env) == []


def test_bootstrap_export_gate_missing_files_archive_nothing_after_grace(env: Env) -> None:
    sid = _draft(env)
    env.store.update_fields(
        sid, future_file="01-llm/ccc-tasks/future/gone.md", future_missing_since=1
    )
    futuresync.run_sync(env.store, env.cfg)
    row = env.store.get(sid)
    assert row is not None and not row.archived


# ---------------------------------------------------------------------------
# --detach
# ---------------------------------------------------------------------------
def test_detach_refuses_with_future_files_on(env: Env) -> None:
    env.cfg.future_files = True
    with pytest.raises(futuresync.DetachError):
        futuresync.detach(env.store, env.cfg)
    assert cli.main(["sync-future", "-x"]) == 2


def test_detach_refuses_on_sync_error(env: Env) -> None:
    env.cfg.future_files = True
    sid = _draft(env)
    futuresync.run_sync(env.store, env.cfg)
    path = _job_path(env, sid)
    path.write_text(path.read_text(encoding="utf-8").replace("Ship it", ""), encoding="utf-8")
    env.cfg.future_files = False
    with pytest.raises(futuresync.DetachError):
        futuresync.detach(env.store, env.cfg)
    assert env.store.get(sid).future_file  # type: ignore[union-attr]  # nothing cleared


def test_detach_clears_all_rows_backfills_and_is_idempotent(env: Env) -> None:
    env.cfg.future_files = True
    draft = _draft(env, "Still a draft")
    launched = _draft(env, "Launched one")
    futuresync.run_sync(env.store, env.cfg)
    row = env.store.get(launched)
    env.store.update_fields(launched, draft=False)
    futuresync.archive_file(env.store, env.cfg, row, "launched")
    archived = _draft(env, "Archived one")
    futuresync.run_sync(env.store, env.cfg)
    env.store.update_fields(archived, draft=False, archived=True)
    env.cfg.future_files = False
    assert futuresync.detach(env.store, env.cfg) == 3
    for sid in (draft, launched, archived):
        r = env.store.get(sid)
        assert r is not None and r.future_file is None and r.future_sync_hash is None
        assert r.future_synced_at == 0 and r.future_missing_since == 0
    assert env.store.get(launched).launched_from_draft is True  # type: ignore[union-attr]
    assert env.store.get(archived).launched_from_draft is True  # type: ignore[union-attr]
    assert env.store.get(draft).launched_from_draft is False  # type: ignore[union-attr]
    assert futuresync.detach(env.store, env.cfg) == 0
    assert cli.main(["sync-future", "--detach"]) == 0


def test_detach_rerun_never_imports_a_stale_file(env: Env) -> None:
    env.cfg.future_files = True
    sid = _draft(env)
    futuresync.run_sync(env.store, env.cfg)
    path = _job_path(env, sid)
    env.cfg.future_files = False
    assert futuresync.detach(env.store, env.cfg) == 1
    assert _edit(sid, "-p", "edited in the DB") == 0
    path.write_text(path.read_text(encoding="utf-8").replace("do it", "stale"), encoding="utf-8")
    assert futuresync.detach(env.store, env.cfg) == 0
    row = env.store.get(sid)
    assert row is not None and row.prompt == "edited in the DB" and row.future_file is None


def test_detach_refuses_an_unregistered_ready_file(env: Env) -> None:
    ready = env.future_dir / "home" / "ccc" / "phone.md"
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text(
        serialize(session_id="", aim="Queued from the phone", status="ready", repo="home/ccc"),
        encoding="utf-8",
    )
    with pytest.raises(futuresync.DetachError, match="unregistered ready job"):
        futuresync.detach(env.store, env.cfg)
    assert env.store.list_sessions() == [] and ready.exists()


# ---------------------------------------------------------------------------
# the capture pad without job files
# ---------------------------------------------------------------------------
def _fill_pad(env: Env, repo: str = "home/ccc") -> None:
    text = futuresync.pad_template(env.cfg)
    text = text.replace('repo: ""', f"repo: {repo}", 1)
    text = text.replace("## AIM\n", "## AIM\n\nPad job\n", 1).replace(
        "launch: false", "launch: true", 1
    )
    env.pad.parent.mkdir(parents=True, exist_ok=True)
    env.pad.write_text(text, encoding="utf-8")


def test_pad_registers_and_launches_without_job_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import accounts

    env.cfg.future_pad_enabled = True
    accounts.ensure_trusted("", env.git_base / "home" / "ccc")
    spawned: list[str] = []

    def _spawn(sid: str) -> bool:
        spawned.append(sid)
        return True

    monkeypatch.setattr(futuresync, "_spawn_launch", _spawn)
    _fill_pad(env)
    report = futuresync.run_sync(env.store, env.cfg)
    assert len(report.registered) == 1 and spawned == report.registered
    assert env.store.get(spawned[0]).future_file is None  # type: ignore[union-attr]
    assert not env.future_dir.exists() or not list(env.future_dir.rglob("*.md"))
    assert "Pad job" not in env.pad.read_text(encoding="utf-8")  # reset after the launch


def test_pad_refused_launch_leaves_error_on_pad(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.cfg.future_pad_enabled = True
    monkeypatch.setattr(futuresync, "_spawn_launch", lambda sid: pytest.fail("spawned"))
    _fill_pad(env)  # folder not trusted → refused
    report = futuresync.run_sync(env.store, env.cfg)
    text = env.pad.read_text(encoding="utf-8")
    assert "Pad job" in text and "ccc-sync-error" in text and "not trusted" in text
    assert "launch: false" in text  # no re-registration loop
    assert not futuresync.run_sync(env.store, env.cfg).registered
    assert len(report.registered) == 1 and not list(env.future_dir.rglob("*.md"))


def test_pad_daemon_gate_and_systemd_watcher_keep_trigger(env: Env) -> None:
    env.cfg.future_pad_enabled = True
    assert systemdunit._vault_features_on(env.cfg)
    daemon._sync_future_files(env.store, env.cfg)
    assert env.pad.exists()  # the pad half ran (self-healed the missing pad)


# ---------------------------------------------------------------------------
# row-only lifecycle at future_file = NULL
# ---------------------------------------------------------------------------
class _FakeAdapter:
    def discover(self) -> list[object]:
        return []


def test_unlaunch_launched_draft_row_only(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _draft(env)
    assert env.store.claim_draft(sid)  # what start-job does
    assert env.store.get(sid).launched_from_draft is True  # type: ignore[union-attr]
    monkeypatch.setattr("command_center.cli._adapter", _FakeAdapter)
    assert cli.cmd_unlaunch(argparse.Namespace(session_id=sid)) == 0
    assert env.store.get(sid).draft is True  # type: ignore[union-attr]
    assert _vault_md(env) == []


def test_unlaunch_refuses_without_provenance(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _draft(env)
    env.store.update_fields(sid, draft=False)
    monkeypatch.setattr("command_center.cli._adapter", _FakeAdapter)
    assert cli.cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1


def test_unlaunch_lifecycle_verbs_touch_no_file(env: Env) -> None:
    a, b, c = _draft(env, "A"), _draft(env, "B"), _draft(env, "C")
    assert cli.cmd_done_job(argparse.Namespace(session_id=a, file=None)) == 0
    assert cli.cmd_delete_job(argparse.Namespace(session_id=b, file=None)) == 0
    row = env.store.get(c)
    futuresync.archive_file(env.store, env.cfg, row, "launched")
    futuresync.unarchive_file(env.store, env.cfg, row)
    assert futuresync.delete_file(env.store, env.cfg, row) is None
    assert cli.main(["new-prompt"]) == 2  # points at new-job instead
    assert _vault_md(env) == [] and not env.pad.exists()
