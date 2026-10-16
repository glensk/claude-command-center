#!/usr/bin/env python3
"""Monthly Anthropic API credits a Claude subscription grants — from config, never guessed.

A Claude plan can carry a monthly grant of API credits to a linked Console org (Max: per
billing cycle to the linked API org; Team: per month to a linked Console org). The credits
pay for the Claude API (Messages, Batches), the Console Playground, Managed Agents and the
Agent SDK — including ``claude -p`` run with a Console API key — but NOT interactive Claude
Code or extra usage, so they are a separate pot from the session/week windows ``ccc quota``
tracks.

**There is no balance source.** The public Admin API (``/v1/organizations/*``) has no
credit-balance endpoint (``cost_report`` states spend, which may be invoiced rather than
paid from the grant), and the claude.ai billing page shows only the grant and the next
credit date. So this module states what the config pins — amount, when the current grant
expires, when the next one lands — and reports the balance as unknown. It never subtracts
anything from the grant: a derived balance would be a guess wearing a measurement's clothes.

Config (``claude_api_credits``, one entry per Claude account label)::

    claude_api_credits = [
      "private=usd:200,day:26,from:2026-10-09",
      "work=usd:500,day:20,from:2026-10-20",
    ]

``usd`` is the grant per cycle, ``day`` the day of month a cycle turns over (the current
grant expires and the next one lands; clamped in short months), ``from`` the optional date
of the FIRST grant — before it the account has no current grant, only a next one.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import calendar
import datetime as _dt
import math
import re
from dataclasses import dataclass
from typing import Any

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELDS = frozenset({"usd", "day", "from"})


@dataclass(frozen=True)
class ApiCredit:
    """One account's configured grant: ``usd`` per cycle, turning over on ``cycle_day``."""

    account: str
    usd: float
    cycle_day: int
    first: _dt.date | None = None


def _parse_fields(raw: str) -> dict[str, str] | None:
    """``"usd:200,day:26"`` → ``{"usd": "200", "day": "26"}``; ``None`` on any bad field."""
    fields: dict[str, str] = {}
    for part in raw.split(","):
        name, sep, value = part.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or name not in _FIELDS or name in fields or not value:
            return None
        fields[name] = value
    return fields


def _entry(label: str, fields: dict[str, str]) -> ApiCredit | None:
    """Validate one entry's fields; ``None`` when any of them is not usable."""
    try:
        usd = float(fields["usd"])
        day = int(fields["day"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(usd) or usd <= 0 or not 1 <= day <= 31:
        return None
    first: _dt.date | None = None
    if "from" in fields:
        if not _ISO_DATE_RE.match(fields["from"]):
            return None
        try:
            first = _dt.date.fromisoformat(fields["from"])
        except ValueError:
            return None  # well-shaped but not a real day (2026-02-30)
    return ApiCredit(account=label, usd=usd, cycle_day=day, first=first)


def parse_api_credits(entries: list[str]) -> dict[str, ApiCredit]:
    """Pure parser of the ``claude_api_credits`` entries, keyed by account label.

    Same tolerance as the other ``label=…`` config lists: an entry with no ``=``, a bad
    label, an unknown or repeated field, a missing ``usd``/``day``, a non-positive amount,
    a day outside 1..31 or a ``from`` that is not a real ISO date is SKIPPED, so one typo
    never blanks the other accounts. A label given twice keeps its last entry.
    """
    out: dict[str, ApiCredit] = {}
    for entry in entries:
        label, sep, raw = str(entry).partition("=")
        label = label.strip()
        if not sep or not _LABEL_RE.match(label):
            continue
        fields = _parse_fields(raw)
        if fields is None:
            continue
        credit = _entry(label, fields)
        if credit is not None:
            out[label] = credit
    return out


def _boundary(year: int, month: int, day: int) -> _dt.date:
    """Day *day* of that month, clamped to its last day (31 → 30.4., 28./29.2.)."""
    return _dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    """(year, month) moved by *delta* months."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def cycle_bounds(cycle_day: int, today: _dt.date) -> tuple[_dt.date, _dt.date]:
    """The cycle *today* falls in: ``(start, end)`` with ``start <= today < end``."""
    start = _boundary(today.year, today.month, cycle_day)
    if start > today:
        start = _boundary(*_shift(today.year, today.month, -1), cycle_day)
    year, month = _shift(start.year, start.month, 1)
    return start, _boundary(year, month, cycle_day)


def credit_status(credit: ApiCredit, today: _dt.date) -> dict[str, Any]:
    """The JSON-ready state of one grant on *today*.

    ``granted_on``/``expires_on`` are ``None`` before the first grant. ``balance_usd`` is
    ALWAYS ``None`` — no reliable source exposes it (see the module docstring) — and
    ``balance_source`` says so, so a consumer cannot mistake the grant for a balance.
    """
    if credit.first is not None and today < credit.first:
        granted: _dt.date | None = None
        expires: _dt.date | None = None
        next_grant = credit.first
    else:
        start, end = cycle_bounds(credit.cycle_day, today)
        granted = max(start, credit.first) if credit.first is not None else start
        expires = next_grant = end
    return {
        "id": f"claude:{credit.account}",
        "account": credit.account,
        "usd_per_cycle": credit.usd,
        "cycle_day": credit.cycle_day,
        "granted_on": granted.isoformat() if granted else None,
        "expires_on": expires.isoformat() if expires else None,
        "next_grant_on": next_grant.isoformat(),
        "balance_usd": None,
        "balance_source": "n/a",
        "source": "config",
    }


def statuses(
    entries: list[str], today: _dt.date, order: list[str] | None = None
) -> list[dict[str, Any]]:
    """Every configured grant's status, accounts in *order* first, then the rest by label."""
    parsed = parse_api_credits(entries)
    rank = {label: index for index, label in enumerate(order or [])}
    labels = sorted(parsed, key=lambda label: (rank.get(label, len(rank)), label))
    return [credit_status(parsed[label], today) for label in labels]


def _short(day: _dt.date, today: _dt.date) -> str:
    """Swiss ``D.M.`` plus how far away it is: ``26.10. (in 10d)``."""
    delta = (day - today).days
    when = "today" if delta == 0 else (f"in {delta}d" if delta > 0 else f"{-delta}d ago")
    return f"{day.day}.{day.month}. ({when})"


def describe(status: dict[str, Any], today: _dt.date) -> str:
    """One report line's detail: amount, grant dates, ``balance: n/a``."""
    usd = float(status["usd_per_cycle"])
    amount = f"${usd:,.0f}" if usd == int(usd) else f"${usd:,.2f}"
    parts = [f"{amount}/cycle"]
    next_grant = _dt.date.fromisoformat(str(status["next_grant_on"]))
    if status.get("granted_on") and status.get("expires_on"):
        granted = _dt.date.fromisoformat(str(status["granted_on"]))
        expires = _dt.date.fromisoformat(str(status["expires_on"]))
        parts.append(f"granted {granted.day}.{granted.month}.")
        parts.append(f"expires {_short(expires, today)}, next grant then")
    else:
        parts.append(f"first grant {_short(next_grant, today)}")
    balance = status.get("balance_usd")
    parts.append("balance: n/a" if balance is None else f"balance: ${float(balance):,.2f}")
    return " · ".join(parts)
