#!/usr/bin/env python3
# pylint: disable=invalid-name  # filename intentionally hyphenated (matches codex-review.py)
"""codex-in-claude.py — pick the OpenAI Codex model for Claude Code's codex commands,
and run one delegated task round (engine behind /codex-implement-task-and-claude-review).

This single script is the shared control point for every Codex-related Claude Code
command (``/codex-implement-task-and-claude-review`` and ``/codex-debate``):

* ``models``      — list the Codex models available on this login.
* ``get-model``   — print the model resolved for a given command.
* ``set-model``   — set the model for a command (or the global default).
* ``alias``       — list/define/delete short model names (``astra`` -> ``gpt-6-astra``).
* ``pick``        — interactive numbered picker for the model (terminal only).
* ``sync-skills`` — stamp the resolved model into the ``description:`` frontmatter of the
                    codex skills/commands, so Claude Code's ``/codex…`` help shows it.
* ``delegate``    — run ONE Codex round, **printing the model as the first stdout line**,
                    then Codex's reply. Used by the codex-implement-task-and-claude-review skill.
* ``run``         — the versioned MACHINE entry point (``-j`` envelope, ``schema_version 1``):
                    one read-only round with seat fallback, for external consumers that
                    would otherwise call ``codex exec`` themselves.
* ``order``       — show/set the ORDER every Codex consumer tries the ChatGPT logins in.
* ``usage``       — print the current Codex rate-limit usage (5h + weekly windows).
* ``headroom``    — decide whether enough quota remains for optional offload work.

Model source: ``codex debug models`` (``--refresh``), else the offline cache
``~/.codex/models_cache.json`` (fast / wifi-friendly default). Only models with
``visibility == "list"`` are shown unless ``--include-hidden``.

Config (JSON, atomic writes)::

    ~/.config/codex-in-claude/config.json        # override via $CODEX_IN_CLAUDE_CONFIG
    {"default": "gpt-5.6-sol", "delegate-review": null, "debate": null,
     "aliases": {"astra": "gpt-6-astra"}}

Resolution order for a command: per-command value -> ``default`` -> ``gpt-5.6-sol``.

Every place that takes a model (``set-model``, ``get-model NAME``, ``delegate -m``,
``run -m`` — and through it ``codex-review.py -m``, i.e. ``/codex-debate <name>``) accepts
a SHORT NAME as well as a slug: the catalog's own codenames are built in (``sol`` ->
``gpt-5.6-sol``, ``astra`` -> ``gpt-6-astra``: the trailing alphabetic segment of a slug,
dropped when two slugs share it) and the config's ``aliases`` map wins over them.

``set-model`` / ``set-effort`` / ``pick`` also re-stamp the ``[codex <model> effort=<e>]``
marker into the codex skill/command descriptions (see ``sync-skills``), so the model in
Claude Code's slash-command help never drifts from the config.

``delegate`` exit codes (the skill branches on these):
  0 ok | 2 usage | 3 invalid-model | 4 codex-missing-or-auth | 5 timeout-or-stall |
  6 codex-nonzero | 7 bad-patch (reserved) | 8 quota-exhausted (skipped, see below) |
  9 network-dead-or-machine-slept (killed after codex stopped making progress).

Launch policy: every ``codex exec`` line is assembled by
:mod:`command_center.codex_launch` — the named ``hardened-ro``/``hardened-rw`` permission
profile (NEVER ``-s/--sandbox``, which forces the legacy sandbox and drops the profile's deny
rules), a validated absolute ``-C`` root (``$HOME`` and its ancestors refused; an implicit cwd
only inside a git work tree), and a 0600 session journal that ``--resume`` re-validates against.
A refused launch exits ``EX_USAGE`` (2) with the reason.

Seat policy: every ``codex exec`` is STARTED by :func:`run_with_fallback`, which tries the
configured ChatGPT logins in ``codex_seat_order`` (see ``order``) and falls through at run
time — a seat that refuses for quota/entitlement/auth reasons is blocked in ccc's cooldown
store and the next one is tried, with the argv rebuilt for THAT home. A task failure, a
timeout, a stall, and a write-mode refusal that already touched the worktree are terminal.
Zero eligible seats start no process at all. Classification reads only the ``--json``
``error``/``turn.failed`` events (never item text, never the prompt), else the stderr tail.

Supervision: ``codex exec`` runs in its own process group and the WHOLE tree is killed on
wall timeout, idle stall, or parent SIGTERM/SIGINT — a killed delegate can never leave
codex editing the workspace behind the caller's back. The wall timeout defaults by effort
(``DEFAULT_TIMEOUTS``: low 600 s .. xhigh 2700 s; ``-t`` overrides, ``-t 0`` = no wall at
all — the recommended mode when the task simply takes as long as it takes), a PROGRESS
watchdog converts hangs into fast failures (``-i``, default 900 s without a non-error codex
event; 120 s once codex itself reports network trouble, 180 s after the machine was
suspended, 240 s for the first model response after launch — codex 0.152.1 otherwise loops
"Reconnecting... waiting for network" forever, and its error lines used to keep the old
any-output watchdog alive), a ``caffeinate -i`` assertion held for exactly the run keeps the
laptop from idle-sleeping under it (the 2026-09-07 seven-hour hang), codex stderr is
streamed through (``codex› `` prefix) for live progress, and the prompt itself tells codex
its time budget so it spends the clock implementing instead of exploring.

Watchability + rounds: every run refreshes a heartbeat JSON under ``_runs_dir()``; ``runs``
lists all in-flight delegates (elapsed, idle seconds, output volume, last line) from one
file read each — the cheap way to check on a long run without reading transcripts. Each
run reports its codex session UUID as a final ``### SESSION`` line (also included in
timeout/stall errors); ``--resume <uuid>`` re-attaches the next round (or a retry after a
kill) to that session with all its discovered context intact. Unless ``--no-repo-map`` is
given, the prompt is prefixed with the repo's ``repo_scope_short.md`` (or a git top-level
summary) so codex starts oriented instead of exploring from zero.

Concurrency + quota awareness: each ``delegate`` first runs a **quota preflight** — it reads
the Codex ``rate_limits`` (5h + weekly) from ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` and,
if a live window is ``>=100%`` used, prints the reset time and exits ``EX_QUOTA`` (8) WITHOUT
launching codex (bypass: ``-Q`` / ``$CODEX_IN_CLAUDE_IGNORE_QUOTA``). It then takes one of N
cross-process flock slots, where the effective cap is **usage-tapered**: <50% used -> 3,
50-75% -> 2, >75% -> 1 (N ceiling = ``-j/--max-concurrent`` -> ``$CODEX_IN_CLAUDE_MAX_CONCURRENT``
-> 3; ``0`` = unlimited). Stale windows (reset already passed) are ignored; unknown usage fails
open. Keeps a fan-out from thrashing CPU/API and from slamming the wall with many in-flight runs.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from . import brokenpipe, codex_launch, codex_ledger

if TYPE_CHECKING:  # pragma: no cover - runtime import is local: quota imports THIS module
    from . import quota, seat_rota

DEFAULT_MODEL = "gpt-5.6-sol"  # newest/best per the Codex catalog
COMMANDS = ("delegate-review", "debate")  # codex-related commands this manager governs
EFFORTS = ("low", "medium", "high", "xhigh")  # codex reasoning levels (API-validated)


def _codex_cache() -> Path:
    """Codex's local models cache, resolved per call (``Path.home()`` may be patched)."""
    return Path.home() / ".codex" / "models_cache.json"


# Concurrency cap: at most this many `delegate` processes run `codex exec` at once.
DEFAULT_MAX_CONCURRENT = 3


def _slot_dir() -> Path:
    """Concurrency-slot lock dir: ``$CODEX_IN_CLAUDE_SLOT_DIR``, read per call (tp#192)."""
    return Path(
        os.environ.get(
            "CODEX_IN_CLAUDE_SLOT_DIR",
            str(Path.home() / ".config" / "codex-in-claude" / "slots"),
        )
    )


# Default wall timeout for one delegate round, keyed by reasoning effort. Higher
# effort reasons (and therefore explores) far longer: a fixed 600s default made
# every xhigh run on a non-trivial repo time out during discovery.
# ``-t 0`` disables the wall entirely (the idle watchdog still guards stalls).
DEFAULT_TIMEOUTS = {"low": 600, "medium": 900, "high": 1500, "xhigh": 2700}
# Kill a run after this long with NO PROGRESS (network hang / wedged CLI), clamped to the
# wall timeout. 0 disables every idle-based kill. Progress = a non-error codex event, or a
# stderr line that is not an ERROR/WARN log line — see :func:`classify_output_line`.
DEFAULT_IDLE_TIMEOUT = 900
# Progress-aware supervision (2026-09-08, after the seven-hour debate hang). Codex 0.152.1
# never gives up on a dead connection: after five reconnects it loops
# ``{"type":"error","message":"Reconnecting... waiting for network …"}`` on stdout and
# ``ERROR codex_models_manager`` lines on stderr FOREVER. The old watchdog counted every
# such line as activity, so it never fired. Now only PROGRESS resets it, and the allowance
# shrinks the moment codex itself reports network trouble, or after the machine slept.
NET_IDLE_TIMEOUT = 120  # $CODEX_IN_CLAUDE_NET_IDLE (0 = keep the plain idle allowance)
POST_SLEEP_IDLE_TIMEOUT = 180  # $CODEX_IN_CLAUDE_POST_SLEEP_IDLE (0 = plain allowance)
# No non-error ``item.*`` event this many AWAKE seconds after launch = codex never got a
# model response. ``thread.started`` / ``turn.started`` are emitted locally and prove
# nothing: the no-egress reproduction printed both, then looped on errors.
DEFAULT_STARTUP_TIMEOUT = 240  # $CODEX_IN_CLAUDE_STARTUP_TIMEOUT (0 disables)
# A 1 s supervision tick that took longer than this = the machine was suspended (lid
# closed, or idle sleep where no assertion could be held). Only AWAKE time is charged to
# the idle/startup allowances, and each tick is credited at most TICK_CAP_S of it.
SLEEP_GAP_S = 30.0
TICK_CAP_S = 5.0


# Heartbeat files for in-flight delegate runs (see ``runs``): one small JSON per
# running delegate, refreshed every few seconds. Lets a caller check progress
# cheaply (one file read) instead of tailing full transcripts. On exit the file is
# NOT removed: one retained final record (``ended`` set, ``child_state`` "gone") stays
# behind so a reader that saw the run alive can tell normal completion from a crash;
# ``_prune_ended_heartbeats`` (and the statusline's ``cc-heartbeat.py gc``) drop it
# after ``HEARTBEAT_ENDED_RETENTION_S``. The dir is resolved per call, never at import:
# an import-time binding ignored a test's ``$CODEX_IN_CLAUDE_RUNS_DIR`` (tp#192).
def _runs_dir() -> Path:
    """Heartbeat dir: ``$CODEX_IN_CLAUDE_RUNS_DIR``, else ``~/.config/codex-in-claude/runs``."""
    return Path(
        os.environ.get(
            "CODEX_IN_CLAUDE_RUNS_DIR",
            str(Path.home() / ".config" / "codex-in-claude" / "runs"),
        )
    )


# Heartbeat contract v1 — the cross-tool keys the Claude Code statusline reader
# (``cc-waiting.py`` in the user's dotfiles) consumes from every heartbeat writer (this runner
# and ``cc-heartbeat.py`` for headless claude children). ``proc_start`` is the runner's
# own ``ps -o lstart=`` token, fetched under a pinned TZ/locale so writer and reader
# compare identical strings — the process identity a stale file can never fake.
HEARTBEAT_SCHEMA_VERSION = 1
HEARTBEAT_INTERVAL_S = 5  # the loop writes on every ``HEARTBEAT_INTERVAL_S``-th 1 s tick
HEARTBEAT_ENDED_RETENTION_S = 600
_PS_IDENTITY_ENV = {"TZ": "UTC", "LC_ALL": "C"}
_LSTART_RE = re.compile(r"^[A-Za-z]{3} [A-Za-z]{3} [ \d]\d \d\d:\d\d:\d\d \d{4}$")


def _heartbeat_writer() -> str:
    """``codex-in-claude/<version>`` — which writer produced a heartbeat record."""
    try:
        from importlib.metadata import version  # pylint: disable=import-outside-toplevel

        return f"codex-in-claude/{version('claude-command-center')}"
    except Exception:  # noqa: BLE001  # pragma: no cover - metadata missing in odd installs
        return "codex-in-claude/0"


def _lstart_of(pid: int) -> str | None:
    """The exact ``ps -o lstart=`` start token of *pid* under ``TZ=UTC LC_ALL=C``.

    Whitespace-normalised (``Thu Sep 10 11:58:50 2026``); ``None`` when ps fails, times
    out (1 s) or prints something that is not a start token.
    """
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
            env={**os.environ, **_PS_IDENTITY_ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = " ".join(proc.stdout.split())
    return token if _LSTART_RE.match(token) else None


@dataclass(frozen=True)
class HeartbeatIdentity:
    """Who a heartbeat is about: the runner and the ``codex exec`` child it supervises."""

    started: int  # epoch of this attempt's start
    proc_start: str | None  # the runner's own lstart token
    child_pid: int | None
    child_proc_start: str | None


def _secure_heartbeat_dir(path: Path) -> None:
    """Create the heartbeat directory 0700 and tighten anything already in it (once)."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    for entry in path.glob("*.json"):
        with contextlib.suppress(OSError):
            os.chmod(entry, 0o600)


def _prune_ended_heartbeats(runs_dir: Path, now: float | None = None) -> None:
    """Drop retained final records older than ``HEARTBEAT_ENDED_RETENTION_S`` (never fatal)."""
    now = time.time() if now is None else now
    try:
        entries = sorted(runs_dir.glob("*.json"))[:64]
    except OSError:
        return
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ended = data.get("ended") if isinstance(data, dict) else None
        if isinstance(ended, (int, float)) and now - ended > HEARTBEAT_ENDED_RETENTION_S:
            with contextlib.suppress(OSError):
                entry.unlink()


# codex exec prints its session id in the startup banner; captured so a later
# round (or a retry after a kill) can `codex exec resume <id>` with the
# session's discovered context intact.
_SESSION_RE = re.compile(
    r"session id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

# Less wall-clock than this left in the whole call's budget is not worth a launch: the
# run would be killed during codex's own startup and buy nothing but a cost row.
_MIN_ATTEMPT_SECONDS = 5

# ``build_cmd(candidate, out_path, permission_args, mcp_disable_args) -> argv``. Each
# consumer of :func:`run_with_fallback` supplies one; the runner calls it AGAIN for every
# seat, so per-home arguments (the permission profile, the MCP servers that home
# declares) can never be carried over from the seat that just refused.
BuildCmd = Callable[["SeatCandidate", str, list[str], list[str]], list[str]]

# delegate exit codes
EX_OK, EX_USAGE = 0, 2
EX_INVALID_MODEL, EX_NO_CODEX, EX_TIMEOUT, EX_CODEX_FAIL, EX_BAD_PATCH = 3, 4, 5, 6, 7
EX_QUOTA = 8  # skipped: Codex quota exhausted (>=100% used on a live window)
EX_NETWORK = 9  # killed: codex reported a dead network / the machine slept; no progress


def _effective_timeout(explicit: int | None, effort: str) -> int:
    """Wall timeout for a round: explicit ``-t`` (0 = unlimited), else effort-scaled."""
    if explicit is not None:
        return max(0, explicit)
    return DEFAULT_TIMEOUTS.get(effort, DEFAULT_TIMEOUTS["medium"])


def _session_id_of(text: str) -> str | None:
    """The codex session UUID from a run's stderr banner, if present."""
    match = _SESSION_RE.search(text)
    return match.group(1) if match else None


def _write_heartbeat(  # pylint: disable=too-many-positional-arguments,too-many-locals
    path: Path,
    meta: dict[str, Any],
    started: float,
    last_activity: float,
    out_buf: list[str],
    err_buf: list[str],
    codex_pgid: int | None = None,
    extra: dict[str, Any] | None = None,
    identity: HeartbeatIdentity | None = None,
) -> None:
    """Atomically refresh one run's heartbeat JSON (never fatal).

    ``runner_pid`` + ``codex_pgid`` are the LAST-RESORT kill contract for a consumer
    whose own outer timeout fires while this runner is still supervising codex: SIGTERM
    the runner (it relays to the group), wait a few seconds, and if the group is still
    alive ``os.killpg(codex_pgid, SIGKILL)`` it directly. Without the pgid a consumer
    can only kill the runner, and codex's own process group — a nested group, because
    the runner starts it with ``start_new_session`` — outlives it.

    The contract-v1 keys (``schema_version`` … ``progress_at``) are what the statusline
    reader joins on; ``extra`` may override any of them (the loop passes the sleep-aware
    ``idle_s``, the real ``progress_at``, ``child_state`` and, on exit, ``ended``). The
    file is 0600 in a 0700 directory: it names the repo, the seat and the last output line.
    """
    now = time.monotonic()
    wall = int(time.time())
    last_line = next((line.strip() for line in reversed(err_buf or out_buf) if line.strip()), "")
    ident = identity or HeartbeatIdentity(int(wall - (now - started)), None, None, None)
    payload = {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "writer": _heartbeat_writer(),
        "tool": "codex",
        "pid": os.getpid(),
        "runner_pid": os.getpid(),
        "proc_start": ident.proc_start,
        "codex_pgid": codex_pgid,
        "child_pid": ident.child_pid,
        "child_proc_start": ident.child_proc_start,
        "child_state": "running",
        **meta,
        "account": meta.get("seat"),
        "model_source": "requested",
        "interval_s": HEARTBEAT_INTERVAL_S,
        "started": ident.started,
        "elapsed_s": int(now - started),
        "idle_s": int(now - last_activity),
        "progress_at": ident.started,
        "lines": len(out_buf) + len(err_buf),
        "last_line": last_line[:200],
        "updated": wall,
        "next_due": wall + HEARTBEAT_INTERVAL_S,
        **(extra or {}),  # sleep-aware idle_s, progress_at, child_state, slept_s, trouble, ended
    }
    try:
        _secure_heartbeat_dir(path.parent)
        tmp = path.with_suffix(".tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()  # a crashed writer's leftover would make O_EXCL refuse
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        tmp.replace(path)
    except OSError:
        pass


class CodexStalledError(RuntimeError):
    """Codex stopped making progress and the supervising loop killed it.

    ``reason`` names the allowance that ran out: ``"stalled"`` (plain silence),
    ``"network"`` (codex itself kept reporting connection trouble), ``"slept"`` (the
    machine was suspended since the last progress) or ``"startup"`` (no model response
    since launch). ``idle_seconds`` are AWAKE seconds — time spent suspended is excluded.
    """

    def __init__(  # pylint: disable=too-many-positional-arguments
        self,
        idle_seconds: int,
        stderr_text: str,
        *,
        reason: str = "stalled",
        stdout_text: str = "",
        slept_seconds: int = 0,
        last_trouble: str = "",
    ) -> None:
        super().__init__(f"no codex progress for {idle_seconds}s ({reason})")
        self.idle_seconds = idle_seconds
        self.stderr_text = stderr_text
        self.reason = reason
        self.stdout_text = stdout_text
        self.slept_seconds = slept_seconds
        self.last_trouble = last_trouble


def _env_seconds(name: str, default: int) -> int:
    """``$name`` as non-negative whole seconds; *default* when unset, empty or garbage."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


# Rust ``tracing`` log lines codex writes to stderr: ``2026-09-08T06:50:08.463644Z ERROR …``.
_LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s+(?:ERROR|WARN)\b")
# Wording codex uses for a dead / flapping connection (stdout error events AND stderr logs).
_NET_TROUBLE_RE = re.compile(
    r"waiting for network|connection failed|error sending request|connection refused|"
    r"failed to refresh available models|failed to connect to websocket|"
    r"stream disconnected|reconnecting|network is unreachable|dns error|timed out",
    re.IGNORECASE,
)
# Event types that prove the MODEL answered. A locally emitted thread/turn start does not.
_WORK_EVENT_PREFIXES = ("item.", "turn.completed", "turn.failed")


@dataclass(frozen=True)
class LineVerdict:
    """What one line of codex output says about the run's health."""

    progress: bool  # resets the idle watchdog
    work: bool  # proves codex got a model response (ends the startup window)
    network: bool  # a trouble line that names the network


def classify_output_line(line: str, *, stderr: bool) -> LineVerdict:
    """Progress, or trouble? — the watchdog's only input (see the module constants).

    stdout is ``codex exec --json``: an ``error`` event, or an item of type ``error``
    (e.g. "Falling back from WebSockets to HTTPS transport"), is TROUBLE; every other
    event is progress, and ``item.*`` / ``turn.completed`` / ``turn.failed`` also count
    as *work*. A non-JSON stdout line is unknown output and counts as progress (the
    conservative choice: never kill on something we do not understand). stderr: an
    ``ERROR``/``WARN`` log line is trouble, anything else ("Reading additional input
    from stdin...") is progress. A blank line is neither.
    """
    text = line.strip()
    if not text:
        return LineVerdict(progress=False, work=False, network=False)
    if stderr:
        trouble = bool(_LOG_LINE_RE.match(text))
        return LineVerdict(not trouble, False, trouble and bool(_NET_TROUBLE_RE.search(text)))
    event = _json_event(text)
    if event is None:
        return LineVerdict(True, False, False)  # unknown output: never a reason to kill
    kind = str(event.get("type") or "")
    item = event.get("item")
    item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
    if kind == "error" or item_type == "error":
        return LineVerdict(False, False, bool(_NET_TROUBLE_RE.search(text)))
    return LineVerdict(True, kind.startswith(_WORK_EVENT_PREFIXES), False)


def _json_event(text: str) -> dict | None:
    """*text* as one ``codex exec --json`` event object, or ``None`` for anything else."""
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


@dataclass(frozen=True)
class _HealthSnapshot:
    progress: int  # progress lines so far
    trouble: int  # trouble lines since the last progress line
    network: int  # of those, the ones naming the network
    last_trouble: str
    started_work: bool


class _RunHealth:
    """What the two reader threads learned about the run, polled by the supervisor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._progress = 0
        self._trouble = 0
        self._network = 0
        self._last_trouble = ""
        self._started_work = False

    def note(self, line: str, *, stderr: bool) -> None:
        """Account one output line (called from the reader threads)."""
        verdict = classify_output_line(line, stderr=stderr)
        with self._lock:
            if verdict.progress:
                self._progress += 1
                self._trouble = 0
                self._network = 0
                self._started_work = self._started_work or verdict.work
            elif line.strip():
                self._trouble += 1
                self._network += int(verdict.network)
                self._last_trouble = line.strip()[:300]

    def snapshot(self) -> _HealthSnapshot:
        """A consistent read of the counters."""
        with self._lock:
            return _HealthSnapshot(
                self._progress,
                self._trouble,
                self._network,
                self._last_trouble,
                self._started_work,
            )


# Injectable wall clock, so a test can simulate a suspended machine (a jump between ticks).
_wall_clock: Callable[[], float] = time.time


def _hold_awake(codex_pid: int) -> subprocess.Popen[bytes] | None:
    """``caffeinate -i -w <codex pid>``: no idle sleep while codex runs (macOS only).

    The 2026-09-07 debate hang: this laptop idle-sleeps after ONE minute (``pmset -g
    custom`` → ``sleep 1``), so an unattended round was suspended seven minutes after
    launch and spent the night in Sleep/DarkWake cycles. ``-i`` blocks exactly that —
    idle sleep, on AC and battery alike — and nothing else: no display, no user-active
    assertion. ``-w`` ties the assertion to codex's lifetime and :func:`_release_awake`
    ends it on every exit path of the supervisor, so it cannot outlive the round. Other
    ``caffeinate`` holders on the machine are neither reused nor touched. Opt out with
    ``$CODEX_IN_CLAUDE_NO_CAFFEINATE=1``; a platform without ``caffeinate`` gets none.
    A closed lid still sleeps the machine — that case is the ``slept`` watchdog's job.
    """
    if os.environ.get("CODEX_IN_CLAUDE_NO_CAFFEINATE"):
        return None
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    try:
        return subprocess.Popen(  # pylint: disable=consider-using-with  # reaped by _release_awake
            [exe, "-i", "-w", str(codex_pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _release_awake(holder: subprocess.Popen[bytes] | None) -> None:
    """End our own caffeinate assertion (SIGTERM, 2 s, SIGKILL); an exited holder is fine."""
    if holder is None or holder.poll() is not None:
        return
    with contextlib.suppress(OSError):
        holder.terminate()
    try:
        holder.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            holder.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            holder.wait(timeout=1)


def _group_is_gone(pgid: int) -> bool:
    """True when NO process is left in group *pgid* (a probe, never a kill)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - a foreign group still exists
        return False
    return False


def _exec_codex(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    idle_timeout: int = 0,
    heartbeat_path: Path | None = None,
    heartbeat_meta: dict[str, Any] | None = None,
    stdin_text: str | None = None,
    startup_timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``codex exec`` supervised; the process TREE can never outlive us.

    Differences from a bare ``subprocess.run``:

    - the child gets its own process group (``start_new_session``) and the whole
      group is SWEPT (SIGTERM, up to 2 s grace, SIGKILL) on wall timeout, idle stall,
      parent SIGTERM/SIGINT, normal completion, or any exception — a killed delegate
      previously left ``codex exec`` (and its shell-tool children) running detached,
      still editing the workspace in ``--write`` mode. The sweep runs even when the
      group LEADER has already exited: an early-exiting codex that forked a background
      child used to leave that grandchild alive, because the old guard returned as soon
      as ``proc.poll()`` was non-None. It costs nothing on the happy path — an empty
      group answers ``ProcessLookupError`` immediately;
    - the prompt travels on **stdin** (*stdin_text*, argv ending in ``-``): a 300 kB
      prompt does not fit in ``ARG_MAX``, and a prompt in argv is visible to every
      ``ps`` on the machine;
    - codex's stderr is streamed line-by-line to our stderr (prefixed
      ``codex› ``) so a caller watching the output file can SEE whether codex is
      exploring, editing, or hung — instead of total silence until the end;
    - a ``caffeinate -i`` assertion is held for exactly as long as codex runs (macOS), so
      the laptop's idle sleep cannot suspend the round (:func:`_hold_awake`);
    - a PROGRESS watchdog converts a wedged run into a fast, diagnosable failure. It
      charges AWAKE seconds since the last progress line (:func:`classify_output_line`:
      codex's own ``error`` events and ``ERROR``/``WARN`` log lines are trouble, not
      progress — codex 0.152.1 loops "Reconnecting... waiting for network" forever on a
      dead connection, and those lines kept the old any-output watchdog alive for
      hours). The allowance is *idle_timeout*, shrunk to ``NET_IDLE_TIMEOUT`` once codex
      reports network trouble and to ``POST_SLEEP_IDLE_TIMEOUT`` once a supervision
      tick took longer than ``SLEEP_GAP_S`` (the machine was suspended); *startup_timeout*
      (default ``$CODEX_IN_CLAUDE_STARTUP_TIMEOUT`` / ``DEFAULT_STARTUP_TIMEOUT``) awake
      seconds without any ``item.*`` event means codex never got a model response.

    Raises FileNotFoundError (binary missing), subprocess.TimeoutExpired (wall), or
    CodexStalledError (reason ``stalled`` / ``network`` / ``slept`` / ``startup``) — the
    partial output is attached to the last two.
    """
    proc = subprocess.Popen(  # pylint: disable=consider-using-with  # lifetime managed by the finally sweep
        cmd,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        codex_pgid: int | None = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - raced its own exit
        codex_pgid = proc.pid
    awake_holder = _hold_awake(proc.pid)
    out_buf: list[str] = []
    err_buf: list[str] = []
    health = _RunHealth()

    def feed_stdin() -> None:
        """Hand codex the prompt, then close stdin (a broken pipe is not our problem)."""
        if stdin_text is None or proc.stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()

    def reader(stream: Any, buf: list[str], stderr: bool) -> None:
        for line in stream:
            buf.append(line)
            health.note(line, stderr=stderr)
            if stderr:
                shown = line if len(line) <= 400 else line[:400] + "…\n"
                sys.stderr.write("codex› " + shown)
                sys.stderr.flush()
        stream.close()

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, out_buf, False), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, err_buf, True), daemon=True),
        threading.Thread(target=feed_stdin, daemon=True),
    ]
    for thread in threads:
        thread.start()

    def kill_group() -> None:
        """Sweep codex's whole process group — ALWAYS, leader alive or not."""
        if codex_pgid is None:  # pragma: no cover - set unconditionally above
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(codex_pgid, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while True:
            if proc.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=0.05)
            if _group_is_gone(codex_pgid):
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(codex_pgid, signal.SIGKILL)

    def stalled(reason: str, seconds: float, snap: _HealthSnapshot) -> CodexStalledError:
        return CodexStalledError(
            int(seconds),
            "".join(err_buf),
            reason=reason,
            stdout_text="".join(out_buf),
            slept_seconds=int(slept),
            last_trouble=snap.last_trouble,
        )

    previous: dict[int, Any] = {}

    def relay(signum: int, _frame: Any) -> None:
        kill_group()
        signal.signal(signum, previous[signum])  # restore, then re-deliver
        os.kill(os.getpid(), signum)

    # Signal handlers are only legal in the main thread; elsewhere the finally
    # sweep still covers every non-signal exit path.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, relay)
    if startup_timeout is None:
        startup_timeout = _env_seconds("CODEX_IN_CLAUDE_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT)
    net_idle = _env_seconds("CODEX_IN_CLAUDE_NET_IDLE", NET_IDLE_TIMEOUT)
    post_sleep_idle = _env_seconds("CODEX_IN_CLAUDE_POST_SLEEP_IDLE", POST_SLEEP_IDLE_TIMEOUT)
    start = time.monotonic()
    prev_wall = _wall_clock()
    awake_elapsed = awake_idle = slept = 0.0
    slept_since_progress = False
    seen_progress = 0
    tick = 0
    # Contract-v1 identity: fetched once per attempt (two ps calls) only when a
    # heartbeat is wanted; ``progress_at`` is the WALL clock of the last PROGRESS line —
    # trouble chatter and a suspended machine never move it (the reader trusts it).
    started_epoch = int(time.time())
    progress_at = started_epoch
    stamped_progress = 0  # progress lines already dated; `seen_progress` drives the watchdog
    identity: HeartbeatIdentity | None = None
    if heartbeat_path is not None:
        identity = HeartbeatIdentity(
            started=started_epoch,
            proc_start=_lstart_of(os.getpid()),
            child_pid=proc.pid,
            child_proc_start=_lstart_of(proc.pid),
        )
        _prune_ended_heartbeats(heartbeat_path.parent)

    def stamp_progress(snap: _HealthSnapshot) -> None:
        """Date a new PROGRESS line the moment it is OBSERVED, and only once.

        Every snapshot stamps -- the two in the loop and the final one taken after the
        reader threads join -- because a run can exit in the gap between any two of them
        and would otherwise inherit an older stamp. ``seen_progress`` cannot do this job:
        it belongs to the sleep-aware idle accounting at the foot of the loop, which is a
        tick behind, so a run that finished inside that tick was filed as never having
        made progress at all.
        """
        nonlocal progress_at, stamped_progress
        if snap.progress != stamped_progress:
            stamped_progress = snap.progress
            progress_at = int(time.time())

    def heartbeat_extra(snap: _HealthSnapshot, *, ended: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "idle_s": int(awake_idle),
            "progress_at": progress_at,
            "child_state": "gone" if ended or proc.poll() is not None else "running",
            "slept_s": int(slept),
            "trouble": snap.trouble,
            "caffeinate_pid": awake_holder.pid if awake_holder else None,
        }
        if ended:
            payload["ended"] = int(time.time())
        return payload

    try:
        while True:
            snap = health.snapshot()
            stamp_progress(snap)
            if heartbeat_path is not None and tick % HEARTBEAT_INTERVAL_S == 0:
                _write_heartbeat(
                    heartbeat_path,
                    heartbeat_meta or {},
                    start,
                    time.monotonic() - awake_idle,
                    out_buf,
                    err_buf,
                    codex_pgid,
                    extra=heartbeat_extra(snap),
                    identity=identity,
                )
            tick += 1
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            now_wall = _wall_clock()
            gap = max(0.0, now_wall - prev_wall)
            prev_wall = now_wall
            if gap > SLEEP_GAP_S:
                slept += gap
                slept_since_progress = True
                sys.stderr.write(
                    f"runner› the machine was suspended for ~{int(gap)}s — codex's connections "
                    "may be dead; watching for progress\n"
                )
                sys.stderr.flush()
            awake_gap = min(gap, TICK_CAP_S)
            awake_elapsed += awake_gap
            snap = health.snapshot()
            stamp_progress(snap)
            if snap.progress != seen_progress:
                seen_progress, awake_idle, slept_since_progress = snap.progress, 0.0, False
            else:
                awake_idle += awake_gap
            if timeout and time.monotonic() - start >= timeout:
                kill_group()
                raise subprocess.TimeoutExpired(
                    cmd, timeout, output="".join(out_buf), stderr="".join(err_buf)
                ) from None
            limit = idle_timeout
            if limit and snap.network and net_idle:
                limit = min(limit, net_idle)
            if limit and slept_since_progress and post_sleep_idle:
                limit = min(limit, post_sleep_idle)
            if limit and awake_idle >= limit:
                kill_group()
                reason = (
                    "slept" if slept_since_progress else "network" if snap.network else "stalled"
                )
                raise stalled(reason, awake_idle, snap) from None
            if startup_timeout and not snap.started_work and awake_elapsed >= startup_timeout:
                kill_group()
                raise stalled("startup", awake_elapsed, snap) from None
        for thread in threads:
            thread.join(timeout=2)
        return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out_buf), "".join(err_buf))
    finally:
        kill_group()
        _release_awake(awake_holder)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if heartbeat_path is not None:
            # The retained final record: a reader that saw this run alive can tell a
            # normal exit (``ended`` set) from a crash (file stale, pid gone, no ``ended``).
            # Output that arrived while ``proc.wait`` was returning only reaches the
            # buffers once the reader threads join, so the last PROGRESS line can be
            # newer than every tick saw: fold it in before the record is frozen, or the
            # retained ``progress_at`` understates the run's real finish.
            final_snap = health.snapshot()
            stamp_progress(final_snap)
            _write_heartbeat(
                heartbeat_path,
                heartbeat_meta or {},
                start,
                time.monotonic() - awake_idle,
                out_buf,
                err_buf,
                codex_pgid,
                extra=heartbeat_extra(final_snap, ended=True),
                identity=identity,
            )
            with contextlib.suppress(OSError):
                heartbeat_path.with_suffix(".tmp").unlink()


def _stderr_tail(text: str, lines: int = 8) -> str:
    """The last few non-empty stderr lines — what codex was doing when killed."""
    kept = [line for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _repo_map(cwd: str | None, explicit: str | None = None, limit: int = 4000) -> str | None:
    """A compact orientation map of the repo, injected into the delegate prompt.

    Discovery is what burns delegate wall-clock: a run that has to find its way
    around a repo can spend its whole budget exploring. ``explicit`` (the
    ``--repo-map FILE`` flag) wins — the caller curates exactly the context
    codex gets; else prefer the repo's own ``repo_scope_short.md`` (a
    human-curated map); else fall back to a top-level tracked-file summary from
    git. Never fatal — returns None when nothing useful exists.
    """
    if explicit:
        try:
            text = Path(explicit).read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            print(f"WARNING: --repo-map {explicit} unreadable ({exc}); no map.", file=sys.stderr)
            return None
        if not text:
            return None
        return f"REPO MAP (from {Path(explicit).name} — trust it for orientation):\n{text[:limit]}"
    if not cwd:
        return None
    root = Path(cwd)
    with contextlib.suppress(OSError):
        scope = root / "repo_scope_short.md"
        if scope.is_file():
            text = scope.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return (
                    "REPO MAP (from repo_scope_short.md — trust it for orientation):\n"
                    + text[:limit]
                )
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not listing.strip():
        return None
    counts: dict[str, int] = {}
    top_files: list[str] = []
    for line in listing.splitlines():
        if "/" in line:
            top = line.split("/", 1)[0] + "/"
            counts[top] = counts.get(top, 0) + 1
        else:
            top_files.append(line)
    entries = [f"{d} ({n} files)" for d, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    entries += top_files
    return "REPO MAP (top-level tracked entries):\n" + "\n".join(entries[:40])


# Codex usage (rate-limit) reading — Codex has no usage API; it writes a rate_limits
# block onto token_count events in $CODEX_HOME/sessions/**/rollout-*.jsonl.
_CODEX_SCAN_LIMIT = (
    200  # newest rollout files to scan for a usable window (short runs log windowless)
)

# Optional offloads should leave room for interactive/debate work.  The function seam
# below deliberately owns this bootstrap value so a learned reserve (3 x P95 debate-round
# cost + 10% margin) can replace it without changing the decision/output machinery.
_HEADROOM_BOOTSTRAP_RESERVE_PERCENT = 35.0
_HEADROOM_MIN_SAMPLES = 10
_HEADROOM_MIN_RESERVE_PERCENT = 5.0
_HEADROOM_MAX_RESERVE_PERCENT = 60.0
_HEADROOM_STALE_AFTER_SECONDS = 6 * 3600
_HEADROOM_RESET_FRESH_SECONDS = 10 * 60
_COST_HISTORY_SECONDS = 90 * 24 * 3600
_FIVE_HOUR_MINUTES = 5 * 60
_SEVEN_DAY_MINUTES = 7 * 24 * 60

# ── routing feedback budgets (plan D6, debate O13) ──────────────────────────────────
# A measurement is best-effort garnish on a run, never a reason for it to be late: the
# whole pre-selection refresh shares ONE 20 s budget, each fetch is capped at 15 s, and
# the post-attempt fetch only runs while at least that much of the call budget is left.
_REFRESH_BUDGET_SEC = 20.0
_REFRESH_FETCH_TIMEOUT_SEC = 15.0
# The write-mode floor when no learned round cost exists yet: a seat with less than a
# tenth of a window left is not where a run that may EDIT files should start.
_WRITE_FLOOR_BOOTSTRAP_PCT = 10.0
# The learned weekly reserve is 3 rounds + 10 % (see _headroom_reserve); dividing it back
# out recovers the P95 cost of ONE round, which is what a single write run needs.
_ROUNDS_PER_RESERVE = 3.3


# --------------------------------------------------------------------------- #
# Clickable-terminal helpers (OSC 8) — per repo convention.
# --------------------------------------------------------------------------- #
def osc8_link(target: str, label: str | None = None) -> str:
    """Wrap *target* (URL) as an OSC 8 hyperlink; degrades to plain text."""
    label = label or target
    return f"\x1b]8;;{target}\x1b\\{label}\x1b]8;;\x1b\\"


def local_link(path: Path) -> str:
    """Clickable link to a local *path* (openterm:// for iTerm2/WezTerm, file:// fallback)."""
    abspath = str(path.resolve())
    return osc8_link("openterm://" + urllib.parse.quote(abspath), abspath)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def config_path() -> Path:
    """Shared config file path ($CODEX_IN_CLAUDE_CONFIG override)."""
    env = os.environ.get("CODEX_IN_CLAUDE_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "codex-in-claude" / "config.json"


def load_config() -> dict:
    """Load the config, tolerating a missing/corrupt file (returns defaults).

    ``codex_home`` / ``codex_home_until`` are the account pin (see
    :func:`pinned_codex_home`); ``codex-review.py`` reads the same two keys.
    """
    path = config_path()
    base: dict[str, Any] = {
        "default": DEFAULT_MODEL,
        "delegate-review": None,
        "debate": None,
        "effort": "xhigh",
        "codex_home": None,
        "codex_home_until": None,
        "aliases": {},
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base.update(data)
    except (OSError, ValueError):
        pass
    if not isinstance(base.get("aliases"), dict):
        base["aliases"] = {}
    return base


def save_config(cfg: dict) -> Path:
    """Atomically persist *cfg*; returns the path written."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), prefix=".cfg.", suffix=".tmp", delete=False
    ) as handle:
        json.dump(cfg, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = handle.name
    os.replace(tmp, path)
    return path


def resolve_model(for_command: str | None) -> str:
    """Resolve the model for *for_command*: per-command -> default -> DEFAULT_MODEL."""
    cfg = load_config()
    if for_command and cfg.get(for_command):
        return str(cfg[for_command])
    return str(cfg.get("default") or DEFAULT_MODEL)


def resolve_effort() -> str | None:
    """Configured reasoning effort, or None to use the model's catalog default."""
    val = load_config().get("effort")
    return str(val) if val in EFFORTS else None


# --------------------------------------------------------------------------- #
# Description markers — make the active model visible in Claude Code's help
#
# Claude Code renders the one-line help for `/codex-debate` and
# `/codex-implement-task-and-claude-review` from the `description:` YAML frontmatter of
# their skill/command markdown, and that text is STATIC (read at session start). So the
# model is surfaced by stamping a leading `[codex <model> effort=<e>]` marker into those
# descriptions whenever the config changes — a prefix, so it survives the terminal's
# right-truncation of long descriptions.
# --------------------------------------------------------------------------- #
MARKER_RE = re.compile(r"^\[codex\s+[^\]]*\]\s*")
_BLOCK_SCALARS = (">", ">-", ">+", "|", "|-", "|+")


def claude_home() -> Path:
    """Claude Code's config dir ($CLAUDE_CONFIG_DIR override, else ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def marker_surfaces() -> list[tuple[str, Path]]:
    """(config key, markdown file) pairs whose ``description:`` carries the model marker.

    Skill files and command files both count: a command file shadows a same-named skill in
    the slash-command listing, so whichever exists must stay in sync. Missing paths are
    reported, never an error — not every surface exists on every machine.
    """
    home = claude_home()
    return [
        ("debate", home / "skills" / "codex-debate" / "SKILL.md"),
        (
            "delegate-review",
            home / "skills" / "codex-implement-task-and-claude-review" / "SKILL.md",
        ),
        ("debate", home / "commands" / "codex-debate.md"),
        ("delegate-review", home / "commands" / "codex-implement-task-and-claude-review.md"),
        ("", home / "commands" / "codex-model.md"),  # "" = the global default model
    ]


def marker_for(key: str) -> str:
    """The ``[codex <model> effort=<e>]`` marker for config *key* (``debate`` / …)."""
    model = resolve_model(key)
    effort = resolve_effort() or effort_of(model)
    return f"[codex {model} effort={effort}]" if effort != "?" else f"[codex {model}]"


def _yaml_dq(value: str) -> str:
    """Double-quote *value* as a YAML scalar (escaping ``\\`` and ``"``).

    Needed because the marker starts with ``[``, which YAML would read as a flow sequence
    at the head of a plain scalar. Block scalars are literal text and need no quoting.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def inject_marker(text: str, marker: str) -> str:
    """Return *text* with its frontmatter ``description:`` prefixed by *marker*.

    Idempotent: an existing marker is replaced, never stacked. Raises ``ValueError`` when
    the file has no usable frontmatter description.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("no YAML frontmatter")
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    if end is None:
        raise ValueError("unterminated YAML frontmatter")
    idx = next((i for i in range(1, end) if lines[i].startswith("description:")), None)
    if idx is None:
        raise ValueError("no description: key in frontmatter")

    value = lines[idx].partition(":")[2].strip()
    if value in _BLOCK_SCALARS:
        # Folded/literal block: stamp the first non-blank continuation line.
        body = next((i for i in range(idx + 1, end) if lines[i].strip()), None)
        if body is None:
            raise ValueError("empty block description")
        indent = lines[body][: len(lines[body]) - len(lines[body].lstrip())]
        content = MARKER_RE.sub("", lines[body].strip(), count=1)
        lines[body] = f"{indent}{marker} {content}\n"
    else:
        quote = value[0] if value[:1] in ("'", '"') else ""
        inner = value[1:-1] if quote and value.endswith(quote) and len(value) > 1 else value
        if quote == '"':  # undo YAML escaping before re-quoting
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        elif quote == "'":
            inner = inner.replace("''", "'")
        inner = MARKER_RE.sub("", inner, count=1)
        lines[idx] = f"description: {_yaml_dq(f'{marker} {inner}')}\n"
    return "".join(lines)


def sync_markers() -> list[tuple[str, Path, str]]:
    """Stamp the current model marker into every surface; returns (status, path, detail).

    Status is ``updated`` / ``ok`` (already current) / ``missing`` / ``error``. Files are
    rewritten **in place** (truncate + write, never temp+rename): a dotfiles setup often
    hard-links these paths to a tracked working copy, and an atomic replace would break the
    link and silently fork the two copies.
    """
    out: list[tuple[str, Path, str]] = []
    for key, path in marker_surfaces():
        if not path.exists():
            out.append(("missing", path, ""))
            continue
        try:
            old = path.read_text(encoding="utf-8")
            new = inject_marker(old, marker_for(key))
        except (OSError, ValueError) as exc:
            out.append(("error", path, str(exc)))
            continue
        if new == old:
            out.append(("ok", path, key))
            continue
        try:
            path.write_text(new, encoding="utf-8")
        except OSError as exc:
            out.append(("error", path, str(exc)))
            continue
        out.append(("updated", path, key))
    return out


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #
def _parse_models(blob: str) -> list[dict]:
    """Extract the model list from a `codex debug models` / cache JSON blob."""
    data = json.loads(blob)
    models = data if isinstance(data, list) else data.get("models", [])
    return [m for m in models if isinstance(m, dict)]


def list_models(*, refresh: bool, include_hidden: bool, timeout: int = 30) -> list[dict]:
    """Return the available Codex models.

    Default reads the offline cache (fast, works on a bad connection); ``--refresh``
    calls ``codex debug models`` (network) and falls back to the cache on failure.
    """
    models: list[dict] = []
    if refresh:
        try:
            proc = subprocess.run(
                ["codex", "debug", "models"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                models = _parse_models(proc.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            models = []
    if not models:
        try:
            models = _parse_models(_codex_cache().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            models = []
    if not include_hidden:
        models = [m for m in models if m.get("visibility", "list") == "list"]
    return models


def effort_of(slug: str) -> str:
    """Catalog default reasoning effort for *slug* (informational), or '?'."""
    for m in list_models(refresh=False, include_hidden=True):
        if m.get("slug") == slug:
            return str(m.get("default_reasoning_level") or "?")
    return "?"


def valid_slug(slug: str) -> bool:
    """True if *slug* is a known model (visible or hidden)."""
    return any(m.get("slug") == slug for m in list_models(refresh=False, include_hidden=True))


def codename_of(slug: str) -> str | None:
    """The catalog codename of *slug*: ``gpt-5.6-sol`` -> ``sol``, ``gpt-5.5`` -> None.

    The trailing ``-<word>`` segment when it is purely alphabetic; a version-only slug
    has none.
    """
    head, sep, tail = slug.rpartition("-")
    if not sep or not head or not tail.isalpha():
        return None
    return tail.lower()


def builtin_aliases() -> dict[str, str]:
    """Codename -> slug for every VISIBLE catalog model, ambiguous codenames dropped.

    Two visible slugs sharing a codename would make the short name a guess, so neither
    gets it — the full slug still works. Hidden models (``codex-auto-review``,
    ``gpt-reserve``) get no built-in name: they are reachable by slug or a config alias.
    """
    seen: dict[str, str | None] = {}
    for model in list_models(refresh=False, include_hidden=False):
        slug = str(model.get("slug") or "")
        name = codename_of(slug)
        if not name:
            continue
        seen[name] = None if name in seen and seen[name] != slug else slug
    return {name: slug for name, slug in seen.items() if slug}


def config_aliases() -> dict[str, str]:
    """The user-defined ``aliases`` map (``codex-in-claude alias NAME SLUG``), keys lowercased."""
    raw = load_config().get("aliases") or {}
    return {
        str(name).lower(): str(slug)
        for name, slug in raw.items()
        if isinstance(name, str) and isinstance(slug, str) and name and slug
    }


def model_aliases() -> dict[str, str]:
    """Every short name that resolves: built-in codenames, overlaid by the config map."""
    merged = builtin_aliases()
    merged.update(config_aliases())
    return merged


def resolve_alias(name: str) -> str | None:
    """Turn a model NAME (slug or short name) into a catalog slug, or None if unknown."""
    if valid_slug(name):
        return name
    return model_aliases().get(name.strip().lower())


def _model_arg(name: str) -> str | None:
    """Resolve a user-supplied model name; on failure print the known names and return None."""
    slug = resolve_alias(name)
    if slug:
        return slug
    known = ", ".join(str(m.get("slug")) for m in list_models(refresh=False, include_hidden=False))
    short = ", ".join(f"{alias}={slug}" for alias, slug in sorted(model_aliases().items()))
    print(
        f"Unknown model '{name}'. Known (visible): {known or '(none)'}\n"
        f"Short names: {short or '(none)'}\n"
        "Use --include-hidden via `models -H` to see hidden ones.",
        file=sys.stderr,
    )
    return None


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_models(args: argparse.Namespace) -> int:
    """List models, starring the ones currently configured."""
    models = list_models(refresh=args.refresh, include_hidden=args.include_hidden)
    if not models:
        print(
            "No models found (cache empty and/or `codex debug models` unavailable).\n"
            "Try: codex-in-claude.py models --refresh",
            file=sys.stderr,
        )
        return EX_NO_CODEX
    cfg = load_config()
    configured = {cfg.get("default"), cfg.get("delegate-review"), cfg.get("debate")}
    width = max(len(str(m.get("slug", ""))) for m in models)
    print(f"Available Codex models  (default: {cfg.get('default') or DEFAULT_MODEL})\n")
    for m in models:
        slug = str(m.get("slug", ""))
        star = "*" if slug in configured else " "
        eff = str(m.get("default_reasoning_level") or "?")
        vis = str(m.get("visibility", "list"))
        hide = "  [hidden]" if vis != "list" else ""
        desc = str(m.get("description") or "")
        print(f" {star} {slug:<{width}}  effort={eff:<7}{hide}  {desc}")
    print()
    user_defined = config_aliases()
    aliases = model_aliases()
    if aliases:
        print("short names (any model argument accepts them):")
        for alias, slug in sorted(aliases.items()):
            tag = "  (config)" if user_defined.get(alias) == slug else ""
            print(f"    {alias:<16} -> {slug}{tag}")
        print()
    print("per-command:")
    for cmd in COMMANDS:
        print(f"    {cmd:<16} -> {resolve_model(cmd)}")
    print(f"    {'effort':<16} -> {resolve_effort() or 'default (each model own)'}")
    print(f"\nconfig: {local_link(config_path())}")
    print(
        "change: codex-in-claude.py pick   |   set-model <slug|name> [--for debate|delegate-review]"
        "   |   alias <name> <slug>"
    )
    return EX_OK


def cmd_get_model(args: argparse.Namespace) -> int:
    """Print the resolved model for a command (or the global default).

    With a NAME (``get-model astra``): print the slug that short name or slug resolves
    to — exit 3 when it is unknown — so a caller can validate a model argument before
    spending a round on it.
    """
    name = getattr(args, "name", None)
    if name:
        slug = _model_arg(name)
        if not slug:
            return EX_INVALID_MODEL
        print(slug)
        return EX_OK
    print(resolve_model(args.for_command))
    return EX_OK


def _alias_delete(name: str) -> int:
    """``alias -d NAME``: drop a config alias (built-in codenames are not deletable)."""
    if not name:
        print("alias -d needs the NAME to delete.", file=sys.stderr)
        return EX_USAGE
    cfg = load_config()
    if name not in cfg["aliases"]:
        hint = ""
        if name in builtin_aliases():
            hint = " (a built-in codename; only config aliases can be deleted)"
        print(f"No config alias '{name}'{hint}.", file=sys.stderr)
        return EX_USAGE
    del cfg["aliases"][name]
    save_config(cfg)
    print(f"deleted alias {name}")
    return EX_OK


def _alias_define(name: str, slug: str) -> int:
    """``alias NAME SLUG``: record NAME -> SLUG in the config (SLUG may itself be a name)."""
    target = _model_arg(slug)
    if not target:
        return EX_INVALID_MODEL
    if valid_slug(name):
        print(f"'{name}' is a model slug itself and cannot be an alias.", file=sys.stderr)
        return EX_USAGE
    cfg = load_config()
    cfg["aliases"][name] = target
    path = save_config(cfg)
    print(f"alias {name} -> {target}\nconfig: {path}")
    return EX_OK


def _alias_list() -> int:
    """``alias`` with no arguments: every short name, tagged built-in or config."""
    user_defined = config_aliases()
    aliases = model_aliases()
    if not aliases:
        print("(no short names: empty catalog and no config aliases)")
        return EX_OK
    for alias, target in sorted(aliases.items()):
        tag = "  (config)" if user_defined.get(alias) == target else "  (built-in)"
        print(f"  {alias:<16} -> {target}{tag}")
    return EX_OK


def cmd_alias(args: argparse.Namespace) -> int:
    """List, define, resolve or delete short model names (config ``aliases``)."""
    name = (getattr(args, "name", None) or "").strip().lower()
    slug = getattr(args, "slug", None)
    if getattr(args, "delete", False):
        return _alias_delete(name)
    if name and slug:
        return _alias_define(name, slug)
    if name:
        resolved = resolve_alias(name)
        if not resolved:
            print(f"'{name}' is not defined.", file=sys.stderr)
            return EX_INVALID_MODEL
        print(resolved)
        return EX_OK
    return _alias_list()


def _report_sync(rows: list[tuple[str, Path, str]], *, verbose: bool = False) -> None:
    """Print a one-line-per-surface summary of a marker sync (quiet unless interesting)."""
    icon = {"updated": "✅", "ok": "•", "missing": "–", "error": "❌"}
    for status, path, detail in rows:
        if status == "ok" and not verbose:
            continue
        if status == "missing" and not verbose:
            continue
        suffix = f"  ({detail})" if detail and status == "error" else ""
        print(f"  {icon.get(status, '?')} {status:<8} {path}{suffix}")


def cmd_sync_skills(args: argparse.Namespace) -> int:
    """Stamp the resolved model into the codex skill/command descriptions."""
    if args.check:
        stale: list[Path] = []
        bad: list[tuple[Path, str]] = []
        for key, path in marker_surfaces():
            if not path.exists():
                continue
            try:
                old = path.read_text(encoding="utf-8")
                if inject_marker(old, marker_for(key)) != old:
                    stale.append(path)
            except (OSError, ValueError) as exc:
                bad.append((path, str(exc)))
        for path, why in bad:
            print(f"  ❌ error    {path}  ({why})", file=sys.stderr)
        for path in stale:
            print(f"  ⚠️  stale    {path}")
        if bad:
            return EX_USAGE
        if stale:
            print("\nrun: codex-in-claude.py sync-skills")
            return 1
        print("all codex skill/command descriptions are current")
        return EX_OK

    rows = sync_markers()
    _report_sync(rows, verbose=True)
    if any(status == "error" for status, _, _ in rows):
        return EX_USAGE
    if any(status == "updated" for status, _, _ in rows):
        print("\nNote: Claude Code reads these descriptions at session start —")
        print("      the new model shows in `/codex…` help in the NEXT session.")
    return EX_OK


def cmd_pick(args: argparse.Namespace) -> int:
    """Interactive numbered model picker (terminal only), then set + sync."""
    models = list_models(refresh=args.refresh, include_hidden=False)
    if not models:
        print(
            "No models found (cache empty and/or `codex debug models` unavailable).\n"
            "Try: codex-in-claude.py models --refresh",
            file=sys.stderr,
        )
        return EX_NO_CODEX
    if not sys.stdin.isatty() or os.environ.get("CI"):
        print(
            "pick needs an interactive terminal. Non-interactive alternative:\n"
            "  codex-in-claude.py set-model <slug> [--for delegate-review|debate|all]",
            file=sys.stderr,
        )
        return EX_USAGE
    target = args.for_command or "all"
    current = resolve_model(None if target == "all" else target)
    width = max(len(str(m.get("slug", ""))) for m in models)
    print(f"Pick the Codex model for {target}  (current: {current})\n")
    for num, m in enumerate(models, 1):
        slug = str(m.get("slug", ""))
        mark = "*" if slug == current else " "
        eff = str(m.get("default_reasoning_level") or "?")
        print(f" {mark} {num}) {slug:<{width}}  effort={eff:<7}  {m.get('description') or ''}")
    try:
        raw = input("\nnumber (Enter = keep current): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return EX_USAGE
    if not raw:
        print(f"kept {current}")
        return EX_OK
    if not raw.isdigit() or not 1 <= int(raw) <= len(models):
        print(f"Not a listed number: {raw!r}", file=sys.stderr)
        return EX_USAGE
    args.slug = str(models[int(raw) - 1].get("slug", ""))
    args.for_command = target
    return cmd_set_model(args)


def cmd_set_model(args: argparse.Namespace) -> int:
    """Set the model for a command (or the global default with --for all/omitted)."""
    slug = _model_arg(args.slug)
    if not slug:
        return EX_INVALID_MODEL
    cfg = load_config()
    target = args.for_command
    if target in (None, "all"):
        cfg["default"] = slug
        where = "default (all commands)"
        if target == "all":  # "all" also clears per-command pins, else they'd shadow it
            for cmd in COMMANDS:
                cfg[cmd] = None
    else:
        cfg[target] = slug
        where = target
    path = save_config(cfg)
    print(f"set {where} model -> {slug}\nconfig: {path}")
    _report_sync(sync_markers())
    return EX_OK


# Marks for the seat table (same vocabulary as ``ccc quota``'s report).
_SEAT_MARK = {"available": "✅", "blocked": "⛔", "unknown": "❔", "disabled": "🚫"}


_POLICY_BLURB = {
    "fill": (
        "policy: fill — spend the seat whose weekly allowance resets soonest; "
        "codex_seat_order is the tiebreak"
    ),
    "order": (
        "policy: order — try the seats strictly in codex_seat_order; "
        "the account pin is inert while one is configured"
    ),
}


def _seat_policy() -> str:
    """The configured seat policy, ``"fill"`` on any failure (the package default)."""
    try:
        from . import config  # pylint: disable=import-outside-toplevel  # cheap, per-call

        return config.codex_seat_policy()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return "fill"


def _seat_order_state() -> tuple[list[str], list[str], list[str]]:
    """``(configured, order, unknown)`` — the seat order as config + reality see it."""
    try:
        from . import config, quota  # pylint: disable=import-outside-toplevel

        configured = config.codex_seat_order()
        homes = quota._canonical_codex_homes()  # noqa: SLF001
        order, unknown = quota.resolve_seat_order(configured, homes)
        return configured, order, unknown
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ([], [], [])


def _candidate_dicts(candidates: list[SeatCandidate]) -> list[dict[str, Any]]:
    """The ``candidates`` array shared by ``home -j`` and ``order -j``.

    ``reason`` / ``cohort`` / ``measured`` carry the ranker's verdict for that seat so a
    machine consumer sees WHY the order is what it is (plan D9); they are empty/``None``
    for a candidate the ranker did not produce (an explicit ``$CODEX_HOME``).
    """
    return [
        {
            "rank": index,
            "label": cand.label,
            "id": cand.pid,
            "home": str(cand.home),
            "email": cand.email,
            "reason": cand.reason,
            "cohort": cand.cohort,
            "measured": cand.measured,
        }
        for index, cand in enumerate(candidates, 1)
    ]


def _pin_payload(cfg: dict) -> dict[str, Any] | None:
    """``{"label", "until", "active", "inert_because"}`` for the pin, or ``None``.

    ``inert_because`` is empty while the pin governs; otherwise it names WHICH rule
    disarmed it, because "not a registered seat" (plan D3) and "an explicit order under
    the ``order`` policy" ask for entirely different fixes.
    """
    pinned = pinned_codex_home(cfg)
    if pinned is None:
        return None
    active = pin_active(cfg)
    if active:
        inert = ""
    elif not _is_registered_seat(pinned):
        inert = "not a registered seat"
    else:
        inert = "explicit order set"
    return {
        "label": _seat_candidate_for(pinned).label,
        "until": str(cfg.get("codex_home_until") or ""),
        "active": active,
        "inert_because": inert,
    }


def cmd_home(  # pylint: disable=too-many-branches,too-many-return-statements,too-many-statements
    args: argparse.Namespace,
) -> int:
    """``home [PATH] [-u DATE] [-c]`` — show, set or clear the Codex account pin.

    No arguments: print the effective ``CODEX_HOME`` and where it comes from (env / pin /
    order / default), the configured seat order, the pin's expiry, and the account
    e-mail behind that home when readable. ``PATH`` sets the pin (must hold an
    ``auth.json``, i.e. a completed ``CODEX_HOME=<PATH> codex login``); ``-u/--until``
    bounds it (ISO date, inclusive); ``-c/--clear`` removes it.

    ``PATH`` must be a REGISTERED seat — one of ``quota._canonical_codex_homes()``
    (``~/.codex``, ``codex_home_private``, a ``codex_homes_extra`` login). An
    unregistered home has no ``codex:<label>`` quota row, so its refusals could be
    recorded nowhere and its usage ranked never, yet the pin would outrank three
    measurable seats (plan D3, debate O8). The refusal names the key that enrolls it.

    Under the ``order`` policy the pin is INERT whenever ``codex_seat_order`` is
    configured (see ``order``): setting one then only warns. Under ``fill`` the order is
    merely a tiebreak, so the pin governs. An explicit ``$CODEX_HOME`` still overrides
    everything, and is the only way to force one specific seat with no fallback.
    """
    cfg = load_config()
    if args.clear:
        cfg["codex_home"] = None
        cfg["codex_home_until"] = None
        save_config(cfg)
        print("codex home pin cleared — back to $CODEX_HOME / the seat order")
        return EX_OK
    if args.path:
        home = Path(args.path).expanduser()
        if not (home / "auth.json").is_file():
            print(
                f"error: {home} has no auth.json — run `CODEX_HOME={home} codex login` first",
                file=sys.stderr,
            )
            return EX_USAGE
        if not _is_registered_seat(home):
            label = home.name.lstrip(".").removeprefix("codex-") or "seat"
            print(
                f"error: {home} is not a registered seat — add it to ccc's config.toml: "
                f'codex_homes_extra = ["{label}={home}"] (or codex_home_private), then pin it',
                file=sys.stderr,
            )
            return EX_USAGE
        if args.until:
            try:
                date.fromisoformat(args.until)
            except ValueError:
                print(f"error: -u/--until must be an ISO date, got {args.until!r}", file=sys.stderr)
                return EX_USAGE
        cfg["codex_home"] = str(home)
        cfg["codex_home_until"] = args.until or None
        save_config(cfg)
        # Only the ``order`` policy lets an order outrank a pin; under ``fill`` the order
        # is a tiebreak and the pin really does govern, so the warning would be a lie.
        if _seat_policy() == "order" and _seat_order_state()[0]:
            print(
                "warning: an explicit codex_seat_order is configured — this pin is recorded "
                "but IGNORED for selection (clear the order with `codex-in-claude order -c`).",
                file=sys.stderr,
            )
    elif args.until:
        print("error: -u/--until needs a PATH (or use -c to clear)", file=sys.stderr)
        return EX_USAGE

    candidates = codex_homes_in_order()
    effective = _codex_home()
    configured, order, _unknown = _seat_order_state()
    policy = _seat_policy()
    stray_pin = pinned_codex_home(cfg)
    unregistered_pin = stray_pin is not None and not _is_registered_seat(stray_pin)
    if os.environ.get("CODEX_HOME"):
        source = "env $CODEX_HOME"
    elif pin_active(cfg):
        until = cfg.get("codex_home_until")
        source = f"config pin (until {until}, inclusive)" if until else "config pin (no expiry)"
    elif policy == "fill":
        source = "fill"
    elif configured:
        source = "codex_seat_order"
    else:
        source = "default"
        if cfg.get("codex_home"):
            source += f" (pin on {cfg['codex_home']} expired {cfg.get('codex_home_until')})"
    email = ""
    try:
        from .usage import codex_account_email  # pylint: disable=import-outside-toplevel

        email = codex_account_email(effective) or ""
    except Exception:  # pylint: disable=broad-exception-caught  # display-only
        email = ""
    if getattr(args, "json", False):
        # The machine contract external drivers (codex-review.py) consume instead of
        # re-implementing pin/hold resolution — ONE selector, everywhere. ``candidates``
        # is the ATTEMPT order: its first entry is always ``home``. ``homes`` is the
        # whole REGISTRY (label → CODEX_HOME, env-independent), blocked and exhausted
        # seats included: a consumer that lets its user name a seat (`tp open N -e
        # codex -s de`) resolves the label here and re-asks with ``$CODEX_HOME`` set,
        # instead of re-implementing ``codex_home_private``/``codex_homes_extra``.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "home": str(effective),
                    "source": source,
                    "label": _seat_candidate_for(effective).label,
                    "email": email,
                    "until": str(cfg.get("codex_home_until") or ""),
                    "order": order,
                    "candidates": _candidate_dicts(candidates),
                    "homes": {label: str(home) for label, home in canonical_codex_homes().items()},
                    "pin_active": pin_active(cfg),
                    "policy": policy,
                }
            )
        )
        return EX_OK
    line = f"codex home: {effective}  [{source}]" + (f"  account: {email}" if email else "")
    if not candidates:
        line += "  (no eligible seat)"
    if order:
        line += "  ·  order: " + " → ".join(order)
    if stray_pin is not None and not pin_active(cfg):
        line += (
            "  (pin ignored: not a registered seat)"
            if unregistered_pin
            else "  (pin ignored: explicit order set)"
        )
    print(line)
    if unregistered_pin:
        print(
            f"pin: {stray_pin} is not a registered seat — ignored (clear with -c)",
            file=sys.stderr,
        )
    return EX_OK


def _seat_table_lines(now: int | None = None) -> list[str]:
    """The ranked seat table ``order`` prints — one line per configured seat.

    The first line names the POLICY, and every seat line ends with the ranker's own
    ``rank_reason`` (``cohort 1: weekly resets in 5d 12h · 12% used`` / ``probe: …`` /
    ``unmeasured``). Under ``fill`` the configured order no longer explains the next
    attempt, so a table without the reason column would look arbitrary (plan D9).
    """
    now_ts = int(time.time()) if now is None else int(now)
    try:
        from . import quota  # pylint: disable=import-outside-toplevel

        snap = quota.snapshot(now=now_ts)
        rows = snap.get("codex_seat_order") or []
        policy = str(snap.get("codex_seat_policy") or "fill")
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return [f"(seat states unavailable: {type(exc).__name__}: {exc})"]
    lines: list[str] = [_POLICY_BLURB.get(policy, _POLICY_BLURB["fill"])]
    for row in rows:
        state = str(row.get("state") or "unknown")
        mark = _SEAT_MARK.get(state, " ")
        windows = " · ".join(
            f"{short} {win['used_pct']:.0f}%"
            for name, short in (("five_hour", "5h"), ("seven_day", "wk"))
            if (win := (row.get("windows") or {}).get(name)) is not None
        )
        detail = windows
        if state != "available":
            blocker = str(row.get("blocked_by") or "")
            reason = str(row.get("reason") or "")
            unblocks = int(row.get("resets_at") or 0)
            # A fail-closed rota reason already starts with "rota:" — never "rota: rota:".
            already = bool(blocker) and reason.startswith(f"{blocker}:")
            detail = (reason if already else f"{blocker + ': ' if blocker else ''}{reason}").strip()
            if unblocks > now_ts:
                detail += f" (unblocks {_format_reset(unblocks, now_ts)})"
            detail = detail or windows
        bits = [
            f"{row.get('configured_rank', '?')}",
            f"{row.get('label', '?'):<8}",
            f"{mark} {state:<9}",
            detail,
        ]
        line = "  ".join(part for part in bits if part)
        if row.get("email"):
            line += f"  {row['email']}"
        if row.get("rank_reason"):
            line += f"  ·  {row['rank_reason']}"
        rota = row.get("rota") or {}
        if rota.get("mine") and rota.get("label"):
            # OUR week on a shared seat. Deliberately not via ``note``, which renders with
            # a ⚠ — whose week it is is a fact about the seat, not a warning about it.
            # (The other person's week already shows as ``rota: <week> used by <name>``
            # in the blocked branch above.)
            line += f"  ·  rota: {rota['label']}"
        if row.get("attempt_rank") == 1:
            line += "  ← next attempt"
        if row.get("note"):
            line += f"  ⚠ {row['note']}"
        lines.append(line)
    return lines


def cmd_order(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements,too-many-branches
    """``order [LABEL …] [-c] [-j]`` — show or set the Codex seat order.

    What the order MEANS depends on ``codex_seat_policy`` (see ``policy``), and the
    table's first line says which is active:

    * under ``fill`` (the default) it is the deterministic TIEBREAK inside a cohort and
      the display order — the ranking itself is "spend the weekly allowance that resets
      soonest";
    * under ``order`` it is the ranking, and the AUTHORITATIVE selector: setting one
      clears (and thereafter ignores) the ``home`` account pin, because two competing
      "use this seat" knobs is exactly how a run ends up on a seat nobody chose.

    Setting still clears the pin under both policies (one knob at a time), refuses an
    unknown label (a typo would silently drop a seat to the end of the order) and
    refuses to rewrite a ``config.toml`` carrying keys ccc does not know, because the
    writer re-emits only known keys and would delete them.
    """
    labels: list[str] = [str(label).strip() for label in (getattr(args, "labels", None) or [])]
    labels = [label for label in labels if label]
    from . import config, quota  # pylint: disable=import-outside-toplevel

    if labels or getattr(args, "clear", False):
        homes = quota._canonical_codex_homes()  # noqa: SLF001
        bad = [label for label in labels if label not in homes]
        if bad:
            print(
                f"error: unknown seat label {bad[0]!r} — known: {', '.join(homes)}",
                file=sys.stderr,
            )
            return EX_USAGE
        stray = config.unknown_config_keys()
        if stray:
            print(
                f"refusing to rewrite config.toml: unknown keys {', '.join(stray)} would be "
                "dropped — remove them or edit codex_seat_order by hand",
                file=sys.stderr,
            )
            return EX_USAGE
        cfg_toml = config.load_config()
        cfg_toml.codex_seat_order = [] if getattr(args, "clear", False) else labels
        config.save_config(cfg_toml)
        cfg = load_config()
        if labels and cfg.get("codex_home"):
            cfg["codex_home"] = None
            cfg["codex_home_until"] = None
            save_config(cfg)
            print("pin cleared (order is authoritative)")
        noun = "codex seat tiebreak order" if _seat_policy() == "fill" else "codex seat order"
        print(f"{noun}: " + (" → ".join(labels) if labels else "(canonical: default → …)"))

    configured, order, unknown = _seat_order_state()
    candidates = codex_homes_in_order()
    cfg = load_config()
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "configured": configured,
                    "order": order,
                    "unknown": unknown,
                    "policy": _seat_policy(),
                    "next_attempt": candidates[0].pid if candidates else "",
                    "candidates": _candidate_dicts(candidates),
                    "pin": _pin_payload(cfg),
                }
            )
        )
        return EX_OK
    for line in _seat_table_lines():
        print(line)
    pin = _pin_payload(cfg)
    if pin is not None:
        suffix = "" if pin["active"] else f"  (ignored: {pin['inert_because']})"
        print(f"pin: {pin['label']} until {pin['until'] or '∞'}{suffix}")
    if unknown:
        print(f"unknown labels in config: {', '.join(unknown)}")
    if not candidates:
        print("next attempt: none eligible")
    return EX_OK


def cmd_policy(args: argparse.Namespace) -> int:
    """``policy [fill|order] [-j]`` — show or set HOW the Codex seats are ranked.

    ``fill`` (the package default): spend the seat whose WEEKLY allowance resets
    soonest, because that is the allowance about to be thrown away; seats resetting
    within 12 h of each other are one cohort and are filled equally. ``order``: the
    strict 2026-09-04 ``codex_seat_order``, kept as the operational escape hatch.

    Writing goes through the same unknown-key guard as ``order`` — ``save_config``
    re-emits only keys ccc knows, so a config carrying a hand-added key would lose it.
    """
    from . import config  # pylint: disable=import-outside-toplevel

    value = str(getattr(args, "policy", None) or "").strip().lower()
    if value:
        if value not in config.CODEX_SEAT_POLICIES:
            print(
                f"error: unknown policy {value!r} — choose: "
                f"{', '.join(config.CODEX_SEAT_POLICIES)}",
                file=sys.stderr,
            )
            return EX_USAGE
        stray = config.unknown_config_keys()
        if stray:
            print(
                f"refusing to rewrite config.toml: unknown keys {', '.join(stray)} would be "
                "dropped — remove them or edit codex_seat_policy by hand",
                file=sys.stderr,
            )
            return EX_USAGE
        cfg_toml = config.load_config()
        cfg_toml.codex_seat_policy = value
        config.save_config(cfg_toml)
    policy = _seat_policy()
    if getattr(args, "json", False):
        print(json.dumps({"schema_version": 1, "policy": policy}))
        return EX_OK
    print(_POLICY_BLURB.get(policy, _POLICY_BLURB["fill"]))
    return EX_OK


# --------------------------------------------------------------------------- #
# `rota` — the weekly schedule of a SHARED seat (config.toml `codex_seat_rota`)
# --------------------------------------------------------------------------- #
def _rota_write_guard() -> str:
    """The refusal text when ``config.toml`` holds keys ccc does not know, else ``""``.

    ``save_config`` re-emits ONLY the keys ccc knows, so rewriting a file that carries a
    hand-added one would delete it. Same guard as ``order`` / ``policy``.
    """
    from . import config  # pylint: disable=import-outside-toplevel

    stray = config.unknown_config_keys()
    if not stray:
        return ""
    return (
        f"refusing to rewrite config.toml: unknown keys {', '.join(stray)} would be "
        "dropped — remove them or edit codex_seat_rota by hand"
    )


def _rota_entry_label(entry: str) -> str:
    """The seat an entry names, read from the raw text alone (parse errors included).

    Rewrites go through THIS, never through the parsed specs: an entry ccc cannot parse
    must survive a ``rota set`` of another seat instead of being silently dropped.
    """
    return entry.partition("=")[0].strip()


def _rota_line(label: str, state: seat_rota.RotaState, me: str) -> str:
    """One seat's rota as a line: whose week it is, and when it changes hands."""
    holder = f"{state.label}{' (you)' if state.mine else ''}"
    if state.mine:
        tail = (
            f"{state.next_holder} from {state.next_other_label}"
            if state.next_other_at
            else "yours from here on"
        )
    elif state.next_mine_at:
        tail = f"yours from {state.next_mine_label}"
    else:
        tail = f"never yours — codex_seat_rota_me is {me!r}"
    return f"{label:<10} {holder}  ·  {tail}"


def _rota_states(
    now: int,
) -> tuple[dict[str, seat_rota.RotaState], list[seat_rota.RotaError], str]:
    """``(state per CONFIGURED seat on a rota, errors, me)`` — the whole read side."""
    from . import config, quota, seat_rota  # pylint: disable=import-outside-toplevel

    homes = quota._canonical_codex_homes()  # noqa: SLF001
    specs, _parse_errors = quota._rota_specs()  # noqa: SLF001
    me = config.codex_seat_rota_me()
    states = {
        label: seat_rota.rota_state(spec, me, now)
        for label, spec in specs.items()
        if label in homes
    }
    return states, quota.rota_errors(homes), me


def cmd_rota(args: argparse.Namespace) -> int:
    """``rota [show|set LABEL|clear LABEL|me NAME]`` — a seat SHARED on alternating weeks.

    A rota turns "this login is my colleague's this week" into a computed block: during
    somebody else's week the seat leaves the ranking for every Codex consumer, and it
    comes back on Monday 00:00 in the entry's own zone without anybody re-arming
    anything. It is ABSOLUTE for automation — the pin, the probe, ``headroom``, ``-Q``, a
    registered ``$CODEX_HOME``, a journal resume — and overridable only by a human
    ``/switch <seat>!`` or by editing the config.

    The verbs are disjoint on purpose (debate O12): ``show`` reads, ``set`` writes one
    seat's schedule, ``clear`` drops it, ``me`` says which name this machine is. Every
    write goes through the same unknown-key guard as ``order``/``policy``, and every verb
    keeps working while another entry is unreadable — a broken rota must not lock the
    operator out of the command that repairs it.
    """
    verb = str(getattr(args, "verb", "") or "show")
    if verb == "set":
        return _cmd_rota_set(args)
    if verb == "clear":
        return _cmd_rota_clear(args)
    if verb == "me":
        return _cmd_rota_me(args)
    return _cmd_rota_show(args)


def _cmd_rota_show(args: argparse.Namespace) -> int:
    """``rota`` / ``rota show [-j]`` — who holds which seat this week, and who is next."""
    from . import quota  # pylint: disable=import-outside-toplevel

    now = int(time.time())
    states, errors, me = _rota_states(now)
    if getattr(args, "json", False):
        specs, _errs = quota._rota_specs()  # noqa: SLF001
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "me": me,
                    "seats": {
                        label: {
                            key: value
                            for key, value in quota._rota_payload(  # noqa: SLF001
                                specs[label], state, me
                            ).items()
                            if key != "me"  # ``me`` is one machine-wide fact, not per seat
                        }
                        for label, state in states.items()
                    },
                    "errors": [
                        {"entry": err.entry, "label": err.label, "error": err.error}
                        for err in errors
                    ],
                }
            )
        )
        return EX_OK
    if not states and not errors:
        print("no seat on a rota")
        return EX_OK
    print(f"me: {me or '(unset — `codex-in-claude rota me <name>`)'}")
    for label, state in states.items():
        print(_rota_line(label, state, me))
    for err in errors:
        print(f"error: {err.entry!r} — {err.error}")
    return EX_OK


def _cmd_rota_set(args: argparse.Namespace) -> int:
    """``rota set LABEL -s MONDAY [-z ZONE] NAME NAME…`` — write one seat's schedule.

    Two things the entry grammar cannot know are checked here — that LABEL really is a
    configured seat, and which zone to default to — and then the entry is handed to
    :func:`command_center.seat_rota.parse_codex_seat_rota`, whose refusal IS the message.
    One grammar, one set of sentences: a date that is not a Monday is refused with the
    Monday of that week, an unknown zone with its own name, a single name with "a rota
    needs at least two names". Never persist an entry ccc cannot read back.
    """
    from . import config, quota, seat_rota  # pylint: disable=import-outside-toplevel

    label = str(getattr(args, "label", "") or "").strip()
    homes = quota._canonical_codex_homes()  # noqa: SLF001
    if label not in homes:
        print(f"error: unknown seat label {label!r} — known: {', '.join(homes)}", file=sys.stderr)
        return EX_USAGE
    # An unnamed zone is the machine's own — but only when it can be named honestly. A
    # rota anchored in the wrong zone changes hands on the wrong day, so guessing is
    # worse than refusing.
    zone = str(getattr(args, "tz", "") or "").strip() or seat_rota.local_zone_name()
    if not zone:
        print(
            "error: this machine's time zone could not be determined — pass -z Europe/Zurich",
            file=sys.stderr,
        )
        return EX_USAGE
    names = [str(name).strip() for name in (getattr(args, "names", None) or [])]
    start = str(getattr(args, "start", "") or "").strip()
    entry = f"{label}={start}@{zone}:{','.join(names)}"
    specs, problems = seat_rota.parse_codex_seat_rota([entry])
    if problems or label not in specs:
        error = problems[0].error if problems else "could not be read back"
        print(f"error: {error}", file=sys.stderr)
        return EX_USAGE
    guard = _rota_write_guard()
    if guard:
        print(guard, file=sys.stderr)
        return EX_USAGE
    cfg = config.load_config()
    cfg.codex_seat_rota = [
        line for line in config.codex_seat_rota() if _rota_entry_label(line) != label
    ] + [entry]
    config.save_config(cfg)
    me = config.codex_seat_rota_me()
    print(_rota_line(label, seat_rota.rota_state(specs[label], me, int(time.time())), me))
    if me not in names:
        # Fail-closed is the right default and a nasty surprise when it is silent: say so
        # here, where the operator is already holding the fix.
        print(
            f"warning: codex_seat_rota_me {me!r} is not one of {','.join(names)} — this seat "
            "stays BLOCKED until you run: codex-in-claude rota me <name>",
            file=sys.stderr,
        )
    return EX_OK


def _cmd_rota_clear(args: argparse.Namespace) -> int:
    """``rota clear LABEL`` — drop that seat's entry (the seat is ours again, always)."""
    from . import config  # pylint: disable=import-outside-toplevel

    label = str(getattr(args, "label", "") or "").strip()
    entries = config.codex_seat_rota()
    remaining = [line for line in entries if _rota_entry_label(line) != label]
    if len(remaining) == len(entries):
        known = ", ".join(_rota_entry_label(line) for line in entries) or "(none)"
        print(f"error: no rota entry for {label!r} — configured: {known}", file=sys.stderr)
        return EX_USAGE
    guard = _rota_write_guard()
    if guard:
        print(guard, file=sys.stderr)
        return EX_USAGE
    cfg = config.load_config()
    cfg.codex_seat_rota = remaining
    config.save_config(cfg)
    print(f"rota cleared: {label} is no longer shared")
    return EX_OK


def _cmd_rota_me(args: argparse.Namespace) -> int:
    """``rota me NAME`` — which rota name THIS machine's operator is."""
    from . import config, seat_rota  # pylint: disable=import-outside-toplevel

    name = str(getattr(args, "name", "") or "").strip()
    if not seat_rota.valid_name(name):  # the ONE definition of a rota name
        print(f"error: {name!r} is not a rota name ([a-z0-9][a-z0-9_-]*)", file=sys.stderr)
        return EX_USAGE
    guard = _rota_write_guard()
    if guard:
        print(guard, file=sys.stderr)
        return EX_USAGE
    cfg = config.load_config()
    cfg.codex_seat_rota_me = name
    config.save_config(cfg)
    print(f"rota me: {name}")
    states, _errors, me = _rota_states(int(time.time()))
    for label, state in states.items():
        print(_rota_line(label, state, me))
    return EX_OK


def cmd_get_effort(_args: argparse.Namespace) -> int:
    """Print the configured reasoning effort (or 'default' = the model's own default)."""
    print(resolve_effort() or "default")
    return EX_OK


def cmd_set_effort(args: argparse.Namespace) -> int:
    """Set the global reasoning effort; 'default' clears it (each model uses its own)."""
    level = args.level
    cfg = load_config()
    if level == "default":
        cfg["effort"] = None
        msg = "effort -> default (each model's own)"
    elif level in EFFORTS:
        cfg["effort"] = level
        msg = f"effort -> {level}"
    else:
        print(f"Unknown effort '{level}'. Choose: {', '.join(EFFORTS)}, default.", file=sys.stderr)
        return EX_USAGE
    path = save_config(cfg)
    print(f"{msg}\nconfig: {path}")
    _report_sync(sync_markers())
    return EX_OK


_PATCH_CONTRACT = (
    "You are Codex, implementing a task delegated by Claude Code. You are READ-ONLY: "
    "you CANNOT edit files. Inspect the repo as needed, then produce a COMPLETE solution "
    "as a single git-apply-able unified diff. Output EXACTLY these two sections and nothing "
    "after the diff:\n"
    "### SELF-CHECK\n"
    "<bullets: what you changed and why it is correct, edge cases considered, and the exact "
    "test/lint/build commands that SHOULD be run to verify it>\n"
    "### DIFF\n"
    "```diff\n"
    "<unified diff, paths relative to the repo root, applies cleanly with `git apply`>\n"
    "```\n"
)

_WRITE_CONTRACT = (
    "You are Codex, implementing a task delegated by Claude Code. You MAY edit files in this "
    "workspace. Implement the task fully, then RUN the project's tests/lint to verify your work "
    "and fix until they pass. Do NOT commit or push. End with a SELF-CHECK section:\n"
    "### SELF-CHECK\n"
    "<files changed; commands you ran and their pass/fail results; any caveats or risks>\n"
)

_SCOUT_CONTRACT = (
    "You are Codex, SCOUTING a task for Claude Code before implementation. You are READ-ONLY: "
    "inspect the repo (read files, run read-only commands) and return a short PLAN. Do NOT write "
    "code, edits, or a diff. Output EXACTLY this section, ~25 lines max:\n"
    "### PLAN\n"
    "<(1) the files/symbols you will change; (2) the approach as a short numbered list of steps; "
    "(3) risks, unknowns, or questions that need the caller's decision>\n"
)


_DISCIPLINE = (
    " Work with discovery discipline either way: when the TASK names files, functions, or "
    "line numbers, open those FIRST and trust them; prefer targeted searches over repo-wide "
    "exploration; leave time to implement and verify. A verified partial result beats an "
    "unfinished perfect one."
)


def _build_delegate_prompt(
    task: str,
    *,
    write: bool,
    feedback: str | None,
    round_no: int,
    scout: bool = False,
    budget_minutes: int | None = None,
    idle_minutes: int | None = None,
    repo_map: str | None = None,
) -> str:
    """Compose the Codex prompt: contract + time budget + repo map + task (+ feedback)."""
    header = _SCOUT_CONTRACT if scout else (_WRITE_CONTRACT if write else _PATCH_CONTRACT)
    parts = [header]
    idle_part = (
        f" The run IS killed after ~{idle_minutes} minutes with NO output at all."
        if idle_minutes
        else ""
    )
    if budget_minutes:
        parts += [
            f"\nTIME BUDGET: ~{budget_minutes} minutes of wall-clock for this ENTIRE run, "
            "enforced externally — overrunning yields NOTHING, not a partial result."
            + idle_part
            + _DISCIPLINE
            + "\n",
        ]
    elif idle_minutes:
        parts += [
            "\nTIME: no hard wall-clock limit on this run — take the time the task needs."
            + idle_part
            + _DISCIPLINE
            + "\n",
        ]
    if repo_map:
        parts += ["\n---\n", repo_map.strip(), "\n"]
    parts += ["\n---\nTASK:\n", task.strip(), "\n"]
    if feedback and feedback.strip():
        parts += [
            f"\n--- REVISION (round {round_no}). Claude reviewed your previous attempt and it "
            "did NOT pass. Address every point concretely:\n",
            feedback.strip(),
            "\n",
        ]
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Concurrency cap — a cross-process flock semaphore so a wide fan-out of
# `delegate` calls runs at most N codex runs at once (uncapped concurrency
# previously thrashed CPU/API and caused Codex timeouts).
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Codex usage / rate-limit awareness (read-only; vendored, mirrors ccc usage.py)
# --------------------------------------------------------------------------- #
def pinned_codex_home(cfg: dict | None = None, today: date | None = None) -> Path | None:
    """The account pin from the shared config, or ``None`` when there is none / it expired.

    ``codex_home`` names a second ``CODEX_HOME`` (another ChatGPT login, e.g.
    ``~/.codex-private``); ``codex_home_until`` is an ISO date, INCLUSIVE, after which the
    pin lapses on its own — "use the private seat until Monday" needs no follow-up edit.
    An unparsable date is treated as expired (fail towards the default seat).
    """
    cfg = load_config() if cfg is None else cfg
    raw = cfg.get("codex_home")
    if not raw:
        return None
    until = cfg.get("codex_home_until")
    if until:
        try:
            if date.fromisoformat(str(until)) < (today or date.today()):
                return None
        except ValueError:
            return None
    return Path(str(raw)).expanduser()


def _is_registered_seat(home: Path) -> bool:
    """True when *home* is one of the seats ccc's config registers (resolved paths).

    A pin at an unregistered path governs NOTHING (plan D3, debate O8): it has no
    ``codex:<label>`` quota row, so its refusals could not be recorded, its usage could
    not be ranked, and it would silently outrank three seats that ARE measurable.
    """
    known = canonical_codex_homes()
    if not known:
        return True  # registry unreadable: cannot verify ⇒ do not veto a real pin
    for path in known.values():
        try:
            if path.expanduser().resolve() == home.expanduser().resolve():
                return True
        except OSError:  # pragma: no cover - resolve() fails only on exotic filesystems
            if str(path) == str(home):
                return True
    return False


def pin_active(cfg: dict | None = None) -> bool:
    """True when the account pin actually GOVERNS seat selection.

    Two gates, both from the reconciled design:

    * the pinned path must be a REGISTERED seat (plan D3, debate O8). ``codex-in-claude
      home PATH`` refuses to record anything else, but a pin written before that rule
      existed — or by hand — is reported and ignored rather than obeyed.
    * under the ``order`` policy a pin is inert the moment an explicit
      ``codex_seat_order`` is configured: an ordered list is the stronger statement of
      intent, and a forgotten pin silently reshuffling it is exactly the surprise the
      order exists to remove (debate objection O2). Under ``fill`` (the default) the
      order is only a tiebreak, so it cannot outrank an explicit pin and the pin leads.

    :func:`pinned_codex_home` keeps reporting the pin either way — it is still
    introspectable, it just decides nothing.
    """
    pinned = pinned_codex_home(cfg)
    if pinned is None:
        return False
    try:
        from . import config  # pylint: disable=import-outside-toplevel  # cheap, per-call

        if not _is_registered_seat(pinned):
            return False
        if config.codex_seat_policy() == "order":
            return not config.codex_seat_order()
        return True
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return True  # a config read that fails must not silently disable a real pin


@dataclass(frozen=True)
class SeatCandidate:  # pylint: disable=too-many-instance-attributes  # flat seat record
    """One Codex seat the runner may try: its label, home, account and provider id.

    ``pid`` is the ``quota`` provider id (``codex`` / ``codex:private`` / ``codex:de``)
    and is EMPTY for a home ccc does not know — an explicit ``$CODEX_HOME`` pointing
    somewhere unregistered. An empty ``pid`` means "record nothing about this seat":
    a refusal from an unknown home has no row to block and would otherwise be
    misattributed to whichever seat happened to share its label.

    The four ranking fields travel WITH the candidate because the runner needs them per
    attempt: ``probe`` says this seat is the one due read-only probe (the runner must
    win :func:`command_center.quota.claim_probe` before launching it — plan D7),
    ``measured`` gates write runs (plan D5), and ``reason``/``cohort`` are what the
    machine payloads render.
    """

    label: str
    home: Path
    email: str
    pid: str
    reason: str = ""
    cohort: int | None = None
    measured: bool = False
    probe: bool = False


def canonical_codex_homes() -> dict[str, Path]:
    """Label → ``CODEX_HOME`` for every login ccc knows; ``{}`` on any failure.

    The seat registry lives in :mod:`command_center.quota` (which imports THIS module for
    the pin), so every other module reaches it through here rather than adding an import
    edge of its own.
    """
    try:
        from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

        return quota._canonical_codex_homes()  # noqa: SLF001
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return {}


def _seat_pid(label: str) -> str:
    """The ``quota`` provider id for a canonical seat *label*."""
    return "codex" if label == "default" else f"codex:{label}"


def _seat_candidate_for(home: Path) -> SeatCandidate:
    """Describe an EXPLICIT home as a candidate, naming it when ccc knows the seat."""
    label, pid = "explicit", ""
    for name, path in canonical_codex_homes().items():
        try:
            same = path.expanduser().resolve() == home.expanduser().resolve()
        except OSError:  # pragma: no cover - resolve() fails only on exotic filesystems
            same = str(path) == str(home)
        if same:
            label, pid = name, _seat_pid(name)
            break
    email = ""
    try:
        from .usage import codex_account_email  # pylint: disable=import-outside-toplevel

        email = codex_account_email(home) or ""
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        email = ""
    return SeatCandidate(label=label, home=home, email=email, pid=pid)


def codex_homes_in_order(now: int | None = None, *, probe: bool = True) -> list[SeatCandidate]:
    """The Codex seats to TRY, best first — the single source of truth for every runner.

    Three regimes, in this order:

    1. ``CCC_NO_CODEX`` set ⇒ ``[]``. The kill switch means zero Codex execution, so
       there is nothing to rank; callers turn the empty list into a typed ``disabled``
       error and start no process.
    2. an inherited ``$CODEX_HOME`` ⇒ exactly ONE candidate, that home. An explicit
       environment override is a hard instruction, not a preference: it never hops.
    3. otherwise ``quota``'s ranking via :func:`command_center.quota.rank_codex_seats` —
       the cooldown store plus the configured ``codex_seat_policy``:

       * ``fill`` (the default): an eligible REGISTERED account pin first, then the ONE
         unmeasured seat whose daily probe is due, then the measured seats grouped into
         weekly-reset cohorts (earliest cohort first, filled equally inside it), then
         the remaining unmeasured seats in configured order;
       * ``order``: the strict ``codex_seat_order``, pin first only while no order is
         configured, and never a probe.

    *probe* is False for a WRITE run: promoting an unmeasured seat is a read-only
    experiment (plan D5). A candidate carrying ``probe=True`` must not be launched until
    :func:`command_center.quota.claim_probe` has been won — the ranker only MARKS it.

    An empty list in regime 3 means every seat is held/blocked, and that is FINAL:
    there is deliberately no "fall back to the default seat anyway" any more (debate
    objection O3). A call the oracle knows will be refused costs a round trip, muddies
    the cost history and re-confirms a block that is already recorded. Any failure of
    the oracle itself also yields ``[]`` plus one stderr line — fail CLOSED, because
    the failure mode of guessing here is billing a seat the user reserved away.
    """
    if os.environ.get("CCC_NO_CODEX"):
        return []
    env = os.environ.get("CODEX_HOME")
    if env:
        return [_seat_candidate_for(Path(env).expanduser())]
    try:
        from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

        now_ts = int(time.time()) if now is None else int(now)
        homes = quota._canonical_codex_homes()  # noqa: SLF001
        rows = quota._codex_quotas(now_ts, quota.read_cooldowns(now_ts))  # noqa: SLF001
        order = quota.codex_seat_order_labels(homes)
        pin = quota._codex_pin_label(homes)  # noqa: SLF001
        ranked = quota.rank_codex_seats(rows, pin, order, now=now_ts, probe=probe)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(
            f"⚠️  Codex seat selection failed ({type(exc).__name__}: {exc}) — no seat tried",
            file=sys.stderr,
        )
        return []
    return [
        SeatCandidate(
            label=rank.row.account,
            home=homes[rank.row.account],
            email=rank.row.email,
            pid=rank.row.id,
            reason=rank.reason,
            cohort=rank.cohort,
            measured=rank.measured,
            probe=rank.probe,
        )
        for rank in ranked
        if rank.row.account in homes
    ]


def _codex_home() -> Path:
    """The Codex state dir to READ from — introspection and render paths only.

    The first candidate of :func:`codex_homes_in_order`, else the pin, else
    ``~/.codex``. That last fallback exists so a usage card / status line still has a
    path to read when nothing is eligible; it is NOT a launch decision — the runner
    consults :func:`codex_homes_in_order` itself and starts no process on an empty
    list. This function never launches anything and never raises.
    """
    candidates = codex_homes_in_order()
    if candidates:
        return candidates[0].home
    pinned = pinned_codex_home()
    return pinned if pinned is not None else Path.home() / ".codex"


def codex_exec_env(
    base: dict[str, str] | None = None, *, home: Path | None = None
) -> dict[str, str]:
    """``base`` (default ``os.environ``) with ``CODEX_HOME`` pinned to the billed seat.

    An explicit *home* (what the runner passes for the seat it is trying right now)
    always wins — it must, or a hop would keep billing the first seat. Without one, an
    explicit ``$CODEX_HOME`` already in *base* is left alone and otherwise
    :func:`_codex_home` is made explicit, so the child ``codex`` can never inherit a
    different seat than the one this process reasoned about.
    """
    env = dict(os.environ if base is None else base)
    if home is not None:
        env["CODEX_HOME"] = str(home)
    else:
        env.setdefault("CODEX_HOME", str(_codex_home()))
    return env


@dataclass(frozen=True)
class _CodexRateWindow:
    """One duration-identified Codex quota window."""

    used_percent: float
    resets_at: int
    window_minutes: int


@dataclass(frozen=True)
class _CodexRateSnapshot:
    """The newest rollout rate-limit event and all duration-keyed windows it carries."""

    captured_at: int
    windows: dict[int, _CodexRateWindow]
    malformed: bool = False


def _parse_codex_window(raw: object) -> _CodexRateWindow | None:
    """Parse a window, retaining the duration that identifies its quota bucket."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent")
    resets = raw.get("resets_at")
    minutes = raw.get("window_minutes")
    if pct is None or resets is None or minutes is None:
        return None
    try:
        parsed = _CodexRateWindow(float(pct), int(resets), int(minutes))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.window_minutes > 0 else None


def _dig_rate_limits(obj: object) -> dict | None:
    """Extract the ``rate_limits`` dict from a rollout line (top-level or under payload)."""
    if not isinstance(obj, dict):
        return None
    for cand in (obj.get("rate_limits"), (obj.get("payload") or {}).get("rate_limits")):
        if isinstance(cand, dict):
            return cand
    return None


def _event_timestamp(obj: object, fallback: int) -> int:
    """Return a rollout event's epoch timestamp, falling back to its file mtime."""
    if not isinstance(obj, dict):
        return fallback
    raw = obj.get("timestamp")
    try:
        if isinstance(raw, (int, float)):
            value = float(raw)
            return int(value / 1000 if value > 10**12 else value)
        if isinstance(raw, str):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.timestamp())
    except (OverflowError, ValueError):
        pass
    return fallback


def _windows_by_duration(rate_limits: dict) -> tuple[dict[int, _CodexRateWindow], bool]:
    """Parse primary/secondary without assigning either one a semantic position."""
    windows: dict[int, _CodexRateWindow] = {}
    malformed = False
    for slot in ("primary", "secondary"):
        raw = rate_limits.get(slot)
        if raw is None:
            continue
        window = _parse_codex_window(raw)
        if window is None or window.window_minutes in windows:
            malformed = True
            continue
        windows[window.window_minutes] = window
    return windows, malformed


def _latest_rate_limits_event(path: Path) -> _CodexRateSnapshot | None:
    """Newest non-windowless ``rate_limits`` event in a rollout JSONL.

    Skips windowless ``premium`` blocks (both windows null) that short ``codex exec``
    runs log. A malformed non-null window is returned as such so safety decisions can
    fail closed instead of silently falling back to an older, apparently healthy event.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        fallback = int(path.stat().st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = _dig_rate_limits(obj)
        if rate_limits is None:
            continue
        windows, malformed = _windows_by_duration(rate_limits)
        if windows or malformed:
            return _CodexRateSnapshot(
                captured_at=_event_timestamp(obj, fallback),
                windows=windows,
                malformed=malformed,
            )
    return None


@dataclass(frozen=True)
class _CodexRefusal:
    """A recorded refusal: Codex declined the call for a reason that is not a window."""

    captured_at: int
    reached_type: str  # raw ``rate_limit_reached_type``, e.g. workspace_owner_credits_depleted


# Human labels for the refusal codes seen in the wild; anything else is de-snaked as-is.
#
# ``workspace_owner_credits_depleted`` is OpenAI's wording and it misleads: the cause is
# almost never "you need to buy credits". Traced end-to-end on 2026-08-28/29 — the 5h
# window read 81% at 23:18, the next call exhausted it, and because the plan's INCLUDED
# allowance is what ran out, Codex tried the workspace credit pool as an overflow. That
# pool is empty (``has_credits: false``) on a normal seat that has never bought credits,
# so the refusal is reported in credit terms. Access came back by itself at 02:57, the
# moment the 5h window reset, with the credit balance still zero. So: this is a rate
# limit, and the fix is to wait for the reset, not to top up.
_REFUSAL_LABELS = {
    "workspace_owner_credits_depleted": "included usage limit reached (no credit overflow)",
}
# Card-sized wording for the same codes. The TUI's Codex card is 34 columns wide, so the
# explanatory label above wraps to three lines there and pushes the bars down; the CLI and
# `headroom`, which have a whole terminal width, keep the long form.
_REFUSAL_LABELS_SHORT = {
    "workspace_owner_credits_depleted": "usage limit reached",
}
# Snapshots persist the ALREADY-EXPANDED long label (there is no reached_type on the cached
# record), so the card shortens by looking the long form back up.
_SHORT_BY_LABEL = {
    _REFUSAL_LABELS[code]: short
    for code, short in _REFUSAL_LABELS_SHORT.items()
    if code in _REFUSAL_LABELS
}


def refusal_label(reached_type: str) -> str:
    """Human-readable form of a ``rate_limit_reached_type`` code."""
    return _REFUSAL_LABELS.get(reached_type, reached_type.replace("_", " "))


def short_refusal_label(label: str) -> str:
    """Card-sized form of an already-expanded refusal *label*; unknown ones pass through."""
    return _SHORT_BY_LABEL.get(label, label)


def _latest_limit_block(path: Path) -> _CodexRefusal | None:
    """Newest ``rate_limits`` block in a rollout JSONL — windowless ones INCLUDED.

    Deliberately the mirror of :func:`_latest_rate_limits_event`, which SKIPS the
    windowless ``premium`` blocks. Those are exactly where a hard refusal is recorded
    (``rate_limit_reached_type``), so a reader that only wants windows never sees it and
    happily reports the last healthy percentages while every call is being rejected.
    ``reached_type`` is empty when the newest block records no refusal.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        fallback = int(path.stat().st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = _dig_rate_limits(obj)
        if rate_limits is None:
            continue
        raw = rate_limits.get("rate_limit_reached_type")
        return _CodexRefusal(
            captured_at=_event_timestamp(obj, fallback),
            reached_type=raw.strip() if isinstance(raw, str) else "",
        )
    return None


def codex_refusal(home: Path | None = None) -> _CodexRefusal | None:
    """The live refusal recorded in *home*'s rollouts (default: the effective home).

    Only the NEWEST block across recent rollout files decides: an older refusal that a
    later successful call superseded must never keep the seat marked blocked.

    *home* matters for attribution: each seat's rollouts carry that seat's refusals,
    and reading "the effective home" while labelling the result "the team seat" is
    exactly the misattribution ``quota``'s per-seat rows exist to avoid — callers
    building per-seat rows MUST pass the row's own home.

    Deliberately NOT keyed on ``credits.has_credits``. That field is ``false`` on a
    perfectly healthy plan-covered seat — verified 2026-08-28 against sessions that
    completed normally minutes before the block appeared — so it says nothing about
    whether calls are being refused. ``rate_limit_reached_type`` is ``null`` while
    healthy and carries the reason once refusals start.
    """
    sessions = (home if home is not None else _codex_home()) / "sessions"
    try:
        files = sorted(
            sessions.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    blocks = [
        block
        for path in files[:_CODEX_SCAN_LIMIT]
        if (block := _latest_limit_block(path)) is not None
    ]
    newest = max(blocks, key=lambda item: item.captured_at, default=None)
    return newest if newest is not None and newest.reached_type else None


def _codex_rate_snapshot(home: Path | None = None) -> _CodexRateSnapshot | None:
    """Newest duration-keyed Codex quota event across *home*'s recent rollout files.

    *home* is the seat whose figures are wanted; ``None`` reads the effective home.
    Every per-seat caller MUST pass it — reading "the effective home" while labelling
    the answer with another seat's name is the misattribution the per-seat rows exist
    to prevent (the same rule as :func:`codex_refusal`).
    """
    sessions = (home if home is not None else _codex_home()) / "sessions"
    try:
        files = sorted(
            sessions.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    snapshots = [
        snapshot
        for path in files[:_CODEX_SCAN_LIMIT]
        if (snapshot := _latest_rate_limits_event(path)) is not None
    ]
    return max(snapshots, key=lambda item: item.captured_at, default=None)


def codex_cost_history_path() -> Path:
    """Per-run Codex cost log, beside this tool's existing config by default."""
    override = os.environ.get("CODEX_IN_CLAUDE_COST_LOG")
    if override:
        return Path(override).expanduser()
    return config_path().with_name("cost-history.jsonl")


def codex_cost_snapshot(home: Path | None = None) -> dict[str, dict[str, float | int]]:
    """Serializable duration-keyed quota snapshot for cost instrumentation.

    This public seam lets an external debate helper capture ``before`` and ``after``
    around its own ``codex exec`` and pass both to :func:`record_codex_run`. Pass the
    *home* the run actually billed: a hop bills two different seats in one call, and
    a before/after pair straddling them would record a nonsense delta.
    """
    snapshot = _codex_rate_snapshot(home)
    if snapshot is None:
        return {}
    return {
        str(minutes): {
            "used_percent": window.used_percent,
            "resets_at": window.resets_at,
        }
        for minutes, window in sorted(snapshot.windows.items())
    }


def _history_row_timestamp(row: object) -> float | None:
    """Finite epoch timestamp from one cost-history row, or None."""
    if not isinstance(row, dict):
        return None
    try:
        value = float(row["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def record_codex_run(
    *,
    purpose: str,
    model: str,
    effort: str,
    before: dict[str, dict[str, float | int]],
    after: dict[str, dict[str, float | int]],
    ts: int | None = None,
    path: Path | None = None,
    home: Path | None = None,
) -> bool:
    """Record one Codex run and prune history older than 90 days.

    Writes are serialized across processes and are deliberately best-effort: cost
    telemetry must never change a delegate run's result or stdout contract.

    *home* is the seat the run billed; the row records it as ``seat`` so a history
    spanning several logins can still be read per seat (the learned headroom reserve
    aggregates over all of them today, which is only sound while the rows say which
    seat each delta came from).
    """
    now = int(time.time()) if ts is None else ts
    target = path or codex_cost_history_path()
    lock_path = target.with_suffix(target.suffix + ".lock")
    row: dict[str, Any] = {
        "ts": now,
        "purpose": purpose,
        "model": model,
        "effort": effort,
        "before": before,
        "after": after,
    }
    if home is not None:
        row["seat"] = _seat_candidate_for(home).label
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            retained: list[str] = []
            cutoff = now - _COST_HISTORY_SECONDS
            try:
                old_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except FileNotFoundError:
                old_lines = []
            for line in old_lines:
                try:
                    old_row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                old_ts = _history_row_timestamp(old_row)
                if old_ts is not None and old_ts >= cutoff:
                    retained.append(json.dumps(old_row, separators=(",", ":")))
            retained.append(json.dumps(row, separators=(",", ":")))
            with tempfile.NamedTemporaryFile(
                "w",
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                handle.write("\n".join(retained) + "\n")
                tmp = Path(handle.name)
            tmp.replace(target)
        return True
    except OSError:
        return False


def _debate_cost_deltas(window_minutes: int, *, now: int | None = None) -> list[float]:
    """Valid debate-run usage deltas for one duration from the retained history."""
    now_ts = int(time.time()) if now is None else now
    try:
        lines = codex_cost_history_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    key = str(window_minutes)
    deltas: list[float] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        row_ts = _history_row_timestamp(row)
        if (
            not isinstance(row, dict)
            or row.get("purpose") != "debate"
            or row_ts is None
            or row_ts < now_ts - _COST_HISTORY_SECONDS
        ):
            continue
        before = row.get("before")
        after = row.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        before_window = before.get(key)
        after_window = after.get(key)
        if not isinstance(before_window, dict) or not isinstance(after_window, dict):
            continue
        try:
            before_pct = float(before_window["used_percent"])
            after_pct = float(after_window["used_percent"])
            before_reset = int(before_window["resets_at"])
            after_reset = int(after_window["resets_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (before_pct, after_pct)):
            continue
        # A changed reset boundary means the run crossed a reset, even if the new
        # window happened to climb back above the old used percentage.
        if after_reset != before_reset or after_pct < before_pct:
            continue
        deltas.append(after_pct - before_pct)
    return deltas


def _codex_usage_windows(
    now: int | None = None, home: Path | None = None
) -> dict[str, tuple[float, int] | None]:
    """Live 5h/weekly windows from *home*'s newest usable rollout block.

    ``{"five_hour": (pct, resets)|None, "seven_day": (pct, resets)|None}``. A window
    whose ``resets_at <= now`` is STALE (its reset already passed) and reported None —
    else the gate would never reopen after a reset. All-None => usage unknown.
    """
    now_ts = int(time.time()) if now is None else now
    snapshot = _codex_rate_snapshot(home)
    if snapshot is None:
        return {"five_hour": None, "seven_day": None}

    def _live(minutes: int) -> tuple[float, int] | None:
        win = snapshot.windows.get(minutes)
        if win is None or win.resets_at <= now_ts:
            return None
        return (win.used_percent, win.resets_at)

    return {
        "five_hour": _live(_FIVE_HOUR_MINUTES),
        "seven_day": _live(_SEVEN_DAY_MINUTES),
    }


def read_codex_usage(
    now: int | None = None, home: Path | None = None
) -> tuple[float | None, int | None]:
    """``(used_percent, resets_at)`` of *home*'s most-consumed live window, else ``(None, None)``.

    The runner's per-seat preflight: a seat already at 100 % is SKIPPED without a
    process, and the next seat is tried instead of the whole call failing.
    """
    windows = _codex_usage_windows(now, home)
    live = [w for w in (windows["five_hour"], windows["seven_day"]) if w is not None]
    if not live:
        return (None, None)
    return max(live, key=lambda w: w[0])


def _format_reset(resets_at: int, now: int | None = None) -> str:
    """Relative reset, minute precision: ``in 4h 5m`` / ``in 3d 2h 4m`` / ``now``."""
    now = int(time.time()) if now is None else now
    secs = int(resets_at) - now
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return "in " + " ".join(parts)


def _headroom_reserve(window_minutes: int) -> tuple[float, str]:
    """Reserve percentage and its source for one duration."""
    deltas = sorted(_debate_cost_deltas(window_minutes))
    if len(deltas) < _HEADROOM_MIN_SAMPLES:
        return (_HEADROOM_BOOTSTRAP_RESERVE_PERCENT, "bootstrap")
    # Nearest-rank P95: the smallest observed value at or above the 95th percentile.
    p95 = deltas[math.ceil(0.95 * len(deltas)) - 1]
    learned = 3.0 * p95 * 1.10
    return (
        min(_HEADROOM_MAX_RESERVE_PERCENT, max(_HEADROOM_MIN_RESERVE_PERCENT, learned)),
        "learned",
    )


def headroom_reserve_percent(window_minutes: int) -> float:
    """Quota to hold back: learned debate cost after 10 samples, else 35%."""
    return _headroom_reserve(window_minutes)[0]


def _format_window_duration(window_minutes: int) -> str:
    """Compact human label for an arbitrary rate-limit duration."""
    if window_minutes % (24 * 60) == 0:
        return f"{window_minutes // (24 * 60)}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def _headroom_window_rows(row: quota.ProviderQuota, now: int) -> list[dict[str, Any]]:
    """One dict per window of *row*, each with its reserve and its own verdict.

    A STALE window (its reset has passed, or the reading predates its own duration) is
    still listed — the user wants to see it — but is marked and given the ``unknown``
    verdict, and :func:`seat_headroom` judges the seat only on the fresh ones. A reading
    from a window that no longer exists must neither allow nor deny anything.
    """
    minutes_of = {"five_hour": _FIVE_HOUR_MINUTES, "seven_day": _SEVEN_DAY_MINUTES}
    rows: list[dict[str, Any]] = []
    for name, win in row.windows.items():
        minutes = minutes_of.get(name)
        if minutes is None:
            continue
        reserve, reserve_source = _headroom_reserve(minutes)
        remaining = 100.0 - win.used_pct
        reset_fresh = win.resets_at <= now + _HEADROOM_RESET_FRESH_SECONDS
        rows.append(
            {
                "duration": _format_window_duration(minutes),
                "window_minutes": minutes,
                "used_percent": win.used_pct,
                "remaining_percent": remaining,
                "resets_at": win.resets_at,
                "resets_in_seconds": win.resets_at - now,
                "reserve_percent": reserve,
                "reserve_source": reserve_source,
                "reset_fresh": reset_fresh,
                "stale": win.stale,
                "verdict": (
                    "unknown"
                    if win.stale
                    else ("allowed" if reset_fresh or remaining >= reserve else "reserve")
                ),
            }
        )
    rows.sort(key=lambda entry: int(entry["window_minutes"]))
    return rows


def seat_headroom(row: quota.ProviderQuota, now: int) -> dict[str, Any]:
    """ONE seat's offload verdict, from the same quota row the ranking used.

    Deliberately the quota row and not a second rollout read (plan D4, debate O4): the
    row already merges the live snapshot, the rollout and the cooldown store, so the
    gate and the selector can no longer disagree — an old rollout at 100 % used to veto
    a seat whose newer live reading said 40 %.

    Fails CLOSED on every measurement doubt, because this gate only ever governs
    OPTIONAL offload work: no fresh window, a snapshot older than
    :data:`_HEADROOM_STALE_AFTER_SECONDS`, or a ``malformed`` window (a non-null window
    whose duration could not be read — it may be the very window that is full) all
    report ``unknown``. Routing itself keeps failing OPEN on the same row; the
    asymmetry is the point.
    """
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    windows = _headroom_window_rows(row, now)
    fresh = [win for win in windows if not win["stale"]]
    if row.state == quota.BLOCKED:
        state, reason = "blocked", row.reason or "seat is blocked"
    elif row.malformed:
        state, reason = "unknown", "usage snapshot contains a malformed window"
    elif not fresh:
        state, reason = "unknown", "no usage snapshot"
    elif now - row.captured_at > _HEADROOM_STALE_AFTER_SECONDS:
        state, reason = "unknown", "newest usage snapshot is older than 6h"
    elif any(win["verdict"] == "reserve" for win in fresh):
        state, reason = "reserve", "at least one live window is inside its reserve"
    else:
        state, reason = "allowed", "every live window has sufficient headroom"
    if state in ("unknown", "blocked"):
        for win in windows:
            win["verdict"] = state
    return {
        "label": row.account,
        "id": row.id,
        "email": row.email,
        "state": state,
        "reason": reason,
        "captured_at": row.captured_at or None,
        "windows": windows,
    }


def _headroom_rows(now: int) -> list[tuple[SeatCandidate, quota.ProviderQuota]]:
    """The candidate seats paired with their quota rows, in attempt order.

    A registered candidate reuses the row :func:`command_center.quota._codex_quotas`
    already built; an unregistered explicit ``$CODEX_HOME`` gets one built on the spot
    so it is judged by exactly the same rules instead of falling into a second code path.
    """
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    candidates = codex_homes_in_order(now)
    if not candidates:
        return []
    cooldowns = quota.read_cooldowns(now)
    by_pid = {row.id: row for row in quota._codex_quotas(now, cooldowns)}  # noqa: SLF001
    pairs: list[tuple[SeatCandidate, quota.ProviderQuota]] = []
    for cand in candidates:
        row = by_pid.get(cand.pid) if cand.pid else None
        if row is None:
            row = quota._codex_seat_quota(  # noqa: SLF001
                cand.pid, cand.label, cand.home, now, cooldowns
            )
        pairs.append((cand, row))
    return pairs


def codex_headroom(now: int | None = None) -> dict[str, Any]:
    """Structured quota-reserve decision for optional Codex offloads — over the POOL.

    The question this answers is "may optional work be offloaded to Codex at all", and
    since 2026-09-09 (plan D4, tp#212) it is asked of every eligible SEAT, not of one
    home's rollout files. That was the concrete bug: a completely fresh team seat at
    0 %/0 % was invisible to the gate, which read the pinned home, found nothing newer
    than six hours and answered ``DENIED (unknown)`` while a whole paid seat sat idle.

    The pool verdict is the best seat's: any ``allowed`` allows (and names that seat),
    else ``reserve``, else ``unknown``, else ``blocked``. Candidates come from
    :func:`codex_homes_in_order`, so ``$CODEX_HOME`` narrows the question to one seat
    and ``CCC_NO_CODEX`` answers ``blocked`` with no seats at all.

    Missing, malformed, or older-than-six-hours data still fails closed per seat. This
    policy governs optional offload work only; debate callers intentionally remain
    always allowed.
    """
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    now_ts = int(time.time()) if now is None else now
    pairs = _headroom_rows(now_ts)
    seats = [seat_headroom(row, now_ts) for _cand, row in pairs]
    chosen: dict[str, Any] | None = None
    for wanted in ("allowed", "reserve", "unknown", "blocked"):
        chosen = next((seat for seat in seats if seat["state"] == wanted), None)
        if chosen is not None:
            break
    if chosen is None:
        state = "blocked"
        reason = (
            "Codex disabled (CCC_NO_CODEX)"
            if os.environ.get("CCC_NO_CODEX")
            else "no eligible seat"
        )
        label, windows, captured = "", [], None
    else:
        state = str(chosen["state"])
        label = str(chosen["label"])
        windows = list(chosen["windows"])
        captured = chosen["captured_at"]
        reason = {
            "allowed": f"seat {label} has headroom",
            "reserve": "every measurable seat is inside its reserve",
        }.get(state, str(chosen["reason"]))
    refused_by, refused_at = "", None
    if state == "blocked":
        # Kept verbatim for the pre-pool consumers: the raw ``rate_limit_reached_type``
        # of the CHOSEN seat's own refusal, else the recorded cooldown's scope.
        chosen_cand = next((cand for cand, _row in pairs if cand.label == label), None)
        refusal = codex_refusal(chosen_cand.home) if chosen_cand is not None else None
        if refusal is not None:
            refused_by, refused_at = refusal.reached_type, refusal.captured_at
        else:
            entry = quota.read_cooldowns(now_ts).get(
                chosen_cand.pid if chosen_cand is not None else ""
            )
            if entry:
                refused_by = str(entry.get("scope") or "")
                refused_at = int(entry.get("observed_at") or 0) or None
    return {
        "state": state,
        "offload_allowed": state == "allowed",
        "reason": reason,
        "refused_by": refused_by,
        "refused_at": refused_at,
        "captured_at": captured,
        "stale_after_seconds": _HEADROOM_STALE_AFTER_SECONDS,
        "reset_fresh_within_seconds": _HEADROOM_RESET_FRESH_SECONDS,
        "windows": windows,
        # Additive (plan D4): which seat the verdict is about, and every seat's own.
        "seat": label,
        "seats": seats,
    }


def _usage_tier_cap(ceiling: int, now: int | None = None) -> int:
    """Usage-tapered concurrency cap within ``[1, ceiling]``.

    <50% used -> 3, 50-75% -> 2, >75% -> 1 (of whichever window is most consumed);
    unknown usage -> ceiling (fail open). Never 0 — the >=100% case is the caller's
    preflight (EX_QUOTA), not the slot loop.
    """
    pct, _ = read_codex_usage(now)
    if pct is None:
        return ceiling
    if pct < 50.0:
        tier = 3
    elif pct <= 75.0:
        tier = 2
    else:
        tier = 1
    return max(1, min(tier, ceiling))


def _max_concurrent(flag_value: int | None) -> int:
    """Resolve the cap: -j flag -> $CODEX_IN_CLAUDE_MAX_CONCURRENT -> default 3.

    A value <= 0 disables gating (unlimited).
    """
    if flag_value is not None:
        return flag_value
    env = os.environ.get("CODEX_IN_CLAUDE_MAX_CONCURRENT")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            print(
                f"WARN: ignoring non-integer CODEX_IN_CLAUDE_MAX_CONCURRENT={env!r}.",
                file=sys.stderr,
            )
    return DEFAULT_MAX_CONCURRENT


@contextlib.contextmanager
def _concurrency_slot(ceiling: int, poll: float = 3.0) -> Iterator[None]:
    """Hold a cross-process slot for the body, capping concurrent codex runs.

    The pool is *ceiling* flock files, but the effective cap is **usage-tapered**
    within ``[1, ceiling]`` and recomputed every poll: <50% used -> 3, 50-75% -> 2,
    >75% -> 1 (see :func:`_usage_tier_cap`); unknown usage -> ceiling. Only the first
    ``cap`` slots are ever tried, so as usage climbs fewer *new* runs are admitted
    while in-flight ones finish (fewer sessions to reload after a reset). A slot
    auto-releases if its holder dies (the OS drops the flock). ``ceiling <= 0``
    disables gating. Fail-open: any setup error runs ungated.
    """
    if ceiling <= 0:
        yield
        return
    try:
        slot_dir = _slot_dir()
        slot_dir.mkdir(parents=True, exist_ok=True)
        handles: list[TextIO] = [
            open(slot_dir / f"slot{i}.lock", "w", encoding="utf-8") for i in range(ceiling)
        ]
    except OSError as exc:
        print(f"… concurrency gate disabled ({exc}); running ungated.", file=sys.stderr)
        yield
        return
    held: TextIO | None = None
    try:
        ticks = 0
        while held is None:
            cap = _usage_tier_cap(ceiling)  # usage-tapered, recomputed each poll
            for handle in handles[:cap]:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = handle
                    break
                except OSError:
                    continue
            if held is None:
                if ticks % 10 == 0:  # heartbeat every ~30s so a waiting task looks alive
                    print(
                        f"… waiting for a Codex slot (usage-tapered cap {cap}/{ceiling})…",
                        file=sys.stderr,
                        flush=True,
                    )
                ticks += 1
                time.sleep(poll)
        yield
    finally:
        if held is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        for handle in handles:
            handle.close()


def cmd_runs(args: argparse.Namespace) -> int:
    """List in-flight delegate runs from their heartbeat files (one cheap read each).

    The supervised runner refreshes a tiny JSON per run every ~5s, so this shows
    elapsed/idle time, output volume, and the last output line of every live delegate
    WITHOUT reading any transcript. A run that exited normally leaves a final record
    with ``ended`` set: not listed here, left for the retention prune (the statusline
    reader still wants it for a few minutes). Heartbeats whose process is gone WITHOUT
    that record (crash, SIGKILL) are cleaned up on sight.
    """
    rows: list[dict[str, Any]] = []
    runs_dir = _runs_dir()
    _prune_ended_heartbeats(runs_dir)
    for path in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if data.get("ended"):
            continue  # normal completion, retained on purpose
        try:
            os.kill(pid, 0)  # liveness probe only
        except ProcessLookupError:
            with contextlib.suppress(OSError):
                path.unlink()  # stale heartbeat from a killed run
            continue
        except PermissionError:
            pass
        rows.append(data)
    if args.json:
        print(json.dumps(rows))
        return EX_OK
    if not rows:
        print("no active delegate runs.")
        return EX_OK
    for data in rows:
        elapsed, idle = int(data.get("elapsed_s", 0)), int(data.get("idle_s", 0))
        slept, trouble = int(data.get("slept_s") or 0), int(data.get("trouble") or 0)
        health = (
            (" · caffeinated" if data.get("caffeinate_pid") else "")
            + (f" · slept {slept // 60}m{slept % 60:02d}s" if slept else "")
            + (f" · {trouble} trouble line(s) since progress" if trouble else "")
        )
        print(
            f"pid {data['pid']} · {data.get('model', '?')}/{data.get('effort', '?')}"
            f"{' · write' if data.get('write') else ''} · {data.get('repo', '?')} · "
            f"elapsed {elapsed // 60}m{elapsed % 60:02d}s · idle {idle}s · "
            f"{data.get('lines', 0)} lines{health} · last: {data.get('last_line', '')!r}"
        )
    return EX_OK


def cmd_usage(args: argparse.Namespace) -> int:
    """Print the current Codex rate-limit usage (5h + weekly windows)."""
    windows = _codex_usage_windows()
    if args.json:
        payload = {
            key: (
                {
                    "used_percent": win[0],
                    "resets_at": win[1],
                    "window_minutes": minutes,
                }
                if win is not None
                else None
            )
            for key, minutes, win in (
                ("five_hour", _FIVE_HOUR_MINUTES, windows["five_hour"]),
                ("seven_day", _SEVEN_DAY_MINUTES, windows["seven_day"]),
            )
        }
        print(json.dumps(payload))
        return EX_OK
    parts = []
    for label, key in (("5h", "five_hour"), ("weekly", "seven_day")):
        win = windows[key]
        parts.append(
            f"{label} —"
            if win is None
            else f"{label} {win[0]:.0f}% (resets {_format_reset(win[1])})"
        )
    print("codex usage: " + "  ·  ".join(parts))
    return EX_OK


def _headroom_unknown(reason: str) -> dict[str, Any]:
    """A :func:`codex_headroom`-shaped ``unknown`` decision (offload denied)."""
    return {
        "state": "unknown",
        "offload_allowed": False,
        "reason": reason,
        "refused_by": "",
        "refused_at": None,
        "captured_at": None,
        "stale_after_seconds": _HEADROOM_STALE_AFTER_SECONDS,
        "reset_fresh_within_seconds": _HEADROOM_RESET_FRESH_SECONDS,
        "windows": [],
        "seat": "",
        "seats": [],
    }


def cmd_headroom(args: argparse.Namespace) -> int:
    """Report whether optional Codex offloads can spend quota beyond the reserve.

    The verdict is the whole seat POOL's (plan D4): the ``seat:`` line names the seat it
    is about, the window lines are that seat's, and one line per OTHER seat says why it
    did not win — so "DENIED (unknown)" can no longer hide a fresh, idle paid seat.

    A crash while computing the verdict (e.g. a missing dependency, tp#394) is reported
    as ``unknown`` / exit 3 — the gate's documented fail-closed code — not as exit 1,
    which callers read as "reserve zone".
    """
    try:
        decision = codex_headroom()
    except Exception as exc:  # pylint: disable=broad-exception-caught  # any crash = unknown
        decision = _headroom_unknown(f"unknown: {type(exc).__name__}: {exc}")
    if args.json:
        print(json.dumps(decision))
    else:
        if decision.get("seat"):
            email = next(
                (
                    str(seat.get("email") or "")
                    for seat in decision.get("seats") or []
                    if seat.get("label") == decision["seat"]
                ),
                "",
            )
            print(f"seat: {decision['seat']}" + (f" ({email})" if email else ""))
        for window in decision["windows"]:
            print(
                f"{window['duration']}: {window['used_percent']:.0f}% used, "
                f"resets {_format_reset(window['resets_at'])}, "
                f"reserve {window['reserve_percent']:.0f}% "
                f"(reserve_source {window['reserve_source']}), "
                f"{str(window['verdict']).upper()}"
            )
        if decision["state"] == "allowed":
            print("offload: ALLOWED")
        elif decision["state"] == "blocked":
            print(f"offload: DENIED (blocked — {decision['reason']})")
        elif decision["state"] == "reserve":
            print(f"offload: DENIED (reserve zone — {decision['reason']})")
        else:
            print(f"offload: DENIED (unknown — {decision['reason']})")
        for seat in decision.get("seats") or []:
            if seat.get("label") != decision.get("seat"):
                print(f"{seat['label']}: {seat['state']} — {seat['reason']}")
    # "blocked" shares the reserve exit code: the documented contract is 0 = offload
    # allowed, non-zero = keep the work on Claude, and 3 already means "no usable data".
    return {"allowed": 0, "reserve": 1, "blocked": 1, "unknown": 3}[decision["state"]]


# --------------------------------------------------------------------------- #
# Run-time refusal classification
#
# `codex exec --json` emits one JSON object per line. A refusal is reported as an
# `error` event plus a `turn.failed` event carrying the SERVER's own message; the
# process exits non-zero and writes NO `-o` file. Captured live on 2026-09-04
# (codex 0.152.1) — see tests/fixtures/codex_json/. Two rules make this safe:
#
# * only the failure EVENTS are read. Item text (the model's own words) and the
#   prompt are never classified: a task that merely mentions "rate limit" or pastes
#   a 401 traceback must not look like a quota refusal and hop to another seat;
# * a message that matches nothing is a TASK failure, not a seat failure — no hop,
#   no cooldown. Blocking a healthy seat on `server_overloaded` would delete a
#   working rung for an hour over a transient.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CodexFailure:
    """Why one ``codex exec`` failed, classified from its own structured output.

    ``kind`` is ``"quota"`` / ``"entitlement"`` / ``"auth"``, or ``""`` for a plain
    task failure (or an unrecognised one — same thing for routing purposes: keep the
    seat, report the error). ``resets_at`` is never available from the ``--json``
    stream (the rate-limit block lives in the rollout file, which ``--ephemeral``
    suppresses), so it is ``None`` here and the deadline is derived from the seat's
    own usage by :func:`record_seat_refusal`.
    """

    kind: str
    detail: str
    resets_at: int | None


# The server's own error CODES, when it sends one (`codex_error_info`). Authoritative:
# a code needs no phrase matching and cannot drift with wording.
_FAILURE_CODES = {
    "usage_limit_exceeded": "quota",
    "usage_not_included": "entitlement",
    "unauthorized": "auth",
    "login_required": "auth",
    "invalid_grant": "auth",
    "token_expired": "auth",
}
# Allowlists over the server-authored MESSAGE, in priority order. Deliberately narrow:
# anything not listed is a task failure, which keeps the seat.
_FAILURE_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quota", ("out of credits", "usage limit", "rate limit", "too many requests")),
    ("entitlement", ("not included",)),
)
_AUTH_RE = re.compile(
    r"not logged in|unauthori[sz]ed|log in again|token .{0,30}expired|invalid_grant",
    re.IGNORECASE,
)
_STDERR_CLASSIFY_LINES = 40  # tail read only when there is no failure EVENT at all


def parse_json_events(stdout: str) -> list[dict]:
    """Every JSON object on its own line in *stdout* (junk lines skipped).

    ``codex exec --json`` interleaves nothing else, but a wrapper, a shell warning or
    a partial write can, so a non-JSON line is dropped rather than failing the parse.
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _failure_events(events: list[dict]) -> list[dict]:
    """The ``error`` / ``turn.failed`` events — the ONLY classification input."""
    return [e for e in events if str(e.get("type") or "") in ("error", "turn.failed")]


def _classify_text(text: str) -> tuple[str, str]:
    """``(kind, matched phrase)`` for one server-authored message; ``("", "")`` if none."""
    lowered = (text or "").lower()
    for kind, phrases in _FAILURE_PHRASES:
        for phrase in phrases:
            if phrase in lowered:
                return kind, phrase
    match = _AUTH_RE.search(text or "")
    if match:
        return "auth", match.group(0)
    return "", ""


def _event_error_code(event: dict) -> str:
    """The ``codex_error_info`` code on a failure event, top-level or under ``error``."""
    nested = event.get("error")
    for container in (event, nested if isinstance(nested, dict) else {}):
        info = container.get("codex_error_info")
        if isinstance(info, str) and info in _FAILURE_CODES:
            return info
        if isinstance(info, dict):
            for key in ("type", "code", "kind", "reason"):
                value = info.get(key)
                if isinstance(value, str) and value in _FAILURE_CODES:
                    return value
    return ""


def _event_message(event: dict) -> str:
    """The server-authored message of a failure event (never item text)."""
    for candidate in (event.get("message"), (event.get("error") or {})):
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, dict) and isinstance(candidate.get("message"), str):
            return str(candidate["message"])
    return ""


def classify_codex_failure(returncode: int, events: list[dict], stderr: str) -> CodexFailure:
    """Decide whether a finished ``codex exec`` failed, and whether the SEAT is at fault.

    ``returncode == 0`` is success, full stop. A non-zero exit is ALWAYS a failure —
    even when the ``-o`` file holds a partial reply. Printing that partial answer as
    the result (what the pre-2026-09-04 ``if not reply and rc != 0`` guard did) turned
    a refused run into a plausible-looking half answer.

    Evidence, strictest first: the failure events' ``codex_error_info`` code, then
    their message against the allowlists. Only when there is NO failure event at all
    (a crash before the stream started) does the same matching run over the last
    ``40`` stderr lines — never over stdout item text, never over the prompt.
    """
    if returncode == 0:
        return CodexFailure("", "", None)
    failures = _failure_events(events)
    for event in failures:
        code = _event_error_code(event)
        if code:
            return CodexFailure(_FAILURE_CODES[code], code, None)
        kind, phrase = _classify_text(_event_message(event))
        if kind:
            return CodexFailure(kind, phrase, None)
    if failures:
        return CodexFailure("", "", None)  # the server named a failure we do not act on
    tail = "\n".join((stderr or "").splitlines()[-_STDERR_CLASSIFY_LINES:])
    kind, phrase = _classify_text(tail)
    return CodexFailure(kind, phrase, None)


def _quota_deadline(home: Path, now: int) -> int:
    """When a quota-refused seat comes back: its exhausted window's reset, else +1 h.

    The ``--json`` stream carries no rate-limit block, so the deadline comes from the
    seat's own usage (live cache or rollout). One hour is the deliberate floor: long
    enough not to re-refuse immediately, short enough that a mis-read never parks a
    healthy seat for a day.
    """
    try:
        from . import usage  # pylint: disable=import-outside-toplevel  # heavy, per-refusal only

        exhausted = usage.codex_exhausted_window(usage.read_codex_usage(now, home), now)
        if exhausted is not None:
            return int(exhausted[1].resets_at)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass
    return now + 3600


def record_seat_refusal(cand: SeatCandidate, failure: CodexFailure, now: int | None = None) -> None:
    """Block *cand* in the cooldown store for what its own refusal proves. Never raises.

    Quota → until the exhausted window resets (else one hour). Entitlement and auth →
    24 h with a scope, because neither heals on a rate-limit boundary: an unpaid or
    logged-out seat stays unpaid or logged out until a human acts, and re-probing it
    every hour just burns the caller's time.

    A candidate with no ``pid`` (an unregistered ``$CODEX_HOME``) records NOTHING —
    there is no row that names that seat, and attributing its refusal to a label it
    merely resembles would block the wrong login.
    """
    if not cand.pid or not failure.kind:
        return
    now_ts = int(time.time()) if now is None else int(now)
    if failure.kind == "quota":
        blocked_until = failure.resets_at or _quota_deadline(cand.home, now_ts)
        scope = "quota"
    else:
        blocked_until = now_ts + 86400
        scope = failure.kind
    try:
        from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

        quota.record_block(
            cand.pid,
            blocked_until=blocked_until,
            reason=f"codex exec refused: {failure.kind} — {failure.detail}",
            source="codex-exec",
            observed_at=now_ts,
            scope=scope,
        )
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass  # telemetry: a failed write must never change the run's outcome


def record_seat_success(cand: SeatCandidate, now: int | None = None) -> None:
    """Clear *cand*'s OBSERVED block after a seat served. Never touches a hold.

    A working seat disproves a recorded rejection, but says nothing about an
    administrative reservation ("do not use the team seat this week") — hence
    ``observed_only``.
    """
    if not cand.pid:
        return
    try:
        from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

        quota.clear_block(cand.pid, observed_at=now, observed_only=True)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass


# --------------------------------------------------------------------------- #
# The runner — the ONE place ccc starts `codex exec`
# --------------------------------------------------------------------------- #
@dataclass
class RunAttempt:  # pylint: disable=too-many-instance-attributes  # flat attempt record
    """One physical (or skipped) attempt on one seat, for the machine envelope."""

    seat: str
    home: str
    elapsed_s: float
    # "ok" | "refused:<kind>" | "failed" | "timeout" | "stalled" | "network" | "slept" |
    # "startup_timeout" | "skipped:rota" (somebody else's week — not waivable by -Q) |
    # "skipped:exhausted" | "skipped:unmeasured" (write run, no weekly
    # reading) | "skipped:below-floor" (-F/--min-remaining) | "skipped:reserve" /
    # "skipped:unknown" (-H/--headroom)
    outcome: str
    # What codex reported on its ``turn.completed`` event (0 when the run never got
    # there), and when the attempt ENDED — an attempt is recorded after the fact, so the
    # construction time is its end. Both feed the run ledger (:mod:`codex_ledger`).
    tokens_in: int = 0
    tokens_out: int = 0
    ended_at: float = field(default_factory=time.time)
    # The ATTEMPT's identity, unique per physical attempt: the ledger line's
    # ``attempt_id`` and the ``run -j`` envelope's, so a caller that records its own row
    # (ai.py's ``attempt_ids``) can reference exactly the lines this attempt wrote.
    attempt_id: str = field(default_factory=codex_ledger.new_attempt_id)


@dataclass
class RunResult:  # pylint: disable=too-many-instance-attributes  # flat result record
    """The outcome of a whole ``run_with_fallback`` call, hops included."""

    ok: bool = False
    reply: str = ""
    seat: SeatCandidate | None = None
    attempts: list[RunAttempt] = field(default_factory=list)
    # "" | disabled | all_seats_unavailable | attempts_exhausted | codex_failed |
    # timeout | stalled | network | slept | startup_timeout | seat_refused_midrun | no_codex
    # | seat_unknown / seat_unavailable / seat_refused (``run -S``: the one confined seat
    # is not registered / was not eligible / refused — never a hop to another seat)
    error_kind: str = ""
    error_message: str = ""
    earliest_reset: int | None = None
    proc: subprocess.CompletedProcess[str] | None = None
    session_id: str = ""
    changed_paths: list[str] = field(default_factory=list)


# Item types that prove codex only TALKED. Anything else in an `item.completed`
# (a file change, a command execution, a patch) means it touched the world, so a
# refusal mid-run must be reviewed rather than retried on another seat.
_BENIGN_ITEM_TYPES = ("agent_message", "reasoning")


def _events_show_side_effects(events: list[dict]) -> bool:
    """True when the stream shows codex did something other than talk."""
    for event in events:
        if str(event.get("type") or "") != "item.completed":
            continue
        item = event.get("item")
        itype = str(item.get("type") or "") if isinstance(item, dict) else ""
        if itype not in _BENIGN_ITEM_TYPES:
            return True
    return False


def _turn_usage(events: list[dict]) -> tuple[int, int]:
    """``(input_tokens, output_tokens)`` summed over the ``turn.completed`` events, or zeros.

    The stream's ``usage`` block is the only token accounting an ephemeral run leaves
    behind (no rollout file), so the ledger takes it from here.
    """
    tokens_in = tokens_out = 0
    for event in events:
        if str(event.get("type") or "") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        with contextlib.suppress(TypeError, ValueError):
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
    return tokens_in, tokens_out


def _agent_message_of(events: list[dict]) -> str:
    """The last ``agent_message`` item's text — the reply when no ``-o`` file survived.

    Never a substitute for classification: this is read ONLY on a rc-0 run whose
    ``--output-last-message`` file came back empty.
    """
    text = ""
    for event in events:
        if str(event.get("type") or "") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            value = item.get("text")
            if isinstance(value, str) and value.strip():
                text = value.strip()
    return text


def _thread_id_of(events: list[dict]) -> str:
    """The codex thread (session) id from the ``thread.started`` event, or ``""``."""
    for event in events:
        if str(event.get("type") or "") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def seat_status_report(now: int | None = None) -> tuple[list[str], int | None]:
    """``(one line per seat, earliest reset)`` — why nothing is eligible right now.

    A ROTA block names itself (``rota: 14.9.–20.9. used by alice``): its reason is the
    week and the holder, which without the prefix reads like a quota figure. This is the
    only evidence a caller gets when an explicit ``$CODEX_HOME`` or a journal resume ends
    in ``all_seats_unavailable``, so it must say what kind of block it is.
    """
    now_ts = int(time.time()) if now is None else int(now)
    try:
        from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

        rows = quota.snapshot(now=now_ts).get("codex_seat_order") or []
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ([], None)
    lines: list[str] = []
    resets: list[int] = []
    for row in rows:
        bits: list[str] = []
        if row.get("reason"):
            reason = str(row["reason"])
            if row.get("blocked_by") == "rota" and not reason.startswith("rota:"):
                reason = f"rota: {reason}"  # a fail-closed reason already says it
            bits.append(reason)
        resets_at = int(row.get("resets_at") or 0)
        if resets_at > now_ts:
            resets.append(resets_at)
            bits.append(f"unblocks {_format_reset(resets_at, now_ts)}")
        detail = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"{row.get('label', '?')}: {row.get('state', '?')}{detail}")
    return lines, (min(resets) if resets else None)


def _changed_paths(before: list[str] | None, after: list[str] | None) -> list[str]:
    """Worktree entries that appeared between two ``git status --porcelain`` reads."""
    return sorted(set(after or []) - set(before or []))


def _attempt_codex(  # pylint: disable=too-many-locals
    cand: SeatCandidate,
    *,
    prompt: str,
    build_cmd: BuildCmd,
    write: bool,
    workdir: codex_launch.Workdir | str,
    remaining: int,
    idle_timeout: int,
    purpose: str,
    model: str,
    effort: str,
    heartbeat_meta: dict[str, Any],
    heartbeat_path: Path,
) -> tuple[subprocess.CompletedProcess[str], str, list[str] | None]:
    """ONE supervised ``codex exec`` on *cand*: ``(proc, reply, worktree before)``.

    Everything seat-specific is rebuilt here, never reused from a previous attempt:
    the permission profile and the MCP disable flags come from THIS home's
    ``config.toml``, the ``-o`` file is fresh (a stale one would let a refused seat's
    partial answer be read as the next seat's reply), and the env's ``CODEX_HOME``
    is overridden explicitly.

    Raises whatever :func:`_exec_codex` raises; the cost row is written either way.
    """
    perm_args = codex_launch.permission_args(write, codex_home=cand.home)
    mcp_args = codex_launch.mcp_disable_args(cand.home)
    handle, out_path = tempfile.mkstemp(prefix="ccc-codex-", suffix=".txt")
    os.close(handle)
    cmd = build_cmd(cand, out_path, perm_args, mcp_args)
    env = codex_exec_env(home=cand.home)
    env["CCC_NO_CODEX"] = "1"  # never let a nested run re-trigger the codex automation
    env["CCC_INTERNAL"] = "1"
    env["AI_NO_AUTOCOMMIT"] = "1"  # never let a nested run auto-commit
    before = _git_status(workdir) if write else None
    cost_before = codex_cost_snapshot(cand.home)
    try:
        try:
            proc = _exec_codex(
                cmd,
                env=env,
                timeout=remaining,
                idle_timeout=idle_timeout,
                heartbeat_path=heartbeat_path,
                heartbeat_meta={**heartbeat_meta, "seat": cand.label},
                stdin_text=prompt,
            )
        finally:
            record_codex_run(
                purpose=purpose,
                model=model,
                effort=effort,
                before=cost_before,
                after=codex_cost_snapshot(cand.home),
                home=cand.home,
            )
        try:
            reply = Path(out_path).read_text(encoding="utf-8").strip()
        except OSError:
            reply = ""  # a refused run writes no -o file at all
    finally:
        with contextlib.suppress(OSError):
            os.unlink(out_path)
    return proc, reply, before


def ephemeral_default() -> bool:
    """Whether ``run`` passes ``--ephemeral``.

    The rule is the mirror image of the routing feedback (plan D6, debate O2): the
    ``codex exec --json`` stream carries NO ``rate_limits`` event (verified live
    2026-09-09 — four events, none of them a limit block), so an ephemeral run leaves
    NOTHING behind that measures the seat it just billed. With ``codex_usage`` OFF the
    rollout file is the only offline measurement there is, so these calls keep it (they
    stay UNJOURNALLED either way — only ``run -P`` journals); with it ON the runner
    fetches the live figures itself and the session file is pure litter again.

    A config read that fails answers True: leaving no session file is the conservative
    side of this choice.
    """
    try:
        from . import config  # pylint: disable=import-outside-toplevel

        return bool(config.load_config().codex_usage)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return True


def _usage_feedback_on() -> bool:
    """True when the live-usage opt-in (``codex_usage``) is configured."""
    return ephemeral_default()


def write_floor_percent() -> float:
    """The default ``--min-remaining`` for a WRITE run, as a percentage.

    A write round that dies half-way through leaves a worktree to review
    (``SEAT-REFUSED-MIDRUN``), so it must not START on a seat that is one round from
    empty. Once ten debate samples exist the learned P95 cost of a single round is the
    honest floor; before that, a flat tenth of the window (plan D5).
    """
    reserve, source = _headroom_reserve(_SEVEN_DAY_MINUTES)
    if source != "learned":
        return _WRITE_FLOOR_BOOTSTRAP_PCT
    return reserve / _ROUNDS_PER_RESERVE


def _candidate_row(cand: SeatCandidate, now: int) -> quota.ProviderQuota:
    """*cand*'s quota row — the SAME evidence the ranking used (plan D5, debate O4).

    The runner used to preflight with a bare rollout read, which could veto a seat whose
    newer live snapshot said it was fine. Going through the row means selection and
    preflight can never disagree; an unregistered ``$CODEX_HOME`` gets a row built for
    its own path rather than a second code path.
    """
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    cooldowns = quota.read_cooldowns(now)
    if cand.pid:
        for row in quota._codex_quotas(now, cooldowns):  # noqa: SLF001
            if row.id == cand.pid:
                return row
    return quota._codex_seat_quota(cand.pid, cand.label, cand.home, now, cooldowns)  # noqa: SLF001


def _below_floor(row: quota.ProviderQuota, floor: float) -> str:
    """The name of a fresh window with less than *floor* % left, or ``""``."""
    for name, win in sorted(row.windows.items()):
        if not win.stale and (100.0 - win.used_pct) < floor:
            return name
    return ""


def _pre_selection_refresh(candidates: list[SeatCandidate], budget: float) -> None:
    """Refresh every stale/unmeasured candidate's live figures, once, inside *budget*.

    Best effort in the strongest sense: each fetch is capped, the aggregate is capped,
    and every failure is swallowed (plan D6, debate O13). Ranking on slightly old
    numbers is a mild inefficiency; making a run wait on the network is a regression.
    A module-level function so tests can monkeypatch the whole step.
    """
    from . import config, usage  # pylint: disable=import-outside-toplevel

    try:
        stale_after = int(config.load_config().codex_usage_refresh_sec)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        stale_after = 600
    now = int(time.time())
    deadline = time.monotonic() + budget
    seen: set[str] = set()
    for cand in candidates:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        home = str(cand.home)
        if home in seen:
            continue
        seen.add(home)
        try:
            row = _candidate_row(cand, now)
            if row.captured_at and now - row.captured_at <= stale_after and row.windows:
                continue
            usage.fetch_codex_usage(cand.home, now, timeout=min(_REFRESH_FETCH_TIMEOUT_SEC, left))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            continue  # a seat we could not measure simply stays where the ranking put it


def _post_attempt_refresh(cand: SeatCandidate, budget: float) -> None:
    """Re-measure the seat we just billed, so the NEXT ranking sees what it cost.

    Only worth doing when there is real time left (*budget*): a fetch started with five
    seconds of the call's deadline remaining cannot finish and would only delay the
    caller's own error. Monkeypatchable, and every failure is swallowed.
    """
    from . import usage  # pylint: disable=import-outside-toplevel

    if budget < _REFRESH_FETCH_TIMEOUT_SEC:
        return
    try:
        usage.fetch_codex_usage(cand.home, timeout=_REFRESH_FETCH_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        pass


def _budget_left(total_timeout: int, started: float) -> float:
    """Seconds left of the whole call's budget; a huge number when it is unlimited."""
    if not total_timeout:
        return float(_REFRESH_BUDGET_SEC * 10)
    return float(total_timeout) - (time.monotonic() - started)


def _record_attempt(cand: SeatCandidate) -> None:
    """Stamp *cand* into the attempt ledger — the round-robin tiebreak's only input."""
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    with contextlib.suppress(Exception):
        quota.record_seat_attempt(cand.pid)


def _claim_probe(cand: SeatCandidate) -> bool:
    """Win the once-a-day probe of unmeasured seat *cand*; False ⇒ someone else has it."""
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    try:
        return quota.claim_probe(cand.pid)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False  # cannot claim ⇒ do not probe; the ranking has other seats


def _rota_configured() -> bool:
    """True when ANY seat is on a ``codex_seat_rota`` (so a row must always be built).

    Cheap (a memoized config read) and fail-safe in the direction that costs least: on
    any doubt it answers False, which only means ``-Q`` keeps its old shortcut on a
    machine with no rota at all.
    """
    from . import config  # pylint: disable=import-outside-toplevel

    try:
        return bool(config.codex_seat_rota())
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return False


def _skip_reason(  # pylint: disable=too-many-return-statements  # one per skip outcome
    cand: SeatCandidate,
    *,
    ignore_quota: bool,
    write: bool,
    min_remaining_pct: float,
    headroom: bool,
) -> str:
    """The ``RunAttempt.outcome`` for a seat that must NOT be launched, or ``""``.

    Every check reads ONE quota row (plan D5, debate O4), so the preflight cannot
    contradict the ranking that produced the candidate. Order matters: the seat ROTA
    first (a policy prohibition, not a quota fact), then proven-exhausted (the cheapest
    and most certain), then the write-mode rules, then the optional headroom gate.

    ``-Q``/``ignore_quota`` waives a QUOTA verdict — "try it even at 100 %, I accept the
    refusal". It deliberately does not waive a rota: the seat belongs to somebody else
    this week, and no amount of accepting a refusal makes billing their week ours
    (debate O7). So whenever a rota is configured the row is built even under ``-Q``,
    and ``skipped:rota`` outranks it. A human override exists — ``/switch <seat>!``.
    """
    if ignore_quota and not (write or min_remaining_pct or headroom or _rota_configured()):
        return ""  # nothing left to check — do not pay for a quota row
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    now = int(time.time())
    try:
        row = _candidate_row(cand, now)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ""  # a row we cannot build proves nothing — fail open, as everywhere else
    if row.block_scope == "rota":
        return "skipped:rota"
    if not ignore_quota and row.state == quota.BLOCKED:
        return "skipped:exhausted"
    if write and min_remaining_pct > 0 and quota._measured_week(row) is None:  # noqa: SLF001
        # A write run on a seat with no weekly reading may refuse mid-edit, and the
        # review that costs is exactly what the runner exists to avoid (plan D5). The
        # skip exists to make the floor checkable, so it goes with the floor: `-F 0`
        # ("no floor, I accept the risk") waives both — otherwise a fresh install with
        # no usage data anywhere could never start a write run at all.
        return "skipped:unmeasured"
    if min_remaining_pct and _below_floor(row, min_remaining_pct):
        return "skipped:below-floor"
    if headroom:
        state = str(seat_headroom(row, now)["state"])
        if state != "allowed":
            return f"skipped:{state}"
    return ""


def _no_seat_result(result: RunResult, now: int | None = None) -> RunResult:
    """Fill *result* in as ``all_seats_unavailable``, with per-seat evidence."""
    lines, earliest = seat_status_report(now)
    detail = ("\n  " + "\n  ".join(lines)) if lines else ""
    result.error_kind = "all_seats_unavailable"
    result.error_message = "no Codex seat is eligible right now:" + (
        detail or " (no seat configured)"
    )
    result.earliest_reset = earliest
    return result


def run_with_fallback(**kwargs: Any) -> RunResult:
    """Run one Codex round, hopping seats on a REFUSAL — the only launcher in ccc.

    The round itself is :func:`_run_with_fallback` (same keyword signature); this
    entrance only adds the run ledger: every PHYSICAL attempt the round made is
    appended to ``<app_home>/codex-runs.jsonl`` (:mod:`command_center.codex_ledger`)
    once the round has returned, whatever its outcome, each line carrying the caller's
    ``note`` (``run -N``) and ``caller`` (``run -X``). Best-effort — a ledger that
    cannot be written never fails or delays the call.
    """
    # ``note``/``caller`` are ledger-only context (``run -N``/``-X``): pop them before the
    # round, which does not know the keywords, so a caller's label can never turn into a
    # TypeError mid-run.
    note = str(kwargs.pop("note", "") or "")
    caller = str(kwargs.pop("caller", "") or "")
    result = _run_with_fallback(**kwargs)
    workdir = kwargs.get("workdir", "")
    codex_ledger.record_result(
        result,
        purpose=str(kwargs.get("purpose") or ""),
        model=str(kwargs.get("model") or ""),
        effort=str(kwargs.get("effort") or ""),
        prompt_chars=len(str(kwargs.get("prompt") or "")),
        write=bool(kwargs.get("write", False)),
        workdir=os.fspath(workdir) if workdir else "",
        note=note,
        caller=caller,
    )
    return result


def _seat_names(label: str, pid: str) -> set[str]:
    """Every spelling ``run -S`` accepts for one seat: its label (``private``), its oracle
    id (``codex:private``) and its display name (``codex-priv``)."""
    pid = pid or _seat_pid(label)
    return {label, pid, _quota_display_id(pid)}


def _quota_display_id(pid: str) -> str:
    """:func:`command_center.quota.display_id`, reached lazily (quota imports this module)."""
    from . import quota  # pylint: disable=import-outside-toplevel  # cycle: quota needs the pin

    return quota.display_id(pid)


def _confined(pending: list[SeatCandidate], seat: str) -> list[SeatCandidate]:
    """*pending* narrowed to the ONE seat ``run -S`` named (all of it when *seat* is "")."""
    if not seat:
        return pending
    return [c for c in pending if seat in _seat_names(c.label, c.pid)]


def _seat_result(result: RunResult, seat: str) -> RunResult:
    """Fill *result* in for a ``run -S`` seat that did not serve: never a hop, one kind.

    ``seat_unknown`` = no registered seat answers to *seat*; ``seat_refused`` = it was
    launched and refused; ``seat_unavailable`` = it was not eligible (held, blocked, a
    skip reason, the rota, or the ``$CODEX_HOME`` override names another home).
    """
    known = any(seat in _seat_names(label, "") for label in canonical_codex_homes())
    mine = [a for a in result.attempts if seat in _seat_names(a.seat, "")]
    last = mine[-1].outcome if mine else ""
    if not known and not mine:
        result.error_kind = "seat_unknown"
        result.error_message = f"no registered Codex seat is called {seat!r}"
    elif last.startswith("refused:"):
        result.error_kind = "seat_refused"
        result.error_message = (
            f"seat {seat} refused ({last.removeprefix('refused:')}) — -S never hops"
        )
    else:
        lines, result.earliest_reset = seat_status_report()
        why = last or "not eligible right now"
        result.error_message = f"seat {seat} is unavailable ({why}) — -S never hops" + (
            ("\n  " + "\n  ".join(lines)) if lines else ""
        )
        result.error_kind = "seat_unavailable"
    return result


def _run_with_fallback(  # pylint: disable=too-many-branches,too-many-statements,too-many-locals,too-many-return-statements
    *,
    prompt: str,
    build_cmd: BuildCmd,
    write: bool,
    workdir: codex_launch.Workdir | str,
    total_timeout: int,
    idle_timeout: int,
    purpose: str,
    model: str,
    effort: str,
    heartbeat_meta: dict[str, Any],
    resume_home: Path | None = None,
    ignore_quota: bool = False,
    persistent: bool = True,
    max_attempts: int = 0,
    on_attempt: Callable[[SeatCandidate, int], None] | None = None,
    headroom: bool = False,
    min_remaining_pct: float = 0.0,
    seat: str = "",
) -> RunResult:
    """The seat-hopping round behind :func:`run_with_fallback` (which adds the ledger).

    ``delegate`` and ``run`` both come through
    here, so "which seat, in which order, and what happens when it says no" has one
    implementation and one set of tests.

    The rules that matter:

    * **Candidates are re-read before every attempt** (cache-only, ~ms): a hold written
      while attempt 1 was running removes that seat from attempt 2. Seats already tried
      in this call are never retried.
    * **One deadline for the whole call.** ``total_timeout`` is the budget for the call,
      not per attempt; each attempt gets what is left, and fewer than 5 s left is a
      ``timeout`` rather than a doomed launch. ``0`` = unlimited (the idle watchdog
      still guards stalls).
    * **Only a seat refusal hops.** A task failure, a timeout, a stall and a missing
      binary are terminal: they say nothing about the seat, and hopping would re-run a
      failing task on a second account.
    * **In write mode a refusal that touched the worktree never hops** (``item.completed``
      of a non-benign type, a changed ``git status``, or a status we could not read):
      the caller must review a half-done edit, not get a second seat's attempt layered
      on top of it.
    * **``resume_home`` binds to one seat.** ``codex exec resume`` re-attaches to a
      session that exists in exactly one ``CODEX_HOME``; hopping would resume nothing.
      A resume therefore gets NO write floor and NO headroom filter either — there is no
      second seat to move to, so a filter could only turn a runnable resume into a
      refusal. The documented trade-off is a possible ``SEAT-REFUSED-MIDRUN`` review
      (plan D5).
    * **Five skip reasons, all evaluated from the seat's QUOTA ROW** (plan D5, debate
      O4), before any process: ``skipped:rota`` (a ``codex_seat_rota`` says this week
      belongs to somebody else — a POLICY block ``-Q`` cannot waive),
      ``skipped:exhausted`` (the row is BLOCKED — a window at
      100 %, a live refusal or a hold), ``skipped:unmeasured`` (a WRITE run with a floor
      refuses a seat whose weekly window is unknown — the floor cannot be checked there;
      ``min_remaining_pct == 0`` waives both), ``skipped:below-floor``
      (``min_remaining_pct``) and, in ``--headroom`` mode, ``skipped:reserve`` /
      ``skipped:unknown``. Using the row rather than a second rollout read is what stops
      an old rollout at 100 % from vetoing a seat whose newer live reading says 40 %.
    * **Feedback, when ``codex_usage`` is on** (plan D6): every stale candidate is
      refreshed once before selection and the attempted seat once after, both inside
      hard budgets and both best-effort. With the opt-in off, ``run`` keeps its session
      file instead (see :func:`ephemeral_default`) so the rollout measures the seat.

    * **``seat`` confines the call to ONE seat** (``run -S``): the candidate list is
      narrowed to it before every attempt, so a skip or a refusal ends the call with
      ``seat_unavailable`` / ``seat_refused`` (``seat_unknown`` for a name no seat
      answers to) instead of hopping. Nothing persistent changes — no pin, no order.

    Never raises for a seat problem — everything is reported through
    :attr:`RunResult.error_kind`. :class:`codex_launch.CodexLaunchError` (a launch this
    policy refuses, e.g. ``--write`` on a seat with no ``hardened-rw`` profile) DOES
    propagate: it is a configuration error the caller must show, not a seat to skip.
    """
    result = RunResult()
    if os.environ.get("CCC_NO_CODEX"):
        result.error_kind = "disabled"
        result.error_message = "Codex disabled (CCC_NO_CODEX)"
        return result
    # The deadline starts BEFORE the refresh: a best-effort measurement must be spent
    # out of the call's budget, never added on top of it (plan D5, debate O13).
    heartbeat_path = _runs_dir() / f"{os.getpid()}.json"
    started = time.monotonic()
    # A resume binds to one seat, so promoting an unmeasured seat would be meaningless;
    # a write run refuses to experiment at all (plan D5/D7).
    probe_ok = resume_home is None and not write
    if resume_home is not None:
        pending = [_seat_candidate_for(resume_home)]
    else:
        if _usage_feedback_on():
            with contextlib.suppress(Exception):
                _pre_selection_refresh(codex_homes_in_order(probe=probe_ok), _REFRESH_BUDGET_SEC)
        pending = codex_homes_in_order(probe=probe_ok)
    pending = _confined(pending, seat)
    if not pending:
        return _seat_result(result, seat) if seat else _no_seat_result(result)

    attempted: set[str] = set()
    physical = 0
    while True:
        if attempted and resume_home is None:
            # a hold written mid-run removes a seat
            pending = _confined(codex_homes_in_order(probe=probe_ok), seat)
        pending = [c for c in pending if str(c.home) not in attempted]
        if not pending:
            break
        cand = pending[0]
        attempted.add(str(cand.home))
        skip = _skip_reason(
            cand,
            ignore_quota=ignore_quota,
            write=write and resume_home is None,
            min_remaining_pct=0.0 if resume_home is not None else min_remaining_pct,
            headroom=headroom and resume_home is None,
        )
        if skip:
            result.attempts.append(RunAttempt(cand.label, str(cand.home), 0.0, skip))
            continue
        if cand.probe and not _claim_probe(cand):
            # Another runner took today's probe of this unmeasured seat: re-rank WITHOUT
            # it rather than spend a second round trip on the same experiment (plan D7).
            probe_ok = False
            attempted.discard(str(cand.home))
            pending = _confined(codex_homes_in_order(probe=False), seat)
            continue
        if max_attempts and physical >= max_attempts:
            result.error_kind = "attempts_exhausted"
            result.error_message = (
                f"stopped after {physical} attempt(s): the -n/--max-attempts budget is spent"
            )
            return result
        remaining = 0
        if total_timeout:
            remaining = int(total_timeout - (time.monotonic() - started))
            # The floor guards a RETRY only: a caller who explicitly asked for `-t 3`
            # gets its 3 seconds, but a hop with 4 s left would be killed during
            # codex's own startup and buy nothing but a cost row.
            if physical and remaining < _MIN_ATTEMPT_SECONDS:
                result.error_kind = "timeout"
                result.error_message = (
                    f"the {total_timeout}s budget ran out before seat {cand.label} could be tried"
                )
                return result
            remaining = max(1, remaining)
        if on_attempt is not None:
            on_attempt(cand, physical)
        if not cand.probe:
            # A claimed probe already wrote its attempt record — the claim IS the record.
            _record_attempt(cand)
        physical += 1
        attempt_started = time.monotonic()
        try:
            proc, reply, before = _attempt_codex(
                cand,
                prompt=prompt,
                build_cmd=build_cmd,
                write=write,
                workdir=workdir,
                remaining=remaining,
                idle_timeout=idle_timeout,
                purpose=purpose,
                model=model,
                effort=effort,
                heartbeat_meta=heartbeat_meta,
                heartbeat_path=heartbeat_path,
            )
        except FileNotFoundError:
            result.attempts.append(
                RunAttempt(cand.label, str(cand.home), time.monotonic() - attempt_started, "failed")
            )
            result.error_kind = "no_codex"
            result.error_message = "`codex` CLI not found on PATH."
            return result
        except subprocess.TimeoutExpired as exc:
            return _killed_result(
                result, cand, exc, attempt_started, kind="timeout", budget=total_timeout
            )
        except CodexStalledError as exc:
            return _killed_result(
                result, cand, exc, attempt_started, kind=_STALL_KINDS.get(exc.reason, "stalled")
            )
        elapsed = time.monotonic() - attempt_started
        if _usage_feedback_on():
            # Re-measure what this attempt actually cost, so the NEXT ranking (this call's
            # own hop included) is not made on pre-attempt figures. Never fatal.
            with contextlib.suppress(Exception):
                _post_attempt_refresh(cand, _budget_left(total_timeout, started))
        events = parse_json_events(proc.stdout or "")
        failure = classify_codex_failure(proc.returncode, events, proc.stderr or "")
        result.proc = proc
        result.session_id = _thread_id_of(events) or (_session_id_of(proc.stderr or "") or "")
        tokens_in, tokens_out = _turn_usage(events)

        if not failure.kind and proc.returncode == 0:
            record_seat_success(cand)
            result.ok = True
            result.reply = reply or _agent_message_of(events)
            result.seat = cand
            result.attempts.append(
                RunAttempt(cand.label, str(cand.home), elapsed, "ok", tokens_in, tokens_out)
            )
            if persistent and result.session_id:
                codex_launch.record_launch(
                    result.session_id, workdir, write=write, codex_home=cand.home
                )
            if write:
                result.changed_paths = _changed_paths(before, _git_status(workdir))
            return result

        if not failure.kind:
            # Non-zero exit the seat is not to blame for: report it, keep the seat.
            result.attempts.append(
                RunAttempt(cand.label, str(cand.home), elapsed, "failed", tokens_in, tokens_out)
            )
            result.seat, result.reply = cand, reply
            result.error_kind = "codex_failed"
            result.error_message = (
                f"Codex exited {proc.returncode}:\n{_stderr_tail(proc.stderr or '', 20)}"
            )
            return result

        record_seat_refusal(cand, failure)
        result.attempts.append(
            RunAttempt(
                cand.label,
                str(cand.home),
                elapsed,
                f"refused:{failure.kind}",
                tokens_in,
                tokens_out,
            )
        )
        if write:
            after = _git_status(workdir)
            unknown = before is None or after is None
            if _events_show_side_effects(events) or unknown or before != after:
                if persistent and result.session_id:
                    codex_launch.record_launch(
                        result.session_id, workdir, write=write, codex_home=cand.home
                    )
                result.seat = cand
                result.changed_paths = _changed_paths(before, after)
                result.error_kind = "seat_refused_midrun"
                result.error_message = (
                    f"seat {cand.label} refused ({failure.kind}: {failure.detail}) AFTER it had "
                    + (
                        "started changing the workspace"
                        if not unknown
                        else "started work and the worktree state is unknown (git status failed)"
                    )
                    + " — not retried on another seat; review the worktree."
                )
                return result
        nxt = next(
            (
                c.label
                for c in _confined(codex_homes_in_order(), seat)
                if str(c.home) not in attempted
            ),
            "",
        )
        print(
            f"seat {cand.label} refused: {failure.kind} ({failure.detail}) — "
            + (f"falling back to {nxt}" if nxt else "no seat left to try"),
            file=sys.stderr,
        )
    return _seat_result(result, seat) if seat else _no_seat_result(result)


# CodexStalledError.reason -> RunResult.error_kind
_STALL_KINDS = {
    "stalled": "stalled",
    "network": "network",
    "slept": "slept",
    "startup": "startup_timeout",
}


def _stall_headline(exc: CodexStalledError) -> str:
    """The human explanation for a progress-watchdog kill, by its reason."""
    slept = f"~{exc.slept_seconds // 60}m{exc.slept_seconds % 60:02d}s"
    if exc.reason == "network":
        return (
            "Codex lost its network connection: it kept reporting connection trouble and made "
            f"no progress for {exc.idle_seconds}s awake (whole process tree killed). Check the "
            "network/VPN and retry."
        )
    if exc.reason == "slept":
        return (
            f"The machine was suspended for {slept} during this round and Codex made no "
            f"progress for {exc.idle_seconds}s after waking (whole process tree killed). The "
            "runner's caffeinate assertion blocks IDLE sleep only — a closed lid or battery "
            "sleep still suspends the round; keep the lid open and retry."
        )
    if exc.reason == "startup":
        return (
            f"Codex never got a model response: no item event within {exc.idle_seconds}s awake "
            "of launch (whole process tree killed). The network is down or model discovery "
            "hung; check the network/VPN and retry."
        )
    text = (
        f"Codex stalled — no progress for {exc.idle_seconds}s (whole process tree killed). "
        "Likely a hung network/CLI; retry, or tune --idle-timeout."
    )
    if exc.slept_seconds:
        text += f" The machine was suspended for {slept} during the run."
    return text


def _killed_result(
    result: RunResult,
    cand: SeatCandidate,
    exc: subprocess.TimeoutExpired | CodexStalledError,
    attempt_started: float,
    *,
    kind: str,
    budget: int = 0,
) -> RunResult:
    """Fill *result* in for a run WE killed (wall timeout or the progress watchdog). Terminal.

    A kill says nothing about the seat, so there is no hop and no cooldown; what the
    caller needs is the session id, so the very same context can be resumed instead of
    paying for the discovery again — and, for a watchdog kill, WHY: ``network``, ``slept``
    and ``startup_timeout`` each name their cause and the fix, so a debate transcript says
    "the laptop slept" instead of "Codex failed".
    """
    if isinstance(exc, CodexStalledError):
        stderr_text, stdout_text = exc.stderr_text, exc.stdout_text
        headline = _stall_headline(exc)
        if exc.last_trouble:
            headline += f" Codex's last report: {exc.last_trouble}"
    else:
        stderr_text, stdout_text = str(exc.stderr or ""), str(exc.output or "")
        headline = (
            f"Codex hit the {budget}s wall timeout (whole process tree killed). Retry with a "
            "larger -t (or -t 0), a tighter task, or lower effort."
        )
    session = _thread_id_of(parse_json_events(stdout_text)) or (_session_id_of(stderr_text) or "")
    tail = _stderr_tail(stderr_text)
    result.session_id = session
    result.seat = cand
    result.attempts.append(
        RunAttempt(cand.label, str(cand.home), time.monotonic() - attempt_started, kind)
    )
    result.error_kind = kind
    result.error_message = (
        headline
        + (f" Resume its context with --resume {session}." if session else "")
        + (f" Last output:\n{tail}" if tail else "")
    )
    return result


def _run_error_exit(result: RunResult) -> int:
    """Print a failed :class:`RunResult` on stderr and map it to this CLI's exit code.

    Shared by ``delegate`` and ``run`` so both report the same typed failures with the
    same codes — a caller that branches on the exit code sees one contract.
    """
    kind = result.error_kind
    print(f"ERROR: {result.error_message}", file=sys.stderr)
    if kind in ("all_seats_unavailable", "attempts_exhausted"):
        if result.earliest_reset:
            print(
                f"earliest seat reset: {_format_reset(result.earliest_reset)}",
                file=sys.stderr,
            )
        return EX_QUOTA
    return {
        "disabled": EX_QUOTA,
        "seat_unavailable": EX_QUOTA,
        "seat_refused": EX_QUOTA,
        "seat_unknown": EX_USAGE,
        "no_codex": EX_NO_CODEX,
        "timeout": EX_TIMEOUT,
        "stalled": EX_TIMEOUT,
        "startup_timeout": EX_TIMEOUT,
        "network": EX_NETWORK,
        "slept": EX_NETWORK,
        "codex_failed": EX_CODEX_FAIL,
        "seat_refused_midrun": EX_CODEX_FAIL,
    }.get(kind, EX_CODEX_FAIL)


def _seat_line(cand: SeatCandidate, index: int) -> str:
    """The ``seat:`` line — the second stdout line of every delegate/run round."""
    suffix = " [fallback]" if index else ""
    return f"seat: {cand.label} ({cand.email or 'unknown account'}){suffix}"


def _idle_timeout_for(explicit: int | None, wall_timeout: int) -> int:
    """The stall watchdog: explicit ``-i``, else min(default, wall), else the default."""
    if explicit is not None:
        return max(0, explicit)
    if wall_timeout:
        return min(DEFAULT_IDLE_TIMEOUT, wall_timeout)
    return DEFAULT_IDLE_TIMEOUT  # an unlimited wall still guards stalls


def _min_remaining_pct(args: argparse.Namespace, *, write: bool) -> float:
    """Resolve ``-F/--min-remaining``: explicit value, else the write floor, else none.

    A READ-ONLY run has no floor by default — the worst case is a refusal and a hop. A
    WRITE run defaults to :func:`write_floor_percent`, because a seat that runs out
    mid-edit costs a worktree review. ``-F 0`` disables it explicitly (plan D5).
    """
    raw = getattr(args, "min_remaining", None)
    if raw is not None:
        return max(0.0, float(raw))
    return write_floor_percent() if write else 0.0


def _wants_ignore_quota(args: argparse.Namespace) -> bool:
    """``-Q`` or ``$CODEX_IN_CLAUDE_IGNORE_QUOTA=1`` — skip the per-seat preflight."""
    return bool(getattr(args, "ignore_quota", False)) or (
        os.environ.get("CODEX_IN_CLAUDE_IGNORE_QUOTA") == "1"
    )


def cmd_delegate(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements,too-many-locals
    """Run one Codex round. First stdout line = the model, second = the seat.

    The seat is chosen (and re-chosen on a refusal) by :func:`run_with_fallback`; this
    function owns the delegate CONTRACT — the two guaranteed leading lines, the
    prompt's contract/budget/repo-map assembly, the concurrency slot, and the
    ``### SESSION`` / ``### CODEX-WROTE`` trailers the skill parses.
    """
    if not args.prompt.strip():
        print("ERROR: empty prompt.", file=sys.stderr)
        return EX_USAGE
    model = resolve_model("delegate-review")
    if args.model:
        resolved = _model_arg(args.model)
        if not resolved:
            return EX_INVALID_MODEL
        model = resolved
    effort = args.effort or resolve_effort()  # None -> let codex use the model's default
    shown_effort = effort or effort_of(model)
    # The guaranteed first line — captured/printed by us, never Claude preamble.
    print(f"model: {model} (effort {shown_effort})", flush=True)

    write = args.write and not args.scout  # scouting is always read-only (plan, no edits)
    resume = getattr(args, "resume", None)
    # ONE launch policy for every `codex exec` (see command_center.codex_launch): the
    # binary, the validated `-C` root, and the NAMED permission profile (rebuilt per seat
    # inside the runner). Never `-s`/`--sandbox` — that forces Codex's legacy sandbox and
    # silently drops the profile's deny rules (credential stores, .env/*.pem, network).
    resume_home: Path | None = None
    try:
        codex_bin = codex_launch.resolve_codex()
        resume_record: codex_launch.LaunchRecord | None = None
        if resume:
            resume_record, resume_home = codex_launch.resolve_resume_any(
                str(resume), write=write, homes=canonical_codex_homes()
            )
        workdir = codex_launch.resolve_workdir(
            resume_record.resolved_cwd if resume_record is not None else args.cwd,
            write=write,
        )
    except codex_launch.CodexMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_NO_CODEX
    except codex_launch.CodexLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_USAGE
    run_cwd = str(workdir)
    wall_timeout = _effective_timeout(args.timeout, shown_effort)
    idle_timeout = _idle_timeout_for(getattr(args, "idle_timeout", None), wall_timeout)
    repo_map = (
        None
        if (resume or getattr(args, "no_repo_map", False))
        else _repo_map(run_cwd, explicit=getattr(args, "repo_map", None))
    )
    # Advisory only: pointered tasks rarely need xhigh's exploration depth.
    if (
        args.effort is None
        and shown_effort == "xhigh"
        and re.search(r"\.\w{1,5}:\d+|\blines? \d+", args.prompt)
    ):
        print(
            "hint: the task contains file:line pointers — xhigh mostly buys exploration "
            "depth those pointers make unnecessary; consider -e high.",
            file=sys.stderr,
        )
    prompt = _build_delegate_prompt(
        args.prompt,
        write=write,
        feedback=args.feedback,
        round_no=args.round,
        scout=args.scout,
        budget_minutes=(max(1, wall_timeout // 60) if wall_timeout else None),
        idle_minutes=(max(1, idle_timeout // 60) if idle_timeout else None),
        repo_map=repo_map,
    )

    def build_cmd(
        _cand: SeatCandidate, out_path: str, perm_args: list[str], _mcp_args: list[str]
    ) -> list[str]:
        """The delegate argv for one seat. MCP servers stay ENABLED (a real round).

        The prompt is NOT here: it travels on stdin (trailing ``-``), because a repo map
        plus a revision round easily exceeds ``ARG_MAX`` and an argv prompt is readable
        by every ``ps`` on the machine.
        """
        if resume_record is not None:
            # `codex exec resume` has no -C: the workspace root is INHERITED from the
            # original session, which is why resolve_resume_any re-validated it.
            cmd = [codex_bin, "exec", "resume", resume_record.session_id]
            cmd += ["--json", "-o", out_path, *perm_args, "-m", model]
        else:
            cmd = [codex_bin, "exec", "--json", *perm_args]
            cmd += ["-o", out_path, "-m", model, "-C", run_cwd]
            if workdir.skip_git_check:
                cmd.append("--skip-git-repo-check")  # only where the root is not a repo
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd.append("-")  # read the prompt from stdin
        return cmd

    if getattr(args, "show_prompt", False):
        # Dry run: show exactly what codex WOULD receive, then stop. Lets a
        # caller sanity-check the assembled contract/budget/repo-map cheaply.
        candidates = codex_homes_in_order()
        seat = candidates[0] if candidates else None
        print("### DRY RUN (no codex launched)")
        print(_seat_line(seat, 0) if seat is not None else "seat: (no eligible seat)")
        try:
            preview = build_cmd(
                seat or SeatCandidate("none", Path.home() / ".codex", "", ""),
                "<OUTPUT>",
                codex_launch.permission_args(write, codex_home=seat.home if seat else None),
                [],
            )
        except codex_launch.CodexLaunchError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EX_USAGE
        print("command: " + " ".join(preview) + "   (prompt on stdin)")
        print("--- PROMPT ---")
        print(prompt)
        return EX_OK

    heartbeat_meta = {
        "model": model,
        "effort": shown_effort,
        "repo": run_cwd,
        "write": write,
    }
    try:
        # Take one concurrency slot around the WHOLE runner call (hops included); the
        # rest of a fan-out waits. A slot per attempt could deadlock a fan-out mid-hop.
        with _concurrency_slot(_max_concurrent(args.max_concurrent)):
            result = run_with_fallback(
                prompt=prompt,
                build_cmd=build_cmd,
                write=write,
                workdir=workdir,
                total_timeout=wall_timeout,
                idle_timeout=idle_timeout,
                purpose=getattr(args, "purpose", "delegate"),
                model=model,
                effort=shown_effort,
                heartbeat_meta=heartbeat_meta,
                resume_home=resume_home,
                ignore_quota=_wants_ignore_quota(args),
                persistent=True,
                max_attempts=max(0, int(getattr(args, "max_attempts", 0) or 0)),
                headroom=bool(getattr(args, "headroom", False)),
                min_remaining_pct=_min_remaining_pct(args, write=write),
                on_attempt=lambda cand, index: print(_seat_line(cand, index), flush=True),
            )
    except codex_launch.CodexLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_USAGE

    if result.ok:
        print(result.reply)
        if result.session_id:
            # The journal (CODEX_HOME/ccc-sessions.jsonl, 0600) was written by the runner
            # BEFORE we report the id: `--resume <id>` is only honoured for sessions this
            # policy started, and it re-checks the root + mode against today's rules.
            print(f"\n### SESSION\n{result.session_id}")
        if result.changed_paths:
            print("\n### CODEX-WROTE (review this diff)\n" + "\n".join(result.changed_paths))
        return EX_OK
    if result.error_kind == "seat_refused_midrun":
        label = result.seat.label if result.seat is not None else "?"
        print(f"\n### SEAT-REFUSED-MIDRUN {label} — review the worktree")
        if result.changed_paths:
            print("\n".join(result.changed_paths))
    return _run_error_exit(result)


def cmd_run(args: argparse.Namespace) -> int:
    """``run`` — the versioned machine entry point external consumers call.

    Deliberately NOT ``delegate``: no patch/write contract, no repo map, no journal,
    read-only and ephemeral by default. It is the thinnest possible wrapper around
    :func:`run_with_fallback`, so ``codex-review.py`` and other drivers get the seat
    order, the run-time fallback and the typed errors without re-implementing any of
    it — and without inheriting the delegate skill's stdout contract.
    """
    prompt = args.prompt
    if prompt is None or prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        print(
            "ERROR: empty prompt (pass PROMPT, or `-` with the prompt on stdin).", file=sys.stderr
        )
        return EX_USAGE
    model = resolve_model(args.purpose or None)
    if args.model:
        resolved = _model_arg(args.model)
        if not resolved:
            return EX_INVALID_MODEL
        model = resolved
    effort = args.effort or resolve_effort()
    shown_effort = effort or effort_of(model)
    as_json = bool(getattr(args, "json", False))
    ephemeral = bool(getattr(args, "ephemeral", False)) or ephemeral_default()
    if not as_json:
        print(f"model: {model} (effort {shown_effort})", flush=True)
    try:
        codex_bin = codex_launch.resolve_codex()
        workdir = codex_launch.resolve_workdir(args.cwd, write=False)
    except codex_launch.CodexMissing as exc:
        return _run_cli_report(
            _failed(RunResult(), "no_codex", str(exc)), args, model, shown_effort
        )
    except codex_launch.CodexLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_USAGE
    persist = bool(getattr(args, "persist", False))

    def build_cmd(
        _cand: SeatCandidate, out_path: str, perm_args: list[str], mcp_args: list[str]
    ) -> list[str]:
        """A read-only, MCP-free ``codex exec`` for one seat; prompt on stdin."""
        cmd = [codex_bin, "exec", "--json"]
        if not persist and ephemeral:
            # `--ephemeral` leaves no session file — and therefore no `rate_limits`
            # block, which is the ONLY offline measurement of what this run cost (the
            # --json stream carries none). So it is dropped whenever the live-usage
            # opt-in is off, unless -E/--ephemeral asks for it (plan D6, debate O2).
            # The run stays UNJOURNALLED either way: only -P journals.
            cmd.append("--ephemeral")
        cmd += [*perm_args, *mcp_args, "-o", out_path, "-m", model, "-C", str(workdir)]
        if workdir.skip_git_check:
            cmd.append("--skip-git-repo-check")
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd.append("-")
        return cmd

    wall_timeout = _effective_timeout(args.timeout, shown_effort)
    try:
        result = run_with_fallback(
            prompt=prompt,
            build_cmd=build_cmd,
            write=False,
            workdir=workdir,
            total_timeout=wall_timeout,
            idle_timeout=_idle_timeout_for(getattr(args, "idle_timeout", None), wall_timeout),
            purpose=args.purpose or "run",
            note=getattr(args, "note", "") or "",
            caller=getattr(args, "caller", "") or "",
            seat=getattr(args, "seat", "") or "",
            model=model,
            effort=shown_effort,
            heartbeat_meta={
                "model": model,
                "effort": shown_effort,
                "repo": str(workdir),
                "write": False,
            },
            ignore_quota=_wants_ignore_quota(args),
            persistent=persist,
            max_attempts=max(0, int(getattr(args, "max_attempts", 0) or 0)),
            headroom=bool(getattr(args, "headroom", False)),
            min_remaining_pct=_min_remaining_pct(args, write=False),
            on_attempt=(
                None
                if as_json
                else (lambda cand, index: print(_seat_line(cand, index), flush=True))
            ),
        )
    except codex_launch.CodexLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_USAGE
    return _run_cli_report(result, args, model, shown_effort)


def _failed(result: RunResult, kind: str, message: str) -> RunResult:
    """Stamp a pre-launch failure onto a :class:`RunResult` (so ``-j`` still answers)."""
    result.error_kind, result.error_message = kind, message
    return result


def _run_cli_report(result: RunResult, args: argparse.Namespace, model: str, effort: str) -> int:
    """Print ``run``'s answer (one JSON object with ``-j``, else text) and exit-code it."""
    if getattr(args, "json", False):
        payload = {
            "schema_version": 1,
            "model": model,
            "effort": effort,
            "ok": result.ok,
            "runner_pid": os.getpid(),
            "seat": (
                None
                if result.seat is None
                else {
                    "label": result.seat.label,
                    "id": result.seat.pid,
                    "home": str(result.seat.home),
                    "email": result.seat.email,
                }
            ),
            "attempts": [
                {
                    "seat": a.seat,
                    "home": a.home,
                    "elapsed_s": round(a.elapsed_s, 3),
                    "outcome": a.outcome,
                    # The ledger line's `attempt_id`; null for a skip (no line, no spend).
                    "attempt_id": None if a.outcome.startswith("skipped:") else a.attempt_id,
                }
                for a in result.attempts
            ],
            # Every ledger line this call wrote, oldest first — what a caller that files
            # its own row records as `attempt_ids`, so `ai logs` can hide exactly these.
            "attempt_ids": [
                a.attempt_id for a in result.attempts if not a.outcome.startswith("skipped:")
            ],
            "reply": result.reply,
            "error": (
                None
                if result.ok
                else {
                    "kind": result.error_kind,
                    "message": result.error_message,
                    "earliest_reset": result.earliest_reset,
                }
            ),
            "session_id": result.session_id,
        }
        print(json.dumps(payload))
        return EX_OK if result.ok else _run_exit_code(result)
    if result.ok:
        print(result.reply)
        if result.session_id and getattr(args, "persist", False):
            print(f"\n### SESSION\n{result.session_id}")
        return EX_OK
    return _run_error_exit(result)


def _run_exit_code(result: RunResult) -> int:
    """The exit code for a failed run, WITHOUT printing anything (the ``-j`` path)."""
    if result.error_kind in (
        "all_seats_unavailable",
        "attempts_exhausted",
        "disabled",
        "seat_unavailable",
        "seat_refused",
    ):
        return EX_QUOTA
    return {
        "seat_unknown": EX_USAGE,
        "no_codex": EX_NO_CODEX,
        "timeout": EX_TIMEOUT,
        "stalled": EX_TIMEOUT,
        "startup_timeout": EX_TIMEOUT,
        "network": EX_NETWORK,
        "slept": EX_NETWORK,
    }.get(result.error_kind, EX_CODEX_FAIL)


def _git_status(cwd: codex_launch.Workdir | str | None) -> list[str] | None:
    """``git status --porcelain`` lines for *cwd*, or ``None`` when UNKNOWN.

    Tri-state on purpose: ``[]`` means "read it, the worktree is clean" and ``None``
    means "could not read it" (git missing, not a repo, timeout, non-zero exit). The
    old version collapsed both to ``[]``, so a failed probe before a write run and a
    failed probe after it compared EQUAL — "nothing changed" — and a seat that had
    already started editing could be silently retried on another account.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=(os.fspath(cwd) if cwd else None),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (every flag has a short form)."""
    parser = argparse.ArgumentParser(
        prog="codex-in-claude.py",
        description="Manage Codex model choice for Claude Code commands; delegate tasks to Codex.",
        epilog=(
            "Examples:\n"
            "  codex-in-claude.py models --refresh\n"
            "  codex-in-claude.py pick                     # interactive picker (+ help sync)\n"
            "  codex-in-claude.py set-model gpt-5.6-sol --for all\n"
            "  codex-in-claude.py get-model --for debate\n"
            "  codex-in-claude.py sync-skills --check      # is /codex… help in sync?\n"
            "  codex-in-claude.py headroom --json          # optional-offload quota gate\n"
            "  codex-in-claude.py delegate --write -C . 'add retry to fetch()'\n"
            "  codex-in-claude order private de default    # seat order (+ clears the pin)\n"
            "  codex-in-claude policy fill                 # rank by weekly reset, not order\n"
            "  codex-in-claude rota set default -s 2026-09-14 alice bob   # share a seat weekly\n"
            "  codex-in-claude run -j -C . 'reply OK'      # machine entry point (-j envelope)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_models = sub.add_parser("models", help="list available Codex models")
    p_models.add_argument(
        "-r", "--refresh", action="store_true", help="refresh via `codex debug models`"
    )
    p_models.add_argument(
        "-H", "--include-hidden", action="store_true", help="include hidden models"
    )
    p_models.set_defaults(func=cmd_models)

    p_get = sub.add_parser(
        "get-model", help="print the model resolved for a command, or the slug a NAME means"
    )
    p_get.add_argument(
        "name",
        nargs="?",
        default=None,
        help="a slug or short name to resolve (astra -> gpt-6-astra); exit 3 = unknown",
    )
    p_get.add_argument(
        "-f",
        "--for",
        dest="for_command",
        default=None,
        metavar="PURPOSE",
        help="the command or `run -p` purpose to answer for (delegate-review, debate, "
        "checker, …; default: global) — what `resolve_model` gives that purpose, so a "
        "purpose with no key of its own prints the global model; ignored when NAME is given",
    )
    p_get.set_defaults(func=cmd_get_model)

    p_alias = sub.add_parser(
        "alias",
        help="list/define/delete short model names (alias astra gpt-6-astra)",
        description=(
            "No arguments: list every short name (the catalog codenames are built in). "
            "NAME alone: print what it resolves to. NAME SLUG: define it in the config "
            "(wins over the built-in codename). -d NAME: delete a config alias."
        ),
    )
    p_alias.add_argument("name", nargs="?", default=None, help="the short name, e.g. astra")
    p_alias.add_argument("slug", nargs="?", default=None, help="the model slug it stands for")
    p_alias.add_argument("-d", "--delete", action="store_true", help="delete the config alias NAME")
    p_alias.set_defaults(func=cmd_alias)

    p_set = sub.add_parser("set-model", help="set the model for a command (or global default)")
    p_set.add_argument("slug", help="model slug or short name, e.g. gpt-5.6-sol or astra")
    p_set.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=(*COMMANDS, "all"),
        default=None,
        help="command (default/all = global; 'all' also clears per-command pins)",
    )
    p_set.set_defaults(func=cmd_set_model)

    p_pick = sub.add_parser("pick", help="interactive model picker (terminal only)")
    p_pick.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=(*COMMANDS, "all"),
        default=None,
        help="command to set (default: all)",
    )
    p_pick.add_argument(
        "-r", "--refresh", action="store_true", help="refresh via `codex debug models`"
    )
    p_pick.set_defaults(func=cmd_pick)

    p_sync = sub.add_parser(
        "sync-skills", help="stamp the model into the codex skill/command descriptions"
    )
    p_sync.add_argument(
        "-c",
        "--check",
        action="store_true",
        help="report drift without writing (exit 1 = stale, 2 = unparsable)",
    )
    p_sync.set_defaults(func=cmd_sync_skills)

    p_geteff = sub.add_parser("get-effort", help="print the configured reasoning effort")
    p_geteff.set_defaults(func=cmd_get_effort)

    p_seteff = sub.add_parser("set-effort", help="set the global reasoning effort")
    p_seteff.add_argument(
        "level", choices=(*EFFORTS, "default"), help="reasoning level (default = each model's own)"
    )
    p_seteff.set_defaults(func=cmd_set_effort)

    p_del = sub.add_parser("delegate", help="run one Codex round (prints model first)")
    p_del.add_argument("prompt", help="the task for Codex")
    p_del.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="let Codex edit files (the hardened-rw permission profile; refused when the "
        "active CODEX_HOME config.toml does not define one)",
    )
    p_del.add_argument(
        "-S",
        "--scout",
        action="store_true",
        help="read-only plan only (no diff) — for a pre-implementation scout round",
    )
    p_del.add_argument(
        "-C",
        "--cwd",
        default=None,
        help="repo dir Codex works in (codex -C). Refused for $HOME or any dir above it; "
        "omit it only inside a git work tree",
    )
    p_del.add_argument("-r", "--round", type=int, default=1, help="loop round (1-based)")
    p_del.add_argument(
        "-f", "--feedback", default=None, help="Claude's review feedback for a revision round"
    )
    p_del.add_argument(
        "-m",
        "--model",
        default=None,
        help="override model — slug or short name (else resolved from config)",
    )
    p_del.add_argument(
        "-p",
        "--purpose",
        default="delegate",
        help="cost-instrumentation purpose label (default: delegate; e.g. debate, review)",
    )
    p_del.add_argument(
        "-e",
        "--effort",
        choices=EFFORTS,
        default=None,
        help="override reasoning effort (else config/model default)",
    )
    p_del.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=None,
        help="wall-clock seconds before the run is killed (default scales with effort: "
        "low 600, medium 900, high 1500, xhigh 2700; 0 = no wall limit — the idle "
        "watchdog still guards stalls)",
    )
    p_del.add_argument(
        "-i",
        "--idle-timeout",
        type=int,
        default=None,
        metavar="SECS",
        help="kill the run after this long with NO codex PROGRESS — codex's own error events "
        "and ERROR/WARN log lines do not count (default min(900, wall timeout), or 900 "
        "with -t 0; shrinks to 120 once codex reports network trouble and to 180 after "
        "the machine slept; 0 disables every idle-based kill)",
    )
    p_del.add_argument(
        "-R",
        "--resume",
        default=None,
        metavar="SESSION",
        help="resume a previous round's codex session (UUID from the ### SESSION line, "
        "or 'last') — keeps its discovered context; permissions/cwd inherit from that "
        "session, so only ccc-launched sessions with a matching read/write mode are allowed",
    )
    p_del.add_argument(
        "-M",
        "--no-repo-map",
        action="store_true",
        help="do not inject the repo orientation map (repo_scope_short.md or a git "
        "top-level summary) into the prompt",
    )
    p_del.add_argument(
        "-P",
        "--repo-map",
        default=None,
        metavar="FILE",
        help="inject THIS file as the repo orientation map instead of the automatic "
        "repo_scope_short.md / git-summary choice (caller-curated context)",
    )
    p_del.add_argument(
        "-n",
        "--show-prompt",
        action="store_true",
        help="dry run: print the assembled codex command and prompt, launch nothing",
    )
    p_del.add_argument(
        "-j",
        "--max-concurrent",
        type=int,
        default=None,
        metavar="N",
        help="cap simultaneous Codex runs across all delegate processes "
        "(default 3, or $CODEX_IN_CLAUDE_MAX_CONCURRENT; 0 = unlimited)",
    )
    p_del.add_argument(
        "-Q",
        "--ignore-quota",
        action="store_true",
        help="skip the per-seat quota preflight (try a seat even at >=100%% used)",
    )
    p_del.add_argument(
        "-N",
        "--max-attempts",
        type=int,
        default=0,
        metavar="N",
        help="stop after N physical seat attempts (default 0 = try every eligible seat)",
    )
    p_del.add_argument(
        "-H",
        "--headroom",
        action="store_true",
        help="only bill a seat whose live windows are outside their reserve (the same "
        "verdict `headroom` reports, per seat); others are skipped as skipped:reserve",
    )
    p_del.add_argument(
        "-F",
        "--min-remaining",
        type=float,
        default=None,
        metavar="PCT",
        help="skip a seat with less than PCT%% left on any live window (default: 0 for a "
        "read-only run, the learned single-round cost for --write; 0 disables the floor "
        "AND lets a write run start on a seat with no weekly reading)",
    )
    p_del.set_defaults(func=cmd_delegate)

    p_run = sub.add_parser(
        "run",
        help="run ONE read-only Codex round with seat fallback (machine entry point)",
        description=(
            "The versioned entry point external consumers should call instead of "
            "`codex exec`: it tries the configured Codex seats in order, falls through "
            "at run time when one is held/exhausted/refusing, and reports a typed "
            "result. Read-only and ephemeral; -j prints one JSON object."
        ),
    )
    p_run.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="the prompt ('-' or omitted = read it from stdin)",
    )
    p_run.add_argument(
        "-C", "--cwd", default=None, help="repo dir codex reads (codex -C); refused for $HOME"
    )
    p_run.add_argument(
        "-m", "--model", default=None, help="override the resolved model (slug or short name)"
    )
    p_run.add_argument(
        "-e", "--effort", choices=EFFORTS, default=None, help="override the reasoning effort"
    )
    p_run.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=None,
        metavar="SECS",
        help="wall-clock budget for the WHOLE call, hops included (0 = unlimited)",
    )
    p_run.add_argument(
        "-i",
        "--idle-timeout",
        type=int,
        default=None,
        metavar="SECS",
        help="kill the run after this long with NO codex PROGRESS (default 900; 120 once codex "
        "reports network trouble, 180 after the machine slept; 0 disables)",
    )
    p_run.add_argument(
        "-p",
        "--purpose",
        default="run",
        help="cost-instrumentation purpose label (e.g. debate, checker)",
    )
    p_run.add_argument(
        "-N",
        "--note",
        default="",
        metavar="TEXT",
        help="context note written to the run ledger on every attempt of this call "
        "(e.g. the ticket: '#255'); shown by `ai logs`",
    )
    p_run.add_argument(
        "-S",
        "--seat",
        default="",
        metavar="SEAT",
        help="run on THIS seat only (label `private`, id `codex:private` or name "
        "`codex-priv`): no fallback to another seat, nothing persistent changes; an "
        "ineligible or refusing seat fails with seat_unavailable / seat_refused",
    )
    p_run.add_argument(
        "-X",
        "--caller",
        default="",
        metavar="NAME",
        help="who is calling (e.g. 'ai.py:<attempt_id>'), written to the run ledger as "
        "`caller` on every attempt",
    )
    p_run.add_argument(
        "-n",
        "--max-attempts",
        type=int,
        default=0,
        metavar="N",
        help="stop after N physical seat attempts (default 0 = try every eligible seat)",
    )
    p_run.add_argument(
        "-P",
        "--persist",
        action="store_true",
        help="keep the codex session (no --ephemeral) and journal it, so --resume works",
    )
    p_run.add_argument(
        "-Q",
        "--ignore-quota",
        action="store_true",
        help="skip the per-seat quota preflight (try a seat even at >=100%% used)",
    )
    p_run.add_argument(
        "-E",
        "--ephemeral",
        action="store_true",
        help="force --ephemeral (no session file). Default: ephemeral only while "
        "codex_usage is on — the rollout is otherwise the only record of what the run cost",
    )
    p_run.add_argument(
        "-H",
        "--headroom",
        action="store_true",
        help="only bill a seat whose live windows are outside their reserve (the same "
        "verdict `headroom` reports, per seat); others are skipped as skipped:reserve",
    )
    p_run.add_argument(
        "-F",
        "--min-remaining",
        type=float,
        default=None,
        metavar="PCT",
        help="skip a seat with less than PCT%% left on any live window (default: 0 for a "
        "read-only run, the learned single-round cost for --write; 0 disables the floor "
        "AND lets a write run start on a seat with no weekly reading)",
    )
    p_run.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="one JSON object: {schema_version, model, effort, ok, seat, attempts, "
        "attempt_ids, reply, error}",
    )
    p_run.set_defaults(func=cmd_run)

    p_usage = sub.add_parser("usage", help="show current Codex rate-limit usage (5h + weekly)")
    p_usage.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_usage.set_defaults(func=cmd_usage)

    p_headroom = sub.add_parser(
        "headroom",
        help="check quota reserve before optional Codex offloads",
        description=(
            "Check whether every live Codex quota window has enough remaining reserve "
            "for optional offload work. Missing/stale data fails closed; debates remain "
            "always allowed and should not use this gate."
        ),
    )
    p_headroom.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_headroom.set_defaults(func=cmd_headroom)

    p_home = sub.add_parser(
        "home",
        help="show/set/clear the Codex account pin (which CODEX_HOME every Codex call bills)",
        description=(
            "Show which CODEX_HOME the next Codex call bills, and pin one login (e.g. "
            "~/.codex-private) until an ISO date (inclusive). The pin is INERT while an "
            "explicit seat order is configured (see `order`); an explicit $CODEX_HOME in "
            "the environment overrides everything and disables the fallback."
        ),
    )
    p_home.add_argument("path", nargs="?", help="CODEX_HOME to pin (needs its auth.json)")
    p_home.add_argument("-u", "--until", help="pin expires after this ISO date (inclusive)")
    p_home.add_argument("-c", "--clear", action="store_true", help="remove the pin")
    p_home.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="machine-readable selection: {home, source, label, email, until, homes}",
    )
    p_home.set_defaults(func=cmd_home)

    p_order = sub.add_parser(
        "order",
        help="show/set the order every Codex consumer tries the seats in",
        description=(
            "The Codex seat order (ccc config.toml `codex_seat_order`): the labels of "
            "the configured ChatGPT logins in the sequence every consumer TRIES them, "
            "with run-time fallback when one is held, exhausted or refusing. Setting an "
            "order clears the `home` account pin and makes it inert. No labels = show."
        ),
    )
    p_order.add_argument(
        "labels", nargs="*", help="seat labels in attempt order (e.g. private de default)"
    )
    p_order.add_argument(
        "-c", "--clear", action="store_true", help="drop the order (back to canonical)"
    )
    p_order.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="machine-readable: {configured, order, unknown, next_attempt, candidates, pin}",
    )
    p_order.set_defaults(func=cmd_order)

    p_policy = sub.add_parser(
        "policy",
        help="show/set HOW the seats are ranked (fill = resets-soonest first | order)",
        description=(
            "`fill` (default) spends the seat whose WEEKLY allowance resets soonest — the "
            "allowance about to be thrown away — filling seats that reset within 12h of "
            "each other equally; `codex_seat_order` is then only the tiebreak. `order` "
            "tries the seats strictly in that configured order. No argument = show."
        ),
    )
    p_policy.add_argument(
        "policy", nargs="?", choices=("fill", "order"), help="the policy to set (omit = show)"
    )
    p_policy.add_argument("-j", "--json", action="store_true", help="machine-readable: {policy}")
    p_policy.set_defaults(func=cmd_policy)

    p_rota = sub.add_parser(
        "rota",
        help="show/set the weekly rota of a SHARED seat (whose week is it?)",
        description=(
            "A seat shared with a colleague on alternating weeks (ccc config.toml "
            "`codex_seat_rota`): `label=YYYY-MM-DD@IANA_ZONE:name,name`, where the date is "
            "the MONDAY of the FIRST name's week and the names take turns from there, "
            "forever. During somebody else's week the seat is BLOCKED for every Codex "
            "consumer — the ranking, the pin, headroom, -Q, an explicit $CODEX_HOME, a "
            "resume — and only a human `/switch <seat>!` overrides it. An entry ccc cannot "
            "read, or one whose names do not include `rota me`, blocks that seat too "
            "(failing open would bill a colleague's week). No verb = show."
        ),
    )
    # ``-j`` lives on BOTH ``rota`` and ``rota show`` so either spelling works; the
    # subparser's default is SUPPRESS, or its False would overwrite ``rota -j show``.
    p_rota.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="machine-readable: {schema_version, me, seats, errors}",
    )
    # No ``verb`` default: argparse's subparsers dest is None when no verb is given,
    # and ``cmd_rota`` reads that as ``show`` (a bare ``rota`` is a report).
    p_rota.set_defaults(func=cmd_rota)
    rota_verbs = p_rota.add_subparsers(dest="verb")
    p_rota_show = rota_verbs.add_parser("show", help="who holds each shared seat this week")
    p_rota_show.add_argument(
        "-j",
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable: {schema_version, me, seats, errors}",
    )
    p_rota_show.set_defaults(func=cmd_rota)
    p_rota_set = rota_verbs.add_parser(
        "set", help="set one seat's rota: the first name's Monday, then the names"
    )
    p_rota_set.add_argument("label", help="the seat label (default / private / an extra)")
    p_rota_set.add_argument("names", nargs="+", help="the names taking turns, in turn order")
    p_rota_set.add_argument(
        "-s",
        "--start",
        required=True,
        metavar="YYYY-MM-DD",
        help="the MONDAY of the week the FIRST name holds the seat",
    )
    p_rota_set.add_argument(
        "-z",
        "--tz",
        metavar="ZONE",
        help="IANA zone the weeks are measured in (default: this machine's)",
    )
    p_rota_set.set_defaults(func=cmd_rota)
    p_rota_clear = rota_verbs.add_parser("clear", help="drop a seat's rota (it is ours again)")
    p_rota_clear.add_argument("label", help="the seat label to un-share")
    p_rota_clear.set_defaults(func=cmd_rota)
    p_rota_me = rota_verbs.add_parser("me", help="which rota name THIS machine's operator is")
    p_rota_me.add_argument("name", help="one of the names in the rota entries")
    p_rota_me.set_defaults(func=cmd_rota)

    p_runs = sub.add_parser(
        "runs", help="list in-flight delegate runs (elapsed/idle/last output, one line each)"
    )
    p_runs.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_runs.set_defaults(func=cmd_runs)
    return parser


def _dispatch(argv: list[str] | None) -> int:
    """Parse args and dispatch to the chosen subcommand; returns its exit code."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


def main(argv: list[str] | None = None) -> int:
    """Console entry point: :func:`_dispatch` behind the shared broken-pipe guard.

    `codex-in-claude rota | head -1` and `codex-in-claude --help | head -1` must exit
    quietly like any other Unix filter — see :mod:`command_center.brokenpipe`.
    """
    return brokenpipe.guard(lambda: _dispatch(argv))


if __name__ == "__main__":
    sys.exit(main())
