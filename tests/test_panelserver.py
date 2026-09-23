"""The resident panel server's protocol and dispatcher, headlessly (tp#70 S3c).

Everything here runs without AppKit: :class:`panelserver.Core` gets fake park/peek seams
and a manual main-thread queue, so the claim/ack protocol, the state machine, generation
tokens, fault points and metrics are pinned exactly as the poker relies on them.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from command_center import panelserver as ps


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _poke(p: ps.Paths, verb: str, nonce: str, age: float = 0.0) -> Path:
    """What the poker does: an atomic ``panel_request.<nonce>`` (optionally back-dated)."""
    path = p.request(nonce)
    ps._atomic_write(path, f"{verb} {nonce} {int(time.time())}\n")  # pylint: disable=protected-access
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


class _Main:
    """A stand-in for the AppKit main thread: ``post_main`` queues, ``drain`` runs."""

    def __init__(self) -> None:
        self.queue: list[Callable[[], None]] = []
        self.lock = threading.Lock()

    def post(self, fn: Callable[[], None]) -> None:
        with self.lock:
            self.queue.append(fn)

    def drain(self, timeout: float = 2.0, until: Callable[[], bool] | None = None) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                batch, self.queue = self.queue, []
            for fn in batch:
                fn()
            if until is None and not batch:
                return
            if until is not None and until():
                return
            time.sleep(0.01)


def _core(tmp_path: Path, **kw: Any) -> tuple[ps.Core, _Main, dict[str, Any]]:
    main = _Main()
    seen: dict[str, Any] = {"park": 0, "shown": [], "during": []}
    p = ps.Paths(tmp_path)

    def _park(request: ps.Request, on_shown: Callable[[], None]) -> None:
        seen["park"] += 1
        on_shown()
        hook = kw.get("during_park")
        if hook:
            hook()

    def _resolve(request: ps.Request) -> tuple[Any, str]:
        delay = kw.get("resolve_delay", 0.0)
        if delay:
            time.sleep(delay)
        if kw.get("resolve_raises"):
            raise RuntimeError("resolve boom")
        return {"nonce": request.nonce}, "hit"

    def _show(request: ps.Request, data: Any, on_shown: Callable[[], None]) -> None:
        on_shown()
        seen["shown"].append(data)
        hook = kw.get("during_peek")
        if hook:
            hook()

    core = ps.Core(
        p,
        os.getpid(),
        123,
        run_park=kw.get("run_park", _park),
        resolve_peek=_resolve,
        show_peek=kw.get("show_peek", _show),
        post_main=main.post,
        fault=kw.get("fault", ""),
    )
    core.set_state("ready")
    return core, main, seen


# --------------------------------------------------------------------------- #
# pidfile, stamp, singleton
# --------------------------------------------------------------------------- #
def test_pidfile_roundtrip_and_state_transitions(tmp_path: Path) -> None:
    core, main, _seen = _core(tmp_path)
    info = ps.read_pidfile(core.p)
    assert info == ps.PidInfo(os.getpid(), "ready", 123)
    states: list[str] = []

    def _park(_r: ps.Request, on_shown: Callable[[], None]) -> None:
        states.append(ps.read_pidfile(core.p).state)  # type: ignore[union-attr]
        on_shown()

    core.run_park = _park
    _poke(core.p, "park", "n1")
    core.tick_default()
    main.drain()
    assert states == ["busy"]
    assert ps.read_pidfile(core.p).state == "ready"  # type: ignore[union-attr]
    # No temp files left behind by the atomic writes.
    assert not list(tmp_path.glob(".*.tmp"))


def test_pidfile_garbage_and_remove_guard(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    p.pidfile.write_text("garbage\n")
    assert ps.read_pidfile(p) is None
    ps.write_pidfile(p, 4242, "ready", 1)
    ps.remove_pidfile(p, pid=1)  # another pid's file stays
    assert ps.read_pidfile(p) is not None
    ps.remove_pidfile(p, pid=4242)
    assert ps.read_pidfile(p) is None


def test_code_stamp_tracks_newest_py(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("y = 2\n")
    first = ps.code_stamp(tmp_path)
    time.sleep(0.01)
    (sub / "b.py").write_text("y = 3\n")
    assert ps.code_stamp(tmp_path) > first
    (tmp_path / "notes.txt").write_text("ignored")
    assert ps.code_stamp(tmp_path) == ps.code_stamp(tmp_path)


def test_singleton_under_tmp_claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("CCC_HOME", raising=False)
    p = ps.paths()
    assert str(p.home).startswith(str(tmp_path))
    with ps.singleton(p) as first:
        assert first is not None
        # A second process cannot take it.
        code = (
            "import sys; from pathlib import Path; from command_center import panelserver as ps;"
            f"p = ps.Paths(Path({str(p.home)!r}));"
            "cm = ps.singleton(p); held = cm.__enter__(); sys.exit(0 if held is None else 1)"
        )
        assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
    with ps.singleton(p) as again:
        assert again is not None


def test_scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ps.SCRUBBED_ENV:
        monkeypatch.setenv(name, "x")
    ps.scrub_env()
    assert not any(name in os.environ for name in ps.SCRUBBED_ENV)


# --------------------------------------------------------------------------- #
# claim / abandon / ack
# --------------------------------------------------------------------------- #
def test_claim_answers_shown_and_records_metrics(tmp_path: Path) -> None:
    core, main, seen = _core(tmp_path)
    _poke(core.p, "peek", "n1")
    core.tick_default()
    main.drain(until=lambda: not core.busy)
    assert ps.read_ack(core.p, "n1") == "shown"
    assert seen["shown"] == [{"nonce": "n1"}]
    assert core.p.claimed("n1").exists() and not core.p.request("n1").exists()
    (row,) = ps.read_metrics(core.p)
    assert row["verb"] == "peek" and row["outcome"] == "shown" and row["cache_state"] == "hit"
    for key in ("queued_ms", "resolve_ms", "build_ms", "shown_ms"):
        assert row[key] is not None and row[key] >= 0


def test_abandoned_request_is_never_claimed(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    path = _poke(p, "peek", "n1")
    os.rename(path, p.abandoned("n1"))  # the poker won the race
    assert ps.claim(p, path) is None
    assert ps.read_ack(p, "n1") is None


def _race_claim(home: str, nonce: str, barrier: Any, out: Any) -> None:
    p = ps.Paths(Path(home))
    barrier.wait()
    out.put(("server", ps.claim(p, p.request(nonce)) is not None))


def _race_abandon(home: str, nonce: str, barrier: Any, out: Any) -> None:
    p = ps.Paths(Path(home))
    barrier.wait()
    try:
        os.rename(p.request(nonce), p.abandoned(nonce))
        out.put(("poker", True))
    except OSError:
        out.put(("poker", False))


def test_claim_abandon_race_across_processes_has_exactly_one_winner(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    for i in range(10):
        nonce = f"race{i}"
        _poke(ps.Paths(tmp_path), "peek", nonce)
        barrier = ctx.Barrier(2)
        out = ctx.Queue()
        procs = [
            ctx.Process(target=_race_claim, args=(str(tmp_path), nonce, barrier, out)),
            ctx.Process(target=_race_abandon, args=(str(tmp_path), nonce, barrier, out)),
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(20)
        results = dict(out.get(timeout=5) for _ in procs)
        assert results["server"] != results["poker"], results


def test_malformed_and_unknown_verb_are_failed(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    p.request("bad").write_text("garbage\n")
    assert ps.claim(p, p.request("bad")) is None
    assert ps.read_ack(p, "bad") == "failed"
    _poke(p, "jump", "odd")
    assert ps.claim(p, p.request("odd")) is None
    assert ps.read_ack(p, "odd") == "failed"


def test_stale_request_is_acked_stale_and_never_shown(tmp_path: Path) -> None:
    core, main, seen = _core(tmp_path)
    _poke(core.p, "park", "old", age=ps.STALE_SEC + 1)
    core.tick_default()
    main.drain()
    assert ps.read_ack(core.p, "old") == "stale"
    assert seen["park"] == 0


def test_purge_removes_only_old_protocol_files(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    for name in ("panel_claimed.a", "panel_abandoned.b", "panel_ack.c", "panel_ack.fresh"):
        (tmp_path / name).write_text("x")
    old = time.time() - ps.PURGE_SEC - 5
    for name in ("panel_claimed.a", "panel_abandoned.b", "panel_ack.c"):
        os.utime(tmp_path / name, (old, old))
    keep = _poke(p, "peek", "pending")
    os.utime(keep, (old, old))  # an unclaimed request is never purged here
    assert ps.purge(p) == 3
    assert sorted(x.name for x in tmp_path.glob("panel_*")) == [
        "panel_ack.fresh",
        "panel_request.pending",
    ]


def test_concurrent_requests_one_shown_rest_busy(tmp_path: Path) -> None:
    core, main, seen = _core(tmp_path, resolve_delay=0.2)
    for i in range(3):
        _poke(core.p, "peek", f"c{i}")
    core.tick_default()  # claims all three: first dispatches, the others are busy
    main.drain(until=lambda: not core.busy)
    acks = [ps.read_ack(core.p, f"c{i}") for i in range(3)]
    assert acks.count("shown") == 1 and acks.count("busy") == 2
    assert len(seen["shown"]) == 1


def test_busy_while_resolving(tmp_path: Path) -> None:
    core, main, _seen = _core(tmp_path, resolve_delay=0.3)
    _poke(core.p, "peek", "first")
    core.tick_default()
    assert core.busy and ps.read_pidfile(core.p).state == "busy"  # type: ignore[union-attr]
    _poke(core.p, "park", "second")
    core.tick_default()  # the default timer answers while the worker resolves
    assert ps.read_ack(core.p, "second") == "busy"
    main.drain(until=lambda: not core.busy)
    assert ps.read_ack(core.p, "first") == "shown"


def test_busy_while_modal(tmp_path: Path) -> None:
    """A panel's modal loop: only the modal-mode timer runs, and it answers busy."""
    holder: dict[str, ps.Core] = {}

    def _during() -> None:
        _poke(holder["core"].p, "peek", "during")
        holder["core"].tick_modal()

    core, main, _seen = _core(tmp_path, during_park=_during)
    holder["core"] = core
    _poke(core.p, "park", "park1")
    core.tick_default()
    main.drain()
    assert ps.read_ack(core.p, "park1") == "shown"
    assert ps.read_ack(core.p, "during") == "busy"
    assert core.state == "ready"


def test_not_ready_state_hands_back_cold(tmp_path: Path) -> None:
    core, main, seen = _core(tmp_path)
    core.link_ready = lambda: False  # the iTerm link is down → settle() keeps degraded
    core.set_state("degraded")
    _poke(core.p, "peek", "n1")
    core.tick_default()
    main.drain()
    assert ps.read_ack(core.p, "n1") == "failed"
    assert not seen["shown"]


# --------------------------------------------------------------------------- #
# generations, timeouts, faults
# --------------------------------------------------------------------------- #
def test_slow_peek_fails_at_the_bound_and_late_result_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "PEEK_RESOLVE_BOUND_SEC", 0.1)
    core, main, seen = _core(tmp_path, resolve_delay=0.4)
    _poke(core.p, "peek", "slow")
    core.tick_default()
    main.drain(timeout=1.0, until=lambda: ps.read_ack(core.p, "slow") is not None)
    assert ps.read_ack(core.p, "slow") == "failed"
    assert not core.busy
    main.drain(timeout=1.0, until=lambda: False)  # let the late worker post its result
    assert seen["shown"] == []  # generation moved on → discarded, no duplicate panel


def test_shutdown_discards_in_flight_result(tmp_path: Path) -> None:
    core, main, seen = _core(tmp_path, resolve_delay=0.1)
    _poke(core.p, "peek", "n1")
    core.tick_default()
    core.shutdown()
    main.drain(timeout=0.5, until=lambda: False)
    assert seen["shown"] == []
    assert ps.read_pidfile(core.p) is None


def test_resolve_exception_is_failed(tmp_path: Path) -> None:
    core, main, _seen = _core(tmp_path, resolve_raises=True)
    _poke(core.p, "peek", "n1")
    core.tick_default()
    main.drain(until=lambda: not core.busy)
    assert ps.read_ack(core.p, "n1") == "failed"
    assert core.state == "ready"


def test_fault_before_claim_leaves_request_for_the_poker(tmp_path: Path) -> None:
    core, main, _seen = _core(tmp_path, fault="before-claim")
    path = _poke(core.p, "peek", "n1")
    core.tick_default()
    main.drain()
    assert path.exists() and ps.read_ack(core.p, "n1") is None  # → abandon → cold


def test_fault_after_claim_raise_is_failed_after_closing(tmp_path: Path) -> None:
    for verb in ("park", "peek"):
        core, main, seen = _core(tmp_path / verb, fault="after-claim-raise")
        _poke(core.p, verb, "n1")
        core.tick_default()
        main.drain(until=lambda c=core: not c.busy)  # type: ignore[misc]
        assert ps.read_ack(core.p, "n1") == "failed", verb
        assert seen["park"] == 0 and seen["shown"] == []


def test_fault_after_show_stays_shown(tmp_path: Path) -> None:
    for verb in ("park", "peek"):
        core, main, _seen = _core(tmp_path / verb, fault="after-show")
        _poke(core.p, verb, "n1")
        core.tick_default()
        main.drain(until=lambda c=core: not c.busy)  # type: ignore[misc]
        assert ps.read_ack(core.p, "n1") == "shown", verb  # never "failed" → no cold duplicate
        assert core.state == "ready"


def test_fault_after_claim_crash_exits_without_ack(tmp_path: Path) -> None:
    code = (
        "import os, time; from pathlib import Path; from command_center import panelserver as ps;"
        f"p = ps.Paths(Path({str(tmp_path)!r}));"
        "core = ps.Core(p, os.getpid(), 1, run_park=None, resolve_peek=None, show_peek=None,"
        " post_main=None, fault='after-claim-crash'); core.set_state('ready');"
        "core.tick_default()"
    )
    _poke(ps.Paths(tmp_path), "peek", "n1")
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 3
    assert ps.read_ack(ps.Paths(tmp_path), "n1") is None
    assert ps.Paths(tmp_path).claimed("n1").exists()  # claimed → the poker's 2nd window


# --------------------------------------------------------------------------- #
# restart verb (deferred while busy, pid-targeted)
# --------------------------------------------------------------------------- #
def test_restart_is_deferred_while_busy(tmp_path: Path) -> None:
    restarts: list[str] = []
    holder: dict[str, ps.Core] = {}

    def _during() -> None:
        ps.request_restart(holder["core"].p, holder["core"].pid)
        holder["core"].tick_modal()
        assert restarts == []  # never while the panel is open

    core, main, _seen = _core(tmp_path, during_park=_during)
    holder["core"] = core
    core.restart_fn = lambda: restarts.append("restart")
    _poke(core.p, "park", "n1")
    core.tick_default()
    main.drain()
    # The request was noticed only by the default tick; the next idle tick restarts.
    ps.request_restart(core.p, core.pid)
    core.tick_default()
    assert restarts == ["restart"]
    assert not core.p.restart.exists()


def test_restart_for_another_pid_is_ignored(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    ps.request_restart(p, proc.pid)  # a dead pid: a leftover for an old server
    assert ps.take_restart(p, os.getpid()) is False
    assert not p.restart.exists()  # cleaned up
    ps.request_restart(p, os.getppid())  # a live other pid: not ours, left alone
    assert ps.take_restart(p, os.getpid()) is False
    assert p.restart.exists()


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def test_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert ps.nearest_rank(values, 95) == 95.0
    assert ps.nearest_rank(values, 50) == 50.0
    assert ps.nearest_rank([7.0], 95) == 7.0
    assert ps.nearest_rank([], 95) is None
    assert ps.nearest_rank([3.0, 1.0, 2.0], 100) == 3.0


def test_summary_splits_peek_by_cache_state_and_judges_slo(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    for ms in range(100, 131):
        ps.append_metric(p, {"verb": "park", "outcome": "shown", "shown_ms": ms})
    for ms in (150, 160):
        ps.append_metric(
            p, {"verb": "peek", "outcome": "shown", "shown_ms": ms, "cache_state": "hit"}
        )
    ps.append_metric(
        p, {"verb": "peek", "outcome": "shown", "shown_ms": 420, "cache_state": "miss"}
    )
    ps.append_metric(p, {"verb": "peek", "outcome": "busy"})
    p.metrics.open("a").write("not json\n")
    groups = {g["group"]: g for g in ps.summarize(ps.read_metrics(p))}
    assert groups["park"]["n"] == 31 and groups["park"]["p95"] == 129 and groups["park"]["slo_pass"]
    assert groups["peek/hit"]["p95"] == 160
    assert groups["peek/miss"]["slo_pass"] is False
    assert groups["peek/-"]["shown"] == 0
    text = ps.format_stats(ps.read_metrics(p, 5))
    assert "miss: exempt" in text


def test_status_line(tmp_path: Path) -> None:
    p = ps.Paths(tmp_path)
    assert ps.status_line(p)[0] == "absent"
    ps.write_pidfile(p, os.getpid(), "ready", ps.code_stamp())
    assert ps.status_line(p)[0] == "ok"
    ps.write_pidfile(p, os.getpid(), "degraded", ps.code_stamp())
    assert ps.status_line(p)[0] == "degraded"
    ps.write_pidfile(p, os.getpid(), "ready", 0)
    assert ps.status_line(p)[0] == "stale"
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    ps.write_pidfile(p, proc.pid, "ready", ps.code_stamp())
    assert ps.status_line(p)[0] == "dead"


# --------------------------------------------------------------------------- #
# ItermFrontmost over a fake link thread
# --------------------------------------------------------------------------- #
class _FakeLink:
    def __init__(self, uuid: str | None, variables: dict[str, str]) -> None:
        self._uuid = uuid
        self._vars = variables

    async def current_session_uuid(self) -> str | None:
        return self._uuid

    async def session_variable(self, uuid: str, name: str) -> str | None:
        del uuid
        return self._vars.get(name)


class _FakeLinkThread:
    def __init__(self, uuid: str | None, variables: dict[str, str]) -> None:
        self.calls = 0
        self._link = _FakeLink(uuid, variables)

    def call(self, factory: Any, timeout: float) -> Any:
        del timeout
        self.calls += 1
        return asyncio.run(factory(self._link))


def test_iterm_frontmost_memoises_uuid_and_reads_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from command_center import jumpstate

    link = _FakeLinkThread("ABC-1", {"path": "/tmp/x", "tty": "/dev/ttys001"})
    front = ps.ItermFrontmost(link)
    assert front.uuid() == "ABC-1" and front.uuid() == "ABC-1"
    assert link.calls == 1
    assert front.cwd() == "/tmp/x" and front.tty() == "/dev/ttys001"
    monkeypatch.setattr(jumpstate, "get_tui", lambda: (os.getpid(), "w0t1p0:abc-1"))
    assert front.is_ccc_tui() is True
    monkeypatch.setattr(jumpstate, "get_tui", lambda: (os.getpid(), "w0t1p0:OTHER"))
    assert ps.ItermFrontmost(link).is_ccc_tui() is False
    monkeypatch.setattr(jumpstate, "get_tui", lambda: None)
    assert ps.ItermFrontmost(link).is_ccc_tui() is False
    none = ps.ItermFrontmost(_FakeLinkThread(None, {}))
    assert none.cwd() is None and none.is_ccc_tui() is False
