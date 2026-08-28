"""Account-wide subscription usage snapshots — Claude Code's ``/usage`` and Codex.

Two providers, same two-window shape (a 5h session + a weekly window). Claude's
numbers arrive via the status-line JSON (captured by ``ccc statusline
--capture-usage``); Codex has no endpoint, so :func:`read_codex_usage` reads the
``rate_limits`` block Codex writes onto each ``token_count`` event in its session
rollout files (the windows are identified by their duration, not primary/secondary
position). Codex emits more than one block shape — ``limit_id: "codex"`` carries the
windows, while short ``codex exec`` runs log a windowless ``limit_id: "premium"`` block
— so the reader skips windowless blocks and scans back through enough files to find the
freshest one with real data.

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
from datetime import datetime
from pathlib import Path

from rich.text import Text

from . import config
from .codex_in_claude import (
    _FIVE_HOUR_MINUTES,
    _SEVEN_DAY_MINUTES,
    _latest_rate_limits_event,
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
# flat brand fill. The bar is wide enough to hold the longest embossed label ("Week:
# Resets in 6d 23h 59m" = 26 chars); the percentage is then right-aligned to
# _CARD_INNER_WIDTH so it sits flush at the card's inner edge (no dead space before the
# border).
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
_codex_cache: tuple[str, int, Usage | None] | None = None

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
class Usage:
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

    def is_empty(self) -> bool:
        return self.five_hour is None and self.seven_day is None


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
_TEMP_PATTERNS = ("usage.json.*.tmp", "usage-*.json.*.tmp", "copilot_usage.json.*.tmp")
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
    try:
        raw = _usage_path(account).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
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


def read_codex_usage(now: int | None = None) -> Usage | None:
    """Current Codex rate-limit snapshot, from the newest session rollout file.

    Codex has no usage endpoint; it writes duration-identified ``rate_limits`` windows
    onto each ``token_count`` event in
    ``$CODEX_HOME/sessions/**/rollout-*.jsonl``. The data is account-global, so the
    freshest entry from any session is the live allocation. Parsing the newest file
    is cached by its ``(path, mtime)`` so the 5 s TUI refresh stays cheap when idle.
    """
    global _codex_cache  # noqa: PLW0603  # tiny module-level parse cache keyed by file mtime
    now = int(time.time()) if now is None else now
    sessions_dir = config.codex_home() / "sessions"
    try:
        files = sorted(
            sessions_dir.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    if not files:
        return None
    newest = files[0]
    try:
        key = (str(newest), int(newest.stat().st_mtime_ns))
    except OSError:
        return None
    if _codex_cache is not None and (_codex_cache[0], _codex_cache[1]) == key:
        return _codex_cache[2]
    snapshot: Usage | None = None
    for path in files[:_CODEX_SCAN_LIMIT]:
        rate_snapshot = _latest_rate_limits_event(path)
        if rate_snapshot is None:
            continue
        windows = {
            minutes: Window(used_percentage=parsed.used_percent, resets_at=parsed.resets_at)
            for minutes, parsed in rate_snapshot.windows.items()
        }

        try:
            captured = int(path.stat().st_mtime)
        except OSError:
            captured = now
        snapshot = Usage(
            captured_at=captured,
            five_hour=windows.get(_FIVE_HOUR_MINUTES),
            seven_day=windows.get(_SEVEN_DAY_MINUTES),
        )
        break
    _codex_cache = (key[0], key[1], snapshot)
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
) -> Text:
    """A ``_BAR_WIDTH``-cell usage bar with *label* embossed over it.

    The fill/track colours stay as each cell's background (usage stays visible);
    only the glyphs covered by *label* change — dark over the bright fill, the
    card's *label_color* over the dark track — so the reset text rides inside the
    bar instead of lengthening the row. *label* is left-aligned; its spaces fall
    back to a solid block so the bar reads as continuous.
    """
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * _BAR_WIDTH)
    label = label[:_BAR_WIDTH]
    bar = Text()
    for i in range(_BAR_WIDTH):
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


def _append_pct(text: Text, pct: float) -> None:
    """Append the percentage flush-right to the card's inner edge (no dead space).

    The bar is ``_BAR_WIDTH`` wide; the percentage is right-aligned to
    ``_CARD_INNER_WIDTH`` so it lands against the box's right padding instead of
    leaving empty cells between it and the border.
    """
    pct_str = f"{int(round(pct))}%"
    pad = max(1, _CARD_INNER_WIDTH - _BAR_WIDTH - len(pct_str))
    text.append(" " * pad + pct_str + "\n", style=_PCT_STYLE)


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
    text.append_text(_bar(win.used_percentage, fill_color, label=embossed, label_color=label_color))
    _append_pct(text, win.used_percentage)
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
    """Render the two-bar OpenAI Codex usage card (green bars) as Rich ``Text``."""
    now = int(time.time()) if now is None else now
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
    credit_quota: int = 3000  # AI-Credit budget the bar is drawn against
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
        except Exception:  # noqa: BLE001 - fall back to the default budget
            credit_quota = 3000
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
    try:
        raw = _copilot_usage_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
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
            credit_quota=int(data.get("credit_quota", 3000) or 3000),
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

    text.append_text(_bar(pct, _COPILOT_FILL, label=label, label_color=_COPILOT_FILL))
    _append_pct(text, pct)
    text.rstrip()
    return text
