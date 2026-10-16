"""Job-account routing — the ``job_account`` "saturate what resets earliest" policy.

Exercises :mod:`command_center.routing` in isolation: the ``score_accounts`` burn-rate
ranking (usable vs no-data/stale/dead/exhausted), the ``pick_job_account`` policy
resolution (``""`` → default, a label → pin, an unknown label → default, ``"auto"`` →
max urgency with exhaustion deprioritized and ties broken by config order), and the two
touch points that let routing fill an empty account at job creation — ``cli._account_config_dir``
and the ``ccc job-account`` report. Usage snapshots and the config are monkeypatched (fixed
``now`` values) so nothing hits the filesystem or the network.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from command_center import config, routing, usage


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(name="accounts_env")
def _accounts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Pin two accounts (private = CLAUDE_HOME, work) — overrides the single-account default."""
    private = tmp_path / "private"
    work = tmp_path / "work"
    private.mkdir()
    work.mkdir()
    private, work = private.resolve(), work.resolve()
    monkeypatch.setenv("CLAUDE_HOME", str(private))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dirs = {"private": private, "work": work}  # config order: private first
    monkeypatch.setattr(config, "claude_config_dirs", lambda: dict(dirs))
    return dirs


def _patch_usage(monkeypatch: pytest.MonkeyPatch, snaps: dict[str, usage.Usage]) -> None:
    """Serve each account label a canned snapshot (a missing label reads as ``None``)."""
    monkeypatch.setattr(usage, "read_usage", lambda account="private": snaps.get(account))


def _patch_policy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Set the ``job_account`` policy the routing reads from ``load_config``."""
    monkeypatch.setattr(config, "load_config", lambda: config.Config(job_account=value))


def _snap(now: int, *, used: float, hours: float, captured_offset: int = 0) -> usage.Usage:
    """A snapshot whose plain 7-day window is *used*% and resets in *hours* hours."""
    win = usage.Window(used_percentage=used, resets_at=now + int(hours * 3600))
    return usage.Usage(captured_at=now - captured_offset, five_hour=None, seven_day=win)


# ---------------------------------------------------------------------------
# pick_job_account — policy resolution
# ---------------------------------------------------------------------------
def test_policy_empty_picks_default_account(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``""`` (default) routes to the default account — today's behaviour, no usage read."""
    _patch_policy(monkeypatch, "")
    assert routing.pick_job_account(now=1000) == ("private", str(accounts_env["private"]))


def test_policy_explicit_label_pins_that_account(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured label is a hard pin to that account's dir."""
    _patch_policy(monkeypatch, "work")
    assert routing.pick_job_account(now=1000) == ("work", str(accounts_env["work"]))


def test_policy_unknown_label_falls_back_to_default(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown (non-``auto``) label never raises — it degrades to the default account."""
    _patch_policy(monkeypatch, "ghost")
    assert routing.pick_job_account(now=1000) == ("private", str(accounts_env["private"]))


# ---------------------------------------------------------------------------
# auto — burn-rate ranking
# ---------------------------------------------------------------------------
def test_auto_picks_higher_urgency(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real-world fixture: work 30%/56h (1.25%/h) beats private 74%/121h (~0.21%/h)."""
    now = 1_000_000
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=74.0, hours=121),
            "work": _snap(now, used=30.0, hours=56),
        },
    )
    _patch_policy(monkeypatch, "auto")
    assert routing.pick_job_account(now=now) == ("work", str(accounts_env["work"]))


def test_auto_skips_exhausted_when_another_usable(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exhausted (≥90%) account is deprioritized even with a higher raw urgency."""
    now = 1_000_000
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=95.0, hours=1),  # urgency 5.0 but exhausted
            "work": _snap(now, used=50.0, hours=100),  # urgency 0.5, usable
        },
    )
    _patch_policy(monkeypatch, "auto")
    assert routing.pick_job_account(now=now)[0] == "work"


def test_auto_all_exhausted_picks_max_urgency(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every account is exhausted the pool re-opens and max urgency still wins."""
    now = 1_000_000
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=95.0, hours=10),  # urgency 0.5
            "work": _snap(now, used=92.0, hours=2),  # urgency 4.0
        },
    )
    _patch_policy(monkeypatch, "auto")
    assert routing.pick_job_account(now=now)[0] == "work"


def test_auto_tie_broken_by_config_order(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal urgency → the account declared first in config order wins (strict ``>``)."""
    now = 1_000_000
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=50.0, hours=50),  # urgency 1.0
            "work": _snap(now, used=50.0, hours=50),  # urgency 1.0 (tie)
        },
    )
    _patch_policy(monkeypatch, "auto")
    assert routing.pick_job_account(now=now)[0] == "private"


def test_auto_all_unusable_falls_back_to_default(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No usable window anywhere → the fail-safe default account (never refuses to pick)."""
    now = 1_000_000
    _patch_usage(monkeypatch, {})  # every account reads None
    _patch_policy(monkeypatch, "auto")
    assert routing.pick_job_account(now=now) == ("private", str(accounts_env["private"]))


# ---------------------------------------------------------------------------
# score_accounts — usable vs unusable classification
# ---------------------------------------------------------------------------
def test_score_marks_stale_snapshot_unusable(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot captured > 6h ago cannot drive routing (urgency None, note ``stale``)."""
    now = 1_000_000
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=40.0, hours=50, captured_offset=6 * 3600 + 1),
            "work": _snap(now, used=40.0, hours=50),
        },
    )
    by_label = {s.label: s for s in routing.score_accounts(now=now)}
    assert by_label["private"].note == "stale" and by_label["private"].urgency is None
    assert by_label["work"].note == "ok" and by_label["work"].urgency is not None


def test_score_marks_dead_window_unusable(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window whose reset already passed is dead (urgency None, note ``window dead``)."""
    now = 1_000_000
    dead = usage.Window(used_percentage=40.0, resets_at=now - 10)
    _patch_usage(
        monkeypatch,
        {
            "private": usage.Usage(captured_at=now, five_hour=None, seven_day=dead),
        },
    )
    private = {s.label: s for s in routing.score_accounts(now=now)}["private"]
    assert private.note == "window dead" and private.urgency is None


def test_score_marks_missing_snapshot_no_data(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing snapshot reads as no-data (urgency None, note ``no data``)."""
    now = 1_000_000
    _patch_usage(monkeypatch, {"work": _snap(now, used=40.0, hours=50)})  # private → None
    private = {s.label: s for s in routing.score_accounts(now=now)}["private"]
    assert private.note == "no data" and private.urgency is None


def test_score_uses_seven_day_without_fable_week(
    accounts_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no Fable window the plain 7-day window drives the metric."""
    now = 1_000_000
    win = usage.Window(used_percentage=60.0, resets_at=now + 40 * 3600)
    _patch_usage(
        monkeypatch,
        {"private": usage.Usage(captured_at=now, five_hour=None, seven_day=win, fable_week=None)},
    )
    private = {s.label: s for s in routing.score_accounts(now=now)}["private"]
    assert private.note == "ok"
    assert private.urgency == pytest.approx((100.0 - 60.0) / 40.0)  # 1.0 %/h


@pytest.mark.usefixtures("accounts_env")
def test_score_ignores_fable_week_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present ``fable_week`` never drives the metric — ``seven_day`` does."""
    now = 1_000_000
    week = usage.Window(used_percentage=60.0, resets_at=now + 40 * 3600)
    fable = usage.Window(used_percentage=10.0, resets_at=now + 5 * 3600)
    _patch_usage(
        monkeypatch,
        {"private": usage.Usage(captured_at=now, five_hour=None, seven_day=week, fable_week=fable)},
    )
    private = {s.label: s for s in routing.score_accounts(now=now)}["private"]
    assert private.note == "ok"
    assert private.used_pct == pytest.approx(60.0)
    assert private.resets_at == week.resets_at
    assert private.urgency == pytest.approx((100.0 - 60.0) / 40.0)


@pytest.mark.usefixtures("accounts_env")
def test_score_fable_week_alone_is_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot carrying only ``fable_week`` has no usable window for routing."""
    now = 1_000_000
    fable = usage.Window(used_percentage=10.0, resets_at=now + 5 * 3600)
    _patch_usage(
        monkeypatch,
        {"private": usage.Usage(captured_at=now, five_hour=None, seven_day=None, fable_week=fable)},
    )
    private = {s.label: s for s in routing.score_accounts(now=now)}["private"]
    assert private.note == "no data" and private.urgency is None


# ---------------------------------------------------------------------------
# call-site wiring — cli._account_config_dir + the ccc job-account report
# ---------------------------------------------------------------------------
def test_cli_account_config_dir_uses_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An omitted/empty account in the CLI resolves to the routed job account."""
    from command_center import cli

    monkeypatch.setattr(routing, "pick_job_account", lambda now=None: ("work", "/routed/dir"))
    assert cli._account_config_dir(None) == ("/routed/dir", None)
    assert cli._account_config_dir("") == ("/routed/dir", None)
    assert cli._account_config_dir("   ") == ("/routed/dir", None)


def test_cmd_job_account_smoke(
    accounts_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ccc job-account`` prints a per-account report ending in the resolved policy line."""
    from command_center import cli

    now = int(time.time())
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=74.0, hours=121),
            "work": _snap(now, used=30.0, hours=56),
        },
    )
    _patch_policy(monkeypatch, "auto")
    assert cli.cmd_job_account(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "← pick" in out
    assert 'policy: job_account = "auto" -> new jobs bill to: work' in out


def test_cmd_job_account_pick_prints_label_only(
    accounts_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ccc job-account --pick`` emits just the routed label — the shell-wrapper form."""
    from command_center import cli

    now = int(time.time())
    _patch_usage(
        monkeypatch,
        {
            "private": _snap(now, used=74.0, hours=121),
            "work": _snap(now, used=30.0, hours=56),
        },
    )
    _patch_policy(monkeypatch, "auto")
    assert cli.cmd_job_account(argparse.Namespace(pick=True)) == 0
    assert capsys.readouterr().out == "work\n"
