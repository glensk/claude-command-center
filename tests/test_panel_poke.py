"""The Karabiner-side poker (``assets/panel-poke.sh``, PLAN_panel-server D1/D2, S4).

Every test installs the REAL template with :func:`panelpoke.install_poker` into a tmp app
home and runs it under Karabiner's minimal environment. The cold command is a fake ``ccc``
that appends ``<argv>|<CCC_HOME>`` to a marker file, so "went cold" == the marker exists.
A fake server is a thread that claims ``panel_request.*`` by ``os.rename`` and answers
``panel_ack.<nonce>`` with a chosen status (or never).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from command_center import panelpoke

_SHELLCHECK = shutil.which("shellcheck")


@dataclass
class Poker:
    """An installed poker plus its fake cold ``ccc``."""

    app_home: Path
    script: Path
    marker: Path

    def write_pidfile(self, pid: int, state: str = "ready") -> None:
        (self.app_home / "panel_server.pid").write_text(f"{pid} {state} stamp123\n")

    def run(self, verb: str = "park", **env: str) -> tuple[subprocess.CompletedProcess[str], float]:
        base = {"HOME": str(self.app_home.parent), "PATH": "/usr/bin:/bin"}
        started = time.monotonic()
        result = subprocess.run(
            ["/bin/sh", "-c", f'exec "$0" {verb}', str(self.script)],
            env={**base, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return result, time.monotonic() - started

    def cold_calls(self) -> list[str]:
        if not self.marker.exists():
            return []
        return self.marker.read_text(encoding="utf-8").splitlines()


def _make_poker(root: Path, env_overrides: dict[str, str] | None = None) -> Poker:
    app_home = root / "app"
    app_home.mkdir(parents=True)
    marker = root / "cold.log"
    fake = root / "fake ccc"
    fake.write_text(
        f'#!/bin/sh\nprintf \'%s|%s\\n\' "$*" "${{CCC_HOME:-}}" >> {shlex.quote(str(marker))}\n',
    )
    fake.chmod(0o755)
    script = panelpoke.install_poker(app_home, str(fake), env_overrides or {})
    return Poker(app_home=app_home, script=script, marker=marker)


@pytest.fixture(name="poker")
def poker_fixture(tmp_path: Path) -> Poker:
    return _make_poker(tmp_path)


@dataclass
class Acker:
    """Fake server: claim each request, then ack with *status* after *delay* (or never)."""

    app_home: Path
    status: str | None
    delay: float = 0.0
    after_claim: list = field(default_factory=list)  # callables run right after a claim
    requests: list[str] = field(default_factory=list)  # content read after each claim
    _stop: threading.Event = field(default_factory=threading.Event)

    def _loop(self) -> None:
        while not self._stop.is_set():
            for path in self.app_home.glob("panel_request.*"):
                nonce = path.name.split(".", 1)[1]
                claimed = self.app_home / f"panel_claimed.{nonce}"
                try:
                    os.rename(path, claimed)
                except FileNotFoundError:
                    continue
                self.requests.append(claimed.read_text(encoding="utf-8"))
                for hook in self.after_claim:
                    hook()
                if self.status is not None:
                    if self.delay:
                        time.sleep(self.delay)
                    ack = self.app_home / f"panel_ack.{nonce}"
                    tmp = self.app_home / f".ack.{nonce}"
                    tmp.write_text(f"{self.status} {int(time.time())}\n")
                    os.rename(tmp, ack)
            time.sleep(0.005)

    def stop(self) -> None:
        self._stop.set()


@pytest.fixture(name="start_acker")
def start_acker_fixture(poker: Poker) -> Iterator:
    threads: list[tuple[Acker, threading.Thread]] = []

    def start(status: str | None, delay: float = 0.0, after_claim: list | None = None) -> Acker:
        acker = Acker(poker.app_home, status, delay, after_claim or [])
        thread = threading.Thread(target=acker._loop, daemon=True)  # pylint: disable=protected-access
        thread.start()
        threads.append((acker, thread))
        return acker

    yield start
    for acker, thread in threads:
        acker.stop()
        thread.join(timeout=2)


def _dead_pid() -> int:
    proc = subprocess.Popen(["/usr/bin/true"])  # pylint: disable=consider-using-with
    proc.wait()
    return proc.pid


# --- no-poke branches ------------------------------------------------------------------


def test_bad_verb_exits_2_without_cold(poker: Poker) -> None:
    poker.write_pidfile(os.getpid())
    result, _ = poker.run("jump")
    assert result.returncode == 2
    assert poker.cold_calls() == []
    result, _ = poker.run("")
    assert result.returncode == 2


@pytest.mark.parametrize(
    "env", [{"CCC_PANEL_COLD": "1"}, {"CCC_PARK_PANEL_TIMEOUT": "1"}], ids=["cold", "timeout"]
)
def test_env_controls_go_cold(poker: Poker, start_acker, env: dict[str, str]) -> None:
    poker.write_pidfile(os.getpid())
    acker = start_acker("shown")
    result, _ = poker.run("park", **env)
    assert result.returncode == 0
    assert poker.cold_calls() == ["park -g|"]
    assert acker.requests == []


def test_no_pidfile_goes_cold(poker: Poker) -> None:
    result, _ = poker.run("peek")
    assert result.returncode == 0
    assert poker.cold_calls() == ["peek|"]
    assert not list(poker.app_home.glob("panel_*"))  # never poked: no window waited


def test_dead_pid_goes_cold(poker: Poker) -> None:
    poker.write_pidfile(_dead_pid())
    result, _ = poker.run("park")
    assert result.returncode == 0
    assert poker.cold_calls() == ["park -g|"]
    assert [p.name for p in poker.app_home.glob("panel_*")] == ["panel_server.pid"]


def test_degraded_state_goes_cold(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid(), "degraded")
    acker = start_acker("shown")
    poker.run("peek")
    assert poker.cold_calls() == ["peek|"]
    assert acker.requests == []


# --- live server -----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["shown", "busy", "stale"])
@pytest.mark.parametrize("state", ["ready", "busy"])
def test_live_ack_exits_0_without_cold(poker: Poker, start_acker, status: str, state: str) -> None:
    poker.write_pidfile(os.getpid(), state)
    start_acker(status)
    result, _ = poker.run("park")
    assert result.returncode == 0
    assert poker.cold_calls() == []
    assert not list(poker.app_home.glob("panel_abandoned.*"))


def test_failed_ack_goes_cold(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid())
    start_acker("failed")
    poker.run("peek")
    assert poker.cold_calls() == ["peek|"]


def test_request_content_is_verb_nonce_epoch(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid())
    acker = start_acker("shown")
    before = int(time.time())
    poker.run("peek")
    assert len(acker.requests) == 1
    match = re.fullmatch(r"peek (\d+-(\d+)) (\d+)\n", acker.requests[0])
    assert match, acker.requests
    assert match.group(2) == match.group(3)
    assert before <= int(match.group(3)) <= int(time.time())
    claimed = list(poker.app_home.glob("panel_claimed.*"))
    assert [p.name for p in claimed] == [f"panel_claimed.{match.group(1)}"]
    assert not list(poker.app_home.glob("panel_tmp.*"))


def test_unclaimed_request_is_abandoned_then_cold(poker: Poker) -> None:
    poker.write_pidfile(os.getpid())
    result, elapsed = poker.run("park")
    assert result.returncode == 0
    assert poker.cold_calls() == ["park -g|"]
    assert len(list(poker.app_home.glob("panel_abandoned.*"))) == 1
    assert not list(poker.app_home.glob("panel_request.*"))
    assert elapsed >= 0.4


def test_claimed_never_acked_live_pid_exits_0_after_two_windows(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid())
    start_acker(None)
    result, elapsed = poker.run("park")
    assert result.returncode == 0
    assert poker.cold_calls() == []
    assert elapsed >= 0.8
    assert not list(poker.app_home.glob("panel_abandoned.*"))


def test_claimed_then_ack_in_second_window_no_cold(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid())
    start_acker("shown", delay=0.65)
    result, elapsed = poker.run("peek")
    assert result.returncode == 0
    assert poker.cold_calls() == []
    assert elapsed >= 0.65


def test_claimed_then_failed_in_second_window_goes_cold(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid())
    start_acker("failed", delay=0.65)
    poker.run("peek")
    assert poker.cold_calls() == ["peek|"]


def test_crash_after_claim_goes_cold(poker: Poker, start_acker) -> None:
    server = subprocess.Popen(["/bin/sleep", "30"])  # pylint: disable=consider-using-with
    try:
        poker.write_pidfile(server.pid)

        def crash() -> None:
            server.kill()
            server.wait()

        start_acker(None, after_claim=[crash])
        result, elapsed = poker.run("park")
    finally:
        server.kill()
        server.wait()
    assert result.returncode == 0
    assert poker.cold_calls() == ["park -g|"]
    assert elapsed >= 0.8


def test_trace_line_on_cold(poker: Poker) -> None:
    result, _ = poker.run("peek", PANEL_POKE_TRACE="1")
    assert re.fullmatch(r"panel-poke: cold peek \d+(\.\d+)?\n", result.stderr), result.stderr
    assert poker.cold_calls() == ["peek|"]


def test_no_output_on_warm_path(poker: Poker, start_acker) -> None:
    poker.write_pidfile(os.getpid())
    start_acker("shown")
    result, _ = poker.run("park", PANEL_POKE_TRACE="1")
    assert result.stdout == "" and result.stderr == ""


# --- installer -------------------------------------------------------------------------


def test_path_with_space_and_quote_and_env_override(tmp_path: Path) -> None:
    root = tmp_path / "it's a dir"
    home_override = "/tmp/o'ther home"
    poker = _make_poker(root, {"CCC_HOME": home_override})
    # cold end to end (no pidfile), with the override exported to the cold ccc
    poker.run("park")
    assert poker.cold_calls() == [f"park -g|{home_override}"]
    # warm end to end: pidfile + request + ack all live under the quoted app home
    poker.marker.unlink()
    poker.write_pidfile(os.getpid())
    acker = Acker(poker.app_home, "shown")
    thread = threading.Thread(target=acker._loop, daemon=True)  # pylint: disable=protected-access
    thread.start()
    try:
        result, _ = poker.run("peek")
    finally:
        acker.stop()
        thread.join(timeout=2)
    assert result.returncode == 0
    assert poker.cold_calls() == []
    assert len(acker.requests) == 1


def test_install_mode_and_atomic_reinstall(tmp_path: Path) -> None:
    home = tmp_path / "app"
    old_umask = os.umask(0o077)
    try:
        path = panelpoke.install_poker(home, "/opt/ccc one", {})
    finally:
        os.umask(old_umask)
    assert path == panelpoke.poker_path(home) == home / "panel-poke.sh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    first_inode = path.stat().st_ino
    assert "COLD_CCC='/opt/ccc one'" in path.read_text(encoding="utf-8")
    for token in ("@APP_HOME@", "@COLD_CCC@", "@ENV_OVERRIDES@"):
        assert token not in path.read_text(encoding="utf-8")

    path2 = panelpoke.install_poker(home, "/opt/ccc two", {"CLAUDE_HOME": "/x y"})
    assert path2 == path
    text = path.read_text(encoding="utf-8")
    assert "COLD_CCC='/opt/ccc two'" in text
    assert "export CLAUDE_HOME='/x y'" in text
    assert path.stat().st_ino != first_inode  # replaced, not rewritten in place
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert sorted(p.name for p in home.iterdir()) == ["panel-poke.sh"]

    assert panelpoke.remove_poker(home) is True
    assert panelpoke.remove_poker(home) is False


def test_install_rejects_bad_env_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        panelpoke.install_poker(tmp_path, "/bin/ccc", {"BAD NAME": "x"})
    assert not panelpoke.poker_path(tmp_path).exists()


@pytest.mark.skipif(_SHELLCHECK is None, reason="shellcheck not installed")
def test_shellcheck_template_and_installed_copy(tmp_path: Path) -> None:
    assert _SHELLCHECK is not None
    template = Path(panelpoke.__file__).parent / "assets" / "panel-poke.sh"
    installed = panelpoke.install_poker(
        tmp_path / "a b'c", "/opt/c'cc", {"CCC_HOME": "/h o'me", "CLAUDE_HOME": "/c"}
    )
    for script in (template, installed):
        result = subprocess.run(
            [_SHELLCHECK, "-s", "sh", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stdout
