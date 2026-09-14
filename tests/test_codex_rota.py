"""Tests for the weekly rota of a SHARED Codex seat (``codex_seat_rota``).

A seat can belong to two people on alternating weeks. Reserving it with an administrative
hold works exactly once — somebody must re-arm it every Monday, and the week nobody does
is the week ccc bills a colleague's seat — so the block is COMPUTED from config plus the
clock on every resolution. What is guarded here, each because its failure is silent and
costs somebody else their allowance:

* the arithmetic: the anchor is a PHASE, not a start of time (weeks before it resolve
  backwards), and the week boundary is Monday 00:00 in the ENTRY's zone, never the
  process's — these tests run under ``TZ=America/New_York`` on purpose;
* fail CLOSED: an entry ccc cannot read, or one whose names do not contain
  ``codex_seat_rota_me``, BLOCKS that seat. "We could not tell whose week it is" must
  never resolve to "ours";
* the block is ABSOLUTE for automation — the ranking under ``fill`` and ``order``, a pin,
  an explicit registered ``$CODEX_HOME``, a journal resume, ``-Q`` and ``headroom`` — and
  a human ``/switch <seat>!`` is the only override;
* the underlying verdict survives the wrapper: a hold that outlasts our next week is not
  promised away by the rota.

The seats come from the ``three_seats`` fixture and ``codex`` is ``tests/fakes/fake_codex.py``.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import codex_launch, config, quota
from command_center import seat_rota as sr

_ZURICH = "Europe/Zurich"
_ANCHOR = date(2026, 9, 14)  # a Monday


def _at(stamp: str, zone: str = _ZURICH) -> int:
    """Epoch of a wall-clock *stamp* (``2026-09-21T00:00:00``) IN *zone*."""
    return int(datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(zone)).timestamp())


def _spec(names: str = "alice,bob", anchor: date = _ANCHOR, zone: str = _ZURICH) -> sr.RotaSpec:
    specs, errors = sr.parse_codex_seat_rota([f"default={anchor.isoformat()}@{zone}:{names}"])
    assert not errors, errors
    return specs["default"]


# ── the arithmetic: an anchor is a phase, not a start of time ────────────────────
def test_holder_cycles_forwards_and_backwards_from_the_anchor() -> None:
    spec = _spec()
    weeks = {
        "2026-09-14T09:00": "alice",  # the anchor week: the FIRST name
        "2026-09-21T09:00": "bob",
        "2026-09-28T09:00": "alice",
        "2026-09-07T09:00": "bob",  # index −1 → the LAST name, not an exception
        "2026-08-31T09:00": "alice",
    }
    for stamp, holder in weeks.items():
        assert sr.rota_state(spec, "bob", _at(stamp)).holder == holder, stamp


def test_three_names_take_turns_in_order() -> None:
    spec = _spec("alice,bob,carol")
    holders = [
        sr.rota_state(spec, "bob", _at(f"2026-09-{day}T09:00")).holder for day in ("14", "21", "28")
    ]
    assert holders == ["alice", "bob", "carol"]
    # …and the cycle continues backwards through the anchor.
    assert sr.rota_state(spec, "bob", _at("2026-09-07T09:00")).holder == "carol"


def test_label_and_the_next_handovers() -> None:
    state = sr.rota_state(_spec(), "bob", _at("2026-09-14T09:00"))
    assert state.label == "14.9.–20.9. used by alice"  # EN DASH, Swiss D.M., inclusive end
    assert (state.week_start, state.week_end_exclusive) == (date(2026, 9, 14), date(2026, 9, 21))
    assert state.mine is False
    assert (state.next_mine_at, state.next_mine_label) == (_at("2026-09-21T00:00"), "Mon 21.9.")
    assert (state.next_holder, state.next_other_at) == ("alice", _at("2026-09-28T00:00"))
    assert state.next_other_label == "Mon 28.9."
    ours = sr.rota_state(_spec(), "bob", _at("2026-09-21T09:00"))
    assert (ours.mine, ours.label) == (True, "21.9.–27.9. used by bob")
    assert (ours.next_holder, ours.next_other_label) == ("alice", "Mon 28.9.")


def test_week_label_and_monday_label_are_swiss_and_locale_free() -> None:
    assert sr.week_label(date(2026, 9, 14), date(2026, 9, 21)) == "14.9.–20.9."
    assert "–" in sr.week_label(date(2026, 9, 14), date(2026, 9, 21))  # EN DASH U+2013
    assert sr.monday_label(date(2026, 9, 21)) == "Mon 21.9."
    assert sr.day_label(_at("2026-09-23T10:00"), _ZURICH) == "23.9."
    assert sr.day_label(0, _ZURICH) == ""
    assert sr.day_label(_at("2026-09-23T10:00"), "Mars/Base") == ""


# ── the boundary is Monday 00:00 in the ENTRY's zone, never the process's ─────────
@pytest.fixture(name="new_york")
def _new_york(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the process itself in America/New_York, so a local-zone leak fails the test."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()  # restore the developer's own zone for every later test


@pytest.mark.usefixtures("new_york")
@pytest.mark.parametrize(
    ("anchor", "stamp", "holder"),
    [
        # a plain week boundary: one second before and after Zurich midnight
        (_ANCHOR, "2026-09-20T23:59:59", "alice"),
        (_ANCHOR, "2026-09-21T00:00:00", "bob"),
        # DST START (2026-03-29, Zurich jumps 02:00 → 03:00): the flip is still midnight
        (date(2026, 3, 23), "2026-03-29T23:59:59", "alice"),
        (date(2026, 3, 23), "2026-03-30T00:00:00", "bob"),
        # DST END (2026-10-25, Zurich repeats 02:00–03:00): likewise
        (date(2026, 10, 19), "2026-10-25T23:59:59", "alice"),
        (date(2026, 10, 19), "2026-10-26T00:00:00", "bob"),
    ],
)
def test_weeks_flip_at_midnight_in_the_entrys_zone(anchor: date, stamp: str, holder: str) -> None:
    assert sr.rota_state(_spec(anchor=anchor), "bob", _at(stamp)).holder == holder


@pytest.mark.usefixtures("new_york")
def test_the_process_zone_does_not_move_the_boundary() -> None:
    """23:00 Sunday in New York is already Monday in Zurich — the entry's zone decides."""
    spec = _spec()
    assert sr.rota_state(spec, "bob", _at("2026-09-20T23:00", "America/New_York")).holder == "bob"
    assert (
        sr.rota_state(
            _spec(zone="America/New_York"), "bob", _at("2026-09-20T23:00", "America/New_York")
        ).holder
        == "alice"
    )


# ── parsing: every malformed shape is an ERROR, never an exception ────────────────
@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ("junk", "no `=`"),
        ("default=2026-09-14:alice,bob", "no time zone"),
        ("default=2026-09-14@Europe/Zurich", "no names"),
        ("default=2026-09-15@Europe/Zurich:alice,bob", "not a Monday"),
        ("default=nonsense@Europe/Zurich:alice,bob", "not a YYYY-MM-DD date"),
        ("default=2026-09-14@Mars/Base:alice,bob", "unknown time zone"),
        ("default=2026-09-14@Europe/Zurich:alice", "at least two names"),
        ("default=2026-09-14@Europe/Zurich:alice,alice", "twice"),
        ("default=2026-09-14@Europe/Zurich:Alice,bob", "is not [a-z0-9]"),
        ("Default=2026-09-14@Europe/Zurich:alice,bob", "seat label"),
        ("=2026-09-14@Europe/Zurich:alice,bob", "seat label"),
    ],
)
def test_every_malformed_entry_is_reported(entry: str, fragment: str) -> None:
    specs, errors = sr.parse_codex_seat_rota([entry])
    assert specs == {}
    assert len(errors) == 1 and fragment in errors[0].error
    assert errors[0].entry == entry


def test_a_second_entry_for_one_seat_is_reported_and_the_first_wins() -> None:
    specs, errors = sr.parse_codex_seat_rota(
        [
            "default=2026-09-14@Europe/Zurich:alice,bob",
            "default=2026-09-21@Europe/Zurich:carol,dave",
        ]
    )
    assert specs["default"].names == ("alice", "bob")
    assert len(errors) == 1 and "second rota" in errors[0].error


def test_a_non_monday_error_names_that_weeks_monday() -> None:
    _specs, errors = sr.parse_codex_seat_rota(["default=2026-09-17@Europe/Zurich:alice,bob"])
    assert "Thursday" in errors[0].error and "2026-09-14" in errors[0].error


def test_local_zone_name_is_a_real_key_or_none() -> None:
    name = sr.local_zone_name()
    assert name is None or sr.valid_zone(name)


# ── the resolver: one seat's row, with and without a rota ────────────────────────
def _write_rota(seats: SeatFixture, *entries: str, me: str = "bob") -> None:
    """Point the fixture's config.toml at *entries* (and say who we are)."""
    path = seats.ccc_home / "command-center" / "config.toml"
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("codex_seat_rota", "codex_seat_rota_me"))
    ]
    listed = ", ".join(f'"{entry}"' for entry in entries)
    lines += [f"codex_seat_rota = [{listed}]", f'codex_seat_rota_me = "{me}"']
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.invalidate_config_cache()


def _this_monday(zone: str = _ZURICH) -> date:
    """The Monday of the CURRENT week in *zone* — the anchor a live-clock test needs."""
    today = datetime.now(ZoneInfo(zone)).date()
    return today - timedelta(days=today.weekday())


def _entry(label: str, names: str, *, weeks: int = 0) -> str:
    """A rota entry anchored *weeks* from this week (so the live clock is deterministic)."""
    return f"{label}={(_this_monday() + timedelta(days=7 * weeks)).isoformat()}@{_ZURICH}:{names}"


def _row_for(seats: SeatFixture, label: str, now: int | None = None) -> quota.ProviderQuota:
    now_ts = int(time.time()) if now is None else now
    pid = "codex" if label == "default" else f"codex:{label}"
    return quota._codex_seat_quota(  # noqa: SLF001
        pid, label, seats.seats[label], now_ts, quota.read_cooldowns(now_ts)
    )


def test_an_off_week_seat_is_blocked_until_our_next_monday(three_seats: SeatFixture) -> None:
    _write_rota(three_seats, _entry("private", "alice,bob"))
    row = _row_for(three_seats, "private")
    assert (row.state, row.blocked_by, row.block_scope, row.source) == (
        quota.BLOCKED,
        "rota",
        "rota",
        "rota",
    )
    assert row.reason == row.rota["label"] and " used by alice" in row.reason
    assert row.resets_at == row.rota["next_mine_at"]  # our next Monday 00:00, Zurich
    assert row.rota["next_mine_label"].startswith("Mon ")
    assert (row.rota["holder"], row.rota["mine"], row.rota["me"]) == ("alice", False, "bob")
    assert row.rota["underlying"]["state"] == quota.UNKNOWN  # what the wrapper replaced


def test_our_own_week_is_the_ordinary_verdict_with_the_rota_attached(
    three_seats: SeatFixture,
) -> None:
    _write_rota(three_seats, _entry("private", "bob,alice"))
    row = _row_for(three_seats, "private")
    assert row.state == quota.UNKNOWN  # unmeasured seat: exactly today's verdict
    assert row.blocked_by == "" and row.block_scope == ""
    assert (row.rota["mine"], row.rota["holder"]) == (True, "bob")
    assert "underlying" not in row.rota  # nothing was replaced
    assert row.rota["next_holder"] == "alice"


def test_a_hold_outlasting_our_next_week_is_not_promised_away(three_seats: SeatFixture) -> None:
    """``resets_at = max(next own Monday, the underlying block)`` — debate O10."""
    now = int(time.time())
    hold_until = now + 30 * 86400  # far beyond the next handover
    quota.record_block(
        "codex:private",
        blocked_until=hold_until,
        observed_at=now,
        kind=quota.KIND_HOLD,
        reason="private seat reserved",
    )
    _write_rota(three_seats, _entry("private", "alice,bob"))
    row = _row_for(three_seats, "private")
    assert row.blocked_by == "rota" and row.resets_at == hold_until
    assert row.rota["underlying"]["blocked_by"] == "hold"
    assert row.rota["underlying"]["reason"] == "private seat reserved"
    assert row.rota["underlying"]["resets_at"] == hold_until
    assert row.rota["underlying"]["resets_label"]  # "D.M." in the rota's own zone


def test_a_seat_on_no_rota_is_untouched(three_seats: SeatFixture) -> None:
    _write_rota(three_seats, _entry("private", "alice,bob"))
    assert _row_for(three_seats, "de").rota == {}
    assert _row_for(three_seats, "default").rota == {}


# ── fail closed: an unusable rota BLOCKS the seat it names ───────────────────────
def test_an_invalid_entry_blocks_the_seat_it_names(three_seats: SeatFixture) -> None:
    _write_rota(three_seats, "private=2026-09-15@Europe/Zurich:alice,bob")  # a Tuesday
    row = _row_for(three_seats, "private")
    assert (row.state, row.blocked_by) == (quota.BLOCKED, "rota")
    assert row.reason.startswith("rota: invalid entry (") and "not a Monday" in row.reason
    assert row.resets_at == 0  # there is no date to wait for — a human must fix the config


@pytest.mark.parametrize("me", ["", "carol", "Bob"])
def test_a_rota_that_does_not_name_us_blocks_the_seat(three_seats: SeatFixture, me: str) -> None:
    """Membership is decided PER SEAT: not being on it is not the same as owning it."""
    _write_rota(three_seats, _entry("private", "alice,bob"), me=me)
    row = _row_for(three_seats, "private")
    assert (row.state, row.blocked_by) == (quota.BLOCKED, "rota")
    assert row.reason == f"rota: codex_seat_rota_me {me!r} is not one of alice,bob"
    assert row.rota["holder"] == "alice"  # the schedule IS known; only "ours?" is not
    assert row.rota["next_mine_at"] == 0 and row.rota["next_mine_label"] == ""


def test_an_entry_naming_no_configured_seat_is_reported_and_ignored(
    three_seats: SeatFixture,
) -> None:
    _write_rota(three_seats, _entry("nope", "alice,bob"))
    snap = quota.snapshot()
    assert [row["rota"] for row in snap["codex_seat_order"]] == [None, None, None]
    errors = snap["codex_seat_rota_errors"]
    assert len(errors) == 1 and errors[0]["label"] == "nope"
    assert "no Codex seat with this label" in errors[0]["error"]


def test_snapshot_carries_the_rota_and_its_errors(three_seats: SeatFixture) -> None:
    _write_rota(three_seats, _entry("private", "alice,bob"), "junk")
    snap = quota.snapshot()
    rows = {row["label"]: row for row in snap["codex_seat_order"]}
    assert rows["private"]["rota"]["holder"] == "alice"
    assert rows["private"]["blocked_by"] == "rota"
    assert rows["de"]["rota"] is None
    assert [err["entry"] for err in snap["codex_seat_rota_errors"]] == ["junk"]
    # the same object survives the providers payload and its round trip
    provider = next(p for p in snap["providers"] if p["id"] == "codex:private")
    assert quota._rehydrate(provider).rota["holder"] == "alice"  # noqa: SLF001


def test_parse_problems_are_printed_once_per_process(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Loud, but not once per resolution — the rota is resolved several times per run."""
    monkeypatch.setattr(quota, "_ROTA_WARNED", False)
    _write_rota(three_seats, "junk")
    quota.snapshot()
    err = capsys.readouterr().err
    assert err.count("codex_seat_rota 'junk'") == 1
    quota.snapshot()
    assert "codex_seat_rota" not in capsys.readouterr().err


# ── the ranking: a rota block removes the seat, pin or no pin ────────────────────
@pytest.mark.parametrize("policy", ["fill", "order"])
def test_the_ranking_skips_an_off_week_seat_even_when_pinned(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    _write_rota(three_seats, _entry("private", "alice,bob"))
    monkeypatch.setattr(config, "codex_seat_policy", lambda: policy)
    cic.save_config({"codex_home": str(three_seats.seats["private"]), "codex_home_until": None})
    labels = [cand.label for cand in cic.codex_homes_in_order()]
    assert "private" not in labels
    assert labels[:1] == ["de"]  # the configured order's next seat, pin ignored


def test_the_ranking_flips_at_the_monday_boundary(three_seats: SeatFixture) -> None:
    """The same config, two instants: the seat comes back by itself at 00:00 Zurich."""
    _write_rota(three_seats, f"private={_ANCHOR.isoformat()}@{_ZURICH}:alice,bob")
    sunday = _at("2026-09-20T23:59:59")
    monday = _at("2026-09-21T00:00:00")
    assert "private" not in [c.label for c in cic.codex_homes_in_order(sunday)]
    assert [c.label for c in cic.codex_homes_in_order(monday)][0] == "private"


def test_headroom_is_the_empty_pool_verdict_when_every_seat_is_off_week(
    three_seats: SeatFixture,
) -> None:
    _write_rota(
        three_seats,
        _entry("private", "alice,bob"),
        _entry("de", "alice,bob"),
        _entry("default", "alice,bob"),
    )
    decision = cic.codex_headroom()
    assert decision["state"] == "blocked"
    assert decision["reason"] == "no eligible seat"
    assert decision["seats"] == []


# ── the runner: no process, and -Q does not buy the week ─────────────────────────
def _run_ns(seats: SeatFixture, **kw: object) -> argparse.Namespace:
    """A `run` argv namespace pointed at the fixture's workdir."""
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": "gpt-5.6-sol",
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


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline — `valid_slug` must not shell out to codex."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [
            {"slug": "gpt-5.6-sol", "visibility": "list", "default_reasoning_level": "medium"}
        ],
    )


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict:
    """The single JSON object `run -j` prints."""
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_an_off_week_seat_is_never_launched_and_the_run_falls_through(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_rota(three_seats, _entry("private", "alice,bob"))
    three_seats.scenarios(de={"scenario": "ok", "reply": "de answered"})
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_OK
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["de"]  # private was never started
    assert envelope["seat"]["label"] == "de"
    assert quota.read_cooldowns() == {}  # a rota writes NOTHING to the cooldown store


def test_ignore_quota_does_not_waive_the_rota(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-Q` accepts a refusal; it cannot accept billing somebody else's week (O7)."""
    _write_rota(three_seats, _entry("private", "alice,bob"))
    three_seats.scenarios(de={"scenario": "ok", "reply": "de answered"})
    assert cic.cmd_run(_run_ns(three_seats, ignore_quota=True)) == cic.EX_OK
    envelope = _envelope(capsys)
    assert three_seats.call_homes() == ["de"]
    assert [a["outcome"] for a in envelope["attempts"]] == ["ok"]  # private is not a candidate


def test_an_explicit_codex_home_on_its_off_week_starts_no_process(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A REGISTERED $CODEX_HOME resolves to its seat row, so the rota governs it too."""
    _write_rota(three_seats, _entry("private", "alice,bob"))
    monkeypatch.setenv("CODEX_HOME", str(three_seats.seats["private"]))
    assert cic.cmd_run(_run_ns(three_seats)) == cic.EX_QUOTA
    envelope = _envelope(capsys)
    assert three_seats.calls() == []
    assert envelope["error"]["kind"] == "all_seats_unavailable"
    assert (
        "rota:" in envelope["error"]["message"] and "used by alice" in envelope["error"]["message"]
    )
    assert [a["outcome"] for a in envelope["attempts"]] == ["skipped:rota"]


def test_a_journal_resume_bound_to_an_off_week_seat_is_refused(
    three_seats: SeatFixture,
) -> None:
    """A resume cannot hop, so an off-week seat means no run at all — never a hop."""
    _write_rota(three_seats, _entry("private", "alice,bob"))
    codex_launch.record_launch(
        "01a06cfb-5cd0-7cb0-836d-e053998a7c64",
        str(three_seats.workdir),
        write=False,
        codex_home=three_seats.seats["private"],
    )
    _record, home = codex_launch.resolve_resume_any(
        "last", write=False, homes=cic.canonical_codex_homes()
    )
    assert home == three_seats.seats["private"]
    result = cic.run_with_fallback(
        prompt="hi",
        build_cmd=lambda *_a: ["/bin/false"],
        write=False,
        workdir=str(three_seats.workdir),
        total_timeout=60,
        idle_timeout=0,
        purpose="test",
        model="gpt-5.6-sol",
        effort="low",
        heartbeat_meta={},
        resume_home=home,
    )
    assert three_seats.calls() == []
    assert result.error_kind == "all_seats_unavailable"
    assert [a.outcome for a in result.attempts] == ["skipped:rota"]


def test_the_order_table_names_our_own_week(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_rota(three_seats, _entry("private", "bob,alice"))
    assert cic.cmd_order(argparse.Namespace(labels=[], clear=False, json=False)) == cic.EX_OK
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if row.strip().startswith("1  private"))
    assert "·  rota: " in line and " used by bob" in line
    assert "⚠" not in line  # a fact about the seat, not a warning


# ── cmd_rota ─────────────────────────────────────────────────────────────────────
def _rota_ns(verb: str = "show", **kw: object) -> argparse.Namespace:
    base: dict[str, object] = {"verb": verb, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_rota_show_says_so_when_nothing_is_shared(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cic.cmd_rota(_rota_ns()) == cic.EX_OK
    assert capsys.readouterr().out.strip() == "no seat on a rota"
    _ = three_seats


def test_rota_set_persists_and_prints_the_state(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cic.cmd_rota(_rota_ns("me", name="bob")) == cic.EX_OK
    capsys.readouterr()
    args = _rota_ns("set", label="private", names=["alice", "bob"], start="2026-09-14", tz=_ZURICH)
    assert cic.cmd_rota(args) == cic.EX_OK
    out = capsys.readouterr().out
    assert "private" in out and "used by " in out
    assert config.codex_seat_rota() == ["private=2026-09-14@Europe/Zurich:alice,bob"]
    assert config.codex_seat_rota_me() == "bob"
    # …and setting it again REPLACES that seat's entry instead of appending a second one.
    args = _rota_ns("set", label="private", names=["bob", "alice"], start="2026-09-21", tz=_ZURICH)
    assert cic.cmd_rota(args) == cic.EX_OK
    assert config.codex_seat_rota() == ["private=2026-09-21@Europe/Zurich:bob,alice"]


def test_rota_set_defaults_the_zone_to_this_machine(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cic.seat_rota if hasattr(cic, "seat_rota") else sr, "local_zone_name", lambda: _ZURICH
    )
    args = _rota_ns("set", label="private", names=["alice", "bob"], start="2026-09-14", tz=None)
    assert cic.cmd_rota(args) == cic.EX_OK
    capsys.readouterr()
    assert config.codex_seat_rota() == ["private=2026-09-14@Europe/Zurich:alice,bob"]
    _ = three_seats


def test_rota_set_refuses_an_unnameable_machine_zone(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sr, "local_zone_name", lambda: None)
    args = _rota_ns("set", label="private", names=["alice", "bob"], start="2026-09-14", tz=None)
    assert cic.cmd_rota(args) == cic.EX_USAGE
    assert "pass -z Europe/Zurich" in capsys.readouterr().err
    assert config.codex_seat_rota() == []
    _ = three_seats


@pytest.mark.parametrize(
    ("kw", "fragment"),
    [
        ({"label": "nope"}, "unknown seat label"),
        ({"start": "2026-09-15"}, "not a Monday"),
        ({"start": "not-a-date"}, "not a YYYY-MM-DD date"),
        ({"tz": "Mars/Base"}, "unknown time zone"),
        ({"names": ["alice"]}, "at least two names"),
        ({"names": ["alice", "alice"]}, "twice"),
        ({"names": ["Alice", "bob"]}, "is not [a-z0-9]"),
    ],
)
def test_rota_set_refusals(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], kw: dict, fragment: str
) -> None:
    base = {"label": "private", "names": ["alice", "bob"], "start": "2026-09-14", "tz": _ZURICH}
    assert cic.cmd_rota(_rota_ns("set", **{**base, **kw})) == cic.EX_USAGE
    assert fragment in capsys.readouterr().err
    assert config.codex_seat_rota() == []  # nothing is written by a refusal
    _ = three_seats


def test_rota_clear_and_its_refusal(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_rota(three_seats, _entry("private", "alice,bob"))
    assert cic.cmd_rota(_rota_ns("clear", label="de")) == cic.EX_USAGE
    assert "no rota entry for 'de'" in capsys.readouterr().err
    assert cic.cmd_rota(_rota_ns("clear", label="private")) == cic.EX_OK
    assert config.codex_seat_rota() == []
    assert _row_for(three_seats, "private").rota == {}  # and the seat is ours again


def test_rota_me_refuses_a_bad_name_and_sets_a_good_one(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cic.cmd_rota(_rota_ns("me", name="Bob!")) == cic.EX_USAGE
    assert "is not a rota name" in capsys.readouterr().err
    assert config.codex_seat_rota_me() == ""
    assert cic.cmd_rota(_rota_ns("me", name="bob")) == cic.EX_OK
    assert config.codex_seat_rota_me() == "bob"
    _ = three_seats


def test_rota_json_reports_seats_and_errors(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_rota(three_seats, f"private={_ANCHOR.isoformat()}@{_ZURICH}:alice,bob", "junk")
    assert cic.cmd_rota(_rota_ns(json=True)) == cic.EX_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1 and payload["me"] == "bob"
    seat = payload["seats"]["private"]
    assert seat["names"] == ["alice", "bob"]
    assert (seat["anchor"], seat["tz"]) == (_ANCHOR.isoformat(), _ZURICH)
    assert set(seat) == {
        "holder",
        "mine",
        "names",
        "anchor",
        "tz",
        "week_start",
        "week_end_exclusive",
        "label",
        "next_mine_at",
        "next_mine_label",
        "next_holder",
        "next_other_at",
        "next_other_label",
    }
    assert payload["errors"][0]["entry"] == "junk"


def test_every_verb_works_while_another_entry_is_unreadable(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken rota must not lock the operator out of the command that repairs it."""
    _write_rota(three_seats, "junk", _entry("de", "alice,bob"))
    assert cic.cmd_rota(_rota_ns()) == cic.EX_OK
    assert "junk" in capsys.readouterr().out
    args = _rota_ns("set", label="private", names=["alice", "bob"], start="2026-09-14", tz=_ZURICH)
    assert cic.cmd_rota(args) == cic.EX_OK
    capsys.readouterr()
    assert "junk" in config.codex_seat_rota()  # the unreadable entry SURVIVES a write
    assert cic.cmd_rota(_rota_ns("clear", label="de")) == cic.EX_OK
    assert "junk" in config.codex_seat_rota()
    assert cic.cmd_rota(_rota_ns("me", name="alice")) == cic.EX_OK


def test_rota_writes_refuse_a_config_with_unknown_keys(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """``save_config`` re-emits only known keys — refuse rather than delete the rest."""
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + 'some_future_key = "x"\n', encoding="utf-8")
    config.invalidate_config_cache()
    args = _rota_ns("set", label="private", names=["alice", "bob"], start="2026-09-14", tz=_ZURICH)
    assert cic.cmd_rota(args) == cic.EX_USAGE
    assert "some_future_key" in capsys.readouterr().err
    assert cic.cmd_rota(_rota_ns("me", name="bob")) == cic.EX_USAGE
    assert "some_future_key" in capsys.readouterr().err


def test_the_cli_parses_every_rota_verb() -> None:
    """The shipped spellings, through the real parser (the epilog advertises this one)."""
    parser = cic.build_parser()
    args = parser.parse_args(["rota", "set", "default", "-s", "2026-09-14", "alice", "bob"])
    assert (args.cmd, args.verb, args.label, args.names) == (
        "rota",
        "set",
        "default",
        ["alice", "bob"],
    )
    assert (args.start, args.tz) == ("2026-09-14", None)
    # argparse leaves the subparsers dest None; ``cmd_rota`` reads that as ``show``.
    assert parser.parse_args(["rota"]).verb is None
    assert parser.parse_args(["rota", "-j"]).json is True
    assert parser.parse_args(["rota", "show", "-j"]).json is True
    assert parser.parse_args(["rota", "-j", "show"]).json is True  # the parent's -j survives
    assert parser.parse_args(["rota", "clear", "default"]).label == "default"
    assert parser.parse_args(["rota", "me", "bob"]).name == "bob"
    for argv in (["rota", "set", "default", "alice"], ["rota", "clear"], ["rota", "me"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
