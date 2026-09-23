"""Warm-vs-cold parity of the park/peek cores across the tp#70 seams.

The resident panel server (``ccc panel-server``) will drive the SAME code the cold
``ccc park -g`` / ``ccc peek`` processes drive today — only through the seams introduced
in S2: :class:`command_center.peek.Frontmost` (focus), the ``capture`` panel callable,
and the injected ``Store``/adapter. "Behaviour is byte-identical on both paths" (the
plan's D10 parity list) is only true if nothing else differs, so these tests run every
D10 branch through BOTH call shapes with the SAME fakes and compare the results:

* **cold** — the CLI path exactly as Karabiner invokes it (``cli.main(["park", "-g"])`` /
  ``peek.run(...)``), with the default AppleScript ``Frontmost`` monkeypatched at the
  module functions it dispatches through.
* **warm** — the shared core called the way the server will
  (``park.grab(opts, frontmost=…, capture=…)`` / ``peek.resolve_peek(frontmost=…)``).

Compared: every store mutation the run made, the panel's inputs (park: the header and
prefill the panel timer would show; peek: the whole :class:`~command_center.peek.
PanelInputs`), the notify feedback and the exit code. There is no server yet — the parity
is between the two call shapes of the shared core.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from command_center import cli, park, peek, usage
from command_center.adapters import ClaudeAdapter
from command_center.store import Store

NOW = 1_700_000_000
RESET_AT = NOW + 1200
ARMED_FIRE_AT = RESET_AT + park.DEFAULT_BUFFER_SEC


# --------------------------------------------------------------------------- #
# shared fakes (identical objects on both paths)
# --------------------------------------------------------------------------- #
@dataclass
class FakeFrontmost:
    """A :class:`peek.Frontmost` whose answers come from the case table, not from iTerm."""

    uuid_value: str | None = None
    cwd_value: str | None = None
    tty_value: str | None = None
    ccc_tui: bool = False
    block: threading.Event | None = None  # set → uuid() wedges (the resolution-timeout case)

    def uuid(self) -> str | None:
        if self.block is not None:
            self.block.wait(30)
        return self.uuid_value

    def cwd(self) -> str | None:
        return self.cwd_value

    def tty(self) -> str | None:
        return self.tty_value

    def is_ccc_tui(self) -> bool:
        return self.ccc_tui


def _install_frontmost(monkeypatch: pytest.MonkeyPatch, fake: FakeFrontmost) -> None:
    """Make the DEFAULT (cold) ``Frontmost`` answer out of *fake*.

    ``OsascriptFrontmost`` dispatches through the module-level functions rather than
    capturing them, so patching those is exactly "the cold path with the same fake" —
    and it proves the default implementation is a faithful adapter over them.
    """
    monkeypatch.setattr(peek, "frontmost_iterm_uuid", fake.uuid)
    monkeypatch.setattr(peek, "frontmost_iterm_cwd", fake.cwd)
    monkeypatch.setattr(peek, "_focused_tty", fake.tty)
    ccc_tty = fake.tty_value if fake.ccc_tui else None
    monkeypatch.setattr("command_center.jump.find_ccc_tty", lambda: ccc_tty)


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _transcript(home: Path, cwd: str, session_id: str, prompts: list[str]) -> Path:
    project = home / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(_user(p)) for p in prompts) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# park -g  (q+p): the D10 park branches
# --------------------------------------------------------------------------- #
_SNAPSHOT_FIELDS = (
    "draft",
    "cwd",
    "prompt",
    "aim",
    "fire_at",
    "fire_window",
    "config_dir",
    "iterm_session_id",
)


@dataclass(frozen=True)
class ParkCase:  # pylint: disable=too-many-instance-attributes
    """One D10 park branch, expressed once for both call shapes."""

    name: str
    seed: str  # store seeding: "" | "live" | "armed" | "dead"
    argv: list[str] = field(default_factory=list)
    opts: park.GrabOptions = park.GrabOptions()
    uuid: str | None = None
    cwd: str | None = None
    usable_reset: bool = True
    wedge: bool = False  # resolution never completes → UNARMED rescue save
    prompt: str | None = "the parked prompt\nsecond line"
    expect_rc: int = 0


PARK_CASES = [
    ParkCase(name="detached", seed="", cwd="/tmp"),
    ParkCase(name="attach", seed="live", uuid="UUID-LIVE"),
    ParkCase(name="retry-lease", seed="live", uuid="UUID-LIVE", usable_reset=False),
    ParkCase(name="unarmed", seed="", cwd="/tmp", usable_reset=False),
    ParkCase(name="timeout-unarmed", seed="", wedge=True, expect_rc=1),
    ParkCase(name="prefill", seed="armed", uuid="UUID-LIVE"),
    ParkCase(
        name="tracked-dead-session-account",
        seed="dead",
        uuid="UUID-DEAD",
        argv=[],
        opts=park.GrabOptions(),
    ),
    ParkCase(
        name="detached-new-job-flag",
        seed="live",
        uuid="UUID-LIVE",
        argv=["-j"],
        opts=park.GrabOptions(new_job=True),
    ),
    ParkCase(name="cancelled", seed="", cwd="/tmp", prompt=None, expect_rc=130),
]


@dataclass
class ParkRun:
    """What one park run did — the comparable surface of a warm/cold invocation."""

    rc: int
    store: list[tuple]
    panel: tuple[str, str, tuple[str, str] | None]  # (header, initial, poll result)
    notified: list[tuple[str, str]]
    spawned: list[list[str]]


def _norm(value: object, home: Path) -> object:
    """Blank out the per-run home so the two runs' absolute paths compare equal."""
    if not isinstance(value, str):
        return value
    for variant in (str(home), str(home.resolve())):  # replacing twice is idempotent
        value = value.replace(variant, "<HOME>")
    return value


def _snapshot_store(seeded: set[str], home: Path) -> list[tuple]:
    with Store() as store:
        rows = store.list_sessions(include_archived=True)
    out = [
        tuple(
            [session.session_id if session.session_id in seeded else "<generated>"]
            + [_norm(getattr(session, name), home) for name in _SNAPSHOT_FIELDS]
        )
        for session in rows
    ]
    return sorted(out, key=repr)


def _seed_store(case: ParkCase, other_account: Path) -> set[str]:
    """Pre-existing rows for *case*; returns the ids that are NOT run-generated."""
    if not case.seed:
        return set()
    with Store() as store:
        store.ensure("live-1", cwd="/tmp")
        if case.seed == "dead":
            store.update_fields(
                "live-1",
                iterm_session_id="w0t0p0:UUID-DEAD",
                aim="the aim",
                config_dir=str(other_account),
            )
        else:
            store.update_fields("live-1", iterm_session_id="w0t0p0:UUID-LIVE", aim="the aim")
        if case.seed == "armed":  # a prompt is already armed → the panel reopens it
            store.update_fields(
                "live-1",
                prompt="old armed prompt",
                fire_at=NOW + 600,
                fire_window="five_hour",
            )
    return {"live-1"}


def _drain(poll: Any) -> tuple[str, str] | None:
    """Drive the deferred-resolution poll like the panel's 100 ms timer would."""
    for _ in range(500):
        result = poll()
        if result is not None:
            return result
        time.sleep(0.01)
    raise AssertionError("target resolution never became ready")


def _run_park(  # noqa: PLR0915  # one linear harness; splitting it hides the warm/cold symmetry
    case: ParkCase,
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unblock: threading.Event,
) -> ParkRun:
    """Run *case* through the cold CLI or the warm core, and report what it did."""
    # Same basename on both runs so the derived account LABEL (and thus the panel header)
    # is identical; the differing parent is normalised out of the store snapshot.
    home = tmp_path / mode / "home"
    other_account = tmp_path / "acct-other"
    other_account.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    seeded = _seed_store(case, other_account)

    snapshot = (
        usage.Usage(
            captured_at=NOW,
            five_hour=usage.Window(used_percentage=100.0, resets_at=RESET_AT),
            seven_day=None,
            oauth_fetched_at=NOW,
        )
        if case.usable_reset
        else None
    )
    monkeypatch.setattr(usage, "read_usage", lambda label: snapshot)
    monkeypatch.setattr(usage, "fetch_claude_usage", lambda label, now=None: None)
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(park, "RESOLVE_JOIN_SEC", 0.05)
    # The detached job's id is a fresh uuid4 and leaks into the notify text; pin it so the
    # two runs' user-visible feedback is comparable (each run has its own store anyway).
    monkeypatch.setattr(
        uuid_mod, "uuid4", lambda: uuid_mod.UUID("11111111-2222-4333-8444-555555555555")
    )

    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "command_center.notify.notify",
        lambda title, msg, channels: notified.append((title, msg)),
    )
    spawned: list[list[str]] = []
    monkeypatch.setattr("command_center.spawn.spawn_ccc", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(
        "command_center.terminal.start_job_in_new_tab", lambda session_id, auto=True: True
    )

    # The attach decision asks the adapter which sessions are LIVE: only the "live"/"armed"
    # seeds are attach-eligible; "dead" is tracked but not running (the D9/V8 account case).
    live = (
        [SimpleNamespace(session_id="live-1", alive=True, kind="interactive")]
        if case.seed in ("live", "armed")
        else []
    )
    monkeypatch.setattr(
        "command_center.adapters.claude.ClaudeAdapter",
        lambda *a, **k: SimpleNamespace(discover=lambda: live),
    )

    fake = FakeFrontmost(
        uuid_value=case.uuid,
        cwd_value=case.cwd,
        block=unblock if case.wedge else None,
    )
    _install_frontmost(monkeypatch, fake)

    seen: list[tuple[str, str, tuple[str, str] | None]] = []

    def _capture(header: str, initial: str = "", poll: Any = None) -> str | None:
        # The panel's inputs: the placeholder header, the prefill text, and what the
        # poll timer would have applied once resolution landed (skipped when wedged —
        # that is exactly the case where the timer never gets an answer).
        seen.append((header, initial, None if case.wedge else _drain(poll)))
        return case.prompt

    if mode == "cold":
        monkeypatch.setattr("command_center.parkpanel.capture_prompt", _capture)
        rc = cli.main(["park", "-g", *case.argv])
    else:
        rc = park.grab(case.opts, frontmost=fake, capture=_capture)

    return ParkRun(
        rc=rc,
        store=_snapshot_store(seeded, home),
        panel=seen[0],
        notified=notified,
        spawned=spawned,
    )


@pytest.mark.parametrize("case", PARK_CASES, ids=lambda c: c.name)
def test_park_grab_is_identical_warm_and_cold(
    case: ParkCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unblock = threading.Event()
    try:
        cold = _run_park(case, "cold", tmp_path, monkeypatch, unblock)
        warm = _run_park(case, "warm", tmp_path, monkeypatch, unblock)
    finally:
        unblock.set()  # release the wedged resolution thread

    assert cold.rc == case.expect_rc
    assert cold == warm


def test_park_case_table_covers_the_d10_branches() -> None:
    """The plan's S2 park list, spelled out so a dropped case fails loudly."""
    assert {c.name for c in PARK_CASES} >= {
        "attach",
        "detached",
        "retry-lease",
        "unarmed",
        "timeout-unarmed",
        "prefill",
        "tracked-dead-session-account",
    }


def test_park_cases_do_what_their_names_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity alone would also hold if BOTH paths were broken — pin the outcomes too."""
    by_name = {case.name: case for case in PARK_CASES}
    unblock = threading.Event()

    def run(name: str) -> ParkRun:
        return _run_park(by_name[name], name, tmp_path, monkeypatch, unblock)

    try:
        detached = run("detached")
        assert detached.store == [
            ("<generated>", 1, "/tmp", "the parked prompt\nsecond line", "the parked prompt",
             ARMED_FIRE_AT, "five_hour", "<HOME>", None)
        ]  # fmt: skip
        assert [title for title, _msg in detached.notified] == ["⏳ prompt parked"]
        assert "fires" in detached.notified[0][1]

        attach = run("attach")
        row = next(r for r in attach.store if r[0] == "live-1")
        assert row[1] == 0 and row[3] == "the parked prompt\nsecond line"  # not a draft
        assert (row[5], row[6]) == (ARMED_FIRE_AT, "five_hour")
        assert len(attach.store) == 1  # attach parks ON the session — no detached job

        lease = run("retry-lease")
        row = next(r for r in lease.store if r[0] == "live-1")
        assert (row[5], row[6]) == (NOW + park.FIRE_RETRY_SEC, "")  # lease, no provenance

        unarmed = run("unarmed")
        assert unarmed.store[0][5] == 0 and unarmed.store[0][6] == ""  # saved, never armed

        timeout = run("timeout-unarmed")
        assert timeout.rc == 1 and timeout.store[0][5] == 0
        assert timeout.notified[0][0] == "⏳ park saved unarmed"

        prefill = run("prefill")
        assert prefill.panel[2] is not None and prefill.panel[2][1] == "old armed prompt"

        dead = run("tracked-dead-session-account")
        job = next(r for r in dead.store if r[0] == "<generated>")
        # V8/D9: a tracked-but-not-live tab still bills ITS account, not the ambient one.
        assert job[7] == str(tmp_path / "acct-other")
        assert next(r for r in dead.store if r[0] == "live-1")[5] == 0  # untouched
    finally:
        unblock.set()


# --------------------------------------------------------------------------- #
# peek (s+p): the D10 peek branches
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PeekCase:
    """One D10 peek branch."""

    name: str
    uuid: str | None = None
    cwd: str | None = None
    ccc_tui: bool = False
    selected: str | None = None
    expect_session: str | None = None
    expect_label_has: str = ""


PEEK_CASES = [
    PeekCase(
        name="tui-selected",
        uuid="UUID-TUI",
        ccc_tui=True,
        selected="parked",
        expect_session="parked",
    ),
    PeekCase(name="uuid-match", uuid="UUID-A", expect_session="tracked"),
    PeekCase(
        name="cwd-fallback",
        uuid="NO-MATCH",
        cwd="/Users/x/untracked",
        expect_session="loose",
    ),
    PeekCase(name="no-focus", uuid=None, expect_label_has="no focused"),
    PeekCase(
        name="no-session",
        uuid="NO-MATCH",
        cwd="/Users/x/nothing-here",
        expect_label_has="no Claude session tracked",
    ),
]


def _seed_peek_home(home: Path) -> None:
    """One tracked tab, one parked row, one untracked transcript — every branch's target."""
    with Store() as store:
        store.ensure("tracked", cwd="/Users/x/repo")
        store.update_fields("tracked", iterm_session_id="w0t1p0:UUID-A", last_response_at=10)
        store.set_aim("tracked", "the tracked aim")
        store.ensure("parked", cwd="/Users/x/other")  # no live tab: reachable only via the TUI
        store.set_aim("parked", "the parked aim")
        store.ensure("stale", cwd="/Users/x/repo")  # stale uuid on the tab the TUI now owns
        store.update_fields("stale", iterm_session_id="w0t1p0:UUID-TUI", last_response_at=5)
    _transcript(home, "/Users/x/repo", "tracked", ["tracked ask one", "tracked ask two"])
    _transcript(home, "/Users/x/other", "parked", ["parked ask"])
    _transcript(home, "/Users/x/repo", "stale", ["stale ask"])
    _transcript(home, "/Users/x/untracked", "loose", ["untracked ask"])


def _run_peek(
    case: PeekCase, mode: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> peek.PanelInputs:
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    fake = FakeFrontmost(
        uuid_value=case.uuid,
        cwd_value=case.cwd,
        tty_value="/dev/ttys007",
        ccc_tui=case.ccc_tui,
    )
    _install_frontmost(monkeypatch, fake)
    monkeypatch.setattr("command_center.jumpstate.get_selected", lambda: case.selected)

    if mode == "warm":
        return peek.panel_inputs(peek.resolve_peek(frontmost=fake))

    monkeypatch.setattr(peek.sys, "platform", "darwin")
    monkeypatch.setattr("command_center.parkpanel.warm_appkit", lambda: None)
    shown: list[peek.PanelInputs] = []
    monkeypatch.setattr(peek, "show_panel_for", lambda inputs, timeout=0.0: shown.append(inputs))
    assert peek.run(argparse.Namespace(session=None, print_only=False, timeout=0.0)) == 0
    return shown[0]


@pytest.mark.parametrize("case", PEEK_CASES, ids=lambda c: c.name)
def test_peek_panel_is_identical_warm_and_cold(
    case: PeekCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    _seed_peek_home(home)

    cold = _run_peek(case, "cold", home, monkeypatch)
    warm = _run_peek(case, "warm", home, monkeypatch)
    assert cold == warm

    if case.expect_session is not None:
        assert cold.session_id == case.expect_session
    if case.expect_label_has:
        assert case.expect_label_has in cold.prompts_text
        assert cold.subtitle == "ccc peek"  # unresolved → the tool's own name
        assert cold.prompts_segments is None and cold.session_segments is None


def test_peek_case_table_covers_the_d10_branches() -> None:
    """The plan's S2 peek list, spelled out so a dropped case fails loudly."""
    assert {c.name for c in PEEK_CASES} == {
        "tui-selected",
        "uuid-match",
        "cwd-fallback",
        "no-focus",
        "no-session",
    }


def test_peek_tui_selection_still_outranks_a_stale_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam must not reorder resolution: the ccc tab's own stale uuid stays shadowed."""
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    _seed_peek_home(home)
    case = next(c for c in PEEK_CASES if c.name == "tui-selected")

    on_tui = _run_peek(case, "warm", home, monkeypatch)
    assert on_tui.session_id == "parked"  # the selected row, not "stale"

    # Same tab, but the focus is NOT the TUI any more → the uuid map wins again.
    off_tui = _run_peek(
        PeekCase(name="off-tui", uuid="UUID-TUI", ccc_tui=False, selected="parked"),
        "warm",
        home,
        monkeypatch,
    )
    assert off_tui.session_id == "stale"


def test_peek_session_id_shortcut_never_probes_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--session`` (the TUI ``sp`` chord) bypasses the seam entirely, warm or cold."""
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    _seed_peek_home(home)

    def _boom() -> str:
        raise AssertionError("focus detection must not run when a session id is given")

    fake = FakeFrontmost()
    monkeypatch.setattr(fake, "uuid", _boom)
    monkeypatch.setattr(fake, "is_ccc_tui", _boom)
    data = peek.resolve_peek(
        adapter=ClaudeAdapter(), store=Store(), session_id="parked", frontmost=fake
    )
    assert data.resolved and data.session_id == "parked"
