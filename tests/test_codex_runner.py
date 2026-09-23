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
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
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
        "ephemeral": False,
        "headroom": False,
        "min_remaining": None,
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
        "headroom": False,
        "min_remaining": None,
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
    # The ledger is wiped first: phase 1 recorded an attempt on `private`, which under
    # the fill policy's round-robin would move this phase's first try to another seat.
    three_seats.reset_log()
    three_seats.forget_attempts()
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
    three_seats.measure()  # a write run refuses an UNMEASURED seat (plan D5)
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
    three_seats.measure()  # a write run refuses an UNMEASURED seat (plan D5)
    three_seats.scenarios(de="refuse_quota", default={"scenario": "ok", "reply": "team"})
    # `default` has no hardened-rw profile either, so the write run is refused there —
    # what matters is that the runner GOT there, i.e. it hopped off the clean refusal.
    assert cic.cmd_delegate(_delegate_ns(three_seats, write=True)) == cic.EX_USAGE
    assert three_seats.call_homes() == ["de"]
    assert "codex:de" in quota.read_cooldowns()


def test_write_refused_on_seat_without_rw_profile(three_seats: SeatFixture) -> None:
    """No [permissions.hardened-rw] on the leading seat ⇒ exit 2, codex never runs."""
    subprocess.run(["git", "init", "-q"], cwd=three_seats.workdir, check=True)
    three_seats.measure()  # a write run refuses an UNMEASURED seat before the profile check
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
        assert argv[:2] == ["exec", "--json"]  # no --ephemeral: codex_usage is off
        assert argv[argv.index("-C") + 1] == str(three_seats.workdir)
        assert argv[argv.index("-m") + 1] == _MODEL
        assert "model_reasoning_effort=low" in argv
        assert "--skip-git-repo-check" in argv  # the fixture workdir is not a repo


def test_run_accepts_a_short_model_name(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run -m sol` (what `/codex-debate sol` sends) launches the SLUG and reports it."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "hi"})
    assert cic.cmd_run(_run_ns(three_seats, model="SOL")) == cic.EX_OK
    envelope = _envelope(capsys)
    assert envelope["model"] == _MODEL
    (call,) = three_seats.calls()
    assert call["argv"][call["argv"].index("-m") + 1] == _MODEL
    # an unknown name is refused before any seat is tried
    assert cic.cmd_run(_run_ns(three_seats, model="astra")) == cic.EX_INVALID_MODEL
    assert len(three_seats.calls()) == 1
    assert "Unknown model 'astra'" in capsys.readouterr().err


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


def test_argv_ephemeral_follows_the_usage_opt_in(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` keeps its session file while `codex_usage` is OFF — that IS the measurement.

    The `--json` stream carries no `rate_limits`, so an ephemeral run leaves nothing
    that says what it cost the seat it billed (plan D6, debate O2). It stays
    UNJOURNALLED either way: only `-P` journals.
    """
    three_seats.scenarios(private={"scenario": "ok", "reply": "hi"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert "--ephemeral" not in three_seats.calls()[0]["argv"]
    assert codex_journal(three_seats.seats["private"]) == []

    # -E forces the old behaviour back
    three_seats.reset_log()
    assert cic.cmd_run(_run_ns(three_seats, json=False, ephemeral=True)) == cic.EX_OK
    assert "--ephemeral" in three_seats.calls()[0]["argv"]

    # and so does the opt-in, because then the runner measures the seat live instead
    three_seats.reset_log()
    monkeypatch.setattr(cic, "ephemeral_default", lambda: True)
    monkeypatch.setattr(cic, "_post_attempt_refresh", lambda _cand, _budget: None)
    monkeypatch.setattr(cic, "_pre_selection_refresh", lambda _cands, _budget: None)
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert "--ephemeral" in three_seats.calls()[0]["argv"]


def test_delegate_keeps_and_journals_its_session(three_seats: SeatFixture) -> None:
    """`delegate` never goes ephemeral: its session is journalled so `--resume` works."""
    three_seats.scenarios(private={"scenario": "ok", "reply": "hi"})
    assert cic.cmd_delegate(_delegate_ns(three_seats)) == cic.EX_OK
    assert "--ephemeral" not in three_seats.calls()[0]["argv"]
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
        "attempt_ids",  # additive (tp#392): the ledger lines this call wrote
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
    assert set(envelope["attempts"][0]) == {"seat", "home", "elapsed_s", "outcome", "attempt_id"}


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
    final = json.loads(heartbeat.read_text(encoding="utf-8"))  # retained final record
    assert final["ended"] and final["child_state"] == "gone"


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


# ── progress watchdog + sleep guard (2026-09-08) ──────────────────────────────────
#
# The seven-hour hang: this laptop idle-slept one minute after an unattended debate
# round launched (`pmset sleep 1`, no assertion held), and the woken codex 0.152.1
# looped `{"type":"error","message":"Reconnecting... waiting for network …"}` on stdout
# plus `ERROR codex_models_manager` on stderr FOREVER. The old watchdog counted every
# line as activity, so nothing fired. What is guarded below: only PROGRESS resets the
# idle clock, the allowance shrinks the moment codex names the network or the machine
# is caught suspended, a run that never gets a model response is killed at startup,
# and a caffeinate assertion is held for exactly the run.

# A recording `caffeinate` that never exits by itself: it logs its argv + its OWN pid
# (`exec` keeps it) so a test can prove the runner released the assertion it took.
# `$FAKE_CAFFEINATE_HOLD=0` makes it return at once — used only to warm it up.
_FAKE_CAFFEINATE = r"""#!/bin/sh
args=""
sep=""
for arg in "$@"; do
  args="$args$sep\"$arg\""
  sep=", "
done
printf '{"argv": [%s], "pid": %s}\n' "$args" "$$" >> "$FAKE_CAFFEINATE_LOG"
exec sleep "${FAKE_CAFFEINATE_HOLD:-60}"
"""


def _fake_caffeinate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the recording ``caffeinate`` first on ``$PATH``; return its JSON-lines log.

    The warm-up run is not cosmetic: macOS charges ~350 ms of syspolicy checking to the
    FIRST ``execve`` of a newly written executable, and a happy-path round is over in
    ~200 ms — so without it the runner's release SIGTERM reaches the fake before it has
    executed a single line and the call is never recorded. A warmed script spawns in
    ~8 ms, like the real ``caffeinate`` would.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "caffeinate"
    script.write_text(_FAKE_CAFFEINATE, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "caffeinate-calls.jsonl"
    subprocess.run(  # warm-up: log elsewhere, `sleep 0`, exit immediately
        [str(script), "-warmup"],
        env={
            **os.environ,
            "FAKE_CAFFEINATE_LOG": str(tmp_path / "caffeinate-warmup.jsonl"),
            "FAKE_CAFFEINATE_HOLD": "0",
        },
        timeout=30,
        check=True,
    )
    monkeypatch.setenv("FAKE_CAFFEINATE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return log


def _caffeinate_calls(log: Path) -> list[dict]:
    """Every fake-caffeinate invocation so far, in order."""
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _eventually(probe: Callable[[], bool], seconds: float) -> bool:
    """Poll *probe* for up to *seconds* — a kill is a signal, not an instant."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(0.05)
    return probe()


@pytest.mark.slow
def test_network_trouble_lines_are_not_progress(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A LOUD dead-network loop is a stall: those lines never reset the idle clock."""
    three_seats.scenarios(private="network_dead")
    started = time.monotonic()
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=3)) == cic.EX_NETWORK
    assert time.monotonic() - started < 15
    error = _envelope(capsys)["error"]
    assert error["kind"] == "network"
    assert "network" in error["message"]
    assert _eventually(lambda: _group_gone(three_seats.calls()[0]["pgid"]), 3)


@pytest.mark.slow
def test_network_idle_allowance_shrinks(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once codex names the network the generous plain allowance no longer applies."""
    monkeypatch.setenv("CODEX_IN_CLAUDE_NET_IDLE", "2")
    three_seats.scenarios(private="network_dead")
    started = time.monotonic()
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=60)) == cic.EX_NETWORK
    assert time.monotonic() - started < 15  # the 60s allowance was NOT the one in force
    assert _envelope(capsys)["error"]["kind"] == "network"


@pytest.mark.slow
def test_startup_guard_kills_a_run_without_model_response(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`thread.started` + `turn.started` are emitted LOCALLY and prove nothing."""
    monkeypatch.setenv("CODEX_IN_CLAUDE_STARTUP_TIMEOUT", "2")
    three_seats.scenarios(private="silent_after_start")
    started = time.monotonic()
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=60)) == cic.EX_TIMEOUT
    assert time.monotonic() - started < 15
    error = _envelope(capsys)["error"]
    assert error["kind"] == "startup_timeout"
    assert "model response" in error["message"]


@pytest.mark.slow
def test_trouble_followed_by_progress_is_not_killed(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reconnect FLAP that recovers must not be killed — trouble is not a verdict."""
    monkeypatch.setenv("CODEX_IN_CLAUDE_NET_IDLE", "1")
    three_seats.scenarios(private={"scenario": "trouble_then_ok", "reply": "recovered"})
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=5)) == cic.EX_OK
    assert _envelope(capsys)["reply"] == "recovered"


@pytest.mark.slow
def test_suspended_machine_is_detected_and_reported(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 1s tick that took an hour = the machine slept; the report SAYS so."""
    calls = 0

    def slept_clock() -> float:
        """Wall clock that jumps an hour between two supervision ticks."""
        nonlocal calls
        calls += 1
        return time.time() + (3600.0 if calls > 3 else 0.0)

    monkeypatch.setattr(cic, "_wall_clock", slept_clock)
    monkeypatch.setenv("CODEX_IN_CLAUDE_POST_SLEEP_IDLE", "2")
    three_seats.scenarios(private="hang")
    started = time.monotonic()
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=60)) == cic.EX_NETWORK
    assert time.monotonic() - started < 15
    error = _envelope(capsys)["error"]
    assert error["kind"] == "slept"
    assert "suspended" in error["message"]


@pytest.mark.slow
def test_caffeinate_is_held_for_the_run_and_released(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`caffeinate -i -w <codex pid>` for exactly the run — on the ok AND killed paths."""
    monkeypatch.delenv("CODEX_IN_CLAUDE_NO_CAFFEINATE", raising=False)
    log = _fake_caffeinate(tmp_path, monkeypatch)
    three_seats.scenarios(private={"scenario": "ok", "reply": "awake"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert _eventually(lambda: len(_caffeinate_calls(log)) == 1, 3)
    held = _caffeinate_calls(log)[0]
    assert held["argv"] == ["-i", "-w", str(three_seats.calls()[0]["pgid"])]
    assert _eventually(lambda: _pid_gone(int(held["pid"])), 3)

    # Every seat, because the fill policy's round-robin moves the leading seat after
    # phase 1's recorded attempt — and a network kill is terminal, so only one runs.
    three_seats.scenarios(private="network_dead", de="network_dead", default="network_dead")
    assert cic.cmd_run(_run_ns(three_seats, timeout=0, idle_timeout=3)) == cic.EX_NETWORK
    assert _eventually(lambda: len(_caffeinate_calls(log)) == 2, 3)
    killed = _caffeinate_calls(log)[1]
    assert killed["argv"] == ["-i", "-w", str(three_seats.calls()[1]["pgid"])]
    assert _eventually(lambda: _pid_gone(int(killed["pid"])), 3)


@pytest.mark.slow
def test_no_caffeinate_when_opted_out(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`$CODEX_IN_CLAUDE_NO_CAFFEINATE=1` takes no assertion at all."""
    log = _fake_caffeinate(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEX_IN_CLAUDE_NO_CAFFEINATE", "1")
    three_seats.scenarios(private={"scenario": "ok", "reply": "no assertion"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert _caffeinate_calls(log) == []


@pytest.mark.parametrize(
    ("line", "stderr", "expected"),
    [
        pytest.param(
            '{"type":"error","message":"Reconnecting... waiting for network '
            '(Connection failed: error sending request)"}',
            False,
            (False, False, True),
            id="stdout-error-event-is-network-trouble",
        ),
        pytest.param(
            '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"hi"}}',
            False,
            (True, True, False),
            id="stdout-item-is-progress-and-work",
        ),
        pytest.param(
            '{"type":"thread.started","thread_id":"x"}',
            False,
            (True, False, False),
            id="stdout-thread-start-is-progress-but-not-work",
        ),
        pytest.param(
            '{"type":"item.completed","item":{"id":"i","type":"error","message":"Falling back '
            'from WebSockets to HTTPS transport. stream disconnected before completion"}}',
            False,
            (False, False, True),
            id="stdout-error-ITEM-is-network-trouble",
        ),
        pytest.param(
            "2026-09-08T06:50:08.463644Z ERROR codex_models_manager::manager: failed to "
            "refresh available models: Connection failed: error sending request",
            True,
            (False, False, True),
            id="stderr-ERROR-log-is-network-trouble",
        ),
        pytest.param(
            "2026-09-08T06:50:08.463644Z WARN something unrelated",
            True,
            (False, False, False),
            id="stderr-WARN-log-is-trouble-but-not-network",
        ),
        pytest.param(
            "Reading additional input from stdin...",
            True,
            (True, False, False),
            id="stderr-plain-line-is-progress",
        ),
        pytest.param(
            "not json at all",
            False,
            (True, False, False),
            id="stdout-unknown-output-is-never-a-reason-to-kill",
        ),
        pytest.param("", False, (False, False, False), id="blank-stdout-is-nothing"),
        pytest.param("   \n", True, (False, False, False), id="blank-stderr-is-nothing"),
    ],
)
def test_classify_output_line(line: str, stderr: bool, expected: tuple[bool, bool, bool]) -> None:
    """The watchdog's ONLY input: progress / work / network for one output line."""
    verdict = cic.classify_output_line(line, stderr=stderr)
    assert (verdict.progress, verdict.work, verdict.network) == expected


@pytest.mark.slow
def test_heartbeat_carries_health_fields(three_seats: SeatFixture, tmp_path: Path) -> None:
    """The heartbeat publishes the health the runner decides on: idle/slept/trouble."""
    heartbeat = tmp_path / "hb-health.json"
    seen: list[dict] = []
    fake = str(Path(__file__).parent / "fakes" / "fake_codex.py")
    env = {**os.environ, "CODEX_HOME": str(three_seats.seats["private"])}
    three_seats.scenarios(private="network_dead")
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            try:
                seen.append(json.loads(heartbeat.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
            done.wait(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        with pytest.raises(cic.CodexStalledError) as excinfo:
            cic._exec_codex(  # noqa: SLF001
                [fake, "exec"],
                env=env,
                timeout=0,
                idle_timeout=7,  # >= 6 ticks, so the tick-5 heartbeat is reached
                heartbeat_path=heartbeat,
                heartbeat_meta={"model": "m"},
                stdin_text="",
            )
    finally:
        done.set()
        watcher.join(timeout=3)
    assert excinfo.value.reason == "network"
    troubled = [snap for snap in seen if int(snap.get("trouble") or 0) >= 1]
    assert troubled, "no heartbeat reported the trouble lines codex was printing"
    assert "caffeinate_pid" in troubled[0]  # None without caffeinate / with the opt-out
    assert troubled[0]["slept_s"] == 0
    final = json.loads(heartbeat.read_text(encoding="utf-8"))  # retained final record
    assert final["ended"] and final["child_state"] == "gone"


def test_exit_codes_for_new_kinds(capsys: pytest.CaptureFixture[str]) -> None:
    """A dead network / a slept machine is EX_NETWORK; no model response is EX_TIMEOUT."""
    for kind, code in (
        ("network", cic.EX_NETWORK),
        ("slept", cic.EX_NETWORK),
        ("startup_timeout", cic.EX_TIMEOUT),
    ):
        assert cic._run_exit_code(cic.RunResult(error_kind=kind)) == code  # noqa: SLF001
        result = cic.RunResult(error_kind=kind, error_message=f"{kind} killed the round")
        assert cic._run_error_exit(result) == code  # noqa: SLF001
        assert f"ERROR: {kind} killed the round" in capsys.readouterr().err


def test_runs_view_shows_health(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`runs` shows WHY a live round is quiet: caffeinated, slept, trouble lines."""
    runs = three_seats.home / "runs"  # RUNS_DIR is resolved at import, before the fixture
    runs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cic, "RUNS_DIR", runs)
    (runs / "live.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "model": "m",
                "effort": "low",
                "repo": "/r",
                "elapsed_s": 125,
                "idle_s": 7,
                "lines": 3,
                "last_line": "x",
                "caffeinate_pid": 4242,
                "slept_s": 125,
                "trouble": 3,
            }
        ),
        encoding="utf-8",
    )
    assert cic.cmd_runs(argparse.Namespace(json=False)) == cic.EX_OK
    out = capsys.readouterr().out
    assert "caffeinated" in out
    assert "slept 2m05s" in out
    assert "3 trouble line(s) since progress" in out


# ── heartbeat contract v1 (tp#221: the statusline reader joins on these keys) ─────
_CONTRACT_KEYS: dict[str, type | tuple[type, ...]] = {
    "schema_version": int,
    "writer": str,
    "tool": str,
    "pid": int,
    "proc_start": (str, type(None)),
    "child_pid": int,
    "child_proc_start": (str, type(None)),
    "child_state": str,
    "account": (str, type(None)),
    "model_source": str,
    "interval_s": int,
    "started": int,
    "updated": int,
    "next_due": int,
    "progress_at": int,
    "idle_s": int,
    "lines": int,
    "last_line": str,
}


def _collect_heartbeats(
    three_seats: SeatFixture, heartbeat: Path, scenario: str, **exec_kwargs: object
) -> tuple[list[dict], BaseException | None]:
    """Run the fake under supervision, sampling every heartbeat write; (snapshots, raised)."""
    seen: list[dict] = []
    fake = str(Path(__file__).parent / "fakes" / "fake_codex.py")
    env = {**os.environ, "CODEX_HOME": str(three_seats.seats["private"])}
    three_seats.scenarios(private=scenario)
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            try:
                snap = json.loads(heartbeat.read_text(encoding="utf-8"))
                if not seen or snap != seen[-1]:
                    seen.append(snap)
            except (OSError, ValueError):
                pass
            done.wait(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    raised: BaseException | None = None
    try:
        cic._exec_codex(  # noqa: SLF001
            [fake, "exec"],
            env=env,
            heartbeat_path=heartbeat,
            heartbeat_meta={"model": "m", "effort": "low", "repo": "/r", "seat": "private"},
            stdin_text="",
            **exec_kwargs,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001  # the scenario decides how the run ends
        raised = exc
    finally:
        done.set()
        watcher.join(timeout=3)
    return seen, raised


def test_heartbeat_carries_contract_v1_keys(three_seats: SeatFixture, tmp_path: Path) -> None:
    """Every write carries the typed cross-tool keys; the final record stays, with ``ended``."""
    heartbeat = tmp_path / "hb-contract.json"
    seen, raised = _collect_heartbeats(three_seats, heartbeat, "hang", timeout=3)
    assert isinstance(raised, subprocess.TimeoutExpired)
    assert seen, "no heartbeat was written"
    live = seen[0]
    for key, kind in _CONTRACT_KEYS.items():
        assert key in live, f"missing {key}"
        assert isinstance(live[key], kind), f"{key}: {live[key]!r} is not {kind}"
    assert live["schema_version"] == cic.HEARTBEAT_SCHEMA_VERSION == 1
    assert live["writer"].startswith("codex-in-claude/")
    assert live["tool"] == "codex"
    assert live["interval_s"] == cic.HEARTBEAT_INTERVAL_S == 5
    assert live["next_due"] == live["updated"] + 5
    assert live["account"] == "private"
    assert live["model_source"] == "requested"
    assert live["child_pid"] == live["codex_pgid"]
    assert live["child_state"] == "running"
    if live["proc_start"] is not None:
        assert re.match(r"^\w{3} \w{3} [ \d]\d \d\d:\d\d:\d\d \d{4}$", live["proc_start"])
    final = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert final["ended"] >= final["started"]
    assert final["child_state"] == "gone"
    assert final["pid"] == os.getpid()


def test_heartbeat_file_modes(three_seats: SeatFixture, tmp_path: Path) -> None:
    """0600 files in a 0700 directory, whatever the umask: the record names repo + seat + output."""
    runs = tmp_path / "runs"
    heartbeat = runs / "hb-modes.json"
    previous = os.umask(0o022)
    try:
        seen, _ = _collect_heartbeats(three_seats, heartbeat, "hang", timeout=2)
    finally:
        os.umask(previous)
    assert seen
    assert heartbeat.stat().st_mode & 0o777 == 0o600
    assert runs.stat().st_mode & 0o777 == 0o700


def test_heartbeat_progress_at_ignores_trouble_and_sleep(
    three_seats: SeatFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``progress_at`` is the wall clock of the last PROGRESS line — reconnect chatter and a
    suspended machine never move it (Codex O14)."""
    heartbeat = tmp_path / "hb-progress.json"
    real_wall = cic._wall_clock  # noqa: SLF001
    calls = {"n": 0}

    def jumping_wall() -> float:
        calls["n"] += 1
        # One simulated 10-minute suspend AFTER the tick-5 heartbeat (call 1 seeds
        # prev_wall, then one call per tick): the awake-idle credit is capped per tick
        # (TICK_CAP_S), so the run is killed a few ticks after the jump and the final
        # record — not a later periodic write — is what carries ``slept_s``.
        return real_wall() + (600.0 if calls["n"] > 7 else 0.0)

    monkeypatch.setattr(cic, "_wall_clock", jumping_wall)
    seen, raised = _collect_heartbeats(
        three_seats, heartbeat, "network_dead", timeout=0, idle_timeout=12
    )
    assert isinstance(raised, cic.CodexStalledError)
    assert getattr(raised, "reason", None) in ("network", "slept")
    final = json.loads(heartbeat.read_text(encoding="utf-8"))
    troubled = [snap for snap in [*seen, final] if int(snap.get("trouble") or 0) >= 1]
    assert troubled, "no heartbeat reported the trouble lines"
    stamps = {snap["progress_at"] for snap in troubled}
    assert len(stamps) == 1, f"progress_at moved on trouble-only output: {sorted(stamps)}"
    first_progress = troubled[0]["progress_at"]
    assert troubled[0]["started"] <= first_progress <= troubled[0]["updated"]
    assert int(final.get("slept_s") or 0) >= 600, "the sleep jump was not recorded"
    assert final["progress_at"] == first_progress
    assert final["ended"] and final["child_state"] == "gone"


def test_final_heartbeat_folds_in_progress_that_arrived_at_exit(
    three_seats: SeatFixture, tmp_path: Path
) -> None:
    """The retained record dates the last REAL progress line, not the last tick.

    Output produced while ``proc.wait`` is returning only reaches the buffers once the
    reader threads join, so a run whose whole answer arrives in one burst at exit would
    otherwise be filed with a ``progress_at`` from before its silent stretch — and the
    statusline would report a healthy finish as minutes idle.
    """
    heartbeat = tmp_path / "hb-late.json"
    seen, raised = _collect_heartbeats(
        three_seats, heartbeat, "late_progress", timeout=0, idle_timeout=30
    )
    assert raised is None, raised
    final = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert final["ended"] and final["child_state"] == "gone"
    silent = [snap for snap in seen if not snap.get("ended")]
    assert silent, "no periodic heartbeat was written during the silent stretch"
    assert final["progress_at"] >= final["started"] + 5, (
        "the final record predates the burst that arrived at exit: "
        f"progress_at={final['progress_at']} started={final['started']}"
    )
    assert final["progress_at"] >= max(snap["progress_at"] for snap in silent)


def test_runs_skips_ended_and_prunes_old_ended(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A retained final record is neither listed nor deleted on sight; a stale one is pruned."""
    runs = three_seats.home / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cic, "RUNS_DIR", runs)
    fresh = runs / "ended-fresh.json"
    stale = runs / "ended-stale.json"
    base = {
        "pid": os.getpid(),
        "model": "m",
        "effort": "low",
        "repo": "/r",
        "last_line": "ENDEDLINE",
    }
    fresh.write_text(json.dumps({**base, "ended": int(time.time()) - 5}), encoding="utf-8")
    stale.write_text(json.dumps({**base, "ended": int(time.time()) - 5000}), encoding="utf-8")
    assert cic.cmd_runs(argparse.Namespace(json=False)) == cic.EX_OK
    out = capsys.readouterr().out
    assert "ENDEDLINE" not in out
    assert fresh.exists(), "a fresh final record must survive a listing"
    assert not stale.exists(), "a final record past the retention window must be pruned"


# ── a superseded refusal buys exactly one attempt ─────────────────────────────────
def test_one_newer_measurement_buys_at_most_one_attempt(three_seats: SeatFixture) -> None:
    """A stale refusal yields to a newer healthy reading — once, not for free forever.

    The seat is tried again because its own measurement disproved the recorded refusal
    (2026-09-14); when it refuses AGAIN, that refusal is recorded with an ``observed_at``
    newer than the measurement, so the next ranking excludes it once more. Without the
    second half, a seat with a stale live cache would be re-attempted on every run.
    """
    now = int(time.time())
    three_seats.measure()  # every seat healthy: no probe promotion, configured order rules
    quota.record_block(
        "codex:private",
        blocked_until=now + 18 * 3600,
        observed_at=now - 86400,
        reason="codex exec refused: quota — usage limit reached",
        scope="quota",
        source="codex-exec",
    )
    assert [cand.label for cand in cic.codex_homes_in_order()] == ["private", "de", "default"]

    three_seats.scenarios(private="refuse_quota", de={"scenario": "ok", "reply": "de"})
    assert cic.cmd_run(_run_ns(three_seats, json=False)) == cic.EX_OK
    assert three_seats.call_homes() == ["private", "de"]  # the superseded seat WAS tried

    entry = quota.read_cooldowns()["codex:private"]
    assert entry["observed_at"] >= now  # the fresh refusal outdates the measurement
    assert "private" not in [cand.label for cand in cic.codex_homes_in_order()]


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
