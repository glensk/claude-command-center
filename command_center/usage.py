#!/usr/bin/env python3
"""Account-wide subscription usage snapshots — Claude Code's ``/usage`` and Codex.

Two providers, same two-window shape (a 5h session + a weekly window). Claude's
numbers arrive via the status-line JSON (captured by ``ccc statusline
--capture-usage``). Codex has two sources: the LIVE ChatGPT usage endpoint
(:func:`fetch_codex_usage` — the very numbers the *Settings → Usage* page shows,
authorized by the token in ``$CODEX_HOME/auth.json``, opt-in via ``codex_usage``) and,
as the offline fallback, the ``rate_limits`` block Codex writes onto each
``token_count`` event in its session rollout files (the windows are identified by their
duration, not primary/secondary position). Codex emits more than one block shape —
``limit_id: "codex"`` carries the windows, while short ``codex exec`` runs log a
windowless ``limit_id: "premium"`` block — so the reader skips windowless blocks and
scans back through enough files to find the freshest one with real data.
:func:`read_codex_usage` then serves whichever of the two is NEWER, so a live figure
never loses to a stale rollout event (and vice versa when the endpoint is unreachable).

Claude's data rides on every API response's ``anthropic-ratelimit-unified-{5h,7d}-*``
headers (``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}`` in the
status-line JSON). The account *totals* are global, but the snapshot in any given
session's status-line JSON only reflects **that session's last API response** — an
idle session keeps reporting a stale block (percentages and ``resets_at`` from days
ago) long after the window actually rolled. Since every concurrent session writes
the one shared ``usage.json`` every few seconds, a stale writer used to clobber a
fresh one and the card would flicker / show a past reset as "Resets now". So
:func:`write_usage` now **merges**: a live window's reset is always in the future,
so a ``resets_at <= now`` is discarded as stale, the one with the later reset wins,
and at an equal reset (idle sessions share the fixed weekly boundary) the higher
cumulative ``used_percentage`` wins so the card neither flickers nor reads
"Resets now". ``five_hour`` → the "Session:" bar, ``seven_day`` → the "Week:" bar.
Reset times are rendered
**relative** (``Session: Resets in 1h 4m``), embossed inside the bar and recomputed
each refresh; the TUI shows both providers' cards top-right of the detail pane.
"""

# pylint: disable=too-many-lines  # cohesive multi-provider usage module (Claude/Codex/Copilot)
from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import base64
import calendar
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

from . import config
from .codex_in_claude import (
    _FIVE_HOUR_MINUTES,
    _SEVEN_DAY_MINUTES,
    _latest_rate_limits_event,
    codex_refusal,
    refusal_label,
    short_refusal_label,
)

# Statuses that mean a job is actively doing work — while any tracked session is in one
# of these the *expensive* usage fetch (currently only Copilot's ``gh`` billing call)
# switches to its shorter "active" throttle so the card tracks reality more closely. Kept
# as raw string values (not a ``models`` import) to keep this low-level module decoupled;
# a test asserts they stay in lock-step with ``models.Status.{WORKING,SNOOZED}``.
_ACTIVE_STATUS_VALUES = frozenset({"working", "snoozed"})


def has_active_work(status_values: Iterable[str]) -> bool:
    """True if any status value marks a session actively working (WORKING or SNOOZED)."""
    return any(value in _ACTIVE_STATUS_VALUES for value in status_values)


def adaptive_interval(idle_sec: float, active_sec: float, *, active: bool) -> float:
    """Pick a refresh cadence: the shorter *active_sec* while a job works, else *idle_sec*.

    A non-positive or not-actually-shorter *active_sec* is ignored (falls back to
    *idle_sec*), so a misconfigured active value can only ever make refreshes *more*
    frequent, never less — and setting it to ``0`` cleanly disables the speed-up.
    """
    if active and 0 < active_sec < idle_sec:
        return active_sec
    return idle_sec


# Bar look — a "used" portion on a dark slate track, the relative reset embossed inside
# the bar, percentage flush-right. The two Claude cards colour each bar from *its own*
# usage (green/orange/red thresholds, see _fill_for_pct); Codex and Copilot keep a single
# flat brand fill. A card row (see _bar_row) is exactly _CARD_INNER_WIDTH wide and the
# percentage butts directly against the bar's end — no gap between the two — so the bar
# takes _CARD_INNER_WIDTH minus the percentage's own width. _BAR_WIDTH is only the
# fallback for a bare _bar() with no percentage after it; it still comfortably holds the
# longest embossed label ("Week: Resets in 6d 23h 59m" = 26 chars).
_BAR_WIDTH = 27
# Content width inside a usage card: the CSS min-width is 38, minus the round border
# (1 each side) and the 0 1 padding (1 each side) = 34. Keep in sync with the #usage*
# rules in views/tui.py.
_CARD_INNER_WIDTH = 34
_FILL_COLOR = "#b3b0f0"  # legacy flat Claude fill (light periwinkle) — kept as the _bar/
# _section default; the Claude cards now colour per-bar via _fill_for_pct (below).
# Per-bar Claude fills, chosen from each bar's own usage percentage (see _fill_for_pct):
_FILL_GREEN = "#3fb950"  # 0–65% used (healthy)
_FILL_ORANGE = "#d29922"  # 66–85% used (warning)
_FILL_RED = "#f85149"  # 86–100% used (critical)
_CODEX_FILL = "#19c37d"  # Codex "used" portion (OpenAI green) — distinguishes the two cards
_COPILOT_FILL = "#a371f7"  # GitHub Copilot accent (violet) — third card's border + figure
_TRACK_COLOR = "#3b3f5c"  # remaining (dark slate)
_PCT_STYLE = "#c8c8d8"
_LABEL_STYLE = "bold #d7d7e6"
_RESET_STYLE = "grey58"
_CLAUDE_ACCENT = "#ffaf00"  # private Claude card's gold border — its reset-label colour
_CLAUDE_WORK_ACCENT = "#6cb6ff"  # work Claude card's blue border/reset colour (same product)
# Reset text is embossed onto the bar: over the bright filled portion it is drawn dark,
# over the dark track it takes the card's accent colour (so it both matches the box and
# stays legible). The bar's fill/track colours remain as each glyph's background, so usage
# is still fully visible behind the text.
_OVERLAY_ON_FILL = "#11131f"  # dark glyphs over the bright "used" portion

# read_codex_usage scans at most this many newest rollout files for a *usable*
# rate_limits block before giving up, and caches the parse by the newest file's
# (path, mtime). Kept generous because short ``codex exec`` runs (the ones ccc itself
# spawns for short-aim/delegate) log a windowless ``limit_id: "premium"`` block and
# nothing else, so dozens of them can pile up newer than the freshest interactive
# session that actually carries the 5h/weekly windows — observed >25 deep. Each file
# is small JSONL and the result is cached, so a deep scan stays cheap (~tens of ms).
_CODEX_SCAN_LIMIT = 200
# CODEX_HOME (as a string) -> (cache key, parsed snapshot). Keyed per home because the
# TUI renders two Codex cards from two homes on the same tick, and a single slot would
# then miss on every one of them. Each key pairs the newest rollout file's (path, mtime)
# with the live cache file's, so ANY new data invalidates it and nothing else does.
_codex_cache: dict[str, tuple[tuple[str, int, str, int], Usage | None]] = {}

# The live ChatGPT usage endpoint — exactly what the web *Settings → Usage* page reads.
# Authorized by the ChatGPT OAuth access token Codex stores in ``$CODEX_HOME/auth.json``
# (``tokens.access_token`` + ``tokens.account_id``; the token is a JWT valid ~10 days and
# `codex` itself refreshes it whenever it runs). Never logged or printed.
_WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Windows are identified by their DURATION, never by primary/secondary position — the
# same rule the rollout reader follows (Codex has been seen putting the weekly window in
# ``primary``). Anything of another length is ignored rather than guessed at.
_WHAM_FIVE_HOUR_SEC = 5 * 3600
_WHAM_SEVEN_DAY_SEC = 7 * 86400
# JSON-RPC ids for the ``codex app-server`` fallback (see _fetch_codex_usage_appserver).
_APPSERVER_INIT_ID = 1
_APPSERVER_LIMITS_ID = 2
_APPSERVER_TIMEOUT_SEC = 20
# (auth.json path, mtime_ns) -> the account e-mail parsed out of its id_token JWT. The
# TUI asks for both cards' titles on every 5 s tick, so the decode is memoized on the
# file's own mtime and re-runs only when `codex login` rewrites it.
_codex_email_cache: dict[tuple[str, int], str | None] = {}

# Claude's OAuth usage endpoint — the same numbers `claude` shows in `/usage`, including
# the Fable-model-scoped weekly window the status-line ``rate_limits`` payload does NOT
# carry (it only ships ``five_hour`` + ``seven_day``). Fetched out-of-band per account.
_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Window at which a stored authoritative OAuth fetch still outranks an incoming
# status-line window in the merge (see :func:`_merge_window`). One hour: long enough to
# survive a persistently-idle session replaying its pre-rebase ``rate_limits`` every 3s,
# short enough that the periodic fetch keeps re-asserting authority.
_OAUTH_AUTHORITY_SEC = 3600

# Ceiling on a persisted 429 backoff. The OAuth usage endpoint has been seen to answer a
# 429 with a very large ``Retry-After`` (observed 3357 s, and it can be larger); we honour
# the server's wait but never longer than this, so a bogus/huge value cannot wedge the
# fetch for hours. See :func:`fetch_claude_usage` + :func:`claude_usage_stale`.
_OAUTH_BACKOFF_CAP_SEC = 7200

# After this long without a *successful* OAuth fetch the Fable weekly figure is stale
# enough that the card marks it (``Fable: stale <age>`` instead of ``Fable: Resets …``) —
# a frozen number (e.g. a persistent 429 backoff) is then never shown as if it were live.
_FABLE_STALE_AFTER_SEC = 3600

# Session/Week staleness for the two Claude cards, keyed on ``Usage.captured_at`` (the
# last time ANY live session under that account talked to the API, or an OAuth fetch
# ran — whichever is more recent). Past this age the figure predates its own window's
# lifetime, so it is no longer a trustworthy reading of "usage right now": the bar is
# replaced with a bare "?%" (see :func:`_section`) instead of a colour that implies
# live data. Thresholds mirror each window's own duration — a 5h-old session figure or
# a 7d-old weekly figure is stale almost by definition.
_SESSION_STALE_AFTER_SEC = 5 * 3600
_WEEK_STALE_AFTER_SEC = 7 * 86400


@dataclass
class Window:
    """One rate-limit window (the 5-hour session or the 7-day week)."""

    used_percentage: float
    resets_at: int  # Unix epoch seconds when the window resets


@dataclass
class Usage:  # pylint: disable=too-many-instance-attributes  # flat snapshot record
    """A captured snapshot of the account's rate-limit windows."""

    captured_at: int  # Unix epoch seconds when ccc recorded it
    five_hour: Window | None
    seven_day: Window | None
    # The Fable-model-scoped weekly window from the OAuth usage endpoint (the status
    # line never carries it). Defaulted None: it only exists on a snapshot healed by an
    # OAuth fetch, and never on its own without a main window in practice.
    fable_week: Window | None = None
    # Epoch seconds of the last *successful* OAuth fetch that produced this snapshot (0 on
    # status-line captures and Codex snapshots). Drives the ``Fable: stale <age>`` marker in
    # the render path — see :data:`_FABLE_STALE_AFTER_SEC` and :func:`_render_card`.
    oauth_fetched_at: int = 0
    # Codex only: non-empty when Codex is REFUSING calls for a reason that is not a
    # window — today `workspace_owner_credits_depleted`. The windows above stay whatever
    # the last successful call reported, so without this the card shows healthy headroom
    # while nothing works. See :func:`codex_in_claude.codex_refusal`.
    blocked_reason: str = ""
    blocked_at: int = 0  # epoch seconds of the refusal that set ``blocked_reason``
    # Codex only: True when the windows came from the LIVE usage endpoint
    # (:func:`fetch_codex_usage`) rather than a rollout file. The blocked banner says
    # so — live figures need no "these numbers are N old" caveat.
    live: bool = False
    email: str = ""  # account the figures belong to (live payload / auth.json JWT)
    plan_type: str = ""  # e.g. "team" / "plus" — as reported by the live payload

    def is_empty(self) -> bool:
        return self.five_hour is None and self.seven_day is None

    @property
    def blocked(self) -> bool:
        """True when the provider refuses calls regardless of what the windows say."""
        return bool(self.blocked_reason)


def codex_exhausted_window(
    snapshot: Usage | None, now: int | None = None
) -> tuple[str, Window] | None:
    """The exhausted live Codex window, matching ``codex-in-claude.py``'s preflight.

    A window counts only when it is still live (``resets_at`` in the future) and is
    at least 100% used. Stale snapshots whose reset already passed do not block. If
    both windows are exhausted, return the most-consumed one, which is the same
    "most consumed live window" signal used by the Codex delegate quota preflight.
    """
    if snapshot is None:
        return None
    now = int(time.time()) if now is None else now
    live = [
        (label, win)
        for label, win in (("5h", snapshot.five_hour), ("weekly", snapshot.seven_day))
        if win is not None and win.resets_at > now and win.used_percentage >= 100.0
    ]
    if not live:
        return None
    return max(live, key=lambda item: item[1].used_percentage)


def _account_config_dir(account: str) -> Path:
    """Resolved config dir for *account* (falls back to ``claude_home()``).

    The account's dir is the input to its usage-cache hash. Looked up in
    :func:`config.claude_config_dirs`; an unconfigured label (including the default
    ``private`` when the user has remapped the account set) degrades to
    ``claude_home()`` so the hash stays deterministic — a mismatch there simply makes
    :func:`read_usage` refuse, which is the intended fail-closed behaviour.
    """
    return config.claude_config_dirs().get(account) or config.claude_home()


def _account_hash(account: str) -> str:
    """8-hex ``sha256`` of the account's resolved config dir — keys the usage cache."""
    return hashlib.sha256(str(_account_config_dir(account)).encode()).hexdigest()[:8]


def _usage_path(account: str = "private") -> Path:
    """Per-account usage-cache path.

    The default account (label ``private``) keeps the back-compat ``usage.json``; any
    other label writes ``usage-<label>-<hash8>.json`` (``hash8`` = the account's config
    dir hash), so two accounts never share a file and label reuse never collides.
    """
    base = config.app_home()
    if account == "private":
        return base / "usage.json"
    return base / f"usage-{account}-{_account_hash(account)}.json"


@contextlib.contextmanager
def _flock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory ``flock`` on *lock_path* for a critical section.

    Serializes the read-merge-write across every concurrent statusline writer (each
    session calls :func:`write_usage` every few seconds), so the shared cache file is
    never interleaved. The lock file itself is a persistent zero-byte sentinel.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically replace *path* with *payload* via a unique ``mkstemp`` temp file.

    A per-write unique temp name (not a fixed ``.json.tmp``) plus ``os.replace`` means
    concurrent writers cannot clobber one another's temp file.

    The ``finally`` unlink covers every *exception* path (the old ``except OSError``
    leaked on ``TypeError`` from ``json.dump`` and on ``KeyboardInterrupt``, which
    contradicted :func:`write_usage`'s "never raises" contract). It cannot cover
    asynchronous death: Claude Code cancels an in-flight status-line command when the
    next update arrives, and this writer is the last thing the status-line script runs
    every few seconds — so the process is killed outright with no Python unwinding.
    Those orphans are reclaimed by :func:`sweep_stale_temps`, not here.

    Deliberately no ``fsync``: this is a cache refreshed every few seconds, and the
    extra latency would widen the very cancellation window that strands the temp file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, path)
        replaced = True
    finally:
        # Only when the replace did NOT happen — otherwise tmp_name IS *path* now.
        if not replaced:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


# Temp files this module strands when a writer is killed mid-write. Enumerated rather
# than a generic ``*.json.*.tmp`` glob so a future unrelated JSON writer in this
# directory cannot silently join the deletion set. Every OTHER temp producer in the
# repo uses a fixed name (``futuresync``/``mirrors``: ``<name>.tmp``; ``resume``:
# ``resume_queue.tmp``; ``install``: ``.ccc-tmp-*``) or another directory.
_TEMP_PATTERNS = (
    "usage.json.*.tmp",
    "usage-*.json.*.tmp",
    "copilot_usage.json.*.tmp",
    "codex_usage-*.json.*.tmp",  # the per-CODEX_HOME live-usage caches
    # :mod:`.quota`'s observed-rejection store writes through _atomic_write_json too, so a
    # killed writer strands the same shape of orphan here.
    "cooldowns.json.*.tmp",
)
# A live temp exists for well under a millisecond, so an hour is ~7 orders of margin.
_TEMP_MAX_AGE_SEC = 3600


def sweep_stale_temps(now: float | None = None, *, max_age_sec: int = _TEMP_MAX_AGE_SEC) -> int:
    """Reclaim orphaned ``mkstemp`` temps left by a killed writer. Returns the count.

    Only files older than *max_age_sec* are removed, so a concurrently running writer's
    temp is never touched. The mtime reference is conservative in the right direction:
    ``mkstemp`` stamps it at creation and the flush at ``close()`` advances it, so age is
    measured from the most recent write.
    """
    now = time.time() if now is None else now
    home = config.app_home()
    removed = 0
    for pattern in _TEMP_PATTERNS:
        try:
            candidates = list(home.glob(pattern))
        except OSError:
            continue
        for tmp in candidates:
            try:
                if now - tmp.stat().st_mtime < max_age_sec:
                    continue
                tmp.unlink()
            except OSError:
                continue
            removed += 1
    return removed


def _window(raw: object) -> Window | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    resets = raw.get("resets_at")
    if pct is None or resets is None:
        return None
    try:
        return Window(used_percentage=float(pct), resets_at=int(resets))
    except (TypeError, ValueError):
        return None


def _merge_window(
    incoming: Window | None,
    stored: Window | None,
    now: int,
    *,
    stored_authoritative: bool = False,
    incoming_stale: bool = False,
) -> Window | None:
    """Pick the live window between a fresh capture and the persisted one.

    A window's reset is always in the *future*; ``resets_at <= now`` means the
    capturing session simply hasn't talked to the API since the window rolled, so
    its snapshot is stale — discard it. Between two live windows keep the one with
    the later ``resets_at`` (the account-global boundary only ever moves forward, so
    a smaller one is an older session's view), and **on an equal reset keep the
    higher ``used_percentage``**: usage within a fixed window is cumulative, so the
    larger figure is the freshest total — this stops idle sessions, which share the
    same weekly boundary but report a days-old lower total, from flip-flopping the
    card (e.g. 8% ↔ 28%). A genuine reset moves ``resets_at`` forward, so the new
    low percentage is still adopted (later reset beats higher percentage).

    **Authority guard** (*stored_authoritative*): when the stored window was written
    by a recent authoritative OAuth fetch (within :data:`_OAUTH_AUTHORITY_SEC`) and is
    still LIVE, two kinds of incoming status-line windows are REJECTED (stored kept):

    - one whose reset is *later* than the stored one, and
    - ANY window from a stale payload (*incoming_stale* — the reporting session's own
      5-hour window is dead/absent, i.e. it has not talked to the API for >5h).

    Anthropic can rebase windows at a rollout (seen at Fable-5: the weekly boundary
    moved BACKWARD Jul 15 → Jul 11, and the weekly percentage was recalibrated DOWN,
    84% → 3%, at the *same* boundary); a long-idle session then replays its pre-rebase
    ``rate_limits`` every 3s, and the plain "later reset wins" / "same reset, higher
    percentage wins" rules would re-pin the stale figures minutes after every
    authoritative fetch heals them. An ACTIVE session's same-reset increase still wins
    (the ~3s fast path between fetches survives). A genuine forward roll is unaffected:
    a truly reset stored window is dead (``resets_at <= now``), dropped from the live
    set, so the incoming later window is adopted as today regardless of authority.
    """
    live = [w for w in (incoming, stored) if w is not None and w.resets_at > now]
    if not live:
        return None
    if stored_authoritative and stored is not None and stored.resets_at > now:
        if incoming_stale:
            return stored
        if incoming is not None and incoming.resets_at > stored.resets_at:
            return stored
    return max(live, key=lambda w: (w.resets_at, w.used_percentage))


def _window_dict(win: Window | None) -> dict | None:
    """Serialize a :class:`Window` to its cache-JSON shape (``None`` stays ``None``)."""
    if win is None:
        return None
    return {"used_percentage": win.used_percentage, "resets_at": win.resets_at}


def write_usage(rate_limits: object, *, account: str = "private", now: int | None = None) -> bool:
    """Merge a ``rate_limits`` snapshot into *account*'s cache; ``True`` if written.

    Skips writing when neither window is present so an empty payload (rate_limits
    is absent until the first API response of a session) never clobbers a good
    snapshot. The whole read-merge-write runs under a per-account ``flock`` so the
    concurrent statuslines of every live session (each calling this every few seconds)
    cannot interleave. Each window is merged against the persisted one via
    :func:`_merge_window` — a stale writer (past/older ``resets_at``, or a lower
    cumulative percentage at the same reset) can neither pull the snapshot backward,
    persist a past reset that would render as "Resets now", nor flip-flop the
    percentage. Merging stays strictly WITHIN one account (the prior read is scoped to
    *account*). The payload stamps the account's ``config_dir_hash`` so a later reader
    can refuse a snapshot left by a different config dir under the same label. Returns
    ``False`` when nothing live survives the merge. Never raises.

    The OAuth-only fields (``fable_week`` + ``oauth_fetched_at`` + ``oauth_backoff_until``)
    are PRESERVED verbatim from the stored snapshot: status-line payloads never carry them,
    so clobbering them to None/0 on every 3-second status-line write would erase what
    :func:`fetch_claude_usage` fetched (or the 429 backoff it recorded). While the stored
    ``oauth_fetched_at`` is fresh (within :data:`_OAUTH_AUTHORITY_SEC`) the merge treats the
    stored windows as authoritative — see :func:`_merge_window`'s re-pin guard.
    """
    if not isinstance(rate_limits, dict):
        return False
    incoming_five = _window(rate_limits.get("five_hour"))
    incoming_seven = _window(rate_limits.get("seven_day"))
    if incoming_five is None and incoming_seven is None:
        return False
    now = int(time.time()) if now is None else now
    path = _usage_path(account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path.with_name(path.name + ".lock")):
            prev = _read_validated(account)
            prev_five = _window(prev.get("five_hour")) if prev else None
            prev_seven = _window(prev.get("seven_day")) if prev else None
            prev_fable = _window(prev.get("fable_week")) if prev else None
            prev_oauth = _int_field(prev, "oauth_fetched_at") if prev else 0
            authoritative = 0 < prev_oauth and (now - prev_oauth) < _OAUTH_AUTHORITY_SEC
            # A payload whose own 5-hour window is dead/absent comes from a session that
            # has not talked to the API for >5h — its weekly figure is at least that old
            # too, so under authority it may fill gaps but never override (see
            # _merge_window's guard; the 84%-replay case).
            payload_stale = incoming_five is None or incoming_five.resets_at <= now
            five = _merge_window(
                incoming_five,
                prev_five,
                now,
                stored_authoritative=authoritative,
                incoming_stale=payload_stale,
            )
            seven = _merge_window(
                incoming_seven,
                prev_seven,
                now,
                stored_authoritative=authoritative,
                incoming_stale=payload_stale,
            )
            if five is None and seven is None:
                return False
            payload = {
                "captured_at": now,
                "config_dir_hash": _account_hash(account),
                "five_hour": _window_dict(five),
                "seven_day": _window_dict(seven),
                # OAuth-only fields preserved verbatim (status line never carries them).
                "fable_week": _window_dict(prev_fable),
                "oauth_fetched_at": prev_oauth,
                "oauth_backoff_until": _int_field(prev, "oauth_backoff_until") if prev else 0,
            }
            _atomic_write_json(path, payload)
    except OSError:
        return False
    return True


def _load_json_dict(path: Path) -> dict | None:
    """*path* parsed as a JSON object; ``None`` when absent, unreadable or not an object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _int_field(data: dict | None, key: str) -> int:
    """Read an int field from a cache dict, tolerating missing/malformed values (→ 0)."""
    if not data:
        return 0
    try:
        return int(data.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _read_validated(account: str) -> dict | None:
    """Load + hash-validate *account*'s cache dict, or ``None`` if absent/refused.

    **Refuses on config-dir-hash mismatch**: if the stored ``config_dir_hash`` is
    present and differs from the account's expected hash the snapshot belonged to a
    DIFFERENT config dir (label reuse), so return ``None``. A hashless payload (a
    pre-existing ``usage.json``) is accepted for the default ``private`` account only.
    Shared by :func:`read_usage`, :func:`oauth_fetched_at`, and :func:`write_usage`'s
    merge so all three honour the same fail-closed rule.
    """
    data = _load_json_dict(_usage_path(account))
    if data is None:
        return None
    stored_hash = data.get("config_dir_hash")
    if stored_hash is not None:
        if stored_hash != _account_hash(account):
            return None  # this cache belongs to a different config dir → refuse
    elif account != "private":
        return None  # hashless payloads are legacy usage.json — default account only
    return data


def read_usage(account: str = "private") -> Usage | None:
    """Load *account*'s last persisted snapshot, or ``None`` if absent/unreadable.

    Falls back to the "start a turn to populate" placeholder on a config-dir-hash
    mismatch (see :func:`_read_validated`) instead of serving another account's numbers.
    """
    data = _read_validated(account)
    if data is None:
        return None
    return Usage(
        captured_at=_int_field(data, "captured_at"),
        five_hour=_window(data.get("five_hour")),
        seven_day=_window(data.get("seven_day")),
        fable_week=_window(data.get("fable_week")),
        oauth_fetched_at=_int_field(data, "oauth_fetched_at"),
    )


def oauth_fetched_at(account: str = "private") -> int:
    """Epoch seconds of *account*'s last authoritative OAuth fetch (0 = never/refused).

    Keyed separately from ``captured_at`` (which the status line bumps every few seconds):
    fetch staleness must track the OAuth fetch alone — see :func:`claude_usage_stale`.
    """
    return _int_field(_read_validated(account), "oauth_fetched_at")


def oauth_backoff_until(account: str = "private") -> int:
    """Epoch seconds until which *account*'s OAuth fetch is backing off (0 = none).

    Persisted by :func:`fetch_claude_usage` when a fetch fails with a large-``Retry-After``
    429, and cleared on the next successful fetch. While ``now`` is before this value
    :func:`claude_usage_stale` reports the cache as *fresh*, so neither the daemon nor the
    TUI re-attempts a rate-limited endpoint until the server-given time has passed.
    """
    return _int_field(_read_validated(account), "oauth_backoff_until")


# --- Claude OAuth usage endpoint (the /usage numbers, incl. the Fable weekly window) ---
#
# The status-line ``rate_limits`` payload only carries ``five_hour`` + ``seven_day``;
# Claude Code's ``/usage`` also shows a Fable-model-scoped weekly window. That window (and
# the authoritative main-window boundaries) come from the account's OAuth usage endpoint,
# fetched out-of-band per account — the same out-of-band pattern the Copilot card uses.
# The fetch is throttled (:func:`claude_usage_stale`) and run by the daemon and a detached
# ``ccc claude-usage`` spawn, never on the render path; :func:`read_usage` only reads cache.


def _iso_to_epoch(value: object) -> int:
    """Parse an ISO-8601 ``resets_at`` string to int epoch seconds (raises on garbage)."""
    return int(datetime.fromisoformat(str(value)).timestamp())


def _oauth_window(raw: object) -> Window | None:
    """One OAuth-endpoint window (``utilization`` float + ISO ``resets_at``)."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("utilization")
    resets = raw.get("resets_at")
    if pct is None or resets is None:
        return None
    try:
        return Window(used_percentage=float(pct), resets_at=_iso_to_epoch(resets))
    except (TypeError, ValueError):
        return None


def _oauth_fable_window(limits: object) -> Window | None:
    """The Fable weekly-scoped window from the OAuth ``limits[]`` list, else ``None``.

    Picks the entry with ``group == "weekly"`` whose ``scope.model.display_name`` is
    ``"Fable"`` and reads its ``percent`` + ISO ``resets_at``. Malformed → ``None``.
    """
    if not isinstance(limits, list):
        return None
    for item in limits:
        if not isinstance(item, dict) or item.get("group") != "weekly":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict) or model.get("display_name") != "Fable":
            continue
        pct = item.get("percent")
        resets = item.get("resets_at")
        if pct is None or resets is None:
            return None
        try:
            return Window(used_percentage=float(pct), resets_at=_iso_to_epoch(resets))
        except (TypeError, ValueError):
            return None
    return None


def _parse_oauth_usage(data: object, now: int) -> Usage | None:
    """Build a :class:`Usage` from an OAuth ``/usage`` response; ``None`` if unusable.

    Uses the top-level ``five_hour`` / ``seven_day`` for the two main windows and the
    Fable weekly-scoped ``limits[]`` entry for ``fable_week``. Returns ``None`` when
    neither main window parses (so a garbage body never overwrites a good cache). Pure —
    no network — so tests exercise it directly.
    """
    if not isinstance(data, dict):
        return None
    five = _oauth_window(data.get("five_hour"))
    seven = _oauth_window(data.get("seven_day"))
    if five is None and seven is None:
        return None
    return Usage(
        captured_at=now,
        five_hour=five,
        seven_day=seven,
        fable_week=_oauth_fable_window(data.get("limits")),
    )


def _keychain_oauth_token(account: str) -> str | None:  # pylint: disable=too-many-return-statements
    """The account's Claude OAuth access token from the macOS Keychain, or ``None``.

    Service name follows Claude Code's own rule: ``"Claude Code-credentials"`` for the
    DEFAULT account (its configured dir equals :func:`config.claude_home`), else
    ``"Claude Code-credentials-<hash8>"`` where ``<hash8>`` is :func:`_account_hash`. The
    secret payload is JSON; the token is at ``claudeAiOauth.accessToken``. If
    ``claudeAiOauth.expiresAt`` (epoch MILLISECONDS) is already in the past the token is
    skipped (returns ``None``): a live session will refresh it — we NEVER run an OAuth
    refresh ourselves. Best-effort: any failure returns ``None``. The token is never
    logged or printed.
    """
    if _account_config_dir(account) == config.claude_home():
        service = "Claude Code-credentials"
    else:
        service = f"Claude Code-credentials-{_account_hash(account)}"
    try:
        raw = subprocess.run(  # noqa: S603
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    try:
        if int(oauth.get("expiresAt", 0)) <= int(time.time() * 1000):
            return None  # expired — a live session refreshes it; we never do
    except (TypeError, ValueError):
        return None
    return token


def _get_oauth_usage_body(token: str) -> tuple[str | None, int]:
    """GET the OAuth usage endpoint body as ``(body, retry_after)`` (never raises).

    Returns ``(body, 0)`` on success. A 429 whose ``Retry-After`` is small (≤ 10 s) is
    slept off and retried ONCE — the endpoint rate-limits tightly enough that fetching two
    accounts back-to-back can trip it (observed ``retry-after: 2``). A 429 carrying a
    parseable ``Retry-After`` > 10 s returns ``(None, retry_after)`` so the caller can
    persist a backoff instead of hammering; every other failure returns ``(None, 0)`` and
    the caller's throttle owns the next attempt.
    """
    req = urllib.request.Request(  # noqa: S310  # fixed https:// endpoint
        _OAUTH_USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": _OAUTH_BETA_HEADER},
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310  # fixed https://
                return resp.read().decode("utf-8"), 0
        except urllib.error.HTTPError as err:
            if err.code == 429:
                try:
                    retry_after = int(err.headers.get("retry-after", ""))
                except (TypeError, ValueError):
                    return None, 0
                if attempt == 0 and 0 <= retry_after <= 10:
                    time.sleep(retry_after or 1)
                    continue
                return (None, retry_after) if retry_after > 10 else (None, 0)
            return None, 0
        except (urllib.error.URLError, OSError, ValueError):
            return None, 0
    return None, 0


def _persist_oauth_backoff(account: str, until: int) -> None:
    """Persist ``oauth_backoff_until`` into *account*'s cache, preserving every other field.

    Called when a fetch fails with a large-``Retry-After`` 429: it records the server-given
    wake time (see :func:`claude_usage_stale`) while leaving every other stored field
    verbatim (windows, ``fable_week``, ``captured_at``, ``oauth_fetched_at``,
    ``config_dir_hash``). When the cache file does not exist yet a minimal payload (the
    account's ``config_dir_hash`` + the backoff) is written. Runs under the same per-account
    ``flock`` :func:`write_usage` uses; never raises.
    """
    path = _usage_path(account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path.with_name(path.name + ".lock")):
            payload = dict(_read_validated(account) or {})
            payload["config_dir_hash"] = _account_hash(account)
            payload["oauth_backoff_until"] = until
            _atomic_write_json(path, payload)
    except OSError:
        pass


def fetch_claude_usage(account: str, now: int | None = None) -> Usage | None:
    """Fetch *account*'s Claude ``/usage`` from the OAuth endpoint and cache it; ``None``
    on any failure.

    Token from the Keychain (:func:`_keychain_oauth_token`) → HTTPS GET the OAuth usage
    endpoint → :func:`_parse_oauth_usage`. On success the snapshot is written as an
    **AUTHORITATIVE REPLACE** (not a :func:`_merge_window` merge) under the same
    per-account ``flock`` :func:`write_usage` uses, stamping ``config_dir_hash``,
    ``captured_at=now`` and ``oauth_fetched_at=now``.

    Why REPLACE, not merge: Anthropic can rebase a window boundary BACKWARD (observed at
    the Fable-5 rollout — the private weekly boundary moved Jul 15 → Jul 11), and
    :func:`_merge_window`'s later-reset-wins rule would pin the stale (further-future)
    boundary forever. This periodic authoritative replace self-heals it; the companion
    re-pin guard in :func:`write_usage` then stops idle status-line writers from
    re-pinning the bad boundary between fetches. Never raises: a missing token, an
    expired token, an HTTP/timeout error, or malformed JSON all return ``None`` with no
    write, so callers degrade to the last cache. The endpoint rate-limits tightly
    (observed HTTP 429 with ``retry-after: 2`` when two accounts fetch back-to-back), so
    a 429 carrying a small ``Retry-After`` is retried ONCE after sleeping it off.

    A 429 carrying a *large* ``Retry-After`` (observed 3357 s) persists
    ``oauth_backoff_until = now + min(Retry-After, _OAUTH_BACKOFF_CAP_SEC)`` into the cache
    (preserving every other field) before returning ``None``, so :func:`claude_usage_stale`
    suppresses re-attempts machine-wide until the server-given time. A successful fetch
    writes a fresh payload WITHOUT that key, clearing the backoff.
    """
    now = int(time.time()) if now is None else now
    token = _keychain_oauth_token(account)
    if not token:
        return None
    raw, retry_after_sec = _get_oauth_usage_body(token)
    if raw is None:
        # A 429 with a large Retry-After: persist a backoff so claude_usage_stale reports
        # the cache fresh until the server-given time (capped), instead of the daemon + TUI
        # re-attempting a rate-limited endpoint every few minutes all day.
        if retry_after_sec > 0:
            _persist_oauth_backoff(account, now + min(retry_after_sec, _OAUTH_BACKOFF_CAP_SEC))
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    snap = _parse_oauth_usage(data, now)
    if snap is None:
        return None
    snap.oauth_fetched_at = now
    path = _usage_path(account)
    # Authoritative replace: a fresh payload with NO ``oauth_backoff_until`` key, which
    # clears any backoff a prior 429 recorded (the fetch just succeeded).
    payload = {
        "captured_at": now,
        "config_dir_hash": _account_hash(account),
        "five_hour": _window_dict(snap.five_hour),
        "seven_day": _window_dict(snap.seven_day),
        "fable_week": _window_dict(snap.fable_week),
        "oauth_fetched_at": now,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path.with_name(path.name + ".lock")):
            _atomic_write_json(path, payload)
    except OSError:
        return None
    return snap


def claude_usage_stale(account: str, refresh_sec: float, now: int | None = None) -> bool:
    """True if *account* has never been OAuth-fetched or the fetch is older than *refresh_sec*.

    Keyed on ``oauth_fetched_at`` (NOT ``captured_at``): the status line bumps
    ``captured_at`` every few seconds, so keying on it would mask fetch staleness. The
    call sites choose *refresh_sec* via :func:`adaptive_interval` (idle vs active), exactly
    like the Copilot card.

    **429 backoff:** while ``now`` is before a persisted ``oauth_backoff_until`` (set by
    :func:`fetch_claude_usage` on a large-``Retry-After`` 429) this returns ``False`` —
    reporting the cache "fresh" — so the daemon and TUI, which both gate their fetch spawn
    on this one function, stop re-attempting a rate-limited endpoint until it passes.
    """
    now = int(time.time()) if now is None else now
    if now < oauth_backoff_until(account):
        return False
    fetched = oauth_fetched_at(account)
    if fetched <= 0:
        return True
    return (now - fetched) >= refresh_sec


# --- Subscription end dates (the " -> 30.9" a card's border title can carry) --------
#
# Neither vendor publishes a renewal date to the surfaces we can reach:
#
# * Anthropic's OAuth ``/profile`` carries ``subscription_created_at`` /
#   ``subscription_status`` / ``billing_type`` but NO ``current_period_end`` — so the
#   next charge is *derived* from the billing anniversary (:func:`next_anniversary`).
# * ChatGPT's ``backend-api/subscriptions`` answers 403 behind Cloudflare (only
#   ``wham/usage`` is reachable with the Codex token), leaving the id_token's
#   ``chatgpt_subscription_active_until`` claim — refreshed only by a ``codex login``,
#   so it goes stale within weeks and is offered as ``auto`` but never as the default.
#
# Hence ``subscription_ends`` takes a PINNED date per card as its normal mode, with
# ``auto`` as the opt-in derivation. Empty (the default) touches neither endpoint.
_OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# A billing anniversary moves once a month at most, so a day-old answer is as good as a
# fresh one — and this fetch rides along with the usage fetch, whose own throttle is
# minutes. Refetching it every 10 minutes would be pure waste on a rate-limited host.
_PROFILE_REFRESH_SEC = 86_400
# (path, mtime_ns) → the cached ``subscription_created_at``, so the 5 s render tick can
# ask for a card's date without re-reading (and re-parsing) the file every time. Same
# shape as _codex_email_cache.
_profile_cache: dict[tuple[str, int], str] = {}


def _profile_path(account: str) -> Path:
    """Per-account subscription-profile cache path.

    A file of its own rather than a field inside ``usage.json``: that cache has an
    authoritative-replace merge (see :func:`write_usage`) which a slow, unrelated,
    once-a-day fetch has no business participating in.
    """
    return config.app_home() / f"profile-{account}-{_account_hash(account)}.json"


def read_subscription_created_at(account: str = "private") -> str:
    """*account*'s cached ``subscription_created_at`` (ISO-8601), or ``""``. Never raises.

    Memoized on the cache file's ``(path, mtime_ns)`` — the TUI asks on every render
    tick, and the file changes at most once a day.
    """
    path = _profile_path(account)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return ""
    if key not in _profile_cache:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        created = data.get("subscription_created_at") if isinstance(data, dict) else None
        _profile_cache[key] = created if isinstance(created, str) else ""
    return _profile_cache[key]


def profile_fetched_at(account: str = "private") -> int:
    """When *account*'s profile cache was last written (epoch seconds); ``0`` if never."""
    try:
        data = json.loads(_profile_path(account).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return _int_field(data, "fetched_at") if isinstance(data, dict) else 0


def claude_profile_stale(
    account: str, refresh_sec: float = _PROFILE_REFRESH_SEC, now: int | None = None
) -> bool:
    """True if *account*'s subscription profile has never been fetched, or is a day old."""
    now = int(time.time()) if now is None else now
    fetched = profile_fetched_at(account)
    if fetched <= 0:
        return True
    return (now - fetched) >= refresh_sec


def fetch_claude_profile(account: str = "private", now: int | None = None) -> str:
    """Fetch and cache *account*'s ``subscription_created_at``; return it (``""`` on failure).

    Best-effort and silent, exactly like :func:`fetch_claude_usage`: no Keychain token,
    a non-200, or unparseable JSON simply leaves the cache untouched and returns ``""``,
    so a card configured ``auto`` shows no date rather than a wrong one. Only ever called
    out-of-band (``ccc claude-usage``), never on a render path.
    """
    now = int(time.time()) if now is None else now
    token = _keychain_oauth_token(account)
    if not token:
        return ""
    req = urllib.request.Request(  # noqa: S310  # fixed https:// endpoint
        _OAUTH_PROFILE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": _OAUTH_BETA_HEADER},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310  # fixed https://
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    org = data.get("organization") if isinstance(data, dict) else None
    created = org.get("subscription_created_at") if isinstance(org, dict) else None
    if not isinstance(created, str) or not created:
        return ""
    path = _profile_path(account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, {"subscription_created_at": created, "fetched_at": now})
    except OSError:
        return ""
    return created


def next_anniversary(created: str, today: date | None = None) -> date | None:
    """The next monthly billing anniversary of an ISO-8601 *created* timestamp.

    ``subscription_created_at`` is the only date Anthropic's profile exposes, so the
    renewal day is inferred from it: the same day-of-month, at or after *today*. A day
    that a short month does not have is clamped to that month's last day (Stripe's own
    rule for a 31st subscription in February). Returns ``None`` for anything unparseable.

    Deliberately the MONTHLY anniversary even though the plan could be annual: it is the
    earlier of the two answers, and this date exists to be cancelled before.
    """
    text = created.strip().replace("Z", "+00:00")
    try:
        start = datetime.fromisoformat(text).date()
    except ValueError:
        return None
    today = datetime.now().date() if today is None else today
    year, month = today.year, today.month
    for _ in range(2):  # this month, else the next one — never more
        day = min(start.day, calendar.monthrange(year, month)[1])
        candidate = datetime(year, month, day).date()
        if candidate >= today:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def codex_subscription_until(home: Path | None) -> date | None:
    """*home*'s ChatGPT subscription end from its id_token, or ``None``. Never raises.

    The ``chatgpt_subscription_active_until`` claim inside ``auth.json``'s id_token —
    the ONLY subscription date reachable for a Codex login, and only as fresh as the
    last ``codex login`` (its sibling ``chatgpt_subscription_last_checked`` claim shows
    how stale). That is why ``auto`` is offered for a Codex card but a pinned date is
    the documented default.
    """
    if home is None:
        return None
    data = _codex_auth_data(home) or {}
    tokens = data.get("tokens")
    token = tokens.get("id_token") if isinstance(tokens, dict) else data.get("id_token")
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth") if claims else None
    until = auth.get("chatgpt_subscription_active_until") if isinstance(auth, dict) else None
    if not isinstance(until, str) or not until:
        return None
    try:
        return datetime.fromisoformat(until.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def format_end_date(day: date, today: date) -> str:
    """A renewal date as a card-title suffix: ``30.9`` — or ``30.9!`` once it is past.

    Swiss ``D.M``, no zero padding and no year: four columns for the two facts that
    matter on a 32-cell title. The ``!`` is what stops a PINNED date from quietly
    rotting after its renewal — an ``auto`` date rolls forward on its own and never
    earns one.
    """
    text = f"{day.day}.{day.month}"
    return f"{text}!" if day < today else text


def subscription_suffix(
    card: str,
    ends: dict[str, str],
    home: Path | None = None,
    today: date | None = None,
) -> str:
    """The `` -> 30.9`` a card's border title carries, or ``""`` when it carries none.

    *ends* is :func:`config.parse_subscription_ends`'s map. A card missing from it (the
    default for all four) gets ``""`` and costs nothing. ``auto`` derives the date —
    :func:`next_anniversary` off the cached Claude profile for the two Claude cards,
    :func:`codex_subscription_until` off the id_token for the two Codex ones — and also
    yields ``""`` when that source has nothing to say, so an unreachable endpoint shows
    no date rather than a wrong one. *home* is the card's ``CODEX_HOME`` and is only
    read for a Codex card.
    """
    spec = ends.get(card, "")
    if not spec:
        return ""
    today = datetime.now().date() if today is None else today
    if spec != "auto":
        try:
            day: date | None = datetime.fromisoformat(spec).date()
        except ValueError:
            return ""
    elif card in config.SUBSCRIPTION_CARD_ACCOUNTS:
        day = next_anniversary(
            read_subscription_created_at(config.SUBSCRIPTION_CARD_ACCOUNTS[card]), today
        )
    else:
        day = codex_subscription_until(home)
    return f" -> {format_end_date(day, today)}" if day is not None else ""


def _force_exhausted_window(
    five: Window | None, seven: Window | None, now: int
) -> tuple[Window | None, Window | None]:
    """Pin the window that a live refusal proves is full to 100%.

    A refusal means an included window reached its limit — but the call that FILLS a
    window returns the windowless refusal instead of a fresh reading, so the last
    numbers on record are pre-limit (81% on 2026-08-28, never 100%). Left alone they
    read as comfortable headroom to every consumer: the card, ``ccc quota -p codex``,
    the offload gate, another agent's router. Reporting 100% is what stops something
    else from spending a turn discovering the block for itself.

    The full window is taken to be the **most-consumed live one**: it is the one that
    filled, and its ``resets_at`` is when access actually returns. Windows whose reset
    has already passed are not live and are never chosen.
    """
    live = [
        (name, window)
        for name, window in (("five_hour", five), ("seven_day", seven))
        if window is not None and window.resets_at > now
    ]
    if not live:
        return five, seven
    name, window = max(live, key=lambda item: item[1].used_percentage)
    full = Window(used_percentage=100.0, resets_at=window.resets_at)
    return (full if name == "five_hour" else five), (full if name == "seven_day" else seven)


# --- Live OpenAI Codex usage (the ChatGPT "Settings → Usage" numbers) ----------
#
# Codex DOES have a usage endpoint after all: the web app's own
# ``chatgpt.com/backend-api/wham/usage``, authorized by the ChatGPT OAuth token that
# ``codex login`` parks in ``$CODEX_HOME/auth.json``. That matters because the rollout
# files are only as fresh as the last Codex turn: on 2026-08-30 the newest windowed
# rollout event was ~14 h old and its 5h reset had already passed, so the only "live"
# window left to pin a refusal on was the WEEKLY one — the card read "Week: 100%,
# access returns in 6d 8h" while the web page said the 5h window was full (19 m to go)
# and the week still had 77% headroom. Live data attributes the block correctly.
#
# The fetch is opt-in (``codex_usage``), throttled (:func:`codex_usage_stale`) and run
# out-of-band — the daemon and a detached ``ccc codex-usage`` — never on the render path;
# :func:`read_codex_live` only reads the cached JSON. More than one CODEX_HOME can be
# configured (``codex_home_private``), so every path here is per-home and every cache
# file is named after a hash of its home.


def _codex_auth_data(home: Path) -> dict | None:
    """Parsed ``<home>/auth.json``, or ``None`` when absent/unreadable/not JSON."""
    return _load_json_dict(home.expanduser() / "auth.json")


def _codex_auth_tokens(home: Path) -> tuple[str, str] | None:
    """``(access_token, account_id)`` for *home*'s ChatGPT login, else ``None``.

    Requires ``auth_mode`` to be absent or ``"chatgpt"`` (an API-key login has no
    subscription windows to report) and both token fields to be non-empty strings. The
    token is never logged or printed.
    """
    data = _codex_auth_data(home)
    if data is None:
        return None
    mode = data.get("auth_mode")
    if mode is not None and mode != "chatgpt":
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    account = tokens.get("account_id")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(account, str) or not account:
        return None
    return access, account


def _jwt_claims(token: object) -> dict | None:
    """A JWT's decoded payload segment — NO signature verification.

    We are reading our own already-trusted local credential purely to label a card, so
    verifying it would buy nothing (and would need OpenAI's JWKS). Any malformed input
    yields ``None``.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _jwt_email(token: object) -> str | None:
    """The ``email`` claim of a JWT's payload segment, or ``None``."""
    claims = _jwt_claims(token)
    email = claims.get("email") if claims else None
    return email if isinstance(email, str) and email else None


def codex_account_email(home: Path) -> str | None:
    """Which ChatGPT account *home* is logged in as, or ``None``. Never raises.

    Read from ``<home>/auth.json``'s ``tokens.id_token`` JWT payload; when that carries
    no ``email`` claim, the last live snapshot's ``email`` (the endpoint returns it) is
    used instead. The JWT decode is memoized on the file's ``(path, mtime_ns)`` so the
    TUI can ask for it on every render tick.
    """
    path = home.expanduser() / "auth.json"
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _codex_email_cache:
        data = _codex_auth_data(home) or {}
        tokens = data.get("tokens")
        _codex_email_cache[key] = _jwt_email(
            tokens.get("id_token") if isinstance(tokens, dict) else None
        ) or _jwt_email(data.get("id_token"))
    if _codex_email_cache[key]:
        return _codex_email_cache[key]
    # The fallback stays OUTSIDE the memo: a later fetch can supply the e-mail a
    # claim-less JWT never had, and auth.json's mtime would not change to invalidate it.
    cached = (_read_codex_live_data(home) or {}).get("email")
    return cached if isinstance(cached, str) and cached else None


def abbrev_email(email: str, *, squeeze_local: bool = False) -> str:
    """Shorten an address for a card title: ``first.last@example.org`` → ``first…@example.org``.

    A dotted local part keeps its first segment whole and drops the rest behind an
    ellipsis — the readable half of an address is its first word, and initials of the
    later segments (the old ``fi…la``) bought two columns at the cost of legibility.
    An undotted local part longer than five characters keeps its first two and last
    two; anything shorter is left alone. A string with no ``@`` is returned unchanged.

    **The domain is never touched.** It is half of what makes an address recognizable,
    and squeezing it produced titles (``albert…@gm…om``) that no longer read as an
    address at all. When a title has to give up cells it gives up the LOCAL part
    instead: *squeeze_local* takes two characters per dotted segment
    (``albert.glensk`` → ``al.gl``, ``openai.account`` → ``op.ac``), which is both
    shorter and more legible than a mangled domain. It is ignored when it would not
    actually be shorter — a three-segment local part squeezes to more cells than
    ``first…``, and there the card grows instead (see ``_set_card_expanded``).
    """
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    if "." in local:
        short = f"{local.split('.')[0]}…"
        if squeeze_local:
            initials = ".".join(segment[:2] for segment in local.split("."))
            short = min(short, initials, key=len)
    elif len(local) > 5:
        short = f"{local[:2]}…{local[-2:]}"
    else:
        short = local
    return f"{short}@{domain}"


# How many cells a card's border title may claim before the card grows past its CSS
# min-width. ``_set_card_expanded`` pins a collapsed card to ``len(title) + 6`` (the
# ``╭─ … ─╮`` furniture), and the #usage* min-width is 38 — so 32 cells of title keep
# every card exactly as wide as the narrowest one. Beyond that the whole right-hand
# column widens and the job-details pane pays for it, which is why a title that would
# overflow squeezes its domain instead. Keep in sync with _CARD_INNER_WIDTH / the CSS.
_CARD_TITLE_BUDGET = 32


def codex_card_title(home: Path | None, chord: str, suffix: str = "") -> str:
    """Border title for a Codex card: ``Codex first…@example.org / t3``.

    Naming the account is what keeps two Codex cards apart, so the vendor prefix gives
    way to the address: ``OpenAI Codex <account>`` overflowed the card's 34-column title
    on a long domain and Textual truncated the domain away — the one part that tells the
    accounts apart. When no e-mail can be resolved (no ``auth.json``, an API-key login,
    or ``home`` is ``None`` because the second home is not configured) the title degrades
    to plain ``Codex / <chord>``.

    *suffix* is the optional subscription-end marker (`` -> 30.9``, see
    :func:`subscription_suffix`). It costs eight cells, which is enough to push most
    addresses over :data:`_CARD_TITLE_BUDGET` — so an overflowing title squeezes its
    LOCAL part (``al.gl@gmail.com``) and keeps the domain whole; if that still does not
    fit, ``_set_card_expanded`` widens the card by the cells needed. The chord and the
    date are never truncated: they are the two things the title exists to say.
    """
    email = codex_account_email(home) if home is not None else None
    if not email:
        return f"Codex / {chord}{suffix}"
    title = f"Codex {abbrev_email(email)} / {chord}{suffix}"
    if cell_len(title) <= _CARD_TITLE_BUDGET:
        return title
    return f"Codex {abbrev_email(email, squeeze_local=True)} / {chord}{suffix}"


def _codex_usage_path(home: Path) -> Path:
    """Per-``CODEX_HOME`` live-usage cache path (the home's path hashed into the name)."""
    digest = hashlib.sha1(  # noqa: S324  # cache-file naming, not security
        str(home.expanduser().resolve()).encode(), usedforsecurity=False
    ).hexdigest()[:8]
    return config.app_home() / f"codex_usage-{digest}.json"


def _wham_window(raw: object) -> tuple[int, Window] | None:
    """One endpoint window as ``(limit_window_seconds, Window)``; ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    seconds = raw.get("limit_window_seconds")
    pct = raw.get("used_percent")
    resets = raw.get("reset_at")
    if seconds is None or pct is None or resets is None:
        return None
    try:
        return int(seconds), Window(used_percentage=float(pct), resets_at=int(resets))
    except (TypeError, ValueError):
        return None


def _wham_blocked_reason(data: dict) -> str:
    """Why Codex is refusing calls right now, or ``""`` when it is not.

    ``rate_limit_reached_type.type`` names the reason when there is one (mapped through
    :func:`codex_in_claude.refusal_label`, so the wording matches the rollout-sourced
    refusal exactly). Failing that, an ``allowed: false`` / ``limit_reached: true`` rate
    limit is a plain window exhaustion.
    """
    reached = data.get("rate_limit_reached_type")
    kind = reached.get("type") if isinstance(reached, dict) else None
    if isinstance(kind, str) and kind.strip():
        return refusal_label(kind.strip())
    rate = data.get("rate_limit")
    if isinstance(rate, dict) and (
        rate.get("allowed") is False or rate.get("limit_reached") is True
    ):
        return "usage limit reached"
    return ""


def _parse_wham_usage(data: object, now: int) -> Usage | None:
    """Build a :class:`Usage` from a ``wham/usage`` response; ``None`` if unusable.

    Windows are picked by ``limit_window_seconds`` (18000 → the 5h session, 604800 → the
    week), NEVER by primary/secondary position. Returns ``None`` when neither window
    parses, so a garbage body can never overwrite a good cache. Pure — no network — so
    tests exercise it directly.
    """
    if not isinstance(data, dict):
        return None
    rate = data.get("rate_limit")
    windows: dict[int, Window] = {}
    if isinstance(rate, dict):
        for field_name in ("primary_window", "secondary_window"):
            parsed = _wham_window(rate.get(field_name))
            if parsed is not None:
                windows[parsed[0]] = parsed[1]
    five = windows.get(_WHAM_FIVE_HOUR_SEC)
    seven = windows.get(_WHAM_SEVEN_DAY_SEC)
    if five is None and seven is None:
        return None
    reason = _wham_blocked_reason(data)
    email = data.get("email")
    plan = data.get("plan_type")
    return Usage(
        captured_at=now,
        five_hour=five,
        seven_day=seven,
        blocked_reason=reason,
        blocked_at=now if reason else 0,
        live=True,
        email=email if isinstance(email, str) else "",
        plan_type=plan if isinstance(plan, str) else "",
    )


def _get_wham_usage_body(token: str, account_id: str) -> tuple[str | None, int]:
    """GET the live usage endpoint as ``(body, http_status)``; never raises.

    ``status`` is the HTTP code on an HTTP error (401/403 = the token needs refreshing,
    which is what triggers the ``codex app-server`` fallback), ``200`` on success and
    ``0`` for a transport-level failure.
    """
    req = urllib.request.Request(  # noqa: S310  # fixed https:// endpoint
        _WHAM_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-Id": account_id,
            "Accept": "application/json",
            "User-Agent": "ccc",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310  # fixed https://
            return resp.read().decode("utf-8"), 200
    except urllib.error.HTTPError as err:
        return None, int(err.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None, 0


def _appserver_usage(limits: object, now: int) -> Usage | None:
    """Map an ``account/rateLimits/read`` result to a :class:`Usage`; ``None`` if unusable.

    Same duration-keyed rule as everywhere else, in the app-server's own spelling
    (``windowDurationMins`` 300 / 10080, ``usedPercent``, ``resetsAt``).
    """
    if not isinstance(limits, dict):
        return None
    windows: dict[int, Window] = {}
    for field_name in ("primary", "secondary"):
        raw = limits.get(field_name)
        if not isinstance(raw, dict):
            continue
        minutes = raw.get("windowDurationMins")
        pct = raw.get("usedPercent")
        resets = raw.get("resetsAt")
        if minutes is None or pct is None or resets is None:
            continue
        try:
            windows[int(minutes)] = Window(used_percentage=float(pct), resets_at=int(resets))
        except (TypeError, ValueError):
            continue
    five = windows.get(_FIVE_HOUR_MINUTES)
    seven = windows.get(_SEVEN_DAY_MINUTES)
    if five is None and seven is None:
        return None
    reached = limits.get("rateLimitReachedType")
    reason = refusal_label(reached.strip()) if isinstance(reached, str) and reached.strip() else ""
    plan = limits.get("planType")
    return Usage(
        captured_at=now,
        five_hour=five,
        seven_day=seven,
        blocked_reason=reason,
        blocked_at=now if reason else 0,
        live=True,
        plan_type=plan if isinstance(plan, str) else "",
    )


def _fetch_codex_usage_appserver(home: Path, now: int | None = None) -> Usage | None:
    """Ask the official ``codex app-server`` for the rate limits; ``None`` on any failure.

    The fallback for a 401/403 from the HTTP endpoint: the access token in ``auth.json``
    has expired and only ``codex`` itself may refresh it (it writes the new one back).
    Three JSON-RPC frames go in on stdin (``initialize`` → ``initialized`` →
    ``account/rateLimits/read``); the answer arrives in ~2.5 s among unrelated
    notifications, so every stdout line without OUR request id is skipped. Hard timeout
    (:data:`_APPSERVER_TIMEOUT_SEC`), after which the child is killed and whatever it had
    already printed is still parsed.
    """
    exe = shutil.which("codex")
    if not exe:
        return None
    now = int(time.time()) if now is None else now
    frames = [
        {
            "jsonrpc": "2.0",
            "id": _APPSERVER_INIT_ID,
            "method": "initialize",
            "params": {"clientInfo": {"name": "ccc", "title": "ccc", "version": "0"}},
        },
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": _APPSERVER_LIMITS_ID, "method": "account/rateLimits/read"},
    ]
    stdin = "".join(json.dumps(frame) + "\n" for frame in frames)
    env = dict(os.environ, CODEX_HOME=str(home.expanduser()))
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "app-server"],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_APPSERVER_TIMEOUT_SEC,
            env=env,
            check=False,
        )
        out = proc.stdout
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout if isinstance(expired.stdout, str) else ""
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    for line in (out or "").splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("id") != _APPSERVER_LIMITS_ID:
            continue
        result = obj.get("result")
        return _appserver_usage(result.get("rateLimits") if isinstance(result, dict) else None, now)
    return None


def _write_codex_usage(home: Path, snap: Usage, now: int) -> None:
    """Persist *snap* as *home*'s live cache (atomic, under the same ``flock`` as the rest).

    ``captured_at`` dates the FIGURES, ``fetched_at`` the call — they coincide today but
    :func:`read_codex_usage` compares ``captured_at`` against the newest rollout event,
    so the two stay separate fields. Never raises.
    """
    payload = {
        "captured_at": snap.captured_at,
        "fetched_at": now,
        "home": str(home.expanduser().resolve()),
        "email": snap.email,
        "plan_type": snap.plan_type,
        "five_hour": _window_dict(snap.five_hour),
        "seven_day": _window_dict(snap.seven_day),
        "blocked_reason": snap.blocked_reason,
        "blocked_at": snap.blocked_at,
    }
    path = _codex_usage_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path.with_name(path.name + ".lock")):
            _atomic_write_json(path, payload)
    except OSError:
        pass


def fetch_codex_usage(home: Path | None = None, now: int | None = None) -> Usage | None:
    """Fetch *home*'s live Codex usage and cache it; ``None`` on any failure.

    Token from ``<home>/auth.json`` → HTTPS GET :data:`_WHAM_USAGE_URL` →
    :func:`_parse_wham_usage`. A 401/403 means the stored access token has expired, and
    only ``codex`` may refresh it, so that case falls back ONCE to
    :func:`_fetch_codex_usage_appserver` (which refreshes and writes the token back).
    Best-effort throughout: a missing/API-key ``auth.json``, an HTTP or timeout error, or
    a malformed body all return ``None`` with NO write, so callers keep the last cache.
    """
    home = config.codex_home() if home is None else home
    now = int(time.time()) if now is None else now
    tokens = _codex_auth_tokens(home)
    if tokens is None:
        return None
    body, status = _get_wham_usage_body(*tokens)
    snap: Usage | None = None
    if body is not None:
        try:
            snap = _parse_wham_usage(json.loads(body), now)
        except (json.JSONDecodeError, ValueError):
            return None
    elif status in (401, 403):
        snap = _fetch_codex_usage_appserver(home, now)
    if snap is None:
        return None
    _write_codex_usage(home, snap, now)
    return snap


def _read_codex_live_data(home: Path) -> dict | None:
    """*home*'s cached live snapshot as a dict, or ``None`` (absent / corrupt / foreign).

    The cache file is named after a hash of the home, and the payload ALSO records the
    home it was written for: a hash collision or a hand-copied file is refused rather
    than served under the wrong account.
    """
    data = _load_json_dict(_codex_usage_path(home))
    if data is None:
        return None
    stored_home = data.get("home")
    if isinstance(stored_home, str) and stored_home != str(home.expanduser().resolve()):
        return None
    return data


def read_codex_live(home: Path) -> Usage | None:
    """Load *home*'s last live snapshot, or ``None`` when absent/unreadable/windowless."""
    data = _read_codex_live_data(home)
    if data is None:
        return None
    try:
        five = _window(data.get("five_hour"))
        seven = _window(data.get("seven_day"))
        if five is None and seven is None:
            return None
        return Usage(
            captured_at=_int_field(data, "captured_at"),
            five_hour=five,
            seven_day=seven,
            blocked_reason=str(data.get("blocked_reason") or ""),
            blocked_at=_int_field(data, "blocked_at"),
            live=True,
            email=str(data.get("email") or ""),
            plan_type=str(data.get("plan_type") or ""),
        )
    except (TypeError, ValueError):
        return None


def codex_usage_stale(home: Path, refresh_sec: float, now: int | None = None) -> bool:
    """True if *home*'s live cache is missing or older than *refresh_sec* (drives refresh).

    Mtime-based, like :func:`copilot_usage_stale`; the call sites pick *refresh_sec* with
    :func:`adaptive_interval` (``codex_usage_refresh_sec`` idle, the shorter
    ``codex_usage_refresh_active_sec`` while a job works).
    """
    now = int(time.time()) if now is None else now
    try:
        mtime = _codex_usage_path(home).stat().st_mtime
    except OSError:
        return True
    return (now - int(mtime)) >= refresh_sec


def _codex_rollout_files(home: Path) -> list[Path]:
    """*home*'s session rollout files, newest mtime first ( ``[]`` when there are none)."""
    try:
        return sorted(
            (home / "sessions").glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []


def _path_key(path: Path | None) -> tuple[str, int]:
    """``(path, mtime_ns)`` cache key for *path*; ``("", 0)`` when it does not exist."""
    if path is None:
        return ("", 0)
    try:
        return (str(path), int(path.stat().st_mtime_ns))
    except OSError:
        return ("", 0)


def _codex_rollout_snapshot(files: list[Path], now: int) -> Usage | None:
    """The freshest windowed ``rate_limits`` event across *files*, as a :class:`Usage`.

    Picks the newest EVENT, not the first file in mtime order, and dates it by the
    event's own timestamp. File mtime is not a proxy for event age: Codex re-touches
    old rollout files (resumed threads, writer locks), and on 2026-08-29 a 3-day-old
    file sat at position 0 by mtime, so breaking on the first hit returned a 08-25
    reading of 19% while the real newest event said 81%. ``_codex_rate_snapshot`` in
    codex_in_claude.py already picks by ``max(captured_at)``; this mirrors it.
    """
    freshest = max(
        (
            rate_snapshot
            for path in files[:_CODEX_SCAN_LIMIT]
            if (rate_snapshot := _latest_rate_limits_event(path)) is not None
        ),
        key=lambda item: item.captured_at,
        default=None,
    )
    if freshest is None:
        return None
    windows = {
        minutes: Window(used_percentage=parsed.used_percent, resets_at=parsed.resets_at)
        for minutes, parsed in freshest.windows.items()
    }
    return Usage(
        captured_at=freshest.captured_at or now,
        five_hour=windows.get(_FIVE_HOUR_MINUTES),
        seven_day=windows.get(_SEVEN_DAY_MINUTES),
    )


def _staple_refusal(snapshot: Usage | None, refusal: object, now: int) -> Usage:
    """Re-issue *snapshot* with the live refusal stapled on and the full window at 100%.

    The windows keep reporting the last successful call's figures while Codex rejects
    everything, so without this the card shows comfortable headroom — see
    :func:`_force_exhausted_window`. Every other attribute (``live``/``email``/
    ``plan_type``) is carried over, so a stapled LIVE snapshot still renders as live.
    """
    reached_type = getattr(refusal, "reached_type", "")
    captured = getattr(refusal, "captured_at", now)
    five, seven = _force_exhausted_window(
        snapshot.five_hour if snapshot is not None else None,
        snapshot.seven_day if snapshot is not None else None,
        now,
    )
    return Usage(
        captured_at=snapshot.captured_at if snapshot is not None else captured,
        five_hour=five,
        seven_day=seven,
        blocked_reason=refusal_label(reached_type),
        blocked_at=captured,
        live=snapshot.live if snapshot is not None else False,
        email=snapshot.email if snapshot is not None else "",
        plan_type=snapshot.plan_type if snapshot is not None else "",
    )


def read_codex_usage(now: int | None = None, home: Path | None = None) -> Usage | None:
    """Current Codex rate-limit snapshot for *home* — the LIVE figures when they are newer.

    Two sources, and the newer one wins:

    * the live ``wham/usage`` cache (:func:`read_codex_live`), refreshed out-of-band when
      ``codex_usage`` is on, and
    * the newest windowed ``rate_limits`` block Codex wrote onto a ``token_count`` event
      in ``<home>/sessions/**/rollout-*.jsonl`` — as old as the last Codex turn.

    A live refusal (:func:`codex_in_claude.codex_refusal`) is read separately from the
    windowless blocks the window scan skips and stapled on, EXCEPT when the chosen live
    snapshot is already newer than it (the endpoint reports the block itself, and its
    windows are the ones that actually filled). The refusal is scanned from THIS home's
    own rollouts — reading the pin-effective home here used to staple a private seat's
    refusal onto the team seat's row whenever a pin was active.

    Parsing is cached per home on the newest rollout file's and the live cache's
    ``(path, mtime)`` so the 5 s TUI refresh stays cheap when idle.
    """
    now = int(time.time()) if now is None else now
    home = config.codex_home() if home is None else home
    files = _codex_rollout_files(home)
    key = _path_key(files[0] if files else None) + _path_key(_codex_usage_path(home))
    cached = _codex_cache.get(str(home))
    if cached is not None and cached[0] == key:
        return cached[1]
    rollout = _codex_rollout_snapshot(files, now) if files else None
    # A refusal lives in the windowless blocks the window scan skips, so it is read
    # separately: the windows keep reporting the last healthy figures while Codex rejects
    # every call, and only this field says so.
    refusal = codex_refusal(home)
    live = read_codex_live(home)
    snapshot: Usage | None
    if live is not None and (rollout is None or live.captured_at >= rollout.captured_at):
        snapshot = live
        # The live payload carries the refusal itself; only a refusal NEWER than the
        # fetch can add anything the endpoint had not seen yet.
        if refusal is not None and not snapshot.blocked and refusal.captured_at > live.captured_at:
            snapshot = _staple_refusal(snapshot, refusal, now)
    else:
        snapshot = rollout
        if refusal is not None:
            snapshot = _staple_refusal(snapshot, refusal, now)
    _codex_cache[str(home)] = (key, snapshot)
    return snapshot


def format_reset(resets_at: int, now: int | None = None) -> str:
    """Relative reset time, minute precision: ``in 1h 4m`` / ``in 4d 13h 4m``."""
    now = int(time.time()) if now is None else now
    delta = resets_at - now
    if delta <= 0:
        return "now"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"in {days}d {hours}h {minutes}m"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def _format_age(seconds: int) -> str:
    """Compact elapsed duration (minute precision): ``6h 25m`` / ``2d 3h`` / ``45m``.

    Mirrors :func:`format_reset`'s day/hour/minute arithmetic but for an already-elapsed
    span and with no ``in`` prefix — used for the ``Fable: stale <age>`` marker.
    """
    seconds = max(0, seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _fill_for_pct(pct: float) -> str:
    """Pick a Claude bar fill from its own usage: green ≤65%, orange ≤85%, else red.

    Thresholds are inclusive at their upper bound: ``pct <= 65`` → green, ``pct <= 85``
    → orange, otherwise red. *pct* may be a float and need not be pre-clamped.
    """
    if pct <= 65:
        return _FILL_GREEN
    if pct <= 85:
        return _FILL_ORANGE
    return _FILL_RED


def _bar(
    pct: float,
    fill_color: str = _FILL_COLOR,
    *,
    label: str = "",
    label_color: str = _RESET_STYLE,
    width: int = _BAR_WIDTH,
) -> Text:
    """A *width*-cell usage bar with *label* embossed over it.

    The fill/track colours stay as each cell's background (usage stays visible);
    only the glyphs covered by *label* change — dark over the bright fill, the
    card's *label_color* over the dark track — so the reset text rides inside the
    bar instead of lengthening the row. *label* is left-aligned; its spaces fall
    back to a solid block so the bar reads as continuous.
    """
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    label = label[:width]
    bar = Text()
    for i in range(width):
        on_fill = i < filled
        cell_bg = fill_color if on_fill else _TRACK_COLOR
        glyph = label[i] if i < len(label) else " "
        # Every cell is a background-filled cell (so the fill/track colour shows as
        # usage); a label glyph rides on top in a contrasting colour. A space stays a
        # real space (kept in .plain) — visually identical to the rest of the bar.
        if glyph == " ":
            bar.append(" ", style=f"on {cell_bg}")
        else:
            fg = _OVERLAY_ON_FILL if on_fill else label_color
            bar.append(glyph, style=f"bold {fg} on {cell_bg}")
    return bar


def _bar_row(
    pct: float,
    fill_color: str = _FILL_COLOR,
    *,
    label: str = "",
    label_color: str = _RESET_STYLE,
) -> Text:
    """One full-width card row: the bar, then its percentage right after it.

    The row always spans ``_CARD_INNER_WIDTH``, so the percentage sits flush at the
    box's inner edge; the bar absorbs whatever the percentage does not need. The bar
    is therefore one cell narrower for a three-digit ``100%`` than for ``27%`` —
    that is what keeps the gap between bar and number at zero on every row.
    """
    pct_str = f"{int(round(pct))}%"
    row = _bar(
        pct,
        fill_color,
        label=label,
        label_color=label_color,
        width=max(1, _CARD_INNER_WIDTH - len(pct_str)),
    )
    row.append(pct_str + "\n", style=_PCT_STYLE)
    return row


def _section(  # pylint: disable=too-many-arguments
    prefix: str,
    win: Window | None,
    now: int,
    fill_color: str = _FILL_COLOR,
    label_color: str = _RESET_STYLE,
    *,
    label: str | None = None,
    stale: bool = False,
) -> Text:
    """One window as a single bar row: ``<prefix>Resets …`` embossed, percentage right.

    *prefix* names the window inside the bar (``"Session: "`` / ``"Week: "``) — the
    standalone title line above the bar was dropped so each window is just one row. Pass
    *label* to override the default ``<prefix>Resets …`` emboss (used for the stale-Fable
    marker), keeping the same bar/percentage rendering. *stale* (see
    :data:`_SESSION_STALE_AFTER_SEC` / :data:`_WEEK_STALE_AFTER_SEC`) drops the bar
    entirely in favour of a bare ``<prefix>?%`` — a coloured fill would otherwise imply
    a live reading the snapshot is too old to support.
    """
    text = Text()
    if win is None:
        text.append(f"{prefix}—\n", style="grey50")
        return text
    if stale:
        text.append(f"{prefix}?%\n", style="grey50")
        return text
    # Reset time is embossed onto the bar (not appended after it) so the row stays
    # short — one line per window, and the card no longer grows wider than the bar.
    embossed = label if label is not None else f"{prefix}Resets {format_reset(win.resets_at, now)}"
    text.append_text(
        _bar_row(win.used_percentage, fill_color, label=embossed, label_color=label_color)
    )
    return text


def _render_card(  # pylint: disable=too-many-arguments
    usage: Usage,
    now: int,
    *,
    fill_color: str,
    label_color: str,
    fill_for_pct: Callable[[float], str] | None = None,
    staleness: tuple[int, int] | None = None,
) -> Text:
    """The two-bar card body (session + week), shared by both providers.

    A third ``Fable:`` row is appended ONLY when :attr:`Usage.fable_week` is set — the
    Codex card (which shares this renderer) and Claude cards before their first OAuth
    fetch both stay two rows. ``Fable: `` is shorter than ``Session: ``, so it fits the
    bar's embossed-label width. When the last successful OAuth fetch is older than
    :data:`_FABLE_STALE_AFTER_SEC` the Fable row is embossed ``Fable: stale <age>`` instead
    of ``Fable: Resets …`` so a frozen figure (e.g. under a 429 backoff) is visibly marked.

    When *fill_for_pct* is given, each bar's fill is chosen from *its own* usage
    percentage (:func:`_fill_for_pct` for the Claude cards' green/orange/red thresholds);
    when None the single flat *fill_color* is used for every bar (Codex/Copilot behaviour,
    exactly as before).

    *staleness*, when given, is ``(session_stale_after_sec, week_stale_after_sec)``:
    each row drops its bar for a bare ``?%`` once ``now - usage.captured_at`` exceeds
    its own threshold (see :func:`_section`). ``None`` (Codex/Copilot) keeps today's
    behaviour — a bar is always drawn from whatever figure the cache holds.
    """

    def _fill(win: Window | None) -> str:
        if fill_for_pct is not None and win is not None:
            return fill_for_pct(win.used_percentage)
        return fill_color

    age = now - usage.captured_at
    session_stale = staleness is not None and age > staleness[0]
    week_stale = staleness is not None and age > staleness[1]

    text = Text()
    text.append_text(
        _section(
            "Session: ",
            usage.five_hour,
            now,
            _fill(usage.five_hour),
            label_color,
            stale=session_stale,
        )
    )
    # No blank line between the windows — keeps the card tight.
    text.append_text(
        _section(
            "Week: ", usage.seven_day, now, _fill(usage.seven_day), label_color, stale=week_stale
        )
    )
    if usage.fable_week is not None:
        fable_label: str | None = None
        if usage.oauth_fetched_at > 0 and now - usage.oauth_fetched_at > _FABLE_STALE_AFTER_SEC:
            fable_label = f"Fable: stale {_format_age(now - usage.oauth_fetched_at)}"
        text.append_text(
            _section(
                "Fable: ",
                usage.fable_week,
                now,
                _fill(usage.fable_week),
                label_color,
                label=fable_label,
            )
        )
    text.rstrip()
    return text


def render_usage(
    usage: Usage | None, now: int | None = None, *, accent: str = _CLAUDE_ACCENT
) -> Text:
    """Render the two-bar Claude ``/usage`` card as Rich ``Text`` for a ``Static``.

    *accent* colours the embossed reset labels so the two Claude cards read apart —
    private gold (:data:`_CLAUDE_ACCENT`), work blue (:data:`_CLAUDE_WORK_ACCENT`).
    Both colour each bar from its own usage via :func:`_fill_for_pct`
    (green ≤65% / orange ≤85% / red otherwise): same product, per-bar health colour.

    Session/Week each go stale (bare ``?%``, no bar) once the snapshot predates its own
    window's lifetime — see :data:`_SESSION_STALE_AFTER_SEC` / :data:`_WEEK_STALE_AFTER_SEC`.
    """
    now = int(time.time()) if now is None else now
    if usage is None or usage.is_empty():
        return Text("—\n(start a turn to populate)", style="grey50")
    return _render_card(
        usage,
        now,
        fill_color=_FILL_COLOR,
        label_color=accent,
        fill_for_pct=_fill_for_pct,
        staleness=(_SESSION_STALE_AFTER_SEC, _WEEK_STALE_AFTER_SEC),
    )


def render_work_usage(usage: Usage | None, now: int | None = None) -> Text:
    """Render the *work* Claude ``/usage`` card (blue accent) — same product, blue reset."""
    return render_usage(usage, now, accent=_CLAUDE_WORK_ACCENT)


def render_codex_usage(usage: Usage | None, now: int | None = None) -> Text:
    """Render the two-bar OpenAI Codex usage card (green bars) as Rich ``Text``.

    A refusal normally prefixes the bars with a red banner — but a **live** snapshot
    whose exhausted window is already pinned at 100% needs none: that row reads
    ``Session: Resets in 19m … 100%``, which is both the refusal and when it lifts, so
    the banner only repeated it (``⛔ usage limit reached`` / ``access returns in 19m`` /
    ``live figures, 0m old``) at the cost of three of the card's lines. The banner stays
    wherever the bars do NOT carry the block: rollout-sourced figures (those bars are the
    last SUCCESSFUL call's and read as headroom, hence the ``100% = the limit that fired``
    caveat) and any snapshot with no live exhausted window to pin (see
    :func:`codex_exhausted_window`).
    """
    now = int(time.time()) if now is None else now
    if usage is not None and usage.blocked:
        if usage.live and not usage.is_empty() and codex_exhausted_window(usage, now) is not None:
            return _render_card(usage, now, fill_color=_CODEX_FILL, label_color=_CODEX_FILL)
        # The bars below are the last SUCCESSFUL call's figures and would read as healthy
        # headroom, so the refusal is stated first, in red, with the age of the numbers.
        # Card-sized wording (short_refusal_label), and no "BLOCKED —" prefix: the red ⛔
        # already says that, and the long CLI form wraps to three lines in 34 columns.
        banner = Text(f"⛔ {short_refusal_label(usage.blocked_reason)}\n", style="bold red")
        if usage.is_empty():
            return banner + Text("(no window data)", style="grey50")
        # The soonest window reset is when access actually returns — the refusal is a
        # rate limit, so waiting is the remedy. Say that, because the bars below still
        # show the last successful call's figures and read as headroom.
        resets = [
            w.resets_at for w in (usage.five_hour, usage.seven_day) if w and w.resets_at > now
        ]
        if resets:
            banner += Text(f"access returns {format_reset(min(resets), now)}\n", style="bold red")
        age = _format_age(now - usage.captured_at) if usage.captured_at else "?"
        # Live figures need no caveat: the endpoint reported the block AND the windows in
        # one answer, so the bars below ARE the state that fired. A rollout-sourced
        # snapshot instead carries the last SUCCESSFUL call's numbers, with only the
        # window ccc pinned reading 100% — say which is which.
        note = (
            f"live figures, {age} old"
            if usage.live
            else f"100% = the limit that fired; other figures are {age} old"
        )
        banner += Text(note + "\n", style="grey50")
        return banner + _render_card(usage, now, fill_color=_CODEX_FILL, label_color=_CODEX_FILL)
    if usage is None or usage.is_empty():
        return Text("—\n(run Codex to populate)", style="grey50")
    return _render_card(usage, now, fill_color=_CODEX_FILL, label_color=_CODEX_FILL)


# --- GitHub Copilot month-to-date usage ----------------------------------------
#
# Copilot bills against a **monthly** allowance (premium requests, resetting on the
# 1st) — historically premium requests, "AI Credits" since 2026-06. The card draws a
# bar of premium requests used ÷ the monthly quota (like the other two providers), with
# the AI-credit quantity and cost on a line beneath it. The data is the user's own, read
# via the official ``gh`` CLI hitting two per-user enhanced-billing endpoints
# (``/settings/billing/usage`` for AI credits, ``/settings/billing/premium_request/usage``
# for the bar) plus ``/copilot_internal/user`` for the seat's **live** credit meter and
# its real entitlement (:func:`_fetch_copilot_quota` — the only authoritative source for
# the denominator: entitlements differ per plan, and the billing endpoint lags by up to a
# day, so a hard-coded budget silently halves or doubles the percentage); no proxy. The
# network call is throttled (:func:`copilot_usage_stale`, cadence chosen by
# :func:`adaptive_interval` — the idle ``copilot_usage_refresh_sec`` normally, the shorter
# ``copilot_usage_refresh_active_sec`` while a job works) and run out-of-band (the daemon
# and a detached ``ccc copilot-usage`` spawn), never on the TUI's render path —
# :func:`read_copilot_usage` only reads the cached JSON.


@dataclass
class CopilotUsage:
    """A month-to-date GitHub Copilot consumption snapshot (one billing month)."""

    captured_at: int  # Unix epoch seconds when ccc fetched it
    year: int
    month: int  # 1..12 (UTC, matching GitHub's billing month)
    sku: str  # GitHub SKU minus the "Copilot " prefix, e.g. "AI Credits"
    unit: str  # human unit for the figure, e.g. "AI credits" / "premium requests"
    quantity: float  # month-to-date count in ``unit``
    gross: float  # USD list price before the subscription discount
    net: float  # USD actually charged (0.0 ⇒ covered by the subscription)
    # Premium-request window (drives the bar): month-to-date premium requests used vs
    # the plan's monthly allowance, resetting on the 1st. Defaulted so older callers /
    # cached files without these fields still construct cleanly.
    premium_used: float = 0.0  # premium requests consumed this month
    premium_quota: int = 300  # monthly included premium requests
    premium_reset_at: int = 0  # Unix epoch of the next reset (1st of next month, UTC)
    # AI-Credit window (drives the bar once the seat is on usage-based billing, where
    # premium_used reads 0): credits used vs the seat's entitlement. Both are read from
    # the live per-seat quota endpoint (:func:`_fetch_copilot_quota`) when it answers;
    # ``credit_quota`` falls back to the configured ``copilot_credit_quota`` guess and
    # ``credits_used`` to the (up-to-a-day stale) billing ``quantity``.
    credit_quota: int = 1900  # AI-Credit budget the bar is drawn against (Business baseline)
    credits_used: float = 0.0  # live credits consumed this month (0.0 ⇒ use ``quantity``)
    quota_source: str = ""  # "api" (seat entitlement) or "config" (fallback guess)


def _copilot_usage_path() -> Path:
    return config.app_home() / "copilot_usage.json"


def _gh_exe() -> str | None:
    """Locate the ``gh`` CLI (PATH, then the usual Homebrew/system spots)."""
    found = shutil.which("gh")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if Path(cand).exists():
            return cand
    return None


def _clean_sku(sku: str) -> str:
    """``"Copilot AI Credits"`` → ``"AI Credits"`` (drop the redundant product prefix)."""
    return sku[len("Copilot ") :] if sku.startswith("Copilot ") else sku


def _clean_unit(unit_type: str) -> str:
    """Map GitHub's ``unitType`` to a compact, readable noun for the card."""
    return {"AICredits": "AI credits", "Requests": "premium requests"}.get(
        unit_type, (unit_type or "units").lower()
    )


def _summarize_copilot(items: list[dict], year: int, month: int, now: int) -> CopilotUsage:
    """Collapse a month's Copilot billing line-items into one headline figure.

    A month may carry more than one SKU (e.g. during the premium-request→AI-credit
    switch); units differ between SKUs, so the headline quantity is the **largest
    single SKU** by count, while the cost line sums gross/net across *all* Copilot
    rows (dollars are comparable even when units are not).
    """
    by_sku: dict[str, list[float]] = {}  # sku -> [qty, gross, net, unit_index]
    units: dict[str, str] = {}
    for item in items:
        sku = str(item.get("sku", ""))
        acc = by_sku.setdefault(sku, [0.0, 0.0, 0.0])
        acc[0] += float(item.get("quantity", 0) or 0)
        acc[1] += float(item.get("grossAmount", 0) or 0)
        acc[2] += float(item.get("netAmount", 0) or 0)
        units.setdefault(sku, str(item.get("unitType", "")))
    if not by_sku:
        return CopilotUsage(now, year, month, sku="", unit="", quantity=0.0, gross=0.0, net=0.0)
    head_sku, head = max(by_sku.items(), key=lambda kv: kv[1][0])
    return CopilotUsage(
        captured_at=now,
        year=year,
        month=month,
        sku=_clean_sku(head_sku),
        unit=_clean_unit(units.get(head_sku, "")),
        quantity=head[0],
        gross=sum(v[1] for v in by_sku.values()),
        net=sum(v[2] for v in by_sku.values()),
    )


def _write_copilot_usage(snap: CopilotUsage) -> None:
    payload = {
        "captured_at": snap.captured_at,
        "year": snap.year,
        "month": snap.month,
        "sku": snap.sku,
        "unit": snap.unit,
        "quantity": snap.quantity,
        "gross": snap.gross,
        "net": snap.net,
        "premium_used": snap.premium_used,
        "premium_quota": snap.premium_quota,
        "premium_reset_at": snap.premium_reset_at,
        "credit_quota": snap.credit_quota,
        "credits_used": snap.credits_used,
        "quota_source": snap.quota_source,
    }
    path = _copilot_usage_path()
    # Same flock + mkstemp treatment as write_usage (the daemon and a TUI-spawned
    # `ccc copilot-usage` can both write this cache): serialize and never raise.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path.with_name(path.name + ".lock")):
            _atomic_write_json(path, payload)
    except OSError:
        pass


def _next_month_reset(year: int, month: int) -> int:
    """Unix epoch (UTC) of the 1st of the month *after* ``year``/``month`` — the reset."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return calendar.timegm((ny, nm, 1, 0, 0, 0, 0, 0, 0))


def _fetch_premium_used(gh: str, login: str) -> float:
    """Month-to-date premium requests (sum of ``grossQuantity``); 0.0 on any failure.

    Reads the per-user premium-request endpoint (the figure the bar is drawn against;
    GitHub's ``/users/{login}/settings/billing/usage`` API). Best-effort:
    since the 2026-06 switch to AI Credits this often reads 0, which is the true
    premium-request count, so the bar simply sits at 0% while credits accrue separately.
    """
    try:
        raw = subprocess.run(  # noqa: S603
            [gh, "api", f"/users/{login}/settings/billing/premium_request/usage"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return 0.0
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    return sum(
        float(i.get("grossQuantity", 0) or 0)
        for i in data.get("usageItems", [])
        if isinstance(i, dict)
    )


def _fetch_copilot_quota(gh: str) -> tuple[int, float] | None:
    """The seat's live AI-Credit meter as ``(entitlement, credits_used)``; None if unusable.

    Reads ``/copilot_internal/user`` — the endpoint the editor plugins use — whose
    ``quota_snapshots.premium_interactions`` carries the seat's **actual** monthly
    entitlement and the credits burnt against it *right now*. This is the only place the
    denominator can be learnt: it varies per plan (1,500 on an individual/faculty seat,
    other values on Business/Enterprise), so the configured budget is only ever a guess.

    Returns None when the call fails, when the seat is ``unlimited`` (no meaningful
    denominator), or when the entitlement is absent — callers then keep the configured
    fallback. Never raises.
    """
    try:
        raw = subprocess.run(  # noqa: S603
            [gh, "api", "/copilot_internal/user"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout
        data = json.loads(raw)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    snap = (data.get("quota_snapshots") or {}).get("premium_interactions")
    if not isinstance(snap, dict) or snap.get("unlimited"):
        return None
    try:
        entitlement = int(float(snap.get("entitlement", 0) or 0))
        used = float(snap.get("credits_used", 0) or 0)
    except (TypeError, ValueError):
        return None
    return (entitlement, used) if entitlement > 0 else None


def fetch_copilot_usage(
    now: int | None = None, quota: int = 300, credit_quota: int | None = None
) -> CopilotUsage | None:
    """Fetch this month's Copilot usage via ``gh`` and cache it; ``None`` on any failure.

    Resolves the login then queries the per-user enhanced-billing usage endpoint scoped
    to the current UTC year/month (AI-credit quantity + cost) AND the premium-request
    endpoint (the count the bar is drawn against, vs the monthly *quota*). Best-effort and
    never raises: a missing ``gh``, an auth/scope error, a timeout, or malformed JSON all
    return ``None`` so callers (daemon, detached spawn) degrade to the last cache.
    """
    now = int(time.time()) if now is None else now
    gh = _gh_exe()
    if not gh:
        return None
    tm = time.gmtime(now)
    try:
        login = subprocess.run(  # noqa: S603
            [gh, "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        if not login:
            return None
        raw = subprocess.run(  # noqa: S603
            [
                gh,
                "api",
                f"/users/{login}/settings/billing/usage?year={tm.tm_year}&month={tm.tm_mon}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    items = [
        i
        for i in data.get("usageItems", [])
        if isinstance(i, dict) and i.get("product") == "copilot"
    ]
    if credit_quota is None:
        try:
            credit_quota = config.load_config().copilot_credit_quota
        # pylint: disable=broad-exception-caught
        except Exception:  # noqa: BLE001 - fall back to the Business baseline budget
            credit_quota = 1900
    snap = _summarize_copilot(items, tm.tm_year, tm.tm_mon, now)
    snap.premium_used = _fetch_premium_used(gh, login)
    snap.premium_quota = max(1, quota)
    snap.premium_reset_at = _next_month_reset(tm.tm_year, tm.tm_mon)
    # The seat's own entitlement + live meter beat both the configured guess and the
    # billing endpoint's up-to-a-day-stale quantity; fall back to them when unavailable.
    live = _fetch_copilot_quota(gh)
    if live is not None:
        snap.credit_quota, snap.credits_used = max(1, live[0]), live[1]
        snap.quota_source = "api"
    else:
        snap.credit_quota = max(1, credit_quota)
        snap.quota_source = "config"
    _write_copilot_usage(snap)
    return snap


def read_copilot_usage() -> CopilotUsage | None:
    """Load the last cached Copilot snapshot, or ``None`` if absent/unreadable."""
    data = _load_json_dict(_copilot_usage_path())
    if data is None:
        return None
    try:
        return CopilotUsage(
            captured_at=int(data.get("captured_at", 0) or 0),
            year=int(data.get("year", 0) or 0),
            month=int(data.get("month", 0) or 0),
            sku=str(data.get("sku", "")),
            unit=str(data.get("unit", "")),
            quantity=float(data.get("quantity", 0) or 0),
            gross=float(data.get("gross", 0) or 0),
            net=float(data.get("net", 0) or 0),
            premium_used=float(data.get("premium_used", 0) or 0),
            premium_quota=int(data.get("premium_quota", 300) or 300),
            premium_reset_at=int(data.get("premium_reset_at", 0) or 0),
            credit_quota=int(data.get("credit_quota", 1900) or 1900),
            credits_used=float(data.get("credits_used", 0) or 0),
            quota_source=str(data.get("quota_source", "")),
        )
    except (TypeError, ValueError):
        return None


def copilot_usage_stale(refresh_sec: float, now: int | None = None) -> bool:
    """True if the cache is missing or older than ``refresh_sec`` (drives the refresh).

    ``refresh_sec`` is chosen by :func:`adaptive_interval` at the call sites (daemon +
    TUI-spawned refresh): the idle ``copilot_usage_refresh_sec`` normally, or the shorter
    ``copilot_usage_refresh_active_sec`` while any job is actively working.
    """
    now = int(time.time()) if now is None else now
    try:
        mtime = _copilot_usage_path().stat().st_mtime
    except OSError:
        return True
    return (now - int(mtime)) >= refresh_sec


def _fmt_credits(value: float) -> str:
    """Credits for the bar's emboss: a decimal while small, whole once it would widen the row."""
    return f"{value:.1f}" if value < 100 else f"{value:.0f}"


def render_copilot_usage(usage: CopilotUsage | None, now: int | None = None) -> Text:
    """Render the GitHub Copilot card as a single premium-request bar.

    The bar mirrors the other two providers' (used ÷ monthly quota, reset embossed
    inside, percentage flush-right), so all three cards read the same. The standalone
    "Premium requests" title line and the AI-credit/cost line beneath were dropped —
    the embossed "Resets in …" is enough.
    """
    now = int(time.time()) if now is None else now
    if usage is None:
        return Text("—\n(run `ccc copilot-usage` to populate)", style="grey50")
    text = Text()
    if usage.premium_reset_at:
        days = max(0, (usage.premium_reset_at - now) // 86400)
        reset = f"Resets in {days}d"
    else:
        reset = ""

    # Premium requests were retired for AI-Credit seats (that meter reads 0), so once
    # the active SKU is AI Credits draw the bar from credits used ÷ the seat's credit
    # entitlement, embossing ``used/quota`` so the denominator is visible on the card
    # (a wrong one is exactly how a 100%-consumed seat once read 50%). ``credits_used``
    # is the live meter; ``quantity`` is the billing endpoint's laggier stand-in.
    used = usage.credits_used or usage.quantity
    if usage.unit == "AI credits" and used > 0:
        quota = max(1, usage.credit_quota)
        pct = used / quota * 100
        credit_label = f"{_fmt_credits(used)}/{quota}cr"
        label = f"{reset} · {credit_label}" if reset else f"{credit_label} used"
    else:
        quota = max(1, usage.premium_quota)
        pct = usage.premium_used / quota * 100
        label = reset

    text.append_text(_bar_row(pct, _COPILOT_FILL, label=label, label_color=_COPILOT_FILL))
    text.rstrip()
    return text
