#!/usr/bin/env python3
"""Route a NEW job to the Claude account that should absorb it — the ``job_account`` policy.

ccc can bill sessions to more than one Claude subscription (see :mod:`.accounts`); each
account has its OWN weekly rate-limit window that refills on its own reset boundary. Left
alone, every new job stamps the DEFAULT (first/private) account, so the private weekly
allowance is spent first and the work allowance sits idle until the private one caps —
wasteful when both windows reset on a rolling clock and unused headroom simply evaporates
at each reset.

The ``job_account`` config key chooses the stamp for a NEW job:

* ``""`` (the default) — the default account, i.e. today's behaviour untouched.
* a configured label (e.g. ``"work"``) — always that account (a hard pin).
* ``"auto"`` — pick by **required burn rate** over the Fable weekly window (falling back
  to the plain 7-day window when ``fable_week`` is absent on the snapshot).

**The metric.** For an account with ``used%`` consumed and ``h`` hours left until its
window resets, ``urgency = (100 - used%) / h`` is the percentage-points-per-hour you would
have to burn to *exactly* exhaust the remaining allowance by the reset. Routing each new
job to the account with the **highest** urgency saturates the allowance that resets
SOONEST first, and it self-balances: as the leader fills, its remaining% falls, its urgency
drops, and once the other account's ``(100-used)/h`` overtakes it, new jobs flip there. No
account is left with stranded headroom at its reset while another caps out mid-run.

Two guards keep the pick safe. A snapshot older than :data:`_STALE_SEC` cannot drive
routing at all — the daemon's OAuth fetch refreshes far more often, so a stale cache means
the usage pipeline is broken and we fail safe to the default account rather than trust a
days-old number. And an account at/above :data:`_EXHAUSTED_PCT` used is DEPRIORITIZED (a
routed job would otherwise risk dying on the hard cap mid-run); it is still eligible when
*every* account is that full, so routing never refuses to pick.

Routing is evaluated once, at job **creation** — the chosen account is stamped into the
draft row's ``config_dir``, visible and editable in the TUI and in the job file's account
select. It is deliberately NOT re-evaluated at start time: re-routing a job that already
carries an explicit (or previously-routed) account on every sync would churn the stamp and
surprise the user, so an empty account is only ever filled in at the moment the job is born.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import time
from dataclasses import dataclass

from . import accounts, config, usage

# A snapshot older than this cannot drive routing (the daemon's OAuth fetch
# refreshes far more often; 6h means "the pipeline is broken — fail safe").
_STALE_SEC = 6 * 3600
# At/above this used% an account is deprioritized so a routed job does not die
# on the hard cap mid-run; it is still chosen when every account is this full.
_EXHAUSTED_PCT = 90.0


@dataclass
class AccountScore:
    """One account's routing scorecard — its burn-rate urgency and why (not) it is usable."""

    label: str
    config_dir: str
    used_pct: float | None  # None = no usable window
    resets_at: int | None
    urgency: float | None  # %/hour required burn rate; None = no usable window
    exhausted: bool
    note: str  # "ok" | "no data" | "stale" | "window dead"


def score_accounts(now: int | None = None) -> list[AccountScore]:
    """Rank every configured account by required burn rate over its weekly window.

    Preserves ``config.claude_config_dirs()`` order (so ties break toward the account
    declared first). For each account the Fable weekly window is preferred, falling back
    to the plain 7-day window when the snapshot has no ``fable_week``. An account whose
    snapshot is missing, stale, or already past its reset is returned with ``urgency`` and
    ``used_pct`` set to ``None`` (and a ``note`` saying why) so it can be shown but never
    routed to; a usable account carries its clamped ``used_pct``, the ``resets_at`` it
    reads from, the ``urgency`` metric, and whether it is at/over :data:`_EXHAUSTED_PCT`.
    """
    now = int(time.time()) if now is None else now
    scores: list[AccountScore] = []
    for label, path in config.claude_config_dirs().items():
        config_dir = str(path)
        snap = usage.read_usage(label)
        window = (snap.fable_week or snap.seven_day) if snap is not None else None
        if snap is None or window is None:
            scores.append(AccountScore(label, config_dir, None, None, None, False, "no data"))
            continue
        if snap.captured_at < now - _STALE_SEC:
            scores.append(
                AccountScore(label, config_dir, None, window.resets_at, None, False, "stale")
            )
            continue
        if window.resets_at <= now:
            scores.append(
                AccountScore(label, config_dir, None, window.resets_at, None, False, "window dead")
            )
            continue
        used = min(100.0, max(0.0, window.used_percentage))
        hours = max((window.resets_at - now) / 3600.0, 1 / 60)
        urgency = (100.0 - used) / hours
        scores.append(
            AccountScore(
                label, config_dir, used, window.resets_at, urgency, used >= _EXHAUSTED_PCT, "ok"
            )
        )
    return scores


def pick_job_account(now: int | None = None) -> tuple[str, str]:
    """The ``(label, config_dir)`` a NEW job should bill to, per the ``job_account`` policy.

    ``""`` (or an unknown non-``"auto"`` label — never raise) resolves to the default
    account; a configured label to that account; ``"auto"`` to the highest-urgency usable
    account, deprioritizing any that is exhausted unless every usable account is. Falls back
    to the default account when ``"auto"`` finds no usable window at all (the fail-safe).
    """
    policy = config.load_config().job_account
    if policy != "auto":
        if policy:
            config_dir = accounts.account_config_dir(policy)
            if config_dir:
                return policy, config_dir
        # Empty policy, or a label no longer configured: the default account.
        return accounts.account_label(""), str(accounts.default_config_dir())

    usable = [s for s in score_accounts(now) if s.urgency is not None]
    if not usable:
        return accounts.account_label(""), str(accounts.default_config_dir())
    pool = [s for s in usable if not s.exhausted] or usable
    # Strict ``>`` keeps the FIRST maximal account (config order) on a tie.
    best: AccountScore | None = None
    best_urgency = -1.0
    for score in pool:
        urgency = score.urgency
        if urgency is not None and urgency > best_urgency:
            best, best_urgency = score, urgency
    assert best is not None  # pool is non-empty and every member has a usable urgency
    return best.label, best.config_dir
