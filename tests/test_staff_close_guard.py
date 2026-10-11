"""The staff close guard in the ``release-locks`` Stop hook (staff_close_guard.py).

A released-lease marker written by ``staff lease -r`` makes every Stop of that session
print ``{"decision": "block", …}`` until a close is armed (``mark-done --close``), the
session is done, or the lease is provably re-held. ``staff`` is a stand-in script here
(``STAFF_BIN``) that records every call, so "no subprocess" is asserted, not assumed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from command_center import cli, config, hooks, staff_close_guard
from command_center.models import now_ms
from command_center.store import Store

ROOT = Path(__file__).resolve().parents[1]
SID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
AGENT = "mac-overseer"
NONCE = "8d3f0a1b2c4e4f6a9b7c5d3e1f0a2b4c"

_FAKE_STAFF = """#!{python}
import json, os, sys, time
with open(os.environ["FAKE_STAFF_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
time.sleep(float(os.environ.get("FAKE_STAFF_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_STAFF_OUT", ""))
sys.exit(int(os.environ.get("FAKE_STAFF_EXIT", "0")))
"""


def _lease(held_by_owner: bool, state: str = "free") -> str:
    return json.dumps(
        {
            "name": f"live:{AGENT}",
            "owner": SID if held_by_owner else None,
            "generation": 3,
            "expires_at": None,
            "state": state,
            "held_by_owner": held_by_owner,
            "release_marker": None if held_by_owner else {"nonce": NONCE},
        }
    )


@pytest.fixture(name="staff")
def _staff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``staff`` on ``STAFF_BIN``; returns the file its calls are appended to."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_SESSION_AIM", raising=False)
    exe = tmp_path / "bin" / "staff"
    exe.parent.mkdir()
    exe.write_text(_FAKE_STAFF.format(python=sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    calls = tmp_path / "staff-calls.jsonl"
    monkeypatch.setenv("STAFF_BIN", str(exe))
    monkeypatch.setenv("FAKE_STAFF_CALLS", str(calls))
    monkeypatch.setenv("FAKE_STAFF_OUT", _lease(held_by_owner=False))
    monkeypatch.setattr("command_center.spawn.spawn_ccc", lambda *_a, **_k: True)
    return calls


def _calls(calls: Path) -> list[list[str]]:
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]


def _write_marker(agent: str = AGENT, sid: str = SID, nonce: str = NONCE) -> Path:
    """What ``staff lease -r`` leaves behind: dir 0700, file 0600, the v1 fields."""
    path = staff_close_guard.marker_path(agent, sid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "agent": agent,
        "owner": sid,
        "generation": 3,
        "nonce": nonce,
        "released_at_iso": "2026-10-11T17:00:00Z",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _session(**fields: Any) -> None:
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        if fields:
            store.update_fields(SID, **fields)


def _stop(capsys: pytest.CaptureFixture[str]) -> dict[str, Any] | None:
    """Run the release-locks Stop hook once; the block decision it printed, or None."""
    assert hooks.handle_release_locks({"session_id": SID, "cwd": "/repo"}) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return None
    lines = out.splitlines()
    assert len(lines) == 1, out  # ONE JSON object per Stop
    decision = json.loads(lines[0])
    assert isinstance(decision, dict)
    return decision


def _blocks() -> int:
    with Store() as store:
        return store.staff_guard_blocks(SID, AGENT, NONCE)


def _events() -> list[str]:
    path = config.app_home() / "events.log"
    if not path.exists():
        return []
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if f"\t{staff_close_guard.LOG_EVENT}\t" in line
    ]


# ---------------------------------------------------------------- the decision itself
def test_marker_without_close_blocks_with_the_close_instruction(
    staff: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _session()
    _write_marker()
    decision = _stop(capsys)
    assert decision is not None and decision["decision"] == "block"
    reason = decision["reason"]
    assert "Call Skill ccc-mark-done-and-close now" in reason
    assert f"staff lease -a {AGENT} -o {SID}" in reason
    assert "block #1." in reason
    assert _calls(staff) == [["lease", "-s", "-j", "-a", AGENT, "-o", SID]]
    assert _blocks() == 1
    assert _events()[-1].endswith(f"agent={AGENT} nonce={NONCE} decision=block")


@pytest.mark.usefixtures("staff")
def test_every_stop_blocks_not_only_the_first(capsys: pytest.CaptureFixture[str]) -> None:
    _session()
    _write_marker()
    for n in (1, 2, 3):
        decision = _stop(capsys)
        assert decision is not None and f"block #{n}." in decision["reason"]
    assert _blocks() == 3


def test_armed_close_passes_and_still_fires_close_now(
    staff: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spawned: list[list[str]] = []

    def record(args: list[str], *_a: Any, **_k: Any) -> bool:
        spawned.append(args)
        return True

    monkeypatch.setattr("command_center.spawn.spawn_ccc", record)
    _session()
    marker = _write_marker()
    with Store() as store:
        store.arm_close(SID, now_ms())  # `ccc mark-done --close` armed it
    assert _stop(capsys) is None
    assert spawned and spawned[0][:3] == ["close-now", "--session", SID]
    assert _calls(staff) == []  # a CloseClaim needs no probe
    assert marker.exists()  # llm-staff owns the marker; ccc only drops a provably stale one
    assert _events()[-1].endswith("decision=pass-close")


def test_done_session_passes_without_a_probe(
    staff: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _session(done=True)
    _write_marker()
    assert _stop(capsys) is None
    assert _calls(staff) == []
    assert _events()[-1].endswith("decision=pass-done")


@pytest.mark.usefixtures("staff")
def test_close_then_undo_blocks_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    monkeypatch.setattr(cli, "_spawn_sync_mirrors", lambda _cfg: None)
    _session()
    _write_marker()
    assert cli.main(["mark-done", "--session", SID, "--close", "-q"]) == 0
    assert cli.main(["mark-done", "--session", SID, "--undo", "-q"]) == 0
    capsys.readouterr()
    decision = _stop(capsys)
    assert decision is not None and decision["decision"] == "block"


def test_reheld_lease_passes_and_deletes_the_stale_marker(
    staff: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAKE_STAFF_OUT", _lease(held_by_owner=True, state="held"))
    _session()
    marker = _write_marker()
    with Store() as store:  # an earlier block on this nonce is forgotten by the pass
        store.staff_guard_block(SID, AGENT, NONCE, limit=5, emit=lambda _n: None)
    assert _stop(capsys) is None
    assert not marker.exists()
    assert _blocks() == 0
    assert len(_calls(staff)) == 1
    assert _events()[-1].endswith("decision=pass-reheld")


@pytest.mark.usefixtures("staff")
def test_reheld_probe_keeps_a_marker_with_a_newer_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stale-marker delete re-reads the nonce: a fresh release meanwhile is kept."""
    monkeypatch.setenv("FAKE_STAFF_OUT", _lease(held_by_owner=True, state="held"))
    marker = _write_marker()
    real = staff_close_guard.lease_reheld

    def reheld_then_released(agent: str, sid: str) -> bool:
        answer = real(agent, sid)
        _write_marker(nonce="f" * 32)  # a new `staff lease -r` right after the probe
        return answer

    monkeypatch.setattr(staff_close_guard, "lease_reheld", reheld_then_released)
    with Store() as store:
        assert staff_close_guard.run(store, SID, None, lambda *_a: None) == "pass-reheld"
    assert marker.exists()


@pytest.mark.usefixtures("staff")
@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"FAKE_STAFF_SLEEP": "5"}, "timeout"),
        ({"FAKE_STAFF_EXIT": "1"}, "non-zero exit"),
        ({"FAKE_STAFF_OUT": "not json"}, "garbage"),
        ({"FAKE_STAFF_OUT": "[]"}, "not an object"),
    ],
)
def test_probe_failure_blocks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    why: str,
) -> None:
    """A marker exists and re-hold is not proven → block (fail toward the guard)."""
    monkeypatch.setattr(staff_close_guard, "PROBE_TIMEOUT_S", 0.5)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _session()
    _write_marker()
    decision = _stop(capsys)
    assert decision is not None and decision["decision"] == "block", why


def test_missing_staff_binary_blocks(
    staff: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STAFF_BIN", str(tmp_path / "gone"))
    _session()
    _write_marker()
    decision = _stop(capsys)
    assert decision is not None and decision["decision"] == "block"
    assert _calls(staff) == []


@pytest.mark.usefixtures("staff")
def test_probe_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setattr(staff_close_guard, "PROBE_TIMEOUT_S", 0.5)
    monkeypatch.setenv("FAKE_STAFF_SLEEP", "30")
    t0 = time.monotonic()
    assert staff_close_guard.lease_reheld(AGENT, SID) is False
    assert time.monotonic() - t0 < 10


# ---------------------------------------------------------------- the escalation counter
@pytest.mark.usefixtures("staff")
def test_five_blocks_then_one_pass_with_an_escalated_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _session()
    _write_marker()
    for n in range(1, 6):
        decision = _stop(capsys)
        assert decision is not None and f"block #{n}." in decision["reason"]
    assert _stop(capsys) is None  # the sixth Stop goes through once …
    events = _events()
    assert events[-1].endswith(f"agent={AGENT} nonce={NONCE} decision=escalated")
    assert events[-1].split("\t")[1] == SID
    assert _blocks() == 0
    decision = _stop(capsys)  # … and the next one is refused again
    assert decision is not None and "block #1." in decision["reason"]


@pytest.mark.usefixtures("staff")
def test_escalated_event_carries_only_ids_and_the_decision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _session()
    _write_marker()
    for _ in range(6):
        _stop(capsys)
    _stamp, sid, event, detail = _events()[-1].split("\t")
    assert (sid, event) == (SID, "staff-close-guard")
    assert detail == f"agent={AGENT} nonce={NONCE} decision=escalated"


@pytest.mark.usefixtures("staff")
def test_a_new_marker_nonce_starts_its_own_count(capsys: pytest.CaptureFixture[str]) -> None:
    _session()
    _write_marker()
    for _ in range(3):
        _stop(capsys)
    _write_marker(nonce="a" * 32)
    decision = _stop(capsys)
    assert decision is not None and "block #1." in decision["reason"]


@pytest.mark.usefixtures("staff")
def test_concurrent_stops_keep_the_counter_consistent(capsys: pytest.CaptureFixture[str]) -> None:
    """Six parallel Stop invocations on one marker: exactly five blocks, one escalation."""
    _session()
    _write_marker()
    barrier = threading.Barrier(6)
    verdicts: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def one_stop() -> None:
        try:
            with Store() as store:  # one connection per "process"
                barrier.wait(timeout=10)
                verdict = staff_close_guard.run(store, SID, None, lambda *_a: None)
            with lock:
                verdicts.append(verdict)
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=one_stop) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert sorted(verdicts) == ["block"] * 5 + ["escalated"]
    blocks = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    numbers = sorted(int(b["reason"].split("block #")[1].split(".")[0]) for b in blocks)
    assert numbers == [1, 2, 3, 4, 5]  # every count handed out exactly once
    assert _blocks() == 0


@pytest.mark.usefixtures("staff")
def test_two_concurrent_hook_processes_count_twice(tmp_path: Path) -> None:
    """Two real `ccc hook release-locks` processes on one store: both block, count 2."""
    _session()
    _write_marker()
    env = _hook_env()
    payload = json.dumps({"session_id": SID, "cwd": "/repo"})
    procs = [
        subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-m", "command_center", "hook", "release-locks"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [proc.communicate(payload, timeout=60) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0]
    reasons = sorted(json.loads(out)["reason"] for out, _err in outs)
    assert "block #1." in reasons[0] and "block #2." in reasons[1]
    assert _blocks() == 2


def _hook_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CCC_INTERNAL", None)
    return env


@pytest.mark.usefixtures("staff")
def test_hook_process_stdout_contract(tmp_path: Path) -> None:
    """`ccc hook release-locks` exits 0 with ONE {"decision":"block","reason":…} line."""
    _session()
    _write_marker()
    proc = subprocess.run(
        [sys.executable, "-m", "command_center", "hook", "release-locks"],
        input=json.dumps({"session_id": SID, "cwd": "/repo"}),
        capture_output=True,
        text=True,
        env=_hook_env(),
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1
    decision = json.loads(lines[0])
    assert set(decision) == {"decision", "reason"} and decision["decision"] == "block"


_KILL_BETWEEN_PRINT_AND_COMMIT = """
import json, os, signal, sys
from pathlib import Path
from command_center.store import Store

store = Store(Path(sys.argv[1]))

def emit(n):
    sys.stdout.write(json.dumps({"decision": "block", "reason": f"block {n}/5"}) + "\\n")
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)

store.staff_guard_block(sys.argv[2], sys.argv[3], sys.argv[4], limit=5, emit=emit)
"""


@pytest.mark.usefixtures("staff")
def test_kill_between_print_and_commit_under_counts(tmp_path: Path) -> None:
    """The block is on stdout before the commit: a kill there loses the count, not the block."""
    _session()
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _KILL_BETWEEN_PRINT_AND_COMMIT,
            str(config.db_path()),
            SID,
            AGENT,
            NONCE,
        ],
        capture_output=True,
        text=True,
        env=_hook_env(),
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL
    assert json.loads(proc.stdout.strip())["decision"] == "block"
    assert _blocks() == 0  # under-counted
    with Store() as store:  # the next Stop is told "1/5" again — a block is never skipped
        verdict, count = store.staff_guard_block(SID, AGENT, NONCE, limit=5, emit=lambda _n: None)
    assert (verdict, count) == ("block", 1)


@pytest.mark.usefixtures("staff")
def test_an_exception_in_emit_rolls_the_count_back() -> None:
    _session()
    with Store() as store:

        def boom(_n: int) -> None:
            raise RuntimeError("stdout gone")

        with pytest.raises(RuntimeError):
            store.staff_guard_block(SID, AGENT, NONCE, limit=5, emit=boom)
        assert store.staff_guard_blocks(SID, AGENT, NONCE) == 0
        assert not store.conn.in_transaction


# ---------------------------------------------------------------- cheap path, off switches
def test_no_marker_runs_no_subprocess(staff: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _session()
    assert _stop(capsys) is None
    assert _calls(staff) == []
    assert _events() == []


def test_marker_of_a_non_allowlisted_agent_is_ignored(
    staff: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _session()
    _write_marker(agent="nixos-overseer")
    assert _stop(capsys) is None
    assert _calls(staff) == []


def test_the_allowlist_env_replaces_the_default(
    staff: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CC_STAFF_CLOSE_GUARD_AGENTS", " nixos-overseer , ../evil,")
    _session()
    _write_marker()  # mac-overseer is no longer guarded
    assert _stop(capsys) is None
    assert _calls(staff) == []
    _write_marker(agent="nixos-overseer")
    decision = _stop(capsys)
    assert decision is not None and "staff lease -a nixos-overseer" in decision["reason"]
    assert staff_close_guard.guarded_agents() == ["nixos-overseer"]


@pytest.mark.usefixtures("staff")
def test_no_stat_beyond_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a marker the guard's whole cost is one lstat() per guarded agent."""
    seen: list[str] = []
    real = os.lstat

    def spy(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if "staff-state" in str(path):
            seen.append(str(path))
        return real(path, *args, **kwargs)

    _session()
    monkeypatch.setattr(staff_close_guard.os, "lstat", spy)
    assert _stop(capsys) is None
    assert seen == [str(staff_close_guard.marker_path(AGENT, SID))]


def test_kill_switch_turns_the_guard_off(
    staff: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CC_NO_STAFF_CLOSE_GUARD", "1")
    _session()
    _write_marker()
    assert _stop(capsys) is None
    assert _calls(staff) == []


@pytest.mark.usefixtures("staff")
def test_an_exception_means_no_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(Store, "get", broken)
    _write_marker()
    assert _stop(capsys) is None


@pytest.mark.usefixtures("staff")
def test_an_exception_keeps_the_lock_lease(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard runs last: its failure leaves the Stop lease of the locks in place."""
    monkeypatch.setattr(
        staff_close_guard, "decide", lambda *_a, **_k: (_ for _ in ()).throw(OSError("x"))
    )
    _session()
    _write_marker()
    with Store() as store:
        assert store.acquire_file_lock(SID, "/repo/a.py", now_ms(), {SID}, 60_000) is None
    assert _stop(capsys) is None
    with Store() as store:
        assert store.protection_deadline("/repo/a.py") > now_ms()


# ---------------------------------------------------------------- marker hardening
def test_symlinked_marker_never_blocks(
    staff: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _session()
    real = _write_marker()
    target = tmp_path / "elsewhere.json"
    real.rename(target)
    real.symlink_to(target)
    assert _stop(capsys) is None
    assert _calls(staff) == []
    assert _events()[-1].endswith("nonce=- decision=invalid-marker")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"agent": "other", "owner": SID, "nonce": NONCE}),
        json.dumps({"agent": AGENT, "owner": "someone-else", "nonce": NONCE}),
        json.dumps({"agent": AGENT, "owner": SID, "nonce": "../../x"}),
        json.dumps({"agent": AGENT, "owner": SID}),
    ],
)
def test_untrusted_marker_content_never_blocks(
    staff: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    _session()
    path = _write_marker()
    path.write_text(content, encoding="utf-8")
    assert _stop(capsys) is None
    assert _calls(staff) == []


@pytest.mark.usefixtures("staff")
def test_a_directory_in_place_of_the_marker_never_blocks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _session()
    path = staff_close_guard.marker_path(AGENT, SID)
    path.mkdir(parents=True)
    assert _stop(capsys) is None


@pytest.mark.parametrize("sid", ["../etc", "a/b", "", ".hidden", "x" * 129, "s1\n"])
def test_invalid_session_ids_are_never_looked_up(sid: str) -> None:
    with Store() as store:
        assert staff_close_guard.run(store, sid, None, lambda *_a: None) == ""
    assert not staff_close_guard.valid_id(sid)


def test_valid_ids() -> None:
    assert staff_close_guard.valid_id(SID)
    assert staff_close_guard.valid_id("mac-overseer")
    assert staff_close_guard.valid_id("x" * 128)
