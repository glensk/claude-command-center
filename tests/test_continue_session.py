"""``ccc continue-session`` (tp#1300): the read-only verbs on fixture transcripts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
        assert hashlib.sha256(stored.encode()).hexdigest() == transfer.prompt_sha256
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
