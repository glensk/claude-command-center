"""The seat-full file and the status line's seat row (tp#2877 B7), stdlib only.

Every status-line capture judges every configured Claude account from the usage cache and
the cooldown store (no network) and keeps ``<app_home>/seat-full.json`` current::

    {"version": 1, "threshold_pct": 98.0,
     "accounts": {"private": {"full": true, "pct": 98.0, "room": false}, ...},
     "at": <epoch>}

``full`` is the relay pick's own "source full" rule (``quota._source_full`` over
``quota._pick_evidence``): an observed cooldown/hold block, or a reading younger than
:data:`FRESH_SEC` at/over the threshold on a live gate window. ``room`` is
``quota._has_room``: available, fresh, every live gate window below the threshold. ``pct``
is the highest live gate window (``null`` when there is none). The threshold and windows
are ccc's ``relay_switch_weekly_pct`` / ``relay_switch_windows``; an invalid value fails
open (``threshold_pct`` ``null``, nothing full). ``tests/test_seatfull.py`` pins every
restated rule against :mod:`.quota` and :mod:`.config`.

The file is rewritten atomically when its content (everything but ``at``) changes, and
also when any account is full and ``at`` is older than :data:`REFRESH_SEC`, so a reader
(the Stop hook) can trust a full verdict that is younger than 15 min.

Shared by ``cli.cmd_statusline`` and the lean ``entrypoint`` path, so it imports nothing
beyond :mod:`.usagecache` and the stdlib. Every public function is silent on error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import usagecache

VERSION = 1
FILE_NAME = "seat-full.json"
#: A full verdict is re-stamped at least this often (the Stop hook trusts 15 min).
REFRESH_SEC = 5 * 60
#: ``quota.PICK_FRESH_SEC``: a reading older than this proves neither full nor room.
FRESH_SEC = 15 * 60
#: ``quota._SESSION_STALE_AFTER_SEC`` / ``quota._WEEK_STALE_AFTER_SEC`` per window.
STALE_AFTER_SEC = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
#: ``config.RELAY_SWITCH_WINDOW_NAMES``.
WINDOW_NAMES = ("five_hour", "seven_day")
#: ``quota._COOLDOWNS_NAME``: any live entry (a rejection or a hold) blocks the account.
COOLDOWNS_NAME = "cooldowns.json"
#: ``quota._EXHAUSTED_PCT``: a live window at/over this blocks the account.
EXHAUSTED_PCT = 100.0
#: ``quota._DISPLAY_SEAT`` (the rule behind ``quota.seat_command``: ``private`` -> ``cpriv``).
DISPLAY_SEAT = {"default": "work", "private": "priv"}
#: How a gate window is named in the row.
WINDOW_WORDS = {"seven_day": "weekly", "five_hour": "5-hour"}

_ORANGE = "\033[38;5;214m"
_RED = "\033[38;5;196m"
_RESET = "\033[0m"


def policy(raw_pct: object, raw_windows: object) -> tuple[float | None, tuple[str, ...]]:
    """``config.relay_switch_policy``'s validation, stdlib only; ``(None, ())`` when invalid."""
    if isinstance(raw_pct, bool) or not isinstance(raw_pct, int | float):
        return None, ()
    pct = float(raw_pct)
    if not 1 <= pct <= 100:
        return None, ()
    if not isinstance(raw_windows, list) or not raw_windows:
        return None, ()
    windows: list[str] = []
    for name in raw_windows:
        if name not in WINDOW_NAMES:
            return None, ()
        if name not in windows:
            windows.append(name)
    return pct, tuple(windows)


def _live_cooldowns(app_home: Path, now: int) -> dict[str, dict]:
    """``quota.read_cooldowns``: live entries keyed by provider id, ``{}`` on any trouble."""
    data = usagecache._load_json_dict(app_home / COOLDOWNS_NAME)  # pylint: disable=protected-access
    entries = data.get("providers") if data else None
    if not isinstance(entries, dict):
        return {}
    live: dict[str, dict] = {}
    for pid, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        try:
            until = int(entry.get("blocked_until", 0) or 0)
        except (TypeError, ValueError):
            continue
        if until > now:
            live[pid] = entry
    return live


def judge_account(  # pylint: disable=too-many-arguments,too-many-locals
    app_home: Path,
    label: str,
    account_dir: Path,
    cooldown: dict | None,
    *,
    threshold: float,
    gate: tuple[str, ...],
    now: int,
) -> dict[str, Any]:
    """``{"full", "pct", "room", "window"}`` of one account (``window``: the gate max's name)."""
    # pylint: disable-next=protected-access
    data = usagecache._read_validated(app_home, label, account_dir)
    captured = usagecache._int_field(data, "captured_at")  # pylint: disable=protected-access
    windows: dict[str, tuple[float, int, bool]] = {}
    for name, stale_after in STALE_AFTER_SEC.items():
        win = usagecache._window(data.get(name)) if data else None  # pylint: disable=protected-access
        if win is not None:
            stale = (captured + stale_after) < now or win[1] <= now
            windows[name] = (win[0], win[1], stale)
    reading_at = captured if windows else 0
    fresh = bool(reading_at) and now - reading_at <= FRESH_SEC
    live = {name: w for name, w in windows.items() if not w[2]}
    gated = [(w[0], name) for name, w in live.items() if name in gate]
    top = max(gated) if gated else None
    pct = top[0] if top else None
    if cooldown is not None:
        state_ok, blocked_observed = False, True
    else:
        blocked_observed = False
        exhausted = any(w[0] >= EXHAUSTED_PCT for w in live.values())
        state_ok = bool(live) and not exhausted
    full = blocked_observed or (fresh and pct is not None and pct >= threshold)
    room = state_ok and fresh and pct is not None and pct < threshold
    return {
        "full": bool(full),
        "pct": None if pct is None else round(pct, 1),
        "room": bool(room),
        "window": top[1] if top else None,
    }


def judge(
    app_home: Path, dirs: dict[str, Path], raw_pct: object, raw_windows: object, now: int
) -> dict[str, Any]:
    """The seat-full document without ``at``; every configured account in config order."""
    threshold, gate = policy(raw_pct, raw_windows)
    cooldowns = _live_cooldowns(app_home, now) if threshold is not None else {}
    accounts: dict[str, dict[str, Any]] = {}
    for label, account_dir in dirs.items():
        if threshold is None:
            accounts[label] = {"full": False, "pct": None, "room": False, "window": None}
            continue
        accounts[label] = judge_account(
            app_home,
            label,
            account_dir,
            cooldowns.get(f"claude:{label}"),
            threshold=threshold,
            gate=gate,
            now=now,
        )
    return {"version": VERSION, "threshold_pct": threshold, "accounts": accounts}


def _content(doc: dict[str, Any]) -> dict[str, Any]:
    """*doc* as it is stored and compared: no ``at``, no ``window`` per account."""
    accounts = doc.get("accounts")
    return {
        "version": doc.get("version"),
        "threshold_pct": doc.get("threshold_pct"),
        "accounts": {
            label: {k: v for k, v in entry.items() if k != "window"}
            for label, entry in (accounts.items() if isinstance(accounts, dict) else ())
            if isinstance(entry, dict)
        },
    }


def keep_file(app_home: Path, doc: dict[str, Any], now: int) -> bool:
    """Write *doc* to the seat-full file when due; True when written. Never raises.

    Due: the stored content (``at`` aside) differs, or any account is full and the stored
    ``at`` is older than :data:`REFRESH_SEC` (or missing).
    """
    try:
        path = app_home / FILE_NAME
        content = _content(doc)
        stored = usagecache._load_json_dict(path)  # pylint: disable=protected-access
        if stored is not None and _content(stored) == content:
            any_full = any(entry.get("full") for entry in content["accounts"].values())
            stamped = stored.get("at")
            age_ok = isinstance(stamped, int | float) and now - stamped < REFRESH_SEC
            if not any_full or age_ok:
                return False
        app_home.mkdir(parents=True, exist_ok=True)
        usagecache._atomic_write_json(path, {**content, "at": now})  # pylint: disable=protected-access
        return True
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # never breaks
        return False


def refresh(
    app_home: Path, dirs: dict[str, Path], raw_pct: object, raw_windows: object, now: int
) -> dict[str, Any] | None:
    """Judge every account, keep the file current; the document, ``None`` on any error."""
    try:
        doc = judge(app_home, dirs, raw_pct, raw_windows, now)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # never breaks
        return None
    keep_file(app_home, doc, now)
    return doc


def seat_name(label: str) -> str:
    """The shell command of a Claude seat: ``private`` -> ``cpriv``, ``work`` -> ``cwork``."""
    return "c" + DISPLAY_SEAT.get(label, label)


def _pct_text(pct: float) -> str:
    return f"{round(pct, 1):g}"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def row_text(doc: dict[str, Any] | None, own: str | None) -> str:
    """The seat row (newline-terminated) when *own*'s seat is full, else ``""``. Never raises.

    ``⇄ cpriv 98 % weekly: the next relay moves to cwork (6 %)`` names the first other seat
    with proven room (config order, as the relay pick takes it); without one,
    ``⚠ cpriv and cwork at or above 98 %: staying on cpriv`` names every full seat, and an
    unproven other seat (no fresh reading) is named as such, never offered.
    """
    try:
        if not doc or not own or doc.get("threshold_pct") is None:
            return ""
        accounts: dict[str, dict[str, Any]] = doc["accounts"]
        mine = accounts.get(own)
        if not mine or not mine.get("full"):
            return ""
        thr = _pct_text(float(doc["threshold_pct"]))
        me = seat_name(own)
        others = [(label, entry) for label, entry in accounts.items() if label != own]
        target = next(((label, e) for label, e in others if e.get("room")), None)
        if target is not None:
            pct = mine.get("pct")
            word = WINDOW_WORDS.get(str(mine.get("window")), "")
            level = f"{_pct_text(float(pct))} % {word}".rstrip() if pct is not None else "blocked"
            other = f"{seat_name(target[0])} ({_pct_text(float(target[1]['pct']))} %)"
            return f"{_ORANGE}⇄ {me} {level}: the next relay moves to {other}{_RESET}\n"
        full = [me] + [seat_name(label) for label, e in others if e.get("full")]
        unproven = [seat_name(label) for label, e in others if not e.get("full")]
        why = f"{_join(full)} at or above {thr} %"
        if unproven:
            why += f", no fresh reading below it for {_join(unproven)}"
        return f"{_RED}⚠ {why}: staying on {me}{_RESET}\n"
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # never breaks
        return ""


def statusline_row(
    app_home: Path,
    dirs: dict[str, Path],
    cfg: tuple[object, object],
    own: str | None,
    *,
    now: int,
    doc: dict[str, Any] | None = None,
) -> str:
    """The seat row for *own* from *doc*, judged afresh when *doc* is ``None``. Never raises."""
    if not own:
        return ""
    if doc is None:
        try:
            doc = judge(app_home, dirs, cfg[0], cfg[1], now)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return ""
    return row_text(doc, own)


__all__ = [
    "FILE_NAME",
    "REFRESH_SEC",
    "judge",
    "keep_file",
    "policy",
    "refresh",
    "row_text",
    "statusline_row",
]
