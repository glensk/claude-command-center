"""Delivering a fired await group: preflight, the live-session matrix, the outbox,
``ccc fire-await`` and ``accounts.pin_environ``.

Every launcher, paster and ``os.execvp`` is a recording fake — no tab opens, nothing is
typed into a real terminal, no ``claude`` runs. The ``test_paste_guard_await_*`` tests run
the real paste guard (:func:`bridge_target.guarded_paste`) against a
:class:`bridgestub.World` (tp#1867).
"""

# pylint: disable=unbalanced-tuple-unpacking  # `[(x, _)] = …` asserts exactly one
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from bridgestub import SID as WORLD_SID
from bridgestub import TAB, World, fixture_screen, turn_end, user

from command_center import accounts, await_delivery, cli
from command_center.await_eval import PassReport
from command_center.await_prompt import FRAMING
from command_center.await_store import SourceSpec
from command_center.bridge_target import PasteResult, guarded_paste
from command_center.models import LiveSession
from command_center.store import Store
from command_center.transfer_store import Transfer

NOW = 1_800_000_000
SID = "11111111-2222-3333-4444-555555555555"


class Notes:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, _title: str, message: str) -> None:
        self.messages.append(message)


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path) -> dict[str, Any]:
    """A trusted cwd and a transcript under a (non-default) account dir."""
    acct = tmp_path / "acct"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (acct / "projects" / str(cwd).replace("/", "-")).mkdir(parents=True)
    (acct / "projects" / str(cwd).replace("/", "-") / f"{SID}.jsonl").write_text("{}\n")
    (acct / ".claude.json").write_text(
        json.dumps({"projects": {str(cwd.resolve()): {"hasTrustDialogAccepted": True}}})
    )
    store = Store(tmp_path / "state.db")
    store.ensure(SID, cwd=str(cwd))
    store.update_fields(SID, config_dir=str(acct), iterm_session_id="w0t0p0:ABC")
    return {"store": store, "acct": str(acct), "cwd": str(cwd), "tmp": tmp_path, "sid": SID}


def _fired(env: dict[str, Any], *, no_codex: bool = False, fresh: bool = False) -> int:
    gid = _armed(env, no_codex=no_codex, fresh=fresh)
    _fire(env, gid)
    return gid


def _armed(env: dict[str, Any], *, no_codex: bool = False, fresh: bool = False) -> int:
    store: Store = env["store"]
    return store.arm_await(
        env["sid"],
        config_dir=env["acct"],
        cwd=env["cwd"],
        no_codex=no_codex,
        prompt_template="Reply arrived: {event}. Continue.",
        until_epoch=NOW + 3600,
        sources=[SourceSpec(kind="zoho-reply", spec={}, watermark="w")],
        now=NOW,
        purpose="Waiting for the requester to confirm the quota.",
        items=["zoho#209", "tp#12"],
        fresh=fresh,
    )


def _fire(env: dict[str, Any], gid: int) -> None:
    store: Store = env["store"]
    [(src, _grp)] = store.lease_due_sources(NOW + 120)
    token = store.fire_group(
        gid,
        src.id,
        src.lease_token,
        event_id="zoho:1:2",
        payload='{"source":"zoho-reply","snippet":"ok"}',
        remote_epoch=NOW,
        watermark="w2",
        now=NOW,
    )
    assert token


def _live(env: dict[str, Any], **over: Any) -> LiveSession:
    base: dict[str, Any] = {
        "pid": 42,
        "session_id": SID,
        "cwd": env["cwd"],
        "alive": True,
        "raw_status": "idle",
        "config_dir": env["acct"],
    }
    base.update(over)
    return LiveSession(**base)


def _fake_paster(
    env: dict[str, Any], typed: list[Any], typer_ok: bool = True
) -> await_delivery.Paster:
    """The paste guard's contract without its checks: the stored tab (``no_tab`` when
    there is none), the claim, then a typer that succeeds (``sent``) or not (``none``)."""

    def paster(live: LiveSession, text: str, claim: Any) -> PasteResult:
        row = env["store"].get(live.session_id)
        tab = (row.iterm_session_id or "") if row is not None else ""
        if not tab:
            return PasteResult(refusal="no_tab")
        if not claim():
            return PasteResult(refusal="claim_lost", tab=tab)
        typed.append((tab, text))
        return PasteResult(state="sent" if typer_ok else "none", tab=tab)

    return paster


def _deliver(
    env: dict[str, Any],
    *,
    live: list[LiveSession] | None = None,
    typer_ok: bool = True,
    launcher_ok: bool = True,
    now: int = NOW + 200,
    paster: await_delivery.Paster | None = None,
) -> tuple[PassReport, list[Any], list[Any], Notes]:
    typed: list[Any] = []
    launched: list[Any] = []
    notes = Notes()
    report = PassReport()

    def launcher(group_id: int, token: str) -> bool:
        launched.append((group_id, token))
        return launcher_ok

    await_delivery.deliver_pending(
        env["store"],
        now=now,
        report=report,
        notifier=notes,
        discover=lambda: list(live or []),
        paster=paster or _fake_paster(env, typed, typer_ok),
        launcher=launcher,
    )
    return report, typed, launched, notes


def _state(env: dict[str, Any], gid: int) -> str:
    group = env["store"].get_await_group(gid)
    assert group is not None
    return str(group.state)


# --------------------------------------------------------------------------- closed session
def test_closed_session_opens_a_fire_await_tab(env: dict[str, Any]) -> None:
    gid = _fired(env)
    report, typed, launched, notes = _deliver(env)
    token = env["store"].get_await_group(gid).delivery_token
    assert launched == [(gid, token)] and not typed
    assert report.launched == [gid] and _state(env, gid) == "delivering"
    assert len(notes.messages) == 1 and "ok" not in notes.messages[0]  # content-free


def test_launcher_false_retries_then_blocks(env: dict[str, Any]) -> None:
    gid = _fired(env)
    for _ in range(await_delivery.MAX_ATTEMPTS - 1):
        _deliver(env, launcher_ok=False)
        assert _state(env, gid) == "fired"
    report, _t, _l, notes = _deliver(env, launcher_ok=False)
    assert _state(env, gid) == "blocked" and report.blocked_groups == [gid]
    assert any("blocked" in m for m in notes.messages)
    # -R recovers it with the payload intact.
    assert env["store"].retry_group(gid, NOW + 300) == "fired"
    assert env["store"].get_await_group(gid).event_payload.startswith('{"source"')


# --------------------------------------------------------------------------- handoff reservation
NEW = "99999999-8888-7777-6666-555555555555"


def _handoff(env: dict[str, Any]) -> str:
    """A ``prepared`` handoff of SID to NEW (same account and cwd; NEW has its transcript)."""
    store: Store = env["store"]
    projects = Path(env["acct"]) / "projects" / env["cwd"].replace("/", "-")
    (projects / f"{NEW}.jsonl").write_text("{}\n")
    store.ensure(NEW, cwd=env["cwd"])
    store.update_fields(NEW, config_dir=env["acct"])
    transfer = store.create_transfer(
        Transfer(
            transfer_id="t1",
            source_session=SID,
            target_session=NEW,
            target_config_dir=env["acct"],
            cwd=env["cwd"],
        ),
        NOW * 1000,
    )
    return transfer.transfer_id


def test_a_group_firing_while_reserved_is_deferred_then_delivered_to_the_successor(
    env: dict[str, Any],
) -> None:
    store: Store = env["store"]
    gid = _armed(env)
    tid = _handoff(env)
    _fire(env, gid)  # the wake arrives mid-handoff
    report, typed, launched, _notes = _deliver(env)
    assert not typed and not launched and report.waiting == [gid]
    assert _state(env, gid) == "fired"
    moved = store.accept_transfer(tid, (NOW + 210) * 1000)
    assert moved is not None and [g.id for g in moved] == [gid]
    report, _typed, launched, _notes = _deliver(env, now=NOW + 220)
    group = store.get_await_group(gid)
    assert group is not None and group.session_id == NEW
    assert launched == [(gid, group.delivery_token)] and report.launched == [gid]
    _report, _typed, again, _notes = _deliver(env, now=NOW + 230)
    assert not again  # delivered once


def test_a_failed_transfer_releases_the_group_to_the_source(env: dict[str, Any]) -> None:
    store: Store = env["store"]
    gid = _fired(env)
    tid = _handoff(env)
    _report, _typed, launched, _notes = _deliver(env)
    assert not launched
    assert store.transition_transfer(tid, "failed", (NOW + 210) * 1000, failure="launch: x")
    report, _typed, launched, _notes = _deliver(env, now=NOW + 220)
    group = store.get_await_group(gid)
    assert group is not None and (group.session_id, group.transfer_id) == (SID, "")
    assert launched == [(gid, group.delivery_token)] and report.launched == [gid]


def test_a_group_armed_after_start_is_deferred_then_delivered_to_the_successor(
    env: dict[str, Any],
) -> None:
    store: Store = env["store"]
    tid = _handoff(env)
    gid = _armed(env)  # OLD arms after start: it joins the reservation at arm time
    assert store.get_await_group(gid).transfer_id == tid  # type: ignore[union-attr]
    _fire(env, gid)
    report, typed, launched, _notes = _deliver(env, live=[_live(env)])
    assert not typed and not launched and report.waiting == [gid]
    assert store.accept_transfer(tid, (NOW + 210) * 1000) is not None
    _report, typed, launched, _notes = _deliver(env, now=NOW + 220)
    group = store.get_await_group(gid)
    assert group is not None and group.session_id == NEW
    assert launched == [(gid, group.delivery_token)] and not typed


def test_a_pass_that_loaded_the_group_before_the_rebind_cannot_claim_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interleaving, deterministic: the pass reads the fired rows, THEN accept rebinds
    (inside the pass's reservation check), THEN the pass tries its CAS with the old row."""
    store: Store = env["store"]
    gid = _fired(env)
    tid = _handoff(env)
    old_token = store.get_await_group(gid).delivery_token  # type: ignore[union-attr]
    real_is_reserved = store.is_reserved

    def accept_in_between(group_id: int) -> bool:
        transfer = store.get_transfer(tid)
        if group_id == gid and transfer is not None and transfer.state == "prepared":
            assert store.accept_transfer(tid, (NOW + 210) * 1000) is not None
        return real_is_reserved(group_id)

    monkeypatch.setattr(store, "is_reserved", accept_in_between)
    # OLD is live and idle: with OLD's row a successful CAS would type the wake into OLD's tab
    report, typed, launched, _notes = _deliver(env, live=[_live(env)])
    assert not typed and not launched and not report.delivered and not report.launched
    group = store.get_await_group(gid)
    assert group is not None and (group.state, group.session_id) == ("fired", NEW)
    assert group.delivery_token != old_token  # rotated by the rebind
    monkeypatch.setattr(store, "is_reserved", real_is_reserved)
    _report, typed, launched, _notes = _deliver(env, live=[_live(env)], now=NOW + 220)
    assert launched == [(gid, group.delivery_token)] and not typed  # NEW is closed: a new tab


def test_a_pass_that_loaded_the_group_before_the_rebind_cannot_block_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same interleaving, refusal path: OLD is done, so the pass's preflight on OLD's row
    would block the group, but the rebind rotated the token and NEW now owns it."""
    store: Store = env["store"]
    gid = _fired(env)
    tid = _handoff(env)
    store.update_fields(SID, done=True)
    real_is_reserved = store.is_reserved

    def accept_in_between(group_id: int) -> bool:
        transfer = store.get_transfer(tid)
        if group_id == gid and transfer is not None and transfer.state == "prepared":
            assert store.accept_transfer(tid, (NOW + 210) * 1000) is not None
        return real_is_reserved(group_id)

    monkeypatch.setattr(store, "is_reserved", accept_in_between)
    report, typed, launched, _notes = _deliver(env)
    group = store.get_await_group(gid)
    assert group is not None and (group.state, group.session_id) == ("fired", NEW)
    assert group.blocked_reason == "" and not report.blocked_groups
    assert not typed and not launched


# --------------------------------------------------------------------------- live matrix
def test_idle_live_session_gets_the_prompt_typed(env: dict[str, Any]) -> None:
    gid = _fired(env)
    report, typed, launched, _notes = _deliver(env, live=[_live(env)])
    assert not launched and report.delivered == [gid]
    [(tab, text)] = typed
    assert tab == "w0t0p0:ABC"
    assert text.startswith(FRAMING) and '"snippet":"ok"' in text
    assert _state(env, gid) == "delivered"


@pytest.mark.parametrize(
    "over",
    [
        {"raw_status": "busy"},
        {"raw_status": "waiting"},
        {"conflict": True},
        {"config_dir": "/some/other/account"},
        {"config_dir": ""},
        {"kind": "bg"},
        {"entrypoint": "sdk-cli"},
    ],
)
def test_live_but_not_typeable_waits(env: dict[str, Any], over: dict[str, Any]) -> None:
    gid = _fired(env)
    report, typed, launched, _notes = _deliver(env, live=[_live(env, **over)])
    assert not typed and not launched
    assert report.waiting == [gid] and _state(env, gid) == "fired"


def test_dead_registry_entry_counts_as_closed(env: dict[str, Any]) -> None:
    gid = _fired(env)
    _report, typed, launched, _notes = _deliver(env, live=[_live(env, alive=False)])
    assert launched and not typed and _state(env, gid) == "delivering"


def test_idle_live_session_without_a_tab_id_waits(env: dict[str, Any]) -> None:
    gid = _fired(env)
    env["store"].update_fields(SID, iterm_session_id="")
    report, typed, launched, _notes = _deliver(env, live=[_live(env)])
    assert not typed and not launched and report.waiting == [gid]


def test_failed_type_in_goes_back_to_fired(env: dict[str, Any]) -> None:
    gid = _fired(env)
    _deliver(env, live=[_live(env)], typer_ok=False)
    group = env["store"].get_await_group(gid)
    assert group.state == "fired" and group.delivery_attempts == 1


def test_unreadable_registry_delivers_nothing(env: dict[str, Any]) -> None:
    gid = _fired(env)
    report = PassReport()
    launched: list[Any] = []

    def broken() -> list[LiveSession]:
        raise OSError("registry unreadable")

    await_delivery.deliver_pending(
        env["store"],
        now=NOW + 200,
        report=report,
        notifier=Notes(),
        discover=broken,
        launcher=lambda g, t: launched.append((g, t)) or True,  # type: ignore[func-returns-value]
    )
    assert not launched and report.waiting == [gid] and _state(env, gid) == "fired"


# --------------------------------------------------------------------------- preflight
def test_pending_attached_prompt_makes_delivery_wait(env: dict[str, Any]) -> None:
    gid = _fired(env)
    env["store"].update_fields(SID, prompt="attached work", fire_at=NOW + 999)
    report, _typed, launched, _notes = _deliver(env)
    assert not launched and report.waiting == [gid]
    session = env["store"].get(SID)
    assert session.prompt == "attached work" and session.fire_at == NOW + 999  # untouched


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: (Path(e["acct"]) / ".claude.json").write_text("{}"), "not trusted"),
        (
            lambda e: next((Path(e["acct"]) / "projects").glob("*/*.jsonl")).unlink(),
            "no transcript",
        ),
        (lambda e: e["store"].update_fields(SID, done=True), "session is done"),
    ],
)
def test_failed_preflight_blocks_with_a_reason(
    env: dict[str, Any], mutate: Any, reason: str
) -> None:
    gid = _fired(env)
    mutate(env)
    report, _typed, launched, notes = _deliver(env)
    assert not launched and report.blocked_groups == [gid]
    group = env["store"].get_await_group(gid)
    assert group.state == "blocked" and reason in group.blocked_reason
    assert len(notes.messages) == 1


def test_transcript_under_another_account_does_not_count(
    env: dict[str, Any],
) -> None:
    gid = _fired(env)
    other = Path(env["tmp"]) / "other" / "projects" / "x"
    other.mkdir(parents=True)
    src = next((Path(env["acct"]) / "projects").glob("*/*.jsonl"))
    src.rename(other / src.name)
    _deliver(env)
    assert _state(env, gid) == "blocked"


def test_unknown_account_blocks_in_multi_account_mode(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store: Store = env["store"]
    gid = _fired(env)
    store.conn.execute("UPDATE await_groups SET config_dir = '' WHERE id = ?", (gid,))
    store.conn.commit()
    monkeypatch.setattr(accounts, "is_multi_account", lambda: True)
    _deliver(env)
    group = store.get_await_group(gid)
    assert group is not None
    assert group.state == "blocked" and "unknown" in group.blocked_reason


def test_account_transcript_is_account_local(env: dict[str, Any]) -> None:
    assert await_delivery.account_transcript(env["acct"], env["cwd"], SID) is not None
    assert await_delivery.account_transcript(env["acct"], "/elsewhere", SID) is not None  # glob
    assert (
        await_delivery.account_transcript(str(Path(env["tmp"]) / "nope"), env["cwd"], SID) is None
    )


# --------------------------------------------------------------------------- fire-await
def _claimable(
    env: dict[str, Any], *, no_codex: bool = False, fresh: bool = False
) -> tuple[int, str]:
    gid = _fired(env, no_codex=no_codex, fresh=fresh)
    group = env["store"].get_await_group(gid)
    assert env["store"].mark_delivering(gid, group.delivery_token, NOW)
    return gid, group.delivery_token


def _register_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]) -> None:
    """pin_environ / chdir mutate the process: register everything for restore."""
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CCC_NO_CODEX"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    monkeypatch.chdir(env["tmp"])


def _fire_await(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any], gid: int, token: str
) -> tuple[int, list[Any]]:
    execs: list[Any] = []
    _register_env(monkeypatch, env)
    monkeypatch.setattr(cli, "Store", lambda: Store(env["tmp"] / "state.db"))
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a, os.getcwd())))
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: pytest.fail("trust granted"))
    code = cli.cmd_fire_await(argparse.Namespace(group_id=gid, token=token))
    return code, execs


def test_fire_await_claims_then_execs_resume(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gid, token = _claimable(env, no_codex=True)
    code, execs = _fire_await(monkeypatch, env, gid, token)
    assert code == 0
    [(prog, argv, cwd)] = execs
    assert prog == "claude" and argv[:4] == ["claude", "--resume", SID, "--"]
    assert argv[4].startswith(FRAMING) and "Reply arrived:" in argv[4]
    assert cwd == str(Path(env["cwd"]).resolve()) or cwd == env["cwd"]
    assert os.environ.get("CCC_NO_CODEX") == "1"
    assert os.environ.get("CLAUDE_CONFIG_DIR") == env["acct"]
    assert _state(env, gid) == "delivered"
    # A second tab with the same token refuses.
    code2, execs2 = _fire_await(monkeypatch, env, gid, token)
    assert code2 == 1 and not execs2


def test_fire_await_wrong_token_refuses(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gid, _token = _claimable(env)
    code, execs = _fire_await(monkeypatch, env, gid, "forged")
    assert code == 1 and not execs and _state(env, gid) == "delivering"


def test_fire_await_without_terminal_hands_back(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gid, token = _claimable(env)
    monkeypatch.setattr(cli, "has_terminal", lambda: False)
    code, execs = _fire_await(monkeypatch, env, gid, token)
    assert code == 1 and not execs and _state(env, gid) == "fired"


def test_fire_await_untrusted_blocks(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    gid, token = _claimable(env)
    (Path(env["acct"]) / ".claude.json").write_text("{}")
    code, execs = _fire_await(monkeypatch, env, gid, token)
    assert code == 1 and not execs and _state(env, gid) == "blocked"


def test_fire_await_exec_failure_reverts(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gid, token = _claimable(env)
    _register_env(monkeypatch, env)
    monkeypatch.setattr(cli, "Store", lambda: Store(env["tmp"] / "state.db"))

    def boom(*_a: Any) -> None:
        raise OSError("no claude")

    monkeypatch.setattr(os, "execvp", boom)
    assert cli.cmd_fire_await(argparse.Namespace(group_id=gid, token=token)) == 1
    group = env["store"].get_await_group(gid)
    assert group.state == "fired" and group.delivery_attempts == 1


# --------------------------------------------------------------------------- pin_environ
def test_pin_environ_never_grants_trust(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(accounts, "ensure_trusted", lambda *_a, **_k: pytest.fail("trust granted"))
    for name in ("CLAUDE_CONFIG_DIR", "CCC_NO_CODEX"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/x")
    accounts.pin_environ(str(tmp_path / "acct"), False)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in os.environ
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path / "acct")
    assert "CCC_NO_CODEX" not in os.environ
    accounts.pin_environ("", True)  # the default account: UNSET
    assert "CLAUDE_CONFIG_DIR" not in os.environ
    assert os.environ["CCC_NO_CODEX"] == "1"


# --------------------------------------------------------------------------- -F/--fresh
def test_fresh_group_with_an_idle_live_session_goes_to_the_launcher(env: dict[str, Any]) -> None:
    gid = _fired(env, fresh=True)
    report, typed, launched, notes = _deliver(env, live=[_live(env)])
    token = env["store"].get_await_group(gid).delivery_token
    assert not typed and launched == [(gid, token)]
    assert report.launched == [gid] and _state(env, gid) == "delivering"
    assert "new session" in notes.messages[0]


def test_fresh_group_needs_no_old_transcript_nor_registry(env: dict[str, Any]) -> None:
    for jsonl in Path(env["acct"]).glob("projects/*/*.jsonl"):
        jsonl.unlink()
    gid = _fired(env, fresh=True)
    typed: list[Any] = []
    launched: list[Any] = []

    def broken() -> list[LiveSession]:
        raise RuntimeError("registry unreadable")

    def launcher(group_id: int, token: str) -> bool:
        launched.append((group_id, token))
        return True

    await_delivery.deliver_pending(
        env["store"],
        now=NOW + 200,
        report=PassReport(),
        notifier=Notes(),
        discover=broken,
        paster=_fake_paster(env, typed),
        launcher=launcher,
    )
    assert not typed and [g for g, _t in launched] == [gid]


def test_resume_group_still_fails_closed_on_a_broken_registry(env: dict[str, Any]) -> None:
    gid = _fired(env)
    report = PassReport()

    def broken() -> list[LiveSession]:
        raise RuntimeError("registry unreadable")

    await_delivery.deliver_pending(
        env["store"],
        now=NOW + 200,
        report=report,
        notifier=Notes(),
        discover=broken,
        paster=lambda *_a: pytest.fail("typed"),
        launcher=lambda *_a: pytest.fail("launched"),
    )
    assert report.waiting == [gid] and _state(env, gid) == "fired"


def _fire_await_exec_in(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any], gid: int, token: str
) -> tuple[int, list[Any]]:
    calls: list[Any] = []
    _register_env(monkeypatch, env)
    monkeypatch.setattr(cli, "Store", lambda: Store(env["tmp"] / "state.db"))
    monkeypatch.setattr(cli, "_exec_in", lambda cwd, argv, **kw: calls.append((cwd, argv, kw)))
    code = cli.cmd_fire_await(argparse.Namespace(group_id=gid, token=token))
    return code, calls


def test_fire_await_fresh_execs_a_new_session(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gid, token = _claimable(env, fresh=True)
    code, calls = _fire_await_exec_in(monkeypatch, env, gid, token)
    assert code == 0
    [(cwd, argv, kw)] = calls
    assert cwd == env["cwd"] and kw == {"strict": True}
    assert argv[:2] == ["claude", "--"] and len(argv) == 3 and "--resume" not in argv
    assert argv[2].startswith(f"This is a NEW session started by ccc await group {gid} ")
    assert f"\n\n{FRAMING}" in argv[2] and "Reply arrived:" in argv[2]
    assert os.environ.get("CLAUDE_CONFIG_DIR") == env["acct"]
    assert _state(env, gid) == "delivered"


def test_fire_await_resume_argv(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    gid, token = _claimable(env)
    code, calls = _fire_await_exec_in(monkeypatch, env, gid, token)
    assert code == 0
    [(_cwd, argv, _kw)] = calls
    assert argv[:4] == ["claude", "--resume", SID, "--"] and len(argv) == 5
    assert argv[4].startswith(FRAMING) and "NEW session" not in argv[4]


# --------------------------------------------------------------------------- paste guard (tp#1867)
@pytest.fixture(name="wenv")
def wenv_fixture(tmp_path: Path) -> dict[str, Any]:
    """The :func:`env_fixture` shape around a :class:`bridgestub.World`: its live session,
    tab and transcript (one finished turn: an established session), a trusted cwd."""
    world = World(tmp_path)
    world.write(user("earlier"), *turn_end())
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (world.config_dir / ".claude.json").write_text(
        json.dumps({"projects": {str(cwd.resolve()): {"hasTrustDialogAccepted": True}}})
    )
    return {
        "store": Store(world.db),
        "acct": str(world.config_dir),
        "cwd": str(cwd),
        "tmp": tmp_path,
        "sid": WORLD_SID,
        "world": world,
    }


def _guarded(
    wenv: dict[str, Any],
    *,
    now: int = NOW + 200,
    live: LiveSession | None = None,
    before_paste: Any = None,
) -> tuple[PassReport, Notes]:
    """One delivery pass through the REAL paste guard against the World's tab."""
    world: World = wenv["world"]
    seen = live if live is not None else world.live()
    notes = Notes()
    report = PassReport()

    def paster(target: LiveSession, text: str, claim: Any) -> PasteResult:
        if before_paste is not None:
            before_paste()
        return guarded_paste(world.deps(), target, text, claim=claim)

    await_delivery.deliver_pending(
        wenv["store"],
        now=now,
        report=report,
        notifier=notes,
        discover=lambda: [seen],
        paster=paster,
        launcher=lambda *_a: pytest.fail("a new tab for a live session"),
    )
    return report, notes


def _group(wenv: dict[str, Any], gid: int) -> Any:
    group = wenv["store"].get_await_group(gid)
    assert group is not None
    return group


def test_paste_guard_await_draft_holds_then_an_empty_box_delivers(wenv: dict[str, Any]) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    world.screen = fixture_screen("draft_one_line")
    report, notes = _guarded(wenv)
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts, group.held_since) == ("fired", 0, NOW + 200)
    assert report.held == [f"{gid}:input_not_empty"] and report.waiting == [gid]
    assert not world.texts and not notes.messages and not report.delivered
    world.screen = fixture_screen("empty_idle")
    report, notes = _guarded(wenv, now=NOW + 260)
    group = _group(wenv, gid)
    assert (group.state, group.held_since, group.held_notice_at) == ("delivered", 0, 0)
    assert report.delivered == [gid] and not report.held
    [text] = world.texts
    assert text.startswith(FRAMING)
    [message] = notes.messages
    assert "event typed into the live session via python-api" in message


def test_paste_guard_await_typing_user_holds_without_reading_the_box(
    wenv: dict[str, Any],
) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    world.frontmost, world.key_idle, world.focused = True, 1.0, TAB
    report, _notes = _guarded(wenv)
    assert report.held == [f"{gid}:user_active"] and world.screen_reads == 0
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts) == ("fired", 0) and not world.texts


def test_paste_guard_await_unreadable_ps_holds_without_an_attempt(wenv: dict[str, Any]) -> None:
    # tp#1868: a timed-out ps under load is a hold, not a failed typer (stale_tab before).
    world: World = wenv["world"]
    gid = _fired(wenv)
    world.ps_queue = [{}]
    report, _notes = _guarded(wenv)
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts, group.held_since) == ("fired", 0, NOW + 200)
    assert report.held == [f"{gid}:ps_unreadable"] and not world.texts
    report, _notes = _guarded(wenv, now=NOW + 260)
    assert report.delivered == [gid] and len(world.texts) == 1


def test_paste_guard_await_held_ten_minutes_notifies_once_and_delivery_still_notifies(
    wenv: dict[str, Any],
) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    world.screen = fixture_screen("draft_one_line")
    messages: list[str] = []
    for now in (NOW + 200, NOW + 500, NOW + 800, NOW + 900):
        _report, notes = _guarded(wenv, now=now)
        messages.extend(notes.messages)
    short = WORLD_SID[:8]
    # Exactly this wording: the count of the draft, never its text.
    assert messages == [
        f"await group {gid} held: the input box of {short} holds 28 chars — clear it in that tab"
    ]
    group = _group(wenv, gid)
    assert (group.held_since, group.held_notice_at, group.notified_at) == (NOW + 200, NOW + 800, 0)
    world.screen = fixture_screen("empty_idle")
    report, notes = _guarded(wenv, now=NOW + 1000)
    assert report.delivered == [gid]
    [message] = notes.messages  # the held notice did not consume `notified_at`
    assert "event typed into the live session" in message
    assert (_group(wenv, gid).held_since, _group(wenv, gid).held_notice_at) == (0, 0)


def test_paste_guard_await_partial_blocks_at_once_and_never_pastes_again(
    wenv: dict[str, Any],
) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    world.send_result = ("", "partial")
    report, notes = _guarded(wenv)
    short = WORLD_SID[:8]
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts) == ("blocked", 1)
    assert group.blocked_reason == f"prompt pasted but not submitted in {short}"
    assert report.blocked_groups == [gid] and not report.delivered
    assert notes.messages == [
        f"await group {gid} blocked: prompt pasted but not submitted in {short}"
        " — press Return in that tab"
    ]
    assert len(world.texts) == 1
    _guarded(wenv, now=NOW + 260)
    assert len(world.texts) == 1 and _group(wenv, gid).state == "blocked"


def test_paste_guard_await_stale_tab_counts_an_attempt(wenv: dict[str, Any]) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    world.tab_tty = "/dev/ttys099"
    report, _notes = _guarded(wenv)
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts) == ("fired", 1)
    assert not world.texts and world.screen_reads == 0 and not report.held


def test_paste_guard_await_session_gone_waits_without_an_attempt(wenv: dict[str, Any]) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    seen = world.live()  # the pass's registry read still listed it ...
    world.registered = False  # ... the one inside the lock does not
    report, notes = _guarded(wenv, live=seen)
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts, group.held_since) == ("fired", 0, 0)
    assert report.waiting == [gid] and not report.held
    assert not world.texts and not notes.messages


def test_paste_guard_await_claim_lost_types_nothing(wenv: dict[str, Any]) -> None:
    world: World = wenv["world"]
    gid = _fired(wenv)
    token = _group(wenv, gid).delivery_token

    def other_deliverer() -> None:
        assert Store(world.db).mark_delivering(gid, token, NOW + 199)

    report, notes = _guarded(wenv, before_paste=other_deliverer)
    group = _group(wenv, gid)
    assert (group.state, group.delivery_attempts) == ("delivering", 0)
    assert not world.texts and not notes.messages
    assert not report.delivered and not report.waiting and not report.held


def test_paste_guard_await_old_await_groups_table_gains_the_hold_columns(
    tmp_path: Path,
) -> None:
    db = tmp_path / "old.db"
    with Store(db) as store:
        store.ensure(SID, cwd="/repo")
        gid = store.arm_await(
            SID,
            config_dir="",
            cwd="/repo",
            no_codex=False,
            prompt_template="t {event}",
            until_epoch=NOW + 3600,
            sources=[SourceSpec(kind="zoho-reply", spec={}, watermark="w")],
            now=NOW,
        )
    conn = sqlite3.connect(db)
    for column in ("held_since", "held_notice_at"):
        conn.execute(f"ALTER TABLE await_groups DROP COLUMN {column}")
    conn.commit()
    conn.close()
    for _ in range(2):  # idempotent: a second open adds nothing and fails nothing
        with Store(db) as store:
            cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(await_groups)")}
            assert {"held_since", "held_notice_at"} <= cols
            group = store.get_await_group(gid)
            assert group is not None and (group.held_since, group.held_notice_at) == (0, 0)
