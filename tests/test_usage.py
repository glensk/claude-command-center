"""Tests for the account-wide /usage snapshot capture, format, and render."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import threading
import time
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center import cli, config, usage

# A realistic status-line rate_limits payload (the live shape, verified).
_RATE_LIMITS = {
    "five_hour": {"used_percentage": 27, "resets_at": 1782320400},
    "seven_day": {"used_percentage": 93, "resets_at": 1782338400},
}
_NOW = 1782302578  # 2026-06-24 14:02 CEST — between "now" and both resets

# A realistic Codex rollout rate_limits block (verified shape). Window duration, rather
# than primary/secondary position, identifies the 5-hour and weekly quota buckets.
_CODEX_RATE_LIMITS = {
    "limit_id": "codex",
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1782320400},
    "secondary": {"used_percent": 45.0, "window_minutes": 10080, "resets_at": 1782893849},
    "plan_type": "team",
}

# The windowless shape short ``codex exec`` runs log (verified): primary/secondary are
# both null, so it carries NO 5h/weekly data and must be skipped by the reader.
_CODEX_PREMIUM_NULL = {
    "limit_id": "premium",
    "limit_name": None,
    "primary": None,
    "secondary": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": None},
    "individual_limit": None,
    "plan_type": None,
    "rate_limit_reached_type": None,
}


def _write_codex_rollout(
    codex_home: Path, rate_limits: dict | None, *, name: str, mtime: int | None = None
) -> Path:
    """Write a minimal Codex session rollout JSONL under ``$CODEX_HOME/sessions/...``."""
    day = codex_home / "sessions" / "2026" / "06" / "24"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-06-24T10-00-00-{name}.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {}})]
    if rate_limits is not None:
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "rate_limits": rate_limits},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_format_reset_hours_and_minutes() -> None:
    # +1h 4m
    assert usage.format_reset(_NOW + 3600 + 4 * 60, now=_NOW) == "in 1h 4m"


def test_format_reset_days_hours_minutes() -> None:
    assert usage.format_reset(_NOW + 4 * 86400 + 13 * 3600 + 4 * 60, now=_NOW) == "in 4d 13h 4m"


def test_format_reset_minutes_only() -> None:
    assert usage.format_reset(_NOW + 9 * 60, now=_NOW) == "in 9m"


def test_format_reset_past_is_now() -> None:
    assert usage.format_reset(_NOW - 10, now=_NOW) == "now"


def test_codex_exhausted_window_matches_live_quota_preflight() -> None:
    snap = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(100.0, _NOW + 3600),
        seven_day=usage.Window(75.0, _NOW + 7 * 86400),
    )
    exhausted = usage.codex_exhausted_window(snap, now=_NOW)
    assert exhausted is not None
    assert exhausted[0] == "5h"
    assert exhausted[1].resets_at == _NOW + 3600

    # Stale exhausted windows do not block after their reset passed.
    stale = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(100.0, _NOW - 1),
        seven_day=usage.Window(99.9, _NOW + 7 * 86400),
    )
    assert usage.codex_exhausted_window(stale, now=_NOW) is None

    # If both live windows are exhausted, the most-consumed one wins, like codex-in-claude.
    both = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(100.0, _NOW + 3600),
        seven_day=usage.Window(101.0, _NOW + 7 * 86400),
    )
    chosen = usage.codex_exhausted_window(both, now=_NOW)
    assert chosen is not None and chosen[0] == "weekly"


def test_write_then_read_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert usage.write_usage(_RATE_LIMITS, now=_NOW) is True
    snap = usage.read_usage()
    assert snap is not None
    assert snap.captured_at == _NOW
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 27
    assert snap.five_hour.resets_at == 1782320400
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 93


def test_write_skips_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    # First a good snapshot, then an empty payload must NOT clobber it.
    assert usage.write_usage(_RATE_LIMITS, now=_NOW) is True
    assert usage.write_usage({}, now=_NOW + 10) is False
    assert usage.write_usage(None, now=_NOW + 10) is False
    snap = usage.read_usage()
    assert snap is not None and snap.captured_at == _NOW  # unchanged


def test_write_drops_past_resets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    # Both windows already reset (idle session's stale snapshot): nothing live to
    # persist, so it must not write a past reset that would render as "Resets now".
    past = {
        "five_hour": {"used_percentage": 60, "resets_at": _NOW - 100},
        "seven_day": {"used_percentage": 20, "resets_at": _NOW - 200},
    }
    assert usage.write_usage(past, now=_NOW) is False
    assert usage.read_usage() is None


def test_write_stale_does_not_clobber_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    fresh = {
        "five_hour": {"used_percentage": 20, "resets_at": _NOW + 3 * 3600},
        "seven_day": {"used_percentage": 50, "resets_at": _NOW + 5 * 86400},
    }
    # A concurrent idle session reports an older (here already-past) snapshot.
    stale = {
        "five_hour": {"used_percentage": 99, "resets_at": _NOW - 86400},
        "seven_day": {"used_percentage": 99, "resets_at": _NOW - 2 * 86400},
    }
    assert usage.write_usage(fresh, now=_NOW) is True
    usage.write_usage(stale, now=_NOW + 5)  # must not pull the snapshot backward
    snap = usage.read_usage()
    assert snap is not None and snap.five_hour is not None and snap.seven_day is not None
    assert snap.five_hour.resets_at == _NOW + 3 * 3600
    assert snap.five_hour.used_percentage == 20  # fresh value preserved
    assert snap.seven_day.resets_at == _NOW + 5 * 86400


def test_write_same_reset_keeps_higher_pct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    reset = _NOW + 5 * 86400  # one fixed weekly boundary, shared by every session
    high = {
        "five_hour": {"used_percentage": 28, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 28, "resets_at": reset},
    }
    # An idle session reports a days-old, lower total for the SAME weekly window.
    stale_low = {
        "five_hour": {"used_percentage": 28, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 8, "resets_at": reset},
    }
    assert usage.write_usage(high, now=_NOW) is True
    usage.write_usage(stale_low, now=_NOW + 3)  # must not flip the card down (8% ↔ 28%)
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.used_percentage == 28
    # A genuinely higher cumulative total still lands.
    higher = {
        "five_hour": {"used_percentage": 28, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 31, "resets_at": reset},
    }
    usage.write_usage(higher, now=_NOW + 6)
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.used_percentage == 31


def test_write_adopts_later_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    first = {
        "five_hour": {"used_percentage": 80, "resets_at": _NOW + 600},
        "seven_day": {"used_percentage": 10, "resets_at": _NOW + 86400},
    }
    # The 5h window genuinely rolled: a later reset boundary is the new window.
    rolled = {
        "five_hour": {"used_percentage": 5, "resets_at": _NOW + 5 * 3600},
        "seven_day": {"used_percentage": 10, "resets_at": _NOW + 86400},
    }
    assert usage.write_usage(first, now=_NOW) is True
    assert usage.write_usage(rolled, now=_NOW + 10) is True
    snap = usage.read_usage()
    assert snap is not None and snap.five_hour is not None
    assert snap.five_hour.resets_at == _NOW + 5 * 3600  # later reset adopted
    assert snap.five_hour.used_percentage == 5


def test_read_missing_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert usage.read_usage() is None


# --- per-account usage snapshots (multi-account) --------------------------------


def _two_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin two accounts (private + work) and return the work config dir."""
    work_dir = tmp_path / "claude-work"
    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: {"private": config.claude_home(), "work": work_dir},
    )
    return work_dir


def test_per_account_write_read_roundtrip_and_no_cross_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each account keeps its own numbers; a work write never touches the private card."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _two_accounts(tmp_path, monkeypatch)
    private_rl = {
        "five_hour": {"used_percentage": 10, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 15, "resets_at": _NOW + 7 * 86400},
    }
    work_rl = {
        "five_hour": {"used_percentage": 80, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 90, "resets_at": _NOW + 7 * 86400},
    }
    assert usage.write_usage(private_rl, account="private", now=_NOW) is True
    assert usage.write_usage(work_rl, account="work", now=_NOW) is True

    # The default account still lives in usage.json; work lives in its own hashed file.
    assert (config.app_home() / "usage.json").exists()
    work_files = list(config.app_home().glob("usage-work-*.json"))
    assert len(work_files) == 1

    priv = usage.read_usage()  # default "private"
    work = usage.read_usage("work")
    assert priv is not None and priv.five_hour is not None
    assert work is not None and work.five_hour is not None
    # No cross-account _merge_window: the work snapshot cannot pull the private card.
    assert priv.five_hour.used_percentage == 10
    assert work.five_hour.used_percentage == 80


def test_work_write_never_lands_in_usage_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write routed to a non-default account must not create/populate usage.json."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _two_accounts(tmp_path, monkeypatch)
    assert usage.write_usage(_RATE_LIMITS, account="work", now=_NOW) is True
    assert not (config.app_home() / "usage.json").exists()
    assert usage.read_usage() is None  # the private card stays empty


def test_read_refuses_on_config_dir_hash_mismatch_default_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remapping an account's dir (usage.json's fixed name) refuses the stale payload."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    dir_a = tmp_path / "acct-a"
    monkeypatch.setattr(config, "claude_config_dirs", lambda: {"private": dir_a})
    assert usage.write_usage(_RATE_LIMITS, account="private", now=_NOW) is True
    assert usage.read_usage() is not None  # same dir → served

    # Reuse the label "private" for a DIFFERENT dir: the stored config_dir_hash no
    # longer matches, so the previous account's numbers must not be served.
    dir_b = tmp_path / "acct-b"
    monkeypatch.setattr(config, "claude_config_dirs", lambda: {"private": dir_b})
    assert usage.read_usage() is None


def test_read_refuses_on_label_reuse_for_nondefault_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write under work→dirA, remap work→dirB, read_usage('work') returns None."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    monkeypatch.setattr(
        config, "claude_config_dirs", lambda: {"private": config.claude_home(), "work": dir_a}
    )
    assert usage.write_usage(_RATE_LIMITS, account="work", now=_NOW) is True
    assert usage.read_usage("work") is not None
    monkeypatch.setattr(
        config, "claude_config_dirs", lambda: {"private": config.claude_home(), "work": dir_b}
    )
    assert usage.read_usage("work") is None


def test_legacy_hashless_usage_json_reads_for_default_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing usage.json (no config_dir_hash) is accepted for private only."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    app_home = config.app_home()
    app_home.mkdir(parents=True, exist_ok=True)
    legacy = {
        "captured_at": _NOW,
        "five_hour": {"used_percentage": 12, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 34, "resets_at": _NOW + 7 * 86400},
    }
    (app_home / "usage.json").write_text(json.dumps(legacy), encoding="utf-8")
    snap = usage.read_usage()  # default private → accepted
    assert snap is not None and snap.five_hour is not None
    assert snap.five_hour.used_percentage == 12

    # The same hashless payload placed at a non-default account's path is refused.
    monkeypatch.setattr(
        config, "claude_config_dirs", lambda: {"private": config.claude_home(), "work": tmp_path}
    )
    work_path = usage._usage_path("work")
    work_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert usage.read_usage("work") is None


def test_concurrent_writers_no_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Many concurrent writers leave valid JSON, the highest total, and no stray temp."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))

    def worker(pct: int) -> None:
        rl = {
            "five_hour": {"used_percentage": pct, "resets_at": _NOW + 3600},
            "seven_day": {"used_percentage": pct, "resets_at": _NOW + 7 * 86400},
        }
        for _ in range(25):
            usage.write_usage(rl, now=_NOW)

    threads = [threading.Thread(target=worker, args=(pct,)) for pct in (10, 20, 30, 40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snap = usage.read_usage()  # valid JSON survived the concurrent writers
    assert snap is not None and snap.five_hour is not None
    # Same reset → higher cumulative percentage wins the merge, monotonically; the
    # highest writer (40) sticks once written, proving the merge stayed consistent.
    assert snap.five_hour.used_percentage == 40
    # No stray temp files left behind (mkstemp + os.replace, never a fixed .tmp name).
    assert list(config.app_home().glob("*.tmp")) == []


def test_atomic_write_cleans_temp_on_non_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-OSError mid-write leaves no stray temp (the old `except OSError` leaked)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    config.app_home().mkdir(parents=True, exist_ok=True)
    target = config.app_home() / "usage.json"
    with pytest.raises(TypeError):
        usage._atomic_write_json(target, {"x": object()})
    assert list(config.app_home().glob("*.tmp")) == []


def test_atomic_write_cleans_temp_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeyboardInterrupt at the replace leaves no temp AND does not touch the target."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    config.app_home().mkdir(parents=True, exist_ok=True)
    target = config.app_home() / "usage.json"
    target.write_text('{"kept": true}', encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(usage.os, "replace", _boom)
    with pytest.raises(KeyboardInterrupt):
        usage._atomic_write_json(target, {"kept": False})
    assert list(config.app_home().glob("*.tmp")) == []
    assert target.read_text(encoding="utf-8") == '{"kept": true}'


def test_sweep_removes_only_stale_temps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Orphans older than the threshold go; a fresh temp, the cache and the lock stay."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    home = config.app_home()
    home.mkdir(parents=True, exist_ok=True)
    stale = home / "usage.json.aaaaaaaa.tmp"
    fresh = home / "usage.json.bbbbbbbb.tmp"
    for path in (stale, fresh):
        path.write_text("{}", encoding="utf-8")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    cache = home / "usage.json"
    cache.write_text("{}", encoding="utf-8")
    lock = home / "usage.json.lock"
    lock.write_text("", encoding="utf-8")

    assert usage.sweep_stale_temps() == 1
    assert not stale.exists()
    assert fresh.exists() and cache.exists() and lock.exists()


def test_sweep_covers_every_producer_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-account `usage-<hash>.json`, `copilot_usage.json` and per-home Codex orphans too."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    home = config.app_home()
    home.mkdir(parents=True, exist_ok=True)
    old = time.time() - 7200
    for name in (
        "usage-work-b6f4d184.json.cccccccc.tmp",
        "copilot_usage.json.dddddddd.tmp",
        "codex_usage-a1b2c3d4.json.eeeeeeee.tmp",
    ):
        path = home / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (old, old))

    assert usage.sweep_stale_temps() == 3
    assert list(home.glob("*.tmp")) == []


def test_sweep_ignores_foreign_temps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Other subsystems' temps are never in the deletion set, however old they are."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    home = config.app_home()
    home.mkdir(parents=True, exist_ok=True)
    old = time.time() - 7200
    foreign = ["config.toml.eeeeeeee.tmp", "future_sync_state.json.tmp", "resume_queue.tmp"]
    for name in foreign:
        path = home / name
        path.write_text("", encoding="utf-8")
        os.utime(path, (old, old))

    assert usage.sweep_stale_temps() == 0
    assert sorted(p.name for p in home.glob("*.tmp")) == sorted(foreign)


def test_sweep_survives_unlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A temp that vanishes (or cannot be removed) mid-sweep is skipped, not raised."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    home = config.app_home()
    home.mkdir(parents=True, exist_ok=True)
    old = time.time() - 7200
    for name in ("usage.json.ffffffff.tmp", "usage.json.gggggggg.tmp"):
        path = home / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (old, old))

    real_unlink = Path.unlink
    calls = {"n": 0}

    def _flaky(self: Path, *, missing_ok: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky)
    assert usage.sweep_stale_temps() == 1


def test_render_contains_labels_pct_and_relative_reset() -> None:
    snap = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(27, 1782320400),
        seven_day=usage.Window(93, 1782338400),
    )
    plain = usage.render_usage(snap, now=_NOW).plain
    # The standalone title lines are gone; the window name is embossed on the bar.
    assert "Session: Resets in " in plain
    assert "Week: Resets in " in plain
    assert "Current session" not in plain and "Current week" not in plain
    assert "27%" in plain and "93%" in plain
    # Reset is relative, not an absolute clock time.
    assert "(Europe" not in plain and "am" not in plain
    # The percentage is right-aligned to the card's inner width (no dead space).
    for line in plain.splitlines():
        assert len(line) == usage._CARD_INNER_WIDTH


def test_render_empty_placeholder() -> None:
    plain = usage.render_usage(None, now=_NOW).plain
    assert "start a turn" in plain


def test_render_usage_accent_distinguishes_private_and_work() -> None:
    """The two Claude cards read apart: private gold vs work blue reset-label accent."""
    snap = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(27, 1782320400),
        seven_day=usage.Window(93, 1782338400),
    )

    def accent_styles(text: object) -> set[str]:
        return {str(span.style) for span in text.spans}  # type: ignore[attr-defined]

    private = usage.render_usage(snap, now=_NOW)  # default gold accent
    work = usage.render_usage(snap, now=_NOW, accent=usage._CLAUDE_WORK_ACCENT)
    # The gold accent appears in the private card and not the work card (and vice versa),
    # while the per-bar usage fills (27% session → green, 93% week → red) are in both.
    assert any(usage._CLAUDE_ACCENT in s for s in accent_styles(private))
    assert not any(usage._CLAUDE_ACCENT in s for s in accent_styles(work))
    assert any(usage._CLAUDE_WORK_ACCENT in s for s in accent_styles(work))
    assert any(usage._FILL_GREEN in s for s in accent_styles(private))
    assert any(usage._FILL_RED in s for s in accent_styles(private))
    assert any(usage._FILL_GREEN in s for s in accent_styles(work))
    assert any(usage._FILL_RED in s for s in accent_styles(work))
    # The render_work_usage convenience wrapper is exactly render_usage with the work accent.
    assert usage.render_work_usage(snap, now=_NOW).plain == work.plain
    assert accent_styles(usage.render_work_usage(snap, now=_NOW)) == accent_styles(work)


def test_render_usage_session_stale_shows_bare_pct_no_bar() -> None:
    """A snapshot older than the session window's own lifetime (5h) drops the bar for
    a bare '?%' — a colour would otherwise imply a live reading it can't support."""
    snap = usage.Usage(
        captured_at=_NOW - usage._SESSION_STALE_AFTER_SEC - 1,  # just past 5h
        five_hour=usage.Window(27, _NOW + 3600),
        seven_day=usage.Window(93, _NOW + 7 * 86400),
    )
    plain = usage.render_usage(snap, now=_NOW).plain
    assert "Session: ?%" in plain
    assert "27%" not in plain  # the stale bar's own percentage is gone, not just hidden
    # The week window is only 5h+1s old too — well under its own 7d threshold — so it
    # still renders as a live bar.
    assert "Week: Resets in " in plain
    assert "93%" in plain


def test_render_usage_week_stale_shows_bare_pct_no_bar() -> None:
    """Past 7d old, the week row goes stale too — independently of the session row."""
    snap = usage.Usage(
        captured_at=_NOW - usage._WEEK_STALE_AFTER_SEC - 1,  # just past 7d
        five_hour=usage.Window(27, _NOW + 3600),
        seven_day=usage.Window(93, _NOW + 7 * 86400),
    )
    plain = usage.render_usage(snap, now=_NOW).plain
    # 7d-old is also >5h old, so both rows go stale off the one shared captured_at clock.
    assert "Session: ?%" in plain
    assert "Week: ?%" in plain
    assert "27%" not in plain and "93%" not in plain


def test_render_usage_fresh_snapshot_keeps_bars() -> None:
    """Just under both thresholds, the bars render exactly as before (regression guard)."""
    snap = usage.Usage(
        captured_at=_NOW - usage._SESSION_STALE_AFTER_SEC + 1,
        five_hour=usage.Window(27, _NOW + 3600),
        seven_day=usage.Window(93, _NOW + 7 * 86400),
    )
    plain = usage.render_usage(snap, now=_NOW).plain
    assert "Session: Resets in " in plain and "27%" in plain
    assert "Week: Resets in " in plain and "93%" in plain
    assert "?%" not in plain


def test_render_codex_usage_never_goes_stale() -> None:
    """The Codex card shares `_render_card` but opts out of staleness (`staleness=None`
    by default) — its own source is already 'as fresh as the last token_count event'."""
    snap = usage.Usage(
        captured_at=_NOW - usage._WEEK_STALE_AFTER_SEC - 100,  # ancient by Claude's rule
        five_hour=usage.Window(27, _NOW + 3600),
        seven_day=usage.Window(93, _NOW + 7 * 86400),
    )
    plain = usage.render_codex_usage(snap, now=_NOW).plain
    assert "?%" not in plain
    assert "27%" in plain and "93%" in plain


def test_fill_for_pct_thresholds() -> None:
    """green ≤65, orange 66–85, red ≥86 — inclusive at each upper boundary."""
    assert usage._fill_for_pct(0) == usage._FILL_GREEN
    assert usage._fill_for_pct(65) == usage._FILL_GREEN
    assert usage._fill_for_pct(66) == usage._FILL_ORANGE
    assert usage._fill_for_pct(85) == usage._FILL_ORANGE
    assert usage._fill_for_pct(86) == usage._FILL_RED
    assert usage._fill_for_pct(100) == usage._FILL_RED


def test_claude_card_high_usage_is_red_while_codex_keeps_its_fill() -> None:
    """A high-usage Claude bar turns red; the Codex card keeps its flat brand fill."""
    snap = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(95, 1782320400),
        seven_day=usage.Window(97, 1782338400),
    )

    def fills(text: object) -> set[str]:
        return {str(span.style) for span in text.spans}  # type: ignore[attr-defined]

    claude = usage.render_usage(snap, now=_NOW)
    codex = usage.render_codex_usage(snap, now=_NOW)
    # Both Claude bars are ≥86% → red, and never fall back to the flat periwinkle fill.
    assert any(usage._FILL_RED in s for s in fills(claude))
    assert not any(usage._FILL_COLOR in s for s in fills(claude))
    # The Codex card is unchanged: flat _CODEX_FILL, no threshold colours.
    assert any(usage._CODEX_FILL in s for s in fills(codex))
    assert not any(usage._FILL_RED in s for s in fills(codex))


def test_statusline_capture_usage_reads_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    # The capture path uses wall-clock now, so the resets must be in the real
    # future — a window whose reset is already past is dropped as stale.
    live = {
        "five_hour": {"used_percentage": 27, "resets_at": int(time.time()) + 3600},
        "seven_day": {"used_percentage": 93, "resets_at": int(time.time()) + 7 * 86400},
    }
    payload = json.dumps({"session_id": "s1", "rate_limits": live})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    # No session row needed: --capture-usage runs before the store lookup.
    args = SimpleNamespace(session="s1", capture_usage=True)
    rc = cli.cmd_statusline(args)  # type: ignore[arg-type]
    assert rc == 0
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.used_percentage == 93


def test_read_codex_usage_maps_windows_by_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()  # reset the module-level parse cache
    _write_codex_rollout(tmp_path, _CODEX_RATE_LIMITS, name="a")
    snap = usage.read_codex_usage(now=_NOW)
    assert snap is not None
    # The 300-minute window feeds Session; the 10080-minute window feeds Week.
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 12.0
    assert snap.five_hour.resets_at == 1782320400
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 45.0
    assert snap.seven_day.resets_at == 1782893849


def test_read_codex_usage_primary_weekly_window_does_not_fill_five_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()
    weekly_only = {
        "limit_id": "codex",
        "primary": {
            "used_percent": 45.0,
            "window_minutes": 10080,
            "resets_at": 1782893849,
        },
        "secondary": None,
        "plan_type": "team",
    }
    _write_codex_rollout(tmp_path, weekly_only, name="weekly-primary")
    snap = usage.read_codex_usage(now=_NOW)
    assert snap is not None
    assert snap.five_hour is None
    assert snap.seven_day == usage.Window(used_percentage=45.0, resets_at=1782893849)
    plain = usage.render_codex_usage(snap, now=_NOW).plain
    assert "Session: —" in plain
    assert "Week: Resets in " in plain and "45%" in plain


def test_read_codex_usage_none_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()
    # No sessions dir at all, then a rollout with no rate_limits → still None.
    assert usage.read_codex_usage(now=_NOW) is None
    usage._codex_cache.clear()
    _write_codex_rollout(tmp_path, None, name="empty")
    assert usage.read_codex_usage(now=_NOW) is None


def test_read_codex_usage_prefers_newest_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()
    old = dict(
        _CODEX_RATE_LIMITS,
        primary={"used_percent": 3.0, "window_minutes": 300, "resets_at": 1782320400},
    )
    new = dict(
        _CODEX_RATE_LIMITS,
        primary={"used_percent": 88.0, "window_minutes": 300, "resets_at": 1782320400},
    )
    _write_codex_rollout(tmp_path, old, name="old", mtime=1782300000)
    _write_codex_rollout(tmp_path, new, name="new", mtime=1782301000)
    snap = usage.read_codex_usage(now=_NOW)
    assert snap is not None and snap.five_hour is not None
    assert snap.five_hour.used_percentage == 88.0  # newest rollout wins


def test_read_codex_usage_skips_windowless_newest_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The newest file is a windowless ``premium`` block (the short-exec shape); an older
    # file carries the real windows. The reader must skip the former and find the latter,
    # else the card stays stuck on "(run Codex to populate)".
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()
    _write_codex_rollout(tmp_path, _CODEX_RATE_LIMITS, name="real", mtime=1782300000)
    _write_codex_rollout(tmp_path, _CODEX_PREMIUM_NULL, name="premium", mtime=1782301000)
    snap = usage.read_codex_usage(now=_NOW)
    assert snap is not None and snap.five_hour is not None
    assert snap.five_hour.used_percentage == 12.0  # the populated (older) block wins
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 45.0


def test_read_codex_usage_skips_trailing_windowless_block_in_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single rollout where a windowless ``premium`` block is logged AFTER the populated
    # ``codex`` block — the exact real-world shape. The shared duration-keyed event parser
    # scans from the end, so it must skip the trailing null block and return the populated one.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()
    day = tmp_path / "sessions" / "2026" / "06" / "24"
    day.mkdir(parents=True, exist_ok=True)
    path = day / "rollout-2026-06-24T10-00-00-mixed.jsonl"

    def _event(rate_limits: dict) -> str:
        return json.dumps(
            {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rate_limits}}
        )

    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {}}),
                _event(_CODEX_RATE_LIMITS),  # populated, earlier
                _event(_CODEX_PREMIUM_NULL),  # windowless, trailing (newest in file)
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    snap = usage.read_codex_usage(now=_NOW)
    assert snap is not None and snap.five_hour is not None
    assert snap.five_hour.used_percentage == 12.0


def test_render_codex_usage_labels_and_color() -> None:
    snap = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(12, 1782320400),
        seven_day=usage.Window(45, 1782338400),
    )
    plain = usage.render_codex_usage(snap, now=_NOW).plain
    assert "Session: Resets in " in plain
    assert "Week: Resets in " in plain
    assert "Current session" not in plain and "Current week" not in plain
    assert "12%" in plain and "45%" in plain
    for line in plain.splitlines():
        assert len(line) == usage._CARD_INNER_WIDTH


def test_render_codex_usage_empty_placeholder() -> None:
    plain = usage.render_codex_usage(None, now=_NOW).plain
    assert "run Codex" in plain


# A realistic per-user enhanced-billing usage payload (verified shape): the current
# month carries Copilot "AI Credits" line-items (net 0 => covered by the subscription).
_COPILOT_API = {
    "usageItems": [
        {
            "product": "copilot",
            "sku": "Copilot AI Credits",
            "quantity": 4.0,
            "unitType": "AICredits",
            "grossAmount": 0.04,
            "netAmount": 0.0,
        },
        {
            "product": "copilot",
            "sku": "Copilot AI Credits",
            "quantity": 2.5,
            "unitType": "AICredits",
            "grossAmount": 0.025,
            "netAmount": 0.0,
        },
        {
            "product": "actions",
            "sku": "Actions Linux",
            "quantity": 99.0,
            "unitType": "Minutes",
            "grossAmount": 0.6,
            "netAmount": 0.6,
        },
    ]
}


def test_summarize_copilot_sums_only_copilot_rows() -> None:
    items = [i for i in _COPILOT_API["usageItems"] if i["product"] == "copilot"]
    snap = usage._summarize_copilot(items, 2026, 6, _NOW)
    assert snap.sku == "AI Credits"  # "Copilot " prefix stripped
    assert snap.unit == "AI credits"
    assert snap.quantity == pytest.approx(6.5)  # only copilot rows, not actions
    assert snap.gross == pytest.approx(0.065)
    assert snap.net == 0.0  # covered


def test_summarize_copilot_empty_is_zero() -> None:
    snap = usage._summarize_copilot([], 2026, 6, _NOW)
    assert snap.quantity == 0.0 and snap.sku == "" and snap.gross == 0.0


def test_summarize_copilot_headline_is_largest_sku() -> None:
    # A transition month with two SKUs: headline = the larger-count one; cost sums both.
    items = [
        {
            "sku": "Copilot Premium Request",
            "quantity": 300.0,
            "unitType": "Requests",
            "grossAmount": 12.0,
            "netAmount": 0.0,
        },
        {
            "sku": "Copilot AI Credits",
            "quantity": 5.0,
            "unitType": "AICredits",
            "grossAmount": 0.05,
            "netAmount": 0.0,
        },
    ]
    snap = usage._summarize_copilot(items, 2026, 6, _NOW)
    assert snap.sku == "Premium Request" and snap.unit == "premium requests"
    assert snap.quantity == 300.0
    assert snap.gross == pytest.approx(12.05)  # both SKUs in the cost line


def test_copilot_usage_roundtrip_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    snap = usage.CopilotUsage(
        captured_at=_NOW,
        year=2026,
        month=6,
        sku="AI Credits",
        unit="AI credits",
        quantity=8.84,
        gross=0.0884,
        net=0.0,
        premium_used=30.0,
        premium_quota=300,
        premium_reset_at=_NOW + 4 * 86400,  # 4 days out
        credit_quota=300,  # explicit so the bar math is independent of the default
    )
    usage._write_copilot_usage(snap)
    back = usage.read_copilot_usage()
    assert back is not None and back.quantity == pytest.approx(8.84)
    assert back.premium_used == pytest.approx(30.0) and back.premium_quota == 300
    assert back.credit_quota == 300  # round-trips
    plain = usage.render_copilot_usage(back, now=_NOW).plain
    # AI-Credit seat: premium requests are retired (that meter reads 0), so the bar is
    # drawn from credits ÷ credit_quota, embossing the live credit count. The
    # "Premium requests" title and the standalone AI-credit/cost line stay gone.
    assert "Premium requests" not in plain and "covered" not in plain
    assert "3%" in plain  # 8.84 / 300 ≈ 3%
    assert "8.8/300cr" in plain  # credits used *and* the denominator embossed in the bar
    assert "Resets in 4d" in plain  # reset embossed in the bar
    for line in plain.splitlines():
        assert len(line) == usage._CARD_INNER_WIDTH

    # Fallback: a premium-request month (the head SKU is Requests, not AI Credits)
    # still draws the premium-request bar.
    pr = usage.CopilotUsage(
        captured_at=_NOW,
        year=2026,
        month=3,
        sku="Premium Request",
        unit="premium requests",
        quantity=300.0,
        gross=12.0,
        net=0.0,
        premium_used=300.0,
        premium_quota=300,
        premium_reset_at=_NOW + 4 * 86400,
        credit_quota=300,
    )
    pr_plain = usage.render_copilot_usage(pr, now=_NOW).plain
    assert "100%" in pr_plain  # 300 / 300 premium requests
    assert "cr" not in pr_plain  # no AI-credit emboss in premium-request mode


def test_copilot_usage_render_empty_placeholder() -> None:
    plain = usage.render_copilot_usage(None, now=_NOW).plain
    assert "copilot-usage" in plain


def test_copilot_usage_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert usage.copilot_usage_stale(900, now=_NOW) is True  # no cache yet
    usage._write_copilot_usage(
        usage.CopilotUsage(_NOW, 2026, 6, "AI Credits", "AI credits", 1.0, 0.01, 0.0)
    )
    fresh_now = int(Path(usage._copilot_usage_path()).stat().st_mtime) + 10
    assert usage.copilot_usage_stale(900, now=fresh_now) is False
    assert usage.copilot_usage_stale(900, now=fresh_now + 1000) is True


# The seat's own quota endpoint — the only authoritative denominator. A trimmed copy of
# `/copilot_internal/user` for a faculty/individual seat that has burnt its month.
_COPILOT_SEAT = {
    "copilot_plan": "individual",
    "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
    "quota_snapshots": {
        "chat": {"unlimited": True, "entitlement": 0, "credits_used": 0},
        "premium_interactions": {
            "unlimited": False,
            "entitlement": 1500,
            "credits_used": 1505,
            "remaining": -6,
            "percent_remaining": 0.0,
        },
    },
}


def _fake_gh(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """Stub `subprocess.run` inside usage.py to answer one `gh api` call with *stdout*."""

    def run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(usage.subprocess, "run", run)


def test_fetch_copilot_quota_reads_seat_entitlement(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(monkeypatch, json.dumps(_COPILOT_SEAT))
    assert usage._fetch_copilot_quota("gh") == (1500, 1505.0)


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps({}),  # no quota_snapshots
        json.dumps({"quota_snapshots": {"premium_interactions": {"unlimited": True}}}),
        json.dumps({"quota_snapshots": {"premium_interactions": {"entitlement": 0}}}),
    ],
)
def test_fetch_copilot_quota_degrades_to_none(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """Unusable answers keep the configured fallback rather than inventing a denominator."""
    _fake_gh(monkeypatch, payload)
    assert usage._fetch_copilot_quota("gh") is None


def test_copilot_render_uses_live_meter_and_real_quota() -> None:
    """A consumed seat reads 100%, not 50% against a guessed budget (the 2026-08 bug).

    `quantity` (billing endpoint, up to a day stale) is 1497.7 and the guessed budget was
    3000 — which drew a half-full bar on a seat that was actually over its 1500 credits.
    """
    snap = usage.CopilotUsage(
        captured_at=_NOW,
        year=2026,
        month=8,
        sku="AI Credits",
        unit="AI credits",
        quantity=1497.693635,
        gross=14.98,
        net=0.0,
        premium_used=0.0,
        premium_quota=300,
        premium_reset_at=_NOW + 3 * 86400,
        credit_quota=1500,
        credits_used=1505.0,
        quota_source="api",
    )
    plain = usage.render_copilot_usage(snap, now=_NOW).plain
    assert "100%" in plain  # 1505 / 1500 — over quota, clamped bar, honest percentage
    assert "1505/1500cr" in plain  # numerator and denominator both on the card
    for line in plain.splitlines():
        assert len(line) == usage._CARD_INNER_WIDTH


def test_copilot_render_falls_back_to_billing_quantity() -> None:
    """Without the live meter (`credits_used == 0`) the billing quantity still draws."""
    snap = usage.CopilotUsage(
        captured_at=_NOW,
        year=2026,
        month=8,
        sku="AI Credits",
        unit="AI credits",
        quantity=750.0,
        gross=7.5,
        net=0.0,
        premium_reset_at=_NOW + 3 * 86400,
        credit_quota=1500,
    )
    plain = usage.render_copilot_usage(snap, now=_NOW).plain
    assert "50%" in plain and "750/1500cr" in plain


def test_has_active_work_matches_status_enum() -> None:
    from command_center.models import Status

    # The raw-string set stays in lock-step with the two "actively working" statuses.
    assert usage._ACTIVE_STATUS_VALUES == {Status.WORKING.value, Status.SNOOZED.value}
    assert usage.has_active_work(["idle", "working", "parked"]) is True
    assert usage.has_active_work(["idle", "snoozed"]) is True
    assert usage.has_active_work(["idle", "parked", "done", "waiting_input"]) is False
    assert usage.has_active_work([]) is False


# --- Claude OAuth usage endpoint (Fable window + authoritative fetch) -----------

# A real OAuth /usage response sample (verified live, private account): top-level
# five_hour/seven_day carry `utilization` + ISO `resets_at`; the Fable weekly window is
# the limits[] entry with group=="weekly" and scope.model.display_name=="Fable".
_OAUTH_FABLE_LIMIT: dict = {
    "kind": "weekly_scoped",
    "group": "weekly",
    "percent": 42,
    "resets_at": "2026-07-15T14:59:59+00:00",
    "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
}
_OAUTH_USAGE: dict = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-07-10T10:49:59.649242+00:00"},
    "seven_day": {"utilization": 3.0, "resets_at": "2026-07-11T14:59:59.649292+00:00"},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 3,
            "resets_at": "2026-07-10T10:49:59+00:00",
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 3,
            "resets_at": "2026-07-11T14:59:59+00:00",
        },
        _OAUTH_FABLE_LIMIT,
    ],
}


def _seed_snapshot(account: str, payload: dict) -> None:
    """Write a raw usage-cache JSON (with the account's config_dir_hash) directly."""
    path = usage._usage_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload.setdefault("config_dir_hash", usage._account_hash(account))
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_parse_oauth_usage_sample() -> None:
    snap = usage._parse_oauth_usage(_OAUTH_USAGE, _NOW)
    assert snap is not None
    assert snap.captured_at == _NOW
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 3.0
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 3.0
    assert snap.fable_week is not None
    assert snap.fable_week.used_percentage == 42.0
    # ISO resets_at parsed to int epoch (2026-07-15T14:59:59Z).
    assert snap.fable_week.resets_at == int(
        datetime(2026, 7, 15, 14, 59, 59, tzinfo=UTC).timestamp()
    )


def test_parse_oauth_usage_malformed_is_none() -> None:
    assert usage._parse_oauth_usage("not a dict", _NOW) is None
    assert usage._parse_oauth_usage({"limits": []}, _NOW) is None  # no main window
    # A body with only a Fable window (no main windows) is still None.
    assert (
        usage._parse_oauth_usage(
            {"limits": [_OAUTH_USAGE["limits"][2]]},
            _NOW,
        )
        is None
    )


def test_parse_oauth_usage_no_fable_window() -> None:
    data = {
        "five_hour": {"utilization": 5.0, "resets_at": "2026-07-10T10:49:59+00:00"},
        "seven_day": {"utilization": 6.0, "resets_at": "2026-07-11T14:59:59+00:00"},
        "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 6, "resets_at": "x"}],
    }
    snap = usage._parse_oauth_usage(data, _NOW)
    assert snap is not None and snap.fable_week is None


def test_fable_and_oauth_fetched_at_roundtrip_and_preserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statusline merge write preserves the OAuth-only fable_week + oauth_fetched_at."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    reset_week = _NOW + 5 * 86400
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "five_hour": {"used_percentage": 10, "resets_at": _NOW + 3600},
            "seven_day": {"used_percentage": 20, "resets_at": reset_week},
            "fable_week": {"used_percentage": 42, "resets_at": _NOW + 9 * 86400},
            "oauth_fetched_at": _NOW,
        },
    )
    # read_usage exposes fable_week; oauth_fetched_at has its own reader.
    snap = usage.read_usage()
    assert snap is not None and snap.fable_week is not None
    assert snap.fable_week.used_percentage == 42
    assert usage.oauth_fetched_at() == _NOW

    # A statusline write with the SAME windows (higher pct) must NOT drop fable/oauth.
    statusline = {
        "five_hour": {"used_percentage": 12, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 25, "resets_at": reset_week},
    }
    assert usage.write_usage(statusline, now=_NOW + 5) is True
    back = usage.read_usage()
    assert back is not None and back.fable_week is not None
    assert back.fable_week.used_percentage == 42  # preserved verbatim
    assert back.seven_day is not None and back.seven_day.used_percentage == 25  # merged up
    assert usage.oauth_fetched_at() == _NOW  # preserved verbatim


def test_authority_guard_rejects_later_reset_while_oauth_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh oauth stamp + live stored window + incoming LATER reset → stored kept."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    stored_reset = _NOW + 5 * 86400
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "seven_day": {"used_percentage": 20, "resets_at": stored_reset},
            "oauth_fetched_at": _NOW,  # fresh (< _OAUTH_AUTHORITY_SEC)
        },
    )
    # A long-idle session replays a pre-rebase (further-future) boundary.
    incoming = {"seven_day": {"used_percentage": 99, "resets_at": _NOW + 9 * 86400}}
    assert usage.write_usage(incoming, now=_NOW + 10) is True
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.resets_at == stored_reset  # stale later boundary rejected
    assert snap.seven_day.used_percentage == 20


def test_authority_guard_rejects_stale_payload_same_reset_higher_pct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh oauth stamp + a STALE payload (dead own 5h window) → same-reset 84% replay rejected.

    The Fable-5 rollout recalibrated the weekly percentage DOWN at the SAME boundary
    (84% → 3%); a >5h-idle session replaying 84% must not beat the fresh OAuth 3%.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    reset_week = _NOW + 5 * 86400
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "seven_day": {"used_percentage": 3, "resets_at": reset_week},
            "oauth_fetched_at": _NOW,  # fresh
        },
    )
    incoming = {
        "five_hour": {"used_percentage": 44, "resets_at": _NOW - 60000},  # dead → stale payload
        "seven_day": {"used_percentage": 84, "resets_at": reset_week},  # same boundary, replay
    }
    assert usage.write_usage(incoming, now=_NOW + 10) is True
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.used_percentage == 3  # stale replay rejected
    assert snap.five_hour is None  # the dead incoming window never survives anyway


def test_authority_guard_live_payload_same_reset_rise_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACTIVE session's same-reset increase still wins under fresh authority (fast path)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    reset_week = _NOW + 5 * 86400
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "five_hour": {"used_percentage": 3, "resets_at": _NOW + 3600},
            "seven_day": {"used_percentage": 3, "resets_at": reset_week},
            "oauth_fetched_at": _NOW,  # fresh
        },
    )
    incoming = {
        "five_hour": {"used_percentage": 5, "resets_at": _NOW + 3600},  # live → active payload
        "seven_day": {"used_percentage": 4, "resets_at": reset_week},
    }
    assert usage.write_usage(incoming, now=_NOW + 10) is True
    snap = usage.read_usage()
    assert snap is not None and snap.five_hour is not None and snap.seven_day is not None
    assert snap.five_hour.used_percentage == 5  # fast path preserved
    assert snap.seven_day.used_percentage == 4


def test_authority_guard_stale_oauth_adopts_later_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale oauth stamp (> _OAUTH_AUTHORITY_SEC) → the old later-reset-wins behaviour."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    stored_reset = _NOW + 5 * 86400
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "seven_day": {"used_percentage": 20, "resets_at": stored_reset},
            "oauth_fetched_at": _NOW - usage._OAUTH_AUTHORITY_SEC - 100,  # stale
        },
    )
    incoming = {"seven_day": {"used_percentage": 5, "resets_at": _NOW + 9 * 86400}}
    assert usage.write_usage(incoming, now=_NOW + 10) is True
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.resets_at == _NOW + 9 * 86400  # later reset adopted


def test_authority_guard_dead_stored_window_adopts_incoming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead stored window (reset already passed) never blocks the incoming one."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "seven_day": {"used_percentage": 90, "resets_at": _NOW - 10},  # dead
            "oauth_fetched_at": _NOW,  # fresh, but the stored window is dead
        },
    )
    incoming = {"seven_day": {"used_percentage": 3, "resets_at": _NOW + 9 * 86400}}
    assert usage.write_usage(incoming, now=_NOW + 10) is True
    snap = usage.read_usage()
    assert snap is not None and snap.seven_day is not None
    assert snap.seven_day.resets_at == _NOW + 9 * 86400
    assert snap.seven_day.used_percentage == 3


def test_claude_usage_stale_keyed_on_oauth_fetched_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert usage.claude_usage_stale("private", 600, now=_NOW) is True  # no cache yet
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "five_hour": {"used_percentage": 3, "resets_at": _NOW + 3600},
            "oauth_fetched_at": _NOW,
        },
    )
    assert usage.claude_usage_stale("private", 600, now=_NOW + 500) is False
    assert usage.claude_usage_stale("private", 600, now=_NOW + 601) is True
    # A statusline write bumps captured_at but NOT oauth_fetched_at → still stale.
    usage.write_usage(
        {"five_hour": {"used_percentage": 4, "resets_at": _NOW + 3600}}, now=_NOW + 601
    )
    assert usage.claude_usage_stale("private", 600, now=_NOW + 602) is True


def test_render_shows_fable_row_iff_present() -> None:
    with_fable = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(3, _NOW + 3600),
        seven_day=usage.Window(3, _NOW + 5 * 86400),
        fable_week=usage.Window(42, _NOW + 9 * 86400),
    )
    plain = usage.render_usage(with_fable, now=_NOW).plain
    assert "Fable: Resets in " in plain
    assert "42%" in plain
    for line in plain.splitlines():
        assert len(line) == usage._CARD_INNER_WIDTH

    without = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(3, _NOW + 3600),
        seven_day=usage.Window(3, _NOW + 5 * 86400),
    )
    assert "Fable:" not in usage.render_usage(without, now=_NOW).plain
    # The Codex card shares _render_card and must stay two rows.
    assert "Fable:" not in usage.render_codex_usage(without, now=_NOW).plain


def _oauth_hdrs(retry_after: str) -> Message:
    """A minimal headers object with a ``retry-after`` field (like HTTPError.headers)."""
    hdrs = Message()
    hdrs["retry-after"] = retry_after
    return hdrs


class _FakeOAuthResp:
    """A minimal ``urlopen()`` context-manager stand-in returning a fixed body."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeOAuthResp:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_get_oauth_usage_body_large_retry_after_returns_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 with a large Retry-After surfaces ``(None, retry_after)`` to the caller."""

    def _raise(*_a: object, **_k: object) -> object:
        raise usage.urllib.error.HTTPError(
            usage._OAUTH_USAGE_URL, 429, "Too Many Requests", _oauth_hdrs("3357"), None
        )

    monkeypatch.setattr(usage.urllib.request, "urlopen", _raise)
    body, retry = usage._get_oauth_usage_body("tok")
    assert body is None
    assert retry == 3357


def test_get_oauth_usage_body_small_retry_after_sleeps_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small (≤10 s) Retry-After is slept off and retried ONCE → ``(body, 0)``."""
    calls = {"n": 0}

    def _urlopen(*_a: object, **_k: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise usage.urllib.error.HTTPError(
                usage._OAUTH_USAGE_URL, 429, "rate", _oauth_hdrs("2"), None
            )
        return _FakeOAuthResp(b'{"ok": true}')

    monkeypatch.setattr(usage.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(usage.time, "sleep", lambda _s: None)  # never actually sleep
    body, retry = usage._get_oauth_usage_body("tok")
    assert body == '{"ok": true}'
    assert retry == 0
    assert calls["n"] == 2  # retried once after the small backoff


def test_fetch_claude_usage_429_backoff_persists_and_preserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large-Retry-After 429 returns None and stamps oauth_backoff_until, keeping fields."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "five_hour": {"used_percentage": 10, "resets_at": _NOW + 3600},
            "seven_day": {"used_percentage": 20, "resets_at": _NOW + 5 * 86400},
            "fable_week": {"used_percentage": 42, "resets_at": _NOW + 9 * 86400},
            "oauth_fetched_at": _NOW,
        },
    )
    monkeypatch.setattr(usage, "_keychain_oauth_token", lambda _account: "tok")
    monkeypatch.setattr(usage, "_get_oauth_usage_body", lambda _token: (None, 3357))
    assert usage.fetch_claude_usage("private", now=_NOW + 100) is None
    data = json.loads(usage._usage_path("private").read_text(encoding="utf-8"))
    # The backoff is now + retry_after (uncapped here), and every other field survived.
    assert data["oauth_backoff_until"] == _NOW + 100 + 3357
    assert data["five_hour"]["used_percentage"] == 10
    assert data["seven_day"]["used_percentage"] == 20
    assert data["fable_week"]["used_percentage"] == 42
    assert data["oauth_fetched_at"] == _NOW
    assert data["captured_at"] == _NOW
    assert usage.oauth_backoff_until() == _NOW + 100 + 3357


def test_fetch_claude_usage_backoff_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge Retry-After is capped at now + _OAUTH_BACKOFF_CAP_SEC (writes a fresh cache)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(usage, "_keychain_oauth_token", lambda _account: "tok")
    monkeypatch.setattr(usage, "_get_oauth_usage_body", lambda _token: (None, 999999))
    assert usage.fetch_claude_usage("private", now=_NOW) is None
    assert usage.oauth_backoff_until() == _NOW + usage._OAUTH_BACKOFF_CAP_SEC
    assert usage._OAUTH_BACKOFF_CAP_SEC == 7200


def test_fetch_claude_usage_success_clears_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful fetch writes a payload with no backoff key → the backoff is cleared."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _seed_snapshot("private", {"captured_at": _NOW, "oauth_backoff_until": _NOW + 3600})
    monkeypatch.setattr(usage, "_keychain_oauth_token", lambda _account: "tok")
    monkeypatch.setattr(
        usage, "_get_oauth_usage_body", lambda _token: (json.dumps(_OAUTH_USAGE), 0)
    )
    snap = usage.fetch_claude_usage("private", now=_NOW + 50)
    assert snap is not None
    assert snap.oauth_fetched_at == _NOW + 50  # the snapshot carries the fetch time
    assert usage.oauth_backoff_until() == 0  # cleared by the authoritative replace


def test_claude_usage_stale_false_during_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future backoff suppresses staleness even when the last fetch is ancient."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "five_hour": {"used_percentage": 3, "resets_at": _NOW + 3600},
            "oauth_fetched_at": _NOW - 100000,  # ancient — normally very stale
            "oauth_backoff_until": _NOW + 3600,
        },
    )
    assert usage.claude_usage_stale("private", 600, now=_NOW) is False  # backoff wins
    # Once the backoff passes, the ancient fetch makes it stale again.
    assert usage.claude_usage_stale("private", 600, now=_NOW + 3601) is True


def test_write_usage_preserves_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A statusline merge write preserves a persisted oauth_backoff_until verbatim."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    reset_week = _NOW + 5 * 86400
    _seed_snapshot(
        "private",
        {
            "captured_at": _NOW,
            "five_hour": {"used_percentage": 10, "resets_at": _NOW + 3600},
            "seven_day": {"used_percentage": 20, "resets_at": reset_week},
            "oauth_backoff_until": _NOW + 3600,
        },
    )
    statusline = {
        "five_hour": {"used_percentage": 12, "resets_at": _NOW + 3600},
        "seven_day": {"used_percentage": 25, "resets_at": reset_week},
    }
    assert usage.write_usage(statusline, now=_NOW + 5) is True
    assert usage.oauth_backoff_until() == _NOW + 3600  # survived the merge


def test_render_fable_stale_marks_label() -> None:
    """A >1h-old OAuth fetch embosses ``Fable: stale <age>``; a fresh one shows Resets."""
    stale = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(3, _NOW + 3600),
        seven_day=usage.Window(3, _NOW + 5 * 86400),
        fable_week=usage.Window(42, _NOW + 9 * 86400),
        oauth_fetched_at=_NOW - 2 * 3600,  # 2h old
    )
    stale_plain = usage.render_usage(stale, now=_NOW).plain
    assert "Fable: stale" in stale_plain
    assert "Fable: Resets" not in stale_plain
    for line in stale_plain.splitlines():
        assert len(line) == usage._CARD_INNER_WIDTH

    fresh = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(3, _NOW + 3600),
        seven_day=usage.Window(3, _NOW + 5 * 86400),
        fable_week=usage.Window(42, _NOW + 9 * 86400),
        oauth_fetched_at=_NOW,  # just fetched
    )
    fresh_plain = usage.render_usage(fresh, now=_NOW).plain
    assert "Fable: Resets" in fresh_plain
    assert "Fable: stale" not in fresh_plain


def test_adaptive_interval_picks_active_only_when_shorter_and_working() -> None:
    # Working + a shorter active interval → the active interval wins.
    assert usage.adaptive_interval(900, 300, active=True) == 300
    # Not working → always the idle interval, regardless of the active value.
    assert usage.adaptive_interval(900, 300, active=False) == 900
    # Guard rails: active can only ever make refreshes MORE frequent, never less.
    assert usage.adaptive_interval(900, 0, active=True) == 900  # 0 disables the speed-up
    assert usage.adaptive_interval(900, 900, active=True) == 900  # not shorter → ignored
    assert usage.adaptive_interval(900, 1200, active=True) == 900  # larger → ignored
    assert usage.adaptive_interval(900, -5, active=True) == 900  # negative → ignored
    # Floats (the render cadence) work the same way.
    assert usage.adaptive_interval(5.0, 2.0, active=True) == 2.0
    assert usage.adaptive_interval(5.0, 2.0, active=False) == 5.0


# --------------------------- Codex refusal → 100% ---------------------------- #
# The refusing shape actually recorded on 2026-08-28: a windowless ``premium`` block
# whose ``rate_limit_reached_type`` names the reason. Note ``has_credits: False`` is
# ALSO true of the healthy blocks above — it is not what marks a refusal.
_CODEX_PREMIUM_DEPLETED = dict(
    _CODEX_PREMIUM_NULL, rate_limit_reached_type="workspace_owner_credits_depleted"
)


def _blocked_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: dict
) -> usage.Usage | None:
    """A refusal logged after a healthy reading, as the real sequence goes."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache.clear()
    _write_codex_rollout(tmp_path, healthy, name="healthy", mtime=1782300000)
    _write_codex_rollout(tmp_path, _CODEX_PREMIUM_DEPLETED, name="refused", mtime=1782301000)
    return usage.read_codex_usage(now=_NOW)


def test_refusal_pins_the_filled_window_to_100(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """81% pre-limit must be reported as 100%, or consumers see phantom headroom."""
    healthy = dict(
        _CODEX_RATE_LIMITS,
        primary={"used_percent": 81.0, "window_minutes": 300, "resets_at": 1782320400},
        secondary={"used_percent": 60.0, "window_minutes": 10080, "resets_at": 1782893849},
    )
    snap = _blocked_snapshot(tmp_path, monkeypatch, healthy)
    assert snap is not None and snap.blocked
    assert snap.blocked_reason == "included usage limit reached (no credit overflow)"
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 100.0
    assert snap.five_hour.resets_at == 1782320400  # reset preserved: when access returns
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 60.0


def test_refusal_pins_the_most_consumed_window_not_always_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weekly exhaustion must pin the WEEKLY window, not the barely-used session."""
    healthy = dict(
        _CODEX_RATE_LIMITS,
        primary={"used_percent": 30.0, "window_minutes": 300, "resets_at": 1782320400},
        secondary={"used_percent": 95.0, "window_minutes": 10080, "resets_at": 1782893849},
    )
    snap = _blocked_snapshot(tmp_path, monkeypatch, healthy)
    assert snap is not None
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 100.0
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 30.0


def test_refusal_never_pins_a_window_whose_reset_already_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired window is not live, so it cannot be the one that is full."""
    healthy = dict(
        _CODEX_RATE_LIMITS,
        primary={"used_percent": 99.0, "window_minutes": 300, "resets_at": _NOW - 60},
        secondary={"used_percent": 40.0, "window_minutes": 10080, "resets_at": 1782893849},
    )
    snap = _blocked_snapshot(tmp_path, monkeypatch, healthy)
    assert snap is not None
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 99.0
    assert snap.seven_day is not None and snap.seven_day.used_percentage == 100.0


def test_quota_reports_codex_blocked_with_the_filled_window_and_its_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ccc quota -p codex`` must exit blocked and name when access returns."""
    from command_center import quota

    healthy = dict(
        _CODEX_RATE_LIMITS,
        primary={"used_percent": 81.0, "window_minutes": 300, "resets_at": 1782320400},
        secondary={"used_percent": 60.0, "window_minutes": 10080, "resets_at": 1782893849},
    )
    _blocked_snapshot(tmp_path, monkeypatch, healthy)
    usage._codex_cache.clear()
    prov = quota._codex_quota(_NOW, {})
    assert prov.state == quota.BLOCKED
    assert prov.blocked_by == "five_hour"
    assert prov.resets_at == 1782320400
    assert prov.windows["five_hour"].used_pct == 100.0


# ------------------- live Codex usage (chatgpt.com wham/usage) --------------------- #
# The verified HTTP-200 payload from a blocked account, trimmed to the fields the reader
# touches. Windows are identified by ``limit_window_seconds`` (18000 = the 5h session,
# 604800 = the week), NEVER by primary/secondary position; the refusal reason lives in the
# top-level ``rate_limit_reached_type``.
_WHAM_BLOCKED = {
    "user_id": "user-0123",
    "account_id": "acct-test",
    "email": "alice.example@example.com",
    "plan_type": "team",
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {
            "used_percent": 100,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 705,
            "reset_at": 1788095849,
        },
        "secondary_window": {
            "used_percent": 23,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 548497,
            "reset_at": 1788643641,
        },
    },
    "code_review_rate_limit": None,
    "additional_rate_limits": None,
    "credits": {
        "has_credits": False,
        "unlimited": False,
        "overage_limit_reached": False,
        "balance": None,
    },
    "spend_control": {"reached": False, "individual_limit": None},
    "rate_limit_reached_type": {"type": "workspace_owner_credits_depleted", "details": None},
    "rate_limit_upsell": None,
    "promo": None,
}
_LIVE_NOW = 1788095000  # ~14 min before the 5h window's reset in the payload above


def _write_codex_auth(
    home: Path,
    *,
    email: str | None = "alice.example@example.com",
    auth_mode: str | None = "chatgpt",
    active_until: str | None = None,
) -> None:
    """Write a fabricated ``$CODEX_HOME/auth.json`` (never the developer's real one)."""
    claims: dict[str, object] = {"sub": "user-0123"}
    if email is not None:
        claims["email"] = email
    if active_until is not None:
        claims["https://api.openai.com/auth"] = {"chatgpt_subscription_active_until": active_until}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    data: dict[str, object] = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": f"eyJhbGciOiJub25lIn0.{payload}.signature",
            "access_token": "access-token-xyz",
            "refresh_token": "refresh-token-xyz",
            "account_id": "acct-1",
        },
        "last_refresh": "2026-08-30T09:00:00.000Z",
    }
    if auth_mode is not None:
        data["auth_mode"] = auth_mode
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(data), encoding="utf-8")
    usage._codex_email_cache.clear()


def _seed_codex_live(
    home: Path,
    *,
    captured_at: int,
    five: tuple[float, int] | None = (100.0, 1788095849),
    seven: tuple[float, int] | None = (23.0, 1788643641),
    blocked_reason: str = "",
    email: str = "alice.example@example.com",
) -> Path:
    """Write a live-usage cache file for *home* directly (no network)."""
    path = usage._codex_usage_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "captured_at": captured_at,
                "fetched_at": captured_at,
                "home": str(home.expanduser().resolve()),
                "email": email,
                "plan_type": "team",
                "five_hour": ({"used_percentage": five[0], "resets_at": five[1]} if five else None),
                "seven_day": (
                    {"used_percentage": seven[0], "resets_at": seven[1]} if seven else None
                ),
                "blocked_reason": blocked_reason,
                "blocked_at": captured_at if blocked_reason else 0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _fake_wham(monkeypatch: pytest.MonkeyPatch, payload: dict) -> dict:
    """Point ``urlopen`` at *payload*; returns a dict recording the request that was made."""
    seen: dict[str, object] = {}

    def _urlopen(req: object, timeout: float | None = None) -> _FakeOAuthResp:
        seen["url"] = req.full_url  # type: ignore[attr-defined]
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}  # type: ignore[attr-defined]
        seen["timeout"] = timeout
        return _FakeOAuthResp(json.dumps(payload).encode())

    monkeypatch.setattr(usage.urllib.request, "urlopen", _urlopen)
    return seen


def test_abbrev_email_shortens_only_the_local_part() -> None:
    """The domain is what tells two accounts apart, so only the local part is squeezed."""
    assert usage.abbrev_email("alice.example@example.com") == "alice…@example.com"
    assert usage.abbrev_email("openai.account@example.org") == "openai…@example.org"
    assert usage.abbrev_email("developer@example.org") == "de…er@example.org"  # >5, undotted
    assert usage.abbrev_email("bob@x.org") == "bob@x.org"  # ≤5 chars: left alone
    assert usage.abbrev_email("not-an-address") == "not-an-address"  # no @: unchanged


def test_codex_account_email_reads_the_id_token_jwt(tmp_path: Path) -> None:
    """The card title's account comes from auth.json's id_token payload (no verification)."""
    home = tmp_path / "codex"
    _write_codex_auth(home)
    assert usage.codex_account_email(home) == "alice.example@example.com"
    assert usage.codex_card_title(home, "t3") == "Codex alice…@example.com / t3"
    # No auth.json (and no home at all) degrade to the plain title instead of raising.
    assert usage.codex_account_email(tmp_path / "absent") is None
    assert usage.codex_card_title(tmp_path / "absent", "t5") == "Codex / t5"
    assert usage.codex_card_title(None, "t5") == "Codex / t5"


def test_abbrev_domain_gives_up_only_what_the_budget_demands() -> None:
    """The domain shrinks to its budget and no further; the TLD tail always survives."""
    assert usage.abbrev_domain("datascience.example", 19) == "datascience.example"  # already fits
    assert usage.abbrev_domain("datascience.example", 18) == "datascience.exa…le"  # 1 over
    assert usage.abbrev_domain("datascience.example", 13) == "datascienc…le"
    assert usage.abbrev_domain("example.com", 5) == "ex…om"  # the floor: two head characters
    assert usage.abbrev_domain("example.com", 1) == "ex…om"  # never tighter, card grows instead
    assert usage.abbrev_email("openai.account@example.org", domain_budget=6) == "openai…@exa…rg"
    assert usage.abbrev_email("openai.account@example.org") == "openai…@example.org"


def test_codex_card_title_squeezes_the_domain_only_on_overflow(tmp_path: Path) -> None:
    """The domain survives whole until the ``-> D.M`` suffix pushes the title over budget.

    The domain is what tells two Codex cards apart, so it is the LAST thing given up —
    and only to keep the card at its 38-column CSS min-width (32 cells of title).
    """
    short = tmp_path / "codex-short"
    _write_codex_auth(short, email="alice.example@ex.com")
    # 25 cells with no date and 33 with one, so the domain survives only in the first.
    assert usage.codex_card_title(short, "t5") == "Codex alice…@ex.com / t5"
    assert usage.codex_card_title(short, "t5", " -> 18.9") == "Codex alice…@ex.com / t5 -> 18.9"
    assert usage.cell_len("Codex alice…@ex.com / t5") <= usage._CARD_TITLE_BUDGET
    # A long domain overflows on its own — and gives up exactly the overflow, no more,
    # so a date costing eight more cells eats further into the SAME domain.
    home = tmp_path / "codex"
    _write_codex_auth(home, email="openai.account@datascience.example")
    assert usage.codex_card_title(home, "t3") == "Codex openai…@datascienc…le / t3"
    assert usage.codex_card_title(home, "t3", " -> 30.9") == ("Codex openai…@da…le / t3 -> 30.9")
    for title in (
        usage.codex_card_title(home, "t3"),
        usage.codex_card_title(home, "t3", " -> 30.9"),
    ):
        assert usage.cell_len(title) <= usage._CARD_TITLE_BUDGET
    # No account at all: the suffix still lands on the degraded title.
    assert usage.codex_card_title(None, "t5", " -> 18.9") == "Codex / t5 -> 18.9"


def test_next_anniversary_derives_the_monthly_billing_day() -> None:
    """The renewal day is the created day-of-month, at or after today, clamped short months."""
    created = "2025-09-18T09:53:15.392387Z"
    # Before this month's 18th → this month's 18th; on it → today; after → next month's.
    assert usage.next_anniversary(created, date(2026, 9, 1)) == date(2026, 9, 18)
    assert usage.next_anniversary(created, date(2026, 9, 18)) == date(2026, 9, 18)
    assert usage.next_anniversary(created, date(2026, 9, 19)) == date(2026, 10, 18)
    # December rolls the YEAR over, not just the month.
    assert usage.next_anniversary(created, date(2026, 12, 19)) == date(2027, 1, 18)
    # A 31st subscription clamps to the last day a short month actually has.
    assert usage.next_anniversary("2025-01-31T00:00:00Z", date(2026, 2, 1)) == date(2026, 2, 28)
    assert usage.next_anniversary("not-a-date", date(2026, 9, 1)) is None
    assert usage.next_anniversary("", date(2026, 9, 1)) is None


def test_format_end_date_is_swiss_and_flags_a_lapsed_date() -> None:
    """``D.M`` with no padding; a date already past earns a ``!`` so a pinned one can't rot."""
    assert usage.format_end_date(date(2026, 9, 30), date(2026, 9, 1)) == "30.9"
    assert usage.format_end_date(date(2026, 9, 1), date(2026, 9, 1)) == "1.9"  # today: not past
    assert usage.format_end_date(date(2026, 8, 30), date(2026, 9, 1)) == "30.8!"


def test_subscription_suffix_pins_derives_and_stays_silent(tmp_path: Path) -> None:
    """A pinned date wins, ``auto`` derives, and anything unresolvable renders nothing."""
    today = date(2026, 9, 1)
    home = tmp_path / "codex"
    _write_codex_auth(home, active_until="2026-11-21T09:10:48+00:00")

    pinned = {"codex_private": "2026-09-30"}
    assert usage.subscription_suffix("codex_private", pinned, home, today) == " -> 30.9"
    # A card with no entry costs nothing — the default for all four.
    assert usage.subscription_suffix("codex", pinned, home, today) == ""
    assert usage.subscription_suffix("codex", {}, home, today) == ""
    # `auto` on a Codex card reads the id_token's own subscription claim.
    assert usage.subscription_suffix("codex", {"codex": "auto"}, home, today) == " -> 21.11"
    # …and yields nothing when the claim (or the whole login) is absent, never a guess.
    bare = tmp_path / "codex-bare"
    _write_codex_auth(bare)
    assert usage.subscription_suffix("codex", {"codex": "auto"}, bare, today) == ""
    assert usage.subscription_suffix("codex", {"codex": "auto"}, None, today) == ""


def test_subscription_suffix_auto_derives_the_claude_card_from_the_cached_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claude_private=auto`` reads the cached profile; an empty cache shows no date."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    usage._profile_cache.clear()
    today = date(2026, 9, 1)
    auto = {"claude_private": "auto"}
    # Nothing cached yet (no fetch has run) ⇒ silence, not a fabricated date.
    assert usage.subscription_suffix("claude_private", auto, None, today) == ""
    path = usage._profile_path("private")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"subscription_created_at": "2025-09-18T09:53:15Z", "fetched_at": 1}),
        encoding="utf-8",
    )
    assert usage.subscription_suffix("claude_private", auto, None, today) == " -> 18.9"


def test_claude_profile_fetch_caches_the_subscription_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile endpoint's ``subscription_created_at`` is cached, and gates on staleness."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    usage._profile_cache.clear()
    monkeypatch.setattr(usage, "_keychain_oauth_token", lambda account: "token-xyz")
    body = json.dumps(
        {
            "account": {"email": "someone@example.org"},
            "organization": {
                "subscription_status": "active",
                "subscription_created_at": "2025-09-18T09:53:15.392387Z",
            },
        }
    ).encode()
    monkeypatch.setattr(usage.urllib.request, "urlopen", lambda *a, **k: _FakeOAuthResp(body))

    assert usage.claude_profile_stale("private", now=1_000_000) is True
    assert usage.fetch_claude_profile("private", now=1_000_000) == "2025-09-18T09:53:15.392387Z"
    assert usage.read_subscription_created_at("private") == "2025-09-18T09:53:15.392387Z"
    # Cached: fresh within a day, stale again after one.
    assert usage.claude_profile_stale("private", now=1_000_000 + 3600) is False
    assert usage.claude_profile_stale("private", now=1_000_000 + 90_000) is True
    # No token ⇒ no call, no write, no crash — the cache simply keeps its last answer.
    monkeypatch.setattr(usage, "_keychain_oauth_token", lambda account: None)
    assert usage.fetch_claude_profile("private", now=2_000_000) == ""
    assert usage.read_subscription_created_at("private") == "2025-09-18T09:53:15.392387Z"


def test_codex_account_email_falls_back_to_the_live_snapshot(tmp_path: Path) -> None:
    """A JWT with no ``email`` claim is healed by the e-mail the endpoint reported."""
    home = tmp_path / "codex"
    _write_codex_auth(home, email=None)
    assert usage.codex_account_email(home) is None
    _seed_codex_live(home, captured_at=_LIVE_NOW, email="alice.example@example.com")
    assert usage.codex_account_email(home) == "alice.example@example.com"


def test_fetch_codex_usage_parses_the_live_payload_and_caches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verified payload → both windows, the refusal, live=True, and a cache file."""
    home = tmp_path / "codex"
    _write_codex_auth(home)
    seen = _fake_wham(monkeypatch, _WHAM_BLOCKED)

    snap = usage.fetch_codex_usage(home, now=_LIVE_NOW)

    assert snap is not None
    assert snap.live is True
    assert snap.five_hour == usage.Window(used_percentage=100.0, resets_at=1788095849)
    assert snap.seven_day == usage.Window(used_percentage=23.0, resets_at=1788643641)
    assert snap.blocked_reason == "included usage limit reached (no credit overflow)"
    assert snap.blocked_at == _LIVE_NOW
    assert snap.email == "alice.example@example.com"
    assert snap.plan_type == "team"
    # The request: the fixed endpoint, the auth.json token, and the account header.
    assert seen["url"] == usage._WHAM_USAGE_URL
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer access-token-xyz"
    assert headers["chatgpt-account-id"] == "acct-1"
    # …and the snapshot is cached for the render path, keyed to this home.
    cached = usage.read_codex_live(home)
    assert cached is not None and cached.live is True
    assert cached.five_hour == snap.five_hour and cached.seven_day == snap.seven_day
    assert cached.blocked_reason == snap.blocked_reason
    assert json.loads(usage._codex_usage_path(home).read_text(encoding="utf-8"))["fetched_at"] == (
        _LIVE_NOW
    )


def test_fetch_codex_usage_maps_windows_by_duration_not_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap the two windows in the payload: they must still land in the right bars."""
    home = tmp_path / "codex"
    _write_codex_auth(home)
    swapped = json.loads(json.dumps(_WHAM_BLOCKED))
    rate = swapped["rate_limit"]
    rate["primary_window"], rate["secondary_window"] = (
        rate["secondary_window"],
        (rate["primary_window"]),
    )
    _fake_wham(monkeypatch, swapped)

    snap = usage.fetch_codex_usage(home, now=_LIVE_NOW)

    assert snap is not None
    assert snap.five_hour == usage.Window(used_percentage=100.0, resets_at=1788095849)
    assert snap.seven_day == usage.Window(used_percentage=23.0, resets_at=1788643641)


def test_fetch_codex_usage_healthy_payload_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``allowed: true`` with no reached-type is the healthy shape — no phantom block."""
    home = tmp_path / "codex"
    _write_codex_auth(home)
    healthy = json.loads(json.dumps(_WHAM_BLOCKED))
    healthy["rate_limit"]["allowed"] = True
    healthy["rate_limit"]["limit_reached"] = False
    healthy["rate_limit"]["primary_window"]["used_percent"] = 12
    healthy["rate_limit_reached_type"] = None
    _fake_wham(monkeypatch, healthy)

    snap = usage.fetch_codex_usage(home, now=_LIVE_NOW)

    assert snap is not None
    assert snap.blocked is False and snap.blocked_reason == "" and snap.blocked_at == 0
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 12.0


def test_fetch_codex_usage_skips_an_api_key_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``auth_mode`` that is not ``chatgpt`` has no subscription windows — never call."""
    home = tmp_path / "codex"
    _write_codex_auth(home, auth_mode="apikey")

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not reach the network for an API-key login")

    monkeypatch.setattr(usage.urllib.request, "urlopen", _boom)
    assert usage.fetch_codex_usage(home, now=_LIVE_NOW) is None
    assert not usage._codex_usage_path(home).exists()


def test_fetch_codex_usage_falls_back_to_the_app_server_on_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 means the stored token expired; only `codex` can refresh it, so ask it to."""
    home = tmp_path / "codex"
    _write_codex_auth(home)

    def _unauthorized(*_a: object, **_k: object) -> object:
        raise usage.urllib.error.HTTPError(
            usage._WHAM_USAGE_URL, 401, "Unauthorized", Message(), None
        )

    monkeypatch.setattr(usage.urllib.request, "urlopen", _unauthorized)
    calls: list[Path] = []
    fallback = usage.Usage(
        captured_at=_LIVE_NOW,
        five_hour=usage.Window(100.0, 1788095849),
        seven_day=usage.Window(23.0, 1788643641),
        live=True,
        plan_type="team",
    )

    def _appserver(home_arg: Path, now: int | None = None) -> usage.Usage:
        calls.append(home_arg)
        assert now == _LIVE_NOW
        return fallback

    monkeypatch.setattr(usage, "_fetch_codex_usage_appserver", _appserver)
    snap = usage.fetch_codex_usage(home, now=_LIVE_NOW)
    assert calls == [home]
    assert snap is fallback
    assert usage.read_codex_live(home) is not None  # the fallback result is cached too

    # A 500 is NOT a token problem: no app-server spawn, no cache write.
    usage._codex_usage_path(home).unlink()
    calls.clear()

    def _server_error(*_a: object, **_k: object) -> object:
        raise usage.urllib.error.HTTPError(usage._WHAM_USAGE_URL, 500, "boom", Message(), None)

    monkeypatch.setattr(usage.urllib.request, "urlopen", _server_error)
    assert usage.fetch_codex_usage(home, now=_LIVE_NOW) is None
    assert calls == []
    assert not usage._codex_usage_path(home).exists()


def test_app_server_fallback_skips_unrelated_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``codex app-server`` interleaves notifications; only OUR request id is the answer."""
    monkeypatch.setattr(
        usage.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None
    )
    stdout = (
        "\n".join(
            [
                json.dumps({"id": 1, "result": {"userAgent": "codex"}}),
                json.dumps(
                    {"method": "remoteControl/status/changed", "params": {"connected": True}}
                ),
                json.dumps(
                    {
                        "id": 2,
                        "result": {
                            "rateLimits": {
                                "limitId": "codex",
                                "primary": {
                                    "usedPercent": 100,
                                    "windowDurationMins": 300,
                                    "resetsAt": 1788095849,
                                },
                                "secondary": {
                                    "usedPercent": 23,
                                    "windowDurationMins": 10080,
                                    "resetsAt": 1788643641,
                                },
                                "planType": "team",
                                "rateLimitReachedType": "workspace_owner_credits_depleted",
                            }
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    captured: dict[str, object] = {}

    def _run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["input"] = kwargs.get("input")
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(usage.subprocess, "run", _run)
    home = tmp_path / "codex"
    snap = usage._fetch_codex_usage_appserver(home, now=_LIVE_NOW)

    assert snap is not None
    assert snap.live is True and snap.plan_type == "team"
    assert snap.five_hour == usage.Window(used_percentage=100.0, resets_at=1788095849)
    assert snap.seven_day == usage.Window(used_percentage=23.0, resets_at=1788643641)
    assert snap.blocked_reason == "included usage limit reached (no credit overflow)"
    assert captured["cmd"] == ["/usr/bin/codex", "app-server"]
    assert captured["env"]["CODEX_HOME"] == str(home)  # type: ignore[index]
    assert "account/rateLimits/read" in str(captured["input"])
    assert captured["timeout"] == usage._APPSERVER_TIMEOUT_SEC

    # No `codex` on PATH → no fallback at all.
    monkeypatch.setattr(usage.shutil, "which", lambda _name: None)
    assert usage._fetch_codex_usage_appserver(home, now=_LIVE_NOW) is None


def test_codex_usage_stale_tracks_the_cache_mtime(tmp_path: Path) -> None:
    """Missing or older than the throttle ⇒ stale; freshly written ⇒ not."""
    home = tmp_path / "codex"
    assert usage.codex_usage_stale(home, 600, now=_LIVE_NOW) is True  # no cache yet
    path = _seed_codex_live(home, captured_at=_LIVE_NOW)
    os.utime(path, (_LIVE_NOW - 100, _LIVE_NOW - 100))
    assert usage.codex_usage_stale(home, 600, now=_LIVE_NOW) is False
    os.utime(path, (_LIVE_NOW - 900, _LIVE_NOW - 900))
    assert usage.codex_usage_stale(home, 600, now=_LIVE_NOW) is True


def test_read_codex_usage_serves_whichever_source_is_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live fetch beats an older rollout event — and an older live cache loses to one."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _write_codex_rollout(home, _CODEX_RATE_LIMITS, name="stale", mtime=_LIVE_NOW - 50000)
    _seed_codex_live(home, captured_at=_LIVE_NOW - 60)
    usage._codex_cache.clear()

    snap = usage.read_codex_usage(now=_LIVE_NOW, home=home)
    assert snap is not None and snap.live is True
    assert snap.five_hour == usage.Window(used_percentage=100.0, resets_at=1788095849)

    # A Codex turn AFTER that fetch writes fresher windows: the rollout wins again.
    _write_codex_rollout(home, _CODEX_RATE_LIMITS, name="fresh", mtime=_LIVE_NOW - 10)
    usage._codex_cache.clear()
    snap = usage.read_codex_usage(now=_LIVE_NOW, home=home)
    assert snap is not None and snap.live is False
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 12.0


def test_read_codex_usage_staples_a_refusal_newer_than_the_live_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block recorded after the fetch still pins the full window — and stays 'live'."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _write_codex_rollout(home, _CODEX_PREMIUM_DEPLETED, name="refused", mtime=_LIVE_NOW - 10)
    _seed_codex_live(
        home,
        captured_at=_LIVE_NOW - 600,
        five=(81.0, 1788095849),
        seven=(23.0, 1788643641),
    )
    usage._codex_cache.clear()

    snap = usage.read_codex_usage(now=_LIVE_NOW, home=home)

    assert snap is not None and snap.live is True
    assert snap.blocked is True
    assert snap.blocked_reason == "included usage limit reached (no credit overflow)"
    # The 5h window is the most-consumed LIVE one, so it is what filled.
    assert snap.five_hour == usage.Window(used_percentage=100.0, resets_at=1788095849)
    assert snap.seven_day == usage.Window(used_percentage=23.0, resets_at=1788643641)
    # …and the card is just the two bars: the pinned 100% row IS the refusal.
    card = usage.render_codex_usage(snap, now=_LIVE_NOW).plain
    assert card.startswith("Session: Resets in ") and "100%" in card
    assert "⛔" not in card


def test_read_codex_usage_live_block_is_not_re_stapled_by_an_older_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint reports the block itself; an OLDER rollout refusal adds nothing."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    _write_codex_rollout(home, _CODEX_PREMIUM_DEPLETED, name="refused", mtime=_LIVE_NOW - 3600)
    _seed_codex_live(
        home,
        captured_at=_LIVE_NOW - 60,
        five=(12.0, 1788095849),
        seven=(23.0, 1788643641),
    )
    usage._codex_cache.clear()

    snap = usage.read_codex_usage(now=_LIVE_NOW, home=home)

    assert snap is not None and snap.live is True and snap.blocked is False
    assert snap.five_hour is not None and snap.five_hour.used_percentage == 12.0


def test_read_codex_usage_second_home_is_live_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second CODEX_HOME has no rollout files — and never inherits the default's refusal."""
    default_home = tmp_path / "codex"
    private_home = tmp_path / "codex-private"
    monkeypatch.setenv("CODEX_HOME", str(default_home))
    # The DEFAULT login is blocked and has windows; the private one only has a live cache.
    _write_codex_rollout(default_home, _CODEX_RATE_LIMITS, name="healthy", mtime=_LIVE_NOW - 200)
    _write_codex_rollout(
        default_home, _CODEX_PREMIUM_DEPLETED, name="refused", mtime=_LIVE_NOW - 10
    )
    _seed_codex_live(private_home, captured_at=_LIVE_NOW - 60, five=(7.0, 1788095849))
    usage._codex_cache.clear()

    private = usage.read_codex_usage(now=_LIVE_NOW, home=private_home)
    assert private is not None and private.live is True
    assert private.blocked is False  # the other account's refusal must not bleed across
    assert private.five_hour is not None and private.five_hour.used_percentage == 7.0

    # …while the default home still reports its own block, from its own rollout files.
    default = usage.read_codex_usage(now=_LIVE_NOW, home=default_home)
    assert default is not None and default.blocked is True

    # Both snapshots are cached under their own home (one slot per home, no thrashing).
    assert set(usage._codex_cache) == {str(default_home), str(private_home)}


def test_read_codex_live_refuses_a_snapshot_from_another_home(tmp_path: Path) -> None:
    """The payload records its home, so a copied/colliding cache is refused, not served."""
    home = tmp_path / "codex"
    path = _seed_codex_live(home, captured_at=_LIVE_NOW)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["home"] = str(tmp_path / "somewhere-else")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert usage.read_codex_live(home) is None


def test_render_codex_usage_blocked_banner_marks_live_figures() -> None:
    """Live+pinned needs no banner at all; the rollout path keeps its caveat."""
    windows = {
        "five_hour": usage.Window(used_percentage=100.0, resets_at=_LIVE_NOW + 600),
        "seven_day": usage.Window(used_percentage=23.0, resets_at=_LIVE_NOW + 5 * 86400),
    }
    reason = "included usage limit reached (no credit overflow)"
    live = usage.Usage(
        captured_at=_LIVE_NOW - 120,
        five_hour=windows["five_hour"],
        seven_day=windows["seven_day"],
        blocked_reason=reason,
        blocked_at=_LIVE_NOW,
        live=True,
    )
    # The 100% bar already embosses "Resets in 10m", so ⛔ / "access returns in 10m" /
    # "live figures, 2m old" were three lines repeating the row beneath them.
    plain = usage.render_codex_usage(live, now=_LIVE_NOW).plain
    assert plain.splitlines() == [
        "Session: Resets in 10m        100%",
        "Week: Resets in 5d 0h 0m       23%",
    ]

    # A LIVE snapshot with nothing pinned (no window at 100%) still needs the banner —
    # otherwise the card would show only headroom while Codex refuses.
    unpinned = usage.Usage(
        captured_at=_LIVE_NOW - 120,
        five_hour=usage.Window(used_percentage=99.0, resets_at=_LIVE_NOW + 600),
        seven_day=windows["seven_day"],
        blocked_reason=reason,
        blocked_at=_LIVE_NOW,
        live=True,
    )
    plain = usage.render_codex_usage(unpinned, now=_LIVE_NOW).plain
    assert plain.startswith("⛔ usage limit reached\naccess returns in 10m\nlive figures, 2m old\n")

    rollout = usage.Usage(
        captured_at=_LIVE_NOW - 120,
        five_hour=windows["five_hour"],
        seven_day=windows["seven_day"],
        blocked_reason=reason,
        blocked_at=_LIVE_NOW,
    )
    plain = usage.render_codex_usage(rollout, now=_LIVE_NOW).plain
    assert "100% = the limit that fired; other figures are 2m old" in plain
    assert "live figures" not in plain


def test_render_codex_usage_blocked_banner_uses_the_short_wording() -> None:
    """The card is 34 columns; the CLI's long refusal label would widen the whole column.

    ``width: auto`` on every ``#usage*`` card means the longest line decides the card's
    width, and the cards share one right-pinned column — so the CLI wording
    (``BLOCKED — included usage limit reached (no credit overflow)``, 61 chars) stretched
    the Codex box past 60 columns and stole that width from the job-details pane.

    Rollout-sourced (``live=False``), since a live pinned snapshot renders no banner —
    its 100% bar already carries the refusal.
    """
    snap = usage.Usage(
        captured_at=_LIVE_NOW - 120,
        five_hour=usage.Window(used_percentage=100.0, resets_at=_LIVE_NOW + 600),
        seven_day=usage.Window(used_percentage=23.0, resets_at=_LIVE_NOW + 5 * 86400),
        blocked_reason="included usage limit reached (no credit overflow)",
        blocked_at=_LIVE_NOW,
    )
    banner = usage.render_codex_usage(snap, now=_LIVE_NOW).plain.split("\n")[0]
    assert banner == "⛔ usage limit reached"
    assert len(banner) <= usage._CARD_INNER_WIDTH
    # An unmapped refusal code has no short form to fall back on and passes through, so
    # the card still says WHY rather than silently dropping the reason.
    snap.blocked_reason = "some new refusal"
    assert usage.render_codex_usage(snap, now=_LIVE_NOW).plain.startswith("⛔ some new refusal")
