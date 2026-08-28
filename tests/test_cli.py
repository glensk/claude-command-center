"""CLI handler tests — sub-goal checking is session-scoped by position, not global DB id.

Regression guard: `ccc check <n>` must tick the *n*-th sub-goal of the resolved
session, never a global DB id that may belong to a different session sharing the
working directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from command_center import cli
from command_center.cli import cmd_check, cmd_jobs, cmd_new_job, cmd_resume, cmd_subgoals
from command_center.store import Store


def _args(position: int, session: str, uncheck: bool = False) -> argparse.Namespace:
    return argparse.Namespace(position=position, session=session, uncheck=uncheck)


def _seed_two_sessions(home: Path) -> Store:
    """Two sessions, each with two sub-goals, so their DB ids interleave across sessions."""
    store = Store(home / "command-center" / "state.db")
    store.ensure("sess-a", cwd="/repo")
    store.set_subgoals("sess-a", ["a-one", "a-two"])
    store.ensure("sess-b", cwd="/repo")  # same cwd — the exact concurrent-session hazard
    store.set_subgoals("sess-b", ["b-one", "b-two"])
    return store


def test_new_job_creates_draft_and_jobs_lists_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: _no_score_cfg())
    args = argparse.Namespace(
        aim="Migrate Zendesk tickets to Zoho Desk",
        prompt="move my zendesk tickets to zoho",
        cwd="/repo/sdsc/zoho",
        deadline=None,
    )
    assert cmd_new_job(args) == 0
    out = capsys.readouterr().out
    assert "future job created" in out

    with Store(tmp_path / "command-center" / "state.db") as store:
        drafts = [s for s in store.list_sessions() if s.draft]
        assert len(drafts) == 1
        assert drafts[0].prompt == "move my zendesk tickets to zoho"
        assert drafts[0].aim == "Migrate Zendesk tickets to Zoho Desk"

    assert cmd_jobs(argparse.Namespace()) == 0
    assert "zoho" in capsys.readouterr().out


def test_tab_symbol_print_is_deterministic_and_no_iterm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ccc tab-symbol --print PATH` prints the deterministic per-repo emoji (the shell hook)."""
    from command_center import tabsymbol

    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert cli.main(["tab-symbol", "--print", "/Users/x/sdsc/runai-cscs"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == tabsymbol.symbol_for_repo("/Users/x/sdsc/runai-cscs")
    assert out in tabsymbol.PALETTE
    # Same path → same symbol on a second invocation.
    assert cli.main(["tab-symbol", "-p", "/Users/x/sdsc/runai-cscs"]) == 0
    assert capsys.readouterr().out.strip() == out


def test_tab_symbol_print_color_appends_style(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    assert cli.main(["tab-symbol", "-p", "-c", "/Users/x/repo"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out.split(" ")) == 2  # "<emoji> <color>"


def test_install_shell_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = tmp_path / ".zshrc"
    assert cli.main(["install-shell", "-s", "zsh", "-r", str(rc), "-n"]) == 0
    assert "# >>> ccc shell integration >>>" in capsys.readouterr().out
    assert not rc.exists()


def test_install_shell_install_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import shell_install

    monkeypatch.setattr(shell_install.shutil, "which", lambda _n: None)  # no PATH collision
    rc = tmp_path / ".zshrc"
    rc.write_text("export KEEP=1\n", encoding="utf-8")
    assert cli.main(["install-shell", "-s", "zsh", "-r", str(rc)]) == 0
    assert "_ccc_tab_badge" in rc.read_text(encoding="utf-8")
    assert cli.main(["install-shell", "-s", "zsh", "-r", str(rc), "-u"]) == 0
    text = rc.read_text(encoding="utf-8")
    assert "# >>> ccc shell integration >>>" not in text
    assert "export KEEP=1" in text


def test_toggle_idle_cli_flips_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ccc toggle-idle` (and -n/--on, -f/--off) drive agentPushNotifEnabled in settings.json."""
    from command_center import idlenotify

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text('{"agentPushNotifEnabled": true}\n', encoding="utf-8")

    assert cli.main(["toggle-idle"]) == 0  # toggles → OFF
    assert idlenotify.is_enabled() is False
    assert "OFF" in capsys.readouterr().out

    assert cli.main(["toggle-idle", "-n"]) == 0  # force ON
    assert idlenotify.is_enabled() is True

    assert cli.main(["toggle-idle", "--off"]) == 0  # force OFF
    assert idlenotify.is_enabled() is False


def test_resume_without_transcript_errors_and_does_not_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A parked row with no `<id>.jsonl` can't be resumed — report it, never exec claude."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("dead-sess", cwd="/repo")
    store.close()

    def _boom(*_a: object, **_k: object) -> None:  # execvp must never be reached
        raise AssertionError("os.execvp should not run when no transcript exists")

    monkeypatch.setattr("command_center.cli.os.execvp", _boom)
    assert cmd_resume(argparse.Namespace(session_id="dead-sess")) == 1
    assert "no recorded conversation" in capsys.readouterr().err


def test_new_job_requires_an_aim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    args = argparse.Namespace(aim="  ", prompt=None, cwd="/repo", deadline=None)
    assert cmd_new_job(args) == 1
    assert "required" in capsys.readouterr().err


def test_score_aim_dry_run_reports_serving_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`score-aim --dry-run` JSON carries the ladder rung that served the call."""
    import json

    from command_center import llm

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(
        llm, "run_ladder", lambda *_a, **_k: ("codex", '{"score":70,"reason":"ok"}')
    )
    args = argparse.Namespace(dry_run="ship rate-limit middleware", session=None)
    assert cli.cmd_score_aim(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["score"] == 70
    assert out["backend"] == "codex"


def test_score_aim_dry_run_lexical_fallback_when_ladder_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every rung failing → the offline lexical estimate, tagged backend "lexical"."""
    import json

    from command_center import llm

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(llm, "run_ladder", lambda *_a, **_k: None)  # all rungs fail
    args = argparse.Namespace(dry_run="improve the thing", session=None)
    assert cli.cmd_score_aim(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["backend"] == "lexical"
    assert isinstance(out["score"], int)


def _no_score_cfg() -> object:
    cfg = type("Cfg", (), {})()
    cfg.aim_score_on_set = False  # don't spawn a detached scorer in the test
    cfg.future_files = False  # nor a detached sync-future
    cfg.job_account = ""  # route a new job to the default account (see routing.pick_job_account)
    return cfg


class _StopExec(Exception):
    """Sentinel raised in place of os.execvp so the test can inspect the built argv."""


def _start_job_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overseer: str,
    executor: str,
    job_type: str = "claude",
    config_toml: str = "",
) -> list[str]:
    """Build a draft with the given models, run cmd_start_job, and capture the exec argv."""
    from command_center.cli import cmd_start_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    if config_toml:
        cfg_dir = tmp_path / "command-center"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.toml").write_text(config_toml, encoding="utf-8")
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress the detached ccc sync-mirrors spawn
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(
        "job-x",
        "/no/such/dir",  # non-existent → os.chdir is skipped, no future_file → no sync
        "Do the thing",
        prompt="run it",
        job_type=job_type,
        llm_overseer=overseer,
        llm_exec=executor,
    )
    store.close()

    captured: dict[str, list[str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id="job-x"))
    return captured["argv"]


def test_start_job_argv_default_models_no_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _start_job_argv(tmp_path, monkeypatch, overseer="opus-4.8", executor="opus-4.8")
    # Runs on the overseer's model; equal exec/overseer → no delegation prefix. Effort is
    # always explicit (launch_effort default xhigh) so settings.json never decides it.
    assert argv == [
        "claude",
        "--model",
        "claude-opus-4-8",
        "--session-id",
        "job-x",
        "--effort",
        "xhigh",
        "run it",
    ]


def test_start_job_argv_delegates_when_exec_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _start_job_argv(tmp_path, monkeypatch, overseer="fable-5", executor="opus-4.8")
    assert argv[:7] == [
        "claude",
        "--model",
        "claude-fable-5",
        "--session-id",
        "job-x",
        "--effort",
        "xhigh",
    ]
    prompt = argv[7]
    assert prompt.startswith("[orchestration] You are the overseer running as fable-5.")
    assert "model 'opus'" in prompt  # Agent-tool alias for the executor
    assert prompt.endswith("run it")


def test_start_job_argv_codex_keeps_model_but_no_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _start_job_argv(
        tmp_path, monkeypatch, overseer="fable-5", executor="opus-4.8", job_type="codex"
    )
    # --model still applies (Claude oversees), but a codex job gets no delegation prefix —
    # instead the job_launch_prefix routes it into /codex-implement-task-and-claude-review.
    assert argv[:3] == ["claude", "--model", "claude-fable-5"]
    assert "[orchestration]" not in argv[7]
    assert argv[7].startswith("/codex-implement-task-and-claude-review ")


def test_start_job_effort_omitted_when_config_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # launch_effort = "" → no --effort flag; settings.json's effortLevel decides again.
    argv = _start_job_argv(
        tmp_path,
        monkeypatch,
        overseer="opus-4.8",
        executor="opus-4.8",
        config_toml='launch_effort = ""\n',
    )
    assert "--effort" not in argv


def test_start_job_invalid_launch_effort_ignored_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An unknown level never reaches the claude CLI (which would refuse to launch) —
    # the flag is omitted and a warning names the valid levels.
    argv = _start_job_argv(
        tmp_path,
        monkeypatch,
        overseer="opus-4.8",
        executor="opus-4.8",
        config_toml='launch_effort = "banana"\n',
    )
    assert "--effort" not in argv
    assert "launch_effort" in capsys.readouterr().err


def test_check_is_scoped_to_the_named_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    _seed_two_sessions(tmp_path).close()

    # Checking position 1 of sess-b must tick b-one — NOT a-one (which owns DB id 1).
    assert cmd_check(_args(position=1, session="sess-b")) == 0

    with Store(tmp_path / "command-center" / "state.db") as store:
        a = {s.text: s.checked for s in store.list_subgoals("sess-a")}
        b = {s.text: s.checked for s in store.list_subgoals("sess-b")}
    assert b == {"b-one": True, "b-two": False}
    assert a == {"a-one": False, "a-two": False}  # the other session is untouched


def test_check_out_of_range_errors_and_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    _seed_two_sessions(tmp_path).close()

    assert cmd_check(_args(position=99, session="sess-a")) == 1  # OOR → non-zero exit
    with Store(tmp_path / "command-center" / "state.db") as store:
        assert all(not s.checked for s in store.list_subgoals("sess-a"))


def test_short_aim_backfills_the_first_revision_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ccc short-aim`` labels the CURRENT AIM and, when it lost one, revision (1) too.

    Revision (1) is what the `/aim` column renders (``aim_column = "first"``), and its label
    does not come along with the current AIM's — so the generator writes both.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = _no_score_cfg()
    for attr, value in (
        ("short_aim", True),
        ("short_aim_backend", "codex"),
        ("short_aim_model", ""),
    ):
        setattr(cfg, attr, value)
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "command_center.short_aim.generate",
        lambda aim, **kw: f"label:{aim.split(':')[0]}",
    )
    (tmp_path / "command-center").mkdir(parents=True)
    db = tmp_path / "command-center" / "state.db"
    with Store(db) as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "first aim: ccc ls lists it")
        store.set_aim("s1", "second aim: pytest -q green")  # revision (1) carries no label

    assert cli.cmd_short_aim(argparse.Namespace(session="s1", dry_run=None)) == 0

    with Store(db) as store:
        session = store.get("s1")
    assert session is not None
    assert session.short_aim == "label:second aim"
    assert session.first_short_aim == "label:first aim"

    # Idempotent: an already-labelled revision (1) is not regenerated (no second codex call).
    monkeypatch.setattr("command_center.short_aim.generate", lambda aim, **kw: "regenerated")
    assert cli.cmd_short_aim(argparse.Namespace(session="s1", dry_run=None)) == 0
    with Store(db) as store:
        session = store.get("s1")
    assert session is not None and session.first_short_aim == "label:first aim"


def test_set_aim_first_rewrites_revision_one_without_adding_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ccc set-aim --first`` adapts ``/aim (1)`` in place; the current AIM stays put."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cfg = _no_score_cfg()
    # The label regenerates: it is what the narrow /aim column shows.
    setattr(cfg, "short_aim", True)  # noqa: B010 — _no_score_cfg is an untyped stub object
    monkeypatch.setattr("command_center.cli.config.load_config", lambda: cfg)
    spawned: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", lambda argv, **kw: spawned.append(argv))
    (tmp_path / "command-center").mkdir(parents=True)
    with Store(tmp_path / "command-center" / "state.db") as store:
        store.ensure("s1", cwd="/repo")
        store.set_aim("s1", "vague first aim")
        store.set_aim("s1", "second aim: pytest -q green")
        store.set_short_aim("s1", "vague first aim")  # stale label built on the OLD original

    args = argparse.Namespace(text="first aim, restated: ccc ls lists it", session="s1", first=True)
    assert cli.cmd_set_aim(args) == 0
    assert "first aim rewritten" in capsys.readouterr().out

    with Store(tmp_path / "command-center" / "state.db") as store:
        assert [h.aim for h in store.list_aim_history("s1")] == [
            "first aim, restated: ccc ls lists it",
            "second aim: pytest -q green",
        ]
        session = store.get("s1")
    assert session is not None and session.aim == "second aim: pytest -q green"
    # The stale short label (built on the OLD original) is dropped and regenerated — without
    # this the narrow /aim column keeps showing the pre-edit wording. No re-score: the rewrite
    # did not touch the current AIM.
    assert session.short_aim is None
    assert spawned == [["short-aim", "--session", "s1"]]

    # Re-running is a no-op; an empty --first is refused (exit 1), history untouched.
    assert cli.cmd_set_aim(args) == 0
    assert "first aim unchanged" in capsys.readouterr().out
    blank = argparse.Namespace(text="  ", session="s1", first=True)
    assert cli.cmd_set_aim(blank) == 1
    with Store(tmp_path / "command-center" / "state.db") as store:
        assert store.list_aim_history("s1")[0].aim == "first aim, restated: ccc ls lists it"


def _sg_args(items: list[str], **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "session": "s1",
        "items": items,
        "list": False,
        "adaptive": False,
        "merge": False,
        "source": "user",
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_subgoals_adaptive_and_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    db = tmp_path / "command-center" / "state.db"

    assert cmd_subgoals(_sg_args(["alpha", "beta"], adaptive=True)) == 0
    with Store(db) as store:
        session = store.get("s1")
        assert session is not None and session.subgoals_adaptive is True  # --adaptive recorded
        beta = next(s for s in store.list_subgoals("s1") if s.text == "beta")
        store.set_subgoal_checked(beta.id, True)

    # --merge regeneration keeps beta's tick (unchanged wording), gamma is new.
    assert cmd_subgoals(_sg_args(["alpha", "beta", "gamma"], adaptive=True, merge=True)) == 0
    with Store(db) as store:
        got = {s.text: s.checked for s in store.list_subgoals("s1")}
    assert got == {"alpha": False, "beta": True, "gamma": False}


def test_check_drift_records_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import drift as drift_mod
    from command_center.cli import cmd_check_drift
    from command_center.models import drift_unresolved

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    # drift_check now defaults OFF (fresh-install INERT contract); this test exercises the
    # drift-recording feature, so opt in explicitly via the on-disk config.
    (tmp_path / "command-center" / "config.toml").write_text(
        "drift_check = true\n", encoding="utf-8"
    )
    db = tmp_path / "command-center" / "state.db"
    with Store(db) as store:
        store.ensure("s1")
        store.set_aim("s1", "aim one concrete: pytest -q green")
        store.set_subgoals("s1", ["a", "b"], source="agent")  # history v1 (first, no drift)
        store.set_aim("s1", "aim two concrete: deploy smoke passes")  # AIM rev 2
        store.set_subgoals("s1", ["a", "c"], source="agent", merge=True)  # history v2 -> pending

    # Stub the impartial checker (no real LLM) — it flags medium drift.
    monkeypatch.setattr(
        drift_mod,
        "check_drift",
        lambda facts, model, **_k: {
            "severity": "medium",
            "drift": True,
            "reason": "dropped sub-goal b",
            "dimensions": {},
            "dropped": ["b"],
            "weakened": [],
        },
    )
    assert cmd_check_drift(argparse.Namespace(session="s1")) == 0
    with Store(db) as store:
        session = store.get("s1")
        assert session is not None and session.drift_severity == "medium"
        assert drift_unresolved(session) is True  # blue dot would show
        assert store.list_subgoal_history("s1")[-1].drift_severity == "medium"  # verdict on history


def test_subgoal_history_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_subgoal_history

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    db = tmp_path / "command-center" / "state.db"
    with Store(db) as store:
        store.ensure("s1")
        store.set_aim("s1", "aim one concrete: pytest -q green")
        store.set_subgoals("s1", ["a", "b"], source="agent")
        store.set_aim("s1", "aim two concrete: deploy smoke passes")
        store.set_subgoals("s1", ["a", "c"], source="agent", merge=True)
        history_id = store.latest_subgoal_history_id("s1")
        assert history_id is not None
        store.set_subgoal_history_drift(history_id, "medium", "dropped sub-goal b", "{}")
    capsys.readouterr()  # drain
    assert cmd_subgoal_history(argparse.Namespace(session="s1")) == 0
    out = capsys.readouterr().out
    assert "Sub-goal history" in out
    assert "drift:medium" in out and "from AIM v" in out


def test_uncheck_clears_the_position(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    _seed_two_sessions(tmp_path).close()

    assert cmd_check(_args(position=2, session="sess-a")) == 0
    assert cmd_check(_args(position=2, session="sess-a", uncheck=True)) == 0
    with Store(tmp_path / "command-center" / "state.db") as store:
        assert all(not s.checked for s in store.list_subgoals("sess-a"))


def _stub_open_tab(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``terminal.start_job_in_new_tab`` call (returns True = tab opened)."""
    calls: list[str] = []

    def _fake(session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr("command_center.terminal.start_job_in_new_tab", _fake)
    return calls


def test_open_job_opens_a_tab_for_a_valid_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-open", "/Users/x/repo", "Migrate tickets")
    store.close()
    calls = _stub_open_tab(monkeypatch)

    assert cmd_open_job(argparse.Namespace(session_id="job-open")) == 0
    assert calls == ["job-open"]  # routed through the SAME helper the TUI's r uses
    assert "opening future job" in capsys.readouterr().out


def test_open_job_rejects_unknown_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    calls = _stub_open_tab(monkeypatch)

    assert cmd_open_job(argparse.Namespace(session_id="nope")) == 1
    assert calls == []  # never opened a tab
    assert "no such job" in capsys.readouterr().err


def test_open_job_rejects_non_draft_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.ensure("live-sess", cwd="/Users/x/repo")  # a normal (non-draft) session
    store.close()
    calls = _stub_open_tab(monkeypatch)

    assert cmd_open_job(argparse.Namespace(session_id="live-sess")) == 1
    assert calls == []
    assert "not a future job" in capsys.readouterr().err


def test_open_job_rejects_archived_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft("job-archived", "/Users/x/repo", "Old idea")
    store.update_fields("job-archived", archived=True)
    store.close()
    calls = _stub_open_tab(monkeypatch)

    assert cmd_open_job(argparse.Namespace(session_id="job-archived")) == 1
    assert calls == []
    assert "archived" in capsys.readouterr().err


def _write_job_file(tmp_path: Path, session_id: str, aim: str = "Do the thing") -> Path:
    """A real future-job markdown file (as the in-note button passes to --file)."""
    from command_center import future_files

    path = tmp_path / f"{future_files.display_hash(session_id)}-job.md"
    path.write_text(
        future_files.serialize(session_id=session_id, aim=aim, repo="home/ccc"),
        encoding="utf-8",
    )
    return path


def test_open_job_from_file_reads_session_id_and_opens_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    sid = "8442ec48-2890-4b41-8315-0f12df96077c"
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(sid, "/Users/x/repo", "Migrate tickets")
    store.close()
    calls = _stub_open_tab(monkeypatch)
    job_file = _write_job_file(tmp_path, sid)

    assert cmd_open_job(argparse.Namespace(session_id=None, file=str(job_file))) == 0
    assert calls == [sid]  # session_id came from the file's frontmatter
    assert "opening future job" in capsys.readouterr().out


def test_open_job_rejects_both_id_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    calls = _stub_open_tab(monkeypatch)
    job_file = _write_job_file(tmp_path, "8442ec48-2890-4b41-8315-0f12df96077c")

    assert cmd_open_job(argparse.Namespace(session_id="8442", file=str(job_file))) == 1
    assert calls == []
    assert "not both" in capsys.readouterr().err


def test_open_job_rejects_neither_id_nor_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    calls = _stub_open_tab(monkeypatch)

    assert cmd_open_job(argparse.Namespace(session_id=None, file=None)) == 1
    assert calls == []
    assert "session_id or --file" in capsys.readouterr().err


def test_open_job_file_without_session_id_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    calls = _stub_open_tab(monkeypatch)
    bad = tmp_path / "no-frontmatter.md"
    bad.write_text("just some text, no session_id", encoding="utf-8")

    assert cmd_open_job(argparse.Namespace(session_id=None, file=str(bad))) == 1
    assert calls == []
    assert "no session_id" in capsys.readouterr().err


def test_open_job_file_missing_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    calls = _stub_open_tab(monkeypatch)

    missing = str(tmp_path / "does-not-exist.md")
    assert cmd_open_job(argparse.Namespace(session_id=None, file=missing)) == 1
    assert calls == []
    assert "cannot read" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# unique-prefix job-id resolution (`ccc start-job ad2096c4` where ad2096c4 is the
# 8-char id shown by `ccc jobs`) — routed through the shared store.resolve_job_id.
# --------------------------------------------------------------------------- #
_UUID_A = "ad2096c4-0000-4000-8000-000000000001"
_UUID_B = "ad2096c4-0000-4000-8000-000000000002"  # shares the 8-char display prefix with A
_UUID_C = "be317d55-0000-4000-8000-000000000003"


def test_open_job_resolves_a_unique_id_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(_UUID_C, "/Users/x/repo", "Only one with this prefix")
    store.close()
    calls = _stub_open_tab(monkeypatch)

    # `be317d55` is a unique prefix → resolves to the full UUID and opens its tab.
    assert cmd_open_job(argparse.Namespace(session_id="be317d55")) == 0
    assert calls == [_UUID_C]  # the tab opened for the full id, not the prefix
    assert "opening future job" in capsys.readouterr().out


def test_open_job_exact_full_id_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(_UUID_A, "/Users/x/repo", "Job A")
    store.create_draft(_UUID_B, "/Users/x/repo", "Job B")  # shares A's 8-char prefix
    store.close()
    calls = _stub_open_tab(monkeypatch)

    # An exact full id wins outright even though B shares its 8-char display prefix.
    assert cmd_open_job(argparse.Namespace(session_id=_UUID_A)) == 0
    assert calls == [_UUID_A]
    assert "opening future job" in capsys.readouterr().out


def test_open_job_ambiguous_prefix_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(_UUID_A, "/Users/x/repo", "Job A")
    store.create_draft(_UUID_B, "/Users/x/repo", "Job B")
    store.close()
    calls = _stub_open_tab(monkeypatch)

    # `ad2096` is a prefix of BOTH A and B → ambiguous, no tab opened.
    assert cmd_open_job(argparse.Namespace(session_id="ad2096")) == 1
    assert calls == []
    assert "ambiguous job id ad2096" in capsys.readouterr().err


def test_open_job_unknown_prefix_reports_no_such_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_open_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(_UUID_A, "/Users/x/repo", "Job A")
    store.close()
    calls = _stub_open_tab(monkeypatch)

    assert cmd_open_job(argparse.Namespace(session_id="ffffffff")) == 1
    assert calls == []
    assert "no such job" in capsys.readouterr().err


def test_start_job_resolves_a_unique_id_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug: `ccc start-job <8-char>` must launch, not error 'no such job'."""
    from command_center.cli import cmd_start_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress the detached ccc sync-mirrors spawn
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(_UUID_C, "/no/such/dir", "Do the thing", prompt="run it")
    store.close()

    captured: dict[str, list[str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id="be317d55"))
    # The full UUID (not the prefix) reaches the `claude --session-id` argv.
    assert _UUID_C in captured["argv"]
    assert "be317d55" not in captured["argv"]


def _make_scheduled_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_date: str) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress the detached ccc sync-mirrors spawn
    store = Store(tmp_path / "command-center" / "state.db")
    store.create_draft(
        "job-sched", "/no/such/dir", "Re-enable FileVault", prompt="run it", start_date=start_date
    )
    store.close()


def test_start_job_refuses_before_start_date_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, timedelta

    from command_center.cli import cmd_start_job

    _make_scheduled_draft(tmp_path, monkeypatch, (date.today() + timedelta(days=30)).isoformat())
    monkeypatch.setattr(
        "command_center.cli.sys.stdin", type("S", (), {"isatty": lambda s: False})()
    )
    called: list[str] = []
    monkeypatch.setattr("command_center.cli.os.execvp", lambda *_a: called.append("exec"))

    assert cmd_start_job(argparse.Namespace(session_id="job-sched")) == 1
    assert not called  # never launched
    store = Store(tmp_path / "command-center" / "state.db")
    assert store.get("job-sched").draft is True  # type: ignore[union-attr]  # untouched
    store.close()


def test_start_job_force_overrides_start_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, timedelta

    from command_center.cli import cmd_start_job

    _make_scheduled_draft(tmp_path, monkeypatch, (date.today() + timedelta(days=30)).isoformat())
    captured: dict[str, list[str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id="job-sched", force=True))
    assert captured["argv"][-1] == "run it"


def test_start_job_tty_yes_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date, timedelta

    from command_center.cli import cmd_start_job

    _make_scheduled_draft(tmp_path, monkeypatch, (date.today() + timedelta(days=30)).isoformat())
    monkeypatch.setattr("command_center.cli.sys.stdin", type("S", (), {"isatty": lambda s: True})())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    captured: dict[str, list[str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id="job-sched"))
    assert captured["argv"][-1] == "run it"


def test_start_job_tty_no_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date, timedelta

    from command_center.cli import cmd_start_job

    _make_scheduled_draft(tmp_path, monkeypatch, (date.today() + timedelta(days=30)).isoformat())
    monkeypatch.setattr("command_center.cli.sys.stdin", type("S", (), {"isatty": lambda s: True})())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")  # Enter = No
    monkeypatch.setattr(
        "command_center.cli.os.execvp", lambda *_a: (_ for _ in ()).throw(_StopExec)
    )

    assert cmd_start_job(argparse.Namespace(session_id="job-sched")) == 1
    store = Store(tmp_path / "command-center" / "state.db")
    assert store.get("job-sched").draft is True  # type: ignore[union-attr]
    store.close()


def test_start_job_past_start_date_needs_no_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center.cli import cmd_start_job

    _make_scheduled_draft(tmp_path, monkeypatch, "2020-01-01")  # date already reached
    captured: dict[str, list[str]] = {}

    def _capture(_file: str, argv: list[str]) -> None:
        captured["argv"] = argv
        raise _StopExec

    monkeypatch.setattr("command_center.cli.os.execvp", _capture)
    with pytest.raises(_StopExec):
        cmd_start_job(argparse.Namespace(session_id="job-sched"))
    assert captured["argv"][-1] == "run it"


# ---- done-job / delete-job / restore-job ------------------------------------
def _seed_filed_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sid: str) -> Path:
    """A draft whose future file exists in the tmp-vault future root; returns the file."""
    from command_center import config as _config
    from command_center import future_files

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")  # suppress detached ccc spawns
    cfg = _config.load_config()  # conftest points every vault dir at tmp_path
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    with Store(tmp_path / "command-center" / "state.db") as store:
        store.create_draft(sid, str(repo_dir), "Ship the feature", prompt="run it")
        file = (
            Path(cfg.future_dir).expanduser()
            / "other"
            / "repo"
            / future_files.job_filename(sid, "Ship the feature")
        )
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            future_files.serialize(
                session_id=sid,
                aim="Ship the feature",
                status="registered",
                repo=str(repo_dir),
                prompt="run it",
                created="2026-07-01",
            ),
            encoding="utf-8",
        )
        rel = str(file.relative_to(Path(cfg.vault_root).expanduser()))
        store.update_fields(sid, future_file=rel)
    return file


_JOB_UUID = "aaaa1111-2222-4333-8444-555566667777"


def test_done_job_promotes_draft_to_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import config as _config
    from command_center.cli import cmd_done_job

    file = _seed_filed_draft(tmp_path, monkeypatch, _JOB_UUID)
    assert cmd_done_job(argparse.Namespace(session_id=_JOB_UUID, file=None)) == 0
    assert "marked done" in capsys.readouterr().out
    with Store(tmp_path / "command-center" / "state.db") as store:
        row = store.get(_JOB_UUID)
        assert row is not None
        assert row.draft is False and row.done is True and row.done_at > 0
        assert row.archived is False  # a DONE session, not a cancelled draft
    # The future file left the live scan with a terminal "done" status.
    assert not file.exists()
    archive = Path(_config.load_config().future_dir).expanduser() / "_archive"
    copies = list(archive.glob("*.md"))
    assert len(copies) == 1
    assert 'status: "done"' in copies[0].read_text(encoding="utf-8")


def test_done_job_rejects_non_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center.cli import cmd_done_job

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")
    with Store(tmp_path / "command-center" / "state.db") as store:
        store.ensure("live-sess", cwd="/repo")
    assert cmd_done_job(argparse.Namespace(session_id="live-sess", file=None)) == 1
    assert "not a live future job" in capsys.readouterr().err


def test_delete_job_moves_file_to_trash_and_soft_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import config as _config
    from command_center import future_files
    from command_center.cli import cmd_delete_job

    file = _seed_filed_draft(tmp_path, monkeypatch, _JOB_UUID)
    assert cmd_delete_job(argparse.Namespace(session_id=_JOB_UUID, file=None)) == 0
    assert "deleted future job" in capsys.readouterr().out
    with Store(tmp_path / "command-center" / "state.db") as store:
        row = store.get(_JOB_UUID)
        assert row is not None
        assert row.draft is True and row.archived is True
        assert (row.future_file or "").startswith("01-llm-tasks/delete/")
    assert not file.exists()
    trash = list(Path(_config.load_config().delete_dir).expanduser().rglob("*.md"))
    assert len(trash) == 1
    text = trash[0].read_text(encoding="utf-8")
    assert 'status: "deleted"' in text
    assert 'deleted: "' in text
    # Restore button only — the start/done/delete action row is gone.
    assert future_files._RESTORE_JOB_COMMAND_ID in text
    assert future_files._START_JOB_COMMAND_ID not in text


def test_restore_job_stages_deleted_job_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import config as _config
    from command_center import future_files
    from command_center.cli import cmd_delete_job, cmd_restore_job

    _seed_filed_draft(tmp_path, monkeypatch, _JOB_UUID)
    assert cmd_delete_job(argparse.Namespace(session_id=_JOB_UUID, file=None)) == 0
    assert cmd_restore_job(argparse.Namespace(session_id=_JOB_UUID, file=None)) == 0
    assert "staged back into FUTURE" in capsys.readouterr().out
    cfg = _config.load_config()
    with Store(tmp_path / "command-center" / "state.db") as store:
        row = store.get(_JOB_UUID)
        assert row is not None
        assert row.draft is True and row.archived is False
    assert list(Path(cfg.delete_dir).expanduser().rglob("*.md")) == []  # trash emptied
    live = list(Path(cfg.future_dir).expanduser().rglob("*.md"))
    assert len(live) == 1
    text = live[0].read_text(encoding="utf-8")
    assert 'status: "registered"' in text
    assert "`BUTTON[start-job, done-job, delete-job]`" in text
    assert future_files._RESTORE_JOB_COMMAND_ID not in text


def test_restore_job_reregisters_pruned_row_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import config as _config
    from command_center.cli import cmd_delete_job, cmd_restore_job

    _seed_filed_draft(tmp_path, monkeypatch, _JOB_UUID)
    assert cmd_delete_job(argparse.Namespace(session_id=_JOB_UUID, file=None)) == 0
    cfg = _config.load_config()
    trash = list(Path(cfg.delete_dir).expanduser().rglob("*.md"))[0]
    with Store(tmp_path / "command-center" / "state.db") as store:
        store.delete(_JOB_UUID)  # the row is gone — only the trashed file remains
    assert cmd_restore_job(argparse.Namespace(session_id=None, file=str(trash))) == 0
    assert "re-registered" in capsys.readouterr().out
    with Store(tmp_path / "command-center" / "state.db") as store:
        row = store.get(_JOB_UUID)
        assert row is not None
        assert row.draft is True and row.archived is False
        assert row.aim == "Ship the feature"
    assert not trash.exists()
    assert len(list(Path(cfg.future_dir).expanduser().rglob("*.md"))) == 1


# --- multi-account: statusline usage capture routing ----------------------------


def test_account_from_env_matches_configured_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE_CONFIG_DIR pointing at a configured account resolves to that label."""
    from command_center import config

    work = (tmp_path / "work").resolve()
    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / "priv", "work": work},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    assert cli._account_from_env() == "work"


def test_account_from_env_unconfigured_dir_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env dir matching no configured account → None (caller must skip the write)."""
    from command_center import config

    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / "priv", "work": tmp_path / "work"},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "somewhere-else"))
    assert cli._account_from_env() is None


def test_account_from_env_single_account_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var + exactly one configured account → that sole label."""
    from command_center import config

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(config, "claude_config_dirs", lambda: {"private": tmp_path / "priv"})
    assert cli._account_from_env() == "private"


def test_account_from_env_multi_without_env_is_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var + several accounts → the FIRST (default) label.

    By accounts.py's invariant the default account runs with CLAUDE_CONFIG_DIR UNSET, so
    an unset env unambiguously IS the default account (no guessing). This is the bug fix:
    before it, a private session under a two-account setup never wrote its usage snapshot.
    """
    from command_center import config

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / "priv", "work": tmp_path / "work"},
    )
    assert cli._account_from_env() == "private"


def test_account_from_env_set_unknown_dir_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SET env var matching no configured account still returns None (never guess)."""
    from command_center import config

    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / "priv", "work": tmp_path / "work"},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "unknown"))
    assert cli._account_from_env() is None


def _live_rate_limits() -> dict:
    import time

    return {
        "five_hour": {"used_percentage": 27, "resets_at": int(time.time()) + 3600},
        "seven_day": {"used_percentage": 93, "resets_at": int(time.time()) + 7 * 86400},
    }


def test_capture_usage_routes_write_to_env_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The captured snapshot lands on the env-selected account, not the private card."""
    import io
    import json

    from command_center import config, usage

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    work = (tmp_path / "work").resolve()
    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / "priv", "work": work},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    payload = json.dumps({"session_id": "s1", "rate_limits": _live_rate_limits()})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    data = cli._read_statusline_stdin()
    assert data is not None
    cli._capture_usage_from_payload(data)
    assert usage.read_usage() is None  # NOT the private card
    assert usage.read_usage("work") is not None  # landed on the work account


def test_capture_usage_skips_write_when_account_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable account skips the write entirely — no card is contaminated."""
    import io
    import json

    from command_center import config, usage

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": tmp_path / "priv", "work": tmp_path / "work"},
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "unconfigured"))
    payload = json.dumps({"session_id": "s1", "rate_limits": _live_rate_limits()})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    data = cli._read_statusline_stdin()
    assert data is not None
    cli._capture_usage_from_payload(data)
    assert usage.read_usage() is None
    assert usage.read_usage("work") is None
    # Nothing was written at all.
    assert list(config.app_home().glob("usage*.json")) == []


# ---- restart-tui ------------------------------------------------------------
def test_restart_tui_with_no_running_tui_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from command_center import jumpstate
    from command_center.cli import cmd_restart_tui

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "command-center").mkdir(parents=True)
    assert jumpstate.get_tui() is None  # no TUI registered

    requested: list[bool] = []
    monkeypatch.setattr(jumpstate, "request_restart", lambda: requested.append(True))

    assert cmd_restart_tui(argparse.Namespace()) == 1
    assert requested == []  # never even asked for a restart
    assert "no running ccc TUI" in capsys.readouterr().err


# ---- mark-done --close / --quiet -------------------------------------------
def test_mark_done_close_arms_close_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mark-done --close` stamps close_requested_at (> 0) alongside marking done."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)  # never fork a real ccc
    assert cli.main(["mark-done", "--session", "s1", "--close"]) == 0
    with Store() as store:
        got = store.get("s1")
    assert got is not None and got.done is True and got.close_requested_at > 0


def test_mark_done_close_is_noop_under_ccc_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under a headless/SDK entrypoint (CCC_INTERNAL) arming is skipped but mark-done still wins."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_INTERNAL", "1")
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    assert cli.main(["mark-done", "--session", "s1", "--close"]) == 0
    with Store() as store:
        got = store.get("s1")
    assert got is not None and got.done is True
    assert got.close_requested_at == 0  # arming no-op'd
    assert "--close ignored" in capsys.readouterr().err


def test_mark_done_undo_clears_armed_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mark-done --undo` disarms any pending close so a resumed session won't self-close."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    with Store() as store:
        store.ensure("s1", cwd="/repo")
        store.update_fields("s1", close_requested_at=1_234_567)  # previously armed
    assert cli.main(["mark-done", "--session", "s1", "--undo"]) == 0
    with Store() as store:
        got = store.get("s1")
    assert got is not None and got.done is False and got.close_requested_at == 0


def test_mark_done_close_and_undo_is_argparse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--close` and `--undo` are mutually exclusive — argparse rejects them together."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        cli.main(["mark-done", "--session", "s1", "--close", "--undo"])


def test_mark_done_quiet_produces_no_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-q` suppresses the human summary — stdout stays empty on success."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    assert cli.main(["mark-done", "--session", "s1", "-q"]) == 0
    assert capsys.readouterr().out == ""


# ---- close-now -------------------------------------------------------------
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_CLOSE_NOW_SETTLE_SEC", 0.0)  # don't actually sleep


def test_close_now_sigterms_a_fresh_live_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh, matching, alive Claude PID is SIGTERM'd."""
    import signal

    from command_center.models import LiveSession

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _no_settle(monkeypatch)
    live = LiveSession(pid=4321, session_id="s1", cwd="/repo", alive=True)
    monkeypatch.setattr(cli.ClaudeAdapter, "discover", lambda _self: [live])
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("command_center.terminal.tmux_pane_for_session", lambda _sid: None)
    monkeypatch.setattr(
        "command_center.terminal.close_iterm_session", lambda _x: (_ for _ in ()).throw(OSError)
    )
    assert cli.cmd_close_now(argparse.Namespace(session="s1", iterm="")) == 0
    assert killed == [(4321, signal.SIGTERM)]


def test_close_now_no_pid_no_iterm_leaves_stale_tab_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored stale iterm id + no live PID + no --iterm → never close (stale-evidence guard)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _no_settle(monkeypatch)
    with Store() as store:
        store.ensure("s1", cwd="/repo")
        store.update_fields("s1", iterm_session_id="w0t1p0:STALE")
    monkeypatch.setattr(cli.ClaudeAdapter, "discover", lambda _self: [])  # no live PID
    monkeypatch.setattr("command_center.terminal.tmux_pane_for_session", lambda _sid: None)
    closed: list[str] = []
    monkeypatch.setattr("command_center.terminal.close_iterm_session", closed.append)
    assert cli.cmd_close_now(argparse.Namespace(session="s1", iterm="")) == 0
    assert closed == []  # never close on a store-only (stale) id


def test_close_now_iterm_flag_closes_even_without_a_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook-supplied fresh --iterm id closes the tab even when no live PID is found."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _no_settle(monkeypatch)
    monkeypatch.setattr(cli.ClaudeAdapter, "discover", lambda _self: [])
    monkeypatch.setattr("command_center.terminal.tmux_pane_for_session", lambda _sid: None)
    closed: list[str] = []
    monkeypatch.setattr("command_center.terminal.close_iterm_session", closed.append)
    assert cli.cmd_close_now(argparse.Namespace(session="s1", iterm="w0t1p0:FRESH")) == 0
    assert closed == ["w0t1p0:FRESH"]


def test_close_now_tmux_kills_only_the_matched_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching tmux pane → only that pane id is passed to kill-pane (iTerm untouched)."""
    import subprocess

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _no_settle(monkeypatch)
    monkeypatch.setattr(cli.ClaudeAdapter, "discover", lambda _self: [])
    # A two-pane window where only %5 hosts this session's claude.
    monkeypatch.setattr(
        "command_center.terminal.tmux_pane_for_session", lambda _sid: ("ccc:3", "%5")
    )
    runs: list[list[str]] = []

    def _fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        runs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(
        "command_center.terminal.close_iterm_session", lambda _x: (_ for _ in ()).throw(OSError)
    )
    # tmux matched first → iTerm must NOT be touched even though --iterm was supplied.
    assert cli.cmd_close_now(argparse.Namespace(session="s1", iterm="w0t1p0:X")) == 0
    assert runs == [["tmux", "kill-pane", "-t", "%5"]]
