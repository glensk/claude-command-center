"""Delivering a fired await group: preflight, the live-session matrix, the outbox,
``ccc fire-await`` and ``accounts.pin_environ``.

Every launcher, typer and ``os.execvp`` is a recording fake — no tab opens, nothing is
typed into a real terminal, no ``claude`` runs.
"""

# pylint: disable=unbalanced-tuple-unpacking  # `[(x, _)] = …` asserts exactly one
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

from command_center import accounts, await_delivery, cli
from command_center.await_eval import PassReport
from command_center.await_prompt import FRAMING
from command_center.await_store import SourceSpec
from command_center.models import LiveSession
from command_center.store import Store

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
    return {"store": store, "acct": str(acct), "cwd": str(cwd), "tmp": tmp_path}


def _fired(env: dict[str, Any], *, no_codex: bool = False, fresh: bool = False) -> int:
    store: Store = env["store"]
    gid = store.arm_await(
        SID,
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
    return gid


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


def _deliver(
    env: dict[str, Any],
    *,
    live: list[LiveSession] | None = None,
    typer_ok: bool = True,
    launcher_ok: bool = True,
    now: int = NOW + 200,
) -> tuple[PassReport, list[Any], list[Any], Notes]:
    typed: list[Any] = []
    launched: list[Any] = []
    notes = Notes()
    report = PassReport()

    def typer(tab: str, text: str) -> bool:
        typed.append((tab, text))
        return typer_ok

    def launcher(group_id: int, token: str) -> bool:
        launched.append((group_id, token))
        return launcher_ok

    await_delivery.deliver_pending(
        env["store"],
        now=now,
        report=report,
        notifier=notes,
        discover=lambda: list(live or []),
        typer=typer,
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
    assert prog == "claude" and argv[:3] == ["claude", "--resume", SID]
    assert argv[3].startswith(FRAMING) and "Reply arrived:" in argv[3]
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

    def typer(tab: str, text: str) -> bool:
        typed.append((tab, text))
        return True

    def launcher(group_id: int, token: str) -> bool:
        launched.append((group_id, token))
        return True

    await_delivery.deliver_pending(
        env["store"],
        now=NOW + 200,
        report=PassReport(),
        notifier=Notes(),
        discover=broken,
        typer=typer,
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
        typer=lambda *_a: pytest.fail("typed"),
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
    assert len(argv) == 2 and argv[0] == "claude" and "--resume" not in argv
    assert argv[1].startswith(f"This is a NEW session started by ccc await group {gid} ")
    assert f"\n\n{FRAMING}" in argv[1] and "Reply arrived:" in argv[1]
    assert os.environ.get("CLAUDE_CONFIG_DIR") == env["acct"]
    assert _state(env, gid) == "delivered"


def test_fire_await_resume_argv(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    gid, token = _claimable(env)
    code, calls = _fire_await_exec_in(monkeypatch, env, gid, token)
    assert code == 0
    [(_cwd, argv, _kw)] = calls
    assert argv[:3] == ["claude", "--resume", SID] and len(argv) == 4
    assert argv[3].startswith(FRAMING) and "NEW session" not in argv[3]
