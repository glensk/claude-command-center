"""``await_eval.run_pass``: leases, the pool + deadline, the winner, grace/expiry,
lifecycle disarm, source vs group blocking — and the crash seams between them.

Probes are replayed by a fake runner keyed on the argv's first element; deliveries
are stubbed out (``deliver=False`` or a recording ``delivery``), so nothing here opens
a tab, types into one or calls a real CLI.
"""

# pylint: disable=unbalanced-tuple-unpacking  # `[(src, _)] = …` asserts exactly one lease
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from awaitstub import seeded_store as _store
from awaitstub import zoho_result

from command_center import await_eval
from command_center.await_store import GRACE_SEC, SourceSpec
from command_center.checks import StructuredResult
from command_center.store import Store

NOW = 1_800_000_000
DUE = NOW + 120
ZOHO = "/bin/zoho-api.py"
SLACK = "/bin/slack_api.py"


class Runner:
    """Answers by executable (or ``cmd``); records every call; thread-safe."""

    def __init__(self, answers: dict[str, StructuredResult]) -> None:
        self.answers = answers
        self.calls: list[Any] = []
        self.lock = threading.Lock()

    def __call__(self, argv: Any, **_kw: Any) -> StructuredResult:
        with self.lock:
            self.calls.append(argv)
        key = "cmd" if isinstance(argv, str) else argv[0]
        return self.answers[key]


class Notes:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, _title: str, message: str) -> None:
        self.messages.append(message)


def _arm(store: Store, *specs: SourceSpec, session_id: str = "s1", until: int = NOW + 3600) -> int:
    return store.arm_await(
        session_id,
        config_dir="",
        cwd="/repo",
        no_codex=False,
        prompt_template="{event}",
        until_epoch=until,
        sources=list(specs),
        now=NOW,
    )


ZSRC = SourceSpec(kind="zoho-reply", spec={"exe": ZOHO, "ticket": "209"}, watermark="1:a")
SSRC = SourceSpec(
    kind="slack-dm", spec={"exe": SLACK, "user_id": "U1AB", "channel": "D1"}, watermark="5.0"
)


def _pass(store: Store, runner: Runner, now: int = DUE, **kw: Any) -> await_eval.PassReport:
    kw.setdefault("deliver", False)
    return await_eval.run_pass(store, now=now, runner=runner, **kw)


def test_idle_pass_touches_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runner = Runner({})
    assert _pass(store, runner).idle
    assert not runner.calls


def test_not_due_is_not_probed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _arm(store, ZSRC)
    runner = Runner({ZOHO: zoho_result(fired=False)})
    report = _pass(store, runner, now=NOW + 10)
    assert not runner.calls and not report.probed


def test_not_fired_advances_watermark_and_reschedules(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    report = _pass(store, Runner({ZOHO: zoho_result(fired=False)}))
    [src] = store.await_sources_of(gid)
    assert report.not_fired == [src.id]
    assert (src.watermark, src.next_check_at, src.lease_token) == ("2:9", DUE + 120, "")


def test_first_fired_source_wins_and_disarms_the_sibling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC, SSRC)
    slack_quiet = StructuredResult(exit=0, stdout='{"channel":"D1","messages":[]}')
    report = _pass(store, Runner({ZOHO: zoho_result(fired=True), SLACK: slack_quiet}))
    group = store.get_await_group(gid)
    assert group is not None
    assert report.fired == [gid]
    assert group.state == "fired" and group.event_id == "zoho:209:9"
    payload = json.loads(group.event_payload)
    assert payload["source"] == "zoho-reply" and payload["snippet"] == "yes"
    states = sorted(s.state for s in store.await_sources_of(gid))
    assert states == ["disarmed", "done"]


def test_both_fire_in_one_pass_only_one_wins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC, SSRC)
    slack_hit = StructuredResult(
        exit=0,
        stdout=json.dumps(
            {"channel": "D1", "messages": [{"ts": "9.0", "user": "U1AB", "text": "hi"}]}
        ),
    )
    report = _pass(store, Runner({ZOHO: zoho_result(fired=True), SLACK: slack_hit}))
    assert report.fired == [gid]
    assert sorted(s.state for s in store.await_sources_of(gid)) == ["disarmed", "done"]


def test_transient_backs_off_and_keeps_watermark(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    _pass(store, Runner({ZOHO: StructuredResult(exit=5, stderr="HTTP 503")}))
    [src] = store.await_sources_of(gid)
    assert (src.fail_count, src.fail_class, src.watermark) == (1, "transient", "1:a")
    assert src.next_check_at == DUE + 240
    _pass(store, Runner({ZOHO: StructuredResult(exit=5)}), now=DUE + 240)
    [src] = store.await_sources_of(gid)
    assert (src.fail_count, src.next_check_at) == (2, DUE + 240 + 480)
    # A clean probe resets the count.
    _pass(store, Runner({ZOHO: zoho_result(fired=False)}), now=DUE + 720)
    [src] = store.await_sources_of(gid)
    assert src.fail_count == 0


def test_permanent_blocks_the_source_not_the_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC, SSRC)
    notes = Notes()
    slack_quiet = StructuredResult(exit=0, stdout='{"channel":"D1","messages":[]}')
    report = _pass(
        store,
        Runner({ZOHO: StructuredResult(exit=4, stderr="auth"), SLACK: slack_quiet}),
        notifier=notes,
    )
    assert len(report.blocked_sources) == 1 and not report.blocked_groups
    assert store.get_await_group(gid).state == "armed"  # type: ignore[union-attr]
    assert len(notes.messages) == 1 and "blocked" in notes.messages[0]
    assert "r@x.org" not in notes.messages[0]  # content-free


def test_last_viable_source_blocking_blocks_the_group_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    notes = Notes()
    report = _pass(store, Runner({ZOHO: StructuredResult(exit=2)}), notifier=notes)
    assert report.blocked_groups == [gid]
    group = store.get_await_group(gid)
    assert group is not None and group.state == "blocked"
    assert group.blocked_reason == "no viable source left"
    assert len(notes.messages) == 2  # one for the source, one for the group
    assert not _pass(store, Runner({}), notifier=notes).probed  # blocked: nothing polled
    assert len(notes.messages) == 2


def test_retry_after_block_catches_what_arrived_during_the_outage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    _pass(store, Runner({ZOHO: StructuredResult(exit=4)}))
    assert store.retry_group(gid, DUE + 10) == "armed"
    runner = Runner({ZOHO: zoho_result(fired=True)})
    _pass(store, runner, now=DUE + 10)
    assert runner.calls[0] == [ZOHO, "-i", "209", "1:a"]  # the ORIGINAL watermark
    assert store.get_await_group(gid).state == "fired"  # type: ignore[union-attr]


def test_grace_accepts_an_event_from_before_the_deadline_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    until = 1_800_003_600
    gid = _arm(store, ZSRC, until=until)
    late = zoho_result(fired=True, when="2027-01-15T12:00:00.000Z")  # epoch > until
    report = _pass(store, Runner({ZOHO: late}), now=until + 10)
    assert store.get_await_group(gid).state == "grace"  # type: ignore[union-attr]
    [src] = store.await_sources_of(gid)
    assert report.late == [src.id]
    assert src.watermark == "2:9"  # consumed, never re-fired


def test_grace_fire_with_a_timestamp_before_until(tmp_path: Path) -> None:
    store = _store(tmp_path)
    until = 1_800_003_600  # 2027-01-15T09:00:00Z
    gid = _arm(store, ZSRC, until=until)
    before = zoho_result(fired=True, when="2027-01-15T08:59:00.000Z")
    report = _pass(store, Runner({ZOHO: before}), now=until + 500)
    assert report.fired == [gid]


def test_expiry_notifies_once_and_never_resumes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC, until=NOW + 60)
    notes = Notes()
    calls: list[Any] = []
    runner = Runner({ZOHO: zoho_result(fired=False)})
    report = await_eval.run_pass(
        store,
        now=NOW + 60 + GRACE_SEC,
        runner=runner,
        notifier=notes,
        delivery=lambda *a, **k: calls.append(a),
    )
    assert report.expired == [gid]
    assert store.get_await_group(gid).state == "expired"  # type: ignore[union-attr]
    assert not runner.calls
    assert len(notes.messages) == 1 and "expired" in notes.messages[0]
    await_eval.run_pass(store, now=NOW + 2 * GRACE_SEC, runner=runner, notifier=notes)
    assert len(notes.messages) == 1


def test_done_session_disarms_its_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    store.update_fields("s1", done=True)
    notes = Notes()
    runner = Runner({ZOHO: zoho_result(fired=True)})
    report = _pass(store, runner, notifier=notes)
    assert report.disarmed == [gid]
    assert not runner.calls
    assert notes.messages and "disarmed" in notes.messages[0]


def test_cap_of_ten_sources_per_pass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(13):
        store.ensure(f"x{i}")
        _arm(store, ZSRC, session_id=f"x{i}")
    runner = Runner({ZOHO: zoho_result(fired=False)})
    assert len(_pass(store, runner).probed) == 10
    assert len(_pass(store, runner).probed) == 3


def test_deadline_releases_unstarted_leases(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(8):
        store.ensure(f"x{i}")
        _arm(store, ZSRC, session_id=f"x{i}")
    ticks = iter([0.0] + [0.0] * 4 + [100.0] * 100)  # the deadline passes after 4 starts
    report = _pass(store, Runner({ZOHO: zoho_result(fired=False)}), clock=lambda: next(ticks))
    assert len(report.probed) == 4
    assert len(report.released) == 4
    # Released leases are due again immediately.
    assert len(_pass(store, Runner({ZOHO: zoho_result(fired=False)})).probed) == 4


def test_probe_crash_is_transient(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)

    def boom(*_a: Any, **_k: Any) -> StructuredResult:
        raise RuntimeError("bug")

    report = await_eval.run_pass(store, now=DUE, runner=boom, deliver=False)
    [src] = store.await_sources_of(gid)
    assert report.transient == [src.id] and src.fail_class == "transient"


def test_dry_run_probes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    runner = Runner({ZOHO: zoho_result(fired=True)})
    report = await_eval.run_pass(store, now=DUE, runner=runner, dry_run=True)
    assert report.would_probe == [store.await_sources_of(gid)[0].id]
    assert not runner.calls
    assert store.await_sources_of(gid)[0].lease_token == ""


def test_delivery_runs_after_probes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _arm(store, ZSRC)
    seen: list[str] = []

    def delivery(st: Store, **_k: Any) -> None:
        seen.append(st.await_groups_in("fired")[0].state)

    await_eval.run_pass(
        store, now=DUE, runner=Runner({ZOHO: zoho_result(fired=True)}), delivery=delivery
    )
    assert seen == ["fired"]


# --------------------------------------------------------------------------- crash seams
def test_crash_between_lease_and_publish_reprobes_after_the_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    leased = store.lease_due_sources(DUE)  # a pass leased … and died before recording
    assert leased
    runner = Runner({ZOHO: zoho_result(fired=True)})
    assert not _pass(store, runner, now=DUE + 10).probed  # still leased
    report = _pass(store, runner, now=DUE + 46)  # lease expired
    assert report.fired == [gid]


def test_crash_between_publish_and_delivery_delivers_next_pass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    _pass(
        store, Runner({ZOHO: zoho_result(fired=True)})
    )  # fired; deliver=False = "crashed" before it
    seen: list[int] = []
    await_eval.run_pass(
        store,
        now=DUE + 60,
        runner=Runner({}),
        delivery=lambda st, **_k: seen.extend(g.id for g in st.await_groups_in("fired")),
    )
    assert seen == [gid]


@pytest.mark.parametrize("stale", [True, False])
def test_crash_after_the_delivery_cas_is_reclaimed_when_stale(tmp_path: Path, stale: bool) -> None:
    from command_center import await_delivery  # pylint: disable=import-outside-toplevel

    store = _store(tmp_path)
    gid = _arm(store, ZSRC)
    _pass(store, Runner({ZOHO: zoho_result(fired=True)}))
    group = store.get_await_group(gid)
    assert group is not None and store.mark_delivering(gid, group.delivery_token, DUE)
    later = DUE + (await_delivery.STALE_DELIVERY_SEC if stale else 60)
    report = await_eval.PassReport()
    await_delivery.deliver_pending(
        store,
        now=later,
        report=report,
        notifier=Notes(),
        discover=lambda: [],
        launcher=lambda _g, _t: False,
    )
    group = store.get_await_group(gid)
    assert group is not None
    if stale:
        # handed back (attempt 1); this fixture then fails preflight (no transcript)
        assert group.state != "delivering" and group.delivery_attempts == 1
    else:
        assert group.state == "delivering" and group.delivery_attempts == 0
