#!/usr/bin/env python3
"""Monthly Anthropic API credits a Claude subscription grants — from config, never guessed.

A Claude plan can carry a monthly grant of API credits to a linked Console org (Max: per
billing cycle to the linked API org; Team: per month to a linked Console org). The credits
pay for the Claude API (Messages, Batches), the Console Playground, Managed Agents and the
Agent SDK — including ``claude -p`` run with a Console API key — but NOT interactive Claude
Code or extra usage, so they are a separate pot from the session/week windows ``ccc quota``
tracks.

**There is no balance API.** The public Admin API (``/v1/organizations/*``) has no
credit-balance endpoint, and the claude.ai billing page shows only the grant and the next
credit date. So the configured grant is never presented as a balance. Two things can be
added on top of it, each labelled for what it is:

* a MEASURED balance (``balance_json:<path>``): a JSON file some other tool of the user's
  writes after reading the Console billing page (remaining promotional credit, its grant,
  its real expiry and the purchased balance, with ``measured_at``). ccc only READS it —
  no browser, no network. While the reading is fresh (:data:`BALANCE_FRESH_SEC`) it fills
  ``balance_usd`` and replaces the config's dates; a missing, failed or stale file falls
  back to the estimate below, with the reading's age said. Schema: :func:`read_balance`.
* an ESTIMATE: the spend since the current grant landed, from a source the user names in
  ``claude_api_credit_spend``, subtracted from the grant —

* ``admin_api[:<dotenv path>]`` — the org's Admin API ``cost_report`` (an org admin key,
  ``ANTHROPIC_ADMIN_API_KEY``, read from that dotenv file or the environment). It states
  what the whole Console org spent, which may be invoiced rather than paid from the
  grant, so it is an estimate. Network: fetched out-of-band by :func:`refresh_spend`
  (``ccc claude-usage``, ``ccc quota -r``) at most every :data:`SPEND_REFRESH_SEC` and
  cached; a report only ever reads the cache.
* ``runs_jsonl:<path>`` — the ``cost_usd`` sum of a local run ledger (one JSON object
  per line with ``ts`` and ``cost_usd``), i.e. what THIS machine spent with the key: a
  lower bound, stated as "this machine only". A ledger shared by several keys is
  narrowed with ``runs_jsonl:<path>#<item>`` — only rows whose ``item`` field equals
  ``<item>`` count (split on the LAST ``#``).

Config (``claude_api_credits``, one entry per Claude account label)::

    claude_api_credits = [
      "private=usd:200,day:26,from:2026-10-09",
      "work=usd:500,day:20,from:2026-10-20,name:api-team,label:API TEAM",
    ]

``usd`` is the grant per cycle, ``day`` the day of month a cycle turns over (the current
grant expires and the next one lands; clamped in short months), ``from`` the optional date
of the FIRST grant — before it the account has no current grant, only a next one,
``name`` an optional display name for the ``ccc quota`` row, and ``label`` the words the
TUI card's bar starts with (default ``API PROMO``; up to 12 characters, spaces allowed).

Spend and balance sources (``claude_api_credit_spend``, optional; per label at most one
spend source plus one ``balance_json``)::

    claude_api_credit_spend = [
      "work=balance_json:~/.local/state/console-balance/work.json",
      "work=admin_api:~/secrets/anthropic-admin.env",
      "private=runs_jsonl:~/.local/state/claude-apikey/runs.jsonl#anthropic-api-private",
    ]
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import calendar
import datetime as _dt
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELDS = frozenset({"usd", "day", "from", "name", "label"})
# A display name: short, printable, no separators the entry syntax uses.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$")
# The card label: a few words (spaces allowed), kept short enough to fit the bar.
_CARD_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,11}$")


@dataclass(frozen=True)
class ApiCredit:
    """One account's configured grant: ``usd`` per cycle, turning over on ``cycle_day``."""

    account: str
    usd: float
    cycle_day: int
    first: _dt.date | None = None
    name: str = ""
    label: str = ""


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
    name, card = fields.get("name", ""), fields.get("label", "")
    if (name and not _NAME_RE.match(name)) or (card and not _CARD_LABEL_RE.match(card)):
        return None
    return ApiCredit(
        account=label, usd=usd, cycle_day=day, first=first, name=name, label=card.strip()
    )


def parse_api_credits(entries: list[str]) -> dict[str, ApiCredit]:
    """Pure parser of the ``claude_api_credits`` entries, keyed by account label.

    Same tolerance as the other ``label=…`` config lists: an entry with no ``=``, a bad
    label, an unknown or repeated field, a missing ``usd``/``day``, a non-positive amount,
    a day outside 1..31, a ``from`` that is not a real ISO date, a ``name`` that is not
    a short plain word (letters, digits, ``_.-``, at most 24) or a ``label`` longer than
    12 characters (spaces allowed) is SKIPPED, so one typo
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
        "name": credit.name or None,
        "label": credit.label or None,
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


def _money(usd: float) -> str:
    """``$200`` for a whole amount, ``$12.34`` otherwise."""
    return f"${usd:,.0f}" if usd == int(usd) else f"${usd:,.2f}"


def _age(seconds: int) -> str:
    """``45s`` / ``12m`` / ``3h`` / ``2d`` — the age of a reading."""
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _describe_measured(status: dict[str, Any], today: _dt.date) -> str:
    """The detail of a row whose balance was MEASURED (:func:`with_balance`)."""
    left = float(status["balance_usd"])
    grant = float(status.get("balance_grant_usd") or 0.0)
    parts = [
        f"{_money(round(left, 2))} of {_money(grant)} left "
        f"(Console, measured {_age(int(status.get('balance_age_sec') or 0))} ago)"
    ]
    if status.get("granted_on"):
        granted = _dt.date.fromisoformat(str(status["granted_on"]))
        parts.append(f"granted {granted.day}.{granted.month}.")
    if status.get("expires_on"):
        parts.append(f"expires {_short(_dt.date.fromisoformat(str(status['expires_on'])), today)}")
    if status.get("purchased_usd") is not None:
        parts.append(f"purchased {_money(round(float(status['purchased_usd']), 2))}")
    if status.get("balance_error"):
        parts.append(f"last read failed: {status['balance_error']}")
    return " · ".join(parts)


def describe_cap(status: dict[str, Any], today: _dt.date) -> str:
    """The detail of an account's spend-cap row (``cap_*`` from :func:`with_balance`).

    ``$S of $L monthly spend limit (Console, measured 12m ago) · resets D.M. (in Nd) · a
    usage ceiling, not a balance`` — the limit caps what the org may spend per month; it
    is never money that is left.
    """
    spent, limit = float(status.get("cap_spent_usd") or 0.0), float(status["cap_limit_usd"])
    age = _age(int(status.get("balance_age_sec") or 0))
    parts = [
        f"{_money(round(spent, 2))} of {_money(round(limit, 2))} monthly spend limit "
        f"(Console, measured {age} ago)"
    ]
    if status.get("cap_resets_on"):
        resets = _dt.date.fromisoformat(str(status["cap_resets_on"]))
        parts.append(f"resets {_short(resets, today)}")
    parts.append("a usage ceiling, not a balance")
    return " · ".join(parts)


def describe(status: dict[str, Any], today: _dt.date) -> str:
    """One report line's detail: the measured balance or the estimate, then the dates.

    A fresh measured balance (:func:`with_balance`): ``$X of $Y left (Console, measured
    12m ago) · granted D.M. · expires D.M. (in Nd) · purchased $P``. Otherwise, with a
    spend estimate (:func:`with_spend`): ``≈$X of $Y left (est.) · $S spent since D.M.
    (<source>)``. Without one the balance stays ``n/a`` — a configured source that has not
    been read yet says so instead of passing for an empty pot. A configured balance file
    that could not be used adds why (``Console reading 7h old``).
    """
    if status.get("balance_source") == "measured":
        return _describe_measured(status, today)
    line = _describe_estimate(status, today)
    if status.get("balance_note"):
        line += f" · {status['balance_note']}"
    return line


def _describe_estimate(status: dict[str, Any], today: _dt.date) -> str:
    """:func:`describe` for a row without a measured balance."""
    usd = float(status["usd_per_cycle"])
    next_grant = _dt.date.fromisoformat(str(status["next_grant_on"]))
    parts: list[str] = []
    left = status.get("est_left_usd")
    if left is not None:
        spent = float(status.get("est_spent_usd") or 0.0)
        granted = _dt.date.fromisoformat(str(status["granted_on"]))
        source = str(status.get("spend_source") or "")
        if status.get("spend_scope") == "machine":
            source += ", this machine only"
        parts.append(f"≈{_money(float(left))} of {_money(usd)} left (est.)")
        parts.append(f"{_money(spent)} spent since {granted.day}.{granted.month}. ({source})")
        if status.get("spend_error"):
            parts.append(f"last fetch failed: {status['spend_error']}")
    else:
        parts.append(f"{_money(usd)}/cycle")
    if status.get("granted_on") and status.get("expires_on"):
        expires = _dt.date.fromisoformat(str(status["expires_on"]))
        if left is None:
            granted = _dt.date.fromisoformat(str(status["granted_on"]))
            parts.append(f"granted {granted.day}.{granted.month}.")
        parts.append(f"expires {_short(expires, today)}, next grant then")
    else:
        parts.append(f"first grant {_short(next_grant, today)}")
    if left is None:
        balance = status.get("balance_usd")
        tail = "balance: n/a" if balance is None else f"balance: ${float(balance):,.2f}"
        if status.get("spend_pending"):
            tail += " — spend not read yet (`ccc quota -r`)"
        if status.get("spend_error"):
            tail += f" — spend fetch failed: {status['spend_error']}"
        parts.append(tail)
    return " · ".join(parts)


# --- Spend estimate (claude_api_credit_spend) ------------------------------------------

# How often the Admin API cost_report may be asked (seconds). The figure moves by the
# day (cost buckets are daily), so a half-hour cache costs nothing in accuracy.
SPEND_REFRESH_SEC = 1800
_ADMIN_API_BASE = "https://api.anthropic.com/v1/organizations"
_ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_API_KEY"
_ADMIN_TIMEOUT_SEC = 15
_MAX_PAGES = 12
_SPEND_KINDS = frozenset({"admin_api", "runs_jsonl"})
_SOURCE_LABEL = {"admin_api": "Admin API cost_report, full days", "runs_jsonl": "run ledger"}


@dataclass(frozen=True)
class SpendSource:
    """Where one account's spend since the grant is read from (see the module docstring)."""

    account: str
    kind: str
    path: str = ""
    item: str = ""


def parse_spend_sources(entries: list[str]) -> dict[str, SpendSource]:
    """``["work=admin_api:<dotenv>", "private=runs_jsonl:<path>"]`` → per label.

    An entry with a bad label, an unknown kind, or ``runs_jsonl`` without a path (or with an
    empty ``#item``) is skipped (same tolerance as :func:`parse_api_credits`); a repeated
    label keeps its last.
    """
    out: dict[str, SpendSource] = {}
    for entry in entries:
        label, sep, raw = str(entry).partition("=")
        label = label.strip()
        if not sep or not _LABEL_RE.match(label):
            continue
        kind, _, path = raw.strip().partition(":")
        kind, path = kind.strip(), path.strip()
        item = ""
        if kind == "runs_jsonl" and "#" in path:
            path, _, item = path.rpartition("#")
            path, item = path.strip(), item.strip()
            if not item:
                continue
        if kind not in _SPEND_KINDS or (kind == "runs_jsonl" and not path):
            continue
        out[label] = SpendSource(account=label, kind=kind, path=path, item=item)
    return out


def used_pct(status: dict[str, Any]) -> float | None:
    """Estimated percentage of the grant spent, clamped to 0..100; ``None`` without one."""
    spent = status.get("est_spent_usd")
    usd = float(status.get("usd_per_cycle") or 0.0)
    if spent is None or usd <= 0:
        return None
    return max(0.0, min(100.0, float(spent) / usd * 100.0))


_RUNS_MEMO: dict[tuple[str, int, int, _dt.date, str], tuple[float, int]] = {}


def runs_spend(path: Path, since: _dt.date, item: str = "") -> tuple[float, int] | None:
    """``(sum of cost_usd, file mtime)`` of ledger rows on or after *since* (local date).

    ``None`` when the file cannot be read. A row without a parseable ``ts`` or a numeric
    ``cost_usd`` is skipped — it cannot be placed in the cycle, and guessing would move
    the estimate in an unknown direction. A non-empty *item* counts only rows whose
    ``item`` field equals it (a ledger shared by several API keys).
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size, since, item)
    if key in _RUNS_MEMO:
        return _RUNS_MEMO[key]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    mtime = int(stat.st_mtime)
    total = 0.0
    for line in lines:
        try:
            row = json.loads(line)
            stamp = _dt.datetime.fromisoformat(str(row["ts"]))
            cost = float(row["cost_usd"])
            if item and row.get("item") != item:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        local = stamp.astimezone() if stamp.tzinfo else stamp
        if local.date() >= since and math.isfinite(cost):
            total += cost
    # The TUI asks on every render tick; the file changes once per run, so the sum is
    # memoised on its identity (one entry — only the latest reading matters).
    _RUNS_MEMO.clear()
    _RUNS_MEMO[key] = (total, mtime)
    return total, mtime


def _admin_key(source: SpendSource) -> str:
    """The org admin key: from the named dotenv file, else the environment. Never logged."""
    if source.path:
        try:
            text = Path(os.path.expanduser(source.path)).read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == _ADMIN_KEY_ENV:
                return value.strip().strip("'\"")
        return ""
    return os.environ.get(_ADMIN_KEY_ENV, "")


def admin_cost_usd(key: str, since: _dt.date, timeout: float = _ADMIN_TIMEOUT_SEC) -> float:
    """USD the org spent since *since* (UTC day buckets), from ``cost_report``.

    ``amount`` is a decimal string in the currency's LOWEST unit (cents for USD), so it
    is divided by 100. The report covers COMPLETED UTC days only (its default end is the
    start of today, and an explicit later end is refused), so today's spend lands
    tomorrow; a grant that started today has no complete day yet and reads ``0`` without
    a call. Raises on any HTTP/parse failure; the caller records that.
    """
    if since >= _dt.datetime.now(_dt.UTC).date():
        return 0.0
    total = 0.0
    page = ""
    for _ in range(_MAX_PAGES):
        params = {
            "starting_at": f"{since.isoformat()}T00:00:00Z",
            "bucket_width": "1d",
            "limit": "31",
        }
        if page:
            params["page"] = page
        request = urllib.request.Request(
            f"{_ADMIN_API_BASE}/cost_report?{urllib.parse.urlencode(params)}",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.load(response)
        for bucket in body.get("data") or []:
            for result in bucket.get("results") or []:
                if str(result.get("currency", "USD")).upper() == "USD":
                    total += float(result.get("amount") or 0) / 100.0
        page = str(body.get("next_page") or "")
        if not body.get("has_more") or not page:
            break
    return total


def spend_cache_path() -> Path:
    """The Admin-API spend cache (``<ccc state>/api_credit_spend.json``)."""
    from . import config  # pylint: disable=import-outside-toplevel  # keep the parser pure

    return config.app_home() / "api_credit_spend.json"


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def refresh_spend(
    credit_entries: list[str],
    spend_entries: list[str],
    now: int,
    *,
    force: bool = False,
    cache: Path | None = None,
) -> list[str]:
    """Fetch every ``admin_api`` source whose cache is older than :data:`SPEND_REFRESH_SEC`.

    Best-effort and out-of-band (``ccc claude-usage``, ``ccc quota -r``): a failure is
    recorded in the cache with its time — so a broken key is retried at the same
    cadence, not on every call — and the last good figure for the same grant is kept.
    No ``admin_api`` source configured ⇒ no network call at all. Returns one short line
    per account it touched (``work: $0.12 since 2026-10-16`` / ``work: failed (…)``).
    """
    sources = parse_spend_sources(spend_entries)
    grants = parse_api_credits(credit_entries)
    path = cache if cache is not None else spend_cache_path()
    data = _read_cache(path)
    today = _dt.datetime.fromtimestamp(now).date()
    lines: list[str] = []
    changed = False
    for label, source in sources.items():
        if source.kind != "admin_api" or label not in grants:
            continue
        granted = credit_status(grants[label], today).get("granted_on")
        if not granted:
            continue
        cached = data.get(label)
        entry: dict[str, Any] = cached if isinstance(cached, dict) else {}
        same_grant = entry.get("since") == granted
        if same_grant and not force and now - int(entry.get("fetched_at") or 0) < SPEND_REFRESH_SEC:
            continue
        key = _admin_key(source)
        new: dict[str, Any] = {"since": granted, "fetched_at": now}
        if not key:
            new["error"] = f"no {_ADMIN_KEY_ENV}"
        else:
            try:
                new["spent_usd"] = round(admin_cost_usd(key, _dt.date.fromisoformat(granted)), 4)
                new["measured_at"] = now
            except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                code = getattr(err, "code", None)
                new["error"] = type(err).__name__ + (f" {code}" if isinstance(code, int) else "")
        if "error" in new and same_grant and "spent_usd" in entry:
            new["spent_usd"] = entry["spent_usd"]
            new["measured_at"] = entry.get("measured_at", 0)
        data[label] = new
        changed = True
        lines.append(
            f"{label}: failed ({new['error']})"
            if "error" in new
            else f"{label}: {_money(new['spent_usd'])} since {granted}"
        )
    if changed:
        try:
            _write_cache(path, data)
        except OSError:
            pass
    return lines


def with_spend(
    status: dict[str, Any],
    source: SpendSource | None,
    *,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """*status* plus the estimate fields, read from the cache / local ledger only.

    Adds ``est_spent_usd`` / ``est_left_usd`` / ``est_used_pct`` / ``spend_source`` /
    ``spend_scope`` (``org`` or ``machine``) / ``spend_at`` when a figure for the
    CURRENT grant exists; ``spend_pending`` when a source is configured but has nothing
    for this grant yet; ``spend_error`` when the last fetch failed. ``balance_usd``
    stays ``None``: an estimate is not a balance. Never touches the network.
    """
    out = dict(status)
    granted = status.get("granted_on")
    if source is None or not granted:
        return out
    out["spend_source"] = _SOURCE_LABEL[source.kind]
    if source.item:
        out["spend_source"] += f" ({source.item})"
    spent: float | None = None
    if source.kind == "runs_jsonl":
        out["spend_scope"] = "machine"
        read = runs_spend(
            Path(os.path.expanduser(source.path)), _dt.date.fromisoformat(granted), source.item
        )
        if read is not None:
            spent, out["spend_at"] = read
    else:
        out["spend_scope"] = "org"
        entry = (cache or {}).get(source.account)
        if isinstance(entry, dict) and entry.get("since") == granted:
            if entry.get("error"):
                out["spend_error"] = str(entry["error"])
            if "spent_usd" in entry:
                spent = float(entry["spent_usd"])
                out["spend_at"] = int(entry.get("measured_at") or entry.get("fetched_at") or 0)
    if spent is None:
        out["spend_pending"] = "spend_error" not in out
        return out
    usd = float(status["usd_per_cycle"])
    out["est_spent_usd"] = round(spent, 4)
    out["est_left_usd"] = round(usd - spent, 4)
    out["est_used_pct"] = used_pct(out)
    return out


# --- Measured balance (balance_json) ----------------------------------------------------

# A reading older than this is not shown as the balance (the estimate takes over and the
# reading's age is said). The writer is expected to run every 30-60 min, so three hours
# tolerate two missed runs.
BALANCE_FRESH_SEC = 3 * 3600
_BALANCE_KIND = "balance_json"


@dataclass(frozen=True)
class BalanceSource:
    """The JSON file one account's measured Console balance is read from."""

    account: str
    path: str


def parse_balance_sources(entries: list[str]) -> dict[str, BalanceSource]:
    """The ``label=balance_json:<path>`` entries of ``claude_api_credit_spend``, per label.

    They live in the same list as the spend sources but are kept apart, so one account can
    have a measured balance AND an estimate to fall back on. A bad label or an empty path
    is skipped; a repeated label keeps its last.
    """
    out: dict[str, BalanceSource] = {}
    for entry in entries:
        label, sep, raw = str(entry).partition("=")
        label = label.strip()
        kind, _, path = raw.strip().partition(":")
        if not sep or not _LABEL_RE.match(label) or kind.strip() != _BALANCE_KIND:
            continue
        if path.strip():
            out[label] = BalanceSource(account=label, path=path.strip())
    return out


def _epoch(value: Any) -> int | None:
    """An epoch-seconds number or an ISO-8601 timestamp → int epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            stamp = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=_dt.UTC)
        return int(stamp.timestamp())
    return None


def _usd(value: Any) -> float | None:
    """A finite non-negative dollar amount, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _promo_row(raw: Any) -> dict[str, Any] | None:
    """One promotional-credit row of the file, validated; ``None`` when unusable."""
    if not isinstance(raw, dict):
        return None
    remaining, granted = _usd(raw.get("remaining_usd")), _usd(raw.get("granted_usd"))
    expires = _epoch(raw.get("expires_at"))
    if remaining is None or granted is None or expires is None:
        return None
    return {
        "remaining_usd": remaining,
        "granted_usd": granted,
        "expires_at": expires,
        "granted_at": _epoch(raw.get("granted_at")),
        "name": str(raw.get("name") or ""),
    }


def read_balance(path: Path) -> dict[str, Any] | None:
    """The balance file at *path*, validated; ``None`` when it is missing or not JSON.

    Schema (version 1, written by the user's own scraper — ccc never writes it)::

        {"schema": 1, "status": "ok", "error": "",
         "measured_at": 1793270400,           # epoch s or ISO-8601: last GOOD reading
         "checked_at": 1793270400,            # last attempt, good or not
         "currency": "USD", "purchased_usd": 100.0,
         "promo": [{"remaining_usd": 498.03, "granted_usd": 500.0,
                    "expires_at": "2026-11-22T00:00:00Z",
                    "granted_at": "2026-10-19T13:43:08Z", "name": "API credit"}],
         "cap": {"spent_usd": 153.89, "limit_usd": 500.0,
                 "resets_at": "2026-11-01T00:00:00Z"}}       # optional

    ``cap`` is the org's MONTHLY SPEND LIMIT (a usage ceiling the provider enforces, not
    money): what was spent this month against it and when it resets.

    A failed attempt keeps the last good figures and their ``measured_at`` and sets
    ``status`` (e.g. ``logged_out``) — so a reading stays usable until it ages out. The
    result carries ``measured_at`` (``None`` without a good reading), ``status``,
    ``error``, ``purchased_usd``, the valid ``promo`` rows (bad rows are dropped) and
    ``cap`` (``None`` when absent or unusable).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    promo = [row for row in map(_promo_row, data.get("promo") or []) if row is not None]
    return {
        "measured_at": _epoch(data.get("measured_at")),
        "checked_at": _epoch(data.get("checked_at")),
        "status": str(data.get("status") or "unknown"),
        "error": str(data.get("error") or ""),
        "currency": str(data.get("currency") or "USD").upper(),
        "purchased_usd": _usd(data.get("purchased_usd")),
        "promo": promo,
        "cap": _cap(data.get("cap")),
    }


def _cap(raw: Any) -> dict[str, Any] | None:
    """The file's ``cap`` object, validated; ``None`` when absent or unusable."""
    if not isinstance(raw, dict):
        return None
    spent, limit = _usd(raw.get("spent_usd")), _usd(raw.get("limit_usd"))
    if spent is None or limit is None or limit <= 0:
        return None
    return {"spent_usd": spent, "limit_usd": limit, "resets_at": _epoch(raw.get("resets_at"))}


def _utc_date(epoch: int) -> str:
    """The UTC calendar date of *epoch* (the Console states its dates in UTC)."""
    return _dt.datetime.fromtimestamp(epoch, _dt.UTC).date().isoformat()


def with_balance(status: dict[str, Any], source: BalanceSource | None, now: int) -> dict[str, Any]:
    """*status* with the measured balance when the file holds a fresh reading.

    Fresh (``measured_at`` within :data:`BALANCE_FRESH_SEC`) and at least one unexpired
    USD promotional row: ``balance_usd`` = their summed remaining, ``balance_grant_usd`` =
    their summed grants, ``balance_used_pct``, ``balance_source = "measured"``,
    ``balance_at`` / ``balance_age_sec``, ``purchased_usd``, and the REAL dates
    (``granted_on`` / ``expires_on`` of the row that expires first, UTC) replace the
    config's cycle. ``balance_error`` names a failed latest attempt while the last good
    reading is still fresh. A fresh reading with a ``cap`` also adds ``cap_spent_usd`` /
    ``cap_limit_usd`` / ``cap_used_pct`` / ``cap_resets_on`` / ``cap_room_usd`` and
    ``cap_binds`` (the org's monthly spend limit — a ceiling, not a balance; it binds when
    its room is smaller than the money left, see :func:`_cap_binds`); a stale one adds
    none. Otherwise nothing is
    replaced and ``balance_note`` says why (missing file, failed read, stale reading with
    its age). Never touches the network.
    """
    out = dict(status)
    if source is None:
        return out
    reading = read_balance(Path(os.path.expanduser(source.path)))
    if reading is None:
        out["balance_note"] = "no Console reading"
        return out
    measured = reading["measured_at"]
    active = [
        row for row in reading["promo"] if row["expires_at"] > now and reading["currency"] == "USD"
    ]
    failed = reading["status"] != "ok"
    if measured is None:
        out["balance_note"] = f"Console read failed: {reading['status']}"
        return out
    age = max(0, now - measured)
    out["balance_at"] = measured
    out["balance_age_sec"] = age
    if age > BALANCE_FRESH_SEC:
        why = f", last read failed: {reading['status']}" if failed else ""
        out["balance_note"] = f"Console reading {_age(age)} old{why}"
        return out
    out["purchased_usd"] = reading["purchased_usd"]
    cap = reading["cap"]
    if cap is not None and reading["currency"] == "USD":
        out["cap_spent_usd"] = round(cap["spent_usd"], 4)
        out["cap_limit_usd"] = round(cap["limit_usd"], 4)
        out["cap_used_pct"] = max(0.0, min(100.0, cap["spent_usd"] / cap["limit_usd"] * 100.0))
        if cap["resets_at"] is not None:
            out["cap_resets_on"] = _utc_date(cap["resets_at"])
    if not active:
        out["balance_note"] = "Console shows no current promotional credit"
        _cap_binds(out, 0.0)
        return out
    first = min(active, key=lambda row: row["expires_at"])
    left = sum(row["remaining_usd"] for row in active)
    grant = sum(row["granted_usd"] for row in active)
    out.update(
        {
            "balance_usd": round(left, 4),
            "balance_grant_usd": round(grant, 4),
            "balance_used_pct": (
                max(0.0, min(100.0, (grant - left) / grant * 100.0)) if grant > 0 else None
            ),
            "balance_source": "measured",
            "balance_rows": len(active),
            "expires_on": _utc_date(first["expires_at"]),
        }
    )
    if first["granted_at"] is not None:
        out["granted_on"] = _utc_date(first["granted_at"])
    if failed:
        out["balance_error"] = reading["status"]
    _cap_binds(out, left)
    return out


def _cap_binds(out: dict[str, Any], promo_left: float) -> None:
    """Set ``cap_binds``: does the monthly spend limit stop the org BEFORE the money does?

    The cap row is worth showing only then — when the room left under the limit
    (``limit - spent``) is smaller than the money left (promo remaining + purchased
    balance). Otherwise the money runs out first and the cap says nothing actionable.
    """
    if "cap_limit_usd" not in out:
        return
    room = float(out["cap_limit_usd"]) - float(out.get("cap_spent_usd") or 0.0)
    money = promo_left + float(out.get("purchased_usd") or 0.0)
    out["cap_room_usd"] = round(room, 4)
    out["cap_binds"] = room < money


def statuses_with_spend(  # pylint: disable=too-many-arguments
    credit_entries: list[str],
    spend_entries: list[str],
    today: _dt.date,
    order: list[str] | None = None,
    *,
    cache: Path | None = None,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """:func:`statuses` with :func:`with_spend` then :func:`with_balance` applied.

    Cache and local files only. The estimate is computed on the config's cycle first, so
    it stays available as the fallback; a fresh measured balance then takes the display.
    """
    sources = parse_spend_sources(spend_entries)
    balances = parse_balance_sources(spend_entries)
    cached = _read_cache(cache if cache is not None else spend_cache_path()) if sources else {}
    at = int(time.time()) if now is None else now
    return [
        with_balance(
            with_spend(row, sources.get(row["account"]), cache=cached),
            balances.get(row["account"]),
            at,
        )
        for row in statuses(credit_entries, today, order)
    ]
