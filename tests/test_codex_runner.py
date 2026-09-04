"""Tests for the ONE Codex runner: seat order, run-time fallback, process hygiene.

The failure this module exists to prevent is silent and expensive: on 2026-09-04 every
`codex exec` in the toolbox inherited `~/.codex` (a team seat out of credits, on an
administrative hold), refused, and the callers reported "codex exited 1" — so the two
healthy paid logins on the same machine were never tried. What is guarded here:

* a REFUSAL hops to the next configured seat and is recorded, so the next call skips it;
* a TASK failure, a timeout, a stall and a write-mode refusal that already touched the
  worktree do NOT hop — hopping there re-runs a failing task, or layers a second seat's
  edits on top of a half-done one;
* zero eligible seats spawn NO process at all (a refusal we can predict is not worth a
  round trip), and `CCC_NO_CODEX` spawns none either;
* nothing is classified from the PROMPT or from codex's own words — only from the
  server-authored `error`/`turn.failed` events, else the stderr tail;
* the argv is rebuilt per seat (that seat's permission profile and MCP servers, a fresh
  `-o`), the prompt travels on stdin, and codex's whole process GROUP is swept on exit.

The seats come from the `three_seats` fixture (temp `$HOME`, three `auth.json`s, three
different `config.toml`s) and `codex` itself is `tests/fakes/fake_codex.py`.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import SeatFixture, make_three_seats

from command_center import codex_in_claude as cic
from command_center import codex_launch, quota

_FIXTURES = Path(__file__).parent / "fixtures" / "codex_json"
_MODEL = "gpt-5.6-sol"


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline — `valid_slug` must not shell out to codex."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [{"slug": _MODEL, "visibility": "list", "default_reasoning_level": "medium"}],
    )


def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    """A `run` argv namespace pointed at the fixture's workdir."""
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": _MODEL,
        "effort": "low",
        "timeout": 60,
        "idle_timeout": 0,
        "purpose": "test",
        "max_attempts": 0,
        "persist": False,
        "ignore_quota": False,
        "json": True,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _delegate_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    """A `delegate` argv namespace pointed at the fixture's workdir."""
    base: dict[str, object] = {
        "prompt": "do the thing",
        "write": False,
        "scout": False,
        "cwd": str(seats.workdir),
        "round": 1,
        "feedback": None,
        "model": _MODEL,
        "purpose": "delegate",
        "effort": "low",
        "timeout": 60,
        "idle_timeout": 0,
        "max_concurrent": 0,  # <=0 disables the flock gate: never touch a real slot dir
        "resume": None,
        "no_repo_map": True,
        "repo_map": None,
        "show_prompt": False,
        "max_attempts": 0,
        "ignore_quota": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict:
    """The single JSON object `run -j` prints."""
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


# ── seat order + run-time fallback ────────────────────────────────────────────────
def test_hops_in_configured_order_on_quota_refusal(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """private refuses on quota → de serves; the refusal is recorded with ITS deadline."""
    reset_at = int(time.time()) + 4200
    three_seats.scenarios(
        private={"scenario": "refuse_quota", "resets_at": reset_at},
        de={"scenario": "ok", "reply": "de answered"},
    )
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["private", "de"]
    assert envelope["seat"]["label"] == "de"
    assert envelope["reply"] == "de answered"
    assert [a["outcome"] for a in envelope["attempts"]] == ["refused:quota", "ok"]
    entry = quota.read_cooldowns()["codex:private"]
    assert entry["source"] == "codex-exec"
    assert entry["scope"] == "quota"
    # The deadline comes from the seat's own exhausted window, not from a flat guess.
    assert entry["blocked_until"] == reset_at


def test_hops_private_quota_de_quota_default_ok(three_seats: SeatFixture) -> None:
    """Two refusals in a row keep hopping — and BOTH seats end up blocked."""
    three_seats.scenarios(
        private="refuse_quota",
        de="refuse_quota",
        default={"scenario": "ok", "reply": "team answered"},
    )
    result = cic.cmd_run(_run_ns(three_seats, json=False))
    assert result == cic.EX_OK
    assert three_seats.call_homes() == ["private", "de", "default"]
    cooldowns = quota.read_cooldowns()
    assert set(cooldowns) == {"codex:private", "codex:de"}


def test_held_seat_is_never_attempted(three_seats: SeatFixture) -> None:
    """An administrative hold removes a seat BEFORE any process is started."""
    quota.record_block(
        "codex:private",
        blocked_until=int(time.time()) + 3600,
        kind=quota.KIND_HOLD,
        reason="private seat reserved",
    )
    three_seats.scenarios(de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert three_seats.call_homes() == ["de"]


def test_all_seats_unavailable_spawns_no_process(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing eligible ⇒ no codex at all, a typed error and the earliest reset."""
    resets = {}
    for index, (label, pid) in enumerate(
        (("private", "codex:private"), ("de", "codex:de"), ("default", "codex"))
    ):
        resets[label] = int(time.time()) + 600 + index * 600
        quota.record_block(pid, blocked_until=resets[label], kind=quota.KIND_HOLD, reason="held")
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_QUOTA
    envelope = _envelope(capsys)
    assert three_seats.calls() == []
    assert envelope["error"]["kind"] == "all_seats_unavailable"
    assert envelope["error"]["earliest_reset"] == min(resets.values())
    assert envelope["seat"] is None
    # delegate maps the same state to the quota exit code.
    assert cic.cmd_delegate(_delegate_ns(three_seats)) == cic.EX_QUOTA
    assert three_seats.calls() == []


def test_ccc_no_codex_is_zero_execution(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The kill switch is checked in the RUNNER's own env — no candidates, no process."""
    monkeypatch.setenv("CCC_NO_CODEX", "1")
    assert cic.codex_homes_in_order() == []
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_QUOTA
    assert _envelope(capsys)["error"]["kind"] == "disabled"
    assert three_seats.calls() == []


def test_explicit_codex_home_is_a_singleton_and_records_nothing(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An inherited $CODEX_HOME is a hard instruction: one seat, no hop, no cooldown."""
    unregistered = three_seats.home / "seats" / "adhoc"
    unregistered.mkdir(parents=True)
    (unregistered / "config.toml").write_text(
        'default_permissions = "hardened-ro"\n\n'
        '[permissions.hardened-ro]\nextends = ":read-only"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(unregistered))
    three_seats.scenarios()  # every home defaults to `ok`
    candidates = cic.codex_homes_in_order()
    assert [(c.label, c.pid) for c in candidates] == [("explicit", "")]

    three_seats.control.write_text(
        json.dumps({str(unregistered): "refuse_quota"}), encoding="utf-8"
    )
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_QUOTA
    envelope = _envelope(capsys)
    assert [call["home"] for call in three_seats.calls()] == [str(unregistered)]
    assert [a["outcome"] for a in envelope["attempts"]] == ["refused:quota"]
    assert quota.read_cooldowns() == {}  # no provider id ⇒ nothing to attribute


def test_candidates_reevaluated_between_attempts(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold written WHILE attempt 1 runs removes seat 2, so seat 3 serves."""
    three_seats.scenarios(
        private="refuse_quota", de="refuse_quota", default={"scenario": "ok", "reply": "team"}
    )
    real_exec = cic._exec_codex  # noqa: SLF001
    calls = {"n": 0}

    def exec_then_hold(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = real_exec(*args, **kwargs)  # type: ignore[arg-type]
        calls["n"] += 1
        if calls["n"] == 1:  # between attempt 1 and 2 the `de` seat is reserved away
            quota.record_block(
                "codex:de",
                blocked_until=int(time.time()) + 3600,
                kind=quota.KIND_HOLD,
                reason="de reserved mid-run",
            )
        return proc

    monkeypatch.setattr(cic, "_exec_codex", exec_then_hold)
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert three_seats.call_homes() == ["private", "default"]


def test_single_deadline_across_attempts(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One budget for the whole call: attempt 2 gets what attempt 1 left, and a hang ends it."""
    # A wall timeout is TERMINAL: seat 2 is never tried, even though it would serve.
    three_seats.scenarios(private="hang", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats, timeout=4)) == cic.EX_TIMEOUT
    assert _envelope(capsys)["error"]["kind"] == "timeout"
    assert three_seats.call_homes() == ["private"]

    # And the budget is shared: what attempt 1 spends, attempt 2 does not get back.
    three_seats.reset_log()
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    seen: list[int] = []
    real_exec = cic._exec_codex  # noqa: SLF001

    def record_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(int(str(kwargs["timeout"])))
        time.sleep(1.1)  # burn budget so attempt 2 must get strictly less
        return real_exec(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cic, "_exec_codex", record_timeout)
    assert cic.cmd_run(_run_ns(three_seats, timeout=30, json=False)) == cic.EX_OK
    assert len(seen) == 2
    assert 29 <= seen[0] <= 30  # the first attempt gets (almost) the whole budget
    assert seen[1] < seen[0]  # the second gets what the first left, not a fresh 30


# ── what must NOT hop ─────────────────────────────────────────────────────────────
def test_task_failure_does_not_hop(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A traceback is the task's fault, not the seat's: report it, keep the seat."""
    three_seats.scenarios(private="task_fail", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_CODEX_FAIL
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["private"]
    assert envelope["error"]["kind"] == "codex_failed"
    assert "the task itself failed" in envelope["error"]["message"]
    assert quota.read_cooldowns() == {}


def test_prompt_text_never_classifies(three_seats: SeatFixture) -> None:
    """A prompt full of refusal vocabulary must not look like a refusal."""
    three_seats.scenarios(private="task_fail", de={"scenario": "ok", "reply": "de"})
    prompt = "fix the handler for usage_limit_exceeded rate limit 401 unauthorized errors"
    assert cic.cmd_run(_run_ns(three_seats, prompt=prompt, json=False)) == cic.EX_CODEX_FAIL
    assert three_seats.call_homes() == ["private"]
    assert quota.read_cooldowns() == {}


def test_stderr_tail_fallback_classifies_quota(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """No failure EVENT at all ⇒ the last 40 stderr lines decide (and 80 are printed)."""
    three_seats.scenarios(private="refuse_stderr_only", de={"scenario": "ok", "reply": "de served"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["private", "de"]
    assert [a["outcome"] for a in envelope["attempts"]] == ["refused:quota", "ok"]
    assert "codex:private" in quota.read_cooldowns()


def test_success_clears_observed_block_not_hold(three_seats: SeatFixture) -> None:
    """A seat that serves disproves its own rejection — but never an administrative hold."""
    now = int(time.time())
    quota.record_block("codex:private", blocked_until=now - 1, reason="stale", source="codex-exec")
    quota.record_block(
        "codex:de", blocked_until=now + 3600, kind=quota.KIND_HOLD, reason="de reserved"
    )
    three_seats.scenarios(private={"scenario": "ok", "reply": "private"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert three_seats.call_homes() == ["private"]
    stored = quota._read_cooldowns_unlocked()  # noqa: SLF001
    assert "codex:private" not in stored  # the expired observed block was cleared
    assert stored["codex:de"]["kind"] == quota.KIND_HOLD  # the hold stands


# ── write mode ────────────────────────────────────────────────────────────────────
def test_write_mode_midrun_refusal_stops_and_journals(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal AFTER codex touched the worktree is terminal and reported for review."""
    three_seats.reorder("de", "default", "private")  # only `de` declares hardened-rw
    three_seats.scenarios(de="midrun_write", default={"scenario": "ok", "reply": "team"})
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    args = _delegate_ns(three_seats, write=True, max_attempts=0)
    assert cic.cmd_delegate(args) == cic.EX_CODEX_FAIL
    out = capsys.readouterr().out
    assert three_seats.call_homes() == ["de"]  # NOT retried on the team seat
    assert "### SEAT-REFUSED-MIDRUN de" in out
    assert (three_seats.workdir / "touched.txt").exists()
    # The session is journalled under the seat that ran it, so --resume can re-attach.
    assert codex_journal(three_seats.seats["de"])


def codex_journal(home: Path) -> list[dict]:
    """The seat journal ccc writes for every persistent launch."""
    path = home / "ccc-sessions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_write_mode_clean_refusal_hops(three_seats: SeatFixture) -> None:
    """A refusal with an UNTOUCHED worktree is safe to retry on the next seat."""
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    three_seats.reorder("de", "default", "private")  # only `de` declares hardened-rw
    three_seats.scenarios(de="refuse_quota", default={"scenario": "ok", "reply": "team"})
    # `default` has no hardened-rw profile either, so the write run is refused there —
    # what matters is that the runner GOT there, i.e. it hopped off the clean refusal.
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True)) == cic.EX_USAGE
    assert three_seats.call_homes() == ["de"]
    assert "codex:de" in quota.read_cooldowns()


def test_write_refused_on_seat_without_rw_profile(three_seats: SeatFixture) -> None:
    """No [permissions.hardened-rw] on the leading seat ⇒ exit 2, codex never runs."""
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    three_seats.scenarios()
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True)) == cic.EX_USAGE
    assert three_seats.calls() == []


# ── resume ────────────────────────────────────────────────────────────────────────
def test_resume_binds_to_recording_home(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """--resume runs on the seat whose journal holds the session — never seat 1."""
    session = "019ff5b3-7bea-7c80-ad5e-21cc5b7c64bd"
    codex_launch.record_launch(
        session, str(three_seats.workdir), write=False, codex_home=three_seats.seats["de"]
    )
    three_seats.scenarios(
        private={"scenario": "ok", "reply": "WRONG SEAT"}, de={"scenario": "ok", "reply": "resumed"}
    )
    assert cic.cmd_delegate(_delegate_ns(three_seats, resume=session)) == cic.EX_OK
    assert three_seats.call_homes() == ["de"]
    call = three_seats.calls()[0]
    assert call["argv"][:3] == ["exec", "resume", session]
    assert "resumed" in capsys.readouterr().out


# ── argv shape (per invocation) ───────────────────────────────────────────────────
def test_argv_rebuilt_per_seat_mcp_and_profile(three_seats: SeatFixture) -> None:
    """Each attempt carries ONLY its own seat's MCP servers and permission profile."""
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    first, second = (call["argv"] for call in three_seats.calls())
    assert 'default_permissions="hardened-ro"' in first
    assert "mcp_servers.alpha.enabled=false" in first
    assert not [arg for arg in first if arg.startswith(("mcp_servers.beta", "mcp_servers.gamma"))]
    assert "mcp_servers.beta.enabled=false" in second
    assert "mcp_servers.gamma.enabled=false" in second
    assert "mcp_servers.alpha.enabled=false" not in second
    for argv in (first, second):
        assert argv[:3] == ["exec", "--json"] + ["--ephemeral"]
        assert argv[argv.index("-C") + 1] == str(three_seats.workdir)
        assert argv[argv.index("-m") + 1] == _MODEL
        assert "model_reasoning_effort=low" in argv
        assert "--skip-git-repo-check" in argv  # the fixture workdir is not a repo


def test_argv_fresh_output_file_per_attempt(three_seats: SeatFixture) -> None:
    """Each attempt gets its OWN -o file, and none survives the call."""
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    outs = [call["argv"][call["argv"].index("-o") + 1] for call in three_seats.calls()]
    assert len(set(outs)) == 2
    assert not [path for path in outs if Path(path).exists()]


def test_argv_never_uses_legacy_sandbox_flag(three_seats: SeatFixture) -> None:
    """`-s`/`--sandbox` forces the legacy sandbox and drops the profile's deny rules."""
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    for call in three_seats.calls():
        assert "-s" not in call["argv"] and "--sandbox" not in call["argv"]


def test_argv_ephemeral_for_run_and_persistent_for_delegate(three_seats: SeatFixture) -> None:
    """`run` leaves no session behind; `delegate` keeps and journals one."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "hi"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert "--ephemeral" in three_seats.calls()[0]["argv"]
    assert codex_journal(three_seats.seats["private"]) == []

    assert cic.cmd_delegate(_delegate_ns(three_seats)) == cic.EX_OK
    delegate_argv = three_seats.calls()[1]["argv"]
    assert "--ephemeral" not in delegate_argv
    assert codex_journal(three_seats.seats["private"])


def test_prompt_travels_on_stdin_not_argv(three_seats: SeatFixture) -> None:
    """The prompt is never in argv (ARG_MAX, and `ps` readability) — argv ends with `-`."""
    big = "x" * 300_000
    three_seats.scenarios(private={"scenario": "ok", "reply": "ok"})
    assert cic.cmd_run(_run_ns(three_seats, prompt=big, json=False)) == cic.EX_OK
    argv = three_seats.calls()[0]["argv"]
    assert argv[-1] == "-"
    assert not [arg for arg in argv if big[:64] in arg]
    assert three_seats.stdin_of(1) == big


def test_child_env_carries_the_guard_vars(three_seats: SeatFixture) -> None:
    """The CHILD gets the three guards; the runner's own env is left untouched."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "ok"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    env = three_seats.calls()[0]["env"]
    assert env["CCC_NO_CODEX"] == "1"
    assert env["CCC_INTERNAL"] == "1"
    assert env["AI_NO_AUTOCOMMIT"] == "1"
    assert env["CODEX_HOME"] == str(three_seats.seats["private"])
    assert "CCC_NO_CODEX" not in os.environ


def test_attempt_budget_stops_early(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """-n caps the number of PHYSICAL attempts, whatever the seat order allows."""
    three_seats.scenarios(
        private="refuse_quota", de="refuse_quota", default={"scenario": "ok", "reply": "team"}
    )
    assert cic.cmd_run(_run_ns(three_seats, max_attempts=2)) == cic.EX_QUOTA
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["private", "de"]
    assert envelope["error"]["kind"] == "attempts_exhausted"


# ── CLI surfaces ──────────────────────────────────────────────────────────────────
def test_run_cli_envelope_schema(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The versioned `-j` envelope external consumers parse."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "the answer"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    assert envelope["schema_version"] == 1
    assert set(envelope) == {
        "schema_version",
        "model",
        "effort",
        "ok",
        "runner_pid",
        "seat",
        "attempts",
        "reply",
        "error",
        "session_id",
    }
    assert envelope["model"] == _MODEL and envelope["effort"] == "low"
    assert envelope["ok"] is True and envelope["error"] is None
    assert envelope["reply"] == "the answer"
    assert envelope["runner_pid"] == os.getpid()
    assert set(envelope["seat"]) == {"label", "id", "home", "email"}
    assert envelope["seat"] == {
        "label": "private",
        "id": "codex:private",
        "home": str(three_seats.seats["private"]),
        "email": "private@example.org",
    }
    assert set(envelope["attempts"][0]) == {"seat", "home", "elapsed_s", "outcome"}


def test_run_cli_text_mode_prints_model_then_seat(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Text mode: `model:` then `seat:` then the reply — nothing else before them."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "hello"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"model: {_MODEL} (effort low)"
    assert lines[1] == "seat: private (private@example.org)"
    assert lines[2] == "hello"


def test_run_cli_reads_prompt_from_stdin(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-` (or no PROMPT) reads the prompt from stdin — the consumers' calling shape."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "ok"})
    monkeypatch.setattr(sys, "stdin", io.StringIO("prompt from stdin"))
    assert cic.cmd_run(_run_ns(three_seats, prompt="-", json=False)) == cic.EX_OK
    capsys.readouterr()
    assert three_seats.stdin_of(1) == "prompt from stdin"


def test_delegate_second_stdout_line_is_seat(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """delegate's contract: line 1 = the model, line 2 = the seat (+ [fallback] on a hop)."""
    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "done"})
    assert cic.cmd_delegate(_delegate_ns(three_seats)) == cic.EX_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"model: {_MODEL} (effort low)"
    assert lines[1] == "seat: private (private@example.org)"
    assert lines[2] == "seat: de (de@example.org) [fallback]"


def test_candidates_map_label_to_home_and_email(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every seat resolves label → its own path → its own account, in both CLIs."""
    expected = {
        "private": (str(three_seats.seats["private"]), "private@example.org"),
        "de": (str(three_seats.seats["de"]), "de@example.org"),
        "default": (str(three_seats.seats["default"]), "team@example.org"),
    }
    cic.cmd_order(argparse.Namespace(labels=[], clear=False, json=True))
    order_payload = json.loads(capsys.readouterr().out)
    assert {c["label"]: (c["home"], c["email"]) for c in order_payload["candidates"]} == expected
    cic.cmd_home(argparse.Namespace(path=None, until=None, clear=False, json=True))
    home_payload = json.loads(capsys.readouterr().out)
    assert {c["label"]: (c["home"], c["email"]) for c in home_payload["candidates"]} == expected
    assert home_payload["home"] == home_payload["candidates"][0]["home"]


# ── classification units (the captured streams) ────────────────────────────────────
def _events(name: str) -> list[dict]:
    return cic.parse_json_events((_FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("quota_refusal", "quota"),
        ("entitlement", "entitlement"),
        ("auth", "auth"),
        ("server_overloaded", ""),
    ],
)
def test_classify_from_captured_streams(fixture: str, kind: str) -> None:
    """The real refusal + the three synthetic ones classify from their EVENTS only."""
    failure = cic.classify_codex_failure(1, _events(fixture), "")
    assert failure.kind == kind
    assert failure.resets_at is None


def test_classify_ok_stream_and_nonzero_exit_with_partial_reply() -> None:
    """rc 0 is success; rc != 0 is ALWAYS a failure, partial `-o` file or not."""
    events = _events("ok")
    assert cic.classify_codex_failure(0, events, "").kind == ""
    # The same successful-looking stream with a non-zero exit is still a failure.
    assert cic.classify_codex_failure(1, events, "").kind == ""
    assert cic.parse_json_events("junk\n{}\nnot json\n") == [{}]


def test_classify_ignores_item_text_and_stdout() -> None:
    """Item text is codex's own words — never classification input."""
    events: list[dict] = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "we are out of credits, unauthorized"},
        },
    ]
    assert cic.classify_codex_failure(1, events, "").kind == ""


# ── process-group hygiene ─────────────────────────────────────────────────────────
def _sentinel_env(three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the fake's grandchild at a sentinel path and return it."""
    sentinel = three_seats.home / "grandchild.touched"
    monkeypatch.setenv("FAKE_CODEX_SENTINEL", str(sentinel))
    return sentinel


def _fake_codex_path(tmp_path: Path) -> str:
    """A ``$PATH`` whose first entry provides ``codex`` = the fake (for subprocess runs)."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    link = bindir / "codex"
    if not link.exists():
        link.symlink_to(Path(__file__).parent / "fakes" / "fake_codex.py")
    return f"{bindir}:{os.environ['PATH']}"


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.slow
def test_runner_timeout_kills_whole_process_group(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wall timeout sweeps codex's GROUP — the grandchild never gets to touch anything."""
    sentinel = _sentinel_env(three_seats, monkeypatch)
    three_seats.scenarios(private="hang_with_grandchild")
    started = time.monotonic()
    assert cic.cmd_run(_run_ns(three_seats, timeout=2)) == cic.EX_TIMEOUT
    assert time.monotonic() - started < 15
    assert _envelope(capsys)["error"]["kind"] == "timeout"
    pgid = three_seats.calls()[0]["pgid"]
    time.sleep(9)
    assert not sentinel.exists()
    assert _group_gone(pgid)


@pytest.mark.slow
def test_runner_stall_kills_whole_process_group(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same sweep on the idle watchdog: silence is a kill, not a leak."""
    sentinel = _sentinel_env(three_seats, monkeypatch)
    three_seats.scenarios(private="stall_with_grandchild")
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=2)) == cic.EX_TIMEOUT
    assert _envelope(capsys)["error"]["kind"] == "stalled"
    pgid = three_seats.calls()[0]["pgid"]
    time.sleep(9)
    assert not sentinel.exists()
    assert _group_gone(pgid)


@pytest.mark.slow
def test_leader_exit_still_sweeps_the_group(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex may exit 0 having forked a background child — the sweep still gets it.

    This is the regression the old guard missed entirely: it returned as soon as
    ``proc.poll()`` was non-None, so a descendant of an early-exiting codex survived.
    """
    sentinel = _sentinel_env(three_seats, monkeypatch)
    three_seats.scenarios(private="leader_exits_leaving_grandchild")
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    pgid = three_seats.calls()[0]["pgid"]
    deadline = time.monotonic() + 3
    while not _group_gone(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _group_gone(pgid)
    time.sleep(9)
    assert not sentinel.exists()


@pytest.mark.slow
def test_heartbeat_carries_codex_pgid(three_seats: SeatFixture, tmp_path: Path) -> None:
    """The heartbeat publishes the runner pid AND codex's pgid (the last-resort kill)."""
    heartbeat = tmp_path / "hb.json"
    seen: list[dict] = []
    fake = str(Path(__file__).parent / "fakes" / "fake_codex.py")
    env = {**os.environ, "CODEX_HOME": str(three_seats.seats["private"])}
    three_seats.scenarios(private="hang")

    def watch() -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if heartbeat.exists():
                try:
                    seen.append(json.loads(heartbeat.read_text(encoding="utf-8")))
                    return
                except (OSError, ValueError):
                    pass
            time.sleep(0.1)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    with pytest.raises(subprocess.TimeoutExpired):
        cic._exec_codex(  # noqa: SLF001
            [fake, "exec"],
            env=env,
            timeout=3,
            heartbeat_path=heartbeat,
            heartbeat_meta={"model": "m"},
            stdin_text="",
        )
    watcher.join(timeout=2)
    assert seen, "no heartbeat was written"
    assert seen[0]["runner_pid"] == os.getpid()
    assert isinstance(seen[0]["codex_pgid"], int) and seen[0]["codex_pgid"] > 0
    assert not heartbeat.exists()  # removed on exit


@pytest.mark.slow
def test_runner_sigterm_relay_kills_codex_group(tmp_path: Path) -> None:
    """SIGTERM to the RUNNER relays into codex's group — what a consumer's killpg does."""
    fixture = make_three_seats(tmp_path)
    sentinel = fixture.home / "grandchild.touched"
    fixture.scenarios(private="hang_with_grandchild")
    script = (
        "import sys;"
        f"sys.argv = ['run', '-C', {str(fixture.workdir)!r}, '-t', '0', '-j', 'hi'];"
        "from command_center.codex_in_claude import main;"
        "raise SystemExit(main(sys.argv))"
    )
    env = fixture.env(FAKE_CODEX_SENTINEL=str(sentinel), PATH=_fake_codex_path(tmp_path))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    runner = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while not fixture.calls() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert fixture.calls(), "the runner never launched the fake codex"
    pgid = fixture.calls()[0]["pgid"]
    runner.send_signal(signal.SIGTERM)
    runner.wait(timeout=15)
    time.sleep(9)
    assert not sentinel.exists()
    assert _group_gone(pgid)


# ── end to end, through the real executable ───────────────────────────────────────
@pytest.mark.slow
def test_e2e_run_cli_with_real_executable(tmp_path: Path) -> None:
    """The real CLI, a real subprocess, three real seats — only `codex` is fake."""
    fixture = make_three_seats(tmp_path)
    fixture.scenarios(
        default="refuse_quota",
        private="refuse_quota",
        de={"scenario": "ok", "reply": "de answered e2e"},
    )
    env = fixture.env(PATH=_fake_codex_path(tmp_path))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "command_center.codex_in_claude",
            "run",
            "-j",
            "-C",
            str(fixture.workdir),
            "-t",
            "120",
            "hello",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    envelope = json.loads(proc.stdout.strip().splitlines()[-1])
    assert envelope["ok"] is True
    assert envelope["seat"]["label"] == "de"
    assert envelope["reply"] == "de answered e2e"
    assert [a["outcome"] for a in envelope["attempts"]] == ["refused:quota", "ok"]
    assert fixture.call_homes() == ["private", "de"]  # `default` is ranked last
    cooldowns = json.loads(
        (fixture.ccc_home / "command-center" / "cooldowns.json").read_text(encoding="utf-8")
    )
    assert "codex:private" in cooldowns["providers"]
