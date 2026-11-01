"""``ccc continue-session`` (tp#1300): the read-only verbs on fixture transcripts."""

# pylint: disable=too-many-lines  # one module covers every continue-session verb

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from command_center import cli, continue_session, handoff_doc, inflight
from command_center.adapters import claude as claude_adapter
from command_center.models import LiveSession
from command_center.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "inflight"
PARENT = FIXTURES / "proj" / "sess-0001.jsonl"
SID = "sess-0001"


def _ps_table() -> dict[int, list[tuple[int, str]]]:
    children: dict[int, list[tuple[int, str]]] = {}
    for line in (FIXTURES / "ps.txt").read_text(encoding="utf-8").splitlines():
        pid, ppid, command = line.split(None, 2)
        children.setdefault(int(ppid), []).append((int(pid), command))
    return children


class _Adapter(claude_adapter.ClaudeAdapter):
    """The real adapter on fixture data: one live session, the fixture transcript."""

    transcript: Path | None = PARENT

    def discover(self) -> list[LiveSession]:
        return [
            LiveSession(pid=4242, session_id=SID, cwd="/repo", alive=True, config_dir="/cfg/work")
        ]

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        del cwd, session_id, config_dir
        return self.transcript


@pytest.fixture(name="fixture_session")
def _fixture_session(monkeypatch: pytest.MonkeyPatch) -> _Adapter:
    adapter = _Adapter()
    monkeypatch.setattr(continue_session, "_adapter", lambda: adapter)
    monkeypatch.setattr(claude_adapter, "_children_map", _ps_table)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.set_aim(SID, "tp#1 shipped")
        store.set_subgoals(SID, ["build", "test", "ship"])
        first = store.list_subgoals(SID)[0]
        store.set_subgoal_checked(first.id, True)
        store.update_fields(SID, no_codex=True, model="opus-5")
    return adapter


@pytest.mark.usefixtures("fixture_session")
def test_state_json_reports_everything_the_handoff_needs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["continue-session", "state", "-s", SID, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session_id"] == SID and data["cwd"] == "/repo"
    assert data["live_pid"] == 4242
    assert data["aim"] == "tp#1 shipped" and data["aim_rev"] == 1
    assert data["subgoals"] == {"unchecked": ["test", "ship"], "checked": ["build"]}
    assert data["no_codex"] is True and data["model"] == "opus-5"
    assert data["active_transfer"] is None
    inv = data["inflight"]
    assert inv["clear"] is False
    kinds = {item["id"]: item["kind"] for item in inv["items"]}
    assert kinds["a1"] == inflight.KIND_SUBAGENT
    assert kinds["b1"] == inflight.KIND_SHELL
    assert kinds["k1"] == inflight.KIND_KEEPWARM


@pytest.mark.usefixtures("fixture_session")
def test_state_text_lists_items(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["continue-session", "state", "-s", SID]) == 0
    out = capsys.readouterr().out
    assert "in flight: NOT clear" in out
    assert "[ ] test" in out and "[x] build" in out
    assert "subagent  a1" in out


def test_template_prints_the_package_template(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["continue-session", "template"]) == 0
    assert capsys.readouterr().out == handoff_doc.template()


def test_drain_times_out_while_work_is_in_flight(
    fixture_session: _Adapter, capsys: pytest.CaptureFixture[str]
) -> None:
    del fixture_session
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "0", "-j"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["drained"] is False and data["reasons"]


def test_drain_returns_once_clear(
    fixture_session: _Adapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean = tmp_path / "clean.jsonl"
    clean.write_text('{"type": "user", "message": {"content": "hi"}}\n', encoding="utf-8")
    fixture_session.transcript = clean
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "5"]) == 0
    assert "in flight: clear" in capsys.readouterr().out


def test_drain_clears_a_stale_subagent_counter(
    fixture_session: _Adapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A counter left by a TaskStop-killed agent no longer vetoes; drain resets it."""
    clean = tmp_path / "clean.jsonl"
    clean.write_text('{"type": "user", "message": {"content": "hi"}}\n', encoding="utf-8")
    fixture_session.transcript = clean
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    with Store() as store:
        store.bump_subagents(SID, 1)
        store.update_fields(SID, subagent_started_at=1)  # long ago
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "0"]) == 0
    captured = capsys.readouterr()
    assert "in flight: clear" in captured.out
    assert "stale subagent counter (1) cleared" in captured.err
    with Store() as store:
        assert store.active_subagents(SID) == 0


def test_unreadable_transcript_never_drains(
    fixture_session: _Adapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture_session.transcript = tmp_path / "missing.jsonl"
    monkeypatch.setattr(claude_adapter, "_children_map", dict)
    assert cli.main(["continue-session", "drain", "-s", SID, "-t", "0"]) == 1


@pytest.mark.parametrize("verb", [[], ["state"], ["template"], ["drain"]])
def test_help_exits_zero(verb: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["continue-session", *verb, "-h"])
    assert exc.value.code == 0
    assert "usage: ccc continue-session" in capsys.readouterr().out


# ------------------------------------------------------------------------- start


def _handoff_text(**override: str) -> str:
    parts = ["# Handoff", ""]
    for title in handoff_doc.REQUIRED_SECTIONS:
        parts += [f"## {title}", "", override.get(title, f"text for {title}"), ""]
    return "\n".join(parts)


class _Harness:
    """Two accounts, an inside-the-session caller, a clean transcript, a fake launcher."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: _Adapter):
        from scrubstub import stub_scrubber  # pylint: disable=import-outside-toplevel

        from command_center import accounts, config, terminal

        self.tmp = tmp_path
        self.private, self.work = tmp_path / "private", tmp_path / "work"
        dirs = {"private": self.private, "work": self.work}
        monkeypatch.setattr(config, "claude_config_dirs", lambda: dirs)
        monkeypatch.setattr(config, "claude_account_email_map", dict)
        emails = {str(self.private): "p@example.org", str(self.work): "w@example.org"}
        monkeypatch.setattr(accounts, "account_email", lambda d: emails.get(str(d)))
        monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a: True)
        monkeypatch.setattr(accounts, "is_trusted", lambda *_a: True)
        monkeypatch.setattr(accounts, "env_config_dir", lambda: str(self.work))
        monkeypatch.setattr(terminal, "ps_table", dict)
        monkeypatch.setattr(claude_adapter, "_children_map", dict)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        clean = tmp_path / "clean.jsonl"
        clean.write_text('{"type": "user", "message": {"content": "hi"}}\n', encoding="utf-8")
        adapter.transcript = clean
        stub = stub_scrubber(tmp_path)
        original = handoff_doc.validate
        monkeypatch.setattr(
            handoff_doc,
            "validate",
            lambda raw, scrub_policy=None: original(raw, scrub_policy=stub.scrub_cmd),
        )
        from command_center import handoff_facts  # pylint: disable=import-outside-toplevel

        real_scrubber = handoff_facts.Scrubber
        monkeypatch.setattr(
            handoff_facts, "Scrubber", lambda policy=None: real_scrubber(policy or stub.scrub_cmd)
        )
        self.launches: list[str] = []
        self.launcher = "iterm_api"
        monkeypatch.setattr(continue_session, "_launch", self._launch)
        self.accept_on_sleep = True
        monkeypatch.setattr(continue_session, "_sleep", self._sleep)
        self.file = tmp_path / "handoff.md"
        self.file.write_text(_handoff_text(), encoding="utf-8")

    def _launch(self, target: str) -> str:
        self.launches.append(target)
        return self.launcher

    def _sleep(self, _seconds: float) -> None:
        """The successor accepts while the source waits (what a real tab would do)."""
        if not self.accept_on_sleep:
            return
        from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

        with Store() as store:
            active = store.active_transfer(SID)
            if active is not None:
                store.transition_transfer(active.transfer_id, ACCEPTED, 1)

    def start(self, *extra: str) -> int:
        return cli.main(
            ["continue-session", "start", "-A", "private", "-f", str(self.file), *extra]
        )


@pytest.fixture(name="harness")
def _harness(
    fixture_session: _Adapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Harness:
    return _Harness(tmp_path, monkeypatch, fixture_session)


def test_start_hands_off_to_a_new_session_and_finalizes(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.start() == 0
    out = capsys.readouterr()
    assert "finalized" in out.out and "Make no further tool calls" in out.out
    assert "account changes work → private" in out.err
    with Store() as store:
        (transfer,) = store.transfers_of(SID)
        target = transfer.target_session
        assert harness.launches == [target] and target != SID
        assert transfer.state == "finalized" and transfer.launcher == "iterm_api"
        assert transfer.target_config_dir == str(harness.private)
        stored = Path(transfer.handoff_path).read_text(encoding="utf-8")
        model = handoff_doc.parse_model_file(harness.file.read_bytes())
        assert transfer.facts_snapshot
        assert transfer.prompt_sha256 == handoff_doc.model_digest(model, transfer.facts_snapshot)
        assert "AIM (done-condition): tp#1 shipped" in stored
        assert oct(Path(transfer.handoff_path).stat().st_mode & 0o777) == "0o600"
        job = store.get(target)
        assert job is not None and job.draft and job.no_codex
        assert job.config_dir == str(harness.private)
        assert job.aim == "tp#1 shipped" and job.llm_overseer == "opus-5"
        assert job.handed_off_from == SID
        assert (job.prompt or "").startswith("[handoff] You take over")
        assert f"ccc continue-session accept {transfer.transfer_id}" in (job.prompt or "")
        assert (job.prompt or "").endswith(stored.strip())
        assert [g.text for g in store.list_subgoals(target)] == ["test", "ship"]
        source = store.get(SID)
        assert source is not None
        assert source.aim == f"handoff accepted by session {target[:8]}"
        assert "tp#" not in source.aim
        assert [rev.aim for rev in store.list_aim_history(SID)][0] == "tp#1 shipped"
        assert source.handed_off_to == target and source.status == "handed_off"
        assert source.close_requested_at > 0 and not source.done


def test_dry_run_uses_session_id_never_resume(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.start("-n", "-j") == 0
    data = json.loads(capsys.readouterr().out)
    assert "--session-id" in data["argv"] and "--resume" not in data["argv"]
    assert data["argv"][-1].startswith("[handoff]")
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []
    from command_center import config  # pylint: disable=import-outside-toplevel

    assert not (config.app_home() / "handoffs").exists()  # a dry run writes nothing


def test_launcher_failure_leaves_the_source_untouched(harness: _Harness) -> None:
    harness.launcher = ""
    assert harness.start() == 1
    with Store() as store:
        (transfer,) = store.transfers_of(SID)
        assert transfer.state == "failed" and transfer.failure.startswith("launch:")
        source = store.get(SID)
        assert source is not None
        assert source.aim == "tp#1 shipped" and not source.handed_off_to
        assert not source.close_requested_at
        job = store.get(transfer.target_session)
        assert job is not None and job.archived


def test_timeout_leaves_both_open_and_a_retry_reuses_the_transfer(harness: _Harness) -> None:
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    with Store() as store:
        (first,) = store.transfers_of(SID)
        assert first.state == "opened"
        source = store.get(SID)
        assert source is not None and not source.handed_off_to
    harness.accept_on_sleep = True
    assert harness.start() == 0
    with Store() as store:
        (again,) = store.transfers_of(SID)
        assert again.transfer_id == first.transfer_id and again.state == "finalized"
    assert harness.launches == [first.target_session] * 2  # still a draft: relaunched


def test_retry_with_a_different_handoff_names_the_field(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    harness.file.write_text(_handoff_text(**{"Current state": "changed"}), encoding="utf-8")
    assert harness.start("-w", "0") == 1
    assert "different prompt_sha256" in capsys.readouterr().err


def test_rerun_after_acceptance_finalizes(harness: _Harness) -> None:
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    with Store() as store:
        (transfer,) = store.transfers_of(SID)
        store.transition_transfer(transfer.transfer_id, ACCEPTED, 1)
    assert harness.start("-w", "0") == 0
    with Store() as store:
        assert store.transfers_of(SID)[0].state == "finalized"
    assert len(harness.launches) == 1


def _model_file(**override: str) -> str:
    parts = ["# Handoff", ""]
    for title in handoff_doc.MODEL_SECTIONS:
        parts += [f"## {title}", "", override.get(title, f"mine: {title}"), ""]
    return "\n".join(parts)


def test_start_composes_a_three_section_model_part_with_the_facts(
    harness: _Harness,
) -> None:
    harness.file.write_text(_model_file(), encoding="utf-8")
    assert harness.start() == 0
    with Store() as store:
        (transfer,) = store.transfers_of(SID)
        stored = Path(transfer.handoff_path).read_text(encoding="utf-8")
        job = store.get(transfer.target_session)
    found = handoff_doc.sections(stored)
    assert "AIM (done-condition): tp#1 shipped" in found["goal and done-condition"]
    assert "[ ] sub-goal: test" in found["goal and done-condition"]
    assert found["first steps"] == "mine: First steps"
    assert "predecessor transcript" in found["current state"].lower()
    assert transfer.facts_snapshot
    from command_center import handoff_facts  # pylint: disable=import-outside-toplevel

    path = handoff_facts.facts_dir(SID) / f"{transfer.facts_snapshot[:12]}.json"
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    assert job is not None and f"continue-session facts {SID}" in (job.prompt or "")
    assert "Decide before restarting" in (job.prompt or "")


def test_an_eleven_kb_model_part_is_refused_before_anything_is_written(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.file.write_text(_model_file(**{"Current state": "y" * 11000}), encoding="utf-8")
    assert harness.start() == 1
    assert "the cap is" in capsys.readouterr().err
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_waiting_then_a_ledger_change_then_the_same_start_reuses_the_transfer(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import handoff_facts  # pylint: disable=import-outside-toplevel

    harness.file.write_text(_model_file(), encoding="utf-8")
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    with Store() as store:
        (first,) = store.transfers_of(SID)
        stored = Path(first.handoff_path).read_text(encoding="utf-8")
    assert first.facts_snapshot

    def _no_recollection(*_a: object, **_k: object) -> None:
        raise AssertionError("a retry must reuse the stored snapshot, not collect again")

    monkeypatch.setattr(handoff_facts, "collect", _no_recollection)
    with Store() as store:  # the ledger moves on: sub-goals and AIM facts differ now
        store.set_subgoals(SID, ["something else"])
    harness.accept_on_sleep = True
    assert harness.start() == 0
    with Store() as store:
        (again,) = store.transfers_of(SID)
    assert again.transfer_id == first.transfer_id and again.state == "finalized"
    assert again.facts_snapshot == first.facts_snapshot
    assert Path(again.handoff_path).read_text(encoding="utf-8") == stored


def test_a_retry_with_another_model_file_still_names_the_digest(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.file.write_text(_model_file(), encoding="utf-8")
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    harness.file.write_text(_model_file(**{"First steps": "different"}), encoding="utf-8")
    assert harness.start("-w", "0") == 1
    assert "different prompt_sha256" in capsys.readouterr().err


def test_a_transfer_from_before_snapshots_keeps_the_whole_document_path(
    harness: _Harness,
) -> None:
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    with Store() as store:
        (first,) = store.transfers_of(SID)
        store.conn.execute(
            "UPDATE transfers SET facts_snapshot = NULL, prompt_sha256 = ?",
            (handoff_doc.validate(harness.file.read_bytes()).sha256,),
        )
        store.conn.commit()
    harness.accept_on_sleep = True
    assert harness.start() == 0
    with Store() as store:
        assert store.transfers_of(SID)[0].transfer_id == first.transfer_id


def test_start_refuses_from_another_shell(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del harness
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "someone-else")
    assert cli.main(["continue-session", "start", "-A", "private", "-f", "x", "-s", SID]) == 1
    assert capsys.readouterr().err


def test_start_refuses_while_work_is_in_flight(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness_adapter = continue_session._adapter()  # noqa: SLF001
    assert isinstance(harness_adapter, _Adapter)
    harness_adapter.transcript = PARENT
    assert harness.start() == 1
    assert "work is still in flight" in capsys.readouterr().err
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_start_refuses_an_invalid_handoff(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.file.write_text("# nothing\n", encoding="utf-8")
    assert harness.start() == 1
    assert "missing section" in capsys.readouterr().err
    assert harness.launches == []


# ------------------------------------------------------------------ accept / status


def _opened_transfer(harness: _Harness) -> str:
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    with Store() as store:
        (transfer,) = store.transfers_of(SID)
    return transfer.transfer_id


def _as_successor(monkeypatch: pytest.MonkeyPatch, harness: _Harness, tid: str) -> str:
    from command_center import accounts  # pylint: disable=import-outside-toplevel

    with Store() as store:
        transfer = store.get_transfer(tid)
    assert transfer is not None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", transfer.target_session)
    monkeypatch.setattr(accounts, "env_config_dir", lambda: str(harness.private))
    return transfer.target_session


def _hooks(monkeypatch: pytest.MonkeyPatch, *hooks: list[str]) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    cfg = config.Config(handoff_hooks=[list(h) for h in hooks])
    monkeypatch.setattr(config, "load_config", lambda: cfg)


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _state(tid: str) -> str:
    with Store() as store:
        transfer = store.get_transfer(tid)
    assert transfer is not None
    return transfer.state


def test_accept_runs_hooks_with_placeholders_then_accepts(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    log = tmp_path / "argv.log"
    hook = _script(
        tmp_path,
        "hook.sh",
        f'echo "$@" > {log}\necho \'{{"ok": true, "items": [], "next_steps": ["tp show 1"]}}\'\n',
    )
    _hooks(monkeypatch, [hook, "-S", "{old}", "--to", "{new}", "{label}", "{transfer}"])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert log.read_text(encoding="utf-8").split() == ["-S", SID, "--to", new, "private", tid]
    assert "next: tp show 1" in capsys.readouterr().out
    assert _state(tid) == "accepted"
    assert cli.main(["continue-session", "accept", tid]) == 0  # duplicate: idempotent
    assert _state(tid) == "accepted"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("exit 3\n", "exit 3"),
        ("echo not json\n", "not JSON"),
        ("echo '{\"ok\": false}'\n", '"ok": true'),
        ("head -c 70000 /dev/zero | tr '\\0' x\n", "exceeds"),
        ("sleep 5\n", "timed out"),
    ],
)
def test_a_failing_hook_leaves_the_transfer_opened(  # pylint: disable=too-many-positional-arguments
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: str,
    why: str,
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    monkeypatch.setattr(continue_session, "HOOK_TIMEOUT_SEC", 1)
    _hooks(monkeypatch, [_script(tmp_path, "bad.sh", body)])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    assert why in capsys.readouterr().err
    assert _state(tid) == "opened"
    with Store() as store:
        transfer = store.get_transfer(tid)
        source = store.get(SID)
    assert transfer is not None and why in transfer.failure
    assert source is not None and not source.handed_off_to


def test_accept_refuses_the_wrong_session_and_the_wrong_account(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from command_center import accounts  # pylint: disable=import-outside-toplevel

    tid = _opened_transfer(harness)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1  # still the SOURCE session
    assert "must run inside the successor" in capsys.readouterr().err
    _as_successor(monkeypatch, harness, tid)
    monkeypatch.setattr(accounts, "env_config_dir", lambda: str(harness.work))
    assert cli.main(["continue-session", "accept", tid]) == 1
    assert "does not bill" in capsys.readouterr().err
    assert _state(tid) == "opened"
    assert cli.main(["continue-session", "accept", "t-missing"]) == 1


def _arm_await(session_id: str) -> int:
    from command_center.await_store import SourceSpec  # pylint: disable=import-outside-toplevel

    with Store() as store:
        return store.arm_await(
            session_id,
            config_dir="/cfg/work",
            cwd="/repo",
            no_codex=False,
            prompt_template="got {event}",
            until_epoch=2_000_000_000,
            sources=[SourceSpec(kind="cmd", spec={"cmd": "true"})],
            now=1_800_000_000,
        )


def _group(gid: int) -> Any:
    with Store() as store:
        group = store.get_await_group(gid)
    assert group is not None
    return group


def _held_by(session_id: str) -> int:
    """A group *session_id* already holds by a path the arm-time refusal does not cover
    (a build before it): armed on a scratch session, then moved by hand."""
    with Store() as store:
        store.ensure("scratch-holder", cwd="/repo")
    gid = _arm_await("scratch-holder")
    with Store() as store:
        store.conn.execute("UPDATE await_groups SET session_id = ? WHERE id = ?", (session_id, gid))
        store.conn.commit()
    return gid


def _ok_hook(tmp_path: Path, log: Path) -> str:
    return _script(tmp_path, "ok.sh", f"echo ran >> {log}\necho '{{\"ok\": true}}'\n")


def test_a_failing_hook_moves_no_await_and_a_retry_moves_it_exactly_once(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gid = _arm_await(SID)
    tid = _opened_transfer(harness)
    assert _group(gid).transfer_id == tid  # reserved by start, with the transfer
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_script(tmp_path, "bad.sh", "exit 3\n")])
    assert cli.main(["continue-session", "accept", tid]) == 1
    assert _state(tid) == "opened"
    group = _group(gid)
    assert (group.session_id, group.transfer_id) == (SID, tid)  # no rebind
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [_ok_hook(tmp_path, log)])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["next_steps"] == [
        f"await group {gid} (armed) now wakes this session (`ccc await -l` lists it)"
    ]
    group = _group(gid)
    assert (group.session_id, group.transfer_id) == (new, "")
    assert (group.config_dir, group.cwd, group.no_codex) == (str(harness.private), "/repo", True)
    assert _state(tid) == "accepted"
    assert cli.main(["continue-session", "accept", tid]) == 0  # a retry after success
    assert "await group" not in capsys.readouterr().out
    with Store() as store:
        assert len(store.list_awaits(include_inactive=True)) == 1
        assert store.active_await(SID) is None
    assert _group(gid).updated_at == group.updated_at  # moved exactly once


def test_accept_refuses_before_any_hook_when_the_successor_holds_an_await(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gid = _arm_await(SID)
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    held = _held_by(new)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [_ok_hook(tmp_path, log)])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    err = capsys.readouterr().err
    assert f"await group {held}" in err and f"ccc await -d {held}" in err
    assert not log.exists()  # refused before any hook ran
    assert _state(tid) == "opened"
    assert (_group(gid).session_id, _group(gid).transfer_id) == (SID, tid)


def test_accept_rebinds_a_blocked_await(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gid = _arm_await(SID)
    with Store() as store:
        assert store.block_group(gid, "no viable source", 1, from_states=("armed",))
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_ok_hook(tmp_path, tmp_path / "ran.log")])
    assert cli.main(["continue-session", "accept", tid]) == 0
    group = _group(gid)
    assert (group.state, group.session_id, group.blocked_reason) == (
        "blocked",
        new,
        "no viable source",
    )


# ------------------------------------------------------------------ late arms (D13 e-g)
def test_a_group_armed_after_start_is_reserved_at_arm_and_moves_on_accept(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid = _opened_transfer(harness)  # the source held no group at start
    gid = _arm_await(SID)  # OLD arms after start returned EXIT_WAITING
    assert _group(gid).transfer_id == tid  # reserved in the arm transaction
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_ok_hook(tmp_path, tmp_path / "ran.log")])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["next_steps"] == [
        f"await group {gid} (armed) now wakes this session (`ccc await -l` lists it)"
    ]
    group = _group(gid)
    assert (group.session_id, group.config_dir, group.transfer_id) == (
        new,
        str(harness.private),
        "",
    )


def test_arming_the_pending_successor_or_the_retired_source_is_refused(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from command_center.await_store import AwaitRefused  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    with pytest.raises(AwaitRefused, match=f"accept {tid}"):
        _arm_await(new)
    _hooks(monkeypatch, [_ok_hook(tmp_path, tmp_path / "ran.log")])
    assert cli.main(["continue-session", "accept", tid]) == 0
    with pytest.raises(AwaitRefused, match="handed off"):
        _arm_await(SID)
    gid = _arm_await(new)  # the successor owns the work now: it may wait
    with Store() as store:
        assert store.list_awaits(SID, include_inactive=True) == []
        assert [g.id for g, _ in store.list_awaits(new)] == [gid]


@pytest.mark.parametrize("reserved_at_arm", [True, False])
def test_a_late_arm_colliding_with_the_successors_group_refuses_before_any_hook(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reserved_at_arm: bool,
) -> None:
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    held = _held_by(new)
    late = _arm_await(SID)  # OLD arms after start
    if not reserved_at_arm:  # a group a build without the arm-time join left unreserved
        with Store() as store:
            store.conn.execute("UPDATE await_groups SET transfer_id = NULL WHERE id = ?", (late,))
            store.conn.commit()
    owner = tmp_path / "owner"  # what a real hook (ticket ownership) would move
    hook = _script(tmp_path, "own.sh", f"echo {new} > {owner}\necho '{{\"ok\": true}}'\n")
    _hooks(monkeypatch, [hook])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    err = capsys.readouterr().err
    assert f"await group {held}" in err and f"ccc await -d {held}" in err
    assert "no hook ran" in err
    assert not owner.exists()  # ownership unchanged: the hook never ran
    assert _state(tid) == "opened"
    group = _group(late)
    assert (group.session_id, group.transfer_id) == (SID, tid if reserved_at_arm else "")
    assert _group(held).session_id == new


def test_a_fresh_delivery_refuses_accept_retryably(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import time  # pylint: disable=import-outside-toplevel

    gid = _arm_await(SID)
    event: dict[str, Any] = {"event_id": "e", "payload": "{}", "remote_epoch": 1, "watermark": "w"}
    with Store() as store:
        src = store.lease_due_sources(1_800_000_120)[0][0]
        token = store.fire_group(gid, src.id, src.lease_token, now=1_800_000_120, **event)
        assert token and store.mark_delivering(gid, token, int(time.time()))  # resuming OLD
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    log = tmp_path / "ran.log"
    _hooks(monkeypatch, [_ok_hook(tmp_path, log)])
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    err = capsys.readouterr().err
    assert f"await group {gid} is being delivered" in err and "run accept again" in err
    assert not log.exists() and _state(tid) == "opened"
    assert (_group(gid).state, _group(gid).session_id) == ("delivering", SID)
    with Store() as store:  # the delivery never finished: it ages past the stale limit
        store.conn.execute(
            "UPDATE await_groups SET updated_at = updated_at - ? WHERE id = ?", (15 * 60, gid)
        )
        store.conn.commit()
    assert cli.main(["continue-session", "accept", tid]) == 0  # the retry goes through
    group = _group(gid)
    assert (group.state, group.session_id) == ("fired", new)
    assert group.delivery_token != token
    assert log.exists()


def test_status_names_the_recovery_step(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _opened_transfer(harness)
    capsys.readouterr()
    assert cli.main(["continue-session", "status", tid, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "opened" and "relaunches it" in data["recovery"]
    assert cli.main(["continue-session", "status", "-s", SID]) == 0
    out = capsys.readouterr().out
    assert tid in out and "next:" in out
    assert cli.main(["continue-session", "status", "t-missing"]) == 1


# ------------------------------------------------------------------ capped path


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    from command_center import spawn  # pylint: disable=import-outside-toplevel

    calls: list[list[str]] = []

    def fake(args: list[str], *, internal: bool = True) -> bool:
        del internal
        calls.append(args)
        return True

    monkeypatch.setattr(spawn, "spawn_ccc", fake)
    return calls


@pytest.mark.usefixtures("fixture_session")
def test_handoff_command_reaches_the_model_on_a_seat_with_quota(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import handoff_capped, switch_intercept  # pylint: disable=C0415

    calls = _capture_spawn(monkeypatch)
    monkeypatch.setattr(handoff_capped, "is_capped", lambda *_a: (False, ""))
    for command in handoff_capped.HANDOFF_COMMANDS:
        assert switch_intercept.intercept(SID, command) is False
    assert not calls and capsys.readouterr().out == ""


@pytest.mark.usefixtures("fixture_session")
def test_capped_with_work_in_flight_refuses_and_names_the_cache_write_hit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import switch_intercept  # pylint: disable=import-outside-toplevel

    calls = _capture_spawn(monkeypatch)
    monkeypatch.setenv("CCC_HANDOFF_ASSUME_CAPPED", "1")
    assert switch_intercept.intercept(SID, "/cwork-to-cpriv") is True
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "/cwork-to-cpriv-cache-write-hit" in out["reason"] and "in flight" in out["reason"]
    assert not calls


def test_capped_and_clear_spawns_the_detached_start(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import switch_intercept  # pylint: disable=import-outside-toplevel

    del harness
    calls = _capture_spawn(monkeypatch)
    monkeypatch.setenv("CCC_HANDOFF_ASSUME_CAPPED", "1")
    capsys.readouterr()
    assert switch_intercept.intercept(SID, "/cwork-to-cpriv") is True
    assert calls == [["continue-session", "start", "--capped", "-A", "private", "-s", SID]]
    assert "a NEW session opens on private" in json.loads(capsys.readouterr().out)["reason"]
    # the same account as the capped seat: refused, pointing to the other seat
    assert switch_intercept.intercept(SID, "/cwork-continue-in-new-session") is True
    assert "/cwork-to-cpriv-cache-write-hit" in json.loads(capsys.readouterr().out)["reason"]
    assert len(calls) == 1


def _router(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    cfg = config.Config(llm_custom_command=command)
    monkeypatch.setattr(config, "load_config", lambda: cfg)


_SUPPLEMENT = {
    "current_state": "parser half done",
    "decisions": "keep the cache",
    "first_steps": "run the tests",
}


def _stored_text(transfer_id_of: str = SID) -> tuple[str, Any]:
    with Store() as store:
        (transfer,) = store.transfers_of(transfer_id_of)
    return Path(transfer.handoff_path).read_text(encoding="utf-8"), transfer


def test_capped_start_writes_the_handoff_through_the_router(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    purpose = tmp_path / "purpose.txt"
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(_SUPPLEMENT), encoding="utf-8")
    _router(monkeypatch, f"printenv CCC_LLM_PURPOSE > {purpose}; cat {answer}")
    argv = ["continue-session", "start", "--capped", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0
    assert purpose.read_text(encoding="utf-8").strip() == "handoff-summary"
    stored, transfer = _stored_text()
    assert transfer.capped and transfer.state == "finalized" and transfer.facts_snapshot
    found = handoff_doc.sections(stored)
    assert "parser half done" in found["current state"]
    assert found["first steps"] == "run the tests"
    assert "AIM (done-condition): tp#1 shipped" in found["goal and done-condition"]
    with Store() as store:
        job = store.get(transfer.target_session)
        source = store.get(SID)
    assert job is not None and job.no_codex
    assert source is not None and source.handed_off_to == transfer.target_session
    assert not source.close_requested_at  # a capped tab has no Stop chain to close it
    assert harness.launches == [transfer.target_session]


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("exec sleep 5", "unset or failed"),
        ("echo hello", "not JSON"),
        ("", "unset or failed"),
        (
            'echo \'{"current_state": "x", "decisions": "y", "first_steps": "z", "more": "q"}\'',
            "unknown keys",
        ),
        (
            'echo \'{"current_state": "## First steps", "decisions": "y", "first_steps": "z"}\'',
            "heading",
        ),
    ],
)
def test_capped_start_falls_back_to_facts_only(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, command: str, why: str
) -> None:
    from command_center import config, handoff_capped  # pylint: disable=C0415

    monkeypatch.setattr(handoff_capped, "SUMMARY_TIMEOUT_SEC", 1)
    _router(monkeypatch, command)
    argv = ["continue-session", "start", "--capped", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0
    log = (config.app_home() / "events.log").read_text(encoding="utf-8")
    assert "relaying facts-only instead" in log and why in log
    stored, transfer = _stored_text()
    assert transfer.state == "finalized" and transfer.facts_snapshot
    assert handoff_capped.EMERGENCY_BANNER in stored
    assert handoff_doc.validate(stored).ok
    assert harness.launches == [transfer.target_session]


def test_facts_only_start_runs_no_model_and_no_router(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import handoff_capped  # pylint: disable=C0415

    def boom(_prompt: str) -> str:
        raise AssertionError("--facts-only must not call the router")

    monkeypatch.setattr(handoff_capped, "summarize", boom)
    argv = ["continue-session", "start", "--facts-only", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0
    stored, transfer = _stored_text()
    found = handoff_doc.sections(stored)
    assert handoff_capped.EMERGENCY_BANNER in found["current state"]
    assert "predecessor transcript" in found["current state"].lower()
    assert "accept" in found["first steps"]
    assert "AIM (done-condition): tp#1 shipped" in found["goal and done-condition"]
    assert handoff_doc.validate(stored).ok and transfer.state == "finalized"
    assert harness.launches == [transfer.target_session]


def test_facts_only_refuses_a_file_or_capped(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.start("--facts-only") == 1
    assert "takes no -f/--file" in capsys.readouterr().err
    with Store() as store:
        assert store.transfers_of(SID) == []


class _Clock:
    """A fake clock: ``_sleep`` advances it and lets a test mutate the transcript."""

    def __init__(self, harness: _Harness, grow_until: int) -> None:
        self.harness, self.grow_until, self.slept = harness, grow_until, 0
        self.transcript = harness.tmp / "clean.jsonl"

    def sleep(self, seconds: float) -> None:
        self.harness._sleep(seconds)  # pylint: disable=protected-access  # the accepting tab
        self.slept += 1
        if self.slept <= self.grow_until:  # the transcript keeps changing: not still
            with self.transcript.open("a", encoding="utf-8") as handle:
                handle.write("{}\n")


def test_await_idle_with_a_busy_session_exits_without_a_transfer(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock(harness, grow_until=10**6)
    monkeypatch.setattr(continue_session, "_sleep", clock.sleep)
    argv = ["continue-session", "start", "--facts-only", "--await-idle", "-A", "private", "-s", SID]
    assert cli.main(argv) == 1
    assert "not provably idle" in capsys.readouterr().err
    assert clock.slept == continue_session.IDLE_WAIT_SEC // continue_session.IDLE_POLL_SEC
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_await_idle_proceeds_once_the_transcript_is_still(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(harness, grow_until=3)
    monkeypatch.setattr(continue_session, "_sleep", clock.sleep)
    monkeypatch.setattr(continue_session, "_idle_now", lambda *_a: True)
    argv = ["continue-session", "start", "--facts-only", "--await-idle", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0
    still = continue_session.IDLE_STABLE_SEC // continue_session.IDLE_POLL_SEC
    assert clock.slept >= 3 + still
    assert len(harness.launches) == 1


def test_llm_routing_lists_the_handoff_summary_purpose() -> None:
    from command_center import config, llmrouting  # pylint: disable=import-outside-toplevel

    assert "handoff-summary" in llmrouting.PURPOSES
    rows = llmrouting.rows(config.Config(llm_custom_command="ai prompt"))
    assert any(row.purpose == "handoff-summary" and row.enabled for row in rows)


def test_dry_run_never_writes_trust(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import accounts  # pylint: disable=import-outside-toplevel

    def boom(*_a: object) -> bool:
        raise AssertionError("dry run must not write the trust flag")

    monkeypatch.setattr(accounts, "ensure_trusted", boom)
    assert harness.start("-n") == 0


# ------------------------------------------------- a failed required facts provider (tp#1520)


def _failing_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str) -> Path:
    """Configure ONE required provider that exits 1 and counts its runs in a file."""
    import sys  # pylint: disable=import-outside-toplevel

    from command_center import config  # pylint: disable=import-outside-toplevel

    counter = tmp_path / "provider-runs.txt"
    script = tmp_path / "failing_provider.py"
    script.write_text(
        f"import sys\nopen({str(counter)!r}, 'a', encoding='utf-8').write('x')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    spec: dict[str, object] = {
        "argv": [sys.executable, str(script)],
        "name": "req",
        "failure_section": "tp ownership",
        **extra,
    }
    cfg = config.Config(handoff_facts=[spec])
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    return counter


_LOUD = "⚠ required facts provider req failed (exit 1): the handoff marks its section UNKNOWN"


def test_facts_only_dry_run_with_a_failed_required_provider_says_so(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    del harness  # the fixture sets up the accounts and the session
    _failing_required(monkeypatch, tmp_path)
    argv = ["continue-session", "start", "--facts-only", "-n", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0  # D4: the relay is never blocked
    assert _LOUD in capsys.readouterr().err


def test_a_retry_reuses_the_frozen_unknown_and_still_says_so(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from command_center import handoff_facts  # pylint: disable=import-outside-toplevel

    counter = _failing_required(monkeypatch, tmp_path)
    harness.accept_on_sleep = False
    argv = ["continue-session", "start", "--facts-only", "-A", "private", "-s", SID, "-w", "0"]
    assert cli.main(argv) == continue_session.EXIT_WAITING
    capsys.readouterr()
    with Store() as store:
        (first,) = store.transfers_of(SID)
    assert first.state in ("prepared", "opened") and first.facts_snapshot
    stored = continue_session._stored_snapshot(  # pylint: disable=protected-access
        SID, first.facts_snapshot
    )
    assert continue_session._failed_required(stored) == [  # pylint: disable=protected-access
        ("req", "exit 1")
    ]
    assert counter.read_text(encoding="utf-8") == "x"  # the provider ran once, at the start

    def _no_recollection(*_a: object, **_k: object) -> None:
        raise AssertionError("a retry must reuse the stored snapshot, not collect again")

    monkeypatch.setattr(handoff_facts, "collect", _no_recollection)
    assert cli.main(argv) == continue_session.EXIT_WAITING
    assert _LOUD in capsys.readouterr().err
    assert counter.read_text(encoding="utf-8") == "x"


def test_capped_start_with_a_failed_required_provider_logs_and_notifies(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from command_center import handoff_capped  # pylint: disable=import-outside-toplevel

    _failing_required(monkeypatch, tmp_path)  # no llm_custom_command: facts-only fallback
    logged: list[str] = []
    notified: list[str] = []
    monkeypatch.setattr(handoff_capped, "log_event", lambda _sid, detail: logged.append(detail))
    monkeypatch.setattr(handoff_capped, "notify_failure", notified.append)
    argv = ["continue-session", "start", "--capped", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0
    assert _LOUD in logged and _LOUD in notified
    assert len(harness.launches) == 1
