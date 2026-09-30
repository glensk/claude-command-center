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
import time
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


def read_channel_health(store: Store) -> dict[str, Any] | None:
    """The recorded channel health (``None`` = never recorded or unreadable). A read only."""
    try:
        return store.channel_health()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


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


#: What the ``-P`` line says for a group armed before ``ccc await -P`` existed.
NO_PURPOSE = "(no purpose recorded)"


def purpose_text(group: AwaitGroup) -> str:
    """The group's ``-P`` purpose, or :data:`NO_PURPOSE`."""
    return group.purpose.strip() or NO_PURPOSE


def items_text(group: AwaitGroup) -> str:
    """The group's ``-T`` related items, comma-joined ('' when none)."""
    return ", ".join(group.items_list())


def source_spec_text(src: AwaitSource) -> str:
    """One source's FULL spec, uncut: the whole shell command (and its cwd) of a ``cmd``,
    the ticket of a ``zoho-reply``, the user (and DM channel) of a ``slack-dm``."""
    spec: dict[str, Any] = src.spec_dict()
    if src.kind == "zoho-reply":
        return f"ticket #{spec.get('ticket') or '?'}"
    if src.kind == "slack-dm":
        channel = spec.get("channel")
        return f"user {spec.get('user_id') or '?'}" + (f" (DM {channel})" if channel else "")
    if src.kind == "cmd":
        cwd = spec.get("cwd")
        return f"$ {spec.get('cmd') or ''}" + (f"   (in {cwd})" if cwd else "")
    return src.spec or ""


def _source_detail(src: AwaitSource) -> str:
    parts = [src.kind]
    if (src.label or "").strip():
        parts.append(f"[{src.label.strip()}]")
    parts.append(source_spec_text(src))
    status = [src.state]
    if src.state == "armed" and src.next_check_at:
        status.append(f"next probe {_date(src.next_check_at)}")
    status.append(f"fails {src.fail_count}")
    if src.last_error:
        status.append(f"last error: {src.last_error}")
    return " ".join(parts) + " — " + " · ".join(status)


def _session_state(entry: AwaitEntry) -> str:
    """``parked`` / ``done`` / ``live (<status>)``; ``unknown session`` without a row."""
    if entry.session is None:
        return "unknown session"
    status = entry.session.status or "?"
    return status if status in ("parked", "done", "failed") else f"live ({status})"


def _session_detail(entry: AwaitEntry, root: str | None) -> str:
    """``folder · abcd1234 · parked — <AIM>``."""
    from . import colors  # pylint: disable=import-outside-toplevel

    text = f"{colors.short_folder(entry.cwd, root)} · {entry.group.session_id[:8]} · "
    text += _session_state(entry)
    if aim := aim_text(entry):
        text += f" — {aim}"
    return text


def _mark(value: object) -> str:
    return "✅" if value else "❌"


def channel_health_text(health: dict[str, Any] | None, now: float | None = None) -> str:
    """``Python API: ✅ · AppleScript: ❌ (checked 14:02)`` — the live-tab delivery channels
    as the poller last recorded them (``Store.channel_health``); ``?`` for both when never
    recorded. Pure: never probes iTerm."""
    if not health:
        return "Python API: ? · AppleScript: ?"
    text = f"Python API: {_mark(health.get('python_api'))} · "
    text += f"AppleScript: {_mark(health.get('applescript'))}"
    checked = int(health.get("checked_at") or 0)
    if checked:
        text += f" (checked {_clock(checked, time.time() if now is None else now)})"
    return text


def channel_detail_text(health: dict[str, Any] | None, now: float | None = None) -> str:
    """:func:`channel_health_text` plus, when the Python API is down, its reason."""
    text = channel_health_text(health, now)
    if health and not health.get("python_api"):
        reason = str(health.get("python_api_error") or "").strip()
        if reason:
            text += f" — Python API: {reason}"
    return text


def detail_lines(
    entry: AwaitEntry,
    root: str | None = None,
    channels: dict[str, Any] | None = None,
    now: float | None = None,
) -> list[tuple[str, str]]:
    """The TUI detail pane of an AWAITING row, one ``(field, value)`` per line.

    Read-only and pure (no store, no clock; *root* is the resolved repo root, see
    ``colors.short_folder``): Purpose, Related items, Target session,
    one ``Source N`` per source (kind, label, FULL spec, state, next probe, fail count,
    last error), Until, Group state, Armed at, Delivery channels (the recorded
    *channels*, :func:`channel_detail_text`), Resume prompt (the template, full text).
    """
    group = entry.group
    lines: list[tuple[str, str]] = [
        ("Purpose", purpose_text(group)),
        ("Related items", items_text(group) or "—"),
        ("Target session", _session_detail(entry, root)),
    ]
    for n, src in enumerate(entry.sources, 1):
        lines.append((f"Source {n}", _source_detail(src)))
    if not entry.sources:
        lines.append(("Sources", "(none)"))
    lines += [
        ("Until", _date(group.until_epoch)),
        ("Group state", f"{state_text(group)}  (group {group.id})"),
        ("Armed at", _date(group.created_at)),
        ("Delivery channels", channel_detail_text(channels, now)),
        ("Resume prompt", group.prompt_template or "—"),
    ]
    return lines
