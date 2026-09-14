#!/usr/bin/env python3
"""Weekly rota for a SHARED Codex seat — pure arithmetic over config plus the clock.

One ChatGPT login can belong to two people on alternating weeks. Reserving it with an
administrative hold works exactly once: somebody has to re-arm the hold every Monday, and
the week nobody does is the week ccc bills a colleague's seat. So the block is COMPUTED —
:mod:`command_center.quota` asks this module "whose week is it?" on every resolution, and
the seat leaves the ranking for the other person's week all by itself.

The vocabulary, and why each piece is shaped the way it is:

* an entry is ``label=YYYY-MM-DD@IANA_ZONE:name,name[,name…]`` — the date is the MONDAY of
  the week the FIRST name holds the seat, and the names take turns week by week in that
  order, forever, forwards AND backwards from that Monday. A schedule that only runs
  forwards would need re-anchoring every time somebody looks at a past week.
* the zone is **required**, and weeks are the aware intervals ``[Monday 00:00, next Monday
  00:00)`` in THAT zone. The process-local zone is deliberately not consulted: a laptop
  that travels, or a consumer running under a different ``TZ``, must not move the boundary
  under which a seat changes hands.
* ``me`` is this machine's operator. Membership is decided PER SEAT — a rota whose names
  do not contain ``me`` is never ours, which is what makes the fail-closed rule in
  :mod:`command_center.quota` (an unusable rota blocks the seat) safe rather than punitive.

Stdlib only, and NO import of :mod:`command_center.config`: this module is the arithmetic,
the config keys are the caller's business, and keeping them apart is what lets the whole
rota be tested with literal entries and an injected ``now``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A rota name is a person handle, not a path or a display name: the same shape a Codex
# seat label and a Claude account label must have (``config._ACCOUNT_LABEL_RE``), so it
# can be typed on the CLI and compared without normalization surprises.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Where a symlinked ``/etc/localtime`` hides the IANA name (``/usr/share/zoneinfo/…`` on
# Linux, ``/var/db/timezone/zoneinfo/…`` on macOS).
_ZONEINFO_MARKER = "/zoneinfo/"

# The weekday names a refusal prints when a start date is not a Monday.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class RotaSpec:
    """One seat's schedule: who holds it, starting which Monday, in which zone."""

    label: str  # the Codex seat label ("default" / "private" / an extra)
    anchor: date  # the Monday the FIRST name holds the seat
    zone: str  # IANA zone key the weeks are measured in
    names: tuple[str, ...]  # ≥ 2 unique names, in turn order


@dataclass(frozen=True)
class RotaError:
    """One unusable entry, reported rather than raised (``ccc quota -j``, ``rota show``)."""

    entry: str  # the raw config entry
    label: str  # the seat it names, or "" when even that could not be read
    error: str  # why it is unusable, in words


# A resolved rota state is a flat render record: every field is one thing a consumer
# would otherwise recompute (and get wrong in its own time zone).
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class RotaState:
    """A seat's rota resolved AT one instant — everything a renderer needs, pre-rendered.

    ``next_mine_label`` / ``next_other_label`` exist because every consumer of this state
    (``ccc quota -j``, ``ai routing``, the ``order`` table) renders in its OWN process,
    under its own ``TZ``. Handing them a bare epoch would have each one re-interpret the
    boundary locally and print a Sunday to anybody east of the rota's zone.
    """

    holder: str  # whose week it is right now
    mine: bool  # … and whether that is us
    week_start: date  # the Monday of the current week, in the entry's zone
    week_end_exclusive: date  # the NEXT Monday (the interval is half-open)
    label: str  # "14.9.–20.9. used by alice"
    next_mine_at: int  # epoch of the first Monday the seat is ours (0 = never)
    next_holder: str  # who takes it at ``next_other_at`` ("" = never)
    next_other_at: int  # epoch of the first Monday it is NOT ours (0 = never)
    next_mine_label: str  # "Mon 21.9." in the entry's zone ("" when never)
    next_other_label: str  # "Mon 28.9." in the entry's zone ("" when never)


def valid_name(name: str) -> bool:
    """True when *name* is a usable rota name (``^[a-z0-9][a-z0-9_-]*$``)."""
    return bool(_NAME_RE.match(name or ""))


def valid_zone(zone: str) -> bool:
    """True when *zone* is a resolvable IANA key on this machine."""
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def week_label(start: date, end_exclusive: date) -> str:
    """``14.9.–20.9.`` — Swiss D.M., the displayed end being the INCLUSIVE Sunday.

    The separator is an EN DASH (U+2013): the interval is a range, and the hyphen reads
    as part of a date to anybody scanning ``14.9.-20.9.`` quickly.
    """
    last = end_exclusive - timedelta(days=1)
    return f"{start.day}.{start.month}.–{last.day}.{last.month}."


def monday_label(day: date) -> str:
    """``Mon 21.9.`` — a week boundary, named so no consumer re-derives it locally.

    The weekday word is a constant rather than ``strftime("%a")``: every boundary IS a
    Monday, and ``%a`` would render it in whatever locale the reading process happens to
    run under.
    """
    return f"Mon {day.day}.{day.month}."


def parse_codex_seat_rota(entries: list[str]) -> tuple[dict[str, RotaSpec], list[RotaError]]:
    """``(specs by seat label, errors)`` for the raw ``codex_seat_rota`` list. Never raises.

    Every problem is REPORTED, never raised and never silently dropped: an entry ccc
    cannot read is exactly the case where it must not decide the seat is free (the caller
    turns an error naming a configured seat into a blocked row — fail closed).

    A second entry for one seat keeps the FIRST and reports the second, mirroring
    ``config.parse_codex_homes_extra``: two schedules for one login is a contradiction,
    and picking the later one would make the file's order silently significant.
    """
    specs: dict[str, RotaSpec] = {}
    errors: list[RotaError] = []
    for raw in entries:
        entry = str(raw).strip()
        if not entry:
            continue
        label, spec, error = _parse_entry(entry)
        if spec is None:
            errors.append(RotaError(entry=entry, label=label, error=error))
            continue
        if spec.label in specs:
            errors.append(
                RotaError(
                    entry=entry,
                    label=spec.label,
                    error=f"a second rota for seat {spec.label!r} (the first entry wins)",
                )
            )
            continue
        specs[spec.label] = spec
    return specs, errors


def _parse_entry(  # pylint: disable=too-many-return-statements  # one per grammar rule
    entry: str,
) -> tuple[str, RotaSpec | None, str]:
    """``(label, spec, error)`` for ONE raw entry — the whole grammar, in one place.

    Each refusal returns its OWN sentence (which Monday was meant, which name is bad,
    which zone is unknown), because this text is what the operator reads in
    ``codex-in-claude rota`` and in the blocked row's reason — "invalid entry" alone
    would send them back to guessing.
    """
    label, sep, rest = entry.partition("=")
    label = label.strip()
    if not sep:
        return "", None, "not a `label=YYYY-MM-DD@Europe/Zurich:alice,bob` entry (no `=`)"
    if not valid_name(label):
        return "", None, f"seat label {label!r} is not [a-z0-9][a-z0-9_-]*"
    schedule, sep, names_part = rest.partition(":")
    if not sep:
        return label, None, "no names (expected `…@Europe/Zurich:alice,bob`)"
    day, sep, zone = schedule.strip().partition("@")
    if not sep or not zone.strip():
        return label, None, "no time zone (expected `YYYY-MM-DD@Europe/Zurich`)"
    zone = zone.strip()
    try:
        anchor = datetime.strptime(day.strip(), "%Y-%m-%d").date()
    except ValueError:
        return label, None, f"start date {day.strip()!r} is not a YYYY-MM-DD date"
    if anchor.weekday() != 0:
        monday = anchor - timedelta(days=anchor.weekday())
        return (
            label,
            None,
            f"start date {anchor.isoformat()} is a {_WEEKDAYS[anchor.weekday()]}, "
            f"not a Monday (that week's Monday is {monday.isoformat()})",
        )
    if not valid_zone(zone):
        return label, None, f"unknown time zone {zone!r}"
    names = tuple(part.strip() for part in names_part.split(","))
    bad = next((name for name in names if not valid_name(name)), None)
    if bad is not None:
        return label, None, f"name {bad!r} is not [a-z0-9][a-z0-9_-]*"
    if len(names) < 2:
        return label, None, "a rota needs at least two names"
    if len(set(names)) != len(names):
        return label, None, "the same name appears twice"
    return label, RotaSpec(label=label, anchor=anchor, zone=zone, names=names), ""


def rota_state(spec: RotaSpec, me: str, now: int) -> RotaState:
    """Resolve *spec* at *now* for operator *me* — the one place the weeks are counted.

    The index is ``(this Monday − the anchor Monday) // 7`` with Python's floor division
    and modulo, so a week BEFORE the anchor lands on the last name rather than on an
    exception: a rota is a cycle, and the anchor is a phase, not a start of time.

    An empty or foreign *me* simply means no week is ours (``mine`` False,
    ``next_mine_at`` 0). The caller decides what that costs — :mod:`command_center.quota`
    turns it into a fail-closed block, because "we could not tell whose week it is" must
    never resolve to "ours".
    """
    zone = ZoneInfo(spec.zone)
    today = datetime.fromtimestamp(now, zone).date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    count = len(spec.names)
    index = (week_start - spec.anchor).days // 7
    holder = spec.names[index % count]
    next_mine_at = next_other_at = 0
    next_mine_label = next_other_label = next_holder = ""
    # One full cycle is enough: past it the same weeks repeat, so a flip that has not
    # happened within ``count`` weeks never happens.
    for step in range(1, count + 1):
        who = spec.names[(index + step) % count]
        monday = week_start + timedelta(days=7 * step)
        if who == me and not next_mine_at:
            next_mine_at, next_mine_label = _week_epoch(monday, zone), monday_label(monday)
        if who != me and not next_other_at:
            next_other_at, next_other_label = _week_epoch(monday, zone), monday_label(monday)
            next_holder = who
    return RotaState(
        holder=holder,
        mine=bool(me) and holder == me,
        week_start=week_start,
        week_end_exclusive=week_end,
        label=f"{week_label(week_start, week_end)} used by {holder}",
        next_mine_at=next_mine_at,
        next_holder=next_holder,
        next_other_at=next_other_at,
        next_mine_label=next_mine_label,
        next_other_label=next_other_label,
    )


def _week_epoch(monday: date, zone: ZoneInfo) -> int:
    """Epoch seconds of ``monday 00:00`` IN *zone* — the instant the seat changes hands."""
    return int(datetime.combine(monday, time(0), tzinfo=zone).timestamp())


def day_label(epoch: int, zone: str) -> str:
    """``D.M.`` of *epoch* rendered in *zone*, or ``""`` for 0 / an unusable zone.

    Used for the underlying blocker's reset under a rota wrapper: that date belongs to
    the same calendar as the rota's own weeks, so it is rendered in the rota's zone.
    """
    if not epoch:
        return ""
    try:
        moment = datetime.fromtimestamp(int(epoch), ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError, OSError, OverflowError):
        return ""
    return f"{moment.day}.{moment.month}."


def local_zone_name() -> str | None:
    """This machine's IANA zone key, or ``None`` when it cannot be named honestly.

    ``/etc/localtime`` is a symlink into the zoneinfo tree on both macOS and Linux, so
    its target names the zone; ``$TZ`` is the fallback, and only when it really is a
    ZoneInfo key. ``None`` is a real answer — the CLI then REFUSES to guess and asks for
    ``-z``, because a rota anchored in the wrong zone changes hands on the wrong day.
    """
    try:
        target = Path("/etc/localtime").resolve().as_posix()
    except OSError:  # pragma: no cover - resolve() fails only on exotic filesystems
        target = ""
    if _ZONEINFO_MARKER in target:
        name = target.rsplit(_ZONEINFO_MARKER, maxsplit=1)[-1]
        if valid_zone(name):
            return name
    env = (os.environ.get("TZ") or "").strip()
    if env and valid_zone(env):
        return env
    return None
