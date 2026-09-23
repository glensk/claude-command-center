#!/usr/bin/env python3
"""Resident panel server: the q+p / s+p floating panels in ≤ 0.2 s (tp#70).

A cold ``ccc park -g`` / ``ccc peek`` pays Python start-up, ccc's imports, the ~210 ms
AppKit framework load and ~280 ms per AppleScript focus query before a pixel shows. This
module keeps ONE process resident (opt-in LaunchAgent, ``ccc panel-server --install``)
with AppKit loaded, the park/peek modules imported and a warm iTerm2 Python-API link;
the Karabiner chord runs the POSIX poker (``assets/panel-poke.sh``) instead of ccc.

**Protocol (D1).** One file per request under :func:`config.app_home`: the poker writes
``panel_request.<nonce>`` (``<verb> <nonce> <epoch_s>``) atomically; the server CLAIMS it
by renaming it to ``panel_claimed.<nonce>`` before reading, the poker ABANDONS it by
renaming it to ``panel_abandoned.<nonce>`` — exactly one rename wins. Every claimed
request is answered with ``panel_ack.<nonce>`` = ``<status> <epoch>``, status one of
``shown`` (the panel's content is on screen), ``busy`` (another request is resolving or
a panel is open), ``stale`` (older than :data:`STALE_SEC` at claim — never shown) or
``failed`` (the server raised before display; the poker falls back cold). Housekeeping
unlinks claimed/abandoned/ack files after :data:`PURGE_SEC`.

**State (D3).** ``panel_server.pid`` = ``<pid> <state> <code_stamp>``, rewritten
atomically on every transition: ``starting → ready | degraded``, ``busy`` while a request
resolves or a panel is open, ``stopping``. The poker only pokes a live pid in ``ready``
or ``busy``; everything else runs today's cold command.

The file protocol, the pidfile, the metrics and the dispatcher (:class:`Core`) are plain
Python and unit-tested headlessly; :class:`AppKitHost` is the thin AppKit shell around
them (timers, modal panels, focus hand-back, App Nap token).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# AppKit / PyObjCTools load lazily (only the resident process needs them), and PyObjC
# resolves AppKit attributes dynamically — hence the two module-wide disables below.
# pylint: disable=import-outside-toplevel,no-member
# pylint: disable=too-many-lines  # one cohesive feature: protocol, dispatcher, AppKit host, CLI
import argparse
import contextlib
import fcntl
import json
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from . import config

VERBS = ("park", "peek")
STATUSES = ("shown", "busy", "stale", "failed")
POKEABLE_STATES = ("ready", "busy")

#: Request-poll period of both timers (dispatch in the default mode, busy-ack in the
#: modal-panel mode — V15: a timer whose callback runs the nested modal loop is never
#: re-fired, so the modal loop needs its own).
POLL_SEC = 0.05
#: A request older than this at claim is answered ``stale`` and never shown.
STALE_SEC = 2.0
#: Claimed / abandoned / ack files older than this are unlinked.
PURGE_SEC = 10.0
#: Bound on a peek resolution; past it the request is answered ``failed`` (→ cold)
#: while the poker is still in its second ack window (~1 s after the chord).
PEEK_RESOLVE_BOUND_SEC = 0.8
#: Bound on one iTerm-link query from a resolver thread.
LINK_CALL_SEC = 1.0
#: Degraded re-probe: first retry, then doubling up to the cap.
PROBE_MIN_SEC = 5.0
PROBE_MAX_SEC = 60.0
#: Grace the watchdog heal gives a busy server before re-exec'ing anyway.
HEAL_BUSY_GRACE_SEC = 60.0
#: Transcripts prewarmed into the cache after start (most recently active first).
PREWARM_SESSIONS = 8

FAULT_ENV = "CCC_PANEL_SERVER_FAULT"
FAULTS = ("before-claim", "after-claim-crash", "after-claim-raise", "after-show", "cookie-hang")
#: Credentials and focus hints a resident process must never inherit (V2, R17).
SCRUBBED_ENV = ("ITERM_SESSION_ID", "ITERM2_COOKIE", "ITERM2_KEY")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Paths:
    """Every file of the protocol, under one directory (normally ``config.app_home()``)."""

    home: Path

    @property
    def pidfile(self) -> Path:
        return self.home / "panel_server.pid"

    @property
    def lock(self) -> Path:
        return self.home / "panel_server.lock"

    @property
    def restart(self) -> Path:
        return self.home / "panel_restart"

    @property
    def metrics(self) -> Path:
        return self.home / "panel-metrics.jsonl"

    @property
    def log(self) -> Path:
        return self.home / "panel-server.log"

    @property
    def err(self) -> Path:
        return self.home / "panel-server.err"

    def request(self, nonce: str) -> Path:
        return self.home / f"panel_request.{nonce}"

    def claimed(self, nonce: str) -> Path:
        return self.home / f"panel_claimed.{nonce}"

    def abandoned(self, nonce: str) -> Path:
        return self.home / f"panel_abandoned.{nonce}"

    def ack(self, nonce: str) -> Path:
        return self.home / f"panel_ack.{nonce}"


def paths(home: Path | None = None) -> Paths:
    """The protocol paths under *home* (default: ``config.app_home()``)."""
    return Paths(home or config.app_home())


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* via a same-directory temp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Pidfile + code stamp
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PidInfo:
    """One parsed ``panel_server.pid``."""

    pid: int
    state: str
    stamp: int


def write_pidfile(p: Paths, pid: int, state: str, stamp: int) -> None:
    """Atomically publish ``<pid> <state> <stamp>`` (the poker reads it with ``read``)."""
    _atomic_write(p.pidfile, f"{pid} {state} {stamp}\n")


def read_pidfile(p: Paths) -> PidInfo | None:
    """The published server identity, or None when absent / garbage."""
    try:
        parts = p.pidfile.read_text(encoding="utf-8").split()
        return PidInfo(int(parts[0]), parts[1], int(parts[2]))
    except (OSError, ValueError, IndexError):
        return None


def remove_pidfile(p: Paths, pid: int | None = None) -> None:
    """Remove the pidfile — only if it still names *pid* when one is given."""
    if pid is not None:
        info = read_pidfile(p)
        if info is not None and info.pid != pid:
            return
    with contextlib.suppress(OSError):
        p.pidfile.unlink()


def pid_alive(pid: int) -> bool:
    """``kill -0`` semantics: True when *pid* exists (even if owned by someone else)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def code_stamp(root: Path | None = None) -> int:
    """Max ``mtime_ns`` over the package's ``*.py`` — the editable install's code version.

    The server stores it at start; a newer tree means the running server is stale (D8).
    """
    base = root or Path(__file__).resolve().parent
    newest = 0
    for path in base.rglob("*.py"):
        with contextlib.suppress(OSError):
            newest = max(newest, path.stat().st_mtime_ns)
    return newest


# --------------------------------------------------------------------------- #
# Requests and acks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Request:
    """One claimed chord request."""

    verb: str
    nonce: str
    epoch_s: int
    mtime_ns: int  # when the poker wrote it — the origin of every latency metric
    claimed_ns: int

    def age_sec(self, now_ns: int | None = None) -> float:
        """Seconds between the poker's write and *now_ns* (default: now)."""
        return ((now_ns if now_ns is not None else time.time_ns()) - self.mtime_ns) / 1e9


def _nonce_of(path: Path, prefix: str) -> str:
    return path.name[len(prefix) :]


def pending_requests(p: Paths) -> list[Path]:
    """Unclaimed request files, oldest first."""
    found: list[tuple[int, Path]] = []
    for path in p.home.glob("panel_request.*"):
        with contextlib.suppress(OSError):
            found.append((path.stat().st_mtime_ns, path))
    return [path for _mtime, path in sorted(found)]


def claim(p: Paths, request_path: Path) -> Request | None:
    """Claim *request_path* by renaming it; None when the poker abandoned it first.

    A malformed or unknown-verb request is claimed too and answered ``failed`` at once
    (its nonce is the file-name suffix), so the poker falls back cold instead of
    waiting out two windows.
    """
    nonce = _nonce_of(request_path, "panel_request.")
    target = p.claimed(nonce)
    try:
        os.rename(request_path, target)
    except OSError:
        return None  # abandoned (or claimed by a second server instance — impossible by flock)
    claimed_ns = time.time_ns()
    try:
        mtime_ns = target.stat().st_mtime_ns
        parts = target.read_text(encoding="utf-8").split()
        verb, body_nonce, epoch_s = parts[0], parts[1], int(parts[2])
    except (OSError, ValueError, IndexError):
        write_ack(p, nonce, "failed")
        return None
    if verb not in VERBS or body_nonce != nonce:
        write_ack(p, nonce, "failed")
        return None
    return Request(verb, nonce, epoch_s, mtime_ns, claimed_ns)


def write_ack(p: Paths, nonce: str, status: str) -> None:
    """Answer request *nonce* with *status* (atomic: the poker never sees half a line)."""
    if status not in STATUSES:
        raise ValueError(f"unknown ack status {status!r}")
    _atomic_write(p.ack(nonce), f"{status} {int(time.time())}\n")


def read_ack(p: Paths, nonce: str) -> str | None:
    """The status acked for *nonce*, or None (tests and ``--status`` diagnostics)."""
    try:
        return p.ack(nonce).read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return None


def purge(p: Paths, now: float | None = None, max_age: float = PURGE_SEC) -> int:
    """Unlink claimed / abandoned / ack files older than *max_age*; the count removed."""
    now = time.time() if now is None else now
    removed = 0
    for pattern in ("panel_claimed.*", "panel_abandoned.*", "panel_ack.*"):
        for path in p.home.glob(pattern):
            try:
                if now - path.stat().st_mtime > max_age:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


# --------------------------------------------------------------------------- #
# Restart verb (D8)
# --------------------------------------------------------------------------- #
def request_restart(p: Paths, target_pid: int) -> None:
    """Ask the server with pid *target_pid* to re-exec itself (deferred while busy).

    The request names its target so a leftover file can never restart a LATER server.
    """
    _atomic_write(p.restart, f"{target_pid}\n")


def take_restart(p: Paths, own_pid: int) -> bool:
    """True (and the file consumed) when a restart request targets *own_pid*.

    A request for another pid is removed once that pid is dead (a leftover); one for a
    live other pid is left alone (not ours to consume).
    """
    try:
        target = int(p.restart.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    if target == own_pid:
        with contextlib.suppress(OSError):
            p.restart.unlink()
        return True
    if not pid_alive(target):
        with contextlib.suppress(OSError):
            p.restart.unlink()
    return False


# --------------------------------------------------------------------------- #
# Metrics (D11)
# --------------------------------------------------------------------------- #
def append_metric(p: Paths, row: dict[str, Any]) -> None:
    """Append one JSON row to ``panel-metrics.jsonl`` (never raises)."""
    with contextlib.suppress(OSError):
        p.home.mkdir(parents=True, exist_ok=True)
        with p.metrics.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def read_metrics(p: Paths, last: int | None = None) -> list[dict[str, Any]]:
    """The metric rows (the *last* N when given); garbage lines are skipped."""
    try:
        lines = p.metrics.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows[-last:] if last else rows


def nearest_rank(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (``ceil(pct/100 · n)``-th smallest); None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per group (park; peek split by ``cache_state``): n, shown n, p50/p95 of shown_ms."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        verb = str(row.get("verb", "?"))
        key = f"peek/{row.get('cache_state') or '-'}" if verb == "peek" else verb
        groups.setdefault(key, []).append(row)
    out = []
    for key in sorted(groups):
        members = groups[key]
        shown = [
            float(r["shown_ms"])
            for r in members
            if r.get("outcome") == "shown" and r.get("shown_ms") is not None
        ]
        p95 = nearest_rank(shown, 95)
        out.append(
            {
                "group": key,
                "n": len(members),
                "shown": len(shown),
                "p50": nearest_rank(shown, 50),
                "p95": p95,
                "slo_pass": None if p95 is None else p95 + 17 <= 200,
            }
        )
    return out


def format_stats(rows: list[dict[str, Any]]) -> str:
    """The ``--stats`` table: raw rows then the per-group nearest-rank summary."""
    lines = []
    for row in rows:
        verb, outcome = row.get("verb", "?"), row.get("outcome", "?")
        cache = row.get("cache_state") or "-"
        lines.append(
            f"{verb:<5} {outcome:<7} cache={cache:<6} queued={_ms(row.get('queued_ms'))} "
            f"resolve={_ms(row.get('resolve_ms'))} build={_ms(row.get('build_ms'))} "
            f"shown={_ms(row.get('shown_ms'))}"
        )
    lines.append("")
    lines.append("group        n  shown   p50 ms   p95 ms   p95+17 ≤ 200")
    for group in summarize(rows):
        verdict = {True: "PASS", False: "FAIL", None: "-"}[group["slo_pass"]]
        if group["group"] == "peek/miss":
            verdict += " (miss: exempt)"
        lines.append(
            f"{group['group']:<11} {group['n']:>3} {group['shown']:>5} "
            f"{_ms(group['p50']):>8} {_ms(group['p95']):>8}   {verdict}"
        )
    return "\n".join(lines)


def _ms(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}"


# --------------------------------------------------------------------------- #
# Core dispatcher (D5b) — main-thread logic, no AppKit
# --------------------------------------------------------------------------- #
ShowFn = Callable[[Request, Any, Callable[[], None]], None]


@dataclass
class _Flight:  # pylint: disable=too-many-instance-attributes  # timing marks
    """The one request in flight: its generation and timing marks."""

    request: Request
    generation: int
    resolve_ms: float | None = None
    build_start_ns: int = 0
    shown_ns: int = 0
    cache_state: str = ""
    done: bool = False
    bound: threading.Timer | None = None  # the peek resolution bound, cancelled on result


@dataclass
class Core:  # pylint: disable=too-many-instance-attributes
    """The dispatcher: claims, acks, the state machine and generation tokens.

    Every method runs on the host's main thread except the peek worker, whose result
    comes back through *post_main* carrying its generation; a result whose generation is
    no longer current (timeout, restart, shutdown) is discarded. The host supplies the
    three effectful seams: *run_park* (the whole modal park grab, calling ``on_shown``),
    *resolve_peek* (worker-thread resolution → a value for *show_peek*) and *show_peek*
    (the modal peek panel, calling ``on_shown``).
    """

    p: Paths
    pid: int
    stamp: int
    run_park: Callable[[Request, Callable[[], None]], None]
    resolve_peek: Callable[[Request], tuple[Any, str]]
    show_peek: ShowFn
    post_main: Callable[[Callable[[], None]], None]
    link_ready: Callable[[], bool] = lambda: True
    on_idle: Callable[[], None] = lambda: None
    fault: str = ""
    state: str = "starting"
    generation: int = 0
    flight: _Flight | None = None
    restart_pending: bool = False
    restart_fn: Callable[[], None] | None = None
    _last_purge: float = field(default=0.0, repr=False)

    # -- state -------------------------------------------------------------- #
    def set_state(self, state: str) -> None:
        """Transition and republish the pidfile (atomic) when the state changed."""
        if state == self.state:
            return
        self.state = state
        write_pidfile(self.p, self.pid, state, self.stamp)

    @property
    def busy(self) -> bool:
        """A request is resolving or a panel is open."""
        return self.flight is not None

    def settle(self) -> None:
        """Back to ``ready``/``degraded`` after a flight (or a link change)."""
        if self.busy or self.state in ("stopping", "starting"):
            return
        self.set_state("ready" if self.link_ready() else "degraded")

    # -- timers ------------------------------------------------------------- #
    def tick_default(self) -> None:
        """The 50 ms default-mode timer: housekeeping, restart, claim + dispatch."""
        now = time.time()
        if now - self._last_purge > 1.0:
            self._last_purge = now
            purge(self.p, now)
        if take_restart(self.p, self.pid):
            self.restart_pending = True
        if not self.busy:
            if self.restart_pending and self.restart_fn is not None:
                self.restart_fn()
                return
            if self.state in ("ready", "degraded"):
                self.settle()
        if self.fault == "before-claim":
            return
        for path in pending_requests(self.p):
            request = claim(self.p, path)
            if request is not None:
                self.handle(request)

    def tick_modal(self) -> None:
        """The 50 ms modal-mode timer: a panel is open → every request is ``busy``."""
        for path in pending_requests(self.p):
            request = claim(self.p, path)
            if request is not None:
                self._answer(request, "stale" if self._is_stale(request) else "busy")

    # -- dispatch ----------------------------------------------------------- #
    def _is_stale(self, request: Request) -> bool:
        return request.age_sec(request.claimed_ns) > STALE_SEC

    def _answer(self, request: Request, status: str, flight: _Flight | None = None) -> None:
        write_ack(self.p, request.nonce, status)
        queued_ms = (request.claimed_ns - request.mtime_ns) / 1e6
        row: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "verb": request.verb,
            "nonce": request.nonce,
            "outcome": status,
            "queued_ms": round(queued_ms, 1),
        }
        if flight is not None:
            row["resolve_ms"] = None if flight.resolve_ms is None else round(flight.resolve_ms, 1)
            if flight.shown_ns:
                row["build_ms"] = round((flight.shown_ns - flight.build_start_ns) / 1e6, 1)
                row["shown_ms"] = round((flight.shown_ns - request.mtime_ns) / 1e6, 1)
            if request.verb == "peek":
                row["cache_state"] = flight.cache_state
        append_metric(self.p, row)

    def handle(self, request: Request) -> None:
        """Answer or dispatch one freshly claimed request."""
        if self._is_stale(request):
            self._answer(request, "stale")
            return
        if self.state not in POKEABLE_STATES:
            # starting / degraded / stopping: the poker should not have poked (race with a
            # transition) — hand the chord straight back to the cold path.
            self._answer(request, "failed")
            return
        if self.busy:
            self._answer(request, "busy")
            return
        if self.fault == "after-claim-crash":
            os._exit(3)  # simulated crash after the claim: the poker's second window sees it
        self.generation += 1
        flight = _Flight(request, self.generation)
        self.flight = flight
        self.set_state("busy")
        if request.verb == "park":
            self._run_park(flight)
        else:
            self._start_peek(flight)

    def _shown(self, flight: _Flight) -> None:
        if flight.shown_ns:
            return
        flight.shown_ns = time.time_ns()
        self._answer(flight.request, "shown", flight)
        if self.fault == "after-show":
            raise RuntimeError("injected fault: after-show")

    def _fail(self, flight: _Flight) -> None:
        """Answer ``failed`` — AFTER bumping the generation so no late worker can show."""
        self.generation += 1
        if not flight.shown_ns:
            self._answer(flight.request, "failed", flight)
        self._finish(flight)

    def _finish(self, flight: _Flight) -> None:
        flight.done = True
        if flight.bound is not None:
            flight.bound.cancel()
        if self.flight is flight:
            self.flight = None
        self.settle()
        self.on_idle()

    def _run_park(self, flight: _Flight) -> None:
        flight.build_start_ns = time.time_ns()
        try:
            if self.fault == "after-claim-raise":
                raise RuntimeError("injected fault: after-claim-raise")
            self.run_park(flight.request, lambda: self._shown(flight))
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            _log(f"park failed: {error!r}")
            self._fail(flight)
            return
        self._finish(flight)

    def _start_peek(self, flight: _Flight) -> None:
        generation = flight.generation

        def _work() -> None:
            start = time.monotonic()
            try:
                if self.fault == "after-claim-raise":
                    raise RuntimeError("injected fault: after-claim-raise")
                result: tuple[Any, str] | BaseException = self.resolve_peek(flight.request)
            except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                result = error
            elapsed = (time.monotonic() - start) * 1000.0
            self.post_main(lambda: self._peek_resolved(generation, flight, result, elapsed))

        def _expire() -> None:
            self.post_main(lambda: self._peek_expired(generation, flight))

        timer = threading.Timer(PEEK_RESOLVE_BOUND_SEC, _expire)
        timer.daemon = True
        flight.bound = timer
        timer.start()
        threading.Thread(target=_work, name="panel-peek-resolve", daemon=True).start()

    def _peek_expired(self, generation: int, flight: _Flight) -> None:
        if generation != self.generation or flight.done or flight.build_start_ns:
            return
        _log(f"peek resolution exceeded {PEEK_RESOLVE_BOUND_SEC}s — failed (cold fallback)")
        self._fail(flight)

    def _peek_resolved(
        self,
        generation: int,
        flight: _Flight,
        result: tuple[Any, str] | BaseException,
        elapsed_ms: float,
    ) -> None:
        if generation != self.generation or flight.done:
            return  # superseded (timeout / restart / shutdown): discard, never show
        if flight.bound is not None:
            flight.bound.cancel()  # resolved in time: the bound thread ends now
        flight.resolve_ms = elapsed_ms
        if isinstance(result, BaseException):
            _log(f"peek resolution failed: {result!r}")
            self._fail(flight)
            return
        data, flight.cache_state = result
        flight.build_start_ns = time.time_ns()
        try:
            self.show_peek(flight.request, data, lambda: self._shown(flight))
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            _log(f"peek panel failed: {error!r}")
            self._fail(flight)
            return
        self._finish(flight)

    def shutdown(self) -> None:
        """``stopping``: invalidate every in-flight worker and drop the pidfile."""
        self.generation += 1
        self.state = "stopping"
        remove_pidfile(self.p, self.pid)


def _log(message: str) -> None:
    """One timestamped line on stderr (the LaunchAgent's ``panel-server.err``)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} panel-server[{os.getpid()}]: {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Frontmost over the warm iTerm link (D4)
# --------------------------------------------------------------------------- #
class ItermFrontmost:
    """``peek.Frontmost`` answered by the resident iTerm2 API link — one per request.

    The focused uuid is fetched once and memoised, so the resolution sees ONE consistent
    tab even if focus moves mid-request. ``is_ccc_tui`` compares the focused uuid with
    the live TUI's published identity (``jumpstate.get_tui``) instead of a ``ps`` scan.
    """

    _UNSET = object()

    def __init__(self, link_thread: Any) -> None:
        self._link = link_thread
        self._uuid: Any = self._UNSET

    def uuid(self) -> str | None:
        if self._uuid is self._UNSET:
            self._uuid = self._link.call(
                lambda link: link.current_session_uuid(), timeout=LINK_CALL_SEC
            )
        return self._uuid

    def _variable(self, name: str) -> str | None:
        uuid = self.uuid()
        if not uuid:
            return None
        return self._link.call(
            lambda link: link.session_variable(uuid, name), timeout=LINK_CALL_SEC
        )

    def cwd(self) -> str | None:
        return self._variable("path")

    def tty(self) -> str | None:
        return self._variable("tty")

    def is_ccc_tui(self) -> bool:
        from . import jumpstate  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        tui = jumpstate.get_tui()
        uuid = self.uuid()
        if tui is None or not uuid or not pid_alive(tui[0]):
            return False
        return tui[1].split(":")[-1].strip().upper() == uuid.strip().upper()


# --------------------------------------------------------------------------- #
# AppKit host (the resident process)
# --------------------------------------------------------------------------- #
class ModalLoop:  # pylint: disable=no-member  # AppKit attrs resolve via PyObjC
    """``peek.PanelLoop`` for the server: a modal session instead of ``NSApp.run()``."""

    def __init__(self, timeout: float = 0.0) -> None:
        self._timeout = timeout
        self._timer: Any = None

    def run(self, panel: Any) -> None:
        import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        app = AppKit.NSApplication.sharedApplication()
        if self._timeout > 0:  # --smoke: auto-dismiss from INSIDE the modal mode
            self._timer = AppKit.NSTimer.timerWithTimeInterval_repeats_block_(
                self._timeout, False, lambda _t: self.stop()
            )
            AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(
                self._timer, AppKit.NSModalPanelRunLoopMode
            )
        try:
            app.runModalForWindow_(panel.window)
        finally:
            if self._timer is not None:
                self._timer.invalidate()
                self._timer = None

    def stop(self) -> None:
        import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        from . import peek  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        app = AppKit.NSApplication.sharedApplication()
        app.stopModal()
        peek.wake_run_loop(app)


@contextlib.contextmanager
def focus_handback() -> Iterator[None]:  # pylint: disable=no-member
    """Return focus to the app that was frontmost before a panel — guarded (D6).

    Only when the server is STILL the active app after the panel closed (a key
    dismissal): a click-away already activated the clicked app, which must stay active.
    """
    import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    previous = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    try:
        yield
    finally:
        app = AppKit.NSApplication.sharedApplication()
        if app.isActive():
            app.hide_(None)
            if app.isActive() and previous is not None:
                previous.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


def _eager_imports() -> None:
    """Import everything a chord needs NOW (S1: a first chord paid 227–597 ms lazily)."""
    # pylint: disable=import-outside-toplevel,unused-import
    import AppKit  # noqa: F401, PLC0415

    from . import (  # noqa: F401, PLC0415
        accounts,
        colors,
        jumpstate,
        models,
        notify,
        park,
        parkpanel,
        peek,
        sessionmd,
        store,
        usage,
    )
    from .adapters import claude  # noqa: F401, PLC0415


class AppKitHost:  # pylint: disable=too-many-instance-attributes,no-member
    """The resident process: AppKit app, timers, iTerm link, cache, watchdog."""

    def __init__(self, p: Paths, *, smoke_frontmost: Any = None, smoke_timeout: float = 0.0):
        self.p = p
        self.smoke_frontmost = smoke_frontmost
        self.smoke_timeout = smoke_timeout
        self.fault = os.environ.get(FAULT_ENV, "")
        self.link: Any = None
        self.cache: Any = None
        self.activity: Any = None
        self.timers: list[Any] = []
        self.watchdog: Any = None
        self._probe_delay = PROBE_MIN_SEC
        self._next_probe = 0.0
        self._probing = False
        self.core = Core(
            p,
            os.getpid(),
            code_stamp(),
            run_park=self._run_park,
            resolve_peek=self._resolve_peek,
            show_peek=self._show_peek,
            post_main=self._post_main,
            link_ready=self._link_ready,
            fault=self.fault,
            restart_fn=self.reexec,
        )

    # -- seams -------------------------------------------------------------- #
    def _frontmost(self) -> Any:
        if self.smoke_frontmost is not None:
            return self.smoke_frontmost
        return ItermFrontmost(self.link)

    def _link_ready(self) -> bool:
        if self.smoke_frontmost is not None:
            return True
        return bool(self.link is not None and self.link.link.ready)

    def _post_main(self, fn: Callable[[], None]) -> None:
        from PyObjCTools import (
            AppHelper,  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        )

        AppHelper.callAfter(fn)

    def _run_park(self, request: Request, on_shown: Callable[[], None]) -> None:
        del request
        from . import park, parkpanel  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        def _capture(header: str, initial: str = "", poll: Any = None) -> str | None:
            return parkpanel.capture_prompt(header, initial, poll=poll, on_shown=on_shown)

        with focus_handback():
            park.grab(park.GrabOptions(), frontmost=self._frontmost(), capture=_capture)

    def _resolve_peek(self, request: Request) -> tuple[Any, str]:
        del request
        from . import peek  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        data = peek.resolve_peek(frontmost=self._frontmost(), cache=self.cache)
        return data, getattr(data, "cache_state", "")

    def _show_peek(self, request: Request, data: Any, on_shown: Callable[[], None]) -> None:
        del request
        from . import peek  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        inputs = peek.panel_inputs(data)
        loop = ModalLoop(self.smoke_timeout)
        with focus_handback():
            panel = peek.build_panel_from(inputs, loop=loop)
            try:
                panel.window.displayIfNeeded()
                on_shown()
                loop.run(panel)
            finally:
                panel.close()

    # -- link probe (degraded → ready) -------------------------------------- #
    def _maybe_probe(self) -> None:
        if self.smoke_frontmost is not None or self._probing or self.core.busy:
            return
        if self.core.state == "ready" and self._link_ready():
            return
        if time.monotonic() < self._next_probe:
            return
        self._probing = True

        def _probe() -> None:
            ok = bool(self.link.call(lambda link: link.reconnect(), timeout=PROBE_TIMEOUT_SEC))
            self._post_main(lambda: self._probed(ok))

        threading.Thread(target=_probe, name="panel-link-probe", daemon=True).start()

    def _probed(self, ok: bool) -> None:
        self._probing = False
        if ok:
            self._probe_delay = PROBE_MIN_SEC
            _log("iTerm link ready")
        else:
            self._next_probe = time.monotonic() + self._probe_delay
            _log(f"iTerm link unavailable — degraded, re-probe in {self._probe_delay:.0f}s")
            self._probe_delay = min(self._probe_delay * 2, PROBE_MAX_SEC)
        if self.core.state == "starting":
            self.core.set_state("ready" if ok else "degraded")
        else:
            self.core.settle()

    # -- lifecycle ---------------------------------------------------------- #
    def _schedule(self, interval: float, fn: Callable[[], None], modes: list[Any]) -> None:
        import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        def _block(_timer: Any) -> None:
            try:
                fn()
            except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                _log(f"timer callback raised: {error!r}")

        timer = AppKit.NSTimer.timerWithTimeInterval_repeats_block_(interval, True, _block)
        loop = AppKit.NSRunLoop.currentRunLoop()
        for mode in modes:
            loop.addTimer_forMode_(timer, mode)
        self.timers.append(timer)

    def _tick_default(self) -> None:
        self._maybe_probe()
        self.core.tick_default()

    def start(self) -> None:
        """Everything but the run loop: app, activity token, link, cache, timers."""
        import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            iterm_api,
            parkpanel,
            watchdog,
        )

        _eager_imports()
        parkpanel.warm_appkit()
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        info = AppKit.NSProcessInfo.processInfo()
        self.activity = info.beginActivityWithOptions_reason_(
            AppKit.NSActivityUserInitiatedAllowingIdleSystemSleep
            | AppKit.NSActivityLatencyCritical,
            "ccc panel server: chord panels must open without App Nap latency",
        )
        if self.smoke_frontmost is None:
            cookie: Callable[[], tuple[str, str] | None] = iterm_api.fetch_cookie
            if self.fault == "cookie-hang":

                def cookie() -> tuple[str, str] | None:
                    time.sleep(3600)  # injected fault: a cookie request that never answers
                    return iterm_api.fetch_cookie()

            self.link = iterm_api.LinkThread(iterm_api.CookieItermLink(cookie=cookie))
        self.cache = _new_cache()
        write_pidfile(self.p, self.core.pid, "starting", self.core.stamp)
        if self.smoke_frontmost is not None:
            self.core.set_state("ready")
        else:
            self._maybe_probe()
            threading.Thread(target=self._prewarm, name="panel-prewarm", daemon=True).start()
        self._schedule(POLL_SEC, self._tick_default, [AppKit.NSDefaultRunLoopMode])
        self._schedule(POLL_SEC, self.core.tick_modal, [AppKit.NSModalPanelRunLoopMode])
        self.watchdog = watchdog.Watchdog(
            is_exiting=lambda: self.core.state == "stopping",
            restart_wanted=lambda: True,
            state=lambda: {"state": self.core.state, "busy": self.core.busy},
            reexec=self.reexec,
            heal_fn=self._heal,
            log=self.p.home / "panel-server-watchdog.log",
        )
        self._schedule(
            1.0,
            self.watchdog.beat,
            [AppKit.NSRunLoopCommonModes, AppKit.NSModalPanelRunLoopMode],
        )
        self.watchdog.start()
        signal.signal(signal.SIGTERM, lambda *_a: self.stop(0))

    def _prewarm(self) -> None:
        """Fill the transcript cache for recently active sessions (``ready`` never waits)."""
        if self.cache is None or not hasattr(self.cache, "prewarm"):
            return
        try:
            from .adapters.claude import (
                ClaudeAdapter,  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            )
            from .store import Store  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

            adapter = ClaudeAdapter()
            with Store() as store:
                sessions = [s for s in store.list_sessions() if not s.done]
            sessions.sort(key=lambda s: s.last_response_at, reverse=True)
            found = []
            for session in sessions[:PREWARM_SESSIONS]:
                path = adapter.transcript_path(session.cwd, session.session_id)
                if path is not None:
                    found.append(path)
            self.cache.prewarm(found)
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            _log(f"prewarm failed: {error!r}")

    def _heal(self, reason: str, *, restart: bool, reexec: Callable[[], None], **_kw: Any) -> None:
        """Watchdog heal: give a busy server a grace, then re-exec (capped) or exit 1."""
        del restart
        deadline = time.monotonic() + HEAL_BUSY_GRACE_SEC
        while self.core.busy and time.monotonic() < deadline:
            time.sleep(1.0)
        from . import watchdog  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        _log(f"wedged ({reason}) — re-exec")
        if watchdog.may_restart():
            reexec()
        os._exit(1)  # KeepAlive(SuccessfulExit=false) brings a fresh process

    def reexec(self) -> None:
        """Replace this process with a fresh ``ccc panel-server`` (restart verb / heal)."""
        self._end_activity()
        remove_pidfile(self.p, self.core.pid)
        argv = list(sys.argv)
        _log(f"re-exec {argv}")
        if os.path.isabs(argv[0]):
            os.execv(argv[0], argv)
        os.execvp(argv[0], argv)

    def _end_activity(self) -> None:
        if self.activity is not None:
            import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

            AppKit.NSProcessInfo.processInfo().endActivity_(self.activity)
            self.activity = None

    def stop(self, code: int = 0) -> None:
        """Orderly shutdown (SIGTERM / end of smoke): pidfile gone, activity ended, exit."""
        self.core.shutdown()
        if self.watchdog is not None:
            self.watchdog.stop()
        for timer in self.timers:
            timer.invalidate()
        self.timers = []
        self._end_activity()
        if self.link is not None:
            self.link.stop()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    def run(self) -> None:
        """Enter the AppKit run loop (never returns; exit via :meth:`stop`)."""
        import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        AppKit.NSApplication.sharedApplication().run()


PROBE_TIMEOUT_SEC = 5.5  # cookie fetch (5 s bound) + connect


def _new_cache() -> Any:
    """The resident transcript cache (D5a), or None when the module is unavailable."""
    try:
        from . import transcript_cache  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    return transcript_cache.TranscriptCache()


# --------------------------------------------------------------------------- #
# Process entry points
# --------------------------------------------------------------------------- #
def scrub_env() -> None:
    """Drop focus hints and iTerm credentials a resident process must never inherit."""
    for name in SCRUBBED_ENV:
        os.environ.pop(name, None)


@contextlib.contextmanager
def singleton(p: Paths) -> Iterator[IO[str] | None]:
    """Hold ``panel_server.lock`` (flock, non-blocking); yields None when already held."""
    p.home.mkdir(parents=True, exist_ok=True)
    fh = p.lock.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        yield fh
    finally:
        fh.close()


def serve(p: Paths | None = None) -> int:
    """Run the resident server in this process (the LaunchAgent's program)."""
    if sys.platform != "darwin":
        print("ccc panel-server: macOS only (AppKit)", file=sys.stderr)
        return 2
    p = p or paths()
    scrub_env()
    with singleton(p) as held:
        if held is None:
            print("ccc panel-server: already running", file=sys.stderr)
            return 0
        host = AppKitHost(p)
        host.start()
        _log(f"started (stamp {host.core.stamp}, fault={host.fault or '-'})")
        host.run()
    return 0


class _SmokeFrontmost:
    """No iTerm: an untracked tab in the current directory (read-only resolution)."""

    def uuid(self) -> str | None:
        return None

    def cwd(self) -> str | None:
        return os.getcwd()

    def tty(self) -> str | None:
        return None

    def is_ccc_tui(self) -> bool:
        return False


def _fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return -1


def smoke(cycles: int = 50, p: Paths | None = None) -> int:  # pylint: disable=too-many-locals
    """Run *cycles* park + peek requests through the REAL host, invisibly; check leaks.

    Panels are transparent and never activated (``CCC_PANEL_SMOKE``), park auto-cancels
    (``CCC_PARK_PANEL_TIMEOUT``) and peek auto-dismisses from inside its modal session.
    Uses a private temp protocol directory, so a live server is never disturbed. PASS
    when every request was answered ``shown``, no window is visible, and windows,
    threads and fds are back at the baseline sampled after a 5-cycle warm-up (monitor /
    observer / timer release is pinned by ``tests/test_panel_lifecycle.py``).
    """
    if sys.platform != "darwin":
        print("smoke: macOS only", file=sys.stderr)
        return 2
    import tempfile  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    import AppKit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    from . import parkpanel  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    os.environ[parkpanel.SMOKE_ENV] = "1"
    os.environ["CCC_PARK_PANEL_TIMEOUT"] = "0.05"
    tmp = Paths(Path(tempfile.mkdtemp(prefix="ccc-panel-smoke-")))
    host = AppKitHost(
        tmp if p is None else p, smoke_frontmost=_SmokeFrontmost(), smoke_timeout=0.05
    )
    host.start()
    app = AppKit.NSApplication.sharedApplication()
    samples: list[dict[str, int]] = []
    sent: list[str] = []
    acks: list[str | None] = []  # recorded as they land (the 10 s purge deletes the files)
    total = cycles * 2
    warmup = min(5, cycles) * 2  # one-time lazy opens (fds, windows) happen in here

    def _sample() -> dict[str, int]:
        cache = getattr(host.cache, "bytes_used", 0) if host.cache is not None else 0
        return {
            "windows": len(app.windows()),
            "visible": sum(1 for w in app.windows() if w.isVisible()),
            "threads": threading.active_count(),
            "fds": _fd_count(),
            "cache_bytes": int(cache),
        }

    def _drive() -> None:
        if host.core.busy:
            return
        if sent:
            status = read_ack(host.p, sent[-1])
            if status is None:
                return
            acks.append(status)
        samples.append(_sample())
        if len(sent) >= total:
            _finish()
            return
        verb = VERBS[len(sent) % 2]
        nonce = f"smoke-{len(sent)}"
        _atomic_write(host.p.request(nonce), f"{verb} {nonce} {int(time.time())}\n")
        sent.append(nonce)

    def _finish() -> None:
        rows = read_metrics(host.p)
        base, last = samples[min(warmup, len(samples) - 1)], samples[-1]
        leaks = {
            k: (base[k], last[k])
            for k in ("windows", "visible", "threads", "fds")
            if last[k] > base[k]
        }
        ok = all(a == "shown" for a in acks) and not leaks and last["visible"] == 0
        print(f"smoke: {len(sent)} requests, acks={sorted(set(map(str, acks)))}")
        print(f"smoke: baseline {base}")
        print(f"smoke: final    {last}")
        if leaks:
            print(f"smoke: growth {leaks}")
        print(format_stats(rows))
        print(f"smoke: {'PASS' if ok else 'FAIL'}")
        host.stop(0 if ok else 1)

    host._schedule(0.1, _drive, [AppKit.NSDefaultRunLoopMode])  # pylint: disable=protected-access
    host.run()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def status_line(p: Paths | None = None) -> tuple[str, str]:
    """``(verdict, detail)`` for ``--status`` / doctor: ``ok|stale|degraded|dead|absent``."""
    p = p or paths()
    info = read_pidfile(p)
    if info is None:
        return "absent", "no pidfile (server not running)"
    if not pid_alive(info.pid):
        return "dead", f"pid {info.pid} not alive (pidfile left behind)"
    if info.state == "degraded":
        return "degraded", f"pid {info.pid} degraded (iTerm link down → chords run cold)"
    if info.stamp < code_stamp():
        return "stale", f"pid {info.pid} {info.state}, code changed — ccc panel-server --restart"
    return "ok", f"pid {info.pid} {info.state}"


def cmd(args: argparse.Namespace) -> int:  # pylint: disable=too-many-return-statements
    """``ccc panel-server`` dispatcher."""
    from . import launchd  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    p = paths()
    if args.install:
        return launchd.panel_server_install()
    if args.uninstall:
        return launchd.panel_server_uninstall(purge=args.purge)
    if args.start:
        return launchd.panel_server_start()
    if args.stop:
        return launchd.panel_server_stop()
    if args.status:
        installed = launchd.panel_server_plist_path().exists()
        loaded = launchd.panel_server_loaded()
        verdict, detail = status_line(p)
        agent = "installed" if installed else "not installed"
        print(f"agent:  {agent}, {launchd.state_badge(loaded)}")
        print(f"server: {verdict} — {detail}")
        return 0 if not installed or verdict == "ok" else 1
    if args.restart:
        info = read_pidfile(p)
        if info is None or not pid_alive(info.pid):
            print("panel server is not running")
            return 1
        request_restart(p, info.pid)
        print(f"restart requested for pid {info.pid} (deferred while a panel is open)")
        return 0
    if args.stats:
        rows = read_metrics(p, args.last)
        if not rows:
            print(f"no metrics yet ({p.metrics})")
            return 0
        print(format_stats(rows))
        return 0
    if args.smoke:
        return smoke(args.cycles)
    return serve(p)
