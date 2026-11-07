"""The tests-only kill guard, Popen guard and audit writer (tp#1793), shared by two hosts.

* ``tests/conftest.py`` imports it in the pytest process and installs the guards per test
  through ``monkeypatch`` (B1/B2: per-test nodeid, opt-out markers, tmp basetemp).
* ``tests/_childguard/sitecustomize.py`` imports it in every Python child started with the
  pinned test env (``sys.executable -m command_center …`` and the like) and calls
  :func:`install_child_guard` (B3): monkeypatches do not reach a child, this does.

A test that hands a fixture pid (4321, 4242, ...) to an unfaked ``os.kill`` signals
whatever process holds that pid right now: under suite load pids wrap every few minutes,
so that can be a live claude. Every signal goes through :func:`_guarded_signal`; one aimed
at a live process outside the pytest tree is refused and recorded. The record goes to an
audit file OUTSIDE the pytest tmp dir, because ``closelog.log_close`` writes its events.log
under the pinned (throwaway) app home. Everything is captured at import, before any test
(or child code) can patch it. Never imported by ``command_center``.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

_REAL_KILL = os.kill
_REAL_KILLPG = os.killpg
_REAL_GETPGRP = os.getpgrp
_REAL_POPEN = subprocess.Popen
_REAL_EXECUTE_CHILD = subprocess.Popen._execute_child  # type: ignore[attr-defined]  # pylint: disable=no-member
# The developer's REAL home, captured before conftest's ``_isolate_home`` re-points ``$HOME``.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()
_PS = "/bin/ps" if os.path.exists("/bin/ps") else "ps"  # never a fake ``ps`` on a test PATH
_WALK_CAP = 64
_AUDIT_ENV = "CCC_TEST_KILL_AUDIT"
_STRICT_ENV = "CCC_TEST_KILL_STRICT"
_ROOT_ENV = "CCC_TEST_ROOT_PID"  # the pytest pid, the descendant root of a child guard
_CHILD_CHANNEL = "child_sitecustomize"
# The channels instrumented in this run, named in the audit header (B2/B3 append theirs).
_AUDIT_CHANNELS: list[str] = ["os.kill", "os.killpg", "log_close"]
_HEADERED: set[str] = set()  # audit files that already got this session's header


class _State:
    """Where this process sits: the pytest root pid, and whether it is a guarded child."""

    root_pid: int = os.getpid()
    child: bool = False
    # A child knows no pytest basetemp; a ``ccc`` built under the temp dir (the smoke
    # matrix's sandbox venv, a tmp-installed wheel) is this checkout's code, never the
    # live install.
    built_root: Path | None = None


# Pids this process spawned (every ``subprocess.Popen`` child, see _record_popen_child) or
# proved to be in the pytest tree; they stay here after they die.
_OWN_PIDS: set[int] = {os.getpid()}

# Binary basename -> the conftest marker that opts a test out of the Popen guard for it.
_POPEN_GUARDED: dict[str, str] = {
    "osascript": "real_osascript",
    "tmux": "real_signal",
    "kill": "real_signal",
    "ccc": "real_signal",
}
# The test interpreter's OWN ``ccc`` console script (this checkout's code, the same child as
# ``sys.executable -m command_center``) is not the live installed ``ccc``.
_OWN_CCC = os.path.realpath(Path(sys.executable).with_name("ccc"))
_SHELL_POS = 7  # ``shell`` in ``Popen._execute_child(args, executable, *rest)``
_ARGV_KEEP = 6  # argv entries kept in a refusal line
_ARG_CHARS = 200  # characters kept per argv entry (an AppleScript body is long)


def _record_popen_child(self: subprocess.Popen, *args: object, **kwargs: object) -> None:
    """``Popen._execute_child`` that remembers the child pid in :data:`_OWN_PIDS`."""
    _REAL_EXECUTE_CHILD(self, *args, **kwargs)
    _OWN_PIDS.add(self.pid)


def _audit_path() -> Path:
    """``$CCC_TEST_KILL_AUDIT``, else ``~/.cache/ccc-test-kill-audit.log`` of the REAL home."""
    raw = os.environ.get(_AUDIT_ENV, "")
    return Path(raw) if raw else _REAL_HOME / ".cache" / "ccc-test-kill-audit.log"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _audit_write(record: dict[str, object]) -> None:
    """Append *record* (and this session's header first, once per file). Never raises.

    A guarded child writes no header: the pytest session that started it already did.
    """
    path = _audit_path()
    lines = []
    if str(path) not in _HEADERED and not _State.child:
        _HEADERED.add(str(path))
        header = {
            "ts": _now(),
            "event": "header",
            "pytest_pid": _State.root_pid,
            "channels": list(_AUDIT_CHANNELS),
            "strict": os.environ.get(_STRICT_ENV) == "1",
        }
        lines.append(json.dumps(header))
    if record:
        if _State.child:
            record["child_pid"] = os.getpid()
            record["via"] = _CHILD_CHANNEL
        lines.append(json.dumps(record))
    if not lines:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _ps_field(pid: int, field: str) -> str | None:
    """``ps -o <field>= -p <pid>`` through the captured Popen; ``None`` on any failure."""
    try:
        with _REAL_POPEN(
            [_PS, "-o", f"{field}=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as proc:
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _REAL_KILL(proc.pid, 9)  # our own ps child; never via the guarded os.kill
                proc.communicate()
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() if proc.returncode == 0 else None


def _in_pytest_tree(pid: int) -> bool:
    """True when *pid* is the pytest process or one of its descendants.

    Walks ``ppid`` links at most :data:`_WALK_CAP` levels; a parse failure, a timeout or
    hitting the cap answers ``False`` (the caller treats the target as foreign).
    """
    current = pid
    for _ in range(_WALK_CAP):
        if current == _State.root_pid:
            return True
        if current <= 1:
            return False
        parent = _ps_field(current, "ppid")
        if not parent or not parent.isdigit():
            return False
        current = int(parent)
    return False


def _exists(probe: Callable[[int, int], None], pid: int) -> bool:
    """Signal-0 probe through the REAL kill: ``EPERM`` still means the target exists."""
    try:
        probe(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _group_of_own_dead_leader(pgid: int) -> bool:
    """True for a live group whose leader this process spawned and which has since exited.

    A group outlives its leader (a forked grandchild keeps it), and POSIX never hands the
    pgid out again while the group exists — so such a group is still the one we created.
    """
    return pgid in _OWN_PIDS and not _exists(_REAL_KILL, pgid)


def _guarded_signal(nodeid: str, channel: str, target: int, sig: int) -> None:
    """Deliver ``os.kill`` / ``os.killpg`` unless it would hit a live process outside pytest.

    Rules, in order: signal 0 passes; the own process group (``kill(0)``, ``kill(-own)``,
    ``killpg(own)``, ``killpg(<=0)``) passes; a target that does not exist goes to the real
    call (which raises ``ProcessLookupError``) and is logged ``would-hit-on-recycle`` unless
    it was ours; a target in the pytest tree passes, and so does a live group whose
    leader we spawned and which has exited (:func:`_group_of_own_dead_leader`); anything else — and
    ``kill(-1, …)``, "every process I may signal" — is ``foreign``: logged, then refused
    with ``ProcessLookupError`` (or ``AssertionError`` under ``CCC_TEST_KILL_STRICT=1``).
    A group (``killpg(g)``, ``kill(-g)``) is judged by its leader pid *g*.
    """
    is_group = channel == "os.killpg" or target < 0

    def deliver() -> None:
        if channel == "os.killpg":
            _REAL_KILLPG(target, sig)
        else:
            _REAL_KILL(target, sig)

    if sig == 0:
        deliver()
        return
    if channel == "os.killpg":
        leader = target
        if leader <= 0:  # killpg(0) = own group; a negative one is EINVAL from the real call
            deliver()
            return
    else:
        leader = -target if target < 0 else target
        if target == 0:
            deliver()
            return
    if is_group and leader == _REAL_GETPGRP():
        deliver()
        return
    if target != -1:
        alive = _exists(_REAL_KILLPG if is_group else _REAL_KILL, leader)
        if not alive:
            if leader not in _OWN_PIDS:
                _audit_write(_audit_record(nodeid, channel, target, sig, "would-hit-on-recycle"))
            deliver()
            return
        if _in_pytest_tree(leader) or (is_group and _group_of_own_dead_leader(leader)):
            _OWN_PIDS.add(leader)
            deliver()
            return
    _audit_write(_audit_record(nodeid, channel, target, sig, "foreign"))
    if os.environ.get(_STRICT_ENV) == "1":
        raise AssertionError(f"test signalled a foreign pid {target} (sig {sig}) via {channel}")
    raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))


def _audit_record(
    nodeid: str, channel: str, target: int, sig: int | None, verdict: str, **extra: object
) -> dict[str, object]:
    """One audit line; *target* is resolved to its ``ps -o comm=`` (a group: its leader's)."""
    signalled = channel != "log_close" and target not in (0, -1)
    comm = _ps_field(abs(target), "comm") if signalled else None
    record: dict[str, object] = {
        "ts": _now(),
        "nodeid": nodeid,
        "channel": channel,
        "pid": target,
        "sig": sig,
        "target": comm or "",
        "verdict": verdict,
    }
    record.update(extra)
    return record


# --------------------------------------------------------------------------- #
# The Popen guard (tp#1793 B2): a real ``osascript`` drives iTerm (close a tab, type into
# one), ``tmux kill-pane`` / ``kill`` signal, the live installed ``ccc`` acts on real
# sessions. Each refusal writes a ``refused`` audit line, then raises ``AssertionError``.
# --------------------------------------------------------------------------- #
_AUDIT_CHANNELS.extend(f"popen:{name}" for name in _POPEN_GUARDED)


def _is_own_ccc(program: str, basetemp: Path | None) -> bool:
    """True for a ``ccc`` that is this checkout's code, not the live install.

    That is the test interpreter's own console script, or one a test built under the pytest
    *basetemp* (``test_packaging`` installs the wheel into a tmp venv; a child guard passes
    the temp dir instead). A bare ``ccc`` is never trusted: ``PATH`` resolves it to the live
    install.
    """
    if not os.path.isabs(program):
        return False
    real = Path(os.path.realpath(program))
    return str(real) == _OWN_CCC or (basetemp is not None and basetemp in real.parents)


def _popen_argv(args: object, executable: object, shell: object) -> list[str]:
    """The argv ``Popen._execute_child`` is about to run (its own POSIX normalisation).

    Under ``shell=True`` the shell itself is argv[0]; the program judged is then the
    first word of the command string.
    """
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(args)]
    else:
        argv = [
            os.fsdecode(arg) if isinstance(arg, (bytes, os.PathLike)) else str(arg)
            for arg in args  # type: ignore[attr-defined]
        ]
    if shell and argv:
        try:
            words = shlex.split(argv[0])
        except ValueError:
            words = argv[0].split()
        argv = [*words, *argv[1:]]
    elif isinstance(executable, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(executable), *argv[1:]]
    return argv


def _refusal(nodeid: str, channel: str, **extra: object) -> dict[str, object]:
    """One ``refused`` audit line (no pid: nothing was started)."""
    record: dict[str, object] = {
        "ts": _now(),
        "nodeid": nodeid,
        "channel": channel,
        "pid": None,
        "sig": None,
        "target": "",
        "verdict": "refused",
    }
    record.update(extra)
    return record


def _popen_check(
    nodeid: str,
    call: tuple[object, object, tuple[object, ...], dict[str, object]],
    basetemp: Path | None,
    opted_out: frozenset[str],
) -> None:
    """Refuse (audit line, then ``AssertionError``) a guarded binary about to be exec'd.

    *call* is ``(args, executable, rest, kwargs)`` of ``Popen._execute_child``.
    """
    args, executable, rest, kwargs = call
    shell = rest[_SHELL_POS] if len(rest) > _SHELL_POS else kwargs.get("shell", False)
    argv = _popen_argv(args, executable, shell)
    name = os.path.basename(argv[0]) if argv else ""
    own_ccc = name == "ccc" and _is_own_ccc(argv[0], basetemp)
    if name in _POPEN_GUARDED and name not in opted_out and not own_ccc:
        kept = [arg[:_ARG_CHARS] for arg in argv[:_ARGV_KEEP]]
        _audit_write(_refusal(nodeid, f"popen:{name}", target=name, argv=kept))
        raise AssertionError(
            f"a test ran the real {name} ({argv[0]}) through subprocess.Popen; fake its "
            f"caller (opt-out: @pytest.mark.{_POPEN_GUARDED[name]})"
        )


# --------------------------------------------------------------------------- #
# The subprocess-child guard (tp#1793 B3), installed by ``sitecustomize``.
# --------------------------------------------------------------------------- #
_AUDIT_CHANNELS.append(_CHILD_CHANNEL)


def _child_nodeid() -> str:
    """The running test, from the ``PYTEST_CURRENT_TEST`` the child inherited."""
    current = os.environ.get("PYTEST_CURRENT_TEST", "")
    return current.rsplit(" (", 1)[0] if current else "child"


def _child_kill(pid: int, sig: int, /) -> None:
    _guarded_signal(_child_nodeid(), "os.kill", pid, sig)


def _child_killpg(pgid: int, sig: int, /) -> None:
    _guarded_signal(_child_nodeid(), "os.killpg", pgid, sig)


def _child_execute_child(
    self: subprocess.Popen, args: object, executable: object, *rest: object, **kwargs: object
) -> None:
    _popen_check(_child_nodeid(), (args, executable, rest, kwargs), _State.built_root, frozenset())
    _record_popen_child(self, args, executable, *rest, **kwargs)


def install_child_guard() -> bool:
    """Install the kill and Popen guards in this (child) process; ``False`` when inactive.

    Active only when ``$CCC_TEST_KILL_AUDIT`` is set (conftest pins it, with the pytest pid
    as ``$CCC_TEST_ROOT_PID``, the descendant root). No opt-out markers reach a child.
    """
    if not os.environ.get(_AUDIT_ENV):
        return False
    raw_root = os.environ.get(_ROOT_ENV, "")
    if raw_root.isdigit():
        _State.root_pid = int(raw_root)
    _State.child = True
    _State.built_root = Path(tempfile.gettempdir()).resolve()
    os.kill = _child_kill
    os.killpg = _child_killpg
    subprocess.Popen._execute_child = _child_execute_child  # type: ignore[attr-defined]  # pylint: disable=no-member
    return True
