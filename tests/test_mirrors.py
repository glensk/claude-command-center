"""Tests for the export-only RUNNING/DONE session mirrors (:mod:`command_center.mirrors`)
plus the CLI lifecycle commands that drive them (``focus-job``, ``resume-job``,
``unlaunch``, and the resume-aware / guarded ``start-job``).

Hermetic (mirrors test_futuresync.py): a tmp ``CLAUDE_HOME`` (store + flock), a tmp
``$GIT_BASE`` and a tmp vault. No Obsidian, no real ``ccc`` spawns (``CCC_INTERNAL``).
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from command_center import config, futuresync, mirrors, sessionmd
from command_center.future_files import slugify
from command_center.models import LiveSession, Session, Status, now_ms
from command_center.store import Store


@dataclass
class Env:
    store: Store
    cfg: config.Config
    git_base: Path
    running: Path
    done: Path
    future: Path
    sessions: Path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress any detached ccc spawn
    git_base = tmp_path / "git"
    (git_base / "home" / "ccc").mkdir(parents=True)
    (git_base / "sdsc" / "zoho").mkdir(parents=True)
    monkeypatch.setenv("GIT_BASE", str(git_base))
    vault = tmp_path / "vault"
    tasks = vault / "01-llm-tasks"
    running = tasks / "running"
    done = tasks / "done"
    future = tasks / "future"
    sessions = tasks / "sessions"
    future.mkdir(parents=True)
    cfg = config.Config(
        vault_root=str(vault),
        future_dir=str(future),
        future_pad=str(tasks / "new-prompt.md"),
        running_dir=str(running),
        done_dir=str(done),
        sessions_dir=str(sessions),
        aim_score_on_set=False,
        short_aim=False,
        # The mirror roots default OFF (fresh-install INERT contract); this whole module
        # exercises the mirror feature, so opt the fixture in explicitly.
        mirror_running=True,
        mirror_done=True,
        mirror_sessions=True,
    )
    store = Store(tmp_path / "claude" / "command-center" / "state.db")
    yield Env(store, cfg, git_base, running, done, future, sessions)
    store.close()


@pytest.fixture(autouse=True)
def _clear_prompt_cache() -> Iterator[None]:
    """Reset the module-level mtime caches so tests never cross-pollute."""
    from command_center.adapters import claude as claude_mod

    def _reset() -> None:
        mirrors._PROMPT_CACHE.clear()  # pylint: disable=protected-access
        sessionmd._RENDER_CACHE.clear()  # pylint: disable=protected-access
        claude_mod._OBSERVED_MODEL_CACHE.clear()  # pylint: disable=protected-access

    _reset()
    yield
    _reset()


def _get(env: Env, sid: str) -> Session:
    session = env.store.get(sid)
    assert session is not None
    return session


def _write_transcript(env: Env, cwd: str, sid: str, prompts: list[str]) -> Path:
    """Write a minimal Claude transcript of *prompts* as user records (oldest first).

    Matches what :func:`command_center.peek.session_prompts` reads, so the mirror's
    ``## Prompts`` section is driven by the same source the ``ccc peek`` panel is.
    """
    munged = cwd.replace("/", "-")
    proj = env.store.path.parents[1] / "projects" / munged  # CLAUDE_HOME/projects/<munged>
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        for text in prompts
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_model_transcript(env: Env, cwd: str, sid: str, model: str) -> Path:
    """A transcript with one assistant record carrying ``message.model`` = *model*.

    Feeds :meth:`ClaudeAdapter.observed_model`, so the mirror's ``model:`` frontmatter
    reflects the model the session actually ran on (not the job-config default).
    """
    munged = cwd.replace("/", "-")
    proj = env.store.path.parents[1] / "projects" / munged
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    rec = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
        },
    }
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return path


def _running_session(env: Env, aim: str, *, repo: str = "home/ccc") -> str:
    """An active (draft=0, archived=0, done=0) session with an AIM → RUNNING set."""
    sid = str(uuid.uuid4())
    env.store.ensure(sid, cwd=str(env.git_base / repo))
    env.store.set_aim(sid, aim)
    return sid


def _mark_done(env: Env, sid: str) -> None:
    env.store.update_fields(sid, done=True, status=Status.DONE.value, done_at=now_ms())


def _rpath(env: Env, aim: str, sid: str, *, repo: str = "home/ccc") -> Path:
    """The canonical RUNNING mirror path for *sid* (4-hex prefix)."""
    return env.running / repo / f"{slugify(aim)}-{sid[:4]}.md"


def _dpath(env: Env, aim: str, sid: str, *, repo: str = "home/ccc") -> Path:
    """The canonical DONE mirror path for *sid* (4-hex prefix)."""
    return env.done / repo / f"{slugify(aim)}-{sid[:4]}.md"


# ---------------------------------------------------------------------------
# set selection (decision 2)
# ---------------------------------------------------------------------------
def test_set_selection_excludes_done_drafts(env: Env) -> None:
    run_sid = _running_session(env, "Fix the widget")
    done_sid = _running_session(env, "Ship the release", repo="sdsc/zoho")
    _mark_done(env, done_sid)
    # A cancelled future job: a draft marked done + archived — must appear in NEITHER set.
    draft_sid = str(uuid.uuid4())
    env.store.create_draft(draft_sid, str(env.git_base / "home" / "ccc"), "Someday maybe")
    env.store.update_fields(draft_sid, done=True, archived=True)
    # A live future job (draft, not done) is also excluded from both.
    plain_draft = str(uuid.uuid4())
    env.store.create_draft(plain_draft, str(env.git_base / "home" / "ccc"), "Later")

    report = mirrors.run_mirrors(env.store, env.cfg)

    assert set(report.running) == {run_sid}
    assert set(report.done) == {done_sid}
    assert draft_sid not in report.running and draft_sid not in report.done
    assert plain_draft not in report.running and plain_draft not in report.done
    # RUNNING file materialised; DONE file materialised; done-draft has no file anywhere.
    assert _rpath(env, "Fix the widget", run_sid).exists()
    assert _dpath(env, "Ship the release", done_sid, repo="sdsc/zoho").exists()
    assert list(env.running.rglob("*.md")) == [_rpath(env, "Fix the widget", run_sid)]


# ---------------------------------------------------------------------------
# frontmatter + body content (decision 6)
# ---------------------------------------------------------------------------
def test_mirror_frontmatter_and_body(env: Env) -> None:
    sid = _running_session(env, "Fix the widget bug")
    env.store.set_subgoals(sid, ["find bug", "write test"], source="user")
    env.store.set_subgoal_checked(env.store.list_subgoals(sid)[0].id, True)
    env.store.update_fields(sid, next_step="write the failing test", summary="looked at it")
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "Fix the widget bug", sid).read_text(encoding="utf-8")
    assert 'ccc_mirror: "running"' in text
    assert f'session_id: "{sid}"' in text
    assert 'progress: "1/2"' in text
    assert "## AIM (1)" in text and "Fix the widget bug" in text
    assert "- [x] find bug" in text and "- [ ] write test" in text
    assert "## Next step\n\nwrite the failing test" in text
    assert f"Resume: `c --resume {sid}`" in text
    assert ".jsonl`" in text  # transcript path pointer
    assert "GENERATED by ccc" in text  # banner


def test_mirror_emits_depends_on_when_set(env: Env) -> None:
    """All three mirror kinds emit ``depends_on:`` only when the session carries one."""
    parent = _running_session(env, "the parent")
    child = _running_session(env, "the child")
    # No dependency yet → the running + session mirrors carry NO depends_on key.
    mirrors.run_mirrors(env.store, env.cfg)
    running_text = _rpath(env, "the child", child).read_text(encoding="utf-8")
    session_text = _spath(env, "the child", child).read_text(encoding="utf-8")
    assert "depends_on:" not in running_text
    assert "depends_on:" not in session_text

    # Set a dependency → it appears in the running AND session mirrors.
    env.store.update_fields(child, depends_on=parent)
    mirrors.run_mirrors(env.store, env.cfg)
    assert f'depends_on: "{parent}"' in _rpath(env, "the child", child).read_text(encoding="utf-8")
    assert f'depends_on: "{parent}"' in _spath(env, "the child", child).read_text(encoding="utf-8")

    # A DONE mirror carries it too.
    _mark_done(env, child)
    mirrors.run_mirrors(env.store, env.cfg)
    assert f'depends_on: "{parent}"' in _dpath(env, "the child", child).read_text(encoding="utf-8")


def test_mirror_frontmatter_observed_model(env: Env) -> None:
    """``model:`` carries the OBSERVED transcript model, not the job-config default.

    A session with no assistant model record yet → empty (like ``deadline: ""``); one
    with a real ``message.model`` → its ccc choice label.
    """
    cwd = str(env.git_base / "home" / "ccc")
    # (a) user-only transcript → no observed model → model: "". Effort mirrors the persisted
    # session.effort observation ("" until reconcile/statusline captures one).
    sid = _running_session(env, "No model recorded")
    _write_transcript(env, cwd, sid, ["hi"])
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "No model recorded", sid).read_text(encoding="utf-8")
    assert 'model: ""' in text
    assert 'effort: ""' in text

    # (b) an assistant record carrying message.model → its choice label lands; the captured
    # effort observation is emitted right after, in both the running and session mirrors.
    zcwd = str(env.git_base / "sdsc" / "zoho")
    sid2 = _running_session(env, "Ran on fable", repo="sdsc/zoho")
    _write_model_transcript(env, zcwd, sid2, "claude-fable-5")
    env.store.update_fields(sid2, effort="xhigh")
    mirrors.run_mirrors(env.store, env.cfg)
    text2 = _rpath(env, "Ran on fable", sid2, repo="sdsc/zoho").read_text(encoding="utf-8")
    assert 'model: "fable-5"' in text2
    assert 'effort: "xhigh"' in text2
    # The SESSION mirror carries the same observed model + effort fields.
    stext = (env.sessions / "sdsc/zoho" / f"{slugify('Ran on fable')}-{sid2[:4]}.md").read_text(
        encoding="utf-8"
    )
    assert 'model: "fable-5"' in stext
    assert 'effort: "xhigh"' in stext


# ---------------------------------------------------------------------------
# lifecycle moves: future → running → done → undone (decisions 2, 3)
# ---------------------------------------------------------------------------
def test_lifecycle_future_running_done_undone(env: Env) -> None:
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Do the thing")
    run_path = _rpath(env, "Do the thing", sid)
    done_path = _dpath(env, "Do the thing", sid)

    # FUTURE (draft) → no running/done mirror.
    mirrors.run_mirrors(env.store, env.cfg)
    assert not run_path.exists() and not done_path.exists()

    # RUNNING (launched: draft cleared).
    env.store.clear_draft(sid)
    mirrors.run_mirrors(env.store, env.cfg)
    assert run_path.exists() and not done_path.exists()

    # DONE — the running mirror is removed, the done mirror written.
    _mark_done(env, sid)
    report = mirrors.run_mirrors(env.store, env.cfg)
    assert not run_path.exists() and done_path.exists()
    assert str(run_path) in report.removed and sid in report.written

    # UNDONE (mark-done --undo) — done mirror removed, running mirror reappears.
    env.store.update_fields(sid, done=False, status=Status.IDLE.value, done_at=0)
    mirrors.run_mirrors(env.store, env.cfg)
    assert run_path.exists() and not done_path.exists()


# ---------------------------------------------------------------------------
# byte-stable no-op idempotence (decision 5)
# ---------------------------------------------------------------------------
def test_byte_stable_noop(env: Env) -> None:
    sid = _running_session(env, "Keep it stable")
    r1 = mirrors.run_mirrors(env.store, env.cfg)
    assert sid in r1.written
    path = _rpath(env, "Keep it stable", sid)
    mtime = path.stat().st_mtime_ns

    r2 = mirrors.run_mirrors(env.store, env.cfg)
    assert r2.changed() == 0 and r2.written == [] and r2.removed == []
    assert path.stat().st_mtime_ns == mtime  # not rewritten


# ---------------------------------------------------------------------------
# ccc_mirror guard never touches foreign files (decision 3)
# ---------------------------------------------------------------------------
def test_cleanup_never_touches_foreign_files(env: Env) -> None:
    sid = _running_session(env, "Real work")
    mirrors.run_mirrors(env.store, env.cfg)

    # A hand-written note WITHOUT a ccc_mirror marker in the running root.
    foreign = env.running / "home" / "ccc" / "my-notes.md"
    foreign.write_text("---\ntitle: mine\n---\n\nhand-written, keep me\n", encoding="utf-8")
    # A stale mirror whose session left the set (its id is not tracked).
    stale = env.running / "home" / "ccc" / "old-ghost.md"
    stale.write_text(
        '---\nccc_mirror: "running"\nsession_id: "dead-0000"\n---\n\nstale\n', encoding="utf-8"
    )

    report = mirrors.run_mirrors(env.store, env.cfg)

    assert foreign.exists()  # no ccc_mirror marker → NEVER touched
    assert not stale.exists()  # stale ccc_mirror file → removed
    assert str(stale) in report.removed
    assert _rpath(env, "Real work", sid).exists()  # the real session's mirror survives


# ---------------------------------------------------------------------------
# 4→8 hex collision extension (decision 4)
# ---------------------------------------------------------------------------
def test_hash_extends_4_to_8_on_same_dir_collision(env: Env) -> None:
    # Two sessions in the SAME dir whose first 4 hex collide ("aaaa") but 8 hex differ.
    sid1 = "aaaa1111-0000-0000-0000-000000000000"
    sid2 = "aaaa2222-0000-0000-0000-000000000000"
    other = _running_session(env, "No clash here")  # random uuid → keeps its 4-hex prefix
    for sid, aim in ((sid1, "Colliding one"), (sid2, "Colliding two")):
        env.store.ensure(sid, cwd=str(env.git_base / "home" / "ccc"))
        env.store.set_aim(sid, aim)

    mirrors.run_mirrors(env.store, env.cfg)
    d = env.running / "home" / "ccc"
    # Both collided ids fall back to the 8-hex prefix; the non-colliding one keeps 4.
    assert (d / f"{slugify('Colliding one')}-aaaa1111.md").exists()
    assert (d / f"{slugify('Colliding two')}-aaaa2222.md").exists()
    assert not (d / f"{slugify('Colliding one')}-aaaa.md").exists()
    assert (d / f"{slugify('No clash here')}-{other[:4]}.md").exists()


# ---------------------------------------------------------------------------
# kill-switches (decision 8)
# ---------------------------------------------------------------------------
def test_kill_switches_disable_roots(env: Env) -> None:
    env.cfg.mirror_running = False
    env.cfg.mirror_sessions = False
    _running_session(env, "Off the record")
    report = mirrors.run_mirrors(env.store, env.cfg)
    assert report.running == [] and report.sessions == [] and report.written == []
    assert not env.running.exists() or list(env.running.rglob("*.md")) == []
    assert not env.sessions.exists() or list(env.sessions.rglob("*.md")) == []


def test_session_mirrors_independent_of_running_done_switches(env: Env) -> None:
    """O8: the SESSION root exports even with mirror_running/mirror_done off."""
    env.cfg.mirror_running = False
    env.cfg.mirror_done = False
    sid = _running_session(env, "Session survives switches")
    report = mirrors.run_mirrors(env.store, env.cfg)
    assert report.sessions == [sid] and report.running == [] and report.done == []
    files = list(env.sessions.rglob("*.md"))
    assert len(files) == 1 and sid[:4] in files[0].name


# ---------------------------------------------------------------------------
# CLI: start-job guards (decision 13)
# ---------------------------------------------------------------------------
class _StopExec(Exception):
    """Stand-in for os.execvp so a test can inspect the built argv."""


def _no_execvp(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    captured: dict[str, list[str]] = {}

    def _cap(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _cap)
    monkeypatch.setattr("command_center.cli.os.chdir", lambda *_a, **_k: None)
    return captured


def test_start_job_refuses_non_draft(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    env.store.ensure("live-1", cwd=str(env.git_base / "home" / "ccc"))  # not a draft
    _no_execvp(monkeypatch)  # must never be reached
    assert cmd_start_job(argparse.Namespace(session_id="live-1")) == 1
    assert "not a future job" in capsys.readouterr().err
    assert _get(env, "live-1").draft is False  # unchanged


def test_start_job_refuses_archived_draft(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Archived one")
    env.store.update_fields(sid, archived=True)
    _no_execvp(monkeypatch)
    assert cmd_start_job(argparse.Namespace(session_id=sid)) == 1
    assert "archived" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI: resume-aware + cwd-scoped transcript (decision 14)
# ---------------------------------------------------------------------------
def _make_transcript(env: Env, cwd: str, session_id: str) -> None:
    munged = cwd.replace("/", "-")
    proj = env.store.path.parents[1] / "projects" / munged  # CLAUDE_HOME/projects/<munged>
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")


def test_start_job_resumes_when_cwd_transcript_exists(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    cwd = str(env.git_base / "home" / "ccc")
    sid = "11111111-1111-1111-1111-111111111111"
    env.store.create_draft(sid, cwd, "Do the thing", prompt="run it")
    _make_transcript(env, cwd, sid)  # transcript in THIS cwd's project dir
    captured = _no_execvp(monkeypatch)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id=sid))
    # Bare resume — NO prompt argument; --model still applied, effort still explicit.
    assert captured["argv"] == [
        "claude",
        "--resume",
        sid,
        "--model",
        "claude-fable-5",
        "--effort",
        "xhigh",
    ]


def test_start_job_first_launch_when_transcript_only_in_other_project_dir(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.cli import cmd_start_job

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    cwd = str(env.git_base / "home" / "ccc")
    sid = "22222222-2222-2222-2222-222222222222"
    env.store.create_draft(sid, cwd, "Do the thing", prompt="run it")
    # Transcript exists, but under a DIFFERENT cwd's project dir — must NOT trigger resume.
    _make_transcript(env, str(env.git_base / "sdsc" / "zoho"), sid)
    captured = _no_execvp(monkeypatch)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id=sid))
    assert captured["argv"] == [
        "claude",
        "--model",
        "claude-fable-5",
        "--session-id",
        sid,
        "--effort",
        "xhigh",
        "run it",
    ]


# ---------------------------------------------------------------------------
# CLI: focus-job live check (decision 11)
# ---------------------------------------------------------------------------
class _FakeAdapter:
    def __init__(self, live_ids: list[str], *, transcript: bool = True) -> None:
        self._ids = live_ids
        self._transcript = transcript

    def discover(self) -> list[LiveSession]:
        return [LiveSession(pid=1, session_id=i, cwd="/x", alive=True) for i in self._ids]

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        return Path(cwd) / f"{session_id}.jsonl" if self._transcript else None


def test_focus_job_refuses_when_not_live(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_focus_job

    env.store.ensure("s1", cwd="/x")
    env.store.update_fields("s1", iterm_session_id="w0t1p0:UUID")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # nothing live
    assert cmd_focus_job(argparse.Namespace(session_id="s1")) == 1
    assert "not live" in capsys.readouterr().err


def test_focus_job_focuses_live_tab(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_focus_job

    env.store.ensure("s1", cwd="/x")
    env.store.update_fields("s1", iterm_session_id="w0t1p0:UUID")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter(["s1"]))
    focused: dict[str, str] = {}

    def _focus(sid: str) -> bool:
        focused["sid"] = sid
        return True

    monkeypatch.setattr("command_center.terminal.focus_iterm_session", _focus)
    assert cmd_focus_job(argparse.Namespace(session_id="s1")) == 0
    assert focused["sid"] == "w0t1p0:UUID"
    assert "focused" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI: resume-job — parked → new tab, live → focus (Obsidian parked dashboard ▶)
# ---------------------------------------------------------------------------
def test_resume_job_parked_with_transcript_opens_new_tab(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = _running_session(env, "Resume me")  # active (not draft), process gone → parked
    cwd = _get(env, sid).cwd
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # not live
    resumed: dict[str, str] = {}

    def _resume(c: str, s: str, config_dir: str = "", **_: object) -> bool:
        resumed["cwd"], resumed["sid"] = c, s
        return True

    monkeypatch.setattr("command_center.terminal.resume_in_new_tab", _resume)
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 0
    assert resumed == {"cwd": cwd, "sid": sid}
    assert "resuming in a new tab" in capsys.readouterr().out


def test_resume_job_live_focuses_tab_without_resuming(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = _running_session(env, "Live one")
    env.store.update_fields(sid, iterm_session_id="w0t1p0:UUID")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([sid]))  # live
    focused: dict[str, str] = {}
    resumed: dict[str, str] = {}

    def _focus(iterm_id: str) -> bool:
        focused["iterm"] = iterm_id
        return True

    def _resume(c: str, s: str, config_dir: str = "", **_: object) -> bool:
        resumed["called"] = s
        return True

    monkeypatch.setattr("command_center.terminal.focus_iterm_session", _focus)
    monkeypatch.setattr("command_center.terminal.resume_in_new_tab", _resume)
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 0
    assert focused["iterm"] == "w0t1p0:UUID"
    assert resumed == {}  # never opened a second REPL
    assert "focused live tab" in capsys.readouterr().out


def test_resume_job_parked_without_transcript_refuses(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = _running_session(env, "No transcript")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([], transcript=False))
    resumed: dict[str, str] = {}
    monkeypatch.setattr(
        "command_center.terminal.resume_in_new_tab",
        lambda c, s, config_dir="", **_: resumed.setdefault("called", s) or True,
    )
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 1
    assert resumed == {}  # never tried to open a tab
    assert "no recorded conversation" in capsys.readouterr().err


def test_resume_job_refuses_draft(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Future job")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))
    assert cmd_resume_job(argparse.Namespace(session_id=sid)) == 1
    assert "FUTURE job" in capsys.readouterr().err


def test_resume_job_unknown_session(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_resume_job

    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))
    assert cmd_resume_job(argparse.Namespace(session_id="nope")) == 1
    assert "no such session" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI: unlaunch guards + provenance (decision 12)
# ---------------------------------------------------------------------------
def test_unlaunch_refuses_when_live(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = _running_session(env, "Live one")
    env.store.update_fields(sid, future_file="01-llm-tasks/future/home/ccc/x.md")
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([sid]))  # live
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1
    assert "still live" in capsys.readouterr().err
    assert _get(env, sid).draft is False


def test_unlaunch_refuses_without_provenance(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = _running_session(env, "No provenance")  # no future_file, no archive file
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # not live
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1
    assert "provenance" in capsys.readouterr().err
    assert _get(env, sid).draft is False


def test_unlaunch_refuses_when_done(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    sid = _running_session(env, "Done one")
    env.store.update_fields(sid, future_file="01-llm-tasks/future/home/ccc/x.md")
    _mark_done(env, sid)
    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 1
    assert "done" in capsys.readouterr().err


def test_unlaunch_returns_launched_job_to_future(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.cli import cmd_unlaunch

    monkeypatch.setattr("command_center.cli.config.load_config", lambda: env.cfg)
    # A launched draft: created, file exported, then promoted + archived (what start-job does).
    sid = str(uuid.uuid4())
    env.store.create_draft(sid, str(env.git_base / "home" / "ccc"), "Relaunch me")
    futuresync.run_sync(env.store, env.cfg)  # exports the future file
    session = env.store.get(sid)
    env.store.clear_draft(sid)
    futuresync.archive_file(env.store, env.cfg, session, "launched")  # → _archive/
    mirrors.run_mirrors(env.store, env.cfg)  # writes its running mirror
    run_mirror = _rpath(env, "Relaunch me", sid)
    assert run_mirror.exists()

    monkeypatch.setattr("command_center.cli._adapter", lambda: _FakeAdapter([]))  # not live
    assert cmd_unlaunch(argparse.Namespace(session_id=sid)) == 0

    restored = _get(env, sid)
    assert restored.draft is True
    assert restored.status == Status.PARKED.value
    assert restored.future_file is not None
    live_file = futuresync._abs_path(env.cfg, restored.future_file)
    assert live_file.exists() and "_archive" not in str(live_file)  # moved back out of archive
    assert not run_mirror.exists()  # running mirror dropped


# ---------------------------------------------------------------------------
# ## Prompts section — shared peek source (change 1)
# ---------------------------------------------------------------------------
def test_prompts_section_matches_peek_source(env: Env) -> None:
    from command_center.adapters import ClaudeAdapter
    from command_center.peek import session_prompts

    cwd = str(env.git_base / "home" / "ccc")
    sid = _running_session(env, "Wire prompts into the mirror")
    typed = ["first ask", "second ask\nwith a second line", "third ask"]
    _write_transcript(env, cwd, sid, typed)

    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "Wire prompts into the mirror", sid).read_text(encoding="utf-8")

    # The rendered section reflects the SAME source ccc peek reads (never diverge).
    assert session_prompts(ClaudeAdapter(), _get(env, sid)) == typed
    assert (
        "## Prompts\n\n1. first ask\n2. second ask\n   with a second line\n3. third ask\n" in text
    )


def test_prompts_section_empty_when_no_transcript(env: Env) -> None:
    sid = _running_session(env, "No transcript yet")
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "No transcript yet", sid).read_text(encoding="utf-8")
    assert "## Prompts\n\n(no prompts in this session yet)\n" in text


def test_prompts_section_count_cap(env: Env) -> None:
    cwd = str(env.git_base / "home" / "ccc")
    sid = _running_session(env, "Many prompts")
    _write_transcript(env, cwd, sid, [f"prompt number {i}" for i in range(1, 251)])  # 250

    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "Many prompts", sid).read_text(encoding="utf-8")

    assert "_showing last 200 of 250 prompts_" in text
    assert "1. prompt number 51" in text  # oldest kept (renumbered 1) = original #51
    assert "200. prompt number 250" in text  # newest kept (renumbered 200)
    assert "prompt number 50" not in text  # #1..#50 dropped from the front


def test_prompts_section_byte_cap(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mirrors, "_PROMPTS_MAX_BYTES", 200)  # tiny budget → keep only newest
    cwd = str(env.git_base / "home" / "ccc")
    sid = _running_session(env, "Fat prompts")
    _write_transcript(env, cwd, sid, ["x" * 100 for _ in range(10)])  # 10 × ~100 bytes

    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "Fat prompts", sid).read_text(encoding="utf-8")

    assert "_showing last 1 of 10 prompts_" in text  # only the most recent survives 200 bytes


# ---------------------------------------------------------------------------
# ## AIM history section (change 2)
# ---------------------------------------------------------------------------
def test_aim_history_section(env: Env) -> None:
    cwd = str(env.git_base / "home" / "ccc")
    sid = str(uuid.uuid4())
    env.store.ensure(sid, cwd=cwd)
    env.store.set_aim(sid, "First aim")
    env.store.set_aim(sid, "Second aim")
    env.store.set_aim(sid, "Third aim")
    env.store.set_short_aim(sid, "third short")  # onto the latest revision

    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "First aim", sid).read_text(encoding="utf-8")  # slug = FIRST aim

    assert "## AIM history\n\n1. " in text
    assert "First aim" in text and "Second aim" in text and "Third aim" in text
    # oldest is (1), current is (N) with the marker + its short label.
    assert "3. " in text and "· current" in text
    assert "↳ short: third short" in text
    # top section is always the FIRST aim, plus the CURRENT revision's short label.
    assert "## AIM (1)\n\nFirst aim\n\n↳ short (current): third short" in text
    assert "## AIM (3)" not in text


# ---------------------------------------------------------------------------
# first-aim slug + rename-on-next-pass of a stale current-aim mirror (change 3)
# ---------------------------------------------------------------------------
def test_slug_uses_first_aim_not_current(env: Env) -> None:
    sid = str(uuid.uuid4())
    env.store.ensure(sid, cwd=str(env.git_base / "home" / "ccc"))
    env.store.set_aim(sid, "Original loose aim")
    mirrors.run_mirrors(env.store, env.cfg)
    first_path = _rpath(env, "Original loose aim", sid)
    assert first_path.exists()

    # Sharpen the AIM mid-session — the filename must NOT churn to the new aim.
    env.store.set_aim(sid, "Much sharper and more specific aim")
    report = mirrors.run_mirrors(env.store, env.cfg)
    assert first_path.exists()  # still named after the FIRST aim
    assert not _rpath(env, "Much sharper and more specific aim", sid).exists()  # no churned name
    assert list(env.running.rglob("*.md")) == [first_path]  # exactly one file, no orphan
    assert sid in report.written  # body rewritten (AIM section grew) at the same path


def test_stale_current_aim_mirror_renamed_next_pass(env: Env) -> None:
    """A mirror left over from the old current-aim naming is renamed to first-aim slug."""
    sid = str(uuid.uuid4())
    env.store.ensure(sid, cwd=str(env.git_base / "home" / "ccc"))
    env.store.set_aim(sid, "First aim")
    env.store.set_aim(sid, "Second aim")  # current aim now differs from first

    # Simulate the pre-change on-disk state: a mirror named after the CURRENT aim.
    directory = env.running / "home" / "ccc"
    directory.mkdir(parents=True, exist_ok=True)
    stale = directory / f"{slugify('Second aim')}-{sid[:4]}.md"
    stale.write_text(
        f'---\nccc_mirror: "running"\nsession_id: "{sid}"\n---\n\nold naming\n', encoding="utf-8"
    )

    report = mirrors.run_mirrors(env.store, env.cfg)

    assert not stale.exists()  # old current-aim-slugged mirror removed (rename)
    assert str(stale) in report.removed
    assert _rpath(env, "First aim", sid).exists()  # new first-aim-slugged mirror written


# ---------------------------------------------------------------------------
# byte-stable no-op with the new sections (extends decision 5)
# ---------------------------------------------------------------------------
def test_byte_stable_noop_with_prompts_and_aim_history(env: Env) -> None:
    cwd = str(env.git_base / "home" / "ccc")
    sid = str(uuid.uuid4())
    env.store.ensure(sid, cwd=cwd)
    env.store.set_aim(sid, "Keep it stable")
    env.store.set_aim(sid, "Keep it stable and specific")
    _write_transcript(env, cwd, sid, ["one prompt", "two\nlines here"])

    r1 = mirrors.run_mirrors(env.store, env.cfg)
    path = _rpath(env, "Keep it stable", sid)  # slug = first aim
    assert sid in r1.written and path.exists()
    mtime = path.stat().st_mtime_ns

    r2 = mirrors.run_mirrors(env.store, env.cfg)
    assert r2.changed() == 0 and r2.written == [] and r2.removed == []
    assert path.stat().st_mtime_ns == mtime  # unchanged session → byte-identical no-op


# ---------------------------------------------------------------------------
# SESSION mirrors (full-conversation tree; PLAN_session-mirrors.md)
# ---------------------------------------------------------------------------
def _write_conversation(env: Env, cwd: str, sid: str) -> Path:
    """A transcript with a prompt, a tool call + paired result, and a reply."""
    munged = cwd.replace("/", "-")
    proj = env.store.path.parents[1] / "projects" / munged
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "please fix it"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "On it."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "pytest -x"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 failed"}],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "Fixed now."}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _spath(env: Env, aim: str, sid: str, *, repo: str = "home/ccc") -> Path:
    """The canonical SESSION mirror path for *sid* (4-hex prefix)."""
    return env.sessions / repo / f"{slugify(aim)}-{sid[:4]}.md"


def test_session_mirror_renders_full_conversation(env: Env) -> None:
    aim = "Fix the failing test"
    sid = _running_session(env, aim)
    _write_conversation(env, str(env.git_base / "home/ccc"), sid)
    mirrors.run_mirrors(env.store, env.cfg)
    spath = _spath(env, aim, sid)
    assert spath.exists()
    text = spath.read_text(encoding="utf-8")
    assert 'ccc_mirror: "session"' in text
    assert f'session_id: "{sid}"' in text
    assert "## (1) you\n\nplease fix it" in text
    assert "## claude\n\nOn it." in text
    assert "⏺ Bash(pytest -x)" in text
    assert "⎿ 1 failed" in text
    assert "Fixed now." in text


def test_running_and_done_mirrors_link_full_session(env: Env) -> None:
    aim = "Link me up"
    sid = _running_session(env, aim)
    _write_transcript(env, str(env.git_base / "home/ccc"), sid, ["hi"])
    mirrors.run_mirrors(env.store, env.cfg)
    link = f"[[01-llm-tasks/sessions/home/ccc/{slugify(aim)}-{sid[:4]}|full session]]"
    assert link in _rpath(env, aim, sid).read_text(encoding="utf-8")

    # Finishing the job moves the running mirror to done/ — the SESSION file stays
    # at its stable path and the done mirror carries the SAME link (decision 2).
    _mark_done(env, sid)
    mirrors.run_mirrors(env.store, env.cfg)
    assert not _rpath(env, aim, sid).exists()
    assert _spath(env, aim, sid).exists()
    done_file = env.done / "home/ccc" / f"{slugify(aim)}-{sid[:4]}.md"
    assert link in done_file.read_text(encoding="utf-8")


def test_no_link_when_session_mirrors_disabled(env: Env) -> None:
    env.cfg.mirror_sessions = False
    sid = _running_session(env, "No sessions here")
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, "No sessions here", sid).read_text(encoding="utf-8")
    assert "full session" not in text
    assert not env.sessions.exists() or list(env.sessions.rglob("*.md")) == []


def test_cleanup_removes_stale_session_mirror(env: Env) -> None:
    aim = "Soon to be archived"
    sid = _running_session(env, aim)
    mirrors.run_mirrors(env.store, env.cfg)
    assert _spath(env, aim, sid).exists()
    env.store.update_fields(sid, archived=True)
    mirrors.run_mirrors(env.store, env.cfg)
    assert not _spath(env, aim, sid).exists()


def test_remove_mirror_cleans_sessions_root(env: Env) -> None:
    aim = "Unlaunch me"
    sid = _running_session(env, aim)
    mirrors.run_mirrors(env.store, env.cfg)
    removed = mirrors.remove_mirror(env.cfg, sid)
    assert str(_spath(env, aim, sid)) in removed
    assert not _spath(env, aim, sid).exists()


def test_session_file_path_helper(env: Env) -> None:
    aim = "Find my file"
    sid = _running_session(env, aim)
    mirrors.run_mirrors(env.store, env.cfg)
    hit = mirrors.session_file_path(env.cfg, sid)
    assert hit is not None
    assert hit.abs_path == _spath(env, aim, sid)
    assert hit.vault_relpath == f"01-llm-tasks/sessions/home/ccc/{slugify(aim)}-{sid[:4]}.md"
    assert mirrors.session_file_path(env.cfg, "not-a-session") is None


def test_spawn_sync_mirrors_gates_on_mirror_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """O2: the lifecycle spawn fires when ONLY mirror_sessions is enabled."""
    from command_center import cli, spawn

    calls: list[list[str]] = []
    monkeypatch.setattr(spawn, "spawn_ccc", lambda argv: calls.append(list(argv)))
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    cfg = config.Config(mirror_running=False, mirror_done=False, mirror_sessions=True)
    cli._spawn_sync_mirrors(cfg)
    assert calls == [["sync-mirrors"]]
    cfg = config.Config(mirror_running=False, mirror_done=False, mirror_sessions=False)
    cli._spawn_sync_mirrors(cfg)
    assert calls == [["sync-mirrors"]]  # fully off → no second spawn


def test_prompt_fences_escaped_in_prompts_section(env: Env) -> None:
    """A pasted ``` fence in a prompt must not swallow the mirror's tail (Transcript)."""
    aim = "Fence safety"
    sid = _running_session(env, aim)
    _write_transcript(
        env,
        str(env.git_base / "home/ccc"),
        sid,
        ["```% bash x.sh\nstuff\nmain```; tail prose"],
    )
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, aim, sid).read_text(encoding="utf-8")
    assert "\\```% bash x.sh" in text
    assert "|full session]]" in text  # the tail structure survives the paste
    # The session mirror escapes the same paste in its ## (N) you body.
    stext = _spath(env, aim, sid).read_text(encoding="utf-8")
    assert "\\```% bash x.sh" in stext


def test_mirror_frontmatter_carries_both_links(env: Env) -> None:
    """The full-session wikilink + transcript path are Obsidian PROPERTIES (top of note)."""
    aim = "Props links"
    sid = _running_session(env, aim)
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, aim, sid).read_text(encoding="utf-8")
    link = f"01-llm-tasks/sessions/home/ccc/{slugify(aim)}-{sid[:4]}"
    assert f'session: "[[{link}|full session]]"' in text
    munged = str(env.git_base / "home/ccc").replace("/", "-")
    jsonl = env.store.path.parents[1] / "projects" / munged / f"{sid}.jsonl"
    assert f'transcript: "{jsonl}"' in text
    # The SESSION mirror carries the transcript property too.
    stext = _spath(env, aim, sid).read_text(encoding="utf-8")
    assert f'transcript: "{jsonl}"' in stext
    # With session mirrors disabled the property stays present but empty.
    env.cfg.mirror_sessions = False
    mirrors.run_mirrors(env.store, env.cfg)
    text = _rpath(env, aim, sid).read_text(encoding="utf-8")
    assert 'session: ""' in text
