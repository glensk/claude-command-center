#!/usr/bin/env python3
"""Read-only rendering of ``ccc await`` groups for the TUI's AWAITING section and ``ccc ls``.

Pure formatting over rows already in the store: nothing here probes a source, touches
the network or writes. :func:`visible_awaits` is the one read (an indexed query per
group); everything else turns an ``(AwaitGroup, [AwaitSource])`` pair into text, so
both views and the tests share one formatter.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import dataclasses
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .await_store import ACTIVE_GROUP_STATES, AwaitGroup, AwaitSource

if TYPE_CHECKING:
    from .models import Session
    from .store import Store

#: Group states the AWAITING section shows: every ACTIVE state (the ones that still
#: hold the session's slot). delivered / expired / disarmed are history — ``-l`` only.
VISIBLE_STATES = ACTIVE_GROUP_STATES
#: Section header label, shared by the TUI rule and the ``ccc ls`` block.
AWAITING_LABEL = "AWAITING"
AWAITING_HINT = "(ccc await -l · -d GROUP disarms)"
_CMD_CHARS = 40


@dataclasses.dataclass
class AwaitEntry:
    """One visible group, its sources and the target session row (``None`` = unknown)."""

    group: AwaitGroup
    sources: list[AwaitSource]
    session: Session | None = None

    @property
    def cwd(self) -> str:
        """The folder the group resumes in (the session's, else the arm-time snapshot)."""
        return (self.session.cwd if self.session is not None else "") or self.group.cwd


def visible_awaits(store: Store) -> list[AwaitEntry]:
    """Every group in a :data:`VISIBLE_STATES` state (oldest first), with its sources
    and its session row. Plain reads only."""
    pairs = sorted(store.list_awaits(None, include_inactive=False), key=lambda p: p[0].id)
    return [
        AwaitEntry(group, sources, store.get(group.session_id))
        for group, sources in pairs
        if group.state in VISIBLE_STATES
    ]


def source_text(src: AwaitSource) -> str:
    """One source, compact: its ``-L`` label, else ``zoho #N`` / ``slack DM U…`` / ``cmd …``."""
    label = (src.label or "").strip()
    if label:
        return label
    spec: dict[str, Any] = src.spec_dict()
    if src.kind == "zoho-reply":
        return f"zoho #{spec.get('ticket') or '?'}"
    if src.kind == "slack-dm":
        return f"slack DM {spec.get('user_id') or '?'}"
    if src.kind == "cmd":
        cmd = " ".join(str(spec.get("cmd") or "").split())
        return "cmd " + (cmd[:_CMD_CHARS] + "…" if len(cmd) > _CMD_CHARS else cmd)
    return src.kind


def state_text(group: AwaitGroup) -> str:
    """The group's state; ``blocked(<reason>)`` when a reason is recorded."""
    if group.state == "blocked" and group.blocked_reason:
        return f"blocked({group.blocked_reason})"
    return group.state


def next_probe(sources: list[AwaitSource]) -> int:
    """Epoch of the soonest still-armed source's next probe (0 = none will be probed)."""
    due = [s.next_check_at for s in sources if s.state == "armed" and s.next_check_at]
    return min(due) if due else 0


def _date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "-"


def _clock(epoch: int, now: float) -> str:
    stamp = datetime.fromtimestamp(epoch)
    same_day = stamp.date() == datetime.fromtimestamp(now).date()
    return stamp.strftime("%H:%M" if same_day else "%m-%d %H:%M")


def summary(group: AwaitGroup, sources: list[AwaitSource], now: float) -> str:
    """``zoho #123 · slack DM U1 — armed · until 2026-10-05 23:59 · next 14:02``.

    Every source is listed (the fired winner and disarmed losers included, so the line
    still says what the group waited on); the next-probe part appears only while a
    source is still armed.
    """
    what = " · ".join(source_text(s) for s in sources) or "(no sources)"
    parts = [state_text(group), f"until {_date(group.until_epoch)}"]
    probe = next_probe(sources) if group.state in ("armed", "grace") else 0
    if probe:
        parts.append(f"next {_clock(probe, now)}")
    return f"{what} — " + " · ".join(parts)


def aim_text(entry: AwaitEntry) -> str:
    """The target session's compact AIM label ('' when the session or its AIM is unknown)."""
    from .models import display_aim  # pylint: disable=import-outside-toplevel

    if entry.session is None:
        return ""
    return display_aim(entry.session) or ""
