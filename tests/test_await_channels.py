"""Live-tab delivery channels: the AppleScript fallback of ``terminal.send_text_via``,
the poller's rate-limited channel-health record, and where it is shown (TUI rule +
detail pane, ``ccc ls``, ``ccc doctor``). No real iTerm: every rung is a fake."""

# pylint: disable=unused-argument,protected-access  # `home` activates the fixture
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from rich.cells import cell_len
from rich.text import Text

from command_center import await_eval, await_view, doctor, terminal
from command_center.adapters.claude import ClaudeAdapter
from command_center.await_store import SourceSpec
from command_center.store import Store
from command_center.views import ls as ls_view

SID = "cafe0000-1111-2222-3333-444455556666"
TAB = "w0t1p0:ABCD-UUID"
TRICKY = 'say "hi" \\ back\nline two — ünïcødé ✅ $(rm -rf /) `x`'


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("CCC_HOME", str(tmp_path / "ccc"))
    return tmp_path


# --------------------------------------------------------------------------- fallback
@pytest.fixture(name="rungs")
def rungs_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Grant pre-check passes; the Python-API connect is refused; osascript is recorded."""
    calls: dict[str, Any] = {"argv": [], "rc": 0, "stdout": "ok\n", "api_connects": 0}
    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: False)
    monkeypatch.setattr(terminal, "_iterm_reachable_by_apple_event", lambda timeout=5: True)
    monkeypatch.setattr(terminal.shutil, "which", lambda name: f"/usr/bin/{name}")

    import iterm2

    async def refused() -> Any:
        calls["api_connects"] += 1
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(iterm2.Connection, "async_create", staticmethod(refused))

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["argv"].append(argv)
        calls["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, calls["rc"], calls["stdout"], "")

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    return calls


def test_refused_python_api_falls_back_to_applescript(rungs: dict[str, Any]) -> None:
    assert terminal.send_text_via(TAB, TRICKY) == "applescript"
    assert terminal.send_text_to_session(TAB, TRICKY) is True
    assert rungs["api_connects"] == 2  # the Python API is always tried first
    argv = rungs["argv"][0]
    assert argv[:2] == ["osascript", "-e"]
    script = argv[2]
    # The text travels as an argv ARGUMENT, never inside the script — flattened to ONE
    # line (iTerm's `write text` drops ESC, so no bracketed paste can protect a newline).
    assert argv[3:] == ["ABCD-UUID", terminal._one_line(TRICKY)]
    assert "\n" not in argv[4] and "\r" not in argv[4]
    assert "ün" not in script and "hi" not in script and "on run argv" in script
    assert "[200~" not in script and "character id 27" not in script and "newline NO" in script
    assert "delay 0.4" in script and "character id 13" in script
    assert rungs["timeout"] == 10


def test_both_channels_failing_is_false(rungs: dict[str, Any]) -> None:
    rungs["rc"] = 1
    rungs["stdout"] = ""
    assert terminal.send_text_via(TAB, "hello") == ""
    assert terminal.send_text_to_session(TAB, "hello") is False


def test_applescript_that_does_not_find_the_session_is_false(rungs: dict[str, Any]) -> None:
    rungs["stdout"] = "\n"  # exit 0, but the session UUID was not in any window
    assert terminal.send_text_to_session(TAB, "hello") is False


def test_python_api_success_does_not_touch_applescript(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal, "_send_text_python_api", lambda uuid, text: terminal._API_SENT)
    assert terminal.send_text_via(TAB, "hello") == "python-api"
    assert not rungs["argv"]


def test_a_half_sent_python_api_paste_never_falls_back(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal, "_send_text_python_api", lambda uuid, text: terminal._API_PARTIAL)
    assert terminal.send_text_via(TAB, "hello") == ""
    assert not rungs["argv"]  # the text must not arrive twice


def test_no_automation_grant_sends_nothing(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal, "_iterm_reachable_by_apple_event", lambda timeout=5: False)
    assert terminal.send_text_via(TAB, "hello") == ""
    assert rungs["api_connects"] == 0 and not rungs["argv"]


# --------------------------------------------------------------------------- probe
_REAL_RUN = subprocess.run


def _child(monkeypatch: pytest.MonkeyPatch, rungs: dict[str, Any], code: str) -> None:
    """The probe child runs *code* for real; every other ``subprocess.run`` stays faked."""
    fake_run = terminal.subprocess.run

    def run(argv: list[str], **kwargs: Any) -> Any:
        if argv[:2] == [sys.executable, "-c"] and "probe child" in argv[2]:
            rungs["child_timeout"] = kwargs.get("timeout")
            return _REAL_RUN(argv, **kwargs)  # pylint: disable=subprocess-run-check
        return fake_run(argv, **kwargs)  # pylint: disable=subprocess-run-check

    monkeypatch.setattr(terminal.subprocess, "run", run)
    monkeypatch.setattr(
        terminal, "_python_api_probe_argv", lambda: [sys.executable, "-c", f"# probe child\n{code}"]
    )


@pytest.mark.real_channel_probe
def test_a_hung_cookie_request_is_killed_within_the_bound(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(monkeypatch, rungs, "import time; time.sleep(30)")
    started = time.monotonic()
    health = terminal.delivery_channel_health()
    assert time.monotonic() - started < 10
    assert health["python_api"] is False and health["applescript"] is True
    assert health["python_api_error"] == terminal.PYTHON_API_COOKIE_PENDING
    assert "cookie request pending" in health["python_api_error"]
    assert "no answer within 4 s" in health["python_api_error"]
    assert rungs["child_timeout"] == terminal.CHANNEL_PROBE_TIMEOUT + 1
    assert not rungs["argv"]  # the probe never runs the typing script


@pytest.mark.real_channel_probe
def test_a_child_that_connected_reports_the_api_up(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(monkeypatch, rungs, "raise SystemExit(0)")
    health = terminal.delivery_channel_health()
    assert health["python_api"] is True and health["python_api_error"] == ""


@pytest.mark.real_channel_probe
def test_the_child_reason_becomes_the_error(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    code = f"print('noise'); print({terminal.PYTHON_API_DISABLED!r}); raise SystemExit(1)"
    _child(monkeypatch, rungs, code)
    health = terminal.delivery_channel_health()
    assert health["python_api"] is False
    assert health["python_api_error"] == terminal.PYTHON_API_DISABLED
    _child(monkeypatch, rungs, "import sys; sys.stderr.write('boom\\n'); raise SystemExit(3)")
    assert terminal.delivery_channel_health()["python_api_error"] == "the probe exited 3: boom"


@pytest.mark.real_channel_probe
@pytest.mark.parametrize("tcc_free", [True, False])
def test_the_version_query_runs_once_per_health_check(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tcc_free: bool
) -> None:
    asked: list[float] = []

    def reachable(timeout: float = 5) -> bool:
        asked.append(timeout)
        return False

    monkeypatch.setattr(terminal, "_iterm_api_auth_is_tcc_free", lambda: tcc_free)
    monkeypatch.setattr(terminal, "_iterm_reachable_by_apple_event", reachable)
    _child(monkeypatch, rungs, "raise SystemExit(0)")
    health = terminal.delivery_channel_health()
    assert len(asked) == 1 and health["applescript"] is False
    # No grant and no TCC-free switch: the child is never started.
    assert health["python_api"] is tcc_free
    assert ("child_timeout" in rungs) is tcc_free


def test_the_real_child_command_imports_this_package() -> None:
    argv = terminal._python_api_probe_argv()
    assert argv[:2] == [sys.executable, "-c"]
    code = argv[2].replace("t.python_api_probe_child()", "0 if t.CHANNEL_PROBE_TIMEOUT else 1")
    assert code != argv[2]
    result = _REAL_RUN(
        [argv[0], "-c", code], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.real_channel_probe
def test_the_child_entry_reports_a_refused_api(
    rungs: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert terminal.python_api_probe_child() == 1
    assert capsys.readouterr().out.strip() == terminal.PYTHON_API_DISABLED
    assert terminal._python_api_connect() == terminal.PYTHON_API_DISABLED
    assert not rungs["argv"]  # the probe never runs the typing script


@pytest.mark.real_channel_probe
def test_the_child_entry_closes_a_working_connection(
    rungs: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import iterm2

    closed: list[bool] = []

    class _Socket:
        async def close(self) -> None:
            closed.append(True)

    class _Conn:
        websocket = _Socket()

    async def ok() -> Any:
        return _Conn()

    monkeypatch.setattr(iterm2.Connection, "async_create", staticmethod(ok))
    assert terminal.python_api_probe_child() == 0
    assert capsys.readouterr().out == ""
    assert closed == [True]


# --------------------------------------------------------------------------- record
def _arm(store: Store) -> int:
    now = int(time.time())
    store.ensure(SID, cwd="/tmp")
    return store.arm_await(
        SID,
        config_dir="",
        cwd="/tmp",
        no_codex=False,
        prompt_template="got {event}",
        until_epoch=now + 3600,
        sources=[SourceSpec(kind="cmd", spec={"schema_version": 1, "cmd": "true"})],
        now=now,
        purpose="waiting for the vendor",
    )


def test_channel_health_is_recorded_at_most_every_five_minutes(home: Path) -> None:
    probes: list[int] = []

    def probe() -> dict[str, object]:
        probes.append(1)
        return {"python_api": False, "applescript": True, "python_api_error": "refused"}

    with Store() as store:
        assert store.channel_health() is None
        first = await_eval.record_channel_health(store, 1_000, probe)
        assert first is not None and first["checked_at"] == 1_000
        assert await_eval.record_channel_health(store, 1_000 + 299, probe) is None
        assert len(probes) == 1
        again = await_eval.record_channel_health(store, 1_000 + 300, probe)
        assert again is not None and len(probes) == 2
        assert store.channel_health() == {
            "python_api": False,
            "applescript": True,
            "checked_at": 1_300,
            "python_api_error": "refused",
        }


def test_run_pass_records_the_channels_it_can_deliver_through(home: Path) -> None:
    def probe() -> dict[str, object]:
        return {"python_api": True, "applescript": True, "python_api_error": ""}

    with Store() as store:
        _arm(store)
        now = int(time.time())
        report = await_eval.run_pass(
            store,
            now=now,
            runner=lambda *a, **k: None,  # type: ignore[arg-type]
            delivery=lambda *a, **k: None,
            channel_probe=probe,
            deadline_sec=0.0,
        )
        assert report.channels["python_api"] is True
        assert store.channel_health() is not None
        dry = await_eval.run_pass(store, now=now + 600, dry_run=True, channel_probe=probe)
        assert not dry.channels  # a dry run measures nothing


# --------------------------------------------------------------------------- render
HEALTH_UP = {"python_api": True, "applescript": True, "checked_at": 0, "python_api_error": ""}


def _health(python_api: bool, applescript: bool, at: int) -> dict[str, Any]:
    return {
        "python_api": python_api,
        "applescript": applescript,
        "checked_at": at,
        "python_api_error": "" if python_api else terminal.PYTHON_API_DISABLED,
    }


def test_channel_text_marks_each_channel() -> None:
    now = time.time()
    at = int(now) - 60
    clock = time.strftime("%H:%M", time.localtime(at))
    assert await_view.channel_health_text(None) == "Python API: ? · AppleScript: ?"
    assert (
        await_view.channel_health_text(_health(True, True, at), now)
        == f"Python API: ✅ · AppleScript: ✅ (checked {clock})"
    )
    assert await_view.channel_health_text(_health(False, True, at), now).startswith(
        "Python API: ❌ · AppleScript: ✅"
    )
    detail = await_view.channel_detail_text(_health(False, True, at), now)
    assert detail.endswith("— Python API: " + terminal.PYTHON_API_DISABLED)
    assert "Enable Python API" in detail
    assert "—" not in await_view.channel_detail_text(_health(True, False, at), now)


def test_detail_lines_carry_the_delivery_channels(home: Path) -> None:
    with Store() as store:
        _arm(store)
        [entry] = await_view.visible_awaits(store)
    fields = dict(await_view.detail_lines(entry, "/nowhere"))
    assert fields["Delivery channels"] == "Python API: ? · AppleScript: ?"
    fields = dict(await_view.detail_lines(entry, "/nowhere", _health(False, True, 1)))
    assert fields["Delivery channels"].startswith("Python API: ❌ · AppleScript: ✅ (checked ")
    assert "Enable Python API" in fields["Delivery channels"]


def test_ls_awaiting_header_shows_the_recorded_channels(home: Path) -> None:
    with Store() as store:
        _arm(store)
        store.put_channel_health(_health(False, True, int(time.time())))
        out = ls_view.render(store, ClaudeAdapter())
    header = out[out.index("AWAITING") :].splitlines()[0]
    assert header.startswith(f"AWAITING  {await_view.AWAITING_HINT}  Python API: ❌ · ")
    assert "AppleScript: ✅ (checked " in header


def test_rule_slices_by_display_cells() -> None:
    from command_center.views.tui import _awaiting_rule_text, _slice_cells

    rule = _awaiting_rule_text(_health(True, False, 0))
    assert rule.endswith("Python API: ✅ · AppleScript: ❌")
    widths = [3, 7, 1, 40, 12]
    pieces = _slice_cells("── " + rule, widths)
    assert [cell_len(p) for p in pieces] == widths  # ✅ is two cells: no column overflows
    assert "".join(pieces).replace(" ", "").startswith("──AWAITING")


def test_tui_rule_and_detail_show_the_channels(home: Path) -> None:
    from command_center.views.tui import CommandCenterApp, SessionTable

    with Store() as store:
        _arm(store)
        store.put_channel_health(_health(False, True, int(time.time())))

    def plain(cell: object) -> str:
        return cell.plain if isinstance(cell, Text) else str(cell)

    seen: dict[str, str] = {}

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test(size=(220, 50)) as pilot:
            while any(not w.is_finished for w in app.workers):
                await pilot.pause()
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            rows = ["".join(plain(c) for c in table.get_row_at(i)) for i in range(table.row_count)]
            header = next(i for i, r in enumerate(rows) if "AWAITING" in r)
            seen["rule"] = rows[header]
            table.move_cursor(row=header + 1)
            await pilot.pause()
            seen["detail"] = str(app.query_one("#detail-fields-view").render())

    asyncio.run(scenario())
    assert "Python API: ❌ · AppleScript: ✅ (checked " in seen["rule"], seen["rule"]
    assert "Delivery channels: Python API: ❌ · AppleScript: ✅" in seen["detail"]
    assert "Enable Python API" in seen["detail"]


# --------------------------------------------------------------------------- doctor
@pytest.mark.parametrize(
    ("python_api", "applescript", "status"),
    [(True, True, doctor.OK), (False, True, doctor.WARN), (False, False, doctor.FAIL)],
)
def test_doctor_verdicts(home: Path, python_api: bool, applescript: bool, status: str) -> None:
    with Store() as store:
        store.put_channel_health(_health(python_api, applescript, int(time.time())))
    check = doctor._delivery_channels_check()
    assert check.status == status
    assert "[poller record]" in check.detail


def test_doctor_probes_once_when_the_record_is_stale(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes: list[int] = []

    def probe() -> dict[str, Any]:
        probes.append(1)
        return _health(True, True, int(time.time()))

    monkeypatch.setattr(terminal, "delivery_channel_health", probe)
    with Store() as store:
        store.put_channel_health(_health(False, False, int(time.time()) - 2 * 3600))
    check = doctor._delivery_channels_check()
    assert check.status == doctor.OK and "[probed by doctor]" in check.detail
    assert probes == [1]


def test_one_line_turns_every_line_break_into_one_space() -> None:
    assert terminal._one_line("a\nb") == "a b"
    assert terminal._one_line("a \r\n\n  b\rc") == "a b c"
    assert (
        terminal._one_line("  keep 'quotes' \\ $(x) `y` ünï  ") == "keep 'quotes' \\ $(x) `y` ünï"
    )
