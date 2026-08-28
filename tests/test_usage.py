"""Tests for the account-wide /usage snapshot capture, format, and render."""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
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
    """Per-account `usage-<hash>.json` and `copilot_usage.json` orphans are swept too."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    home = config.app_home()
    home.mkdir(parents=True, exist_ok=True)
    old = time.time() - 7200
    for name in ("usage-work-b6f4d184.json.cccccccc.tmp", "copilot_usage.json.dddddddd.tmp"):
        path = home / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (old, old))

    assert usage.sweep_stale_temps() == 2
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
    usage._codex_cache = None  # reset the module-level parse cache
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
    usage._codex_cache = None
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
    usage._codex_cache = None
    # No sessions dir at all, then a rollout with no rate_limits → still None.
    assert usage.read_codex_usage(now=_NOW) is None
    usage._codex_cache = None
    _write_codex_rollout(tmp_path, None, name="empty")
    assert usage.read_codex_usage(now=_NOW) is None


def test_read_codex_usage_prefers_newest_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage._codex_cache = None
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
    usage._codex_cache = None
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
    usage._codex_cache = None
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
