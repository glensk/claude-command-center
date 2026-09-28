#!/usr/bin/env python3
"""One ``ccc await`` evaluation pass — the single function both callers run.

The dedicated poller (``ccc await -r`` every 60 s) and the daemon's 300 s backstop both
call :func:`run_pass`, so a missing poller degrades the reaction time, never the
behaviour. A pass:

1. exits at once when no group is active (the poller's < 50 ms idle tick);
2. disarms groups whose session is done or archived;
3. moves ``armed → grace`` at ``until`` and expires groups at ``grace_until``;
4. LEASES up to :data:`MAX_SOURCES_PER_PASS` due sources, then probes them in a bounded
   pool (:data:`POOL_SIZE`) under a wall-clock :data:`PASS_DEADLINE_SEC` — leases not
   started by then are released, a running probe dies at its own 30 s timeout;
5. records each outcome in the main thread (the store connection never crosses a
   thread): a fired source wins the group in ONE transaction (``Store.fire_group``),
   a transient failure backs the source off, a permanent one blocks the source, and the
   group blocks only when no viable source is left;
6. runs the deliveries (:mod:`command_center.await_delivery`).

A dry run (``daemon --dry-run``, ``await -r -n``) runs no probe and no delivery; it
reports what WOULD be probed.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import dataclasses
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from . import await_probes
from .await_prompt import build_payload
from .await_store import POLLING_GROUP_STATES, AwaitGroup, AwaitSource
from .checks import run_structured, sanitize_error

if TYPE_CHECKING:
    from .store import Store

MAX_SOURCES_PER_PASS = 10
POOL_SIZE = 4
PASS_DEADLINE_SEC = 50.0

Notifier = Callable[[str, str], None]


@dataclasses.dataclass
class PassReport:  # pylint: disable=too-many-instance-attributes  # a flat tally
    """What one pass did (group / source ids)."""

    idle: bool = False
    probed: list[int] = dataclasses.field(default_factory=list)
    fired: list[int] = dataclasses.field(default_factory=list)
    not_fired: list[int] = dataclasses.field(default_factory=list)
    transient: list[int] = dataclasses.field(default_factory=list)
    blocked_sources: list[int] = dataclasses.field(default_factory=list)
    blocked_groups: list[int] = dataclasses.field(default_factory=list)
    expired: list[int] = dataclasses.field(default_factory=list)
    disarmed: list[int] = dataclasses.field(default_factory=list)
    released: list[int] = dataclasses.field(default_factory=list)
    late: list[int] = dataclasses.field(default_factory=list)  # fired after `until`, ignored
    would_probe: list[int] = dataclasses.field(default_factory=list)  # dry run
    delivered: list[int] = dataclasses.field(default_factory=list)
    launched: list[int] = dataclasses.field(default_factory=list)
    waiting: list[int] = dataclasses.field(default_factory=list)

    def is_empty(self) -> bool:
        """True when the pass changed nothing worth reporting."""
        return not any(getattr(self, f.name) for f in dataclasses.fields(self) if f.name != "idle")


def _null_notifier(_title: str, _message: str) -> None:
    return None


def config_notifier() -> Notifier:
    """The user's configured channels (``cfg.notify``) as a two-argument callable."""
    from . import config  # pylint: disable=import-outside-toplevel
    from .notify import notify  # pylint: disable=import-outside-toplevel

    channels = list(config.load_config().notify)
    return lambda title, message: notify(title, message, channels)


def notify_group(store: Store, group_id: int, message: str, now: int, notifier: Notifier) -> None:
    """One content-free notification per group state (``notified_at`` dedup)."""
    if store.claim_group_notice(group_id, now):
        notifier("ccc await", message)


def _lifecycle(store: Store, now: int, report: PassReport, notifier: Notifier) -> None:
    for group, _sources in store.list_awaits():
        session = store.get(group.session_id)
        if session is None:
            continue  # the FK cascade already removed (or is removing) it
        if session.done or session.archived:
            if store.disarm_group(group.id, now, reason="session done"):
                report.disarmed.append(group.id)
                notify_group(
                    store,
                    group.id,
                    f"await group {group.id} disarmed: its session is done",
                    now,
                    notifier,
                )


def _handle(  # pylint: disable=too-many-arguments
    store: Store,
    src: AwaitSource,
    result: await_probes.ProbeResult,
    *,
    now: int,
    report: PassReport,
    notifier: Notifier,
) -> None:
    """Record one probe outcome (main thread)."""
    report.probed.append(src.id)
    group = store.get_await_group(src.group_id)
    if group is None or group.state not in POLLING_GROUP_STATES:
        store.release_lease(src.id, src.lease_token)
        return
    if result.outcome == "fired" and result.event is not None:
        if group.state == "grace" and result.event.remote_epoch > group.until_epoch:
            # Too late: the deadline passed before this event happened. Consume it.
            report.late.append(src.id)
            store.finish_probe(
                src.id,
                src.lease_token,
                next_check_at=await_probes.next_check_after(now, src.interval_sec, 0),
                fail_count=0,
                watermark=result.watermark,
            )
            return
        token = store.fire_group(
            group.id,
            src.id,
            src.lease_token,
            event_id=result.event.event_id,
            payload=build_payload(result.event),
            remote_epoch=result.event.remote_epoch,
            watermark=result.watermark,
            now=now,
        )
        if token:
            report.fired.append(group.id)
        else:
            store.release_lease(src.id, src.lease_token)
        return
    if result.outcome in ("fired", "not_fired"):  # "fired" without an event: not usable
        report.not_fired.append(src.id)
        store.finish_probe(
            src.id,
            src.lease_token,
            next_check_at=await_probes.next_check_after(now, src.interval_sec, 0),
            fail_count=0,
            watermark=result.watermark,
        )
        return
    if result.outcome == "transient":
        report.transient.append(src.id)
        fails = src.fail_count + 1
        store.finish_probe(
            src.id,
            src.lease_token,
            next_check_at=await_probes.next_check_after(now, src.interval_sec, fails),
            fail_count=fails,
            fail_class="transient",
            last_error=sanitize_error(result.error),
        )
        return
    # permanent: block this SOURCE; the group only when nothing viable is left.
    if store.finish_probe(
        src.id,
        src.lease_token,
        next_check_at=now,
        fail_count=src.fail_count + 1,
        fail_class="permanent",
        last_error=sanitize_error(result.error),
        blocked=True,
    ):
        report.blocked_sources.append(src.id)
        if store.claim_source_notice(src.id, now):
            notifier(
                "ccc await",
                f"await group {group.id}: source {src.id} ({src.kind}) blocked — "
                f"`ccc await -l` shows why, `ccc await -R {group.id}` retries",
            )
    if store.viable_source_count(group.id) == 0 and store.block_group(
        group.id, "no viable source left", now, from_states=POLLING_GROUP_STATES
    ):
        report.blocked_groups.append(group.id)
        notify_group(
            store,
            group.id,
            f"await group {group.id} blocked: no source can be probed "
            f"(`ccc await -R {group.id}` retries)",
            now,
            notifier,
        )


def _probe_one(src: AwaitSource, now: int, runner: await_probes.Runner) -> await_probes.ProbeResult:
    try:
        return await_probes.probe(src.kind, src.spec_dict(), src.watermark, now=now, runner=runner)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return await_probes.ProbeResult(
            "transient", watermark=src.watermark, error=f"probe crashed: {type(exc).__name__}"
        )


def _run_probes(  # pylint: disable=too-many-arguments
    store: Store,
    leased: list[tuple[AwaitSource, AwaitGroup]],
    now: int,
    report: PassReport,
    notifier: Notifier,
    *,
    runner: await_probes.Runner,
    deadline_sec: float,
    clock: Callable[[], float],
) -> None:
    """Probe the leased sources in a bounded pool; release what the deadline cut off."""
    started = clock()
    queue = [src for src, _group in leased]
    running: dict[Future[await_probes.ProbeResult], AwaitSource] = {}
    with ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="await-probe") as pool:
        while queue or running:
            while queue and len(running) < POOL_SIZE and clock() - started < deadline_sec:
                src = queue.pop(0)
                running[pool.submit(_probe_one, src, now, runner)] = src
            if not running:
                break  # deadline hit with nothing in flight
            done, _pending = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done:
                src = running.pop(future)
                _handle(store, src, future.result(), now=now, report=report, notifier=notifier)
            if clock() - started >= deadline_sec:
                # Leases not started give their slot back; running ones finish (bounded
                # by the probe's own timeout) and are recorded above next iteration.
                for src in queue:
                    if store.release_lease(src.id, src.lease_token):
                        report.released.append(src.id)
                queue = []


def _dry_run(store: Store, now: int, report: PassReport) -> PassReport:
    for group, sources in store.list_awaits():
        if group.state not in POLLING_GROUP_STATES:
            continue
        for src in sources:
            if src.state == "armed" and src.next_check_at <= now and src.lease_until <= now:
                report.would_probe.append(src.id)
    report.would_probe = report.would_probe[:MAX_SOURCES_PER_PASS]
    return report


def run_pass(  # pylint: disable=too-many-arguments
    store: Store,
    *,
    now: int | None = None,
    dry_run: bool = False,
    deliver: bool = True,
    runner: await_probes.Runner = run_structured,
    notifier: Notifier | None = None,
    deadline_sec: float = PASS_DEADLINE_SEC,
    clock: Callable[[], float] = time.monotonic,
    delivery: Callable[..., None] | None = None,
) -> PassReport:
    """One evaluation pass (see the module docstring). Never raises for a probe failure."""
    report = PassReport()
    if not store.has_active_awaits():
        report.idle = True
        return report
    now = int(time.time()) if now is None else now
    if dry_run:
        return _dry_run(store, now, report)
    notifier = notifier or _null_notifier
    _lifecycle(store, now, report, notifier)
    for group in store.advance_deadlines(now):
        report.expired.append(group.id)
        notify_group(
            store,
            group.id,
            f"await group {group.id} expired: nothing arrived before its deadline; "
            "the session was not resumed",
            now,
            notifier,
        )
    leased = store.lease_due_sources(now, limit=MAX_SOURCES_PER_PASS)
    if leased:
        _run_probes(
            store,
            leased,
            now,
            report,
            notifier,
            runner=runner,
            deadline_sec=deadline_sec,
            clock=clock,
        )
    if deliver:
        if delivery is None:
            from . import await_delivery  # pylint: disable=import-outside-toplevel

            delivery = await_delivery.deliver_pending
        delivery(store, now=now, report=report, notifier=notifier)
    return report
