#!/usr/bin/env python3
"""Run a user-configured shell predicate; exit 0 means the check passed.

Shared by the session-level done-check (`done_check_cmd`) and the per-sub-goal
machine-check predicates. The command is user-authored (same trust model as
`done_check_cmd`) — auto-derived sub-goals never get one. Never raises.

:func:`run_structured` is the bounded twin the ``ccc await`` probes use: argv by
default (``shell=True`` only for the user-authored ``cmd`` source), stdout and stderr
capped at *max_bytes*, the whole process GROUP killed at the timeout, stderr sanitized.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import dataclasses
import os
import re
import signal
import subprocess
import threading
from typing import IO


def run_exit0(command: str, cwd: str | None = None, timeout: int = 30) -> bool:
    """Return True iff *command* (run via the shell in *cwd*) exits 0.

    Output is captured/discarded; a timeout, non-zero exit, or spawn error all
    read as "not satisfied" (False) so callers degrade gracefully.
    """
    try:
        result = subprocess.run(  # noqa: S602  # user-configured command, intentional
            command,
            shell=True,
            cwd=cwd or None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


#: Bound on the sanitized stderr kept in a probe result (and so in ``last_error``).
STDERR_KEEP = 400

# Token-shaped runs that must never reach a notification or a DB row: Slack tokens,
# bearer headers, and any long opaque run (keys, OAuth tokens).
_SECRETISH = re.compile(
    r"xox[a-z]-[A-Za-z0-9-]+|(?i:bearer)\s+\S+|(?i:zoho-oauthtoken)\s+\S+|[A-Za-z0-9_+/=-]{32,}"
)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclasses.dataclass(frozen=True)
class StructuredResult:
    """What :func:`run_structured` observed. ``exit`` is ``None`` when no exit code exists."""

    exit: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_error: str = ""
    truncated: bool = False


def sanitize_error(text: str, limit: int = STDERR_KEEP) -> str:
    """One bounded line of *text* with controls stripped and token-shaped runs redacted."""
    line = _CONTROLS.sub(" ", text or "")
    line = _SECRETISH.sub("[redacted]", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line[:limit]


def _drain(stream: IO[bytes], sink: list[bytes], cap: int, flags: list[bool]) -> None:
    """Keep the first *cap* bytes of *stream*, read (and drop) the rest until EOF."""
    kept = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        if kept < cap:
            take = chunk[: cap - kept]
            sink.append(take)
            kept += len(take)
            if len(take) < len(chunk):
                flags[0] = True
        else:
            flags[0] = True


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def run_structured(  # pylint: disable=too-many-arguments
    argv: list[str] | str,
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
    max_bytes: int = 256 * 1024,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> StructuredResult:
    """Run *argv* bounded in time and output; never raises.

    The child gets its own session (process group), so a timeout kills everything it
    spawned, not just the leader. stdin is ``/dev/null`` — a probe must never wait on
    a terminal. stdout is decoded as UTF-8 (replace); stderr comes back sanitized
    (:func:`sanitize_error`), because it ends up in ``last_error`` and notifications.
    """
    if shell != isinstance(argv, str):
        return StructuredResult(exit=None, spawn_error="shell=True needs a string, argv a list")
    try:
        proc = subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
            argv,  # the with-block below owns it; Popen can raise before one exists
            shell=shell,  # noqa: S604  # argv, or the user-authored cmd source
            cwd=cwd or None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return StructuredResult(
            exit=None, spawn_error=sanitize_error(f"{type(exc).__name__}: {exc}")
        )
    out: list[bytes] = []
    err: list[bytes] = []
    out_flag = [False]
    err_flag = [False]
    timed_out = False
    with proc:
        assert proc.stdout is not None and proc.stderr is not None  # PIPE above
        readers = [
            threading.Thread(
                target=_drain, args=(proc.stdout, out, max_bytes, out_flag), daemon=True
            ),
            threading.Thread(
                target=_drain, args=(proc.stderr, err, max_bytes, err_flag), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        for reader in readers:
            reader.join(timeout=5)
    stdout = b"".join(out).decode("utf-8", errors="replace")
    stderr = sanitize_error(b"".join(err).decode("utf-8", errors="replace"))
    return StructuredResult(
        exit=None if timed_out else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=out_flag[0] or err_flag[0],
    )
