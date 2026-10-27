"""tp#1327: a BLOCKED quota reading is a cap only while no turn on the seat succeeded since."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from command_center import accounts, config, handoff_capped, quota, usage
from command_center.adapters import claude as claude_adapter
from command_center.continue_session import Source
from command_center.models import LiveSession

FULL_AT = 1_793_000_000  # the first capture that read the weekly window full
RESET = FULL_AT + 3 * 86400


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _ok(epoch: float) -> dict:
    return {
        "type": "assistant",
        "timestamp": _stamp(epoch),
        "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]},
    }


def _halt(epoch: float) -> dict:
    text = "You've hit your weekly limit · resets Oct 28, 11pm"
    return {
        "type": "assistant",
        "timestamp": _stamp(epoch),
        "isApiErrorMessage": True,
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
    }


def _synthetic(epoch: float) -> dict:
    return {
        "type": "assistant",
        "timestamp": _stamp(epoch),
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "No response"}]},
    }


def _transcript(path: Path, *records: dict) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class _Adapter(claude_adapter.ClaudeAdapter):
    """Live sessions and transcripts from the test, nothing from ``~/.claude``."""

    def __init__(self, live: list[tuple[LiveSession, Path]]) -> None:
        super().__init__()
        self.live = live

    def discover(self) -> list[LiveSession]:
        return [session for session, _ in self.live]

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        del cwd, config_dir
        return next((p for s, p in self.live if s.session_id == session_id), None)


@pytest.fixture(name="seat")
def _seat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The work seat reads BLOCKED on its weekly window, full since FULL_AT."""
    work = tmp_path / "cfg-work"
    monkeypatch.setattr(
        config, "claude_config_dirs", lambda: {"private": tmp_path / "cfg-priv", "work": work}
    )
    monkeypatch.setattr(accounts, "_resolve", Path)
    monkeypatch.delenv(handoff_capped.ASSUME_CAPPED_ENV, raising=False)
    blocked = quota.ProviderQuota(
        id="claude:work",
        kind="claude",
        state=quota.BLOCKED,
        reason="seven_day window at 100%",
        source="windows",
        blocked_by="seven_day",
        captured_at=FULL_AT + 7200,  # bumped by the status line long after it filled
    )
    monkeypatch.setattr(quota, "provider_state", lambda pid, **_k: blocked)
    monkeypatch.setattr(
        usage, "full_since", lambda label, window: FULL_AT if window == "seven_day" else 0
    )
    return work


def _src(work: Path, transcript: Path) -> Source:
    return Source(
        session_id="src", cwd="/repo", config_dir=str(work), label="work", transcript=transcript
    )


def test_blocked_then_a_successful_turn_is_not_capped(seat: Path, tmp_path: Path) -> None:
    path = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT - 60), _ok(FULL_AT + 600))
    capped, why = handoff_capped.is_capped(_Adapter([]), _src(seat, path))
    assert capped is False
    assert "blocked" in why and "a turn succeeded" in why


def test_blocked_with_no_turn_since_is_capped(seat: Path, tmp_path: Path) -> None:
    # The turn that filled the window predates the first full reading; the synthetic
    # filler after it is no model call.
    path = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT - 60), _synthetic(FULL_AT + 900))
    capped, why = handoff_capped.is_capped(_Adapter([]), _src(seat, path))
    assert capped is True and "work seat is blocked" in why


def test_another_live_session_on_the_seat_proves_it_runs(seat: Path, tmp_path: Path) -> None:
    src = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT - 60))
    other = _transcript(tmp_path / "other.jsonl", _ok(FULL_AT + 300))
    priv = _transcript(tmp_path / "priv.jsonl", _ok(FULL_AT + 400))
    live = [
        (LiveSession(pid=1, session_id="o", cwd="/x", alive=True, config_dir=str(seat)), other),
    ]
    assert handoff_capped.is_capped(_Adapter(live), _src(seat, src))[0] is False
    # a success on the OTHER account proves nothing about this seat
    priv_dir = str(tmp_path / "cfg-priv")
    live = [(LiveSession(pid=2, session_id="p", cwd="/x", alive=True, config_dir=priv_dir), priv)]
    assert handoff_capped.is_capped(_Adapter(live), _src(seat, src))[0] is True


def test_a_halt_after_the_success_caps_again(seat: Path, tmp_path: Path) -> None:
    src = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT + 300))
    other = _transcript(tmp_path / "other.jsonl", _halt(FULL_AT + 600))
    live = [
        (LiveSession(pid=1, session_id="o", cwd="/x", alive=True, config_dir=str(seat)), other),
    ]
    assert handoff_capped.is_capped(_Adapter(live), _src(seat, src))[0] is True


def test_unknown_start_of_the_blocked_reading_stays_capped(
    seat: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "full_since", lambda label, window: 0)
    path = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT + 600))
    assert handoff_capped.is_capped(_Adapter([]), _src(seat, path))[0] is True


def test_a_real_usage_limit_stop_is_capped(seat: Path, tmp_path: Path) -> None:
    path = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT + 300), _halt(FULL_AT + 600))
    capped, why = handoff_capped.is_capped(_Adapter([]), _src(seat, path))
    assert capped is True and "usage limit" in why


def test_the_override_env_forces_capped(
    seat: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(handoff_capped.ASSUME_CAPPED_ENV, "1")
    path = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT + 600))
    assert handoff_capped.is_capped(_Adapter([]), _src(seat, path)) == (
        True,
        "CCC_HANDOFF_ASSUME_CAPPED=1",
    )


def test_a_seat_that_is_not_blocked_is_not_capped(
    seat: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quota, "provider_state", lambda pid, **_k: None)
    path = _transcript(tmp_path / "src.jsonl", _ok(FULL_AT - 600))
    assert handoff_capped.is_capped(_Adapter([]), _src(seat, path)) == (False, "")


def test_full_since_dates_the_first_full_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    five = {"used_percentage": 10, "resets_at": FULL_AT + 3600}

    def week(pct: float, reset: int = RESET) -> dict:
        return {"five_hour": five, "seven_day": {"used_percentage": pct, "resets_at": reset}}

    assert usage.write_usage(week(99), now=FULL_AT - 10)
    assert usage.full_since("private", "seven_day") == 0
    assert usage.write_usage(week(100), now=FULL_AT)
    # later status-line writes bump captured_at but keep the first full time
    assert usage.write_usage(week(100), now=FULL_AT + 3)
    assert usage.write_usage(week(100, RESET + 2), now=FULL_AT + 6)
    assert usage.full_since("private", "seven_day") == FULL_AT
    assert usage.full_since("private", "five_hour") == 0
