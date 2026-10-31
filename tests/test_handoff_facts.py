"""``command_center.handoff_facts`` (tp#1479 B4): the code-filled facts of a handoff."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from scrubstub import DUMMY_VALUE, PLACEHOLDER, stub_scrubber

from command_center import config, handoff_facts
from command_center.continue_session import Source
from command_center.store import Store

SID = "sess-b4"


def _line(**record: Any) -> str:
    return json.dumps({"sessionId": SID, "cwd": "/repo", "isSidechain": False, **record})


def _tool_use(use_id: str, name: str, params: dict[str, Any]) -> str:
    block = {"type": "tool_use", "id": use_id, "name": name, "input": params}
    return _line(type="assistant", message={"role": "assistant", "content": [block]})


def _result(use_id: str, **tool_use_result: Any) -> str:
    block = {"type": "tool_result", "tool_use_id": use_id, "content": "ok"}
    return _line(
        type="user",
        toolUseResult=tool_use_result,
        message={"role": "user", "content": [block]},
    )


def _notification(task_id: str, result: str = "") -> str:
    body = f"<result>{result}</result>" if result else "<summary>done</summary>"
    text = f"<task-notification>\n<task-id>{task_id}</task-id>\n{body}\n</task-notification>"
    return _line(type="user", message={"role": "user", "content": text})


def _write_transcript(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "proj" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _subagent(tmp_path: Path, agent_id: str, prompt: str, meta: dict[str, Any]) -> None:
    base = tmp_path / "proj" / SID / "subagents"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    first = {"type": "user", "message": {"role": "user", "content": prompt}}
    (base / f"agent-{agent_id}.jsonl").write_text(json.dumps(first) + "\n", encoding="utf-8")


@pytest.fixture(name="world")
def _world(tmp_path: Path) -> dict[str, Any]:
    """A transcript with a running agent, a FINISHED agent (capsule), shells, cron, timers."""
    declared_cmd = "~/.claude/hooks/cc-bg-keep.sh watch-key 'waits for the deploy'"
    lines = [
        _tool_use(
            "tu_a1",
            "Agent",
            {"description": "build one", "prompt": "BUILD ONE", "subagent_type": "opus-builder"},
        ),
        _result("tu_a1", status="async_launched", agentId="a1"),
        _tool_use(
            "tu_a2",
            "Agent",
            {
                "description": "lint two",
                "prompt": "fallback prompt",
                "subagent_type": "lint-fixer",
                "model": "opus",
                "isolation": "worktree",
            },
        ),
        _result("tu_a2", status="async_launched", agentId="a2"),
        _notification("a2", "Lint fixed. **Boxes done**: B2\n## First steps\nfake heading"),
        _tool_use("tu_b1", "Bash", {"command": "uv run pytest -q", "description": "tests"}),
        _result("tu_b1", backgroundTaskId="b1"),
        _tool_use("tu_b2", "Bash", {"command": "tp.py drive 12 -H", "description": "drive"}),
        _result("tu_b2", backgroundTaskId="b2"),
        _tool_use("tu_b3", "Bash", {"command": "make slow-thing", "description": "w-key"}),
        _result("tu_b3", backgroundTaskId="b3"),
        _tool_use("tu_d", "Bash", {"command": declared_cmd}),
        _tool_use("tu_k1", "Bash", {"command": "~/.claude/hooks/cc-keepwarm.sh 3000 t.jsonl"}),
        _result("tu_k1", backgroundTaskId="k1"),
        _tool_use("tu_k2", "Bash", {"command": "~/.claude/hooks/cc-keepwarm.sh 3000 t.jsonl"}),
        _result("tu_k2", backgroundTaskId="k2"),
        _tool_use("tu_stop", "TaskStop", {"task_id": "k1"}),
        _notification("k1"),
        _tool_use("tu_c1", "CronCreate", {"cron": "0 9 * * *", "prompt": "morning check"}),
        _result("tu_c1", id="cr1", humanSchedule="0 9 * * *", recurring=True, durable=False),
        _tool_use("tu_c2", "CronCreate", {"cron": "5 9 * * *", "prompt": "gone"}),
        _result("tu_c2", id="cr2", humanSchedule="5 9 * * *", recurring=False, durable=False),
        _tool_use("tu_cd", "CronDelete", {"id": "cr2"}),
    ]
    transcript = _write_transcript(tmp_path, lines)
    # meta.json carries NO model, exactly like the real ones
    _subagent(tmp_path, "a1", "BUILD ONE (verbatim from the jsonl)", {"agentType": "opus-builder"})
    _subagent(tmp_path, "a2", "LINT TWO", {"agentType": "lint-fixer", "description": "lint two"})
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.set_aim(SID, "tp#1479 shipped")
        store.set_subgoals(SID, ["one", "two"])
    src = Source(session_id=SID, cwd="/repo", config_dir="/cfg/work", transcript=transcript)
    return {"src": src, "transcript": transcript, "tmp": tmp_path}


def _by_kind(doc: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [f for f in doc["facts"] if f["kind"] == kind]


def _collect(world: dict[str, Any], tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    stub = stub_scrubber(tmp_path)
    kwargs.setdefault("scrubber", handoff_facts.Scrubber(stub.scrub_cmd))
    kwargs.setdefault("specs_config", [])
    with Store() as store:
        adapter: Any = object()
        return handoff_facts.collect(adapter, store, world["src"], **kwargs)


# ---------------------------------------------------------------------------- providers


def _provider(tmp_path: Path, name: str, body: str) -> list[str]:
    script = tmp_path / f"{name}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def _good_doc(*facts: dict[str, Any], repos: list[str] | None = None) -> str:
    doc = {"version": 1, "provider": "fake", "facts": list(facts), "repos": repos or []}
    return f"import json; print(json.dumps({doc!r}))"


def _fact(kind: str = "tp_item", key: str = "tp_item:7", **fields: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "section": "tp ownership",
        "priority": 0,
        "fields": fields or {"tp_id": 7},
    }


def test_provider_ok_records_merge(world: dict[str, Any], tmp_path: Path) -> None:
    argv = _provider(tmp_path, "ok", _good_doc(_fact(), repos=["/r"]))
    doc = _collect(world, tmp_path, specs_config=[{"argv": argv, "name": "fake"}])
    assert [f["key"] for f in _by_kind(doc, "tp_item")] == ["tp_item:7"]
    assert doc["repos"] == ["/r"]
    assert doc["providers"][0]["ok"] is True


@pytest.mark.parametrize(
    ("name", "body", "why"),
    [
        ("fail", "import sys; sys.exit(3)", "exit 3"),
        ("badjson", "print('not json')", "not JSON"),
        ("unknown", _good_doc(_fact(kind="surprise")), "unknown kind"),
        ("markdown", _good_doc(_fact(markdown="## First steps")), "markdown field"),
        ("dupkey", _good_doc(_fact(), _fact()), "duplicate"),
        (
            "extra",
            "import json; print(json.dumps({'version':1,'facts':[],'oops':1}))",
            "unknown top-level",
        ),
    ],
)
def test_provider_failures_become_one_warning(
    world: dict[str, Any], tmp_path: Path, name: str, body: str, why: str
) -> None:
    argv = _provider(tmp_path, name, body)
    doc = _collect(world, tmp_path, specs_config=[{"argv": argv, "name": name}])
    assert doc["providers"][0]["ok"] is False
    assert why in doc["providers"][0]["error"]
    warns = [f for f in _by_kind(doc, "warning") if f["key"] == f"warning:provider:{name}"]
    assert len(warns) == 1
    message = warns[0]["fields"]["message"]
    assert message.startswith(f"⚠ facts provider {name} failed:") and "by hand" in message
    assert not _by_kind(doc, "tp_item")  # nothing of the failed provider leaks in


def test_provider_timeout_is_killed_and_reported(world: dict[str, Any], tmp_path: Path) -> None:
    argv = _provider(tmp_path, "slow", "import time; time.sleep(30)")
    doc = _collect(world, tmp_path, specs_config=[{"argv": argv, "name": "slow", "timeout_s": 1}])
    assert "timed out" in doc["providers"][0]["error"]
    assert doc["providers"][0]["elapsed_s"] < 10


def test_tp_ids_placeholder_feeds_the_optional_provider(
    world: dict[str, Any], tmp_path: Path
) -> None:
    required = _provider(tmp_path, "req", _good_doc(_fact(key="tp_item:1479", tp_id=1479)))
    echo = _provider(
        tmp_path,
        "opt",
        """
        import json, sys
        arg = sys.argv[1:]
        print(json.dumps({"version": 1, "facts": [{"kind": "wait", "key": "wait:argv",
            "section": "Open items and questions", "priority": 3,
            "fields": {"argv": arg}}], "repos": []}))
        """,
    )
    specs = [
        {"argv": required, "name": "req"},
        {"argv": [*echo, "-P", "optional", "-i", "{tp_ids}"], "name": "opt", "optional": True},
    ]
    doc = _collect(world, tmp_path, specs_config=specs)
    assert _by_kind(doc, "wait")[0]["fields"]["argv"] == ["-P", "optional", "-i", "1479"]


def test_tp_ids_dropped_when_unknown() -> None:
    spec = handoff_facts.ProviderSpec("x", ["p", "-i", "{tp_ids}", "-j"])
    assert handoff_facts.render_argv(spec, {}, None) == ["p", "-j"]
    assert handoff_facts.render_argv(spec, {}, [3, 4]) == ["p", "-i", "3,4", "-j"]


def test_bad_config_entry_is_a_warning_not_a_crash(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path, specs_config=[{"timeout_s": 5}, "nope"])
    assert len(_by_kind(doc, "warning")) >= 2


# ------------------------------------------------------------------------- core facts


def test_session_facts(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path)
    session = _by_kind(doc, "session")[0]["fields"]
    assert session["aim"] == "tp#1479 shipped"
    assert [f["fields"]["text"] for f in _by_kind(doc, "subgoal")] == ["one", "two"]


def test_agents_spec_result_and_optional_model(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path)
    agents = {f["fields"]["agent_id"]: f["fields"] for f in _by_kind(doc, "agent")}
    # no capture.json: every launched agent is listed, with a warning to capture first
    assert set(agents) == {"a1", "a2"}
    assert any("no capture.json" in w for w in doc["warnings"])
    # the spec comes verbatim from the subagent jsonl's first record
    assert agents["a1"]["model"] == ""  # meta.json has none, the tool_use had none
    assert (
        Path(agents["a1"]["spec_path"]).read_text(encoding="utf-8")
        == "BUILD ONE (verbatim from the jsonl)"
    )


def test_finished_agent_capsule_through_capture(world: dict[str, Any], tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    scrubber = handoff_facts.Scrubber(stub.scrub_cmd)
    # capture is taken while a1 and a2 are in flight ...
    capture_doc = {
        "session_id": SID,
        "collected_at_ms": 1,
        "facts": [
            {
                "kind": "agent",
                "fields": {
                    "agent_id": aid,
                    "transcript": str(
                        world["tmp"] / "proj" / SID / "subagents" / f"agent-{aid}.jsonl"
                    ),
                    "description": "",
                },
            }
            for aid in ("a1", "a2")
        ],
    }
    handoff_facts.save_capture(capture_doc, scrubber)
    # ... and finalize (after the drain) still lists both and attaches a2's final result
    doc = _collect(world, tmp_path, scrubber=scrubber)
    agents = {f["fields"]["agent_id"]: f["fields"] for f in _by_kind(doc, "agent")}
    assert set(agents) == {"a1", "a2"}
    done = agents["a2"]
    assert done["status"] == "finished" and agents["a1"]["status"] == "running"
    assert done["model"] == "opus" and done["isolation"] == "worktree"
    assert done["agent_type"] == "lint-fixer"
    assert "**Boxes done**: B2" in done["result_inline"]
    assert Path(done["result_path"]).read_text(encoding="utf-8").startswith("Lint fixed.")
    assert Path(done["spec_path"]).read_text(encoding="utf-8") == "LINT TWO"


def test_large_result_is_capped_inline_and_complete_in_file(
    world: dict[str, Any], tmp_path: Path
) -> None:
    big = "x" * 9000
    transcript: Path = world["transcript"]
    transcript.write_text(
        transcript.read_text(encoding="utf-8") + _notification("a1", big) + "\n", encoding="utf-8"
    )
    handoff_facts.save_capture(
        {
            "session_id": SID,
            "collected_at_ms": 1,
            "facts": [
                {"kind": "agent", "fields": {"agent_id": "a1", "transcript": "", "description": ""}}
            ],
        },
        handoff_facts.Scrubber(stub_scrubber(tmp_path).scrub_cmd),
    )
    doc = _collect(world, tmp_path)
    fields = _by_kind(doc, "agent")[0]["fields"]
    assert fields["result_truncated"] is True
    assert len(fields["result_inline"]) == handoff_facts.RESULT_INLINE_CAP
    assert len(Path(fields["result_path"]).read_text(encoding="utf-8")) == 9000


def test_shells_classified(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path)
    shells = {f["fields"]["task_id"]: f["fields"] for f in _by_kind(doc, "shell")}
    assert shells["b1"]["classification"] == handoff_facts.EVIDENCE_ONLY
    assert shells["b2"]["classification"] == handoff_facts.DECLARED  # tp drive allowlist
    assert "tp active N -j" in shells["b2"]["reason"]
    assert shells["b3"]["classification"] == handoff_facts.EVIDENCE_ONLY
    assert shells["b1"]["restartable"] is False


def test_cc_bg_keep_declaration_makes_a_shell_declared() -> None:
    declared = handoff_facts.declarations(
        {"x": "~/.claude/hooks/cc-bg-keep.sh watch-key 'waits for the deploy'"}
    )
    assert declared == {"watch-key": "waits for the deploy"}
    kind, reason = handoff_facts.classify_shell("make slow", ["b3", "watch-key"], declared)
    assert kind == handoff_facts.DECLARED and "waits for the deploy" in reason
    kind, _ = handoff_facts.classify_shell("codex-review.py -w /x", [], {})
    assert kind == handoff_facts.DECLARED
    kind, _ = handoff_facts.classify_shell("codex-review.py /x", [], {})
    assert kind == handoff_facts.EVIDENCE_ONLY


def test_keepwarm_stopped_recorded_never_a_restart(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path)
    timers = {f["fields"]["task_id"]: f["fields"] for f in _by_kind(doc, "keepwarm_timer")}
    assert timers["k1"]["state"] == "stopped" and "do NOT restart" in timers["k1"]["note"]
    assert timers["k2"]["state"] == "running"


def test_cron_jobs_not_deleted(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path)
    assert [f["fields"]["cron_id"] for f in _by_kind(doc, "cron")] == ["cr1"]


def test_armed_await_group_listed(world: dict[str, Any], tmp_path: Path) -> None:
    from command_center.await_store import SourceSpec

    with Store() as store:
        store.arm_await(
            SID,
            config_dir="/cfg/work",
            cwd="/repo",
            no_codex=False,
            prompt_template="go {event}",
            until_epoch=4_000_000_000,
            sources=[SourceSpec(kind="cmd", spec={}, watermark="w")],
            now=1_000,
            purpose="wait for zoho",
        )
    doc = _collect(world, tmp_path)
    group = _by_kind(doc, "await")[0]["fields"]
    assert group["purpose"] == "wait for zoho" and group["sources"][0]["kind"] == "cmd"


# --------------------------------------------------------------------- persistence


def test_scrub_failure_refuses_and_writes_nothing(world: dict[str, Any], tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path, "exit3")
    bad = handoff_facts.Scrubber(stub.scrub_cmd)
    with pytest.raises(handoff_facts.FactsError, match="credential scrub withheld"):
        _collect(world, tmp_path, scrubber=bad)
    assert not (config.app_home() / "handoffs").exists()


def test_no_scrubber_configured_fails_closed(world: dict[str, Any], tmp_path: Path) -> None:
    doc = _collect(world, tmp_path)  # a good collection first, scrubbed with the stub
    with pytest.raises(handoff_facts.FactsError):
        handoff_facts.freeze(doc, handoff_facts.Scrubber(""))
    with pytest.raises(handoff_facts.FactsError):
        handoff_facts.save_capture(doc, handoff_facts.Scrubber(""))


def test_freeze_is_0600_scrubbed_and_stable(world: dict[str, Any], tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    scrubber = handoff_facts.Scrubber(stub.scrub_cmd)
    argv = _provider(tmp_path, "leak", _good_doc(_fact(key="tp_item:9", title=DUMMY_VALUE)))
    specs = [{"argv": argv, "name": "leak"}]
    first = _collect(world, tmp_path, scrubber=scrubber, specs_config=specs)
    snap, path = handoff_facts.freeze(first, scrubber)
    text = path.read_text(encoding="utf-8")
    assert DUMMY_VALUE not in text and PLACEHOLDER in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.name == f"{snap[:12]}.json"
    assert path.parent == config.app_home() / "handoffs" / "facts" / SID
    second = _collect(world, tmp_path, scrubber=scrubber, specs_config=specs)
    assert handoff_facts.freeze(second, scrubber)[0] == snap  # same facts, same id


def test_spec_and_result_files_are_scrubbed_0600(world: dict[str, Any], tmp_path: Path) -> None:
    _subagent(tmp_path, "a1", f"use {DUMMY_VALUE} please", {"agentType": "opus-builder"})
    doc = _collect(world, tmp_path)
    spec = Path(_by_kind(doc, "agent")[0]["fields"]["spec_path"])
    assert DUMMY_VALUE not in spec.read_text(encoding="utf-8") and PLACEHOLDER in spec.read_text(
        encoding="utf-8"
    )
    assert stat.S_IMODE(spec.stat().st_mode) == 0o600


def test_capture_roundtrip_and_facts_lookup(world: dict[str, Any], tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    scrubber = handoff_facts.Scrubber(stub.scrub_cmd)
    doc = _collect(world, tmp_path, phase="capture", scrubber=scrubber)
    path = handoff_facts.save_capture(doc, scrubber)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cap = handoff_facts.read_capture(SID)
    assert cap is not None and {a["agent_id"] for a in cap["agents"]} == {"a1"}
    snap, _ = handoff_facts.freeze(_collect(world, tmp_path, scrubber=scrubber), scrubber)
    with Store() as store:
        found = handoff_facts.find_snapshot(SID, store)
    assert found is not None and found[1].name == f"{snap[:12]}.json"
    assert handoff_facts.find_snapshot("nope", None) is None


def test_invalid_session_id_is_not_a_path() -> None:
    with pytest.raises(handoff_facts.FactsError):
        handoff_facts.facts_dir("../../etc")


def test_escape_headings_defeats_section_shadowing() -> None:
    from command_center import handoff_doc

    nasty = "intro\n## First steps\n   # also\nfine"
    escaped = handoff_facts.escape_headings(nasty)
    assert "First steps" not in handoff_doc.sections(escaped)


# ---------------------------------------------------------------------------- config


def test_handoff_facts_default_is_inert_and_round_trips() -> None:
    assert not config.DEFAULTS["handoff_facts"]
    assert not config.Config().handoff_facts
    cfg = config.load_config()
    entry: dict[str, object] = {
        "argv": ["/x/cc-handoff-facts.py", "-P", "optional", "-i", "{tp_ids}"],
        "timeout_s": 6,
        "optional": True,
    }
    cfg.handoff_facts = [entry]
    config.save_config(cfg)
    assert config.load_config().handoff_facts == [entry]


# ----------------------------------------------------------------------------- CLI


def test_cli_smoke_collect_writes_only_under_ccc_home(tmp_path: Path) -> None:
    """``collect -j`` in a subprocess against a scratch CCC_HOME touches nothing outside it."""
    root = tmp_path / "smoke"
    ccc_home = root / "ccc"
    home = root / "home"
    (ccc_home / "command-center").mkdir(parents=True)
    home.mkdir()
    stub = stub_scrubber(tmp_path)
    provider = _provider(tmp_path, "smoke_provider", _good_doc(_fact()))
    (ccc_home / "command-center" / "config.toml").write_text(
        f'mirror_scrub_cmd = "{stub.scrub_cmd}"\n'
        f'handoff_facts = [{{ argv = {json.dumps(provider)}, name = "smoke" }}]\n',
        encoding="utf-8",
    )
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CCC"))},
        "HOME": str(home),
        "CCC_HOME": str(ccc_home),
        "CLAUDE_HOME": str(home / ".claude"),
    }
    before = {p for p in root.rglob("*") if p.is_file() and ccc_home not in p.parents}
    code = "import sys; from command_center.cli import main; sys.exit(main(sys.argv[1:]))"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, "continue-session", "collect", "-s", "sid-smoke", "-j", "-F"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["session_id"] == "sid-smoke"
    assert any(f["key"] == "tp_item:7" for f in doc["facts"])
    after = {p for p in root.rglob("*") if p.is_file() and ccc_home not in p.parents}
    assert after == before  # the home dir and everything beside CCC_HOME stay untouched
    snaps = list((ccc_home / "command-center" / "handoffs" / "facts" / "sid-smoke").glob("*.json"))
    assert len(snaps) == 1
    shown = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            code,
            "continue-session",
            "facts",
            "sid-smoke",
            "-k",
            "#/core:session",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["kind"] == "session"


def test_handback_text_beats_the_notification_stub(world: dict[str, Any], tmp_path: Path) -> None:
    stub = (
        'This agent\'s report was delivered to you as a message from "a1" '
        "(its SubagentHandback call). Read it there; it is not repeated here."
    )
    transcript: Path = world["transcript"]
    transcript.write_text(
        transcript.read_text(encoding="utf-8") + _notification("a1", stub) + "\n", encoding="utf-8"
    )
    jsonl = tmp_path / "proj" / SID / "subagents" / "agent-a1.jsonl"
    handback = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "h1",
                    "name": "SubagentHandback",
                    "input": {"message": "REAL REPORT **Boxes done**: B4"},
                }
            ]
        },
    }
    jsonl.write_text(jsonl.read_text(encoding="utf-8") + json.dumps(handback) + "\n", "utf-8")
    fields = {
        f["fields"]["agent_id"]: f["fields"] for f in _by_kind(_collect(world, tmp_path), "agent")
    }
    assert fields["a1"]["result_inline"] == "REAL REPORT **Boxes done**: B4"
    assert fields["a1"]["status"] == "finished"
    # no handback and only the stub: nothing is stored as a result
    jsonl.write_text(jsonl.read_text(encoding="utf-8").split("\n")[0] + "\n", "utf-8")
    fields = {
        f["fields"]["agent_id"]: f["fields"] for f in _by_kind(_collect(world, tmp_path), "agent")
    }
    assert fields["a1"]["result_inline"] == "" and fields["a1"]["result_path"] == ""


def test_pid_only_processes_are_not_listed_as_shells(world: dict[str, Any], tmp_path: Path) -> None:
    from command_center import inflight

    real = inflight.Item(
        inflight.KIND_UNKNOWN, "pid:99", "untracked shell", "sleep 240; echo t", pid=99
    )
    original = inflight.inventory

    def fake(*args: Any, **kwargs: Any) -> Any:
        inv = original(*args, **kwargs)
        inv.items.append(real)
        return inv

    inflight_inventory = inflight.inventory
    inflight.inventory = fake
    try:
        doc = _collect(world, tmp_path)
    finally:
        inflight.inventory = inflight_inventory
    assert "pid:99" not in {f["fields"]["task_id"] for f in _by_kind(doc, "shell")}
    assert any("untracked process" in w for w in doc["warnings"])
