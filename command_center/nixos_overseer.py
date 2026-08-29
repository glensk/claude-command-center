#!/usr/bin/env python3
"""Read-only view of an external homelab **overseer** alert-triage daemon.

This is a passive reader for an SQLite database owned by a *separate* project — a
home-server "overseer" that triages infrastructure alerts into incidents and
proposes remediation plans. It is entirely unrelated to ccc's own future-job
plumbing; nothing here shares state with, or knows about, ccc's sessions.

Two TUI usage cards read this DB on the render tick:

* **supervised** — incidents awaiting a human decision (``read_supervised``),
* **tier_a** — recent automatic activity (``read_tier_a``).

Both reads are deliberately cheap (one indexed query each) and never raise:
they run on the UI thread every ``usage_refresh_sec`` (~5 s). Every failure mode
— the feature being off (``nixos_overseer_dir`` unset), the file missing, a
``sqlite3`` error, or a locked/busy DB — returns an :class:`OverseerResult`
*sentinel* that the renderers turn into a one-line placeholder. The DB is opened
read-only (``mode=ro``) against the *live* WAL file, so ``immutable=1`` is
deliberately NOT used.

The external schema is fixed (owned by the other repo)::

    incidents(id TEXT, fingerprint TEXT, first_seen INT, last_seen INT,
              occurrences INT, status TEXT, tier TEXT, track TEXT, title TEXT,
              md_path TEXT, model TEXT, session_id TEXT, cost_usd REAL)
    kv(key TEXT, value TEXT)

``first_seen`` / ``last_seen`` are Unix epoch **seconds**.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from .config import Config

# Incident statuses that mean "a human still has to act" — the supervised card.
_SUPERVISED_STATUSES: tuple[str, ...] = (
    "proposed_tier_b",
    "needs_supervised_plan",
    "open_unverified",
)
# tier_a card: recent automatic activity, most-recent-first, capped with a tail.
_TIER_A_WINDOW_SEC = 7 * 86400
_TIER_A_CAP = 10
# SQLite open timeout — a live WAL DB may be briefly busy; fail fast, never block the UI.
_DB_TIMEOUT_SEC = 0.2

# Sentinel states an OverseerResult can carry (drives the placeholder the renderer shows).
STATE_OK = "ok"
STATE_DIR_UNSET = "dir_unset"  # feature off: nixos_overseer_dir == ""
STATE_DB_MISSING = "db_missing"  # dir set but the sqlite file is not there
STATE_ERROR = "error"  # OperationalError / locked / any sqlite failure


@dataclass(frozen=True)
class OverseerRow:
    """One incident row projected for a card (``<id> <status> <fingerprint> <age>``)."""

    id: str
    status: str
    fingerprint: str
    first_seen: int
    last_seen: int


@dataclass(frozen=True)
class OverseerResult:
    """Read outcome: either OK with rows, or a sentinel state for the placeholder.

    ``dispatch_disabled`` (supervised only) flags a halted daemon; ``more`` (tier_a
    only) is the count of rows beyond the display cap, rendered as a ``… +N more`` tail.
    """

    state: str
    rows: tuple[OverseerRow, ...] = field(default_factory=tuple)
    dispatch_disabled: bool = False
    more: int = 0


def db_path(cfg: Config) -> Path:
    """Path to the external overseer SQLite DB under ``nixos_overseer_dir``.

    ``<nixos_overseer_dir>/state/overseer.sqlite``. Only meaningful when the feature
    is enabled (``nixos_overseer_dir`` non-empty); callers gate on that first.
    """
    return Path(cfg.nixos_overseer_dir).expanduser() / "state" / "overseer.sqlite"


def _overseer_dir(cfg: Config) -> Path:
    return Path(cfg.nixos_overseer_dir).expanduser()


def _connect(path: Path) -> sqlite3.Connection:
    """Open the live WAL DB read-only (``mode=ro``, never ``immutable=1``)."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_DB_TIMEOUT_SEC)


def _sentinel(cfg: Config) -> OverseerResult | None:
    """Return the pre-query sentinel (dir unset / db missing), or None to proceed."""
    if not cfg.nixos_overseer_dir:
        return OverseerResult(STATE_DIR_UNSET)
    try:
        exists = db_path(cfg).exists()
    except OSError:
        exists = False
    if not exists:
        return OverseerResult(STATE_DB_MISSING)
    return None


def _select(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> tuple[OverseerRow, ...]:
    """Run *sql* (selecting id, status, fingerprint, first_seen, last_seen) → rows."""
    cur = conn.execute(sql, params)
    return tuple(
        OverseerRow(
            id=str(row[0] or ""),
            status=str(row[1] or ""),
            fingerprint=str(row[2] or ""),
            first_seen=int(row[3] or 0),
            last_seen=int(row[4] or 0),
        )
        for row in cur.fetchall()
    )


def _dispatch_disabled(conn: sqlite3.Connection, cfg: Config) -> bool:
    """True if the daemon is halted: a ``DISABLED`` file, or kv ``auto_disabled='1'``.

    Swallows its own sqlite errors (a missing ``kv`` table must not fail the read that
    already fetched the incident rows).
    """
    try:
        if (_overseer_dir(cfg) / "DISABLED").exists():
            return True
    except OSError:
        pass
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = 'auto_disabled'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]) == "1"


def read_supervised(cfg: Config) -> OverseerResult:
    """Incidents awaiting the human, newest first — the supervised card's data.

    Statuses ``proposed_tier_b`` / ``needs_supervised_plan`` / ``open_unverified``,
    ordered by ``first_seen`` DESC. Also reports whether dispatch is disabled. Never
    raises: any failure returns a sentinel :class:`OverseerResult`.
    """
    sentinel = _sentinel(cfg)
    if sentinel is not None:
        return sentinel
    try:
        with _connect(db_path(cfg)) as conn:
            rows = _select(
                conn,
                "SELECT id, status, fingerprint, first_seen, last_seen FROM incidents "
                "WHERE status IN (?, ?, ?) ORDER BY first_seen DESC",
                _SUPERVISED_STATUSES,
            )
            disabled = _dispatch_disabled(conn, cfg)
    except sqlite3.Error:
        return OverseerResult(STATE_ERROR)
    return OverseerResult(STATE_OK, rows=rows, dispatch_disabled=disabled)


def read_tier_a(cfg: Config, now: int | None = None) -> OverseerResult:
    """Recent tier-A (automatic) activity, newest first, capped at 10 with a tail.

    Any status, ``tier='a'`` and ``first_seen`` within the last 7 days, ordered by
    ``first_seen`` DESC. Rows beyond :data:`_TIER_A_CAP` are counted into ``more``.
    Never raises: any failure returns a sentinel :class:`OverseerResult`.
    """
    now = int(time.time()) if now is None else now
    sentinel = _sentinel(cfg)
    if sentinel is not None:
        return sentinel
    cutoff = now - _TIER_A_WINDOW_SEC
    try:
        with _connect(db_path(cfg)) as conn:
            rows = _select(
                conn,
                "SELECT id, status, fingerprint, first_seen, last_seen FROM incidents "
                "WHERE tier = 'a' AND first_seen >= ? ORDER BY first_seen DESC",
                (cutoff,),
            )
    except sqlite3.Error:
        return OverseerResult(STATE_ERROR)
    more = max(0, len(rows) - _TIER_A_CAP)
    return OverseerResult(STATE_OK, rows=rows[:_TIER_A_CAP], more=more)


def card_title(result: OverseerResult, base: str) -> str:
    """Border title with a live count — ``base (N)`` — when the read succeeded.

    N counts every incident in the category: the supervised read is uncapped
    (``more`` is always 0) and the tier_a count includes the rows folded into
    the ``… +N more`` tail. Sentinel states (dir unset / db missing / error)
    render the bare *base* so a broken source never shows a misleading ``(0)``.
    """
    if result.state != STATE_OK:
        return base
    return f"{base} ({len(result.rows) + result.more})"


def _humanize_age(epoch_sec: int, now: int) -> str:
    """Compact age from a Unix-seconds timestamp: ``45s``/``12m``/``3h``/``5d``/``2w``."""
    if not epoch_sec:
        return "—"
    seconds = max(0, now - epoch_sec)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    return f"{days // 7}w"


def _placeholder(result: OverseerResult) -> Text | None:
    """The grey one-line placeholder for a sentinel result, or None when OK."""
    if result.state == STATE_DIR_UNSET:
        return Text("—\n(set nixos_overseer_dir in config.toml)", style="grey50")
    if result.state == STATE_DB_MISSING:
        return Text("—\n(overseer db not found)", style="grey50")
    if result.state == STATE_ERROR:
        return Text("—\n(overseer db unavailable)", style="grey50")
    return None


def _append_row(text: Text, row: OverseerRow, now: int) -> None:
    """Append one ``<id>  <status>  <fingerprint>  <age>`` line (trailing newline)."""
    age = _humanize_age(row.last_seen, now)
    text.append(f"{row.id}  {row.status}  {row.fingerprint}  {age}\n")


def render_supervised(result: OverseerResult, now: int | None = None) -> Text:
    """Render the supervised card — incidents awaiting the human — as Rich ``Text``.

    A halted daemon prepends a red ``⛔ dispatch disabled`` line. Zero rows is the good
    state (``— none —``, green). Otherwise the rows are followed by a dim approve hint.
    """
    now = int(time.time()) if now is None else now
    placeholder = _placeholder(result)
    if placeholder is not None:
        return placeholder
    text = Text()
    if result.dispatch_disabled:
        text.append("⛔ dispatch disabled\n", style="bold red")
    if not result.rows:
        text.append("— none —", style="green3")
        return text
    for row in result.rows:
        _append_row(text, row, now)
    text.append("approve: overseer.py approve <id> --close", style="dim")
    return text


def render_tier_a(result: OverseerResult, now: int | None = None) -> Text:
    """Render the tier-A card — recent automatic activity — as Rich ``Text``.

    Zero rows renders a neutral ``— none —``; a truncated list gets a dim ``… +N more``
    tail.
    """
    now = int(time.time()) if now is None else now
    placeholder = _placeholder(result)
    if placeholder is not None:
        return placeholder
    if not result.rows:
        return Text("— none —", style="grey50")
    text = Text()
    for row in result.rows:
        _append_row(text, row, now)
    if result.more:
        text.append(f"… +{result.more} more", style="dim")
    else:
        text.rstrip()
    return text
