#!/usr/bin/env python3
"""Fast, cache-first quota oracle — "which provider still has tokens, and until when?"

Every LLM-calling tool in this toolbox walks a *fallback ladder* of providers (Google
Antigravity → GitHub Copilot seat → Codex/ChatGPT seat → Claude subscription). Left alone,
each rung learns it is exhausted only by ATTEMPTING it and failing, which is the failure this module
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
* ``disabled``  — a capability fact, not a quota fact: a rung that cannot succeed at any
  hour of any day. It cannot be "waited out", so it is never given a reset time. No
  provider reports it today (the retired Gemini CLI row that did was dropped); the state
  remains for the next capability that is off rather than merely empty.

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


# pylint: disable=too-many-lines
# One oracle, one file, on purpose: the cooldown store, the per-provider resolvers and
# the Codex seat ranking share the WindowState/ProviderQuota vocabulary and the fail-open
# rules documented above, and splitting them would put the rules a reader must hold in
# their head in three places. It crossed 1000 lines with the `fill` seat policy (tp#212).
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import config, seat_rota, usage

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
#
# v2 stayed v2 on 2026-09-04: the seat-order fields are purely ADDITIVE. New keys:
# ``codex_next_attempt`` (the honest name — the runner hops on a run-time refusal, so
# this names the FIRST try; ``best_codex_account`` is now its alias),
# ``codex_seat_order`` (one ranked row per seat) and ``codex_seat_order_unknown``.
# ``codex_pin`` appears ONLY while the pin governs selection (see codex_seat_order).
#
# v2 stayed v2 again on 2026-09-09 (tp#212), same reason — every seat-routing field is
# ADDITIVE: top-level ``codex_seat_policy``, and per ``codex_seat_order`` row ``cohort``,
# ``measured``, ``probe``, ``rank_reason`` and ``malformed``. ``codex_pin`` now follows
# ``codex_in_claude.pin_active()``, which under the ``fill`` policy is true even with an
# explicit order configured.
#
# v2 stayed v2 on 2026-09-18, same reason — the rename is ADDITIVE: each provider row
# gains ``display`` (the human spelling: ``claude:work`` → ``claude-work``, ``codex`` →
# ``codex-work``) and, for a seat with one, ``command`` (the shell alias that opens it:
# ``cwork``/``cpriv``). ``id`` is untouched and stays the key for the cooldown store and
# every consumer's provider map; a consumer that ignores the new fields is unaffected.
#
# v2 stayed v2 once more on 2026-09-14 (the seat rota), same reason — everything it adds
# is ADDITIVE: per ``codex_seat_order`` row a ``rota`` object (``None`` when that seat is
# on no rota), the same object on the seat's ``providers`` entry, and a top-level
# ``codex_seat_rota_errors`` when an entry could not be read. A v2 consumer that ignores
# them still reads the row correctly: a seat blocked by the rota is a BLOCKED row with
# ``blocked_by="rota"``, which every existing consumer already renders as "skip it".
# v2 stayed v2 on 2026-09-18 for the Antigravity rungs too, same reason — they are NEW
# ROWS, not changed ones: ``providers`` gains ``agy`` (window ``gemini_week``) and
# ``agy:gpt`` (window ``claudegpt_week``), both kind ``agy``, both weekly-only —
# Antigravity has no session window. The two allowances are independent, hence two rows
# rather than one with two windows: each is blocked only by its own bucket. A consumer
# that looks its own provider ids up by name never sees them; one that iterates every row
# reads them with the same field set as any other.
# v2 stayed v2 on 2026-09-18 when the ``gemini`` row was REMOVED. That is a subtraction,
# not a rename, and the only honest way to read it: the Gemini CLI's individual tier was
# retired, so the row could never be anything but ``disabled`` — a permanent "skip me"
# occupying a line in every report and a key in every consumer's provider map. A consumer
# that looks it up now finds nothing, which is the same instruction (do not use it) with
# none of the noise. Restoring it is one entry in ``providers`` if the tier returns.
# v2 stayed v2 on 2026-09-19 for the OpenCode Zen rungs, same reason as the Antigravity
# pair — they are NEW ROWS: ``providers`` gains ``opencode:free`` and ``opencode:priv``,
# both kind ``opencode``, whose ``command`` fields are the shell aliases ``ofree`` and
# ``opriv``. Both are normally ``unknown``: Zen publishes no meter of any kind, and the
# one measurable quantity — what THIS machine spent, read from opencode's own sqlite
# store — is a spend figure, not an allowance. The priv row therefore carries a
# ``monthly`` window ONLY when the user has named a cap (``opencode_budget_usd``), and
# reaching that cap blocks it as LOCAL POLICY (``blocked_by="budget"``), not as provider
# exhaustion. A consumer that iterates rows reads them with the usual field set; one
# that looks providers up by name never sees them.
# v2 stayed v2 on 2026-09-20 for the Muse Code rung, same reason — ONE NEW ROW: ``providers``
# gains ``muse`` (kind ``muse``, no seat, so ``display`` is ``muse`` and there is no
# ``command``: the name IS the command). Its one window is ``budget`` — muse's own
# per-step token records priced at the provider's own catalog, summed over every session
# log on this machine, against ``muse_budget_usd`` — and reaching the cap blocks the row
# as LOCAL POLICY (``blocked_by="budget"``), like the Zen wallet. With the cap at 0 the
# row still appears, spend as prose, no bar.
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
# Same 24 h reasoning for the Antigravity meter: its windows are weekly, so the FIGURE
# ages slowly, but a reading older than a day was taken before a day's worth of calls
# could have been made and must not establish exhaustion on its own.
_AGY_STALE_AFTER_SEC = 24 * 3600

# Models whose usage is governed by the Fable-scoped weekly window. Any other model
# ignores ``fable_week`` entirely — the bug this mapping exists to prevent is treating a
# Fable-week-exhausted account as out of tokens for an Opus request.
_FABLE_MODEL_HINTS = ("fable",)

# Antigravity serves two model families out of two SEPARATE weekly allowances, so it is
# TWO rungs, not one provider with a model-scoped verdict. Each row owns one bucket:
#
#   agy      gemini-weekly  Gemini Flash / Pro          — what plain `agy` spends
#   agy-gpt  3p-weekly      Claude Opus/Sonnet, GPT-OSS — what `agy-gpt` spends
#
# One row per allowance is what a reader can act on: the report shows each bar beside the
# command that spends it, a refusal is recorded against the bucket that refused, and an
# exhausted Claude/GPT week cannot take the Gemini rung down with it. The previous shape —
# one `agy` row carrying both windows, the verdict scoped by guessing the family from the
# model name — could only ever answer for one of them at a time.
#
# The ids follow the seat convention (`codex:private` → `codex-priv`): the wire id is
# ``agy:gpt``, the human name ``agy-gpt``, and `display_id`/`canonical_id` join them.
_AGY_ROWS: tuple[tuple[str, str, str], ...] = (
    # (provider id, bucket id in the /usage payload, window name in the contract)
    ("agy", "gemini-weekly", "gemini_week"),
    ("agy:gpt", "3p-weekly", "claudegpt_week"),
)
# Bucket id → the window name the report and the JSON contract show. An id with no entry
# keeps its own spelling (dashes to underscores), so a third group Google adds still
# appears — on the `agy` row, until it is given a row of its own here.
_AGY_WINDOW_NAMES = {bucket: window for _pid, bucket, window in _AGY_ROWS}

# OpenCode Zen is two rungs for the same reason Antigravity is: one command spends
# money and the other does not, and nothing about them is shared but the binary.
#
#   opencode-free  ofree   a free Zen model      — costs nothing, has no published meter
#   opencode-priv  opriv   opencode's own default — costs money, has no published balance
#
# Zen answers 404 on every usage/billing endpoint and documents no rate limits, so
# NEITHER row can prove headroom and both stay ``unknown`` unless something authoritative
# says otherwise: a recorded refusal, or the user's own spending cap.
_OPENCODE_ROWS: tuple[tuple[str, str], ...] = (
    # (provider id, seat label)
    ("opencode:free", "free"),
    ("opencode:priv", "priv"),
)
# The spend reading is local and cheap, but a figure from yesterday was taken before a
# day of calls could have been made — same 24 h reasoning as Copilot and Antigravity.
_OPENCODE_STALE_AFTER_SEC = 24 * 3600

# Muse Code is ONE rung: a single login, a single price list, and every step it bills
# is in its own session logs on this machine. What `ccc quota` follows is the figure
# muse's `/usage` panel calls `Cost (USD, est.)` — the provider's token counts at the
# provider's list prices — summed over EVERY session log (hidden child sessions
# included, which the per-session panel omits) and measured against `muse_budget_usd`.
# The reading is local and complete for this machine; its 24 h staleness rule is the
# same as the other spend meters' — a figure older than that predates a day of calls.
_MUSE_STALE_AFTER_SEC = 24 * 3600

# ── The report's two bars ────────────────────────────────────────────────────
#
# `ccc quota`'s text report draws the SAME two bars the TUI usage cards draw — a session
# bar and a weekly one — for every row, whatever the provider. Each provider names its
# windows differently, so this is the map from "the slot a bar occupies" to "the window
# names that may fill it", most specific first; the FIRST window a row actually has wins.
#
# Two consequences worth stating, because they are choices and not accidents:
#
# Only the providers that really have TWO horizons are in these slots: the Claude and
# Codex seats, which meter a 5-hour session window alongside a weekly one. Copilot and
# both Antigravity rungs have a single allowance each and SPAN both columns instead —
# see :data:`BAR_SPAN_KINDS`.
#
# A slot a provider has no window for draws an EMPTY bar reading `0%`, not a dash: both
# say "nothing measured here", but the bar keeps the column's shape so the eye reads down
# a row of bars instead of down a row of holes.
#
# Windows drawn as a bar are omitted from the report's textual `windows` column, so a row
# states each figure once. Anything with no slot here (`fable_week`) still shows there,
# which is what keeps this map from hiding data.
BAR_SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("session", ("five_hour",)),
    ("week", ("seven_day",)),
)
# Provider KINDS that have one allowance rather than a session/week pair, and are drawn
# as a single bar across BOTH columns, filled by the first of `BAR_SPAN_WINDOWS` they
# carry. Copilot's credit budget is a month and Antigravity's two buckets are weeks;
# neither meters a session at all, so filing them under "week" and leaving "session"
# permanently empty described providers with two horizons when they have one — and cost
# every such row half its bar to say nothing. The row's `unblocks` column still carries
# the real reset date.
#
# Keyed on the KIND, not on which windows happen to be present, so a row with no figures
# at all (blocked by an observed 429 before any meter was read) still draws its one empty
# bar instead of briefly turning into a two-window provider.
BAR_SPAN_KINDS: tuple[str, ...] = ("copilot", "agy", "opencode", "muse")
BAR_SPAN_WINDOWS: tuple[str, ...] = ("credits", "gemini_week", "claudegpt_week", "wallet", "budget")
# What a SPANNING bar says when the provider has no window to draw at all. Without it
# such a row shows an empty bar embossed `0%`, which reads as "nothing spent" when the
# truth is "nothing is measured" — the opposite claim, and the one that would send a
# reader to a rung believing it was proven idle.
BAR_SPAN_EMPTY: dict[str, str] = {"opencode": "unmetered"}

# The word a window's bar is labelled with: WHAT PERIOD this allowance renews on. A bar
# without it is a percentage of an unnamed thing — "56 %" reads very differently against a
# month than against five hours. Every window that can fill a bar has an entry, and a name
# with none is simply left unlabelled rather than guessed at.
BAR_HORIZON: dict[str, str] = {
    "five_hour": "session",
    # Not a period: a prepaid wallet renews when money is added, never on a clock. The
    # word still belongs in the bar, because "37 %" of an unnamed thing is unreadable —
    # it just names a POT rather than a horizon.
    "wallet": "balance",
    # Also not a period: a spending cap the user named, against which every dollar this
    # machine's muse sessions cost is counted. Nothing renews it but a bigger number.
    "budget": "budget",
    "seven_day": "weekly",
    "gemini_week": "weekly",
    "claudegpt_week": "weekly",
    "credits": "monthly",
}

# The Fable weekly figure is only as fresh as the last successful OAuth fetch
# (``oauth_fetched_at``): statusline writes refresh ``captured_at`` while PRESERVING a
# stale Fable value, so judging ``fable_week`` by ``captured_at`` + 7d let a days-old
# figure govern verdicts as if live. Mirrors the card's own threshold
# (:data:`usage._FABLE_STALE_AFTER_SEC`).
_FABLE_EVIDENCE_STALE_SEC = usage._FABLE_STALE_AFTER_SEC  # noqa: SLF001

_COOLDOWNS_NAME = "cooldowns.json"
# One line per Codex seat: when a physical attempt was last made on it. Two jobs (plan
# D2/D7): the deterministic round-robin tiebreak inside a cohort (no new usage evidence
# arrives between two short runs — see :func:`rank_codex_seats`), and the once-a-day
# claim that lets ONE unmeasured seat be probed.
_SEAT_ATTEMPTS_NAME = "codex-seat-attempts.json"

# ── the ``fill`` policy's constants (plan D2, debate O1/O2/O5/O7) ────────────────────
# Two weekly resets this close together are "about equally urgent": the seats form ONE
# cohort and are filled equally rather than strictly ordered. 12 h is half a day — a
# gap larger than that makes the earlier seat meaningfully more urgent.
_FILL_COHORT_TOLERANCE_SEC = 12 * 3600
# A 5-hour window renewing within the hour with at least half of it unused is allowance
# that is about to evaporate: spend it first, inside its cohort.
_SESSION_SOON_SEC = 3600
_SESSION_UNUSED_MAX_PCT = 50.0
# Weekly-usage bucket width for "fill them equally": two seats within 5 % of each other
# are treated as equally used, so the round-robin tiebreak (not a 0.3 % difference)
# decides which one is billed next.
_FILL_BUCKET_PCT = 5.0
# An unmeasured seat is worth ONE read-only probe per day — enough to discover a fresh
# seat (the tp#212 trigger: a completely unused team seat was invisible), few enough
# that a permanently unmeasurable home cannot soak up every run.
_PROBE_INTERVAL_SEC = 24 * 3600
# A refusal stapled from a rollout is evidence with an expiry (plan D8, debate O12):
# past its exhausted window's reset it proves nothing, and with no window known at all
# it is re-checked after 5 h — the length of the shortest Codex window.
_REFUSAL_RECHECK_SEC = 5 * 3600

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


# ── Human-facing names ───────────────────────────────────────────────────────
#
# The ids above are this module's WIRE format and never change: consumers pin them
# (``ai.py``'s ``_ORACLE_IDS``), the cooldown store is keyed by them, and renaming them
# would orphan every recorded block. What a HUMAN reads is a different thing: the seats
# are opened from a shell as ``cwork``, ``codex-de``, ``codex-priv``, so a report that
# says ``claude:work`` / ``codex`` / ``codex:private`` makes the reader translate every
# row back into a command before they can act on it — and ``codex`` (the canonical team
# seat) does not even hint that it is the work login.
#
# :func:`display_id` is that translation (``<kind>-<seat>``, seat spelled the way the
# shell spells it), :func:`canonical_id` its exact inverse, and every id-taking flag
# (``-p``/``-m``/``-c``) accepts EITHER spelling — so nothing that already works breaks
# and nothing a human reads has to be translated. Round-trip is a test invariant.
_DISPLAY_SEAT = {"default": "work", "private": "priv"}
# The inverse, per kind: Codex's canonical team seat is labelled ``default`` while the
# work Claude seat really is labelled ``work``, so ``work`` un-maps differently per kind.
_CANONICAL_SEAT: dict[str, dict[str, str]] = {
    "claude": {"priv": "private"},
    "codex": {"work": "default", "priv": "private"},
    # Antigravity's second rung is a model-family "seat" (`agy:gpt` → `agy-gpt`); no
    # spelling differs between the two forms, so the map is empty and only its PRESENCE
    # matters — that is what makes `canonical_id` split the name at all.
    "agy": {},
    # OpenCode's seats spell the same in both forms (`opencode:free` <-> `opencode-free`);
    # only the PRESENCE of the kind matters, and that is what makes `canonical_id` split
    # the name at all.
    "opencode": {},
}

# The shell command that opens a rung, where the name does not already say it. Claude's
# seats follow a rule (`claude:work` -> `cwork`); OpenCode's are two fixed aliases, and
# a rule invented to cover two cases would just be a lookup table with extra steps.
_SEAT_COMMANDS = {"opencode:free": "ofree", "opencode:priv": "opriv"}


def display_id(pid: str) -> str:
    """Wire id → the name a human reads: ``claude:work`` → ``claude-work``,
    ``codex`` → ``codex-work``, ``codex:private`` → ``codex-priv``.

    Single-seat providers (``copilot``, ``gemini``) have no seat to name and pass through.
    """
    kind, _sep, seat = pid.partition(":")
    if kind == "codex" and not seat:
        seat = "default"  # the canonical team seat's implicit label
    if not seat:
        return pid
    return f"{kind}-{_DISPLAY_SEAT.get(seat, seat)}"


def canonical_id(name: str) -> str:
    """The inverse of :func:`display_id`; anything already canonical passes through.

    Lets ``ccc quota -c claude-work`` and ``ccc quota -c claude:work`` mean the same
    thing, so a user can copy the name straight off the report.
    """
    kind, sep, seat = name.partition("-")
    if not sep or kind not in _CANONICAL_SEAT:
        return name
    seat = _CANONICAL_SEAT[kind].get(seat, seat)
    if kind == "codex" and seat == "default":
        return "codex"
    return f"{kind}:{seat}"


def seat_color(pid: str, kind: str, account: str = "") -> str:
    """The hex accent a provider row is painted in — the SAME colour its TUI usage card
    is drawn in: gold for the private Claude seat, blue for the work one, OpenAI-green
    for Codex, violet for Copilot, the two Antigravity buckets in their own olive pair,
    Meta-magenta for Muse Code.

    Published on every ``-j`` provider row as ``color`` so that a consumer painting the
    same seat (``ai logs``, ``ai routing``) reads the value from here instead of keeping
    a copy of the palette: change an accent in :mod:`usage` and every surface follows.
    "" for a kind with no card (none today), which consumers leave unpainted.
    """
    if kind == "claude":
        return usage._CLAUDE_WORK_ACCENT if account == "work" else usage._CLAUDE_ACCENT  # noqa: SLF001
    if kind == "codex":
        return usage._CODEX_FILL  # noqa: SLF001
    if kind == "copilot":
        return usage._COPILOT_FILL  # noqa: SLF001
    if kind == "agy":
        return usage._AGY_GPT_ACCENT if pid == "agy:gpt" else usage._AGY_ACCENT  # noqa: SLF001
    if kind == "muse":
        return usage._MUSE_ACCENT  # noqa: SLF001
    return ""


def seat_command(pid: str) -> str:
    """The shell command that opens a session on this seat, when it is not the
    display name itself — ``claude:work`` → ``cwork``, ``claude:private`` → ``cpriv``.

    The Codex seats are named after their own commands (``codex-de`` IS the alias), so
    they return "" rather than repeating themselves. The commands themselves live in the
    user's shell configuration; this is only the naming convention they follow.
    """
    if fixed := _SEAT_COMMANDS.get(pid):
        return fixed
    kind, _sep, seat = pid.partition(":")
    if kind != "claude" or not seat:
        return ""
    return "c" + _DISPLAY_SEAT.get(seat, seat)


@dataclass
class ProviderQuota:
    """One provider (or one Claude account) resolved to a state, with its evidence."""

    # "copilot" | "codex[:private]" | "agy[:gpt]" | "opencode:<seat>" | "muse" | "claude:<account>"
    id: str
    kind: str  # "copilot" | "codex" | "agy" | "opencode" | "muse" | "claude"
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
    # Advisory prose about the seat that must NEVER change ``state``: an unproven
    # entitlement (``plan_type == "free"``), a renewal date that has passed. It is
    # rendered next to the row so a seat that is technically usable but suspicious is
    # visible, without a measurement doubt silently deleting a working rung.
    note: str = ""
    # The seat's newest usage snapshot carried a window whose duration could not be
    # determined. Routing IGNORES it (fail-open: an unmeasurable seat stays runnable);
    # :func:`codex_in_claude.seat_headroom` fails closed on it (plan D4).
    malformed: bool = False
    # Codex seats on a ``codex_seat_rota``: whose week it is, when it is ours again, and
    # — while it is somebody else's — the ``underlying`` verdict this wrapper replaced.
    # Empty for every seat that is on no rota (and dropped from the JSON by
    # :func:`_provider_dict`), so a machine without one sees exactly today's payload.
    rota: dict[str, Any] = field(default_factory=dict)


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


def _seat_attempts_path() -> Path:
    """Path of the per-seat attempt ledger (beside the cooldown store)."""
    return config.app_home() / _SEAT_ATTEMPTS_NAME


def _read_seat_attempts_unlocked() -> dict[str, int]:
    """The attempt map, or ``{}`` when absent/corrupt/foreign-shaped. Never raises."""
    try:
        raw = _seat_attempts_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    entries = data.get("attempts") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    out: dict[str, int] = {}
    for pid, value in entries.items():
        try:
            out[str(pid)] = int(value)
        except (TypeError, ValueError):
            continue  # one bad row must not blank the whole ledger
    return out


def read_seat_attempts() -> dict[str, int]:
    """``{provider id: last physical attempt, epoch seconds}``. Never raises.

    The ledger is advisory in both of its jobs (round-robin tiebreak, probe rate limit),
    so a missing or corrupt file is simply "nothing attempted yet" — losing it costs one
    extra probe, never a wrong verdict.
    """
    return _read_seat_attempts_unlocked()


def _write_seat_attempts(current: dict[str, int]) -> None:
    """Persist the attempt map (caller holds the lock)."""
    usage._atomic_write_json(  # noqa: SLF001
        _seat_attempts_path(), {"version": SCHEMA_VERSION, "attempts": current}
    )


def record_seat_attempt(pid: str, now: int | None = None) -> None:
    """Record that a physical attempt is being made on *pid*, now. No-op for ``""``.

    An empty pid is an unregistered ``$CODEX_HOME``: it has no row to rank and no probe
    to rate-limit, so recording it would only invent an id. Read-modify-write under the
    same :func:`usage._flock` as :func:`record_block` — two concurrent runners marking
    different seats must not drop one another's entries.
    """
    if not pid:
        return
    now = int(time.time()) if now is None else int(now)
    path = _seat_attempts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
            current = _read_seat_attempts_unlocked()
            current[pid] = now
            _write_seat_attempts(current)
    except OSError:
        pass  # the ledger is advisory; a read-only home must not fail a run


def claim_probe(pid: str, now: int | None = None, interval: int = _PROBE_INTERVAL_SEC) -> bool:
    """Atomically claim the once-per-*interval* probe of unmeasured seat *pid*.

    True only when the stored attempt is absent or older than ``now - interval``; the
    claim WRITES ``now``, so it is also the attempt record (plan D7, debate O9). Two
    runners racing for the same fresh seat therefore produce exactly one probe — the
    loser re-ranks without it instead of spending a second round trip on a seat nobody
    can measure yet.

    A crash between the claim and the launch postpones that seat's next probe by up to
    *interval*. That is the accepted trade: the alternative (claim on success) lets N
    concurrent runners all probe the same seat.
    """
    if not pid:
        return False
    now = int(time.time()) if now is None else int(now)
    path = _seat_attempts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with usage._flock(path.with_suffix(".lock")):  # noqa: SLF001
            current = _read_seat_attempts_unlocked()
            if int(current.get(pid, 0)) > now - int(interval):
                return False
            current[pid] = now
            _write_seat_attempts(current)
    except OSError:
        return False  # cannot claim ⇒ do not probe (another runner may hold the file)
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


def observed_block_superseded(
    entry: dict,
    snap: usage.Usage | None,
    windows: Iterable[WindowState],
    now: int,  # pylint: disable=unused-argument  # see the note below
) -> bool:
    """True when *entry*'s recorded refusal is disproved by a NEWER healthy reading.

    The bug this ends (2026-09-14): ``record_seat_refusal`` writes a cooldown entry whose
    ``blocked_until`` is the exhausted window's reset, and :func:`_codex_seat_quota`
    consulted the store FIRST — so the entry stood until its own deadline no matter what
    the seat's own usage said afterwards. A refusal observed on 09-11 therefore still
    reported ``unblocks in 18h`` on 09-14 while the seat's live reading said 0 % / 0 %
    with an empty ``blocked_reason``.

    Every condition is a deliberate narrowing:

    * never a ``hold`` — an administrative reservation is policy, and no measurement may
      lift it (the same rule :func:`record_block` and :func:`clear_block` already follow);
    * the scope must be EXACTLY ``"quota"`` — the only scope a rate-limit refusal writes.
      ``auth`` / ``entitlement`` blocks say something a usage reading cannot refute, and
      an operator's scope-less ``ccc quota -m`` stands until its own deadline;
    * the reading must EXIST, be well-formed and be newer than the refusal, and it must
      not itself carry a refusal (:attr:`usage.Usage.blocked`) — ``read_codex_usage``
      staples a rollout refusal newer than the reading, so refusal → success → refusal
      still resolves to blocked;
    * and its governing windows must fold to AVAILABLE. Stale-only, absent or malformed
      windows are UNKNOWN, and UNKNOWN is a measurement failure, not evidence.

    *now* is the instant the *windows* were resolved against; it is the caller's business
    (it decides freshness while BUILDING them, which is why this predicate needs no clock
    of its own) and stays in the signature so the four inputs of one verdict travel
    together.

    Pure: readers never write. The superseded entry stays in ``cooldowns.json`` until its
    own ``blocked_until``, so two processes can disagree about nothing — and the runner's
    next refusal, recorded with a NEWER ``observed_at`` than this reading, excludes the
    seat again (one healthy measurement buys at most one attempt).
    """
    if entry.get("kind") == KIND_HOLD:
        return False
    if entry.get("scope") != "quota":
        return False
    if snap is None or snap.malformed or snap.blocked:
        return False
    if snap.captured_at <= int(entry.get("observed_at", 0) or 0):
        return False
    return _verdict_from_windows(windows)[0] == AVAILABLE


def _cooldown_quota(
    pid: str, kind: str, entry: dict, windows: dict[str, WindowState] | None = None
) -> ProviderQuota:
    """Build a BLOCKED provider straight from a cooldown entry (rejection or hold).

    *windows* are the provider's MEASURED windows, when the caller has them. They do not
    enter the verdict — that is the entry's, which is the whole point of the store — but
    they are still the truth about the provider's meter, and a row that drops them lies by
    omission: ``ccc quota`` drew empty 0 % bars for a seat whose weekly window the TUI card
    beside it showed at 100 %, and a JSON consumer reading a blocked row could not see the
    meter at all. Every caller that has a snapshot now measures FIRST and passes it here.
    """
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
        windows=dict(windows or {}),
        blocked_by="hold" if is_hold else "observed-rejection",
        resets_at=int(entry.get("blocked_until", 0) or 0),
        captured_at=int(entry.get("observed_at", 0) or 0),
        risky=True,
        block_scope=str(entry.get("scope") or ("hold" if is_hold else "")),
    )


def _claude_windows(snap: usage.Usage, now: int) -> dict[str, WindowState]:
    """One Claude snapshot's windows, each aged against the evidence that produced it."""
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
    return windows


def _claude_quota(account: str, model: str, now: int, cooldowns: dict[str, dict]) -> ProviderQuota:
    """Resolve one Claude account against the windows that govern *model*."""
    pid = f"claude:{account}"
    config_dir = str(config.claude_config_dirs().get(account, ""))
    # Measured BEFORE the cooldown store is consulted, so a blocked row still carries the
    # account's real windows (see :func:`_cooldown_quota`). The verdict order is unchanged
    # — an entry still outranks the meter — only the evidence travels with it now.
    snap = usage.read_usage(account)
    windows = _claude_windows(snap, now) if snap is not None else {}
    if pid in cooldowns:
        quota = _cooldown_quota(pid, "claude", cooldowns[pid], windows)
        quota.account, quota.config_dir = account, config_dir
        return quota
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


def _subscription_card(label: str) -> str:
    """The ``subscription_ends`` card key a Codex seat *label* advertises its date on."""
    if label == "default":
        return "codex"
    if label == "private":
        return "codex_private"
    return f"codex_{label}"


def _codex_seat_note(label: str, live: usage.Usage | None, today: str = "") -> str:
    """Advisory prose for one seat — NEVER a state (see :attr:`ProviderQuota.note`).

    Two facts a technically-usable seat's windows cannot show: a ``free`` plan (the
    entitlement may not cover the request — that refusal arrives at run time as
    ``usage_not_included``) and a ``subscription_ends`` date that has passed.
    """
    parts: list[str] = []
    if live is not None and (live.plan_type or "").strip().lower() == "free":
        parts.append("plan free — entitlement unproven")
    # ISO-8601 dates compare correctly as strings, and config.parse_subscription_ends
    # has already rejected anything that is not a real day — no date parsing needed.
    ends = config.subscription_end_map().get(_subscription_card(label), "")
    if ends and ends != "auto" and ends < (today or time.strftime("%Y-%m-%d")):
        parts.append(f"renewal date {ends} passed")
    return " · ".join(parts)


def _codex_windows(snap: usage.Usage, now: int) -> dict[str, WindowState]:
    """*snap*'s two Codex windows, resolved to states (absent ones simply missing)."""
    windows: dict[str, WindowState] = {}
    for name, win, stale_after in (
        ("five_hour", snap.five_hour, _SESSION_STALE_AFTER_SEC),
        ("seven_day", snap.seven_day, _WEEK_STALE_AFTER_SEC),
    ):
        state = _window_state(name, win, snap.captured_at, now, stale_after)
        if state is not None:
            windows[name] = state
    return windows


def _seat_evidence(home: Path, now: int) -> tuple[usage.Usage | None, dict[str, WindowState], Any]:
    """``(snapshot, windows, live cache)`` for *home* — the seat's own evidence, read once.

    Read BEFORE the cooldown store is consulted (2026-09-14): a recorded refusal may be
    superseded by a newer healthy reading, and the only way to know is to have the reading
    in hand first. Never raises — a bad cache is a missing measurement, not a failed row.
    """
    try:
        live = usage.read_codex_live(home)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        live = None  # advisory only; a bad cache never fails the row
    try:
        snap = usage.read_codex_usage(now, home)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        snap = None
    return snap, (_codex_windows(snap, now) if snap is not None else {}), live


def _codex_seat_quota(
    pid: str, label: str, home: Path, now: int, cooldowns: dict[str, dict]
) -> ProviderQuota:
    """Resolve ONE Codex/ChatGPT seat: its own evidence, its cooldowns, then its rota."""
    return _apply_rota(_codex_seat_row(pid, label, home, now, cooldowns), label, now)


def _codex_seat_row(  # pylint: disable=too-many-return-statements,too-many-locals
    pid: str, label: str, home: Path, now: int, cooldowns: dict[str, dict]
) -> ProviderQuota:
    """The ORDINARY row for one seat — windows, refusals and the cooldown store.

    Order matters and is the 2026-09-14 fix: the seat's own usage is read FIRST, and a
    recorded ``quota`` refusal only stands while :func:`observed_block_superseded` says a
    newer healthy reading has not disproved it. The store used to be consulted before any
    measurement, which made an entry final until its own deadline — a refusal from Friday
    kept a seat out of the ladder all weekend while its live reading said 0 %.

    A seat we could not measure at all (no ``auth.json``, no snapshot) keeps its cooldown:
    a missing reading can never supersede anything.
    """
    email = ""
    try:
        email = usage.codex_account_email(home) or ""
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        email = ""  # identity is display metadata, never a reason to fail the row
    has_auth = (home.expanduser() / "auth.json").is_file()
    snap, windows, live = _seat_evidence(home, now) if has_auth else (None, {}, None)
    entry = cooldowns.get(pid)
    superseded = entry is not None and observed_block_superseded(entry, snap, windows.values(), now)
    if entry is not None and not superseded:
        quota = _cooldown_quota(pid, "codex", entry, windows)
        quota.account, quota.email = label, email
        return quota
    # No auth.json = no login here (or a keyring store this reader cannot see). UNKNOWN,
    # never BLOCKED: "we could not measure it" must not delete a rung — the run-time
    # refusal classifier is what turns a real auth failure into a block.
    if not has_auth:
        return ProviderQuota(
            id=pid,
            kind="codex",
            state=UNKNOWN,
            reason="no auth.json (login? keyring store?)",
            source="windows",
            account=label,
            email=email,
        )
    note = _codex_seat_note(label, live)
    free_plan = live is not None and (live.plan_type or "").strip().lower() == "free"
    if snap is None:
        return ProviderQuota(
            id=pid,
            kind="codex",
            state=UNKNOWN,
            reason="no usage snapshot",
            source="windows",
            account=label,
            email=email,
            risky=free_plan,
            note=note,
        )
    if superseded and entry is not None:
        # Say WHY the row disagrees with a cooldown entry a reader can still see in
        # cooldowns.json: the refusal is older than the measurement that disproved it.
        refusal_age = _compact_duration(now - int(entry.get("observed_at", 0) or 0))
        reading_age = _compact_duration(now - snap.captured_at)
        note = " · ".join(
            part
            for part in (
                note,
                f"refusal {refusal_age} old superseded by a reading {reading_age} old",
            )
            if part
        )
    if snap.blocked:
        # Codex is refusing calls. ``read_codex_usage`` has already pinned the window
        # that filled to 100%, so name it as the blocker and carry its reset — that is
        # when access returns, and it is what ``unblocks`` should show instead of "—".
        full = max(
            (state for state in windows.values() if state.used_pct >= _EXHAUSTED_PCT),
            key=lambda state: state.resets_at,
            default=None,
        )
        observed = snap.blocked_at or snap.captured_at
        # A refusal read out of a ROLLOUT file has no expiry of its own, so without this
        # it blocks the seat forever (plan D8, debate O12): an 8-day-old refusal whose
        # window has long since reset would keep a healthy paid seat out of the ladder.
        # Past the reset — or past 5 h when no window is known — it stops being evidence
        # and the seat falls through to whatever its windows say (UNKNOWN when they are
        # all stale, which is eligible and remeasurable).
        expired = (
            full.resets_at <= now if full is not None else observed + _REFUSAL_RECHECK_SEC < now
        )
        if not expired:
            return ProviderQuota(
                id=pid,
                kind="codex",
                state=BLOCKED,
                reason=snap.blocked_reason,
                source="refusal",
                windows=windows,
                blocked_by=full.name if full is not None else "refusal",
                resets_at=full.resets_at if full is not None else 0,
                captured_at=observed,
                account=label,
                email=snap.email or email,
                note=note,
                malformed=snap.malformed,
            )
        age = _compact_duration(now - observed) if observed else "unknown-age"
        note = " · ".join(part for part in (note, f"refusal {age} old — remeasure") if part)
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
        risky=risky or free_plan,
        account=label,
        email=snap.email or email,
        note=note,
        malformed=snap.malformed,
    )


# ── the weekly seat rota: a COMPUTED policy block, not a quota fact ─────────────────
# A seat shared with a colleague on alternating weeks cannot be reserved with a hold:
# somebody would have to re-arm it every Monday, and the week nobody does is the week we
# bill their seat. So the block is derived from ``codex_seat_rota`` + the clock on every
# resolution. Parse problems are reported ONCE per process (the rota is re-resolved
# several times per run), mirroring ``config.codex_seat_policy``'s fallback note.
_ROTA_WARNED = False


def _rota_specs() -> tuple[dict[str, seat_rota.RotaSpec], list[seat_rota.RotaError]]:
    """The configured rota, parsed. Never raises: a failed read is "no rota, one error"."""
    try:
        return seat_rota.parse_codex_seat_rota(config.codex_seat_rota())
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return {}, [seat_rota.RotaError(entry="", label="", error=f"unreadable: {exc}")]


def rota_errors(homes: dict[str, Path]) -> list[seat_rota.RotaError]:
    """Every unusable rota entry, a valid one naming no configured seat included.

    The second kind is reported and IGNORED (there is no seat to block), which is the one
    rota problem that is not fail-closed: a rota for a login this machine does not have
    can only be a leftover or a config shared between machines.
    """
    specs, errors = _rota_specs()
    raw = config.codex_seat_rota()
    for label in specs:
        if label in homes:
            continue
        entry = next((line for line in raw if line.partition("=")[0].strip() == label), label)
        errors.append(
            seat_rota.RotaError(
                entry=entry, label=label, error="no Codex seat with this label is configured"
            )
        )
    return errors


def _warn_rota_errors(errors: list[seat_rota.RotaError]) -> None:
    """Print each unusable entry once per process (never per resolution)."""
    global _ROTA_WARNED  # pylint: disable=global-statement
    if not errors or _ROTA_WARNED:
        return
    _ROTA_WARNED = True
    for err in errors:
        print(f"⚠️  codex_seat_rota {err.entry!r}: {err.error}", file=sys.stderr)


def _rota_payload(
    spec: seat_rota.RotaSpec | None, state: seat_rota.RotaState | None, me: str
) -> dict[str, Any]:
    """The ``rota`` object a row carries — every key always present, empty when unknown.

    The two ``*_label`` fields are PRE-RENDERED in the rota's own zone on purpose: every
    consumer (``ai routing``, the ``order`` table, a script) renders in its own process
    under its own ``TZ``, and a bare epoch re-interpreted locally prints a Sunday to
    anybody east of the entry's zone.
    """
    if spec is None or state is None:
        return {
            "holder": "",
            "mine": False,
            "me": me,
            "names": [],
            "anchor": "",
            "tz": "",
            "week_start": "",
            "week_end_exclusive": "",
            "label": "",
            "next_mine_at": 0,
            "next_mine_label": "",
            "next_holder": "",
            "next_other_at": 0,
            "next_other_label": "",
        }
    return {
        "holder": state.holder,
        "mine": state.mine,
        "me": me,
        "names": list(spec.names),
        "anchor": spec.anchor.isoformat(),
        "tz": spec.zone,
        "week_start": state.week_start.isoformat(),
        "week_end_exclusive": state.week_end_exclusive.isoformat(),
        "label": state.label,
        "next_mine_at": state.next_mine_at,
        "next_mine_label": state.next_mine_label,
        "next_holder": state.next_holder,
        "next_other_at": state.next_other_at,
        "next_other_label": state.next_other_label,
    }


def _rota_blocked(
    row: ProviderQuota, reason: str, payload: dict[str, Any], now: int
) -> ProviderQuota:
    """*row* wrapped in the rota's block, carrying the verdict it replaced.

    ``resets_at`` is ``max(next own Monday, the underlying block's own reset)`` (debate
    O10): a hold or an exhausted window that outlasts our next week is NOT promised away
    by the rota — the seat comes back when BOTH are over. The underlying verdict survives
    inside ``rota.underlying`` so a consumer can say "rota until Mon 21.9., then the hold
    until 23.9." instead of pretending the rota is the only reason.
    """
    underlying_resets = row.resets_at if row.state == BLOCKED else 0
    rota = {
        **payload,
        "underlying": {
            "state": row.state,
            "blocked_by": row.blocked_by,
            "reason": row.reason,
            "resets_at": row.resets_at,
            "resets_label": seat_rota.day_label(row.resets_at, str(payload.get("tz") or "")),
        },
    }
    return replace(
        row,
        state=BLOCKED,
        reason=reason,
        source="rota",
        blocked_by="rota",
        block_scope="rota",
        resets_at=max(int(payload.get("next_mine_at") or 0), underlying_resets),
        captured_at=now,
        rota=rota,
    )


@dataclass(frozen=True)
class RotaVerdict:
    """Whose week ONE seat is in, decided from ``codex_seat_rota`` plus the clock alone.

    The verdict half of :func:`_apply_rota`, lifted out so a consumer that has no
    :class:`ProviderQuota` to wrap can ask the same question and get the same answer —
    the TUI's usage cards collapse a seat that is not ours this week, and a second
    implementation of the fail-closed rules below would be a second chance to get them
    wrong. Config + arithmetic only: no home is read, no provider evidence consulted, so
    it is cheap enough for a render tick.
    """

    label: str  # the Codex seat label the verdict is about
    blocked: bool  # True ⇒ this seat is unusable this week
    reason: str  # why, in the words the quota row carries ("" when not blocked)
    holder: str  # whose week it is ("" when the rota could not be read at all)
    mine: bool  # … and whether that is us
    payload: dict[str, Any]  # the ``rota`` object a row/JSON consumer carries


def rota_verdict(  # pylint: disable=too-many-return-statements  # one per rota outcome
    label: str, now: int | None = None
) -> RotaVerdict | None:
    """The rota's verdict for seat *label*, or ``None`` when it is on no rota at all.

    FAIL CLOSED (debate O5): an entry naming a configured seat that ccc cannot read, or
    one whose names do not contain ``codex_seat_rota_me``, BLOCKS that seat. The
    alternative — ignoring the broken entry — resolves "we do not know whose week it is"
    to "ours", which is exactly the week we must not bill.
    """
    now = int(time.time()) if now is None else now
    specs, errors = _rota_specs()
    spec = specs.get(label)
    me = config.codex_seat_rota_me()
    if spec is None:
        broken = next((err for err in errors if err.label == label), None)
        if broken is None:
            return None  # this seat is on no rota at all — today's behaviour, unchanged
        return RotaVerdict(
            label=label,
            blocked=True,
            reason=f"rota: invalid entry ({broken.error})",
            holder="",
            mine=False,
            payload=_rota_payload(None, None, me),
        )
    known = seat_rota.valid_name(me) and me in spec.names
    # The schedule itself is fine even when ``me`` is not on it, so the holder and the
    # week ARE known; only "is it ours?" is not — and that question may never default to
    # yes. Resolving with ``me=""`` keeps the verdict informative while blocking it.
    state = _rota_state(spec, me if known else "", now)
    if state is None:
        # ``snapshot()`` never raises (module docstring), and an unresolvable rota is
        # still a rota: block rather than let the exception fail the seat open.
        return RotaVerdict(
            label=label,
            blocked=True,
            reason="rota: unresolvable entry",
            holder="",
            mine=False,
            payload=_rota_payload(None, None, me),
        )
    payload = _rota_payload(spec, state, me)
    if not known:
        names = ",".join(spec.names)
        return RotaVerdict(
            label=label,
            blocked=True,
            reason=f"rota: codex_seat_rota_me {me!r} is not one of {names}",
            holder=state.holder,
            mine=False,
            payload=payload,
        )
    return RotaVerdict(
        label=label,
        blocked=not state.mine,
        reason="" if state.mine else state.label,
        holder=state.holder,
        mine=state.mine,
        payload=payload,
    )


def _apply_rota(row: ProviderQuota, label: str, now: int) -> ProviderQuota:
    """Apply this seat's rota to its ordinary *row* — ours, somebody else's, or unusable.

    The verdict itself is :func:`rota_verdict` (which owns the fail-closed rules); this
    function only wraps *row* in it.

    A row with no provider id is an UNREGISTERED explicit ``$CODEX_HOME``: it has no
    label a rota could name (``_seat_candidate_for`` calls it ``explicit``), so it is
    deliberately exempt — the documented escape hatch.
    """
    if not row.id:
        return row
    verdict = rota_verdict(label, now)
    if verdict is None:
        return row
    if verdict.blocked:
        return _rota_blocked(row, verdict.reason, verdict.payload, now)
    row.rota = verdict.payload
    return row


def _rota_state(spec: seat_rota.RotaSpec, me: str, now: int) -> seat_rota.RotaState | None:
    """:func:`seat_rota.rota_state`, or ``None`` when the clock cannot be placed in the zone."""
    try:
        return seat_rota.rota_state(spec, me, now)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None


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


def resolve_seat_order(
    configured: list[str], homes: dict[str, Path]
) -> tuple[list[str], list[str]]:
    """``(order, unknown)`` — the configured seat order resolved against real *homes*.

    Pure. *order* is every configured label naming a real home (first occurrence wins),
    then every home NOT listed, in canonical order — a login the user forgot to rank is
    still tried, last, instead of vanishing. *unknown* is every configured label with no
    home: reported, never fatal (a seat can be unconfigured while its name stays listed).
    """
    order: list[str] = []
    unknown: list[str] = []
    for label in configured:
        if label in homes:
            if label not in order:
                order.append(label)
        elif label not in unknown:
            unknown.append(label)
    for label in homes:
        if label not in order:
            order.append(label)
    return order, unknown


def codex_seat_order_labels(homes: dict[str, Path]) -> list[str]:
    """The seat labels in the order every Codex consumer should TRY them."""
    return resolve_seat_order(config.codex_seat_order(), homes)[0]


@dataclass(frozen=True)
class SeatRank:
    """One eligible Codex seat with its place in the ranking and WHY it is there.

    ``cohort`` is 1-based and only set for a measured seat under the ``fill`` policy
    (the pin, the probe, an unmeasured seat and every seat under ``order`` have none).
    ``reason`` is rendered verbatim by ``codex-in-claude order`` and ``ccc quota -j``,
    so it must read as an explanation, not as a key.
    """

    row: ProviderQuota
    cohort: int | None
    reason: str
    measured: bool
    probe: bool = False


def _compact_duration(seconds: int) -> str:
    """``5d 12h`` / ``2h 5m`` / ``40m`` — at most two units, for a ``rank_reason``."""
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def _measured_week(row: ProviderQuota) -> WindowState | None:
    """The seat's FRESH weekly window, or ``None`` when it is unmeasured.

    "Measured" is deliberately the weekly window alone: the ``fill`` policy ranks by
    when the weekly allowance renews, so a seat with only a 5-hour reading cannot be
    placed in a cohort at all (plan D2).
    """
    win = row.windows.get("seven_day")
    return win if win is not None and not win.stale else None


def _session_soon(row: ProviderQuota, now: int) -> bool:
    """True when this seat's 5-hour allowance renews within the hour and is half unused.

    That allowance is about to be thrown away, so inside its cohort the seat is spent
    first (plan D2). A window that is already mostly used has nothing left to waste.
    """
    win = row.windows.get("five_hour")
    return (
        win is not None
        and not win.stale
        and win.resets_at - now <= _SESSION_SOON_SEC
        and win.used_pct <= _SESSION_UNUSED_MAX_PCT
    )


def _configured_rank(label: str, order: list[str]) -> int:
    """The seat's place in the configured order; unlisted seats sort after every listed one."""
    return order.index(label) if label in order else len(order)


def _probe_due(pid: str, attempts: dict[str, int], now: int) -> bool:
    """True when *pid* has had no physical attempt for :data:`_PROBE_INTERVAL_SEC`."""
    return int(attempts.get(pid, 0)) + _PROBE_INTERVAL_SEC < now


def _fill_reason(
    row: ProviderQuota, cohort: int, week: WindowState, now: int, last_attempt: int
) -> str:
    """The human sentence explaining why a measured seat sits where it does."""
    parts = [f"weekly resets in {_compact_duration(week.resets_at - now)}"]
    five = row.windows.get("five_hour")
    if _session_soon(row, now) and five is not None:
        parts.append(
            f"session renews in {_compact_duration(five.resets_at - now)}, "
            f"{100.0 - five.used_pct:.0f}% unused"
        )
    parts.append(f"{week.used_pct:.0f}% used")
    if last_attempt:
        parts.append(f"last attempt {_compact_duration(now - last_attempt)} ago")
    return f"cohort {cohort}: " + " · ".join(parts)


def _order_ranking(
    eligible: list[ProviderQuota], pin_label: str, order: list[str]
) -> list[SeatRank]:
    """The strict 2026-09-04 ranking, kept verbatim as the ``order`` policy.

    An ACTIVE account pin goes first, but only while NO explicit order is configured (an
    order is the stronger statement of intent, so a leftover pin must not silently
    reshuffle it — debate objection O2); a row whose label is not in *order* at all (a
    home that vanished between two reads) is kept, last, in row order rather than
    silently dropped. No probe: this policy promotes nothing on its own.
    """
    by_label: dict[str, ProviderQuota] = {}
    for row in eligible:
        by_label.setdefault(row.account, row)
    ranked: list[ProviderQuota] = []
    taken: set[int] = set()

    def _add(row: ProviderQuota) -> None:
        if id(row) not in taken:
            taken.add(id(row))
            ranked.append(row)

    if pin_label and not config.codex_seat_order():
        pinned = by_label.get(pin_label)
        if pinned is not None:
            _add(pinned)
    for label in order:
        listed = by_label.get(label)
        if listed is not None:
            _add(listed)
    for row in eligible:
        if row.account not in order:
            _add(row)
    return [
        SeatRank(row=row, cohort=None, reason="order", measured=_measured_week(row) is not None)
        for row in ranked
    ]


def _cohorts(
    measured: list[ProviderQuota], weeks: dict[int, WindowState]
) -> list[list[ProviderQuota]]:
    """Group seats whose weekly resets are within :data:`_FILL_COHORT_TOLERANCE_SEC`.

    Earliest reset first. A seat joins the cohort of its LEADER (not of its predecessor)
    so a long chain of 11-hour gaps cannot merge a whole week into one cohort.
    """
    groups: list[list[ProviderQuota]] = []
    leader = 0
    for row in sorted(measured, key=lambda r: weeks[id(r)].resets_at):
        reset = weeks[id(row)].resets_at
        if not groups or reset - leader > _FILL_COHORT_TOLERANCE_SEC:
            groups.append([row])
            leader = reset
        else:
            groups[-1].append(row)
    return groups


def _fill_ranking(  # pylint: disable=too-many-locals
    eligible: list[ProviderQuota],
    pin_label: str,
    order: list[str],
    *,
    now: int,
    attempts: dict[str, int],
    probe: bool,
) -> list[SeatRank]:
    """The ``fill`` routing rule, made concrete (plan D2, debate O1/O2/O5/O7).

    Precedence: an eligible active pin, then the ONE due probe, then the measured seats
    grouped into cohorts by weekly reset (earliest cohort first), then the unmeasured
    remainder in configured order.

    Cohorts are what makes "fill the seat that renews next" survive Codex's
    counterexample: a seat at 92 % resetting in 2 h and one at 0 % resetting in 20 h are
    NOT interchangeable — the first is about to throw its remainder away, so it leads
    even though it is the more-used seat. Within one cohort the seats are close enough
    in urgency that filling them equally is right, so the sort is
    ``(session renewing soon, 5 % usage bucket, oldest attempt, configured rank)``: the
    bucket keeps a 0.3 % difference from pinning every run onto one seat, and the
    attempt ledger then alternates deterministically when no new measurement has
    arrived between two short runs (debate O2).

    Deliberately absent: any projection of a PASSED reset (a reset that has gone by
    proves nothing about usage since), any ``subscription_ends`` term (advisory only),
    and any ``risky`` demotion — the write floor in the runner is the safety valve.
    """
    position = {id(row): index for index, row in enumerate(eligible)}

    def _configured(rows: list[ProviderQuota]) -> list[ProviderQuota]:
        return sorted(rows, key=lambda r: (_configured_rank(r.account, order), position[id(r)]))

    ranks: list[SeatRank] = []
    taken: set[int] = set()

    by_label: dict[str, ProviderQuota] = {}
    for row in eligible:
        by_label.setdefault(row.account, row)
    pinned = by_label.get(pin_label) if pin_label else None
    if pinned is not None:
        taken.add(id(pinned))
        ranks.append(
            SeatRank(
                row=pinned,
                cohort=None,
                reason="pin",
                measured=_measured_week(pinned) is not None,
            )
        )

    unmeasured = [row for row in eligible if id(row) not in taken and _measured_week(row) is None]
    if probe:
        due = next(
            (row for row in _configured(unmeasured) if _probe_due(row.id, attempts, now)), None
        )
        if due is not None:
            taken.add(id(due))
            ranks.append(
                SeatRank(
                    row=due,
                    cohort=None,
                    reason="probe: unmeasured, no attempt in 24h",
                    measured=False,
                    probe=True,
                )
            )

    weeks = {
        id(row): week
        for row in eligible
        if id(row) not in taken and (week := _measured_week(row)) is not None
    }
    measured = [row for row in eligible if id(row) in weeks]
    for index, group in enumerate(_cohorts(measured, weeks), 1):
        ordered = sorted(
            group,
            key=lambda r: (
                not _session_soon(r, now),
                int(weeks[id(r)].used_pct // _FILL_BUCKET_PCT),
                int(attempts.get(r.id, 0)),
                _configured_rank(r.account, order),
                position[id(r)],
            ),
        )
        for row in ordered:
            taken.add(id(row))
            ranks.append(
                SeatRank(
                    row=row,
                    cohort=index,
                    reason=_fill_reason(
                        row, index, weeks[id(row)], now, int(attempts.get(row.id, 0))
                    ),
                    measured=True,
                )
            )

    for row in _configured([r for r in eligible if id(r) not in taken]):
        ranks.append(SeatRank(row=row, cohort=None, reason="unmeasured", measured=False))
    return ranks


def rank_codex_seats(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    rows: list[ProviderQuota],
    pin_label: str,
    order: list[str],
    *,
    policy: str | None = None,
    now: int | None = None,
    attempts: dict[str, int] | None = None,
    probe: bool = True,
) -> list[SeatRank]:
    """The eligible seats, best first, each carrying WHY — the ONE ranking, explained.

    Eligible = not BLOCKED and not DISABLED (UNKNOWN stays runnable: fail-open; a seat
    we merely failed to measure must never be deleted). *policy* defaults to
    :func:`config.codex_seat_policy`, *now* to the clock and *attempts* to the ledger,
    so the whole function is PURE when a caller injects them — which is what makes the
    ranking testable without a filesystem.

    *probe* is False for write runs: promoting an unmeasured seat is a read-only
    experiment, and a write round on a seat that may refuse mid-edit is exactly the
    ``SEAT-REFUSED-MIDRUN`` review the runner exists to avoid.
    """
    policy = config.codex_seat_policy() if policy is None else policy
    now = int(time.time()) if now is None else int(now)
    attempts = read_seat_attempts() if attempts is None else attempts
    eligible = [row for row in rows if row.state not in (BLOCKED, DISABLED)]
    if policy == "order":
        return _order_ranking(eligible, pin_label, order)
    return _fill_ranking(eligible, pin_label, order, now=now, attempts=attempts, probe=probe)


def codex_seat_candidates(
    rows: list[ProviderQuota],
    pin_label: str,
    order: list[str],
    *,
    policy: str | None = None,
    now: int | None = None,
) -> list[ProviderQuota]:
    """The eligible seats in attempt order — :func:`rank_codex_seats` without the reasons."""
    return [rank.row for rank in rank_codex_seats(rows, pin_label, order, policy=policy, now=now)]


def select_codex_account(
    rows: list[ProviderQuota], pin_label: str, order: list[str] | None = None
) -> str:
    """The provider id of the Codex seat the NEXT attempt should bill, or ``""``.

    The first of :func:`codex_seat_candidates`; ``order=None`` ranks by the row order the
    caller handed us (the pre-order behaviour). ``""`` = nothing eligible, which since
    2026-09-04 is terminal for the executor too: :func:`codex_in_claude.run_with_fallback`
    starts NO process rather than call a seat the oracle just said is held.
    """
    ranking = [row.account for row in rows] if order is None else order
    candidates = codex_seat_candidates(rows, pin_label, ranking)
    return candidates[0].id if candidates else ""


def _copilot_window(snap: usage.CopilotUsage | None) -> dict[str, WindowState]:
    """Copilot's credit meter as a window — measurement only, no verdict.

    Built from whatever snapshot exists, INCLUDING one whose denominator is a configured
    guess or whose figures are stale, because this is what the seat's meter says and it is
    what the TUI card draws. Whether that reading may establish exhaustion is a separate
    question, answered by :func:`_copilot_quota` alone.
    """
    if snap is None:
        return {}
    used = snap.credits_used or snap.quantity
    quota = max(1, snap.credit_quota)
    return {
        "credits": WindowState(
            name="credits",
            used_pct=used / quota * 100.0,
            resets_at=int(snap.premium_reset_at),
            evidence_at=snap.captured_at,
        )
    }


def _copilot_quota(now: int, cooldowns: dict[str, dict]) -> ProviderQuota:
    """Resolve the GitHub Copilot seat.

    Precedence, strictest evidence first:

    1. An unexpired observed 429 -> ``blocked``. The seat's own rejection outranks any
       billing snapshot, which lags by up to a day.
    2. A FRESH snapshot whose denominator came from the live seat entitlement
       (``quota_source == "api"``) -> ``blocked`` or ``available`` by the meter.
    3. Anything else - a *guessed* ``copilot_credit_quota`` denominator, a stale
       snapshot, or no snapshot -> ``unknown``. A guessed denominator can never establish
       exhaustion: the configured default has been observed to be 2x the real entitlement,
       which would report a dead seat as half-full.

    Every one of those outcomes carries the meter's window when there is one
    (:func:`_copilot_window`). Only rule 2 lets it decide anything; the rest merely show
    it, so the row and the card can never disagree about what was measured.
    """
    snap = usage.read_copilot_usage()
    windows = _copilot_window(snap)
    if "copilot" in cooldowns:
        return _cooldown_quota("copilot", "copilot", cooldowns["copilot"], windows)
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
            windows=windows,
            captured_at=snap.captured_at,
        )
    if snap.captured_at + _COPILOT_STALE_AFTER_SEC < now:
        return ProviderQuota(
            id="copilot",
            kind="copilot",
            state=UNKNOWN,
            reason="meter snapshot stale",
            source="meter",
            windows=windows,
            captured_at=snap.captured_at,
        )
    window = windows["credits"]
    if window.exhausted:
        used = snap.credits_used or snap.quantity
        quota = max(1, snap.credit_quota)
        return ProviderQuota(
            id="copilot",
            kind="copilot",
            state=BLOCKED,
            reason=f"AI credits {used:.0f}/{quota} ({window.used_pct:.0f}%)",
            source="meter",
            windows=windows,
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
        windows=windows,
        captured_at=snap.captured_at,
        risky=window.risky,
    )


def _agy_window(snap: usage.AgyUsage | None, bucket_id: str, now: int) -> dict[str, WindowState]:
    """One Antigravity bucket as a window (measurement only, no verdict)."""
    if snap is None:
        return {}
    bucket = snap.bucket(bucket_id)
    if bucket is None:
        return {}
    name = _AGY_WINDOW_NAMES.get(bucket.id, bucket.id.replace("-", "_"))
    win = _window_state(
        name,
        usage.Window(used_percentage=bucket.used_percentage, resets_at=bucket.resets_at),
        snap.captured_at,
        now,
        _AGY_STALE_AFTER_SEC,
    )
    return {name: win} if win is not None else {}


def _agy_quotas(now: int, cooldowns: dict[str, dict]) -> list[ProviderQuota]:
    """One row per Antigravity weekly allowance — see :data:`_AGY_ROWS`.

    Both rows come from the SAME cached ``/usage`` snapshot (one account, one meter), but
    each carries only its own bucket and is blocked only by its own. That is the whole
    reason they are two rows: the allowances are independent, so a spent Claude/GPT week
    must not remove the Gemini rung, and a refusal from one must not be recorded against
    the other.
    """
    snap = usage.read_agy_usage()
    off = snap is None and not config.load_config().agy_usage
    rows: list[ProviderQuota] = []
    for pid, bucket_id, _name in _AGY_ROWS:
        windows = _agy_window(snap, bucket_id, now)
        if pid in cooldowns:
            rows.append(_cooldown_quota(pid, "agy", cooldowns[pid], windows))
            continue
        if not windows:
            rows.append(
                ProviderQuota(
                    id=pid,
                    kind="agy",
                    state=UNKNOWN,
                    reason="no usage snapshot" + (" (agy_usage is off)" if off else ""),
                    source="meter",
                    captured_at=snap.captured_at if snap is not None else 0,
                )
            )
            continue
        state, reason, blocked_by, resets_at, risky = _verdict_from_windows(windows.values())
        rows.append(
            ProviderQuota(
                id=pid,
                kind="agy",
                state=state,
                reason=reason,
                source="meter",
                windows=windows,
                blocked_by=blocked_by,
                resets_at=resets_at,
                captured_at=snap.captured_at if snap is not None else 0,
                risky=risky,
            )
        )
    return rows


def _opencode_money(amount: float) -> str:
    """``$0.24`` — a spend figure, always two decimals, always with its currency."""
    return f"${amount:,.2f}"


def _opencode_free_row(pid: str, snap: usage.OpencodeUsage | None, now: int) -> ProviderQuota:
    """The free rung, whose only evidence is whether Zen last SERVED a request.

    Zen publishes no meter for the free tier — the docs state no limits at all — so the
    honest question is not "how much is left" but "does it answer right now", and one
    free request answers it outright. A fresh success is the only thing in this module
    that can make an unmetered rung ``available``: it is not an inference, it is the
    provider doing the thing.

    A refusal is ``blocked`` with NO reset: the deadline exists (the TUI renders
    "Free usage exceeded … retrying in 9h 53m") but the CLI never writes it anywhere a
    script can read, so the row states the refusal and leaves the clock to a human
    (`ccc quota -m opencode-free -u <sec>` records what the TUI showed). Inventing a
    plausible-looking reset would be the one thing worse than admitting there is none.

    An old verdict is ``unknown``, not sticky: the free tier can flip within the hour.
    """
    ttl = float(config.load_config().opencode_probe_ttl_sec)
    probe = snap.probe if snap is not None else None
    replies = f"{snap.free_messages} replies here all-time" if snap is not None else ""
    if probe is None:
        return ProviderQuota(
            id=pid,
            kind="opencode",
            state=UNKNOWN,
            reason=f"free tier — no published meter; ask it with `ccc quota -P` ({replies})"
            if replies
            else "free tier — no published meter; ask it with `ccc quota -P`",
            source="meter",
            captured_at=snap.captured_at if snap is not None else 0,
            account="free",
        )
    age = usage._format_age(max(0, now - probe.at))  # noqa: SLF001
    if (probe.at + ttl) < now:
        return ProviderQuota(
            id=pid,
            kind="opencode",
            state=UNKNOWN,
            reason=(
                f"last probe {age} ago: {'served' if probe.ok else 'refused'} — "
                f"stale, the free tier turns over faster than that"
            ),
            source="probe",
            captured_at=probe.at,
            account="free",
        )
    if probe.ok:
        return ProviderQuota(
            id=pid,
            kind="opencode",
            state=AVAILABLE,
            reason="",
            source="probe",
            captured_at=probe.at,
            account="free",
            note=f"served a free request {age} ago",
        )
    return ProviderQuota(
        id=pid,
        kind="opencode",
        state=BLOCKED,
        reason=(
            f"{probe.detail or 'free tier refused'} (probed {age} ago) — Zen states no "
            f"reset; record the TUI's countdown with `ccc quota -m opencode-free -u SEC`"
        ),
        source="probe",
        blocked_by="free-tier",
        captured_at=probe.at,
        account="free",
        risky=True,
    )


def _opencode_priv_row(pid: str, snap: usage.OpencodeUsage | None, now: int) -> ProviderQuota:
    """The paid rung: a PREPAID WALLET, which is why it has no renewal at all.

    Zen bills pay-as-you-go against a balance you top up, and publishes no way to read
    that balance with an API key (four upstream requests open for one; `/zen/go/v1/usage`
    is Go-subscription only). So the figure is assembled from an anchor and a delta:
    the balance the user last read off the console (``ccc quota -C``) minus what this
    machine has spent since. Both halves are stated in the row, because the second is a
    LOWER BOUND — spend from another machine is invisible here, and on the store this was
    built against it accounted for about half the real burn.

    With no anchor the row falls back to all-time local spend against the top-up, which
    is a floor on what has been used and is labelled as one. With no ``opencode_credit_usd``
    at all there is no denominator and therefore no bar — only the spend, as prose.

    The state stays ``unknown`` until the wallet is provably empty: a balance
    reconstructed from a human reading plus an incomplete delta cannot prove headroom,
    and saying ``available`` from it would be a guess wearing a verdict's clothes.
    """
    wallet = float(config.load_config().opencode_credit_usd)
    if snap is None:
        return ProviderQuota(
            id=pid,
            kind="opencode",
            state=UNKNOWN,
            reason="opencode store unreadable (missing, locked or migrated)",
            source="meter",
            account="priv",
        )
    spent_total = _opencode_money(snap.paid_usd_total)
    if wallet <= 0:
        return ProviderQuota(
            id=pid,
            kind="opencode",
            state=UNKNOWN,
            reason=(
                f"{spent_total} spent here all-time — Zen publishes no balance; "
                f"set opencode_credit_usd and record yours with `ccc quota -C <usd>`"
            ),
            source="meter",
            captured_at=snap.captured_at,
            account="priv",
        )
    if snap.credit is not None:
        left = snap.credit.usd - snap.paid_usd_since_credit
        read_age = usage._format_age(max(0, now - snap.credit.at))  # noqa: SLF001
        detail = (
            f"≈{_opencode_money(left)} of {_opencode_money(wallet)} left — "
            f"{_opencode_money(snap.credit.usd)} read {read_age} ago "
            f"− {_opencode_money(snap.paid_usd_since_credit)} spent here since "
            f"(this machine only)"
        )
    else:
        left = wallet - snap.paid_usd_total
        detail = (
            f"≤{_opencode_money(left)} of {_opencode_money(wallet)} left — "
            f"{spent_total} spent here all-time, a LOWER bound; "
            f"anchor it with `ccc quota -C <usd>` from the console"
        )
    used_pct = max(0.0, min(100.0, (wallet - left) / wallet * 100.0))
    # `wallet`, not `credits`: Copilot's `credits` window renews monthly and the renew
    # field speaks for it. This one renews when money is added and never on a clock, so
    # it must not be mistaken for a period — see `_RENEW_WINDOWS`, which omits it.
    window = WindowState(
        name="wallet",
        used_pct=used_pct,
        resets_at=0,
        stale=(snap.captured_at + _OPENCODE_STALE_AFTER_SEC) < now,
        evidence_at=snap.captured_at,
    )
    empty = left <= 0 and not window.stale
    return ProviderQuota(
        id=pid,
        kind="opencode",
        state=BLOCKED if empty else UNKNOWN,
        reason=(f"wallet empty: {detail}" if empty else detail),
        source="meter",
        windows={"wallet": window},
        blocked_by="wallet" if empty else "",
        captured_at=snap.captured_at,
        risky=used_pct >= _RISKY_PCT,
        account="priv",
    )


def _opencode_quotas(now: int, cooldowns: dict[str, dict]) -> list[ProviderQuota]:
    """The two OpenCode Zen rungs — see :data:`_OPENCODE_ROWS`.

    One binary, two rungs, nothing shared: `ofree` spends no money and is metered only by
    asking it, `opriv` spends a prepaid wallet no API will report. A recorded cooldown
    outranks both readings — it is the only place a real deadline can come from.
    """
    snap = usage.read_opencode_usage(now)
    rows: list[ProviderQuota] = []
    for pid, seat in _OPENCODE_ROWS:
        row = (
            _opencode_free_row(pid, snap, now)
            if seat == "free"
            else _opencode_priv_row(pid, snap, now)
        )
        if pid in cooldowns:
            row = _cooldown_quota(pid, "opencode", cooldowns[pid], row.windows)
            row.account = seat
        rows.append(row)
    return rows


def _muse_quota(now: int, cooldowns: dict[str, dict]) -> ProviderQuota:
    """The Muse Code rung: muse's own ``Cost (USD, est.)`` against ``muse_budget_usd``.

    The figure is the one thing in this module that is both LOCAL and COMPLETE for this
    machine: muse records every model step's token counts itself and caches the
    provider's price list beside them (see :func:`usage.read_muse_usage`), so the sum is
    muse's own estimate, not ccc's — over every session log, child sessions included.
    That is why a fresh reading under the cap is ``available`` here where the Zen wallet
    (half of whose burn is invisible) stays ``unknown``: the meter is the provider's own
    counts at the provider's own prices, and the only thing the user supplies is the
    line to measure them against.

    What it still is NOT: a provider fact. The cap is local policy, so reaching it blocks
    with ``blocked_by="budget"`` and no reset — nothing renews a budget but a bigger
    number — and the row says ``est.`` because the prices are list prices, and ``here``
    because a session on another machine, or one run with ``--no-session-log``, leaves
    nothing to count. A step whose model no price list names is counted in tokens and
    NAMED in the reason, never priced as free.
    """
    pid, kind = "muse", "muse"
    row = _muse_measured(pid, kind, now)
    # A recorded refusal or hold outranks the meter — it is the only place a real
    # deadline can come from — but the measured window still rides along (see
    # :func:`_cooldown_quota`), so a blocked row does not draw an empty bar.
    if pid in cooldowns:
        return _cooldown_quota(pid, kind, cooldowns[pid], row.windows)
    return row


def _muse_measured(pid: str, kind: str, now: int) -> ProviderQuota:
    """The Muse row from the meter alone — see :func:`_muse_quota` for the verdict rules."""
    budget = float(config.load_config().muse_budget_usd)
    snap = usage.read_muse_usage(now)
    if snap is None:
        return ProviderQuota(
            id=pid,
            kind=kind,
            state=UNKNOWN,
            reason="no muse session logs on this machine (~/.local/share/muse/sessions)",
            source="meter",
        )
    spent = _opencode_money(snap.est_usd_total)
    tokens = f"{_muse_count(snap.input_tokens)} in / {_muse_count(snap.output_tokens)} out"
    models = ", ".join(snap.models[:2]) + (" …" if len(snap.models) > 2 else "")
    tail = f"{tokens} over {snap.steps} steps in {snap.sessions} sessions"
    if models:
        tail += f" · {models}"
    if snap.unpriced:
        named = ", ".join(f"{count} of {model}" for model, count in snap.unpriced.items())
        tail += f" · unpriced steps NOT in the sum: {named}"
    if budget <= 0:
        return ProviderQuota(
            id=pid,
            kind=kind,
            state=UNKNOWN,
            reason=f"{spent} spent here (est.) — {tail}; set muse_budget_usd for a bar",
            source="meter",
            captured_at=snap.captured_at,
        )
    used_pct = max(0.0, min(100.0, snap.est_usd_total / budget * 100.0))
    window = WindowState(
        name="budget",
        used_pct=used_pct,
        resets_at=0,
        stale=(snap.captured_at + _MUSE_STALE_AFTER_SEC) < now,
        evidence_at=snap.captured_at,
    )
    detail = f"{spent} of {_opencode_money(budget)} spent here (est.) — {tail}"
    if window.exhausted:
        return ProviderQuota(
            id=pid,
            kind=kind,
            state=BLOCKED,
            reason=f"budget reached: {detail}",
            source="meter",
            windows={"budget": window},
            blocked_by="budget",
            captured_at=snap.captured_at,
            risky=True,
        )
    return ProviderQuota(
        id=pid,
        kind=kind,
        state=UNKNOWN if window.stale else AVAILABLE,
        reason=(f"stale reading — {detail}" if window.stale else detail),
        source="meter",
        windows={"budget": window},
        captured_at=snap.captured_at,
        risky=window.risky,
    )


def _muse_count(tokens: int) -> str:
    """``61k`` / ``1.2M`` — a token count sized for one table cell."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}k"
    return str(tokens)


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
    configured = config.codex_seat_order()
    policy = config.codex_seat_policy()
    order, unknown_labels = resolve_seat_order(configured, homes)
    ranks = rank_codex_seats(codex_rows, pin_label, order, policy=policy, now=now)
    next_attempt = ranks[0].row.id if ranks else ""

    providers = [
        _copilot_quota(now, cooldowns),
        *codex_rows,
        *claude,
        *_agy_quotas(now, cooldowns),
        *_opencode_quotas(now, cooldowns),
        _muse_quota(now, cooldowns),
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
        # The Codex seat the next attempt bills. ``best_codex_account`` is the v2 name
        # kept for existing consumers; ``codex_next_attempt`` is the honest one — the
        # runner hops on a run-time refusal, so this is a first try, not a verdict.
        "best_codex_account": next_attempt,
        "codex_next_attempt": next_attempt,
        # How the ranking was produced — "fill" (weekly-reset-soonest first) or the
        # strict "order". Consumers render it so a surprising next attempt is legible.
        "codex_seat_policy": policy,
        "codex_seat_order": _seat_order_rows(codex_rows, order, ranks, pin_label),
        "providers": [_provider_dict(q) for q in providers],
    }
    if unknown_labels:
        result["codex_seat_order_unknown"] = unknown_labels
    # Rota entries ccc could not use. Reported in the payload AND once on stderr: an
    # entry naming a configured seat has already BLOCKED it (fail closed), so the
    # operator must be able to see why without reading the JSON.
    rota_problems = rota_errors(homes)
    if rota_problems:
        result["codex_seat_rota_errors"] = [asdict(err) for err in rota_problems]
        _warn_rota_errors(rota_problems)
    # Only an ACTIVE pin is reported: a pin that governs nothing (an explicit order under
    # the ``order`` policy, an unregistered path) advertised here would have every
    # consumer render a lie. Under ``fill`` a registered pin DOES govern, so it appears
    # even with an order configured — ``pin_active`` is the one authority (plan D3/D9).
    from . import codex_in_claude  # local: display metadata only

    if pin_label and codex_in_claude.pin_active():
        result["codex_pin"] = {
            "account": pin_label,
            "until": str(codex_in_claude.load_config().get("codex_home_until") or ""),
        }
    return result


def _seat_order_rows(
    rows: list[ProviderQuota],
    order: list[str],
    ranks: list[SeatRank],
    pin_label: str,
) -> list[dict[str, Any]]:
    """One ranked dict per seat label in *order* — the ``codex_seat_order`` payload.

    The row ORDER stays the configured one (it is the table every consumer renders);
    the ranking is carried by ``attempt_rank`` plus the ``fill`` fields ``cohort`` /
    ``measured`` / ``probe`` / ``rank_reason``, which are additive (plan D9).
    ``configured_rank`` is the seat's place in the resolved order (1-based);
    ``attempt_rank`` its place among the ELIGIBLE candidates, ``None`` when skipped.
    """
    by_label = {row.account: row for row in rows}
    by_id = {id(rank.row): (index, rank) for index, rank in enumerate(ranks, 1)}
    out: list[dict[str, Any]] = []
    for index, label in enumerate(order, 1):
        row = by_label.get(label)
        if row is None:
            continue
        attempt_rank, rank = by_id.get(id(row), (None, None))
        out.append(
            {
                "configured_rank": index,
                "attempt_rank": attempt_rank,
                "id": row.id,
                "label": label,
                "email": row.email,
                "state": row.state,
                "reason": row.reason,
                "blocked_by": row.blocked_by,
                "resets_at": row.resets_at,
                "windows": {
                    name: {"used_pct": win.used_pct, "resets_at": win.resets_at}
                    for name, win in row.windows.items()
                },
                "note": row.note,
                "pinned": label == pin_label,
                "cohort": rank.cohort if rank is not None else None,
                "measured": rank.measured if rank is not None else _measured_week(row) is not None,
                "probe": rank.probe if rank is not None else False,
                "rank_reason": rank.reason if rank is not None else "",
                "malformed": row.malformed,
                # ``None`` for a seat on no rota, so a consumer can test the key itself
                # instead of an empty-object convention (plan B, 2026-09-14).
                "rota": row.rota or None,
            }
        )
    return out


def _provider_dict(quota: ProviderQuota) -> dict[str, Any]:
    """Serialize one provider, dropping empty optional fields to keep the JSON readable."""
    data = asdict(quota)
    data["windows"] = {name: asdict(win) for name, win in quota.windows.items()}
    # ADDITIVE (2026-09-18, v2 stays v2): the human spelling of ``id`` and the shell
    # command that opens that seat, so a consumer rendering a report for a person does
    # not have to re-derive this module's naming rules and drift from them. ``id`` is
    # unchanged and remains the key for everything machine-facing.
    data["display"] = display_id(quota.id)
    if command := seat_command(quota.id):
        data["command"] = command
    # ADDITIVE (2026-09-19): the accent the report paints this row in, so `ai logs` /
    # `ai routing` colour the same seat identically without a second copy of the palette.
    if color := seat_color(quota.id, quota.kind, quota.account):
        data["color"] = color
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
        note=raw.get("note", ""),
        malformed=bool(raw.get("malformed", False)),
        # Without this a round-tripped row lost its rota, so ``ccc quota -p codex`` (which
        # goes through the serialized form) would report a rota block with no rota (O11).
        rota=dict(raw.get("rota") or {}),
    )


def main(argv: list[str] | None = None) -> int:
    """``./command_center/quota.py [args]`` — same report and exit codes as ``ccc quota``.

    Forwarded to the CLI rather than reimplemented: ``ccc quota``'s flags, table and
    ``--provider`` exit codes (0 available / 1 blocked / 2 unknown) are the contract other
    tools depend on, and a second renderer here would drift from it.
    """
    from .cli import main as _cli_main

    return _cli_main(["quota", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
