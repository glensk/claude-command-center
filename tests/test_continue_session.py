"""``ccc continue-session`` (tp#1300): the read-only verbs on fixture transcripts."""

# pylint: disable=too-many-lines  # one module covers every continue-session verb

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from command_center import cli, continue_session, handoff_doc, inflight
from command_center.adapters import claude as claude_adapter
from command_center.models import (
    AIM_SET_BY_LAUNCH,
    AIM_SET_BY_TOOL,
    AIM_SET_BY_USER,
    LiveSession,
)
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
        store.set_aim(SID, "tp#1 shipped", set_by=AIM_SET_BY_USER)
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
    # tp#2032: the source observed the retired opus-5; the handoff carries its successor.
    assert data["no_codex"] is True and data["model"] == "opus-5.5"
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


class _Harness:  # pylint: disable=too-many-instance-attributes  # + the fake clock
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
        self.clock = 0.0  # the fake monotonic clock: only ``_sleep`` advances it
        monkeypatch.setattr(continue_session, "_sleep", self._sleep)
        monkeypatch.setattr(continue_session, "_monotonic", lambda: self.clock)
        self.left_background = 0  # accept's QoS step (tp#1805): counted, never real here
        monkeypatch.setattr(continue_session, "leave_darwin_background", self._leave_background)
        self.file = tmp_path / "handoff.md"
        self.file.write_text(_handoff_text(), encoding="utf-8")
        # accept spawns the detached handoff closer (tp#1582): never a real ccc in a test
        self.spawned: list[list[str]] = []
        self.spawn_ok = True
        from command_center import spawn  # pylint: disable=import-outside-toplevel

        monkeypatch.setattr(spawn, "spawn_ccc", self._spawn)

    def _spawn(self, args: list[str], *, internal: bool = True) -> bool:
        del internal
        self.spawned.append(args)
        return self.spawn_ok

    def _launch(self, target: str) -> str:
        self.launches.append(target)
        return self.launcher

    def _leave_background(self) -> None:
        self.left_background += 1

    def _sleep(self, _seconds: float) -> None:
        """The successor accepts while the source waits (what a real tab would do)."""
        self.clock += _seconds
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
        # tp#2032: a predecessor on the retired opus-5 never hands it to its successor.
        assert job.aim == "tp#1 shipped" and job.llm_overseer == "opus-5.5"
        assert job.llm_exec == "opus-5.5"
        assert job.handed_off_from == SID
        assert (job.prompt or "").startswith("[handoff] You take over")
        assert f"ccc continue-session accept {transfer.transfer_id}" in (job.prompt or "")
        assert (job.prompt or "").endswith(stored.strip())
        assert [g.text for g in store.list_subgoals(target)] == ["test", "ship"]
        source = store.get(SID)
        assert source is not None
        assert source.aim == "tp#1 shipped"  # finalize never rewrites the source AIM
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
    assert harness.start() == continue_session.EXIT_MODEL_TOO_LONG
    assert "the cap is" in capsys.readouterr().err
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_handoff_cap_refuses_a_long_model_part_naming_its_largest_section(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """tp#1681 L3c: a model-written part over 2,500 chars exits 2 and names the section."""
    big = "d" * (handoff_doc.MODEL_MAX_CHARS - 10)
    harness.file.write_text(_model_file(**{"Decisions and constraints": big}), encoding="utf-8")
    assert harness.start() == 2
    err = capsys.readouterr().err
    assert f"the cap is {handoff_doc.MODEL_MAX_CHARS}" in err
    assert "## Decisions and constraints" in err
    with Store() as store:
        assert store.transfers_of(SID) == []
    assert harness.launches == []


def test_handoff_cap_allows_a_part_at_the_cap(harness: _Harness) -> None:
    room = handoff_doc.MODEL_MAX_CHARS - len("mine: Current state") - len("mine: First steps")
    harness.file.write_text(
        _model_file(**{"Decisions and constraints": "d" * room}), encoding="utf-8"
    )
    assert harness.start() == 0


def test_handoff_cap_never_refuses_the_identical_rerun_of_a_stored_transfer(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transfer stored before the cap (or under a higher one) keeps its exit-3 retry."""
    harness.file.write_text(_model_file(**{"Current state": "s" * 3000}), encoding="utf-8")
    monkeypatch.setattr(handoff_doc, "MODEL_MAX_CHARS", 10_000)
    harness.accept_on_sleep = False
    assert harness.start("-w", "0") == continue_session.EXIT_WAITING
    monkeypatch.setattr(handoff_doc, "MODEL_MAX_CHARS", 2500)
    harness.accept_on_sleep = True
    assert harness.start() == 0


def test_handoff_cap_counts_no_template_comments() -> None:
    assert (
        handoff_doc.model_chars_error("<!-- " + "c" * 9000 + " -->\n## First steps\n\nok\n") == ""
    )


def test_preamble_keys_point_to_keyed_facts_only() -> None:
    """tp#1681 L3c: keyed facts pointers, no unkeyed ``facts -j``, no re-claim instruction."""
    transfer = type(
        "T", (), {"source_session": "src-1", "transfer_id": "t9", "handoff_path": "/h.md"}
    )()
    flat = " ".join(continue_session.preamble(transfer, "work", "/t.jsonl").split())
    assert "ccc continue-session facts src-1 -k <key>" in flat
    assert "facts src-1 -j" not in flat and " -j" not in flat
    assert "claim" not in flat.lower()
    assert "ccc continue-session accept t9" in flat


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


def test_start_drain_with_work_in_flight_exits_1_and_creates_no_transfer(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness_adapter = continue_session._adapter()  # noqa: SLF001
    assert isinstance(harness_adapter, _Adapter)
    harness_adapter.transcript = PARENT
    assert harness.start("-D", "0") == 1
    out = capsys.readouterr()
    assert "in flight: NOT clear" in out.out and "subagent  a1" in out.out
    assert "work is still in flight after 0s — no transfer started" in out.err
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_start_drain_clear_proceeds_and_says_the_accept_wait(
    harness: _Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.start("-D", "5") == 0
    out = capsys.readouterr()
    assert "finalized" in out.out and "in flight" not in out.out
    assert "accept wait: 420s (effective -w)" in out.err
    with Store() as store:
        assert store.transfers_of(SID)[0].state == "finalized"


@pytest.mark.parametrize(
    ("wait", "drain", "timeouts", "expected"),
    [
        (420, 0, [], 420),  # 540 - 0 - 0 - 30 = 510: untouched
        (420, 30, [15.0, 6.0], 420),  # 459: untouched
        (420, 60, [120.0, 120.0], 210),  # 540 - 60 - 240 - 30
        (420, 60, [120.0, 120.0, 120.0], 120),  # 90 → the 120 s floor
        (500, 30, [25.5], 454),  # 454.5 → whole seconds
        (60, 0, [], 60),  # an explicit smaller -w is kept, even below the floor
        (0, 60, [120.0] * 4, 0),
    ],
)
def test_effective_wait_clamps_to_the_tool_cap(
    wait: int, drain: int, timeouts: list[float], expected: int
) -> None:
    assert continue_session.effective_wait(wait, drain, timeouts) == expected


def test_start_clamps_the_accept_wait_by_the_provider_timeouts(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys  # pylint: disable=import-outside-toplevel

    from command_center import config  # pylint: disable=import-outside-toplevel

    quiet = [sys.executable, "-c", 'print(\'{"version": 1, "facts": []}\')']
    specs: list[dict[str, object]] = [
        {"argv": quiet, "name": "a", "timeout_s": 120, "optional": True},
        {"argv": quiet, "name": "b", "timeout_s": 100, "optional": True},
    ]
    cfg = config.Config(handoff_facts=specs)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    assert harness.start("-D", "40") == 0
    assert "accept wait: 250s (effective -w)" in capsys.readouterr().err  # 540-40-220-30


@pytest.mark.parametrize("value", ["61", "-1", "x"])
def test_start_drain_out_of_range_is_a_usage_error(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["continue-session", "start", "-A", "private", "-D", value])
    assert exc.value.code == 2
    assert "-D/--drain" in capsys.readouterr().err


def test_start_help_lists_drain(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["continue-session", "start", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "-D, --drain SECONDS" in out or "-D SECONDS, --drain SECONDS" in out
    assert "timeout 600000" in out


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


def _hooks(
    monkeypatch: pytest.MonkeyPatch,
    *hooks: list[str],
    window: int | None = None,
    timeout: int | None = None,
) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    cfg = config.Config(handoff_hooks=[list(h) for h in hooks])
    if window is not None:
        cfg.accept_hook_retry_sec = window
    if timeout is not None:
        cfg.accept_hook_timeout_sec = timeout
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
    assert _state(tid) == "finalized"
    assert cli.main(["continue-session", "accept", tid]) == 0  # duplicate: idempotent
    assert _state(tid) == "finalized"


def test_accept_carries_the_lineages_first_aim_as_the_anchor(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tp#1553: the successor's history starts with the source's FIRST AIM, its current
    AIM stays the handed-over one, and a second accept adds no duplicate row."""
    with Store() as store:
        store.set_aim(SID, "build B2: tp#1", set_by=AIM_SET_BY_USER)
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    with Store() as store:
        assert [r.aim for r in store.list_aim_history(new)] == ["build B2: tp#1"]
    _hooks(monkeypatch)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert cli.main(["continue-session", "accept", tid]) == 0
    with Store() as store:
        assert [r.aim for r in store.list_aim_history(new)] == ["tp#1 shipped", "build B2: tp#1"]
        successor = store.get(new)
        assert successor is not None
        assert successor.aim == "build B2: tp#1" and successor.first_aim == "tp#1 shipped"


def test_accept_records_the_sources_thread_on_the_successor(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tp#1989: accept copies the thread id (the source's root) onto the successor; the
    draft carries none before accept, and a second accept changes nothing."""
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    with Store() as store:
        draft = store.get(new)
        assert draft is not None and draft.thread_id is None
        store.update_fields(SID, thread_id="root-0")  # the source is itself a successor
    _hooks(monkeypatch)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert cli.main(["continue-session", "accept", tid]) == 0
    with Store() as store:
        successor = store.get(new)
        assert successor is not None and successor.thread_id == "root-0"
        assert store.thread_of(new) == "root-0"


def test_a_duplicate_accept_records_the_thread_a_crash_skipped(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    with Store() as store:
        assert store.transition_transfer(tid, ACCEPTED, 1)  # the crash right after the CAS
        draft = store.get(new)
        assert draft is not None and draft.thread_id is None
    assert cli.main(["continue-session", "accept", tid]) == 0
    with Store() as store:
        successor = store.get(new)
        assert successor is not None and successor.thread_id == SID


def test_a_duplicate_accept_carries_the_anchor_a_crash_skipped_and_never_reconciles(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tp#1991: a crash right after the accepted CAS skipped ``carry_first_aim``; the
    duplicate accept re-runs it (once), but never ``_reconcile_successor_aim``: an AIM the
    successor changed since stays, and a source AIM changed since is not pulled in."""
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    with Store() as store:
        store.set_aim(SID, "build B2: tp#1", set_by=AIM_SET_BY_USER)
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    with Store() as store:
        assert store.transition_transfer(tid, ACCEPTED, 1)  # the crash right after the CAS
        assert [r.aim for r in store.list_aim_history(new)] == ["build B2: tp#1"]
        store.set_aim(new, "the successor's own goal", set_by=AIM_SET_BY_USER)
        store.set_aim(SID, "source changed later", set_by=AIM_SET_BY_USER)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert cli.main(["continue-session", "accept", tid]) == 0
    with Store() as store:
        assert [(r.aim, r.set_by) for r in store.list_aim_history(new)] == [
            ("tp#1 shipped", "user"),
            ("build B2: tp#1", "user"),
            ("the successor's own goal", "user"),
        ]
        successor = store.get(new)
        assert successor is not None and successor.aim == "the successor's own goal"
        assert successor.first_aim == "tp#1 shipped"


def test_carry_first_aim_passes_the_root_down_the_chain() -> None:
    with Store() as store:
        for sid, aims in (("root", ["goal", "box 1"]), ("mid", ["box 2"]), ("leaf", ["box 3"])):
            store.ensure(sid, cwd="/repo")
            for aim in aims:
                store.set_aim(sid, aim)
        assert store.carry_first_aim("mid", "root")
        assert store.carry_first_aim("leaf", "mid")
        assert not store.carry_first_aim("leaf", "mid")  # idempotent
        assert not store.carry_first_aim("leaf", "nobody")  # no source history: no-op
        assert [r.aim for r in store.list_aim_history("leaf")] == ["goal", "box 3"]
        assert store.count_aim_history("leaf") == 2


def _source_aims(*revisions: tuple[str, str | None]) -> None:
    """Replace the source's AIM history with *revisions* (``(aim, set_by)``, oldest first)."""
    with Store() as store:
        store.conn.execute("DELETE FROM aim_history WHERE session_id = ?", (SID,))
        store.conn.commit()
        store.update_fields(SID, aim=None)
        for aim, set_by in revisions:
            store.set_aim(SID, aim, set_by=set_by)


def _accepted_successor(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Hand the source over and accept in the successor; return (transfer id, successor id)."""
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    assert cli.main(["continue-session", "accept", tid]) == 0
    return tid, new


def test_b6_successor_takes_the_latest_user_aim(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B6 (a): user "A", tool "A: tp#1", user "B" → current AIM "B", `/aim (1)` "A"."""
    _source_aims(("A", AIM_SET_BY_USER), ("A: tp#1", AIM_SET_BY_TOOL), ("B", AIM_SET_BY_USER))
    _, new = _accepted_successor(harness, monkeypatch)
    with Store() as store:
        successor = store.get(new)
        assert successor is not None
        assert successor.aim == "B" and successor.first_aim == "A"
        history = store.list_aim_history(new)
        assert [(r.aim, r.set_by) for r in history] == [("A", "user"), ("B", "user")]


@pytest.mark.parametrize(
    ("current", "expected"),
    [("B: tp#5, #6", "B"), ("tp#5", "A")],
    ids=["run-stripped-current", "run-only-current-falls-back-to-first"],
)
def test_b6_legacy_source_without_stamps(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, current: str, expected: str
) -> None:
    """B6 (b): unstamped history "A" then *current* → the current AIM without its tp id
    run, or the first AIM when the current one is only a run."""
    _source_aims(("A", None), (current, None))
    _, new = _accepted_successor(harness, monkeypatch)
    with Store() as store:
        successor = store.get(new)
        assert successor is not None
        assert successor.aim == expected and successor.first_aim == "A"


def test_b6_chain_keeps_the_user_aim_set_in_the_middle_session() -> None:
    """B6 (c): A→B→C — the AIM the user set in B reaches C; B's own carried stamp lets a
    tool rewrite in B fall back to it too."""
    seed = continue_session._successor_aim  # pylint: disable=protected-access
    reconcile = continue_session._reconcile_successor_aim  # pylint: disable=protected-access
    with Store() as store:
        store.ensure("a", cwd="/repo")
        store.set_aim("a", "root goal", set_by=AIM_SET_BY_USER)
        for source, target in (("a", "b"), ("b", "c")):
            if source == "b":
                store.set_aim("b", "B goal", set_by=AIM_SET_BY_USER)
                store.set_aim("b", "B goal: tp#9", set_by=AIM_SET_BY_TOOL)
            hop = SimpleNamespace(source_session=source, target_session=target, source_aim="")
            text, set_by = seed(store, hop)
            store.create_draft(target, "/repo", text, aim_set_by=set_by)
            store.carry_first_aim(target, source)
            assert not reconcile(store, hop)
            if target == "b":
                assert store.latest_user_aim("b") == ("root goal", "user")
        leaf = store.get("c")
        assert leaf is not None
        assert leaf.aim == "B goal" and leaf.first_aim == "root goal"
        assert store.latest_user_aim("c") == ("B goal", "user")


def test_b6_reconcile_successor_aim_is_idempotent(harness: _Harness) -> None:
    """B6 (d): the user changes the source AIM after `start`; the first reconcile moves
    the successor to it, the second returns False and adds no history row."""
    tid = _opened_transfer(harness)
    with Store() as store:
        store.set_aim(SID, "changed after start", set_by=AIM_SET_BY_USER)
        transfer = store.get_transfer(tid)
        assert transfer is not None
        reconcile = continue_session._reconcile_successor_aim  # pylint: disable=protected-access
        assert reconcile(store, transfer)
        rows = store.count_aim_history(transfer.target_session)
        assert not reconcile(store, transfer)
        assert store.count_aim_history(transfer.target_session) == rows
        successor = store.get(transfer.target_session)
        assert successor is not None and successor.aim == "changed after start"


def _successor_stamps(new: str) -> list[tuple[str, str | None]]:
    with Store() as store:
        return [(r.aim, r.set_by) for r in store.list_aim_history(new)]


def _null_rows() -> int:
    with Store() as store:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM aim_history WHERE set_by IS NULL"
        ).fetchone()
    return int(row["n"])


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        (
            (("A", AIM_SET_BY_LAUNCH), ("B", AIM_SET_BY_LAUNCH)),
            [("A", "launch"), ("B", "launch")],
        ),
        ((("A", None), ("B", None)), [("A", "relay"), ("B", "relay")]),
        ((("A", AIM_SET_BY_LAUNCH), ("B", AIM_SET_BY_TOOL)), [("A", "launch"), ("B", "tool")]),
    ],
    ids=["launch-only-source", "null-only-source", "tool-current-aim"],
)
def test_tp1991_successor_rows_carry_the_source_stamp(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    revisions: tuple[tuple[str, str | None], ...],
    expected: list[tuple[str, str | None]],
) -> None:
    """Without a ``user`` row the successor's rows take the matching source row's stamp;
    an unstamped source row becomes ``relay`` — never NULL."""
    _source_aims(*revisions)
    _, new = _accepted_successor(harness, monkeypatch)
    assert _successor_stamps(new) == expected


def test_tp1991_fallback_aims_are_stamped() -> None:
    """The recorded ``source_aim`` without a matching row → ``relay``; the generic pointer
    (ccc's own text) → ``tool``."""
    seed = continue_session._successor_aim  # pylint: disable=protected-access
    with Store() as store:
        store.ensure("bare", cwd="/repo")
        recorded = SimpleNamespace(source_session="bare", source_aim="recorded at start")
        assert seed(store, recorded) == ("recorded at start", "relay")
        pointer = SimpleNamespace(source_session="bare0000-0000", source_aim="")
        assert seed(store, pointer) == (
            "continue the work handed over by session bare0000",
            "tool",
        )


def test_tp1991_accept_resync_keeps_the_resolved_stamp(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source's AIM changes between ``start`` and ``accept`` (no ``user`` row): the
    re-synced successor row carries the stamp of the source row holding it."""
    _source_aims(("A", None))
    tid = _opened_transfer(harness)
    with Store() as store:
        store.set_aim(SID, "changed after start", set_by=AIM_SET_BY_LAUNCH)
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert _successor_stamps(new) == [("A", "relay"), ("changed after start", "launch")]


def test_tp1991_relay_round_trip_writes_no_null_row(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start → accept → duplicate accept on a stamped source: no NULL row anywhere in
    ``aim_history``, and the duplicate accept writes no AIM row at all."""
    assert _null_rows() == 0
    tid, new = _accepted_successor(harness, monkeypatch)
    assert _null_rows() == 0
    rows = _successor_stamps(new)
    assert rows and all(by for _aim, by in rows)
    assert cli.main(["continue-session", "accept", tid]) == 0  # the duplicate accept
    assert _successor_stamps(new) == rows
    assert _null_rows() == 0


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
    _hooks(monkeypatch, [_script(tmp_path, "bad.sh", body)], window=0, timeout=1)
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


def _relay_state_dir() -> Path:
    import os  # pylint: disable=import-outside-toplevel

    path = Path(os.environ["CC_RELAY_STATE_DIR"])  # pinned under tmp_path by conftest
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_accept_writes_the_retired_marker_0600_and_finalize_keeps_it(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import relay_state  # pylint: disable=import-outside-toplevel

    base = _relay_state_dir()
    tid = _opened_transfer(harness)
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    assert not relay_state.retired_path(SID).exists()  # nothing before the accept event
    assert cli.main(["continue-session", "accept", tid]) == 0
    marker = base / "retired" / f"{SID}.json"
    body = json.loads(marker.read_text(encoding="utf-8"))
    assert body["transfer"] == tid and body["successor"] == new and body["at"] > 0
    assert oct(marker.stat().st_mode & 0o777) == "0o600"
    assert oct(marker.parent.stat().st_mode & 0o777) == "0o700"
    assert not [p for p in marker.parent.iterdir() if p.name != marker.name]  # no temp left
    # accept finalized at once; a duplicate accept re-finalizes and keeps the marker
    assert _state(tid) == "finalized"
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert json.loads(marker.read_text(encoding="utf-8")) == body


def test_finalize_writes_a_missing_retired_marker(harness: _Harness) -> None:
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    base = _relay_state_dir()
    tid = _opened_transfer(harness)
    with Store() as store:
        store.transition_transfer(tid, ACCEPTED, 1)  # accepted before the marker existed
        transfer = store.get_transfer(tid)
    assert transfer is not None
    assert harness.start("-w", "0") == 0
    body = json.loads((base / "retired" / f"{SID}.json").read_text(encoding="utf-8"))
    assert body["transfer"] == tid and body["successor"] == transfer.target_session


def test_finalize_twice_keeps_the_same_close_token(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before the finalized CAS re-runs finalize: the close token is never replaced."""
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    with Store() as store:
        store.transition_transfer(tid, ACCEPTED, 1)
        transfer = store.get_transfer(tid)
        assert transfer is not None
        with monkeypatch.context() as patch:  # the first finalizer dies before its CAS
            patch.setattr(Store, "transition_transfer", lambda *_a, **_k: False)
            assert not continue_session.finalize(store, transfer)
        first = store.get(SID)
        assert first is not None and first.close_token and first.close_requested_at > 0
        assert store.claim_close(SID, first.close_token) > 0  # a closer holds it in flight
        assert continue_session.finalize(store, transfer)
        again = store.get(SID)
        assert again is not None and again.close_token == first.close_token
        assert again.close_requested_at == 0  # the in-flight claim was not re-armed
        assert _state(tid) == "finalized"


def test_b7_finalize_keeps_the_source_aim_and_its_history(harness: _Harness) -> None:
    """finalize never writes the source AIM: the handoff shows as handed_off + marker."""
    from command_center.models import handoff_marker  # pylint: disable=C0415
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    with Store() as store:
        store.transition_transfer(tid, ACCEPTED, 1)
        transfer = store.get_transfer(tid)
        assert transfer is not None
        before = store.get(SID)
        assert before is not None
        history = [(r.aim, r.set_by) for r in store.list_aim_history(SID)]
        assert continue_session.finalize(store, transfer)
        source = store.get(SID)
        assert source is not None
        assert source.aim == before.aim == "tp#1 shipped"
        assert [(r.aim, r.set_by) for r in store.list_aim_history(SID)] == history
        target = transfer.target_session
        assert source.handed_off_to == target and source.status == "handed_off"
        assert handoff_marker(source) == f"⇢ {target[:8]}"
    assert _state(tid) == "finalized"


def test_no_retired_marker_without_the_relay_state_dir(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import relay_state  # pylint: disable=import-outside-toplevel

    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert _state(tid) == "finalized"
    assert not relay_state.state_dir().exists()  # inert: the hooks are not installed


def test_a_retired_marker_write_failure_never_blocks_accept(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import relay_state  # pylint: disable=import-outside-toplevel

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    _relay_state_dir()
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    monkeypatch.setattr(relay_state, "write_retired", boom)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert "retired marker" in capsys.readouterr().err
    assert _state(tid) == "finalized"


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
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    err = capsys.readouterr().err
    # 0, 10, 30, 70 s of a 420 s window: the attempt cap (4) ends it
    assert "after 4 attempts" in err and "still opened" in err
    assert _state(tid) == "opened"
    with Store() as store:
        failed = store.get_transfer(tid)
    assert failed is not None and failed.failure.startswith("hook:")
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
    assert _state(tid) == "finalized"
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


# ------------------------------------------------- accept finalizes the source (tp#1582)


def _closer(tid: str) -> list[str]:
    return ["close-now", "--handoff-transfer", tid, "--session", SID]


def _source_row() -> Any:
    with Store() as store:
        row = store.get(SID)
    assert row is not None
    return row


def test_accept_finalizes_an_idle_source_whose_start_timed_out(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reported sequence: start exits 3, the source sits idle, the successor accepts —
    the transfer is finalized, lineage moves (the AIM stays), ONE closer is spawned, and
    nothing runs in the source (no second start, no launch)."""
    tid = _opened_transfer(harness)  # start timed out: the source is idle from here on
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == {
        "session": SID,
        "state": "finalized",
        "capped": False,
        "alive": True,
        "closer": "spawned",
    }
    assert _state(tid) == "finalized"
    row = _source_row()
    assert row.aim == "tp#1 shipped"  # finalize never rewrites the source AIM
    assert row.handed_off_to == new and row.status == "handed_off"
    assert row.close_token and row.close_requested_at > 0  # pending: the closer claims it
    assert harness.spawned == [_closer(tid)]
    assert len(harness.launches) == 1  # only the timed-out start ever launched


def test_accept_text_names_the_source_line(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert f"source {SID[:8]} — finalized, closing its tab when idle" in capsys.readouterr().out


def test_accept_with_the_source_gone_finalizes_only(
    harness: _Harness,
    fixture_session: _Adapter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    monkeypatch.setattr(fixture_session, "discover", list)  # no live registry entry
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert f"source {SID[:8]} gone — finalized" in capsys.readouterr().out
    assert _state(tid) == "finalized" and _source_row().handed_off_to
    assert not harness.spawned


def test_a_busy_source_start_still_running_keeps_one_token_for_its_own_stop(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start is still waiting in the source: the closer accept spawns refuses the busy
    source and hands the token back; start's own wait sees ``finalized``, and its Stop
    chain claims the SAME token."""
    from command_center import handoff_close  # pylint: disable=import-outside-toplevel
    from command_center.hooks import CLOSE_REQUEST_TTL_MS  # pylint: disable=C0415
    from command_center.models import CloseClaim, now_ms  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert harness.spawned == [_closer(tid)]
    token = _source_row().close_token

    def busy(_self: Any, session_id: str = "") -> list[LiveSession]:
        return [LiveSession(4242, session_id, "/repo", raw_status="busy", alive=True)]

    def no_kill(*_a: object) -> None:
        raise AssertionError("a busy source must never be signalled")

    monkeypatch.setattr(cli.ClaudeAdapter, "discover_raw", busy)
    monkeypatch.setattr(cli.ClaudeAdapter, "transcript_path", lambda *_a, **_k: None)
    monkeypatch.setattr(handoff_close, "_start_of", lambda _pid: "start-1")
    monkeypatch.setattr(cli.os, "kill", no_kill)
    assert cli.main(["close-now", "-H", tid, "-s", SID]) == 1  # the spawned closer, run inline
    row = _source_row()
    assert row.close_token == token and row.close_requested_at > 0  # handed back, pending
    with Store() as store:
        transfer = store.get_transfer(tid)
        assert continue_session.finalize(store, transfer)  # start's own wait: already done
        kind, claim = store.claim_after_turn(SID, now_ms(), CLOSE_REQUEST_TTL_MS)
    assert kind == "close" and isinstance(claim, CloseClaim) and claim.token == token


def test_a_capped_accept_finalizes_metadata_only(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _opened_transfer(harness)
    with Store() as store:
        store.conn.execute("UPDATE transfers SET capped = 1 WHERE transfer_id = ?", (tid,))
        store.conn.commit()
    new = _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert "finalized (capped: its tab stays open)" in capsys.readouterr().out
    assert _state(tid) == "finalized"
    row = _source_row()
    assert row.handed_off_to == new and not row.close_token and not row.close_requested_at
    assert not harness.spawned


def test_a_duplicate_accept_heals_a_crash_after_the_accepted_cas(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Accepted but never finalized (the live t1e301… state): accept again finalizes
    without re-running the hooks; a token already in flight spawns no second closer."""
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    log = tmp_path / "hook.log"
    _hooks(monkeypatch, [_ok_hook(tmp_path, log)])
    with Store() as store:
        assert store.transition_transfer(tid, ACCEPTED, 1)  # the crash right after the CAS
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    out = capsys.readouterr().out
    assert f"already accepted: transfer {tid} (accepted)" in out
    assert "closing its tab when idle" in out
    assert not log.exists()  # no hook ran again
    assert _state(tid) == "finalized" and harness.spawned == [_closer(tid)]
    with Store() as store:
        assert store.claim_close(SID, _source_row().close_token) > 0  # the closer holds it
    assert cli.main(["continue-session", "accept", tid, "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["already"] == "finalized" and data["source"]["closer"] == "in_flight"
    assert harness.spawned == [_closer(tid)]  # no needless second closer


def test_a_failed_closer_spawn_warns_and_a_retry_spawns_it(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    harness.spawn_ok = False
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert "closer did not start" in capsys.readouterr().err
    harness.spawn_ok = True
    assert cli.main(["continue-session", "accept", tid]) == 0  # token still pending
    assert harness.spawned == [_closer(tid)] * 2


def test_a_hook_failure_finalizes_nothing_and_spawns_nothing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_script(tmp_path, "bad.sh", "exit 3\n")])
    assert cli.main(["continue-session", "accept", tid]) == 1
    assert _state(tid) == "opened" and not _source_row().handed_off_to
    assert not harness.spawned


def test_a_finalize_failure_after_accept_only_warns(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sqlite3  # pylint: disable=import-outside-toplevel

    def boom(*_a: object, **_k: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch)
    monkeypatch.setattr(continue_session, "finalize", boom)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid, "-j"]) == 0  # accept succeeded
    out = capsys.readouterr()
    assert "was not finalized (database is locked)" in out.err and "never resume" in out.err
    assert json.loads(out.out)["source"]["state"] == "error"
    assert _state(tid) == "accepted" and not harness.spawned


def test_the_accepted_recovery_step_never_sends_the_source_back_to_start(
    harness: _Harness,
) -> None:
    from command_center.transfer_store import ACCEPTED  # pylint: disable=C0415

    tid = _opened_transfer(harness)
    with Store() as store:
        store.transition_transfer(tid, ACCEPTED, 1)
        transfer = store.get_transfer(tid)
    step = continue_session.recovery_step(transfer, target_is_draft=False)
    assert f"in the successor run `ccc continue-session accept {tid}` again" in step
    assert "background" in step and "never resume" in step
    assert "continue-session start" not in step


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


def test_facts_only_start_fits_a_big_snapshot(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """tp#1835: an oversized snapshot is cut to fit, never refused for its size."""
    import bigsnap  # pylint: disable=import-outside-toplevel,import-error

    from command_center import handoff_facts  # pylint: disable=import-outside-toplevel

    doc = bigsnap.big_snapshot(held=200, title_bytes=200)
    doc["session_id"] = SID
    monkeypatch.setattr(handoff_facts, "collect", lambda *_a, **_k: doc)
    argv = ["continue-session", "start", "--facts-only", "-A", "private", "-s", SID]
    assert cli.main(argv) == 0
    assert "refused" not in capsys.readouterr().err
    stored, transfer = _stored_text()
    assert len(stored.encode()) <= handoff_doc.MAX_BYTES
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


def _outside(*extra: str) -> list[str]:
    """``start -X -s SID`` from a shell that is not the source session (tp#1722)."""
    return ["continue-session", "start", "--facts-only", "-A", "private", "-X", *extra]


def test_outside_without_x_names_the_flag(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    argv = ["continue-session", "start", "--facts-only", "-A", "private", "-s", SID]
    assert cli.main(argv) == 1
    assert "not from another shell" in capsys.readouterr().err
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_outside_with_x_opens_the_transfer_of_an_idle_live_source(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "someone-else")
    clock = _Clock(harness, grow_until=0)
    monkeypatch.setattr(continue_session, "_sleep", clock.sleep)
    monkeypatch.setattr(continue_session, "_idle_now", lambda *_a: True)
    assert cli.main(_outside("-s", SID)) == 0
    assert clock.slept >= continue_session.IDLE_STABLE_SEC // continue_session.IDLE_POLL_SEC
    assert len(harness.launches) == 1
    with Store() as store:
        (transfer,) = store.transfers_of(SID)
        assert transfer.target_session == harness.launches[0]


def test_outside_resolves_from_the_registry_and_ignores_the_env(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    del harness
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)  # even the source's own env: outside
    adapter = continue_session._adapter()  # noqa: SLF001
    with Store() as store:
        src = continue_session.resolve_source(adapter, store, SID, outside=True)
        assert continue_session.resolve_source(adapter, store, None, outside=True) is None
    assert src is not None and not src.inside
    assert src.config_dir == "/cfg/work" and src.pid == 4242 and src.cwd == "/repo"
    assert src.ignore == frozenset()


def test_outside_with_x_on_a_busy_source_is_refused(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    clock = _Clock(harness, grow_until=10**6)
    monkeypatch.setattr(continue_session, "_sleep", clock.sleep)
    assert cli.main(_outside("-s", SID)) == 1
    assert "not provably idle" in capsys.readouterr().err
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_outside_with_x_on_a_closed_source_is_refused(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    adapter = continue_session._adapter()  # noqa: SLF001
    monkeypatch.setattr(adapter, "discover", list)  # no live process: pid 0
    clock = _Clock(harness, grow_until=0)
    monkeypatch.setattr(continue_session, "_sleep", clock.sleep)
    assert cli.main(_outside("-s", SID)) == 1
    assert "not a live session" in capsys.readouterr().err
    assert clock.slept == 0  # refused at once, no idle wait
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_outside_with_x_skips_only_the_inside_check_in_preflight(harness: _Harness) -> None:
    del harness
    src = continue_session.Source(session_id=SID, pid=0, config_dir="/cfg/work", cwd="/repo")
    adapter = continue_session._adapter()  # noqa: SLF001
    with Store() as store, pytest.raises(continue_session.StartRefused, match="not a live"):
        continue_session._preflight(  # noqa: SLF001
            adapter, store, src, "private", outside=True
        )
    with Store() as store, pytest.raises(continue_session.StartRefused, match="inside the"):
        continue_session._preflight(adapter, store, src, "private")  # noqa: SLF001


def test_outside_without_session_is_a_usage_error(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)  # the env never stands in for -s
    assert cli.main(_outside()) == 2
    assert "-X/--outside requires -s/--session" in capsys.readouterr().err
    assert harness.launches == []
    with Store() as store:
        assert store.transfers_of(SID) == []


def test_start_help_lists_outside(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["continue-session", "start", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "-X, --outside" in out and "start -X -s" in out


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


# ------------------------------------------- accept retries a refusing hook (tp#1521)


def _counting_hook(tmp_path: Path, name: str, fail_runs: int) -> tuple[str, Path]:
    """A hook that exits 1 on its first *fail_runs* runs, then succeeds; its run counter."""
    counter = tmp_path / f"{name}.count"
    body = (
        f'n=$(cat {counter} 2>/dev/null || echo 0)\nn=$((n + 1))\necho "$n" > {counter}\n'
        f'if [ "$n" -le {fail_runs} ]; then echo "source is working" >&2; exit 1; fi\n'
        "echo '{\"ok\": true}'\n"
    )
    return _script(tmp_path, name, body), counter


def _runs(counter: Path) -> int:
    return int(counter.read_text(encoding="utf-8").strip()) if counter.exists() else 0


def _failure(tid: str) -> str:
    with Store() as store:
        transfer = store.get_transfer(tid)
    assert transfer is not None
    return transfer.failure


def test_accept_retry_a_refusing_hook_until_it_succeeds(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    hook, counter = _counting_hook(tmp_path, "flaky.sh", fail_runs=2)
    _hooks(monkeypatch, [hook], window=60)
    during: list[str] = []
    real_sleep = continue_session._sleep  # pylint: disable=protected-access

    def _sleep(seconds: float) -> None:
        during.append(_failure(tid))
        real_sleep(seconds)

    monkeypatch.setattr(continue_session, "_sleep", _sleep)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    err = capsys.readouterr().err
    assert _runs(counter) == 3 and _state(tid) == "finalized"
    assert err.count("waiting: handoff hook failed") == 2 and "attempt 1 of at most 4" in err
    assert during[0].startswith("hook (retrying 1): flaky.sh: exit 1")  # test 7
    assert during[1].startswith("hook (retrying 2):")
    assert _failure(tid) == ""


def test_accept_retry_starts_no_attempt_past_the_window_and_bounds_each_run(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_script(tmp_path, "slow.sh", "exit 1\n")], window=60)
    timeouts: list[float] = []

    def _bounded(_argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        timeouts.append(timeout)
        harness.clock += 25  # each run takes 25 s of the window
        return 1, b"", b"refused\n"

    monkeypatch.setattr(continue_session, "run_bounded", _bounded)
    assert cli.main(["continue-session", "accept", tid]) == 1
    # attempt 1 at 0 s (full timeout), ends at 25; 25 + 10 + 20 <= 60: attempt 2 at 35 with
    # the 25 s left; it ends at 60, no room for a third
    assert timeouts == [continue_session.HOOK_TIMEOUT_SEC, 25]
    assert _state(tid) == "opened"


def _timing_out(harness: _Harness, calls: list[float], succeed_on: int | None = None) -> Any:
    """A fake ``run_bounded``: every run takes its whole budget on the fake clock and times
    out, except run number *succeed_on* (1-based), which answers ``{"ok": true}`` at once."""
    import subprocess  # pylint: disable=import-outside-toplevel

    def _bounded(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        calls.append(timeout)
        if len(calls) == succeed_on:
            return 0, b'{"ok": true}', b""
        harness.clock += timeout
        raise subprocess.TimeoutExpired(argv, timeout)

    return _bounded


def test_accept_retry_retries_a_timeout_until_the_hook_answers(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """tp#1805: under load the idempotent hook timed out and accept gave up after one run;
    now a timeout is retried within the window and the transfer is accepted."""
    harness.accept_on_sleep = False
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_script(tmp_path, "slow.sh", "exit 0\n")])  # the defaults
    calls: list[float] = []
    monkeypatch.setattr(continue_session, "run_bounded", _timing_out(harness, calls, 2))
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 0
    err = capsys.readouterr().err
    assert calls == [180, 180]
    assert "waiting: handoff hook failed (slow.sh: timed out after 180s); retrying in 10s" in err
    assert _state(tid) == "finalized" and _failure(tid) == ""
    assert harness.left_background == 1  # accept left darwin background before the hook


def test_accept_retry_bounds_timeouts_by_the_window_and_names_attempts(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defaults (180 s per run, 420 s window): runs at 0, 190 and 390 s; the third gets the
    30 s left; the next wait (40 s) no longer fits, so accept stops at the window."""
    harness.accept_on_sleep = False
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_script(tmp_path, "hang.sh", "exit 0\n")])
    calls: list[float] = []
    monkeypatch.setattr(continue_session, "run_bounded", _timing_out(harness, calls))
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    err = capsys.readouterr().err
    assert calls == [180, 180, 30] and harness.clock == 420
    assert "handoff hook failed after 3 attempts in 420s" in err
    assert "still opened" in err and "run accept again" in err
    assert _failure(tid).startswith("hook: hang.sh: timed out")
    assert _state(tid) == "opened"


def test_accept_retry_backs_off_and_caps_the_attempts(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A quick refusal: waits of 10, 20 and 40 s, then attempt 4 is the last one."""
    harness.accept_on_sleep = False
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    _hooks(monkeypatch, [_script(tmp_path, "no.sh", "exit 1\n")])
    waits: list[float] = []
    real_sleep = continue_session._sleep  # pylint: disable=protected-access

    def _sleep(seconds: float) -> None:
        waits.append(seconds)
        real_sleep(seconds)

    monkeypatch.setattr(continue_session, "_sleep", _sleep)
    monkeypatch.setattr(continue_session, "run_bounded", lambda _a, _t: (1, b"", b"refused\n"))
    assert cli.main(["continue-session", "accept", tid]) == 1
    assert waits == [10, 20, 40]
    assert _state(tid) == "opened"


def test_accept_retry_timeout_and_window_are_clamped(tmp_path: Path) -> None:
    from command_center import config  # pylint: disable=import-outside-toplevel

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        "accept_hook_timeout_sec = 9999\naccept_hook_retry_sec = 9999\n", encoding="utf-8"
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "app_home", lambda: home)
        config.invalidate_config_cache()
        cfg = config.load_config()
        config.invalidate_config_cache()
    assert (cfg.accept_hook_timeout_sec, cfg.accept_hook_retry_sec) == (480, 480)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_leave_darwin_background_runs_only_on_macos(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """accept's QoS step: setpriority + ``taskpolicy -B -p <own pid>``; no-op elsewhere."""
    import subprocess  # pylint: disable=import-outside-toplevel

    prio: list[tuple[int, int, int]] = []
    runs: list[list[str]] = []

    def _setpriority(which: int, who: int, value: int) -> None:
        prio.append((which, who, value))
        raise OSError("not permitted")  # a refusal never escapes

    def _run(argv: list[str], **_kw: Any) -> Any:
        runs.append(argv)
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(continue_session.sys, "platform", platform)
    monkeypatch.setattr(continue_session.os, "setpriority", _setpriority, raising=False)
    monkeypatch.setattr(subprocess, "run", _run)
    continue_session.leave_darwin_background()
    if platform == "darwin":
        assert prio == [(4, 0, 0)]
        assert runs == [["/usr/sbin/taskpolicy", "-B", "-p", str(continue_session.os.getpid())]]
    else:
        assert not prio and not runs


@pytest.mark.parametrize(("to_state", "code"), [("failed", 1), ("accepted", 0)])
def test_accept_retry_stops_when_the_transfer_moved_during_the_wait(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    to_state: str,
    code: int,
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    hook, counter = _counting_hook(tmp_path, "flaky.sh", fail_runs=5)
    _hooks(monkeypatch, [hook], window=60)

    def _sleep(_seconds: float) -> None:
        with Store() as store:
            assert store.transition_transfer(tid, to_state, 1, failure="gone")

    monkeypatch.setattr(continue_session, "_sleep", _sleep)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == code
    out = capsys.readouterr()
    assert _runs(counter) == 1  # no hook ran after the move
    if to_state == "accepted":
        assert "already accepted" in out.out
    else:
        assert f"transfer {tid} is failed" in out.err


def test_accept_retry_reruns_accept_prepare_and_stops_on_a_fresh_delivery(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import time  # pylint: disable=import-outside-toplevel

    gid = _arm_await(SID)
    event: dict[str, Any] = {"event_id": "e", "payload": "{}", "remote_epoch": 1, "watermark": "w"}
    stale = int(time.time()) - 15 * 60  # a delivery the first attempt may reclaim
    with Store() as store:
        src = store.lease_due_sources(1_800_000_120)[0][0]
        token = store.fire_group(gid, src.id, src.lease_token, now=1_800_000_120, **event)
        assert token and store.mark_delivering(gid, token, stale)
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    hook, counter = _counting_hook(tmp_path, "flaky.sh", fail_runs=1)
    _hooks(monkeypatch, [hook], window=60)

    def _sleep(_seconds: float) -> None:  # the old session's delivery is live again mid-wait
        with Store() as store:
            store.conn.execute(
                "UPDATE await_groups SET state = 'delivering', updated_at = ? WHERE id = ?",
                (int(time.time()), gid),
            )
            store.conn.commit()

    monkeypatch.setattr(continue_session, "_sleep", _sleep)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    assert f"await group {gid} is being delivered" in capsys.readouterr().err
    assert _runs(counter) == 1 and _state(tid) == "opened"
    assert (_group(gid).state, _group(gid).session_id) == ("delivering", SID)  # no rebind


def test_accept_retry_window_zero_keeps_one_run(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    hook, counter = _counting_hook(tmp_path, "flaky.sh", fail_runs=1)
    _hooks(monkeypatch, [hook], window=0)
    capsys.readouterr()
    assert cli.main(["continue-session", "accept", tid]) == 1
    err = capsys.readouterr().err
    assert _runs(counter) == 1 and "waiting:" not in err and "after 1 attempt" in err
    assert _state(tid) == "opened"


def test_accept_retry_reruns_the_whole_hook_list(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    first, first_count = _counting_hook(tmp_path, "first.sh", fail_runs=0)
    second, second_count = _counting_hook(tmp_path, "second.sh", fail_runs=1)
    _hooks(monkeypatch, [first], [second], window=60)
    assert cli.main(["continue-session", "accept", tid]) == 0
    assert (_runs(first_count), _runs(second_count)) == (2, 2)
    assert _state(tid) == "finalized"


def test_accept_retry_preamble_allows_one_more_accept() -> None:
    transfer = type(
        "T", (), {"source_session": "s", "transfer_id": "t1", "handoff_path": "/h.md"}
    )()
    text = continue_session.preamble(transfer, "private", "")
    flat = " ".join(text.split())
    assert "accept already retries a refusing or timed-out hook" in flat
    assert (
        "if it still fails and the error says the transfer is still opened, run accept "
        "once more; stop and report on any other failure" in flat
    )
    assert "pass the Bash tool timeout 600000" in flat


def test_accept_retry_integration_with_a_stub_executable(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real ``run_hook`` path: a stub exits 1 until the marker the wait creates exists."""
    tid = _opened_transfer(harness)
    _as_successor(monkeypatch, harness, tid)
    marker = tmp_path / "source-is-running-start"
    stub = _script(
        tmp_path,
        "tp-stub",
        f'[ -e {marker} ] || {{ echo "session is working" >&2; exit 1; }}\n'
        "echo '{\"ok\": true}'\n",
    )
    _hooks(monkeypatch, [stub, "handoff", "-S", "{old}", "--to", "{new}", "-j"])  # default window
    real_sleep = continue_session._sleep  # pylint: disable=protected-access

    def _sleep(seconds: float) -> None:
        marker.touch()  # the source's re-run `start` is back
        real_sleep(seconds)

    monkeypatch.setattr(continue_session, "_sleep", _sleep)
    assert continue_session.cmd_accept(type("A", (), {"transfer": tid, "json": False})()) == 0
    assert _state(tid) == "finalized"
