"""A Codex seat whose login is dead (tp#1196): classify, hop, remember, show, ask once.

On 2026-10-24 55 `codex-in-claude run` calls failed on ONE seat whose refresh token had
been spent (`401 … refresh_token_reused … Please log out and sign in again`) while two
other seats were healthy: the message matched no auth phrase, so every call was a plain
task failure — no hop, no record — and `order` kept showing the seat as available. What
is guarded here, against the recorded streams in `fixtures/codex_json/`:

* the dead-login streams (refresh token reused, no `auth.json`) classify as `auth`, and
  a narrow stderr match never turns an MCP server's 401 into one;
* the run hops to the next seat, records a needs-login block, and later calls skip the
  seat without launching it; `-S` on that seat fails with `seat_needs_login`;
* `order` / `headroom` / `usage` say `needs login` with the exact `codex login` command,
  and the headroom verdict never comes from that seat;
* a fresh `auth.json` (a new login) or a successful run lifts the block;
* the push asking for the login goes out at most once per seat per day.

Seats come from the `three_seats` fixture (order private → de → default) and `codex` is
`tests/fakes/fake_codex.py`; the push hook is a local script that appends to a file.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import pytest
from conftest import SeatFixture

from command_center import codex_in_claude as cic
from command_center import codex_login, quota

_FIXTURES = Path(__file__).parent / "fixtures" / "codex_json"
_MODEL = "gpt-5.6-sol"


@pytest.fixture(autouse=True)
def _known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the model catalog offline — `valid_slug` must not shell out to codex."""
    monkeypatch.setattr(
        cic,
        "list_models",
        lambda **_: [{"slug": _MODEL, "visibility": "list", "default_reasoning_level": "medium"}],
    )


@pytest.fixture(name="pushes")
def _pushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A push hook that appends each JSON payload it receives to a log; returns the log."""
    log = tmp_path / "pushes.log"
    hook = tmp_path / "push-hook.sh"
    hook.write_text(f'#!/bin/sh\ncat >> "{log}"\necho >> "{log}"\n', encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv(codex_login.PUSH_HOOK_ENV, str(hook))
    return log


def _pushed(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _stream(name: str) -> tuple[list[str], list[str]]:
    """The recorded ``(stdout lines, stderr lines)`` of one fixture."""
    stdout = [
        line
        for line in (_FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.startswith("{")
    ]
    stderr_path = _FIXTURES / f"{name}.stderr"
    stderr = stderr_path.read_text(encoding="utf-8").splitlines() if stderr_path.exists() else []
    return stdout, stderr


def _scripted(name: str) -> dict:
    stdout, stderr = _stream(name)
    return {"scenario": "scripted", "stdout": stdout, "stderr": stderr, "rc": 1}


def _run(
    seats: SeatFixture, capsys: pytest.CaptureFixture[str], **kw: object
) -> tuple[int, dict, str]:
    """``run -j`` → ``(exit code, envelope, stderr)``."""
    base: dict[str, object] = {
        "prompt": "reply OK",
        "cwd": str(seats.workdir),
        "model": _MODEL,
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
    capsys.readouterr()
    code = cic.cmd_run(argparse.Namespace(**base))
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip().splitlines()[-1]), captured.err


def _kill_private_login(seats: SeatFixture, capsys: pytest.CaptureFixture[str]) -> None:
    """One run whose first seat (private) refuses with the recorded dead-login stream."""
    seats.scenarios(private=_scripted("refresh_token_reused"), de={"scenario": "ok"})
    code, _env, _err = _run(seats, capsys)
    assert code == cic.EX_OK


# ── classification against the recorded streams ──────────────────────────────────
def _events(name: str) -> list[dict]:
    stdout, _stderr = _stream(name)
    return cic.parse_json_events("\n".join(stdout))


@pytest.mark.parametrize("fixture", ["refresh_token_reused", "no_auth_json"])
def test_dead_login_streams_classify_as_auth(fixture: str) -> None:
    """Events alone, stderr alone and both together all say: this seat needs login."""
    _stdout, stderr_lines = _stream(fixture)
    stderr = "\n".join(stderr_lines)
    assert cic.classify_codex_failure(1, _events(fixture), stderr).kind == "auth"
    assert cic.classify_codex_failure(1, _events(fixture), "").kind == "auth"
    assert cic.classify_codex_failure(1, [], stderr).kind == "auth"


def test_login_manager_stderr_wins_over_a_generic_failure_event() -> None:
    """A generic `turn.failed` plus codex's `Failed to refresh token: 401` → auth."""
    events = [{"type": "turn.failed", "error": {"message": "stream disconnected"}}]
    _stdout, stderr_lines = _stream("refresh_token_reused")
    failure = cic.classify_codex_failure(1, events, "\n".join(stderr_lines))
    assert failure.kind == "auth"


def test_an_mcp_401_in_stderr_is_not_a_dead_login() -> None:
    """Only codex's own login manager counts — a tool server's 401 keeps the seat."""
    events = [{"type": "turn.failed", "error": {"message": "stream disconnected"}}]
    stderr = (
        "2026-10-24T17:10:50Z ERROR rmcp::transport: MCP server 'docs' returned "
        "401 Unauthorized\n2026-10-24T17:10:51Z ERROR codex_login::auth::manager: "
        "Failed to refresh token: error sending request for url"
    )
    assert cic.classify_codex_failure(1, events, stderr).kind == ""


# ── the run: hop, record, skip, push once ─────────────────────────────────────────
def test_dead_login_hops_records_and_pushes_once(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], pushes: Path
) -> None:
    three_seats.scenarios(
        private=_scripted("refresh_token_reused"), de={"scenario": "ok", "reply": "de answered"}
    )
    before = int(time.time())
    code, envelope, err = _run(three_seats, capsys)
    assert code == cic.EX_OK
    assert envelope["reply"] == "de answered"
    assert three_seats.call_homes() == ["private", "de"]
    assert [a["outcome"] for a in envelope["attempts"]] == ["refused:auth", "ok"]
    command = codex_login.login_command(three_seats.seats["private"])
    assert "seat private needs login" in err and command in err and "falling back to de" in err
    entry = quota.read_cooldowns()["codex:private"]
    assert entry["scope"] == "auth" and entry["source"] == "codex-exec"
    assert entry["blocked_until"] >= before + 29 * 86400  # until login or success
    sent = _pushed(pushes)
    assert len(sent) == 1
    assert "Codex seat private" in sent[0]["message"] and command in sent[0]["message"]

    # The next call never launches the dead seat, and today's push is not repeated.
    three_seats.scenarios()  # every seat would answer ok now — private must not be asked
    code, envelope, _err = _run(three_seats, capsys)
    assert code == cic.EX_OK
    assert "private" not in [a["seat"] for a in envelope["attempts"]]
    assert three_seats.call_homes()[:2] == ["private", "de"]
    assert "private" not in three_seats.call_homes()[2:]
    assert len(_pushed(pushes)) == 1


def test_seat_pin_on_a_dead_login_fails_typed(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], pushes: Path
) -> None:
    """`-S private`: the refusal itself, and every later call, end as seat_needs_login."""
    three_seats.scenarios(private=_scripted("refresh_token_reused"))
    code, envelope, err = _run(three_seats, capsys, seat="private")
    assert code == cic.EX_QUOTA
    assert envelope["error"]["kind"] == "seat_needs_login"
    assert codex_login.login_command(three_seats.seats["private"]) in envelope["error"]["message"]
    assert "falling back" not in err
    assert three_seats.call_homes() == ["private"]

    code, envelope, _err = _run(three_seats, capsys, seat="private")
    assert code == cic.EX_QUOTA and envelope["error"]["kind"] == "seat_needs_login"
    assert three_seats.call_homes() == ["private"]  # nothing launched the second time
    assert len(_pushed(pushes)) == 1


def test_explicit_home_is_skipped_unless_q_and_a_success_clears(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pushes: Path,  # pylint: disable=unused-argument  # keeps the real hook out
) -> None:
    _kill_private_login(three_seats, capsys)
    monkeypatch.setenv("CODEX_HOME", str(three_seats.seats["private"]))
    code, envelope, _err = _run(three_seats, capsys)
    assert code == cic.EX_QUOTA
    assert [a["outcome"] for a in envelope["attempts"]] == ["skipped:needs_login"]
    # -Q is the explicit "try it anyway"; a success disproves the dead login.
    three_seats.scenarios(private={"scenario": "ok", "reply": "logged in after all"})
    code, envelope, _err = _run(three_seats, capsys, ignore_quota=True)
    assert code == cic.EX_OK and envelope["reply"] == "logged in after all"
    assert "codex:private" not in quota.read_cooldowns()


def test_fresh_login_lifts_the_block(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    pushes: Path,  # pylint: disable=unused-argument  # keeps the real hook out
) -> None:
    """A rewritten auth.json (what `codex login` does) makes the seat eligible again."""
    _kill_private_login(three_seats, capsys)
    assert [s["label"] for s in cic.needs_login_seats()] == ["private"]
    assert "private" not in [c.label for c in cic.codex_homes_in_order()]
    entry = quota.read_cooldowns()["codex:private"]
    later = int(entry["observed_at"]) + 5
    os.utime(three_seats.seats["private"] / "auth.json", (later, later))
    assert not cic.needs_login_seats()
    assert "private" in [c.label for c in cic.codex_homes_in_order()]


# ── what the read-only commands show ──────────────────────────────────────────────
def test_order_headroom_usage_say_needs_login(
    three_seats: SeatFixture,
    capsys: pytest.CaptureFixture[str],
    pushes: Path,  # pylint: disable=unused-argument  # keeps the real hook out
) -> None:
    _kill_private_login(three_seats, capsys)
    command = codex_login.login_command(three_seats.seats["private"])

    cic.cmd_order(argparse.Namespace(labels=[], clear=False, json=False))
    table = capsys.readouterr().out
    private = next(line for line in table.splitlines() if "private" in line)
    assert "🔑 needs login" in private and command in private and "available" not in private

    cic.cmd_order(argparse.Namespace(labels=[], clear=False, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert [s["label"] for s in payload["needs_login"]] == ["private"]
    assert payload["needs_login"][0]["login_command"] == command
    assert "private" not in [c["label"] for c in payload["candidates"]]

    decision = cic.codex_headroom()
    assert decision["seat"] != "private"
    assert [s["label"] for s in decision["needs_login"]] == ["private"]
    cic.cmd_headroom(argparse.Namespace(json=False))
    text = capsys.readouterr().out
    assert f"private: needs login (private@example.org) — run `{command}`" in text

    cic.cmd_usage(argparse.Namespace(json=False))
    assert f"private: needs login (private@example.org) — run `{command}`" in (
        capsys.readouterr().out
    )
    cic.cmd_usage(argparse.Namespace(json=True))
    assert json.loads(capsys.readouterr().out)["needs_login"][0]["label"] == "private"


def test_seat_headroom_never_chooses_a_dead_login() -> None:
    row = quota.ProviderQuota(
        id="codex:private",
        kind="codex",
        state=quota.BLOCKED,
        reason="codex exec refused: auth — refresh token was already used",
        account="private",
        block_scope="auth",
    )
    verdict = cic.seat_headroom(row, int(time.time()))
    assert verdict["state"] == "needs_login"


# ── the push: once per seat per day, and only through a real hook ─────────────────
def test_daily_alert_is_once_per_seat_per_day(three_seats: SeatFixture) -> None:
    del three_seats  # only for the isolated app home
    noon = int(time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1)))
    assert codex_login.claim_daily_alert("codex:private", noon)
    assert not codex_login.claim_daily_alert("codex:private", noon + 3600)
    assert codex_login.claim_daily_alert("codex:de", noon + 3600)
    assert codex_login.claim_daily_alert("codex:private", noon + 86400)


def test_push_hook_disabled_or_missing_sends_nothing(
    three_seats: SeatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(codex_login.PUSH_HOOK_ENV, "")
    assert codex_login.push_hook() is None
    home = three_seats.seats["private"]
    assert not codex_login.notify_needs_login("private", "codex:private", home)
    monkeypatch.delenv(codex_login.PUSH_HOOK_ENV)
    assert codex_login.push_hook() is None  # no hook under the isolated $HOME
    assert not codex_login.notify_needs_login("de", "codex:de", three_seats.seats["de"])
    assert not codex_login.notify_needs_login("explicit", "", home)  # unregistered: never
