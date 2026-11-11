"""Parked prompts (park.py + fire_at plumbing): scheduling, claims, dispatch, markers."""

# pylint: disable=too-many-lines  # one module covers park, its fire plumbing and dispatch

from __future__ import annotations

import argparse
import fcntl
import io
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from bridgestub import SID, TAB, World, fixture_screen, turn_end, user

from command_center import cli, config, daemon, park, usage
from command_center.bridge_target import find_live, guarded_paste, is_live
from command_center.screenread import ScreenCell, ScreenRead, line_text, rule_rows
from command_center.snapshot import PsTimedOut
from command_center.store import Store

NOW = 1_700_000_000
ATT_PROMPT = "tp1867 attached prompt"
#: A screen with no full-width rule: the parser cannot find the input box.
UNRECOGNIZED = ScreenRead(80, (0, 0), ((ScreenCell("x"), ScreenCell("y")),))


def _box_draft(name: str = "draft_one_line") -> str:
    """The masked draft text of a fixture's input box (the lines between the two rules)."""
    read = fixture_screen(name)
    rows = rule_rows(read)
    lines = [line_text(read.lines[y]) for y in range(rows[-2] + 1, rows[-1])]
    return " ".join(line.replace("\x00", " ").strip(" ❯") for line in lines).strip()


def _snapshot(
    *,
    five_hour: usage.Window | None = None,
    seven_day: usage.Window | None = None,
    oauth_fetched_at: int = NOW,
) -> usage.Usage:
    return usage.Usage(
        captured_at=NOW,
        five_hour=five_hour,
        seven_day=seven_day,
        oauth_fetched_at=oauth_fetched_at,
    )


# ---- fire_time --------------------------------------------------------------


def test_fire_time_is_resets_at_plus_buffer_regardless_of_utilization() -> None:
    snap = _snapshot(five_hour=usage.Window(used_percentage=12.0, resets_at=NOW + 3600))
    assert park.fire_time(snap, "five_hour", NOW, 90) == NOW + 3690


def test_fire_time_none_without_usable_reset() -> None:
    assert park.fire_time(None, "five_hour", NOW) is None  # no snapshot
    snap = _snapshot(five_hour=usage.Window(used_percentage=100.0, resets_at=NOW - 10))
    assert park.fire_time(snap, "five_hour", NOW) is None  # reset already passed
    assert park.fire_time(snap, "seven_day", NOW) is None  # window absent
    assert park.fire_time(snap, "bogus", NOW) is None  # unknown window name


def test_fire_time_selects_only_the_requested_window() -> None:
    snap = _snapshot(
        five_hour=usage.Window(used_percentage=100.0, resets_at=NOW + 1000),
        seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 90_000),
    )
    assert park.fire_time(snap, "seven_day", NOW, 0) == NOW + 90_000


# ---- postpone_until ----------------------------------------------------------


def test_postpone_only_on_fresh_exhaustion_of_the_own_window() -> None:
    snap = _snapshot(seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 5000))
    assert park.postpone_until(snap, "seven_day", NOW, buffer_sec=90) == NOW + 5090


def test_postpone_ignores_other_windows() -> None:
    # An exhausted weekly window must never hold back a five_hour job (debate R2-O1).
    snap = _snapshot(
        five_hour=usage.Window(used_percentage=40.0, resets_at=NOW + 900),
        seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 500_000),
    )
    assert park.postpone_until(snap, "five_hour", NOW) is None


def test_retired_fable_week_window_reads_as_seven_day() -> None:
    """A job armed on the retired ``fable_week`` window fires and postpones on ``seven_day``."""
    assert park.normalize_window("fable_week") == "seven_day"
    assert park.normalize_window("five_hour") == "five_hour"
    assert "fable_week" not in park.WINDOW_CHOICES
    snap = _snapshot(seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 5000))
    assert park.fire_time(snap, "fable_week", NOW, 90) == NOW + 5090
    assert park.postpone_until(snap, "fable_week", NOW, buffer_sec=90) == NOW + 5090


def test_postpone_requires_fresh_oauth_and_real_exhaustion() -> None:
    stale = _snapshot(
        seven_day=usage.Window(used_percentage=100.0, resets_at=NOW + 5000),
        oauth_fetched_at=NOW - 3600,
    )
    assert park.postpone_until(stale, "seven_day", NOW) is None  # stale ⇒ fire as recorded
    fresh_ok = _snapshot(seven_day=usage.Window(used_percentage=99.0, resets_at=NOW + 5000))
    assert park.postpone_until(fresh_ok, "seven_day", NOW) is None  # not exhausted
    assert park.postpone_until(fresh_ok, "", NOW) is None  # no window recorded
    assert park.postpone_until(None, "seven_day", NOW) is None


# ---- format_fire ---------------------------------------------------------------


def test_format_fire_future_now_and_overdue() -> None:
    hhmm = time.strftime("%H:%M", time.localtime(NOW + 2220))
    assert park.format_fire(NOW + 2220, NOW) == f"fires {hhmm} (in 37m)"
    assert park.format_fire(NOW + 30, NOW) == "fires now"
    assert park.format_fire(NOW - park.FIRE_GRACE_SEC, NOW) == "fires now"
    assert park.format_fire(NOW - 300, NOW) == "overdue 5m"
    assert park.format_fire(NOW - (2 * 86400 + 3 * 3600), NOW) == "overdue 2d 3h"


# ---- prompt guards --------------------------------------------------------------


def test_prompt_size_error_caps_argv_budget() -> None:
    assert park.prompt_size_error("fine") is None
    assert "argv budget" in (park.prompt_size_error("x" * (park.MAX_PROMPT_BYTES + 1)) or "")


def test_resolve_prompt_positional_wins_and_empty_stdin_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(prompt="do the thing", clipboard=False)
    assert park._resolve_prompt(args, "hdr") == (  # pylint: disable=protected-access
        "do the thing",
        None,
    )
    empty = io.StringIO("")
    monkeypatch.setattr("sys.stdin", empty)
    text, err = park._resolve_prompt(  # pylint: disable=protected-access
        argparse.Namespace(prompt=None, clipboard=False), "hdr"
    )
    assert text is None and err is not None


def test_pick_editor_falls_back_when_configured_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setenv("EDITOR", "code -w")  # the observed real-world failure
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vim" if name == "vim" else None)
    argv, note = park._pick_editor()  # pylint: disable=protected-access
    assert argv == ["vim"]
    assert note is not None and "code -w" in note
    # Nothing runnable at all → a final error, not a crash.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv, note = park._pick_editor()  # pylint: disable=protected-access
    assert argv is None and note is not None and note.startswith("error:")


def test_pick_editor_uses_configured_editor_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    monkeypatch.setenv("EDITOR", "code -w")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/code")
    argv, note = park._pick_editor()  # pylint: disable=protected-access
    assert argv == ["code", "-w"] and note is None


# ---- grab mode (the q+p chord panel) --------------------------------------------


def _grab_setup(monkeypatch: pytest.MonkeyPatch, panel_text: str | None) -> list[str]:
    from command_center import parkpanel, peek

    notified: list[str] = []
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: None)
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", lambda: "/tmp")
    monkeypatch.setattr(
        parkpanel, "capture_prompt", lambda header, initial="", poll=None: panel_text
    )
    monkeypatch.setattr(
        "command_center.notify.notify", lambda title, msg, channels: notified.append(title)
    )
    snap = _snapshot(five_hour=usage.Window(used_percentage=100.0, resets_at=NOW + 1200))
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    monkeypatch.setattr(time, "time", lambda: NOW)
    return notified


def test_grab_registers_armed_job_for_frontmost_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    notified = _grab_setup(monkeypatch, "panel prompt\nsecond line")
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.cwd == "/tmp" and job.prompt == "panel prompt\nsecond line"
        assert job.fire_at == NOW + 1200 + park.DEFAULT_BUFFER_SEC
        assert job.fire_window == "five_hour" and job.aim == "panel prompt"
    assert notified == ["⏳ prompt parked"]


def test_grab_trusts_the_parked_folder_at_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tp#1512: a park is a human registration, so its folder is trusted right away."""
    from command_center import accounts, peek

    _grab_setup(monkeypatch, "panel prompt")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", lambda: str(repo))
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        job = next(s for s in store.list_sessions() if s.draft)
    assert accounts.is_trusted(job.config_dir, repo)
    assert f"✅ trusted {repo.resolve()} for " in capsys.readouterr().err


def test_grab_cancel_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _grab_setup(monkeypatch, None)
    assert cli.main(["park", "-g"]) == 130
    with Store() as store:
        assert not [s for s in store.list_sessions() if s.draft]


# ---- attach mode (q+p over a LIVE Claude session tab) -----------------------------


def _attach_setup(monkeypatch: pytest.MonkeyPatch, panel_text: str | None) -> list[str]:
    """A live tracked session in the frontmost tab + a panel returning *panel_text*."""
    from types import SimpleNamespace

    from command_center import peek
    from command_center.adapters import claude as claude_adapter

    notified = _grab_setup(monkeypatch, panel_text)
    with Store() as store:
        store.ensure("live-1", cwd="/tmp")
        store.update_fields("live-1", iterm_session_id="w0t0p0:UUID-LIVE", aim="the aim")
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: "UUID-LIVE")
    monkeypatch.setattr(peek, "_session_for_uuid", lambda store, uuid: store.get("live-1"))

    class _FakeAdapter:
        def discover(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(session_id="live-1", alive=True, kind="interactive")]

    monkeypatch.setattr(claude_adapter, "ClaudeAdapter", _FakeAdapter)
    return notified


def test_grab_attaches_to_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    notified = _attach_setup(monkeypatch, "the real prompt")
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and not row.draft
        assert row.prompt == "the real prompt"
        assert row.fire_at == NOW + 1200 + park.DEFAULT_BUFFER_SEC
        assert row.fire_window == "five_hour"
        assert not [s for s in store.list_sessions() if s.draft]  # NO detached job
    assert notified == ["⏳ prompt parked"]


def test_grab_new_job_flag_skips_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    _attach_setup(monkeypatch, "detached prompt")
    assert cli.main(["park", "-g", "-j"]) == 0
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and row.fire_at == 0 and row.prompt is None
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.prompt == "detached prompt"


# ---- `ccc park -g -N` into the attached live tab: the paste guard (tp#1867 B4) -------

NOW_PROMPT = "tp1867 park now prompt"


@pytest.fixture(name="pnow")
def pnow_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The frontmost tab is an established live session (bridgestub World, whose store is
    the default one ``grab`` opens); the panel returns :data:`NOW_PROMPT`; the resolver's
    adapter and the paste deps each count their registry reads."""
    import dataclasses

    from command_center import bridge_target, peek
    from command_center.adapters import claude as claude_adapter

    _grab_setup(monkeypatch, NOW_PROMPT)
    world = World(tmp_path)
    world.write(user("earlier"), *turn_end())
    world.db = config.db_path()
    with Store() as store:
        store.ensure(SID, cwd="/repo")
        store.update_fields(SID, iterm_session_id=TAB, config_dir=str(world.config_dir))
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", lambda: TAB.rsplit(":", maxsplit=1)[-1])
    monkeypatch.setattr(peek, "_session_for_uuid", lambda store, uuid: store.get(SID))
    reads = {"adapter": 0, "deps": 0}

    class _CountingAdapter:
        def discover(self) -> list[Any]:
            reads["adapter"] += 1
            return [world.live()]

    def _deps_discover() -> list[Any]:
        reads["deps"] += 1
        return world.discover()

    monkeypatch.setattr(claude_adapter, "ClaudeAdapter", _CountingAdapter)
    deps = dataclasses.replace(world.deps(), discover=_deps_discover)
    monkeypatch.setattr(bridge_target, "default_deps", lambda: deps)
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "command_center.notify.notify", lambda title, msg, channels: notes.append((title, msg))
    )
    return {"world": world, "reads": reads, "notes": notes, "deps": deps}


def _pnow_row() -> Any:
    with Store() as store:
        row = store.get(SID)
    assert row is not None
    return row


def test_paste_guard_park_now_empty_box_types_and_exits_0(
    pnow: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world: World = pnow["world"]
    assert cli.main(["park", "-g", "-N"]) == 0
    assert world.texts == [NOW_PROMPT]
    row = _pnow_row()
    assert row.fire_at == 0 and row.prompt is None  # delivered, nothing armed
    assert pnow["notes"] == [("⏳ prompt delivered", f"delivered into session {SID[:8]}")]
    assert f"delivered into session {SID[:8]}" in capsys.readouterr().out


def test_paste_guard_park_now_draft_parks_with_the_hold_lease(
    pnow: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world: World = pnow["world"]
    world.screen = fixture_screen("draft_one_line")
    assert cli.main(["park", "-g", "-N"]) == 0
    assert not world.texts
    row = _pnow_row()
    lease = daemon.hold_lease(config.load_config(), NOW)
    assert lease == NOW + 240
    assert (row.prompt, row.fire_at, row.fire_window) == (NOW_PROMPT, lease, "")
    assert (row.prompt_held_since, row.prompt_held_notice_at) == (NOW, NOW)
    retry = time.strftime("%H:%M", time.localtime(lease))
    want = (
        f"the input box of {SID[:8]} holds 28 chars — clear it in that tab — prompt parked, "
        f"retrying at {retry}"
    )
    assert pnow["notes"] == [("⏳ prompt held", want)]
    out = capsys.readouterr().out
    assert want in out
    for text in (want, out):
        assert NOW_PROMPT not in text and _box_draft() not in text


def test_paste_guard_park_now_timed_out_ps_parks_held_and_exits_0(
    pnow: dict[str, Any],
) -> None:
    # tp#1868 B5: `park -g -N` on a timed-out ps parks with the hold lease, exit 0.
    world: World = pnow["world"]
    world.ps_queue = [PsTimedOut()]
    assert cli.main(["park", "-g", "-N"]) == 0
    assert not world.texts
    row = _pnow_row()
    lease = daemon.hold_lease(config.load_config(), NOW)
    assert (row.prompt, row.fire_at, row.prompt_held_since) == (NOW_PROMPT, lease, NOW)
    [(title, message)] = pnow["notes"]
    assert title == "⏳ prompt held" and "could not be read (machine under load)" in message


def test_paste_guard_park_now_held_prompt_is_silent_on_the_daemons_first_pass(
    pnow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The park notice already went out: the daemon's next held pass does not repeat it."""
    world: World = pnow["world"]
    world.screen = fixture_screen("draft_one_line")
    assert cli.main(["park", "-g", "-N"]) == 0
    daemon_notes: list[str] = []
    monkeypatch.setattr(daemon, "notify", lambda title, msg, channels: daemon_notes.append(title))
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    monkeypatch.setattr(time, "time", lambda: NOW + 300)
    report = daemon.DaemonReport()
    with Store() as store:
        daemon._deliver_attached_prompts(  # pylint: disable=protected-access
            store,
            config.load_config(),
            report,
            False,
            {SID: world.live()},
            paster=lambda target, text, claim: guarded_paste(
                pnow["deps"], target, text, claim=claim
            ),
            still_alive=lambda sid: True,
        )
    assert report.attached_held == [f"{SID[:8]}:input_not_empty"] and not daemon_notes
    world.screen = fixture_screen("empty_idle")
    monkeypatch.setattr(time, "time", lambda: NOW + 600)
    with Store() as store:
        daemon._deliver_attached_prompts(  # pylint: disable=protected-access
            store,
            config.load_config(),
            daemon.DaemonReport(),
            False,
            {SID: world.live()},
            paster=lambda target, text, claim: guarded_paste(
                pnow["deps"], target, text, claim=claim
            ),
            still_alive=lambda sid: True,
        )
    assert world.texts == [NOW_PROMPT] and daemon_notes == ["⏳ parked prompt delivered"]


def test_paste_guard_park_now_types_although_a_key_was_just_pressed(
    pnow: dict[str, Any],
) -> None:
    world: World = pnow["world"]
    world.frontmost, world.key_idle, world.focused = True, 1.0, TAB
    assert cli.main(["park", "-g", "-N"]) == 0
    assert world.texts == [NOW_PROMPT] and world.focus_reads == 0


def test_paste_guard_park_now_panel_path_reads_the_registry_once(pnow: dict[str, Any]) -> None:
    assert cli.main(["park", "-g", "-N"]) == 0
    # The resolver's one read; the paste's only read is revalidate's, inside the tab lock.
    assert pnow["reads"] == {"adapter": 1, "deps": 1}


def test_paste_guard_park_now_falls_back_to_find_live_without_a_resolved_live(
    pnow: dict[str, Any],
) -> None:
    world: World = pnow["world"]
    cfg = config.load_config()
    code = park._park_now(  # pylint: disable=protected-access
        cfg, _pnow_row(), None, NOW_PROMPT, NOW, pnow["deps"]
    )
    assert code == 0 and world.texts == [NOW_PROMPT]
    assert pnow["reads"]["deps"] == 2  # find_live, then revalidate


def test_paste_guard_park_now_partial_exits_1_and_is_not_reparked(
    pnow: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world: World = pnow["world"]
    world.send_result = ("", "partial")
    assert cli.main(["park", "-g", "-N"]) == 1
    assert world.texts == [NOW_PROMPT]
    row = _pnow_row()
    assert row.prompt is None and row.fire_at == 0 and row.prompt_held_since == 0
    want = f"prompt pasted but not submitted in {SID[:8]} — press Return in that tab"
    assert pnow["notes"] == [("⏳ prompt not submitted", want)]
    assert f"error: {want}" in capsys.readouterr().err


def test_paste_guard_park_now_session_gone_exits_1(
    pnow: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world: World = pnow["world"]
    world.registered = False  # left between the panel's registry read and the lock
    assert cli.main(["park", "-g", "-N"]) == 1
    assert not world.texts
    row = _pnow_row()
    assert row.prompt is None and row.fire_at == 0
    err = capsys.readouterr().err
    assert f"session {SID[:8]} is no longer live (not_running)" in err
    assert [t for t, _m in pnow["notes"]] == ["⏳ park failed"]


def test_paste_guard_park_now_stale_tab_exits_1(
    pnow: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world: World = pnow["world"]
    world.tab_tty = "/dev/ttys099"
    assert cli.main(["park", "-g", "-N"]) == 1
    assert not world.texts and world.screen_reads == 0
    assert _pnow_row().fire_at == 0
    assert f"could not type into session {SID[:8]}'s tab" in capsys.readouterr().err


# ---- daemon delivery of attached prompts ---------------------------------------------


def _attached_row(store: Store, fire_at: int) -> None:
    store.ensure("att-1", cwd="/tmp")
    store.update_fields(
        "att-1",
        prompt="attached prompt",
        fire_at=fire_at,
        fire_window="five_hour",
        iterm_session_id="w0t0p0:UUID-ATT",
    )


def test_deliver_attached_injects_into_live_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center.bridge_target import PasteResult
    from command_center.models import LiveSession

    _, notified = _daemon_setup(monkeypatch)
    sent: list[tuple[str, str]] = []
    leases: list[int] = []
    now = int(time.time())

    def paster(target: LiveSession, text: str, claim: Any) -> PasteResult:
        assert claim()  # the 900 s lease, written by the claim inside the tab lock
        row = store.get("att-1")
        assert row is not None
        leases.append(row.fire_at)
        sent.append((target.session_id, text))
        return PasteResult(via="python-api", state="sent", tab="w0t0p0:UUID-ATT")

    with Store() as store:
        _attached_row(store, now - 10)
        live = {"att-1": LiveSession(pid=1, session_id="att-1", cwd="/tmp", alive=True)}
        report = daemon.DaemonReport()
        daemon._deliver_attached_prompts(  # pylint: disable=protected-access
            store, config.load_config(), report, False, live, paster=paster
        )
        assert report.reset_fired == ["att-1"] and sent == [("att-1", "attached prompt")]
        assert leases and leases[0] >= now + park.FIRE_RETRY_SEC
        row = store.get("att-1")
        assert row is not None and row.fire_at == 0
    assert notified == ["⏳ parked prompt delivered"]


def test_deliver_attached_falls_back_to_resume_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import terminal

    _, notified = _daemon_setup(monkeypatch)
    resumed: list[str] = []
    monkeypatch.setattr(
        terminal,
        "fire_attached_in_new_tab",
        lambda sid: resumed.append(sid) or True,  # type: ignore[func-returns-value]
    )
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now - 10)
        report = daemon.DaemonReport()
        daemon._deliver_attached_prompts(  # pylint: disable=protected-access
            store, config.load_config(), report, False, {}
        )
        assert resumed == ["att-1"]
        row = store.get("att-1")  # lease kept: fire-attached consumes it via claim_fire
        assert row is not None and row.fire_at >= now + park.FIRE_RETRY_SEC - 5
    assert notified == ["⏳ parked prompt resuming in a new tab"]


# ---- daemon delivery of attached prompts: the paste guard (tp#1867 B3) --------------


@pytest.fixture(name="att")
def att_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An established live session (bridgestub World) whose row carries a due attached
    prompt; notifications, new tabs and every ``fire_at`` write are recorded."""
    from command_center import terminal

    world = World(tmp_path)
    world.write(user("earlier"), *turn_end())
    store = Store(world.db)
    now = int(time.time())
    store.update_fields(SID, prompt=ATT_PROMPT, fire_at=now - 10)
    notes: list[tuple[str, str]] = []
    resumed: list[str] = []
    writes: list[int] = []
    monkeypatch.setattr(daemon, "notify", lambda title, msg, channels: notes.append((title, msg)))
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    monkeypatch.setattr(
        terminal,
        "fire_attached_in_new_tab",
        lambda sid: resumed.append(sid) or True,  # type: ignore[func-returns-value]
    )
    real_update = store.update_fields

    def spy(session_id: str, **fields: Any) -> None:
        if "fire_at" in fields:
            writes.append(int(fields["fire_at"]))
        real_update(session_id, **fields)

    monkeypatch.setattr(store, "update_fields", spy)
    cfg = config.load_config()
    cfg.daemon_interval_sec = 300
    return {
        "world": world,
        "store": store,
        "cfg": cfg,
        "now": now,
        "notes": notes,
        "resumed": resumed,
        "writes": writes,
    }


def _attached_pass(att: dict[str, Any], *, live: bool = True) -> daemon.DaemonReport:
    """One attached-prompt pass through the REAL paste guard against the World's tab."""
    world: World = att["world"]
    report = daemon.DaemonReport()
    daemon._deliver_attached_prompts(  # pylint: disable=protected-access
        att["store"],
        att["cfg"],
        report,
        False,
        {SID: world.live()} if live else {},
        paster=lambda target, text, claim: guarded_paste(world.deps(), target, text, claim=claim),
        still_alive=lambda sid: is_live(find_live(world.deps(), sid)),
    )
    return report


def _att_row(att: dict[str, Any]) -> Any:
    row = att["store"].get(SID)
    assert row is not None
    return row


def _no_retry_lease(att: dict[str, Any]) -> bool:
    """The 900 s lease was never written this test."""
    return all(w < att["now"] + park.FIRE_RETRY_SEC - 5 for w in att["writes"])


def test_paste_guard_attached_empty_box_types_and_disarms(att: dict[str, Any]) -> None:
    world: World = att["world"]
    report = _attached_pass(att)
    assert world.texts == [ATT_PROMPT] and report.reset_fired == [SID] and not report.attached_held
    row = _att_row(att)
    assert (row.fire_at, row.prompt_held_since, row.prompt_held_notice_at) == (0, 0, 0)
    assert [t for t, _m in att["notes"]] == ["⏳ parked prompt delivered"]
    assert not att["resumed"]


def test_paste_guard_attached_draft_holds_with_one_pass_lease(att: dict[str, Any]) -> None:
    world: World = att["world"]
    world.screen = fixture_screen("draft_one_line")
    report = _attached_pass(att)
    now = att["now"]
    short = SID[:8]
    assert not world.texts and not att["resumed"] and not report.reset_fired
    assert report.attached_held == [f"{short}:input_not_empty"]
    row = _att_row(att)
    assert row.fire_at - now in (240, 241) and row.prompt == ATT_PROMPT
    assert row.prompt_held_since in (now, now + 1) and row.prompt_held_notice_at
    [(title, message)] = att["notes"]
    assert title == "⏳ parked prompt held"
    assert message.startswith(
        f"parked prompt held — the input box of {short} holds 28 chars — clear it in that tab;"
        " retrying at "
    )
    assert ATT_PROMPT not in message and _box_draft() not in message
    assert _no_retry_lease(att)


def test_paste_guard_attached_hold_lease_floor_at_a_60_s_interval(att: dict[str, Any]) -> None:
    att["world"].screen = fixture_screen("draft_one_line")
    att["cfg"].daemon_interval_sec = 60
    _attached_pass(att)
    assert _att_row(att).fire_at - att["now"] in (60, 61)
    assert daemon.hold_lease(att["cfg"], 1000) == 1060
    att["cfg"].daemon_interval_sec = 300
    assert daemon.hold_lease(att["cfg"], 1000) == 1240


def test_paste_guard_attached_second_held_pass_within_ten_minutes_is_silent(
    att: dict[str, Any],
) -> None:
    att["world"].screen = fixture_screen("draft_one_line")
    now = att["now"]
    att["store"].update_fields(
        SID, fire_at=now - 5, prompt_held_since=now - 300, prompt_held_notice_at=now - 300
    )
    report = _attached_pass(att)
    assert report.attached_held and not att["notes"]
    row = _att_row(att)
    assert (row.prompt_held_since, row.prompt_held_notice_at) == (now - 300, now - 300)


def test_paste_guard_attached_held_ten_minutes_notifies_exactly_once_more(
    att: dict[str, Any],
) -> None:
    att["world"].screen = fixture_screen("draft_one_line")
    now = att["now"]
    att["store"].update_fields(
        SID, fire_at=now - 5, prompt_held_since=now - 700, prompt_held_notice_at=now - 700
    )
    _attached_pass(att)
    assert len(att["notes"]) == 1
    row = _att_row(att)
    assert row.prompt_held_since == now - 700 and row.prompt_held_notice_at >= now
    att["store"].update_fields(SID, fire_at=now - 5)
    _attached_pass(att)
    assert len(att["notes"]) == 1  # the 10-min notice went out once
    assert not att["world"].texts and not att["resumed"]


@pytest.mark.parametrize(
    ("code", "wording"),
    [
        ("user_active", "you are typing in {short}'s tab — held until the keyboard is idle"),
        ("input_not_ready", "the input box of {short} is not drawn yet"),
        ("tab_locked", "another delivery to {short}'s tab is in progress"),
        # tp#1868 B5: a timed-out ps holds; no _attached_new_tab (``resumed`` stays empty)
        ("ps_unreadable", "the process table could not be read (machine under load) for {short}"),
    ],
)
def test_paste_guard_attached_held_codes_use_their_own_wording(
    att: dict[str, Any], code: str, wording: str
) -> None:
    world: World = att["world"]
    if code == "user_active":
        world.frontmost, world.key_idle, world.focused = True, 1.0, TAB
    elif code == "input_not_ready":  # opened AIM first: no transcript, box not drawn yet
        world.transcript.unlink()
        world.transcript_resolves_missing = True
        world.screen = UNRECOGNIZED
    elif code == "ps_unreadable":
        world.ps_queue = [PsTimedOut()]
    fd = -1
    if code == "tab_locked":
        lock = world.tmp / "ccc" / "locks" / f"tab-{TAB.rsplit(':', maxsplit=1)[-1].upper()}.lock"
        lock.parent.mkdir(parents=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        report = _attached_pass(att)
    finally:
        if fd >= 0:
            os.close(fd)
    short = SID[:8]
    assert report.attached_held == [f"{short}:{code}"]
    assert not world.texts and not att["resumed"] and _no_retry_lease(att)
    [(_title, message)] = att["notes"]
    assert wording.format(short=short) in message and "chars" not in message
    assert _att_row(att).fire_at - att["now"] in (240, 241)


def test_paste_guard_attached_nothing_typed_while_alive_holds(att: dict[str, Any]) -> None:
    att["world"].send_result = ("", "none")
    report = _attached_pass(att)
    short = SID[:8]
    assert report.attached_held == [f"{short}:not_typed"] and not att["resumed"]
    [(_title, message)] = att["notes"]
    assert f"could not type into {short}'s tab" in message
    assert _att_row(att).fire_at - att["now"] in (240, 241)


def test_paste_guard_attached_partial_disarms_and_keeps_the_prompt(att: dict[str, Any]) -> None:
    world: World = att["world"]
    world.send_result = ("", "partial")
    report = _attached_pass(att)
    short = SID[:8]
    assert len(world.texts) == 1 and not att["resumed"] and not report.attached_held
    row = _att_row(att)
    assert row.fire_at == 0 and row.prompt == ATT_PROMPT
    assert att["notes"] == [
        (
            "⏳ parked prompt not submitted",
            f"prompt pasted but not submitted in {short} — press Return in that tab",
        )
    ]
    _attached_pass(att)  # disarmed: never pasted again
    assert len(world.texts) == 1


def test_paste_guard_attached_stale_tab_while_alive_holds(att: dict[str, Any]) -> None:
    world: World = att["world"]
    world.tab_tty = "/dev/ttys099"
    report = _attached_pass(att)
    short = SID[:8]
    assert report.attached_held == [f"{short}:stale_tab"]
    assert not world.texts and not att["resumed"] and world.screen_reads == 0
    [(_title, message)] = att["notes"]
    assert f"could not type into {short}'s tab (stale_tab)" in message


def test_paste_guard_attached_session_gone_opens_a_new_tab(att: dict[str, Any]) -> None:
    report = _attached_pass(att, live=False)
    assert att["resumed"] == [SID] and report.reset_fired == [SID] and not att["world"].texts
    row = _att_row(att)
    assert row.fire_at >= att["now"] + park.FIRE_RETRY_SEC and row.prompt_held_since == 0
    assert [t for t, _m in att["notes"]] == ["⏳ parked prompt resuming in a new tab"]


def test_paste_guard_attached_session_left_before_the_lock_opens_a_new_tab(
    att: dict[str, Any],
) -> None:
    world: World = att["world"]
    seen = world.live()
    world.registered = False  # the registry read inside the lock no longer lists it
    report = daemon.DaemonReport()
    daemon._deliver_attached_prompts(  # pylint: disable=protected-access
        att["store"],
        att["cfg"],
        report,
        False,
        {SID: seen},
        paster=lambda target, text, claim: guarded_paste(world.deps(), target, text, claim=claim),
        still_alive=lambda sid: is_live(find_live(world.deps(), sid)),
    )
    assert att["resumed"] == [SID] and not world.texts and not report.attached_held


def test_paste_guard_attached_old_sessions_table_gains_the_hold_columns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, "
        "cwd TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT 'claude', "
        "archived INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('old-1', '/tmp')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(sessions)")}
        assert {"prompt_held_since", "prompt_held_notice_at"} <= cols
        row = store.get("old-1")
        assert row is not None and (row.prompt_held_since, row.prompt_held_notice_at) == (0, 0)


def test_fire_attached_claims_and_execs_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    execs: list[list[str]] = []
    monkeypatch.setattr(os, "execvp", lambda prog, argv: execs.append(list(argv)))
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now + 900)
    assert cli.main(["fire-attached", "att-1"]) == 0
    assert execs == [["claude", "--resume", "att-1"]]  # prompt on stdin (tp#1681 L24)
    with Store() as store:
        row = store.get("att-1")
        assert row is not None and row.fire_at == 0
    # Second delivery attempt: the claim is gone → refuse, never resume twice.
    assert cli.main(["fire-attached", "att-1"]) == 1
    assert len(execs) == 1


def _poll_result(poll: object) -> tuple[str, str]:
    """Drive the deferred-resolution poll like the panel's timer would (bounded)."""
    assert callable(poll)
    for _ in range(500):
        result = poll()
        if result is not None:
            return result
        time.sleep(0.01)
    raise AssertionError("grab target resolution never became ready")


def test_grab_second_park_prefills_the_armed_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import parkpanel

    _attach_setup(monkeypatch, "ignored")
    with Store() as store:  # a prompt is already armed on the session
        store.update_fields(
            "live-1", prompt="old armed prompt", fire_at=NOW + 600, fire_window="five_hour"
        )
    seen_prefill: list[str] = []

    def _panel(header: str, initial: str = "", poll: object = None) -> str:
        del header, initial
        seen_prefill.append(_poll_result(poll)[1])  # what the timer would prefill
        return "edited prompt"

    monkeypatch.setattr(parkpanel, "capture_prompt", _panel)
    assert cli.main(["park", "-g"]) == 0
    assert seen_prefill == ["old armed prompt"]  # the panel reopened for editing
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and row.prompt == "edited prompt"
        assert row.fire_at == NOW + 1200 + park.DEFAULT_BUFFER_SEC  # re-armed


def test_grab_without_usable_reset_never_loses_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Detached target: saved as an UNARMED future job instead of refusing.
    _grab_setup(monkeypatch, "precious text")
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.prompt == "precious text" and job.fire_at == 0


def test_grab_attach_without_usable_reset_arms_retry_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Attach target: armed for one retry lease so hooks/claim-fire/daemon see it.
    _attach_setup(monkeypatch, "precious text")
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    assert cli.main(["park", "-g"]) == 0
    with Store() as store:
        row = store.get("live-1")
        assert row is not None and row.prompt == "precious text"
        assert row.fire_at == NOW + park.FIRE_RETRY_SEC and row.fire_window == ""


def test_claim_fire_prints_prompt_once(capsys: pytest.CaptureFixture[str]) -> None:
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now + 600)
    assert cli.main(["claim-fire", "att-1"]) == 0
    assert capsys.readouterr().out.strip() == "attached prompt"
    with Store() as store:
        row = store.get("att-1")
        assert row is not None and row.fire_at == 0  # disarmed: the daemon won't deliver
    assert cli.main(["claim-fire", "att-1"]) == 1  # one-shot


def test_hooks_announce_armed_parked_prompt(capsys: pytest.CaptureFixture[str]) -> None:
    import json as json_mod

    from command_center import hooks

    now = int(time.time())
    with Store() as store:
        _attached_row(store, now + 600)
    assert hooks.handle_user_prompt({"session_id": "att-1", "cwd": "/tmp"}) == 0
    payload = json_mod.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "parked prompt" in context.lower() and "ccc claim-fire att-1" in context
    assert hooks.handle_session_start({"session_id": "att-1", "cwd": "/tmp"}) == 0
    payload = json_mod.loads(capsys.readouterr().out)
    assert "ccc claim-fire att-1" in payload["hookSpecificOutput"]["additionalContext"]
    with Store() as store:  # disarmed → the notice disappears
        store.update_fields("att-1", fire_at=0)
    hooks.handle_user_prompt({"session_id": "att-1", "cwd": "/tmp"})
    leftover = capsys.readouterr().out
    assert "claim-fire" not in leftover


def test_fire_attached_rearms_on_exec_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(prog: str, argv: list[str]) -> None:
        raise OSError("no claude")

    monkeypatch.setattr(os, "execvp", _boom)
    now = int(time.time())
    with Store() as store:
        _attached_row(store, now - 5)
    assert cli.main(["fire-attached", "att-1"]) == 1
    with Store() as store:
        row = store.get("att-1")
        assert row is not None and row.fire_at > now  # re-armed: the daemon retries


# ---- store: columns, index, claim, summary ------------------------------------


def test_upgrade_adds_fire_columns_and_partial_index(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    # A pre-fire_at DB: the original-schema columns exist (archived was never an
    # ALTER-add), fire_at/fire_window and the partial index do not.
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, "
        "cwd TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT 'claude', "
        "archived INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    with Store(db) as store:
        cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(sessions)")}
        assert {"fire_at", "fire_window"} <= cols
        indexes = {
            row["name"]
            for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_sessions_fire_pending" in indexes
        assert "idx_sessions_armed_fire" not in indexes  # draft-scoped first cut is dropped


def test_create_draft_stamps_fire_fields_and_claim_is_one_shot() -> None:
    with Store() as store:
        store.create_draft("j1", "/tmp", "aim", fire_at=NOW + 100, fire_window="five_hour")
        job = store.get("j1")
        assert job is not None and job.fire_at == NOW + 100 and job.fire_window == "five_hour"
        assert store.claim_draft("j1") is True
        claimed = store.get("j1")
        assert claimed is not None and not claimed.draft and claimed.fire_at == 0
        assert store.claim_draft("j1") is False  # already consumed


def test_claim_draft_exactly_one_winner_across_connections() -> None:
    with Store() as store:
        store.create_draft("j2", "/tmp", "aim")
    first, second = Store(), Store()
    try:
        wins = [first.claim_draft("j2"), second.claim_draft("j2")]
    finally:
        first.close()
        second.close()
    assert wins.count(True) == 1


def test_armed_draft_summary_counts_only_armed_live_drafts() -> None:
    with Store() as store:
        assert store.armed_draft_summary() is None
        store.create_draft("a1", "/tmp", "one", fire_at=NOW + 500, fire_window="five_hour")
        store.create_draft("a2", "/tmp", "two", fire_at=NOW + 100, fire_window="five_hour")
        store.create_draft("a3", "/tmp", "unarmed")
        store.create_draft("a4", "/tmp", "gone", fire_at=NOW + 50, fire_window="five_hour")
        store.update_fields("a4", archived=True)
        assert store.armed_draft_summary() == (NOW + 100, 2)
        # An ATTACHED prompt (fire_at on a live non-draft row) counts too.
        store.ensure("a5", cwd="/tmp")
        store.update_fields("a5", prompt="attached", fire_at=NOW + 10, fire_window="five_hour")
        assert store.armed_draft_summary() == (NOW + 10, 3)


def test_claim_fire_is_one_shot() -> None:
    with Store() as store:
        store.ensure("f1", cwd="/tmp")
        store.update_fields("f1", prompt="p", fire_at=NOW + 10)
        assert store.claim_fire("f1") is True
        row = store.get("f1")
        assert row is not None and row.fire_at == 0
        assert store.claim_fire("f1") is False


# ---- new-job --at-reset ----------------------------------------------------------


def test_new_job_at_reset_arms_from_usage_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snapshot(five_hour=usage.Window(used_percentage=100.0, resets_at=NOW + 1800))
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    monkeypatch.setattr(time, "time", lambda: NOW)
    assert cli.main(["new-job", "-a", "armed job", "-c", "/tmp", "-R"]) == 0
    with Store() as store:
        job = next(s for s in store.list_sessions() if s.draft)
        assert job.fire_at == NOW + 1800 + park.DEFAULT_BUFFER_SEC
        assert job.fire_window == "five_hour"


def test_new_job_at_reset_fails_loudly_without_usable_reset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(usage, "read_usage", lambda label: None)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    assert cli.main(["new-job", "-a", "cannot arm", "-c", "/tmp", "-R"]) == 1
    assert "usable five_hour reset" in capsys.readouterr().err
    with Store() as store:
        assert not [s for s in store.list_sessions() if s.draft]  # nothing half-registered


# ---- start-job --auto ---------------------------------------------------------------


def test_start_job_auto_disarms_on_tripped_guard(capsys: pytest.CaptureFixture[str]) -> None:
    from datetime import date, timedelta

    from command_center import accounts

    ahead = (date.today() + timedelta(days=3)).isoformat()
    with Store() as store:
        store.create_draft(
            "g1", "/tmp", "guarded", start_date=ahead, fire_at=NOW - 300, fire_window="five_hour"
        )
    accounts.ensure_trusted("", "/tmp")  # past the tp#1512 trust gate: the date guard trips
    assert cli.main(["start-job", "g1", "--auto"]) == 1
    assert "start date" in capsys.readouterr().err
    with Store() as store:
        job = store.get("g1")
        assert job is not None and job.draft and job.fire_at == 0  # disarmed, draft kept


# ---- lifecycle: restore must not re-arm ----------------------------------------------


def test_restore_job_clears_stale_fire_at(monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import futuresync

    monkeypatch.setattr(futuresync, "unarchive_file", lambda *a, **k: None)
    with Store() as store:
        store.create_draft("r1", "/tmp", "restored", fire_at=NOW - 999, fire_window="five_hour")
        store.update_fields("r1", archived=True)
    assert cli.main(["restore-job", "r1"]) == 0
    with Store() as store:
        job = store.get("r1")
        assert job is not None and job.draft and not job.archived and job.fire_at == 0


# ---- daemon dispatch -------------------------------------------------------------------


def _daemon_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, bool]], list[str]]:
    launched: list[tuple[str, bool]] = []
    notified: list[str] = []
    from command_center import terminal

    def _fake_launch(sid: str, force: bool = False, auto: bool = False) -> bool:
        del force
        launched.append((sid, auto))
        return True

    monkeypatch.setattr(terminal, "start_job_in_new_tab", _fake_launch)
    monkeypatch.setattr(daemon, "notify", lambda title, msg, channels: notified.append(title))
    monkeypatch.setattr(usage, "read_usage", lambda label: None)  # no postpone data
    return launched, notified


def test_fire_reset_jobs_dispatches_due_with_rearm_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched, notified = _daemon_setup(monkeypatch)
    now = int(time.time())
    with Store() as store:
        store.create_draft(
            "d1", "/tmp", "due", fire_at=now - park.FIRE_GRACE_SEC - 5, fire_window="five_hour"
        )
        store.create_draft("d2", "/tmp", "graced", fire_at=now - 5, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=False
        )
        assert report.reset_fired == ["d1"]
        assert launched == [("d1", True)]
        assert notified == ["⏳ parked prompt fired"]
        fired = store.get("d1")
        # Re-arm-forward lease BEFORE dispatch: a crash/tab-that-never-ran retries later.
        assert fired is not None and fired.fire_at >= now + park.FIRE_RETRY_SEC - 5
        graced = store.get("d2")  # within the grace window — the foreground waiter's slot
        assert graced is not None and graced.fire_at == now - 5


def test_fire_reset_jobs_postpones_on_fresh_own_window_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched, _ = _daemon_setup(monkeypatch)
    now = int(time.time())
    snap = usage.Usage(
        captured_at=now,
        five_hour=usage.Window(used_percentage=100.0, resets_at=now + 4000),
        seven_day=None,
        oauth_fetched_at=now,
    )
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    with Store() as store:
        store.create_draft("p1", "/tmp", "held", fire_at=now - 500, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=False
        )
        assert report.reset_postponed == ["p1"] and not launched
        held = store.get("p1")
        assert held is not None and held.fire_at == now + 4000 + park.DEFAULT_BUFFER_SEC


def test_fire_reset_jobs_stored_fable_week_window_postpones_on_seven_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB row armed on the retired ``fable_week`` window is handled as ``seven_day``."""
    launched, _ = _daemon_setup(monkeypatch)
    messages: list[str] = []
    monkeypatch.setattr(daemon, "notify", lambda title, msg, channels: messages.append(msg))
    now = int(time.time())
    snap = usage.Usage(
        captured_at=now,
        five_hour=None,
        seven_day=usage.Window(used_percentage=100.0, resets_at=now + 4000),
        oauth_fetched_at=now,
    )
    monkeypatch.setattr(usage, "read_usage", lambda label: snap)
    with Store() as store:
        store.create_draft("f1", "/tmp", "legacy", fire_at=now - 500, fire_window="fable_week")
        legacy = store.get("f1")
        assert legacy is not None and legacy.fire_window == "fable_week"  # row loads as stored
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=False
        )
        assert report.reset_postponed == ["f1"] and not launched
        held = store.get("f1")
        assert held is not None and held.fire_at == now + 4000 + park.DEFAULT_BUFFER_SEC
    assert len(messages) == 1 and "seven_day still exhausted" in messages[0]


def test_fire_reset_jobs_dry_run_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    launched, _ = _daemon_setup(monkeypatch)
    now = int(time.time())
    with Store() as store:
        store.create_draft("d3", "/tmp", "dry", fire_at=now - 500, fire_window="five_hour")
        report = daemon.DaemonReport()
        daemon._fire_reset_jobs(  # pylint: disable=protected-access
            store, config.load_config(), report, dry_run=True
        )
        assert report.reset_fired == ["d3"] and not launched
        row = store.get("d3")
        assert row is not None and row.fire_at == now - 500
