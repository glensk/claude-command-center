#!/usr/bin/env python3
"""Fast, cache-first quota oracle — "which provider still has tokens, and until when?"

Every LLM-calling tool in this toolbox walks a *fallback ladder* of providers (GitHub
Copilot seat → Codex/ChatGPT seat → Claude subscription). Left alone, each rung learns it
is exhausted only by ATTEMPTING it and failing, which is exactly the failure this module
exists to end: a Copilot seat that is hard-429 for three days still cost ``ai.py push`` a
300-second doomed retry on every single commit.

This module answers the question from **cache** in ~70 ms — no network unless a refresh is
asked for explicitly — so consulting it before building a ladder is free. It aggregates the
snapshots ccc already maintains (:mod:`.usage`) and adds the one signal they lack: an
authoritative *observed rejection* store (:data:`_COOLDOWNS_NAME`).

Four states, and the distinction between them is the whole design:

* ``available`` — headroom is proven by fresh, authoritative data.
* ``blocked``   — proven exhausted: a window at 100 %, or a provider's own rejection whose
  retry deadline has not passed. A blocked rung SHOULD be skipped.
* ``unknown``   — no data, stale data, or a *guessed* denominator. Never treated as blocked:
  refusing to try a provider because we failed to measure it is how a working rung gets
  silently deleted. Callers **fail open** on ``unknown``.
* ``disabled``  — a capability fact, not a quota fact (e.g. the Gemini CLI's individual tier
  was retired). It cannot be "waited out", so it is never given a reset time.

**Windows are never collapsed into one percentage.** A provider can sit at 100 % on its
5-hour window and 49 % on its weekly one; a single ``used_pct`` would render that as
healthy and send the caller straight into a rejection. Each provider therefore carries a
``windows`` map and, when blocked, a ``blocked_by`` naming the window that blocks and a
``resets_at`` taken from *that* window.

**Hard exhaustion is not the same as routing risk.** :mod:`.routing` deprioritizes an
account at ``_EXHAUSTED_PCT`` (90 %) because a long job launched there might die mid-run —
a sensible *risk* threshold that would, if reused here, throw away a tenth of a paid
subscription. This module reports ``risky`` separately from ``blocked``; only ``blocked``
removes a rung.

**Model scoping matters.** Claude accounts expose several windows, including a
Fable-model-scoped weekly one. An account at 100 % on ``fable_week`` is NOT out of tokens
for an Opus request. :func:`snapshot` therefore takes the model being requested and
consults only the windows that apply to it — see :data:`_FABLE_MODEL_HINTS`.

Runnable directly (``./command_center/quota.py -h``) as well as through ``ccc quota`` —
see :mod:`._direct`. :func:`main` forwards to the CLI rather than reimplementing the
report, so there is exactly one implementation of it.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config, usage

# Schema version of the ``snapshot()`` payload / ``ccc quota --json`` contract. Consumers
# (notably the ``ai.py`` commit-message ladder) MUST refuse a version they do not know
# rather than misread a renamed field — an oracle that is silently misparsed is worse than
# no oracle, because it removes working rungs.
#
# v2 (2026-09-01): the ``codex`` row now means the CANONICAL team seat (``~/.codex``,
# env-independent) and a second ``codex:private`` row appears when a private login is
# configured; ``best_codex_account`` / ``codex_pin`` name the seat delegation should
# bill; windows carry ``evidence_at``; cooldown-backed rows carry ``block_scope``.
# A v1 consumer reading a v2 payload could misattribute the ``codex`` row, so the
# version is bumped and old consumers fail closed to "no opinion" — by design.
SCHEMA_VERSION = 2

# Provider states. Only BLOCKED may remove a rung from a ladder; UNKNOWN deliberately
# stays runnable (fail-open), and DISABLED is a config/capability fact with no reset.
AVAILABLE = "available"
BLOCKED = "blocked"
UNKNOWN = "unknown"
DISABLED = "disabled"

# Exit codes for ``ccc quota --provider`` — a shell caller's whole API.
EXIT_AVAILABLE = 0
EXIT_BLOCKED = 1
EXIT_UNKNOWN = 2

# A window is hard-exhausted at 100 %. This is deliberately NOT routing._EXHAUSTED_PCT
# (90 %), which is a risk threshold for launching long jobs — see the module docstring.
_EXHAUSTED_PCT = 100.0
# Mirrors routing._EXHAUSTED_PCT: reported as advisory ``risky``, never as blocked.
_RISKY_PCT = 90.0

# Past this age a Claude/Codex snapshot predates its own window's lifetime and can no
# longer be read as "usage right now" → UNKNOWN, not blocked. Mirrors usage.py's card
# staleness thresholds, per window.
_SESSION_STALE_AFTER_SEC = 5 * 3600
_WEEK_STALE_AFTER_SEC = 7 * 86400
# The Copilot billing snapshot lags by up to a day; past this it cannot establish anything.
_COPILOT_STALE_AFTER_SEC = 24 * 3600

# Models whose usage is governed by the Fable-scoped weekly window. Any other model
# ignores ``fable_week`` entirely — the bug this mapping exists to prevent is treating a
# Fable-week-exhausted account as out of tokens for an Opus request.
_FABLE_MODEL_HINTS = ("fable",)

# The Fable weekly figure is only as fresh as the last successful OAuth fetch
# (``oauth_fetched_at``): statusline writes refresh ``captured_at`` while PRESERVING a
# stale Fable value, so judging ``fable_week`` by ``captured_at`` + 7d let a days-old
# figure govern verdicts as if live. Mirrors the card's own threshold
# (:data:`usage._FABLE_STALE_AFTER_SEC`).
_FABLE_EVIDENCE_STALE_SEC = usage._FABLE_STALE_AFTER_SEC  # noqa: SLF001

_COOLDOWNS_NAME = "cooldowns.json"

# Cooldown entry kinds. ``observed`` is a provider's own rejection with a retry
# deadline; ``hold`` is an ADMINISTRATIVE reservation ("do not use this seat until…")
# that no observed rejection or success may overwrite or shorten — only its own expiry
# or an explicit clear removes it.
KIND_OBSERVED = "observed"
KIND_HOLD = "hold"


@dataclass
class WindowState:
    """One rate-limit window, resolved to a state.

    ``stale`` is tracked separately from ``used_pct`` because a stale 100 % must NOT
    block: it is a reading from a window that may since have reset. ``evidence_at`` is
    when the figure itself was measured — for ``fable_week`` that is the OAuth fetch,
    which can be much older than the snapshot's ``captured_at``.
    """

    name: str
    used_pct: float
    resets_at: int
    stale: bool = False
    evidence_at: int = 0

    @property
    def exhausted(self) -> bool:
        """True only for a *live, fresh* window at/over 100 %."""
        return not self.stale and self.used_pct >= _EXHAUSTED_PCT

    @property
    def risky(self) -> bool:
        """Advisory: at/over routing's 90 % risk threshold, but not necessarily blocked."""
        return not self.stale and self.used_pct >= _RISKY_PCT


@dataclass
class ProviderQuota:
    """One provider (or one Claude account) resolved to a state, with its evidence."""

    id: str  # "copilot" | "codex[:private]" | "gemini" | "claude:<account>"
    kind: str  # "copilot" | "codex" | "gemini" | "claude"
    state: str  # AVAILABLE | BLOCKED | UNKNOWN | DISABLED
    reason: str = ""  # human explanation, always set for non-available states
    source: str = ""  # where the verdict came from: "cooldown" | "meter" | "windows" | "config"
    windows: dict[str, WindowState] = field(default_factory=dict)
    blocked_by: str = ""  # name of the window/signal that blocks
    resets_at: int = 0  # reset of the BLOCKING window (0 when not blocked)
    captured_at: int = 0  # when the underlying snapshot was taken
    risky: bool = False  # advisory 90 % flag (never removes a rung on its own)
    account: str = ""  # Claude account label / Codex home label ("default"|"private")
    config_dir: str = ""  # Claude account config dir, when kind == "claude"
    urgency: float | None = None  # %/hour burn needed to exhaust by reset (Claude only)
    email: str = ""  # billable identity behind a Codex home (auth.json id_token)
    block_scope: str = ""  # cooldown-backed blocks: the entry's scope (e.g. "auth", "hold")


def _cooldowns_path() -> Path:
    """Path of the observed-rejection store (beside the other usage snapshots)."""
    return config.app_home() / _COOLDOWNS_NAME


def _read_cooldowns_unlocked() -> dict[str, dict]:
    """Load the cooldown map, or ``{}`` when absent/corrupt. Never raises."""
    try:
        raw = _cooldowns_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("providers")
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def read_cooldowns(now: int | None = None) -> dict[str, dict]:
    """Live (unexpired) cooldown entries, keyed by provider id.

    Expired entries are filtered out on READ rather than deleted, so a reader never needs
    the write lock and a crash can never resurrect a stale block.
    """
    now = int(time.time()) if now is None else now
    return {
        pid: entry
        for pid, entry in _read_cooldowns_unlocked().items()
        if int(entry.get("blocked_until", 0) or 0) > now
    }


def _is_live_hold(entry: object, now: int) -> bool:
    """True for an unexpired administrative hold entry."""
    return (
        isinstance(entry, dict)
        and entry.get("kind") == KIND_HOLD
        and int(entry.get("blocked_until", 0) or 0) > now
    )


def record_block(
    provider: str,
    *,
    blocked_until: int,
    reason: str = "",
    status: int = 0,
    scope: str = "",
    observed_at: int | None = None,
    source: str = "",
    kind: str = KIND_OBSERVED,
) -> dict:
    """Record an authoritative rejection (or a ``kind="hold"``) for *provider*.

    Returns the stored entry. The whole read-merge-write runs under ONE
    :func:`usage._flock`: atomic replacement alone prevents a corrupt file but not a
    lost update, and concurrent ``ai.py push`` runs marking different providers would
    otherwise silently drop one another's entries.

    Writes are ordered by ``observed_at``, not by arrival: a 429 observed before a later
    success must never overwrite that success just because its process was slower to
    write. An older observation is therefore discarded, not applied.

    An unexpired HOLD outranks every observed write regardless of timestamps: "do not
    use this seat until <deadline>" is policy, and a provider rejection with a shorter
    retry must not quietly shorten it. Only another explicit hold (or expiry / an
    explicit clear) replaces a hold.
    """
    observed_at = int(time.time()) if observed_at is None else int(observed_at)
    entry = {
        "blocked_until": int(blocked_until),
        "observed_at": observed_at,
        "reason": reason,
        "status": int(status),
        "scope": scope,
        "source": source,
        "kind": kind if kind in (KIND_OBSERVED, KIND_HOLD) else KIND_OBSERVED,
    }
    path = _cooldowns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
        current = _read_cooldowns_unlocked()
        existing = current.get(provider)
        if _is_live_hold(existing, observed_at) and entry["kind"] != KIND_HOLD:
            return existing  # type: ignore[return-value]  # an unexpired hold stands
        if isinstance(existing, dict) and int(existing.get("observed_at", 0) or 0) > observed_at:
            return existing  # a NEWER observation already stands — do not regress it
        current[provider] = entry
        usage._atomic_write_json(  # noqa: SLF001
            path, {"version": SCHEMA_VERSION, "providers": current}
        )
    return entry


def clear_block(
    provider: str, *, observed_at: int | None = None, observed_only: bool = False
) -> bool:
    """Drop *provider*'s block (a success, or an explicit ``--clear``). True if removed.

    Also ``observed_at``-ordered: clearing is just another observation, so a stale success
    cannot wipe a block recorded after it.

    ``observed_only`` is the SUCCESS-path mode (``ai.py`` clearing a memoized auth
    failure after a rung served): it refuses to touch an unexpired hold, because a
    provider working again says nothing about an administrative reservation. An
    explicit ``ccc quota -c`` (without ``-O``) removes anything.
    """
    observed_at = int(time.time()) if observed_at is None else int(observed_at)
    path = _cooldowns_path()
    if not path.exists():
        return False
    with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
        current = _read_cooldowns_unlocked()
        existing = current.get(provider)
        if not isinstance(existing, dict):
            return False
        if observed_only and _is_live_hold(existing, observed_at):
            return False  # a success never lifts an administrative hold
        if int(existing.get("observed_at", 0) or 0) > observed_at:
            return False  # a newer block stands
        del current[provider]
        usage._atomic_write_json(  # noqa: SLF001
            path, {"version": SCHEMA_VERSION, "providers": current}
        )
    return True


def _window_state(
    name: str,
    win: usage.Window | None,
    captured_at: int,
    now: int,
    stale_after: int,
    evidence_at: int | None = None,
) -> WindowState | None:
    """Resolve one :class:`usage.Window` to a :class:`WindowState`, or ``None`` if absent.

    A window whose ``resets_at`` has already passed is reported stale: the snapshot
    describes a window that no longer exists, so its percentage proves nothing.

    *evidence_at* overrides which timestamp ages the figure. ``fable_week`` needs it:
    statusline writes refresh ``captured_at`` while carrying the OLD Fable value
    forward, so aging that window by ``captured_at`` reported a days-stale figure as
    live — the bug that let a stale Fable reading govern a definitive verdict.
    """
    if win is None:
        return None
    basis = captured_at if evidence_at is None else evidence_at
    stale = (basis + stale_after) < now or win.resets_at <= now
    return WindowState(
        name=name,
        used_pct=float(win.used_percentage),
        resets_at=int(win.resets_at),
        stale=stale,
        evidence_at=basis,
    )


def _windows_for_model(windows: dict[str, WindowState], model: str) -> list[WindowState]:
    """The windows that actually govern *model*.

    ``fable_week`` applies ONLY to Fable models. Including it for an Opus request is the
    concrete bug this function prevents: an account at 100 % Fable-week but 83 % on its
    plain weekly window has ample Opus headroom and must not be reported blocked.
    """
    wants_fable = any(hint in model.lower() for hint in _FABLE_MODEL_HINTS)
    return [win for name, win in windows.items() if name != "fable_week" or wants_fable]


def _verdict_from_windows(
    windows: Iterable[WindowState],
) -> tuple[str, str, str, int, bool]:
    """Fold governing windows into ``(state, reason, blocked_by, resets_at, risky)``.

    Any exhausted window blocks (the most-consumed one is named). Otherwise, a provider
    with at least one fresh window is available; one with only stale windows is UNKNOWN —
    never blocked, because staleness is a measurement failure, not proof of exhaustion.
    """
    wins = list(windows)
    if not wins:
        return UNKNOWN, "no window data", "", 0, False
    blocking = [w for w in wins if w.exhausted]
    if blocking:
        worst = max(blocking, key=lambda w: w.used_pct)
        return (
            BLOCKED,
            f"{worst.name} window at {worst.used_pct:.0f}%",
            worst.name,
            worst.resets_at,
            True,
        )
    fresh = [w for w in wins if not w.stale]
    if not fresh:
        return UNKNOWN, "snapshot stale", "", 0, False
    return AVAILABLE, "", "", 0, any(w.risky for w in fresh)


def _cooldown_quota(pid: str, kind: str, entry: dict) -> ProviderQuota:
    """Build a BLOCKED provider straight from a cooldown entry (rejection or hold)."""
    is_hold = entry.get("kind") == KIND_HOLD
    return ProviderQuota(
        id=pid,
        kind=kind,
        state=BLOCKED,
        reason=str(
            entry.get("reason")
            or ("administrative hold" if is_hold else "provider rejected the request")
        ),
        source="hold" if is_hold else "cooldown",
        blocked_by="hold" if is_hold else "observed-rejection",
        resets_at=int(entry.get("blocked_until", 0) or 0),
        captured_at=int(entry.get("observed_at", 0) or 0),
        risky=True,
        block_scope=str(entry.get("scope") or ("hold" if is_hold else "")),
    )


def _claude_quota(account: str, model: str, now: int, cooldowns: dict[str, dict]) -> ProviderQuota:
    """Resolve one Claude account against the windows that govern *model*."""
    pid = f"claude:{account}"
    config_dir = str(config.claude_config_dirs().get(account, ""))
    if pid in cooldowns:
        quota = _cooldown_quota(pid, "claude", cooldowns[pid])
        quota.account, quota.config_dir = account, config_dir
        return quota
    snap = usage.read_usage(account)
    if snap is None:
        return ProviderQuota(
            id=pid,
            kind="claude",
            state=UNKNOWN,
            reason="no usage snapshot",
            source="windows",
            account=account,
            config_dir=config_dir,
        )
    windows: dict[str, WindowState] = {}
    for name, win, stale_after, evidence_at in (
        ("five_hour", snap.five_hour, _SESSION_STALE_AFTER_SEC, None),
        ("seven_day", snap.seven_day, _WEEK_STALE_AFTER_SEC, None),
        # Fable's figure only changes on a successful OAuth fetch; statusline writes
        # refresh captured_at while carrying the old value, so the fetch time is the
        # honest evidence age and the card's 1-hour threshold applies.
        ("fable_week", snap.fable_week, _FABLE_EVIDENCE_STALE_SEC, snap.oauth_fetched_at),
    ):
        state = _window_state(name, win, snap.captured_at, now, stale_after, evidence_at)
        if state is not None:
            windows[name] = state
    governing = _windows_for_model(windows, model)
    verdict, reason, blocked_by, resets_at, risky = _verdict_from_windows(governing)
    quota = ProviderQuota(
        id=pid,
        kind="claude",
        state=verdict,
        reason=reason,
        source="windows",
        windows=windows,
        blocked_by=blocked_by,
        resets_at=resets_at,
        captured_at=snap.captured_at,
        risky=risky,
        account=account,
        config_dir=config_dir,
    )
    quota.urgency = _urgency(governing, now)
    return quota


def _urgency(windows: Iterable[WindowState], now: int) -> float | None:
    """``(100 - used) / hours_to_reset`` over the governing weekly window.

    The percentage-points-per-hour you would have to burn to exactly exhaust the
    remaining allowance by its reset. Ranking accounts by DESCENDING urgency spends the
    allowance that resets soonest first, so no headroom evaporates unused — this is the
    same metric :mod:`.routing` uses for job placement, recomputed here over the windows
    that govern the requested model rather than always over ``fable_week``.
    """
    weekly = [w for w in windows if w.name in ("seven_day", "fable_week") and not w.stale]
    if not weekly:
        return None
    win = max(weekly, key=lambda w: w.used_pct)
    hours = max((win.resets_at - now) / 3600.0, 1 / 60)
    return (100.0 - min(100.0, max(0.0, win.used_pct))) / hours


def _canonical_codex_homes() -> dict[str, Path]:
    """Label → ``CODEX_HOME`` for every Codex login, ENV-INDEPENDENT and deduped.

    Deliberately NOT :func:`config.codex_homes`: that honours an ambient
    ``$CODEX_HOME``, so a process launched with the private home in its env would
    label the private path ``codex`` and list the same seat twice — and a hold on
    ``codex`` would then mean different seats in different processes. Provider ids
    must name the same billable identity everywhere.

    ``default`` and ``private`` first, then one entry per ``codex_homes_extra`` login in
    config order. A home whose path is already listed is DROPPED, however it is spelled:
    two labels for one seat would double-count it and split its holds.
    """
    homes: dict[str, Path] = {"default": Path.home() / ".codex"}

    def _is_new(candidate: Path) -> bool:
        """True when *candidate* is not already one of the collected homes."""
        for home in homes.values():
            try:
                same = candidate.expanduser().resolve() == home.expanduser().resolve()
            except OSError:  # pragma: no cover - resolve() fails only on exotic filesystems
                same = str(candidate) == str(home)
            if same:
                return False
        return True

    private = config.codex_home_private()
    if private is not None and _is_new(private):
        homes["private"] = private
    for label, home in config.codex_homes_extra().items():
        if _is_new(home):
            homes[label] = home
    return homes


def _codex_seat_quota(
    pid: str, label: str, home: Path, now: int, cooldowns: dict[str, dict]
) -> ProviderQuota:
    """Resolve ONE Codex/ChatGPT seat from its cached window snapshot."""
    email = ""
    try:
        email = usage.codex_account_email(home) or ""
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        email = ""  # identity is display metadata, never a reason to fail the row
    if pid in cooldowns:
        quota = _cooldown_quota(pid, "codex", cooldowns[pid])
        quota.account, quota.email = label, email
        return quota
    snap = usage.read_codex_usage(now, home)
    if snap is None:
        return ProviderQuota(
            id=pid,
            kind="codex",
            state=UNKNOWN,
            reason="no usage snapshot",
            source="windows",
            account=label,
            email=email,
        )
    windows: dict[str, WindowState] = {}
    for name, win, stale_after in (
        ("five_hour", snap.five_hour, _SESSION_STALE_AFTER_SEC),
        ("seven_day", snap.seven_day, _WEEK_STALE_AFTER_SEC),
    ):
        state = _window_state(name, win, snap.captured_at, now, stale_after)
        if state is not None:
            windows[name] = state
    if snap.blocked:
        # Codex is refusing calls. ``read_codex_usage`` has already pinned the window
        # that filled to 100%, so name it as the blocker and carry its reset — that is
        # when access returns, and it is what ``unblocks`` should show instead of "—".
        full = max(
            (state for state in windows.values() if state.used_pct >= _EXHAUSTED_PCT),
            key=lambda state: state.resets_at,
            default=None,
        )
        return ProviderQuota(
            id=pid,
            kind="codex",
            state=BLOCKED,
            reason=snap.blocked_reason,
            source="refusal",
            windows=windows,
            blocked_by=full.name if full is not None else "refusal",
            resets_at=full.resets_at if full is not None else 0,
            captured_at=snap.blocked_at or snap.captured_at,
            account=label,
            email=snap.email or email,
        )
    verdict, reason, blocked_by, resets_at, risky = _verdict_from_windows(windows.values())
    return ProviderQuota(
        id=pid,
        kind="codex",
        state=verdict,
        reason=reason,
        source="windows",
        windows=windows,
        blocked_by=blocked_by,
        resets_at=resets_at,
        captured_at=snap.captured_at,
        risky=risky,
        account=label,
        email=snap.email or email,
    )


def _codex_quotas(now: int, cooldowns: dict[str, dict]) -> list[ProviderQuota]:
    """One row per configured Codex seat.

    ``codex`` (team), then ``codex:private``, then one ``codex:<label>`` per
    ``codex_homes_extra`` login — the ids an account pin and a hold are named by.
    """
    rows = []
    for label, home in _canonical_codex_homes().items():
        pid = "codex" if label == "default" else f"codex:{label}"
        rows.append(_codex_seat_quota(pid, label, home, now, cooldowns))
    return rows


def _codex_pin_label(homes: dict[str, Path]) -> str:
    """The label of an ACTIVE codex-in-claude account pin, or ``""``.

    The pin lives in codex-in-claude's own config (``codex_home`` +
    ``codex_home_until``); mapping its path onto the canonical homes names the seat in
    this module's vocabulary. A pin at a path outside the known homes reports ``""`` —
    the selector then treats it as absent rather than inventing an id.
    """
    from . import codex_in_claude  # local: keep quota importable without the CLI half

    pinned = codex_in_claude.pinned_codex_home()
    if pinned is None:
        return ""
    for label, home in homes.items():
        try:
            if pinned.expanduser().resolve() == home.expanduser().resolve():
                return label
        except OSError:  # pragma: no cover - resolve() fails only on exotic filesystems
            if str(pinned) == str(home):
                return label
    return ""


def select_codex_account(rows: list[ProviderQuota], pin_label: str) -> str:
    """The provider id of the Codex seat delegation should bill, or ``""``.

    Precedence (debate-settled): seats under a hold or any BLOCKED/DISABLED verdict
    are excluded FIRST; an eligible pin wins; otherwise the first eligible seat in
    team→private order. When nothing is eligible the answer is ``""`` — the executor
    (:func:`codex_in_claude._codex_home`) then falls back pin-else-default loudly,
    because a debate the user explicitly requested must still be able to run.
    """
    eligible = [row for row in rows if row.state not in (BLOCKED, DISABLED)]
    if pin_label:
        pinned = next((row for row in eligible if row.account == pin_label), None)
        if pinned is not None:
            return pinned.id
    return eligible[0].id if eligible else ""


def _copilot_quota(now: int, cooldowns: dict[str, dict]) -> ProviderQuota:
    """Resolve the GitHub Copilot seat.

    Precedence, strictest evidence first:

    1. An unexpired observed 429 → ``blocked``. The seat's own rejection outranks any
       billing snapshot, which lags by up to a day.
    2. A FRESH snapshot whose denominator came from the live seat entitlement
       (``quota_source == "api"``) → ``blocked`` or ``available`` by the meter.
    3. Anything else — a *guessed* ``copilot_credit_quota`` denominator, a stale
       snapshot, or no snapshot → ``unknown``. A guessed denominator can never establish
       exhaustion: the configured default has been observed to be 2x the real entitlement,
       which would report a dead seat as half-full.
    """
    if "copilot" in cooldowns:
        return _cooldown_quota("copilot", "copilot", cooldowns["copilot"])
    snap = usage.read_copilot_usage()
    if snap is None:
        return ProviderQuota(
            id="copilot", kind="copilot", state=UNKNOWN, reason="no usage snapshot", source="meter"
        )
    if snap.quota_source != "api":
        return ProviderQuota(
            id="copilot",
            kind="copilot",
            state=UNKNOWN,
            reason="denominator is a configured guess, not the seat entitlement",
            source="meter",
            captured_at=snap.captured_at,
        )
    if snap.captured_at + _COPILOT_STALE_AFTER_SEC < now:
        return ProviderQuota(
            id="copilot",
            kind="copilot",
            state=UNKNOWN,
            reason="meter snapshot stale",
            source="meter",
            captured_at=snap.captured_at,
        )
    used = snap.credits_used or snap.quantity
    quota = max(1, snap.credit_quota)
    pct = used / quota * 100.0
    window = WindowState(name="credits", used_pct=pct, resets_at=int(snap.premium_reset_at))
    if window.exhausted:
        return ProviderQuota(
            id="copilot",
            kind="copilot",
            state=BLOCKED,
            reason=f"AI credits {used:.0f}/{quota} ({pct:.0f}%)",
            source="meter",
            windows={"credits": window},
            blocked_by="credits",
            resets_at=int(snap.premium_reset_at),
            captured_at=snap.captured_at,
            risky=True,
        )
    return ProviderQuota(
        id="copilot",
        kind="copilot",
        state=AVAILABLE,
        source="meter",
        windows={"credits": window},
        captured_at=snap.captured_at,
        risky=window.risky,
    )


def _gemini_quota(cooldowns: dict[str, dict]) -> ProviderQuota:
    """The Gemini CLI rung — a capability state, not a quota one.

    The individual Gemini Code Assist tier this CLI authenticated against was retired
    (``IneligibleTierError``), so the rung cannot succeed at any hour of any day. That is
    ``disabled``, deliberately NOT ``blocked``: a block implies "retry after the reset",
    and there is no reset to wait for. Re-enable by configuration if the tier returns.
    """
    if "gemini" in cooldowns:
        return _cooldown_quota("gemini", "gemini", cooldowns["gemini"])
    return ProviderQuota(
        id="gemini",
        kind="gemini",
        state=DISABLED,
        reason="Gemini Code Assist individual tier retired (IneligibleTierError)",
        source="config",
    )


def snapshot(
    *, model: str = "", now: int | None = None, accounts: list[str] | None = None
) -> dict[str, Any]:
    """The full quota picture, cache-only. Never raises, never touches the network.

    *model* scopes which windows govern the Claude rungs (see :func:`_windows_for_model`);
    pass the model the caller actually intends to invoke. Claude accounts are ordered by
    DESCENDING urgency so the first usable one is the account whose allowance would
    otherwise expire soonest.
    """
    now = int(time.time()) if now is None else now
    cooldowns = read_cooldowns(now)
    labels = accounts if accounts is not None else list(config.claude_config_dirs())

    claude = [_claude_quota(label, model, now, cooldowns) for label in labels]
    # Usable accounts first, then by descending urgency (spend what resets soonest).
    claude.sort(key=lambda q: (q.state != AVAILABLE, -(q.urgency or 0.0)))

    homes = _canonical_codex_homes()
    codex_rows = _codex_quotas(now, cooldowns)
    pin_label = _codex_pin_label(homes)

    providers = [
        _copilot_quota(now, cooldowns),
        *codex_rows,
        *claude,
        _gemini_quota(cooldowns),
    ]
    best = next((q.id for q in claude if q.state == AVAILABLE), "")
    result: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "now": now,
        "model": model,
        # Deliberately NO cross-provider "best": ranking copilot against claude is a COST
        # decision owned by each caller's ladder config, not a quota fact this module can
        # know. Only Claude-account ranking is a quota-domain decision.
        "best_claude_account": best,
        # The Codex seat delegation (codex-debate / codex-implement / ccc's own codex
        # calls) should bill: pin > eligibility > team-first. "" = nothing eligible.
        "best_codex_account": select_codex_account(codex_rows, pin_label),
        "providers": [_provider_dict(q) for q in providers],
    }
    if pin_label:
        from . import codex_in_claude  # local: display metadata only

        result["codex_pin"] = {
            "account": pin_label,
            "until": str(codex_in_claude.load_config().get("codex_home_until") or ""),
        }
    return result


def _provider_dict(quota: ProviderQuota) -> dict[str, Any]:
    """Serialize one provider, dropping empty optional fields to keep the JSON readable."""
    data = asdict(quota)
    data["windows"] = {name: asdict(win) for name, win in quota.windows.items()}
    return {
        k: v for k, v in data.items() if v not in ("", 0, None, {}) or k in ("state", "id", "kind")
    }


def provider_state(pid: str, *, model: str = "", now: int | None = None) -> ProviderQuota | None:
    """One provider's resolved state by id, or ``None`` when it is not known."""
    snap = snapshot(model=model, now=now)
    for raw in snap["providers"]:
        if raw["id"] == pid:
            return _rehydrate(raw)
    return None


def _rehydrate(raw: dict[str, Any]) -> ProviderQuota:
    """Rebuild a :class:`ProviderQuota` from its serialized form (fields may be absent)."""
    windows = {name: WindowState(**win) for name, win in (raw.get("windows") or {}).items()}
    return ProviderQuota(
        id=raw["id"],
        kind=raw["kind"],
        state=raw["state"],
        reason=raw.get("reason", ""),
        source=raw.get("source", ""),
        windows=windows,
        blocked_by=raw.get("blocked_by", ""),
        resets_at=int(raw.get("resets_at", 0) or 0),
        captured_at=int(raw.get("captured_at", 0) or 0),
        risky=bool(raw.get("risky", False)),
        account=raw.get("account", ""),
        config_dir=raw.get("config_dir", ""),
        urgency=raw.get("urgency"),
        email=raw.get("email", ""),
        block_scope=raw.get("block_scope", ""),
    )


def main(argv: list[str] | None = None) -> int:
    """``./command_center/quota.py [args]`` — same report and exit codes as ``ccc quota``.

    Forwarded to the CLI rather than reimplemented: ``ccc quota``'s flags, table and
    ``--provider`` exit codes (0 available / 1 blocked / 2 unknown) are the contract other
    tools depend on, and a second renderer here would drift from it.
    """
    import sys

    from .cli import main as _cli_main

    return _cli_main(["quota", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
